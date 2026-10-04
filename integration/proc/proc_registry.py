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
`HOME` is the home of the root, and the global file is `/dev/null`.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from typing import Final

from proc_tree import ATTENDED, AUTONOMOUS, INSTRUCTIONS, MODEL_ALIAS, THIN, Tree

FAMILY_FILE: Final = "family.yaml"
INSTRUCTIONS_FILE: Final = "instructions.md"
FAMILIES_DIR: Final = "families"

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
    """Put one family in the checkout: its file and its instructions."""
    directory = tree.registry_root / FAMILIES_DIR / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / FAMILY_FILE).write_text(text, encoding="utf-8")
    (directory / INSTRUCTIONS_FILE).write_text(INSTRUCTIONS, encoding="utf-8")


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
    """The path of one family file, relative to the checkout."""
    return f"{FAMILIES_DIR}/{name}/{FAMILY_FILE}"


def read_family(tree: Tree, name: str) -> str:
    return (tree.registry_root / family_path(tree, name)).read_text(encoding="utf-8")


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


def uncommitted(tree: Tree) -> str:
    """What `git status` lists: a change that no commit holds. Empty when none."""
    return _git(tree, "status", "--porcelain")


def _git(tree: Tree, *args: str) -> str:
    """One `git` command in the checkout. Returns its stdout, without the end."""
    tree.home.mkdir(parents=True, exist_ok=True)
    env = {
        "PATH": os.environ.get("PATH", os.defpath),
        "HOME": str(tree.home),
        "GIT_CONFIG_GLOBAL": _NO_FILE,
        "GIT_CONFIG_NOSYSTEM": "1",
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
