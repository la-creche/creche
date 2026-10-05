"""The registry of one test: family files from contract 01, in a git checkout.

    <root>/registry/                      one git repository, one commit
      families/<name>/family.yaml         contract 01 §2
      families/<name>/instructions.md     contract 01 §1

Each family file here follows the contract, never a module of a service, so
a service in another language reads the same bytes. On the host a person
writes these files, and three services read them: the trigger door (the
webhook names and `quiet`), the noticeboard (the edit form) and `caregiver`.

`git` runs with an environment that this module builds whole. No variable of
the shell that runs the suite reaches it, and no config file of a person:
`HOME` is the home of the root, and the global file is `/dev/null`. `git`
looks for a repository in the root and never above it, so a root inside
another checkout cannot send a command to that checkout.
"""

from __future__ import annotations

import os
import stat
import subprocess
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, cast

import yaml
from proc_tree import (
    ATTENDED,
    AUTONOMOUS,
    MODEL_ALIAS,
    THIN,
    Tree,
    write_family_prose,
    write_registry_file,
)

#: A comment in each family file. A save of the noticeboard must keep it
#: (`docs/rework/spec.md` §8.2 step 1).
FILE_COMMENT: Final = "# A fixture of the process suite. Every reach goes through the chaperone."

#: Contract 01 §3.13: the two trigger forms a scenario of this suite fires.
CRON_HOURLY: Final = 'cron: "@hourly"'

#: Who made the first commit of the checkout. Never a person.
_AUTHOR_NAME: Final = "process-suite"
_AUTHOR_EMAIL: Final = "process-suite@localhost"
_FIRST_SUBJECT: Final = "Add the families of the fixture"
_BRANCH: Final = "main"

_GIT: Final = "git"
_GIT_TIMEOUT_S: Final = 30.0
_NO_FILE: Final = os.devnull

#: The file that `git` makes while a command writes the index. A second
#: command that finds the file writes nothing and fails.
_INDEX_LOCK: Final = ".git/index.lock"

#: A file of the checkout that no commit holds, outside each family. A save
#: of the noticeboard must leave it as it is.
UNTRACKED_PATH: Final = "notes/draft.md"
_UNTRACKED_TEXT: Final = "A note of the fixture. No commit holds it.\n"

#: What a person types into the instructions of a family before a commit of
#: their own. A save of the noticeboard for another family must leave it.
_EDITED_PROSE: Final = "Be helpful, and answer in one sentence.\n"

#: The name of the file that a save of the noticeboard writes before the
#: rename: a dot, the name of the family file, 16 hex digits and this end.
_TEMP_DIGITS: Final = "0123456789abcdef"
_TEMP_END: Final = ".noticeboard-tmp"
_TEMP_TEXT: Final = "The text of a save that did not end.\n"


class RegistryError(Exception):
    """A `git` command of the harness failed. The message holds its output."""


@dataclass(frozen=True, slots=True)
class Commit:
    """The newest commit of the checkout, as `git` reports it."""

    sha: str
    author: str
    subject: str
    body: str
    paths: tuple[str, ...]


def webhook(name: str) -> str:
    """One webhook trigger (contract 01 §3.13), as an entry of `triggers`."""
    return f"webhook: {name}"


def family_text(
    name: str,
    kind: str,
    *,
    description: str,
    delegates: tuple[str, ...] = (),
    triggers: tuple[str, ...] = (),
    quiet: str | None = None,
) -> str:
    """One whole `family.yaml` (contract 01 §2).

    Every reach is empty: no mount, no MCP server, no egress. The one verb is
    `embed`, the verb each family of the old fixtures holds. A thin family
    gets the default job limit (§3.12). An autonomous family gets `triggers`
    and the default turn limit (§3.13, §3.14), and `quiet` when a scenario
    gives one (§3.15), as the text of a YAML mapping.
    """
    lines = [
        FILE_COMMENT,
        f"name: {name}",
        f"kind: {kind}",
        f"description: {description}",
        "",
        f"model: {{ router: {MODEL_ALIAS}, budget_usd_per_day: 15 }}",
        "",
        "verbs:",
        "  embed: {}",
        "",
        f"delegates: [{', '.join(delegates)}]",
        "",
        "egress: []",
        "",
        "shell: false",
        "sandbox_tools: [read, grep, find, ls]",
        "sandbox: { cpus: 2, memory: 4g }",
        "",
        "skills: []",
        "approval: []",
    ]

    if kind == THIN:
        lines += ["job: { timeout: 120s }"]

    if kind == AUTONOMOUS:
        lines += ["", "triggers:", *(f"  - {entry}" for entry in triggers), "max_running_turns: 1"]

    if quiet is not None:
        lines += ["", f"quiet: {quiet}"]

    return "\n".join(lines) + "\n"


def write_family(tree: Tree, name: str, text: str) -> None:
    """Put one family in the checkout: its instructions, then its file.

    `proc_tree.py` writes each file of the registry, by rename. This module
    gives the text of a family file. `family_body` of `proc_tree.py` gives a
    mapping, for a topology in which `caregiver` reads the file.
    """
    write_family_prose(tree, name)
    write_registry_file(tree, tree.family_file(name), text)


def write_attended(tree: Tree, name: str, delegates: tuple[str, ...] = ()) -> None:
    text = family_text(
        name, ATTENDED, description="General assistant of the fixture.", delegates=delegates
    )
    write_family(tree, name, text)


def write_thin(tree: Tree, name: str) -> None:
    write_family(tree, name, family_text(name, THIN, description="Retrieval of the fixture."))


def write_autonomous(
    tree: Tree, name: str, *, webhooks: tuple[str, ...] = (), quiet: str | None = None
) -> None:
    """An autonomous family with one cron trigger and one trigger per webhook."""
    text = family_text(
        name,
        AUTONOMOUS,
        description="Reviews the house on a timer or an alert.",
        triggers=(CRON_HOURLY, *(webhook(one) for one in webhooks)),
        quiet=quiet,
    )
    write_family(tree, name, text)


def family_path(tree: Tree, name: str) -> str:
    """The path of one family file, relative to the checkout, as `git` prints it."""
    return tree.family_file(name).relative_to(tree.registry_root).as_posix()


def read_family(tree: Tree, name: str) -> str:
    return tree.family_file(name).read_text(encoding="utf-8")


def family_bytes(tree: Tree, name: str) -> bytes:
    """One family file as it is on the disk.

    `read_family` gives each line end as LF. Only the bytes show a CR.
    """
    return tree.family_file(name).read_bytes()


def load_family(tree: Tree, name: str) -> dict[str, Any]:
    """One family file of the checkout, as the mapping of contract 01 §2.

    This is the one reader of a family file in the suite. A scenario asserts
    on a field of the mapping, and no test names the markup of the file.
    """
    loaded: object = yaml.safe_load(read_family(tree, name))

    if not isinstance(loaded, dict):
        raise RegistryError(f"{family_path(tree, name)} holds no mapping at the top level")

    return cast("dict[str, Any]", loaded)


def block_text(key: str, value: object) -> str:
    """One field as the edit form of the noticeboard takes it in a block.

    A block control holds the key line and the value, in the markup of the
    family file.
    """
    return yaml.safe_dump({key: value}, sort_keys=False)


def family_mode(tree: Tree, name: str) -> int:
    """The permission bits of one family file."""
    return stat.S_IMODE(tree.family_file(name).stat().st_mode)


def set_family_mode(tree: Tree, name: str, mode: int) -> None:
    tree.family_file(name).chmod(mode)


def leave_temp_file(tree: Tree, name: str) -> Path:
    """Put the temporary file of a save that did not end beside one family file.

    A save writes its text to a file in the directory of the family, then
    renames the file. A process that ends between the two steps leaves the
    file. No commit holds it. Returns its path.
    """
    target = tree.family_file(name)
    left = target.with_name(f".{target.name}.{_TEMP_DIGITS}{_TEMP_END}")
    left.write_text(_TEMP_TEXT, encoding="utf-8")

    return left


def write_untracked(tree: Tree) -> str:
    """Put one file in the checkout that no commit holds. Returns its path, as `git` prints it."""
    write_registry_file(tree, tree.registry_root / UNTRACKED_PATH, _UNTRACKED_TEXT)

    return UNTRACKED_PATH


def edit_family_prose(tree: Tree, name: str) -> str:
    """Change the instructions of one family in the checkout, with no commit.

    A person with an editor does this. A commit holds the file, so `git`
    lists it as changed. Returns its path, as `git` prints it.
    """
    write_family_prose(tree, name, _EDITED_PROSE)

    return tree.family_prose_file(name).relative_to(tree.registry_root).as_posix()


def stage_family_prose(tree: Tree, name: str) -> str:
    """Change the instructions of one family, then put the change in the index.

    A person does this with `git add`, before a commit of their own. Returns
    the path of the file, as `git` prints it.
    """
    path = edit_family_prose(tree, name)
    _git(tree, "add", "--", path)

    return path


@contextmanager
def index_locked(tree: Tree) -> Generator[None]:
    """Hold the lock of the index, as a `git` command of another program does.

    While the lock is there, no `git` command can write the index, so no
    commit is possible. The lock goes at the end of the block.
    """
    lock = tree.registry_root / _INDEX_LOCK
    lock.touch(exist_ok=False)

    try:
        yield
    finally:
        lock.unlink(missing_ok=True)


def commit_all(tree: Tree, subject: str = _FIRST_SUBJECT) -> None:
    """Make the checkout a git repository, and commit every file in it.

    The first call runs `git init`. Each call makes one commit.
    """
    if not (tree.registry_root / ".git").exists():
        tree.registry_root.mkdir(parents=True, exist_ok=True)
        _git(tree, "init", "--quiet", f"--initial-branch={_BRANCH}")

    _git(tree, "add", "--all")
    _git(
        tree,
        "-c",
        f"user.name={_AUTHOR_NAME}",
        "-c",
        f"user.email={_AUTHOR_EMAIL}",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "--quiet",
        "--message",
        subject,
    )


def commit_count(tree: Tree) -> int:
    return int(_git(tree, "rev-list", "--count", "HEAD"))


def head(tree: Tree) -> Commit:
    """The newest commit: who made it, what it says and which paths it changed."""
    paths = _git(tree, "show", "--name-only", "--format=", "HEAD").split("\n")

    return Commit(
        sha=_git(tree, "rev-parse", "HEAD"),
        author=_git(tree, "log", "-1", "--format=%an"),
        subject=_git(tree, "log", "-1", "--format=%s"),
        body=_git(tree, "log", "-1", "--format=%b"),
        paths=tuple(path for path in paths if path),
    )


def trailers(tree: Tree) -> tuple[str, ...]:
    """Each trailer line of the newest commit, as `git` reads the message."""
    return _lines(_git(tree, "log", "-1", "--format=%(trailers:only,unfold)"))


def uncommitted(tree: Tree) -> str:
    """What `git status` lists: a change that no commit holds. Empty when none."""
    return _git(tree, "status", "--porcelain")


def untracked(tree: Tree) -> tuple[str, ...]:
    """Each file of the checkout that no commit holds and that `git` does not ignore."""
    return _lines(_git(tree, "ls-files", "--others", "--exclude-standard"))


def changed(tree: Tree) -> tuple[str, ...]:
    """Each file of a commit whose text in the checkout is not its text in the index."""
    return _lines(_git(tree, "diff", "--name-only"))


def staged(tree: Tree) -> tuple[str, ...]:
    """Each file whose text in the index is not its text in the newest commit."""
    return _lines(_git(tree, "diff", "--cached", "--name-only"))


def _lines(output: str) -> tuple[str, ...]:
    """The lines of one `git` output that hold a text."""
    return tuple(line for line in output.split("\n") if line)


def _git(tree: Tree, *args: str) -> str:
    """One `git` command in the checkout. Returns its stdout, without the end."""
    tree.home.mkdir(parents=True, exist_ok=True)
    env = {
        "PATH": os.environ.get("PATH", os.defpath),
        "HOME": str(tree.home),
        "GIT_CONFIG_GLOBAL": _NO_FILE,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CEILING_DIRECTORIES": str(tree.root),
        "GIT_TERMINAL_PROMPT": "0",
    }

    try:
        done = subprocess.run(
            [_GIT, "-C", str(tree.registry_root), *args],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_S,
            check=False,
            env=env,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise RegistryError(f"cannot run git {' '.join(args)}: {error}") from error

    if done.returncode != 0:
        raise RegistryError(f"git {' '.join(args)} exited {done.returncode}\n{done.stderr}")

    return done.stdout.strip()
