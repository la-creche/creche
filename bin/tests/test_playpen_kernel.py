"""`playpen/bin/playpen-kernel`, with a `curl` stub and no network (CH7-3).

The real fetch runs in CI through `playpen/bin/playpen-ci`. Here the stub
serves other bytes, so the pin check must stop the script, and the pin
must equal the one the KVM smoke job checks.
"""

from __future__ import annotations

import re
import stat
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
KERNEL = REPO_ROOT / "playpen" / "bin" / "playpen-kernel"
SMOKE = REPO_ROOT / "bin" / "vm-smoke-setup.sh"

#: `curl`: writes `not a kernel` to the `--output` path.
CURL_STUB = """#!/usr/bin/env bash
while [[ "$1" != --output ]]; do shift; done
printf 'not a kernel\\n' > "$2"
"""


def _pin(text: str, pattern: str) -> str:
    found = re.search(pattern, text, re.MULTILINE)
    assert found is not None
    return found.group(1)


def test_other_bytes_stop_the_script_and_leave_no_kernel(tmp_path: Path) -> None:
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    curl = stubs / "curl"
    curl.write_text(CURL_STUB, encoding="utf-8")
    curl.chmod(curl.stat().st_mode | stat.S_IXUSR)
    out = tmp_path / "out"
    out.mkdir()

    done = subprocess.run(
        ["bash", str(KERNEL), str(out)],
        env={"PATH": f"{stubs}:/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=False,
    )

    assert done.returncode == 1
    assert done.stdout == ""
    assert "playpen-kernel: vmlinux-x86_64 has the SHA-256 " in done.stderr
    assert list(out.iterdir()) == []


def test_the_pin_is_the_one_the_kvm_smoke_job_checks() -> None:
    """Fact 53: one kernel, one release, one digest, in both scripts."""
    kernel = KERNEL.read_text(encoding="utf-8")
    smoke = SMOKE.read_text(encoding="utf-8")

    assert _pin(kernel, r"^SHA256=([0-9a-f]{64})$") == _pin(smoke, r"^([0-9a-f]{64})  vmlinux$")
    release = _pin(smoke, r"^KERNEL_URL=(\S+)$")
    assert f'URL="{release}/$NAME"' in kernel
