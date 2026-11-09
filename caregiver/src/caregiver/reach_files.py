"""The two reach files of a family beside its config mount (contract 01
§6.1.1): `egress.json` and `repos.json`.

`egress.json` is the egress allowlist of the vmm proxy, the wire type
`FamilyEgress` of `creche-wire` (decisions CH6-4, CH69 rules 3 and 5). It
sits in the family dir beside `config/`, never inside it, so a swap of
`config/` never moves it (decision CH69 rule 6, CH45 rule 2). caregiver is
its only writer (CH5b), and the vmm reads it.

The file holds the canonical form of the `egress` list of the family file
(decision CH69 rule 5, CH70 rule 4):

1. Each host in lower case. The grammar takes either case, and the vmm
   compares two names byte for byte.
2. An explicit port. A bare host is port 443 (contract 01 §3.7 rule 7).
3. No entry twice. `GitHub.com` and `github.com:443` are one entry.
4. Sorted by host, then by port as a number.

The two plane endpoints never enter the file (decision CH6-2, contract 01
§3.7 rule 4). The vmm splices them with no allowlist lookup, and this
module reads only the family file. A family with no `egress` gets `{"hosts": []}` (decision CH69
rule 3).

A list whose file would pass `EGRESS_CAP`, the cap of the vmm (decision
CH70 rule 1), is refused (decision CH71). `render_egress_file` raises
`EgressTooLargeError`, and a pass calls it before any write. The family
then reads `invalid` with one error that names the cap, and the last good
`egress.json` stays, as for a refused `policy.toml` (invariant 19).

`repos.json` is the clone list of the vmm mirror, the wire type
`FamilyRepos` (decisions CH10-10, CH69 rules 4 and 5). It sits beside
`egress.json`, with the same mode and the same byte compare. It holds the
`repos` list of the family file, sorted. A family with no `repos` gets
`{"repos": []}`. `check_family` of `agent_family` refuses a name given
twice (decision CH73 rule 3), so the file has no name twice.

`repos.json` gets no refusal of its own. The vmm cap `REPOS_CAP` is 4 KiB
(decision CH73 rule 2), and the longest list that `check_family` passes,
32 names of 100 characters (decision CH69 rule 15), renders to 3476
bytes. `test_reach_files.py` holds that bound."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from agent_family import FamilyFile
from creche_wire import EgressHost, FamilyEgress, FamilyRepos, HostName, RepoList, RepoName

from . import paths
from .atomic import atomic_write

#: The mode of each reach file (contract 01 §6.1.1 rule 1).
REACH_FILE_MODE: Final = 0o640

#: The port of a bare host (contract 01 §3.7 rule 7).
BARE_HOST_PORT: Final = 443

#: `EGRESS_CAP` of `rust/crates/vmm-confine/src/family.rs`: the most bytes
#: of `egress.json` that the vmm reads (decisions CH70 rule 1, CH71).
#: `rust/crates/wire/vectors/family.json` holds the same value, and a test
#: of each side holds it equal.
EGRESS_CAP: Final = 64 * 1024


class EgressTooLargeError(ValueError):
    """The `egress.json` of a family would pass `EGRESS_CAP` (decision
    CH71). The message is the one error of the refusal."""


@dataclass(frozen=True)
class EgressFile:
    """The canonical text of `egress.json` of one family, at most
    `EGRESS_CAP` bytes. `render_egress_file` makes it."""

    family: str
    body: bytes


def render_egress_file(family: FamilyFile) -> EgressFile:
    """The `egress.json` of `family`, with no write.

    It raises `EgressTooLargeError` for a text over `EGRESS_CAP`, which the
    vmm would refuse and so allow nothing (decision CH71). A pass calls it
    before its first write."""
    body = _egress_text(family.egress)
    if len(body) > EGRESS_CAP:
        raise EgressTooLargeError(
            f"egress.json would have {len(body)} bytes, and the cap is {EGRESS_CAP}"
        )

    return EgressFile(family.name, body)


def write_egress_file(egress: EgressFile, state_root: Path) -> bool:
    """Write `egress.json` of one family when its bytes change. Answers
    whether it wrote.

    One `rename` lands the removals and the additions at once (invariant
    9). Bytes equal to the file on disk write nothing, so a pass that moves
    nothing touches no file."""
    return _write_when_changed(paths.egress_path(state_root, egress.family), egress.body)


def write_repos_file(family: FamilyFile, state_root: Path) -> bool:
    """Write `repos.json` of `family` when its bytes change, as
    `write_egress_file` writes `egress.json` (decision CH69 rule 6).
    Answers whether it wrote."""
    names = [RepoName(name) for name in sorted(family.repos)]
    body = _json_text(FamilyRepos(repos=RepoList(names)))
    return _write_when_changed(paths.repos_path(state_root, family.name), body)


def _egress_text(entries: list[str]) -> bytes:
    """The canonical text of `FamilyEgress` for the `egress` entries of a
    valid family file. `egress_problem` of `family/grammar.py` passed each
    entry, so the port is ASCII digits from 1 to 65535."""
    unique = {_host_port(entry) for entry in entries}
    hosts = [EgressHost(host=HostName(host), port=port) for host, port in sorted(unique)]
    return _json_text(FamilyEgress(hosts=hosts))


def _json_text(file: FamilyEgress | FamilyRepos) -> bytes:
    """The text of one reach file: JSON with an indent of 2 and one end of
    line."""
    return json.dumps(file.model_dump(mode="json"), indent=2).encode("utf-8") + b"\n"


def _host_port(entry: str) -> tuple[str, int]:
    host, colon, port = entry.partition(":")
    return host.lower(), int(port) if colon else BARE_HOST_PORT


def _write_when_changed(path: Path, body: bytes) -> bool:
    if _bytes_of(path) == body:
        return False

    atomic_write(path, body, mode=REACH_FILE_MODE)
    return True


def _bytes_of(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None
