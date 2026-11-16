"""The slash commands of a family: one command for each granted skill.

attendance answers `session/new` with no boot (decision CH21-7), so the
door sends the list of a new session itself, from the family config that
`caregiver` writes (contract 01 §6.1):

    config/skills/<dir>/SKILL.md ──► name, description ──► the command list

The facade sends the same list from the pulled skills on `session/load`,
`session/resume` and each prompt (`facade/src/commands.ts`). Each side
keeps a skill only when Flue's `defineSkill` takes it, so the two lists
are equal. The parse is the one of `agent/src/skills.ts`, and the checks
are those of `normalizeSkillDefinition` in Flue 2.3.0.

The files come from another process, so each one is a claim until it
passes the checks (invariants 12 and 14). A file that does not pass is
left out of the list.
"""

from __future__ import annotations

import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

SKILL_FILE: Final = "SKILL.md"

#: The largest `SKILL.md` that the door reads, in bytes: the cap of the
#: whole family state reply (`STATE_REPLY_CAP` of `agent/src/family-state.ts`).
MAX_SKILL_BYTES: Final = 262_144

#: Flue's limits of a skill (`normalizeSkillDefinition`, Flue 2.3.0).
MAX_NAME_CHARS: Final = 64
MAX_DESCRIPTION_CHARS: Final = 1024
_NAME_RE: Final = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")

#: `---` on its own line opens and closes the frontmatter
#: (`FRONTMATTER_RE` of `agent/src/skills.ts`).
_FRONTMATTER_RE: Final = re.compile(r"---\r?\n(.*?)\r?\n---\r?\n?(.*)", re.DOTALL)
#: A JavaScript `.` matches no line terminator (`FIELD_RE` of `agent/src/skills.ts`).
_FIELD_RE: Final = re.compile(r"([a-z][a-z-]*):[ \t]*([^\n\r\u2028\u2029]*)")
_LINE_SPLIT_RE: Final = re.compile(r"\r?\n")
_QUOTES: Final = ('"', "'")


@dataclass(frozen=True, slots=True)
class Command:
    """One ACP `AvailableCommand`."""

    name: str
    description: str

    def as_json(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description}


def _scalar(raw: str) -> str:
    """A one-line value, without the quotes of a YAML scalar."""
    text = raw.strip()

    if len(text) >= 2 and text[0] in _QUOTES and text[-1] == text[0]:
        return text[1:-1]

    return text


def command_of(directory: str, text: str) -> Command | None:
    """The command of one `SKILL.md` text. None: Flue would refuse the skill."""
    match = _FRONTMATTER_RE.fullmatch(text)

    if match is None:
        return None

    fields: dict[str, str] = {}

    for line in _LINE_SPLIT_RE.split(match.group(1)):
        field = _FIELD_RE.fullmatch(line)

        if field is not None:
            fields[field.group(1)] = _scalar(field.group(2))

    name = fields.get("name", directory).strip()
    description = fields.get("description", "").strip()
    body = match.group(2).strip()

    if len(name) > MAX_NAME_CHARS or _NAME_RE.fullmatch(name) is None:
        return None

    if not description or len(description) > MAX_DESCRIPTION_CHARS or not body:
        return None

    return Command(name, description)


def _read_skill(path: Path) -> str | None:
    """The text of one regular `SKILL.md` under the cap, or None."""
    try:
        status = path.lstat()

        if not stat.S_ISREG(status.st_mode) or status.st_size > MAX_SKILL_BYTES:
            return None

        return path.read_bytes().decode("utf-8")
    except OSError, UnicodeDecodeError:
        return None


def read_commands(skills_dir: Path) -> list[Command]:
    """The commands of the family's granted skills, by name. A family
    with no `skills/` directory has none. A directory that does not read
    gives none: the facade sends the list again at the first prompt."""
    try:
        directories = sorted(entry for entry in skills_dir.iterdir() if entry.is_dir())
    except OSError:
        return []

    commands: list[Command] = []

    for directory in directories:
        text = _read_skill(directory / SKILL_FILE)
        command = command_of(directory.name, text) if text is not None else None

        if command is not None:
            commands.append(command)

    return sorted(commands, key=lambda command: command.name)
