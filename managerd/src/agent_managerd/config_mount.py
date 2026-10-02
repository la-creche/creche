"""The family config mount (contract 01 section 6.1, contract 03 section
7.1): `instructions.md`, `skills/<name>/SKILL.md`, `runtime.json`.

`managerd` is its only writer. The directory is built fully in a staging
copy, then swapped in with one atomic operation, so a reader never sees
half a revision (contract 01 section 6.1 rule 1). One counter versions the
whole directory; this module writes the files and leaves stamping
`config_rev` into the status document to its caller, which is the one
place that knows the registry revision this write came from."""

from __future__ import annotations

import json
import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from .atomic import atomic_replace_dir

INSTRUCTIONS_FILE: Final = "instructions.md"
SKILLS_DIR: Final = "skills"
SKILL_FILE: Final = "SKILL.md"
RUNTIME_FILE: Final = "runtime.json"
#: Contract 01 §3.8: `instructions.md` is appended to pi's own prompt.
DEFAULT_SYSTEM_PROMPT: Final = "append"


@dataclass(frozen=True)
class RuntimeConfig:
    """`runtime.json` (contract 01 section 3.8 rule 4, section 6.1). Holds
    no credential and no grant -- the perimeter stays mounts, egress, the
    PEP and the model key (invariant 12)."""

    shell: bool
    sandbox_tools: tuple[str, ...]
    model_alias: str
    # `append` or `replace`: how `instructions.md` meets pi's own prompt.
    system_prompt: str = DEFAULT_SYSTEM_PROMPT

    def as_json(self) -> dict[str, Any]:
        """The file's content. `system_prompt` is written only when it is not
        the default: `config_mount_matches` compares bytes, so a key every
        family carried would rewrite every family's mount at the first pass
        after a release, and a swapped directory leaves a running sandbox's
        read-only mount dead until that sandbox restarts. The supervisor
        reads a missing key as the default."""
        body: dict[str, Any] = {
            "shell": self.shell,
            "sandbox_tools": list(self.sandbox_tools),
            "model_alias": self.model_alias,
        }
        if self.system_prompt != DEFAULT_SYSTEM_PROMPT:
            body["system_prompt"] = self.system_prompt

        return body


def write_config_mount(
    target: Path, *, instructions: str, skills: Mapping[str, str], runtime: RuntimeConfig
) -> None:
    """Build the whole directory in `<target>.tmp`, then swap it in.

    `instructions` is `families/<name>/instructions.md`'s text and `skills`
    maps a granted skill's name to its `SKILL.md` text -- both read from
    the registry by the caller, since this module has no registry
    knowledge of its own (it only writes files)."""
    staging = target.with_name(target.name + ".tmp")
    if staging.exists():
        shutil.rmtree(staging)

    staging.mkdir(parents=True)
    (staging / INSTRUCTIONS_FILE).write_text(instructions, encoding="utf-8")

    skills_dir = staging / SKILLS_DIR
    skills_dir.mkdir()
    for name, text in skills.items():
        one = skills_dir / name
        one.mkdir()
        (one / SKILL_FILE).write_text(text, encoding="utf-8")

    runtime_body = json.dumps(runtime.as_json(), indent=2) + "\n"
    (staging / RUNTIME_FILE).write_text(runtime_body, encoding="utf-8")

    atomic_replace_dir(staging, target)


def config_mount_matches(
    target: Path, *, instructions: str, skills: Mapping[str, str], runtime: RuntimeConfig
) -> bool:
    """Whether the directory already holds exactly this revision.

    The reconciler asks before it writes. `config_rev` equals
    `applied_rev` (contract 05 §9's example), so a rewrite that changes
    nothing would still swap the directory's inode under a reader for no
    reason."""
    if _read(target / INSTRUCTIONS_FILE) != instructions:
        return False

    if _read(target / RUNTIME_FILE) != json.dumps(runtime.as_json(), indent=2) + "\n":
        return False

    skills_dir = target / SKILLS_DIR
    if _skill_names(skills_dir) != set(skills):
        return False

    return all(_read(skills_dir / name / SKILL_FILE) == text for name, text in skills.items())


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def _skill_names(skills_dir: Path) -> set[str]:
    if not skills_dir.is_dir():
        return set()

    return {one.name for one in skills_dir.iterdir() if one.is_dir()}
