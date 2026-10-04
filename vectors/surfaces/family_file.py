"""`family.yaml` (contract 01): the bytes of one file to its validation report.

The entry point is `agent_family.load_registry`, the one `caregiver` calls.
A family file is validated against the rest of a registry, so every vector
names the registry it sits in and its directory under `families/`. The
generator copies that registry to a temporary directory, writes the input
there and reads the report of that one family back.

Two surfaces. `family_file` has no host: the two checks that need one
downgrade to a warning. `family_file.host` has a written host.
"""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from agent_family import Registry, load_registry
from agent_family.validate import HostFacts

from vectors.core import (
    Json,
    Raised,
    Surface,
    Vector,
    accepted,
    attempt,
    bytes_input,
    normalize,
    raised,
    refused,
)
from vectors.surfaces.family_cases import CASES, PLACEHOLDER, Case

REPO_ROOT: Final = Path(__file__).resolve().parents[2]

#: The registry every written case is filed in. Public test data of `family`.
FAMILY_REGISTRY: Final = "family/tests/fixtures/registry"

#: Every registry in this repository whose family files are inputs as they are.
FIXTURE_REGISTRIES: Final = (
    FAMILY_REGISTRY,
    "integration/fixtures/eq-registry",
    "integration/fixtures/stage3-registry",
    "integration/fixtures/stage4-registry",
    "integration/fixtures/stage5-registry",
)

FAMILIES_DIR: Final = "families"
FAMILY_FILE: Final = "family.yaml"

CONTRACT: Final = "contract 01"
ENTRY: Final = "agent_family.load_registry"

NOTES: Final = (
    "The input is the bytes of one family.yaml.",
    "params.registry is a registry in this repository, as a path from the repository root. "
    "params.directory is the directory of the input under families/ in that registry.",
    "params.files holds every other file a vector adds to the registry, by path from the "
    "registry root. A file there replaces the registry's own file at that path.",
    "A family file is validated against the whole registry. To replay a vector: copy the "
    "registry, write params.files, write the input to families/<directory>/family.yaml, "
    "load the registry and read the report of <directory>.",
    "result is accepted when the report holds no issue of severity error. issues holds every "
    "issue in order. value is the parsed family, with every default filled in.",
)

#: The host of the `family_file.host` surface: the aliases LiteLLM serves,
#: and one symbolic link out of an allowed root.
HOST_ALIASES: Final = ("agent-router", "code-router", "fast")
HOST_LINKS: Final = {
    "/srv/agents/vault/linked-out": "/etc/shadow",
    "/srv/agents/vault/linked-in": "/srv/agents/code/example",
    "/srv/agents/vault/linked-platform": "/srv/agents/work/platform/agent-control",
}


@dataclass(frozen=True)
class WrittenHost:
    """A host as data: the aliases it serves and where its links point."""

    aliases: frozenset[str]
    links: dict[str, str] = field(default_factory=dict[str, str])

    def model_aliases(self) -> frozenset[str]:
        return self.aliases

    def real_path(self, path: str) -> str:
        return self.links.get(path, path)


def _params(registry: str, case: Case) -> dict[str, Json]:
    params: dict[str, Json] = {"registry": registry, "directory": case.directory_name()}
    if case.files:
        files: dict[str, Json] = {
            path: text.replace(PLACEHOLDER, case.directory_name())
            for path, text in case.files.items()
        }
        params["files"] = files

    return params


def _write_input(root: Path, case: Case) -> Path:
    """The case's own file, in its directory under `root`. Returns the directory."""
    directory = root / FAMILIES_DIR / case.directory_name()
    directory.mkdir(parents=True, exist_ok=True)
    (directory / FAMILY_FILE).write_bytes(case.text_bytes())

    return directory


@contextmanager
def _staged(registry: str, case: Case, scratch: Path) -> Generator[Path]:
    """A copy of `registry` with the case written into it.

    A case that adds files or takes the directory of a family the registry
    holds gets a copy of its own. Every other case adds one directory to a
    shared copy and takes it away again, which is the same registry and
    saves a copy per case.
    """
    if case.files or case.directory:
        root = scratch / case.id
        shutil.copytree(REPO_ROOT / registry, root)
        for path, text in case.files.items():
            target = root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text.replace(PLACEHOLDER, case.directory_name()), encoding="utf-8")

        _write_input(root, case)
        yield root
        return

    shared = scratch / "shared"
    if not shared.is_dir():
        shutil.copytree(REPO_ROOT / registry, shared)

    directory = _write_input(shared, case)
    try:
        yield shared
    finally:
        shutil.rmtree(directory)


def _outcome(case: Case, loaded: Registry, given: dict[str, Json], params: Json) -> Vector:
    """The report of one family, out of one registry that is already loaded."""
    report = loaded.reports[case.directory_name()]
    issues = [issue.as_json() for issue in report.issues]
    if not report.ok:
        return refused(case.id, given, params=params, status=report.state, issues=issues)

    return accepted(
        case.id,
        given,
        loaded.families[case.directory_name()],
        params=params,
        status=report.state,
        issues=issues,
    )


def _vector(registry: str, case: Case, scratch: Path, host: HostFacts | None) -> Vector:
    given = bytes_input(case.text_bytes())
    params = _params(registry, case)
    with _staged(registry, case, scratch) as root:
        loaded = attempt(lambda: load_registry(root, host))

    if isinstance(loaded, Raised):
        return raised(case.id, given, loaded.exc, params=params)

    return _outcome(case, loaded, given, params)


def _fixture_vectors(registry: str, host: HostFacts | None) -> list[Vector]:
    """Every family file of one registry of this repository, as it is.

    One load serves them all. The input is the file the registry already
    holds, so a copy with the input written into it is the same registry.
    """
    loaded = load_registry(REPO_ROOT / registry, host)

    return [
        _outcome(case, loaded, bytes_input(case.text_bytes()), _params(registry, case))
        for case in _fixture_cases(registry)
    ]


def _fixture_cases(registry: str) -> tuple[Case, ...]:
    """Every family file a registry of this repository holds, as it is."""
    base = REPO_ROOT / registry / FAMILIES_DIR
    names = sorted(path.name for path in base.iterdir() if (path / FAMILY_FILE).is_file())
    label = Path(registry).name.removesuffix("-registry")

    return tuple(
        Case(f"fixture-{label}-{name}", (base / name / FAMILY_FILE).read_bytes(), name)
        for name in names
    )


HOST_CASES: Final = (
    Case(
        "host-alias-unknown",
        b"name: %NAME%\nkind: thin\ndescription: One written input.\n"
        b"model: { router: no-such-alias, budget_usd_per_day: 1 }\n",
    ),
    Case(
        "host-links",
        b"name: %NAME%\nkind: thin\ndescription: One written input.\n"
        b"model: { router: fast, budget_usd_per_day: 1 }\n"
        b"files:\n"
        b"  - { path: /srv/agents/vault/linked-out, mode: ro }\n"
        b"  - { path: /srv/agents/vault/linked-in, mode: ro }\n"
        b"  - { path: /srv/agents/vault/linked-platform, mode: ro }\n"
        b"  - { path: /srv/agents/vault/plain, mode: ro }\n",
    ),
)


def surfaces() -> tuple[Surface, ...]:
    host = WrittenHost(frozenset(HOST_ALIASES), dict(HOST_LINKS))
    with tempfile.TemporaryDirectory(prefix="vectors-family-") as scratch_name:
        scratch = Path(scratch_name)
        plain: list[Vector] = []
        for registry in FIXTURE_REGISTRIES:
            plain.extend(_fixture_vectors(registry, None))

        for case in CASES:
            plain.append(_vector(FAMILY_REGISTRY, case, scratch / "written", None))

        hosted = _fixture_vectors(FAMILY_REGISTRY, host)
        for case in HOST_CASES:
            hosted.append(_vector(FAMILY_REGISTRY, case, scratch / "host", host))

    host_context = normalize({"model_aliases": sorted(HOST_ALIASES), "links": HOST_LINKS})

    return (
        Surface(
            name="family_file",
            path="family_file.json",
            entry=ENTRY,
            contract=CONTRACT,
            notes=(*NOTES, "No host: the checks that need one are warnings with downgraded true."),
            vectors=tuple(plain),
        ),
        Surface(
            name="family_file.host",
            path="family_file.host.json",
            entry=ENTRY,
            contract=CONTRACT,
            notes=(
                *NOTES,
                "context.host is the host: the aliases it serves, and the paths that are "
                "symbolic links with the path each one resolves to.",
            ),
            context={"host": host_context},
            vectors=tuple(hosted),
        ),
    )
