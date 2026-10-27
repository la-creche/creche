"""The caregiver writes a grant file of the cap and no larger file.

Contract 04 §1.2 gives the cap. The caregiver writes the largest file that
it can write, and refuses a file of one byte more.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from agent_family.grammar import TOOL_NAME
from caregiver.grants import (
    GRANT_FILE_MAX_BYTES,
    GrantFile,
    GrantFileTooLargeError,
    write_grant_file,
)

FAMILY = "chat"
SERVER = "kagi"

#: The count of characters of each tool name but the last one. One such
#: name is one line of 60 bytes in the file, so the last name has 60
#: characters at most. The tool name form takes 64 characters at most.
TOOL_CHARS = 50


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


def test_the_caregiver_writes_a_grant_file_of_the_cap(tmp_path: Path) -> None:
    """The caregiver writes a file of the cap. It writes no file of one byte
    more."""
    scratch = tmp_path / "scratch"
    path = tmp_path / "grants" / f"{FAMILY}.json"
    at_cap = tools_for(scratch, GRANT_FILE_MAX_BYTES)
    over_cap = [*at_cap[:-1], at_cap[-1] + "f"]
    assert all(TOOL_NAME.fullmatch(name) is not None for name in over_cap)

    write_grant_file(path, grant(at_cap))

    assert path.stat().st_size == GRANT_FILE_MAX_BYTES

    with pytest.raises(GrantFileTooLargeError):
        write_grant_file(path, grant(over_cap))

    assert path.stat().st_size == GRANT_FILE_MAX_BYTES
