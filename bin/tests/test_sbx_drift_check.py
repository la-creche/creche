"""`bin/sbx-drift-check.sh` reads the one allowed LAN host from the site file.

Every sandbox may reach LiteLLM, the PEP and TEI on the host's LAN address,
at any port. That address is the site's (`/etc/agent-control/site.env`), so
a rule that allows it is no drift, a rule that allows any other LAN host is,
and a host with no site file stops before it judges anything.

`sbx` is a binstub. The script needs bash 4 (`mapfile`), so the two cases
that reach the rule check run on Linux only, which is where CI and the host
run it. The refusal comes first and runs everywhere.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Final

import pytest

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
SCRIPT: Final = REPO_ROOT / "bin" / "sbx-drift-check.sh"

STUB_MODE: Final = 0o755

#: `sbx policy inspect` holds no global allow, and `sbx policy ls --wide`
#: prints whatever `DRIFT_WIDE` holds.
SBX_STUB: Final = """#!/bin/sh
case "$*" in
  *"ls --wide"*) printf '%s\\n' "$DRIFT_WIDE" ;;
esac
exit 0
"""


def _has_mapfile() -> bool:
    bash = shutil.which("bash")
    if bash is None:
        return False

    done = subprocess.run([bash, "-c", "type mapfile"], capture_output=True, check=False)
    return done.returncode == 0


needs_bash4 = pytest.mark.skipif(not _has_mapfile(), reason="the script needs bash 4 (mapfile)")


def _run(tmp_path: Path, wide: str, **overrides: str) -> subprocess.CompletedProcess[str]:
    stubs = tmp_path / "stubs"
    stubs.mkdir(exist_ok=True)
    sbx = stubs / "sbx"
    sbx.write_text(SBX_STUB, encoding="utf-8")
    sbx.chmod(STUB_MODE)

    env = dict(os.environ)
    env["SBX_DRIFT_TEST_PREFIX"] = str(tmp_path / "host")
    env["HOME"] = str(tmp_path)
    env["PATH"] = f"{stubs}{os.pathsep}{env['PATH']}"
    env["DRIFT_WIDE"] = wide
    env.update(overrides)

    return subprocess.run(
        ["bash", str(SCRIPT)], capture_output=True, text=True, env=env, timeout=60
    )


def test_no_site_file_stops_with_one_line_naming_it(tmp_path: Path) -> None:
    absent = tmp_path / "absent.env"

    done = _run(tmp_path, "", AGENT_SITE_FILE=str(absent))

    assert done.returncode != 0
    assert done.stderr.count("\n") == 1, done.stderr
    assert "AGENT_LAN_ADDRESS" in done.stderr
    assert str(absent) in done.stderr


@needs_bash4
def test_an_allow_to_the_sites_address_is_no_drift(tmp_path: Path) -> None:
    address = os.environ["AGENT_LAN_ADDRESS"]

    done = _run(tmp_path, f"sandbox:chat-s1  allow {address}:4000")

    assert done.returncode == 0, done.stdout + done.stderr


@needs_bash4
def test_an_allow_to_another_lan_host_is_drift(tmp_path: Path) -> None:
    done = _run(tmp_path, "sandbox:chat-s1  allow 192.0.2.99:4000")

    assert done.returncode == 1, done.stdout + done.stderr
    assert "192.0.2.99" in done.stdout
