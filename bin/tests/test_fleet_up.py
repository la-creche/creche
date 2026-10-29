"""`bin/fleet-up`: the fleet layer of the host, made from nothing.

The script runs as the operator. `check` reads the host and changes
nothing. `up` makes the fleet layer and records each path it makes. `down`
removes each recorded path and nothing else. `docs/fleet-bootstrap.md` is
the design.

Each case runs the script for real, against a fake host:

1. A prefix stands in for `/`. `FLEET_TEST_PREFIX` points every absolute
   root of the script into it.
2. A fake `HOME` holds the component trees and the user unit directory.
3. Binstubs first on `PATH` stand in for `systemctl`, `getent`, `ss` and
   `id`. The `systemctl` stub writes each call to a log, and it keeps the
   set of enabled units in a file.

Nothing here needs the host, and nothing reaches a live service.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import pytest
from attendance.auth import Principal

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
SCRIPT: Final = REPO_ROOT / "bin" / "fleet-up"
UNIT_SOURCE: Final = REPO_ROOT / "systemd"

STATE: Final = "srv/agents/state/creche"
MASTER_KEY: Final = "sk-fleet-up-test-master-key-0123456789abcdef"
IMAGE_BASE: Final = "127.0.0.1:5000/agent-sandbox@sha256:" + "a" * 64
IMAGE_PYTHON: Final = "127.0.0.1:5000/agent-sandbox-python@sha256:" + "b" * 64

#: The status of an `up` that did its part and waits for a step of another
#: actor: a release, or a root step.
PENDING: Final = 3

#: Each user unit `up` installs, in the order it installs them.
UNITS: Final = (
    "creche-attendance.service",
    "creche-caregiver.service",
    "creche-noticeboard.service",
    "creche-door-acp.service",
    "creche-trigger-webhooks.service",
    "creche-trigger@.service",
    "creche-watchdog.service",
    "creche-watchdog.timer",
    "creche-registry-sync.service",
    "creche-registry-sync.timer",
    "creche-code-corpus-sync.service",
    "creche-code-corpus-sync.timer",
    "creche-sbx-drift.service",
    "creche-sbx-drift.timer",
    "creche-library@.service",
    "creche-library@.timer",
    "creche-follow.service",
    "creche-follow.timer",
)

#: The units `up` enables and starts. A template and a service that a
#: timer starts are installed only.
ENABLED: Final = (
    "creche-attendance.service",
    "creche-caregiver.service",
    "creche-noticeboard.service",
    "creche-door-acp.service",
    "creche-trigger-webhooks.service",
    "creche-watchdog.timer",
    "creche-registry-sync.timer",
    "creche-code-corpus-sync.timer",
    "creche-sbx-drift.timer",
    "creche-follow.timer",
)

DIRS: Final = (
    "fleet",
    "tokens",
    "families",
    "leases",
    "faults",
    "triggers",
    "triggers/webhooks",
    "log",
    "sock",
)

ENV_FILES: Final = ("attendance.env", "noticeboard.env", "caregiver.env")

#: The `systemctl` verbs that change nothing.
READ_ONLY_VERBS: Final = frozenset({"is-enabled", "is-active", "show"})

#: What `check` names for each item of the root checklist.
ROOT_ITEMS: Final = (
    "cosign",
    "uv 0.12.17",
    "group chaperone",
    "group agents",
    "/etc/chaperone mode 1775",
    "/etc/chaperone/install.toml",
    "/etc/chaperone/secrets",
    "CHAPERONE_LISTEN",
    "chaperone binary",
    "8310",
    "state root",
    "sessions root",
    "registry checkout",
    "handover tree",
    "playpen images",
    "LITELLM_MASTER_KEY",
)

SYSTEMCTL_STUB: Final = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$FLEET_STUB_LOG"
[[ "$1" == --user ]] && shift
enabled="$FLEET_STUB_ENABLED"
touch "$enabled"
case "$1" in
  is-enabled)
    if grep -qxF -- "$2" "$enabled"; then echo enabled; exit 0; fi
    echo disabled; exit 1 ;;
  enable)
    [[ "$2" == --now ]] && shift
    grep -qxF -- "$2" "$enabled" || printf '%s\\n' "$2" >> "$enabled" ;;
  disable)
    [[ "$2" == --now ]] && shift
    grep -vxF -- "$2" "$enabled" > "$enabled.new" || true
    mv "$enabled.new" "$enabled" ;;
esac
exit 0
"""

GETENT_STUB: Final = """#!/usr/bin/env bash
[[ -f "$FLEET_STUB_GROUPS" ]] || exit 2
grep -E "^$2:" "$FLEET_STUB_GROUPS" || exit 2
"""

SS_STUB: Final = """#!/usr/bin/env bash
[[ -f "$FLEET_STUB_SS" ]] && cat "$FLEET_STUB_SS"
exit 0
"""

ID_STUB: Final = """#!/usr/bin/env bash
if [[ "$1" == -u && -n "${FLEET_STUB_UID:-}" ]]; then echo "$FLEET_STUB_UID"; exit 0; fi
exec /usr/bin/id "$@"
"""

UV_STUB: Final = """#!/usr/bin/env bash
echo "uv 0.12.17 (00000000 2026-10-01)"
"""

COSIGN_STUB: Final = """#!/usr/bin/env bash
echo "cosign v2.6.0"
"""


def _executable(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)


@dataclass
class Host:
    """A fake host: a prefix for `/`, a fake `HOME` and the binstubs."""

    root: Path
    prefix: Path
    home: Path
    stubs: Path
    log: Path
    enabled: Path

    @property
    def state(self) -> Path:
        return self.prefix / STATE

    @property
    def tokens(self) -> Path:
        return self.state / "tokens"

    @property
    def unit_dir(self) -> Path:
        return self.home / ".config" / "systemd" / "user"

    @property
    def record(self) -> Path:
        return self.state / "fleet" / "record.json"

    def run(self, *args: str, **overrides: str) -> subprocess.CompletedProcess[str]:
        env = dict(os.environ)
        env.update(
            {
                "HOME": str(self.home),
                "FLEET_TEST_PREFIX": str(self.prefix),
                "FLEET_STUB_LOG": str(self.log),
                "FLEET_STUB_ENABLED": str(self.enabled),
                "FLEET_STUB_GROUPS": str(self.root / "groups"),
                "FLEET_STUB_SS": str(self.root / "ss.txt"),
                "PATH": f"{self.stubs}{os.pathsep}{env['PATH']}",
            }
        )
        env.update(overrides)

        return subprocess.run(
            ["bash", str(SCRIPT), *args],
            capture_output=True,
            text=True,
            env=env,
            timeout=120,
        )

    def calls(self) -> list[str]:
        if not self.log.exists():
            return []

        return self.log.read_text(encoding="utf-8").splitlines()


def _bare_host(tmp_path: Path) -> Host:
    host = Host(
        root=tmp_path,
        prefix=tmp_path / "prefix",
        home=tmp_path / "home",
        stubs=tmp_path / "stubs",
        log=tmp_path / "systemctl.log",
        enabled=tmp_path / "enabled.txt",
    )
    host.prefix.mkdir()
    host.home.mkdir()
    _executable(host.stubs / "systemctl", SYSTEMCTL_STUB)
    _executable(host.stubs / "getent", GETENT_STUB)
    _executable(host.stubs / "ss", SS_STUB)
    _executable(host.stubs / "id", ID_STUB)

    return host


@pytest.fixture
def empty(tmp_path: Path) -> Host:
    """A host on which no root step ran and no release landed."""
    return _bare_host(tmp_path)


@pytest.fixture
def ready(tmp_path: Path) -> Host:
    """A host after the root steps and after the first release."""
    host = _bare_host(tmp_path)
    host.state.mkdir(parents=True, mode=0o750)
    (host.prefix / "srv/agents/sessions").mkdir(parents=True)
    (host.prefix / "srv/agents/registry/.git").mkdir(parents=True)

    for name in ("attendance", "caregiver", "noticeboard"):
        tree = host.home / ".local" / "components" / name
        tree.mkdir(parents=True)
        (tree / ".release-version").write_text("0.1.50\n", encoding="utf-8")

    handover = host.prefix / "opt/components/handover"
    handover.mkdir(parents=True)
    (handover / ".release-version").write_text("0.1.30\n", encoding="utf-8")
    playpen = host.prefix / "opt/components/playpen"
    playpen.mkdir(parents=True)
    (playpen / "images.env").write_text(
        f"base={IMAGE_BASE}\npython={IMAGE_PYTHON}\n", encoding="utf-8"
    )

    hooks = host.state / "hooks.env"
    hooks.write_text(f"LITELLM_MASTER_KEY={MASTER_KEY}\n", encoding="utf-8")
    hooks.chmod(0o600)

    return host


def _snapshot(*roots: Path) -> dict[str, tuple[int, int, bytes]]:
    """Each path under the roots: its mode, its modification time and its bytes."""
    found: dict[str, tuple[int, int, bytes]] = {}

    for root in roots:
        for path in sorted([root, *root.rglob("*")]):
            facts = path.lstat()
            body = path.read_bytes() if path.is_file() else b""
            found[str(path)] = (facts.st_mode, facts.st_mtime_ns, body)

    return found


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _fail_lines(done: subprocess.CompletedProcess[str]) -> list[str]:
    return [line for line in done.stdout.splitlines() if line.startswith("FAIL: ")]


# --- check ------------------------------------------------------------------


def test_check_on_an_empty_tree_lists_every_missing_item(empty: Host) -> None:
    done = empty.run("check")

    assert done.returncode == 1, done.stdout + done.stderr
    fails = "\n".join(_fail_lines(done))
    for item in ROOT_ITEMS:
        assert item in fails, f"no FAIL line names {item!r}:\n{done.stdout}"

    for principal in Principal:
        assert f"tokens/{principal.value}.token" in fails, principal

    for name in ("tokens/noticeboard-access.key", "door-acp.key", *ENV_FILES, *UNITS):
        assert name in fails, f"no FAIL line names {name!r}:\n{done.stdout}"

    for name in DIRS:
        assert f"{STATE}/{name}" in fails, name


def test_check_changes_nothing(ready: Host) -> None:
    before = _snapshot(ready.prefix, ready.home)

    ready.run("check")

    assert _snapshot(ready.prefix, ready.home) == before
    assert all(call.split()[1] in READ_ONLY_VERBS for call in ready.calls()), ready.calls()


def test_check_passes_an_item_once_it_is_there(empty: Host) -> None:
    _executable(empty.prefix / "usr/local/bin/cosign", COSIGN_STUB)
    _executable(empty.prefix / "usr/local/bin/uv", UV_STUB)

    done = empty.run("check")

    passes = [line for line in done.stdout.splitlines() if line.startswith("PASS: ")]
    assert any("cosign" in line for line in passes), done.stdout
    assert any("uv 0.12.17" in line for line in passes), done.stdout


def test_check_after_up_finds_the_whole_fleet_layer(ready: Host) -> None:
    assert ready.run("up").returncode == 0

    done = ready.run("check")

    fails = "\n".join(_fail_lines(done))
    assert f"{STATE}/" not in fails, fails
    assert ".service" not in fails, fails
    assert ".timer" not in fails, fails


# --- up ---------------------------------------------------------------------


def test_up_mints_each_principal_token_at_0600(ready: Host) -> None:
    done = ready.run("up")

    assert done.returncode == 0, done.stdout + done.stderr
    minted = {path.name for path in ready.tokens.glob("*.token")}
    assert minted == {f"{principal.value}.token" for principal in Principal}

    for path in (*ready.tokens.iterdir(), ready.state / "door-acp.key"):
        assert _mode(path) == 0o600, path
        assert len(path.read_bytes().strip()) >= 32, path


def test_up_never_prints_a_secret(ready: Host) -> None:
    done = ready.run("up")

    said = done.stdout + done.stderr
    assert MASTER_KEY not in said
    for path in (*ready.tokens.iterdir(), ready.state / "door-acp.key"):
        assert path.read_text(encoding="utf-8").strip() not in said, path


def test_up_writes_the_env_files_at_0600(ready: Host) -> None:
    assert ready.run("up").returncode == 0

    for name in ENV_FILES:
        assert _mode(ready.state / name) == 0o600, name

    managerd = (ready.state / "caregiver.env").read_text(encoding="utf-8")
    assert f"LITELLM_MASTER_KEY={MASTER_KEY}\n" in managerd
    assert f"AGENT_SANDBOX_IMAGE={IMAGE_BASE}\n" in managerd
    assert f"AGENT_SANDBOX_PYTHON_IMAGE={IMAGE_PYTHON}\n" in managerd
    assert "AGENT_PEP_URL=http://192.0.2.10:8300\n" in managerd

    view = (ready.state / "noticeboard.env").read_text(encoding="utf-8")
    assert f"NOTICEBOARD_ACCESS_KEY_FILE={ready.tokens}/noticeboard-access.key\n" in view

    for name in ("attendance.env", "noticeboard.env"):
        text = (ready.state / name).read_text(encoding="utf-8")
        assert "KEY=" not in text.replace("ACCESS_KEY_FILE=", ""), name


def test_up_never_writes_hooks_env(ready: Host) -> None:
    hooks = ready.state / "hooks.env"
    before = _snapshot(hooks)

    ready.run("up")

    assert _snapshot(hooks) == before


def test_up_without_a_master_key_waits_for_the_root_step(ready: Host) -> None:
    (ready.state / "hooks.env").unlink()

    done = ready.run("up")

    assert done.returncode == PENDING, done.stdout + done.stderr
    assert "LITELLM_MASTER_KEY" in done.stdout
    assert "sops -d" in done.stdout
    assert not (ready.state / "hooks.env").exists()
    managerd = (ready.state / "caregiver.env").read_text(encoding="utf-8")
    assert "LITELLM_MASTER_KEY" not in managerd
    assert (ready.unit_dir / "creche-caregiver.service").exists()
    assert "creche-caregiver.service" not in ready.enabled.read_text(encoding="utf-8")


def test_up_installs_each_unit_of_this_tree(ready: Host) -> None:
    assert ready.run("up").returncode == 0

    for name in UNITS:
        installed = ready.unit_dir / name
        assert installed.read_bytes() == (UNIT_SOURCE / name).read_bytes(), name
        assert _mode(installed) == 0o644, name

    assert set(ready.enabled.read_text(encoding="utf-8").split()) == set(ENABLED)


def test_up_starts_attendance_before_each_other_unit(ready: Host) -> None:
    ready.run("up")

    starts = [call.split()[-1] for call in ready.calls() if "enable --now" in call]
    assert starts[0] == "creche-attendance.service", starts


def test_up_refuses_a_unit_whose_tree_is_absent(ready: Host) -> None:
    tree = ready.home / ".local" / "components" / "caregiver"
    (tree / ".release-version").unlink()
    tree.rmdir()

    done = ready.run("up")

    assert done.returncode == PENDING, done.stdout + done.stderr
    assert "handover request caregiver" in done.stdout
    assert not (ready.unit_dir / "creche-caregiver.service").exists()
    assert (ready.unit_dir / "creche-attendance.service").exists()
    assert "creche-caregiver.service" not in ready.enabled.read_text(encoding="utf-8")


def test_up_twice_changes_nothing_the_second_time(ready: Host) -> None:
    assert ready.run("up").returncode == 0
    before = _snapshot(ready.prefix, ready.home)
    calls_before = len(ready.calls())

    done = ready.run("up")

    assert done.returncode == 0, done.stdout + done.stderr
    assert _snapshot(ready.prefix, ready.home) == before
    second = ready.calls()[calls_before:]
    assert all(call.split()[1] in READ_ONLY_VERBS for call in second), second


def test_up_keeps_a_key_that_exists(ready: Host) -> None:
    ready.tokens.mkdir(mode=0o700)
    token = ready.tokens / "door-acp.token"
    token.write_text("x" * 48, encoding="utf-8")
    token.chmod(0o600)

    ready.run("up")

    assert token.read_text(encoding="utf-8") == "x" * 48


def test_up_refuses_to_run_as_root(ready: Host) -> None:
    before = _snapshot(ready.prefix, ready.home)

    done = ready.run("up", FLEET_STUB_UID="0")

    assert done.returncode == 1
    assert "root" in done.stdout + done.stderr
    assert _snapshot(ready.prefix, ready.home) == before


def test_up_refuses_a_host_with_no_state_root(empty: Host) -> None:
    done = empty.run("up")

    assert done.returncode == 1
    assert f"{STATE}" in done.stdout + done.stderr
    assert not (empty.prefix / "srv").exists()


def test_the_record_is_json_and_names_each_path(ready: Host) -> None:
    assert ready.run("up").returncode == 0

    record = json.loads(ready.record.read_text(encoding="utf-8"))
    paths = {entry["path"] for entry in record["entries"]}
    assert str(ready.tokens / "door-acp.token") in paths
    assert str(ready.state / "caregiver.env") in paths
    assert str(ready.unit_dir / "creche-attendance.service") in paths
    assert "creche-attendance.service" in paths
    assert str(ready.state / "hooks.env") not in paths
    assert _mode(ready.record) == 0o600
    assert _mode(ready.record.parent) == 0o700


# --- down -------------------------------------------------------------------


def test_down_removes_only_recorded_paths(ready: Host) -> None:
    ready.unit_dir.mkdir(parents=True)
    foreign_unit = ready.unit_dir / "someone-else.service"
    foreign_unit.write_text("[Unit]\n", encoding="utf-8")
    foreign = (foreign_unit, ready.state / "hooks.env")
    assert ready.run("up").returncode == 0
    made = [ready.tokens / f"{p.value}.token" for p in Principal]
    made += [ready.state / name for name in ENV_FILES]
    made += [ready.unit_dir / name for name in UNITS]

    done = ready.run("down")

    assert done.returncode == 0, done.stdout + done.stderr
    for path in made:
        assert not path.exists(), path
    for path in foreign:
        assert path.exists(), path
    for name in DIRS:
        assert not (ready.state / name).exists(), name
    assert ready.state.is_dir()
    assert ready.enabled.read_text(encoding="utf-8").split() == []


def test_a_key_that_existed_before_up_survives_down(ready: Host) -> None:
    ready.tokens.mkdir(mode=0o700)
    token = ready.tokens / "door-acp.token"
    token.write_text("y" * 48, encoding="utf-8")
    token.chmod(0o600)
    key = ready.state / "door-acp.key"
    key.write_text("z" * 48, encoding="utf-8")
    key.chmod(0o600)

    assert ready.run("up").returncode == 0
    assert ready.run("down").returncode == 0

    assert token.read_text(encoding="utf-8") == "y" * 48
    assert key.read_text(encoding="utf-8") == "z" * 48
    assert ready.tokens.is_dir()
    assert not (ready.tokens / "chaperone-dispatch.token").exists()


def test_down_keeps_a_recorded_directory_that_holds_another_file(ready: Host) -> None:
    assert ready.run("up").returncode == 0
    family = ready.state / "families" / "chat"
    family.mkdir()

    done = ready.run("down")

    assert done.returncode == 0, done.stdout + done.stderr
    assert family.is_dir()
    assert "kept" in done.stdout
    record = json.loads(ready.record.read_text(encoding="utf-8"))
    assert [entry["path"] for entry in record["entries"]] == [str(ready.state / "families")]


def test_down_refuses_a_record_entry_outside_its_roots(ready: Host) -> None:
    assert ready.run("up").returncode == 0
    outside = ready.prefix / "etc" / "chaperone" / "install.toml"
    outside.parent.mkdir(parents=True)
    outside.write_text("x\n", encoding="utf-8")
    text = ready.record.read_text(encoding="utf-8")
    line = f'    {{"kind": "file", "path": "{outside}"}},\n'
    ready.record.write_text(text.replace('  "entries": [\n', '  "entries": [\n' + line), "utf-8")
    before = _snapshot(ready.prefix, ready.home)

    done = ready.run("down")

    assert done.returncode == 1
    assert str(outside) in done.stdout + done.stderr
    assert _snapshot(ready.prefix, ready.home) == before


def test_down_with_no_record_removes_nothing(ready: Host) -> None:
    before = _snapshot(ready.prefix, ready.home)

    done = ready.run("down")

    assert done.returncode == 0
    assert _snapshot(ready.prefix, ready.home) == before


def test_up_after_down_makes_the_same_layer(ready: Host) -> None:
    assert ready.run("up").returncode == 0
    first = sorted(json.loads(ready.record.read_text("utf-8"))["entries"], key=str)
    assert ready.run("down").returncode == 0

    assert ready.run("up").returncode == 0

    again = sorted(json.loads(ready.record.read_text("utf-8"))["entries"], key=str)
    assert again == first
