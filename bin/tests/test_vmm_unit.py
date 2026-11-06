"""The decided lines of `systemd/creche-vmm.service`.

`systemd-analyze verify` in the systemd-proof job checks the syntax of the
unit only. It accepts a wrong value, a missing program and a wrong mode
word. `test_bin_path_refs` matches no `/opt/components/` path. So each
decided line of the unit gets a row here, with the row that decides it.
The stop timeout must also equal `UNIT_STOP_TIMEOUT` of the vmm, or
systemd sends SIGKILL in the middle of `SHUTDOWN_WAIT` (gap G16, decision
CH56).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SYSTEMD = ROOT / "systemd"
UNIT = SYSTEMD / "creche-vmm.service"
SHUTDOWN_RS = ROOT / "rust" / "crates" / "vmm" / "src" / "shutdown.rs"
MAIN_RS = ROOT / "rust" / "crates" / "vmm" / "src" / "main.rs"
DEFAULT_ENV = ROOT / "env" / "src" / "creche_env" / "default.env"
STATE_ROOT_DEFAULT = "VMM_STATE_ROOT=${AGENT_OPERATOR_HOME}/.local/state/"

# Each row: the key, every value the unit gives it in order, the decision.
DECIDED = [
    ("ExecStart", ["/opt/components/vmm/bin/vmm serve"], "CH7-5"),
    ("Restart", ["always"], "the rework restart policy"),
    ("RestartPreventExitStatus", ["78"], "owner item 86"),
    ("RuntimeDirectory", ["vmm"], "CH20, G5 option A"),
    ("RuntimeDirectoryMode", ["0700"], "CH20"),
    ("RuntimeDirectoryPreserve", [], "accept.rs refuses an existing vmm.sock"),
    ("StateDirectoryMode", ["0700"], "CH50 rule 11"),
    ("LimitCORE", ["0"], "CH8-5, G27 option A"),
    ("KillMode", ["mixed"], "G16, CH56 rule 2"),
    ("NoNewPrivileges", ["yes"], "systemd/AGENTS.md"),
    ("BindsTo", [], "CH19 (4), CH20"),
    ("Requires", [], "CH19 (4), CH20"),
    (
        "EnvironmentFile",
        ["/srv/agents/state/creche/vmm.env", "/etc/creche/site.env"],
        "CH59 rule 1, the site file last",
    ),
]


def _settings(text: str, key: str) -> list[str]:
    """Every value one key takes, in order, comments skipped."""
    found: list[str] = []
    for line in text.splitlines():
        name, sign, value = line.partition("=")
        if sign and not name.lstrip().startswith("#") and name.strip() == key:
            found.append(value.strip())

    return found


@pytest.mark.parametrize(("key", "values", "decision"), DECIDED)
def test_each_decided_line_holds_its_value(key: str, values: list[str], decision: str) -> None:
    assert _settings(UNIT.read_text(), key) == values, decision


def test_the_state_dir_is_the_default_state_root() -> None:
    """CH50 rule 11: `StateDirectory=` is the dir that VMM_STATE_ROOT names."""
    lines = DEFAULT_ENV.read_text().splitlines()
    prefix = STATE_ROOT_DEFAULT
    names = [line.removeprefix(prefix) for line in lines if line.startswith(prefix)]

    assert len(names) == 1
    assert _settings(UNIT.read_text(), "StateDirectory") == names


def test_the_stop_timeout_is_unit_stop_timeout() -> None:
    """G16, CH56 rules 2 and 3: the vmm plans its stop inside this bound."""
    pattern = r"const UNIT_STOP_TIMEOUT: Duration = Duration::from_secs\((\d+)\);"
    found = re.findall(pattern, SHUTDOWN_RS.read_text())

    assert len(found) == 1
    assert _settings(UNIT.read_text(), "TimeoutStopSec") == found


def test_serve_is_a_mode_of_the_vmm() -> None:
    """A wrong mode word exits 78 at the first start on the host."""
    usage = re.findall(r'const USAGE: &str = "usage: (.*)";', MAIN_RS.read_text())

    assert len(usage) == 1
    assert "vmm serve" in [mode.strip() for mode in usage[0].split("|")]


def test_no_client_unit_needs_the_vmm() -> None:
    """CH19 (4), CH20: a client orders with `After=` at most."""
    for unit in SYSTEMD.glob("*.service"):
        text = unit.read_text()
        for key in ("Requires", "BindsTo", "Requisite", "PartOf"):
            values = _settings(text, key)
            assert not any("creche-vmm.service" in value for value in values), unit.name
