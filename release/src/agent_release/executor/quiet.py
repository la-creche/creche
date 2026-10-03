"""§2.8's quiet window: do not drop every model call on the host.

Recreating `ai-litellm` drops every in-flight model call: sandboxes, Open
WebUI and the TUI alike. `ai-postgres` and `ai-redis` drag it along,
because it depends on them. So when step 8's compose dry run lists any of
those three, step 9 waits for five consecutive quiet minutes **before it
switches `infra`, and not before**.

The wait sits in step 9 and not step 7 for two reasons §2.8 gives. Step 7
cannot know what compose would recreate, because the dry run needs the
fetched files. And an hour of waiting should not be spent before the build
has proved it works.

**Quiet means: the session service has reported no turn in flight for five
consecutive minutes.** Every model call an agent makes belongs to a turn,
and `attendance` knows every turn. `bin/rework-cutover.sh up` asks exactly
this before it restarts anything, over the same socket, with the same
`view-ro` bearer.

**Why not an nftables counter.** A named counter on `hook input` matching
`ip daddr 192.0.2.10 tcp dport 4000` never sees the traffic. Measured on the
host 2026-09-21: `ai-litellm` is a BRIDGED container,
`192.0.2.10:4000 -> <its bridge address>:4000`, with `docker-proxy` listening. Docker
DNATs that traffic in `prerouting` and in `output`, so after NAT it is
forwarded to the container and never passes the `input` hook. The counter
never moves, "never moved for five minutes" is always true, and the window
opens at once while turns are running. That is fail OPEN, the one direction
this window exists to prevent. A traffic counter is the wrong instrument even
with the right hooks: `caregiver` reads LiteLLM for spend and keys on every
pass, which would hold a correct counter moving for ever, and Open WebUI
talks to LiteLLM inside the Docker network, where no host hook sees it.

**How root asks without becoming a confused deputy.** Root is the caller and
both the socket and the token file are the operator's. If root opened
`view-ro.token` by path and sent its bytes to a socket the operator controls, a
operator-side process could swap that path for a symlink to a root-only file
and have the file's contents delivered to its own socket. So root opens with
`O_NOFOLLOW` and believes the file only after `fstat` on the OPEN descriptor
says: regular file, the operator's uid, mode 0600, size inside the contract's
bounds. The check is on the descriptor and not on the path, so there is no
window between the check and the read. Root therefore only ever reads bytes
out of a file the operator already owns and can already read, and the same rule on
the socket (`lstat`, must be a socket, must be the operator's) keeps root's `GET`
away from a daemon the operator could not otherwise reach.

`runuser` into the operator's account was the other candidate and it does not work here. Root
would still have to put the bearer somewhere the child can read it: argv is
invariant 13, a file root writes means root read the token first, and a
shell line is §3.2 rule 6. No installed program reads the token and speaks
HTTP in one process, so dropping privilege for the whole query would need a
new one.

**What a lying `attendance` can achieve.** `attendance` is the operator's, so it can
say "quiet" while turns run. The cost is dropped model calls and never a
privilege. That is acceptable where an operator-written live-state document was
not: the document decided WHAT root installs, and a lie there changes the
code on the host. This lie only changes WHEN root does what was already
approved, and the worst outcome is the very thing the window is a courtesy
against — which the operator can cause at any moment anyway, by killing the pi
processes the turns run in.

**`BusySeenFn` is a seam**, and `attendance_signal` is what production wires
into it. Every failure answers None, and None is never quiet.
"""

from __future__ import annotations

import http.client
import json
import os
import socket
import stat
from collections.abc import Callable, Iterable
from enum import StrEnum
from pathlib import Path
from typing import Any, Final, cast

#: §2.8: five consecutive quiet minutes.
QUIET_WINDOW_S: Final = 300.0

#: §2.8: poll every 30 seconds.
QUIET_POLL_S: Final = 30.0

#: §2.8: then fail the release, `step: switch`.
QUIET_MAX_WAIT_S: Final = 3600.0

#: §2.8's three services. Recreating any of them drops model calls.
WATCHED_SERVICES: Final = ("ai-litellm", "ai-postgres", "ai-redis")

#: §2.8's own words, as a fixed reason (§3.2 rule 4).
NO_WINDOW: Final = f"no quiet window in {QUIET_MAX_WAIT_S:.0f}s"

#: Contract 02 §4.1's three states a switch would cut short, and the same
#: three `bin/rework-cutover.sh up` asks for.
#:
#: `running` is a turn making model calls now. `waiting-approval` is a live
#: turn whose tool call waits for a phone tap: it makes no call while it
#: waits, and it resumes with one the moment the tap lands, which root
#: cannot see coming. `queued` is an autonomous turn holding a slot, which
#: starts the instant a slot frees. Counting a state that is momentarily
#: silent costs a delayed release. Not counting one costs dropped calls, so
#: all three count (invariant 19's fail closed, applied to a wait).
BUSY_STATES: Final = ("running", "queued", "waiting-approval")

#: One page of one row. Root needs "is there one", never the list.
LIST_LIMIT: Final = 1

#: One question to `attendance`. Three of them run per poll, so a wedged
#: socket costs at most 15 s of a 30 s poll and the hour cap still holds.
ASK_TIMEOUT_S: Final = 5.0

#: An answer root will read. One row of contract 02 §4.2 is under 1 KiB.
MAX_REPLY_BYTES: Final = 64 * 1024

#: Contract 02 §3 rule 7's floor. A shorter file is not this token.
MIN_TOKEN_BYTES: Final = 32

#: A file root reads whole is a file a writer can grow.
MAX_TOKEN_BYTES: Final = 4096

#: What the `Host:` header carries. `attendance` serves one name over a Unix
#: socket and no DNS is involved, so this is a constant and not a hostname.
SOCKET_HOST: Final = "sessiond"


class Busy(StrEnum):
    """What one question to `attendance` answered."""

    #: At least one session is in a `BUSY_STATES` state.
    YES = "yes"
    #: `attendance` answered, and no session is in flight.
    NO = "no"
    #: Root could not ask, or could not believe the answer. Never quiet.
    UNKNOWN = "unknown"


#: When root last saw the host busy, or None when root could not look.
#: `wait_for_quiet` treats None as busy, so a signal root cannot read waits
#: out the hour instead of recreating `ai-litellm` under live calls.
BusySeenFn = Callable[[], float | None]


def needs_window(recreates: Iterable[str]) -> bool:
    """§2.8: a diff that leaves LiteLLM alone needs no window.

    A change to `litellm-config.yaml` alone is a compose restart of one
    service and not a recreate. It takes the same window, because the
    service still goes away, which is why the check is on the SERVICE and
    not on the verb.
    """
    listed = {one.strip() for one in recreates}

    return any(service in listed for service in WATCHED_SERVICES)


def wait_for_quiet(
    busy_seen: BusySeenFn,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
) -> float | None:
    """Seconds waited, or None after `QUIET_MAX_WAIT_S`.

    Quiet means: the last time root saw the host busy is at least
    `QUIET_WINDOW_S` ago. A read root could not make is never quiet.

    None rather than an exception, so this module imports nothing from
    `install`: `install` reads `WATCHED_SERVICES` from here for the dry
    run's scan, and two modules that import each other are one module with
    a harder failure mode. The caller raises `StepFailed(NO_WINDOW)`.
    """
    started = clock()
    while True:
        now = clock()
        newest = busy_seen()
        if newest is not None and now - newest >= QUIET_WINDOW_S:
            return round(now - started, 1)

        if now - started >= QUIET_MAX_WAIT_S:
            return None

        sleep(QUIET_POLL_S)


def read_view_token(path: Path, owner_uid: int) -> bytes | None:
    """The `view-ro` bearer, or None when root will not vouch for the file.

    Every check runs on the OPEN descriptor, so nothing can be swapped
    between the check and the read. `O_NOFOLLOW` refuses a symlink at the
    last component, and the owner check refuses everything a swapped parent
    directory could point at, because whatever root ends up holding must be
    a file the expected owner wrote.

    `O_NONBLOCK` and `O_NOCTTY` are there so a FIFO or a tty planted at the
    path cannot block root or take a controlling terminal before `fstat`
    gets a word in.
    """
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_NOCTTY | os.O_CLOEXEC
    try:
        handle = os.open(path, flags)
    except OSError:
        return None

    try:
        return _token_of(handle, owner_uid)
    except OSError:
        return None
    finally:
        os.close(handle)


def _token_of(handle: int, owner_uid: int) -> bytes | None:
    """What an open descriptor holds, when root will vouch for all of it."""
    facts = os.fstat(handle)
    if not stat.S_ISREG(facts.st_mode):
        return None

    if facts.st_uid != owner_uid:
        return None

    # Contract 02 §3 rule 5: `view-ro` is not one of the two tokens the PEP
    # reads, so nothing wider than 0600 is this file.
    if stat.S_IMODE(facts.st_mode) & 0o077:
        return None

    if not MIN_TOKEN_BYTES <= facts.st_size <= MAX_TOKEN_BYTES:
        return None

    value = _read_all(handle).strip()

    return value if len(value) >= MIN_TOKEN_BYTES else None


def _read_all(handle: int) -> bytes:
    """Up to the cap, in as many reads as it takes.

    One `os.read` may answer short. A truncated bearer is refused by
    `attendance` and costs the release its whole hour, which is a fail-closed
    end nobody could diagnose, so the loop is worth its four lines.
    """
    chunks: list[bytes] = []
    left = MAX_TOKEN_BYTES
    while left > 0:
        piece = os.read(handle, left)
        if not piece:
            break

        chunks.append(piece)
        left -= len(piece)

    return b"".join(chunks)


def _is_operators_socket(path: Path, owner_uid: int) -> bool:
    """`lstat`, so a link planted at the path is seen as a link.

    A socket is connected and not opened, so there is no `O_NOFOLLOW` for
    this half and the check cannot be moved onto the connected descriptor.
    What it buys is still the whole of the reachable set: root connects only
    to a socket the expected owner bound, which is a socket the operator can talk
    to directly. What root sends is one `GET` and the operator's own bearer, so
    winning the race between the check and the connect gains an attacker
    nothing he did not already hold.
    """
    try:
        facts = os.lstat(path)
    except OSError:
        return False

    return stat.S_ISSOCK(facts.st_mode) and facts.st_uid == owner_uid


class _Connection(http.client.HTTPConnection):
    """`http.client` over `AF_UNIX`. Stdlib only, for `AGENTS.md` rule 4: a
    release re-syncs `releasectl`'s own venv under the running executor, so
    a third-party import can vanish mid-run."""

    def __init__(self, path: str, timeout_s: float) -> None:
        super().__init__(SOCKET_HOST, timeout=timeout_s)
        self._path = path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        try:
            sock.connect(self._path)
        except OSError:
            sock.close()
            raise

        self.sock = sock


def _any_session(socket_path: Path, token: bytes, state: str, timeout_s: float) -> bool | None:
    """Whether `attendance` names one session in `state`, or None.

    The bearer rides a header and never the path (invariant 13): a token in
    a URL lands in every access log and every error message there is.
    """
    connection = _Connection(str(socket_path), timeout_s)
    try:
        connection.request(
            "GET",
            f"/v1/sessions?state={state}&limit={LIST_LIMIT}",
            headers={
                "Authorization": f"Bearer {token.decode('utf-8', 'strict')}",
                "Accept": "application/json",
                "Connection": "close",
            },
        )
        reply = connection.getresponse()
        if reply.status != 200:
            return None

        raw = reply.read(MAX_REPLY_BYTES + 1)
    except (OSError, UnicodeDecodeError, http.client.HTTPException):
        return None
    finally:
        connection.close()

    return _has_rows(raw)


def _has_rows(raw: bytes) -> bool | None:
    """Contract 02 §5.2's answer shape, or None. Everything that crosses a
    process boundary is validated before use (invariant 12)."""
    if len(raw) > MAX_REPLY_BYTES:
        return None

    try:
        loaded: Any = json.loads(raw)
    except ValueError:
        return None

    if not isinstance(loaded, dict):
        return None

    rows = cast("dict[str, object]", loaded).get("sessions")
    if not isinstance(rows, list):
        return None

    return len(cast("list[object]", rows)) > 0


def ask_attendance(
    socket_path: Path,
    token_path: Path,
    owner_uid: int,
    timeout_s: float = ASK_TIMEOUT_S,
) -> Busy:
    """One poll: is any turn in flight?

    The token is read again on every poll, so root holds no copy across an
    hour of waiting and a rotated token is picked up. Every way root can
    fail to get an answer — no socket, no token, a timeout, a non-200, a
    body that is not the list shape — reads as `UNKNOWN`, which is never
    quiet.
    """
    token = read_view_token(token_path, owner_uid)
    if token is None:
        return Busy.UNKNOWN

    if not _is_operators_socket(socket_path, owner_uid):
        return Busy.UNKNOWN

    for state in BUSY_STATES:
        found = _any_session(socket_path, token, state, timeout_s)
        if found is None:
            return Busy.UNKNOWN

        if found:
            return Busy.YES

    return Busy.NO


class _Window:
    """When root last saw the host busy, held across the polls of ONE wait.

    Private: nothing above this module has a reason to reach the stamp.
    """

    def __init__(
        self, socket_path: Path, token_path: Path, owner_uid: int, clock: Callable[[], float]
    ) -> None:
        self._socket = socket_path
        self._token = token_path
        self._owner_uid = owner_uid
        self._clock = clock
        # Root has watched no window yet and may not claim one, so the first
        # poll starts the five minutes rather than ending them.
        self._busy_at = clock()

    def busy_seen(self) -> float | None:
        answer = ask_attendance(self._socket, self._token, self._owner_uid)
        if answer is not Busy.NO:
            # Busy, or root could not ask. Both restart the window: root may
            # only count minutes it actually watched. Without this, four
            # blind minutes followed by one quiet read would open the window
            # on a host root never saw.
            self._busy_at = self._clock()

        if answer is Busy.UNKNOWN:
            return None

        return self._busy_at


def attendance_signal(
    socket_path: Path,
    token_path: Path,
    owner_uid: int,
    clock: Callable[[], float],
) -> BusySeenFn:
    """§2.8's signal: when root last saw a turn in flight.

    `owner_uid` is the operator's, and it is what root demands of both the token
    file and the socket. A caller that passes root's own uid gets a signal
    that refuses every operator-owned file, which is the fail-closed end.
    """
    return _Window(socket_path, token_path, owner_uid, clock).busy_seen
