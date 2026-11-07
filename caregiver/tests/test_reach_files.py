"""`egress.json` beside the config mount (contract 01 §6.1.1, decisions
CH69 rules 3, 5 and 6, CH70 rule 4)."""

from __future__ import annotations

import stat
from pathlib import Path
from typing import Final

from caregiver.egress import EgressConfig
from caregiver.reach_files import write_egress_file
from caregiver_helpers import chat_family
from creche_wire import EgressHost, FamilyEgress, HostName, read

from caregiver import paths

#: `EGRESS_CAP` of `rust/crates/vmm-confine/src/family.rs`: the most bytes
#: of `egress.json` that the vmm reads (decision CH70 rule 1).
VMM_EGRESS_CAP: Final = 64 * 1024


def written(state_root: Path) -> FamilyEgress:
    """The file as the vmm reads it: strict JSON, then the wire type."""
    data = paths.egress_path(state_root, "chat").read_bytes()
    return read(FamilyEgress, data, cap=VMM_EGRESS_CAP)


def pairs(egress: FamilyEgress) -> list[tuple[str, int]]:
    return [(one.host.root, one.port) for one in egress.hosts]


def test_the_egress_file_reads_back_through_creche_wire(tmp_path: Path) -> None:
    write_egress_file(chat_family(egress=["pypi.org:443"]), tmp_path)
    assert written(tmp_path) == FamilyEgress(
        hosts=[EgressHost(host=HostName("pypi.org"), port=443)]
    )


def test_the_file_has_mode_0640(tmp_path: Path) -> None:
    write_egress_file(chat_family(egress=["pypi.org:443"]), tmp_path)
    mode = paths.egress_path(tmp_path, "chat").stat().st_mode
    assert stat.S_IMODE(mode) == 0o640


def test_a_bare_host_is_written_with_port_443(tmp_path: Path) -> None:
    write_egress_file(chat_family(egress=["github.com", "ssh.github.com:0022"]), tmp_path)
    assert pairs(written(tmp_path)) == [("github.com", 443), ("ssh.github.com", 22)]


def test_case_and_duplicates_are_normalized(tmp_path: Path) -> None:
    family = chat_family(
        egress=[
            "PyPI.org",
            "github.com:8443",
            "github.com:1000",
            "GitHub.com:443",
            "github.com",
            "pypi.org:443",
        ]
    )
    write_egress_file(family, tmp_path)
    # Port 1000 sorts after 443 as a number. As text, "1000" sorts first.
    assert pairs(written(tmp_path)) == [
        ("github.com", 443),
        ("github.com", 1000),
        ("github.com", 8443),
        ("pypi.org", 443),
    ]


def test_a_family_with_no_egress_gets_an_empty_file(tmp_path: Path) -> None:
    write_egress_file(chat_family(), tmp_path)
    assert written(tmp_path) == FamilyEgress(hosts=[])


def test_the_planes_never_enter_the_file(tmp_path: Path) -> None:
    """Decision CH6-2: the two planes have their own vsock ports, so the
    allowlist of the proxy holds the family list alone."""
    write_egress_file(chat_family(egress=["github.com"]), tmp_path)
    text = paths.egress_path(tmp_path, "chat").read_text(encoding="utf-8")
    planes = EgressConfig()
    for endpoint in (planes.litellm, planes.chaperone):
        assert endpoint.partition(":")[0] not in text

    assert pairs(written(tmp_path)) == [("github.com", 443)]


def test_an_unchanged_list_writes_nothing(tmp_path: Path) -> None:
    """Another spelling of the same list gives the same bytes, so the file
    on disk stays the same file: no rename replaced it."""
    write_egress_file(chat_family(egress=["github.com", "pypi.org"]), tmp_path)
    before = paths.egress_path(tmp_path, "chat").stat()
    write_egress_file(chat_family(egress=["PyPI.org:443", "github.com"]), tmp_path)
    after = paths.egress_path(tmp_path, "chat").stat()
    assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns)


def test_an_edit_replaces_the_file_whole(tmp_path: Path) -> None:
    """Invariant 9: one rename lands the removal and the addition."""
    write_egress_file(chat_family(egress=["github.com"]), tmp_path)
    write_egress_file(chat_family(egress=["pypi.org"]), tmp_path)
    assert pairs(written(tmp_path)) == [("pypi.org", 443)]
    family_dir = paths.family_dir(tmp_path, "chat")
    assert sorted(one.name for one in family_dir.iterdir()) == ["egress.json"]
