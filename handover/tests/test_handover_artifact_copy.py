"""Root's copy of the two assets of a binary component, and its digest.

The operator side fetches the archive and its bundle into the store below
the home of the operator. At step 2 root copies the two files into the work
tree of the request and hashes its copy of the archive. The digest goes
into `artifact_digest` of the resolved manifest. At step 8 root reads its
copy again, and never the store.

Four groups of tests:

1. `copy_from_store` against a store that a test writes, as the operator
   side writes it: each directory 0700, each file 0600.
2. Each fault of the store, which gives no copy and one note line.
3. A second call in the same release.
4. The method of `Release` that puts the digest into the state, and a
   whole release of a component whose manifest says `binary`.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from pathlib import Path
from typing import cast

import pytest
from handover.artifacts import asset_names
from handover.catalog import BUNDLE_MAX_BYTES, CATALOG, CATALOG_BY_NAME, Kind
from handover.executor.approval import Decision, Summary
from handover.executor.artifact import (
    COPY_CHANGED,
    COPY_DIR,
    COPY_GONE,
    GREW,
    NO_FILE,
    NO_HOME,
    NO_STORE_DIR,
    NOT_ITS_TAG,
    NOT_OPERATORS_DIR,
    NOT_OPERATORS_FILE,
    NOT_REGULAR,
    OPEN_TO_OTHERS,
    OUTSIDE_LIMIT,
    SECOND_NAME,
    Copied,
    NoCopy,
    copy_from_store,
)
from handover.executor.drain import Counter, handle
from handover.executor.host import Host
from handover.executor.request import parse_request
from handover.executor.source import artifact_store
from handover.executor.spool import DONE_DIR, Spool
from handover.executor.steps import REGISTRY_MCP_DIR, Release, Wiring
from handover.manifest import ComponentManifest, Markup, parse_manifest
from handover.resolve import build_document, resolve
from handover.state import ReleaseState, SourceFacts
from handover_bin_fixtures import (
    BINARY_NAME,
    CARGO,
    UNIT,
    binary_manifest_text,
    staged_binary_tree,
    unit_text,
    use_binary_catalog,
    write_program,
)
from handover_executor_fixtures import (
    DIGEST,
    REQUEST_ID,
    SHA_OF,
    FakeRun,
    GitFake,
    fake_host,
    fake_readers,
    git_host_answers,
    grant_transport,
    green_api,
    make_spool_dirs,
    request_body,
    stamp_tree,
    this_uid,
    write_request,
)
from handover_fixtures import manifest_text

from handover import site

COMPONENT = BINARY_NAME
LIVE_VERSION = "0.4.0"
NEW_VERSION = "0.5.0"
TAG = f"{COMPONENT}-v{NEW_VERSION}"
REPO = str(CATALOG_BY_NAME[COMPONENT].repo)
NAMES = asset_names(COMPONENT)

#: The two files of the store, as CI made them. Root parses no byte of the
#: bundle: only the verifier reads it.
ARCHIVE = b"\x1f\x8b an archive of compiled programs\n"
BUNDLE = b'{"a bundle": "of a signature"}\n'

#: The modes that the operator side gives the store.
STORE_DIR_MODE = 0o700
STORE_FILE_MODE = 0o600


def _sha256(raw: bytes) -> str:
    return f"sha256:{hashlib.sha256(raw).hexdigest()}"


@pytest.fixture
def home(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The home of the operator, which a site file of this test names."""
    made = tmp_path_factory.mktemp("home")
    site_file = tmp_path_factory.mktemp("site") / "site.env"
    site_file.write_text(
        "AGENT_GITHUB_OWNER=example-owner\nAGENT_OPERATOR_USER=operator\n"
        f"AGENT_OPERATOR_HOME={made}\nAGENT_LAN_ADDRESS=192.0.2.10\n",
        encoding="utf-8",
    )
    monkeypatch.setenv(site.SITE_FILE_ENV, str(site_file))

    return made


#: The store below the home of the operator, as the operator side writes it.
STORE_PARTS = (".local", "state", "creche", "artifacts")


def _tag_dir(home: Path) -> Path:
    """The directory of the tag in the store, each directory 0700."""
    current = home
    for part in (*STORE_PARTS, REPO, TAG):
        current = current / part
        current.mkdir(exist_ok=True)
        current.chmod(STORE_DIR_MODE)

    return current


def _stock(home: Path, archive: bytes = ARCHIVE, bundle: bytes | None = BUNDLE) -> Path:
    """The two files in the store, as the operator side leaves them."""
    directory = _tag_dir(home)
    _put(directory / NAMES.archive, archive)
    if bundle is not None:
        _put(directory / NAMES.bundle, bundle)

    return directory


def _put(path: Path, raw: bytes) -> None:
    path.write_bytes(raw)
    path.chmod(STORE_FILE_MODE)


@pytest.fixture
def work(tmp_path: Path) -> Path:
    """The work directory of the request. The work root exists."""
    (tmp_path / "work").mkdir()

    return tmp_path / "work" / REQUEST_ID


def _copy(work: Path, uid: int | None = None) -> Copied | NoCopy:
    return copy_from_store(COMPONENT, TAG, work, os.getuid() if uid is None else uid)


def _copy_dir(work: Path) -> Path:
    return work / COPY_DIR / COMPONENT


def _note(template: str, asset: str = "") -> str:
    return template.format(component=COMPONENT, asset=asset)


def _no_copy(result: Copied | NoCopy) -> str:
    assert isinstance(result, NoCopy), result

    return result.note


def _copied(result: Copied | NoCopy) -> Copied:
    assert isinstance(result, Copied), result

    return result


def _left(work: Path) -> list[str]:
    """The files that a copy left in the directory of the component."""
    return sorted(one.name for one in _copy_dir(work).iterdir())


# ---- 1. the copy ----------------------------------------------------------------


def test_both_files_are_copied(home: Path, work: Path) -> None:
    _stock(home)

    copied = _copied(_copy(work))

    assert copied.archive == _copy_dir(work) / NAMES.archive
    assert copied.bundle == _copy_dir(work) / NAMES.bundle
    assert copied.archive.read_bytes() == ARCHIVE
    assert copied.bundle.read_bytes() == BUNDLE


def test_the_digest_is_the_sha256_of_the_archive(home: Path, work: Path) -> None:
    _stock(home)

    assert _copied(_copy(work)).digest == _sha256(ARCHIVE)


def test_the_bundle_does_not_change_the_digest(home: Path, work: Path) -> None:
    _stock(home, bundle=b"another bundle\n")

    assert _copied(_copy(work)).digest == _sha256(ARCHIVE)


def test_the_modes_of_the_copy_under_the_umask_0027(home: Path, work: Path) -> None:
    """The release unit runs with the umask 0027. The verifier runs later as
    a dynamic user, so each mode is set by hand."""
    _stock(home)
    before = os.umask(0o027)
    try:
        copied = _copied(_copy(work))
    finally:
        os.umask(before)

    for directory in (work, work / COPY_DIR, _copy_dir(work)):
        assert stat.S_IMODE(directory.stat().st_mode) == 0o755, directory

    for path in (copied.archive, copied.bundle):
        assert stat.S_IMODE(path.stat().st_mode) == 0o644, path


def test_the_store_is_left_as_it_was(home: Path, work: Path) -> None:
    directory = _stock(home)

    _copy(work)

    assert (directory / NAMES.archive).read_bytes() == ARCHIVE
    assert stat.S_IMODE((directory / NAMES.archive).stat().st_mode) == STORE_FILE_MODE


def test_the_store_is_the_store_of_the_operator_side(home: Path) -> None:
    assert artifact_store() == home.joinpath(*STORE_PARTS)


def test_no_catalog_row_has_the_name_of_a_fixed_directory_of_the_work_tree() -> None:
    """Each component clones into `<work root>/<request id>/<name>`. Two
    more names below that directory are fixed: the copies and the servers
    of `mcp-servers`."""
    assert COPY_DIR == "artifacts"
    assert REGISTRY_MCP_DIR == "mcp"
    assert COPY_DIR not in CATALOG_BY_NAME
    assert REGISTRY_MCP_DIR not in CATALOG_BY_NAME


# ---- 2. a fault gives no copy and one note --------------------------------------


@pytest.mark.parametrize("absent", ["archive", "bundle"])
def test_one_absent_file_gives_no_copy(home: Path, work: Path, absent: str) -> None:
    directory = _stock(home)
    (directory / getattr(NAMES, absent)).unlink()

    assert _no_copy(_copy(work)) == _note(NO_FILE, absent)
    assert _left(work) == []


def test_an_absent_store_gives_no_copy(home: Path, work: Path) -> None:
    del home

    assert _no_copy(_copy(work)) == _note(NO_STORE_DIR)


def test_a_link_in_the_place_of_a_file_gives_no_copy(
    home: Path, work: Path, tmp_path: Path
) -> None:
    directory = _stock(home)
    elsewhere = tmp_path / "elsewhere"
    _put(elsewhere, ARCHIVE)
    (directory / NAMES.archive).unlink()
    (directory / NAMES.archive).symlink_to(elsewhere)

    assert _no_copy(_copy(work)) == _note(NO_FILE, "archive")


@pytest.mark.parametrize("level", [".local", "creche", COPY_DIR, REPO, TAG])
def test_a_link_in_the_place_of_a_directory_of_the_path_gives_no_copy(
    home: Path, work: Path, tmp_path: Path, level: str
) -> None:
    """The directory moves aside, and a link to it takes its place. Each
    file is still there through the link."""
    directory = _stock(home)
    linked = next(one for one in (directory, *directory.parents) if one.name == level)
    aside = tmp_path / "aside"
    linked.rename(aside)
    linked.symlink_to(aside)
    assert (directory / NAMES.archive).read_bytes() == ARCHIVE

    assert _no_copy(_copy(work)) == _note(NO_STORE_DIR)
    assert _left(work) == []


def test_a_store_of_another_uid_gives_no_copy(home: Path, work: Path) -> None:
    """Root copies no file of a directory that the operator does not own."""
    _stock(home)

    assert _no_copy(_copy(work, uid=os.getuid() + 1)) == _note(NOT_OPERATORS_DIR)
    assert _left(work) == []


def _faked_fstat(
    monkeypatch: pytest.MonkeyPatch, change: Callable[[os.stat_result], os.stat_result]
) -> None:
    """`os.fstat` with `change` applied to the answer for each regular file.
    A test cannot give a file another owner, and it cannot make a file grow
    between the check and the read. The site file is read first, with the
    real `os.fstat`: its reader checks the owner of the file too."""
    site.operator_home()
    real = os.fstat

    def fstat(fd: int) -> os.stat_result:
        found = real(fd)
        if not stat.S_ISREG(found.st_mode):
            return found

        return change(found)

    monkeypatch.setattr(os, "fstat", fstat)


def _with(found: os.stat_result, **fields: int) -> os.stat_result:
    """`found` with other values for some of its first ten fields."""
    names = ("st_mode", "st_ino", "st_dev", "st_nlink", "st_uid", "st_gid", "st_size")
    values = list(found[:10])
    for name, value in fields.items():
        values[names.index(name)] = value

    return os.stat_result(values)


def test_a_file_of_another_uid_gives_no_copy(
    home: Path, work: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stock(home)
    _faked_fstat(monkeypatch, lambda found: _with(found, st_uid=found.st_uid + 1))

    assert _no_copy(_copy(work)) == _note(NOT_OPERATORS_FILE, "archive")
    assert _left(work) == []


def test_a_fifo_gives_no_copy_and_does_not_block(home: Path, work: Path) -> None:
    """A FIFO with no writer would block a plain open for read."""
    directory = _stock(home)
    (directory / NAMES.archive).unlink()
    os.mkfifo(directory / NAMES.archive, STORE_FILE_MODE)
    results: list[Copied | NoCopy] = []
    worker = threading.Thread(target=lambda: results.append(_copy(work)), daemon=True)

    worker.start()
    worker.join(timeout=30)

    assert not worker.is_alive(), "the copy blocked on the FIFO"
    assert _no_copy(results[0]) == _note(NOT_REGULAR, "archive")


def test_a_directory_in_the_place_of_a_file_gives_no_copy(home: Path, work: Path) -> None:
    directory = _stock(home)
    (directory / NAMES.bundle).unlink()
    (directory / NAMES.bundle).mkdir(mode=STORE_DIR_MODE)

    assert _no_copy(_copy(work)) == _note(NOT_REGULAR, "bundle")
    assert _left(work) == []


def test_a_file_with_two_names_gives_no_copy(home: Path, work: Path) -> None:
    directory = _stock(home)
    os.link(directory / NAMES.archive, directory / "second-name")

    assert _no_copy(_copy(work)) == _note(SECOND_NAME, "archive")


def test_a_file_open_to_group_or_other_gives_no_copy(home: Path, work: Path) -> None:
    """The operator side gives each file 0600 (`handover/AGENTS.md`, rule
    5a)."""
    directory = _stock(home)
    (directory / NAMES.bundle).chmod(0o640)

    assert _no_copy(_copy(work)) == _note(OPEN_TO_OTHERS, "bundle")
    assert _left(work) == []


def test_a_size_over_the_limit_gives_no_copy(home: Path, work: Path) -> None:
    directory = _stock(home)
    with (directory / NAMES.bundle).open("r+b") as handle:
        handle.truncate(BUNDLE_MAX_BYTES + 1)

    assert _no_copy(_copy(work)) == _note(OUTSIDE_LIMIT, "bundle")
    assert _left(work) == []


def test_an_archive_over_its_limit_gives_no_copy(home: Path, work: Path) -> None:
    """A sparse file: the size is over the limit, and the disk holds no
    such count of bytes."""
    from handover.catalog import ARCHIVE_MAX_BYTES

    directory = _stock(home)
    with (directory / NAMES.archive).open("r+b") as handle:
        handle.truncate(ARCHIVE_MAX_BYTES + 1)

    assert _no_copy(_copy(work)) == _note(OUTSIDE_LIMIT, "archive")


def test_an_empty_file_gives_no_copy(home: Path, work: Path) -> None:
    _stock(home, archive=b"")

    assert _no_copy(_copy(work)) == _note(OUTSIDE_LIMIT, "archive")


def test_a_file_longer_than_its_size_said_gives_no_copy(
    home: Path, work: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The read stops past the limit, whatever the size said."""
    _stock(home, bundle=b"b" * (BUNDLE_MAX_BYTES + 1))
    _faked_fstat(monkeypatch, lambda found: _with(found, st_size=min(found.st_size, 1)))

    assert _no_copy(_copy(work)) == _note(GREW, "bundle")
    assert _left(work) == []


def test_a_file_of_exactly_its_limit_is_copied(home: Path, work: Path) -> None:
    _stock(home, bundle=b"b" * BUNDLE_MAX_BYTES)

    assert _copied(_copy(work)).bundle.stat().st_size == BUNDLE_MAX_BYTES


def test_a_tag_of_another_component_gives_no_copy(home: Path, work: Path) -> None:
    _stock(home)

    result = copy_from_store(COMPONENT, f"caregiver-v{NEW_VERSION}", work, os.getuid())

    assert _no_copy(result) == _note(NOT_ITS_TAG)
    assert not work.exists()


def test_a_site_file_with_no_home_gives_no_copy(
    work: Path, tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    site_file = tmp_path_factory.mktemp("site") / "site.env"
    site_file.write_text("AGENT_OPERATOR_USER=operator\n", encoding="utf-8")
    monkeypatch.setenv(site.SITE_FILE_ENV, str(site_file))

    assert _no_copy(_copy(work)) == _note(NO_HOME)


# ---- 3. a second call in the same release ----------------------------------------


def test_a_second_call_gives_the_digest_of_the_first_after_a_change_of_the_store(
    home: Path, work: Path
) -> None:
    directory = _stock(home)
    first = _copied(_copy(work))
    _put(directory / NAMES.archive, b"other bytes in the store\n")

    second = _copied(_copy(work))

    assert second.digest == first.digest == _sha256(ARCHIVE)


def test_a_second_call_after_the_copy_is_removed_gives_no_copy(home: Path, work: Path) -> None:
    """The store still holds both files. The second call does not read it."""
    _stock(home)
    first = _copied(_copy(work))
    first.archive.unlink()
    first.bundle.unlink()

    assert _no_copy(_copy(work)) == _note(COPY_GONE, "archive")
    assert _left(work) == []


def test_a_second_call_after_a_first_with_no_store_reads_no_store(home: Path, work: Path) -> None:
    """The files arrive in the store after step 2. Step 8 copies nothing."""
    assert isinstance(_copy(work), NoCopy)
    _stock(home)

    assert _no_copy(_copy(work)) == _note(COPY_GONE, "archive")


def test_a_second_call_hashes_a_changed_copy_again(home: Path, work: Path) -> None:
    _stock(home)
    first = _copied(_copy(work))
    changed = bytearray(ARCHIVE)
    changed[0] ^= 1
    first.archive.write_bytes(bytes(changed))

    second = _copied(_copy(work))

    assert second.digest == _sha256(bytes(changed)) != first.digest


def test_a_second_call_refuses_a_copy_with_another_mode(home: Path, work: Path) -> None:
    _stock(home)
    first = _copied(_copy(work))
    first.bundle.chmod(0o666)

    assert _no_copy(_copy(work)) == _note(COPY_CHANGED, "bundle")


# ---- 4. the digest in the resolved manifest ----------------------------------------


def _manifest(components: Path, kind: Kind) -> ComponentManifest:
    return parse_manifest(
        binary_manifest_text(components, UNIT, kind),
        f"{COMPONENT}/component.yaml",
        Markup.YAML,
    )


@pytest.fixture
def release(tmp_path: Path) -> Iterator[Release]:
    """A release of the component, live at `LIVE_VERSION`, to `NEW_VERSION`."""
    run = FakeRun(answers=git_host_answers())
    wiring = Wiring(host=fake_host(tmp_path, run), transport=grant_transport())
    raw = json.dumps(request_body({COMPONENT: NEW_VERSION})).encode("utf-8")
    spool = Spool(str(make_spool_dirs(tmp_path)), this_uid())
    try:
        yield Release(parse_request(raw, REQUEST_ID), wiring, spool)
    finally:
        spool.close()


def _state() -> ReleaseState:
    facts = SourceFacts(sha=SHA_OF[COMPONENT], input_digest=DIGEST, artifact_digest=None)

    return ReleaseState(live={COMPONENT: LIVE_VERSION}, facts={COMPONENT: facts})


def test_a_binary_manifest_puts_the_digest_into_the_state(
    home: Path, release: Release, tmp_path: Path
) -> None:
    """The catalog row of the component says `venv`. The method reads the
    kind of the manifest and no kind of a row."""
    assert CATALOG_BY_NAME[COMPONENT].kind is Kind.VENV
    _stock(home)
    state = _state()
    manifests = {COMPONENT: _manifest(tmp_path / "components", Kind.BINARY)}

    found = release._copy_artifacts(state, manifests)

    assert found.facts[COMPONENT].artifact_digest == _sha256(ARCHIVE)
    assert found.facts[COMPONENT].sha == SHA_OF[COMPONENT]
    assert state.facts[COMPONENT].artifact_digest is None
    assert isinstance(release.copies[COMPONENT], Copied)


def test_a_venv_manifest_gets_no_copy_and_null(
    home: Path, release: Release, tmp_path: Path
) -> None:
    """The store holds both files, and the manifest says `venv`."""
    _stock(home)
    manifests = {COMPONENT: _manifest(tmp_path / "components", Kind.VENV)}

    found = release._copy_artifacts(_state(), manifests)

    assert found.facts[COMPONENT].artifact_digest is None
    assert release.copies == {}
    assert not (tmp_path / "work" / REQUEST_ID / COPY_DIR).exists()


def test_a_component_that_does_not_deploy_gets_no_copy(
    home: Path, release: Release, tmp_path: Path
) -> None:
    _stock(home)
    state = replace(_state(), live={COMPONENT: NEW_VERSION})
    manifests = {COMPONENT: _manifest(tmp_path / "components", Kind.BINARY)}

    found = release._copy_artifacts(state, manifests)

    assert found.facts[COMPONENT].artifact_digest is None
    assert release.copies == {}


def test_a_binary_component_with_no_copy_keeps_null_and_says_why(
    home: Path, release: Release, tmp_path: Path
) -> None:
    del home
    manifests = {COMPONENT: _manifest(tmp_path / "components", Kind.BINARY)}

    found = release._copy_artifacts(_state(), manifests)

    assert found.facts[COMPONENT].artifact_digest is None
    assert release.copies[COMPONENT] == NoCopy(_note(NO_STORE_DIR))
    assert _note(NO_STORE_DIR) in release.entry.log


# ---- a whole release ---------------------------------------------------------------


@dataclass
class Bench:
    """A whole release of the component, whose catalog row and manifest say
    `binary`, with both files in the store. Root still builds the component
    from its `build` commands."""

    run: FakeRun
    host: Host
    spool_root: Path

    def release(self, at_approval: Callable[[], None] | None = None) -> dict[str, object]:
        """Run the release. `at_approval` runs at the tap, between step 2
        and step 8. The answer is the ledger entry."""
        grant = grant_transport()

        def transport(action_id: str, gate: str, summary: Summary, wait_s: float) -> Decision:
            if at_approval is not None:
                at_approval()

            return grant(action_id, gate, summary, wait_s)

        wiring = Wiring(
            host=self.host,
            transport=transport,
            readers=fake_readers(latest={COMPONENT: NEW_VERSION}),
            api=green_api(REPO, TAG, SHA_OF[COMPONENT]),
        )
        write_request(self.spool_root, REQUEST_ID, request_body({COMPONENT: NEW_VERSION}))
        spool = Spool(str(self.spool_root), this_uid())
        try:
            handle(spool, f"{REQUEST_ID}.json", wiring, Counter())
        finally:
            spool.close()

        done = self.spool_root / DONE_DIR / f"{REQUEST_ID}.json"
        loaded = json.loads(done.read_text(encoding="utf-8"))
        assert isinstance(loaded, dict)

        return cast("dict[str, object]", loaded)


@pytest.fixture
def bench(tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch) -> Bench:
    use_binary_catalog(monkeypatch)
    _stock(home)
    components = tmp_path / "components"
    run = FakeRun(answers=git_host_answers())
    host = fake_host(tmp_path, run)
    stamp_tree(components, COMPONENT, LIVE_VERSION)
    (tmp_path / "system-units" / UNIT).write_text(
        unit_text(str(components / COMPONENT / "bin" / COMPONENT)), encoding="utf-8"
    )
    write_program(components / COMPONENT / "bin" / COMPONENT)
    run.hooks[CARGO] = lambda _: staged_binary_tree(components / f"{COMPONENT}.new")

    def write_tree(destination: Path, name: str) -> None:
        del name
        for row in CATALOG:
            text = manifest_text(row.name).replace("/opt/components", str(components))
            if row.name == COMPONENT:
                text = binary_manifest_text(components, UNIT)

            sub = destination if row.path == "." else destination / row.path
            sub.mkdir(parents=True, exist_ok=True)
            (sub / "component.yaml").write_text(text, encoding="utf-8")

    run.dynamic = GitFake(write_tree)

    return Bench(run=run, host=host, spool_root=make_spool_dirs(tmp_path))


def _row(ledger: dict[str, object]) -> dict[str, object]:
    manifest = cast("dict[str, object]", ledger["manifest"])
    rows = cast("list[dict[str, object]]", manifest["components"])

    return next(one for one in rows if one["name"] == COMPONENT)


def _copy_of(tmp_path: Path) -> Path:
    return tmp_path / "work" / REQUEST_ID / COPY_DIR / COMPONENT / NAMES.archive


def test_the_resolved_manifest_holds_the_digest_of_a_binary_component(bench: Bench) -> None:
    ledger = bench.release()

    assert ledger["status"] == "succeeded", ledger.get("reason")
    assert _row(ledger)["artifact_digest"] == _sha256(ARCHIVE)


def test_each_other_component_keeps_null(bench: Bench) -> None:
    manifest = cast("dict[str, object]", bench.release()["manifest"])
    rows = cast("list[dict[str, object]]", manifest["components"])

    assert [one["name"] for one in rows if one["artifact_digest"] is not None] == [COMPONENT]


def test_the_release_still_builds_the_component(bench: Bench) -> None:
    """This change copies and hashes. The stage still runs the build."""
    bench.release()

    assert bench.run.ran(CARGO)


def test_the_release_removes_the_copy(bench: Bench, tmp_path: Path) -> None:
    bench.release()

    assert not (tmp_path / "work" / REQUEST_ID).exists()


def test_a_copy_removed_after_step_2_is_refused_with_drift(bench: Bench, tmp_path: Path) -> None:
    """The copy is absent at step 8, so the digest is null and the hash of
    the resolved manifest is not the approved hash. No component is staged."""

    def remove() -> None:
        _copy_of(tmp_path).unlink()

    ledger = bench.release(remove)

    assert ledger["status"] == "refused"
    assert ledger["refused_check"] == "drift"
    assert not bench.run.ran(CARGO)
    assert not (tmp_path / "components" / f"{COMPONENT}.new").exists()


def test_one_changed_byte_of_the_copy_is_refused_with_drift(bench: Bench, tmp_path: Path) -> None:
    def change() -> None:
        copy = _copy_of(tmp_path)
        changed = bytearray(copy.read_bytes())
        changed[-1] ^= 1
        copy.write_bytes(bytes(changed))

    ledger = bench.release(change)

    assert ledger["refused_check"] == "drift"
    assert not bench.run.ran(CARGO)


def test_one_changed_byte_of_the_copy_changes_the_hash(
    home: Path, release: Release, tmp_path: Path
) -> None:
    _stock(home)
    manifests = {COMPONENT: _manifest(tmp_path / "components", Kind.BINARY)}
    wanted = release.request.wanted()

    def hash_of(state: ReleaseState) -> str:
        document = build_document(
            resolve(manifests, state, wanted), state, REQUEST_ID, "human", 1.0
        )

        return str(document["manifest_sha256"])

    first = hash_of(release._copy_artifacts(_state(), manifests))
    copy = _copy_of(tmp_path)
    changed = bytearray(copy.read_bytes())
    changed[0] ^= 1
    copy.write_bytes(bytes(changed))

    assert hash_of(release._copy_artifacts(_state(), manifests)) != first
