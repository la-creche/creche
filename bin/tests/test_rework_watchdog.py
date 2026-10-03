"""What `bin/rework-watchdog.sh` can be held to without the host.

A rework service can die in silence, and nothing else would tell the operator.
This script tells the operator's phone inside five minutes, so the cases that matter
most are the ones about SAYING it, and saying it once.

**A check must fail on two CONSECUTIVE runs before it pushes.**
`agent-control-deploy` and the release visit restart the PEP on
purpose, and a lone probe landing inside the 10-15 s restart window must
not page the operator for an outage they ordered. A single failure still turns the
exit code non-zero and still names itself in the journal — it just does
not reach the phone until it repeats. Most cases below run the check
TWICE before asserting a push, and a handful pin the single-failure
("1 of 2") behaviour on its own.

Every external binary is a PATH-injected binstub, the way
`bin/tests/test_rework_cutover.py` already does it. Nothing reaches a
live service and nothing needs the host.

Two portability rules this file keeps, because it runs on a Mac and the
script runs on Linux:

1. The `date` stub answers the GNU form first and the BSD form second,
   which is the order the script itself tries. The real `date` is what
   runs for `+%s`, so "now" is real and only the PARSE is stubbed.
2. Every fixture file and directory gets an explicit mode. Nothing here
   depends on the umask, on who owns a file, or on an account name.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Final

import pytest

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
SCRIPT: Final = REPO_ROOT / "bin" / "rework-watchdog.sh"

#: The five units the script asks `systemctl --user is-failed` about.
UNITS: Final = (
    "agent-sessiond.service",
    "agent-managerd.service",
    "agent-door-owui.service",
    "agent-trigger-webhooks.service",
    "creche-noticeboard.service",
)

#: The registry's door (check 5). The sync is what pulls
#: `/srv/agents/registry`, and a timer that stopped firing looks exactly
#: like a fleet that ignored every merge.
SYNC_TIMER: Final = "registry-sync.timer"
SYNC_SERVICE: Final = "registry-sync.service"

#: A value that must never reach argv or stdout.
TOKEN: Final = "WATCHDOG-SECRET-MUST-NEVER-APPEAR"
HOOK_URL: Final = "http://192.0.2.10:1881/endpoint/approval"

DIR_MODE: Final = 0o700
FILE_MODE: Final = 0o600
STUB_MODE: Final = 0o755

STUB: Final = """#!/bin/sh
printf '%s\\n' "{name} $*" >> "$WD_STUB_LOG"
{body}
exit 0
"""

#: One canned answer per call shape. `WD_PEP_CODE` and `WD_SOCK_CODE` move
#: per case, and the POST records its own argv so a case can read the
#: notice text back.
#:
#: The POST comes FIRST on purpose. Its `-d` body quotes the very URL the
#: PEP check names, so a `*/healthz*` pattern ahead of it swallows the
#: notice and answers it with an HTTP code.
CURL_BODY: Final = """case "$*" in
  *"-X POST"*)
    printf '%s\\n' "$*" >> "$WD_POST_LOG"
    exit "${WD_POST_EXIT:-0}" ;;
  *"--unix-socket"*) printf '%s' "${WD_SOCK_CODE:-401}" ;;
  *"/healthz"*) printf '%s' "${WD_PEP_CODE:-200}" ;;
esac
"""

#: macOS has no `timeout`, so the script's per-check ceiling needs a stand
#: -in here. It drops the duration and runs the command, which is what the
#: cases are about: the ceiling itself is a production guard against a hung
#: mount and there is nothing to hang on a temp directory.
TIMEOUT_BODY: Final = """shift
exec "$@"
"""

#: `is-failed` exits 0 when the unit IS failed. The default answers 1 for
#: every unit, which is "not failed"; `WD_FAILED_UNITS` names the ones
#: that are.
#:
#: `is-enabled` and `show -p Result` are the registry sync's half (check
#: 5). The defaults are a host that took the cutover and whose last sync
#: run ended well.
SYSTEMCTL_BODY: Final = """case "$*" in
  *is-failed*)
    for one in ${WD_FAILED_UNITS:-}; do
      case "$*" in *"$one"*) exit 0 ;; esac
    done
    exit 1 ;;
  *is-enabled*)
    printf '%s\\n' "${WD_SYNC_ENABLED:-enabled}"
    [ "${WD_SYNC_ENABLED:-enabled}" = enabled ] || exit 1 ;;
  *Result*) printf '%s\\n' "${WD_SYNC_RESULT:-success}" ;;
esac
"""

#: The `date` the script parses stamps with. GNU `-d` first, BSD `-j -f`
#: second, and `+%s` on its own is the real clock: a case computes an age
#: against the moment it runs, never against a frozen one.
DATE_BODY: Final = """case "$1" in
  -u)
    shift
    case "${1:-}" in
      -d) shift; parsed="$1"; shift ;;
      -j) shift; shift; shift; parsed="$1"; shift ;;
      *) exec /bin/date -u "$@" ;;
    esac
    # The stub's own backend must be portable too: CI is Linux, whose `date`
    # has no `-j`. GNU first, BSD second, the order the script itself uses.
    /bin/date -u -d "$parsed" "$@" 2>/dev/null && exit 0
    exec /bin/date -u -j -f '%Y-%m-%dT%H:%M:%SZ' "$parsed" "$@" ;;
esac
exec /bin/date "$@"
"""


def _stub(directory: Path, name: str, body: str = ":") -> None:
    path = directory / name
    path.write_text(STUB.format(name=name, body=body), encoding="utf-8")
    path.chmod(STUB_MODE)


def _write(path: Path, body: str) -> None:
    """A fixture file with an explicit mode, never the umask's answer."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(DIR_MODE)
    path.write_text(body, encoding="utf-8")
    path.chmod(FILE_MODE)


def _stamp(seconds_ago: int) -> str:
    """An RFC 3339 stamp that many seconds in the past, in the shape
    `managerd/src/agent_managerd/clock.py` writes."""
    import datetime

    moment = datetime.datetime.now(datetime.UTC) - datetime.timedelta(seconds=seconds_ago)

    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


class Rig:
    """A fake host: a prefixed root, binstubs first on PATH, and one
    fresh status document."""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.prefix = tmp_path / "host"
        self.state = self.prefix / "srv/agents/state/rework"
        self.families = self.state / "families"
        self.socket = self.state / "sock/sessiond.sock"
        self.verdict = self.state / "watchdog/verdict"
        self.stub_log = tmp_path / "stub.log"
        self.post_log = tmp_path / "post.log"

        self.sync_stamp = self.state / "registry-sync/last-success"

        self.hooks = self.state / "hooks.env"
        self.old_env = self.prefix / "srv/agents/state/materializer/env"
        _write(self.hooks, f"APPROVAL_URL={HOOK_URL}\nAPPROVAL_TOKEN={TOKEN}\n")
        self.publish("chat", 5)
        self.sync_ran(0)
        self.make_socket()

        self.stubs = tmp_path / "stubs"
        self.stubs.mkdir()
        self.stubs.chmod(STUB_MODE)
        _stub(self.stubs, "curl", CURL_BODY)
        _stub(self.stubs, "systemctl", SYSTEMCTL_BODY)
        _stub(self.stubs, "date", DATE_BODY)
        _stub(self.stubs, "timeout", TIMEOUT_BODY)

    def publish(self, family: str, seconds_ago: int) -> None:
        """One status document, as contract 05 §2.1 shapes it."""
        body = {"family": family, "written_at": _stamp(seconds_ago), "state": "in_sync"}
        _write(self.families / family / "status.json", json.dumps(body, indent=2) + "\n")

    def sync_ran(self, minutes_ago: int) -> None:
        """The stamp `bin/rework-registry-sync.sh` writes when a run ended
        well. Its AGE is what check 5 reads, so the mtime is set here
        rather than the text: the check asks `find -mmin`, which needs no
        `date` parsing and reads the same on both platforms."""
        _write(self.sync_stamp, _stamp(minutes_ago * 60) + "\n")
        when = time.time() - (minutes_ago * 60)
        os.utime(self.sync_stamp, (when, when))

    def forget_the_sync(self) -> None:
        self.sync_stamp.unlink(missing_ok=True)

    def make_socket(self) -> None:
        """A real AF_UNIX socket file, because the script tests `-S` and a
        plain file would answer the wrong thing.

        Bound from inside its own directory, by its bare name. `sun_path`
        is 104 bytes on macOS and pytest's temp roots are long enough on
        their own to overflow it with an absolute path.
        """
        import socket

        self.socket.parent.mkdir(parents=True, exist_ok=True)
        self.socket.parent.chmod(DIR_MODE)
        self.socket.unlink(missing_ok=True)
        was = Path.cwd()
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            os.chdir(self.socket.parent)
            listener.bind(self.socket.name)
        finally:
            listener.close()
            os.chdir(was)

        self.socket.chmod(FILE_MODE)

    def run(self, *args: str, **overrides: str) -> subprocess.CompletedProcess[str]:
        env = dict(os.environ)
        env["WATCHDOG_TEST_PREFIX"] = str(self.prefix)
        env["WD_STUB_LOG"] = str(self.stub_log)
        env["WD_POST_LOG"] = str(self.post_log)
        env["PATH"] = f"{self.stubs}{os.pathsep}{env['PATH']}"
        env.update(overrides)

        return subprocess.run(
            ["bash", str(SCRIPT), *args], capture_output=True, text=True, env=env, timeout=120
        )

    def posts(self) -> list[str]:
        if not self.post_log.exists():
            return []

        return [one for one in self.post_log.read_text(encoding="utf-8").splitlines() if one]

    def stored(self) -> str:
        """The CONFIRMED verdict alone: line 1. Line 2 is the raw set the
        script compares its next run against (`raw()` reads that one)."""
        return self._line(1)

    def raw(self) -> str:
        """The raw (unconfirmed included) set the last run found."""
        return self._line(2)

    def _line(self, number: int) -> str:
        if not self.verdict.exists():
            return ""

        lines = self.verdict.read_text(encoding="utf-8").splitlines()

        return lines[number - 1] if len(lines) >= number else ""


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    return Rig(tmp_path)


# --- a healthy host ----------------------------------------------------------


def test_a_healthy_host_is_quiet_and_exits_zero(rig: Rig) -> None:
    done = rig.run()

    assert done.returncode == 0, done.stdout + done.stderr
    assert rig.posts() == []
    assert rig.stored() == "up"


def test_a_second_healthy_run_pushes_nothing(rig: Rig) -> None:
    rig.run()
    rig.run()

    assert rig.posts() == []


def test_a_healthy_run_asks_all_five_questions(rig: Rig) -> None:
    """Five checks, and each one has to actually run. A check that was
    skipped is a check that cannot find an outage."""
    rig.run()
    lines = rig.stub_log.read_text(encoding="utf-8")

    assert "/healthz" in lines
    assert "--unix-socket" in lines
    assert all(f"is-failed {one}" in lines for one in UNITS)
    assert SYNC_TIMER in lines
    assert SYNC_SERVICE in lines


# --- where the PEP is --------------------------------------------------------


def test_the_pep_is_dialled_on_the_sites_address(rig: Rig) -> None:
    """The root conftest points `AGENT_SITE_FILE` at the example site."""
    rig.run()

    address = os.environ["AGENT_LAN_ADDRESS"]
    assert f"http://{address}:8300/healthz" in rig.stub_log.read_text(encoding="utf-8")


def test_an_explicit_pep_url_still_wins(rig: Rig, tmp_path: Path) -> None:
    done = rig.run(WATCHDOG_PEP_URL="http://pep.test:1", AGENT_SITE_FILE=str(tmp_path / "none"))

    assert done.returncode == 0, done.stdout + done.stderr
    assert "http://pep.test:1/healthz" in rig.stub_log.read_text(encoding="utf-8")


def test_no_site_file_stops_with_one_line_naming_it(rig: Rig, tmp_path: Path) -> None:
    absent = tmp_path / "absent.env"

    done = rig.run(AGENT_SITE_FILE=str(absent))

    assert done.returncode != 0
    assert done.stderr.count("\n") == 1
    assert "AGENT_LAN_ADDRESS" in done.stderr
    assert str(absent) in done.stderr
    assert rig.posts() == []


# --- a lone failure: recorded, not pushed ------------------------------------


def test_a_single_failure_is_recorded_but_not_pushed(rig: Rig) -> None:
    """THE FALSE ALARM THIS FIX REMOVES. `agent-control-deploy` restarts
    the PEP on purpose and `/healthz` is away for 10-15 s; a lone probe
    landing in that window must not page the operator for a restart they ordered."""
    done = rig.run(WD_PEP_CODE="000")

    assert done.returncode == 1, "a failed check still turns the unit red at once"
    assert rig.posts() == []
    assert rig.stored() == "up", "not yet confirmed, so the verdict has not moved"
    assert "pep: " in done.stdout
    assert ", 1 of 2" in done.stdout
    assert "DOWN: " not in done.stdout


def test_a_blip_that_clears_never_confirms(rig: Rig) -> None:
    """A restart that finishes inside the next minute recovers before the
    second probe. The streak must reset, not carry over."""
    rig.run(WD_PEP_CODE="000")
    rig.run()
    done = rig.run(WD_PEP_CODE="000")

    assert done.returncode == 1
    assert rig.stored() == "up"
    assert rig.posts() == []
    assert ", 1 of 2" in done.stdout, "this is a FRESH first failure, not a second one"


def test_two_consecutive_failures_confirm_and_push(rig: Rig) -> None:
    rig.run(WD_PEP_CODE="000")
    done = rig.run(WD_PEP_CODE="000")

    assert done.returncode == 1
    assert rig.stored() == "pep"
    assert len(rig.posts()) == 1
    assert "DOWN: " in done.stdout


# --- each of the four finds its own outage (two consecutive runs) -------------


def test_a_dead_pep_is_found_and_pushed_once(rig: Rig) -> None:
    """A PEP that answers nothing leaves every document saying `in_sync`,
    so no other check finds it."""
    rig.run(WD_PEP_CODE="000")
    first = rig.run(WD_PEP_CODE="000")

    assert first.returncode == 1
    assert rig.stored() == "pep"
    assert len(rig.posts()) == 1
    assert "rework DOWN" in rig.posts()[0]
    assert "healthz" in rig.posts()[0]


def test_a_manager_that_stopped_publishing_is_found(rig: Rig) -> None:
    """A dead `managerd` leaves every status document saying `in_sync`,
    because a dead writer changes nothing. Only the AGE of the newest
    document says so. This still confirms in two runs, not one: the 180 s
    staleness floor already carries most of the slack, and an outage of
    hours does not notice one more minute."""
    rig.publish("chat", 4000)
    rig.run()
    done = rig.run()

    assert done.returncode == 1
    assert rig.stored() == "managerd"
    assert "no status document written" in rig.posts()[0]


def test_one_fresh_document_is_enough(rig: Rig) -> None:
    """The NEWEST stamp, not every stamp. A family that was deleted
    leaves a stale document behind, and `managerd` is plainly alive."""
    rig.publish("chat", 4000)
    rig.publish("vault-oracle", 5)

    assert rig.run().returncode == 0


def test_a_host_with_no_status_document_is_down(rig: Rig) -> None:
    """A reconciler that has published nothing is not a healthy one."""
    (rig.families / "chat" / "status.json").unlink()
    rig.run()
    done = rig.run()

    assert done.returncode == 1
    assert rig.stored() == "managerd"


def test_a_missing_socket_is_found(rig: Rig) -> None:
    rig.socket.unlink()
    rig.run()
    done = rig.run()

    assert done.returncode == 1
    assert rig.stored() == "sessiond"
    assert "no socket" in rig.posts()[0]


def test_a_socket_that_answers_nothing_is_found(rig: Rig) -> None:
    """A socket file outlives the process that bound it. Its existence is
    not an answer."""
    rig.run(WD_SOCK_CODE="000")
    done = rig.run(WD_SOCK_CODE="000")

    assert done.returncode == 1
    assert rig.stored() == "sessiond"


def test_a_failed_unit_is_found_and_named(rig: Rig) -> None:
    rig.run(WD_FAILED_UNITS="creche-noticeboard.service")
    done = rig.run(WD_FAILED_UNITS="creche-noticeboard.service")

    assert done.returncode == 1
    assert rig.stored() == "units"
    assert "creche-noticeboard.service" in rig.posts()[0]


# --- check 5: the registry still reaches this host ---------------------------


def test_a_sync_that_stopped_firing_is_found(rig: Rig) -> None:
    """THE REGRESSION THIS CHECK IS FOR. When nothing pulls
    `/srv/agents/registry`, every service stays up, every document says
    `in_sync`, and every family change the operator merges reaches nothing.
    Nothing else on this host can tell that apart from a quiet morning."""
    rig.sync_ran(12)
    rig.run()
    done = rig.run()

    assert done.returncode == 1
    assert rig.stored() == "registry"
    assert len(rig.posts()) == 1
    assert "registry" in rig.posts()[0]


def test_a_sync_that_refused_its_last_run_is_found(rig: Rig) -> None:
    """A fresh stamp and a failed run is the shape of a checkout that
    diverged: the sync ran, refused, and the stamp stayed where the last
    good run left it. `Result` is what names it in the same minute."""
    rig.run(WD_SYNC_RESULT="exit-code")
    done = rig.run(WD_SYNC_RESULT="exit-code")

    assert done.returncode == 1
    assert rig.stored() == "registry"
    assert "exit-code" in rig.posts()[0]


def test_a_sync_that_never_ran_is_found(rig: Rig) -> None:
    """An enabled timer with no stamp behind it is a sync that has never
    ended well — a wrong deploy key, most likely."""
    rig.forget_the_sync()
    rig.run()
    done = rig.run()

    assert done.returncode == 1
    assert rig.stored() == "registry"


def test_a_single_stale_sync_is_recorded_but_not_pushed(rig: Rig) -> None:
    """The two-failure rule applies here like everywhere else. A fetch
    that ran long enough to miss one firing must not page the operator."""
    rig.sync_ran(12)
    done = rig.run()

    assert done.returncode == 1
    assert rig.posts() == []
    assert ", 1 of 2" in done.stdout


def test_a_host_without_the_sync_timer_is_never_alarmed(rig: Rig) -> None:
    """A host that has not taken this cutover, or one after a `rollback`,
    has no timer to be stale. A check that cannot be satisfied is a
    watchdog that cries every minute for ever."""
    rig.forget_the_sync()
    rig.run(WD_SYNC_ENABLED="disabled")
    done = rig.run(WD_SYNC_ENABLED="disabled")

    assert done.returncode == 0, done.stdout + done.stderr
    assert rig.posts() == []
    assert rig.stored() == "up"


def test_the_sync_check_never_dials_github(rig: Rig) -> None:
    """The question "is the checkout behind `main`?" cannot be asked
    without a credential and a network call, and this watchdog holds
    neither. It asks whether the SYNC is running instead.

    Read off the CODE, not the whole file: the comment beside the check
    says the word, because saying why it is not asked is the point.
    """
    rig.run()
    called = rig.stub_log.read_text(encoding="utf-8").lower()
    code = [
        one.lower()
        for one in SCRIPT.read_text(encoding="utf-8").splitlines()
        if not one.lstrip().startswith("#")
    ]

    assert "github" not in called
    assert not [one for one in code if "github" in one or "git " in one]


def test_a_sync_the_checkout_recovers_clears_the_verdict(rig: Rig) -> None:
    """One "recovered" card when the sync starts moving the checkout
    again, like every other check."""
    rig.sync_ran(12)
    rig.run()
    rig.run()
    rig.sync_ran(0)
    done = rig.run()

    assert done.returncode == 0, done.stdout + done.stderr
    assert rig.stored() == "up"
    assert len(rig.posts()) == 2
    assert "recovered" in rig.posts()[1]


def test_two_things_down_are_one_push_that_names_both(rig: Rig) -> None:
    rig.run(WD_PEP_CODE="000", WD_FAILED_UNITS="agent-managerd.service")
    rig.run(WD_PEP_CODE="000", WD_FAILED_UNITS="agent-managerd.service")

    assert rig.stored() == "pep units"
    assert len(rig.posts()) == 1
    assert "healthz" in rig.posts()[0]
    assert "agent-managerd.service" in rig.posts()[0]


# --- one push per outage, and one per recovery --------------------------------


def test_a_long_outage_is_one_push_not_one_a_minute(rig: Rig) -> None:
    """The whole point of the state file. Five hours at one run a minute
    is 322 runs; the operator gets ONE card (after the first, unconfirmed, run)."""
    for _ in range(5):
        rig.run(WD_PEP_CODE="000")

    assert len(rig.posts()) == 1


def test_a_detail_that_moves_does_not_push_again(rig: Rig) -> None:
    """The verdict is keyed on WHICH checks are down, never on their
    detail. An age in seconds grows every run, and pushing on that would
    be one card a minute wearing the fix's clothes."""
    rig.publish("chat", 4000)
    rig.run()
    rig.publish("chat", 9000)
    rig.run()
    rig.publish("chat", 15000)
    rig.run()

    assert len(rig.posts()) == 1


def test_recovery_pushes_once_and_says_what_came_back(rig: Rig) -> None:
    rig.run(WD_PEP_CODE="000")
    rig.run(WD_PEP_CODE="000")
    done = rig.run()

    assert done.returncode == 0
    assert rig.stored() == "up"
    assert len(rig.posts()) == 2
    assert "rework recovered" in rig.posts()[1]
    assert "pep" in rig.posts()[1]


def test_recovery_needs_no_second_clean_run(rig: Rig) -> None:
    """Missing an early recovery costs nothing; delaying one costs a
    false "still down" card. Only the DOWN side waits for confirmation."""
    rig.run(WD_PEP_CODE="000")
    rig.run(WD_PEP_CODE="000")
    done = rig.run()

    assert done.returncode == 0
    assert rig.stored() == "up"


def test_a_verdict_that_grows_pushes_again(rig: Rig) -> None:
    """A second service going down is news, and the first card does not
    say so."""
    rig.run(WD_PEP_CODE="000")
    rig.run(WD_PEP_CODE="000")
    rig.run(WD_PEP_CODE="000", WD_SOCK_CODE="000")
    done = rig.run(WD_PEP_CODE="000", WD_SOCK_CODE="000")

    assert done.returncode == 1
    assert rig.stored() == "pep sessiond"
    assert len(rig.posts()) == 2


def test_a_notice_that_cannot_be_sent_is_retried(rig: Rig) -> None:
    """Node-RED restarts too. A push that failed must not be recorded as
    made, or the outage is never told at all."""
    rig.run(WD_PEP_CODE="000")
    first = rig.run(WD_PEP_CODE="000", WD_POST_EXIT="7")

    assert first.returncode == 1
    assert "NOTICE NOT SENT" in first.stdout
    assert rig.stored() == "up", "a failed push must not be recorded as confirmed"

    second = rig.run(WD_PEP_CODE="000")

    assert rig.stored() == "pep"
    assert second.returncode == 1


def test_no_hook_configured_still_says_it_in_the_journal(rig: Rig) -> None:
    """A host without the approval bearers is a host where the alarm can
    only write to the journal. It must still run every check and still
    exit non-zero."""
    _write(rig.hooks, "LITELLM_MASTER_KEY=other\n")
    rig.run(WD_PEP_CODE="000")
    done = rig.run(WD_PEP_CODE="000")

    assert done.returncode == 1
    assert "NOTICE NOT SENT" in done.stdout
    assert rig.posts() == []


# --- where the two bearers come from -----------------------------------------


def test_the_old_env_file_is_a_fallback_and_says_so(rig: Rig) -> None:
    """Nothing writes /srv/agents/state/materializer/env any more. A host
    that has not run `up` yet still has the file, so the alarm still reads
    it — and says in the journal that it did."""
    rig.hooks.unlink()
    _write(
        rig.old_env,
        f"LITELLM_MASTER_KEY=other\nAPPROVAL_URL={HOOK_URL}\nAPPROVAL_TOKEN={TOKEN}\n",
    )

    rig.run(WD_PEP_CODE="000")
    done = rig.run(WD_PEP_CODE="000")

    assert rig.posts(), "the fallback did not reach the hook"
    assert "DEPRECATED" in done.stderr
    assert "srv/agents/state/materializer/env" in done.stderr
    assert TOKEN not in done.stderr


def test_the_new_file_wins_over_the_old_one(rig: Rig) -> None:
    """`up` writes hooks.env and leaves the old file alone. While both are
    there the alarm reads the new one, and says nothing about the old."""
    _write(rig.old_env, "APPROVAL_URL=http://192.0.2.30:1/stale\nAPPROVAL_TOKEN=stale\n")

    rig.run(WD_PEP_CODE="000")
    done = rig.run(WD_PEP_CODE="000")

    assert rig.posts(), "no push at all"
    assert all("stale" not in one for one in rig.posts())
    assert "DEPRECATED" not in done.stderr


# --- secrets, and the last verdict ---------------------------------------------


def test_the_bearer_never_reaches_argv_or_stdout(rig: Rig) -> None:
    """`/proc/<pid>/cmdline` is world readable for the life of the curl
    process on a host that sets no hidepid (bin/AGENTS.md §Secrets)."""
    rig.run(WD_PEP_CODE="000")
    done = rig.run(WD_PEP_CODE="000")

    assert TOKEN not in done.stdout
    assert TOKEN not in done.stderr
    assert TOKEN not in rig.stub_log.read_text(encoding="utf-8")
    assert all(TOKEN not in one for one in rig.posts())


def test_the_header_file_is_removed_after_the_push(rig: Rig) -> None:
    rig.run(WD_PEP_CODE="000")
    rig.run(WD_PEP_CODE="000")
    posted = rig.posts()[0]
    header = next(one for one in posted.split() if one.startswith("@"))

    assert not Path(header.lstrip("@")).exists()


def test_last_prints_the_stored_verdict_and_nothing_else(rig: Rig) -> None:
    rig.run(WD_PEP_CODE="000")
    rig.run(WD_PEP_CODE="000")
    before = len(rig.posts())
    done = rig.run("--last")

    assert done.returncode == 0
    assert done.stdout.strip() == "pep"
    assert len(rig.posts()) == before, "--last must check nothing and push nothing"
    assert "is-failed" not in rig.stub_log.read_text(encoding="utf-8").rsplit("\n", 2)[-1]


def test_the_real_date_of_this_machine_parses_a_stamp(rig: Rig) -> None:
    """The stub proves the script tries the GNU form first. This proves
    the fall-through against the `date` that is actually installed: on a
    Mac the GNU form fails with "illegal option -- d" and the BSD form
    answers, on the host the GNU form answers and the BSD form is never
    reached. One test, both platforms, no branch on the platform's name.
    """
    (rig.stubs / "date").unlink()
    rig.publish("chat", 5)

    assert rig.run().returncode == 0, "a fresh document read as stale"

    rig.publish("chat", 4000)
    rig.run()
    done = rig.run()

    assert done.returncode == 1
    assert rig.stored() == "managerd"
    assert "could not read the time" not in done.stdout


def test_last_before_any_run_says_unknown(rig: Rig) -> None:
    done = rig.run("--last")

    assert done.returncode == 0
    assert done.stdout.startswith("unknown")


def test_an_unknown_argument_is_refused(rig: Rig) -> None:
    done = rig.run("--send-everything")

    assert done.returncode == 2
    assert rig.posts() == []
