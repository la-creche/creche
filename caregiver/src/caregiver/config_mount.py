"""The family config directory (contract 01 section 6.1):
`instructions.md`, `skills/<name>/SKILL.md`, `runtime.json`, `shape.json`.

`caregiver` is its only writer (decision CH5b). The directory is built fully
in a staging copy, then swapped in with one atomic operation, so a reader
never sees half a revision (contract 01 section 6.1 rule 1). The vmm reads
it as the operator through one directory handle and handles the rename
window (decision CH5b). One counter versions the
whole directory; this module writes the files and leaves stamping
`config_rev` into the status document to its caller, which is the one
place that knows the registry revision this write came from.

The vmm reads two parts that the guest does not: `model_live_at` in
`runtime.json` and the family VM shape `shape.json` (decisions CH23-1,
CH36 G6, CH43 rule 3). Both change in the same swap as the rest, so the
vmm never reads a new alias with an old live time (decision CH45 rule 2)."""

from __future__ import annotations

import json
import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from creche_wire import FamilyShape

from .atomic import atomic_replace_dir, read_json

INSTRUCTIONS_FILE: Final = "instructions.md"
SKILLS_DIR: Final = "skills"
SKILL_FILE: Final = "SKILL.md"
RUNTIME_FILE: Final = "runtime.json"
#: `FAMILY_SHAPE_FILE` of `creche-wire` in Rust spells the same name
#: (decision CH43 rule 3).
SHAPE_FILE: Final = "shape.json"
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
    #: When a `/key/update` of `model_alias` is live on every LiteLLM
    #: worker (contract 05 §7 rule 6, decision CH23-1). `None` until a
    #: model change sets it. Integer milliseconds since 1970-01-01T00:00:00Z,
    #: UTC (contract 01 §6.1 rule 5, decisions CH22-1, CH31), the form
    #: that `live_at` of `rust/crates/vmm-confine/src/family.rs` reads.
    model_live_at: int | None = None

    def as_json(self) -> dict[str, Any]:
        """The file's content. `system_prompt` is written only when it is not
        the default: `config_mount_matches` compares bytes, so a key every
        family carried would rewrite every family's directory at the first
        pass after a release. The playpen reads a missing key as the
        default. `model_live_at` is written only when it is set, for the
        same reason (decision CH45 rule 1)."""
        body: dict[str, Any] = {
            "shell": self.shell,
            "sandbox_tools": list(self.sandbox_tools),
            "model_alias": self.model_alias,
        }
        if self.system_prompt != DEFAULT_SYSTEM_PROMPT:
            body["system_prompt"] = self.system_prompt

        if self.model_live_at is not None:
            body["model_live_at"] = self.model_live_at

        return body


@dataclass(frozen=True)
class LiveModel:
    """The model of a `runtime.json` on disk: its alias and the live time
    of that alias (decision CH23-1)."""

    alias: str
    live_at: int | None


def read_model(target: Path) -> LiveModel | None:
    """The model of the `runtime.json` in `target`. `None` when the file is
    absent or does not read as a file that `write_config_mount` wrote."""
    body = read_json(target / RUNTIME_FILE)
    if body is None:
        return None

    alias = body.get("model_alias")
    live_at = body.get("model_live_at")
    # A bool is an int in Python, and no time is `true`.
    if not isinstance(alias, str) or isinstance(live_at, bool):
        return None

    if not isinstance(live_at, int | None):
        return None

    return LiveModel(alias, live_at)


def write_config_mount(
    target: Path,
    *,
    instructions: str,
    skills: Mapping[str, str],
    runtime: RuntimeConfig,
    shape: FamilyShape,
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
    (staging / SHAPE_FILE).write_text(_shape_text(shape), encoding="utf-8")

    atomic_replace_dir(staging, target)


def config_mount_matches(
    target: Path,
    *,
    instructions: str,
    skills: Mapping[str, str],
    runtime: RuntimeConfig,
    shape: FamilyShape,
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

    if _read(target / SHAPE_FILE) != _shape_text(shape):
        return False

    skills_dir = target / SKILLS_DIR
    if _skill_names(skills_dir) != set(skills):
        return False

    return all(_read(skills_dir / name / SKILL_FILE) == text for name, text in skills.items())


def _shape_text(shape: FamilyShape) -> str:
    """The text of `shape.json`: JSON with an indent of 2 and one end of
    line, as `runtime.json`."""
    return json.dumps(shape.model_dump(mode="json"), indent=2) + "\n"


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _skill_names(skills_dir: Path) -> set[str]:
    if not skills_dir.is_dir():
        return set()

    return {one.name for one in skills_dir.iterdir() if one.is_dir()}
