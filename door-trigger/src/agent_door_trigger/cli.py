"""`agent-trigger`: the `fire` and `serve` subcommands.

`fire` is what `systemd/creche-trigger@.service` runs on a cron tick, and
what the webhook listener's own logic is built from (`fire.py`). `serve`
runs the webhook listener (`webhooks.py`) under
`systemd/creche-trigger-webhooks.service`.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path

import httpx
import uvicorn

from .attendance import AttendanceClient, HttpAttendance
from .config import (
    ConfigError,
    FireConfig,
    ServeConfig,
    fire_config_from_env,
    serve_config_from_env,
)
from .errors import AttendanceError, ExitCode
from .families import StatusFiles
from .fire import Firing, TriggerKind, fire_trigger
from .payload import PayloadError, read_payload
from .quiet.gate import QuietGate, gated_family
from .quiet.pep import HttpFamilyReads, family_token
from .quiet.records import HostRecords
from .quiet.state import StateFiles
from .routes import RouteTable
from .webhooks import create_app

#: Under the state root: `attendance`'s outcome records (contract 02 §13.1),
#: the PEP's audit (contract 04 §6) and the quiet check's own records.
OUTCOMES_DIR = "outcomes"
AUDIT_DIR = "audit"
QUIET_DIR = "triggers/quiet"

_LOG = logging.getLogger(__name__)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    # httpx logs every request URL at INFO, and a header carrying the
    # attendance token rides on every one of these calls (invariant 13).
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    args = _parse_args(argv)
    if args.command == "fire":
        return int(_run_fire(args))

    return int(_run_serve(args))


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="agent-trigger")
    sub = parser.add_subparsers(dest="command", required=True)

    fire_cmd = sub.add_parser("fire", help="fire one autonomous family's trigger")
    fire_cmd.add_argument("family")
    fire_cmd.add_argument(
        "--trigger", metavar="NAME", help="the webhook trigger name; omit for a cron firing"
    )
    fire_cmd.add_argument(
        "--payload-file",
        metavar="FILE",
        type=Path,
        help="a JSON file to pass through as the job's payload, untouched",
    )
    fire_cmd.add_argument(
        "--check", action="store_true", help="validate config and exit, without calling attendance"
    )
    fire_cmd.add_argument(
        "--force", action="store_true", help="fire without the family's quiet check"
    )

    serve_cmd = sub.add_parser("serve", help="run the webhook listener")
    serve_cmd.add_argument(
        "--check", action="store_true", help="validate config and exit, without binding a port"
    )

    return parser.parse_args(argv)


def _run_fire(args: argparse.Namespace) -> ExitCode:
    try:
        config = fire_config_from_env()
    except ConfigError as exc:
        print(f"agent-trigger: refusing to start: {exc}", file=sys.stderr)
        return ExitCode.USAGE

    if args.check:
        print(f"agent-trigger: config OK: {config.describe()}")
        return ExitCode.ACCEPTED

    try:
        payload = _read_payload_file(args.payload_file)
    except PayloadError as exc:
        print(f"agent-trigger: refusing to fire: {exc}", file=sys.stderr)
        return ExitCode.USAGE

    attendance = HttpAttendance(config.attendance)
    pep = httpx.Client(base_url=config.pep_url)
    try:
        # Only the cron's own firing is checked: a named trigger or a
        # payload is work, and `--force` is a person meaning it.
        checked = not (args.force or args.trigger or payload)
        gate = _gate(config, args.family, attendance, pep) if checked else None
        return execute_fire(attendance, args.family, args.trigger, payload, gate)
    except (OSError, httpx.HTTPError) as exc:
        # A dead socket, a refused connection, a timeout: none of these
        # are attendance REFUSING the job (AttendanceError, handled inside
        # execute_fire), so they get a different exit code and message.
        print(f"agent-trigger: cannot reach attendance: {exc}", file=sys.stderr)
        return ExitCode.USAGE
    finally:
        attendance.close()
        pep.close()


def _gate(
    config: FireConfig, family: str, attendance: HttpAttendance, pep: httpx.Client
) -> QuietGate | None:
    """The family's quiet check, or None when its file asks for none."""
    gated = gated_family(config.registry_root, family)
    if gated is None:
        return None

    found, quiet = gated
    return QuietGate(
        found,
        quiet,
        reads=HttpFamilyReads(pep, family_token(config.families_dir, family)),
        records=HostRecords(config.state_root / OUTCOMES_DIR, config.state_root / AUDIT_DIR),
        store=StateFiles(config.state_root / QUIET_DIR),
        sessions=attendance,
        clock=lambda: datetime.now(UTC),
    )


def execute_fire(
    attendance: AttendanceClient,
    family: str,
    trigger: str | None,
    payload: str | None,
    gate: QuietGate | None = None,
) -> ExitCode:
    """One firing against an already-built `AttendanceClient`. Split out of
    `_run_fire` so a test drives it with `FakeAttendance` and never opens a
    real connection (the wire shape itself is `test_trigger_fire.py`'s
    job). With a `gate`, the family's quiet check decides first
    (contract 01 §3.15): quiet starts nothing and still exits 0."""
    kind = TriggerKind.WEBHOOK if trigger else TriggerKind.TIMER
    firing = Firing(family=family, kind=kind, name=trigger, payload=payload)

    check = gate.check() if gate is not None else None
    if check is not None:
        print(f"{'quiet' if check.quiet else 'wake'}: {family}: {check.summary()}")

    if check is not None and check.quiet:
        return ExitCode.ACCEPTED

    try:
        outcome = fire_trigger(attendance, firing)
    except AttendanceError as exc:
        # The contract's own code and message, never retried in a loop by
        # this door. systemd's journal is where the operator reads this line.
        print(f"agent-trigger: refused: {exc.code}: {exc.message}", file=sys.stderr)
        return ExitCode.REFUSED

    if gate is not None and check is not None:
        gate.record(outcome.session, check)

    print(f"{outcome.session} {outcome.state}")
    return ExitCode.ACCEPTED


def _read_payload_file(path: Path | None) -> str | None:
    if path is None:
        return None

    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise PayloadError(f"cannot read {path}: {exc.strerror or exc}") from exc

    return read_payload(raw)


def _run_serve(args: argparse.Namespace) -> ExitCode:
    try:
        config = serve_config_from_env()
    except ConfigError as exc:
        print(f"agent-trigger: refusing to start: {exc}", file=sys.stderr)
        return ExitCode.USAGE

    if args.check:
        print(f"agent-trigger: config OK: {config.describe()}")
        return ExitCode.ACCEPTED

    _serve(config)
    return ExitCode.ACCEPTED


def _serve(config: ServeConfig) -> None:
    families = StatusFiles(config.families_dir)
    routes = RouteTable(
        registry_root=config.registry_root, webhooks_dir=config.webhooks_dir, families=families
    )
    attendance = HttpAttendance(config.attendance)
    app = create_app(config, attendance, routes)
    # config.attendance and config.bind_host are already validated by
    # serve_config_from_env: 0.0.0.0 is refused there, so nothing here can
    # widen it back out.
    uvicorn.run(app, host=config.bind_host, port=config.bind_port)


if __name__ == "__main__":
    sys.exit(main())
