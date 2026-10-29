"""What the `kind: binary` tests share.

The catalog holds no binary component, and this change adds none. So a test
that needs one swaps one row of the catalog for a binary copy of itself, for
that test alone. A test of the executor needs no catalog change: the
executor reads the kind from the manifest, so the manifest says `binary`.

Nothing here runs `cargo`, and root runs no build of a binary component. CI
builds the programs and packs them into one signed archive. Root verifies
its copy of the archive (check P6) and unpacks it at step 8. So this module
holds:

1. `archive_bytes`: a component archive, as CI packs one.
2. `signed_bundle` and `STAND_IN`: the bundle of a signature, and a
   stand-in verifier that passes only a bundle for the bytes of the archive
   and for the identity of the signer.
3. `StandIn`: the runner of a whole release. It gives each verifier child
   to the real check of `host.verifier_argv`, and then starts the stand-in
   as a child with the words of the verifier.
4. `verified_artifact`: a `VerifiedArtifact`, from check P6 itself.

`staged_binary_tree` writes what the unpack leaves. `Staging.build` puts
`made` in the place of the unpack, so a test can stage a tree that no
archive unpacks to, and the walk after the unpack still refuses it.
"""

from __future__ import annotations

import hashlib
import io
import json
import subprocess
import sys
import tarfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path

import pytest
from handover.artifacts import TOKEN_ISSUER, asset_names, signer_policy
from handover.catalog import CATALOG, CATALOG_BY_NAME, CatalogRow, Kind
from handover.executor import install
from handover.executor.artifact import Copied
from handover.executor.host import (
    NO_VERIFIER_EXIT_CODE,
    UNIT_END,
    As,
    Command,
    Result,
    verifier_argv,
)
from handover.executor.install import Installer, Paths, paths_of
from handover.executor.provenance import VerifiedArtifact, check_artifact
from handover.executor.source import artifact_store
from handover.manifest import ComponentManifest, Markup, parse_manifest
from handover_executor_fixtures import SHA_OF, FakeRun, fake_host, git_host_answers
from handover_fixtures import manifest_text

from handover import allocate

#: The component the tests turn into a binary one. It is a venv in the catalog.
BINARY_NAME = "noticeboard"

#: A crate directory under the Cargo workspace, as a build bundles one.
NESTED_BUNDLE = "rust/crates/creche-contracts"

#: The fixture manifest of a binary component, beside this module.
BINARY_MANIFEST = Path(__file__).resolve().parent / "handover_bin_component.yaml"

#: The word `FakeRun` knows the build by. Every build argv below holds it.
CARGO = "cargo"

#: The fixture manifests carry no `build`, and the executor stages nothing
#: without one. This is the build of the fixture manifest file.
BUILD_LINE = (
    'build:\n  - ["cargo", "install", "--locked", "--no-track",'
    f' "--path", "rust/crates/{BINARY_NAME}"]\ninstall:'
)

#: The same build for a manifest of another kind. The first word of such a
#: build is an absolute path, so `env` starts `cargo` here.
OTHER_KIND_BUILD_LINE = BUILD_LINE.replace('"cargo"', '"/usr/bin/env", "cargo"', 1)

#: The unit of the binary component, and a second unit out of the same tree.
UNIT = "creche-noticeboard.service"
SIBLING_UNIT = "creche-noticeboard-door.service"

#: What a compiled program starts with on the host. The tests never run one.
PROGRAM_BYTES = b"\x7fELF\x02\x01\x01\x00 a compiled program\n"

PROGRAM_MODE = 0o755


def binary_row(bundles: tuple[str, ...] = (NESTED_BUNDLE,)) -> CatalogRow:
    """`BINARY_NAME`'s own row, as a binary component with these bundles."""
    return replace(CATALOG_BY_NAME[BINARY_NAME], kind=Kind.BINARY, bundles=bundles)


def catalog_with(row: CatalogRow) -> tuple[CatalogRow, ...]:
    """The catalog, in its own order, with `row` in place of its namesake."""
    return tuple(row if one.name == row.name else one for one in CATALOG)


def use_binary_catalog(
    monkeypatch: pytest.MonkeyPatch, bundles: tuple[str, ...] = (NESTED_BUNDLE,)
) -> CatalogRow:
    """Make `BINARY_NAME` a binary component for one test. The allocator
    reads the row list and the digest reads the map, so both change."""
    row = binary_row(bundles)
    monkeypatch.setattr(allocate, "CATALOG", catalog_with(row))
    monkeypatch.setitem(CATALOG_BY_NAME, row.name, row)

    return row


# -- a binary component on the fake host -------------------------------------


def binary_manifest_text(
    components: Path, unit: str | None = None, kind: Kind = Kind.BINARY
) -> str:
    """`BINARY_NAME`'s manifest, with its tree under `components`, the
    cargo build and the given unit. `kind` lets a test read the same
    manifest as a venv, to show what the kind alone changes. A kind that
    is not binary gets `OTHER_KIND_BUILD_LINE`."""
    build = BUILD_LINE if kind is Kind.BINARY else OTHER_KIND_BUILD_LINE
    text = manifest_text(BINARY_NAME).replace("/opt/components", str(components))
    text = text.replace(f"kind: {CATALOG_BY_NAME[BINARY_NAME].kind}", f"kind: {kind}")
    text = text.replace("install:", build, 1)
    if unit is not None:
        text = text.replace("unit: null", f"unit: {unit}")

    return text


def unit_text(*programs: str) -> str:
    """A unit file with one `ExecStart=` line per program."""
    lines = ["[Unit]", "Description=a fixture unit", "", "[Service]"]
    lines.extend(f"ExecStart={program}" for program in programs)

    return "\n".join(lines) + "\n"


def write_program(path: Path, mode: int = PROGRAM_MODE, body: bytes = PROGRAM_BYTES) -> Path:
    """One compiled program, as `cargo install` writes it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    path.chmod(mode)

    return path


def staged_binary_tree(new: Path, *programs: str) -> Path:
    """The tree `cargo install --no-track` leaves at `new`: a `bin/` that
    holds the component's program and its verify hook, plus `programs`."""
    for name in (BINARY_NAME, f"{BINARY_NAME}-verify", *programs):
        write_program(new / "bin" / name)

    return new


@dataclass
class Staging:
    """One binary component about to be built on the fake host."""

    tmp_path: Path
    run: FakeRun
    installer: Installer
    manifest: ComponentManifest
    paths: Paths
    #: The fetched work tree, where the build argv runs.
    source: Path
    #: What the stage leaves at `<install.to>.new`, or None for nothing.
    made: Callable[[Path], object] | None = None

    def build(self) -> None:
        """Step 8 for the component. A binary manifest gets a
        `VerifiedArtifact`, and `made` stands in for its unpack."""
        if self.manifest.kind is not Kind.BINARY:
            self.installer.build(self.manifest, self.source, self.paths)

            return

        verified = verified_artifact(self.tmp_path / "copy")
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(install, "extract_tar", self._unpack)
            self.installer.build(self.manifest, self.source, self.paths, verified)

    def _unpack(self, archive: Path, into: Path, component: str | None = None) -> None:
        del archive, component
        if self.made is not None:
            self.made(into)

    def program(self, name: str = BINARY_NAME) -> str:
        """The path a unit names for one program of the LIVE tree."""
        return str(self.paths.to / "bin" / name)

    def install_unit(self, text: str, name: str = UNIT) -> Path:
        """Put a unit file where systemd reads system units."""
        path = self.tmp_path / "system-units" / name
        path.write_text(text, encoding="utf-8")

        return path

    def carry_unit(self, text: str, name: str = UNIT) -> Path:
        """Put a unit file in the fetched tree's `systemd/`."""
        path = self.source / "systemd" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

        return path

    def builds(self) -> list[Command]:
        """Every build child the executor started."""
        return [one for one in self.run.seen if any(CARGO in word for word in one.argv)]


def make_staging(
    tmp_path: Path,
    made: Callable[[Path], object] | None = staged_binary_tree,
    unit: str | None = None,
    kind: Kind = Kind.BINARY,
) -> Staging:
    """A fake host on which the stage of the component leaves what `made`
    writes at `<install.to>.new`. None leaves nothing. For a binary
    manifest `made` stands in for the unpack, and for each other kind it
    runs at the build argv."""
    components = tmp_path / "components"
    manifest = parse_manifest(
        binary_manifest_text(components, unit, kind), f"{BINARY_NAME}/component.yaml", Markup.YAML
    )
    paths = paths_of(manifest, (components,))
    run = FakeRun(answers=git_host_answers())
    if made is not None and kind is not Kind.BINARY:
        run.hooks[CARGO] = lambda _: made(paths.new)

    host = fake_host(tmp_path, run)
    source = host.work_root / "01K5J8M2Q7V3X9R4T6N0B8C2DE" / BINARY_NAME
    source.mkdir(parents=True)

    return Staging(tmp_path, run, Installer(host), manifest, paths, source, made)


# -- the archive, its signature and the verifier ------------------------------

#: Two files that root owns on each test machine, with each directory above
#: them, so `host.distrusted` passes for both. The site file of a whole
#: release names them as the verifier and as `systemd-run`. `StandIn` checks
#: them, and then the stand-in runs in their place.
ROOT_OWNED_VERIFIER = "/usr/bin/env"
ROOT_OWNED_STARTER = "/usr/bin/true"

#: The owner of the repositories in the example site file of the suite.
OWNER = "example-owner"

#: The two programs of the archive of the binary component: the component's
#: own program and its verify hook.
PROGRAMS = (BINARY_NAME, f"{BINARY_NAME}-verify")

#: The modes that the operator side gives the store.
STORE_DIR_MODE = 0o700
STORE_FILE_MODE = 0o600

#: The stand-in verifier, as the text of a Python program. It takes the
#: words of the verifier after the program name. `initialize` passes. A
#: check passes only when the bundle holds the SHA-256 of the archive and
#: the issuer, the identity and the commit of the words. It reads no other
#: file and opens no socket.
STAND_IN = """
import hashlib, json, sys
words = sys.argv[1:]
if words == ["initialize"]:
    raise SystemExit(0)
if not words or words[0] != "verify-blob" or len(words) % 2:
    raise SystemExit(2)
flags = dict(zip(words[1:-1:2], words[2:-1:2]))
with open(flags["--bundle"], "rb") as handle:
    bundle = json.load(handle)
with open(words[-1], "rb") as handle:
    digest = hashlib.sha256(handle.read()).hexdigest()
claims = {
    "sha256": digest,
    "issuer": flags["--certificate-oidc-issuer"],
    "identity": flags["--certificate-identity"],
    "commit": flags["--certificate-github-workflow-sha"],
}
raise SystemExit(0 if bundle == claims else 1)
"""


def archive_bytes(
    component: str = BINARY_NAME,
    programs: Mapping[str, bytes] | None = None,
    extra: Callable[[tarfile.TarFile], None] | None = None,
) -> bytes:
    """A component archive, as CI packs one: a gzip of a tar with one
    regular file `<component>/bin/<program>` of the mode 0755 for each
    program. `extra` adds members of its own."""
    found = programs if programs is not None else dict.fromkeys(PROGRAMS, PROGRAM_BYTES)
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w:gz") as tar:
        for name, body in found.items():
            info = tarfile.TarInfo(f"{component}/bin/{name}")
            info.size = len(body)
            info.mode = PROGRAM_MODE
            tar.addfile(info, io.BytesIO(body))

        if extra is not None:
            extra(tar)

    return raw.getvalue()


def signed_bundle(
    archive: bytes,
    commit: str = SHA_OF[BINARY_NAME],
    owner: str = OWNER,
    name: str | None = None,
) -> bytes:
    """The bundle that `STAND_IN` passes for `archive`: a signature of the
    release workflow of `creche`, hosted as `<owner>/<name>`, at
    `commit`. `name` is the catalog name when it is None."""
    row = CATALOG_BY_NAME[BINARY_NAME]
    hosted = name if name is not None else str(row.repo)
    policy = signer_policy(row.repo, owner, hosted, commit)
    claims = {
        "sha256": hashlib.sha256(archive).hexdigest(),
        "issuer": TOKEN_ISSUER,
        "identity": policy.identity(),
        "commit": commit,
    }

    return json.dumps(claims).encode("utf-8")


def stock_store(home: Path, tag: str, archive: bytes | None, bundle: bytes | None) -> Path:
    """The two files of `BINARY_NAME` at `tag` in the store below `home`,
    the operator home of the site file, as the operator side leaves them:
    each directory 0700 and each file 0600. None leaves a file out."""
    parts = (*artifact_store().relative_to(home).parts, str(CATALOG_BY_NAME[BINARY_NAME].repo), tag)
    directory = home
    for part in parts:
        directory = directory / part
        directory.mkdir(exist_ok=True)
        directory.chmod(STORE_DIR_MODE)

    names = asset_names(BINARY_NAME)
    for name, raw in ((names.archive, archive), (names.bundle, bundle)):
        if raw is None:
            continue

        (directory / name).write_bytes(raw)
        (directory / name).chmod(STORE_FILE_MODE)

    return directory


def site_text(home: Path, verifier: str = ROOT_OWNED_VERIFIER, owner: str = OWNER) -> str:
    """A site file with the owner `owner`, the operator home `home`, the
    verifier `verifier` and `ROOT_OWNED_STARTER` as `systemd-run`."""
    return (
        f"AGENT_GITHUB_OWNER={owner}\nAGENT_OPERATOR_USER=operator\n"
        f"AGENT_OPERATOR_HOME={home}\nAGENT_LAN_ADDRESS=192.0.2.10\n"
        f"AGENT_COSIGN_PROGRAM={verifier}\nAGENT_SYSTEMD_RUN_PROGRAM={ROOT_OWNED_STARTER}\n"
    )


@dataclass
class StandIn:
    """The runner of a whole release. Each child but a verifier child goes
    to `fake`. A verifier child gets the real check of `host.verifier_argv`
    and, when the check passes, the stand-in runs as a child with the words
    of the verifier. `words` records each whole argv that the check made."""

    fake: FakeRun
    words: list[tuple[str, ...]] = field(default_factory=list[tuple[str, ...]])

    def __call__(self, command: Command) -> Result:
        if command.identity is not As.VERIFIER:
            return self.fake(command)

        self.fake.seen.append(command)
        argv = verifier_argv(command)
        if argv is None:
            return Result(NO_VERIFIER_EXIT_CODE)

        self.words.append(argv)
        child = argv[argv.index(UNIT_END) + 2 :]
        done = subprocess.run(
            [sys.executable, "-I", "-c", STAND_IN, *child],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=60.0,
            check=False,
        )

        return Result(done.returncode)


def verified_artifact(
    where: Path, component: str = BINARY_NAME, archive: bytes | None = None
) -> VerifiedArtifact:
    """A `VerifiedArtifact` of `archive`, from check P6 itself: a copy in
    `where`, and a runner that passes each verifier child."""
    raw = archive if archive is not None else archive_bytes(component)
    names = asset_names(component)
    where.mkdir(parents=True, exist_ok=True)
    (where / names.archive).write_bytes(raw)
    (where / names.bundle).write_bytes(b"{}")
    copy = Copied(
        archive=where / names.archive,
        bundle=where / names.bundle,
        digest=f"sha256:{hashlib.sha256(raw).hexdigest()}",
    )
    row = CATALOG_BY_NAME[component]
    policy = signer_policy(row.repo, OWNER, str(row.repo), SHA_OF[component])

    return check_artifact(FakeRun(), component, policy, copy)
