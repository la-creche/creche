"""The signed assets of a component: their names, their limits, who signs
them, and the words that check a signature.

CI builds each `kind: binary` component for `catalog.BINARY_TARGET`, packs
the programs into one archive and signs the archive with a keyless
signature. The Release of the tag holds two assets: the archive and the
bundle of its signature. A `kind: disk-image` component has N files and no
archive: each file and the bundle of its signature is an asset (contract
06 §3.4, §8.4). This module holds what each side of that path reads, so
each side reads one source:

1. The names of the assets and their limits, and a download URL.
2. `assets_expected`: the one answer to "does a component of this kind
   ship signed assets".
3. `SignerPolicy` and `verify_words`: the workflow, the ref and the commit
   that a signature must name, as the words of the verifier.
4. `rows` and `crate_row`: what CI builds for a binary component, as the
   line form that `handover artifacts` prints. `disk_rows`: what CI builds
   for a disk-image component, as the line form that `handover
   disk-assets` prints.
5. `ArtifactRefusal`: the four reasons of a refusal with the code `P6`.

Pure: no child, no socket, and no import of `executor/`. It reads no file.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import InitVar, dataclass
from enum import StrEnum
from typing import Final

from .allocate import TAG_RE
from .catalog import (
    ARCHIVE_MAX_BYTES,
    ARCHIVE_MAX_MEMBERS,
    ARCHIVE_MAX_UNPACKED_BYTES,
    ARCHIVE_SUFFIX,
    ARRIVING,
    BINARY_BUILD_PROGRAM,
    BINARY_INSTALL_ENV,
    BINARY_TARGET,
    BUNDLE_MAX_BYTES,
    BUNDLE_SUFFIX,
    CATALOG_BY_NAME,
    DIGESTS_FILE,
    DIGESTS_MAX_BYTES,
    DISK_FILE_MAX_BYTES,
    DISK_FLAVORS,
    DISK_OUT_ENV,
    DISK_SUFFIX,
    KERNEL_FILE,
    MAIN_BRANCH,
    RELEASE_WORKFLOW,
    RETIRING,
    Kind,
    Repo,
)
from .errors import Refusal, RefusalCode, safe_token
from .manifest import MAX_PATH_CHARS, NAME_RE, PATH_SEGMENT_RE, ComponentManifest
from .site import is_owner_name, is_repo_name
from .state import SHA_RE

#: The issuer of the identity token of a job of the code service. The
#: certificate of each signature of CI names it. The check compares it as
#: text and opens no connection to it, so it is a trust anchor and a
#: constant of the code.
TOKEN_ISSUER: Final = "https://token.actions.githubusercontent.com"

#: The host of the code service. The identity of a signature names a
#: workflow file on it, and an asset of a Release downloads from it. One
#: service, so the two read this one constant.
CODE_HOST: Final = "github.com"

#: The scheme of each URL that this module makes.
HTTPS: Final = "https://"

#: Where a repository keeps its workflow files.
WORKFLOW_DIR: Final = ".github/workflows"

#: The ref that each signature of a release names: the branch
#: `catalog.MAIN_BRANCH`.
MAIN_REF: Final = f"refs/heads/{MAIN_BRANCH}"

#: The directory of the crates of the Cargo workspace. A crate that CI
#: builds is a directory below it.
CRATES_DIR: Final = "rust/crates"

#: The words of a build after the program name and before the crate
#: directory. The build installs what `rust/Cargo.lock` pins, and cargo
#: writes no record that names the source tree.
INSTALL_ARGS: Final = ("install", "--locked", "--no-track", "--path")

#: The two path segments that a plain path never holds.
_DOT_SEGMENTS: Final = frozenset({".", ".."})


class ArtifactRefusal(StrEnum):
    """The four reasons of a refusal with the code `P6`, a closed list.

    The detail of each such refusal is one of the four texts, and the
    ledger writes the detail as the `reason`. The follower reads the
    reason back out of the ledger. Only this module holds the texts.
    """

    #: The store holds no archive or no bundle of the component.
    NO_ARTIFACT = "no artifact in the store"
    #: The verifier did not pass the signature, or the certificate names
    #: another workflow, ref or commit.
    SIGNATURE_REFUSED = "the artifact signature was refused"
    #: Root found no verifier that it can start, or a child of the
    #: verifier did not start, did not end in its time or could not load
    #: the trust root. The signature has no answer, so the follower asks
    #: again.
    VERIFIER_UNAVAILABLE = "the verifier was not available"
    #: The signature passed, and the unpack refuses the member list of
    #: the archive.
    SHAPE_REFUSED = "the archive shape was refused"


#: The kinds whose Release holds signed assets that root checks (check P6).
SIGNED_KINDS: Final = frozenset({Kind.BINARY, Kind.DISK_IMAGE})


def assets_expected(kind: Kind) -> bool:
    """Whether a component of `kind` ships signed assets: true for
    `Kind.BINARY` (one archive) and `Kind.DISK_IMAGE` (N files, CH35 (4)),
    and for no other kind.

    This is the one place that answers the question. Each caller gives the
    kind that it trusts: root the kind of a manifest, the operator side
    and CI the kind of a catalog row.
    """
    return kind in SIGNED_KINDS


@dataclass(frozen=True)
class AssetNames:
    """The names of the two assets of one binary component."""

    archive: str
    bundle: str


def asset_names(component: str) -> AssetNames:
    """`<component>-<target>.tar.gz`, and that name with the bundle end."""
    archive = f"{component}-{BINARY_TARGET}{ARCHIVE_SUFFIX}"

    return AssetNames(archive=archive, bundle=f"{archive}{BUNDLE_SUFFIX}")


@dataclass(frozen=True)
class AssetLimit:
    """One asset of a Release and its largest size in bytes."""

    name: str
    max_bytes: int


def disk_files() -> tuple[AssetLimit, ...]:
    """The files of a disk-image component, each with its largest size:
    `digests.json`, the kernel, then one root disk for each flavor of
    `catalog.DISK_FLAVORS` (contract 06 §8.4 rule 2, CH76 rules 5 and 7)."""
    disks = (f"{flavor}{DISK_SUFFIX}" for flavor in DISK_FLAVORS)

    return (
        AssetLimit(name=DIGESTS_FILE, max_bytes=DIGESTS_MAX_BYTES),
        AssetLimit(name=KERNEL_FILE, max_bytes=DISK_FILE_MAX_BYTES),
        *(AssetLimit(name=name, max_bytes=DISK_FILE_MAX_BYTES) for name in disks),
    )


def asset_limits(component: str, kind: Kind) -> tuple[AssetLimit, ...]:
    """The assets of a component of `kind`. A binary component has its
    archive, then its bundle. A disk-image component has each file of
    `disk_files`, each before its own bundle (contract 06 §3.4). A kind
    that ships no signed asset has none."""
    if kind is Kind.DISK_IMAGE:
        return tuple(
            one
            for file in disk_files()
            for one in (file, AssetLimit(f"{file.name}{BUNDLE_SUFFIX}", BUNDLE_MAX_BYTES))
        )

    if kind is not Kind.BINARY:
        return ()

    names = asset_names(component)

    return (
        AssetLimit(name=names.archive, max_bytes=ARCHIVE_MAX_BYTES),
        AssetLimit(name=names.bundle, max_bytes=BUNDLE_MAX_BYTES),
    )


def download_url(owner: str, repo: str, tag: str, asset: str) -> str:
    """Where one asset of the Release of `tag` downloads from:
    `https://<CODE_HOST>/<owner>/<repo>/releases/download/<tag>/<asset>`.

    `repo` is the name of the repository on the code service. Each part is
    checked before it goes into the URL.
    """
    _require_owner(owner)
    _require_repo(repo)
    if not TAG_RE.fullmatch(tag):
        raise _refuse("tag", f"not a component tag: {safe_token(tag)}")

    if asset in _DOT_SEGMENTS or not PATH_SEGMENT_RE.fullmatch(asset):
        raise _refuse("asset", f"not a plain file name: {safe_token(asset)}")

    return f"{HTTPS}{CODE_HOST}/{owner}/{repo}/releases/download/{tag}/{asset}"


class _Made:
    """No module but this one names this type. `SignerPolicy` takes a value
    of it first, so only `signer_policy` makes a policy: pyright refuses the
    private name in each other module, and the type refuses each other
    value."""


_MADE: Final = _Made()


@dataclass(frozen=True)
class SignerPolicy:
    """Who must have signed an archive: a job of the workflow file
    `workflow` of the repository `owner/repo`, on `MAIN_REF`, at the commit
    `commit`. The ref is no field: it is the same for each policy.

    `signer_policy` is the one maker. The fields are private to this module,
    and the accessors read them. The type checks each field itself, so a
    copy that `dataclasses.replace` makes holds the same rules.
    """

    made: InitVar[_Made]
    _owner: str
    _repo: str
    _workflow: str
    _commit: str

    def __post_init__(self, made: _Made) -> None:
        """An owner or a name that the site file would refuse, and a commit
        that is not 40 lower-case hex digits, refuse with the `request` code.
        A workflow that `catalog.RELEASE_WORKFLOW` does not hold refuses with
        the `catalog` code."""
        if made is not _MADE:
            raise TypeError("only signer_policy makes a SignerPolicy")

        _require_owner(self._owner)
        _require_repo(self._repo)
        if self._workflow not in RELEASE_WORKFLOW.values():
            detail = f"the catalog names no such release workflow: {safe_token(self._workflow)}"
            raise Refusal(RefusalCode.CATALOG, "workflow", detail)

        if not SHA_RE.fullmatch(self._commit):
            raise _refuse("commit", f"not 40 lower-case hex digits: {safe_token(self._commit)}")

    @property
    def owner(self) -> str:
        return self._owner

    @property
    def repo(self) -> str:
        return self._repo

    @property
    def workflow(self) -> str:
        return self._workflow

    @property
    def commit(self) -> str:
        return self._commit

    def repository(self) -> str:
        """`<owner>/<repo>`, as the certificate names the repository."""
        return f"{self._owner}/{self._repo}"

    def identity(self) -> str:
        """The identity of the certificate: the URL of the workflow file at
        `MAIN_REF`."""
        return f"{HTTPS}{CODE_HOST}/{self.repository()}/{WORKFLOW_DIR}/{self._workflow}@{MAIN_REF}"


def signer_policy(repo: Repo, owner: str, name: str, commit: str) -> SignerPolicy:
    """The policy for the archives of the catalog repository `repo`, which
    the code service hosts as `owner/name`, at `commit`.

    The workflow file comes from `catalog.RELEASE_WORKFLOW`. A repository
    that the map does not hold refuses with the `catalog` code: no workflow
    of it signs. The type checks the owner, the name and the commit.
    """
    workflow = RELEASE_WORKFLOW.get(repo)
    if workflow is None:
        detail = "the catalog names no release workflow for this repository"
        raise Refusal(RefusalCode.CATALOG, str(repo), detail)

    return SignerPolicy(_MADE, owner, name, workflow, commit)


def verify_words(
    policy: SignerPolicy, archive: str, bundle: str, trusted_root: str | None
) -> tuple[str, ...]:
    """The words of the verifier after its program name. The verifier
    passes only for a signature of `policy` over the bytes of `archive`.

    Without `trusted_root` the two words of `--trusted-root` are absent,
    and the verifier reads its own trust root. Each path is absolute: a
    word that starts with `-` reads as an option. No path holds a line
    feed, so each word is one line of `handover artifacts --verify-words`.
    """
    words = ["verify-blob", "--bundle", _absolute("bundle", bundle)]
    if trusted_root is not None:
        words.extend(["--trusted-root", _absolute("trusted root", trusted_root)])

    words.extend(
        [
            "--certificate-oidc-issuer",
            TOKEN_ISSUER,
            "--certificate-identity",
            policy.identity(),
            "--certificate-github-workflow-repository",
            policy.repository(),
            "--certificate-github-workflow-ref",
            MAIN_REF,
            "--certificate-github-workflow-sha",
            policy.commit,
            _absolute("archive", archive),
        ]
    )

    return tuple(words)


def install_words(crate_dir: str) -> tuple[str, ...]:
    """The build of one crate: `cargo install --locked --no-track --path
    <crate_dir>`. `crate_dir` is a relative path below `CRATES_DIR`, with
    plain segments only."""
    segments = crate_dir.split("/")
    base = CRATES_DIR.split("/")
    plain = all(one not in _DOT_SEGMENTS and PATH_SEGMENT_RE.fullmatch(one) for one in segments)
    below = len(segments) > len(base) and segments[: len(base)] == base
    if len(crate_dir) > MAX_PATH_CHARS or not plain or not below:
        detail = f"not a plain directory below {CRATES_DIR}/: {safe_token(crate_dir)}"
        raise _refuse("crate directory", detail)

    return (BINARY_BUILD_PROGRAM, *INSTALL_ARGS, crate_dir)


@dataclass(frozen=True)
class ArtifactRow:
    """What CI builds and packs for one archive, and the limits it holds
    the archive to. `commands` are the build commands, each a list of words.

    The target, the asset names, the variable and the limits are no field.
    Each one reads its constant, so no row holds another value.
    """

    component: str
    commands: tuple[tuple[str, ...], ...]

    def __post_init__(self) -> None:
        """The component has the grammar of a component name. The row has
        one command or more, and each command has one word or more. No word
        holds a line feed or a NUL. A row that breaks a rule refuses with
        the `request` code: CI would build nothing, or the line form would
        have more than one reading."""
        _check_row(self.component, self.commands)

    @property
    def target(self) -> str:
        return BINARY_TARGET

    @property
    def names(self) -> AssetNames:
        return asset_names(self.component)

    @property
    def install_env(self) -> str:
        return BINARY_INSTALL_ENV

    @property
    def max_archive_bytes(self) -> int:
        return ARCHIVE_MAX_BYTES

    @property
    def max_members(self) -> int:
        return ARCHIVE_MAX_MEMBERS

    @property
    def max_unpacked_bytes(self) -> int:
        return ARCHIVE_MAX_UNPACKED_BYTES

    def lines(self) -> tuple[str, ...]:
        """The line form, for a `read` loop of bash 3.2: one key and one
        value on each line, then `argv <count>` and one word on each of the
        next `<count>` lines for each command, then `end`. A word holds no
        line feed (`manifest.py`), so the form has one reading."""
        head = (
            f"component {self.component}",
            f"target {self.target}",
            f"archive {self.names.archive}",
            f"bundle {self.names.bundle}",
            f"install-env {self.install_env}",
            f"max-archive-bytes {self.max_archive_bytes}",
            f"max-members {self.max_members}",
            f"max-unpacked-bytes {self.max_unpacked_bytes}",
        )
        body = (line for words in self.commands for line in (f"argv {len(words)}", *words))

        return (*head, *body, "end")


def _components(repo: Repo, kind: Kind) -> tuple[str, ...]:
    """Each component of `repo` and `kind`, in catalog order. A component
    that leaves or comes into the catalog is not one of them."""
    return tuple(
        row.name
        for row in CATALOG_BY_NAME.values()
        if row.repo is repo and row.kind is kind and row.name not in RETIRING | ARRIVING
    )


def archive_components(repo: Repo) -> tuple[str, ...]:
    """Each component of `repo` that ships an archive: each `kind: binary`
    row, in catalog order."""
    return _components(repo, Kind.BINARY)


def disk_components(repo: Repo) -> tuple[str, ...]:
    """Each `kind: disk-image` component of `repo`, in catalog order."""
    return _components(repo, Kind.DISK_IMAGE)


def rows(manifests: Mapping[str, ComponentManifest], repo: Repo) -> tuple[ArtifactRow, ...]:
    """One row for each component of `archive_components(repo)`, with the
    build commands of its manifest.

    A component with no manifest in `manifests`, or with a manifest of
    another kind, refuses with the `catalog` code. A manifest with no build
    command refuses with the `manifest` code: CI would pack an empty tree.
    """
    names = archive_components(repo)

    return tuple(ArtifactRow(name, _built(manifests, name, Kind.BINARY).build) for name in names)


def _built(manifests: Mapping[str, ComponentManifest], name: str, kind: Kind) -> ComponentManifest:
    """The manifest of a component that CI builds: present, of `kind`, and
    with a build command. A missing manifest and another kind refuse with
    the `catalog` code. No build command refuses with the `manifest` code:
    CI would sign nothing."""
    manifest = manifests.get(name)
    if manifest is None:
        raise Refusal(RefusalCode.CATALOG, name, f"a {kind} component with no manifest")

    if manifest.kind is not kind:
        detail = f"the catalog says {kind}, and the manifest says {manifest.kind}"
        raise Refusal(RefusalCode.CATALOG, name, detail)

    if not manifest.build:
        raise Refusal(RefusalCode.MANIFEST, name, f"a {kind} component with no build command")

    return manifest


@dataclass(frozen=True)
class DiskRow:
    """What CI builds and signs for one disk-image component (contract 06
    §8.4 rule 1). `commands` are the build commands, each a list of words.
    The out variable and the files are no field: each reads its constant.
    """

    component: str
    commands: tuple[tuple[str, ...], ...]

    def __post_init__(self) -> None:
        """The rules of `ArtifactRow`, for the same reasons."""
        _check_row(self.component, self.commands)

    @property
    def out_env(self) -> str:
        return DISK_OUT_ENV

    @property
    def assets(self) -> tuple[AssetLimit, ...]:
        return asset_limits(self.component, Kind.DISK_IMAGE)

    def lines(self) -> tuple[str, ...]:
        """The line form of `ArtifactRow.lines`: `component`, `out-env`,
        one `asset <name> <largest size in bytes>` line for each file and
        each bundle, then each command, then `end`. A file name holds no
        space, so the form has one reading."""
        head = (
            f"component {self.component}",
            f"out-env {self.out_env}",
            *(f"asset {one.name} {one.max_bytes}" for one in self.assets),
        )
        body = (line for words in self.commands for line in (f"argv {len(words)}", *words))

        return (*head, *body, "end")


def disk_rows(manifests: Mapping[str, ComponentManifest], repo: Repo) -> tuple[DiskRow, ...]:
    """One row for each component of `disk_components(repo)`, with the
    build commands of its manifest, under the refusals of `rows`."""
    names = disk_components(repo)

    return tuple(DiskRow(name, _built(manifests, name, Kind.DISK_IMAGE).build) for name in names)


def crate_row(name: str, crate_dir: str) -> ArtifactRow:
    """One row of the same shape for a crate that need not be a component:
    one command, `install_words(crate_dir)`. `name` has the grammar of a
    component name. No release reads such a row."""
    if not NAME_RE.fullmatch(name):
        raise _refuse("crate row", f"not a component name: {safe_token(name)}")

    return ArtifactRow(name, (install_words(crate_dir),))


def _check_row(component: str, commands: tuple[tuple[str, ...], ...]) -> None:
    """The rules of a row of CI: a component name, one command or more,
    each with a word, and no word with a line feed or a NUL."""
    if not NAME_RE.fullmatch(component):
        raise _refuse("artifact row", f"not a component name: {safe_token(component)}")

    if not commands or not all(commands):
        raise _refuse("artifact row", "a row needs one command or more, each with a word")

    words = (word for command in commands for word in command)
    if any("\n" in word or "\x00" in word for word in words):
        raise _refuse("artifact row", "a word of a command holds a line feed or a NUL")


def _require_owner(owner: str) -> None:
    if not is_owner_name(owner):
        raise _refuse("owner", f"not an owner name: {safe_token(owner)}")


def _require_repo(name: str) -> None:
    if not is_repo_name(name):
        raise _refuse("repository", f"not a repository name: {safe_token(name)}")


def _absolute(label: str, path: str) -> str:
    if not path.startswith("/") or "\n" in path or "\x00" in path:
        raise _refuse(label, "the path must be absolute and one line")

    return path


def _refuse(subject: str, detail: str) -> Refusal:
    return Refusal(RefusalCode.REQUEST, subject, detail)
