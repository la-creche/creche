"""The reach file of a family beside its config mount (contract 01 §6.1.1).

`egress.json` is the egress allowlist of the vmm proxy, the wire type
`FamilyEgress` of `creche-wire` (decisions CH6-4, CH69 rules 3 and 5). It
sits in the family dir beside `config/`, never inside it, so no sbx mount
sees a swap (decision CH69 rule 6, CH45 rule 2). caregiver is its only
writer (CH5b). Until the cut, no process on the live host reads it.

The file holds the canonical form of the `egress` list of the family file
(decision CH69 rule 5, CH70 rule 4):

1. Each host in lower case. The grammar takes either case, and the vmm
   compares two names byte for byte.
2. An explicit port. A bare host is port 443 (contract 01 §3.7 rule 7).
3. No entry twice. `GitHub.com` and `github.com:443` are one entry.
4. Sorted by host, then by port as a number.

The two plane endpoints never enter the file (decision CH6-2, contract 01
§3.7 rule 4). They are `EgressConfig`'s, and this module reads only the
family file. A family with no `egress` gets `{"hosts": []}` (decision CH69
rule 3).

`CONTRACT-QUESTION`: no row gives a rule for an `egress` list whose file is
over the 64 KiB cap of the vmm (decision CH70 rule 1). A family file of
64 KiB can hold such a list. This module writes the file all the same, and
the vmm allows nothing for it. No fault names it."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Final

from agent_family import FamilyFile
from creche_wire import EgressHost, FamilyEgress, HostName

from . import paths
from .atomic import atomic_write

#: The mode of a reach file (contract 01 §6.1.1 rule 1).
REACH_FILE_MODE: Final = 0o640

#: The port of a bare host (contract 01 §3.7 rule 7).
BARE_HOST_PORT: Final = 443


def write_egress_file(family: FamilyFile, state_root: Path) -> None:
    """Write `egress.json` of `family` when its bytes change.

    One `rename` lands the removals and the additions at once (invariant
    9). Bytes equal to the file on disk write nothing, so a pass that moves
    nothing touches no file."""
    path = paths.egress_path(state_root, family.name)
    body = _egress_text(family.egress)
    if _bytes_of(path) == body:
        return

    atomic_write(path, body, mode=REACH_FILE_MODE)


def _egress_text(entries: list[str]) -> bytes:
    """The canonical text of `FamilyEgress` for the `egress` entries of a
    valid family file. `egress_problem` of `family/grammar.py` passed each
    entry, so the port is ASCII digits from 1 to 65535."""
    unique = {_host_port(entry) for entry in entries}
    hosts = [EgressHost(host=HostName(host), port=port) for host, port in sorted(unique)]
    egress = FamilyEgress(hosts=hosts)
    return json.dumps(egress.model_dump(mode="json"), indent=2).encode("utf-8") + b"\n"


def _host_port(entry: str) -> tuple[str, int]:
    host, colon, port = entry.partition(":")
    return host.lower(), int(port) if colon else BARE_HOST_PORT


def _bytes_of(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None
