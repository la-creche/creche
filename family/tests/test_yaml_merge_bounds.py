"""The YAML reader bounds merge keys in time and memory.

The constructor of PyYAML copies the entries of each merged mapping into the
mapping that holds the `<<` key. A chain of merged aliases then doubles the
work at each level, so one small file can make the reader use time and memory
with no bound. `parse_family` composes the node graph first and refuses such a
file before it builds a value.

The dangerous input runs in a child process with a memory limit and a time
limit, so this test stays bounded (the root `AGENTS.md`: validate shape and
size before use)."""

from __future__ import annotations

import subprocess
import sys
import textwrap
from typing import Final

from agent_family.parse import parse_family

#: The address space the child may use. `setrlimit` holds on Linux. macOS
#: does not always honor it, so the time limit is the guard there.
_MEMORY_CAP_BYTES: Final = 3 * (1 << 30)

#: The wall-clock the child may use.
_TIME_CAP_SECONDS: Final = 20

#: The refusals the reader gives for merge keys past a bound.
_TOO_MANY: Final = "YAML will not parse: the merge keys make too many entries"
_TOO_DEEP: Final = "YAML will not parse: the merge keys nest too deep"

_HEAD: Final = (
    "name: probe\n"
    "kind: thin\n"
    "description: One written input.\n"
    "model: { router: fast, budget_usd_per_day: 1 }\n"
)


def _amplifying_merges(levels: int) -> str:
    """A family file whose merge keys double the entry count at each level."""
    lines = [_HEAD, "a0: &a0 { x: 1 }"]
    for level in range(1, levels + 1):
        lines.append(f"a{level}: &a{level} {{ <<: [*a{level - 1}, *a{level - 1}] }}")

    return "\n".join(lines) + "\n"


def _deep_merges(levels: int) -> str:
    """A family file whose merge keys nest to a depth."""
    lines = [_HEAD, "c0: &c0 { x: 1 }"]
    for level in range(1, levels + 1):
        lines.append(f"c{level}: &c{level} {{ <<: *c{level - 1} }}")

    return "\n".join(lines) + "\n"


_CHILD: Final = textwrap.dedent(
    """
    import resource
    import sys

    cap = {cap}
    try:
        resource.setrlimit(resource.RLIMIT_AS, (cap, cap))
    except (ValueError, OSError):
        pass

    from agent_family.parse import parse_family

    family, issues = parse_family(sys.stdin.read())
    if family is not None:
        print("PARSED")
    else:
        print(issues[0].msg if issues else "NO-ISSUE")
    """
)


def _probe(text: str) -> str:
    """The first issue message of `parse_family(text)`, from a child process
    with a memory cap and a time cap.

    The call raises when the child is killed or does not end in time, which
    is what a reader with no bound does."""
    done = subprocess.run(
        [sys.executable, "-c", _CHILD.format(cap=_MEMORY_CAP_BYTES)],
        input=text,
        capture_output=True,
        text=True,
        timeout=_TIME_CAP_SECONDS,
        check=True,
    )

    return done.stdout.strip()


def test_amplifying_merge_keys_are_refused_in_bounds() -> None:
    assert _probe(_amplifying_merges(26)) == _TOO_MANY


def test_deep_merge_keys_are_refused() -> None:
    family, issues = parse_family(_deep_merges(402))
    assert family is None
    assert [issue.msg for issue in issues] == [_TOO_DEEP]


def test_a_merge_key_still_parses() -> None:
    text = (
        _HEAD + "files:\n"
        "  - &vault { path: /srv/agents/vault, mode: ro }\n"
        "  - { <<: *vault, path: /srv/agents/code }\n"
    )
    family, issues = parse_family(text)
    assert family is not None, [issue.msg for issue in issues]
    assert len(family.files) == 2
