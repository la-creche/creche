"""The fixed component list and the fixed contract owners (contract 06 §1, §3).

Both lists live in code, not in a file a merge can lengthen. Contract 06 §10
turns that into two refusals: a listed component with no `component.yaml`, and
a `component.yaml` for a name this list does not hold. Contract 06 §7 says why
the list is fixed: a manifest whose shape changes under the release tool is
not a manifest.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Repo(StrEnum):
    AGENT_CONTROL = "agent-control"
    AGENT_MCP = "agent-mcp"
    AGENT_REGISTRY = "agent-registry"


class Kind(StrEnum):
    VENV = "venv"
    #: A tree of compiled programs, each at `<install.to>/bin/<name>`, so a
    #: unit's `ExecStart=` path and a verify command path keep the shape a
    #: venv gives them. Root builds it on the host from the manifest's own
    #: `build` argv, as it builds a venv.
    #: CONTRACT-QUESTION: contract 06 §8 lists four kinds and not this one.
    #: The reading taken keeps the trust model of a venv: root fetches the
    #: source at the approved tag and builds it on the host. The other
    #: reading verifies an artifact that CI built and attested, and the
    #: operator decides between them. A cargo build script runs arbitrary
    #: code as the user that runs the build, so that user matters. A change
    #: costs a new provenance rule and a download path, which root does not
    #: have today.
    BINARY = "binary"
    OCI_IMAGE = "oci-image"
    COMPOSE = "compose"
    DATA = "data"


class RunsAs(StrEnum):
    ROOT = "root"
    #: The site's operator account (`site.py`). A manifest never names the
    #: account itself: which one it is differs from one host to the next.
    OPERATOR = "operator"
    SANDBOX = "sandbox"
    #: The one value that names no single user. `mcp-servers` is a FAMILY
    #: of processes and each one runs as its own `mcp-<name>` (contract 01b
    #: §9, `stage7-releases.md` §4.2), so a third-party package never runs
    #: as root and never as the PEP's own user. Contract 06 §8.
    MCP = "mcp"
    NONE = "none"


class VerifyUser(StrEnum):
    ROOT = "root"
    OPERATOR = "operator"


class RestoreMode(StrEnum):
    AUTOMATIC = "automatic"
    MANUAL = "manual"


class Action(StrEnum):
    DEPLOY = "deploy"
    UNCHANGED = "unchanged"
    RESTORE = "restore"


class ContractId(StrEnum):
    FAMILY_FILE = "family-file"
    SESSION_API = "session-api"
    CHANNEL = "channel"
    PEP_GRANT = "pep-grant"
    MANAGER_STATUS = "manager-status"
    COMPONENT_MANIFEST = "component-manifest"


class Releases(StrEnum):
    """Contract 06 §8's `release` field, as an enum rather than a bare bool."""

    YES = "yes"
    NO = "no"


@dataclass(frozen=True)
class CatalogRow:
    """One row of contract 06 §1. `path` is repo-relative, `.` for a whole repo.

    `bundles` is every other workspace directory the component's `build`
    installs (contract 06 §1 rule 9). A change there changes the artifact,
    so it moves the component's tag exactly as a change under `path` does.
    """

    name: str
    repo: Repo
    path: str
    kind: Kind
    releases: Releases
    bundles: tuple[str, ...] = ()


#: Contract 06 §1, in its own order. `caregiver` sits at `caregiver/`,
#: `noticeboard` at `noticeboard/` and `playpen` at `playpen/`.
#:
#: `bundles` follows each build through `uv.lock`: `attendance` installs the
#: doors, and `agent-door-trigger` brings `agent-family`; `chaperone`
#: imports the requester, so `handover` ships in `chaperone`'s tree.
#: `playpen`'s build is an image build, and `playpen/Dockerfile` copies the
#: pi pin out of `toybox/`, so a pin that moves is a new image.
#: `test_each_component_bundles_what_its_build_installs` holds it equal.
CATALOG: tuple[CatalogRow, ...] = (
    CatalogRow(
        "chaperone", Repo.AGENT_CONTROL, "chaperone", Kind.VENV, Releases.YES, ("handover",)
    ),
    CatalogRow(
        "attendance",
        Repo.AGENT_CONTROL,
        "attendance",
        Kind.VENV,
        Releases.YES,
        ("door-owui", "door-tui", "door-trigger", "family"),
    ),
    CatalogRow("caregiver", Repo.AGENT_CONTROL, "caregiver", Kind.VENV, Releases.YES, ("family",)),
    CatalogRow(
        "noticeboard", Repo.AGENT_CONTROL, "noticeboard", Kind.VENV, Releases.YES, ("family",)
    ),
    CatalogRow("playpen", Repo.AGENT_CONTROL, "playpen", Kind.OCI_IMAGE, Releases.YES, ("toybox",)),
    CatalogRow("mcp-servers", Repo.AGENT_MCP, ".", Kind.VENV, Releases.YES),
    CatalogRow("infra", Repo.AGENT_CONTROL, "infra", Kind.COMPOSE, Releases.YES),
    CatalogRow("handover", Repo.AGENT_CONTROL, "handover", Kind.VENV, Releases.YES),
    CatalogRow("registry-data", Repo.AGENT_REGISTRY, ".", Kind.DATA, Releases.NO),
)

CATALOG_BY_NAME: dict[str, CatalogRow] = {row.name: row for row in CATALOG}

#: The three files of the Cargo workspace that every `kind: binary` build
#: reads beside its own crates. The lock file pins the third-party crates,
#: the workspace manifest holds the build profile and the shared
#: dependencies, and the toolchain file names the compiler. A change to one
#: changes the programs that a build makes, so it moves the tag of every
#: binary component and of no other kind. `uv.lock` is the same rule for a
#: venv (contract 06 §1 rule 10).
#: CONTRACT-QUESTION: contract 06 §1 rule 10 names `uv.lock` and no file of
#: a Cargo workspace. The reading taken is these three files, at these
#: paths. A `rust/.cargo/config.toml` can also change a build and is not in
#: the list. A wider list costs a release of every binary component for
#: each change to the added file.
BINARY_BUILD_FILES: tuple[str, ...] = (
    "rust/Cargo.lock",
    "rust/Cargo.toml",
    "rust/rust-toolchain.toml",
)

#: Components on their way out of the catalog. A checkout may carry such a
#: component's manifest or not, and both read. A component leaves in two
#: commits, this entry first and the directory's deletion second, so the
#: requester a host already runs keeps planning across the second one. The
#: row goes once no installed requester expects the file.
RETIRING: frozenset[str] = frozenset({"infra"})

#: Components on their way INTO the catalog, the opposite of `RETIRING`. The
#: row lands before the directory, so a checkout carries the manifest or not,
#: and both read. Discovery tolerates the absent file and the allocator tags
#: none of them: a tag is a version somebody can release, and there is no
#: tree to release yet. A name leaves this set in the commit that adds its
#: directory. Empty now: `handover` arrived with its directory.
ARRIVING: frozenset[str] = frozenset()

#: Contract 06 §3's provider column. Rule C3 refuses any other claimant.
CONTRACT_OWNER: dict[ContractId, str] = {
    ContractId.FAMILY_FILE: "caregiver",
    ContractId.SESSION_API: "attendance",
    ContractId.CHANNEL: "playpen",
    ContractId.PEP_GRANT: "chaperone",
    ContractId.MANAGER_STATUS: "caregiver",
    ContractId.COMPONENT_MANIFEST: "handover",
}

#: The `component-manifest` contract version this code speaks: contract 06's
#: own `Version:` line, first two numbers (contract 06 §3). A manifest written
#: against `0.MINOR` for MINOR at or below this one is accepted, which is what
#: §3.1's `provides` means.
MANIFEST_CONTRACT_MAJOR = 0
MANIFEST_CONTRACT_MINOR = 6

#: `handover` deploys last whatever `depends_on` says (contract 06 §1.1).
LAST_IN_ORDER = "handover"

#: stage7-releases.md §2.3: at most eight entries, the catalog minus
#: `registry-data`, which never releases.
MAX_REQUEST_COMPONENTS = 8


def releasable_names() -> tuple[str, ...]:
    """Every component a request may name, in catalog order."""
    return tuple(row.name for row in CATALOG if row.releases is Releases.YES)
