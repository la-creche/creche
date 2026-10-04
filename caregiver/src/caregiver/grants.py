"""The PEP grant file: caregiver's writer side (contract 04 section 1).

`caregiver` validates first and never writes a grant file it knows to be
wrong (contract 04 section 1.3 rule 1 -- the caller enforces that, not this
module). `all` and `<server>__*` are expanded here, once, so the PEP never
reads a server file and never resolves a wildcard (contract 04 section
1.2)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, cast

from agent_family import FamilyFile, Index

from .atomic import atomic_write

GRANT_FILE_VERSION: Final = 2
GRANT_FILE_MODE: Final = 0o640

#: Contract 01 section 3.11's entry grammar: "<server>__<tool>" or
#: "<server>__*". Spelled here rather than imported: `agent_family.validate`
#: keeps its own copies private to its own checks (one meaning per module).
CALL_SEPARATOR: Final = "__"
WILDCARD: Final = "*"

#: Contract 04 section 1.2: these two have no counterpart in the family file.
#: `caregiver` writes its own defaults; changing them is a change to
#: contract 01, not a value a caller may pass here. The third limit,
#: `max_inflight_delegations`, IS a family field (contract 01 section 3.6.1),
#: so it is copied from the file rather than defaulted here.
DEFAULT_PEP_RPM: Final = 60
DEFAULT_MAX_OPEN_GATES: Final = 10

#: What `grant_file_matches` leaves out. `rev` changes on every write
#: (contract 04 section 1.2). The digests are rotation's: `rotate` writes
#: `creds.json` and then the file from its own process, so a pass holding
#: the older credentials must not write the new digest away.
UNCOMPARED: Final = frozenset({"rev", "token_sha256"})


@dataclass(frozen=True)
class GrantFile:
    """One family's grants, already expanded, as contract 04 section 1.2
    writes them."""

    family: str
    rev: str
    token_sha256: tuple[str, ...]
    model_alias: str
    tools: dict[str, list[str]]
    verbs: dict[str, Any]
    delegates: tuple[str, ...]
    max_inflight_delegations: int
    approval: tuple[str, ...]

    def as_json(self) -> dict[str, Any]:
        return {
            "version": GRANT_FILE_VERSION,
            "family": self.family,
            "rev": self.rev,
            "token_sha256": list(self.token_sha256),
            "model_alias": self.model_alias,
            "tools": self.tools,
            "verbs": self.verbs,
            "delegates": list(self.delegates),
            "approval": list(self.approval),
            "limits": {
                "pep_rpm": DEFAULT_PEP_RPM,
                "max_inflight_delegations": self.max_inflight_delegations,
                "max_open_gates": DEFAULT_MAX_OPEN_GATES,
            },
        }


def build_grant_file(
    family: FamilyFile, index: Index, *, rev: str, token_sha256: tuple[str, ...]
) -> GrantFile:
    """Everything the PEP needs to decide one call, expanded once so it
    never has to (contract 04 section 1.2)."""
    return GrantFile(
        family=family.name,
        rev=rev,
        token_sha256=token_sha256,
        model_alias=family.model.router,
        tools={
            server: list(index.granted_tools(family, server)) for server in sorted(family.tools)
        },
        verbs=_verbs_json(family),
        delegates=tuple(family.delegates),
        max_inflight_delegations=family.max_inflight_delegations,
        approval=tuple(_expand_approval(family, index)),
    )


def _verbs_json(family: FamilyFile) -> dict[str, Any]:
    """Only the granted verbs, each dumped whole. Contract 04 section 1.2's
    own example keeps `entity_id: null` inside a granted `ha_call` triple,
    so this never strips an inner null the way a recursive `exclude_none`
    dump of the whole `VerbsBlock` would."""
    verbs = family.verbs
    out: dict[str, Any] = {}
    if verbs.embed is not None:
        out["embed"] = {}

    if verbs.ha_call is not None:
        out["ha_call"] = verbs.ha_call.model_dump(mode="json")

    if verbs.enqueue is not None:
        out["enqueue"] = verbs.enqueue.model_dump(mode="json")

    if verbs.job_status is not None:
        out["job_status"] = {}

    if verbs.release is not None:
        out["release"] = verbs.release.model_dump(mode="json")

    return out


def _expand_approval(family: FamilyFile, index: Index) -> list[str]:
    """`<server>__*` expands to one entry per tool the file actually grants
    from that server (contract 04 section 1.2). A verb name, `invoke_agent`
    or a already-named `<server>__<tool>` entry passes through unchanged."""
    expanded: list[str] = []
    for entry in family.approval:
        server, separator, tool = entry.partition(CALL_SEPARATOR)
        if not separator or tool != WILDCARD:
            expanded.append(entry)
            continue

        expanded.extend(
            f"{server}{CALL_SEPARATOR}{name}" for name in index.granted_tools(family, server)
        )

    return expanded


def write_grant_file(path: Path, grant: GrantFile) -> None:
    """Contract 04 section 1.3: `caregiver` validates first (the caller's
    job), then writes atomically, mode 0640."""
    _write_body(path, grant.as_json())


def rewrite_digests(path: Path, family: str, *, rev: str, token_sha256: tuple[str, ...]) -> bool:
    """The grants this file already holds, under new digests (contract 04
    section 2.2). Which tokens the PEP accepts moves, and no grant does:
    rotation's write for a family whose file cannot be applied.

    Answers False and writes nothing unless `holds_grants`."""
    body = _kept_body(path, family)
    if body is None:
        return False

    _write_body(path, {**body, "rev": rev, "token_sha256": list(token_sha256)})
    return True


def holds_grants(path: Path, family: str) -> bool:
    """Is this a grant file `rewrite_digests` can keep? Only one this
    version wrote for this family. The PEP refuses every call on a missing,
    unreadable or malformed file, and one of another version or family
    holds content nobody here can answer for."""
    return _kept_body(path, family) is not None


def grant_file_matches(path: Path, grant: GrantFile) -> bool:
    """Does the file on disk already hold `grant`, `UNCOMPARED` aside? A
    missing, unreadable or malformed file does not: the PEP refuses every
    call on one of those, so each is a file to write again."""
    on_disk = _read_body(path)
    if on_disk is None:
        return False

    return _comparable(on_disk) == _comparable(grant.as_json())


def _write_body(path: Path, body: dict[str, Any]) -> None:
    atomic_write(path, json.dumps(body, indent=2).encode("utf-8") + b"\n", mode=GRANT_FILE_MODE)


def _read_body(path: Path) -> dict[str, Any] | None:
    try:
        on_disk = json.loads(path.read_bytes())
    except (OSError, ValueError, RecursionError):
        return None

    return cast(dict[str, Any], on_disk) if isinstance(on_disk, dict) else None


def _kept_body(path: Path, family: str) -> dict[str, Any] | None:
    body = _read_body(path)
    if body is None or body.get("version") != GRANT_FILE_VERSION or body.get("family") != family:
        return None

    return body


def _comparable(body: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in body.items() if key not in UNCOMPARED}


def delete_grant_file(path: Path) -> None:
    """Contract 04 section 1.3 rule 5: delete the file to revoke the
    family entirely. Missing already is not an error."""
    path.unlink(missing_ok=True)
