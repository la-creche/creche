"""The approval hook's credentials have one home, and every reader names it.

Nothing writes `/srv/agents/state/materializer/env` any more. Three values
that file carried are still needed: `LITELLM_MASTER_KEY`, `APPROVAL_URL`
and `APPROVAL_TOKEN`.
They live at `/srv/agents/state/rework/hooks.env` now, which the site's
own cutover script writes.

This module pins the SHAPE of that move, across the scripts of this tree
that read it, so another reader cannot quietly reach for the dead path
again:

1. every reader names the new file,
2. every reader that still reaches the old path does it through
   `envfile_hook_value`, which puts a DEPRECATED line in the journal, and
3. nobody sources either file with `set -a`, which would export every other
   daemon secret in it into the process and its children.

`bin/tests/test_env_upsert.sh` holds the helpers themselves.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parents[1]

HOOKS_ENV = "/srv/agents/state/rework/hooks.env"
DEPRECATED_ENV = "/srv/agents/state/materializer/env"

#: The scripts that read one of the three values at run time. The script
#: that writes the file is the site's, and so is its test.
READERS = ("rework-watchdog.sh", "sbx-drift-check.sh")


def _body(name: str) -> str:
    return (BIN / name).read_text(encoding="utf-8")


@pytest.mark.parametrize("name", READERS)
def test_every_script_names_the_new_file(name: str) -> None:
    assert HOOKS_ENV in _body(name)


@pytest.mark.parametrize("name", READERS)
def test_nothing_sources_an_env_file_wholesale(name: str) -> None:
    """`set -a && . <file>` reads two values out of a file that holds every
    other daemon secret, and exports the lot into curl and every other
    child (bin/AGENTS.md §Secrets)."""
    for line in _body(name).splitlines():
        if line.strip().startswith("#"):
            continue

        assert "set -a" not in line, line


@pytest.mark.parametrize("name", READERS)
def test_the_old_path_is_reached_only_through_the_fallback(name: str) -> None:
    """One reader, one deprecation notice. A second way to read that file
    would be a way to read it in silence."""
    body = _body(name)
    for line in body.splitlines():
        if line.strip().startswith("#") or "DEPRECATED_ENV=" in line:
            continue

        assert DEPRECATED_ENV not in line, line

    assert "envfile_hook_value" in body


def test_the_helpers_carry_their_own_shell_test() -> None:
    """`bin/tests/test_env_upsert.sh` is the regression test for the
    env wipe, and for the two readers as well. It needs no
    host, so it runs here too rather than only by hand."""
    done = subprocess.run(
        ["bash", str(BIN / "tests" / "test_env_upsert.sh")],
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert done.returncode == 0, done.stdout + done.stderr
    assert "GATE: PASS" in done.stdout
