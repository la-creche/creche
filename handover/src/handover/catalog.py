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
    """A repository by its name on GitHub. The value also names its clone,
    `/srv/agents/code/<value>`, and its optional site key."""

    CRECHE = "creche"
    MCP = "mcp"
    REGISTRY = "registry"
    CHAPERONE = "chaperone"


class Kind(StrEnum):
    VENV = "venv"
    #: A tree of compiled programs, each at `<install.to>/bin/<name>`, so a
    #: unit's `ExecStart=` path and a verify command path keep the shape a
    #: venv gives them. CI builds the programs from the manifest's own
    #: `build` argv, packs them into one archive and signs it. Root runs no
    #: build: check P6 verifies root's copy of the archive, and step 8
    #: unpacks it.
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
#: doors, and `agent-door-trigger` brings `agent-family`. `playpen`'s build
#: is an image build, and `playpen/Dockerfile` copies `agent/` and
#: `facade/` and installs them from the facade's lockfile, so a change to
#: either one, the Flue pin included, is a new image.
#: `test_each_component_bundles_what_its_build_installs` holds it equal.
#:
#: `chaperone` is the Rust program of the chaperone repository, and its
#: manifest is at the root of that repository. Its row replaced the row of
#: the Python `chaperone` of this repository.
CATALOG: tuple[CatalogRow, ...] = (
    CatalogRow("chaperone", Repo.CHAPERONE, ".", Kind.BINARY, Releases.YES),
    CatalogRow(
        "attendance",
        Repo.CRECHE,
        "attendance",
        Kind.VENV,
        Releases.YES,
        ("door-acp", "door-trigger", "family"),
    ),
    CatalogRow("caregiver", Repo.CRECHE, "caregiver", Kind.VENV, Releases.YES, ("family",)),
    CatalogRow("noticeboard", Repo.CRECHE, "noticeboard", Kind.VENV, Releases.YES, ("family",)),
    CatalogRow(
        "playpen", Repo.CRECHE, "playpen", Kind.OCI_IMAGE, Releases.YES, ("agent", "facade")
    ),
    CatalogRow("mcp-servers", Repo.MCP, ".", Kind.VENV, Releases.YES),
    CatalogRow("handover", Repo.CRECHE, "handover", Kind.VENV, Releases.YES),
    CatalogRow("registry-data", Repo.REGISTRY, ".", Kind.DATA, Releases.NO),
)

CATALOG_BY_NAME: dict[str, CatalogRow] = {row.name: row for row in CATALOG}

#: The three files of the Cargo workspace of each repository that every
#: `kind: binary` build of that repository reads beside its own crates. The
#: lock file pins the third-party crates, the workspace manifest holds the
#: build profile and the shared dependencies, and the toolchain file names
#: the compiler. A change to one changes the programs that a build makes,
#: so it moves the tag of every binary component of that repository and of
#: no other kind. `uv.lock` is the same rule for a venv (contract 06 §1 rule
#: 10). A repository with no entry has no Cargo workspace.
#:
#: The workspace of the chaperone repository is at its root. Its row has
#: the path `.`, so each path of that repository, `crates/` included,
#: already moves `chaperone`.
#: CONTRACT-QUESTION: contract 06 §1 rule 10 names `uv.lock` and no file of
#: a Cargo workspace. The reading taken is these three files, at these
#: paths. A `rust/.cargo/config.toml` can also change a build and is not in
#: the list. A wider list costs a release of every binary component for
#: each change to the added file.
BINARY_BUILD_FILES: dict[Repo, tuple[str, ...]] = {
    Repo.CRECHE: ("rust/Cargo.lock", "rust/Cargo.toml", "rust/rust-toolchain.toml"),
    Repo.CHAPERONE: ("Cargo.lock", "Cargo.toml", "rust-toolchain.toml"),
}

#: The first word of each `build` argv of a `kind: binary` manifest, as it
#: is written: the program name and no path. A CI runner has no fixed path
#: for this program, so the manifest names it by name only. The `build`
#: argv of every other kind starts with an absolute path.
BINARY_BUILD_PROGRAM = "cargo"

#: The one target of a `kind: binary` build in CI: a static program for
#: Linux on x86_64, with no C library of the host.
BINARY_TARGET = "x86_64-unknown-linux-musl"

#: The end of the name of the archive of a binary component:
#: `<component>-<BINARY_TARGET><ARCHIVE_SUFFIX>`, a gzip of a tar.
ARCHIVE_SUFFIX = ".tar.gz"

#: The end of the name of the signature bundle of an archive: the name of
#: the archive, then this.
BUNDLE_SUFFIX = ".sigstore.json"

#: The variable that tells `cargo install` where to write the programs of a
#: binary build. The build writes each program at `<this>/bin/<name>`.
BINARY_INSTALL_ENV = "CARGO_INSTALL_ROOT"

#: The workflow file that signs the archives of a repository. The identity
#: of each signature names this file. A repository with no entry has no
#: signer, so a binary component of that repository gets no tag.
RELEASE_WORKFLOW: dict[Repo, str] = {
    Repo.CRECHE: "release.yml",
    Repo.CHAPERONE: "release.yml",
}

#: The one branch that a release comes from. A signature names the ref of
#: this branch, and the merge of a release is a merge into it.
MAIN_BRANCH = "main"

#: Components on their way out of the catalog. A checkout may carry such a
#: component's manifest or not, and both read. A component leaves in two
#: commits, this entry first and the directory's deletion second, so the
#: requester a host already runs keeps planning across the second one. The
#: row goes once no installed requester expects the file.
RETIRING: frozenset[str] = frozenset()

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

#: The newest version of the `component-manifest` contract that this reader
#: takes, as its two numbers. A manifest that declares `0.MINOR` reads when
#: its MINOR is at or below this one (contract 06 §3.1). The one source of
#: the two numbers is the `provides` row of `handover/component.yaml`.
#: `tests/test_handover_contract_version.py` fails when they differ from it.
MANIFEST_CONTRACT_MAJOR = 0
MANIFEST_CONTRACT_MINOR = 20

#: `handover` deploys last whatever `depends_on` says (contract 06 §1.1).
LAST_IN_ORDER = "handover"

#: stage7-releases.md §2.3: at most eight entries, the catalog minus
#: `registry-data`, which never releases.
MAX_REQUEST_COMPONENTS = 8

#: The limits of the two assets of a binary component. CI refuses an
#: archive past a limit before it signs it, and each reader of the archive
#: refuses it too. The size of the archive and of its bundle, in bytes.
ARCHIVE_MAX_BYTES = 256 * 1024 * 1024
BUNDLE_MAX_BYTES = 1024 * 1024

#: The count of members of an archive, and the bytes of those members after
#: the unpack.
ARCHIVE_MAX_MEMBERS = 64
ARCHIVE_MAX_UNPACKED_BYTES = 512 * 1024 * 1024


def releasable_names() -> tuple[str, ...]:
    """Every component a request may name, in catalog order."""
    return tuple(row.name for row in CATALOG if row.releases is Releases.YES)
