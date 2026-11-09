"""`egress.json` and `repos.json` beside the config directory (contract 01
§6.1.1, decisions CH69 rules 3 to 6, CH70 rule 4, CH71, CH73 rules 2 and
3)."""

from __future__ import annotations

import stat
from pathlib import Path
from typing import Final, Literal, cast

import pytest
from agent_family import FamilyFile
from agent_family.grammar import REPO_NAME_MAX, REPOS_MAX, is_repo_name
from caregiver.lan import Port, url
from caregiver.reach_files import (
    EGRESS_CAP,
    EgressTooLargeError,
    render_egress_file,
    write_egress_file,
    write_repos_file,
)
from caregiver_helpers import chat_family
from creche_wire import (
    EgressHost,
    FamilyEgress,
    FamilyRepos,
    HostName,
    RepoList,
    RepoName,
    read,
    strictjson,
)

from caregiver import paths

#: The caps that the vmm and caregiver share (decisions CH71, CH73 rule 2).
#: The vmm tests `the_egress_cap_matches_the_wire_vectors` and
#: `the_repos_cap_matches_the_wire_vectors` hold `EGRESS_CAP` and
#: `REPOS_CAP` of `rust/crates/vmm-confine/src/family.rs` equal to the same
#: file.
FAMILY_VECTORS: Final = (
    Path(__file__).resolve().parents[2] / "rust" / "crates" / "wire" / "vectors" / "family.json"
)


def vmm_cap(key: Literal["egress_cap", "repos_cap"]) -> int:
    """One cap of the wire vectors: `egress_cap`, the most bytes of
    `egress.json` that the vmm reads (decision CH70 rule 1), or
    `repos_cap`, the most bytes of `repos.json` (decision CH73 rule 2)."""
    vectors = strictjson.loads(FAMILY_VECTORS.read_bytes(), cap=4096)
    assert isinstance(vectors, dict)
    cap = cast(dict[str, object], vectors)[key]
    assert isinstance(cap, int)
    return cap


def land(family: FamilyFile, state_root: Path) -> None:
    write_egress_file(render_egress_file(family), state_root)


def hosts(count: int) -> list[str]:
    """`count` distinct entries, each a bare host."""
    return [f"h{index:05}.example.com" for index in range(count)]


def written(state_root: Path) -> FamilyEgress:
    """The file as the vmm reads it: strict JSON, then the wire type."""
    data = paths.egress_path(state_root, "chat").read_bytes()
    return read(FamilyEgress, data, cap=vmm_cap("egress_cap"))


def pairs(egress: FamilyEgress) -> list[tuple[str, int]]:
    return [(one.host.root, one.port) for one in egress.hosts]


def test_the_egress_file_reads_back_through_creche_wire(tmp_path: Path) -> None:
    land(chat_family(egress=["pypi.org:443"]), tmp_path)
    assert written(tmp_path) == FamilyEgress(
        hosts=[EgressHost(host=HostName("pypi.org"), port=443)]
    )


def test_the_file_has_mode_0640(tmp_path: Path) -> None:
    land(chat_family(egress=["pypi.org:443"]), tmp_path)
    mode = paths.egress_path(tmp_path, "chat").stat().st_mode
    assert stat.S_IMODE(mode) == 0o640


def test_a_bare_host_is_written_with_port_443(tmp_path: Path) -> None:
    land(chat_family(egress=["github.com", "ssh.github.com:0022"]), tmp_path)
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
    land(family, tmp_path)
    # Port 1000 sorts after 443 as a number. As text, "1000" sorts first.
    assert pairs(written(tmp_path)) == [
        ("github.com", 443),
        ("github.com", 1000),
        ("github.com", 8443),
        ("pypi.org", 443),
    ]


def test_a_family_with_no_egress_gets_an_empty_file(tmp_path: Path) -> None:
    land(chat_family(), tmp_path)
    assert written(tmp_path) == FamilyEgress(hosts=[])


def test_the_planes_never_enter_the_file(tmp_path: Path) -> None:
    """Decision CH6-2: the two planes have their own vsock ports, so the
    allowlist of the proxy holds the family list alone."""
    land(chat_family(egress=["github.com"]), tmp_path)
    text = paths.egress_path(tmp_path, "chat").read_text(encoding="utf-8")
    for plane in (Port.LITELLM, Port.PEP):
        assert url(plane).removeprefix("http://").partition(":")[0] not in text

    assert pairs(written(tmp_path)) == [("github.com", 443)]


def test_an_unchanged_list_writes_nothing(tmp_path: Path) -> None:
    """Another spelling of the same list gives the same bytes, so the file
    on disk stays the same file: no rename replaced it."""
    land(chat_family(egress=["github.com", "pypi.org"]), tmp_path)
    before = paths.egress_path(tmp_path, "chat").stat()
    land(chat_family(egress=["PyPI.org:443", "github.com"]), tmp_path)
    after = paths.egress_path(tmp_path, "chat").stat()
    assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns)


def test_an_edit_replaces_the_file_whole(tmp_path: Path) -> None:
    """Invariant 9: one rename lands the removal and the addition."""
    land(chat_family(egress=["github.com"]), tmp_path)
    land(chat_family(egress=["pypi.org"]), tmp_path)
    assert pairs(written(tmp_path)) == [("pypi.org", 443)]
    family_dir = paths.family_dir(tmp_path, "chat")
    assert sorted(one.name for one in family_dir.iterdir()) == ["egress.json"]


def test_the_egress_cap_matches_the_wire_vectors() -> None:
    assert vmm_cap("egress_cap") == EGRESS_CAP


def test_a_list_within_the_cap_renders() -> None:
    egress = render_egress_file(chat_family(egress=hosts(900)))
    assert len(egress.body) <= EGRESS_CAP


def test_a_list_over_the_cap_is_refused_with_the_cap_named() -> None:
    """Decision CH71: the vmm would read the file as `TooLarge` and allow
    nothing, so caregiver refuses the revision."""
    with pytest.raises(EgressTooLargeError, match=f"the cap is {EGRESS_CAP}$"):
        render_egress_file(chat_family(egress=hosts(1000)))


def repos_written(state_root: Path) -> list[str]:
    """`repos.json` as the vmm reads it: strict JSON, then the wire type."""
    data = paths.repos_path(state_root, "chat").read_bytes()
    return [one.root for one in read(FamilyRepos, data, cap=vmm_cap("repos_cap")).repos.root]


def test_the_repos_file_reads_back_through_creche_wire(tmp_path: Path) -> None:
    write_repos_file(chat_family(repos=["creche"]), tmp_path)
    data = paths.repos_path(tmp_path, "chat").read_bytes()
    assert read(FamilyRepos, data, cap=vmm_cap("repos_cap")) == FamilyRepos(
        repos=RepoList([RepoName("creche")])
    )
    mode = paths.repos_path(tmp_path, "chat").stat().st_mode
    assert stat.S_IMODE(mode) == 0o640


def test_the_repos_file_is_sorted(tmp_path: Path) -> None:
    """Decision CH69 rule 5: the canonical form. Upper case sorts first."""
    write_repos_file(chat_family(repos=["mcp", "creche", "Chaperone"]), tmp_path)
    assert repos_written(tmp_path) == ["Chaperone", "creche", "mcp"]


def test_a_family_with_no_repos_gets_an_empty_file(tmp_path: Path) -> None:
    write_repos_file(chat_family(), tmp_path)
    assert paths.repos_path(tmp_path, "chat").read_bytes() == b'{\n  "repos": []\n}\n'


def test_a_repos_edit_rewrites_the_file(tmp_path: Path) -> None:
    """Invariant 9: one rename lands the removal and the addition."""
    write_repos_file(chat_family(repos=["creche"]), tmp_path)
    write_repos_file(chat_family(repos=["mcp"]), tmp_path)
    assert repos_written(tmp_path) == ["mcp"]
    family_dir = paths.family_dir(tmp_path, "chat")
    assert sorted(one.name for one in family_dir.iterdir()) == ["repos.json"]


def test_an_unchanged_repos_list_writes_nothing(tmp_path: Path) -> None:
    """Another order of the same list gives the same bytes, so no rename
    replaced the file."""
    write_repos_file(chat_family(repos=["creche", "mcp"]), tmp_path)
    before = paths.repos_path(tmp_path, "chat").stat()
    write_repos_file(chat_family(repos=["mcp", "creche"]), tmp_path)
    after = paths.repos_path(tmp_path, "chat").stat()
    assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns)


def test_the_longest_repos_list_fits_the_vmm_cap(tmp_path: Path) -> None:
    """Decision CH73 rule 2: the grammar caps the list at `REPOS_MAX` names
    of `REPO_NAME_MAX` characters (decision CH69 rule 15), so the file of
    any valid family fits `repos_cap` and caregiver needs no refusal of its
    own. The module docstring cites the length."""
    names = [f"{index:0>{REPO_NAME_MAX}}" for index in range(REPOS_MAX)]
    assert all(is_repo_name(name) for name in names)
    write_repos_file(chat_family(repos=names), tmp_path)
    size = paths.repos_path(tmp_path, "chat").stat().st_size
    assert size == 3476
    assert size <= vmm_cap("repos_cap")
    assert repos_written(tmp_path) == names
