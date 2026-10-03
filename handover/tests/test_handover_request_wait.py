"""`handover request --wait`: filed right after a tag dispatch, it
waits for the tag instead of being refused.

A tag reaches GitHub some time after the operator dispatches its repository's
`rework component tags` workflow (about a minute for `mcp-servers-v0.1.2`),
and a request filed before it lands is refused with `nothing would deploy`.
The fetch (`test_handover_r7j_freshness.py`) finds only a tag that has
landed, so the request waits for it.

Every test runs the real builder over real clones under `tmp_path`.
`cli.refresh` stands in for GitHub and lands a tag on the poll the test
names. `cli.monotonic` and `cli.sleep` are one fake clock, so no test sleeps.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest
from handover.catalog import CATALOG, CATALOG_BY_NAME, Repo
from handover.corpus import FETCH_NOTE, ROOT_REFUSAL, CorpusReport
from handover.executor.live_state import write_stamp
from handover.executor.request import parse_request
from handover_fixtures import manifest_text, write_manifest

from handover import cli

COMPONENT = "noticeboard"
LIVE = "0.1.0"
NEXT = "0.1.1"
NEXT_TAG = f"{COMPONENT}-v{NEXT}"


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


@dataclass(frozen=True)
class _Host:
    """The options every request here passes, and the two paths tests read."""

    options: list[str]
    spool: Path
    #: The agent-control clone, where `noticeboard` and `chaperone` are tagged.
    control: Path


def _host(tmp_path: Path, live: dict[str, str]) -> _Host:
    """Every manifest, three clones, and each `live` component installed and
    tagged at its version, the way the previous release left it."""
    roots = tmp_path / "repo"
    for row in CATALOG:
        write_manifest(roots, row.name, manifest_text(row.name))

    corpus = tmp_path / "corpus"
    corpus.mkdir()
    corpus.chmod(0o755)
    for repo in Repo:
        clone = corpus / str(repo)
        clone.mkdir()
        clone.chmod(0o755)
        (clone / "README.md").write_text(f"{repo}\n", encoding="utf-8")
        _git(clone, "init", "-q", "-b", "main")
        _git(clone, "add", "-A")
        _git(clone, "commit", "-q", "-m", "first")

    installs = tmp_path / "components"
    for name, version in live.items():
        _git(corpus / str(CATALOG_BY_NAME[name].repo), "tag", f"{name}-v{version}")
        tree = installs / name
        tree.mkdir(parents=True)
        write_stamp(tree, version)

    spool = tmp_path / "spool"
    spool.mkdir()
    options = [
        "--root",
        str(roots),
        "--partial",
        "--corpus",
        str(corpus),
        "--install-root",
        str(installs),
        "--spool",
        str(spool),
    ]

    return _Host(options, spool, corpus / str(Repo.AGENT_CONTROL))


class _GitHub:
    """`cli.refresh`: poll N brings the tag `lands[N]` into the clone, the
    way the tag workflow's run lands about a minute after the dispatch."""

    def __init__(self, clone: Path, lands: dict[int, str]) -> None:
        self.clone = clone
        self.lands = lands
        self.polls = 0

    def __call__(self, repos: tuple[str, ...], *_args: object, **_kwargs: object) -> CorpusReport:
        self.polls += 1
        tag = self.lands.get(self.polls)
        if tag is not None:
            _git(self.clone, "tag", tag)

        return CorpusReport(True, [f"fetched {name}" for name in repos])


class _Clock:
    """`cli.monotonic` and `cli.sleep` as one clock: a sleep moves it."""

    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    fake = _Clock()
    monkeypatch.setattr(cli, "monotonic", fake.monotonic)
    monkeypatch.setattr(cli, "sleep", fake.sleep)

    return fake


def _github(monkeypatch: pytest.MonkeyPatch, host: _Host, lands: dict[int, str]) -> _GitHub:
    fake = _GitHub(host.control, lands)
    monkeypatch.setattr(cli, "refresh", fake)

    return fake


def _filed(host: _Host) -> list[dict[str, str]]:
    return [
        parse_request(path.read_bytes(), path.stem).wanted()
        for path in sorted(host.spool.glob("*.json"))
    ]


def test_a_tag_that_lands_on_the_second_poll_files_the_request(
    tmp_path: Path,
    clock: _Clock,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The dispatch case. The first poll finds `noticeboard` at its live version,
    which is where a request without `--wait` refuses. `--wait` sleeps once
    and files."""
    host = _host(tmp_path, {COMPONENT: LIVE})
    github = _github(monkeypatch, host, {2: NEXT_TAG})

    code = cli.main(["request", COMPONENT, *host.options, "--wait"])

    err = capsys.readouterr().err
    assert code == cli.EXIT_OK, err
    assert _filed(host) == [{COMPONENT: "latest"}]
    assert github.polls == 2
    assert clock.slept == [cli.POLL_S]
    assert err.count("waiting for a tag that moves noticeboard (0s of 600s)") == 1


def test_the_deadline_refuses_with_todays_words_and_the_wait(
    tmp_path: Path,
    clock: _Clock,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """No tag ever lands. Polls at 0, 10, 20 and 30 s, then today's refusal
    plus how long it waited, and nothing in the spool."""
    host = _host(tmp_path, {COMPONENT: LIVE})
    github = _github(monkeypatch, host, {})

    code = cli.main(["request", COMPONENT, *host.options, "--wait", "30"])

    err = capsys.readouterr().err
    assert code == cli.EXIT_REFUSED
    assert _filed(host) == []
    assert (
        "refused [request] request: nothing would deploy: every named component "
        "is already at that version after waiting 30s"
    ) in err
    assert github.polls == 4
    assert clock.slept == [cli.POLL_S] * 3
    # One line per waiting poll: the fetch note is said once, not per poll.
    assert err.count(FETCH_NOTE.format(root=host.control.parent)) == 1
    assert err.count("waiting for a tag that moves noticeboard") == 3


def test_a_dry_run_that_waits_files_nothing(
    tmp_path: Path,
    clock: _Clock,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """It waits like the real thing, then prints and files nothing. It
    fetches on every poll: a wait that never fetched could see no tag."""
    host = _host(tmp_path, {COMPONENT: LIVE})
    github = _github(monkeypatch, host, {2: NEXT_TAG})

    code = cli.main(["request", COMPONENT, *host.options, "--wait", "--dry-run"])

    assert code == cli.EXIT_OK
    assert "dry run: nothing written" in capsys.readouterr().out
    assert _filed(host) == []
    assert github.polls == 2
    assert clock.slept == [cli.POLL_S]


def test_no_flag_means_one_fetch_one_resolve_and_no_sleep(
    tmp_path: Path,
    clock: _Clock,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Today's behaviour, unchanged: the tag would land on a second poll,
    and there is no second poll."""
    host = _host(tmp_path, {COMPONENT: LIVE})
    github = _github(monkeypatch, host, {2: NEXT_TAG})
    resolved: list[object] = []
    real = cli.resolve
    monkeypatch.setattr(cli, "resolve", lambda *a: resolved.append(a) or real(*a))

    code = cli.main(["request", COMPONENT, *host.options])

    err = capsys.readouterr().err
    assert code == cli.EXIT_REFUSED
    assert f"refused [request] request: {cli.NOTHING_DEPLOYS}\n" in err
    assert "after waiting" not in err
    assert github.polls == 1
    assert len(resolved) == 1
    assert clock.slept == []


def test_a_named_version_waits_for_exactly_that_tag(
    tmp_path: Path, clock: _Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`noticeboard@0.1.2` would deploy from the first poll, but its tag is not there
    yet. `noticeboard-v0.1.1` landing does not move it. `noticeboard-v0.1.2` does."""
    host = _host(tmp_path, {COMPONENT: LIVE})
    github = _github(monkeypatch, host, {2: NEXT_TAG, 3: f"{COMPONENT}-v0.1.2"})

    code = cli.main(["request", f"{COMPONENT}@0.1.2", *host.options, "--wait"])

    assert code == cli.EXIT_OK
    assert _filed(host) == [{COMPONENT: "0.1.2"}]
    assert github.polls == 3
    assert clock.slept == [cli.POLL_S] * 2


def test_every_named_component_must_move_not_only_one(
    tmp_path: Path,
    clock: _Clock,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Without `--wait` a set that moves `noticeboard` alone files. With it, the set
    the operator named waits whole, and the deadline names what did not move."""
    host = _host(tmp_path, {COMPONENT: LIVE, "chaperone": LIVE})
    _github(monkeypatch, host, {2: NEXT_TAG})

    code = cli.main(["request", COMPONENT, "chaperone", *host.options, "--wait", "20"])

    err = capsys.readouterr().err
    assert code == cli.EXIT_REFUSED
    assert _filed(host) == []
    assert "waiting for a tag that moves chaperone, noticeboard (0s of 20s)" in err
    assert "waiting for a tag that moves chaperone (10s of 20s)" in err
    assert "refused [request] request: no tag moves chaperone after waiting 20s" in err
    assert clock.slept == [cli.POLL_S] * 2


def test_a_fetch_that_fails_is_said_on_the_poll_it_fails(
    tmp_path: Path,
    clock: _Clock,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Run as root, the fetch refuses on every poll. Waiting in silence
    would read as a tag that never came, for the whole deadline."""
    host = _host(tmp_path, {COMPONENT: LIVE})
    monkeypatch.setattr(cli, "refresh", lambda *_a, **_k: CorpusReport(False, [ROOT_REFUSAL]))

    code = cli.main(["request", COMPONENT, *host.options, "--wait", "10"])

    err = capsys.readouterr().err
    assert code == cli.EXIT_REFUSED
    note = err.index(f"note: {ROOT_REFUSAL}")
    assert note < err.index("waiting for a tag that moves noticeboard (0s of 10s)")
    assert clock.slept == [cli.POLL_S]


def test_a_malformed_version_is_refused_before_any_fetch_or_wait(
    tmp_path: Path, clock: _Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No tag can ever be named `noticeboard-v0.1`. The executor's own parser says so
    before the first poll, not after the whole deadline."""
    host = _host(tmp_path, {COMPONENT: LIVE})
    github = _github(monkeypatch, host, {})

    code = cli.main(["request", f"{COMPONENT}@0.1", *host.options, "--wait"])

    assert code == cli.EXIT_REFUSED
    assert github.polls == 0
    assert clock.slept == []


def test_a_refusal_no_tag_can_lift_ends_the_wait_at_once(
    tmp_path: Path, clock: _Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The resolver's own refusals are not waited on: a name contract 06 §1
    does not list stays unlisted whatever GitHub tags."""
    host = _host(tmp_path, {COMPONENT: LIVE})
    github = _github(monkeypatch, host, {})

    code = cli.main(["request", "nosuch", *host.options, "--wait"])

    assert code == cli.EXIT_REFUSED
    assert github.polls == 1
    assert clock.slept == []
