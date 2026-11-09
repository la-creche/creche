"""caregiver serve | reconcile-once | rotate | apply-once | status |
delete.

Every mutating verb prints its plan and needs `--write` to act -- a plan
readable before anything touches LiteLLM, `policy.toml` or the vmm.
`status` is read-only and needs no gate.

`serve` is the one the operator actually runs, through
`systemd/creche-caregiver.service`. The other verbs exist for a host where
something has gone wrong and for the tests."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Final, cast

from agent_family import FenceError, Fences, Index, Registry, Report, load_registry, read_fences

from . import paths
from .apply import apply_once
from .atomic import read_json
from .chaperone_watch import (
    PEP_PROBE_INTERVAL_S,
    PEP_UNREACHABLE_AFTER_S,
    HttpPepProbe,
    PepWatch,
)
from .delete import delete_family
from .lan import ConfigError, Port, url
from .litellm_keys import HttpLiteLLMKeys, LiteLLMError, LiteLLMKeys
from .loop import LoopConfig, SignalControl, serve
from .policy_fleet import ChaperoneChecker, FleetPolicy, PolicyChecker, PolicyTarget
from .reconcile import Actors, SpendRead, has_family_file, reconcile_family
from .rotate import (
    Mode,
    Reason,
    RotateError,
    RotateOutcome,
    RotateRequest,
    Scope,
    ServingFamily,
    ValidFamily,
    rotate,
    rotate_serving,
    serving_family,
    valid_family,
)
from .timers import UnitWriter, UserUnits
from .vmm import VMM_SOCKET_ENV, HttpVmmClient, VmmClient, VmmError, socket_from

EXIT_OK: Final = 0
EXIT_PROBLEM: Final = 1
EXIT_USAGE: Final = 2
#: The fences of the host do not read, or the env key of the vmm socket is
#: absent: the exit status of `creche-env` for a fault of the env files.
#: Each verb that applies the registry needs the fences. `serve` and
#: `delete` need the socket to act.
EXIT_CONFIG: Final = 78

# The two plane URLs below default to the site's LAN address (`lan.py`),
# never loopback, built when a verb needs one: a verb that reaches no plane
# needs no address. An explicit flag always wins.
#
# LiteLLM:  `--litellm-base-url`, else `url(Port.LITELLM)`.
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
    apply_cmd.add_argument("--state-root", type=Path, default=paths.STATE_ROOT)
    apply_cmd.add_argument("--litellm-base-url", default=None)
    _chaperone_args(apply_cmd)
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
    _pep_watch_args(serve_cmd)

    once_cmd = sub.add_parser("reconcile-once", help="one reconcile pass over one family")
    _watch_args(once_cmd)
    once_cmd.add_argument("family")

    rotate_cmd = sub.add_parser("rotate", help="mint a new family key and token")
    rotate_cmd.add_argument("registry", type=Path)
    rotate_cmd.add_argument("family")
    rotate_cmd.add_argument("--state-root", type=Path, default=paths.STATE_ROOT)
    rotate_cmd.add_argument("--litellm-base-url", default=None)
    _chaperone_args(rotate_cmd)
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
        "delete", help="delete a family: its credentials, then its sessions on the vmm"
    )
    delete_cmd.add_argument("family")
    delete_cmd.add_argument("--state-root", type=Path, default=paths.STATE_ROOT)
    delete_cmd.add_argument("--litellm-base-url", default=None)
    _chaperone_args(delete_cmd)
    delete_cmd.add_argument("--write", action="store_true")

    return parser


def _watch_args(parser: argparse.ArgumentParser) -> None:
    """What `serve` and `reconcile-once` both need. One place, so the unit
    file and a hand-run pass cannot drift apart."""
    parser.add_argument("registry", type=Path, help="the registry root, holding families/")
    parser.add_argument("--state-root", type=Path, default=paths.STATE_ROOT)
    parser.add_argument("--litellm-base-url", default=None)
    _chaperone_args(parser)
    parser.add_argument("--write", action="store_true", help="act. Without it, print the plan only")


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


def _chaperone_args(parser: argparse.ArgumentParser) -> None:
    """Where the one `policy.toml` goes, and the binary that checks it.
    Every verb that moves a grant or a token declares both."""
    parser.add_argument(
        "--chaperone-config-dir",
        type=Path,
        default=None,
        help="the configuration directory of chaperone: install.toml and plugins/ of root, "
        f"and the policy.toml this service writes. Default {paths.CHAPERONE_CONFIG_DIR}, "
        f"and <state-root>/{paths.CHAPERONE_SCRATCH_DIR} for a --state-root of its own",
    )
    parser.add_argument(
        "--chaperone-bin",
        type=Path,
        default=paths.CHAPERONE_BIN,
        help="the chaperone binary. This service runs its check verb before each write "
        "of policy.toml, and nothing else",
    )


def _chaperone_config_dir(args: argparse.Namespace) -> Path:
    """The flag, else the host directory. A run with a `--state-root` of
    its own writes into that root, never into the directory of the host."""
    if args.chaperone_config_dir is not None:
        return Path(args.chaperone_config_dir)

    if args.state_root != paths.STATE_ROOT:
        return Path(args.state_root) / paths.CHAPERONE_SCRATCH_DIR

    return paths.CHAPERONE_CONFIG_DIR


def _fleet_policy(args: argparse.Namespace, checker: PolicyChecker | None) -> FleetPolicy:
    """The target and the checker of `policy.toml`, or the checker a test
    injected."""
    config_dir = _chaperone_config_dir(args)
    resolved = (
        checker
        if checker is not None
        else ChaperoneChecker(
            binary=Path(args.chaperone_bin),
            config_dir=config_dir,
            plugins_dir=config_dir / paths.CHAPERONE_PLUGINS_DIR,
        )
    )
    return FleetPolicy(PolicyTarget(config_dir), resolved)


def _chaperone_line(args: argparse.Namespace) -> str:
    return f"policy.toml: {_chaperone_config_dir(args)}, checked by {args.chaperone_bin}"


def _litellm_url(args: argparse.Namespace) -> str:
    return args.litellm_base_url or url(Port.LITELLM)


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
        lines.append("plan: stop here (invariant 19); no credential, grant or config change")
        return lines

    has_creds = paths.creds_path(state_root, family).is_file()
    lines.append(
        "plan: mint a new LiteLLM key and PEP token"
        if not has_creds
        else "plan: refresh the existing LiteLLM key's model and budget"
    )
    lines.append("plan: write the table of policy.toml, the config directory and the reach files")
    return lines


def _apply_once_command(
    args: argparse.Namespace,
    fences: Fences,
    litellm: LiteLLMKeys | None,
    checker: PolicyChecker | None,
) -> int:
    registry = load_registry(args.registry, fences=fences)
    report = registry.reports.get(args.family)
    if report is None or not has_family_file(registry, args.family):
        print(f"caregiver: no family '{args.family}' in {args.registry}", file=sys.stderr)
        return EXIT_USAGE

    for line in _apply_plan(args.state_root, args.family, report):
        print(line)

    print(_chaperone_line(args))

    if not args.write:
        return EXIT_OK if report.ok else EXIT_PROBLEM

    resolved_litellm = (
        litellm if litellm is not None else HttpLiteLLMKeys(_litellm_url(args), _master_key())
    )
    result = apply_once(
        args.registry,
        args.family,
        state_root=args.state_root,
        fences=fences,
        litellm=resolved_litellm,
        policy=_fleet_policy(args, checker),
    )
    print(f"result: {'ok' if result.ok else 'not ok'}, state={result.status.state}")
    return EXIT_OK if result.ok else EXIT_PROBLEM


def _actors(
    args: argparse.Namespace,
    litellm: LiteLLMKeys | None,
    units: UnitWriter | None,
    checker: PolicyChecker | None,
) -> Actors:
    """Build the live clients, or take the fakes a test injected. Nothing
    here is built before `--write`."""
    return Actors(
        litellm=litellm
        if litellm is not None
        else HttpLiteLLMKeys(_litellm_url(args), _master_key()),
        units=units if units is not None else UserUnits(),
        policy=_fleet_policy(args, checker),
    )


def _vmm(vmm: VmmClient | None, environ: Mapping[str, str]) -> VmmClient | None:
    """The client a test injected, or the vmm on the socket that
    `CAREGIVER_VMM_SOCKET` names (decision CH20). None after one line that
    names the key."""
    if vmm is not None:
        return vmm

    socket = socket_from(environ)
    if socket is None:
        print(
            f"caregiver: {VMM_SOCKET_ENV} is not set. It names vmm.sock, "
            "the socket of the vmm (decision CH20)",
            file=sys.stderr,
        )
        return None

    return HttpVmmClient.over_socket(socket)


def _watch_plan(args: argparse.Namespace, families: Sequence[str]) -> list[str]:
    return [
        f"registry: {args.registry}",
        f"state: {args.state_root}",
        f"families: {', '.join(families) if families else '(none)'}",
        _chaperone_line(args),
    ]


def _serve_command(
    args: argparse.Namespace,
    fences: Fences,
    vmm: VmmClient | None,
    litellm: LiteLLMKeys | None,
    units: UnitWriter | None,
    checker: PolicyChecker | None,
    environ: Mapping[str, str],
) -> int:
    registry = load_registry(args.registry, fences=fences)
    families = sorted(name for name in registry.reports if has_family_file(registry, name))
    for line in _watch_plan(args, families):
        print(line)

    # Not in `_watch_plan`: only `serve` holds an interval to probe over,
    # so only `serve` declares these three.
    print(
        f"chaperone watch: {_pep_url(args) or 'OFF (nothing watches the PEP)'}, "
        f"every {args.pep_probe_interval_s:.0f}s, "
        f"fault after {args.pep_unreachable_after_s:.0f}s of silence"
    )
    if not args.write:
        print("plan: watch and converge. Pass --write to start.")
        return EXIT_OK

    resolved_vmm = _vmm(vmm, environ)
    if resolved_vmm is None:
        return EXIT_CONFIG

    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
    control = SignalControl()
    control.install()
    config = LoopConfig(
        registry_root=args.registry,
        state_root=args.state_root,
        fences=fences,
        vmm=resolved_vmm,
        poll_interval_s=args.poll_interval_s,
        max_concurrent_passes=args.max_concurrent_passes,
        stop_grace_s=args.stop_grace_s,
        chaperone=_pep_watch(args),
    )
    serve(config, _actors(args, litellm, units, checker), control)
    return EXIT_OK


def _reconcile_once_command(
    args: argparse.Namespace,
    fences: Fences,
    litellm: LiteLLMKeys | None,
    units: UnitWriter | None,
    checker: PolicyChecker | None,
) -> int:
    registry = load_registry(args.registry, fences=fences)
    if not has_family_file(registry, args.family):
        print(f"caregiver: no family '{args.family}' in {args.registry}", file=sys.stderr)
        return EXIT_USAGE

    for line in _watch_plan(args, [args.family]):
        print(line)

    if not args.write:
        print("plan: one reconcile pass. Pass --write to run it.")
        return EXIT_OK

    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
    result = reconcile_family(
        registry,
        args.family,
        state_root=args.state_root,
        actors=_actors(args, litellm, units, checker),
        spend=SpendRead.READ,
    )
    print(result.log_line())
    return EXIT_OK if result.ok else EXIT_PROBLEM


def _rotate_command(
    args: argparse.Namespace,
    fences: Fences,
    litellm: LiteLLMKeys | None,
    checker: PolicyChecker | None,
) -> int:
    registry = load_registry(args.registry, fences=fences)
    family = valid_family(registry, args.family, state_root=args.state_root)
    serving = serving_family(registry, args.family, state_root=args.state_root)
    if family is None and serving is None:
        print(f"caregiver: no valid family '{args.family}'", file=sys.stderr)
        return EXIT_USAGE

    print(f"family: {args.family}")
    print(f"plan: rotate scope={args.scope} mode={args.mode} reason={args.reason}")
    print("plan: the KEY half runs brake, delete, mint, with no overlap")
    if family is None:
        print("plan: the family file is invalid or refused, so the grants stay as applied")

    print(_chaperone_line(args))

    if not args.write:
        return EXIT_OK

    resolved = (
        litellm if litellm is not None else HttpLiteLLMKeys(_litellm_url(args), _master_key())
    )
    request = RotateRequest(
        scope=Scope(args.scope), mode=Mode(args.mode), reason=Reason(args.reason)
    )
    try:
        outcome = _rotated(
            request,
            registry,
            family,
            serving,
            args.state_root,
            resolved,
            _fleet_policy(args, checker),
        )
    except RotateError as exc:
        print(f"caregiver: {exc}", file=sys.stderr)
        return EXIT_PROBLEM

    print(f"result: epoch={outcome.epoch} state={outcome.state} ({outcome.note})")
    if outcome.policy_error is not None:
        print(
            f"caregiver: policy.toml does not hold the new digests: {outcome.policy_error}",
            file=sys.stderr,
        )
        return EXIT_PROBLEM

    return EXIT_OK


def _rotated(
    request: RotateRequest,
    registry: Registry,
    family: ValidFamily | None,
    serving: ServingFamily | None,
    state_root: Path,
    litellm: LiteLLMKeys,
    policy: FleetPolicy,
) -> RotateOutcome:
    """The family file when it can be applied. The applied definition, and
    the credentials alone, when it cannot."""
    if family is not None:
        return rotate(
            request,
            family,
            _index_of(registry),
            state_root=state_root,
            litellm=litellm,
            policy=policy,
            revision=registry.revision,
        )

    if serving is None:
        raise RotateError("no definition serves this family")

    return rotate_serving(request, serving, state_root=state_root, litellm=litellm, policy=policy)


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
    return read_json(paths.status_path(state_root, family))


def _print_status_text(doc: dict[str, Any]) -> None:
    print(f"{doc.get('family')}: {doc.get('state')} (kind={doc.get('kind')})")
    print(f"  registry_rev={doc.get('registry_rev')} applied_rev={doc.get('applied_rev')}")
    _print_error_line(doc)
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
    content, and a code alone would send the reader to the journal. It is
    bounded by its writer, and this only ever prints what a document
    already holds."""
    message = fault.get("message")
    detail = f": {message}" if isinstance(message, str) and message else ""
    print(f"  fault: {fault.get('code')} (blocks_turns={fault.get('blocks_turns')}){detail}")


def _print_error_line(doc: dict[str, Any]) -> None:
    """The first error of an `invalid` document: a refused field, or the
    one line of the assembly that left the family out of `policy.toml`."""
    block = doc.get("validation")
    if not isinstance(block, dict):
        return

    first = cast("dict[str, Any]", block).get("first_error")
    if isinstance(first, str) and first:
        print(f"  error: {first}")


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
    return [
        f"family: {family}",
        "plan: delete the LiteLLM key, the table of policy.toml and creds.json",
        "plan: remove config/, egress.json and repos.json",
        "plan: drop every session of the family on the vmm",
        f"plan: remove {paths.family_dir(state_root, family)}",
    ]


def _delete_command(
    args: argparse.Namespace,
    vmm: VmmClient | None,
    litellm: LiteLLMKeys | None,
    checker: PolicyChecker | None,
    environ: Mapping[str, str],
) -> int:
    for line in _delete_plan(args.state_root, args.family):
        print(line)

    print(_chaperone_line(args))

    if not args.write:
        return EXIT_OK

    resolved_vmm = _vmm(vmm, environ)
    if resolved_vmm is None:
        return EXIT_CONFIG

    resolved_litellm = (
        litellm if litellm is not None else HttpLiteLLMKeys(_litellm_url(args), _master_key())
    )
    kept = delete_family(
        args.family,
        state_root=args.state_root,
        vmm=resolved_vmm,
        litellm=resolved_litellm,
        policy=_fleet_policy(args, checker),
    )
    print("deleted.")
    if kept is not None:
        print(
            f"caregiver: policy.toml still holds the table of the family: {kept}",
            file=sys.stderr,
        )
        return EXIT_PROBLEM

    return EXIT_OK


def main(
    argv: Sequence[str] | None = None,
    *,
    vmm: VmmClient | None = None,
    litellm: LiteLLMKeys | None = None,
    units: UnitWriter | None = None,
    checker: PolicyChecker | None = None,
    environ: Mapping[str, str] | None = None,
) -> int:
    """The five keyword arguments are for a test to inject fakes and an
    environment. The real CLI never passes them: each `--write` path builds
    the live client itself, once it knows the action will actually run, and
    the fences and the vmm socket come from the environment of the
    process."""
    args = _parser().parse_args(argv)
    try:
        return _run(args, vmm, litellm, units, checker, environ)
    except ConfigError as exc:
        print(f"caregiver: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except (VmmError, LiteLLMError) as exc:
        # No master key, or a call that LiteLLM or the vmm refused in a step
        # with no handler of its own: a delete. The message holds no
        # secret: no client puts one into its error.
        print(f"caregiver: {exc}", file=sys.stderr)
        return EXIT_PROBLEM


def _run(
    args: argparse.Namespace,
    vmm: VmmClient | None,
    litellm: LiteLLMKeys | None,
    units: UnitWriter | None,
    checker: PolicyChecker | None,
    environ: Mapping[str, str] | None,
) -> int:
    """The verb `args` names."""
    env = environ if environ is not None else os.environ
    if args.command == "status":
        return _status_command(args)

    if args.command == "delete":
        return _delete_command(args, vmm, litellm, checker, env)

    fences = _fences(env)
    if fences is None:
        return EXIT_CONFIG

    if args.command == "serve":
        return _serve_command(args, fences, vmm, litellm, units, checker, env)

    if args.command == "reconcile-once":
        return _reconcile_once_command(args, fences, litellm, units, checker)

    if args.command == "rotate":
        return _rotate_command(args, fences, litellm, checker)

    if args.command == "apply-once":
        return _apply_once_command(args, fences, litellm, checker)

    return EXIT_USAGE  # unreachable: the subparsers group is required=True


def _fences(environ: Mapping[str, str]) -> Fences | None:
    """The fences of the host (`agent_family.fences`), or None after one
    line that names the fault. The unit reads them from the site file."""
    try:
        return read_fences(environ)
    except FenceError as error:
        print(f"caregiver: {error}", file=sys.stderr)
        return None


if __name__ == "__main__":
    raise SystemExit(main())
