"""The edge of each listener: a request that no handler takes, and a signal.

Five services listen: `attendance`, the Open WebUI door, the chaperone, the
noticeboard and the webhook listener of the trigger door. Each one answers
some requests before a handler of the service runs: an unknown path, a wrong
method, a path with a final slash, `HEAD`. Each one ends at some signals with
no code of the service. No suite that hosts a service in the test process can
see either.

Each scenario reads a process boundary: an HTTP status, the bytes of an
answer, the session store, the record of a stand-in, a journal, a socket
file, the end of a process.

Each request carries the credential of its listener. The scenarios hold what
a listener answers to a caller that it knows.

No contract gives the result of most scenarios here. Each such scenario has a
`CONTRACT-QUESTION:` comment. The comment gives the reading taken, what the
services do today, and what a change costs.

CONTRACT-QUESTION: no contract names the exit code of a listener after a
signal. The services differ today. After `SIGTERM`, `attendance` exits with
code 0, and the signal ends each other listener, with no exit code. After
`SIGINT`, each listener exits with code 0. `SIGHUP` ends each listener that
has no reload, with no exit code. Reading taken: no scenario here reads an
exit code. A change to one fixed code costs one assertion in each of the
three signal scenarios.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
from proc_board import BoardStack
from proc_chat import (
    CHAT_PATH,
    MODELS_PATH,
    SESSIONS_PATH,
    TURN_SETTLED,
    await_settled,
    chat_body,
    chat_id,
    first_chunk,
    message_id,
    owui_headers,
    session_of,
    until,
)
from proc_delegate import TARGET, DelegateStack
from proc_harness import (
    Address,
    Child,
    TcpAddress,
    UnixAddress,
    answers_http,
    is_listening,
)
from proc_owui import OwuiStack
from proc_services import SERVICES, Service
from proc_stack import Stack
from proc_standins import set_pi_env
from proc_tree import FAMILY
from proc_trigger import TriggerStack, hook_path

#: The five services that listen. The other two rows of the service table,
#: the terminal door and `caregiver`, do not listen.
LISTENERS = (
    Service.ATTENDANCE,
    Service.DOOR_OWUI,
    Service.CHAPERONE,
    Service.NOTICEBOARD,
    Service.DOOR_TRIGGER,
)

#: A path that is a route of no listener.
NO_ROUTE = "/no-such-route"

CALL_PATH = "/call"
HEALTH_PATH = "/healthz"

#: For each listener, one route of it and a method that the route does not
#: take. No method here carries a body.
WRONG_METHOD = {
    Service.ATTENDANCE: ("PUT", SESSIONS_PATH),
    Service.DOOR_OWUI: ("GET", CHAT_PATH),
    Service.CHAPERONE: ("GET", CALL_PATH),
    Service.NOTICEBOARD: ("DELETE", HEALTH_PATH),
    Service.DOOR_TRIGGER: ("GET", hook_path()),
}

#: One route that takes `GET`, for each listener that has one. The webhook
#: listener has none: its one route takes `POST`.
GET_ROUTE = {
    Service.ATTENDANCE: SESSIONS_PATH,
    Service.DOOR_OWUI: MODELS_PATH,
    Service.CHAPERONE: HEALTH_PATH,
    Service.NOTICEBOARD: HEALTH_PATH,
}

#: The listeners whose request creates a session or starts a turn. The
#: noticeboard does neither. Its save route writes a commit, and no scenario
#: sends that route a final slash.
DO_WORK = (Service.ATTENDANCE, Service.DOOR_OWUI, Service.CHAPERONE, Service.DOOR_TRIGGER)

#: The listeners that read a JSON body, with no scenario for such a body in
#: another file. `test_proc_trigger_webhooks.py` holds the webhook listener.
READ_JSON = (Service.ATTENDANCE, Service.DOOR_OWUI, Service.CHAPERONE)

#: The two listeners that have no reload at `SIGHUP` in each configuration.
NO_RELOAD = (Service.DOOR_OWUI, Service.NOTICEBOARD)

FINAL_SLASH = "/"
JSON_TYPE = {"Content-Type": "application/json"}
CONTENT_TYPE = "content-type"

#: How deep the third body of `NOT_JSON` nests.
NESTING = 100_000

#: Three bodies that no listener takes as a request. The first is a text
#: that is not JSON. The second is three bytes that are not UTF-8. The third
#: is 100,000 arrays, one inside the next. RFC 8259 §9 lets a reader set a
#: limit on the nesting. A reader with a limit refuses the third body. A
#: reader with no limit finds an array, and each request here is an object.
NOT_JSON = {
    "a-text": b"not json at all",
    "not-utf-8": b"\xff\xfe\xfd",
    "deep-nesting": b"[" * NESTING + b"]" * NESTING,
}

#: What the caller of the chaperone sends (contract 04 §7.1).
INVOKE_AGENT = "invoke_agent"
SESSION_HEADER = "X-Session-Id"

#: The prompt of each turn that a scenario starts.
PROMPT = "is the heating on"

#: A turn long enough to act inside: 60 deltas, 30 ms apart.
LONG_TURN = {"events": 60, "delay_ms": 30}

#: The header that makes a service close the connection after one answer
#: (RFC 9112 §9.6). A test then reads the answer to the end of the connection.
CLOSE = {"Connection": "close"}

#: How each HTTP answer starts, and what ends its header block.
HTTP_ANSWER = b"HTTP/"
HEAD_END = b"\r\n\r\n"
LINE_END = b"\r\n"

#: How long one answer may take on a loaded machine.
ANSWER_DEADLINE_S = 30.0

#: How many bytes of an answer one read takes.
READ_BYTES = 65536

#: How long the system has to reap a killed process.
EXIT_DEADLINE_S = 30.0

#: systemd.service(5): the stop limit of a unit that names no
#: `TimeoutStopSec`, unless the manager of the host sets another default.
SYSTEMD_STOP_LIMIT_S = 90.0

#: `TimeoutStopSec` of the unit of each listener, in seconds. The unit of the
#: chaperone names none. `test_each_stop_limit_is_that_of_its_unit` holds
#: this table against the unit files.
STOP_LIMIT_S = {
    Service.ATTENDANCE: 60.0,
    Service.DOOR_OWUI: 30.0,
    Service.CHAPERONE: SYSTEMD_STOP_LIMIT_S,
    Service.NOTICEBOARD: 20.0,
    Service.DOOR_TRIGGER: 30.0,
}

#: What the `SIGHUP` scenario says in a run that cannot judge it.
RUN_IGNORES_SIGHUP = (
    "this run of the suite ignores SIGHUP, and each service takes that from it. "
    "Start the suite with no `nohup`."
)

#: `integration/proc/test_proc_edges.py` is 3 deep in the checkout.
UNIT_DIR = Path(__file__).resolve().parents[2] / "systemd"
STOP_LIMIT_KEY = "TimeoutStopSec="


@dataclass(frozen=True, slots=True)
class Listener:
    """One service that listens, as a scenario reaches it."""

    service: Service
    stack: Stack
    child: Child
    address: Address
    #: A client that carries the credential of this listener.
    client: Callable[[], httpx.AsyncClient]


@dataclass(frozen=True, slots=True)
class Work:
    """One request that makes a listener create a session or start a turn."""

    path: str
    headers: dict[str, str]
    #: The JSON body. The webhook listener takes a request with none.
    body: dict[str, object] | None


@pytest.fixture
def listener(request: pytest.FixtureRequest) -> Listener:
    """One listener, in its topology, serving.

    A scenario names the listener with `indirect=True`. Only the topology of
    that listener starts, through its fixture in `conftest.py`.
    """
    return _LISTENER_OF[Service(request.param)](request)


def _attendance(request: pytest.FixtureRequest) -> Listener:
    stack: OwuiStack = request.getfixturevalue("owui")
    address = UnixAddress(stack.tree.attendance_socket)

    return Listener(
        Service.ATTENDANCE, stack, _running(stack.attendance), address, stack.attendance_client
    )


def _door_owui(request: pytest.FixtureRequest) -> Listener:
    stack: OwuiStack = request.getfixturevalue("owui")
    address = TcpAddress(stack.door_port)

    return Listener(Service.DOOR_OWUI, stack, _running(stack.door), address, stack.door_client)


def _chaperone(request: pytest.FixtureRequest) -> Listener:
    stack: DelegateStack = request.getfixturevalue("delegate")
    address = TcpAddress(stack.chaperone_port)

    return Listener(
        Service.CHAPERONE, stack, _running(stack.chaperone), address, stack.sandbox_client
    )


def _noticeboard(request: pytest.FixtureRequest) -> Listener:
    stack: BoardStack = request.getfixturevalue("board_alone")
    address = TcpAddress(stack.board_port)

    return Listener(Service.NOTICEBOARD, stack, _running(stack.board), address, stack.client)


def _door_trigger(request: pytest.FixtureRequest) -> Listener:
    stack: TriggerStack = request.getfixturevalue("trigger")
    address = TcpAddress(stack.listener_port)

    return Listener(
        Service.DOOR_TRIGGER, stack, _running(stack.listener), address, stack.automation
    )


_LISTENER_OF: dict[Service, Callable[[pytest.FixtureRequest], Listener]] = {
    Service.ATTENDANCE: _attendance,
    Service.DOOR_OWUI: _door_owui,
    Service.CHAPERONE: _chaperone,
    Service.NOTICEBOARD: _noticeboard,
    Service.DOOR_TRIGGER: _door_trigger,
}


# CONTRACT-QUESTION: no contract names the answer of a listener to a path
# that is no route of it. Contract 02 §14 gives `not_found` for a session
# that does not exist, and no code for a path. Reading taken: the status that
# RFC 9110 §15.5.5 gives, and no assertion on the body. Each service answers
# 404 today, with the body `{"detail":"Not Found"}` as `application/json`.
# A change to one body for each listener costs one assertion here.
@pytest.mark.parametrize("listener", LISTENERS, indirect=True)
async def test_an_unknown_path_gets_404(listener: Listener) -> None:
    """A path that is no route of the listener is not found."""
    async with listener.client() as client:
        answer = await client.get(NO_ROUTE)

    assert answer.status_code == httpx.codes.NOT_FOUND, answer.text


# CONTRACT-QUESTION: no contract names the answer of a listener to a route
# with a method that the route does not take. Reading taken: the status that
# RFC 9110 §15.5.6 gives, and no assertion on the body or on the `Allow`
# header. Each service answers 405 today, with the body
# `{"detail":"Method Not Allowed"}` as `application/json` and with an `Allow`
# header. A change to one body for each listener costs one assertion here.
@pytest.mark.parametrize("listener", LISTENERS, indirect=True)
async def test_a_wrong_method_gets_405(listener: Listener) -> None:
    """A route of the listener, with a method that the route does not take."""
    method, path = WRONG_METHOD[listener.service]

    async with listener.client() as client:
        answer = await client.request(method, path)

    assert answer.status_code == httpx.codes.METHOD_NOT_ALLOWED, answer.text


# CONTRACT-QUESTION: contract 02 §3 rule 3 says that a request body is JSON,
# and it names no header. Reading taken: `attendance` reads the body as JSON
# when the request has no `Content-Type` header, as it does today. A reading
# that demands the header costs this scenario, and each caller that sends
# none.
#
# The door also reads such a body today, and the chaperone answers 422 to
# it. No scenario holds either.
async def test_attendance_reads_a_body_with_no_content_type(
    owui: OwuiStack, attendance_api: httpx.AsyncClient
) -> None:
    """Contract 02 §3 rule 3 and §5.1. A create with no such header makes the session."""
    session = session_of(chat_id())
    body = json.dumps({"family": FAMILY, "session": session}).encode()
    request = attendance_api.build_request("POST", SESSIONS_PATH, content=body)
    assert CONTENT_TYPE not in request.headers, "this scenario sends no such header"

    answer = await attendance_api.send(request)

    assert answer.status_code == httpx.codes.CREATED, answer.text
    assert answer.json()["session"] == session
    assert owui.tree.sessions_of(FAMILY) == [session]


# CONTRACT-QUESTION: contract 02 §14 gives `attendance` the code
# `bad_request` with status 400 for a malformed field. No contract gives the
# door or the chaperone an answer to a body that is not JSON: contract 04 §5
# has no row for it. No contract gives a body a limit on its nesting.
# Reading taken: one assertion for the three listeners and for each body of
# `NOT_JSON`, a status of the 4xx class, and no work started. Today
# `attendance` answers 400 with `bad_request` to each body, and the door
# answers 400 with `bad_body` to each body. The chaperone answers 422 to the
# text and 400 to the other two bodies. A change to one status for each
# listener costs one assertion here.
@pytest.mark.parametrize("body", list(NOT_JSON.values()), ids=list(NOT_JSON))
@pytest.mark.parametrize("listener", READ_JSON, indirect=True)
async def test_a_body_that_is_not_json_is_refused(listener: Listener, body: bytes) -> None:
    """The listener checks the shape before a session exists and before a turn starts."""
    work = work_of(listener.service)

    async with listener.client() as client:
        answer = await client.post(work.path, headers=work.headers | JSON_TYPE, content=body)

    assert answer.is_client_error, f"{answer.status_code}\n{answer.text}"
    assert work_done(listener.stack) == []


# CONTRACT-QUESTION: no contract says what a listener does with a route that
# has one more `/` at its end. Reading taken: such a request creates no
# session and starts no turn, and the scenario asserts no status. Each
# service answers 307 today, with a `Location` header that names the path
# with no final slash, and with no body. The client of this scenario follows
# no redirect. A reading that serves the path as the route costs this
# scenario.
@pytest.mark.parametrize("listener", DO_WORK, indirect=True)
async def test_a_final_slash_starts_no_work(listener: Listener) -> None:
    """A route with a final slash creates no session and starts no turn.

    The same request to the route itself starts work. So the final slash is
    the one reason that the first request started none.
    """
    work = work_of(listener.service)

    async with listener.client() as client:
        assert not client.follow_redirects, "this scenario follows no redirect"
        await client.post(work.path + FINAL_SLASH, headers=work.headers, json=work.body)
        started_by_the_slash = work_done(listener.stack)
        accepted = await client.post(work.path, headers=work.headers, json=work.body)

    assert started_by_the_slash == []
    assert accepted.is_success, f"{accepted.status_code}\n{accepted.text}"
    assert work_done(listener.stack) != []


# CONTRACT-QUESTION: no contract says what a listener answers to `HEAD` on a
# route that takes `GET`. Reading taken: the answer has no body, which
# RFC 9110 §9.3.2 demands of each answer to `HEAD`, and the scenario asserts
# no status. Each service answers 405 today, with an `Allow` header, with the
# `Content-Length` of a 405 body, and with no body. A listener that answers
# as it does to `GET`, with status 200 and no body, passes this scenario
# too. A change to one fixed status costs one assertion here.
@pytest.mark.parametrize("listener", tuple(GET_ROUTE), indirect=True)
async def test_head_on_a_get_route_has_no_body(listener: Listener) -> None:
    """An answer to `HEAD` ends at its header block.

    An HTTP client drops the body of such an answer, so the scenario reads
    the bytes of the connection itself.
    """
    async with listener.client() as client:
        request = client.build_request("HEAD", GET_ROUTE[listener.service], headers=CLOSE)

    answer = await raw_answer(listener.address, wire_form(request))
    head, end, body = answer.partition(HEAD_END)

    assert head.startswith(HTTP_ANSWER), answer
    assert end == HEAD_END, answer
    assert body == b"", answer


def test_attendance_starts_on_the_socket_file_of_a_killed_process(
    owui_prepared: OwuiStack,
) -> None:
    """`Restart=always` of the unit, after a kill. Contract 02 §3 rule 1 gives the path.

    A killed process removes nothing. Its socket file stays at the path that
    the next process binds, and that process must start.
    """
    tree = owui_prepared.tree
    address = UnixAddress(tree.attendance_socket)
    killed = owui_prepared.spawn_attendance()
    owui_prepared.await_attendance()

    os.killpg(killed.pgid, signal.SIGKILL)
    killed.wait(EXIT_DEADLINE_S)

    assert tree.attendance_socket.is_socket(), "the kill left no socket file"
    assert not is_listening(address)

    started = owui_prepared.spawn_attendance()
    owui_prepared.await_attendance()

    assert started.exit_code() is None
    assert answers_http(address)


# CONTRACT-QUESTION: no contract says what the door does with an open stream
# at a stop. Reading taken: the process ends inside `TimeoutStopSec` of its
# unit, and the turn settles on the host, as invariant 4 says for a client
# that goes away. Today the door sends the stream to its end and then ends.
# A door that cuts the stream at the stop passes this scenario too. A reading
# that demands the whole stream costs one assertion here.
async def test_a_stop_with_an_open_stream_ends_the_door(
    owui: OwuiStack, door: httpx.AsyncClient
) -> None:
    """`TimeoutStopSec=30` of the unit. The stop ends the door, and the turn settles."""
    assert owui.door is not None
    stopped = owui.door
    set_pi_env(owui.tree, **LONG_TURN)
    chat = chat_id()
    session = session_of(chat)

    async with door.stream(
        "POST",
        CHAT_PATH,
        headers=owui_headers(chat, message_id()),
        json=chat_body(PROMPT, stream=True),
    ) as response:
        assert response.status_code == httpx.codes.OK
        await first_chunk(response.aiter_text())
        assert TURN_SETTLED not in owui.tree.journal_kinds(session), "the stream was not open"

        stopped.send(signal.SIGTERM)
        await until(
            lambda: stopped.exit_code() is not None,
            "the door to end with one stream open",
            STOP_LIMIT_S[Service.DOOR_OWUI],
        )

    # Rule 13: the journal says that the turn settled. The stream does not.
    await await_settled(owui.tree, session)


# CONTRACT-QUESTION: no contract says what a listener does at `SIGINT`. No
# unit sends it: `KillSignal=SIGTERM`. Reading taken: the signal ends the
# listener as `SIGTERM` does, inside `TimeoutStopSec` of its unit, and
# nothing answers at its address after that. Each service does so today, in
# less than one second. A reading that ignores the signal costs this
# scenario.
@pytest.mark.parametrize("listener", LISTENERS, indirect=True)
def test_sigint_ends_a_listener(listener: Listener) -> None:
    """The process ends inside the stop limit of its unit, and its address closes."""
    listener.child.send(signal.SIGINT)
    listener.child.wait(STOP_LIMIT_S[listener.service])

    assert not is_listening(listener.address)


# CONTRACT-QUESTION: contract 02 §3 rule 8 gives `SIGHUP` a reload in
# `attendance`. No contract says what a listener with no reload does at the
# signal. Reading taken: what the door and the noticeboard do today. The
# signal ends the process, and its port closes. The other reading is a
# listener that ignores the signal. A change to that reading costs the two
# assertions here.
#
# The chaperone has a reload only with a roster source. The chaperone of this
# suite has none, so `SIGHUP` ends it too. No scenario holds either case.
@pytest.mark.parametrize("listener", NO_RELOAD, indirect=True)
def test_sighup_ends_a_listener_with_no_reload(listener: Listener) -> None:
    """The process ends inside the stop limit of its unit, and its port closes.

    A child ignores each signal that the process which started it ignores,
    until the child installs a handler. A run of the suite under `nohup`
    ignores `SIGHUP`, so each service of that run ignores it too. Such a run
    cannot judge this scenario, and it fails here with the reason.
    """
    assert signal.getsignal(signal.SIGHUP) is not signal.SIG_IGN, RUN_IGNORES_SIGHUP

    listener.child.send(signal.SIGHUP)
    listener.child.wait(STOP_LIMIT_S[listener.service])

    assert not is_listening(listener.address)


def test_each_stop_limit_is_that_of_its_unit() -> None:
    """The table of this file, held against the unit files. No service starts."""
    for service, limit in STOP_LIMIT_S.items():
        unit = _unit_of(service)
        named = _stop_limit_of(unit)

        assert (SYSTEMD_STOP_LIMIT_S if named is None else named) == limit, unit


def work_of(service: Service) -> Work:
    """The request of one listener that creates a session or starts a turn.

    The scenario of the final slash also sends each request to the route
    itself, and the listener accepts it there.
    """
    session = session_of(chat_id())

    if service is Service.ATTENDANCE:
        # Contract 02 §5.1: create or find a session.
        return Work(SESSIONS_PATH, {}, {"family": FAMILY, "session": session})

    if service is Service.DOOR_OWUI:
        # One whole turn, and the answer comes after the turn settled.
        headers = owui_headers(chat_id(), message_id())

        return Work(CHAT_PATH, headers, chat_body(PROMPT, stream=False))

    if service is Service.CHAPERONE:
        # Contract 04 §7.1: one job turn in the thin family.
        body: dict[str, object] = {
            "tool": INVOKE_AGENT,
            "args": {"family": TARGET, "message": PROMPT},
        }

        return Work(CALL_PATH, {SESSION_HEADER: session}, body)

    if service is Service.DOOR_TRIGGER:
        # `docs/rework/spec.md` §7.3: one job of the autonomous family.
        return Work(hook_path(), {}, None)

    raise AssertionError(f"{service.value} takes no request that starts work")


def work_done(stack: Stack) -> list[str]:
    """What the services of one root did for a caller, one line for each thing.

    A request that a listener accepted leaves one of three things:

    1. A session in the session store, while the session exists.
    2. An outcome record, after a job ended and its session went.
    3. The record of the pi stand-in, for each pi process that started.
    """
    tree = stack.tree
    done = [f"pi process {call.pid}" for call in stack.pi_starts()]

    for family in sorted(entry.name for entry in tree.sessions_root.iterdir()):
        done.extend(f"session {family}/{session}" for session in tree.sessions_of(family))
        done.extend(f"outcome record of {family}" for _ in tree.outcomes(family))

    return done


def wire_form(request: httpx.Request) -> bytes:
    """One request with no body, as the bytes that HTTP/1.1 puts on a connection."""
    request_line = b" ".join((request.method.encode("ascii"), request.url.raw_path, b"HTTP/1.1"))
    fields = [name + b": " + value for name, value in request.headers.raw]

    return LINE_END.join([request_line, *fields]) + HEAD_END


async def raw_answer(address: Address, request: bytes) -> bytes:
    """Send the bytes of one request. Return each byte that comes back.

    The read ends when the service closes the connection. A service that
    keeps the connection open after a request with `Connection: close` fails
    here at the deadline. The failure gives that cause and each byte that
    came before the deadline.
    """
    if isinstance(address, UnixAddress):
        reader, writer = await asyncio.open_unix_connection(str(address.path))
    else:
        reader, writer = await asyncio.open_connection(address.host, address.port)

    answer = b""

    try:
        writer.write(request)
        await writer.drain()

        async with asyncio.timeout(ANSWER_DEADLINE_S):
            while chunk := await reader.read(READ_BYTES):
                answer += chunk

        return answer
    except TimeoutError:
        raise AssertionError(
            f"the listener kept the connection open for {ANSWER_DEADLINE_S} s "
            f"after a request with `Connection: close`. It sent {answer!r}"
        ) from None
    finally:
        writer.close()

        with contextlib.suppress(OSError):
            await writer.wait_closed()


def _running(child: Child | None) -> Child:
    """The process of a listener that its fixture started."""
    assert child is not None, "the fixture started no such service"

    return child


def _unit_of(service: Service) -> str:
    """The unit that runs one listener. The first unit of the trigger door runs `serve`."""
    return SERVICES[service].units[0]


def _stop_limit_of(unit: str) -> float | None:
    """`TimeoutStopSec` of one unit file in seconds, or None when the unit names none."""
    lines = (UNIT_DIR / unit).read_text(encoding="utf-8").splitlines()
    named = [line.removeprefix(STOP_LIMIT_KEY) for line in lines if line.startswith(STOP_LIMIT_KEY)]

    return float(named[-1]) if named else None
