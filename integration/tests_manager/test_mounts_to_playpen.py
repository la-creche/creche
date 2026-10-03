"""Seam 3: the mounts `apply_once` wrote, read by the real playpen.

`caregiver` writes three directories that contract 03 §7.1 mounts into the
sandbox. The playpen reads all three when it starts a pi process. Until
now the playpen's own tests wrote those directories themselves, so the
two sides had never met.

    apply_once ──► families/chat/creds/   ──► creds.json  ──► pi's env
               ├─► families/chat/config/  ──► runtime.json ─► pi's flags
               │                          ├─► instructions.md
               │                          └─► skills/
               └─► families/chat/control/ ──► the lock and the turn file

pi itself is `playpen/test/fake-pi.mjs`. Everything else is real.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from .conftest import CHAT_FAMILY, FAMILY, SKILL, Applied

DRIVER_TIMEOUT_S = 180

SANDBOX = "chat-s1"
SESSION = "owui-3f2a9c41-77b0-4a1e-9a4c-1d0e5f8b2c33"
TURN = "01JBQ7WZ0X4T9V6K2H8M3N5PQR"

#: Contract 01 §3.8 rule 6. The fixture grants `read` and `grep`, so the
#: other five of pi's seven built-ins are denied by name.
DENIED_BUILTINS = "bash,edit,write,find,ls"

#: pi's name for the one provider a sandbox may reach
#: (`playpen/src/constants.ts`).
PI_PROVIDER = "litellm"


@pytest.fixture
def report(
    node_bin: str,
    playpen_build: Path,
    playpen_driver: Path,
    applied: Applied,
    scratch: Path,
) -> dict[str, Any]:
    """One playpen run against `apply_once`'s directories."""
    path = scratch / "playpen-report.json"
    done = subprocess.run(
        [node_bin, str(playpen_driver), str(path)],
        capture_output=True,
        text=True,
        check=False,
        timeout=DRIVER_TIMEOUT_S,
        env={
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "HOME": str(scratch),
            "DRIVER_CREDS_DIR": str(applied.creds_dir),
            "DRIVER_CONFIG_DIR": str(applied.config_dir),
            "DRIVER_CONTROL_DIR": str(applied.control_dir),
            "DRIVER_SESSIONS_ROOT": str(applied.sessions_root),
            "DRIVER_BRIDGE_PATH": str(playpen_build / "dist" / "pep-bridge.js"),
            "DRIVER_FAKE_PI": str(playpen_build / "test" / "fake-pi.mjs"),
            "DRIVER_SANDBOX": SANDBOX,
            "DRIVER_FAMILY": FAMILY,
            "DRIVER_SESSION": SESSION,
            "DRIVER_TURN": TURN,
        },
    )
    if done.returncode != 0:
        pytest.fail(f"playpen_driver exited {done.returncode}:\n{done.stdout}\n{done.stderr}")

    body: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return body


def lines_of(report: dict[str, Any], kind: str) -> list[dict[str, Any]]:
    return [line for line in report["lines"] if line["type"] == kind]


def spawn(report: dict[str, Any]) -> dict[str, Any]:
    assert report["spawns"], "the playpen started no pi process"
    first: dict[str, Any] = report["spawns"][0]
    return first


def flag(args: list[str], name: str) -> str:
    """The value after a flag, or "" when the flag is absent."""
    if name not in args:
        return ""
    return args[args.index(name) + 1]


# --- what caregiver wrote is what the playpen needs --------------------


def test_apply_writes_the_three_mounts(applied: Applied) -> None:
    """Contract 03 §7.1's directories, before anything reads them."""
    assert (applied.creds_dir / "creds.json").is_file()
    assert (applied.config_dir / "runtime.json").is_file()
    assert (applied.config_dir / "instructions.md").is_file()
    assert (applied.config_dir / "skills" / SKILL / "SKILL.md").is_file()
    assert applied.control_dir.is_dir()


def test_the_playpen_takes_the_lock_and_starts(report: dict[str, Any]) -> None:
    """Contract 05 §4.3 rule 5: `apply_once` empties the control directory,
    so no dead playpen's lock is waiting there."""
    assert report["started"] is True


def test_the_handshake_completes(report: dict[str, Any]) -> None:
    ready = lines_of(report, "ready")
    assert len(ready) == 1
    assert ready[0]["sandbox"] == SANDBOX
    assert ready[0]["protocol"] == "1.0"


def test_a_pi_process_starts(report: dict[str, Any]) -> None:
    assert len(report["spawns"]) == 1
    assert flag(spawn(report)["args"], "--session-id") == SESSION


def test_the_turn_settles(report: dict[str, Any]) -> None:
    settled = lines_of(report, "turn_settled")
    assert len(settled) == 1
    assert settled[0]["session"] == SESSION
    assert settled[0]["turn"] == TURN


# --- the family file reaches pi's flags ----------------------------------


def test_sandbox_tools_becomes_the_deny_list(report: dict[str, Any]) -> None:
    """Contract 01 §3.8 rule 6, and §3.8's own reason: `--tools` would drop
    every PEP tool, so the playpen names the excluded built-ins."""
    args = spawn(report)["args"]
    assert flag(args, "--exclude-tools") == DENIED_BUILTINS
    assert "--tools" not in args
    assert "--no-tools" not in args


def test_the_model_alias_reaches_the_command_line(report: dict[str, Any]) -> None:
    """The family names an alias, `agent-router`. pi names a model by
    provider, so the playpen qualifies it with the one provider a sandbox
    may reach (`src/models-json.ts`). The alias itself is unchanged."""
    args = spawn(report)["args"]
    alias = CHAT_FAMILY["model"]["router"]
    assert flag(args, "--model") == f"{PI_PROVIDER}/{alias}"
    assert flag(args, "--models") == f"{PI_PROVIDER}/{alias}"


def test_the_persona_path_is_the_config_mount(report: dict[str, Any], applied: Applied) -> None:
    """Contract 03 §7.1: instructions travel by path, never as argv text."""
    args = spawn(report)["args"]
    assert flag(args, "--append-system-prompt") == str(applied.config_dir / "instructions.md")


def test_the_skills_path_is_the_config_mount(report: dict[str, Any], applied: Applied) -> None:
    args = spawn(report)["args"]
    assert "--no-skills" in args
    assert flag(args, "--skill") == str(applied.config_dir / "skills")


def test_the_bridge_is_the_one_named_extension(report: dict[str, Any]) -> None:
    args = spawn(report)["args"]
    assert "--no-extensions" in args
    assert flag(args, "--extension").endswith("pep-bridge.js")


# --- the credential mount reaches pi's environment -----------------------


def test_the_credentials_reach_the_environment_not_argv(
    report: dict[str, Any], applied: Applied
) -> None:
    """Invariant 13. argv is world readable through /proc for the whole life
    of the process, so neither value may appear there."""
    child = spawn(report)
    token = applied.token
    assert child["env"]["PEP_TOKEN"] == token
    assert child["env"]["LITELLM_VIRTUAL_KEY"].startswith("sk-fake-")
    assert token not in " ".join(child["args"])
    assert child["env"]["LITELLM_VIRTUAL_KEY"] not in " ".join(child["args"])


def test_the_environment_names_the_family_and_the_sandbox(report: dict[str, Any]) -> None:
    env = spawn(report)["env"]
    assert env["AGENT_FAMILY"] == FAMILY
    assert env["AGENT_SANDBOX"] == SANDBOX
    assert env["AGENT_SESSION"] == SESSION
    assert env["AGENT_TURN"] == TURN


def test_the_turn_file_lives_under_the_control_mount(
    report: dict[str, Any], applied: Applied
) -> None:
    """Contract 03 §7.4. `apply_once` mounts `control/` read-write for it."""
    env = spawn(report)["env"]
    turn_file = Path(env["AGENT_TURN_FILE"])
    assert turn_file.is_relative_to(applied.control_dir)
    assert json.loads(turn_file.read_text(encoding="utf-8"))["turn"] == TURN


def test_the_two_plane_urls_are_the_ones_the_manager_allows(report: dict[str, Any]) -> None:
    """The playpen's constants and `caregiver`'s egress config have to
    name the same two endpoints, or the sandbox reaches neither."""
    from caregiver.egress import litellm_endpoint, pep_endpoint

    env = spawn(report)["env"]
    assert env["LITELLM_BASE_URL"] == f"http://{litellm_endpoint()}"
    assert env["PEP_URL"] == f"http://{pep_endpoint()}"
