"""The one place the noticeboard speaks HTTP to `attendance` (contract 02 §3).

`sessions.py` holds the meaning and stays free of `httpx`, so its tests
need no socket. This module holds the wire and nothing else: a Unix
socket by default, TCP when an operator points the noticeboard at an `attendance`
with `bind_lan: true`, and the `Authorization` header.

Every failure here becomes a `Reply` with a `problem` string. A page that
cannot reach `attendance` says so and still renders the rest (invariant 19).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import httpx

from .sessions import Reply

#: A page must answer. `attendance` is on the same host, so a read that
#: takes longer than this is an `attendance` that is not answering.
CONNECT_TIMEOUT_S: Final = 2.0
READ_TIMEOUT_S: Final = 10.0

#: A replayed stream is longer than a list call, and a page waits for it.
STREAM_TIMEOUT_S: Final = 30.0

#: Contract 02 §5.5 answers NDJSON; every other read answers JSON.
_STREAM_PATH: Final = "/events"

#: What a page says when the client refuses a request. It names no part of
#: the request.
_NOT_SENT: Final = "cannot call attendance: the client refused the request"


@dataclass
class HttpTransport:
    """`sessions.Transport` over a socket or over TCP."""

    client: httpx.Client

    def get(self, path: str, params: Mapping[str, str], bearer: str) -> Reply:
        try:
            response = self.client.get(
                path,
                params=dict(params),
                # Contract 02 §3 rule 4. The token appears here and in no
                # URL, no log line and no page (invariant 13).
                headers={"Authorization": f"Bearer {bearer}"},
                timeout=_timeout(path),
            )
        except (httpx.LocalProtocolError, httpx.InvalidURL, UnicodeEncodeError):
            # The client itself refused a header or a path that HTTP cannot
            # carry. The text of such an error quotes what it refused, and a
            # header holds the token (invariant 13). The page gets a fixed
            # sentence.
            return Reply(problem=_NOT_SENT)
        except httpx.HTTPError as error:
            # Every other error is about the connection or the answer. Its
            # text carries no request header, so the token cannot reach a
            # page through this line.
            return Reply(problem=f"cannot reach attendance: {type(error).__name__}: {error}")

        return Reply(status=response.status_code, body=response.content)


def build_client(socket: Path | None, url: str) -> httpx.Client:
    """Contract 02 §3 rules 1 and 2: a Unix socket, or a LAN address.

    The socket wins when there is one. `url` still supplies the scheme,
    the path prefix and the Host header, which a socket client has no
    other way to learn. The name in it is never resolved.
    """
    transport = httpx.HTTPTransport(uds=str(socket)) if socket is not None else None

    return httpx.Client(base_url=url, transport=transport)


def _timeout(path: str) -> httpx.Timeout:
    read = STREAM_TIMEOUT_S if path.endswith(_STREAM_PATH) else READ_TIMEOUT_S

    return httpx.Timeout(CONNECT_TIMEOUT_S, read=read)
