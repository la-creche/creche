"""The graph check tells a dead graph database from one that opens."""

import asyncio
from pathlib import Path

import pytest
from cognee_db_workers.harness import SubprocessTransportError
from creche_memory.graph import (
    MAX_ERROR_CHARS,
    SIDE_SUFFIXES,
    Dead,
    Usable,
    graph_state,
    side_files,
)
from memory_graph_helpers import (
    GRAPH_NAME,
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
    with held_open(graph_file), pytest.raises(RuntimeError, match="Could not set lock on file"):
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
