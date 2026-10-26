"""A stand-in for `creche-facade`: the ACP side of the supervisor link.

    fake_facade.py

It reads JSON-RPC lines on stdin and answers on stdout, one line each.
`fake_sbx.py` starts it, so its environment comes from the env file only.

- `FAKE_FACADE_MODE=garbage` writes one line that is not JSON after it
  answers `initialize`.
- `FAKE_FACADE_RECORD=<path>` appends each method it receives to the file.

`_creche/shutdown` ends the process with exit status 0.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

INITIALIZE_RESULT: dict[str, Any] = {
    "protocolVersion": 1,
    "agentCapabilities": {"loadSession": True, "sessionCapabilities": {"list": {}}},
    "authMethods": [],
}


def answer(request_id: object, result: object) -> None:
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": request_id, "result": result}) + "\n")
    sys.stdout.flush()


def record(method: str) -> None:
    path = os.environ.get("FAKE_FACADE_RECORD", "")

    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(method + "\n")


def main() -> int:
    for line in sys.stdin:
        message = json.loads(line)
        method = message.get("method", "")
        record(method)

        if method == "initialize":
            answer(message["id"], INITIALIZE_RESULT)

            if os.environ.get("FAKE_FACADE_MODE") == "garbage":
                sys.stdout.write("not json\n")
                sys.stdout.flush()

        elif method == "_creche/beat":
            answer(message["id"], {})

        elif method == "_creche/shutdown":
            return 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
