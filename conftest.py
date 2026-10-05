"""Rules every test in this repository gets, whichever package holds it.

Under pytest-xdist the test process IS the worker, and one worker runs
hundreds of tests from every package in turn. Process-wide state that a test
changes and does not put back outlives it, and lands on whatever test that
worker runs next.

It also holds `--shard K/N`, which CI's gate uses to run the one suite on N
machines at once (.github/workflows/gate.yml).

It also drops the variables that point a `git` child at the repository of
the caller, when pytest imports this file. At the same time it gives each
`git` child a global config file that holds only the settings of this file,
no config file of the system, and no ignore file and no attributes file of a
person. Those settings stop the maintenance that `git` starts and does not
wait for.

A run that starts with a signal ignored, from a background job or under
`nohup`, passes the same tests and keeps that signal ignored.
"""

from __future__ import annotations

import atexit
import contextlib
import os
import pwd
import shutil
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


#: What gives a `git` child no config file of the system. `_drop_git_config`
#: also names a file of this run in place of the global config file. The
#: global file is the one of the person who runs the suite, in the home
#: directory or in the variable.
#:
#: `git` also reads an ignore file and an attributes file from the config
#: directory of that person, and an attributes file of the system. No
#: variable names another place for the first two, so the first two pairs give
#: each setting the empty file. A pair outranks the config of a repository: a
#: fixture that needs one of the two settings passes it with `git -c`.
#:
#: The last two pairs are the settings of `GIT_SETTINGS`.
GIT_NO_CONFIG = {
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_ATTR_NOSYSTEM": "1",
    "GIT_CONFIG_COUNT": "4",
    "GIT_CONFIG_KEY_0": "core.excludesFile",
    "GIT_CONFIG_VALUE_0": os.devnull,
    "GIT_CONFIG_KEY_1": "core.attributesFile",
    "GIT_CONFIG_VALUE_1": os.devnull,
    "GIT_CONFIG_KEY_2": "maintenance.auto",
    "GIT_CONFIG_VALUE_2": "false",
    "GIT_CONFIG_KEY_3": "gc.autoDetach",
    "GIT_CONFIG_VALUE_3": "false",
}

#: The variable that names the global config file.
GIT_GLOBAL_ENV = "GIT_CONFIG_GLOBAL"

#: The global config file of a test run: the settings that stop the work
#: `git` starts and does not wait for. After a commit, a merge or a fetch,
#: `git` starts `git maintenance run --auto`. A repository that receives a
#: push does the same. That process detaches. From git 2.55 it keeps
#: `objects/maintenance.lock` until it ends, after the command returned. A
#: test that removes or copies the repository in that time finds a file that
#: is gone.
#:
#: * `maintenance.auto`: no command starts that process.
#: * `gc.autoDetach`: maintenance that a fixture asks for runs in the
#:   foreground. `maintenance.autoDetach` falls back to this setting.
#:
#: Each setting reaches a `git` child in two ways, because each way has a gap.
#: A fixture that names its own global file does not read this one, and it
#: gets the pairs of `GIT_NO_CONFIG`. `git` removes the pairs from the
#: environment of the repository that receives a push, and that repository
#: reads this file. A fixture that needs maintenance passes
#: `maintenance.auto=true` with `git -c`.
#:
#: `core.fsmonitor` is not here. Its default starts no program. A pair for it
#: would outrank the config of a repository, and some tests write that config
#: to prove that the code under test passes the setting itself.
GIT_SETTINGS = "[maintenance]\n\tauto = false\n[gc]\n\tautoDetach = false\n"

#: The start of the name of the directory that holds the file.
SETTINGS_PREFIX = "git-settings-"

#: The mode that lets no account make a file in that directory, and the mode
#: that lets this process remove it.
READ_ONLY = 0o500
OWNER_ONLY = 0o700


def _settings_file() -> str:
    """Write `GIT_SETTINGS` to a file of this process. Return its path.

    The directory takes no new file, so a `git config --global` of a test
    fails, as it did when the variable named the empty file. This process
    removes the directory when it ends.

    A process that a signal ends runs no exit handler and leaves the
    directory. Run `chmod 700` on such a directory before the delete.
    """
    directory = Path(tempfile.mkdtemp(prefix=SETTINGS_PREFIX))
    path = directory / "config"
    path.write_text(GIT_SETTINGS, encoding="utf-8")
    directory.chmod(READ_ONLY)
    atexit.register(_remove_settings, directory, os.getpid())

    return str(path)


def _remove_settings(directory: Path, owner: int) -> None:
    """Remove the directory of `_settings_file`, in the process that made it.

    A fork of that process holds the same exit handler. It must not remove a
    file that its parent still names. A directory that is gone is no error.
    """
    if os.getpid() != owner:
        return

    with contextlib.suppress(OSError):
        directory.chmod(OWNER_ONLY)

    shutil.rmtree(directory, ignore_errors=True)


def _drop_git_config() -> None:
    """Keep the config file of a person and of the system from each `git` child.

    Some fixtures run `git` in a throwaway repository with the environment
    they inherit. `git` then reads the global config file of the person who
    runs the suite. One setting there, `core.fsmonitor`, starts a program for
    each repository. Other settings change what a test sees. The ignore file
    of that person can keep a file of a fixture out of `git add -A`.
    bin/tests/test_git_config_dropped.py holds the proof.

    The same variables stop the maintenance of `git`.
    bin/tests/test_git_background_dropped.py holds that proof.
    """
    os.environ.update(GIT_NO_CONFIG)
    os.environ[GIT_GLOBAL_ENV] = _settings_file()


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


def _loop_writes(one: signal.Signals) -> object:
    """What asyncio writes for a signal when a loop stops taking it.

    A loop keeps no record of how the process took the signal before. It
    writes the default handler of Python for SIGINT, and the default action
    for each other signal.
    """
    if one is signal.SIGINT:
        return signal.default_int_handler

    return signal.SIG_DFL


@pytest.fixture(autouse=True)
def _signals_kept() -> Iterator[None]:
    """Fail a test that leaves a signal disposition changed, and put it back.

    A leaked disposition does its damage later, in a test that did nothing
    wrong. chaperone's launcher test left SIGPIPE at SIG_DFL, so the worker's next
    write to a closed socket killed the whole worker. xdist then blamed
    whichever release test it was running: "worker 'gw6' crashed while
    running ...".

    One change is not a fault of the test. A shell with no job control starts
    a background job with SIGINT ignored, and `nohup` starts a command with
    SIGHUP ignored. A service that runs in the test process takes signals in
    its asyncio loop, and that loop cannot ignore a signal again when it
    closes. For a signal that the run ignored before the test, this fixture
    ignores the signal again and does not fail the test for the value that a
    loop writes. A run that starts with the default has that same value
    before the test, so this run judges the test as that run does.
    bin/tests/test_ignored_signal_kept.py holds the proof.
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

    faults = [
        one
        for one in changed
        if not (before[one] is signal.SIG_IGN and after[one] is _loop_writes(one))
    ]
    if not faults:
        return

    names = ", ".join(one.name for one in faults)
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
