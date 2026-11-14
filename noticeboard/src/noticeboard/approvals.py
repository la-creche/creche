"""The approval page: one open gate of the chaperone, and the decision on it.

The phone card of a gate holds Approve and Deny behind a long press. A
plain tap opens the card's link instead, and the buttons are gone. The
link is `/approvals/<job>` on this service. The page shows the gate and
posts the decision, so a lost tap is not a lost answer.

The approval plugin of the chaperone (`ha-approval`) holds every gate. It
serves two routes under one bearer, the bearer the flow already uses for
its taps:

    GET  /pending/{job}   the gate while it waits for its tap, else 404
    POST /resume          {"job", "gate", "decision"}, the same as a tap

The plugin forgets a gate when it is decided, when it expires and when
the chaperone stops waiting. The page then answers that the request is
gone. The page thus lives only as long as the request, and it holds no
state of its own.

Three rules this module keeps.

1. **The bearer is read from a file at each call**, never from the
   environment, and it appears in one place: an `Authorization` header.
   No page, log line or URL holds it (invariant 13).
2. **A missing channel decides nothing.** With no URL or no token file,
   each call answers a problem, and the page shows no button.
3. **Every answer of the plugin is untrusted input.** It is read through
   `jsonfiles`, capped and checked against the job that was asked.
"""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import httpx

from . import jsonfiles
from .jsonfiles import Surface

#: A job id of the plugin: 26 lower-case characters of the ULID alphabet.
#: The first holds only 3 bits. A route checks it before any call.
_JOB_RE: Final = re.compile(r"^[0-7][0-9a-hjkmnp-tv-z]{25}\Z")

#: A gate id of the plugin: the first 8 bytes of the gate, in hex.
_GATE_RE: Final = re.compile(r"^[0-9a-f]{16}\Z")

#: An answer of the plugin is one short object.
MAX_ANSWER_BYTES: Final = 64 * 1024

#: The cap of the summary on the page. The plugin cuts it to 400.
MAX_SUMMARY_CHARS: Final = 400

#: The plugin is on the same host. A call that takes longer is a plugin
#: that does not answer.
_TIMEOUT: Final = httpx.Timeout(5.0)

_OK: Final = 200
_NOT_FOUND: Final = 404
_CONFLICT: Final = 409

#: What a page says when the client refuses a request. It names no part of
#: the request, because a header holds the bearer.
_NOT_SENT: Final = "cannot call the approval plugin: the client refused the request"
_NO_CHANNEL: Final = "the approval page is not set up on this host"
_NO_TOKEN: Final = "the approval token file cannot be read"


def is_job(value: str) -> bool:
    """A job id of the plugin. A route checks it, because it becomes one
    segment of a path on the plugin."""
    return _JOB_RE.match(value) is not None


class Decision(enum.StrEnum):
    """The two buttons of the page, with the words of `/resume`."""

    APPROVE = "approve"
    DENY = "deny"


class Outcome(enum.StrEnum):
    """What the plugin did with a decision of the page."""

    #: The first decision. The waiting call is answered.
    RECORDED = "recorded"
    #: The same decision came before, from the card or from the page.
    REPEATED = "repeated"
    #: The other decision came first, and it stands.
    CONTRADICTED = "contradicted"
    #: The gate is decided, expired or no longer awaited.
    GONE = "gone"
    #: The plugin did not answer, or answered something else.
    FAILED = "failed"


@dataclass(frozen=True)
class OpenGate:
    """A gate that waits for its tap."""

    job: str
    gate: str
    agent: str
    summary: str
    expires_in_s: int


@dataclass(frozen=True)
class Lookup:
    """The gate, or no gate and maybe a problem.

    No gate and no problem means the request is gone.
    """

    gate: OpenGate | None = None
    problem: str = ""


@dataclass(frozen=True)
class Sent:
    """The outcome of a decision, and the problem of a failure."""

    outcome: Outcome
    problem: str = ""


@dataclass
class ApprovalChannel:
    """The two calls of the page to the approval plugin.

    `client` carries the plugin's base URL. `token_file` holds the bearer.
    Either one `None` is a channel that is not set up (rule 2).
    """

    client: httpx.Client | None
    token_file: Path | None

    def lookup(self, job: str) -> Lookup:
        """The gate of `job` while it waits for its tap."""
        reply, problem = self._call("GET", f"/pending/{job}", None)

        if reply is None:
            return Lookup(problem=problem)

        if reply.status_code == _NOT_FOUND:
            return Lookup()

        if reply.status_code != _OK:
            return Lookup(problem=f"the approval plugin answered HTTP {reply.status_code}")

        return _gate_of(reply.content, job)

    def decide(self, gate: OpenGate, decision: Decision) -> Sent:
        """Send `decision` on `gate`, the same as a tap on the card."""
        body = {"job": gate.job, "gate": gate.gate, "decision": decision.value}
        reply, problem = self._call("POST", "/resume", body)

        if reply is None:
            return Sent(Outcome.FAILED, problem)

        return _sent_of(reply)

    def _call(
        self, method: str, path: str, body: dict[str, str] | None
    ) -> tuple[httpx.Response | None, str]:
        if self.client is None or self.token_file is None:
            return None, _NO_CHANNEL

        bearer = _token(self.token_file)

        if not bearer:
            return None, _NO_TOKEN

        try:
            reply = self.client.request(
                method,
                path,
                json=body,
                # The bearer appears here and in no URL, no log line and no
                # page (invariant 13).
                headers={"Authorization": f"Bearer {bearer}"},
                timeout=_TIMEOUT,
            )
        except (httpx.LocalProtocolError, httpx.InvalidURL, UnicodeEncodeError):
            # The text of such an error can quote the header that holds the
            # bearer. The page gets a fixed sentence.
            return None, _NOT_SENT
        except httpx.HTTPError as error:
            return None, f"cannot reach the approval plugin: {type(error).__name__}"

        return reply, ""


def build_channel(url: str, token_file: Path | None) -> ApprovalChannel:
    """The channel of the configuration. An empty URL is no channel."""
    if not url:
        return ApprovalChannel(client=None, token_file=token_file)

    return ApprovalChannel(client=httpx.Client(base_url=url), token_file=token_file)


def _token(path: Path) -> str:
    """The bearer, or empty for a file that cannot be read."""
    try:
        return path.read_text(encoding="utf-8").strip()
    except (OSError, ValueError):
        # A decode error quotes bytes of the file, so it reaches no page.
        return ""


def _gate_of(raw: bytes, job: str) -> Lookup:
    body, problem = jsonfiles.parse_object(
        raw, "the approval answer", MAX_ANSWER_BYTES, surface=Surface.APPROVAL_ANSWER
    )

    if body is None:
        return Lookup(problem=problem or "the approval answer is not an object")

    gate = jsonfiles.text(body, "gate")

    if jsonfiles.text(body, "job") != job or not _GATE_RE.match(gate):
        return Lookup(problem="the approval answer names another gate")

    return Lookup(
        gate=OpenGate(
            job=job,
            gate=gate,
            agent=jsonfiles.text(body, "agent"),
            summary=jsonfiles.text(body, "summary", MAX_SUMMARY_CHARS),
            expires_in_s=max(0, jsonfiles.integer(body, "expires_in_s")),
        )
    )


def _sent_of(reply: httpx.Response) -> Sent:
    """The status of `/resume`, which says what the decision did."""
    if reply.status_code == _NOT_FOUND:
        return Sent(Outcome.GONE)

    if reply.status_code == _CONFLICT:
        return Sent(Outcome.CONTRADICTED)

    if reply.status_code != _OK:
        return Sent(Outcome.FAILED, f"the approval plugin answered HTTP {reply.status_code}")

    body, problem = jsonfiles.parse_object(
        reply.content, "the approval answer", MAX_ANSWER_BYTES, surface=Surface.APPROVAL_ANSWER
    )

    if body is None:
        return Sent(Outcome.FAILED, problem or "the approval answer is not an object")

    status = jsonfiles.text(body, "status")

    if status == "resumed":
        return Sent(Outcome.RECORDED)

    if status == "already_decided":
        return Sent(Outcome.REPEATED)

    return Sent(Outcome.FAILED, "the approval answer has no known status")
