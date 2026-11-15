"""The setup scripts name the units of creche-release, and install none.

A `host` release of creche-release links `creche-docs-mirror`,
`creche-code-corpus-sync` and `creche-nodered-flows-sync` (contract 06 §8.3).
A hand-installed copy of the old name makes that release refuse to link, so
neither script may install or name an old unit, nor run the flowsync console
script that the release does not install.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

BIN: Final = Path(__file__).resolve().parent.parent
SCRIPTS: Final = ("nr-setup.sh", "setup-docs-mirror.sh")


def _code(name: str) -> list[str]:
    lines = (BIN / name).read_text(encoding="utf-8").splitlines()
    return [line for line in lines if not line.lstrip().startswith("#")]


def test_no_script_installs_a_unit_file() -> None:
    for name in SCRIPTS:
        for line in _code(name):
            assert not ("install -m" in line and ".service" in line), (name, line)
            assert "/systemd/" not in line, (name, line)


def test_no_script_runs_code_that_names_an_old_unit() -> None:
    for name in SCRIPTS:
        for line in _code(name):
            assert not re.search(
                r"(?<!creche-)(docs-mirror|code-corpus-sync|nodered-flows-sync)\.(service|timer)",
                line,
            ), (name, line)
    assert "SYNC_UNIT=creche-nodered-flows-sync" in _code("nr-setup.sh")
    assert "UNITS=(creche-code-corpus-sync creche-docs-mirror)" in _code("setup-docs-mirror.sh")


def test_nr_setup_runs_flowsync_as_the_unit_does() -> None:
    text = "\n".join(_code("nr-setup.sh"))

    assert "/opt/components/host/flowsync/src" in text
    assert "python3 -m agent_flowsync.noderedsync" in text
    assert ".venv/bin/nodered-flows-sync" not in text
