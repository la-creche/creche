"""The perimeter: the access key, and CSRF on every state-changing POST.

The key is compared in constant time and never appears in a log line, in a
URL or in an error message. The CSRF token rides in a `SameSite=Strict;
HttpOnly` cookie and is checked against a hidden field AND against `Origin`
or `Referer`, because a token alone does not stop a same-site subdomain.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from enum import StrEnum
from typing import Final
from urllib.parse import urlsplit

#: Injected by the reverse proxy inside its `location /` block, so the
#: identity provider is the only working door (README §9).
ACCESS_HEADER: Final = "X-View-Key"

CSRF_COOKIE: Final = "view_csrf"
CSRF_FIELD: Final = "csrf_token"
CSRF_BYTES: Final = 32

#: Styling and the liveness probe hold nothing and are fetched by a proxy
#: outside the header-injecting block (README §9).
KEYLESS_PATHS: Final = ("/healthz", "/static/")


class Refusal(StrEnum):
    """Why a request was refused. The page says this word, never the value
    that failed to match."""

    NO_KEY = "no_key"
    BAD_KEY = "bad_key"
    NO_TOKEN = "no_token"
    BAD_TOKEN = "bad_token"
    FOREIGN_ORIGIN = "foreign_origin"


@dataclass(frozen=True)
class Origin:
    """The two headers that say where a form post came from."""

    origin: str | None
    referer: str | None
    host: str | None


def is_keyless(path: str) -> bool:
    """True for the paths a proxy fetches without the injected header."""
    return any(path == one.rstrip("/") or path.startswith(one) for one in KEYLESS_PATHS)


def check_key(offered: str | None, expected: str) -> Refusal | None:
    """None when the request may pass. Every comparison is constant time.

    An empty `expected` can only happen on a loopback bind: `config.py`
    refuses to start otherwise. A loopback service with no key configured
    still checks nothing, which is the developer case and nothing else.
    """
    if not expected:
        return None

    if offered is None or not offered:
        return Refusal.NO_KEY

    if not secrets.compare_digest(offered.encode("utf-8"), expected.encode("utf-8")):
        return Refusal.BAD_KEY

    return None


def mint_csrf() -> str:
    """A fresh token for a browser that arrives without one."""
    return secrets.token_urlsafe(CSRF_BYTES)


def check_csrf(cookie: str | None, field: str | None, sender: Origin) -> Refusal | None:
    """None when this POST may change state.

    Three things must hold: a cookie, a matching hidden field, and a sender
    this host recognises. The origin check runs even when the token matches,
    because a token that leaked is exactly the case it covers.
    """
    if cookie is None or not cookie:
        return Refusal.NO_TOKEN

    if field is None or not field:
        return Refusal.NO_TOKEN

    if not secrets.compare_digest(cookie.encode("utf-8"), field.encode("utf-8")):
        return Refusal.BAD_TOKEN

    return _check_origin(sender)


def _check_origin(sender: Origin) -> Refusal | None:
    """A foreign `Origin` or `Referer` is refused whatever the token says.

    A request with neither header is refused too. Every browser sends at
    least one on a form post to a different origin, so the absence of both
    is either a non-browser client or a stripped header, and both are
    reasons to say no on a state-changing call.
    """
    if sender.host is None or not sender.host:
        return Refusal.FOREIGN_ORIGIN

    stated = sender.origin or sender.referer

    if stated is None or not stated:
        return Refusal.FOREIGN_ORIGIN

    return None if _host_of(stated) == sender.host else Refusal.FOREIGN_ORIGIN


def _host_of(url: str) -> str:
    """The `host:port` of a URL, or the whole string when it has no scheme."""
    parsed = urlsplit(url)
    return parsed.netloc or url
