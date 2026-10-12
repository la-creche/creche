"""index-scope <scope-path> <index-dir>: build/update one scope's index against TEI.

Runs inside a per-scope sandbox whose only egress is TEI — but is
equally happy on a host for tests. TEI preflight embeds a canary before any
work; a scope with an unreachable embedder is left untouched."""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import httpx

from . import strictjson
from .library import DIMS, EMBED_MAX_BATCH, PROFILES, Corpus, index_scope

log = logging.getLogger("library")

#: An explicit TEI base URL. It wins over the address below.
TEI_URL_ENV = "TEI_URL"

#: The site's LAN address, baked into the image at build time
#: (`Dockerfile`, `bin/provision-library.sh`): a sandbox has no site file.
LAN_ADDRESS_ENV = "AGENT_LAN_ADDRESS"

#: TEI's port on that address (`infra/compose.yaml`).
TEI_PORT = 8085

#: The most bytes of one answer of TEI: 16 MiB.
ANSWER_MAX_BYTES = 16_777_216

#: The name of an answer of TEI in a log line.
ANSWER_SURFACE = "embedding.answer"


class ConfigError(Exception):
    """The environment names no TEI. The message says which variable to set."""


def tei_url(environ: Mapping[str, str]) -> str:
    """`TEI_URL`, else TEI on the LAN address. No default: a default would
    be somebody's host."""
    explicit = environ.get(TEI_URL_ENV, "").strip()
    if explicit:
        return explicit

    address = environ.get(LAN_ADDRESS_ENV, "").strip()
    if not address:
        raise ConfigError(f"set {TEI_URL_ENV} or {LAN_ADDRESS_ENV}")

    return f"http://{address}:{TEI_PORT}"


# CONTRACT-QUESTION: `rust/AGENTS.md`, "JSON", says that a service records a
# notice for the operator when the strict reader refuses a text. It does not
# say what a program in a sandbox does. Reading taken: the log line below is
# the one report of a refused answer. A sandbox of the index builder has no
# state root and no hook, so this program can write no notice file. A notice
# from this program costs a path from the sandbox to the host.
def _answer(response: httpx.Response) -> Any:
    """The value of one answer of TEI, read as strict JSON.

    For an answer that is not strict, the function writes one log line with
    the rule that the answer breaks. The refusal then leaves the function.
    In the preflight, `main` ends the run for it. In the walk, `index_scope`
    reports it as the error of one file. The line and the refusal hold no
    byte of the answer.

    The value has the type `Any`: each caller reads the form that it needs.
    """
    try:
        return strictjson.loads(response.content, cap=ANSWER_MAX_BYTES)
    except strictjson.NotStrictJson as refusal:
        log.error("json_not_strict %s %s", ANSWER_SURFACE, refusal.rule.value)
        raise


class TeiEmbedder:
    def __init__(self, url: str) -> None:
        self._url = url.rstrip("/")
        self._client = httpx.Client(timeout=60)
        info = self._client.get(f"{self._url}/info")
        info.raise_for_status()
        self.model = str(_answer(info).get("model_id", "unknown"))

    def embed(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for start in range(0, len(texts), EMBED_MAX_BATCH):
            resp = self._client.post(
                f"{self._url}/embed", json={"inputs": texts[start : start + EMBED_MAX_BATCH]}
            )
            resp.raise_for_status()
            vectors.extend(_answer(resp))
        return vectors


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    if len(sys.argv) not in (3, 4):
        print("usage: index-scope <scope-path> <index-dir> [vault|code]", file=sys.stderr)
        return 2
    scope = Path(sys.argv[1])
    index_dir = Path(sys.argv[2])
    # Profile defaults to vault so existing units keep their two-arg call.
    try:
        profile = PROFILES[Corpus(sys.argv[3] if len(sys.argv) == 4 else Corpus.VAULT)]
    except ValueError:
        print(f"unknown profile {sys.argv[3]!r}; use vault or code", file=sys.stderr)
        return 2
    if not scope.is_dir():
        print(f"not a directory: {scope}", file=sys.stderr)
        return 2
    try:
        url = tei_url(os.environ)
    except ConfigError as exc:
        # `EX_CONFIG` is status 78: the status of a config that is not valid.
        print(f"no TEI: {exc}", file=sys.stderr)
        return os.EX_CONFIG

    try:
        embedder = TeiEmbedder(url)
        canary = embedder.embed(["canary"])
        if len(canary[0]) != DIMS:
            print(f"TEI returned {len(canary[0])} dims, expected {DIMS}", file=sys.stderr)
            return 2
    except Exception as exc:
        print(f"TEI preflight failed ({url}): {exc}", file=sys.stderr)
        return 2
    report = index_scope(scope, embedder, index_dir, profile=profile)
    print(report.render())
    return 0


if __name__ == "__main__":
    sys.exit(main())
