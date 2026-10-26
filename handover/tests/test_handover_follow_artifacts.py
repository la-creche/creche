"""`handover follow` and the artifact of a binary member (`follow/`, rule 7).

Root installs a binary component from the archive in the store of the
operator. A member that ships an archive and has no two files there is not
ready. The follower files nothing for `ARTIFACT_WAIT_S` after the first run
that read its tag, and after that time the member leaves the set of the run.

A refusal of check P6 whose verifier gave no answer is asked again, as a
tap nobody gave is (rule 5). Each other P6 refusal holds the set.

Each test runs the real builder over a real clone under `tmp_path`, as
`test_handover_follow.py` does. `cli.refresh` stands in for the git fetch,
`corpus.artifacts.run_capped` for curl, and `cli.wall_time` is the clock of
the test. The catalog holds no binary component, so a test that needs one
swaps one row for a binary copy of itself (`handover_bin_fixtures.py`).
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

import pytest
from handover.artifacts import ArtifactRefusal, asset_names
from handover.catalog import CATALOG_BY_NAME, Repo
from handover.corpus import CorpusReport
from handover.corpus import artifacts as fetching
from handover.corpus.artifacts import CHILD_MAX_S, PRUNE_ROOT_REFUSAL, prune_artifacts
from handover.errors import RefusalCode
from handover.executor.approval import Verdict, not_granted
from handover.executor.ledger import Entry, Outcome
from handover.executor.live_state import write_stamp
from handover.executor.request import parse_request
from handover.follow import (
    AGAIN_S,
    ARTIFACT_WAIT_S,
    MAX_ASKS,
    MAX_MARKER_BYTES,
    REQUESTED_BY,
    RUN_DEADLINE_S,
    Answer,
    Marker,
    Wait,
    gate_artifacts,
    read_answer,
    read_marker,
    write_marker,
)
from handover.site import SITE_FILE_ENV
from handover_bin_fixtures import BINARY_NAME, use_binary_catalog

from handover import cli, notices, strictjson

BOARD = BINARY_NAME
CARE = "caregiver"
OLDER = "0.0.9"
LIVE = "0.1.0"
NEXT = "0.1.1"
LATER = "0.1.2"

START = 1_760_000_000.0

#: The lines of the wait rule for `noticeboard` at `NEXT`.
WAITING = f"waiting: the artifact of {BOARD} {NEXT} is not in the store"
OMITTED = f"omitted: {BOARD} {NEXT} has no artifact after {ARTIFACT_WAIT_S} s"

_UNIT = Path(__file__).resolve().parents[2] / "systemd" / "creche-follow.service"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
        env={
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "HOME": str(repo),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
        },
    )


@dataclass
class _Releases:
    """The Releases of the code host, as curl reads them: the asset names of
    each published tag, and a 404 for each other asset."""

    published: set[str] = field(default_factory=set[str])
    calls: list[list[str]] = field(default_factory=list[list[str]])

    def publish(self, name: str, version: str) -> None:
        names = asset_names(name)
        self.published |= {
            f"{name}-v{version}/{names.archive}",
            f"{name}-v{version}/{names.bundle}",
        }

    def __call__(self, argv: list[str], _timeout_s: float, _max_file_bytes: int) -> int:
        self.calls.append(list(argv))
        tag, asset = argv[-1].rsplit("/", 2)[-2:]
        if f"{tag}/{asset}" not in self.published:
            return 22

        Path(argv[argv.index("--output") + 1]).write_bytes(b"bytes of " + asset.encode())

        return 0


@dataclass
class _Host:
    """One host under `tmp_path`: a corpus, install trees, a spool, a marker,
    a store, the Releases and a clock."""

    root: Path
    releases: _Releases
    now: float = START

    @property
    def store(self) -> Path:
        return fetching.artifact_store()

    @property
    def requests(self) -> Path:
        return self.root / "spool" / "requests"

    @property
    def marker(self) -> Path:
        return self.root / "state" / "follow.json"

    def options(self) -> list[str]:
        return [
            "--corpus",
            str(self.root / "corpus"),
            "--install-root",
            str(self.root / "components"),
            "--spool",
            str(self.requests),
            "--marker",
            str(self.marker),
            "--notices",
            str(self.root / "state" / "notices" / "follow.json"),
        ]

    def tag(self, name: str, version: str) -> None:
        _git(self.root / "corpus" / str(CATALOG_BY_NAME[name].repo), "tag", f"{name}-v{version}")

    def install(self, name: str, version: str) -> None:
        tree = self.root / "components" / name
        tree.mkdir(parents=True)
        write_stamp(tree, version)
        self.tag(name, version)

    def follow(self, *names: str, more: tuple[str, ...] = ()) -> int:
        return cli.main(["follow", *names, *self.options(), *more])

    def filed(self) -> list[dict[str, str]]:
        """The components of each filed request, at their versions."""
        return [
            parse_request(path.read_bytes(), path.stem).wanted()
            for path in sorted(self.requests.glob("*.json"))
        ]

    def tag_dir(self, name: str, version: str) -> Path:
        return self.store / str(CATALOG_BY_NAME[name].repo) / f"{name}-v{version}"

    def refuse(self, check: RefusalCode, reason: str) -> None:
        """Root's ledger entry for the newest request: a refusal with
        `check` and `reason`. Root also takes the request out of
        `requests/`."""
        newest = sorted(self.requests.glob("*.json"))[-1]
        _write_entry(self.root / "spool" / "done", newest.stem, check, reason)
        newest.rename(self.root / "spool" / "taken" / newest.name)

    def taken(self) -> int:
        return len(list((self.root / "spool" / "taken").glob("*.json")))


def _write_entry(done: Path, request_id: str, check: object, reason: object) -> None:
    """One refused entry, as `executor/ledger.py` writes it. `check` and
    `reason` can be any JSON value."""
    entry = Entry(id=request_id, kind="release", requested_by=REQUESTED_BY)
    entry.status = Outcome.REFUSED
    body = {**entry.as_dict(), "refused_check": check, "reason": reason}
    (done / f"{request_id}.json").write_bytes(strictjson.dumps(body).encode())


def _make_host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Host:
    monkeypatch.setattr(notices, "_settings", None)
    home = tmp_path / "home"
    home.mkdir()
    site = tmp_path / "site.env"
    site.write_text(
        "AGENT_GITHUB_OWNER=example-owner\nAGENT_OPERATOR_USER=operator\n"
        f"AGENT_OPERATOR_HOME={home}\nAGENT_LAN_ADDRESS=192.0.2.10\n",
        encoding="utf-8",
    )
    monkeypatch.setenv(SITE_FILE_ENV, str(site))
    made = _Host(tmp_path, _Releases())
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    corpus.chmod(0o755)
    for repo in (Repo.AGENT_CONTROL, Repo.AGENT_MCP):
        clone = corpus / str(repo)
        clone.mkdir()
        clone.chmod(0o755)
        (clone / "README.md").write_text(f"{repo}\n", encoding="utf-8")
        _git(clone, "init", "-q", "-b", "main")
        _git(clone, "add", "-A")
        _git(clone, "commit", "-q", "-m", "first")

    for name in ("requests", "done", "taken"):
        (tmp_path / "spool" / name).mkdir(parents=True)

    def refresh(repos: tuple[str, ...], *_args: object, **_kwargs: object) -> CorpusReport:
        return CorpusReport(True, [f"fetched {name}" for name in repos])

    monkeypatch.setattr(cli, "refresh", refresh)
    monkeypatch.setattr(cli, "wall_time", lambda: made.now)
    # The run starts at 0 on the monotonic clock, and that clock stands.
    monkeypatch.setattr(cli, "PROCESS_START", 0.0)
    monkeypatch.setattr(fetching, "monotonic", lambda: 0.0)
    monkeypatch.setattr(fetching, "run_capped", made.releases)

    return made


@pytest.fixture
def host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Host:
    """A host on which `noticeboard` is a binary component."""
    use_binary_catalog(monkeypatch)

    return _make_host(tmp_path, monkeypatch)


@pytest.fixture
def today(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Host:
    """A host with the catalog of today: no binary component."""
    return _make_host(tmp_path, monkeypatch)


def _out(capsys: pytest.CaptureFixture[str]) -> list[str]:
    return capsys.readouterr().out.splitlines()


# -- the wait ------------------------------------------------------------------


def test_a_member_with_no_artifact_files_nothing_and_waits(
    host: _Host, capsys: pytest.CaptureFixture[str]
) -> None:
    host.install(BOARD, LIVE)
    host.tag(BOARD, NEXT)

    for later in (0.0, 120.0, ARTIFACT_WAIT_S - 1):
        host.now = START + later
        assert host.follow(BOARD) == cli.EXIT_OK
        assert _out(capsys) == [WAITING]

    assert host.filed() == []
    assert read_marker(host.marker).waits == {BOARD: Wait(NEXT, START)}
    # Each run fetches again: the archive answers 404, so no bundle is asked.
    assert len(host.releases.calls) == 3


def test_after_the_wait_the_member_leaves_the_set_of_the_run(
    host: _Host, capsys: pytest.CaptureFixture[str]
) -> None:
    """One network fault costs no refusal and stops no other member."""
    host.install(BOARD, LIVE)
    host.install(CARE, LIVE)
    host.tag(BOARD, NEXT)
    host.tag(CARE, NEXT)
    host.follow(BOARD, CARE)
    capsys.readouterr()

    host.now = START + ARTIFACT_WAIT_S
    host.follow(BOARD, CARE)
    assert _out(capsys) == [
        OMITTED,
        f"seen: {CARE} {NEXT}. The next run files it if it reads the same set",
    ]

    host.now += 120
    host.follow(BOARD, CARE)
    assert host.filed() == [{CARE: NEXT}]
    assert _out(capsys)[0] == OMITTED

    host.now += 120
    host.follow(BOARD, CARE)
    assert host.filed() == [{CARE: NEXT}]
    assert read_marker(host.marker).waits == {BOARD: Wait(NEXT, START)}


def test_an_artifact_that_arrives_later_makes_a_new_set(
    host: _Host, capsys: pytest.CaptureFixture[str]
) -> None:
    host.install(BOARD, LIVE)
    host.install(CARE, LIVE)
    host.tag(BOARD, NEXT)
    host.tag(CARE, NEXT)
    for later in (0.0, ARTIFACT_WAIT_S, ARTIFACT_WAIT_S + 120):
        host.now = START + later
        host.follow(BOARD, CARE)

    host.releases.publish(BOARD, NEXT)
    host.now += 120
    host.follow(BOARD, CARE)
    host.now += 120
    host.follow(BOARD, CARE)

    assert host.filed() == [{CARE: NEXT}, {BOARD: NEXT, CARE: NEXT}]
    assert read_marker(host.marker).waits == {}
    assert sorted(path.name for path in host.tag_dir(BOARD, NEXT).iterdir()) == sorted(
        (asset_names(BOARD).archive, asset_names(BOARD).bundle)
    )
    assert "filed:" in _out(capsys)[-1]


def test_an_artifact_that_arrives_in_time_files_the_whole_set(host: _Host) -> None:
    host.install(BOARD, LIVE)
    host.tag(BOARD, NEXT)
    host.follow(BOARD)

    host.releases.publish(BOARD, NEXT)
    host.now += 120
    host.follow(BOARD)
    host.now += 120
    host.follow(BOARD)

    assert host.filed() == [{BOARD: NEXT}]


def test_a_newer_tag_starts_the_wait_again(host: _Host, capsys: pytest.CaptureFixture[str]) -> None:
    host.install(BOARD, LIVE)
    host.tag(BOARD, NEXT)
    host.follow(BOARD)
    host.tag(BOARD, LATER)

    host.now = START + ARTIFACT_WAIT_S + 100
    host.follow(BOARD)

    assert read_marker(host.marker).waits == {BOARD: Wait(LATER, host.now)}
    assert _out(capsys)[-1] == f"waiting: the artifact of {BOARD} {LATER} is not in the store"


def test_a_dry_run_writes_no_marker_and_removes_nothing(host: _Host) -> None:
    host.install(BOARD, LIVE)
    host.tag(BOARD, NEXT)
    old = host.tag_dir(BOARD, OLDER)
    old.mkdir(parents=True)

    assert host.follow(BOARD, more=("--dry-run",)) == cli.EXIT_OK

    assert not host.marker.exists()
    assert old.is_dir()


def test_a_run_gets_the_time_that_is_left_of_its_deadline(host: _Host) -> None:
    """The run started at 0, so its child has `RUN_DEADLINE_S` at most."""
    host.install(BOARD, LIVE)
    host.tag(BOARD, NEXT)

    host.follow(BOARD)

    argv = host.releases.calls[0]
    assert argv[argv.index("--max-time") + 1] == str(min(RUN_DEADLINE_S, CHILD_MAX_S))


def test_the_deadline_of_a_run_is_below_the_time_limit_of_the_unit() -> None:
    """The unit stops a run that is still active at its limit. The fetch
    must end before that, so a run ends with its own line."""
    lines = _UNIT.read_text(encoding="utf-8").splitlines()
    found = [line.partition("=")[2] for line in lines if line.startswith("TimeoutStartSec=")]

    assert len(found) == 1
    assert int(found[0]) > RUN_DEADLINE_S


# -- a set with no binary member ------------------------------------------------


def test_a_set_with_no_binary_member_gets_the_decision_and_the_lines_of_today(
    today: _Host, capsys: pytest.CaptureFixture[str]
) -> None:
    """No fetch, no store and no new line. The marker has the text of a
    follower with no wait rule."""
    today.install(BOARD, LIVE)
    today.tag(BOARD, NEXT)
    capsys.readouterr()

    assert today.follow(BOARD) == cli.EXIT_OK
    first = capsys.readouterr()
    assert today.follow(BOARD) == cli.EXIT_OK
    second = capsys.readouterr()

    filed = next(today.requests.glob("*.json"))
    assert first.out.splitlines() == [
        f"seen: {BOARD} {NEXT}. The next run files it if it reads the same set"
    ]
    assert second.out.splitlines() == [f"filed: {BOARD} {NEXT} as {filed}"]
    assert first.err == second.err == ""
    assert today.releases.calls == []
    assert not (today.root / "home" / ".local").exists()
    written = strictjson.loads(today.marker.read_bytes(), cap=MAX_MARKER_BYTES)
    assert isinstance(written, dict)
    assert "waits" not in cast("dict[str, object]", written)


def test_a_run_with_no_binary_member_reads_no_store(
    today: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No fetch, no prune and no read of the site file for the store."""

    def no_store() -> Path:
        raise AssertionError("the run asked for the path of the store")

    monkeypatch.setattr(cli, "artifact_store", no_store)
    today.install(BOARD, LIVE)
    today.tag(BOARD, NEXT)

    assert today.follow(BOARD) == cli.EXIT_OK
    assert today.follow(BOARD) == cli.EXIT_OK
    assert today.filed() == [{BOARD: NEXT}]


# -- the marker -----------------------------------------------------------------

ASKED = {BOARD: NEXT, CARE: NEXT}
ID = "01K5J8M2Q7V3X9R4T6N0B8C2DE"


def _installed_read(raw: bytes) -> tuple[object, ...]:
    """What the reader of an installed follower with no wait rule takes from a
    marker: five keys by their names. It reads no other key."""
    body = strictjson.loads(raw, cap=MAX_MARKER_BYTES)
    assert isinstance(body, dict)
    found = cast("dict[str, object]", body)

    return tuple(found.get(key) for key in ("seen", "asked", "id", "asks", "at"))


def test_a_marker_with_no_waits_key_reads_as_a_marker_with_no_wait(tmp_path: Path) -> None:
    """The marker of an installed follower, read by this version."""
    path = tmp_path / "follow.json"
    text = {"seen": ASKED, "asked": ASKED, "id": ID, "asks": 2, "at": START}
    path.write_bytes(strictjson.dumps(text, sort_keys=True).encode())

    assert read_marker(path) == Marker(
        seen=ASKED, asked=ASKED, request_id=ID, asks=2, asked_at=START, waits={}
    )


def test_a_marker_with_waits_reads_the_same_for_an_installed_follower(tmp_path: Path) -> None:
    """The marker of this version, read by an installed follower."""
    path = tmp_path / "follow.json"
    waits = {BOARD: Wait(NEXT, START)}
    write_marker(path, Marker(ASKED, ASKED, ID, 2, START, waits))

    assert _installed_read(path.read_bytes()) == (ASKED, ASKED, ID, 2, START)
    assert read_marker(path).waits == waits


def test_a_marker_with_eight_waits_is_below_the_size_limit(tmp_path: Path) -> None:
    """The longest names and versions of eight members, in each set."""
    names = [f"{letter}{'-' * 29}x" for letter in "abcdefgh"]
    version = "999999.999999.999999"
    moving = dict.fromkeys(names, version)
    waits = {name: Wait(version, START + 0.123456) for name in names}
    path = tmp_path / "follow.json"

    write_marker(path, Marker(moving, moving, ID, 4, START + 0.5, waits))

    assert path.stat().st_size < MAX_MARKER_BYTES
    assert read_marker(path).waits == waits


@pytest.mark.parametrize(
    "value",
    [
        None,
        [],
        "x",
        {BOARD: NEXT},
        {BOARD: {"version": NEXT}},
        {BOARD: {"version": 1, "since": START}},
        {BOARD: {"version": NEXT, "since": True}},
        {BOARD: {"version": NEXT, "since": "1"}},
        {BOARD: {"version": NEXT, "since": START, "more": 1}},
    ],
    ids=["null", "list", "text", "flat", "no-time", "number", "bool", "text-time", "more"],
)
def test_a_waits_value_of_another_shape_reads_as_an_empty_marker(
    tmp_path: Path, value: object
) -> None:
    path = tmp_path / "follow.json"
    text = {"seen": ASKED, "asked": ASKED, "id": ID, "asks": 1, "at": START, "waits": value}
    path.write_bytes(strictjson.dumps(text).encode())

    assert read_marker(path) == Marker()


# -- the rule, with no host at all -------------------------------------------------


def test_a_ready_member_does_not_wait() -> None:
    gate = gate_artifacts({BOARD: NEXT, CARE: NEXT}, frozenset({BOARD, CARE}), {}, START)

    assert (gate.moving, gate.waits, gate.waiting, gate.omitted) == (ASKED, {}, {}, {})
    assert gate.lines() == ()


@pytest.mark.parametrize(
    ("waits", "now", "waiting", "since"),
    [
        ({}, START, True, START),
        ({BOARD: Wait(NEXT, START)}, START + ARTIFACT_WAIT_S - 1, True, START),
        ({BOARD: Wait(NEXT, START)}, START + ARTIFACT_WAIT_S, False, START),
        ({BOARD: Wait(LIVE, START)}, START + ARTIFACT_WAIT_S, True, START + ARTIFACT_WAIT_S),
        ({BOARD: Wait(NEXT, START + 10)}, START, True, START),
    ],
    ids=["first", "in-time", "over", "other-version", "clock-went-back"],
)
def test_the_wait_rule(waits: dict[str, Wait], now: float, waiting: bool, since: float) -> None:
    gate = gate_artifacts({BOARD: NEXT, CARE: NEXT}, frozenset({CARE}), waits, now)

    assert gate.waits == {BOARD: Wait(NEXT, since)}
    assert (gate.waiting, gate.omitted) == (({BOARD: NEXT}, {}) if waiting else ({}, {BOARD: NEXT}))
    assert gate.moving == (ASKED if waiting else {CARE: NEXT})
    assert gate.lines() == ((WAITING,) if waiting else (OMITTED,))


def test_a_wait_of_a_member_that_no_longer_moves_is_dropped() -> None:
    gate = gate_artifacts({CARE: NEXT}, frozenset({CARE}), {BOARD: Wait(NEXT, START)}, START)

    assert gate.waits == {}


# -- the prune ----------------------------------------------------------------------


def _write_tag(directory: Path) -> None:
    directory.mkdir(parents=True)
    names = asset_names(BOARD)
    for name in (names.archive, names.bundle, f".{names.archive}.part"):
        (directory / name).write_bytes(b"x")


def test_the_follower_removes_each_tag_below_the_live_version(
    host: _Host, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Below the live version a tag is never released again. The live tag
    and a newer one stay, and so does each name that is no such tag."""
    host.install(BOARD, LIVE)
    for version in (OLDER, LIVE, NEXT):
        _write_tag(host.tag_dir(BOARD, version))

    control = host.store / str(Repo.AGENT_CONTROL)
    (control / "notes").mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "kept").write_bytes(b"x")
    (control / f"{BOARD}-v0.0.8").symlink_to(elsewhere)
    (host.store / str(Repo.AGENT_MCP) / f"{BOARD}-v0.0.1").mkdir(parents=True)
    capsys.readouterr()

    host.follow(BOARD)

    assert sorted(path.name for path in control.iterdir()) == sorted(
        [f"{BOARD}-v0.0.8", f"{BOARD}-v{LIVE}", f"{BOARD}-v{NEXT}", "notes"]
    )
    assert (elsewhere / "kept").exists()
    assert (host.store / str(Repo.AGENT_MCP) / f"{BOARD}-v0.0.1").is_dir()
    note = f"note: removed the artifacts of {BOARD}-v{OLDER} from the store"
    assert capsys.readouterr().err.splitlines() == [note]


def test_a_tag_directory_that_holds_a_directory_stays(host: _Host) -> None:
    old = host.tag_dir(BOARD, OLDER)
    _write_tag(old)
    (old / "inside").mkdir()

    lines = prune_artifacts(host.store, {BOARD: LIVE})

    assert lines == (f"{BOARD}-v{OLDER}: the store directory could not be removed",)
    assert sorted(path.name for path in old.iterdir()) == ["inside"]


def test_a_store_that_is_a_link_is_not_read(host: _Host, tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    _write_tag(elsewhere / str(Repo.AGENT_CONTROL) / f"{BOARD}-v{OLDER}")
    host.store.parent.mkdir(parents=True)
    host.store.symlink_to(elsewhere)

    assert prune_artifacts(host.store, {BOARD: LIVE}) == ()
    assert (elsewhere / str(Repo.AGENT_CONTROL) / f"{BOARD}-v{OLDER}").is_dir()


def test_the_prune_as_root_removes_nothing(host: _Host, monkeypatch: pytest.MonkeyPatch) -> None:
    old = host.tag_dir(BOARD, OLDER)
    _write_tag(old)
    monkeypatch.setattr(fetching.os, "geteuid", lambda: 0)

    assert prune_artifacts(host.store, {BOARD: LIVE}) == (PRUNE_ROOT_REFUSAL,)
    assert old.is_dir()


def test_the_store_directories_are_the_operators_alone(host: _Host) -> None:
    host.install(BOARD, LIVE)
    host.tag(BOARD, NEXT)
    host.releases.publish(BOARD, NEXT)

    host.follow(BOARD)

    for path in (host.store, host.store / str(Repo.AGENT_CONTROL), host.tag_dir(BOARD, NEXT)):
        assert os.lstat(path).st_mode & 0o777 == 0o700, path


# -- the answers of check P6 ----------------------------------------------------------


def test_a_p6_refusal_with_no_verifier_answer_is_asked_again(host: _Host) -> None:
    """The verifier gave root no answer about the signature, so nobody
    answered. The set is asked again after `AGAIN_S`, as a tap nobody
    gave is."""
    host.install(BOARD, LIVE)
    host.tag(BOARD, NEXT)
    host.releases.publish(BOARD, NEXT)
    host.follow(BOARD)
    host.follow(BOARD)
    host.refuse(RefusalCode.P6, ArtifactRefusal.VERIFIER_UNAVAILABLE)

    host.now += AGAIN_S - 1
    host.follow(BOARD)
    assert host.filed() == []

    host.now += 1
    host.follow(BOARD)
    assert host.filed() == [{BOARD: NEXT}]
    assert host.taken() == 1


def test_a_p6_refusal_with_no_verifier_answer_is_asked_a_fixed_number_of_times(
    host: _Host,
) -> None:
    host.install(BOARD, LIVE)
    host.tag(BOARD, NEXT)
    host.releases.publish(BOARD, NEXT)
    host.follow(BOARD)

    for _ in range(MAX_ASKS + 3):
        host.follow(BOARD)
        if host.filed():
            host.refuse(RefusalCode.P6, ArtifactRefusal.VERIFIER_UNAVAILABLE)

        host.now += AGAIN_S

    assert host.taken() == MAX_ASKS
    assert host.filed() == []


@pytest.mark.parametrize(
    "reason",
    [
        ArtifactRefusal.NO_ARTIFACT,
        ArtifactRefusal.SIGNATURE_REFUSED,
        ArtifactRefusal.SHAPE_REFUSED,
        "the verifier was not available.",
    ],
    ids=["no-artifact", "signature", "shape", "outside-the-enum"],
)
def test_each_other_p6_refusal_holds_the_set(host: _Host, reason: str) -> None:
    """Root pushed the outcome already. Only a newer tag asks again."""
    host.install(BOARD, LIVE)
    host.tag(BOARD, NEXT)
    host.releases.publish(BOARD, NEXT)
    host.follow(BOARD)
    host.follow(BOARD)
    host.refuse(RefusalCode.P6, reason)

    host.now += AGAIN_S * 10
    assert host.follow(BOARD) == cli.EXIT_OK

    assert host.filed() == []
    assert host.taken() == 1


def test_check_p6_with_no_verifier_answer_reads_as_unanswered(tmp_path: Path) -> None:
    _write_entry(tmp_path, ID, "P6", "the verifier was not available")

    assert read_answer(tmp_path, ID) is Answer.UNANSWERED


@pytest.mark.parametrize(
    ("check", "reason"),
    [
        ("P6", "no artifact in the store"),
        ("P6", "the artifact signature was refused"),
        ("P6", "the archive shape was refused"),
        ("P6", "The verifier was not available"),
        ("P6", "the verifier was not available "),
        ("P6", not_granted(Verdict.TIMEOUT)),
        ("P6", ""),
        ("P6", None),
        ("P6", ["the verifier was not available"]),
        ("P5", "the verifier was not available"),
        ("approval", "the verifier was not available"),
        (["P6"], "the verifier was not available"),
        (None, "the verifier was not available"),
        ("approval", [not_granted(Verdict.TIMEOUT)]),
        ("approval", {"reason": not_granted(Verdict.TIMEOUT)}),
    ],
    ids=[
        "no-artifact",
        "signature",
        "shape",
        "upper-case",
        "final-space",
        "reason-of-the-gate",
        "empty",
        "null",
        "list",
        "check-p5",
        "check-approval",
        "check-list",
        "check-null",
        "approval-list",
        "approval-object",
    ],
)
def test_each_other_answer_of_check_p6_is_final(
    tmp_path: Path, check: object, reason: object
) -> None:
    """A reason outside the enum, the reason under another check, or a check
    or a reason that is no text, holds the set. This applies to each check."""
    _write_entry(tmp_path, ID, check, reason)

    assert read_answer(tmp_path, ID) is Answer.FINAL
