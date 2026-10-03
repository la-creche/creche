"""The real phone path: the one the PEP's gates already use.

`stage7-releases.md` §2.5 and contract 04 §8. This module is the
production `Transport` and the production `Notifier`, and it is **not a
second approval system**: it posts to the same protected Node-RED hook, in
the same body shape, under the same kind of bearer, as
`chaperone/src/chaperone/gatekeeper.py`'s `HttpApprovalNotifier`.

Contract 04 §8.4's four rules, and where each lands here:

1. The push carries family, gate and summary. `_push` sends exactly those
   plus `kind`, which is what the PEP's own notifier sends.
2. Node-RED sends the actionable push through Home Assistant.
3. The tap raises `mobile_app_notification_action` on Node-RED's own
   outbound websocket. Nothing inbound reaches this process.
4. Node-RED holds the decision. **Root asks for it**, at the same URL,
   under the same bearer.

Rule 4 is where a release differs from a PEP call, and the reason is
structural. The PEP owns an HTTP server, so Node-RED calls it back at
`POST /approval/<gate>`. Root's executor owns none: the one root listener
is the secret intake, a unit of its own. So root polls. The one
thing root must never do is read the verdict out of a file the operator can
write: that would let the requester approve its own release and take
invariant 10 with it. Polling the hook keeps the decision behind the
bearer the whole way.

Three rules this module keeps.

1. **Stdlib only.** A release re-syncs `handover`'s own venv under the
   running executor (contract 06 §1.1), so a third-party import can vanish
   mid-run. `urllib.request` cannot.
2. **No secret in a URL, in argv or in a log line** (invariant 13). The
   bearer rides one header. A failure logs the status, never the body.
3. **Fail closed.** An unreachable hook, an unparseable answer, an answer
   for another gate: all deny. Contract 04 §8.4 already says an
   undeliverable push denies at once.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any, Final, cast

from .approval import ACTION_RE, Decision, Summary, Verdict, release_action
from .notice import Notice

#: Where root's own environment carries the hook. The wrapper decrypts
#: `infra/secrets.enc.env` and execs the executor, exactly as it does for
#: the GitHub token.
URL_ENV: Final = "RELEASE_APPROVAL_URL"
TOKEN_ENV: Final = "RELEASE_APPROVAL_TOKEN"

#: Contract 04 §8.4 rule 1 and `gatekeeper.NOTIFY_KIND`.
PUSH_KIND: Final = "approval"

#: What root asks for when it wants the verdict, and what it sends when it
#: has one to report. One endpoint, three kinds, so the flow needs one
#: http-in node and root needs no URL that carries an id.
POLL_KIND: Final = "approval-poll"
OUTCOME_KIND: Final = "release-outcome"

#: Contract 04 §8.4: the action string is `AGENT_APPROVE_<family>_<gate>`,
#: split on the underscore, so NEITHER field may contain one. A release is
#: not a family. The middle field is the release's own id (`action_id_of`):
#: the literal `release` fails `ACTION_RE` on length alone, so no tap could
#: ever arrive. This word rides beside it as the label a human reads.
RELEASE_LABEL: Final = "release"

#: `gatekeeper.NOTIFY_TIMEOUT_S`. One request, not the whole wait.
REQUEST_TIMEOUT_S: Final = 10.0

#: How often root asks Node-RED whether the tap has landed. A tap is a
#: human action, so seconds of lag cost nothing and a tighter poll only
#: costs the flow requests.
POLL_S: Final = 5.0

#: An answer root will read into memory. A verdict is four short fields.
MAX_REPLY_BYTES: Final = 64 * 1024

#: Contract 04 §8.3 caps the whole summary. §2.5's seven fields ride
#: beside it as `fields`, so the reader gets both the line and the table.
SUMMARY_MAX_CHARS: Final = 200

HTTP_REDIRECT: Final = 300

APPROVE: Final = "approve"
DENY: Final = "deny"
PENDING: Final = "pending"

#: What `_post` answers when the hook could not be reached at all.
UNREACHABLE: Final = "the approval push could not be delivered"


PostFn = Callable[[dict[str, object]], dict[str, object] | None]


def summary_line(summary: Summary) -> str:
    """§2.5's seven fields as the one line a notification shows.

    `review` first, because §2.5 puts the adversarial verdict first, then
    what moves and what restarts. Capped at contract 04 §8.3's 200.
    """
    joined = " | ".join((summary.review, summary.components, summary.contracts, summary.restore))

    return joined[:SUMMARY_MAX_CHARS]


def _post(url: str, token: str, body: dict[str, object]) -> dict[str, object] | None:
    """One request. None means the hook could not be reached or would not
    answer JSON. The bearer rides a header and reaches no log line."""
    payload = json.dumps(body).encode("utf-8")
    # The URL is root's own configuration, never a byte a requester wrote.
    request = urllib.request.Request(
        url,
        data=payload,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
    )
    # An empty ProxyHandler: root's environment must not be able to send
    # an approval push through a proxy a compose secret happened to name.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=REQUEST_TIMEOUT_S) as reply:
            if reply.status >= HTTP_REDIRECT:
                return None

            raw = reply.read(MAX_REPLY_BYTES + 1)
    except (urllib.error.URLError, OSError, ValueError):
        return None

    if len(raw) > MAX_REPLY_BYTES:
        return None

    return _parse(raw)


def _parse(raw: bytes) -> dict[str, object] | None:
    if not raw:
        return {}

    try:
        loaded: Any = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None

    if not isinstance(loaded, dict):
        return None

    return {str(key): value for key, value in cast("dict[object, object]", loaded).items()}


def _verdict_of(reply: dict[str, object], gate: str) -> Decision | None:
    """One poll's answer, believed only when it names THIS gate.

    `check_decision` refuses a decision for another gate anyway. Refusing
    it here as well means a flow that answers about the wrong gate keeps
    root waiting rather than ending the release on somebody else's tap.
    """
    if str(reply.get("gate", gate)) != gate:
        return None

    decision = reply.get("decision")
    if decision == PENDING or decision is None:
        return None

    at = reply.get("at")
    when = float(at) if isinstance(at, int | float) else None
    if decision == APPROVE:
        return Decision(Verdict.GRANTED, gate, when)

    return Decision(Verdict.DENIED, gate, when)


def make_transport(
    url: str,
    token: str,
    sleep: Callable[[float], None],
    clock: Callable[[], float],
    post: PostFn | None = None,
) -> Callable[[str, str, Summary, float], Decision]:
    """Step 5's `Transport`: push once, then ask until the tap lands.

    `post` is the seam a test replaces. Production leaves it None and gets
    `urllib`.
    """

    def send(body: dict[str, object]) -> dict[str, object] | None:
        if post is not None:
            return post(body)

        return _post(url, token, body)

    def transport(action_id: str, gate: str, summary: Summary, wait_s: float) -> Decision:
        # A tap the flow would drop is a fifteen minute silence, which reads
        # like a hung release. Refuse it here instead, which is contract 04
        # §8.4's undeliverable rule applied one step earlier.
        if ACTION_RE.fullmatch(release_action(action_id, gate)) is None:
            return Decision(Verdict.DENIED, gate, None)

        pushed = send(
            {
                "kind": PUSH_KIND,
                "family": action_id,
                "label": RELEASE_LABEL,
                "gate": gate,
                "summary": summary_line(summary),
                "fields": summary.as_dict(),
            }
        )
        if pushed is None:
            # Contract 04 §8.4: an undeliverable push denies at once.
            return Decision(Verdict.DENIED, gate, None)

        return _wait(send, gate, wait_s, sleep, clock)

    return transport


def _wait(
    send: PostFn,
    gate: str,
    wait_s: float,
    sleep: Callable[[float], None],
    clock: Callable[[], float],
) -> Decision:
    """Ask for the verdict until it arrives or the gate expires.

    An unreachable hook mid-wait does NOT deny: the push already went out,
    so the operator may be holding an actionable notification, and denying under
    them would cost a release for one dropped poll. It keeps asking until
    §2.5's fifteen minutes run out, and then answers `timeout`.
    """
    started = clock()
    while True:
        reply = send({"kind": POLL_KIND, "gate": gate})
        if reply is not None:
            decided = _verdict_of(reply, gate)
            if decided is not None:
                return decided

        if clock() - started >= wait_s:
            return Decision(Verdict.TIMEOUT, gate, None)

        sleep(POLL_S)


def make_notifier(
    url: str,
    token: str,
    post: PostFn | None = None,
) -> Callable[[Notice], bool]:
    """§2.6's outcome push: one message, NOT actionable."""

    def notify(notice: Notice) -> bool:
        # An outcome push is NOT actionable, so `ACTION_RE` never sees it
        # and the label may stay a word (§2.6's "one push, not actionable").
        body: dict[str, object] = {
            "kind": OUTCOME_KIND,
            "family": RELEASE_LABEL,
            "label": RELEASE_LABEL,
            "summary": notice.line(),
            "fields": notice.as_dict(),
        }
        if post is not None:
            return post(body) is not None

        return _post(url, token, body) is not None

    return notify


def build_phone(
    url: str,
    token: str,
    sleep: Callable[[float], None],
    clock: Callable[[], float],
) -> tuple[Callable[[str, str, Summary, float], Decision] | None, Callable[[Notice], bool] | None]:
    """The production pair, or (None, None) when root holds neither half.

    A host with no hook configured gets `deny_all` and `say_nothing` from
    the caller. That is the fail-closed end: an unconfigured approval path
    must stop a release, never wave one through (invariant 10).
    """
    if not url or not token:
        return None, None

    return make_transport(url, token, sleep, clock), make_notifier(url, token)
