"""Nothing a pass READS can kill the loop.

`caregiver` runs as the operator, and a root script can leave a directory it reads
`root:root 0750`. A pass then ends in:

    loop._finish -> loop._mcp_servers -> mcp_wire.mcp_pass
      -> pathlib is_dir -> os.stat
    PermissionError: [Errno 13] Permission denied:
      '/srv/agents/state/rework/releases/requests'

`Path.is_dir` swallows ENOENT and ENOTDIR and re-raises EACCES. Outside
`mcp_pass`'s own guard, that one call ends the loop, and every status
document goes on saying `in_sync`.

Two kinds of test here, because one of them cannot run everywhere.

1. Mode-driven: a real 0000 directory, which is the fault as the host had
   it. Root reads a 0000 directory anyway, so these skip for root, the
   same way `attendance/tests/test_attendance_approvals.py` already does.
2. Injected: a raising stand-in for the call, which proves the same guard
   under any account. Every claim this file makes is covered by at least
   one test of kind 2, so a run as root still proves the guard.
"""

from __future__ import annotations

import logging
import os
import shutil
import time
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Final

import pytest
import yaml
from agent_family import FamilyState, revision_of
from caregiver.driver import FakeDriver
from caregiver.egress import EgressConfig
from caregiver.litellm_keys import FakeLiteLLMKeys
from caregiver.loop import BACKOFF_FIRST_S, Backoff, LoopConfig, LoopState, look
from caregiver.mcp_release import MARKER_NAME, McpPaths
from caregiver.mcp_wire import mcp_pass
from caregiver.reconcile import Actors
from caregiver.status import forget_published
from caregiver.switch import FakeSwitchClient
from caregiver.timers import FakeUnits
from caregiver_helpers import published, write_registry

from caregiver import loop as loop_module
from caregiver import paths

IMAGE: Final = "sha256:deadbeef"
NOW: Final = 1_758_153_600.0

SERVER: Final = "weather"
SECRET: Final = "weather_token"

#: The two families every test converges. ONE unreadable path costs
#: neither of them a pass.
FAMILIES: Final = ("chat", "vault-oracle")

#: No `x` bit, so nothing below this directory can be STATTED. That is
#: the fault exactly: with `releases/` `root:root 0750` and the
#: operator not root, `releases/requests` cannot be stat'ed at all. A
#: 0000 on the directory ITSELF would not reproduce it — a stat of a
#: directory needs the search bit on its PARENT, not on itself.
NO_SEARCH: Final = 0o600

#: Readable and searchable, not writable.
READ_ONLY: Final = 0o500

#: Whatever the test put back afterwards. Never the umask's answer.
RESTORED: Final = 0o700

is_root = pytest.mark.skipif(os.geteuid() == 0, reason="root reads a 0000 directory anyway")


def server_yaml(name: str = SERVER, secret: str = SECRET) -> str:
    """Contract 01b §8.1, shortened to what `mcp_wire` reads."""
    return yaml.safe_dump(
        {
            "name": name,
            "identity": f"The fleet's {name} credential. Read only.",
            "install": {
                "source": "pypi",
                "package": f"{name}-mcp",
                "version": "1.0.0",
                "lock": f"mcp/{name}/install.lock",
                "python": "3.12",
            },
            "run": {
                "entrypoint": f"{name}-mcp",
                "env": {"API_TOKEN": f"secret:{secret}"},
            },
            "tools": [{"name": "forecast", "description": "Tomorrow's weather, by city."}],
        }
    )


class Bench:
    """Two families, one declared MCP server, and the spool's own paths."""

    def __init__(self, tmp_path: Path) -> None:
        self.registry_root = tmp_path / "registry"
        self.state_root = tmp_path / "state"
        for name in FAMILIES:
            write_registry(self.registry_root, name=name)

        directory = self.registry_root / "mcp" / SERVER
        directory.mkdir(parents=True)
        (directory / "server.yaml").write_text(server_yaml(), encoding="utf-8")

        self.mcp = McpPaths(
            requests=_made(tmp_path / "spool" / "requests"),
            done=_made(tmp_path / "spool" / "done"),
            secrets=_made(tmp_path / "secrets"),
            gaps=_made(tmp_path / "secret-gaps"),
            installed_root=_made(tmp_path / "opt-mcp"),
            marker=_made(self.state_root) / MARKER_NAME,
            roster=self.state_root / "upstreams.yaml",
        )
        self.state = LoopState()

    def config(self) -> LoopConfig:
        return LoopConfig(
            registry_root=self.registry_root,
            state_root=self.state_root,
            image=IMAGE,
            mcp=self.mcp,
            clock=lambda: NOW,
        )

    def actors(self) -> Actors:
        return Actors(
            FakeDriver(), FakeLiteLLMKeys(), FakeSwitchClient(), EgressConfig(), FakeUnits()
        )

    def look(self, **overrides: float) -> tuple[str, ...]:
        """One real pass of the loop, registry read and all."""
        return look(replace(self.config(), **overrides), self.actors(), self.state)

    def published(self) -> tuple[str, ...]:
        """Every family that now has a status document."""
        families = self.state_root / "families"

        return tuple(
            sorted(one.name for one in families.iterdir() if (one / "status.json").is_file())
        )

    def requests(self) -> list[str]:
        return sorted(one.name for one in self.mcp.requests.iterdir())


def _made(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


@pytest.fixture
def bench(tmp_path: Path) -> Iterator[Bench]:
    forget_published()
    yield Bench(tmp_path)
    forget_published()


# --- the release spool, exactly as the host had it ---------------------------


@is_root
def test_an_unreadable_release_spool_does_not_kill_the_loop(bench: Bench) -> None:
    """`mcp_pass`'s first line stats `requests/`, under a spool ROOT a root
    script can re-mode."""
    bench.mcp.requests.parent.chmod(NO_SEARCH)
    try:
        started = bench.look()
    finally:
        bench.mcp.requests.parent.chmod(RESTORED)

    assert started == FAMILIES
    assert bench.published() == FAMILIES


@is_root
def test_an_unreadable_release_spool_says_the_path_and_the_error(bench: Bench) -> None:
    """Item 1 rule 1: the problem is SAID. Contract 05 gives a per-family
    fault no honest home for a fleet-wide path this manager cannot read,
    so the report line and the log line are the whole answer."""
    bench.mcp.requests.parent.chmod(NO_SEARCH)
    try:
        bench.look()
    finally:
        bench.mcp.requests.parent.chmod(RESTORED)

    line = "; ".join(bench.state.mcp.problems)
    assert "PermissionError" in line
    assert str(bench.mcp.requests) in line


def test_a_spool_that_is_a_file_is_quiet(bench: Bench) -> None:
    """A file where the directory belongs. `is_dir` answers False for one,
    which is rule 2's "this host has no spool": quiet, not a problem."""
    bench.mcp.requests.rmdir()
    bench.mcp.requests.write_text("not a directory\n", encoding="utf-8")

    started = bench.look()

    assert started == FAMILIES
    assert bench.state.mcp.problems == ()


def test_an_unstattable_spool_is_a_problem_and_not_a_crash(
    bench: Bench, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Kind 2 of the same fault, so a run as root proves it too."""
    real = Path.is_dir

    def refuse(self: Path, **kwargs: object) -> bool:
        if self == bench.mcp.requests:
            raise PermissionError(13, "Permission denied", str(self))

        return bool(real(self, **kwargs))  # pyright: ignore[reportCallIssue]

    monkeypatch.setattr(Path, "is_dir", refuse)

    started = bench.look()

    assert started == FAMILIES
    assert any("PermissionError" in one for one in bench.state.mcp.problems)


# --- the secrets directory: a fault a host can carry ------------------------


def test_a_gap_that_cannot_be_read_does_not_stop_the_request(
    bench: Bench, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With `secrets/` `root:root 0700`, the operator cannot stat a child of it and
    `open_gaps` raises on every pass. With both calls under ONE `try`, the
    raise would ALSO skip `request_servers` and invariant 18 would file
    nothing at all, for ever, with the log line blaming a secret.

    One broken read may cost its own answer. It may not cost another's.
    """
    real = Path.exists

    def refuse(self: Path, **kwargs: object) -> bool:
        if self.parent == bench.mcp.secrets:
            raise PermissionError(13, "Permission denied", str(self))

        return bool(real(self, **kwargs))  # pyright: ignore[reportCallIssue]

    monkeypatch.setattr(Path, "exists", refuse)

    bench.look()

    report = bench.state.mcp
    assert report.requested == (SERVER,)
    assert len(bench.requests()) == 1
    assert any("PermissionError" in one for one in report.problems)


@is_root
def test_an_unreadable_secrets_directory_still_files_the_request(bench: Bench) -> None:
    """`root:root 0700` on the host, reproduced by taking the search bit
    off. The gap cannot be judged, and the REQUEST still goes."""
    bench.mcp.secrets.chmod(NO_SEARCH)
    try:
        started = bench.look()
    finally:
        bench.mcp.secrets.chmod(RESTORED)

    assert started == FAMILIES
    assert bench.published() == FAMILIES
    assert bench.state.mcp.requested == (SERVER,)
    assert len(bench.requests()) == 1


@is_root
def test_an_unwritable_gap_directory_leaves_the_families_alone(bench: Bench) -> None:
    """The write half. `atomic_write` into a read-only directory raises
    EACCES, and the families still converge."""
    bench.mcp.gaps.chmod(READ_ONLY)
    try:
        started = bench.look()
    finally:
        bench.mcp.gaps.chmod(RESTORED)

    assert started == FAMILIES
    assert any("PermissionError" in one for one in bench.state.mcp.problems)


def test_mcp_pass_never_raises_whatever_the_spool_is(tmp_path: Path) -> None:
    """The module's own rule 1, asserted against the module. A path that
    is a file, a path that is gone, and a path nothing may read."""
    registry_root = write_registry(tmp_path / "registry")
    gone = tmp_path / "not-there"
    (tmp_path / "a-file").write_text("x\n", encoding="utf-8")

    for requests in (gone, tmp_path / "a-file", tmp_path):
        paths = McpPaths(
            requests=requests,
            done=gone,
            secrets=gone,
            gaps=gone,
            installed_root=gone,
            marker=gone / MARKER_NAME,
            roster=gone / "upstreams.yaml",
        )
        from agent_family import load_registry

        # No assertion on the report: the claim is that the call RETURNS.
        mcp_pass(load_registry(registry_root), paths, NOW)


# --- caregiver's own state root ----------------------------------------------


@is_root
def test_an_unreadable_families_directory_does_not_kill_the_loop(bench: Bench) -> None:
    """`_forget_deleted` lists `families/` to find a family whose file is
    gone. The listing is on the LOOP thread, so a raise there is the
    outage again by another path."""
    bench.look()
    families = bench.state_root / "families"
    families.chmod(0o000)
    try:
        bench.look()
    finally:
        families.chmod(RESTORED)

    assert bench.published() == FAMILIES


def test_a_state_root_that_cannot_be_listed_deletes_nothing(
    bench: Bench, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The dangerous reading of the same fault. "I cannot list the
    families" must never become "no family has state", which is the
    branch that removes credentials."""
    bench.look()
    deleted: list[str] = []

    def record(config: object, actors: object, name: str) -> bool:
        deleted.append(name)
        return True

    def refuse(self: Path) -> object:
        raise PermissionError(13, "Permission denied", str(self))

    monkeypatch.setattr(loop_module, "_delete_one", record)
    monkeypatch.setattr(Path, "iterdir", refuse)
    bench.look()

    assert deleted == []


# --- the one guard at the loop's top -----------------------------------------


def refuse_revision(root: Path) -> str:
    """A registry read that raises, which is what a re-moded bind mount
    does. `revision_of` belongs to `agent_family`, so nothing in this
    package can map it where it happens."""
    raise PermissionError(13, "Permission denied", str(root))


def test_a_raising_registry_read_does_not_kill_serve(
    bench: Bench, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """`revision_of` and `load_registry` belong to `agent_family`, and the
    registry is an NFS-less bind mount that a root visit can re-mode. The
    guard at the loop's top is what covers every read this file does not
    name one by one."""
    monkeypatch.setattr(loop_module, "revision_of", refuse_revision)
    control = loop_module.FixedControl(looks=4)

    with caplog.at_level(logging.ERROR, logger="caregiver.loop"):
        loop_module.serve(bench.config(), bench.actors(), control)

    assert control.left == 0
    assert len(caplog.records) >= 1


def test_one_bad_path_writes_one_traceback_and_not_one_per_pass(
    bench: Bench, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A bad directory can last hours. At one look every two seconds, five
    hours is 9,000 tracebacks. One per distinct error, and then a counted
    line."""
    monkeypatch.setattr(loop_module, "revision_of", refuse_revision)

    with caplog.at_level(logging.ERROR, logger="caregiver.loop"):
        loop_module.serve(bench.config(), bench.actors(), loop_module.FixedControl(looks=20))

    traced = [one for one in caplog.records if one.exc_info is not None]
    assert len(traced) == 1
    assert len(caplog.records) == 1


def test_a_second_distinct_error_gets_its_own_traceback(
    bench: Bench, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Distinct, not "any". Two broken paths are two things to fix, and a
    guard that said the second one nothing would hide half the fault."""
    seen: list[int] = []

    def alternate(root: Path) -> str:
        seen.append(1)
        which = "first" if len(seen) % 2 else "second"
        raise PermissionError(13, "Permission denied", f"{root}/{which}")

    monkeypatch.setattr(loop_module, "revision_of", alternate)

    with caplog.at_level(logging.ERROR, logger="caregiver.loop"):
        loop_module.serve(bench.config(), bench.actors(), loop_module.FixedControl(looks=20))

    traced = [one for one in caplog.records if one.exc_info is not None]
    assert len(traced) == 2


def test_a_family_pass_has_a_guard_of_its_own(
    bench: Bench, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Item 1 rule 2 asks this claim to be CHECKED. `Passes._run` catches
    every `Exception`, so a raising pass releases that family's name and
    the other family still converges. `_pass.run` catches it first and
    adds a backoff."""
    real = loop_module.reconcile_family

    def refuse(registry: object, name: str, **kwargs: object) -> object:
        if name == FAMILIES[0]:
            raise ValueError("a pass raised something that is not an OSError")

        return real(registry, name, **kwargs)  # pyright: ignore[reportCallIssue, reportArgumentType]

    monkeypatch.setattr(loop_module, "reconcile_family", refuse)

    started = bench.look()

    assert started == FAMILIES
    assert bench.published() == (FAMILIES[1],)
    assert bench.state.of(FAMILIES[0]).backoff is not None


# --- each step of a look ends in a handler of the loop ------------------------

#: An error that no step names in a handler of its own.
UNNAMED: Final = ValueError("a step raised an error that it does not name")


def refuse(*args: object, **kwargs: object) -> object:
    """A stand-in for a step. It raises an error that is not an `OSError`."""
    del args, kwargs
    raise UNNAMED


def test_a_dispatch_that_raises_costs_no_other_family(
    bench: Bench, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The guard of a dispatch is per family. A guard that names `OSError`
    alone lets another error end the sweep, and then no later family
    passes and the tail of the look does not run."""
    bench.look()
    first, second = FAMILIES
    # `first` waits out a backoff, so its dispatch restamps and starts no
    # pass.
    bench.state.note(first, backoff=Backoff(next_at=time.monotonic() + 3600.0, delay_s=5.0))
    monkeypatch.setattr(loop_module, "restamp_status", refuse)

    started = bench.look(heartbeat_s=0.0)

    assert started == (second,)
    assert bench.state.revision == revision_of(bench.registry_root)


def test_a_dispatch_that_raises_is_said_once(
    bench: Bench, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    bench.look()
    first = FAMILIES[0]
    bench.state.note(first, backoff=Backoff(next_at=time.monotonic() + 3600.0, delay_s=5.0))
    monkeypatch.setattr(loop_module, "restamp_status", refuse)

    with caplog.at_level(logging.ERROR, logger="caregiver.loop"):
        for _ in range(3):
            bench.look(heartbeat_s=0.0)

    traced = [one for one in caplog.records if one.exc_info is not None]
    assert [one.getMessage().split(":")[0] for one in traced] == [first]


def test_an_mcp_pass_that_raises_does_not_end_the_look(
    bench: Bench, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The MCP pass runs after the families. An error that leaves it must
    not leave the look, and the look must still record the revision. A look
    with no revision reads every later tick as an edit, and an edit clears
    each create backoff."""
    monkeypatch.setattr(loop_module, "mcp_pass", refuse)

    assert bench.look() == FAMILIES
    assert bench.state.revision == revision_of(bench.registry_root)
    assert bench.look() == ()


def test_a_listing_that_raises_does_not_stop_the_mcp_pass(
    bench: Bench, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two steps follow the families. One that raises must not stop the
    other."""
    monkeypatch.setattr(loop_module, "_families_with_state", refuse)

    assert bench.look() == FAMILIES
    assert bench.requests() != []
    assert bench.state.revision == revision_of(bench.registry_root)


@pytest.mark.parametrize("step", ["delete_family", "remove_timers", "read_applied"])
def test_a_delete_that_raises_ends_in_the_handler_of_the_loop(
    bench: Bench, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, step: str
) -> None:
    """A family file that is gone starts a delete. Each step of the delete
    ends in one handler: one traceback for the error, the state of the
    family kept, and the other family in sync."""
    bench.look()
    gone, kept = FAMILIES
    shutil.rmtree(bench.registry_root / "families" / gone)
    monkeypatch.setattr(loop_module, step, refuse)

    with caplog.at_level(logging.ERROR, logger="caregiver.loop"):
        for _ in range(3):
            bench.look(heartbeat_s=0.0)

    traced = [one for one in caplog.records if one.exc_info is not None]
    assert [one.getMessage().split(";")[0] for one in traced] == [
        f"{gone}: delete: ValueError: {UNNAMED}"
    ]
    assert paths.family_dir(bench.state_root, gone).is_dir()
    assert published(bench.state_root, kept)["state"] == FamilyState.IN_SYNC


class FailingDelete:
    """A stand-in for the delete of one family. It records each try."""

    def __init__(self) -> None:
        self.tries: list[str] = []

    def __call__(self, config: LoopConfig, actors: Actors, name: str) -> None:
        del config, actors
        self.tries.append(name)
        raise UNNAMED


def test_a_delete_that_raises_waits_before_the_next_try(
    bench: Bench, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A delete that fails gets the wait of a pass that raised. A delete at
    each look calls each client every two seconds, and an error text that
    moves at each try fills the complaint ledger."""
    bench.look()
    gone = FAMILIES[0]
    shutil.rmtree(bench.registry_root / "families" / gone)
    delete = FailingDelete()
    monkeypatch.setattr(loop_module, "_delete_one", delete)

    for _ in range(3):
        bench.look(heartbeat_s=0.0)

    assert delete.tries == [gone]
    backoff = bench.state.of(gone).backoff
    assert backoff is not None
    assert backoff.delay_s == BACKOFF_FIRST_S


def test_a_delete_starts_again_when_its_wait_ends(
    bench: Bench, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The second failure doubles the wait, as a second failed create does."""
    bench.look()
    gone = FAMILIES[0]
    shutil.rmtree(bench.registry_root / "families" / gone)
    delete = FailingDelete()
    monkeypatch.setattr(loop_module, "_delete_one", delete)
    bench.look(heartbeat_s=0.0)

    bench.state.note(gone, backoff=Backoff(next_at=time.monotonic(), delay_s=BACKOFF_FIRST_S))
    bench.look(heartbeat_s=0.0)

    assert delete.tries == [gone, gone]
    backoff = bench.state.of(gone).backoff
    assert backoff is not None
    assert backoff.delay_s == BACKOFF_FIRST_S * 2


def test_a_registry_edit_starts_a_waiting_delete_again(
    bench: Bench, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An edit clears each backoff, because the edit can be the repair."""
    bench.look()
    gone = FAMILIES[0]
    shutil.rmtree(bench.registry_root / "families" / gone)
    delete = FailingDelete()
    monkeypatch.setattr(loop_module, "_delete_one", delete)
    bench.look(heartbeat_s=0.0)

    write_registry(bench.registry_root, name="notes")
    bench.look(heartbeat_s=0.0)

    assert delete.tries == [gone, gone]


def test_a_waiting_delete_does_not_open_the_gate_of_the_loop(
    bench: Bench, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A family with no file has no document to keep fresh. While its
    delete waits, a look within one heartbeat reads no registry."""
    bench.look()
    gone = FAMILIES[0]
    shutil.rmtree(bench.registry_root / "families" / gone)
    monkeypatch.setattr(loop_module, "_delete_one", FailingDelete())
    bench.look()
    loads: list[Path] = []
    real_load = loop_module.load_registry

    def counted(root: Path, host: object) -> object:
        loads.append(root)
        return real_load(root, host)  # type: ignore[arg-type]

    monkeypatch.setattr(loop_module, "load_registry", counted)

    for _ in range(3):
        bench.look()

    assert loads == []


def test_a_delete_that_works_after_a_wait_forgets_the_family(
    bench: Bench, monkeypatch: pytest.MonkeyPatch
) -> None:
    bench.look()
    gone = FAMILIES[0]
    shutil.rmtree(bench.registry_root / "families" / gone)
    with monkeypatch.context() as patched:
        patched.setattr(loop_module, "_delete_one", FailingDelete())
        bench.look(heartbeat_s=0.0)

    bench.state.note(gone, backoff=Backoff(next_at=time.monotonic(), delay_s=BACKOFF_FIRST_S))
    bench.look(heartbeat_s=0.0)

    assert not paths.family_dir(bench.state_root, gone).exists()
    assert bench.state.of(gone).backoff is None
