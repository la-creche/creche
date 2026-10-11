"""What `bin/rework-watchdog.sh` can be held to without the host.

A rework service can die in silence, and nothing else would tell the operator.
This script tells the operator's phone inside five minutes, so the cases that matter
most are the ones about SAYING it, and saying it once.

**A check must fail on two CONSECUTIVE runs before it pushes.**
`creche-deploy` and the release visit restart the PEP on
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
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING, Final

import pytest
from chaperone.family_ids import FAMILY_NAME_RE

if TYPE_CHECKING:
    from collections.abc import Sequence

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
SCRIPT: Final = REPO_ROOT / "bin" / "rework-watchdog.sh"

#: The five units the script asks `systemctl --user is-failed` about.
UNITS: Final = (
    "creche-attendance.service",
    "creche-caregiver.service",
    "creche-door-owui.service",
    "creche-trigger-webhooks.service",
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

#: The two ids of the outage notice, and the two ids of the notice of a
#: refused JSON text. The pairs differ, so one card does not replace the
#: other on the phone.
OUTAGE_IDS: Final = ("yyyyyyyyyyyyyyyyyyyyyyyyyy", "0000000000000001")
JSON_IDS: Final = ("xxxxxxxxxxxxxxxxxxxxxxxxxx", "0000000000000002")

#: The first word of the summary of a notice of a refused JSON text.
JSON_WORD: Final = "json_not_strict"
SUMMARY: Final = re.compile(r'"summary":"([^"]*)"')

#: The modes that a service gives its notice file and the directory of it.
NOTICE_DIR_MODE: Final = 0o750
NOTICE_FILE_MODE: Final = 0o640

#: The most bytes of a notice file that the script reads.
NOTICE_READ_CAP: Final = 65536
#: The most bytes of its own record that the script reads.
SENT_READ_CAP: Final = 262144

MINUTE_S: Final = 60
HOUR_S: Final = 3600
DAY_S: Final = 86400

#: The time of a refusal in a token: a count of seconds from 1970.
REFUSED_AT: Final = 1_760_000_000

#: Seven pairs of a surface and a rule, one more than the script sends in
#: one hour.
SEVEN_PAIRS: Final = (
    "status.document:syntax",
    "status.document:not_utf8",
    "status.fault_file:byte_order_mark",
    "status.outcome:constant",
    "audit.line:trailing_data",
    "session.answer:too_deep",
    "session.journal_line:lone_surrogate",
)

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
#:
#: `list-units` and `show -p ExecMainStatus` are the firing units' half of
#: check 4. `WD_FIRINGS` holds one word for each instance, in the form
#: `<unit>=<status of its last run>`. `list-units` prints one line for each
#: word, in the column form of `--plain --no-legend`. `WD_SHOW_EXIT` is the
#: status of each `show -p ExecMainStatus` call: 124 is what `timeout` gives
#: for a call that it stopped.
SYSTEMCTL_BODY: Final = """case "$*" in
  *list-units*)
    for one in ${WD_FIRINGS:-}; do
      printf '%s loaded failed failed Fire one cron trigger\\n' "${one%%=*}"
    done ;;
  *ExecMainStatus*)
    for one in ${WD_FIRINGS:-}; do
      case " $* " in *" ${one%%=*} "*) printf '%s\\n' "${one##*=}" ;; esac
    done
    exit "${WD_SHOW_EXIT:-0}" ;;
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
#:
#: `WD_CLOCK_AHEAD_S` moves that clock: `date -u +%s` then answers the real
#: time plus that count of seconds. A case uses it to run the script one hour
#: or one day later. `Rig.run_later` sets it.
#:
#: `WD_DATE_FAIL_AT` makes one `date -u +%s` call fail with no output: the
#: call with that number, from 1. The stub counts the calls in its log, so
#: the number counts each run of one rig. Check 2 makes one such call in a
#: run, before the notice step.
DATE_BODY: Final = """case "$1" in
  -u)
    shift
    case "${1:-}" in
      -d) shift; parsed="$1"; shift ;;
      -j) shift; shift; shift; parsed="$1"; shift ;;
      +%s)
        calls="$(grep -c '^date -u +%s$' "$WD_STUB_LOG")"
        [ "$calls" = "${WD_DATE_FAIL_AT:-}" ] && exit 1
        real="$(/bin/date -u +%s)"
        printf '%s\\n' "$(( real + ${WD_CLOCK_AHEAD_S:-0} ))"
        exit 0 ;;
      *) exec /bin/date -u "$@" ;;
    esac
    # The stub's own backend must be portable too: CI is Linux, whose `date`
    # has no `-j`. GNU first, BSD second, the order the script itself uses.
    /bin/date -u -d "$parsed" "$@" 2>/dev/null && exit 0
    exec /bin/date -u -j -f '%Y-%m-%dT%H:%M:%SZ' "$parsed" "$@" ;;
esac
exec /bin/date "$@"
"""

#: `mktemp` and `awk` are the real programs. `WD_MKTEMP_EXIT` makes `mktemp`
#: fail with that status and no file: the script then cannot write its
#: record of sent notices. `WD_AWK_EXIT` makes `awk` fail with that status
#: and no output: the script then has no answer on the two limits.
REAL_BODY: Final = """[ -n "${{{switch}:-}}" ] && exit "${switch}"
{more}
exec {real} "$@"
"""

#: `WD_MKTEMP_FAIL_AT` makes one `mktemp` call for the record fail: the call
#: with that number, from 1. The stub counts the calls in its log, so the
#: number counts each run of one rig.
MKTEMP_MORE: Final = """case "$*" in
  *notices-sent.*)
    calls="$(grep -c '^mktemp .*notices-sent[.]' "$WD_STUB_LOG")"
    [ "$calls" = "${WD_MKTEMP_FAIL_AT:-}" ] && exit 1 ;;
esac"""

#: `WD_AWK_KEY_EXIT` makes one kind of `awk` call fail with that status: the
#: call that gets the variable `key`. That call makes the new record after a
#: send.
AWK_MORE: Final = """case "$*" in
  *"key="*) [ -n "${WD_AWK_KEY_EXIT:-}" ] && exit "$WD_AWK_KEY_EXIT" ;;
esac"""


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


def token(pair: str, seconds: int = REFUSED_AT) -> str:
    """One token of the text `push`: `<surface>:<rule>:<seconds>`."""
    return f"{pair}:{seconds}"


def notice_document(service: str, tokens: Sequence[str]) -> str:
    """The document of a notice file, as its writer shapes it: one row for
    each pair, the text `push` with one token for each pair, sorted keys and
    an indent of one space. The text `push` is then one line of the file."""
    rows: list[dict[str, object]] = []
    for one in tokens:
        surface, rule, seconds = one.split(":")
        at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(seconds)))
        rows.append(
            {
                "surface": surface,
                "rule": rule,
                "family": None,
                "count": 1,
                "first_at": at,
                "last_at": at,
            }
        )

    document = {"kind": "notices", "service": service, "rows": rows, "push": " ".join(tokens)}

    return json.dumps(document, indent=1, sort_keys=True) + "\n"


def push_line(value: str) -> str:
    """The line of the key `push` in a notice file."""
    return f' "push": "{value}",'


def _write_notice(path: Path, body: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(NOTICE_DIR_MODE)
    path.write_bytes(body)
    path.chmod(NOTICE_FILE_MODE)


def _stamp(seconds_ago: int) -> str:
    """An RFC 3339 stamp that many seconds in the past, in the shape
    `caregiver/src/caregiver/clock.py` writes."""
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

        self.notices = self.state / "notices"
        self.pep_notices = self.state / "faults/pep/_notices.json"
        self.sent = self.state / "watchdog/notices-sent"

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
        for name, switch, more in (
            ("mktemp", "WD_MKTEMP_EXIT", MKTEMP_MORE),
            ("awk", "WD_AWK_EXIT", AWK_MORE),
        ):
            body = REAL_BODY.format(switch=switch, more=more, real=shutil.which(name))
            _stub(self.stubs, name, body)

        #: The script of a run. One case runs a changed copy.
        self.script = SCRIPT

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
            ["bash", str(self.script), *args], capture_output=True, text=True, env=env, timeout=120
        )

    def run_later(self, ahead_s: int, **overrides: str) -> subprocess.CompletedProcess[str]:
        """One run with the clock that many seconds ahead. The status
        document gets a stamp of that moment, so check 2 reads it as fresh."""
        self.publish("chat", 5 - ahead_s)

        return self.run(WD_CLOCK_AHEAD_S=str(ahead_s), **overrides)

    def notice(self, service: str, *tokens: str) -> Path:
        """The notice file of one service, with one token for each pair."""
        path = self.notices / f"{service}.json"
        _write_notice(path, notice_document(service, tokens).encode())

        return path

    def notice_with_line(self, service: str, line: bytes) -> Path:
        """A notice file whose line of the key `push` is these bytes."""
        whole = notice_document(service, [token("status.document:syntax")]).encode()
        valid = push_line(token("status.document:syntax")).encode()
        assert valid in whole
        path = self.notices / f"{service}.json"
        _write_notice(path, whole.replace(valid, line))

        return path

    def pep_notice(self, *tokens: str) -> None:
        """The notice file of the chaperone, in its fault directory."""
        _write_notice(self.pep_notices, notice_document("chaperone", tokens).encode())

    def json_summaries(self) -> list[str]:
        """The summary of each notice of a refused JSON text, in send order."""
        found = [SUMMARY.search(one) for one in self.posts()]

        return [one.group(1) for one in found if one and one.group(1).startswith(JSON_WORD)]

    def sent_lines(self) -> list[str]:
        """The record of sent notices: one line for each sent pair."""
        if not self.sent.exists():
            return []

        return self.sent.read_text(encoding="utf-8").splitlines()

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
    done = rig.run(
        WATCHDOG_PEP_URL="http://chaperone.test:1", AGENT_SITE_FILE=str(tmp_path / "none")
    )

    assert done.returncode == 0, done.stdout + done.stderr
    assert "http://chaperone.test:1/healthz" in rig.stub_log.read_text(encoding="utf-8")


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
    """THE FALSE ALARM THIS FIX REMOVES. `creche-deploy` restarts
    the PEP on purpose and `/healthz` is away for 10-15 s; a lone probe
    landing in that window must not page the operator for a restart they ordered."""
    done = rig.run(WD_PEP_CODE="000")

    assert done.returncode == 1, "a failed check still turns the unit red at once"
    assert rig.posts() == []
    assert rig.stored() == "up", "not yet confirmed, so the verdict has not moved"
    assert "chaperone: " in done.stdout
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
    assert rig.stored() == "chaperone"
    assert len(rig.posts()) == 1
    assert "DOWN: " in done.stdout


# --- each of the four finds its own outage (two consecutive runs) -------------


def test_a_dead_pep_is_found_and_pushed_once(rig: Rig) -> None:
    """A PEP that answers nothing leaves every document saying `in_sync`,
    so no other check finds it."""
    rig.run(WD_PEP_CODE="000")
    first = rig.run(WD_PEP_CODE="000")

    assert first.returncode == 1
    assert rig.stored() == "chaperone"
    assert len(rig.posts()) == 1
    assert "rework DOWN" in rig.posts()[0]
    assert "healthz" in rig.posts()[0]


def test_a_manager_that_stopped_publishing_is_found(rig: Rig) -> None:
    """A dead `caregiver` leaves every status document saying `in_sync`,
    because a dead writer changes nothing. Only the AGE of the newest
    document says so. This still confirms in two runs, not one: the 180 s
    staleness floor already carries most of the slack, and an outage of
    hours does not notice one more minute."""
    rig.publish("chat", 4000)
    rig.run()
    done = rig.run()

    assert done.returncode == 1
    assert rig.stored() == "caregiver"
    assert "no status document written" in rig.posts()[0]


def test_one_fresh_document_is_enough(rig: Rig) -> None:
    """The NEWEST stamp, not every stamp. A family that was deleted
    leaves a stale document behind, and `caregiver` is plainly alive."""
    rig.publish("chat", 4000)
    rig.publish("vault-oracle", 5)

    assert rig.run().returncode == 0


def test_a_host_with_no_status_document_is_down(rig: Rig) -> None:
    """A reconciler that has published nothing is not a healthy one."""
    (rig.families / "chat" / "status.json").unlink()
    rig.run()
    done = rig.run()

    assert done.returncode == 1
    assert rig.stored() == "caregiver"


def test_a_missing_socket_is_found(rig: Rig) -> None:
    rig.socket.unlink()
    rig.run()
    done = rig.run()

    assert done.returncode == 1
    assert rig.stored() == "attendance"
    assert "no socket" in rig.posts()[0]


def test_a_socket_that_answers_nothing_is_found(rig: Rig) -> None:
    """A socket file outlives the process that bound it. Its existence is
    not an answer."""
    rig.run(WD_SOCK_CODE="000")
    done = rig.run(WD_SOCK_CODE="000")

    assert done.returncode == 1
    assert rig.stored() == "attendance"


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
    rig.run(WD_PEP_CODE="000", WD_FAILED_UNITS="creche-caregiver.service")
    rig.run(WD_PEP_CODE="000", WD_FAILED_UNITS="creche-caregiver.service")

    assert rig.stored() == "chaperone units"
    assert len(rig.posts()) == 1
    assert "healthz" in rig.posts()[0]
    assert "creche-caregiver.service" in rig.posts()[0]


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
    assert "chaperone" in rig.posts()[1]


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
    assert rig.stored() == "chaperone attendance"
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

    assert rig.stored() == "chaperone"
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
    assert done.stdout.strip() == "chaperone"
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
    assert rig.stored() == "caregiver"
    assert "could not read the time" not in done.stdout


def test_last_before_any_run_says_unknown(rig: Rig) -> None:
    done = rig.run("--last")

    assert done.returncode == 0
    assert done.stdout.startswith("unknown")


def test_an_unknown_argument_is_refused(rig: Rig) -> None:
    done = rig.run("--send-everything")

    assert done.returncode == 2
    assert rig.posts() == []


# --- check 4: a firing unit that ended with status 78 -------------------------

FIRING: Final = "creche-trigger@standup.service"


def test_check_4_reads_each_firing_unit(rig: Rig) -> None:
    """The words of the two calls, and one `show` call for each instance."""
    other = "creche-trigger@ops.service"

    done = rig.run(WD_FIRINGS=f"{FIRING}=0 {other}=0")
    lines = rig.stub_log.read_text(encoding="utf-8").splitlines()

    assert done.returncode == 0, done.stdout + done.stderr
    listing = "systemctl --user list-units --all --plain --no-legend creche-trigger@*.service"
    assert lines.count(listing) == 1
    for unit in (FIRING, other):
        assert lines.count(f"systemctl --user show {unit} -p ExecMainStatus --value") == 1


def test_a_firing_that_ended_with_78_is_a_failed_unit(rig: Rig) -> None:
    """Status 78 is the status of a start that the config refuses. The rule
    of two runs in a row holds for it as for each other check."""
    first = rig.run(WD_FIRINGS=f"{FIRING}=78")

    assert first.returncode == 1
    assert "units: " in first.stdout
    assert ", 1 of 2" in first.stdout
    assert rig.posts() == []

    second = rig.run(WD_FIRINGS=f"{FIRING}=78")

    assert second.returncode == 1
    assert rig.stored() == "units"
    assert len(rig.posts()) == 1
    assert "rework DOWN" in rig.posts()[0]
    assert FIRING in rig.posts()[0]
    assert "78" in rig.posts()[0]


@pytest.mark.parametrize("status", ["0", "1", "2", "143", "780", "", "seventy-eight"])
def test_a_firing_with_another_status_is_no_failure(rig: Rig, status: str) -> None:
    """A firing ends with status 1 when the session service refuses the job.
    Only status 78 names a config."""
    rig.run(WD_FIRINGS=f"{FIRING}={status}")
    done = rig.run(WD_FIRINGS=f"{FIRING}={status}")

    assert done.returncode == 0, done.stdout + done.stderr
    assert rig.posts() == []
    assert rig.stored() == "up"


def test_a_show_call_with_no_answer_ends_the_read(rig: Rig) -> None:
    """Each call has the time limit of the other checks. After one call that
    the limit stopped, the run reads no other firing unit: a user manager
    that does not answer costs the run one time limit, not one for each
    instance. A call with no answer is no failure, as in the rest of check 4."""
    units = ("creche-trigger@ops.service", FIRING, "creche-trigger@vault.service")

    done = rig.run(WD_FIRINGS=" ".join(f"{one}=78" for one in units), WD_SHOW_EXIT="124")
    calls = [
        one
        for one in rig.stub_log.read_text(encoding="utf-8").splitlines()
        if one.startswith("systemctl ") and "ExecMainStatus" in one
    ]

    assert len(calls) == 1
    assert done.returncode == 0, done.stdout + done.stderr
    assert done.stdout.count("no answer") == 1
    assert rig.posts() == []


def test_a_firing_and_a_failed_unit_are_one_units_failure(rig: Rig) -> None:
    states = {"WD_FIRINGS": f"{FIRING}=78", "WD_FAILED_UNITS": "creche-caregiver.service"}
    rig.run(**states)
    rig.run(**states)

    assert rig.stored() == "units"
    assert len(rig.posts()) == 1
    assert FIRING in rig.posts()[0]
    assert "creche-caregiver.service" in rig.posts()[0]


@pytest.mark.parametrize(
    "unit",
    [
        "creche-trigger@Standup.service",
        "creche-trigger@s.service",
        "creche-trigger@-standup.service",
        "creche-trigger@standup.timer",
        "creche-trigger@stand.up.service",
        "creche-trigger@" + "a" * 32 + ".service",
        "creche-other@standup.service",
        "--version",
    ],
)
def test_a_listed_name_that_is_no_firing_unit_is_not_read(rig: Rig, unit: str) -> None:
    """The instance of a firing unit is the name of a family. The script
    gives no other listed word to `systemctl show`."""
    done = rig.run(WD_FIRINGS=f"{unit}=78")
    called = rig.stub_log.read_text(encoding="utf-8")

    assert done.returncode == 0, done.stdout + done.stderr
    assert "ExecMainStatus" not in called
    assert done.stdout.count("is no firing unit") == 1, done.stdout
    assert rig.posts() == []


def _constant(name: str) -> str:
    """The one line of the script that sets a constant."""
    start = f"{name}="
    lines = [
        one for one in SCRIPT.read_text(encoding="utf-8").splitlines() if one.startswith(start)
    ]
    assert len(lines) == 1, name

    return lines[0]


def _number(name: str) -> int:
    """The value of a constant of the script that is a count."""
    return int(_constant(name).partition("=")[2])


def test_the_family_pattern_equals_its_source() -> None:
    """The instance of a firing unit is the name of a family. The script
    cannot import the chaperone, so it has a copy of the pattern of a family
    name. This case fails when the pattern of the chaperone moves."""
    source = FAMILY_NAME_RE.pattern
    assert source.startswith("^")
    assert source.endswith(r"\Z")

    family = source.removeprefix("^").removesuffix(r"\Z")

    assert _constant("FAMILY_NAME") == f"FAMILY_NAME='{family}'"


def test_both_unit_patterns_come_from_the_same_constants() -> None:
    """The pattern for `list-units` and the exact form of a unit name have
    one head, one tail and one pattern of a family name."""
    names = ("FIRING_UNIT_HEAD", "FIRING_UNIT_TAIL", "FAMILY_NAME", "FIRING_UNITS")
    program = "\n".join(_constant(one) for one in (*names, "FIRING_UNIT_NAME"))
    program += '\nprintf \'%s\\n\' "$FIRING_UNITS" "$FIRING_UNIT_NAME"\n'
    family = FAMILY_NAME_RE.pattern.removeprefix("^").removesuffix(r"\Z")

    done = subprocess.run(["bash", "-c", program], capture_output=True, text=True, timeout=60)

    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines() == [
        "creche-trigger@*.service",
        f"creche-trigger@{family}\\.service",
    ]


# --- the notice of a refused JSON text ----------------------------------------


def test_one_token_gives_one_json_notice(rig: Rig) -> None:
    """A notice file from before the first run of the script counts too:
    the script sends each token that it never sent."""
    rig.notice("noticeboard", token("status.document:syntax"))

    done = rig.run()

    assert done.returncode == 0, done.stdout + done.stderr
    assert rig.json_summaries() == [f"{JSON_WORD} noticeboard status.document syntax"]
    assert len(rig.posts()) == 1


def test_a_second_run_with_the_same_file_sends_nothing(rig: Rig) -> None:
    rig.notice("noticeboard", token("status.document:syntax"))
    rig.run()

    done = rig.run()

    assert done.returncode == 0, done.stdout + done.stderr
    assert len(rig.posts()) == 1


def test_a_newer_refusal_waits_for_24_hours(rig: Rig) -> None:
    """One notice for one service, surface and rule in 24 hours. A run after
    that time sends the newer refusal."""
    pair = "status.document:syntax"
    summary = f"{JSON_WORD} noticeboard status.document syntax"
    rig.notice("noticeboard", token(pair))
    rig.run()
    rig.notice("noticeboard", token(pair, REFUSED_AT + MINUTE_S))

    rig.run()
    rig.run_later(DAY_S - HOUR_S)

    assert rig.json_summaries() == [summary]

    rig.run_later(DAY_S + MINUTE_S)

    assert rig.json_summaries() == [summary, summary]

    rig.run_later(DAY_S + 2 * MINUTE_S)

    assert rig.json_summaries() == [summary, summary]


def test_an_older_time_for_a_sent_pair_sends_nothing(rig: Rig) -> None:
    pair = "status.document:syntax"
    rig.notice("noticeboard", token(pair))
    rig.run()
    rig.notice("noticeboard", token(pair, REFUSED_AT - MINUTE_S))

    rig.run_later(DAY_S + MINUTE_S)

    assert len(rig.json_summaries()) == 1


def test_a_time_far_after_now_gets_no_notice(rig: Rig) -> None:
    """The clock of a host can jump. A token whose time is more than one
    hour after now gets no notice and no record line, and the journal gets
    one line with the count. A later refusal of the same service, surface
    and rule with a time before now then gets its notice."""
    pair = "status.document:syntax"
    now = int(time.time())
    near = token("audit.line:syntax", now + 30 * MINUTE_S)
    rig.notice("noticeboard", token(pair, now + 400 * DAY_S), near)

    done = rig.run()

    assert done.returncode == 0, done.stdout + done.stderr
    assert rig.json_summaries() == [f"{JSON_WORD} noticeboard audit.line syntax"]
    assert done.stdout.count("after now (no notice): 1") == 1, done.stdout
    assert [one.split()[1] for one in rig.sent_lines()] == ["audit.line"]

    rig.notice("noticeboard", token(pair, now - MINUTE_S), near)
    done = rig.run()

    assert rig.json_summaries()[1:] == [f"{JSON_WORD} noticeboard status.document syntax"]
    assert "after now" not in done.stdout


def test_a_second_token_in_the_file_sends_one_more(rig: Rig) -> None:
    first = token("status.document:syntax")
    rig.notice("noticeboard", first)
    rig.run()
    rig.notice("noticeboard", first, token("audit.line:lone_surrogate", REFUSED_AT + 1))

    rig.run()

    assert rig.json_summaries() == [
        f"{JSON_WORD} noticeboard status.document syntax",
        f"{JSON_WORD} noticeboard audit.line lone_surrogate",
    ]


def test_one_pair_two_times_in_a_line_is_one_notice(rig: Rig) -> None:
    """The pattern of the line permits one pair two times. The second token
    is no news with the same time. With a newer time, the limit of one
    service, surface and rule holds it, also in the same run. The file of
    the chaperone and a file with the name of the chaperone are one service."""
    pair = "status.document:syntax"
    rig.notice("noticeboard", token(pair), token(pair), token(pair, REFUSED_AT + MINUTE_S))
    rig.pep_notice(token("grants.file:syntax"))
    _write_notice(
        rig.notices / "chaperone.json",
        notice_document("chaperone", [token("grants.file:syntax", REFUSED_AT + 1)]).encode(),
    )

    first = rig.run()

    assert rig.json_summaries() == [
        f"{JSON_WORD} chaperone grants.file syntax",
        f"{JSON_WORD} noticeboard status.document syntax",
    ]
    assert "2 sent, 1 held" in first.stdout

    rig.run_later(DAY_S + MINUTE_S)

    assert len(rig.json_summaries()) == 3
    assert rig.json_summaries()[2] == f"{JSON_WORD} noticeboard status.document syntax"


def test_the_same_pair_of_two_services_is_two_notices(rig: Rig) -> None:
    rig.notice("noticeboard", token("status.document:syntax"))
    rig.notice("door-owui", token("status.document:syntax"))

    rig.run()

    assert sorted(rig.json_summaries()) == [
        f"{JSON_WORD} door-owui status.document syntax",
        f"{JSON_WORD} noticeboard status.document syntax",
    ]


@pytest.mark.parametrize("spread", [(7,), (3, 2, 2), (0, 1, 6)])
def test_six_json_notices_in_one_hour_for_all_services(rig: Rig, spread: tuple[int, ...]) -> None:
    """Seven tokens in one run are six notices. A run 61 minutes later sends
    the seventh. The limit counts the notices of all services together."""
    pairs = list(SEVEN_PAIRS)
    expected: set[str] = set()
    for service, count in zip(("attendance", "caregiver", "noticeboard"), spread, strict=False):
        mine, pairs = pairs[:count], pairs[count:]
        if not mine:
            continue

        rig.notice(service, *[token(one) for one in mine])
        expected |= {f"{JSON_WORD} {service} {one.replace(':', ' ')}" for one in mine}

    first = rig.run()

    assert first.returncode == 0, first.stdout + first.stderr
    assert len(rig.json_summaries()) == 6

    rig.run()
    rig.run_later(30 * MINUTE_S)

    assert len(rig.json_summaries()) == 6

    rig.run_later(61 * MINUTE_S)

    assert len(rig.json_summaries()) == 7
    assert set(rig.json_summaries()) == expected


def test_the_chaperone_file_is_read_as_the_chaperone(rig: Rig) -> None:
    """The chaperone keeps its notice file in its fault directory. The
    service name of that file is the word `chaperone`."""
    rig.pep_notice(token("grants.file:duplicate_key"))

    rig.run()

    assert rig.json_summaries() == [f"{JSON_WORD} chaperone grants.file duplicate_key"]


def test_a_run_that_moves_the_verdict_sends_the_json_notice(rig: Rig) -> None:
    """The notice step runs at each end of the script. The run that moves
    the verdict sends the outage notice first, then the notice of a refused
    text."""
    rig.run(WD_PEP_CODE="000")
    rig.notice("noticeboard", token("status.document:syntax"))

    done = rig.run(WD_PEP_CODE="000")

    assert done.returncode == 1, done.stdout + done.stderr
    assert "verdict moved" in done.stdout
    assert len(rig.posts()) == 2
    assert "rework DOWN" in rig.posts()[0]
    assert rig.json_summaries() == [f"{JSON_WORD} noticeboard status.document syntax"]
    assert len(rig.sent_lines()) == 1
    assert rig.stored() == "chaperone"


def test_a_moved_verdict_with_the_hook_away_tries_both_notices(rig: Rig) -> None:
    """The outage notice fails, so the stored verdict stays. The notice step
    still runs: it tries one notice and writes no record line."""
    rig.run(WD_PEP_CODE="000")
    rig.notice("noticeboard", token("status.document:syntax"))

    done = rig.run(WD_PEP_CODE="000", WD_POST_EXIT="7")

    assert done.returncode == 1, done.stdout + done.stderr
    assert "verdict moved" in done.stdout
    assert len(rig.posts()) == 2
    assert "rework DOWN" in rig.posts()[0]
    assert JSON_WORD in rig.posts()[1]
    assert rig.sent_lines() == []
    assert rig.stored() == "up"


def test_a_json_notice_has_ids_of_its_own(rig: Rig) -> None:
    """The outage notice and the notice of a refused text are two cards."""
    rig.notice("noticeboard", token("status.document:syntax"))
    rig.run(WD_PEP_CODE="000")
    rig.run(WD_PEP_CODE="000")

    outage = [one for one in rig.posts() if "rework DOWN" in one]
    refused = [one for one in rig.posts() if JSON_WORD in one]

    assert len(outage) == 1
    assert len(refused) == 1
    for (job, gate), post in ((OUTAGE_IDS, outage[0]), (JSON_IDS, refused[0])):
        assert f'"job_id":"{job}"' in post
        assert f'"gate_id":"{gate}"' in post

    assert set(OUTAGE_IDS).isdisjoint(JSON_IDS)


#: Each line is the line of the key `push` in a notice file, and each one
#: breaks the exact pattern of that line.
BROKEN_PUSH_LINES: Final = {
    "upper-case": b' "push": "Status.document:syntax:1760000000",',
    "hyphen": b' "push": "status-document:syntax:1760000000",',
    "two-spaces": b' "push": "audit.line:syntax:1760000000  status.outcome:syntax:1760000000",',
    "tab": b' "push": "audit.line:syntax:1760000000\tstatus.outcome:syntax:1760000000",',
    "space-first": b' "push": " status.document:syntax:1760000000",',
    "space-last": b' "push": "status.document:syntax:1760000000 ",',
    "thirteen-digits": b' "push": "status.document:syntax:1760000000000",',
    "no-digit": b' "push": "status.document:syntax:",',
    "sign": b' "push": "status.document:syntax:-1760000000",',
    "two-fields": b' "push": "status.document:1760000000",',
    "four-fields": b' "push": "status.document:syntax:1760000000:7",',
    "no-surface": b' "push": ":syntax:1760000000",',
    "long-surface": b' "push": "' + b"s" * 65 + b':syntax:1760000000",',
    "long-rule": b' "push": "status.document:' + b"r" * 65 + b':1760000000",',
    "nul-byte": b' "push": "status.document:syntax:1760000000\x00",',
    "escape": b' "push": "\\u0073tatus.document:syntax:1760000000",',
    "not-ascii": ' "push": "stätus.document:syntax:1760000000",'.encode(),
    "not-utf-8": b' "push": "st\xfftus.document:syntax:1760000000",',
    "carriage-return": b' "push": "status.document:syntax:1760000000",\r',
    "no-indent": b'"push": "status.document:syntax:1760000000",',
    "more-indent": b'  "push": "status.document:syntax:1760000000",',
    "no-space": b' "push":"status.document:syntax:1760000000",',
    "number": b' "push": 1760000000,',
    "null": b' "push": null,',
    "no-end-quote": b' "push": "status.document:syntax:1760000000,',
    "text-after": b' "push": "status.document:syntax:1760000000", "rows": [],',
    "two-lines": (
        b' "push": "status.document:syntax:1760000000",\n "push": "audit.line:syntax:1760000000",'
    ),
    "after-a-key": b' "kind": "notices", "push": "status.document:syntax:1760000000",',
    "in-braces": b'{"push": "status.document:syntax:1760000000"}',
}


@pytest.mark.parametrize("name", sorted(BROKEN_PUSH_LINES))
def test_a_push_line_that_breaks_its_pattern_is_one_line(rig: Rig, name: str) -> None:
    """One line in the journal, no notice, and the run ends as it would
    without the file."""
    path = rig.notice_with_line("caregiver", BROKEN_PUSH_LINES[name])

    done = rig.run()
    said = [one for one in done.stdout.splitlines() if "breaks its pattern" in one]

    assert done.returncode == 0, done.stdout + done.stderr
    assert rig.posts() == []
    assert len(said) == 1, done.stdout
    assert str(path) in said[0]
    assert rig.sent_lines() == []


@pytest.mark.parametrize("separators", [(", ", ": "), (",", ":")])
def test_a_document_on_one_line_breaks_its_pattern(rig: Rig, separators: tuple[str, str]) -> None:
    """A writer can put the whole document on one line. The key `push` is
    then at no start of a line. The file gets one line in the journal and
    no notice, as each other file whose key is not in the exact form."""
    spread = json.loads(notice_document("caregiver", [token("status.document:syntax")]))
    path = rig.notices / "caregiver.json"
    _write_notice(path, json.dumps(spread, sort_keys=True, separators=separators).encode() + b"\n")

    done = rig.run()
    said = [one for one in done.stdout.splitlines() if "breaks its pattern" in one]

    assert done.returncode == 0, done.stdout + done.stderr
    assert rig.posts() == []
    assert len(said) == 1, done.stdout
    assert str(path) in said[0]
    assert rig.sent_lines() == []


def test_a_broken_file_does_not_stop_the_other_files(rig: Rig) -> None:
    rig.notice_with_line("caregiver", BROKEN_PUSH_LINES["upper-case"])
    rig.notice("noticeboard", token("status.document:syntax"))

    rig.run()

    assert rig.json_summaries() == [f"{JSON_WORD} noticeboard status.document syntax"]


def test_no_byte_of_a_notice_file_is_evaluated(rig: Rig, tmp_path: Path) -> None:
    """A notice file comes from another program. The script reads a value
    with an exact pattern and gives no byte of the file to a shell."""
    marker = tmp_path / "evaluated"
    for service, text in (
        ("caregiver", f"$(touch {marker}):syntax:1760000000"),
        ("noticeboard", f"`touch {marker}`:syntax:1760000000"),
        ("attendance", f"a:b:1; touch {marker}"),
        ("door-owui", "*:*:1"),
    ):
        rig.notice_with_line(service, push_line(text).encode())

    done = rig.run()

    assert done.returncode == 0, done.stdout + done.stderr
    assert not marker.exists()
    assert rig.posts() == []
    assert done.stdout.count("breaks its pattern") == 4


@pytest.mark.parametrize(
    "line",
    [b' "push": "",', b' "push": ""', b' "pushed": "status.document:syntax:1760000000",'],
)
def test_an_empty_push_value_asks_for_no_notice(rig: Rig, line: bytes) -> None:
    rig.notice_with_line("caregiver", line)

    done = rig.run()

    assert done.returncode == 0, done.stdout + done.stderr
    assert rig.posts() == []
    assert "breaks its pattern" not in done.stdout


@pytest.mark.parametrize(
    "body", [b"", b"\n", b"{}\n", b'{\n "kind": "notices",\n "rows": [],\n "service": "x"\n}\n']
)
def test_a_file_with_no_push_line_asks_for_no_notice(rig: Rig, body: bytes) -> None:
    _write_notice(rig.notices / "caregiver.json", body)

    done = rig.run()

    assert done.returncode == 0, done.stdout + done.stderr
    assert rig.posts() == []
    assert "breaks its pattern" not in done.stdout


def test_the_last_key_of_a_document_needs_no_comma(rig: Rig) -> None:
    """The key `push` can be the last key of the object. Its line then ends
    with the quote."""
    rig.notice_with_line("caregiver", b' "push": "status.document:syntax:1760000000"')

    rig.run()

    assert rig.json_summaries() == [f"{JSON_WORD} caregiver status.document syntax"]


def test_a_file_with_no_final_newline_is_read(rig: Rig) -> None:
    whole = notice_document("caregiver", [token("status.document:syntax")]).rstrip("\n")
    _write_notice(rig.notices / "caregiver.json", whole.encode())

    rig.run()

    assert rig.json_summaries() == [f"{JSON_WORD} caregiver status.document syntax"]


def test_leading_zeros_in_a_time_are_a_decimal_number(rig: Rig) -> None:
    """`<seconds>` has 1 to 12 digits. A value with a zero in front is no
    octal number: `0900` is above `0899`."""
    pair = "status.document:syntax"
    rig.notice("caregiver", f"{pair}:000000000899")
    rig.run()
    rig.notice("caregiver", f"{pair}:0900")

    done = rig.run_later(DAY_S + MINUTE_S)

    assert done.returncode == 0, done.stdout + done.stderr
    assert len(rig.json_summaries()) == 2
    assert [one.split()[3] for one in rig.sent_lines()] == ["900"]


def test_a_lock_file_and_a_temporary_file_are_not_read(rig: Rig, tmp_path: Path) -> None:
    """A writer keeps a lock file and a temporary file beside its notice
    file. The script reads only a regular file whose name ends in `.json`.
    It reads no file whose name starts with a dot."""
    whole = notice_document("door-owui", [token("status.document:syntax")]).encode()
    names = ("door-owui.json.lock", "door-owui.json.tmp", ".door-owui.json.7.tmp", "door-owui")
    for name in (*names, ".json", ".door-owui.json"):
        _write_notice(rig.notices / name, whole)

    _write_notice(rig.notices / "folder.json" / "inner.json", whole)
    outside = tmp_path / "outside.json"
    outside.write_bytes(whole)
    (rig.notices / "link.json").symlink_to(outside)

    done = rig.run()

    assert done.returncode == 0, done.stdout + done.stderr
    assert rig.posts() == []
    assert "breaks its pattern" not in done.stdout


@pytest.mark.parametrize(
    "name",
    [
        "Noticeboard.json",
        "notice_board.json",
        "notice board.json",
        "notice.board.json",
        "n" * 65 + ".json",
        "caregiver\nnoticeboard.json",
    ],
)
def test_a_file_name_that_is_no_service_name_is_not_read(rig: Rig, name: str) -> None:
    whole = notice_document("caregiver", [token("status.document:syntax")]).encode()
    _write_notice(rig.notices / name, whole)

    done = rig.run()

    assert done.returncode == 0, done.stdout + done.stderr
    assert rig.posts() == []
    assert done.stdout.count("is no service name") == 1, done.stdout


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a file of each mode")
def test_a_notice_file_that_the_run_cannot_read_is_one_line(rig: Rig) -> None:
    """One line in the journal names the file. The other files are read."""
    closed = rig.notice("caregiver", token("status.document:syntax"))
    closed.chmod(0)
    rig.notice("noticeboard", token("status.document:syntax"))

    done = rig.run()

    assert done.returncode == 0, done.stdout + done.stderr
    assert rig.json_summaries() == [f"{JSON_WORD} noticeboard status.document syntax"]
    assert done.stdout.count(f"cannot read {closed}") == 1


def test_a_push_line_past_the_byte_cap_is_not_read(rig: Rig) -> None:
    """The script reads the first 65,536 bytes of a file and no more."""
    whole = notice_document("caregiver", [token("status.document:syntax")])
    padded = whole.replace(' "push"', ' "pad": "' + "p" * NOTICE_READ_CAP + '",\n "push"')
    _write_notice(rig.notices / "caregiver.json", padded.encode())

    done = rig.run()

    assert done.returncode == 0, done.stdout + done.stderr
    assert rig.posts() == []


def test_a_push_line_that_the_byte_cap_cuts_breaks_its_pattern(rig: Rig) -> None:
    line = ' "push": "status.document:syntax:1760000000",'
    head = '{\n "kind": "notices",\n "pad": "'
    pad = "p" * (NOTICE_READ_CAP - len(head) - len('",\n') - len(line) + 5)
    body = head + pad + '",\n' + line + '\n "rows": [],\n "service": "caregiver"\n}\n'
    assert body.index(line) < NOTICE_READ_CAP < body.index(line) + len(line)
    _write_notice(rig.notices / "caregiver.json", body.encode())

    done = rig.run()

    assert rig.posts() == []
    assert done.stdout.count("breaks its pattern") == 1


def test_a_large_file_with_an_early_push_line_is_read(rig: Rig) -> None:
    """The keys of a notice file are sorted, so the line of `push` stands
    before the rows. A file with many rows is larger than the byte cap."""
    whole = notice_document("caregiver", [token("status.document:syntax")])
    padded = whole.replace(' "rows"', ' "pushed": "' + "p" * (2 * NOTICE_READ_CAP) + '",\n "rows"')
    assert padded.index(' "push"') < 100
    _write_notice(rig.notices / "caregiver.json", padded.encode())

    rig.run()

    assert rig.json_summaries() == [f"{JSON_WORD} caregiver status.document syntax"]


def test_a_push_line_holds_256_tokens_at_most(rig: Rig) -> None:
    """A service has fewer than 256 pairs of a surface and a rule. A line
    with more tokens breaks its pattern."""
    tokens = [token(f"surface.s{number}:syntax") for number in range(257)]
    rig.notice("caregiver", *tokens)
    rig.notice("noticeboard", *tokens[:256])

    done = rig.run()

    assert done.returncode == 0, done.stdout + done.stderr
    assert done.stdout.count("breaks its pattern") == 1
    assert len(rig.json_summaries()) == 6
    assert all(one.startswith(f"{JSON_WORD} noticeboard ") for one in rig.json_summaries())


def test_a_json_notice_that_fails_is_sent_by_the_next_run(rig: Rig) -> None:
    """A notice that the hook did not take leaves the record as it was."""
    rig.notice("noticeboard", token("status.document:syntax"))

    first = rig.run(WD_POST_EXIT="7")

    assert first.returncode == 0, first.stdout + first.stderr
    assert "NOTICE NOT SENT" in first.stdout
    assert len(rig.posts()) == 1, "one try"
    assert rig.sent_lines() == []

    second = rig.run()

    assert "phone alert sent" in second.stdout
    assert len(rig.posts()) == 2
    assert len(rig.sent_lines()) == 1

    rig.run()

    assert len(rig.posts()) == 2


def test_a_run_stops_at_the_first_json_notice_that_fails(rig: Rig) -> None:
    """A hook that is away costs a run the time limit of one notice, not of
    six. The next run sends each notice."""
    rig.notice("noticeboard", *[token(one) for one in SEVEN_PAIRS[:3]])

    rig.run(WD_POST_EXIT="7")

    assert len(rig.posts()) == 1

    rig.run()

    assert len(rig.json_summaries()) == 4
    assert len(rig.sent_lines()) == 3


def test_no_hook_leaves_each_json_notice_for_a_later_run(rig: Rig) -> None:
    hook = rig.hooks.read_text(encoding="utf-8")
    _write(rig.hooks, "LITELLM_MASTER_KEY=other\n")
    rig.notice("noticeboard", token("status.document:syntax"))

    done = rig.run()

    assert done.returncode == 0, done.stdout + done.stderr
    assert "NOTICE NOT SENT" in done.stdout
    assert rig.posts() == []
    assert rig.sent_lines() == []

    _write(rig.hooks, hook)
    rig.run()

    assert len(rig.json_summaries()) == 1


def test_the_record_of_sent_notices_has_one_line_for_each_pair(rig: Rig) -> None:
    """The service, the surface, the rule, the `<seconds>` that the script
    sent and the time of the send. The file has the modes of the verdict
    file: 0700 for the directory and 0600 for the file."""
    pair = "status.document:syntax"
    rig.notice("noticeboard", token(pair))
    before = int(time.time())
    rig.run()
    rig.notice("noticeboard", token(pair, REFUSED_AT + MINUTE_S))
    rig.run_later(DAY_S + MINUTE_S)
    after = int(time.time())

    lines = rig.sent_lines()

    assert len(lines) == 1
    service, surface, rule, seconds, sent_at = lines[0].split(" ")
    assert (service, surface, rule) == ("noticeboard", "status.document", "syntax")
    assert int(seconds) == REFUSED_AT + MINUTE_S
    assert before + DAY_S + MINUTE_S <= int(sent_at) <= after + DAY_S + MINUTE_S
    assert rig.sent.stat().st_mode & 0o777 == FILE_MODE
    assert rig.sent.parent.stat().st_mode & 0o777 == DIR_MODE
    assert sorted(one.name for one in rig.sent.parent.iterdir()) == ["notices-sent", "verdict"]


def test_a_send_time_far_from_now_holds_no_notice(rig: Rig) -> None:
    """The clock of a host can jump. A record line whose send time is more
    than the limit away from now, before or after, holds no notice."""
    pair = "status.document:syntax"
    ahead = int(time.time()) + 10 * DAY_S
    lines = [f"noticeboard {pair.replace(':', ' ')} {REFUSED_AT - MINUTE_S} {ahead}"]
    lines += [f"caregiver {one.replace(':', ' ')} {REFUSED_AT} {ahead}" for one in SEVEN_PAIRS[:6]]
    _write(rig.sent, "\n".join(lines) + "\n")
    rig.notice("noticeboard", token(pair))

    rig.run()

    assert rig.json_summaries() == [f"{JSON_WORD} noticeboard status.document syntax"]
    assert len(rig.sent_lines()) == 7


def test_a_record_line_that_breaks_its_pattern_is_not_read(rig: Rig) -> None:
    """The record is the own file of the script. The script still reads it
    with an exact pattern, and it keeps each line that holds the pattern."""
    now = int(time.time())
    good = f"noticeboard status.document syntax {REFUSED_AT} {now}"
    _write(rig.sent, f"noticeboard audit.line syntax $(id) {now}\n{good}\nnot a record line\n")
    rig.notice("noticeboard", token("status.document:syntax"), token("audit.line:syntax"))

    done = rig.run()

    assert done.returncode == 0, done.stdout + done.stderr
    assert rig.json_summaries() == [f"{JSON_WORD} noticeboard audit.line syntax"]
    assert "break the record pattern: 2" in done.stdout
    assert good in rig.sent_lines()
    assert len(rig.sent_lines()) == 2


def test_a_record_over_its_byte_cap_sends_nothing(rig: Rig) -> None:
    """The limits come from the record. With a record that the script does
    not read, it sends no notice of this kind and says so in one line."""
    line = f"noticeboard audit.line syntax {REFUSED_AT} {int(time.time())}\n"
    _write(rig.sent, line * (SENT_READ_CAP // len(line) + 1))
    rig.notice("noticeboard", token("status.document:syntax"))

    done = rig.run()

    assert done.returncode == 0, done.stdout + done.stderr
    assert rig.posts() == []
    assert done.stdout.count(str(rig.sent)) == 1


def _fill_record(rig: Rig, room: int) -> list[str]:
    """Writes a record with room for that count of bytes under its cap.
    Each line is one week old. Gives the lines."""
    old = int(time.time()) - 7 * DAY_S
    lines: list[str] = []
    size = 0
    while size < SENT_READ_CAP - room - 105:
        lines.append(f"attendance surface.s{len(lines)} syntax {REFUSED_AT} {old}")
        size += len(lines[-1]) + 1

    last = f"attendance  syntax {REFUSED_AT} {old}"
    pad = SENT_READ_CAP - room - size - len(last) - 1
    assert 1 <= pad <= 64
    lines.append(last.replace("attendance ", "attendance " + "p" * pad))
    _write(rig.sent, "\n".join(lines) + "\n")
    assert rig.sent.stat().st_size == SENT_READ_CAP - room

    return lines


def test_the_script_keeps_its_record_under_the_byte_cap(rig: Rig) -> None:
    """A notice that would take the record past its byte cap is not sent.
    The script can then read its record in each later run. This record has
    room for 10 bytes."""
    lines = _fill_record(rig, 10)
    rig.notice("noticeboard", token("status.document:syntax"))

    for _ in range(2):
        done = rig.run()

        assert done.returncode == 0, done.stdout + done.stderr
        assert rig.posts() == []
        assert done.stdout.count(str(rig.sent)) == 1
        assert rig.sent_lines() == lines


@pytest.mark.parametrize("spare", [0, -1])
def test_a_record_line_that_fits_exactly_is_sent(rig: Rig, spare: int) -> None:
    """The edge of the byte cap. A new line that fills the record to the
    cap is sent. A new line that is 1 byte longer is not sent."""
    new_line = f"noticeboard status.document syntax {REFUSED_AT} {int(time.time())}\n"
    lines = _fill_record(rig, len(new_line) + spare)
    rig.notice("noticeboard", token("status.document:syntax"))

    done = rig.run()
    sent = 1 if spare == 0 else 0

    assert done.returncode == 0, done.stdout + done.stderr
    assert len(rig.posts()) == sent
    assert len(rig.sent_lines()) == len(lines) + sent
    assert rig.sent.stat().st_size == SENT_READ_CAP - len(new_line) - spare + sent * len(new_line)

    again = rig.run()

    assert len(rig.posts()) == sent
    assert "is over" not in again.stdout


def test_a_failed_record_update_keeps_each_old_line(rig: Rig) -> None:
    """`awk` makes the new record after a send. When that call fails, the
    record keeps each line, and the run sends no more notices of this kind."""
    old = int(time.time()) - 7 * DAY_S
    lines = [f"attendance {one.replace(':', ' ')} {REFUSED_AT} {old}" for one in SEVEN_PAIRS[:5]]
    _write(rig.sent, "\n".join(lines) + "\n")
    rig.notice("noticeboard", token("status.document:syntax"), token("audit.line:syntax"))

    done = rig.run(WD_AWK_KEY_EXIT="2")

    assert done.returncode == 0, done.stdout + done.stderr
    assert len(rig.posts()) == 1
    assert done.stdout.count("awk gave no answer after a send") == 1, done.stdout
    assert rig.sent_lines() == lines


def test_a_record_write_that_fails_after_a_send_stops_the_step(rig: Rig) -> None:
    """The run proves the record with one write before the first send. This
    case fails the second write, the one after the send. The run then sends
    no more notices of this kind, and the next run sends each one."""
    rig.notice("noticeboard", *[token(one) for one in SEVEN_PAIRS[:3]])

    done = rig.run(WD_MKTEMP_FAIL_AT="2")

    assert done.returncode == 0, done.stdout + done.stderr
    assert len(rig.posts()) == 1
    assert done.stdout.count("after a send") == 1, done.stdout
    assert rig.sent_lines() == []

    rig.run()

    assert len(rig.json_summaries()) == 4
    assert len(rig.sent_lines()) == 3


def test_no_time_from_date_sends_nothing(rig: Rig) -> None:
    """Both limits need the time of the run. A run in which `date` gives no
    time sends no notice of this kind, and the next run sends each one."""
    rig.notice("noticeboard", token("status.document:syntax"))

    done = rig.run(WD_DATE_FAIL_AT="2")

    assert done.returncode == 0, done.stdout + done.stderr
    assert rig.posts() == []
    assert done.stdout.count("date gave no time") == 1, done.stdout
    assert rig.sent_lines() == []

    rig.run()

    assert len(rig.json_summaries()) == 1


def test_no_answer_on_the_limits_sends_nothing(rig: Rig) -> None:
    """`awk` holds the two limits. A run in which it gives no answer sends
    no notice of this kind, and the next run sends each one."""
    rig.notice("noticeboard", token("status.document:syntax"))

    done = rig.run(WD_AWK_EXIT="2")

    assert done.returncode == 0, done.stdout + done.stderr
    assert rig.posts() == []
    assert rig.sent_lines() == []

    rig.run()

    assert len(rig.json_summaries()) == 1


@pytest.mark.parametrize("blocked", ["record", "directory"])
def test_a_record_that_cannot_be_written_sends_nothing(rig: Rig, blocked: str) -> None:
    """A notice with no record line has no limit. The script proves that it
    can write the record before it sends the first notice of a run."""
    if blocked == "record":
        rig.sent.mkdir(parents=True)
    else:
        _write(rig.sent.parent, "a file in the place of the state directory\n")

    rig.notice("noticeboard", token("status.document:syntax"))

    rig.run()
    done = rig.run()

    assert done.returncode == 0, done.stdout + done.stderr
    assert rig.posts() == []
    assert str(rig.sent) in done.stdout


#: What the host does in a run: nothing, one check that fails, or a hook
#: that takes no notice.
HOST_STATES: Final = (
    {},
    {"WD_PEP_CODE": "000"},
    {"WD_SOCK_CODE": "000", "WD_POST_EXIT": "7"},
    {"WD_POST_EXIT": "7"},
    {"WD_MKTEMP_EXIT": "1"},
    {"WD_PEP_CODE": "000", "WD_MKTEMP_EXIT": "1"},
    {"WD_AWK_EXIT": "2"},
)


@pytest.mark.parametrize("state", HOST_STATES)
def test_the_notice_step_changes_no_verdict_and_no_status(
    tmp_path: Path, state: dict[str, str]
) -> None:
    """Two hosts in the same state. One has notice files, a file that breaks
    its pattern among them. Each run ends with the same status on both, and
    both store the same verdict."""
    bare = Rig(tmp_path / "bare")
    full = Rig(tmp_path / "full")
    full.notice("noticeboard", *[token(one) for one in SEVEN_PAIRS])
    full.notice_with_line("caregiver", BROKEN_PUSH_LINES["nul-byte"])
    full.pep_notice(token("grants.file:syntax"))

    for _ in range(3):
        expected = bare.run(**state)
        done = full.run(**state)

        assert done.returncode == expected.returncode, done.stdout + done.stderr
        assert full.stored() == bare.stored()
        assert full.raw() == bare.raw()


#: The first line of the function that holds the notice step.
STEP_OPENING: Final = "send_json_notices() {\n"

#: The line of the script that sets the time budget of the notice step.
BUDGET_LINE: Final = re.compile(r"^NOTICE_STEP_BUDGET_S=[0-9]+$", re.MULTILINE)


def _script_copy(tmp_path: Path, text: str) -> Path:
    """A copy of the script with this text, beside a copy of its library."""
    copy = tmp_path / "bin" / SCRIPT.name
    shutil.copytree(SCRIPT.parent / "lib", copy.parent / "lib")
    copy.write_text(text, encoding="utf-8")

    return copy


def test_a_run_past_its_time_budget_starts_no_send(rig: Rig, tmp_path: Path) -> None:
    """The unit of the script gives a run a start limit, and a hook can
    answer slowly. A run that is older than the budget of the step starts no
    send and says so in one line. This case runs a copy of the script with a
    budget that each run is past. The next run of the script sends the
    notice."""
    text, found = BUDGET_LINE.subn("NOTICE_STEP_BUDGET_S=-1", SCRIPT.read_text(encoding="utf-8"))
    assert found == 1
    rig.script = _script_copy(tmp_path, text)
    rig.notice("noticeboard", token("status.document:syntax"))

    done = rig.run()

    assert done.returncode == 0, done.stdout + done.stderr
    assert rig.posts() == []
    assert done.stdout.count("so it sends no more of this kind") == 1, done.stdout
    assert rig.sent_lines() == []

    rig.script = SCRIPT
    rig.run()

    assert len(rig.json_summaries()) == 1


@pytest.mark.parametrize("fault", ["exit 9", ': "$NO_SUCH_VARIABLE"', ": $(( 10#x ))"])
def test_a_fault_in_the_notice_step_does_not_end_the_run(
    rig: Rig, tmp_path: Path, fault: str
) -> None:
    """The step runs in a shell of its own. This case runs a copy of the
    script whose step starts with a command that ends its shell. The run
    still ends with the status of the five checks."""
    text = SCRIPT.read_text(encoding="utf-8")
    assert text.count(STEP_OPENING) == 1
    rig.script = _script_copy(tmp_path, text.replace(STEP_OPENING, f"{STEP_OPENING}  {fault}\n"))
    rig.notice("noticeboard", token("status.document:syntax"))

    well = rig.run()
    down = rig.run(WD_PEP_CODE="000")

    assert well.returncode == 0, well.stdout + well.stderr
    assert down.returncode == 1, down.stdout + down.stderr
    assert rig.stored() == "up"
    assert rig.raw() == "chaperone"
    assert rig.posts() == []


def test_the_bearer_of_a_json_notice_never_reaches_argv(rig: Rig) -> None:
    rig.notice("noticeboard", token("status.document:syntax"))

    done = rig.run()

    assert len(rig.json_summaries()) == 1
    assert TOKEN not in done.stdout
    assert TOKEN not in done.stderr
    assert TOKEN not in rig.stub_log.read_text(encoding="utf-8")
    assert all(TOKEN not in one for one in rig.posts())


def test_last_reads_no_notice_file(rig: Rig) -> None:
    rig.notice("noticeboard", token("status.document:syntax"))

    done = rig.run("--last")

    assert done.returncode == 0
    assert rig.posts() == []
    assert rig.sent_lines() == []


# --- what the script is made of -------------------------------------------------


def _code_lines() -> list[str]:
    """Each line of the script that is no comment."""
    return [
        one
        for one in SCRIPT.read_text(encoding="utf-8").splitlines()
        if not one.lstrip().startswith("#")
    ]


def test_the_script_is_valid_bash() -> None:
    done = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True, timeout=60)

    assert done.returncode == 0, done.stderr


def test_the_script_calls_no_json_program_and_no_python() -> None:
    """The script runs while a component tree is in a release, so it needs
    no program of a component and no `jq`."""
    words = re.compile(r"\b(jq|python[0-9.]*|uv|node)\b")

    assert not [one for one in _code_lines() if words.search(one)]


def test_the_script_stays_clean_for_bash_3_2() -> None:
    """`bin/AGENTS.md` names the four forms that bash 3.2 does not have."""
    forms = re.compile(r"declare -A|local -A|\bmapfile\b|\breadarray\b|\$\{[A-Za-z_]+,,|&>>")

    assert not [one for one in _code_lines() if forms.search(one)]


def _one_line(text: str) -> str:
    """A text with one space in the place of each run of white space."""
    return " ".join(text.split())


def test_the_prose_holds_the_numbers_of_the_script() -> None:
    """The comments of the script and `bin/AGENTS.md` name the limits of the
    notice step as numbers. Each number equals its constant in the script."""
    script = SCRIPT.read_text(encoding="utf-8").splitlines()
    comments = _one_line(" ".join(one.lstrip("# ") for one in script if one.startswith("#")))
    rules = _one_line((SCRIPT.parent / "AGENTS.md").read_text(encoding="utf-8"))
    longest = re.search(r"\{1,([0-9]+)\}'$", _constant("SERVICE_NAME"))
    assert longest is not None

    phrases = (
        f"{_number('NOTICE_READ_CAP'):,} bytes of a file",
        f"{_number('SENT_READ_CAP'):,} bytes of the record",
        f"rule in {_number('SAME_NOTICE_EVERY_S') // HOUR_S} hours",
        f"{_number('NOTICES_PER_HOUR')} notices of this kind",
        f"name has {longest.group(1)} characters at most",
    )
    for phrase in phrases:
        assert phrase in comments, phrase
        assert phrase in rules, phrase

    assert f"one line has {_number('NOTICE_TOKENS_MAX')} tokens at most" in rules
    assert _number("HOUR_S") == HOUR_S


def test_the_header_names_each_command_of_the_script() -> None:
    """The header has one line with the programs that the script needs. The
    line holds each program that the notice step and check 4 start."""
    text = SCRIPT.read_text(encoding="utf-8")
    listed = next(one for one in text.splitlines() if one.startswith("#   command -v "))

    programs = ("curl", "date", "grep", "systemctl", "mktemp", "timeout", "head", "tr", "awk", "mv")
    for program in programs:
        assert program in listed.split(), program
