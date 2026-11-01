"""`bin/sbx-drift-check.sh` reads the one allowed LAN host from the site file.

Every sandbox may reach LiteLLM, the PEP and TEI on the host's LAN address,
at any port. That address is the site's (`/etc/creche/site.env`), so
a rule that allows it is no drift, a rule that allows any other LAN host is,
and a host with no site file stops before it judges anything.

The script reads the rules from `sbx policy ls --json`. sbx v0.43 and v0.48
give that listing the same fields. A listing that does not read is drift.

`sbx` is a binstub. The script needs bash 4 (`mapfile`), so the cases
that reach the rule check run on Linux only, which is where CI and the host
run it. The refusal comes first and runs everywhere.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Final

import pytest

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
SCRIPT: Final = REPO_ROOT / "bin" / "sbx-drift-check.sh"

STUB_MODE: Final = 0o755

#: `sbx policy ls --json` prints whatever `DRIFT_JSON` holds, and each call
#: ends with `DRIFT_STATUS`.
SBX_STUB: Final = """#!/bin/sh
case "$*" in
  *"policy ls --json"*) printf '%s\\n' "$DRIFT_JSON" ;;
esac
exit "${DRIFT_STATUS:-0}"
"""

SANDBOX: Final = "sandbox:chat-s1"
KIT_HOST: Final = "openrouter.ai"

#: The rules of sbx v0.48 on a host with no sandbox. Each one has fields
#: that sbx v0.43 does not give: `provenance`, `actions`, `policy_name`.
V048_DEFAULTS: Final = (
    {
        "id": "default-fs-read-allow-all",
        "policy_id": "local-policy",
        "scope": "global",
        "applies_to": "all",
        "resource_type": "filesystem:read",
        "decision": "allow",
        "resources": ["**"],
        "provenance": {"created_via": "default"},
        "actions": ["fs:mount:read"],
    },
    {
        "id": "default-deny-all",
        "policy_name": "default-deny-all",
        "scope": "global",
        "applies_to": "all",
        "resource_type": "network",
        "decision": "deny",
        "resources": ["**"],
        "actions": ["net:connect:tcp", "net:connect:udp"],
    },
)


def _has_mapfile() -> bool:
    bash = shutil.which("bash")
    if bash is None:
        return False

    done = subprocess.run([bash, "-c", "type mapfile"], capture_output=True, check=False)
    return done.returncode == 0


needs_bash4 = pytest.mark.skipif(not _has_mapfile(), reason="the script needs bash 4 (mapfile)")


def _rule(applies_to: str, decision: str, *resources: str) -> dict[str, object]:
    """One network rule, with the fields of sbx v0.43."""
    return {
        "applies_to": applies_to,
        "resource_type": "network",
        "decision": decision,
        "resources": list(resources),
    }


def _listing(*rules: dict[str, object]) -> str:
    return json.dumps({"rules": [*V048_DEFAULTS, *rules]})


def _run(tmp_path: Path, listing: str, **overrides: str) -> subprocess.CompletedProcess[str]:
    stubs = tmp_path / "stubs"
    stubs.mkdir(exist_ok=True)
    sbx = stubs / "sbx"
    sbx.write_text(SBX_STUB, encoding="utf-8")
    sbx.chmod(STUB_MODE)

    env = dict(os.environ)
    env["SBX_DRIFT_TEST_PREFIX"] = str(tmp_path / "host")
    env["HOME"] = str(tmp_path)
    env["PATH"] = f"{stubs}{os.pathsep}{env['PATH']}"
    env["DRIFT_JSON"] = listing
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
def test_the_rules_of_v048_with_no_sandbox_are_no_drift(tmp_path: Path) -> None:
    done = _run(tmp_path, _listing())

    assert done.returncode == 0, done.stdout + done.stderr
    assert "global policy clean" in done.stdout


@needs_bash4
def test_an_allow_to_the_sites_address_is_no_drift(tmp_path: Path) -> None:
    address = os.environ["AGENT_LAN_ADDRESS"]

    done = _run(tmp_path, _listing(_rule(SANDBOX, "allow", f"{address}:4000")))

    assert done.returncode == 0, done.stdout + done.stderr


@needs_bash4
def test_an_allow_to_another_lan_host_is_drift(tmp_path: Path) -> None:
    done = _run(tmp_path, _listing(_rule(SANDBOX, "allow", "192.0.2.99:4000")))

    assert done.returncode == 1, done.stdout + done.stderr
    assert "192.0.2.99" in done.stdout


@needs_bash4
def test_an_allow_of_every_host_is_drift(tmp_path: Path) -> None:
    done = _run(tmp_path, _listing(_rule(SANDBOX, "allow", "**")))

    assert done.returncode == 1, done.stdout + done.stderr
    assert f"{SANDBOX} allow **" in done.stdout


@needs_bash4
def test_a_network_allow_that_names_no_sandbox_is_drift(tmp_path: Path) -> None:
    address = os.environ["AGENT_LAN_ADDRESS"]

    done = _run(tmp_path, _listing(_rule("all", "allow", f"{address}:4000")))

    assert done.returncode == 1, done.stdout + done.stderr
    assert "global network allow" in done.stdout
    assert f"all allow {address}:4000" in done.stdout


@needs_bash4
def test_the_kit_host_with_a_deny_of_its_sandbox_is_no_drift(tmp_path: Path) -> None:
    rules = _listing(_rule(SANDBOX, "allow", KIT_HOST), _rule(SANDBOX, "deny", KIT_HOST))

    done = _run(tmp_path, rules)

    assert done.returncode == 0, done.stdout + done.stderr


@needs_bash4
def test_the_kit_host_with_a_deny_of_another_sandbox_is_drift(tmp_path: Path) -> None:
    rules = _listing(_rule(SANDBOX, "allow", KIT_HOST), _rule("sandbox:other", "deny", KIT_HOST))

    done = _run(tmp_path, rules)

    assert done.returncode == 1, done.stdout + done.stderr
    assert f"{SANDBOX} allow {KIT_HOST}" in done.stdout


@needs_bash4
@pytest.mark.parametrize(
    "listing",
    [
        "",
        "Policy: local-policy",
        json.dumps({"rules": {}}),
        json.dumps({"rules": [_rule(SANDBOX, "maybe", "192.0.2.99")]}),
        json.dumps({"rules": [_rule(SANDBOX, "allow", "a\tb")]}),
    ],
    ids=["empty", "text", "rules-not-a-list", "unknown-decision", "resource-with-tab"],
)
def test_a_listing_that_does_not_read_is_drift(tmp_path: Path, listing: str) -> None:
    done = _run(tmp_path, listing)

    assert done.returncode == 1, done.stdout + done.stderr
    assert "cannot read" in done.stdout
    assert "clean" not in done.stdout


@needs_bash4
def test_a_failed_sbx_is_drift(tmp_path: Path) -> None:
    done = _run(tmp_path, _listing(), DRIFT_STATUS="1")

    assert done.returncode == 1, done.stdout + done.stderr
    assert "cannot read" in done.stdout
