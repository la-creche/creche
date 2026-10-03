"""§2.8's quiet window, read from `attendance` over its Unix socket.

Why not an nftables counter: on the real host (measured 2026-09-21)
`ai-litellm` is a BRIDGED container. Docker DNATs `192.0.2.10:4000` in
`prerouting` and in `output`, so the traffic is forwarded to the container
and never passes an `input` hook. A counter there never moves, "never moved
for five minutes" is always true, and the window opens at once while turns
are running. That is fail OPEN, in the one direction §2.8 exists to prevent.

The signal here is the one root can actually get: **the session service has
reported no turn in flight for five consecutive minutes**. Every model call
an agent makes belongs to a turn, and `attendance` knows every turn.
`bin/rework-cutover.sh up` has asked exactly this question on the real host a
dozen times, over the same socket, with the same `view-ro` bearer.

Root is the caller and both files are the operator's, so the confused-deputy case is
the first thing these tests pin: root opens the token with `O_NOFOLLOW` and
believes it only after `fstat` on the OPEN descriptor says regular file,
the operator's uid, mode 0600, bounded size. Root therefore only ever reads bytes
out of a file the operator already owns and can already read, and a symlink planted
at that path delivers nothing.
"""

from __future__ import annotations

import json
import os
import shutil
import socketserver
import stat
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Final

import pytest
from handover.executor.quiet import (
    BUSY_STATES,
    MIN_TOKEN_BYTES,
    QUIET_WINDOW_S,
    Busy,
    ask_attendance,
    attendance_signal,
    read_view_token,
    wait_for_quiet,
)

NOW: Final = 1_758_153_590.0

#: Long enough to pass contract 02 §3 rule 7's floor, and it is not a secret:
#: no host reads this file.
TOKEN: Final = "r7q-view-ro-token-0123456789abcdef"

#: Short enough for `AF_UNIX` on macOS, which caps the path near 104 bytes.
#: pytest's `tmp_path` is longer than that on its own.
SOCKET_NAME: Final = "sessiond.sock"
TOKEN_NAME: Final = "view-ro.token"

#: One fake answer must not hold a test for the production timeout.
FAST_TIMEOUT_S: Final = 0.5

#: How often the fake's accept loop looks for a shutdown.
SHUTDOWN_POLL_S: Final = 0.01

MAX_LINE: Final = 8192


@dataclass
class Plan:
    """What the fake `attendance` answers next, and what it was asked."""

    busy: bool = False
    status: int = 200
    #: Replaces the JSON body entirely, for the shapes root must refuse.
    body: bytes | None = None
    #: How long the handler waits before it answers anything at all.
    delay_s: float = 0.0
    seen: list[tuple[str, str]] = field(default_factory=list)

    def paths(self) -> list[str]:
        return [line for line, _ in self.seen]

    def bearers(self) -> list[str]:
        return [value for _, value in self.seen]


def _serve(reader: IO[bytes], writer: IO[bytes], plan: Plan) -> None:
    """Just enough HTTP to answer one `GET`, on a real stream."""
    request = reader.readline(MAX_LINE).decode("latin-1").strip()
    bearer = ""
    while True:
        raw = reader.readline(MAX_LINE)
        if raw in (b"\r\n", b"\n", b""):
            break

        name, _, value = raw.decode("latin-1").partition(":")
        if name.strip().lower() == "authorization":
            bearer = value.strip()

    plan.seen.append((request, bearer))
    if plan.delay_s:
        time.sleep(plan.delay_s)

    rows = [{"family": "chat", "session": "owui-1", "state": "running"}] if plan.busy else []
    body = plan.body
    if body is None:
        body = json.dumps({"sessions": rows, "next_cursor": None}).encode("utf-8")

    head = (
        f"HTTP/1.1 {plan.status} answer\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Content-Type: application/json\r\n"
        "Connection: close\r\n\r\n"
    )
    writer.write(head.encode("latin-1") + body)


def _handler_for(plan: Plan) -> type[socketserver.StreamRequestHandler]:
    class Handler(socketserver.StreamRequestHandler):
        def handle(self) -> None:
            _serve(self.rfile, self.wfile, plan)

    return Handler


class _Server(socketserver.ThreadingUnixStreamServer):
    #: A handler left sleeping in the slow-socket case must not hold the
    #: fixture's teardown open.
    daemon_threads = True

    def handle_error(self, request: object, client_address: object) -> None:
        # Expected, and not a failure: the slow-socket test times the client
        # out, so the handler's write lands on a closed peer. The default
        # prints a traceback that reads like a broken test.
        del request, client_address


@dataclass
class Rig:
    """A real Unix socket, a real token file, and root's own uid as owner."""

    socket_path: Path
    token_path: Path
    plan: Plan
    owner_uid: int

    def ask(self, timeout_s: float = FAST_TIMEOUT_S) -> Busy:
        return ask_attendance(self.socket_path, self.token_path, self.owner_uid, timeout_s)


@pytest.fixture
def rig() -> Iterator[Rig]:
    # Not under `tmp_path`: an `AF_UNIX` path is capped near 104 bytes on
    # macOS, and pytest's own is longer than that before a name is added.
    root = Path(tempfile.mkdtemp(dir="/tmp", prefix="r7q-"))
    plan = Plan()
    server = _Server(str(root / SOCKET_NAME), _handler_for(plan))
    # `shutdown()` waits for the accept loop to come round, and the default
    # poll is 0.5 s: 27 teardowns at half a second is 13 s of nothing.
    threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": SHUTDOWN_POLL_S}, daemon=True
    ).start()
    token = root / TOKEN_NAME
    token.write_text(TOKEN, encoding="utf-8")
    # Explicit, never the umask: the mode is what root checks.
    token.chmod(0o600)

    yield Rig(root / SOCKET_NAME, token, plan, os.getuid())

    server.shutdown()
    server.server_close()
    shutil.rmtree(root, ignore_errors=True)


# -- 1. root reads the operator's token without becoming their deputy ------


def test_the_token_is_read_when_it_is_the_operators_own_file(rig: Rig) -> None:
    assert read_view_token(rig.token_path, rig.owner_uid) == TOKEN.encode("utf-8")


def test_a_symlinked_token_is_refused(rig: Rig) -> None:
    """THE confused-deputy case. An operator-side process swaps the path for a
    link to a root-only file, and root would read that file and send its
    bytes to a socket the operator controls. `O_NOFOLLOW` refuses the open, so the
    target's contents never enter root's memory."""
    planted = rig.token_path.parent / "planted.token"
    planted.write_text(TOKEN, encoding="utf-8")
    planted.chmod(0o600)
    rig.token_path.unlink()
    rig.token_path.symlink_to(planted)

    assert read_view_token(rig.token_path, rig.owner_uid) is None


def test_a_token_owned_by_somebody_else_is_refused(rig: Rig) -> None:
    """The second half of the same defence, and the half that also covers a
    swapped PARENT directory: whatever root ends up with must be a file the
    expected owner wrote. A root-owned file has another uid and is refused."""
    assert read_view_token(rig.token_path, rig.owner_uid + 1) is None


def test_a_token_wider_than_0600_is_refused(rig: Rig) -> None:
    """Contract 02 §3 rule 5: `view-ro` is not one of the PEP's two tokens,
    so nothing but 0600 is this file."""
    rig.token_path.chmod(0o640)

    assert read_view_token(rig.token_path, rig.owner_uid) is None


def test_a_token_that_is_not_a_regular_file_is_refused(rig: Rig) -> None:
    rig.token_path.unlink()
    rig.token_path.mkdir()

    assert read_view_token(rig.token_path, rig.owner_uid) is None


def test_a_token_under_the_contract_floor_is_refused(rig: Rig) -> None:
    rig.token_path.write_text("x" * (MIN_TOKEN_BYTES - 1), encoding="utf-8")
    rig.token_path.chmod(0o600)

    assert read_view_token(rig.token_path, rig.owner_uid) is None


def test_an_oversized_token_is_refused(rig: Rig) -> None:
    """A file root reads whole is a file a writer can grow."""
    rig.token_path.write_text("x" * (64 * 1024), encoding="utf-8")
    rig.token_path.chmod(0o600)

    assert read_view_token(rig.token_path, rig.owner_uid) is None


def test_an_absent_token_is_refused(rig: Rig) -> None:
    rig.token_path.unlink()

    assert read_view_token(rig.token_path, rig.owner_uid) is None


# -- 2. one question, over a real socket -----------------------------------


def test_an_idle_host_is_not_busy(rig: Rig) -> None:
    assert rig.ask() is Busy.NO


def test_a_session_in_flight_is_busy(rig: Rig) -> None:
    rig.plan.busy = True

    assert rig.ask() is Busy.YES


def test_every_state_a_restart_would_cut_short_is_asked(rig: Rig) -> None:
    """Contract 02 §4.1, and the same three `bin/rework-cutover.sh up` asks."""
    rig.ask()

    asked = " ".join(rig.plan.paths())
    for state in BUSY_STATES:
        assert f"state={state}" in asked


def test_the_bearer_rides_a_header_and_never_the_request_line(rig: Rig) -> None:
    """Invariant 13. A token in a path lands in every access log there is."""
    rig.ask()

    assert rig.plan.bearers() == [f"Bearer {TOKEN}"] * len(BUSY_STATES)
    assert all(TOKEN not in line for line in rig.plan.paths())


# -- 3. every way root cannot tell reads as BUSY ---------------------------


def test_no_socket_is_never_quiet(rig: Rig) -> None:
    absent = rig.socket_path.parent / "gone.sock"

    assert ask_attendance(absent, rig.token_path, rig.owner_uid, FAST_TIMEOUT_S) is Busy.UNKNOWN


def test_no_token_is_never_quiet(rig: Rig) -> None:
    rig.token_path.unlink()

    assert rig.ask() is Busy.UNKNOWN


def test_a_socket_owned_by_somebody_else_is_never_quiet(rig: Rig) -> None:
    """Root connects only to a socket the expected owner bound. A link
    planted at that path pointing at a root-only daemon's socket would
    otherwise take root's `GET` and the operator's bearer."""
    assert (
        ask_attendance(rig.socket_path, rig.token_path, rig.owner_uid + 1, FAST_TIMEOUT_S)
        is Busy.UNKNOWN
    )


def test_a_path_that_is_not_a_socket_is_never_quiet(rig: Rig) -> None:
    plain = rig.socket_path.parent / "plain.sock"
    plain.write_text("", encoding="utf-8")

    assert ask_attendance(plain, rig.token_path, rig.owner_uid, FAST_TIMEOUT_S) is Busy.UNKNOWN


def test_a_non_200_answer_is_never_quiet(rig: Rig) -> None:
    rig.plan.status = 503

    assert rig.ask() is Busy.UNKNOWN


def test_a_body_that_is_not_the_list_shape_is_never_quiet(rig: Rig) -> None:
    rig.plan.body = json.dumps({"rows": []}).encode("utf-8")

    assert rig.ask() is Busy.UNKNOWN


def test_a_body_that_is_not_json_is_never_quiet(rig: Rig) -> None:
    rig.plan.body = b"<html>not the session service</html>"

    assert rig.ask() is Busy.UNKNOWN


def test_a_slow_socket_is_never_quiet_and_is_bounded(rig: Rig) -> None:
    """A hostile or wedged `attendance` must not hold a release open. Every
    read carries its own deadline."""
    rig.plan.delay_s = FAST_TIMEOUT_S * 6
    started = time.monotonic()

    answer = rig.ask()

    assert answer is Busy.UNKNOWN
    assert time.monotonic() - started < FAST_TIMEOUT_S * 5


# -- 4. the window itself --------------------------------------------------


class _Clock:
    def __init__(self) -> None:
        self.at = NOW

    def now(self) -> float:
        return self.at

    def advance(self, seconds: float) -> None:
        self.at += seconds


def _wait(rig: Rig, clock: _Clock, each_poll: Callable[[], None]) -> float | None:
    """§2.8's own wait, driven by a fake clock and a real socket."""
    signal = attendance_signal(rig.socket_path, rig.token_path, rig.owner_uid, clock.now)

    def sleep(seconds: float) -> None:
        clock.advance(seconds)
        each_poll()

    return wait_for_quiet(signal, clock.now, sleep)


def test_the_first_reading_never_opens_the_window(rig: Rig) -> None:
    """Root has watched no window yet, so it may not claim one. An idle host
    still costs the full five minutes."""
    clock = _Clock()

    waited = _wait(rig, clock, lambda: None)

    assert waited == QUIET_WINDOW_S


def test_the_window_restarts_at_a_busy_read(rig: Rig) -> None:
    """Busy, quiet for less than the window, busy again, then quiet for the
    whole window.

    The number is the assertion. Five quiet minutes from the FIRST read would
    have returned 300. The busy read at 150 s restarts the clock, so the
    window opens at 450 s and not before.
    """
    clock = _Clock()
    rig.plan.busy = True
    script = iter([False, False, False, False, True] + [False] * 60)

    waited = _wait(rig, clock, lambda: setattr(rig.plan, "busy", next(script)))

    assert waited == 450.0


def test_minutes_root_could_not_ask_do_not_count_as_quiet(rig: Rig) -> None:
    """A read root could not make is never quiet, and it also RESTARTS the window: root
    may only count minutes it actually watched. Four unreadable polls, the
    last of them at 120 s, push the opening from 300 s out to 420 s."""
    clock = _Clock()
    statuses = iter([503, 503, 503, 503] + [200] * 60)

    waited = _wait(rig, clock, lambda: setattr(rig.plan, "status", next(statuses)))

    assert waited == QUIET_WINDOW_S + 120.0


def test_a_host_that_never_goes_quiet_fails_the_switch(rig: Rig) -> None:
    """§2.8: an hour with no window is `failed, step: switch`."""
    clock = _Clock()
    rig.plan.busy = True

    assert _wait(rig, clock, lambda: None) is None


def test_a_attendance_that_is_down_for_the_hour_fails_the_switch(rig: Rig) -> None:
    """The fail-closed end, and the one the nftables counter got wrong: a
    signal root cannot read waits out the hour instead of recreating
    `ai-litellm` under live calls."""
    clock = _Clock()
    rig.token_path.unlink()

    assert _wait(rig, clock, lambda: None) is None


def test_the_token_is_re_read_on_every_poll(rig: Rig) -> None:
    """Root holds no copy across an hour of polling, so a rotated token is
    picked up and a revoked one stops the window."""
    clock = _Clock()

    def revoke() -> None:
        if clock.at - NOW >= 60.0:
            rig.token_path.chmod(0o644)

    assert _wait(rig, clock, revoke) is None


def test_the_rig_stands_up_what_the_host_has(rig: Rig) -> None:
    """The two things root reads are a real socket and a 0600 file, which is
    what `bin/rework-cutover.sh up` leaves on the host."""
    assert stat.S_IMODE(rig.token_path.stat().st_mode) == 0o600
    assert stat.S_ISSOCK(rig.socket_path.stat().st_mode)
