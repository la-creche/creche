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
from creche_memory.graph import Dead, GraphCheck, GraphReopen, LeftOpen, Reopened
from creche_memory.sync import (
    BACKOFF_MAX_S,
    POLL_S,
    Debounce,
    Outcome,
    Syncer,
    Trigger,
    backoff_s,
    ingest_folders,
    vault_stamp,
)
from memory_graph_helpers import GRAPH_NAME, kill_graph, ladybug_check, make_graph
from memory_model_helpers import UNFIT, Answer, ingest_through_cognee

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
        #: The items whose graph extraction fails in each sync.
        self.unfit = 0

    async def __call__(self, folders: list[str]) -> int:
        self.calls.append(folders)
        if self.fail:
            raise ConnectionError("http://secret.test/ refused")

        return self.unfit


class Reopens:
    """Counts the reopens, and answers each one with `answer`."""

    def __init__(self, answer: GraphReopen | None = None) -> None:
        self.calls = 0
        self.answer: GraphReopen = Reopened(1.5) if answer is None else answer

    async def __call__(self) -> GraphReopen:
        self.calls += 1
        return self.answer


def _step(syncer: Syncer) -> Outcome | Dead:
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
    syncer = Syncer(vault, ingests, usable, Reopens(), quiet_s=QUIET_S, clock=clock)

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
    syncer = Syncer(vault, ingests, usable, Reopens(), quiet_s=QUIET_S, clock=clock)

    (vault / "profile" / "memories" / "new.md").write_text("new", encoding="utf-8")
    syncer.request()

    assert _step(syncer) is Outcome.SYNCED
    assert _step(syncer) is Outcome.UNCHANGED


def test_a_failed_sync_prints_no_message_and_tries_again(
    vault: Path, capsys: pytest.CaptureFixture[str], usable: GraphCheck
) -> None:
    clock, ingests = Clock(), Ingests()
    syncer = Syncer(vault, ingests, usable, Reopens(), quiet_s=0.0, clock=clock)
    ingests.fail = True

    assert _step(syncer) is Outcome.FAILED
    assert "secret.test" not in capsys.readouterr().err
    assert not syncer.ready.is_set()

    ingests.fail = False
    clock.now += POLL_S
    assert _step(syncer) is Outcome.SYNCED


def test_a_failed_sync_with_a_dead_graph_returns_the_dead_graph(
    vault: Path, capsys: pytest.CaptureFixture[str], dead: GraphCheck
) -> None:
    ingests = Ingests()
    ingests.fail = True
    syncer = Syncer(vault, ingests, dead, Reopens(), quiet_s=0.0, clock=Clock())

    state = _step(syncer)

    assert isinstance(state, Dead)
    assert state.side_files[0] == f"{GRAPH_NAME}.wal.checkpoint"
    err = capsys.readouterr().err
    assert "secret.test" not in err
    # No next sync follows a dead graph, so the line names no wait.
    assert err.splitlines()[-1].endswith("Failed syncs in a row: 1.")
    assert not syncer.ready.is_set()


def test_a_dead_graph_ends_run_and_sets_the_stop(vault: Path, dead: GraphCheck) -> None:
    ingests = Ingests()
    ingests.fail = True
    syncer = Syncer(vault, ingests, dead, Reopens(), poll_s=60.0, quiet_s=0.0, clock=Clock())
    stop = threading.Event()

    started = time.monotonic()
    state = asyncio.run(syncer.run(stop))

    assert isinstance(state, Dead)
    assert stop.is_set()
    assert len(ingests.calls) == 1
    assert time.monotonic() - started < 5.0


def test_a_successful_sync_does_not_check_the_graph(vault: Path, dead: GraphCheck) -> None:
    syncer = Syncer(vault, Ingests(), dead, Reopens(), quiet_s=0.0, clock=Clock())

    assert _step(syncer) is Outcome.SYNCED


def test_a_good_sync_and_a_failed_one_each_reopen_the_graph(
    vault: Path, usable: GraphCheck
) -> None:
    ingests, reopens = Ingests(), Reopens()
    syncer = Syncer(vault, ingests, usable, reopens, quiet_s=0.0, clock=Clock())

    assert _step(syncer) is Outcome.SYNCED
    assert reopens.calls == 1

    ingests.fail = True
    (vault / "docs" / "new.md").write_text("new\n")
    assert _step(syncer) is Outcome.FAILED
    assert reopens.calls == 2


def test_a_dead_graph_after_a_failed_sync_is_not_reopened(vault: Path, dead: GraphCheck) -> None:
    ingests, reopens = Ingests(), Reopens()
    ingests.fail = True
    syncer = Syncer(vault, ingests, dead, reopens, quiet_s=0.0, clock=Clock())

    assert isinstance(_step(syncer), Dead)
    assert reopens.calls == 0


def test_a_reopen_that_finds_a_dead_graph_ends_run_and_sets_the_stop(
    vault: Path, dead: GraphCheck
) -> None:
    # The sync ends well. The new open of the reopen does not open.
    state = asyncio.run(dead())
    assert isinstance(state, Dead)
    syncer = Syncer(vault, Ingests(), dead, Reopens(state), poll_s=60.0, quiet_s=0.0, clock=Clock())
    stop = threading.Event()

    assert asyncio.run(syncer.run(stop)) == state
    assert stop.is_set()


def test_a_reopen_that_writes_nothing_prints_why_and_the_loop_goes_on(
    vault: Path, capsys: pytest.CaptureFixture[str], usable: GraphCheck
) -> None:
    left = LeftOpen(f"a checkpoint did not end ({GRAPH_NAME}.shadow)")
    syncer = Syncer(vault, Ingests(), usable, Reopens(left), quiet_s=0.0, clock=Clock())

    assert _step(syncer) is Outcome.SYNCED
    assert capsys.readouterr().err.splitlines()[-1] == (
        "memory: the reopen of the graph database wrote nothing: "
        f"a checkpoint did not end ({GRAPH_NAME}.shadow). The next sync uses the same open."
    )


def test_an_error_of_the_check_ends_run_and_sets_the_stop(vault: Path) -> None:
    async def broken() -> Dead:
        raise TimeoutError("the worker did not answer")

    ingests = Ingests()
    ingests.fail = True
    syncer = Syncer(vault, ingests, broken, Reopens(), poll_s=60.0, quiet_s=0.0, clock=Clock())
    stop = threading.Event()

    with pytest.raises(TimeoutError):
        asyncio.run(syncer.run(stop))

    assert stop.is_set()


class SlowIngests(Ingests):
    """Each sync moves the clock on by 125 seconds."""

    def __init__(self, clock: Clock) -> None:
        super().__init__()
        self.clock = clock

    async def __call__(self, folders: list[str]) -> int:
        self.clock.now += 125.0
        return await super().__call__(folders)


def test_each_sync_logs_its_start_and_end_and_the_first_says_search_is_ready(
    vault: Path, capsys: pytest.CaptureFixture[str], usable: GraphCheck
) -> None:
    clock = Clock()
    syncer = Syncer(vault, SlowIngests(clock), usable, Reopens(), quiet_s=0.0, clock=clock)
    folders = len(ingest_folders(vault))

    assert _step(syncer) is Outcome.SYNCED
    assert capsys.readouterr().err.splitlines() == [
        f"memory: sync started: {folders} folders",
        "memory: sync done in 125 s; search is ready",
        "memory: The graph database reopened in 1.5 s.",
    ]

    (vault / "docs" / "new.md").write_text("new\n")
    assert _step(syncer) is Outcome.SYNCED
    assert capsys.readouterr().err.splitlines()[-2] == "memory: sync done in 125 s"


def test_a_failed_sync_logs_its_time_and_the_type_only(
    vault: Path, capsys: pytest.CaptureFixture[str], usable: GraphCheck
) -> None:
    clock = Clock()
    ingests = SlowIngests(clock)
    ingests.fail = True
    syncer = Syncer(vault, ingests, usable, Reopens(), quiet_s=0.0, clock=clock)

    assert _step(syncer) is Outcome.FAILED
    assert capsys.readouterr().err.splitlines()[-2:] == [
        "memory: sync failed after 125 s: ConnectionError. Failed syncs in a row: 1. "
        "The next sync waits at least 30 s.",
        "memory: The graph database reopened in 1.5 s.",
    ]


def test_a_partial_sync_opens_search_and_waits_for_the_next_change(
    vault: Path, capsys: pytest.CaptureFixture[str], usable: GraphCheck
) -> None:
    clock = Clock()
    ingests, reopens = SlowIngests(clock), Reopens()
    ingests.fail = True
    syncer = Syncer(vault, ingests, usable, reopens, quiet_s=0.0, clock=clock)
    assert _step(syncer) is Outcome.FAILED

    ingests.fail, ingests.unfit = False, 2
    clock.now += POLL_S
    assert _step(syncer) is Outcome.PARTIAL
    assert syncer.ready.is_set()
    assert reopens.calls == 2
    assert capsys.readouterr().err.splitlines()[-2] == (
        "memory: sync done in 125 s after 1 failed syncs; 2 note(s) or PDF(s) failed graph "
        "extraction and sync again at the next change; search is ready"
    )

    # The failed items do not sync again at each check, and no wait follows.
    clock.now += POLL_S
    assert _step(syncer) is Outcome.UNCHANGED
    assert len(ingests.calls) == 2

    (vault / "docs" / "new.md").write_text("new\n")
    assert _step(syncer) is Outcome.PARTIAL
    assert len(ingests.calls) == 3


def test_a_sync_through_cognee_counts_the_note_that_fits_no_schema(tmp_path: Path) -> None:
    notes = tmp_path / "vault" / "notes"
    notes.mkdir(parents=True)
    (notes / "a.md").write_text("# A\n\nAlpha talks to beta.\n")
    (notes / "b.md").write_text("# B\n\nBeta builds the delta.\n")
    (notes / "unfit.md").write_text(f"# Unfit\n\n{UNFIT}: alpha in dense table rows.\n")

    # The other notes complete, and search finds the text of all three. The
    # sync compacts the vector store: without it, a table keeps 3 fragments.
    assert ingest_through_cognee(tmp_path, Answer.FIT) == [
        "failed: 1",
        "found: notes/a.md,notes/b.md,notes/unfit.md",
        "fragments: 1",
    ]


def test_a_sync_through_cognee_compacts_the_store_during_the_run(tmp_path: Path) -> None:
    notes = tmp_path / "vault" / "notes"
    notes.mkdir(parents=True)
    for n in range(4):
        (notes / f"{n}.md").write_text(f"# Note {n}\n\nAlpha talks to beta {n}.\n")

    # A pass every 0.2 s runs beside the upserts, and the run completes.
    assert ingest_through_cognee(tmp_path, Answer.FIT, compact_every_s=0.2) == [
        "failed: 0",
        "found: notes/0.md,notes/1.md,notes/2.md,notes/3.md",
        "fragments: 1",
        "compactions: some",
    ]


@pytest.mark.parametrize(
    ("answer", "cause"),
    [(Answer.AUTH, "AuthenticationError"), (Answer.BUDGET, "LLMPaymentRequiredError")],
)
def test_an_auth_or_budget_error_of_each_item_still_fails_the_sync(
    tmp_path: Path, answer: Answer, cause: str
) -> None:
    notes = tmp_path / "vault" / "notes"
    notes.mkdir(parents=True)
    (notes / "a.md").write_text("# A\n\nAlpha talks to beta.\n")

    lines = ingest_through_cognee(tmp_path, answer)

    assert lines[0] == f"raised: CognifyFailedError {cause}"


def test_the_wait_doubles_with_each_failed_sync_up_to_the_cap() -> None:
    waits = [backoff_s(failures, POLL_S) for failures in range(1, 9)]

    assert waits == [30.0, 60.0, 120.0, 240.0, 480.0, 960.0, BACKOFF_MAX_S, BACKOFF_MAX_S]
    assert backoff_s(10_000, POLL_S) == BACKOFF_MAX_S


def test_failed_syncs_in_a_row_back_off_and_a_good_one_resets_the_count(
    vault: Path, capsys: pytest.CaptureFixture[str], usable: GraphCheck
) -> None:
    clock, ingests = Clock(), Ingests()
    ingests.fail = True
    syncer = Syncer(vault, ingests, usable, Reopens(), quiet_s=0.0, clock=clock)

    assert _step(syncer) is Outcome.FAILED
    clock.now += POLL_S
    assert _step(syncer) is Outcome.FAILED
    assert (
        capsys.readouterr()
        .err.splitlines()[-2]
        .endswith("Failed syncs in a row: 2. The next sync waits at least 60 s.")
    )

    # Each check in the wait syncs nothing.
    clock.now += 59.0
    assert _step(syncer) is Outcome.BACKING_OFF
    assert len(ingests.calls) == 2

    ingests.fail = False
    clock.now += 1.0
    assert _step(syncer) is Outcome.SYNCED
    assert capsys.readouterr().err.splitlines()[-2] == (
        "memory: sync done in 0 s after 2 failed syncs; search is ready"
    )

    # The count starts again after a good sync.
    ingests.fail = True
    (vault / "docs" / "new.md").write_text("new\n")
    assert _step(syncer) is Outcome.FAILED
    assert (
        capsys.readouterr()
        .err.splitlines()[-2]
        .endswith("Failed syncs in a row: 1. The next sync waits at least 30 s.")
    )


def test_a_write_in_the_wait_syncs_after_the_wait(vault: Path, usable: GraphCheck) -> None:
    clock, ingests = Clock(), Ingests()
    ingests.fail = True
    syncer = Syncer(vault, ingests, usable, Reopens(), quiet_s=QUIET_S, clock=clock)
    syncer.request()
    assert _step(syncer) is Outcome.FAILED

    ingests.fail = False
    syncer.request()
    assert _step(syncer) is Outcome.BACKING_OFF

    # The quiet time has not passed, so only the kept write syncs now.
    clock.now += POLL_S
    (vault / "docs" / "new.md").write_text("new\n")
    assert _step(syncer) is Outcome.SYNCED


def test_a_vault_with_no_folder_to_read_is_never_synced(tmp_path: Path, usable: GraphCheck) -> None:
    ingests = Ingests()
    syncer = Syncer(tmp_path, ingests, usable, Reopens(), quiet_s=0.0, clock=Clock())

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
    syncer = Syncer(vault, ingests, usable, Reopens(), poll_s=60.0, quiet_s=0.0, clock=clock)
    assert _step(syncer) is Outcome.SYNCED

    (vault / "house" / "scrum" / "STATE.md").write_text("an hour later", encoding="utf-8")
    syncer.request()
    assert _step(syncer) is Outcome.UNCHANGED

    checks: list[float] = []
    counted = Syncer(
        vault,
        ingests,
        usable,
        Reopens(),
        poll_s=60.0,
        quiet_s=0.0,
        clock=lambda: checks.append(0) or 0,
    )
    counted.request()
    stop = threading.Event()

    # One timer, the stop first: with two timers, a request that came first
    # started one more check, and the count was 4 in about 1 run in 8.
    def end() -> None:
        stop.set()
        counted.request()

    threading.Timer(0.3, end).start()
    asyncio.run(counted.run(stop))

    assert len(ingests.calls) == 2
    assert len(checks) <= 3


def test_wake_ends_the_wait_of_run_at_once(vault: Path, usable: GraphCheck) -> None:
    syncer = Syncer(vault, Ingests(), usable, Reopens(), poll_s=60.0, quiet_s=60.0, clock=Clock())
    stop = threading.Event()

    def end() -> None:
        stop.set()
        syncer.wake()

    threading.Timer(0.2, end).start()
    started = time.monotonic()
    asyncio.run(syncer.run(stop))

    assert time.monotonic() - started < 5.0
