"""The intake's entry point: drain, mint, push, serve, close.

This module drains the gaps, mints the tokens, starts the listener and
builds the sealer, and `systemd/agent-rework-intake.service` starts it.

One pass, every `POLL_S`:

1. Read the gap directory as hostile bytes (`gaps.GapDirectory`).
2. A gap whose name ALREADY has a value is stale — the break-glass sops
   path can fill one — so root closes it and mints nothing.
3. A gap root has not pushed recently gets ONE token and ONE push. A push
   that did not land drops the token: a capability nobody was told about
   is not a capability, and it would hold the listener open for nothing.
4. The listener is bound while a token is pending and closed when none is
   (`Listener`). **A host with no open gap has no root listener on the LAN
   at all**, which is the smallest surface this design can have while
   still being a service the operator can reach when they need it.

Four rules this module keeps.

1. **The token appears in exactly one place**: the link inside the push
   body. Not in a journal line, not in an argv, not in a file. Every
   `print` here names a server and a secret, both of which passed their
   patterns, and never a token and never a value.
2. **Root's own cooldown decides re-pushes.** `caregiver` re-opens a gap
   whose name still has no value on every reconcile pass, which is a few
   seconds apart, so the gap file cannot be what paces the phone. Root
   remembers what it pushed and when.
3. **Root proves the certificate and the key before it loads them.** A
   key file the operator can write is a certificate the operator chose, and root would
   then serve the operator's browser a certificate an attacker holds the private
   half of. The paths are root's and are checked as such.
4. **The process holds two secrets and drops the rest.** The wrapper runs
   it under `sops exec-env`, so root's environment arrives holding every
   compose secret — and unlike the executor, this process is long lived.
   `keep_only_the_push_secrets` takes the two it needs and clears the
   rest before the first connection is accepted.
"""

from __future__ import annotations

import os
import pwd
import stat
import sys
import threading
import time
from collections.abc import Callable, MutableMapping
from dataclasses import dataclass, field
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Final, Protocol

from ..errors import Refusal
from ..executor.host import make_sealer
from ..site import lan_address, operator_user
from .gaps import DEFAULT_GAPS_DIR, GapDirectory, OpenGap
from .notify import TOKEN_ENV, URL_ENV, PushFn, make_push, say_nothing
from .service import DEFAULT_PORT, Intake, serve
from .store import DEFAULT_SECRETS_DIR, SecretStore, recipients_of
from .token import TOKEN_TTL_S, Tokens

LOG_PREFIX: Final = "agent-release-intake"

#: How often root looks at the gap directory. A gap is opened by a
#: reconciler and answered by a human, so seconds of lag cost nothing.
POLL_S: Final = 5.0

#: How long root waits before it pushes the SAME gap again. It covers the
#: whole life of the token it last minted plus a pause, so there is never
#: a second live token for one name and the operator gets at most one
#: notification an hour for a gap they have not answered.
REMINT_COOLDOWN_S: Final = TOKEN_TTL_S + 2700.0

#: How many gaps one pass will push for. The gap
#: directory is operator-writable and `gaps.MAX_GAPS_PER_PASS` is 64, so
#: without this ONE write cost the operator sixty-four notifications at once.
#: `Watch` paces a repeat of one NAME, and a writer that wants a flood uses
#: a new name each round, so the burst needs its own bound.
#:
#: A cap on the total would be worse than a cap on the burst: a writer that
#: exhausted a total would block the one human step this service exists
#: for, and a burst cap only makes a real roster arrive over a few passes.
#: What is left — a sustained flood pacing the operator's phone and holding the
#: port bound — is denial of the intake by a writer who can deny it more
#: simply by filling the disk.
MAX_PUSHES_PER_PASS: Final = 8

#: How many names `Watch` remembers. It is a dict keyed on a name the
#: attacker writes, in a process that runs for days rather than for one
#: release.
MAX_WATCHED: Final = 256

#: Root's own certificate and key for the host's LAN address, port 8380.
#: Root-owned, made BY HAND, never by a release, and never in a repository —
#: see the unit.
#: A self-signed certificate with the LAN address in `subjectAltName`.
DEFAULT_CERT_FILE: Final = "/etc/agent-intake/intake.crt"
DEFAULT_KEY_FILE: Final = "/etc/agent-intake/intake.key"

#: Root's own `.sops.yaml`, copied there by hand from the repository. It
#: is a trust root: adding one recipient reads every secret written
#: afterwards, so it lives where only root can write it and never in a
#: checkout under `/srv/agents/work/platform/`, which is operator-writable.
ROOT_SOPS_FILE: Final = "/etc/agent-intake/sops.yaml"

#: What `recipients_of` matches `path_regex` against. Every secret this
#: service writes lands beside every other, so one representative path
#: picks the same rule as any of them would.
RECIPIENT_PROBE_PATH: Final = f"{DEFAULT_SECRETS_DIR}/a_secret.enc"

#: What this process keeps of root's environment. The same shape as
#: `executor/drain.KEPT_ENV`, and the reason is sharper here: the
#: executor is a oneshot and this is a listener that runs for days.
KEPT_ENV: Final = frozenset({"PATH", "HOME", "LANG", "LC_ALL", "TZ"})


@dataclass(frozen=True)
class Secrets:
    """The two values root keeps out of `sops exec-env`'s whole set."""

    approval_url: str
    approval_token: str


def keep_only_the_push_secrets(environ: MutableMapping[str, str]) -> Secrets:
    """Take the hook out of the environment, clear the rest.

    It does NOT rewrite `/proc/<pid>/environ`, which keeps what `execve`
    was given. That file is root-only, so what this bounds is anything
    that reads `os.environ` inside this process — including a traceback
    printer, which is the realistic one for a listener.
    """
    found = Secrets(environ.get(URL_ENV, ""), environ.get(TOKEN_ENV, ""))
    for name in [one for one in environ if one not in KEPT_ENV]:
        del environ[name]

    return found


class Binding(Protocol):
    """What `one_pass` needs of a listener: whether it is bound, and the
    two verbs that change that. A test binds no socket at all."""

    @property
    def bound(self) -> bool: ...

    def open(self) -> bool: ...

    def close(self) -> None: ...


class Listener:
    """The HTTPS listener, bound only while a token is pending.

    `ThreadingHTTPServer.serve_forever` runs in one thread and
    `shutdown()` stops it. The socket is created by `open` and released by
    `close`, so between gaps the port answers nothing at all.
    """

    def __init__(self, intake: Intake, certfile: str, keyfile: str, bind: str, port: int) -> None:
        self._intake = intake
        self._certfile = certfile
        self._keyfile = keyfile
        self._bind = bind
        self._port = port
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def bound(self) -> bool:
        return self._server is not None

    @property
    def port(self) -> int:
        """The port actually bound, or 0. Production asks for one fixed
        port and gets it, so this exists for the end-to-end test, which
        asks for an ephemeral one on loopback."""
        if self._server is None:
            return 0

        return int(self._server.server_address[1])

    def open(self) -> bool:
        """Bind and serve. False means root could not, and root then mints
        nothing: a token for a page nobody can open is a wasted push and a
        live capability with no purpose.

        A bind fails for reasons root cannot fix in this pass — the
        address is not up yet at boot, the port is taken, the certificate
        was replaced under it — so the failure is not fatal and the next
        pass tries again.
        """
        if self.bound:
            return True

        try:
            server = serve(self._intake, self._certfile, self._keyfile, self._bind, self._port)
        except OSError:
            return False

        thread = threading.Thread(target=server.serve_forever, name="intake", daemon=True)
        thread.start()
        self._server, self._thread = server, thread

        return True

    def close(self) -> None:
        """Release the port. A wedged connection thread is a daemon
        thread, so it can never hold a shutdown open."""
        server, thread = self._server, self._thread
        self._server, self._thread = None, None
        if server is None:
            return

        server.shutdown()
        server.server_close()
        if thread is not None:
            thread.join(timeout=POLL_S)


@dataclass
class Watch:
    """What root pushed, and when. Rule 2.

    It holds names and timestamps, never a token and never a value, so a
    dump of this object teaches an attacker the roster it already wrote.
    """

    clock: Callable[[], float]
    pushed_at: dict[str, float] = field(default_factory=dict[str, float])

    def due(self, gap: OpenGap) -> bool:
        last = self.pushed_at.get(gap.secret)

        return last is None or self.clock() - last >= REMINT_COOLDOWN_S

    def pushed(self, gap: OpenGap) -> None:
        self.pushed_at[gap.secret] = self.clock()
        self._forget_oldest()

    def _forget_oldest(self) -> None:
        """Bound the roster. A name that falls out
        is a name root may push for again, which costs one notification
        and is paced by `MAX_PUSHES_PER_PASS`."""
        while len(self.pushed_at) > MAX_WATCHED:
            oldest = min(self.pushed_at, key=lambda name: self.pushed_at[name])
            del self.pushed_at[oldest]


@dataclass(frozen=True)
class Wiring:
    """Every side effect one pass has, injected so a test fakes all of it."""

    gaps: GapDirectory
    store: SecretStore
    tokens: Tokens
    push: PushFn
    watch: Watch
    listener: Binding
    say: Callable[[str], None] = print


def one_pass(wiring: Wiring) -> tuple[int, int]:
    """Drain, mint, push, then bind or unbind. Answers (minted, closed).

    A pass never raises for one bad gap. The directory is operator-written,
    so one planted entry must not stop every real one.
    """
    minted, closed, held = 0, 0, 0
    for gap in wiring.gaps.open_gaps():
        if wiring.store.has(gap.secret):
            # Stale: the name was filled some other way, and §4.3 keeps
            # the sops path as break glass. Nothing to ask the operator.
            closed += int(wiring.gaps.close(gap))
            continue

        if minted >= MAX_PUSHES_PER_PASS:
            # The burst cap, not a refusal: the gap is read again next
            # pass. Stale gaps are still closed above, because a cap on
            # pushes must not stop root tidying what it already filled.
            held += 1
            continue

        if _mint_and_push(wiring, gap):
            minted += 1

    if held:
        wiring.say(f"{LOG_PREFIX}: {held} more gaps are waiting for a later pass")

    _match_listener(wiring)

    return minted, closed


def _mint_and_push(wiring: Wiring, gap: OpenGap) -> bool:
    """One gap's token and one gap's push, or nothing at all."""
    if not wiring.watch.due(gap):
        return False

    if not wiring.listener.open():
        wiring.say(f"{LOG_PREFIX}: no listener, so {gap.server}/{gap.secret} was not pushed")

        return False

    try:
        token = wiring.tokens.mint(gap.server, gap.secret)
    except (ValueError, RuntimeError):
        # A name root cannot mint for, or the pending cap. Both are the
        # attacker's roster, and neither is worth a second line.
        return False

    if not wiring.push(gap, token):
        # Rule 4 of `notify.py`: nobody was told, so nobody holds it.
        wiring.tokens.drop(token)
        wiring.say(f"{LOG_PREFIX}: the push for {gap.server}/{gap.secret} did not land")

        return False

    wiring.watch.pushed(gap)
    wiring.say(f"{LOG_PREFIX}: pushed a link for {gap.server}/{gap.secret}")

    return True


def _match_listener(wiring: Wiring) -> None:
    """The listener exists exactly while a token does.

    The close lands at the end of a pass rather than the instant the last
    token is spent, so the port stays bound for at most one `POLL_S` past
    the operator's paste. Closing it from inside a request would shut the server
    down from one of its own handler threads.
    """
    if wiring.tokens.pending_count() == 0 and wiring.listener.bound:
        wiring.listener.close()
        wiring.say(f"{LOG_PREFIX}: no gap is waiting, so the listener is closed")


def proved_root_file(path: Path, owner_uid: int = 0) -> bool:
    """Rule 3. A regular file owned by root and closed to group and other.

    `O_NOFOLLOW` refuses a symlink planted at the path, and `fstat` on the
    descriptor answers about the file root actually opened rather than
    about whatever the name pointed at a moment ago.
    """
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        return False

    try:
        info = os.fstat(fd)
    finally:
        os.close(fd)

    if not stat.S_ISREG(info.st_mode) or info.st_uid != owner_uid:
        return False

    return not info.st_mode & (stat.S_IWGRP | stat.S_IWOTH | stat.S_IRGRP | stat.S_IROTH)


def build_wiring(secrets: Secrets, writer_uid: int, clock: Callable[[], float]) -> Wiring:
    """The production wiring.

    A host with no hook configured gets `say_nothing`, which answers
    False, so every token is dropped and no page is ever served. That is
    the fail-closed end: an intake that cannot tell the operator about a token
    must not leave one lying about.

    The address comes from the site FILE, read here rather than at import:
    the intake clears its environment, so no unit can hand it one. A site
    with none is the `site` refusal.
    """
    address = lan_address()
    tokens = Tokens(clock)
    store = SecretStore(Path(DEFAULT_SECRETS_DIR), make_sealer(_recipients(), writer_uid))
    gaps = GapDirectory(Path(DEFAULT_GAPS_DIR), writer_uid)
    intake = Intake(tokens=tokens, store=store, close_gap=gaps.close_named)
    push = (
        make_push(secrets.approval_url, secrets.approval_token, _link_base(address))
        if secrets.approval_url and secrets.approval_token
        else say_nothing
    )

    return Wiring(
        gaps=gaps,
        store=store,
        tokens=tokens,
        push=push,
        watch=Watch(clock),
        listener=Listener(intake, DEFAULT_CERT_FILE, DEFAULT_KEY_FILE, address, DEFAULT_PORT),
    )


def _link_base(address: str) -> str:
    """What the link the operator taps starts with. It is root's own
    configuration and holds no byte a requester wrote."""
    return f"https://{address}:{DEFAULT_PORT}"


def _recipients() -> tuple[str, ...]:
    """Root's own copy of the recipient set.

    A checkout is the wrong home for root's copy of `.sops.yaml`, because
    a checkout is operator-writable. So root reads its own file at a
    root-owned path, and `recipients_of` refuses it unless root owns it
    and nobody else may write it. An empty answer refuses every seal,
    which is the fail-closed end.
    """
    return recipients_of(Path(ROOT_SOPS_FILE), RECIPIENT_PROBE_PATH)


def main() -> int:
    """Root only. It runs until systemd stops it."""
    if os.geteuid() != 0:
        print(f"{LOG_PREFIX}: root only (agent-rework-intake.service)", file=sys.stderr)

        return 1

    secrets = keep_only_the_push_secrets(os.environ)
    if not _certificate_is_roots():
        return 1

    # Whose files the gap directory holds: `caregiver` runs as the operator.
    try:
        writer_uid = pwd.getpwnam(operator_user()).pw_uid
        wiring = build_wiring(secrets, writer_uid, time.time)
    except Refusal as refused:
        print(f"{LOG_PREFIX}: {refused.as_line()}", file=sys.stderr)

        return 1

    print(f"{LOG_PREFIX}: watching {DEFAULT_GAPS_DIR}", flush=True)
    _loop(wiring, time.sleep)

    return 0


def _certificate_is_roots() -> bool:
    for path in (DEFAULT_CERT_FILE, DEFAULT_KEY_FILE):
        if not proved_root_file(Path(path)):
            print(f"{LOG_PREFIX}: {path} is not a root-owned private file", file=sys.stderr)

            return False

    return True


def _loop(wiring: Wiring, sleep: Callable[[float], None]) -> None:
    """Forever, and one bad pass never ends it.

    An `OSError` here is a directory that came and went, which is a normal
    thing for a directory another process writes. The pass after it reads
    the same gaps again.
    """
    while True:
        try:
            one_pass(wiring)
        except OSError as exc:
            print(f"{LOG_PREFIX}: a pass failed ({type(exc).__name__})", flush=True)

        sleep(POLL_S)
