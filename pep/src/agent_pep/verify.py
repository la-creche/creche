"""The component's verify hook (contract 06 §4, `pep/component.yaml`).

`pep/component.yaml` names `bin/pep-verify --json`, and §4 rule 6 says the
hook ships inside the component's own artifact. This is that hook.

It answers one question: does this component work RIGHT NOW? It asks the
running process, never the files a release just wrote — a tree that swapped
cleanly and a service that starts are two different facts, and only the
second one matters after a switch.

Contract 06 §4's rules, and how each is kept here:

1. Read only. It opens one HTTP connection, asks systemd one question
   when the roster is not `ok`, and writes nothing anywhere.
2. No network beyond this host. `/healthz` on the address the unit binds.
3. Exit 0 passes. Anything else fails.
4. The output is small, so the executor's 8 KiB capture is never the limit.
5. Runnable at any time, not only after a release.

Invariant 13: the hook holds no secret. `/healthz` is the one path the PEP
answers without a bearer, so there is nothing to carry and nothing to leak.

**What passes** (contract 04 §10). A 200 that says `ok` is not
enough: a PEP whose roster will not parse, or that refused an upstream
at start, answers exactly that. Four checks, all on one answer:

    healthz    200, and `ok: true`
    roster     `ok`. `unreadable` fails. `off` fails when the unit sets
               PEP_UPSTREAMS_GENERATED, and passes when it does not
    upstreams  `refused` is 0. `null` passes only beside an `off` roster

`PEP_UPSTREAMS_GENERATED` decides `off`: it is the roster root writes at
the end of every verified `mcp-servers` release, so a unit that names it
has a roster to read. Rule 7 below keeps the unit's `Environment=` out of
this process, so the hook asks systemd for it, and only when the roster is
not `ok`: a healthy PEP passes on `/healthz` alone.

**The environment.** Contract 06 §4 rule 7: root's executor hands every
hook a small, fixed environment (`stage7-releases.md` §2.4) that carries
none of `agent-pep.service`'s own. The bind is the site's:
`pep/component.yaml` passes `--env-file /etc/agent-control/site.env`, the
unit's own `EnvironmentFile=`, and the hook reads `PEP_BIND`, else
`AGENT_LAN_ADDRESS` on port 8300, from the merged environment (`site.py`),
the way `__main__` does. With neither, the hook reports one failed check
that names the variable. It never falls back to a default address. The
flag has the same shape as in `view-verify`, `sessiond-verify` and
`managerd-verify`.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Final, cast

import httpx

from . import site

EXIT_OK: Final = 0
EXIT_FAILED: Final = 1

HEALTH_PATH: Final = "/healthz"
HEALTH_TIMEOUT_S: Final = 5.0
HTTP_OK: Final = 200

#: `pep/component.yaml`'s `unit`, and the one question the hook asks
#: systemd about it. systemd's answer includes every drop-in; a read of the
#: unit file would not.
UNIT: Final = "agent-pep.service"
SYSTEMCTL: Final = "/usr/bin/systemctl"
UNIT_ENV_ARGV: Final = (SYSTEMCTL, "show", UNIT, "--property=Environment", "--value")
SYSTEMCTL_TIMEOUT_S: Final = 10.0

#: The two roster files, named the way `__main__` names them.
GENERATED_ENV: Final = "PEP_UPSTREAMS_GENERATED"
BASE_ENV: Final = "PEP_UPSTREAMS"

#: Contract 04 §10's `roster.state`. Spelled here rather than imported:
#: `reload_wiring` pulls in the whole `mcp` stack, and this hook needs none.
ROSTER_OFF: Final = "off"
ROSTER_OK: Final = "ok"
ROSTER_UNREADABLE: Final = "unreadable"
ROSTER_STATES: Final = frozenset({ROSTER_OFF, ROSTER_OK, ROSTER_UNREADABLE})

#: Contract 04 §10's `since`: RFC 3339, UTC, second resolution.
_SINCE_RE: Final = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")

#: A shell variable name (POSIX `Name`), which is what `envfile_upsert`
#: (`bin/lib/envfile.sh`) writes to the left of `=`.
_ENV_NAME_RE: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str


@dataclass(frozen=True)
class Roster:
    """`/healthz`'s `roster` (contract 04 §10)."""

    state: str
    since: str | None


@dataclass(frozen=True)
class Upstreams:
    """`/healthz`'s `upstreams` (contract 04 §10)."""

    serving: int
    refused: int


@dataclass(frozen=True)
class Read:
    """What `/healthz` said beyond `ok`, where it said it in the contract's
    shape. The report prints both, so the ledger's `verify pep:` line says
    what the PEP served."""

    roster: Roster | None = None
    upstreams: Upstreams | None = None


@dataclass(frozen=True)
class UnitRoster:
    """The two roster files `agent-pep.service` sets, or why systemd did
    not say."""

    generated: str | None = None
    base: str | None = None
    error: str | None = None


def main(argv: list[str] | None = None) -> int:
    """The `pep-verify` entry point."""
    parsed = _parse_args(argv if argv is not None else sys.argv[1:])
    try:
        source = _environ(parsed.env_file)
    except OSError as error:
        return _report([Check("env-file", False, str(error))], Read(), as_json=parsed.json)

    try:
        bind = site.bind(source)
    except site.ConfigError as error:
        return _report([Check("bind", False, str(error))], Read(), as_json=parsed.json)

    checks, read = _checks(bind)

    return _report(checks, read, as_json=parsed.json)


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
    `KEY=value` per line, blank lines and `#` comments
    skipped, no quoting and no expansion — the shape `bin/lib/envfile.sh`
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


def _checks(bind: str) -> tuple[list[Check], Read]:
    healthz, body = _healthy(bind)
    checks = [Check("bind", True, bind), healthz]
    if body is None:
        return checks, Read()

    # `upstreams` is judged against the roster as read: `null` is the
    # contract's answer only beside an `off` one.
    roster = _roster(body)
    upstreams_check, upstreams = _upstreams_check(body, roster)
    checks += [_roster_check(roster), upstreams_check]

    return checks, Read(roster, upstreams)


def _healthy(bind: str) -> tuple[Check, dict[str, object] | None]:
    """The one call. A 200 carrying `{"ok": true}` is the PEP answering —
    a 200 from something else that happens to hold the port is not."""
    url = f"http://{bind}{HEALTH_PATH}"

    try:
        answer = httpx.get(url, timeout=HEALTH_TIMEOUT_S)
    except httpx.HTTPError as error:
        return Check("healthz", False, f"{url}: {type(error).__name__}"), None

    if answer.status_code != HTTP_OK:
        return Check("healthz", False, f"{url} answered {answer.status_code}"), None

    body = _pep_body(answer)
    if body is None:
        return Check("healthz", False, f"{url} answered 200 but not the PEP's body"), None

    return Check("healthz", True, url), body


def _pep_body(answer: httpx.Response) -> dict[str, object] | None:
    """The body, when it is an object that says `ok: true`."""
    try:
        body: object = answer.json()
    except ValueError:
        return None

    if not isinstance(body, dict):
        return None

    found = cast("dict[str, object]", body)

    return found if found.get("ok") is True else None


def _roster(body: dict[str, object]) -> Roster | None:
    """`roster`, or None when the body carries no block of §10's shape: a
    PEP older than this hook, or not the PEP."""
    raw = body.get("roster")
    if not isinstance(raw, dict):
        return None

    block = cast("dict[str, object]", raw)
    state = block.get("state")
    since = block.get("since")
    if not isinstance(state, str) or state not in ROSTER_STATES:
        return None

    if since is None:
        return Roster(state, None)

    if not isinstance(since, str) or not _SINCE_RE.fullmatch(since):
        return None

    return Roster(state, since)


def _roster_check(roster: Roster | None) -> Check:
    """Contract 04 §10 rules 2 to 4, as a verdict.

    `unreadable` fails whatever the unit says: the PEP started without the
    roster, so a release that leaves it there has not worked. `off` asks
    the unit whether there was a roster to read.
    """
    if roster is None:
        return Check(
            "roster", False, "/healthz carries no roster block: a PEP older than this hook"
        )

    if roster.state == ROSTER_OK:
        return Check("roster", True, f"ok since {roster.since}")

    unit = _unit_roster()
    if roster.state == ROSTER_UNREADABLE:
        detail = f"unreadable since {roster.since}: {_files(unit)} will not parse"

        return Check("roster", False, f"{detail} (journalctl -u agent-pep names which)")

    if unit.error is not None:
        detail = f"off, and {UNIT} would not say whether it sets {GENERATED_ENV} ({unit.error})"

        return Check("roster", False, detail)

    if unit.generated is not None:
        detail = f"off, but {UNIT} sets {GENERATED_ENV}={unit.generated}"

        reason = "no roster read runs, so no MCP server a release installed is served"

        return Check("roster", False, f"{detail}: {reason}")

    return Check("roster", True, f"off: {UNIT} sets no {GENERATED_ENV}")


def _files(unit: UnitRoster) -> str:
    """The roster files the unit names, the generated one first: it is the
    one root writes, and it wins a name."""
    if unit.error is not None:
        return f"a roster file ({UNIT} would not say which: {unit.error})"

    named = [
        f"{name}={path}"
        for name, path in ((GENERATED_ENV, unit.generated), (BASE_ENV, unit.base))
        if path is not None
    ]

    return " or ".join(named) if named else f"a roster file {UNIT} does not name"


def _upstreams_check(
    body: dict[str, object], roster: Roster | None
) -> tuple[Check, Upstreams | None]:
    """`refused` must be 0. An upstream refused at start is a launcher
    that cannot switch users, or no `mcp-<name>` user, and the PEP answers
    200 over it."""
    if "upstreams" not in body:
        return Check(
            "upstreams", False, "/healthz carries no upstreams block: a PEP older than this hook"
        ), None

    raw = body["upstreams"]
    if raw is None:
        if roster is not None and roster.state == ROSTER_OFF:
            return Check("upstreams", True, "not counted: no roster"), None

        return Check("upstreams", False, "not counted, beside a roster that is not off"), None

    counts = _counts(raw)
    if counts is None:
        return Check("upstreams", False, "not two counts named serving and refused"), None

    if counts.refused:
        detail = f"{counts.refused} refused, {counts.serving} serving"

        return Check("upstreams", False, f"{detail} (journalctl -u agent-pep names each)"), counts

    return Check("upstreams", True, f"{counts.serving} serving, 0 refused"), counts


def _counts(raw: object) -> Upstreams | None:
    if not isinstance(raw, dict):
        return None

    block = cast("dict[str, object]", raw)
    serving = block.get("serving")
    refused = block.get("refused")
    if not (_count(serving) and _count(refused)):
        return None

    return Upstreams(cast("int", serving), cast("int", refused))


def _count(value: object) -> bool:
    """A JSON integer of zero or more. `bool` is an `int` to Python."""
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _unit_roster() -> UnitRoster:
    """What `agent-pep.service` sets for the two roster variables.

    Contract 06 §4 rule 7: the executor hands this hook none of the unit's
    `Environment=` lines, so the hook asks systemd. The answer is one line
    of assignments split by spaces, and one that holds a space is quoted.
    """
    try:
        done = subprocess.run(
            list(UNIT_ENV_ARGV),
            capture_output=True,
            text=True,
            timeout=SYSTEMCTL_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        return UnitRoster(error=type(error).__name__)

    if done.returncode != 0:
        return UnitRoster(error=f"systemctl exited {done.returncode}")

    try:
        words = shlex.split(done.stdout)
    except ValueError as error:
        return UnitRoster(error=f"unparsable: {error}")

    values: dict[str, str] = {}
    for word in words:
        name, sep, value = word.partition("=")
        if sep:
            values[name] = value

    return UnitRoster(generated=_named(values, GENERATED_ENV), base=_named(values, BASE_ENV))


def _named(values: dict[str, str], name: str) -> str | None:
    """`__main__._generated_roster`'s read: blank is unset."""
    raw = values.get(name, "").strip()

    return raw or None


def _report(checks: list[Check], read: Read, *, as_json: bool) -> int:
    failed = [one for one in checks if not one.ok]

    if as_json:
        body = {
            "ok": not failed,
            "roster": asdict(read.roster) if read.roster is not None else None,
            "upstreams": asdict(read.upstreams) if read.upstreams is not None else None,
            "checks": [{"name": one.name, "ok": one.ok, "detail": one.detail} for one in checks],
        }
        print(json.dumps(body, indent=2))
    else:
        for one in checks:
            print(f"{'PASS' if one.ok else 'FAIL'} {one.name}: {one.detail}")

    return EXIT_FAILED if failed else EXIT_OK


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="pep-verify", description="Does agent-pep work right now? (contract 06 §4)"
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
