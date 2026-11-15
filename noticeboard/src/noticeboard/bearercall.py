"""One call to a peer under a bearer that a file holds.

The approval page (`approvals.py`) and the release page (`releases.py`)
each call one peer under one bearer. This module is the call they share.

Three rules this module keeps.

1. **The bearer is read from a file at each call**, never from the
   environment, and it appears in one place: an `Authorization` header.
   No page, log line or URL holds it (invariant 13).
2. **A missing channel calls nothing.** With no client or no token file,
   the call answers a problem.
3. **No text of a refused request reaches a page.** Such a text can quote
   the header that holds the bearer.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Final

import httpx

#: The peer is on the same host. A call that takes longer is a peer that
#: does not answer.
TIMEOUT: Final = httpx.Timeout(5.0)


@dataclass(frozen=True)
class Peer:
    """The words of the problems of one page.

    `name` is the peer, for example "the approval plugin". `page` is the
    word of the page and of its token file, for example "approval".
    """

    name: str
    page: str

    @property
    def not_set_up(self) -> str:
        return f"the {self.page} page is not set up on this host"

    @property
    def no_token(self) -> str:
        return f"the {self.page} token file cannot be read"

    @property
    def not_sent(self) -> str:
        # It names no part of the request, because a header holds the bearer.
        return f"cannot call {self.name}: the client refused the request"

    def unreachable(self, error: httpx.HTTPError) -> str:
        return f"cannot reach {self.name}: {type(error).__name__}"


def read_token(path: Path) -> str:
    """The bearer, or empty for a file that cannot be read."""
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError, ValueError:
        # A decode error quotes bytes of the file, so it reaches no page.
        return ""


def call(
    peer: Peer,
    client: httpx.Client | None,
    token_file: Path | None,
    request: tuple[str, str, object],
) -> tuple[httpx.Response | None, str]:
    """Send `request`, a method, a URL and a JSON body or None.

    The answer is the reply, or None and the problem of the page.
    """
    if client is None or token_file is None:
        return None, peer.not_set_up

    bearer = read_token(token_file)

    if not bearer:
        return None, peer.no_token

    method, url, body = request

    try:
        reply = client.request(
            method,
            url,
            json=body,
            # The bearer appears here and in no URL, no log line and no
            # page (invariant 13).
            headers={"Authorization": f"Bearer {bearer}"},
            timeout=TIMEOUT,
        )
    except httpx.LocalProtocolError, httpx.InvalidURL, UnicodeEncodeError:
        # The text of such an error can quote the header that holds the
        # bearer. The page gets a fixed sentence.
        return None, peer.not_sent
    except httpx.HTTPError as error:
        return None, peer.unreachable(error)

    return reply, ""
