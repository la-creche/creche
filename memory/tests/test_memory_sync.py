"""The sync reads the right folders, waits for a quiet vault, and keeps going
until a failed sync finds a dead graph."""

import asyncio
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from creche_memory.graph import Dead, GraphCheck
from creche_memory.sync import (
    Debounce,
    Outcome,
    Syncer,
    Trigger,
    ingest_folders,
    vault_stamp,
)
from memory_graph_helpers import GRAPH_NAME, kill_graph, ladybug_check, make_graph

QUIET_S = 30.0


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    for folder in (
        "docs/creche",
        "finance/memories",
        "house/ha/flows",
        "house/scrum",
        "house/memories",
        "house/05 - Insurance",
        "profile/memories",
        ".obsidian",
    ):
        (root / folder).mkdir(parents=True)

    (root / "Welcome.md").write_text("root note", encoding="utf-8")
    (root / "house" / "README.md").write_text("house index", encoding="utf-8")
    (root / "house" / "memories" / "printer.md").write_text("Epson", encoding="utf-8")
    (root / "house" / "ha" / "flows" / "f.md").write_text("flow", encoding="utf-8")
    (root / "house" / "scrum" / "STATE.md").write_text("state", encoding="utf-8")
    (root / "profile" / "memories" / "dentist.md").write_text("Oak St.", encoding="utf-8")

    return root


@pytest.fixture
def usable(tmp_path_factory: pytest.TempPathFactory) -> GraphCheck:
    """The check of a real graph database that opens. It lives outside
    `tmp_path`, which one test uses as its vault."""
    return ladybug_check(make_graph(tmp_path_factory.mktemp("usable") / GRAPH_NAME))


@pytest.fixture
def dead(tmp_path_factory: pytest.TempPathFactory) -> GraphCheck:
    """The check of a real graph database in the state of 2026-11-04."""
    graph_file = make_graph(tmp_path_factory.mktemp("dead") / GRAPH_NAME)
    kill_graph(graph_file)

    return ladybug_check(graph_file)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class Ingests:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.fail = False

    async def __call__(self, folders: list[str]) -> None:
        self.calls.append(folders)
        if self.fail:
            raise ConnectionError("http://secret.test/ refused")


def _step(syncer: Syncer) -> Outcome:
    return asyncio.run(syncer.step())


def test_the_store_reads_every_folder_but_the_generated_ones(vault: Path, tmp_path: Path) -> None:
    (vault / "linked").symlink_to(tmp_path)

    assert ingest_folders(vault) == [
        "docs",
        "finance",
        "house/05 - Insurance",
        "house/memories",
        "profile",
    ]


def test_the_stamp_moves_for_an_edit_a_new_note_and_a_removal(vault: Path) -> None:
    folders = ingest_folders(vault)
    first = vault_stamp(vault, folders)
    note = vault / "profile" / "memories" / "dentist.md"

    note.write_text("Oak St., second floor", encoding="utf-8")
    edited = vault_stamp(vault, folders)
    (vault / "docs" / "creche" / "new.md").write_text("new", encoding="utf-8")
    added = vault_stamp(vault, folders)
    note.unlink()
    removed = vault_stamp(vault, folders)

    assert len({first, edited, added, removed}) == 4


def test_the_stamp_ignores_the_generated_folders(vault: Path) -> None:
    folders = ingest_folders(vault)
    before = vault_stamp(vault, folders)

    (vault / "house" / "scrum" / "STATE.md").write_text("state, an hour later", encoding="utf-8")
    (vault / "house" / "ha" / "flows" / "g.md").write_text("flow", encoding="utf-8")

    assert vault_stamp(vault, folders) == before


def test_a_poll_waits_for_a_quiet_vault_and_a_write_does_not() -> None:
    debounce = Debounce(QUIET_S)

    assert not debounce.due("a", 0.0, Trigger.POLL)
    assert not debounce.due("b", 20.0, Trigger.POLL)
    assert not debounce.due("b", 49.0, Trigger.POLL)
    assert debounce.due("b", 50.0, Trigger.POLL)

    debounce.done("b")

    assert not debounce.due("b", 500.0, Trigger.POLL)
    assert not debounce.due("b", 500.0, Trigger.WRITE)
    assert debounce.due("c", 501.0, Trigger.WRITE)


def test_the_syncer_syncs_once_the_vault_is_quiet(vault: Path, usable: GraphCheck) -> None:
    clock, ingests = Clock(), Ingests()
    syncer = Syncer(vault, ingests, usable, quiet_s=QUIET_S, clock=clock)

    assert _step(syncer) is Outcome.WAITING
    assert not syncer.ready.is_set()

    clock.now += QUIET_S
    assert _step(syncer) is Outcome.SYNCED
    assert ingests.calls == [ingest_folders(vault)]
    assert syncer.ready.is_set()

    clock.now += QUIET_S
    assert _step(syncer) is Outcome.UNCHANGED
    assert len(ingests.calls) == 1


def test_a_write_syncs_at_the_next_check(vault: Path, usable: GraphCheck) -> None:
    clock, ingests = Clock(), Ingests()
    syncer = Syncer(vault, ingests, usable, quiet_s=QUIET_S, clock=clock)

    (vault / "profile" / "memories" / "new.md").write_text("new", encoding="utf-8")
    syncer.request()

    assert _step(syncer) is Outcome.SYNCED
    assert _step(syncer) is Outcome.UNCHANGED


def test_a_failed_sync_prints_no_message_and_tries_again(
    vault: Path, capsys: pytest.CaptureFixture[str], usable: GraphCheck
) -> None:
    clock, ingests = Clock(), Ingests()
    syncer = Syncer(vault, ingests, usable, quiet_s=0.0, clock=clock)
    ingests.fail = True

    assert _step(syncer) is Outcome.FAILED
    assert "secret.test" not in capsys.readouterr().err
    assert not syncer.ready.is_set()

    ingests.fail = False
    assert _step(syncer) is Outcome.SYNCED


def test_a_failed_sync_with_a_dead_graph_returns_the_dead_graph(
    vault: Path, capsys: pytest.CaptureFixture[str], dead: GraphCheck
) -> None:
    ingests = Ingests()
    ingests.fail = True
    syncer = Syncer(vault, ingests, dead, quiet_s=0.0, clock=Clock())

    state = _step(syncer)

    assert isinstance(state, Dead)
    assert state.side_files[0] == f"{GRAPH_NAME}.wal.checkpoint"
    assert "secret.test" not in capsys.readouterr().err
    assert not syncer.ready.is_set()


def test_a_dead_graph_ends_run_and_sets_the_stop(vault: Path, dead: GraphCheck) -> None:
    ingests = Ingests()
    ingests.fail = True
    syncer = Syncer(vault, ingests, dead, poll_s=60.0, quiet_s=0.0, clock=Clock())
    stop = threading.Event()

    started = time.monotonic()
    state = asyncio.run(syncer.run(stop))

    assert isinstance(state, Dead)
    assert stop.is_set()
    assert len(ingests.calls) == 1
    assert time.monotonic() - started < 5.0


def test_a_successful_sync_does_not_check_the_graph(vault: Path, dead: GraphCheck) -> None:
    syncer = Syncer(vault, Ingests(), dead, quiet_s=0.0, clock=Clock())

    assert _step(syncer) is Outcome.SYNCED


def test_an_error_of_the_check_ends_run_and_sets_the_stop(vault: Path) -> None:
    async def broken() -> Dead:
        raise TimeoutError("the worker did not answer")

    ingests = Ingests()
    ingests.fail = True
    syncer = Syncer(vault, ingests, broken, poll_s=60.0, quiet_s=0.0, clock=Clock())
    stop = threading.Event()

    with pytest.raises(TimeoutError):
        asyncio.run(syncer.run(stop))

    assert stop.is_set()


class SlowIngests(Ingests):
    """Each sync moves the clock on by 125 seconds."""

    def __init__(self, clock: Clock) -> None:
        super().__init__()
        self.clock = clock

    async def __call__(self, folders: list[str]) -> None:
        self.clock.now += 125.0
        await super().__call__(folders)


def test_each_sync_logs_its_start_and_end_and_the_first_says_search_is_ready(
    vault: Path, capsys: pytest.CaptureFixture[str], usable: GraphCheck
) -> None:
    clock = Clock()
    syncer = Syncer(vault, SlowIngests(clock), usable, quiet_s=0.0, clock=clock)
    folders = len(ingest_folders(vault))

    assert _step(syncer) is Outcome.SYNCED
    assert capsys.readouterr().err.splitlines() == [
        f"memory: sync started: {folders} folders",
        "memory: sync done in 125 s; search is ready",
    ]

    (vault / "docs" / "new.md").write_text("new\n")
    assert _step(syncer) is Outcome.SYNCED
    assert capsys.readouterr().err.splitlines()[-1] == "memory: sync done in 125 s"


def test_a_failed_sync_logs_its_time_and_the_type_only(
    vault: Path, capsys: pytest.CaptureFixture[str], usable: GraphCheck
) -> None:
    clock = Clock()
    ingests = SlowIngests(clock)
    ingests.fail = True
    syncer = Syncer(vault, ingests, usable, quiet_s=0.0, clock=clock)

    assert _step(syncer) is Outcome.FAILED
    assert capsys.readouterr().err.splitlines()[-1] == (
        "memory: sync failed after 125 s: ConnectionError"
    )


def test_a_vault_with_no_folder_to_read_is_never_synced(tmp_path: Path, usable: GraphCheck) -> None:
    ingests = Ingests()
    syncer = Syncer(tmp_path, ingests, usable, quiet_s=0.0, clock=Clock())

    assert _step(syncer) is Outcome.NOTHING_TO_READ
    assert ingests.calls == []


#: Builds the connector with the arguments of `cognee_ingest`, in a process
#: of its own, so a change of its keywords in a later Cognee fails here.
PROBE = """
import sys
from pathlib import Path
from creche_memory.store import StoreSettings, configure
from creche_memory.sync import RESOURCE, ingest_folders

root = Path(sys.argv[1])
configure(StoreSettings(
    store_root=root / "store",
    llm_base_url="http://llm.test:4000",
    llm_key="sk-test",
    embedding_base_url="http://embed.test:8085",
    tokenizer_cache=root / "hf",
))

from cognee.tasks.ingestion.connectors import obsidian_source

vault = root / "vault"
source = obsidian_source(vault_path=vault, resource_name=RESOURCE, folders=ingest_folders(vault))
print(type(source).__name__)
"""


def test_the_connector_takes_the_arguments_of_the_sync(vault: Path) -> None:
    done = subprocess.run(
        [sys.executable, "-c", PROBE, str(vault.parent)],
        capture_output=True,
        text=True,
        timeout=180,
        cwd=vault.parent,
        env={**os.environ, "HOME": str(vault.parent)},
    )

    assert done.returncode == 0, done.stderr[-2000:]
    assert done.stdout.strip().splitlines()[-1] == "DltResource"


def test_a_write_to_a_skipped_folder_neither_syncs_nor_spins(
    vault: Path, usable: GraphCheck
) -> None:
    clock, ingests = Clock(), Ingests()
    syncer = Syncer(vault, ingests, usable, poll_s=60.0, quiet_s=0.0, clock=clock)
    assert _step(syncer) is Outcome.SYNCED

    (vault / "house" / "scrum" / "STATE.md").write_text("an hour later", encoding="utf-8")
    syncer.request()
    assert _step(syncer) is Outcome.UNCHANGED

    checks: list[float] = []
    counted = Syncer(
        vault, ingests, usable, poll_s=60.0, quiet_s=0.0, clock=lambda: checks.append(0) or 0
    )
    counted.request()
    stop = threading.Event()
    threading.Timer(0.3, stop.set).start()
    threading.Timer(0.3, counted.request).start()
    asyncio.run(counted.run(stop))

    assert len(ingests.calls) == 2
    assert len(checks) <= 3


def test_wake_ends_the_wait_of_run_at_once(vault: Path, usable: GraphCheck) -> None:
    syncer = Syncer(vault, Ingests(), usable, poll_s=60.0, quiet_s=60.0, clock=Clock())
    stop = threading.Event()

    def end() -> None:
        stop.set()
        syncer.wake()

    threading.Timer(0.2, end).start()
    started = time.monotonic()
    asyncio.run(syncer.run(stop))

    assert time.monotonic() - started < 5.0
