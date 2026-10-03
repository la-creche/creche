"""Entry point: environment-driven config, then uvicorn.

Required env: PEP_REWORK_DIR (the rework state root, contract 04: grants/,
audit/ and faults/pep/ hang off it) and PEP_AUDIT_DIR (the log for a request
that resolved to no family; NOT contract 04 §6's audit). Optional:
PEP_UPSTREAMS (upstreams.yaml path), PEP_SECRETS (sops-encrypted yaml),
PEP_BIND (host:port, default the site's LAN address on 8300), HA_URL (Home
Assistant base for the `ha_call` verb, default the site's),
PEP_SESSIOND_SOCKET + PEP_DELEGATE_TOKEN_FILE (contract 04 §7: attendance's
delegate door; without both, invoke_agent stays a seam. The token file is
read on each delegate call, not here, because attendance writes it at ITS
start and this process can start first), PEP_DISPATCH_TOKEN_FILE
(contract 02 §13.4: attendance's dispatch door, on the same socket with its own
bearer; without it enqueue and job_status stay seams),
PEP_RELEASE_REQUESTS_DIR (`stage7-releases.md` §2.3: root's release spool;
without it the `release` verb stays a seam), PEP_APPROVAL_URL (contract 04
§8.4: the protected Node-RED hook a gate is pushed to; without it, and
without both approval tokens in PEP_SECRETS, a gated action stays a seam),
PEP_FAULT_SWEEP_INTERVAL_S (contract 04 §1.6 rule 7: how often a faulted
family's grant file is re-read with no call to drive it; a value that is not
a positive number takes the default).

`PEP_REWORK_DIR` is required: a PEP without it would resolve no bearer at
all. So is a bind: `AGENT_LAN_ADDRESS` from the site file, or `PEP_BIND`
(`site.py`).

A token is read from its FILE, never taken from the environment: the unit
file lives in git and a process's environment is not a place for a bearer
(invariant 13). The two approval bearers ride inside PEP_SECRETS for the
same reason."""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import uvicorn

from . import site
from .app import PepConfig, create_app
from .delegate import DoorTokenError, read_door_token
from .fault_sweep import FAULT_SWEEP_INTERVAL_S
from .reload_wiring import RosterSource
from .secrets import SecretsError, SecretsFormatError, load_secret_dir, load_sops_secrets


def _roster(upstreams_path: str | None, secrets_path: str | None) -> RosterSource | None:
    """`stage7-releases.md` §4.4: where the lifespan reads the roster at
    start, and where every `SIGHUP` re-reads it.

    A PEP with no `PEP_UPSTREAMS` has no roster to re-read, so it keeps the
    boot-time pool. That is the fail-closed end: a reload of nothing would
    remove every upstream the process was constructed with.

    `PEP_SECRETS_DIR` is §4.3's per-secret store, which is what the intake
    writes when the operator pastes a value. Without it a reload sees only the
    monolith and a pasted credential never reaches its server.

    `PEP_UPSTREAMS_GENERATED` is the roster root writes at the end of a
    verified `mcp-servers` release. Without it a released server is
    installed and never served.
    """
    if not upstreams_path:
        return None

    secrets_dir = os.environ.get("PEP_SECRETS_DIR")

    return RosterSource(
        upstreams_file=Path(upstreams_path),
        secrets_file=Path(secrets_path) if secrets_path else None,
        secrets_dir=Path(secrets_dir) if secrets_dir else None,
        generated_file=_generated_roster(),
    )


def _generated_roster() -> Path | None:
    raw = os.environ.get("PEP_UPSTREAMS_GENERATED", "").strip()

    return Path(raw) if raw else None


def _sweep_interval() -> float:
    """`PEP_FAULT_SWEEP_INTERVAL_S`, or the default when it is unusable.

    A typo must not stop the sweep: the fault it clears blocks every turn of
    the family, so a host that misspells the interval is better served by the
    default than by a loop that spins or never runs.
    """
    raw = os.environ.get("PEP_FAULT_SWEEP_INTERVAL_S", "").strip()
    try:
        seconds = float(raw)
    except ValueError:
        return FAULT_SWEEP_INTERVAL_S

    if seconds <= 0:
        return FAULT_SWEEP_INTERVAL_S

    return seconds


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    # httpx logs every request URL at INFO; a URL can carry a key. WARNING only.
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    try:
        rework_dir = Path(os.environ["PEP_REWORK_DIR"])
        audit_dir = Path(os.environ["PEP_AUDIT_DIR"])
    except KeyError as exc:
        print(f"missing required env var {exc}", file=sys.stderr)
        return 2

    try:
        bind = site.bind(os.environ)
    except site.ConfigError as exc:
        return _no_site(exc)

    upstreams_path = os.environ.get("PEP_UPSTREAMS")
    secrets_path = os.environ.get("PEP_SECRETS")
    # Not read here. The app's lifespan reads it on the path a `SIGHUP`
    # takes, with the SAME merge, so a restart serves what the last
    # `mcp-servers` release installed and a file that will not
    # parse starts the PEP with no MCP server instead of stopping it here.
    roster = _roster(upstreams_path, secrets_path)
    secrets: dict[str, str] = {}
    # §4.3's per-secret store, read at start as well as on every reload: a
    # value pasted before this process started is as real as one pasted
    # after it.
    secrets_dir = os.environ.get("PEP_SECRETS_DIR")
    if secrets_path:
        try:
            secrets = load_sops_secrets(Path(secrets_path))
        except SecretsFormatError as exc:
            return _unparsable(exc)
        except SecretsError as exc:
            # Fail closed per upstream, not per process: the PEP still serves
            # builtin tools; upstreams needing secrets stay absent from every
            # manifest until decryption works (e.g. recipient not added yet).
            logging.getLogger("agent_pep").error(
                "secrets unavailable (%s); MCP upstreams needing them will not start", exc
            )

    if secrets_dir:
        try:
            secrets |= load_secret_dir(Path(secrets_dir))
        except SecretsFormatError as exc:
            return _unparsable(exc)

    host, _, port = bind.rpartition(":")
    attendance_socket = os.environ.get("PEP_SESSIOND_SOCKET")
    # `stage7-releases.md` §2.3. Unset leaves `release` a named seam.
    release_requests_dir = os.environ.get("PEP_RELEASE_REQUESTS_DIR")
    delegate_token_file = _delegate_token_file()
    dispatch_token_file = _door_token_file("PEP_DISPATCH_TOKEN_FILE", "enqueue and job_status")
    app = create_app(
        PepConfig(
            audit_dir=audit_dir,
            rework_dir=rework_dir,
            secrets=secrets,
            tei_url=site.tei_url(os.environ),
            ha_url=site.ha_url(os.environ),
            fault_sweep_interval_s=_sweep_interval(),
            roster=roster,
            attendance_socket=Path(attendance_socket) if attendance_socket else None,
            delegate_token_file=delegate_token_file,
            dispatch_token_file=dispatch_token_file,
            release_requests_dir=Path(release_requests_dir) if release_requests_dir else None,
            # Contract 04 §8.4. The URL is configuration; both bearers ride in
            # the sops file, not the unit, because the unit is in git. Any one
            # of the three missing leaves a gated action a seam.
            approval_hook_url=os.environ.get("PEP_APPROVAL_URL", ""),
            approval_hook_token=secrets.get("approval_hook_token", ""),
            approval_callback_token=secrets.get("approval_callback_token", ""),
        )
    )
    uvicorn.run(app, host=host or "127.0.0.1", port=int(port))
    return 0


def _unparsable(exc: SecretsFormatError) -> int:
    """Stop, in one line. The file decrypted and will not parse, so an
    operator must fix it, and a PEP without its credentials serves nothing
    useful. The line names a position, never the content.

    `os.EX_CONFIG`, so `systemctl status` reads `status=78/CONFIG`.
    """
    logging.getLogger("agent_pep").error("secrets unreadable, the PEP stops: %s", exc)

    return os.EX_CONFIG


def _no_site(exc: site.ConfigError) -> int:
    """Stop, in one line, as `_unparsable` does. A PEP with no address to
    bind would serve nobody, and guessing one would be somebody's host."""
    logging.getLogger("agent_pep").error("no LAN address, the PEP stops: %s", exc)

    return os.EX_CONFIG


def _delegate_token_file() -> Path | None:
    """The PATH of the `door-delegate` bearer, from `PEP_DELEGATE_TOKEN_FILE`
    (contract 02 §3.1, contract 04 §7.3)."""
    return _door_token_file("PEP_DELEGATE_TOKEN_FILE", "invoke_agent")


def _door_token_file(variable: str, verbs: str) -> Path | None:
    """The PATH of one door bearer, from its environment variable.

    The file is NOT read here. `attendance` writes it when it starts and this
    process can start first, so a value read at boot would be empty for the
    life of the process and every rotation would need a restart. The door
    reads it on each call and fails that call closed when it cannot.

    The read below is a startup PROBE only: it turns a typo in the unit into
    one journal line on boot rather than into a failure on the first call an
    hour later. Its result changes nothing.
    """
    path = os.environ.get(variable, "").strip()
    if not path:
        return None

    found = Path(path)
    try:
        read_door_token(found)
    except DoorTokenError as exc:
        logging.getLogger("agent_pep").warning(
            "%s; %s fails closed until the file is readable", exc, verbs
        )

    return found


if __name__ == "__main__":
    sys.exit(main())
