"""A fake model server, and a real sync through Cognee that calls it.

The server answers OpenAI's chat and embedding routes on 127.0.0.1, as
LiteLLM and the embedding server do on the host. `Answer` says how it
answers each extraction. It always answers Cognee's connection test, which
runs once in a process, before the first sync: so a later failure reaches
each item of a sync, as a key or a budget that runs out on the host does.

`ingest_through_cognee` runs `cognee_ingest`, the sync of the service, in a
process of its own: Cognee reads its settings once, at its first import."""

import json
import os
import re
import subprocess
import sys
import threading
from collections.abc import Generator
from contextlib import contextmanager
from enum import Enum
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import cast

from creche_memory.store import EMBEDDING_DIMENSIONS, EMBEDDING_MODEL

#: A note with this text gets an answer that fits no schema.
UNFIT = "unfit-note"

#: The system prompt of Cognee's connection test.
CONNECTION_TEST = 'Respond to me with the following string: \\"test\\"'

#: A made-up revision of the tokenizer in the test's cache.
TOKENIZER_REVISION = "0" * 40

#: LiteLLM's answer to a key that it does not know.
AUTH_ERROR = (401, "Authentication Error, Invalid proxy server token passed.", "auth_error")

#: LiteLLM's answer to a key whose budget is spent, in its own words.
BUDGET_ERROR = (
    400,
    "Budget has been exceeded! Current cost: 10.5, Max budget: 10.0",
    "budget_exceeded",
)


class Answer(Enum):
    #: An answer that fits the schema, except for a note with `UNFIT`.
    FIT = "fit"
    #: An auth error for each extraction.
    AUTH = "auth"
    #: A budget error for each extraction.
    BUDGET = "budget"


type Json = dict[str, object] | list[object] | str | int | bool | None


def _instance(schema: dict[str, object], defs: dict[str, object]) -> Json:
    """The smallest value that fits `schema`, with one item in each list."""
    ref = schema.get("$ref")
    if isinstance(ref, str):
        return _instance(_object(defs[ref.split("/")[-1]]), defs)

    choices = schema.get("anyOf")
    if choices is not None:
        options = [_object(one) for one in _list(choices)]
        return _instance(next(one for one in options if one.get("type") != "null"), defs)

    enum = schema.get("enum")
    if enum is not None:
        return cast(Json, _list(enum)[0])

    kind = schema.get("type", "object")
    if kind == "array":
        return [_instance(_object(schema.get("items", {"type": "string"})), defs)]
    if kind == "string":
        return "alpha"
    if kind in ("integer", "number"):
        return 1
    if kind == "boolean":
        return True

    properties = _object(schema.get("properties", {}))
    return {name: _instance(_object(value), defs) for name, value in properties.items()}


def _object(value: object) -> dict[str, object]:
    assert isinstance(value, dict)
    return cast(dict[str, object], value)


def _list(value: object) -> list[object]:
    assert isinstance(value, list)
    return cast(list[object], value)


def _schema(body: dict[str, object]) -> dict[str, object] | None:
    """The schema of the request: in `response_format`, or in the prompt of
    Cognee's JSON fallback."""
    form = _object(body.get("response_format") or {})
    if form.get("type") == "json_schema":
        return _object(_object(form["json_schema"])["schema"])

    for message in _list(body.get("messages") or []):
        found = re.search(r"```json\n(.*?)\n```", str(_object(message).get("content")), re.S)
        if found is not None:
            return _object(json.loads(found.group(1)))

    return None


def _content(body: dict[str, object]) -> str:
    if UNFIT in json.dumps(body.get("messages")):
        return "this is not JSON"

    schema = _schema(body)
    if schema is None:
        return "test"

    return json.dumps(_instance(schema, _object(schema.get("$defs", {}))))


class _Handler(BaseHTTPRequestHandler):
    answer = Answer.FIT

    def log_message(self, format: str, *args: object) -> None:
        return

    def _send(self, status: int, reply: object) -> None:
        raw = json.dumps(reply).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self) -> None:
        body = _object(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
        if self.path.endswith("/embeddings"):
            texts = body["input"]
            count = len(cast(list[object], texts)) if isinstance(texts, list) else 1
            data = [
                {
                    "object": "embedding",
                    "index": n,
                    "embedding": [0.01 * (n + 1)] * EMBEDDING_DIMENSIONS,
                }
                for n in range(count)
            ]
            usage = {"prompt_tokens": 1, "total_tokens": 1}
            self._send(
                200, {"object": "list", "data": data, "model": body["model"], "usage": usage}
            )
            return

        errors = {Answer.AUTH: AUTH_ERROR, Answer.BUDGET: BUDGET_ERROR}
        if self.answer in errors and CONNECTION_TEST not in json.dumps(body["messages"]):
            status, message, kind = errors[self.answer]
            self._send(
                status,
                {"error": {"message": message, "type": kind, "param": None, "code": str(status)}},
            )
            return

        message = {"role": "assistant", "content": _content(body)}
        self._send(
            200,
            {
                "id": "fake",
                "object": "chat.completion",
                "created": 0,
                "model": body["model"],
                "choices": [{"index": 0, "finish_reason": "stop", "message": message}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        )


@contextmanager
def fake_model(answer: Answer) -> Generator[str]:
    """The base URL of a fake model server that answers as `answer` says."""
    handler = type("Handler", (_Handler,), {"answer": answer})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


#: A child that runs one sync of `sys.argv[1]/vault` through `cognee_ingest`,
#: against the model server at `sys.argv[2]`. It prints the count of failed
#: items, or the type of the error and the cause that Cognee recorded. Then
#: it prints the notes that the search of the service finds.
_INGEST = """
import asyncio, sys
from pathlib import Path
from typing import cast

from creche_memory.store import EMBEDDING_DIMENSIONS, EMBEDDING_MODEL
from creche_memory.store import StoreSettings, configure

root, url = Path(sys.argv[1]), sys.argv[2]
configure(StoreSettings(root / "store", url, "sk-test", url, root / "hf"))

from tenacity import stop_after_attempt
from cognee.infrastructure.llm.structured_output_framework.litellm_native.native_adapter import (
    NativeLiteLLMAdapter,
)
from creche_memory.cognee_search import cognee_backend, cognee_paths
from creche_memory.sync import cognee_ingest, ingest_folders

# Cognee tries a failed extraction again for at least 240 s. One try is
# enough here: the fake model gives the same answer each time.
NativeLiteLLMAdapter.acreate_structured_output.retry.stop = stop_after_attempt(1)

async def main():
    vault = root / "vault"
    try:
        print("failed:", await cognee_ingest(vault)(ingest_folders(vault)), flush=True)
    except Exception as error:
        print("raised:", type(error).__name__, getattr(error, "error_class", None), flush=True)
    _, hits = await cognee_backend("alpha", 10)
    paths = await cognee_paths()
    print("found:", ",".join(sorted({paths[hit.document_id] for hit in hits})), flush=True)

asyncio.run(main())
"""


def cache_tokenizer(cache: Path) -> None:
    """A tokenizer for `EMBEDDING_MODEL` in the Hugging Face cache `cache`,
    as the operator copies the real one to the host. Each word is one token.
    Without it, Cognee downloads the encoding of TikToken."""
    from tokenizers import Tokenizer, models, pre_tokenizers

    repo = cache / f"models--{EMBEDDING_MODEL.replace('/', '--')}"
    snapshot = repo / "snapshots" / TOKENIZER_REVISION
    snapshot.mkdir(parents=True)
    (repo / "refs").mkdir()
    (repo / "refs" / "main").write_text(TOKENIZER_REVISION)

    tokenizer = Tokenizer(models.WordLevel({"[UNK]": 0}, unk_token="[UNK]"))
    tokenizer.pre_tokenizer = pre_tokenizers.Whitespace()  # pyright: ignore[reportAttributeAccessIssue]
    tokenizer.save(str(snapshot / "tokenizer.json"))  # pyright: ignore[reportUnknownMemberType]


def ingest_through_cognee(root: Path, answer: Answer) -> list[str]:
    """The lines that `_INGEST` prints, for the vault and the store under
    `root`, with a model that answers as `answer` says."""
    cache_tokenizer(root / "hf")
    env = {**os.environ, "NO_COLOR": "1", "HOME": str(root)}
    with fake_model(answer) as url:
        done = subprocess.run(
            [sys.executable, "-c", _INGEST, str(root), url],
            env=env,
            capture_output=True,
            text=True,
            timeout=300,
            cwd=root,
        )

    assert done.returncode == 0, done.stderr[-3000:]

    marks = ("failed:", "raised:", "found:")

    return [line for line in done.stdout.splitlines() if line.startswith(marks)]
