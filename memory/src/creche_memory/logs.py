"""The lines that Cognee writes to the journal.

Cognee 1.6.8 sets up the root logger at its first import
(`cognee.shared.logging_utils.setup_logging`). Its console handler writes
each record at INFO and above to stderr. It forces colour codes, writes an
empty line before each record, and draws a rich traceback box for each
record with an exception. Each line of a box is one journal line. Many
records carry an exception message that holds the whole traceback of
Cognee's graph worker too.

On 2026-11-08 a sync that failed every 8 to 9 minutes wrote 3.5 million
journal lines in one day this way, about 20,000 for each failed sync: one
box for each document of the vault (`memory/AGENTS.md`, the journal).

`plain_console` swaps that handler for one that writes one plain line for
each record at WARNING and above. The line holds the level, the event and
the type of the exception, with no colour and no traceback. Cognee's file
handler stays as it is: each record, with its whole traceback, still goes
to the rotating log files in `<store>/logs`."""

import importlib
import logging
import sys
from typing import cast

#: The lowest level of a Cognee record that reaches the journal. The file
#: log keeps INFO.
CONSOLE_LEVEL = logging.WARNING

#: The longest event text that a journal line keeps.
MAX_EVENT_CHARS = 300

#: The cut mark at the end of a cut event text.
CUT = "..."


def _one_line(text: str) -> str:
    flat = " ".join(text.split())
    if len(flat) <= MAX_EVENT_CHARS:
        return flat

    return flat[: MAX_EVENT_CHARS - len(CUT)] + CUT


class OneLine(logging.Formatter):
    """One plain line for each record.

    A record of structlog, which Cognee uses, carries its event dict as
    `record.msg`. Cognee's own processor adds `exception_type` to it. A
    record of the standard library carries its exception in `exc_info`.
    The line names only the type of the exception: its message can hold the
    URL of a service, or a whole traceback of the graph worker."""

    def format(self, record: logging.LogRecord) -> str:
        message: object = record.msg
        fields: dict[str, object] = {}
        if isinstance(message, dict):
            fields = cast(dict[str, object], message)
            event = str(fields.get("event", ""))
        else:
            event = record.getMessage()

        source = str(fields.get("logger", record.name))
        error = fields.get("exception_type")
        if error is None and record.exc_info is not None and record.exc_info[0] is not None:
            error = record.exc_info[0].__name__

        cause = "" if error is None else f" ({error})"

        return f"cognee {record.levelname.lower()}: {_one_line(event)}{cause} [{source}]"


def console_handlers(root: logging.Logger) -> list[logging.Handler]:
    """The handlers of `root` that write to a stream and not to a file."""
    return [
        handler
        for handler in root.handlers
        if isinstance(handler, logging.StreamHandler)
        and not isinstance(handler, logging.FileHandler)
    ]


def plain_console() -> None:
    """Import Cognee, then swap its console handler for a plain one. Call it
    after `configure`, before the first sync. The records of the import
    itself come before the swap, so the three lines of each start keep
    Cognee's form.

    A Cognee whose import leaves any other number of console handlers stops
    the start: the swap could then miss the handler that floods."""
    importlib.import_module("cognee")

    root = logging.getLogger()
    found = console_handlers(root)
    if len(found) != 1:
        raise RuntimeError(f"Cognee left {len(found)} console handlers on the root logger, not 1")

    root.removeHandler(found[0])
    plain = logging.StreamHandler(sys.stderr)
    plain.setLevel(CONSOLE_LEVEL)
    plain.setFormatter(OneLine())
    root.addHandler(plain)
