"""The `files:` declaration in a SKILL.md frontmatter.

A skill that carries helper files declares them in its own frontmatter, so
the declaration travels with the skill and no family file names a path
(contract 01 §1 rule 2: a family file never widens reach). The parser reads
only the `files:` key: every other frontmatter key belongs to the skill's
readers, and this package neither knows nor validates them.

The parser is deliberately small and strict. YAML in full is not the goal:
upstream skills may carry any frontmatter at all, and the reader must never
raise on one (contract 01 §7 rule 5). Whatever it cannot read, it reports,
and the validator puts the report on every family that grants the skill.
"""

from __future__ import annotations

from typing import Final

#: The frontmatter fence, as SKILL.md writes it.
FENCE: Final = "---"
#: The key this package reads. Every other key passes through unseen.
DECLARATION_KEY: Final = "files"
#: The one file that is never a helper: the skill itself.
SKILL_FILE: Final = "SKILL.md"


def parse_declared_files(text: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """The helper paths a SKILL.md declares, and what stopped a clean read.

    Answers `(declared, faults)`. `declared` holds only the paths that
    passed every rule; a path with a fault is dropped and named in
    `faults`. The validator reports each fault on every family that grants
    the skill, so a typo in a frontmatter list is a report, never a silent
    loss of a file.
    """
    lines = text.split("\n")
    faults: list[str] = []

    if not lines or lines[0].strip() != FENCE:
        return (), ()

    end = None
    for position in range(1, len(lines)):
        if lines[position].strip() == FENCE:
            end = position
            break
    if end is None:
        faults.append("has a frontmatter that never closes")
        end = len(lines)

    declared, key_faults = _read_key(lines[1:end])
    faults.extend(key_faults)
    kept: list[str] = []
    for one in declared:
        fault = _path_fault(one)
        if fault is None:
            kept.append(one)
        else:
            faults.append(f"declares '{one}', which is not a helper path ({fault})")
    return tuple(kept), tuple(faults)


def _read_key(frontmatter: list[str]) -> tuple[list[str], list[str]]:
    """The paths under a top-level `files:` key, and what stopped a clean read."""
    faults: list[str] = []
    for position, line in enumerate(frontmatter):
        if not line.startswith(DECLARATION_KEY) or line[len(DECLARATION_KEY) :][:1] != ":":
            continue

        inline = line[len(DECLARATION_KEY) + 1 :].strip()
        if inline:
            return _read_flow(inline, faults), faults

        items: list[str] = []
        for item in frontmatter[position + 1 :]:
            if not item.strip():
                continue
            if not item[0].isspace():
                break  # a new top-level key ends the block
            if not item.lstrip().startswith("- "):
                faults.append(
                    f"has a '{DECLARATION_KEY}' list with a line that is no item: {item.strip()!r}"
                )
                break
            one = item.lstrip()[2:].strip().strip("'\"")
            if one in items:
                faults.append(f"declares '{one}' twice")
            else:
                items.append(one)
        return items, faults

    return [], faults


def _read_flow(inline: str, faults: list[str]) -> list[str]:
    """An inline list: `files: ["a.md", "b.md"]` or `files: []`."""
    body = inline.strip()
    if not (body.startswith("[") and body.endswith("]")):
        faults.append(f"has a '{DECLARATION_KEY}' value that is neither a list nor an inline list")
        return []

    inner = body[1:-1].strip()
    if not inner:
        return []

    kept: list[str] = []
    for one in inner.split(","):
        item = one.strip().strip("'\"")
        if item:
            kept.append(item)
    return kept


def _path_fault(path: str) -> str | None:
    """Why a declared path cannot be a helper file, or `None` when it can."""
    if not path or path != path.strip():
        return "is empty or carries padding"
    if path.startswith("/"):
        return "is absolute"
    if "\\" in path:
        return "holds a backslash"
    parts = path.split("/")
    if any(part in ("", ".", "..") for part in parts):
        return "is not a plain relative path"
    if path == SKILL_FILE:
        return "is the SKILL.md itself"
    return None
