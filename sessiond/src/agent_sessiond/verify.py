"""The component's verify hook (contract 06 §4, `sessiond/component.yaml`).

`sessiond/component.yaml` names `bin/sessiond-verify --json`, and §4 rule 6
says the hook ships inside the component's own artifact. This is that hook.

It answers one question: does this component work RIGHT NOW? It asks the
running process over the socket the service binds, never the files a release
just wrote.

**The one call is deliberately unauthenticated.** `GET /v1/sessions` without
a bearer must answer 401. That single answer proves two things at once:

1. the process is up and serving HTTP on its socket, and
2. it still fails closed. A 200 here would mean the door opened to anyone
   who can reach the socket. So a 200 is a FAILURE, not a pass.

Invariant 13: the hook reads no token file and carries no bearer. There is
nothing here to leak.

**The environment.** Contract 06 §4 rule 7: root's executor hands every
hook a small, fixed environment (`stage7-releases.md` §2.4) that carries
none of `agent-sessiond.service`'s own `EnvironmentFile=`. This host's
documented default (`DEFAULT_SOCKET`) covers the common case, and
`--env-file <path>` — the same file the unit's own `EnvironmentFile=`
names (`sessiond/component.yaml`'s `verify.command`, pinned to the unit by
`release/tests/test_release_r7r_verify_env.py`) — is there for whenever it
does not: read by this hook itself, as its own user, never by root.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import httpx

from .config import DEFAULT_SOCKET, ENV_PREFIX

EXIT_OK: Final = 0
EXIT_FAILED: Final = 1

SOCKET_ENV: Final = f"{ENV_PREFIX}SOCKET"

#: A shell variable name (POSIX `Name`), which is what `envfile_upsert`
#: (`bin/lib/envfile.sh`) writes to the left of `=`.
_ENV_NAME_RE: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

#: A Unix-socket client still needs a base URL for the path and the Host
#: header. The name is never resolved.
SOCKET_BASE_URL: Final = "http://sessiond"

#: Contract 02 §3: the listing endpoint. Chosen because it is a plain read
#: that changes nothing even if the auth check were ever to let it through.
PROBE_PATH: Final = "/v1/sessions"
PROBE_TIMEOUT_S: Final = 5.0

#: The only answer that means "running and still failing closed".
HTTP_UNAUTHORIZED: Final = 401


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str


def main(argv: list[str] | None = None) -> int:
    """The `sessiond-verify` entry point."""
    parsed = _parse_args(argv if argv is not None else sys.argv[1:])
    try:
        source = _environ(parsed.env_file)
    except OSError as error:
        return _report([Check("env-file", False, str(error))], as_json=parsed.json)

    socket_path = Path(source.get(SOCKET_ENV) or DEFAULT_SOCKET)

    return _report(_checks(socket_path), as_json=parsed.json)


def _environ(env_file: Path | None) -> dict[str, str]:
    """What this process reads its configuration from: whatever it
    inherited, with `--env-file` layered on top.

    The file wins over an inherited value on purpose: it is what the unit
    would have given this process, and the inherited environment is only
    ever root's own fixed one (the module docstring's "The environment").
    """
    merged = dict(os.environ)
    if env_file is not None:
        merged.update(_parse_env_file(env_file))

    return merged


def _parse_env_file(path: Path) -> dict[str, str]:
    """A shell-free reader for a systemd `EnvironmentFile=`. One
    `KEY=value` per line, blank lines and `#` comments skipped, no quoting
    and no expansion — the shape `bin/lib/envfile.sh`
    already writes. A parser that understood more would invite a file this
    hook cannot predict its own read of.
    """
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        name, sep, value = stripped.partition("=")
        if sep and _ENV_NAME_RE.fullmatch(name):
            values[name] = value

    return values


def _checks(socket_path: Path) -> list[Check]:
    bound = _bound(socket_path)
    if not bound.ok:
        # Without a socket there is nothing to ask, and a second failing
        # check would say the same thing twice.
        return [bound]

    return [bound, _refuses_anonymous(socket_path)]


def _bound(socket_path: Path) -> Check:
    """Present, and a socket rather than a file somebody left behind."""
    try:
        mode = socket_path.lstat().st_mode
    except OSError as error:
        return Check("socket", False, f"{socket_path}: {error.strerror or error}")

    if not stat.S_ISSOCK(mode):
        return Check("socket", False, f"{socket_path} is not a socket")

    return Check("socket", True, str(socket_path))


def _refuses_anonymous(socket_path: Path) -> Check:
    """The one call. 401 passes. 200 fails: the door would be open."""
    url = f"{SOCKET_BASE_URL}{PROBE_PATH}"
    transport = httpx.HTTPTransport(uds=str(socket_path))

    try:
        with httpx.Client(transport=transport, timeout=PROBE_TIMEOUT_S) as client:
            answer = client.get(url)
    except httpx.HTTPError as error:
        return Check("anonymous read", False, f"{socket_path}: {type(error).__name__}")

    if answer.status_code != HTTP_UNAUTHORIZED:
        detail = f"{PROBE_PATH} answered {answer.status_code}, not {HTTP_UNAUTHORIZED}"
        return Check("anonymous read", False, detail)

    return Check("anonymous read", True, f"{PROBE_PATH} answered {HTTP_UNAUTHORIZED}")


def _report(checks: list[Check], *, as_json: bool) -> int:
    failed = [one for one in checks if not one.ok]

    if as_json:
        body = {
            "ok": not failed,
            "checks": [{"name": one.name, "ok": one.ok, "detail": one.detail} for one in checks],
        }
        print(json.dumps(body, indent=2))
    else:
        for one in checks:
            print(f"{'PASS' if one.ok else 'FAIL'} {one.name}: {one.detail}")

    return EXIT_FAILED if failed else EXIT_OK


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="sessiond-verify", description="Does sessiond work right now? (contract 06 §4)"
    )
    parser.add_argument("--json", action="store_true", help="one JSON object instead of lines")
    parser.add_argument(
        "--env-file",
        type=Path,
        default=None,
        help="KEY=value lines to read before checking the environment",
    )

    return parser.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(main())
