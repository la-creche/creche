"""The one-time token (`stage7-releases.md` §4.3, step 1).

32 random bytes, bound to exactly one secret name, single use, 15 minutes.
**The token is a capability, not a value.** It authorizes one write of one
named gap and nothing else: it cannot read, it cannot overwrite, and it
cannot name a second secret.

Three rules, each with the attack it stops.

1. **The mint never answers its caller.** The token leaves this process on
   one actionable phone push and by no other route (§4.3 step 2). A
   operator-side process that could both ask for a token and read it would be
   able to fill a gap by itself, which is the one thing the phone step is
   there to prevent.
2. **A claim is constant time.** `hmac.compare_digest`, because a token
   compared byte by byte is a token guessable one byte at a time.
3. **Tokens live in memory only.** A restart drops every pending one,
   which costs the operator a second push and leaks nothing.
"""

from __future__ import annotations

import hmac
import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

#: §4.3 step 1: 32 random bytes. `token_urlsafe` takes a byte count and
#: answers base64url, so the string is longer than the entropy it carries.
TOKEN_BYTES: Final = 32

#: §4.3 step 1: fifteen minutes. The same window as an approval, because it
#: is the same human doing the same thing.
TOKEN_TTL_S: Final = 900.0

#: How many gaps may wait at once. A gap arrives from the reconciler, and
#: the roster of MCP servers is small. A cap keeps a loop in a caller from
#: growing root's memory without bound.
MAX_PENDING_TOKENS: Final = 64

#: A family or server name (contracts 01 and 01b). The gap root drains
#: carries this value out of `mcp/<name>/server.yaml`, which the attacker
#: writes, and the form page shows it to the operator.
SERVER_NAME_RE: Final = re.compile(r"[a-z][a-z0-9-]{1,30}")

#: Contract 01b §4.1's `secret:<key>`, the same pattern `store.py` uses on
#: the name before it becomes a file name.
SECRET_NAME_RE: Final = re.compile(r"[a-z][a-z0-9_]{1,62}")


@dataclass(frozen=True)
class Gap:
    """What one token authorizes: one value, for one name, of one server."""

    server: str
    name: str
    expires_at: float


class Tokens:
    """Every token root has minted and not yet spent."""

    def __init__(self, clock: Callable[[], float]) -> None:
        self._clock = clock
        self._pending: dict[str, Gap] = {}
        #: Claimed and not yet resolved. A reservation is NOT claimable, so
        #: single use holds while the value is still being sealed.
        self._reserved: dict[str, Gap] = {}

    def mint(self, server: str, name: str) -> str:
        """One token for one gap. The caller pushes it to the phone.

        Returns the token so the PUSH can carry it. No HTTP route of this
        service answers with one, which is rule 1.

        Both names are checked here and not only at the caller. They come
        out of a gap file whose `server` field is copied from
        `mcp/<name>/server.yaml`, which the attacker
        writes, and the form page shows the server name to the operator. A
        `ValueError` is deliberate: a gap root cannot name is a gap root
        must not push, and the caller that drains the gap directory is the
        place to refuse the file, not to guess a name.
        """
        if SERVER_NAME_RE.fullmatch(server) is None:
            raise ValueError("the gap names no server this may mint for")

        if SECRET_NAME_RE.fullmatch(name) is None:
            raise ValueError("the gap names no secret this may mint for")

        self._forget_expired()
        if len(self._pending) >= MAX_PENDING_TOKENS:
            # Never evict the OLDEST pending token to keep minting. The
            # roster is driven by `mcp/<name>/server.yaml` files the
            # attacker adds, so a flood of declared servers would drop the
            # token the operator is walking to the phone to use. A token in flight
            # outranks one nobody has seen yet.
            raise RuntimeError("too many gaps are already waiting for a value")

        token = secrets.token_urlsafe(TOKEN_BYTES)
        self._pending[token] = Gap(server, name, self._clock() + TOKEN_TTL_S)

        return token

    def describe(self, token: str) -> Gap | None:
        """What this token is for, without spending it.

        The form page needs the server and the secret name to show the operator
        what they are pasting into. Reading the page must not spend the token,
        or a reload would cost them the paste.
        """
        return self._match(token)

    def claim(self, token: str, name: str) -> Gap | None:
        """RESERVE the token, or answer None.

        A reservation is not claimable, so two requests racing on one
        token still fill one gap. The caller then calls `spend` when the
        value landed, or `release` when it did not.

        None covers every refusal, and they are one answer on purpose: an
        unknown token, an expired one, one already spent, one already
        reserved, and one bound to another name all tell a guesser the
        same thing.
        """
        found = self._match(token)
        if found is None or found.name != name:
            return None

        self._forget(token)
        self._reserved[token] = found

        return found

    def spend(self, token: str) -> None:
        """The value landed. The token is gone for good."""
        self._reserved.pop(token, None)

    def release(self, token: str) -> None:
        """The value did NOT land, so give the token back.

        A `claim` that destroys the token before `SecretStore.fill` runs
        lets one failed seal — a missing `sops`, a full disk, a planted
        symlink — cost the operator the token, store nothing, and need a whole new
        phone push.
        """
        gap = self._reserved.pop(token, None)
        if gap is None or self._clock() > gap.expires_at:
            return

        self._pending[token] = gap

    def drop(self, token: str) -> None:
        """Throw a token away before anybody was told about it.

        `make_push` answers False when the hook could not be reached, and
        a token nobody holds is worse than no token: it fills one of
        `MAX_PENDING_TOKENS` and keeps the listener bound for fifteen
        minutes for a notification that never arrived. The mint is the
        only place that ever learns a token's value, so the mint's caller
        is the only place that can undo one.
        """
        self._forget(token)

    def pending_count(self) -> int:
        """How many tokens are alive. `run.py` binds the listener while
        this is above zero and closes it when it reaches zero, so a host
        with no gap open has no root listener on the LAN at all."""
        self._forget_expired()

        return len(self._pending)

    def _match(self, token: str) -> Gap | None:
        """The constant-time lookup. A dict hit would compare byte by byte
        and answer faster for a near miss, which is rule 2's attack."""
        probe = _ascii(token)
        if probe is None:
            return None

        now = self._clock()
        found: Gap | None = None
        for known, gap in self._pending.items():
            if hmac.compare_digest(known.encode("ascii"), probe) and now <= gap.expires_at:
                found = gap

        return found

    def _forget(self, token: str) -> None:
        probe = _ascii(token)
        if probe is None:
            return

        for known in list(self._pending):
            if hmac.compare_digest(known.encode("ascii"), probe):
                del self._pending[known]

    def _forget_expired(self) -> None:
        now = self._clock()
        for token, gap in list(self._pending.items()):
            if now > gap.expires_at:
                del self._pending[token]

        # A reservation whose caller never came back. It is unreachable
        # either way, and leaving it would grow root's memory.
        for token, gap in list(self._reserved.items()):
            if now > gap.expires_at:
                del self._reserved[token]


def _ascii(token: str) -> bytes | None:
    """The token as bytes, or None when it holds a character a minted
    token never can.

    `hmac.compare_digest` on two `str` RAISES on a non-ASCII character
    rather than answering False, and the caller supplies one side: the
    request line, which `http.server` decodes as iso-8859-1, or a JSON
    field, which carries any Unicode. An exception here leaves the handler
    with no answer at all, which rule 5's closed refusal list promises
    never happens. `token_urlsafe` answers ASCII, so a token that is not
    ASCII is simply a miss.
    """
    try:
        return token.encode("ascii")
    except UnicodeEncodeError:
        return None
