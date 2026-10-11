"""The cap of a grant file has one source.

The source is `GRANT_FILE_MAX_BYTES` of the Rust crate `creche-contracts`
(contract 04 §1.2). A Python package cannot read a Rust constant, so two
packages hold a copy: the caregiver writes the file, and the chaperone reads
it. Each test here fails when a copy differs from the source.

The test reads the Rust constant from its source line. It also fails when
that line has another form: change the pin together with the Rust file.

One more test holds the two packages together. The caregiver writes the
largest file that it can write, and the chaperone reads that file.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from agent_family.grammar import TOOL_NAME
from caregiver.grants import (
    GRANT_FILE_MAX_BYTES,
    GrantFile,
    GrantFileTooLargeError,
    write_grant_file,
)
from chaperone.family_grants import MAX_GRANT_FILE_BYTES, FamilyStore
from chaperone.faults import FaultWriter

REPO = Path(__file__).resolve().parents[2]

#: The home of the constant.
RUST_FILE = REPO / "rust" / "crates" / "creche-contracts" / "src" / "grants" / "file.rs"

#: The line of the constant: the product of two counts.
RUST_CAP = re.compile(r"pub const GRANT_FILE_MAX_BYTES: usize = (\d+) \* (\d+);")

FAMILY = "chat"
SERVER = "kagi"

#: The count of characters of each tool name but the last one. One such
#: name is one line of 60 bytes in the file, so the last name has 60
#: characters at most. The tool name form takes 64 characters at most.
TOOL_CHARS = 50


def rust_cap() -> int:
    """The value of the Rust constant, from the one line that defines it."""
    lines = RUST_FILE.read_text(encoding="utf-8").splitlines()
    found = [match for match in map(RUST_CAP.fullmatch, lines) if match is not None]
    assert len(found) == 1, f"{RUST_FILE.name} holds no `GRANT_FILE_MAX_BYTES` in the pinned form"
    return int(found[0].group(1)) * int(found[0].group(2))


def grant(tools: list[str]) -> GrantFile:
    return GrantFile(
        family=FAMILY,
        rev="reg-abc",
        token_sha256=("0" * 64,),
        model_alias="agent-router",
        tools={SERVER: tools},
        verbs={},
        delegates=(),
        max_inflight_delegations=2,
        approval=(),
    )


def tool(index: int) -> str:
    return f"t{index:0{TOOL_CHARS - 1}d}"


def size_of(scratch: Path, tools: list[str]) -> int:
    """The count of bytes of the file that the caregiver writes for `tools`."""
    path = scratch / "scratch.json"
    write_grant_file(path, grant(tools))
    return path.stat().st_size


def tools_for(scratch: Path, size: int) -> list[str]:
    """The tools of a grant file of `size` bytes. Each tool is one line of
    the file. The last name fills the file: one character is one byte."""
    smallest = size_of(scratch, [tool(0), "f"])
    line = size_of(scratch, [tool(0), tool(1), "f"]) - smallest
    lines, rest = divmod(size - smallest, line)

    return [*(tool(index) for index in range(lines + 1)), "f" * (rest + 1)]


def test_the_caregiver_holds_the_cap_of_the_rust_code() -> None:
    assert rust_cap() == GRANT_FILE_MAX_BYTES


def test_the_chaperone_holds_the_cap_of_the_rust_code() -> None:
    assert rust_cap() == MAX_GRANT_FILE_BYTES


def test_the_chaperone_reads_the_largest_grant_file_of_the_caregiver(tmp_path: Path) -> None:
    """The two packages compare a size with the cap in the same direction.
    The caregiver writes a file of the cap, and the chaperone reads it. The
    caregiver writes no file of one byte more."""
    scratch = tmp_path / "scratch"
    grants_dir = tmp_path / "grants"
    path = grants_dir / f"{FAMILY}.json"
    at_cap = tools_for(scratch, GRANT_FILE_MAX_BYTES)
    over_cap = [*at_cap[:-1], at_cap[-1] + "f"]
    assert all(TOOL_NAME.fullmatch(name) is not None for name in over_cap)

    write_grant_file(path, grant(at_cap))

    assert path.stat().st_size == GRANT_FILE_MAX_BYTES
    store = FamilyStore(grants_dir, FaultWriter(tmp_path / "faults"))
    served = store.current(FAMILY)
    assert served is not None
    assert served.tools == {SERVER: tuple(at_cap)}

    with pytest.raises(GrantFileTooLargeError):
        write_grant_file(path, grant(over_cap))

    assert path.stat().st_size == GRANT_FILE_MAX_BYTES
