"""The graph check tells a dead graph database from one that opens."""

import asyncio
import os
from pathlib import Path

import pytest
from cognee_db_workers.harness import SubprocessTransportError
from creche_memory.graph import (
    MAX_ERROR_CHARS,
    SIDE_SUFFIXES,
    Dead,
    NotAVerdict,
    Usable,
    graph_state,
    side_files,
)
from memory_graph_helpers import (
    FILL_ROWS,
    GRAPH_NAME,
    count_rows,
    fill_through_cognee,
    held_open,
    kill_graph,
    ladybug_check,
    leave_recoverable,
    make_graph,
)

CHECKSUM_ERROR = "Checksum verification failed, the WAL file is corrupted"


@pytest.fixture
def graph_file(tmp_path: Path) -> Path:
    return make_graph(tmp_path / "databases" / GRAPH_NAME)


def test_side_files_names_each_file_that_is_there(tmp_path: Path) -> None:
    graph_file = tmp_path / GRAPH_NAME
    graph_file.write_bytes(b"")

    assert side_files(graph_file) == ()

    for suffix in SIDE_SUFFIXES:
        (tmp_path / (GRAPH_NAME + suffix)).write_bytes(b"")
    (tmp_path / "other.wal").write_bytes(b"")

    assert side_files(graph_file) == tuple(GRAPH_NAME + suffix for suffix in SIDE_SUFFIXES)


def test_a_cleanly_closed_graph_is_usable_and_has_no_side_file(graph_file: Path) -> None:
    assert side_files(graph_file) == ()
    assert asyncio.run(ladybug_check(graph_file)()) == Usable()


def test_a_checkpoint_that_stopped_early_is_usable_and_the_open_clears_it(
    graph_file: Path,
) -> None:
    leave_recoverable(graph_file)
    assert len(side_files(graph_file)) == 3

    assert asyncio.run(ladybug_check(graph_file)()) == Usable()
    assert side_files(graph_file) == ()


def test_a_frozen_wal_that_fails_its_checksum_is_dead(graph_file: Path) -> None:
    kill_graph(graph_file)

    state = asyncio.run(ladybug_check(graph_file)())

    assert isinstance(state, Dead)
    assert state.graph_file == graph_file
    assert CHECKSUM_ERROR in state.error
    assert state.side_files == (
        f"{GRAPH_NAME}.wal.checkpoint",
        f"{GRAPH_NAME}.shadow",
        f"{GRAPH_NAME}.checkpoint.intent.lock",
        f"{GRAPH_NAME}.checkpoint.apply.lock",
    )
    # The check opens again and fails the same way: nothing repairs it.
    assert asyncio.run(ladybug_check(graph_file)()) == state


def test_the_report_is_one_line_with_the_cause_and_the_recovery(graph_file: Path) -> None:
    kill_graph(graph_file)
    state = asyncio.run(ladybug_check(graph_file)())
    assert isinstance(state, Dead)

    report = state.report()

    assert "\n" not in report
    assert ";" not in report
    assert report.startswith(f"The graph database {graph_file} does not open: ")
    assert CHECKSUM_ERROR in report
    assert f"Files beside it: {GRAPH_NAME}.wal.checkpoint, " in report
    assert report.endswith(
        f"To recover, set aside {graph_file.parent}. Then start the unit. "
        "The first sync rebuilds the store from the vault."
    )


def test_an_error_that_is_not_a_runtime_error_goes_to_the_caller(graph_file: Path) -> None:
    async def slow() -> None:
        raise TimeoutError("the worker did not answer")

    with pytest.raises(TimeoutError):
        asyncio.run(graph_state(graph_file, slow))


@pytest.mark.parametrize(
    "message",
    [
        "Subprocess init timed out after 60.0s",
        "Subprocess exited unexpectedly (exit code -9)",
        "Reader thread did not stop; refusing to respawn",
    ],
)
def test_an_error_of_the_worker_process_is_not_a_verdict(graph_file: Path, message: str) -> None:
    async def broken() -> None:
        raise SubprocessTransportError(message)

    with pytest.raises(SubprocessTransportError):
        asyncio.run(graph_state(graph_file, broken))


def test_a_lock_that_another_opener_holds_is_not_a_verdict(graph_file: Path) -> None:
    with held_open(graph_file), pytest.raises(NotAVerdict, match="Could not set lock on file"):
        asyncio.run(ladybug_check(graph_file)())

    assert asyncio.run(ladybug_check(graph_file)()) == Usable()


def test_a_long_error_is_cut_to_one_line(graph_file: Path) -> None:
    async def failing() -> None:
        raise RuntimeError("IO exception: Cannot open file\n" + "x" * 1000)

    state = asyncio.run(graph_state(graph_file, failing))

    assert isinstance(state, Dead)
    assert "\n" not in state.error
    assert state.error.startswith("IO exception: Cannot open file x")
    assert len(state.error) == MAX_ERROR_CHARS


def test_an_index_error_of_the_open_is_dead(graph_file: Path) -> None:
    # pybind gives ladybug's `std::out_of_range` as an `IndexError`. A
    # recovered checkpoint with one write after it opens with this error
    # on every later try.
    async def broken() -> None:
        raise IndexError("unordered_map::at: key not found")

    state = asyncio.run(graph_state(graph_file, broken))

    assert isinstance(state, Dead)
    assert state.error == "unordered_map::at: key not found"


@pytest.mark.parametrize(
    "text",
    [
        "No space left on device",
        "Disk quota exceeded",
        "Permission denied",
        "Operation not permitted",
        "Read-only file system",
        "Too many open files",
        "Input/output error",
    ],
)
def test_an_error_of_the_host_is_not_a_verdict(graph_file: Path, text: str) -> None:
    message = f"IO exception: Cannot open file {graph_file}: {text}"

    async def full() -> None:
        raise RuntimeError(message)

    with pytest.raises(NotAVerdict) as raised:
        asyncio.run(graph_state(graph_file, full))

    assert str(raised.value) == message


@pytest.mark.skipif(os.geteuid() == 0, reason="root opens a file of any mode")
def test_a_graph_that_the_service_may_not_write_is_not_a_verdict(graph_file: Path) -> None:
    graph_file.chmod(0o444)
    try:
        with pytest.raises(NotAVerdict, match="Permission denied"):
            asyncio.run(ladybug_check(graph_file)())
    finally:
        graph_file.chmod(0o644)

    assert asyncio.run(ladybug_check(graph_file)()) == Usable()


def test_the_report_names_the_files_from_before_the_open(graph_file: Path) -> None:
    wal = graph_file.with_name(GRAPH_NAME + ".wal")
    wal.write_bytes(b"x")

    async def removes_the_wal() -> None:
        # Cognee's open removes the `.wal` file when the error names the WAL.
        wal.unlink()
        raise RuntimeError(CHECKSUM_ERROR)

    state = asyncio.run(graph_state(graph_file, removes_the_wal))

    assert isinstance(state, Dead)
    assert state.side_files == (f"{GRAPH_NAME}.wal",)


def test_the_check_after_a_failed_checkpoint_keeps_every_row(tmp_path: Path) -> None:
    store = tmp_path / "store"
    lines = fill_through_cognee(store)

    # The open engine answers. The check does not close it: a close after
    # the failed checkpoint makes a file that does not open. The close of
    # the stop sees the shadow of the failed checkpoint and writes nothing.
    assert lines == [
        "Usable()",
        "full: True",
        "Usable()",
        f"LeftOpen(reason='a checkpoint did not end ({GRAPH_NAME}.shadow)')",
    ]
    # The child exits with no close. A new process opens the file with
    # every row.
    graph_file = store / "system" / "databases" / GRAPH_NAME
    assert count_rows(graph_file, "F") == FILL_ROWS
