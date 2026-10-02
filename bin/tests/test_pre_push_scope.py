"""What a push tests: the suites of the packages it touches.

`bin/tests/test_pre_push_select.sh` holds the cases: one package, several,
a path in no package (the full suite), a new branch counted from its
merge-base, a deleted branch (nothing), a docs-only push (the tests marked
docs). It needs no host, so it runs here
too rather than only by hand, and CI runs it with everything else.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

SHELL_TEST = Path(__file__).resolve().parent / "test_pre_push_select.sh"


def test_the_push_scope_holds() -> None:
    done = subprocess.run(
        ["bash", str(SHELL_TEST)],
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert done.returncode == 0, done.stdout + done.stderr
    assert "GATE: PASS" in done.stdout
