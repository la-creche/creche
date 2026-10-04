"""`handover follow`: a new tag files the release request by itself.

The operator used to type `handover request <name> --wait` after every
merge, and only then did the phone ask. `follow` is that command on a
timer. It files, and it approves nothing: the tap stays the operator's.

Every test runs the real builder over a real clone under `tmp_path`.
`cli.refresh` stands in for GitHub and `cli.wall_time` is the test's clock.
A test lands a tag with `git tag`, the way a fetch would bring one.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from handover.catalog import CATALOG_BY_NAME, RETIRING, Repo, releasable_names
from handover.corpus import CorpusReport
from handover.executor.approval import Verdict, not_granted
from handover.executor.ledger import Entry, Outcome
from handover.executor.live_state import write_stamp
from handover.executor.request import parse_request
from handover.follow import AGAIN_S, MAX_ASKS, REQUESTED_BY, Answer, Marker, Step, decide

from handover import cli

BOARD = "noticeboard"
PEP = "chaperone"
SERVERS = "mcp-servers"
LIVE = "0.1.0"
NEXT = "0.1.1"
LATER = "0.1.2"

START = 1_760_000_000.0


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
class _Host:
    """One host under `tmp_path`: a corpus, install trees, a spool, a marker
    and a clock."""

    root: Path
    now: float = START
    fetches: list[tuple[str, ...]] = field(default_factory=list[tuple[str, ...]])

    @property
    def clone(self) -> Path:
        return self.root / "corpus" / str(Repo.AGENT_CONTROL)

    def clone_of(self, name: str) -> Path:
        return self.root / "corpus" / str(CATALOG_BY_NAME[name].repo)

    @property
    def requests(self) -> Path:
        return self.root / "spool" / "requests"

    @property
    def done(self) -> Path:
        return self.root / "spool" / "done"

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
        ]

    def tag(self, name: str, version: str) -> None:
        _git(self.clone_of(name), "tag", f"{name}-v{version}")

    def install(self, name: str, version: str | None) -> None:
        """A live tree. `version` None is a tree no release has stamped."""
        tree = self.root / "components" / name
        tree.mkdir(parents=True)
        if version is None:
            return

        write_stamp(tree, version)
        self.tag(name, version)

    def follow(self, *names: str, more: tuple[str, ...] = ()) -> int:
        return cli.main(["follow", *names, *self.options(), *more])

    def filed(self) -> list[dict[str, object]]:
        return [
            parse_request(path.read_bytes(), path.stem).as_dict()
            for path in sorted(self.requests.glob("*.json"))
        ]

    def answer(self, verdict: Verdict) -> None:
        """Root's ledger entry for the newest request: the gate did not grant.
        Root also takes the request out of `requests/`."""
        newest = sorted(self.requests.glob("*.json"))[-1]
        entry = Entry(id=newest.stem, kind="release", requested_by=REQUESTED_BY)
        entry.status = Outcome.REFUSED
        entry.refused_check = "approval"
        entry.reason = not_granted(verdict)
        (self.done / newest.name).write_text(json.dumps(entry.as_dict()), encoding="utf-8")
        newest.rename(self.root / "spool" / "taken" / newest.name)

    def taken(self) -> int:
        return len(list((self.root / "spool" / "taken").glob("*.json")))


@pytest.fixture
def host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Host:
    made = _Host(tmp_path)
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
        made.fetches.append(tuple(repos))

        return CorpusReport(True, [f"fetched {name}" for name in repos])

    monkeypatch.setattr(cli, "refresh", refresh)
    monkeypatch.setattr(cli, "wall_time", lambda: made.now)

    return made


def test_a_new_tag_is_seen_once_and_filed_on_the_next_run(host: _Host) -> None:
    """Two runs, on purpose. CI makes a tag and then its Release, and one
    merge can tag several components. A request filed at the first sight of
    a tag can name half a set, or a tag root finds no Release for."""
    host.install(BOARD, LIVE)
    host.tag(BOARD, NEXT)

    assert host.follow(BOARD) == cli.EXIT_OK
    assert host.filed() == []

    assert host.follow(BOARD) == cli.EXIT_OK
    filed = host.filed()
    assert len(filed) == 1
    assert filed[0]["components"] == {BOARD: NEXT}
    assert filed[0]["requested_by"] == REQUESTED_BY
    assert filed[0]["kind"] == "release"


def test_every_run_fetches_the_repository_it_follows(host: _Host) -> None:
    host.install(BOARD, LIVE)

    host.follow(BOARD)

    assert host.fetches == [(str(Repo.AGENT_CONTROL),)]


def test_no_new_tag_files_nothing_and_writes_no_marker(host: _Host) -> None:
    host.install(BOARD, LIVE)

    assert host.follow(BOARD) == cli.EXIT_OK
    assert host.follow(BOARD) == cli.EXIT_OK

    assert host.filed() == []
    assert not host.marker.exists()


def test_a_filed_set_is_not_filed_again(host: _Host) -> None:
    """The request waits in `requests/`, or root holds the tap open. The
    timer keeps firing, and each run must leave the phone alone."""
    host.install(BOARD, LIVE)
    host.tag(BOARD, NEXT)

    for _ in range(5):
        host.now += 120
        assert host.follow(BOARD) == cli.EXIT_OK

    assert len(host.filed()) == 1


def test_two_new_tags_file_as_one_set(host: _Host) -> None:
    """One tap for any number of components (`stage7-releases.md` §2.7)."""
    host.install(BOARD, LIVE)
    host.install(PEP, LIVE)
    host.tag(BOARD, NEXT)
    host.tag(PEP, NEXT)

    host.follow(BOARD, PEP)
    host.follow(BOARD, PEP)

    assert [item["components"] for item in host.filed()] == [{BOARD: NEXT, PEP: NEXT}]


def test_components_of_two_repositories_file_as_one_set(host: _Host) -> None:
    """`mcp-servers` is tagged in the MCP repository. One run fetches both
    clones, and the request names both components."""
    host.install(BOARD, LIVE)
    host.install(SERVERS, LIVE)
    host.tag(BOARD, NEXT)
    host.tag(SERVERS, NEXT)

    host.follow(BOARD, SERVERS)
    host.follow(BOARD, SERVERS)

    assert host.fetches[0] == (str(Repo.AGENT_CONTROL), str(Repo.AGENT_MCP))
    assert [item["components"] for item in host.filed()] == [{BOARD: NEXT, SERVERS: NEXT}]


def test_a_set_that_grows_between_two_runs_waits_one_more_run(host: _Host) -> None:
    """The second tag of one merge lands after the first run saw the first."""
    host.install(BOARD, LIVE)
    host.install(PEP, LIVE)
    host.tag(BOARD, NEXT)
    host.follow(BOARD, PEP)

    host.tag(PEP, NEXT)
    host.follow(BOARD, PEP)
    assert host.filed() == []

    host.follow(BOARD, PEP)
    assert [item["components"] for item in host.filed()] == [{BOARD: NEXT, PEP: NEXT}]


def test_a_denied_set_stays_quiet(host: _Host) -> None:
    """The operator said no. Only a newer tag asks again."""
    host.install(BOARD, LIVE)
    host.tag(BOARD, NEXT)
    host.follow(BOARD)
    host.follow(BOARD)
    host.answer(Verdict.DENIED)

    host.now += AGAIN_S * 10
    assert host.follow(BOARD) == cli.EXIT_OK

    assert host.filed() == []
    assert host.taken() == 1


@pytest.mark.parametrize("verdict", [Verdict.TIMEOUT, Verdict.EXPIRED])
def test_a_tap_nobody_gave_is_asked_again_after_the_hold(host: _Host, verdict: Verdict) -> None:
    """The phone was out of reach for fifteen minutes. That is not a no."""
    host.install(BOARD, LIVE)
    host.tag(BOARD, NEXT)
    host.follow(BOARD)
    host.follow(BOARD)
    host.answer(verdict)

    host.now += AGAIN_S - 1
    host.follow(BOARD)
    assert host.filed() == []

    host.now += 1
    host.follow(BOARD)
    assert [item["components"] for item in host.filed()] == [{BOARD: NEXT}]
    assert host.taken() == 1


def test_an_unanswered_set_is_asked_a_fixed_number_of_times(host: _Host) -> None:
    host.install(BOARD, LIVE)
    host.tag(BOARD, NEXT)
    host.follow(BOARD)

    for _ in range(MAX_ASKS + 3):
        host.follow(BOARD)
        if host.filed():
            host.answer(Verdict.TIMEOUT)

        host.now += AGAIN_S

    assert host.taken() == MAX_ASKS
    assert host.filed() == []


def test_a_newer_tag_asks_again_after_a_denial(host: _Host) -> None:
    host.install(BOARD, LIVE)
    host.tag(BOARD, NEXT)
    host.follow(BOARD)
    host.follow(BOARD)
    host.answer(Verdict.DENIED)

    host.tag(BOARD, LATER)
    host.follow(BOARD)
    host.follow(BOARD)

    assert [item["components"] for item in host.filed()] == [{BOARD: LATER}]


def test_a_component_no_release_has_stamped_is_not_followed(
    host: _Host, capsys: pytest.CaptureFixture[str]
) -> None:
    """A first release is filed by hand. `follow` moves a tree forward and
    never makes the first one."""
    host.install(BOARD, None)
    host.tag(BOARD, NEXT)

    host.follow(BOARD)
    host.follow(BOARD)

    assert host.filed() == []
    assert f"{BOARD} has had no release" in capsys.readouterr().err


def test_a_component_that_is_not_installed_is_not_followed(host: _Host) -> None:
    host.tag(BOARD, NEXT)

    host.follow(BOARD)
    host.follow(BOARD)

    assert host.filed() == []


def test_a_tag_below_the_live_version_is_not_followed(host: _Host) -> None:
    """A component never goes backwards (`stage7-releases.md` §6 row 7)."""
    host.install(BOARD, LATER)
    host.tag(BOARD, NEXT)
    _git(host.clone, "tag", "-d", f"{BOARD}-v{LATER}")

    host.follow(BOARD)
    host.follow(BOARD)

    assert host.filed() == []


def test_a_dry_run_writes_nothing(host: _Host, capsys: pytest.CaptureFixture[str]) -> None:
    host.install(BOARD, LIVE)
    host.tag(BOARD, NEXT)

    assert host.follow(BOARD, more=("--dry-run",)) == cli.EXIT_OK
    assert host.follow(BOARD, more=("--dry-run",)) == cli.EXIT_OK

    assert host.filed() == []
    assert not host.marker.exists()
    assert f"{BOARD} {NEXT}" in capsys.readouterr().out


def test_no_marker_means_no_request(host: _Host) -> None:
    """The marker goes down BEFORE the request. A request with no marker
    behind it is filed again on every run, and each one is a push."""
    host.install(BOARD, LIVE)
    host.tag(BOARD, NEXT)
    host.follow(BOARD)
    host.marker.parent.chmod(0o500)

    try:
        code = host.follow(BOARD)
    finally:
        host.marker.parent.chmod(0o700)

    assert code == cli.EXIT_USAGE
    assert host.filed() == []


def test_a_marker_that_does_not_parse_reads_as_no_marker(host: _Host) -> None:
    host.install(BOARD, LIVE)
    host.tag(BOARD, NEXT)
    host.marker.parent.mkdir(parents=True)
    host.marker.write_text("{not json", encoding="utf-8")

    host.follow(BOARD)
    host.follow(BOARD)

    assert len(host.filed()) == 1


def test_the_marker_is_the_operators_alone(host: _Host) -> None:
    host.install(BOARD, LIVE)
    host.tag(BOARD, NEXT)

    host.follow(BOARD)

    assert host.marker.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("name", ["registry-data", "sessiond", "../etc"])
def test_a_name_that_cannot_release_is_a_usage_error(host: _Host, name: str) -> None:
    with pytest.raises(SystemExit) as stopped:
        host.follow(name)

    assert stopped.value.code == cli.EXIT_USAGE


def test_follow_names_itself_on_the_phone() -> None:
    """`requested_by` is a row of the phone summary (§2.5). The operator
    reads who asked before the tap."""
    assert REQUESTED_BY not in ("human", "managerd")
    assert REQUESTED_BY not in CATALOG_BY_NAME


# -- the decision, with no host at all -------------------------------------

SET = {BOARD: NEXT}
ASKED = Marker(seen=SET, asked=SET, request_id="01K5J8M2Q7V3X9R4T6N0B8C2DE", asks=1, asked_at=START)


@pytest.mark.parametrize(
    ("moving", "marker", "answer", "now", "expected"),
    [
        ({}, Marker(), Answer.NONE, START, Step.IDLE),
        ({}, ASKED, Answer.FINAL, START, Step.IDLE),
        (SET, Marker(), Answer.NONE, START, Step.SEEN),
        (SET, Marker(seen=SET), Answer.NONE, START, Step.FILE),
        ({BOARD: LATER}, ASKED, Answer.FINAL, START, Step.SEEN),
        (SET, ASKED, Answer.NONE, START + AGAIN_S, Step.HELD),
        (SET, ASKED, Answer.FINAL, START + AGAIN_S, Step.HELD),
        (SET, ASKED, Answer.UNANSWERED, START + AGAIN_S - 1, Step.HELD),
        (SET, ASKED, Answer.UNANSWERED, START + AGAIN_S, Step.FILE),
    ],
)
def test_the_decision(
    moving: dict[str, str], marker: Marker, answer: Answer, now: float, expected: Step
) -> None:
    assert decide(moving, marker, answer, now) is expected


def test_the_last_ask_is_final() -> None:
    spent = Marker(seen=SET, asked=SET, request_id=ASKED.request_id, asks=MAX_ASKS, asked_at=START)

    assert decide(SET, spent, Answer.UNANSWERED, START + AGAIN_S) is Step.HELD


# -- the package's own fence ------------------------------------------------

_PACKAGE = Path(__file__).resolve().parents[1] / "src" / "handover"


def test_follow_starts_no_child_and_opens_no_socket() -> None:
    """It reads one ledger entry and writes one marker. The fetch is
    `corpus/`'s and the request is `requester/`'s."""
    for module in (_PACKAGE / "follow").rglob("*.py"):
        text = module.read_text(encoding="utf-8")
        for word in ("subprocess", "socket", "urllib", "http"):
            assert f"import {word}" not in text, (module.name, word)


def test_follow_imports_only_the_executors_pure_modules() -> None:
    """The words a gate that did not grant is ledgered with, and the ledger
    directory's name. Anything else would give a timer the executor's reach."""
    allowed = ("from ..executor.approval import", "from ..executor.spool import")
    for module in (_PACKAGE / "follow").rglob("*.py"):
        for line in module.read_text(encoding="utf-8").splitlines():
            if "executor" in line and line.startswith(("from", "import")):
                assert line.startswith(allowed), (module.name, line)


def test_the_chaperone_never_imports_follow() -> None:
    """The `release` verb files what its caller names. A chaperone that
    could follow tags would file with no caller at all."""
    chaperone = _PACKAGE.parents[2] / "chaperone" / "src"
    for module in chaperone.rglob("*.py"):
        assert "handover.follow" not in module.read_text(encoding="utf-8"), module.name


# -- the units ----------------------------------------------------------------

_SYSTEMD = _PACKAGE.parents[2] / "systemd"


def _unit_value(name: str, key: str) -> str:
    lines = (_SYSTEMD / name).read_text(encoding="utf-8").splitlines()
    found = [line.partition("=")[2] for line in lines if line.startswith(f"{key}=")]
    assert len(found) == 1, (name, key, found)

    return found[0]


def test_the_service_runs_follow_out_of_the_handover_tree() -> None:
    """A `handover` release then changes what the timer runs, with no deploy."""
    argv = _unit_value("creche-follow.service", "ExecStart").split()

    assert argv[:2] == ["/opt/components/handover/bin/handover", "follow"]
    assert cli.build_parser().parse_args(argv[1:]).command == "follow"


def test_the_service_follows_every_component_a_release_can_deploy() -> None:
    """Every releasable component of the catalog, less the one on its way
    out. A name that is absent here waits for a typed command for ever."""
    argv = _unit_value("creche-follow.service", "ExecStart").split()
    followed = cli.build_parser().parse_args(argv[1:]).components

    assert set(followed) == set(releasable_names()) - RETIRING
    assert len(followed) == len(set(followed))


def test_the_timer_starts_the_service() -> None:
    assert _unit_value("creche-follow.timer", "Unit") == "creche-follow.service"
