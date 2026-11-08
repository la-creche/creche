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
    CRECHE_RELEASE = "creche-release"


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
    #: A tree of unit files and the scripts they run (contract 06 §8.3).
    #: It has no `build` and names no `unit`. Root copies the fetched tree,
    #: and step 9 links each unit file of the live tree into place.
    UNITS = "units"
    #: A set of signed files at the root of `install.to`: the guest kernel,
    #: one root disk for each flavor and `digests.json` (contract 06 §8.4).
    #: CI builds and signs each file. Root checks each signature, installs
    #: each file unchanged and runs no build (CH7-2, CH35 (4), CH76 rule 5).
    #: It names no unit, and nothing runs it.
    DISK_IMAGE = "disk-image"


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


class Lock(StrEnum):
    """Contract 06 §1 rule 10: which lock file a venv row's build reads."""

    #: The root `uv.lock`. The root workspace builds the row, so a change to
    #: that lock moves its tag.
    WORKSPACE = "workspace"
    #: `<path>/uv.lock`, under the row's own path. The row is no workspace
    #: member, and the root `uv.lock` moves no tag of it.
    OWN = "own"


@dataclass(frozen=True)
class CatalogRow:
    """One row of contract 06 §1. `path` is repo-relative, `.` for a whole repo.

    `bundles` is every other workspace directory the component's `build`
    installs (contract 06 §1 rule 9). A change there changes the artifact,
    so it moves the component's tag exactly as a change under `path` does.

    `lock` is the lock file that a venv build reads (contract 06 §1 rule
    10). Only a `kind: venv` row reads it.
    """

    name: str
    repo: Repo
    path: str
    kind: Kind
    releases: Releases
    bundles: tuple[str, ...] = ()
    lock: Lock = Lock.WORKSPACE


#: Contract 06 §1, in its own order. `caregiver` sits at `caregiver/`,
#: `noticeboard` at `noticeboard/` and `playpen` at `playpen/`.
#:
#: `bundles` follows each build through `uv.lock`: `attendance` installs the
#: doors, `agent-door-trigger` brings `agent-family`, and `caregiver`
#: brings `agent-family` and `creche-wire`. `playpen`'s build
#: is an image build, and `playpen/Dockerfile` copies `agent/` and
#: `facade/` into the image. Its test venv resolves the workspace from
#: `pyproject.toml` and `uv.lock`, so the row bundles those two files: the
#: pins of the venv are the pins of the lock, and no member's code is in
#: the image.
#: `test_each_component_bundles_what_its_build_installs` holds it equal.
#:
#: `host` is the whole creche-release repository: the units that belong to
#: no component, and the scripts they run (contract 06 §1 rule 12).
#:
#: `chaperone` is the Rust program of the chaperone repository, and its
#: manifest is at the root of that repository. Its row replaced the row of
#: the Python `chaperone` of this repository.
#:
#: `vmm` is the Rust workspace of this repository, at `rust/`: four
#: programs, `vmm`, `guest-init`, `cloud-hypervisor` and `ch-remote`, in one
#: archive (CH7-5, CH9, item 12 (1)). Its manifest is `rust/component.toml`.
#: It is the row before `disks`.
#:
#: `disks` is the set of signed disk files of this repository, at `disks/`:
#: the guest kernel, one root disk for each flavor of `DISK_FLAVORS` and
#: `digests.json` (CH7-2, CH35 (4), CH76 rule 5). The files come from
#: `playpen/Dockerfile` and the playpen scripts, so the row bundles
#: `playpen` and each path that `playpen` bundles: a change that moves the
#: `playpen` tag moves the `disks` tag too (contract 06 §1 rule 15). Its
#: manifest is `disks/component.toml`. It is the last row.
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
    CatalogRow("caregiver", Repo.CRECHE, "caregiver", Kind.VENV, Releases.YES, ("family", "wire")),
    CatalogRow("noticeboard", Repo.CRECHE, "noticeboard", Kind.VENV, Releases.YES, ("family",)),
    CatalogRow(
        "playpen",
        Repo.CRECHE,
        "playpen",
        Kind.OCI_IMAGE,
        Releases.YES,
        ("agent", "facade", "pyproject.toml", "uv.lock"),
    ),
    CatalogRow("mcp-servers", Repo.MCP, ".", Kind.VENV, Releases.YES),
    CatalogRow("handover", Repo.CRECHE, "handover", Kind.VENV, Releases.YES),
    CatalogRow("registry-data", Repo.REGISTRY, ".", Kind.DATA, Releases.NO),
    CatalogRow("host", Repo.CRECHE_RELEASE, ".", Kind.UNITS, Releases.YES),
    CatalogRow("memory", Repo.CRECHE, "memory", Kind.VENV, Releases.YES, (), Lock.OWN),
    CatalogRow("vmm", Repo.CRECHE, "rust", Kind.BINARY, Releases.YES),
    CatalogRow(
        "disks",
        Repo.CRECHE,
        "disks",
        Kind.DISK_IMAGE,
        Releases.YES,
        ("playpen", "agent", "facade", "pyproject.toml", "uv.lock"),
    ),
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
#: directory. `vmm` arrived with `rust/component.toml` (CH7-5, CH76 rule
#: 1). `disks` leaves it with `disks/component.toml` (CH76 rule 1).
ARRIVING: frozenset[str] = frozenset({"disks"})

#: Repositories that a host may not have cloned yet. Discovery tolerates the
#: absent manifest of each component of such a repository, as it does for a
#: name in `ARRIVING`. The allocator tags each one as usual: the repository
#: holds the tree, so a tag is a version somebody can release. A repository
#: leaves this set in the commit that makes its clone on the host a
#: requirement. Empty now: `creche-release` left it when the host cloned it.
ARRIVING_REPOS: frozenset[Repo] = frozenset()

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
MANIFEST_CONTRACT_MINOR = 23

#: `handover` deploys last whatever `depends_on` says (contract 06 §1.1).
LAST_IN_ORDER = "handover"

#: spec §9: at most eleven entries. The catalog minus `registry-data`, which
#: never releases, is eleven rows with the `disks` row, at the cap (CH35
#: (1)). `tests/test_handover_catalog_cap.py` fails when a releasing row
#: passes it.
MAX_REQUEST_COMPONENTS = 11

#: The limits of the two assets of a binary component. CI refuses an
#: archive past a limit before it signs it, and each reader of the archive
#: refuses it too. The size of the archive and of its bundle, in bytes.
#: Each bundle of a disk-image component has the bundle limit too.
ARCHIVE_MAX_BYTES = 256 * 1024 * 1024
BUNDLE_MAX_BYTES = 1024 * 1024

#: The count of members of an archive, and the bytes of those members after
#: the unpack.
ARCHIVE_MAX_MEMBERS = 64
ARCHIVE_MAX_UNPACKED_BYTES = 512 * 1024 * 1024

#: The flavors of a root disk, one file `<flavor>.ext4.zst` each. They are
#: the values of the wire enum `SandboxFlavor`, and the vmm refuses a
#: `digests.json` with any other key (CH58 rule 4, CH76 rule 7).
#: `tests/test_handover_disk_flavors.py` holds the two equal. Coding joins
#: at the cut, with the PR that adds it to the enum.
DISK_FLAVORS: tuple[str, ...] = ("base", "python")

#: The end of the name of the root disk of a flavor: `<flavor><this>`. An
#: ext4 file, compressed with zstd. Root installs it unchanged (CH35 (4)).
DISK_SUFFIX = ".ext4.zst"

#: The guest kernel, compressed with zstd (CH7-3).
KERNEL_FILE = "vmlinux-x86_64.zst"

#: The digest of each other file of the row, after decompression (CH41
#: rule 1, CH58 rule 4). The vmm reads it from `VMM_IMAGE_ROOT` (CH57
#: rule 1).
DIGESTS_FILE = "digests.json"

#: The largest file of a disk-image row: GitHub takes an asset under 2 GiB
#: (contract 06 §8.4).
DISK_FILE_MAX_BYTES = 2 * 1024 * 1024 * 1024 - 1

#: The largest `digests.json`: the cap of the reader of the vmm
#: (`DIGESTS_CAP` of `rust/crates/vmm/src/image_root.rs`).
DIGESTS_MAX_BYTES = 64 * 1024

#: The variable that tells the build of a disk-image row where to write
#: its files. Only CI runs that build (contract 06 §8).
DISK_OUT_ENV = "DISK_OUT"


def releasable_names() -> tuple[str, ...]:
    """Every component a request may name, in catalog order."""
    return tuple(row.name for row in CATALOG if row.releases is Releases.YES)
