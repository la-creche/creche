"""The intake's HTTP surface (`stage7-releases.md` §4.3, steps 3, 4 and 6).

The operator's decision binds this: root owned, on the LAN
address, TLS, reachable by no other route. That makes it a new listener in
the most privileged process on the host, so the surface is as small as a
form can be:

```
  GET  /secret/<token>/   the form. Names the server and the secret.
  POST /secret            token, name, value. Answers `stored` or a code.
```

**There is no third route, and there is no read verb** (§4.3 rule 4).

Seven rules, each with the reason it is a rule and not a preference.

1. **Stdlib only.** A root listener is not the place for a third-party
   request parser. `http.server` plus `ssl` is the whole dependency set.
2. **The value rides the POST body** (§4.3 rule 1). The store route carries
   no id at all, so nothing about a secret can land in a URL, a proxy log
   or a browser history.
3. **No request line is logged.** The form's URL carries the token, and a
   token is a capability. `log_message` is overridden to a fixed line.
4. **The value reaches no log, no journal and no ledger.** The only place
   it goes is `SecretStore.fill`, which hands it to a sealer's stdin.
5. **Every answer is a fixed string** from a closed list. A refusal never
   quotes the request, and every refusal reads the same to a guesser.
6. **The LAN address, never `0.0.0.0`**: the host's services bind
   the host's LAN address. Binding the wildcard would put a
   root listener on every interface the host grows later.
7. **The page carries NO script**, and the policy says `script-src 'none'`.
   That decides where the token rides, so the reasoning is written out in
   `_page`.
"""

from __future__ import annotations

import socket
import ssl
import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Final
from urllib.parse import parse_qs

from .store import MAX_SECRET_CHARS, SECRET_NAME_RE, SecretStore
from .token import Tokens

#: A port no other service on the host binds.
DEFAULT_PORT: Final = 8380

FORM_PATH: Final = "/secret/"
STORE_PATH: Final = "/secret"

#: What a scriptless form sends. Nothing else is read: one encoding, one
#: parser, one meaning.
FORM_CONTENT_TYPE: Final = "application/x-www-form-urlencoded"

#: The closed field set of one paste. A body with a field more or a field
#: less is not this request.
FORM_FIELDS: Final = frozenset({"token", "name", "value"})

#: Percent encoding costs at most three bytes per character, and the token
#: and the name ride the same body. A value is still capped at
#: `MAX_SECRET_CHARS` after it is decoded, which is the cap that matters.
MAX_BODY_BYTES: Final = (MAX_SECRET_CHARS * 3) + 1024

#: §4.3 step 6's answer, and the only success string this service has.
STORED: Final = "stored"

#: Every refusal, in one closed list. None of them quotes the request.
REFUSALS: Final = {
    400: "the request did not carry one token, one name and one value",
    403: "that token does not fill that gap",
    404: "no such gap",
    413: "the value is larger than this accepts",
    500: "the value could not be sealed",
}

HTTP_OK: Final = 200

#: How many outcome lines this service keeps. The
#: journal is the durable record. This list is what the view reads, and a
#: LAN peer with no token can append to it.
MAX_LOG_LINES: Final = 256

#: The last of three layers against a hostile server name.
#:
#: A policy that allows inline script does not stop the one attack it
#: exists for: the server name is copied out of `mcp/<name>/server.yaml`,
#: which the attacker writes, and a `<script>` there runs on THIS origin
#: and reads the token out of the hidden field. Escaping stops it and
#: `mint`'s pattern stops it, and the policy stops it too.
#:
#: `script-src 'none'` is the layer. The form posts by itself, so no
#: script has to run, so none may. Every other source is `'none'` as well:
#: the page loads no style sheet, no font and no image, and a page that
#: needs nothing should be allowed nothing.
CONTENT_SECURITY_POLICY: Final = (
    "default-src 'none'; "
    "script-src 'none'; "
    "style-src 'none'; "
    "img-src 'none'; "
    "form-action 'self'; "
    "base-uri 'none'; "
    "frame-ancestors 'none'"
)

#: Every answer carries it, not only the form. A refusal is a short plain
#: page, and a policy that covers one route and not the others is a policy
#: a future route forgets.
POLICY_HEADERS: Final = (("Content-Security-Policy", CONTENT_SECURITY_POLICY),)

#: TLS 1.2 floor. A root listener has no reason to speak anything older,
#: and the one client is a browser the operator controls.
TLS_MINIMUM: Final = ssl.TLSVersion.TLSv1_2

#: How long ONE connection may take to hand root a complete request, from
#: the first byte of the handshake to the last byte of the body. Without
#: it a LAN peer that connects and says
#: nothing holds a thread for ever, and — before `accept_one` existed — the
#: whole accept loop with it. The operator pastes into a form on the same LAN, so
#: twenty seconds is generous for the one real client.
CONNECTION_TIMEOUT_S: Final = 20.0


@dataclass
class Reply:
    """One answer: a status and a body, and nothing built from input."""

    status: int
    body: bytes
    content_type: str = "text/plain; charset=utf-8"
    #: Extra headers, from a fixed list. Only the form page adds any.
    headers: tuple[tuple[str, str], ...] = ()


#: What the intake does once a value has landed: close the gap that asked
#: for it (§4.3 step 6, and §4.1 step 3 in reverse). It takes the secret's
#: NAME, which has passed `SECRET_NAME_RE`, and never a value. True means
#: a gap file went away, and no caller here acts on the answer: a gap root
#: could not close is one `caregiver` re-opens, which costs one push.
CloseFn = Callable[[str], bool]


def _close_nothing(name: str) -> bool:
    """The default. A test that drives `Intake` directly has no gap
    directory, and a fill that closes nothing is still a fill."""
    del name

    return False


@dataclass
class Intake:
    """The service's logic, with no socket. A test drives it directly."""

    tokens: Tokens
    store: SecretStore
    #: §4.3 step 6's other half. The gap file lives in an operator-written
    #: directory and `caregiver` re-opens a gap whose name still has no
    #: value, so the close has to happen AFTER the value lands or the
    #: reconciler simply writes it again.
    close_gap: CloseFn = _close_nothing
    #: What this service says about itself. It holds names and outcomes,
    #: never a value and never a token (rules 3 and 4).
    #:
    #: Bounded. `form()` appends on a MISS, and that
    #: route needs no token, so an unauthenticated LAN peer grew root's
    #: memory at its own request rate. The recent outcomes are all any
    #: reader wants.
    log: deque[str] = field(default_factory=lambda: deque[str](maxlen=MAX_LOG_LINES))

    def form(self, token: str) -> Reply:
        """The page the operator opens. Reading it does NOT spend the token."""
        gap = self.tokens.describe(token)
        if gap is None:
            self._say("form refused: no such gap")

            return _refuse(404)

        self._say(f"form shown for {gap.server}/{gap.name}")

        return Reply(
            HTTP_OK,
            _page(gap.server, gap.name, token),
            "text/html; charset=utf-8",
        )

    def store_value(self, body: bytes) -> Reply:
        """§4.3 steps 4 to 6: read the body, seal it, answer `stored`."""
        if len(body) > MAX_BODY_BYTES:
            self._say("store refused: body past the cap")

            return _refuse(413)

        parsed = _parse(body)
        if parsed is None:
            self._say("store refused: unreadable body")

            return _refuse(400)

        token, name, value = parsed
        if not value or len(value) > MAX_SECRET_CHARS:
            self._say(f"store refused: no value for {name}")

            return _refuse(400 if not value else 413)

        gap = self.tokens.claim(token, name)
        if gap is None:
            self._say(f"store refused: no gap for {name}")

            return _refuse(403)

        if not self.store.fill(name, value):
            # The token was reserved, not spent, so a failure that is not
            # the operator's fault does not cost the operator the paste.
            self.tokens.release(token)
            self._say(f"store failed for {gap.server}/{name}")

            return _refuse(500)

        self.tokens.spend(token)
        # The value landed, so the gap is answered. The close runs after
        # the token dies: a close that raised before `spend` would leave a
        # live token for a name that now has a value, which `fill` would
        # refuse for ever and the operator would read as "the paste did nothing".
        self._close_quietly(name)
        self._say(f"stored {gap.server}/{name}")

        return Reply(HTTP_OK, STORED.encode("utf-8"))

    def _close_quietly(self, name: str) -> None:
        """A gap root cannot close is a gap `caregiver` re-opens, which
        costs one more push. It is never a reason to un-store a value."""
        try:
            self.close_gap(name)
        except OSError:
            self._say(f"the gap file for {name} is still open")

    def _say(self, line: str) -> None:
        """One log line. Every caller passes a fixed string plus a name
        that has already passed `SECRET_NAME_RE`, so no value and no token
        can reach it."""
        self.log.append(line)


def _refuse(status: int) -> Reply:
    return Reply(status, REFUSALS[status].encode("utf-8"))


def _is_form(content_type: str) -> bool:
    """Whether the body is the one encoding this service reads.

    A browser appends nothing to this type for a form post, but a client
    may add a charset parameter, so the media type is compared and the
    parameters are ignored.
    """
    return content_type.split(";", 1)[0].strip().lower() == FORM_CONTENT_TYPE


def _parse(body: bytes) -> tuple[str, str, str] | None:
    """`token`, `name` and `value` out of one form post, or None.

    The encoding is what a form with no script sends, and it is the only
    one read: the page has no script to build JSON. A closed field set, one
    value each — a repeated field is not this request, and taking the first
    or the last would be a second reading of one body.
    """
    try:
        fields = parse_qs(
            body.decode("utf-8"),
            keep_blank_values=True,
            strict_parsing=True,
            max_num_fields=len(FORM_FIELDS),
        )
    except (UnicodeDecodeError, ValueError):
        return None

    if frozenset(fields) != FORM_FIELDS or any(len(one) != 1 for one in fields.values()):
        return None

    token, name, value = fields["token"][0], fields["name"][0], fields["value"][0]
    if SECRET_NAME_RE.fullmatch(name) is None:
        return None

    return token, name, value


def _page(server: str, name: str, token: str) -> bytes:
    """The form: the name being filled, one field, one button, no script.

    **Why the token rides the PATH and not the fragment.** A fragment never
    reaches a server log or a proxy log, which is the better property, and
    the token rides the path anyway. The reason is that a fragment is only
    readable by script — `location.hash` is the only way to get at it —
    and the page that reads it would have to run inline code, so the
    policy would have to say `script-src 'unsafe-inline'` again. That
    trade is the wrong way round. The attack is an injected
    `<script>` reading this token out of the page, and it is stopped by a
    policy that allows no script at all. The path's own exposure is
    narrower than it looks: `log_message` is silenced, so root's journal
    never holds the request line, TLS covers the wire, §4.3 step 3 says
    the service is reachable by no other route so there is no proxy in
    front, and `Referrer-Policy: no-referrer` keeps the URL out of the
    form post that follows. What is left is the browser's own history on a
    phone the operator holds, for a capability that dies in fifteen minutes and
    can only ever fill one gap.

    Scriptless also means the form posts by itself, in the encoding a form
    sends. `_parse` reads that one encoding and no other.

    Every value is escaped on the way in. The pattern check in
    `Tokens.mint` is the control and this is the backstop.
    """
    server = escape(server, quote=True)
    name = escape(name, quote=True)
    token = escape(token, quote=True)
    html = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="no-referrer">
<title>Secret for {server}</title></head>
<body>
<h1>{server}</h1>
<p>Paste the value for <code>{name}</code>. It is stored once and never read back.</p>
<p>One line only. A multi-line value goes through the sops path instead.</p>
<form method="post" action="{STORE_PATH}" enctype="{FORM_CONTENT_TYPE}">
  <input type="hidden" name="token" value="{token}">
  <input type="hidden" name="name" value="{name}">
  <input type="password" name="value" autocomplete="off" autofocus size="60">
  <button type="submit">Store</button>
</form>
</body></html>
"""

    return html.encode("utf-8")


def accept_one(listener: socket.socket, context: ssl.SSLContext) -> tuple[socket.socket, Any]:
    """Take one connection and put it under a clock. No handshake here.

    Moving the handshake off the LISTENING socket is half the fix, and
    moving it out of the accept loop is the other half.
    `socketserver` calls `get_request()` in the ACCEPT LOOP thread, before
    `process_request` spawns anything, so a handshake in this function
    would still stop every other connection — for `CONNECTION_TIMEOUT_S`
    rather than for ever. One LAN host that connects, sends one byte and
    waits is enough, and nothing says why: `log_message` is silenced and
    `socketserver` swallows the error.

    So this function does the two things that must happen in the accept
    loop and nothing else. `IntakeServer.process_request_thread` shakes
    hands, in the thread that will serve the connection.

    `context` is unused and is kept because it is this function's whole
    subject: a reader looking for where the handshake went must find the
    answer here.
    """
    del context
    sock, address = listener.accept()
    sock.settimeout(CONNECTION_TIMEOUT_S)

    return sock, address


#: `ThreadingHTTPServer` caps neither the thread count nor the connection
#: count, so a LAN peer's ceiling was its own connection rate times
#: `CONNECTION_TIMEOUT_S`, in the ROOT process.
#:
#: The number is what one human needs. The operator opens one page and posts one
#: form, over one HTTP/1.1 connection that is reused. The phone may open a
#: second while the first is idle, and a stale one dies at the timeout. A
#: refusal is a closed connection and costs the peer a reconnect, which is
#: the smallest bound that cannot lock the operator out.
MAX_LIVE_CONNECTIONS: Final = 8


class IntakeServer(ThreadingHTTPServer):
    """`ThreadingHTTPServer` that shakes hands in the worker thread."""

    #: A wedged thread must never hold a shutdown open.
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        handler: type[BaseHTTPRequestHandler],
        context: ssl.SSLContext,
    ) -> None:
        self._context = context
        self._live = 0
        self._counter = threading.Lock()
        super().__init__(address, handler)

    def get_request(self) -> tuple[socket.socket, Any]:
        return accept_one(self.socket, self._context)

    def handle_error(self, request: Any, client_address: Any) -> None:
        """One fixed line, never a traceback (rule 5, applied to stderr).

        `socketserver` prints the exception and the peer's address to
        stderr, which is root's journal. A LAN peer chooses when that
        happens and a traceback says which line of root's code it reached,
        so the default is both noise and a reply to a prober.
        """
        del request, client_address

    def process_request(self, request: Any, client_address: Any) -> None:
        """Refuse past the cap, in the accept loop, before a thread exists.

        Here and not in the worker, because the cost being capped IS the
        thread: counting after it was spawned would bound nothing.
        """
        if not self._take_slot():
            self.shutdown_request(request)

            return

        super().process_request(request, client_address)

    def _take_slot(self) -> bool:
        with self._counter:
            if self._live >= MAX_LIVE_CONNECTIONS:
                return False

            self._live += 1

            return True

    def _free_slot(self) -> None:
        with self._counter:
            self._live = max(0, self._live - 1)

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        """The handshake, off the accept loop.

        A failure closes this one connection and costs no other, which is
        what a plain HTTP probe of this port should cost. `wrap_socket`
        detaches the raw socket before it raises, so the descriptor is
        already closed and `shutdown_request` only tidies the husk.
        """
        try:
            self._handshake_and_serve(request, client_address)
        finally:
            # Every exit frees the slot. A slot that leaked on a failed
            # handshake would let a peer that only sends garbage close the
            # port for good, which is worse than the unbounded threads the
            # cap replaced.
            self._free_slot()

    def _handshake_and_serve(self, request: Any, client_address: Any) -> None:
        try:
            secure = self._context.wrap_socket(request, server_side=True)
        except OSError:
            self.shutdown_request(request)

            return

        super().process_request_thread(secure, client_address)


def tls_context(certfile: str, keyfile: str) -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = TLS_MINIMUM
    context.load_cert_chain(certfile, keyfile)

    return context


def serve(
    intake: Intake,
    certfile: str,
    keyfile: str,
    bind: str,
    port: int = DEFAULT_PORT,
) -> ThreadingHTTPServer:
    """The listener. TLS, the LAN address, and the two routes above.

    `bind` is the site's LAN address (`run.build_wiring`), never loopback
    for anything another host must reach, never `0.0.0.0`."""
    return IntakeServer((bind, port), _handler_for(intake), tls_context(certfile, keyfile))


def _handler_for(intake: Intake) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        #: A browser the operator opens by hand. HTTP/1.1 so the form post reuses
        #: the connection the page arrived on, rather than paying a second
        #: TLS handshake.
        protocol_version = "HTTP/1.1"

        #: The handler's half of `CONNECTION_TIMEOUT_S`.
        #: `BaseHTTPRequestHandler` inherits
        #: `timeout = None`, so a request line or a body that never ends
        #: holds one thread of the ROOT process for ever, and a LAN peer
        #: can open as many as it likes. `handle_one_request` turns the
        #: expiry into a closed connection.
        timeout = CONNECTION_TIMEOUT_S

        def do_GET(self) -> None:
            if not self.path.startswith(FORM_PATH):
                self._answer(_refuse(404))

                return

            token = self.path[len(FORM_PATH) :].strip("/")
            self._answer(intake.form(token))

        def do_POST(self) -> None:
            if self.path != STORE_PATH:
                self._answer(_refuse(404))

                return

            if not _is_form(self.headers.get("Content-Type", "")):
                # One encoding is read, so a body in any other is refused
                # before it is read rather than guessed at.
                self.close_connection = True
                self._answer(_refuse(400))

                return

            body = self._body()
            if body is None:
                # The body was never read, so the rest
                # of it is still on the socket and would be parsed as the
                # next request line. The connection ends here instead.
                self.close_connection = True
                self._answer(_refuse(413))

                return

            self._answer(intake.store_value(body))

        def log_message(self, format: str, *args: object) -> None:
            """Rule 3: the request line carries the token, so it is never
            written. The intake's own log says what happened instead."""
            del format, args

        def _body(self) -> bytes | None:
            """The body, or None when this connection will not get one.

            A body root refuses is a body root does not read.
            """
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                return None

            if length < 0 or length > MAX_BODY_BYTES:
                return None

            return self.rfile.read(length)

        def _answer(self, reply: Reply) -> None:
            self.send_response(reply.status)
            self.send_header("Content-Type", reply.content_type)
            self.send_header("Content-Length", str(len(reply.body)))
            # A page that holds a token must not be cached anywhere.
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Content-Type-Options", "nosniff")
            # Every answer, not only the form (`POLICY_HEADERS`).
            for header, value in (*POLICY_HEADERS, *reply.headers):
                self.send_header(header, value)

            self.end_headers()
            self.wfile.write(reply.body)

    return Handler
