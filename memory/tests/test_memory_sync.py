"""The sync reads the right folders, waits for a quiet vault, and keeps going."""

import asyncio
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from creche_memory.sync import (
    Debounce,
    Outcome,
    Syncer,
    Trigger,
    ingest_folders,
    vault_stamp,
)

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


def test_the_syncer_syncs_once_the_vault_is_quiet(vault: Path) -> None:
    clock, ingests = Clock(), Ingests()
    syncer = Syncer(vault, ingests, quiet_s=QUIET_S, clock=clock)

    assert _step(syncer) is Outcome.WAITING
    assert not syncer.ready.is_set()

    clock.now += QUIET_S
    assert _step(syncer) is Outcome.SYNCED
    assert ingests.calls == [ingest_folders(vault)]
    assert syncer.ready.is_set()

    clock.now += QUIET_S
    assert _step(syncer) is Outcome.UNCHANGED
    assert len(ingests.calls) == 1


def test_a_write_syncs_at_the_next_check(vault: Path) -> None:
    clock, ingests = Clock(), Ingests()
    syncer = Syncer(vault, ingests, quiet_s=QUIET_S, clock=clock)

    (vault / "profile" / "memories" / "new.md").write_text("new", encoding="utf-8")
    syncer.request()

    assert _step(syncer) is Outcome.SYNCED
    assert _step(syncer) is Outcome.UNCHANGED


def test_a_failed_sync_prints_no_message_and_tries_again(
    vault: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    clock, ingests = Clock(), Ingests()
    syncer = Syncer(vault, ingests, quiet_s=0.0, clock=clock)
    ingests.fail = True

    assert _step(syncer) is Outcome.FAILED
    assert "secret.test" not in capsys.readouterr().err
    assert not syncer.ready.is_set()

    ingests.fail = False
    assert _step(syncer) is Outcome.SYNCED


def test_a_vault_with_no_folder_to_read_is_never_synced(tmp_path: Path) -> None:
    ingests = Ingests()
    syncer = Syncer(tmp_path, ingests, quiet_s=0.0, clock=Clock())

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


def test_a_write_to_a_skipped_folder_neither_syncs_nor_spins(vault: Path) -> None:
    clock, ingests = Clock(), Ingests()
    syncer = Syncer(vault, ingests, poll_s=60.0, quiet_s=0.0, clock=clock)
    assert _step(syncer) is Outcome.SYNCED

    (vault / "house" / "scrum" / "STATE.md").write_text("an hour later", encoding="utf-8")
    syncer.request()
    assert _step(syncer) is Outcome.UNCHANGED

    checks: list[float] = []
    counted = Syncer(vault, ingests, poll_s=60.0, quiet_s=0.0, clock=lambda: checks.append(0) or 0)
    counted.request()
    stop = threading.Event()
    threading.Timer(0.3, stop.set).start()
    threading.Timer(0.3, counted.request).start()
    asyncio.run(counted.run(stop))

    assert len(ingests.calls) == 2
    assert len(checks) <= 3
