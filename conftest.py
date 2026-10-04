"""Rules every test in this repository gets, whichever package holds it.

Under pytest-xdist the test process IS the worker, and one worker runs
hundreds of tests from every package in turn. Process-wide state that a test
changes and does not put back outlives it, and lands on whatever test that
worker runs next.

It also holds `--shard K/N`, which CI's gate uses to run the one suite on N
machines at once (.github/workflows/gate.yml).

It also drops the variables that point a `git` child at the repository of
the caller, when pytest imports this file. At the same time it gives each
`git` child an empty global config file and no config file of the system.
"""

from __future__ import annotations

import os
import pwd
import signal
import tempfile
import zlib
from collections.abc import Iterator
from pathlib import Path

import pytest

#: What git exports to a command it starts: a hook, an alias, `git rebase
#: --exec`, `git bisect run`. Each one points a `git` child at the repository
#: of the caller. The two hooks unset the same five (githooks/pre-commit).
GIT_ENV = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_COMMON_DIR",
    "GIT_OBJECT_DIRECTORY",
)


def _drop_git_env() -> None:
    """Remove git's own variables from this process, so no child gets one.

    Some fixtures run `git init` and `git config` in a throwaway repository
    with the environment they inherit. With `GIT_DIR` set, those commands
    write into the repository of the caller instead. A test run from a linked
    worktree left `core.bare` and a `[user]` section in the shared config
    that way. bin/tests/test_git_env_dropped.py holds the proof.
    """
    for name in GIT_ENV:
        os.environ.pop(name, None)


#: What gives a `git` child an empty file in place of the global config file,
#: and no config file of the system. The global file is the one of the person
#: who runs the suite, in the home directory or in the variable.
GIT_NO_CONFIG = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}


def _drop_git_config() -> None:
    """Keep the config file of a person and of the system from each `git` child.

    Some fixtures run `git` in a throwaway repository with the environment
    they inherit. `git` then reads the global config file of the person who
    runs the suite. One setting there, `core.fsmonitor`, starts a program for
    each repository. Other settings change what a test sees.
    bin/tests/test_git_config_dropped.py holds the proof.
    """
    os.environ.update(GIT_NO_CONFIG)


# At import and not in a hook. pytest imports this file before it imports a
# test module or a conftest.py below it, and before xdist starts a worker.
# So no module, no fixture and no test ever sees one of the variables, and
# each one runs `git` with no config file of a person.
_drop_git_env()
_drop_git_config()

#: Every signal this platform names. Linux's unnamed real-time signals are
#: left out: no test here touches them.
NAMED = [one for one in signal.Signals if one in signal.valid_signals()]


def _dispositions() -> dict[signal.Signals, object]:
    """How this process takes each signal, as `signal.getsignal` reports it."""
    return {one: signal.getsignal(one) for one in NAMED}


@pytest.fixture(autouse=True)
def _signals_kept() -> Iterator[None]:
    """Fail a test that leaves a signal disposition changed, and put it back.

    A leaked disposition does its damage later, in a test that did nothing
    wrong. chaperone's launcher test left SIGPIPE at SIG_DFL, so the worker's next
    write to a closed socket killed the whole worker. xdist then blamed
    whichever release test it was running: "worker 'gw6' crashed while
    running ...".
    """
    before = _dispositions()

    yield

    after = _dispositions()
    changed = [one for one in NAMED if after[one] is not before[one]]
    if not changed:
        return

    # Restore first, so the rest of this worker's run is unharmed. A handler
    # installed outside Python reads as None and cannot be put back from here.
    for one in changed:
        previous = before[one]
        if previous is None:
            continue

        signal.signal(one, previous)  # pyright: ignore[reportArgumentType]

    names = ", ".join(one.name for one in changed)
    pytest.fail(f"the test left {names} changed. Restore every signal handler a test sets.")


#: Names the site file every test reads (handover/src/handover/site.py).
SITE_FILE_ENV = "AGENT_SITE_FILE"

#: A site that is nobody's: the values a test may rely on. The addresses
#: are TEST-NET-1 (RFC 5737), which no host ever holds.
EXAMPLE_LAN_ADDRESS = "192.0.2.10"
EXAMPLE_HA_URL = "http://192.0.2.20:8123"
EXAMPLE_SITE = f"""\
AGENT_GITHUB_OWNER=example-owner
AGENT_OPERATOR_USER=operator
AGENT_OPERATOR_HOME=/home/operator
AGENT_LAN_ADDRESS={EXAMPLE_LAN_ADDRESS}
AGENT_HA_URL={EXAMPLE_HA_URL}
"""

#: What a unit's `EnvironmentFile` hands a service from that same file.
LAN_ADDRESS_ENV = "AGENT_LAN_ADDRESS"
HA_URL_ENV = "AGENT_HA_URL"

#: The example site file of this process, written once in `pytest_configure`.
_example_site_file: list[Path] = []


@pytest.fixture(autouse=True)
def _site_is_the_example(monkeypatch: pytest.MonkeyPatch) -> None:
    """Point every test at the example site file.

    No test may read the host's own `/etc/creche/site.env`: a suite
    that passes only on the machine that has one proves nothing, and one that
    reads it would carry that machine's values into its assertions. A test
    that needs another site sets the variable again.

    A service gets the same values from its unit's `EnvironmentFile`, so the
    two a service reads are in the environment too.
    """
    monkeypatch.setenv(SITE_FILE_ENV, str(_example_site_file[0]))
    monkeypatch.setenv(LAN_ADDRESS_ENV, EXAMPLE_LAN_ADDRESS)
    monkeypatch.setenv(HA_URL_ENV, EXAMPLE_HA_URL)


@pytest.fixture
def operator_is_an_account(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the site's operator an account of this machine, with the home
    the site names. `site.operator_account` asks the password database, and
    no test machine has the account a site file names."""
    from handover import site

    def lookup(name: str) -> pwd.struct_passwd:
        if name != site.operator_user():
            raise KeyError(name)

        return pwd.struct_passwd((name, "x", 1234, 1234, "", site.operator_home(), "/bin/sh"))

    monkeypatch.setattr(pwd, "getpwnam", lookup)


#: `--shard K/N`: run shard K of N. Unset, a run keeps every test.
SHARD_OPTION = "--shard"

#: Between K and N, e.g. `2/4`.
SHARD_SEPARATOR = "/"


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        SHARD_OPTION,
        default=None,
        metavar="K/N",
        help="run only shard K of N: every test is in exactly one of the N",
    )


def parse_shard(text: str) -> tuple[int, int]:
    """`2/4` -> (2, 4). Anything else, or a K outside 1..N, is a usage error."""
    shard, separator, total = text.partition(SHARD_SEPARATOR)
    if not separator or not shard.isdecimal() or not total.isdecimal():
        raise pytest.UsageError(f"{SHARD_OPTION} {text}: not K/N, e.g. 2/4")

    if not 1 <= int(shard) <= int(total):
        raise pytest.UsageError(f"{SHARD_OPTION} {text}: K must be 1 to N")

    return int(shard), int(total)


def in_shard(nodeid: str, shard: int, total: int) -> bool:
    """Whether shard `shard` of `total` runs the test `nodeid`.

    The test's own id decides, through a hash that is the same on every
    machine: crc32("chaperone/tests/test_x.py::test_y") % 4 == 1 puts that test in
    shard 2/4 and in no other. A rule that counted positions instead would
    drop or repeat a test wherever two machines collect in different orders.
    Hashing per test and not per file also spreads one slow file over every
    shard: bin/tests/test_rework_release_visit.py alone is 44% of the suite's
    time.
    """
    return zlib.crc32(nodeid.encode()) % total == shard - 1


def pytest_configure(config: pytest.Config) -> None:
    """Refuse a bad `--shard` before collection, not from inside each worker.

    It also names the example site for everything that runs OUTSIDE a test
    function: a module that parses this repository's manifests as it is
    imported, and a module-scoped fixture, both run before
    `_site_is_the_example` does.
    """
    path = Path(tempfile.mkdtemp(prefix="example-site-")) / "site.env"
    path.write_text(EXAMPLE_SITE, encoding="utf-8")
    _example_site_file.append(path)
    os.environ[SITE_FILE_ENV] = str(path)

    text = config.getoption(SHARD_OPTION)
    if text is not None:
        parse_shard(text)


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Keep this run's shard of the collected tests, and deselect the rest."""
    text = config.getoption(SHARD_OPTION)
    if text is None:
        return

    shard, total = parse_shard(text)
    kept = [one for one in items if in_shard(one.nodeid, shard, total)]
    dropped = [one for one in items if not in_shard(one.nodeid, shard, total)]

    items[:] = kept
    config.hook.pytest_deselected(items=dropped)
