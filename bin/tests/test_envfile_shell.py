"""`bin/tests/test_env_upsert.sh` is the regression test for the env-file
wipe and for the readers of `bin/lib/envfile.sh`. It needs no host, so the
suite runs it.

The rule that each reader of `hooks.env` names the new file moved to
creche-release with the two scripts it held (`tests/test_bin_hook_env.py`
there).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

BIN = Path(__file__).resolve().parents[1]


def test_the_helpers_carry_their_own_shell_test() -> None:
    done = subprocess.run(
        ["bash", str(BIN / "tests" / "test_env_upsert.sh")],
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert done.returncode == 0, done.stdout + done.stderr
    assert "GATE: PASS" in done.stdout
