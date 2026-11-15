"""What `bin/setup-ups.sh` and `bin/ups-onbatt.sh` can be held to without Hyperion.

The setup script runs as root and writes the files that decide when Hyperion
shuts itself down, and whether it ever cuts the UPS output that also feeds
the router and the switch. The parts worth pinning:

1. **It never makes upsmon a primary.** A primary writes POWERDOWNFLAG, and
   Debian's shutdown hook then runs `upsdrvctl shutdown`: the UPS output
   goes off, with the router on it. Both `upsd.users` and `upsmon.conf` must
   say `secondary`.
2. **The two secrets are minted once and never printed.** A second run keeps
   the upsd password and the SNMP community, and neither appears in any
   output.
3. **Every file lands root:nut with the mode Debian ships.** The secret
   holders 0640, `nut.conf` 0644, the directory 0750.
4. **The test seam cannot be a privilege escalation.** With
   `SETUP_UPS_TEST_ROOT` set, the script refuses to run as root.
5. **The timer's script is in the deployed tree.** upssched.conf names
   `/opt/creche/bin/ups-onbatt.sh` with no test prefix leaked into
   it, and the setup refuses to run before that file exists.
6. **`ups-onbatt.sh` forces the shutdown on `onbatt` and on nothing else.**

Every external binary — `id`, `install`, `systemctl`, `apt-get`, `getent`,
`upsc`, `upsmon`, `upsd`, `logger` — is a PATH-injected binstub. Nothing here
reaches a live service and nothing needs Hyperion.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SETUP = REPO_ROOT / "bin" / "setup-ups.sh"
ONBATT = REPO_ROOT / "bin" / "ups-onbatt.sh"

CMDSCRIPT = "/opt/creche/bin/ups-onbatt.sh"
COMMUNITY = "s3cret-community"
#: A documentation address (RFC 5737), never a real card.
CARD = "192.0.2.10"
ONBATT_SHUTDOWN_S = 300

#: The six files, with the mode Debian ships them in.
FILES = {
    "nut.conf": "0644",
    "ups.conf": "0640",
    "upsd.conf": "0640",
    "upsd.users": "0640",
    "upsmon.conf": "0640",
    "upssched.conf": "0640",
}

# One shell per stub: record the argv, answer the least the script needs.
STUB = """#!/bin/sh
printf '%s\\n' "{name} $*" >> "$SU_STUB_LOG"
{body}
exit 0
"""

# The real `install` behaviour the script depends on, minus the chown: a test
# runs as an ordinary user. Owner, group and mode land in the log line.
INSTALL_BODY = """dir=0; first=""; second=""
while [ $# -gt 0 ]; do
  case "$1" in
    -d) dir=1 ;;
    -o|-g|-m) shift ;;
    *) if [ -z "$first" ]; then first="$1"; else second="$1"; fi ;;
  esac
  shift
done
if [ "$dir" = 1 ]; then mkdir -p "$first"
else mkdir -p "$(dirname "$second")"; cp "$first" "$second"; fi"""

ID_BODY = """case "$1" in -u) printf '%s\\n' "${SU_UID:-1000}" ;; esac"""

SYSTEMCTL_BODY = """case "$1" in is-active) printf 'active\\n' ;; esac"""

UPSC_BODY = """printf 'ups.status: OL\\nbattery.charge: 100\\nups.load: 15\\n'"""

UPSMON_BODY = """case "$1" in -K) printf 'Power down flag is not set\\n' ;; esac"""


def _stub(directory: Path, name: str, body: str = ":") -> None:
    path = directory / name
    path.write_text(STUB.format(name=name, body=body), encoding="utf-8")
    path.chmod(0o755)


@pytest.fixture
def host(tmp_path: Path) -> dict[str, Path]:
    """A fake Hyperion: a prefixed root with the deployed timer script in
    place, a community file, and a binstub directory that goes first on PATH."""
    prefix = tmp_path / "host"
    cmdscript = prefix / CMDSCRIPT.lstrip("/")
    cmdscript.parent.mkdir(parents=True)
    cmdscript.write_text("#!/bin/sh\n", encoding="utf-8")
    cmdscript.chmod(0o755)

    community = tmp_path / "ups1.community"
    community.write_text(COMMUNITY + "\n", encoding="utf-8")

    stubs = tmp_path / "stubs"
    stubs.mkdir()
    _stub(stubs, "install", INSTALL_BODY)
    _stub(stubs, "id", ID_BODY)
    _stub(stubs, "systemctl", SYSTEMCTL_BODY)
    _stub(stubs, "upsc", UPSC_BODY)
    _stub(stubs, "upsmon", UPSMON_BODY)
    for name in ("apt-get", "getent", "upsd", "logger"):
        _stub(stubs, name)

    return {
        "prefix": prefix,
        "etc": prefix / "etc" / "nut",
        "cmdscript": cmdscript,
        "community": community,
        "stubs": stubs,
        "stub_log": tmp_path / "stub.log",
    }


def _run(
    host: dict[str, Path], script: Path = SETUP, *args: str, tty: bool = False, **overrides: str
) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["SETUP_UPS_TEST_ROOT"] = str(host["prefix"])
    env["UPS1_COMMUNITY_FILE"] = str(host["community"])
    env.setdefault("UPS1_CARD", CARD)
    env["SU_STUB_LOG"] = str(host["stub_log"])
    env.update(overrides)
    env["PATH"] = f"{host['stubs']}{os.pathsep}{env['PATH']}"

    return subprocess.run(
        ["bash", str(script), *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
        stdin=None if tty else subprocess.DEVNULL,
    )


def _stub_lines(host: dict[str, Path], name: str) -> list[str]:
    log = host["stub_log"]
    if not log.exists():
        return []
    return [line for line in log.read_text(encoding="utf-8").splitlines() if line.startswith(name)]


def _conf(host: dict[str, Path], name: str) -> str:
    return (host["etc"] / name).read_text(encoding="utf-8")


def _value(text: str, key: str) -> str:
    match = re.search(rf"^\s*{key}\s*=\s*(\S+)", text, re.M)
    assert match, f"{key} not in:\n{text}"
    return match.group(1)


# ---- the killpower guard ---------------------------------------------------


def test_upsmon_is_a_secondary_and_never_a_primary(host: dict[str, Path]) -> None:
    done = _run(host)
    assert done.returncode == 0, done.stdout + done.stderr

    lines = _conf(host, "upsmon.conf").splitlines()
    monitor = [line for line in lines if line.startswith("MONITOR ")]
    assert len(monitor) == 1
    assert monitor[0].endswith(" secondary")
    assert "upsmon secondary" in _conf(host, "upsd.users")

    for name in FILES:
        for line in _conf(host, name).splitlines():
            assert "primary" not in line, line


def test_the_timer_runs_the_deployed_script_after_five_minutes(host: dict[str, Path]) -> None:
    assert _run(host).returncode == 0

    sched = _conf(host, "upssched.conf")
    assert f"CMDSCRIPT {CMDSCRIPT}\n" in sched
    assert str(host["prefix"]) not in sched
    assert f"AT ONBATT * START-TIMER onbatt {ONBATT_SHUTDOWN_S}\n" in sched
    assert "AT ONLINE * CANCEL-TIMER onbatt online\n" in sched

    mon = _conf(host, "upsmon.conf")
    assert "RUN_AS_USER root\n" in mon
    assert "NOTIFYCMD /sbin/upssched\n" in mon
    assert "NOTIFYFLAG ONBATT   SYSLOG+EXEC\n" in mon
    assert "NOTIFYFLAG ONLINE   SYSLOG+EXEC\n" in mon


def test_it_refuses_to_run_before_the_deploy_put_the_script_there(host: dict[str, Path]) -> None:
    host["cmdscript"].unlink()
    done = _run(host)
    assert done.returncode == 1
    assert "deploy the branch first" in done.stderr
    assert not host["etc"].exists()


# ---- secrets ----------------------------------------------------------------


def test_secrets_are_minted_once_and_kept(host: dict[str, Path]) -> None:
    assert _run(host).returncode == 0
    first_password = _value(_conf(host, "upsd.users"), "password")
    first_files = {name: _conf(host, name) for name in FILES}
    assert first_password in _conf(host, "upsmon.conf")
    assert _value(_conf(host, "ups.conf"), "community") == COMMUNITY

    # No community file on the second run: the value must come back from
    # ups.conf, like the password comes back from upsd.users.
    host["community"].unlink()
    done = _run(host, UPS1_COMMUNITY_FILE="")
    assert done.returncode == 0, done.stdout + done.stderr
    assert {name: _conf(host, name) for name in FILES} == first_files


def test_no_secret_reaches_stdout_or_stderr(host: dict[str, Path]) -> None:
    done = _run(host)
    assert done.returncode == 0
    password = _value(_conf(host, "upsd.users"), "password")

    for secret in (COMMUNITY, password):
        assert secret not in done.stdout
        assert secret not in done.stderr
        for line in host["stub_log"].read_text(encoding="utf-8").splitlines():
            assert secret not in line, line


def test_no_community_and_no_terminal_refuses_and_writes_nothing(host: dict[str, Path]) -> None:
    host["community"].unlink()
    env = {"UPS1_COMMUNITY_FILE": ""}
    done = _run(host, **env)
    assert done.returncode == 1
    assert "UPS1_COMMUNITY_FILE" in done.stderr
    assert not (host["etc"] / "ups.conf").exists()


def test_no_card_address_refuses_and_writes_nothing(host: dict[str, Path]) -> None:
    done = _run(host, UPS1_CARD="")
    assert done.returncode == 1
    assert "UPS1_CARD" in done.stderr
    assert not (host["etc"] / "ups.conf").exists()


def test_the_card_address_lands_in_ups_conf(host: dict[str, Path]) -> None:
    assert _run(host).returncode == 0
    assert _value(_conf(host, "ups.conf"), "port") == CARD


# ---- modes and owners --------------------------------------------------------


def test_every_file_lands_root_nut_with_debians_mode(host: dict[str, Path]) -> None:
    assert _run(host).returncode == 0

    installs = _stub_lines(host, "install")
    assert f"install -d -m 0750 -o root -g nut {host['etc']}" in installs

    by_target = {line.rsplit(" ", 1)[-1]: line for line in installs if " -d " not in line}
    for name, mode in FILES.items():
        line = by_target[str(host["etc"] / name)]
        assert f" -m {mode} -o root -g nut " in line, line
    assert len(by_target) == len(FILES)


# ---- the seam and the check -------------------------------------------------


def test_the_seam_refuses_root(host: dict[str, Path]) -> None:
    done = _run(host, SU_UID="0")
    assert done.returncode == 1
    assert "Refusing" in done.stderr
    assert not host["etc"].exists()


def test_check_needs_no_root_and_writes_nothing(host: dict[str, Path]) -> None:
    done = _run(host, SETUP, "--check")
    assert done.returncode == 0, done.stdout + done.stderr
    assert "ups.status: OL" in done.stdout
    assert "Power down flag is not set" in done.stdout
    assert not host["etc"].exists()
    assert not _stub_lines(host, "install")


# ---- ups-onbatt.sh ------------------------------------------------------------


def test_onbatt_forces_the_shutdown(host: dict[str, Path]) -> None:
    done = _run(host, ONBATT, "onbatt")
    assert done.returncode == 0, done.stderr
    assert "upsmon -c fsd" in _stub_lines(host, "upsmon")


@pytest.mark.parametrize("event", ["online", "lowbatt"])
def test_other_events_only_log(host: dict[str, Path], event: str) -> None:
    done = _run(host, ONBATT, event)
    assert done.returncode == 0, done.stderr
    assert not _stub_lines(host, "upsmon")
    assert _stub_lines(host, "logger")


def test_an_unknown_event_fails_loudly(host: dict[str, Path]) -> None:
    done = _run(host, ONBATT, "bogus")
    assert done.returncode == 1
    assert not _stub_lines(host, "upsmon")
    assert any("unknown event: bogus" in line for line in _stub_lines(host, "logger"))


# ---- a host that is not perfect ---------------------------------------------

#: `apt-get update` fails for one unrelated repository (exit 100), the way
#: Hyperion's did on 2026-10-01. The install from the present lists works.
APT_UPDATE_FAILS = """case "$1" in update) exit 100 ;; esac"""

APT_INSTALL_FAILS = """case "$1" in install) exit 100 ;; esac"""


def test_a_failed_apt_update_does_not_stop_an_install_that_works(host: dict[str, Path]) -> None:
    (host["stubs"] / "upsd").unlink()
    _stub(host["stubs"], "apt-get", APT_UPDATE_FAILS)

    done = _run(host)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "apt-get install -y -qq nut-client nut-server nut-snmp" in _stub_lines(host, "apt-get")
    assert "apt-get update failed" in done.stdout + done.stderr
    assert (host["etc"] / "upsmon.conf").exists()


def test_a_failed_install_still_stops_everything(host: dict[str, Path]) -> None:
    (host["stubs"] / "upsd").unlink()
    _stub(host["stubs"], "apt-get", APT_INSTALL_FAILS)

    done = _run(host)
    assert done.returncode != 0
    assert not host["etc"].exists()


def test_a_community_file_replaces_a_stored_wrong_value(host: dict[str, Path]) -> None:
    """A mistyped community is in ups.conf after the first run. The file is
    the way to correct it: it wins over the stored value."""
    assert _run(host).returncode == 0
    assert _value(_conf(host, "ups.conf"), "community") == COMMUNITY
    password = _value(_conf(host, "upsd.users"), "password")

    host["community"].write_text("the-right-one\n", encoding="utf-8")
    done = _run(host)
    assert done.returncode == 0, done.stdout + done.stderr
    assert _value(_conf(host, "ups.conf"), "community") == "the-right-one"
    assert _value(_conf(host, "upsd.users"), "password") == password


def test_a_silent_ups_says_how_to_correct_the_community(host: dict[str, Path]) -> None:
    _stub(host["stubs"], "upsc", "exit 1")
    _stub(host["stubs"], "sleep")

    done = _run(host)
    assert done.returncode == 1
    assert "UPS1_COMMUNITY_FILE" in done.stderr


def test_a_named_community_file_that_is_missing_is_a_refusal(host: dict[str, Path]) -> None:
    assert _run(host).returncode == 0
    before = _conf(host, "ups.conf")

    host["community"].unlink()
    done = _run(host)
    assert done.returncode == 1
    assert "not readable" in done.stderr
    assert _conf(host, "ups.conf") == before
