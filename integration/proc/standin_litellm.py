"""A stand-in for the key API of LiteLLM, over loopback HTTP.

    standin_litellm.py <state directory> <port>

No test may mint a key at a model provider. `caregiver` still makes these
four requests, and this program answers each one:

    POST /key/generate   the master key as the bearer   a new key for an alias
    POST /key/update     the master key as the bearer   new models and budget
    POST /key/delete     the master key as the bearer   delete by alias
    GET  /key/info       the key itself as the bearer   spend and budget

Every fact is a file under the state directory:

    master.key     the bearer that the three POST requests need
    keys.json      each alias with its key, its models, its budget, its spend
    calls.jsonl    one line for each request, in arrival order
    tune/          what a test writes to change one answer

Three things here copy what the real service does, or take the strict reading
where no probe of the real service exists:

1. A key starts with `sk-`.
2. One alias holds one key. A second `generate` for a live alias is refused.
3. A request with another bearer gets 401.

A line of `calls.jsonl` names who asked and holds no bearer. A key in a
request body is written as the alias that owns it. A `GET /` leaves no line:
the harness sends one to learn that this program listens.

A test tunes one answer with a file under `tune/`:

    tune/fail-<generate|update|delete|info>   that request answers 500
    tune/spend-<alias>                        the spend that `info` reports
"""

from __future__ import annotations

import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Final, cast

LOOPBACK: Final = "127.0.0.1"

GENERATE_PATH: Final = "/key/generate"
UPDATE_PATH: Final = "/key/update"
DELETE_PATH: Final = "/key/delete"
INFO_PATH: Final = "/key/info"

#: What the harness asks to learn that this program listens.
PROBE_PATH: Final = "/"

MASTER_FILE: Final = "master.key"
KEYS_FILE: Final = "keys.json"
CALLS_FILE: Final = "calls.jsonl"
TUNE_DIR: Final = "tune"

KEY_PREFIX: Final = "sk-standin-"
BEARER: Final = "Bearer "

#: Who sent a request, as `calls.jsonl` names it.
AS_MASTER: Final = "master"
AS_KEY: Final = "key"
AS_OTHER: Final = "other"

HTTP_OK: Final = 200
HTTP_BAD_REQUEST: Final = 400
HTTP_UNAUTHORIZED: Final = 401
HTTP_NOT_FOUND: Final = 404
HTTP_FAILED: Final = 500

_MAX_BODY: Final = 65_536

Answer = tuple[int, dict[str, Any]]


class KeyStore:
    """The keys and the call log, behind one lock. Each change is one file write."""

    def __init__(self, state: Path) -> None:
        self._state = state
        self._lock = threading.Lock()
        self._master = (state / MASTER_FILE).read_text(encoding="utf-8").strip()
        self._minted = 0

    def answer(self, method: str, path: str, bearer: str, body: dict[str, Any]) -> Answer:
        """Record one request and answer it."""
        if (method, path) == ("GET", PROBE_PATH):
            return HTTP_NOT_FOUND, {"error": "no such route"}

        with self._lock:
            keys = self._read_keys()
            sender = self._sender(bearer, keys)
            self._record(method, path, sender, _without_key(body, keys))

            return self._decide(method, path, sender, bearer, body, keys)

    def _decide(
        self,
        method: str,
        path: str,
        sender: str,
        bearer: str,
        body: dict[str, Any],
        keys: dict[str, dict[str, Any]],
    ) -> Answer:
        verb = path.rpartition("/")[2]

        if (method, path) == ("GET", INFO_PATH):
            return self._info(sender, bearer, keys)

        if method != "POST" or path not in (GENERATE_PATH, UPDATE_PATH, DELETE_PATH):
            return HTTP_NOT_FOUND, {"error": "no such route"}

        if sender != AS_MASTER:
            return HTTP_UNAUTHORIZED, {"error": "the master key is needed"}

        if self._tuned_to_fail(verb):
            return HTTP_FAILED, {"error": f"{verb} failed"}

        if path == GENERATE_PATH:
            return self._generate(body, keys)

        if path == UPDATE_PATH:
            return self._update(body, keys)

        return self._delete(body, keys)

    def _generate(self, body: dict[str, Any], keys: dict[str, dict[str, Any]]) -> Answer:
        alias = str(body.get("key_alias", ""))

        if not alias or alias in keys:
            return HTTP_BAD_REQUEST, {"error": "the alias is empty or holds a key"}

        self._minted += 1
        key = f"{KEY_PREFIX}{alias}-{self._minted}-{os.urandom(8).hex()}"
        keys[alias] = {
            "key": key,
            "models": body.get("models", []),
            "max_budget": body.get("max_budget"),
            "budget_duration": body.get("budget_duration"),
        }
        self._write_keys(keys)

        return HTTP_OK, {"key": key, "key_alias": alias}

    def _update(self, body: dict[str, Any], keys: dict[str, dict[str, Any]]) -> Answer:
        alias = _alias_of(str(body.get("key", "")), keys)

        if alias is None:
            return HTTP_NOT_FOUND, {"error": "no such key"}

        for field in ("models", "max_budget", "budget_duration"):
            if field in body:
                keys[alias][field] = body[field]

        self._write_keys(keys)

        return HTTP_OK, {"key_alias": alias}

    def _delete(self, body: dict[str, Any], keys: dict[str, dict[str, Any]]) -> Answer:
        aliases: object = body.get("key_aliases")

        if not isinstance(aliases, list):
            return HTTP_BAD_REQUEST, {"error": "key_aliases is not a list"}

        named = [str(alias) for alias in cast("list[object]", aliases)]
        deleted = [alias for alias in named if keys.pop(alias, None) is not None]
        self._write_keys(keys)

        return HTTP_OK, {"deleted_keys": deleted}

    def _info(self, sender: str, bearer: str, keys: dict[str, dict[str, Any]]) -> Answer:
        alias = _alias_of(bearer, keys)

        if sender != AS_KEY or alias is None:
            return HTTP_UNAUTHORIZED, {"error": "the key itself is needed"}

        if self._tuned_to_fail("info"):
            return HTTP_FAILED, {"error": "info failed"}

        info = {
            "key_alias": alias,
            "spend": self._tuned_spend(alias),
            "max_budget": keys[alias]["max_budget"],
        }

        return HTTP_OK, {"info": info}

    def _sender(self, bearer: str, keys: dict[str, dict[str, Any]]) -> str:
        if bearer and bearer == self._master:
            return AS_MASTER

        if _alias_of(bearer, keys) is not None:
            return AS_KEY

        return AS_OTHER

    def _tuned_to_fail(self, verb: str) -> bool:
        return (self._state / TUNE_DIR / f"fail-{verb}").exists()

    def _tuned_spend(self, alias: str) -> float:
        try:
            return float((self._state / TUNE_DIR / f"spend-{alias}").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return 0.0

    def _read_keys(self) -> dict[str, dict[str, Any]]:
        try:
            keys: dict[str, dict[str, Any]] = json.loads(
                (self._state / KEYS_FILE).read_text(encoding="utf-8")
            )
        except FileNotFoundError:
            return {}

        return keys

    def _write_keys(self, keys: dict[str, dict[str, Any]]) -> None:
        path = self._state / KEYS_FILE
        temp = path.with_name(f".{path.name}.tmp")
        temp.write_text(json.dumps(keys) + "\n", encoding="utf-8")
        temp.replace(path)

    def _record(self, method: str, path: str, sender: str, body: dict[str, Any]) -> None:
        line = json.dumps({"method": method, "path": path, "as": sender, "body": body})

        with (self._state / CALLS_FILE).open("a", encoding="utf-8") as calls:
            calls.write(line + "\n")


def _alias_of(key: str, keys: dict[str, dict[str, Any]]) -> str | None:
    """The alias that owns a key, or None for a key that no alias owns."""
    if not key:
        return None

    return next((alias for alias, entry in keys.items() if entry["key"] == key), None)


def _without_key(body: dict[str, Any], keys: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """The body for the call log: the alias in the place of a key."""
    if "key" not in body:
        return body

    alias = _alias_of(str(body["key"]), keys)

    return {**body, "key": f"<the key of {alias}>" if alias else "<a key that no alias owns>"}


def _handler_for(store: KeyStore) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self._answer("GET", {})

        def do_POST(self) -> None:
            self._answer("POST", self._body())

        def log_message(self, format: str, *args: object) -> None:
            """Write no access line. `calls.jsonl` is the record."""

        def _body(self) -> dict[str, Any]:
            length = min(int(self.headers.get("Content-Length") or 0), _MAX_BODY)

            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                return {}

            return body if isinstance(body, dict) else {}  # pyright: ignore[reportUnknownVariableType]

        def _answer(self, method: str, body: dict[str, Any]) -> None:
            bearer = (self.headers.get("Authorization") or "").removeprefix(BEARER)
            status, answer = store.answer(method, self.path, bearer, body)
            raw = json.dumps(answer).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    return Handler


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        sys.stderr.write("usage: standin_litellm.py <state directory> <port>\n")

        return 64

    store = KeyStore(Path(argv[0]))
    server = ThreadingHTTPServer((LOOPBACK, int(argv[1])), _handler_for(store))
    server.daemon_threads = True
    server.serve_forever()

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
