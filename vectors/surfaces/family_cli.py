"""`agent-family validate` (contract 01 §7): a command line to its output.

The entry point is `agent_family.cli.main`. The generator copies a registry
to a directory named `registry` in a temporary directory, makes that
temporary directory the working directory and calls the entry point. A
command line names the registry as `registry`, so no output holds a path of
the machine.
"""

from __future__ import annotations

import contextlib
import io
import os
import tempfile
from collections.abc import Generator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from agent_family.cli import main

from vectors.core import Json, Raised, Surface, Vector, accepted, attempt, raised, refused
from vectors.surfaces.family_file import (
    FAMILY_REGISTRY,
    FIXTURE_REGISTRIES,
    REPO_ROOT,
    copy_registry,
)

CONTRACT: Final = "contract 01 §7"
ENTRY: Final = "agent_family.cli.main"

#: The name of the copy of the registry, in the working directory.
COPY: Final = "registry"

#: The start of the text that argparse writes for a command line that it
#: refuses. Its text differs between two Python versions.
ARGPARSE_USAGE: Final = "usage:"

NOTES: Final = (
    "The input holds one argument: argv, the arguments after the program name.",
    "params.registry is a registry in this repository, as a path from the repository root. "
    "family_file.registries.json holds each file of each such registry. params.files holds "
    "every other file a vector adds, by path from the registry root.",
    "To replay a vector: copy the registry to a directory named registry in an empty "
    "directory, write params.files, make the empty directory the working directory and "
    "start the program with argv.",
    "result is accepted for the exit status 0. value and refusal hold exit, the exit status, "
    "and stdout, the exact text of the standard output.",
    "stderr is the exact text of the standard error. It is absent when argparse writes the "
    "text: that text differs between two Python versions. stdout is absent for the same "
    "reason when the program prints a help text.",
)

#: A family file that breaks rules, with a character outside ASCII and a
#: control character in a message.
BROKEN_FAMILY: Final = """\
name: broken-family
kind: thin
description: One written input.
model: { router: "caf\\u00e9 \\"x\\"\\t\\\\", budget_usd_per_day: 1000 }
delegates: [no-such-family]
"""

#: A server file that breaks rules.
BROKEN_SERVER: Final = """\
name: another-name
identity: One written server.
install: { source: docker }
run: { entrypoint: example-mcp }
"""

BROKEN_FILES: Final = {
    "families/broken-family/family.yaml": BROKEN_FAMILY,
    "mcp/broken-server/server.yaml": BROKEN_SERVER,
    "families/no-file/notes.txt": "A directory with no family file.\n",
}


#: Files with each line end that is not one line feed. The revision of a
#: registry reads each such line end as one line feed.
LINE_END_FILES: Final = {
    "families/line-ends/family.yaml": (
        "name: line-ends\r\nkind: thin\r\ndescription: One written input.\r\n"
        "model: { router: fast, budget_usd_per_day: 1 }\r\n"
    ),
    "skills/line-ends/SKILL.md": "one\rtwo\r\nthree\n",
}

#: A server file in a directory with the name of a family.
SAME_NAME_FILES: Final = {
    "mcp/chat/server.yaml": (
        "name: chat\nidentity: One written server.\n"
        "install: { source: docker }\nrun: { entrypoint: example-mcp }\n"
    ),
}

#: A skill directory with no skill file, and a family that names the skill.
NO_SKILL_FILES: Final = {
    "skills/no-file-skill/notes.txt": "A skill directory with no skill file.\n",
    "families/names-skill/family.yaml": (
        "name: names-skill\nkind: thin\ndescription: One written input.\n"
        "model: { router: fast, budget_usd_per_day: 1 }\nskills: [no-file-skill]\n"
    ),
}


@dataclass(frozen=True)
class Case:
    """One command line, and the registry it runs against."""

    id: str
    argv: tuple[str, ...]
    registry: str = FAMILY_REGISTRY
    files: dict[str, str] = field(default_factory=dict[str, str])


def _fixture_cases() -> tuple[Case, ...]:
    """The whole report of each registry of this repository, in both forms."""
    cases: list[Case] = []
    for registry in FIXTURE_REGISTRIES:
        label = Path(registry).name.removesuffix("-registry")
        cases.append(Case(f"fixture-{label}-text", ("validate", COPY), registry))
        cases.append(Case(f"fixture-{label}-json", ("validate", COPY, "--json"), registry))

    return tuple(cases)


CASES: Final[tuple[Case, ...]] = (
    *_fixture_cases(),
    # --- one report ---
    Case("family-text", ("validate", COPY, "--family", "chat")),
    Case("family-json", ("validate", COPY, "--family", "chat", "--json")),
    Case("server-text", ("validate", COPY, "--family", "kagi")),
    Case("server-json", ("validate", COPY, "--json", "--family", "kagi")),
    Case("family-unknown", ("validate", COPY, "--family", "no-such-name")),
    Case("family-empty-name", ("validate", COPY, "--family", "")),
    # --- how argparse reads a command line ---
    Case("option-first", ("validate", "--json", "--family", "chat", COPY)),
    Case("option-equals", ("validate", "--family=chat", COPY)),
    Case("option-short-form", ("validate", COPY, "--fam", "chat", "--j")),
    Case("option-twice", ("validate", COPY, "--family", "chat", "--family", "code")),
    Case("option-end-mark", ("validate", "--json", "--", COPY)),
    Case("path-not-plain", ("validate", f"./{COPY}//", "--family", "chat", "--json")),
    # --- a path that is no directory ---
    Case("path-missing", ("validate", "no-such-directory")),
    Case("path-missing-json", ("validate", "./no-such//directory/", "--json")),
    Case("path-is-a-file", ("validate", f"{COPY}/families/chat/family.yaml")),
    # --- a registry with errors ---
    Case("broken-text", ("validate", COPY), files=BROKEN_FILES),
    Case("broken-json", ("validate", COPY, "--json"), files=BROKEN_FILES),
    Case("broken-one-family", ("validate", COPY, "--family", "broken-family"), files=BROKEN_FILES),
    Case("broken-one-server", ("validate", COPY, "--family", "broken-server"), files=BROKEN_FILES),
    Case("broken-clean-family", ("validate", COPY, "--family", "chat"), files=BROKEN_FILES),
    Case("broken-no-file", ("validate", COPY, "--family", "no-file", "--json"), files=BROKEN_FILES),
    # --- what the loader reads of a registry ---
    Case("line-ends-json", ("validate", COPY, "--json"), files=LINE_END_FILES),
    Case("same-name-text", ("validate", COPY), files=SAME_NAME_FILES),
    Case("same-name-one", ("validate", COPY, "--family", "chat", "--json"), files=SAME_NAME_FILES),
    Case("skill-no-file", ("validate", COPY, "--family", "names-skill"), files=NO_SKILL_FILES),
    # --- a command line that argparse refuses, and the help ---
    Case("usage-no-argument", ()),
    Case("usage-no-registry", ("validate",)),
    Case("usage-command", ("check", COPY)),
    Case("usage-option", ("validate", COPY, "--bogus")),
    Case("usage-option-before", ("--bogus", "validate", COPY)),
    Case("usage-second-path", ("validate", COPY, "other")),
    Case("usage-family-no-value", ("validate", COPY, "--family")),
    Case("usage-family-then-option", ("validate", COPY, "--family", "--json")),
    Case("usage-json-value", ("validate", COPY, "--json=1")),
    Case("help", ("-h",)),
    Case("help-validate", ("validate", "--help")),
)


@contextlib.contextmanager
def _working_directory(path: Path) -> Generator[None]:
    """`path` as the working directory, and the old one put back."""
    previous = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


@dataclass(frozen=True)
class Ran:
    """What one call of the entry point wrote and how it ended."""

    exit: int
    stdout: str
    stderr: str


def _run(argv: tuple[str, ...]) -> Ran:
    """The entry point with its two streams kept. argparse ends with `SystemExit`."""
    out = io.StringIO()
    err = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            status = main(argv)
        except SystemExit as exc:
            status = exc.code if isinstance(exc.code, int) else 1

    return Ran(status, out.getvalue(), err.getvalue())


def _outcome(ran: Ran) -> dict[str, Json]:
    """What a vector keeps of one run: no text that argparse writes."""
    outcome: dict[str, Json] = {"exit": ran.exit}
    by_argparse = ran.stderr.startswith(ARGPARSE_USAGE) or ran.stdout.startswith(ARGPARSE_USAGE)
    if not by_argparse:
        outcome["stdout"] = ran.stdout
        outcome["stderr"] = ran.stderr

    return outcome


def _vector(case: Case, scratch: Path) -> Vector:
    root = scratch / case.id
    copy_registry(REPO_ROOT / case.registry, root / COPY)
    for path, text in case.files.items():
        target = root / COPY / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(text.encode("utf-8"))

    given: dict[str, Json] = {"args": {"argv": list(case.argv)}}
    params: dict[str, Json] = {"registry": case.registry}
    if case.files:
        params["files"] = dict(case.files)

    with _working_directory(root):
        ran = attempt(lambda: _run(case.argv))

    if isinstance(ran, Raised):
        return raised(case.id, given, ran.exc, params=params)

    if ran.exit == 0:
        return accepted(case.id, given, _outcome(ran), params=params)

    return refused(case.id, given, _outcome(ran), params=params)


def surfaces() -> tuple[Surface, ...]:
    with tempfile.TemporaryDirectory(prefix="vectors-cli-") as scratch:
        vectors = tuple(_vector(case, Path(scratch)) for case in CASES)

    return (
        Surface(
            name="family_file.cli",
            path="family_file.cli.json",
            entry=ENTRY,
            contract=CONTRACT,
            notes=NOTES,
            vectors=vectors,
        ),
    )
