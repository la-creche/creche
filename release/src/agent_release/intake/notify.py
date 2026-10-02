"""The one push that carries a token (`stage7-releases.md` §4.3 step 2).

> One actionable phone push carries the server name, the secret name and
> the link. The link carries the token. The token is a capability, not a
> value.

**And by no other route.** `Tokens.mint` answers its caller so that this
module can build the link, and nothing else in the intake ever answers
with a token: no HTTP route, no journal line, no ledger entry. A
operator-side process that could both ask for a token and read it would fill
a gap by itself, which is the one thing the phone step exists to prevent.

Four rules, each with its reason.

1. **Stdlib only.** `urllib.request`, like `executor/phone.py`. This
   module does not import that one: its body has a different shape, its
   `_post` is private to the approval path, and the gate keeps the
   intake's plaintext-adjacent code out of the executor's import graph.
   The duplication is two dozen lines and is deliberate.
2. **The bearer rides one header, never a URL** (invariant 13). The link
   carries the token and the link rides the BODY, over TLS to a hook that
   needs the bearer, so a token never reaches a query string root writes.
3. **Nothing about a push is logged.** A journal line that said which URL
   went out would be the token in root's journal, which §4.3 forbids in as
   many words. The caller gets True or False and says only that.
4. **Fail closed, and say so.** A push that did not land means the operator is
   holding no notification, so the token it carried is dead weight: it
   fills a slot and keeps the listener bound for nothing. `run.py` drops
   it.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Final

from .gaps import OpenGap

#: Where root's own environment carries the hook. The same two names the
#: executor reads (`executor/phone.py`), because it is the same hook: one
#: Node-RED flow, one bearer, one path to the operator's phone. A second one
#: would be a second approval system.
URL_ENV: Final = "RELEASE_APPROVAL_URL"
TOKEN_ENV: Final = "RELEASE_APPROVAL_TOKEN"

#: What the Node-RED flow routes on. It is NOT `approval`: an approval
#: push carries a gate the flow polls a verdict for, and this one carries
#: a link the operator opens. The flow maps `fields.link` to the notification's
#: tap action.
PUSH_KIND: Final = "secret-gap"

#: The `family` and `label` fields the flow's envelope expects. A secret
#: gap belongs to no family, and saying so beats inventing one.
PUSH_LABEL: Final = "secret"

#: `gatekeeper.NOTIFY_TIMEOUT_S`, and `executor/phone.py`'s.
REQUEST_TIMEOUT_S: Final = 10.0

#: An answer root will read into memory. An acknowledgement is short.
MAX_REPLY_BYTES: Final = 64 * 1024

HTTP_REDIRECT: Final = 300

#: One push, or nothing. True means the hook took it.
PushFn = Callable[[OpenGap, str], bool]


def link_for(base: str, token: str) -> str:
    """The URL the operator taps.

    The token is a path segment, and `_page` says why the path and not the
    fragment. `base` is root's own configuration — a scheme, a host and a
    port this process chose — so no byte a requester wrote reaches it.
    """
    return f"{base.rstrip('/')}/secret/{token}/"


def summary_of(gap: OpenGap) -> str:
    """The one line the notification shows. Two names, both of which have
    passed their patterns, and no value because this process holds none."""
    return f"{gap.server} needs the secret {gap.secret}"


def make_push(url: str, token: str, base: str, post: object = None) -> PushFn:
    """§4.3 step 2's push. `post` is the seam a test replaces."""
    sender = post if callable(post) else None

    def push(gap: OpenGap, minted: str) -> bool:
        body: dict[str, object] = {
            "kind": PUSH_KIND,
            "family": PUSH_LABEL,
            "label": PUSH_LABEL,
            "summary": summary_of(gap),
            "fields": {
                "server": gap.server,
                "secret": gap.secret,
                "link": link_for(base, minted),
            },
        }
        if sender is not None:
            return bool(sender(body))

        return _post(url, token, body)

    return push


def say_nothing(gap: OpenGap, minted: str) -> bool:
    """A host with no hook configured. It answers False, so `run.py` drops
    the token rather than leaving a capability nobody was told about."""
    del gap, minted

    return False


def _post(url: str, token: str, body: dict[str, object]) -> bool:
    """One request. False means the hook could not be reached.

    Nothing of the request and nothing of the answer is returned or
    logged: the body holds the link, and the link holds the token.
    """
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
    )
    # An empty ProxyHandler, for the same reason `executor/phone.py` has
    # one: root's environment must not be able to send a push carrying a
    # token through a proxy some other secret happened to name.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=REQUEST_TIMEOUT_S) as reply:
            if reply.status >= HTTP_REDIRECT:
                return False

            reply.read(MAX_REPLY_BYTES)
    except (urllib.error.URLError, OSError, ValueError):
        return False

    return True
