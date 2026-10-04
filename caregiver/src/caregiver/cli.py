"""caregiver serve | reconcile-once | rotate | apply-once | status |
delete.

Every mutating verb prints its plan and needs `--write` to act -- a plan
readable before anything touches LiteLLM, the PEP's grant file or a
sandbox. `status` is read-only and needs no gate.

`serve` is the one the operator actually runs, through
`systemd/creche-caregiver.service`. The other verbs exist for a host where
something has gone wrong and for the tests."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Final, cast

from agent_family import Index, Registry, Report, load_registry

from . import paths
from .apply import apply_once
from .chaperone_watch import (
    PEP_PROBE_INTERVAL_S,
    PEP_UNREACHABLE_AFTER_S,
    HttpPepProbe,
    PepWatch,
)
from .delete import delete_family
from .driver import SandboxDriver, SbxDriver
from .egress import EgressConfig
from .images import SandboxImages
from .lan import ConfigError, Port, url
from .litellm_keys import HttpLiteLLMKeys, LiteLLMKeys
from .loop import LoopConfig, SignalControl, serve
from .mcp_release import paths_under
from .reconcile import Actors, SpendRead, reconcile_family
from .released import RELEASED_IMAGES, ReleasedImages
from .rotate import Mode, Reason, RotateError, RotateRequest, Scope, rotate
from .switch import HttpSwitchClient, SwitchClient, SwitchError, read_token
from .timers import UnitWriter, UserUnits

EXIT_OK: Final = 0
EXIT_PROBLEM: Final = 1
EXIT_USAGE: Final = 2

# The three plane URLs below default to the site's LAN address (`lan.py`),
# never loopback, built when a verb needs one: a verb that reaches no plane
# needs no address. An explicit flag always wins.
#
# LiteLLM:  `--litellm-base-url`, else `url(Port.LITELLM)`.
#
# attendance: contract 02 §3 rules 1 and 2. It binds a Unix socket and, when
#           configured, the LAN address at 8350. `--sessiond-url`, else
#           `url(Port.ATTENDANCE)`.
#
# PEP:      where it answers `/healthz` (contract 05 §3.3). The production
#           address is the DEFAULT, not an empty string: the PEP binds the
#           LAN address and loopback answers nothing there, and a host that
#           forgets the flag must end up watching the right PEP rather than
#           watching none. `--pep-url ''` turns the watch off on purpose,
#           and every status document then says `pep.watch: off`.

LOG_FORMAT: Final = "%(asctime)s %(levelname)s %(name)s %(message)s"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="caregiver", description="The family manager (docs/rework/contracts/01, 04, 05)."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    apply_cmd = sub.add_parser("apply-once", help="bring one family up from its registry file")
    apply_cmd.add_argument("registry", type=Path, help="the registry root, holding families/")
    apply_cmd.add_argument("family")
    apply_cmd.add_argument(
        "--image",
        required=True,
        help="the sandbox image digest for flavor 'base' (contract 06 chooses it, not this CLI)",
    )
    _python_image_arg(apply_cmd)
    apply_cmd.add_argument("--state-root", type=Path, default=paths.STATE_ROOT)
    apply_cmd.add_argument("--litellm-base-url", default=None)
    _attendance_args(apply_cmd)
    apply_cmd.add_argument(
        "--write", action="store_true", help="act. Without it, print the plan only"
    )

    serve_cmd = sub.add_parser("serve", help="watch the registry and converge every family")
    _watch_args(serve_cmd)
    serve_cmd.add_argument(
        "--poll-interval-s", type=float, default=LoopConfig.poll_interval_s, help="seconds per look"
    )
    serve_cmd.add_argument(
        "--max-concurrent-passes",
        type=int,
        default=LoopConfig.max_concurrent_passes,
        help="how many families may converge at once",
    )
    serve_cmd.add_argument(
        "--stop-grace-s",
        type=float,
        default=LoopConfig.stop_grace_s,
        help="on SIGTERM, how long to wait for the steps in flight. "
        "Keep it below the unit's TimeoutStopSec",
    )
    serve_cmd.add_argument(
        "--release-root",
        type=Path,
        default=paths.RELEASE_ROOT,
        help="root's own root: the release spool, the sealed secrets and the roster. "
        "A --state-root of its own needs one of these too",
    )
    _pep_watch_args(serve_cmd)

    once_cmd = sub.add_parser("reconcile-once", help="one reconcile pass over one family")
    _watch_args(once_cmd)
    once_cmd.add_argument("family")

    rotate_cmd = sub.add_parser("rotate", help="mint a new family key and token")
    rotate_cmd.add_argument("registry", type=Path)
    rotate_cmd.add_argument("family")
    rotate_cmd.add_argument("--state-root", type=Path, default=paths.STATE_ROOT)
    rotate_cmd.add_argument("--litellm-base-url", default=None)
    rotate_cmd.add_argument("--scope", choices=[str(one) for one in Scope], default=str(Scope.BOTH))
    rotate_cmd.add_argument(
        "--mode", choices=[str(one) for one in Mode], default=str(Mode.GRACEFUL)
    )
    rotate_cmd.add_argument(
        "--reason", choices=[str(one) for one in Reason], default=str(Reason.MANUAL)
    )
    rotate_cmd.add_argument("--write", action="store_true")

    status_cmd = sub.add_parser("status", help="print one family's status, or every family's")
    status_cmd.add_argument("family", nargs="?", help="omit to list every family")
    status_cmd.add_argument("--state-root", type=Path, default=paths.STATE_ROOT)
    status_cmd.add_argument("--json", action="store_true")

    delete_cmd = sub.add_parser(
        "delete", help="delete a family: its credentials, then its sandboxes"
    )
    delete_cmd.add_argument("family")
    delete_cmd.add_argument("--state-root", type=Path, default=paths.STATE_ROOT)
    delete_cmd.add_argument("--litellm-base-url", default=None)
    delete_cmd.add_argument(
        "--egress",
        action="append",
        default=[],
        help="a hostname sbx should stop allowing (repeatable); best effort, see delete_family",
    )
    delete_cmd.add_argument("--write", action="store_true")

    return parser


def _watch_args(parser: argparse.ArgumentParser) -> None:
    """What `serve` and `reconcile-once` both need. One place, so the unit
    file and a hand-run pass cannot drift apart."""
    parser.add_argument("registry", type=Path, help="the registry root, holding families/")
    parser.add_argument(
        "--image", required=True, help="the approved sandbox image digest for flavor 'base'"
    )
    _python_image_arg(parser)
    parser.add_argument(
        "--released-images",
        type=Path,
        default=None,
        help="the file a playpen release installed. Its references win over --image and "
        f"--image-python. Default {RELEASED_IMAGES}, and none for a --state-root of its own",
    )
    parser.add_argument("--state-root", type=Path, default=paths.STATE_ROOT)
    parser.add_argument("--litellm-base-url", default=None)
    _attendance_args(parser)
    parser.add_argument("--write", action="store_true", help="act. Without it, print the plan only")


def _python_image_arg(parser: argparse.ArgumentParser) -> None:
    """Contract 01 §3.9's second flavor. NOT required: a host that has not
    built it yet runs fine, and only a family that asks for `python` is
    faulted. Required would stop every family over one image."""
    parser.add_argument(
        "--image-python",
        default="",
        help="the approved sandbox image digest for flavor 'python'. Without it, a family "
        "with sandbox.image: python raises image_flavor_unconfigured and gets no sandbox",
    )


def _pep_watch_args(parser: argparse.ArgumentParser) -> None:
    """`serve` only. The watch is a fact over time, which is what a
    one-shot verb cannot hold: `apply-once` and `reconcile-once` publish
    `pep.watch: off` because one pass in one process has no interval to
    probe over and may not claim the PEP answered."""
    parser.add_argument(
        "--pep-url",
        default=None,
        help="where the PEP answers /healthz. Empty turns the watch off, and every "
        "status document then says so",
    )
    parser.add_argument(
        "--pep-probe-interval-s",
        type=float,
        default=PEP_PROBE_INTERVAL_S,
        help="seconds between probes, for the whole fleet",
    )
    parser.add_argument(
        "--pep-unreachable-after-s",
        type=float,
        default=PEP_UNREACHABLE_AFTER_S,
        help="how long the PEP must stay silent before every family is faulted. "
        "A deploy's restart costs about 10 to 15 s, so one flap is not an outage",
    )


def _pep_url(args: argparse.Namespace) -> str:
    """`--pep-url` as given, even empty, else the PEP on the LAN address."""
    if args.pep_url is None:
        return url(Port.PEP)

    return str(args.pep_url)


def _pep_watch(args: argparse.Namespace) -> PepWatch | None:
    """None when no address was given, which `serve` publishes as `off`."""
    pep_url = _pep_url(args)
    if not pep_url:
        return None

    return PepWatch(
        HttpPepProbe(pep_url),
        url=pep_url,
        interval_s=args.pep_probe_interval_s,
        threshold_s=args.pep_unreachable_after_s,
    )


def _attendance_args(parser: argparse.ArgumentParser) -> None:
    """Where `attendance` answers. Every verb that can build a switch client
    declares BOTH, in one place: `apply-once` once read `args.attendance_url`
    without declaring it, and died on the first host that had a token file."""
    # The flags keep the old name: the installed caregiver unit passes
    # `--sessiond-socket`, and that unit changes only by a host step.
    parser.add_argument("--sessiond-url", dest="attendance_url", default=None)
    parser.add_argument(
        "--sessiond-socket",
        dest="attendance_socket",
        type=Path,
        default=None,
        help="attendance's Unix socket. When given, it wins over --sessiond-url",
    )


def _litellm_url(args: argparse.Namespace) -> str:
    return args.litellm_base_url or url(Port.LITELLM)


def _attendance_url(args: argparse.Namespace) -> str:
    return args.attendance_url or url(Port.ATTENDANCE)


def _master_key() -> str:
    # invariant 13: never on argv, never in a URL, never in a log line. An
    # environment variable is the one channel that satisfies all three.
    return os.environ.get("LITELLM_MASTER_KEY", "")


def _apply_plan(state_root: Path, family: str, report: Report) -> list[str]:
    lines = [
        f"family: {family}",
        f"validation: {'ok' if report.ok else 'INVALID'} "
        f"({report.errors} errors, {report.warnings} warnings)",
    ]
    if not report.ok:
        lines.append(
            "plan: stop here (invariant 19); no credential, grant, config or sandbox change"
        )
        return lines

    has_creds = paths.creds_path(state_root, family).is_file()
    lines.append(
        "plan: mint a new LiteLLM key and PEP token"
        if not has_creds
        else "plan: refresh the existing LiteLLM key's model and budget"
    )
    lines.append("plan: write the grant file and the config mount")
    sequence = paths.read_sandbox_sequence(state_root, family)
    lines.append(
        f"plan: create sandbox {paths.sandbox_id(family, 1)}"
        if sequence == 0
        else f"plan: sandbox {paths.sandbox_id(family, sequence)} already exists; leave it"
    )
    return lines


def _apply_once_command(
    args: argparse.Namespace, driver: SandboxDriver | None, litellm: LiteLLMKeys | None
) -> int:
    registry = load_registry(args.registry)
    report = registry.reports.get(args.family)
    if report is None:
        print(f"caregiver: no family '{args.family}' in {args.registry}", file=sys.stderr)
        return EXIT_USAGE

    for line in _apply_plan(args.state_root, args.family, report):
        print(line)

    if not args.write:
        return EXIT_OK if report.ok else EXIT_PROBLEM

    resolved_litellm = (
        litellm if litellm is not None else HttpLiteLLMKeys(_litellm_url(args), _master_key())
    )
    resolved_driver = driver if driver is not None else SbxDriver()
    result = apply_once(
        args.registry,
        args.family,
        state_root=args.state_root,
        image=args.image,
        python_image=args.image_python,
        driver=resolved_driver,
        litellm=resolved_litellm,
        switch=_optional_switch(args),
    )
    print(f"result: {'ok' if result.ok else 'not ok'}, state={result.status.state}")
    return EXIT_OK if result.ok else EXIT_PROBLEM


def _optional_switch(args: argparse.Namespace) -> SwitchClient | None:
    """The client `apply_once` asks for the handshake with, or `None`.

    `up` runs this verb BEFORE `attendance` is listening, and on a fresh host
    before its token file exists. Neither is an error: with no client the
    sandbox stays `creating` (contract 05 §4.2 rule 4), and the apply that
    runs once the service is up promotes it. A missing token must not fail
    the verb that builds the family in the first place.
    """
    try:
        return _switch_client(args)
    except SwitchError as exc:
        print(f"caregiver: no handshake this pass ({exc})", file=sys.stderr)
        return None


def _actors(
    args: argparse.Namespace,
    driver: SandboxDriver | None,
    litellm: LiteLLMKeys | None,
    switch: SwitchClient | None,
    units: UnitWriter | None,
) -> Actors:
    """Build the live clients, or take the fakes a test injected. Nothing
    here is built before `--write`: an `HttpSwitchClient` reads the token
    file in its constructor, and a plan run must not need one."""
    return Actors(
        driver=driver if driver is not None else SbxDriver(),
        litellm=litellm
        if litellm is not None
        else HttpLiteLLMKeys(_litellm_url(args), _master_key()),
        switch=switch if switch is not None else _switch_client(args),
        egress=EgressConfig(),
        units=units if units is not None else UserUnits(),
    )


def _released(args: argparse.Namespace) -> ReleasedImages | None:
    """The released file this run reads, or None.

    The default belongs to the real plane and to no other: a scratch run
    names a `--state-root` of its own and is given its images by its caller,
    so it must not pick up what the host's own `playpen` release installed.
    The same rule `--release-root` follows. No unit names the flag, so an
    installer can put a new unit file over an older `caregiver` tree."""
    if args.released_images is not None:
        return ReleasedImages(args.released_images)

    if args.state_root != paths.STATE_ROOT:
        return None

    return ReleasedImages(RELEASED_IMAGES)


def _images(args: argparse.Namespace) -> SandboxImages:
    """One read, for a command that runs one pass."""
    given = SandboxImages(base=args.image, python=args.image_python)
    released = _released(args)

    return released.current(given) if released is not None else given


def _switch_client(args: argparse.Namespace) -> SwitchClient:
    token = read_token(paths.caregiver_token_path(args.state_root))
    if args.attendance_socket is not None:
        return HttpSwitchClient.over_socket(args.attendance_socket, token)

    return HttpSwitchClient(_attendance_url(args), token)


def _watch_plan(args: argparse.Namespace, families: Sequence[str]) -> list[str]:
    # What a pass will use, not what the flags say: a `playpen` release's
    # references win over them (`released.py`).
    images = _images(args)

    return [
        f"registry: {args.registry}",
        f"state: {args.state_root}",
        f"image: {images.base}",
        f"image (python): {images.python or '(none configured)'}",
        f"families: {', '.join(families) if families else '(none)'}",
        f"attendance: {_attendance_address(args)}, "
        f"token {paths.caregiver_token_path(args.state_root)}",
    ]


def _attendance_address(args: argparse.Namespace) -> str:
    """What `_switch_client` dials, for the banner. It once printed the LAN
    URL while the socket was in use, and sent a reader looking for a listener
    that production does not have."""
    if args.attendance_socket is not None:
        return f"socket {args.attendance_socket}"

    return _attendance_url(args)


def _serve_command(
    args: argparse.Namespace,
    driver: SandboxDriver | None,
    litellm: LiteLLMKeys | None,
    switch: SwitchClient | None,
    units: UnitWriter | None,
) -> int:
    registry = load_registry(args.registry)
    for line in _watch_plan(args, sorted(registry.reports)):
        print(line)

    # Not in `_watch_plan`: only `serve` holds an interval to probe over,
    # so only `serve` declares these three.
    print(
        f"chaperone watch: {_pep_url(args) or 'OFF (nothing watches the PEP)'}, "
        f"every {args.pep_probe_interval_s:.0f}s, "
        f"fault after {args.pep_unreachable_after_s:.0f}s of silence"
    )
    print(f"release root: {args.release_root}")
    if not args.write:
        print("plan: watch and converge. Pass --write to start.")
        return EXIT_OK

    # A scratch state root beside root's REAL release root would file a
    # scratch run's requests into the spool root drains for real.
    if args.state_root != paths.STATE_ROOT and args.release_root == paths.RELEASE_ROOT:
        print(
            "caregiver: a --state-root of its own needs a --release-root of its own",
            file=sys.stderr,
        )
        return EXIT_USAGE

    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
    control = SignalControl()
    control.install()
    config = LoopConfig(
        registry_root=args.registry,
        state_root=args.state_root,
        image=args.image,
        python_image=args.image_python,
        released=_released(args),
        poll_interval_s=args.poll_interval_s,
        max_concurrent_passes=args.max_concurrent_passes,
        stop_grace_s=args.stop_grace_s,
        # `stage7-releases.md` §4.1 step 3. Derived from THIS run's two
        # roots, so a scratch run never writes into root's real spool, and
        # a host where root has not made the spool yet gets a quiet pass
        # rather than an error every two seconds.
        mcp=paths_under(Path(args.state_root), Path(args.release_root)),
        chaperone=_pep_watch(args),
    )
    try:
        serve(config, _actors(args, driver, litellm, switch, units), control)
    except SwitchError as exc:
        print(f"caregiver: {exc}", file=sys.stderr)
        return EXIT_PROBLEM

    return EXIT_OK


def _reconcile_once_command(
    args: argparse.Namespace,
    driver: SandboxDriver | None,
    litellm: LiteLLMKeys | None,
    switch: SwitchClient | None,
    units: UnitWriter | None,
) -> int:
    registry = load_registry(args.registry)
    if args.family not in registry.reports:
        print(f"caregiver: no family '{args.family}' in {args.registry}", file=sys.stderr)
        return EXIT_USAGE

    for line in _watch_plan(args, [args.family]):
        print(line)

    if not args.write:
        print("plan: one reconcile pass. Pass --write to run it.")
        return EXIT_OK

    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
    try:
        actors = _actors(args, driver, litellm, switch, units)
    except SwitchError as exc:
        print(f"caregiver: {exc}", file=sys.stderr)
        return EXIT_PROBLEM

    images = _images(args)
    result = reconcile_family(
        registry,
        args.family,
        state_root=args.state_root,
        image=images.base,
        python_image=images.python,
        actors=actors,
        spend=SpendRead.READ,
    )
    print(result.log_line())
    return EXIT_OK if result.ok else EXIT_PROBLEM


def _rotate_command(args: argparse.Namespace, litellm: LiteLLMKeys | None) -> int:
    registry = load_registry(args.registry)
    family = registry.families.get(args.family)
    if family is None:
        print(f"caregiver: no valid family '{args.family}'", file=sys.stderr)
        return EXIT_USAGE

    print(f"family: {args.family}")
    print(f"plan: rotate scope={args.scope} mode={args.mode} reason={args.reason}")
    print("plan: the KEY half runs brake, delete, mint, with no overlap")
    if not args.write:
        return EXIT_OK

    resolved = (
        litellm if litellm is not None else HttpLiteLLMKeys(_litellm_url(args), _master_key())
    )
    request = RotateRequest(
        scope=Scope(args.scope), mode=Mode(args.mode), reason=Reason(args.reason)
    )
    try:
        outcome = rotate(
            request,
            family,
            _index_of(registry),
            state_root=args.state_root,
            litellm=resolved,
        )
    except RotateError as exc:
        print(f"caregiver: {exc}", file=sys.stderr)
        return EXIT_PROBLEM

    print(f"result: epoch={outcome.epoch} state={outcome.state} ({outcome.note})")
    return EXIT_OK


def _index_of(registry: Registry) -> Index:
    return Index(
        kinds={name: one.kind for name, one in registry.families.items()},
        servers=registry.servers,
        skills=registry.skills,
    )


def _every_family(state_root: Path) -> list[str]:
    families_dir = state_root / paths.FAMILIES_DIR
    if not families_dir.is_dir():
        return []

    return sorted(child.name for child in families_dir.iterdir() if child.is_dir())


def _read_status_json(state_root: Path, family: str) -> dict[str, Any] | None:
    try:
        body = json.loads(paths.status_path(state_root, family).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None

    if not isinstance(body, dict):
        return None

    return cast("dict[str, Any]", body)


def _print_status_text(doc: dict[str, Any]) -> None:
    print(f"{doc.get('family')}: {doc.get('state')} (kind={doc.get('kind')})")
    print(f"  registry_rev={doc.get('registry_rev')} applied_rev={doc.get('applied_rev')}")
    _print_pep_line(doc)
    faults = doc.get("faults")
    if not isinstance(faults, list):
        return

    for raw in cast("list[Any]", faults):
        if isinstance(raw, dict):
            _print_fault(cast("dict[str, Any]", raw))


def _print_fault(fault: dict[str, Any]) -> None:
    """One fault, with its message.

    Contract 05 §3.3's `message` is where several codes keep their whole
    content: `mcp_install_held` carries root's reason for refusing a
    release there, and a code alone would send the reader to the journal
    — which is the thing that row exists to avoid. It is bounded by its
    writer, and this only ever prints what a document already holds."""
    message = fault.get("message")
    detail = f": {message}" if isinstance(message, str) and message else ""
    print(f"  fault: {fault.get('code')} (blocks_turns={fault.get('blocks_turns')}){detail}")


def _print_pep_line(doc: dict[str, Any]) -> None:
    """Contract 05 §2.1's `pep` block, in the one place an operator looks
    first. `off` is printed as loudly as `unreachable`: a watch nobody
    turned on and a PEP nobody can reach both mean the fleet is running
    unwatched, and a PEP outage then goes unnoticed.

    A document with no block at all was written by an older `caregiver`,
    and saying so beats printing nothing."""
    block = doc.get("pep")
    if not isinstance(block, dict):
        print("  pep: no watch block (written by an older caregiver)")
        return

    chaperone = cast("dict[str, Any]", block)
    print(f"  pep: {chaperone.get('watch')} {chaperone.get('url') or '(no address configured)'}")


def _status_command(args: argparse.Namespace) -> int:
    names = [args.family] if args.family else _every_family(args.state_root)
    if not names:
        print("caregiver: no family has a status document yet", file=sys.stderr)
        return EXIT_OK

    documents: list[dict[str, Any]] = []
    for name in names:
        doc = _read_status_json(args.state_root, name)
        if doc is None:
            print(f"caregiver: no status document for '{name}'", file=sys.stderr)
            return EXIT_USAGE

        documents.append(doc)

    if args.json:
        print(json.dumps(documents if args.family is None else documents[0], indent=2))
        return EXIT_OK

    for doc in documents:
        _print_status_text(doc)

    return EXIT_OK


def _delete_plan(state_root: Path, family: str) -> list[str]:
    sequence = paths.read_sandbox_sequence(state_root, family)
    return [
        f"family: {family}",
        "plan: delete the LiteLLM key, the grant file and creds.json",
        f"plan: destroy {sequence} sandbox(es)" if sequence else "plan: no sandbox to destroy",
        f"plan: remove {paths.family_dir(state_root, family)}",
    ]


def _delete_command(
    args: argparse.Namespace, driver: SandboxDriver | None, litellm: LiteLLMKeys | None
) -> int:
    for line in _delete_plan(args.state_root, args.family):
        print(line)

    if not args.write:
        return EXIT_OK

    resolved_litellm = (
        litellm if litellm is not None else HttpLiteLLMKeys(_litellm_url(args), _master_key())
    )
    resolved_driver = driver if driver is not None else SbxDriver()
    delete_family(
        args.family,
        state_root=args.state_root,
        driver=resolved_driver,
        litellm=resolved_litellm,
        egress=tuple(args.egress),
    )
    print("deleted.")
    return EXIT_OK


def main(
    argv: Sequence[str] | None = None,
    *,
    driver: SandboxDriver | None = None,
    litellm: LiteLLMKeys | None = None,
    switch: SwitchClient | None = None,
    units: UnitWriter | None = None,
) -> int:
    """The four keyword arguments are for a test to inject fakes. The real
    CLI never passes them: each `--write` path builds the live client
    itself, once it knows the action will actually run."""
    args = _parser().parse_args(argv)
    try:
        return _run(args, driver, litellm, switch, units)
    except ConfigError as exc:
        print(f"caregiver: {exc}", file=sys.stderr)
        return EXIT_USAGE


def _run(
    args: argparse.Namespace,
    driver: SandboxDriver | None,
    litellm: LiteLLMKeys | None,
    switch: SwitchClient | None,
    units: UnitWriter | None,
) -> int:
    """The verb `args` names."""
    if args.command == "serve":
        return _serve_command(args, driver, litellm, switch, units)

    if args.command == "reconcile-once":
        return _reconcile_once_command(args, driver, litellm, switch, units)

    if args.command == "rotate":
        return _rotate_command(args, litellm)

    if args.command == "apply-once":
        return _apply_once_command(args, driver, litellm)

    if args.command == "status":
        return _status_command(args)

    if args.command == "delete":
        return _delete_command(args, driver, litellm)

    return EXIT_USAGE  # unreachable: the subparsers group is required=True


if __name__ == "__main__":
    raise SystemExit(main())
