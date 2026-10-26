"""`server.toml` (contract 01b): the bytes of one file to its validation
report.

The entry point is `agent_family.load_registry`, as for `family_file`. It
calls `parse_server` and `check_server` for each directory under `mcp/`. The
generator copies a registry to a temporary directory, writes the input there
and reads the report of that one server back.

One surface, `server_file.toml`. Each input is a TOML text in a copy of
`TOML_REGISTRY`. The surface `server_file` held the YAML texts. Its file is
frozen, because the YAML reader left this repository.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import Final

from agent_family import Registry, load_registry
from agent_family.registry import SERVER_FILE as TOML_FILE

from vectors.core import (
    Json,
    Raised,
    Surface,
    Vector,
    accepted,
    attempt,
    bytes_input,
    raised,
    refused,
)
from vectors.surfaces.family_file import (
    REPO_ROOT,
    TOML_REGISTRIES,
    TOML_REGISTRY,
    copy_registry,
)
from vectors.surfaces.family_toml_cases import PLACEHOLDER, Case
from vectors.surfaces.server_toml_cases import CASES as TOML_CASES

MCP_DIR: Final = "mcp"

CONTRACT: Final = "contract 01b"
ENTRY: Final = "agent_family.load_registry"


def _notes(file: str) -> tuple[str, ...]:
    """The notes of a surface whose input is written as `file`."""
    return (
        f"The input is the bytes of one {file}.",
        "params.registry is a registry in this repository, as a path from the repository root. "
        "params.directory is the directory of the input under mcp/ in that registry.",
        "family_file.registries.json holds each file of each such registry.",
        f"To replay a vector: copy the registry, write the input to mcp/<directory>/{file}, "
        "load the registry and read the server report of <directory>.",
        "result is accepted when the report holds no issue of severity error. issues holds every "
        "issue in order. value is the parsed server file, with every default filled in.",
    )


#: The note of a surface whose vectors can add files to the registry.
FILES_NOTE: Final = (
    "params.files holds every other file a vector adds to the registry, by path from the "
    "registry root. Write each one before the input."
)


def _params(registry: str, case: Case) -> dict[str, Json]:
    params: dict[str, Json] = {"registry": registry, "directory": case.directory_name()}
    if case.files:
        files: dict[str, Json] = {
            path: text.replace(PLACEHOLDER, case.directory_name())
            for path, text in case.files.items()
        }
        params["files"] = files

    return params


def _outcome(case: Case, loaded: Registry, registry: str) -> Vector:
    """The report of one server, out of one registry that is already loaded."""
    given = bytes_input(case.text_bytes())
    params = _params(registry, case)
    report = loaded.server_reports[case.directory_name()]
    issues = [issue.as_json() for issue in report.issues]
    if not report.ok:
        return refused(case.id, given, params=params, status=report.state, issues=issues)

    return accepted(
        case.id,
        given,
        loaded.servers[case.directory_name()],
        params=params,
        status=report.state,
        issues=issues,
    )


def _staged(registry: str, case: Case, scratch: Path) -> Path:
    """The root of a registry with the files of the case: a copy of its own
    for a case with files, else the shared copy."""
    if not case.files:
        return scratch / "shared"

    root = scratch / case.id
    copy_registry(REPO_ROOT / registry, root)
    for path, text in case.files.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text.replace(PLACEHOLDER, case.directory_name()), encoding="utf-8")

    return root


def _vector(registry: str, case: Case, scratch: Path, file: str = TOML_FILE) -> Vector:
    """One written case: a directory in a copy of the registry, and away
    again."""
    directory = _staged(registry, case, scratch) / MCP_DIR / case.directory_name()
    # Only a case with files has a copy of its own, and a file of the case can
    # be in the directory already. A case in the shared copy gets a new one.
    directory.mkdir(parents=True, exist_ok=bool(case.files))
    (directory / file).write_bytes(case.text_bytes())
    try:
        loaded = attempt(lambda: load_registry(directory.parents[1]))
    finally:
        shutil.rmtree(directory)

    if isinstance(loaded, Raised):
        return raised(
            case.id, bytes_input(case.text_bytes()), loaded.exc, params=_params(registry, case)
        )

    return _outcome(case, loaded, registry)


def _fixture_vectors(registry: str, file: str = TOML_FILE) -> list[Vector]:
    """Every server file named `file` of one registry of this repository, as
    it is. `TOML_REGISTRY` gives the ids `fixture-registry-<name>`."""
    base = REPO_ROOT / registry / MCP_DIR
    if not base.is_dir():
        return []

    names = sorted(path.name for path in base.iterdir() if (path / file).is_file())
    if not names:
        return []

    loaded = load_registry(REPO_ROOT / registry)
    label = Path(registry).name.removesuffix("-toml").removesuffix("-registry")

    return [
        _outcome(
            Case(f"fixture-{label}-{name}", (base / name / file).read_bytes(), name),
            loaded,
            registry,
        )
        for name in names
    ]


def _written(registry: str, cases: tuple[Case, ...], scratch: Path, file: str) -> list[Vector]:
    """Each written case, in one copy of `registry` that the cases share."""
    copy_registry(REPO_ROOT / registry, scratch / "shared")

    return [_vector(registry, case, scratch, file) for case in cases]


def surfaces() -> tuple[Surface, ...]:
    toml: list[Vector] = []
    for registry in TOML_REGISTRIES:
        toml.extend(_fixture_vectors(registry, TOML_FILE))

    with tempfile.TemporaryDirectory(prefix="vectors-server-") as scratch:
        toml.extend(_written(TOML_REGISTRY, TOML_CASES, Path(scratch) / "toml", TOML_FILE))

    return (
        Surface(
            name="server_file.toml",
            path="server_file.toml.json",
            entry=ENTRY,
            contract=CONTRACT,
            notes=(*_notes(TOML_FILE), FILES_NOTE),
            vectors=tuple(toml),
        ),
    )
