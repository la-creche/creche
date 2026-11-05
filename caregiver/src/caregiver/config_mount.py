"""The family config mount (contract 01 section 6.1, contract 03 section
7.1): `instructions.md`, `skills/<name>/` and its declared helper files,
`runtime.json`.

`caregiver` is its only writer. The directory is built fully in a staging
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
from pathlib import Path, PurePosixPath
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
        read-only mount dead until that sandbox restarts. The playpen
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
    target: Path,
    *,
    instructions: str,
    skills: Mapping[str, Mapping[str, str]],
    runtime: RuntimeConfig,
) -> None:
    """Build the whole directory in `<target>.tmp`, then swap it in.

    `instructions` is `families/<name>/instructions.md`'s text. `skills`
    maps a granted skill's name to its files: each key is a path relative
    to the skill's own directory, so `SKILL.md` and the helper files that
    skill's frontmatter declares. Both come from the registry, read by the
    caller, since this module has no registry knowledge of its own (it
    only writes files)."""
    staging = target.with_name(target.name + ".tmp")
    if staging.exists():
        shutil.rmtree(staging)

    staging.mkdir(parents=True)
    (staging / INSTRUCTIONS_FILE).write_text(instructions, encoding="utf-8")

    skills_dir = staging / SKILLS_DIR
    skills_dir.mkdir()
    for name, files in skills.items():
        one = skills_dir / name
        one.mkdir()
        for relpath, text in files.items():
            if not _is_plain_relative(relpath):
                continue

            path = one / relpath
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")

    runtime_body = json.dumps(runtime.as_json(), indent=2) + "\n"
    (staging / RUNTIME_FILE).write_text(runtime_body, encoding="utf-8")

    atomic_replace_dir(staging, target)


def config_mount_matches(
    target: Path,
    *,
    instructions: str,
    skills: Mapping[str, Mapping[str, str]],
    runtime: RuntimeConfig,
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

    return all(_skill_matches(skills_dir / name, files) for name, files in skills.items())


def _skill_matches(directory: Path, files: Mapping[str, str]) -> bool:
    """Whether one skill's directory holds exactly these files.

    The set matters as much as the text. A helper file the registry
    dropped since the last write leaves a stale copy in the mount, and a
    comparison of the declared files alone would never notice it."""
    staged = {relpath: text for relpath, text in files.items() if _is_plain_relative(relpath)}

    return _held_files(directory) == set(staged) and all(
        _read(directory / relpath) == text for relpath, text in staged.items()
    )


def _held_files(directory: Path) -> set[str]:
    """Every file under a skill's directory, as a relative path."""
    if not directory.is_dir():
        return set()

    return {str(one.relative_to(directory)) for one in directory.rglob("*") if one.is_file()}


def _is_plain_relative(relpath: str) -> bool:
    """Whether a declared path may be written under a skill's directory.

    The registry is a user-editable tree, so a path a SKILL.md declares is
    not trusted here. An absolute path, or one that climbs out of the
    skill's directory, would write somewhere else in the mount. The loader
    refuses such a path too, and faults it; this is the second check, in
    the one module that actually writes the file."""
    if not relpath or relpath != relpath.strip():
        return False

    if relpath.startswith("/") or "\\" in relpath:
        return False

    return ".." not in PurePosixPath(relpath).parts


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _skill_names(skills_dir: Path) -> set[str]:
    if not skills_dir.is_dir():
        return set()

    return {one.name for one in skills_dir.iterdir() if one.is_dir()}
