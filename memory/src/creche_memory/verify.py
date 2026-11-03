"""The verify hook of the memory component (contract 06 §4).

`memory/component.yaml` names `creche-memory-verify --json --env-file <path>`.
It answers one question: does this component work right now? Exit 0 is a
pass, and 1 is a failure. It is read-only and makes no call off the host.

It checks four things:

1. The env file of the unit holds the LiteLLM key. The key is never printed.
2. The vault, the socket folder and the tokenizer cache exist (the same
   check as `creche-memory --check`).
3. The peer user exists.
4. The socket of the service accepts a connection within 10 seconds. The
   service answers only the peer user, so it closes this connection at
   once, and that is a pass.

**The environment.** The executor gives the hook a small, fixed
environment, with nothing of the unit's own (§4 rule 7). `--env-file`
names the same file as the first `EnvironmentFile=` of
`systemd/creche-memory.service`, and the hook reads it as its own user."""

import argparse
import json
import os
import re
import socket
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from .config import ConfigError, check, key_from_env, places_from_env

EXIT_OK = 0
EXIT_FAILED = 1

#: The seconds of one connect.
SOCKET_TIMEOUT_S = 5.0

#: The seconds the hook tries the socket for. The executor runs the hook
#: right after it restarts the unit, and the service binds its socket about
#: a second later. Well under the hook's 30 s limit.
SOCKET_WAIT_S = 10.0
SOCKET_RETRY_S = 0.5

#: The most bytes of the env file.
MAX_ENV_BYTES = 64 * 1024

#: One line of the env file: `KEY=value`, with no quoting and no expansion.
ENV_LINE = re.compile(r"([A-Z_][A-Z0-9_]*)=(.*)")


@dataclass(frozen=True)
class Result:
    name: str
    ok: bool
    detail: str


def read_env_file(path: Path) -> dict[str, str]:
    """The keys of one env file. A line that is not a key, a comment or
    blank stops the read."""
    data = path.read_bytes()
    if len(data) > MAX_ENV_BYTES:
        raise ValueError(f"{path} is over {MAX_ENV_BYTES} bytes")

    values: dict[str, str] = {}
    for number, line in enumerate(data.decode("utf-8").splitlines(), start=1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue

        matched = ENV_LINE.fullmatch(line)
        if matched is None:
            raise ValueError(f"{path}:{number} is not KEY=value")

        values[matched.group(1)] = matched.group(2)

    return values


def _connect(path: Path) -> None:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(SOCKET_TIMEOUT_S)
        client.connect(str(path))


def _socket_answers(path: Path) -> Result:
    deadline = time.monotonic() + SOCKET_WAIT_S
    while True:
        try:
            _connect(path)
        except OSError as error:
            if time.monotonic() >= deadline:
                return Result("socket", False, f"{path}: {type(error).__name__}")

            time.sleep(SOCKET_RETRY_S)
            continue

        return Result("socket", True, f"{path} accepts a connection")


def run(env_file: Path) -> list[Result]:
    try:
        source = os.environ | read_env_file(env_file)
    except (OSError, UnicodeDecodeError, ValueError) as error:
        return [Result("env-file", False, f"{env_file}: {type(error).__name__}")]

    results = [Result("env-file", True, str(env_file))]
    try:
        key_from_env(source)
        results.append(Result("key", True, "set"))
    except ConfigError as error:
        results.append(Result("key", False, str(error)))

    try:
        places = places_from_env(source)
    except ConfigError as error:
        return [*results, Result("config", False, str(error))]

    problems = check(places)
    results.append(Result("host", not problems, "; ".join(problems) or "folders exist"))
    results.append(_socket_answers(places.socket_path))

    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="creche-memory-verify")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--env-file", type=Path, required=True)
    args = parser.parse_args(argv)
    results = run(args.env_file)
    ok = all(one.ok for one in results)

    if args.json:
        checks = [{"name": one.name, "ok": one.ok, "detail": one.detail} for one in results]
        print(json.dumps({"component": "memory", "ok": ok, "checks": checks}))
    else:
        for one in results:
            print(f"{'ok' if one.ok else 'FAIL'} {one.name}: {one.detail}")

    return EXIT_OK if ok else EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main())
