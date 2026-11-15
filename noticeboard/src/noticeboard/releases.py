"""The release page: one open release card, and one more push of it.

A release card on the phone holds Approve and Deny behind a long press. A
plain tap opens the card's link instead, `/releases/<id>` on this service,
and the card is gone from the phone. The page shows the release and can
send the same card again. The operator then long-presses the card that
came again.

**The page decides nothing.** Root takes a release verdict only from the
tap on the card (`handover/src/handover/executor/phone.py`, invariant 10).
This service runs as the operator's user, as the requester of a release
does. A bearer here that could decide would let such a process approve
its own release. So the bearer of this page reads a card and asks for one
more push of it, and the flow refuses it every other kind.

The approvals Node-RED holds each open release card. Its one hook takes
two kinds under the bearer of this page:

    {"kind": "release-pending", "family": <id>}
        200 {family, title, message, decision, expires_in_s}, else 404
    {"kind": "release-resend", "family": <id>}
        200 sent, 404 gone, 409 decided, 429 too soon

`family` is the field of the release id on every release body of the
hook. The flow sends the card that it holds, so the page cannot change its
text. The flow forgets a card 15 minutes after its push, when root's gate
times out, and the page then answers that the release is gone.

Two rules this module keeps.

1. **The bearer is read from a file at each call** (`bearercall.py`).
2. **Every answer of the flow is untrusted input.** It is read through
   `jsonfiles`, capped and checked against the release that was asked.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import httpx

from . import approvals, bearercall, jsonfiles
from .jsonfiles import Surface

#: The two kinds of the hook that the bearer of this page may send.
KIND_PENDING: Final = "release-pending"
KIND_RESEND: Final = "release-resend"

#: An answer of the flow is one short object.
MAX_ANSWER_BYTES: Final = 64 * 1024

#: The caps of the flow on the two texts of a release card.
MAX_TITLE_CHARS: Final = 60
MAX_MESSAGE_CHARS: Final = 900

_OK: Final = 200
_NOT_FOUND: Final = 404
_CONFLICT: Final = 409
_TOO_MANY: Final = 429

#: The words of the problems of this page (`bearercall.py`).
_PEER: Final = bearercall.Peer(name="the approvals Node-RED", page="release")


def is_release(value: str) -> bool:
    """A release id as the card carries it.

    Root lower-cases the release ULID for the card (`action_id_of`). That is
    the form of a job id of the approval plugin, so one pattern holds both.
    """
    return approvals.is_job(value)


class Tap(enum.StrEnum):
    """What the flow holds for the card: no tap yet, or the tap."""

    PENDING = "pending"
    APPROVE = "approve"
    DENY = "deny"


class Outcome(enum.StrEnum):
    """What the flow did with a request for one more push."""

    #: The card is on the phone again.
    SENT = "sent"
    #: A tap came first. The card stays decided.
    DECIDED = "decided"
    #: The card expired, or the flow never held it.
    GONE = "gone"
    #: The card went out a moment ago. The flow sends it again later.
    TOO_SOON = "too_soon"
    #: The flow did not answer, or answered something else.
    FAILED = "failed"


@dataclass(frozen=True)
class OpenRelease:
    """A release card that the flow holds."""

    release: str
    title: str
    message: str
    tap: Tap
    expires_in_s: int


@dataclass(frozen=True)
class Lookup:
    """The card, or no card and maybe a problem.

    No card and no problem means the release is gone.
    """

    card: OpenRelease | None = None
    problem: str = ""


@dataclass(frozen=True)
class Sent:
    """The outcome of a request for one more push, and its problem."""

    outcome: Outcome
    problem: str = ""


@dataclass
class ReleaseChannel:
    """The two calls of the page to the hook of the approvals Node-RED.

    `client` is None, or `url` is empty, or `token_file` is None: a channel
    that is not set up. Each call then answers a problem.
    """

    client: httpx.Client | None
    url: str
    token_file: Path | None

    def lookup(self, release: str) -> Lookup:
        """The card of `release` while the flow holds it."""
        reply, problem = self._call(KIND_PENDING, release)

        if reply is None:
            return Lookup(problem=problem)

        if reply.status_code == _NOT_FOUND:
            return Lookup()

        if reply.status_code != _OK:
            return Lookup(problem=f"{_PEER.name} answered HTTP {reply.status_code}")

        return _card_of(reply.content, release)

    def resend(self, release: str) -> Sent:
        """Ask the flow to push the card of `release` again."""
        reply, problem = self._call(KIND_RESEND, release)

        if reply is None:
            return Sent(Outcome.FAILED, problem)

        return _sent_of(reply)

    def _call(self, kind: str, release: str) -> tuple[httpx.Response | None, str]:
        client = self.client if self.url else None
        body = {"kind": kind, "family": release}

        return bearercall.call(_PEER, client, self.token_file, ("POST", self.url, body))


def build_channel(url: str, token_file: Path | None) -> ReleaseChannel:
    """The channel of the configuration. An empty URL is no channel."""
    if not url:
        return ReleaseChannel(client=None, url="", token_file=token_file)

    return ReleaseChannel(client=httpx.Client(), url=url, token_file=token_file)


def _card_of(raw: bytes, release: str) -> Lookup:
    body, problem = jsonfiles.parse_object(
        raw, "the release answer", MAX_ANSWER_BYTES, surface=Surface.RELEASE_ANSWER
    )

    if body is None:
        return Lookup(problem=problem or "the release answer is not an object")

    if jsonfiles.text(body, "family") != release:
        return Lookup(problem="the release answer names another release")

    try:
        tap = Tap(jsonfiles.text(body, "decision"))
    except ValueError:
        return Lookup(problem="the release answer has no known decision")

    title = jsonfiles.text(body, "title", MAX_TITLE_CHARS)
    message = jsonfiles.text(body, "message", MAX_MESSAGE_CHARS)

    if not title.strip() or not message.strip():
        return Lookup(problem="the release answer has no card text")

    return Lookup(
        card=OpenRelease(
            release=release,
            title=title,
            message=message,
            tap=tap,
            expires_in_s=max(0, jsonfiles.integer(body, "expires_in_s")),
        )
    )


def _sent_of(reply: httpx.Response) -> Sent:
    """The status of `release-resend`, which says what the flow did."""
    if reply.status_code == _OK:
        return Sent(Outcome.SENT)

    if reply.status_code == _NOT_FOUND:
        return Sent(Outcome.GONE)

    if reply.status_code == _CONFLICT:
        return Sent(Outcome.DECIDED)

    if reply.status_code == _TOO_MANY:
        return Sent(Outcome.TOO_SOON)

    return Sent(Outcome.FAILED, f"{_PEER.name} answered HTTP {reply.status_code}")
