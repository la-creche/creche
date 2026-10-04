"""`server.yaml` (contract 01b): the bytes of one file to its validation report.

The entry point is `agent_family.load_registry`, as for `family_file`. It
calls `parse_server` and `check_server` for each directory under `mcp/`. The
generator copies a registry to a temporary directory, writes the input there
and reads the report of that one server back.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import Final

from agent_family import Registry, load_registry

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
from vectors.surfaces.family_cases import Case
from vectors.surfaces.family_file import (
    FAMILY_REGISTRY,
    FIXTURE_REGISTRIES,
    REPO_ROOT,
    copy_registry,
)
from vectors.surfaces.server_cases import CASES

MCP_DIR: Final = "mcp"
SERVER_FILE: Final = "server.yaml"

CONTRACT: Final = "contract 01b"
ENTRY: Final = "agent_family.load_registry"

NOTES: Final = (
    "The input is the bytes of one server.yaml.",
    "params.registry is a registry in this repository, as a path from the repository root. "
    "params.directory is the directory of the input under mcp/ in that registry.",
    "family_file.registries.json holds each file of each such registry.",
    "To replay a vector: copy the registry, write the input to mcp/<directory>/server.yaml, "
    "load the registry and read the server report of <directory>.",
    "result is accepted when the report holds no issue of severity error. issues holds every "
    "issue in order. value is the parsed server file, with every default filled in.",
)


def _params(registry: str, case: Case) -> dict[str, Json]:
    return {"registry": registry, "directory": case.directory_name()}


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


def _vector(registry: str, case: Case, shared: Path) -> Vector:
    """One written case: a directory in the shared copy, and away again."""
    directory = shared / MCP_DIR / case.directory_name()
    directory.mkdir(parents=True)
    (directory / SERVER_FILE).write_bytes(case.text_bytes())
    try:
        loaded = attempt(lambda: load_registry(shared))
    finally:
        shutil.rmtree(directory)

    if isinstance(loaded, Raised):
        return raised(
            case.id, bytes_input(case.text_bytes()), loaded.exc, params=_params(registry, case)
        )

    return _outcome(case, loaded, registry)


def _fixture_vectors(registry: str) -> list[Vector]:
    """Every server file of one registry of this repository, as it is."""
    base = REPO_ROOT / registry / MCP_DIR
    if not base.is_dir():
        return []

    loaded = load_registry(REPO_ROOT / registry)
    names = sorted(path.name for path in base.iterdir() if (path / SERVER_FILE).is_file())
    label = Path(registry).name.removesuffix("-registry")

    return [
        _outcome(
            Case(f"fixture-{label}-{name}", (base / name / SERVER_FILE).read_bytes(), name),
            loaded,
            registry,
        )
        for name in names
    ]


def surfaces() -> tuple[Surface, ...]:
    vectors: list[Vector] = []
    for registry in FIXTURE_REGISTRIES:
        vectors.extend(_fixture_vectors(registry))

    with tempfile.TemporaryDirectory(prefix="vectors-server-") as scratch:
        shared = Path(scratch) / "shared"
        copy_registry(REPO_ROOT / FAMILY_REGISTRY, shared)
        for case in CASES:
            vectors.append(_vector(FAMILY_REGISTRY, case, shared))

    return (
        Surface(
            name="server_file",
            path="server_file.json",
            entry=ENTRY,
            contract=CONTRACT,
            notes=NOTES,
            vectors=tuple(vectors),
        ),
    )
