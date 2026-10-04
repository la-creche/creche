"""What a background task says when it ends with an error.

The event loop keeps the error of a task that nothing awaits. It reports the
error only when it collects the task, and not in this service's log. A task
that ends in silence is a loop that stopped, and nothing says so.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

_LOG = logging.getLogger("attendance")


def report_failure(task: asyncio.Task[Any]) -> None:
    """A done-callback. Log the error of a task that did not end clean.

    A cancelled task is not a failure: `close()` cancels each task that the
    service owns. Reading the error here also marks it as retrieved.
    """
    if task.cancelled():
        return

    error = task.exception()

    if error is None:
        return

    _LOG.error("task %s ended with an error", task.get_name(), exc_info=error)
