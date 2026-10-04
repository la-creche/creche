"""Fixtures of the process-level suite.

Every test here is marked `slow`, as every test of the other integration
suites is. The mark is applied once, here, so no scenario can forget it.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from proc_services import describe_table

_HERE = Path(__file__).resolve().parent


def pytest_report_header() -> list[str]:
    """Say which command each service runs, so a reader knows what was judged."""
    return describe_table()


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Contract: `uv run pytest integration/proc -m slow` runs the suite."""
    for item in items:
        if _HERE in item.path.parents:
            item.add_marker(pytest.mark.slow)
