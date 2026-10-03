"""The component's verify hook (contract 06 §4, `caregiver/component.yaml`).

`caregiver/component.yaml` names `bin/caregiver-verify --json`, and §4 rule 6
says the hook ships inside the component's own artifact. This is that hook.

It answers one question: does this component work RIGHT NOW?

`caregiver` serves no HTTP, so there is no `/healthz` to call. What it does
instead is republish every family's `status.json` on a heartbeat
(`loop.py`), and contract 05 §2 rule 5 says a document older than
`STALE_AFTER_S` is not current. So a fresh `written_at` on every family IS
the running-process evidence, and a stale one is the exact symptom of a
reconciler that swapped cleanly and then stopped reconciling. The hook reads
the documents, and what it judges is the CLOCK on them, never their content.

A host with no family yet passes with that said in the detail. A release
must not be blocked because nothing has been declared.

Contract 06 §4: read only, no network, exit 0 passes, small output,
runnable at any time. No secret is read, so nothing can leak (invariant 13).

**The environment.** Contract 06 §4 rule 7: root's executor hands every
hook a small, fixed environment (`stage7-releases.md` §2.4) that carries
none of `creche-caregiver.service`'s own `EnvironmentFile=`. This host's
documented default (`paths.STATE_ROOT`) covers the common case, and
`--env-file <path>` — the same file the unit's own `EnvironmentFile=`
names (`caregiver/component.yaml`'s `verify.command`, pinned to the unit by
`release/tests/test_release_r7r_verify_env.py`) — is there for whenever it
does not: read by this hook itself, as its own user, never by root.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, cast

from . import paths
from .clock import RFC3339_FORMAT
from .status import STALE_AFTER_S

EXIT_OK: Final = 0
EXIT_FAILED: Final = 1

STATE_ROOT_ENV: Final = "MANAGERD_STATE_ROOT"

#: A shell variable name (POSIX `Name`), which is what `envfile_upsert`
#: (`bin/lib/envfile.sh`) writes to the left of `=`.
_ENV_NAME_RE: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

#: A `status.json` is one page. Anything larger is not one, and the hook
#: refuses to read it rather than pulling an arbitrary file into memory.
MAX_STATUS_BYTES: Final = 1 << 20


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str


def main(argv: list[str] | None = None) -> int:
    """The `caregiver-verify` entry point."""
    parsed = _parse_args(argv if argv is not None else sys.argv[1:])
    try:
        source = _environ(parsed.env_file)
    except OSError as error:
        return _report([Check("env-file", False, str(error))], as_json=parsed.json)

    root = Path(source.get(STATE_ROOT_ENV) or paths.STATE_ROOT)

    return _report(_checks(root, datetime.now(UTC)), as_json=parsed.json)


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


def _checks(root: Path, now: datetime) -> list[Check]:
    families_dir = root / paths.FAMILIES_DIR
    if not families_dir.is_dir():
        return [Check("state root", False, f"{families_dir} is missing")]

    names = sorted(one.name for one in families_dir.iterdir() if one.is_dir())

    return [
        Check("state root", True, f"{families_dir}: {len(names)} family/families"),
        _heartbeat(root, names, now),
    ]


def _heartbeat(root: Path, names: list[str], now: datetime) -> Check:
    """Every family's document younger than contract 05 §2 rule 5's window."""
    if not names:
        return Check("heartbeat", True, "no family declared yet")

    stale: list[str] = []
    for name in names:
        age = _age_s(paths.status_path(root, name), now)
        if age is None or age > STALE_AFTER_S:
            stale.append(name)

    if stale:
        return Check("heartbeat", False, f"stale past {STALE_AFTER_S}s: {', '.join(stale)}")

    return Check("heartbeat", True, f"{len(names)} family/families inside {STALE_AFTER_S}s")


def _age_s(status_path: Path, now: datetime) -> float | None:
    """Seconds since `written_at`, or None when there is no usable stamp."""
    written = _written_at(status_path)
    if written is None:
        return None

    try:
        moment = datetime.strptime(written, RFC3339_FORMAT).replace(tzinfo=UTC)
    except ValueError:
        return None

    return (now - moment).total_seconds()


def _written_at(status_path: Path) -> str | None:
    try:
        if status_path.stat().st_size > MAX_STATUS_BYTES:
            return None

        body: Any = json.loads(status_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None

    if not isinstance(body, dict):
        return None

    written = cast("dict[str, object]", body).get("written_at")

    return written if isinstance(written, str) else None


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
        prog="caregiver-verify", description="Does caregiver work right now? (contract 06 §4)"
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
