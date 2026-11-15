"""Cognee's records reach the journal as one plain line each, and its file
log keeps the whole traceback."""

import json
import logging
import subprocess
import sys
from pathlib import Path

from creche_memory.logs import MAX_EVENT_CHARS, OneLine

#: The text of a graph worker's error, as Cognee's harness builds it: many
#: lines, the remote traceback, and an address.
WORKER_ERROR = (
    "Storage exception: Checksum verification failed, the WAL file is corrupted.\n"
    "[NOTE] Remote subprocess traceback:\n"
    "Traceback (most recent call last):\n"
    '  File "kuzu_worker.py", line 78, in _open_database\n'
    "http://secret.test/v1"
)

#: Configures the store, swaps Cognee's console handler, then logs as
#: Cognee's orphan cleanup does when the graph worker fails
#: (`cognee/tasks/ingestion/resolve_dlt_sources.py`). A process of its own,
#: because Cognee sets up its logging at its first import.
PROBE = """
import json, logging, sys
from pathlib import Path
from creche_memory.logs import plain_console
from creche_memory.store import StoreSettings, configure

root = Path(sys.argv[1])
configure(StoreSettings(
    store_root=root / "store",
    llm_base_url="http://llm.test:4000",
    llm_key="sk-test-not-a-real-key",
    embedding_base_url="http://embed.test:8085",
    tokenizer_cache=root / "hf",
))
plain_console()
print("--- swapped", file=sys.stderr, flush=True)

from cognee.shared.logging_utils import get_logger

logger = get_logger("resolve_dlt_sources")
logger.info("Pipeline item completed")
try:
    raise RuntimeError(sys.argv[2])
except RuntimeError:
    logger.warning("Failed to delete orphaned dlt row data_id=%s, skipping.", "d-1", exc_info=True)

logging.getLogger("cognee_db_workers.harness").error("worker gone\\nsecond line")
files = [h for h in logging.getLogger().handlers if isinstance(h, logging.FileHandler)]
print(json.dumps([h.baseFilename for h in files]))
"""


def test_a_cognee_record_reaches_the_journal_as_one_plain_line(tmp_path: Path) -> None:
    done = subprocess.run(
        [sys.executable, "-c", PROBE, str(tmp_path), WORKER_ERROR],
        capture_output=True,
        text=True,
        timeout=180,
        cwd=tmp_path,
    )
    assert done.returncode == 0, done.stderr[-2000:]

    after = done.stderr.split("--- swapped\n", 1)[1]

    assert after.splitlines() == [
        "cognee warning: Failed to delete orphaned dlt row data_id=d-1, skipping. "
        "(RuntimeError) [resolve_dlt_sources]",
        "cognee error: worker gone second line [cognee_db_workers.harness]",
    ]
    assert "\x1b" not in after

    # The file log still holds the whole traceback and the INFO record.
    (log_file,) = json.loads(done.stdout.strip().splitlines()[-1])
    text = Path(log_file).read_text(encoding="utf-8")

    assert Path(log_file).parent == tmp_path / "store" / "logs"
    assert "Pipeline item completed" in text
    assert "Remote subprocess traceback" in text


def _record(message: object, level: int = logging.ERROR) -> logging.LogRecord:
    return logging.LogRecord("cognee.x", level, __file__, 1, message, None, None)


def test_a_long_event_is_cut_and_its_spaces_folded() -> None:
    line = OneLine().format(_record({"event": "a  b\n" + "c" * 1000, "logger": "pipeline"}))

    event = line.removeprefix("cognee error: ").removesuffix(" [pipeline]")
    assert event.startswith("a b c")
    assert event.endswith("...")
    assert len(event) == MAX_EVENT_CHARS


def test_the_line_names_the_type_and_never_the_message_of_the_error() -> None:
    try:
        raise ConnectionError("http://secret.test/ refused")
    except ConnectionError:
        record = logging.LogRecord(
            "litellm", logging.WARNING, __file__, 1, "call failed", None, sys.exc_info()
        )

    assert OneLine().format(record) == "cognee warning: call failed (ConnectionError) [litellm]"
