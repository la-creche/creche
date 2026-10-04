"""How the two services of the first topology start, refuse and reload.

No suite that hosts a service in the test process can see these: an exit
code, a socket that was never bound, the mode of a socket file, a signal.
Each scenario names the rule it holds, and each assertion reads a process
boundary.

CONTRACT-QUESTION: contract 02 §3 rule 7 says a service "refuses to start"
and names no exit code. Reading taken: any exit code that is not 0, with
nothing bound. A unit with `Restart=always` starts the service again
whatever the code is. A change to one fixed code costs one assertion here.
"""

from __future__ import annotations

import asyncio
import signal
import stat
from pathlib import Path

import httpx
import pytest
from proc_chat import SESSIONS_PATH, chat_id, session_of
from proc_harness import LOOPBACK, TcpAddress, free_port, is_listening
from proc_owui import OwuiStack, door_env
from proc_services import Service
from proc_stack import attendance_env
from proc_tree import DOOR_KEY, FAMILY, PRINCIPALS, SECRET_MODE, token_of

#: One byte under the floor of contract 02 §3 rule 7. Its text is one a test
#: can find in an output, to prove that no output holds it.
SHORT_SECRET = "short-secret-" + "s" * 18
ROTATED_TOKEN = "rotated-door-owui-" + "r" * 32

#: Contract 02 §3 rule 1: the mode `attendance` gives its socket after bind.
SOCKET_MODE = 0o660

EXIT_DEADLINE_S = 30.0
RELOAD_DEADLINE_S = 10.0
RELOAD_POLL_S = 0.02
CHECK_FLAG = "--check"
EVERY_INTERFACE = "0.0.0.0"


@pytest.mark.parametrize("content", [None, "", SHORT_SECRET], ids=["missing", "empty", "short"])
def test_attendance_refuses_a_bad_token_file(owui_prepared: OwuiStack, content: str | None) -> None:
    """Contract 02 §3 rule 7. Fail closed: no socket, and a failed start."""
    tree = owui_prepared.tree
    _replace(tree.token_file("door-tui"), content)

    child = owui_prepared.spawn(Service.ATTENDANCE, attendance_env(tree))

    assert child.wait(EXIT_DEADLINE_S) != 0
    assert not tree.attendance_socket.exists()
    # Rule 6: no token, and no part of one, in anything the service writes.
    assert SHORT_SECRET not in child.output()


@pytest.mark.parametrize(
    ("principal", "mode"),
    [("door-owui", 0o640), ("door-delegate", 0o644), ("door-delegate", 0o660)],
    ids=["a-door-token-with-group-read", "world-read", "group-write"],
)
def test_attendance_refuses_a_token_file_that_is_too_open(
    owui_prepared: OwuiStack, principal: str, mode: int
) -> None:
    """Contract 02 §3 rule 5. Mode 0600, and 0640 for the two files the chaperone reads.

    The other side of the rule is in every scenario that starts `attendance`:
    the fixture writes those two files at 0640, and the service starts.
    """
    tree = owui_prepared.tree
    tree.token_file(principal).chmod(mode)

    child = owui_prepared.spawn(Service.ATTENDANCE, attendance_env(tree))

    assert child.wait(EXIT_DEADLINE_S) != 0
    assert not tree.attendance_socket.exists()


def test_attendance_check_validates_and_binds_nothing(owui_prepared: OwuiStack) -> None:
    """`ExecStartPre` of the unit: `--check` reads the config and exits 0."""
    tree = owui_prepared.tree

    child = owui_prepared.spawn(Service.ATTENDANCE, attendance_env(tree), CHECK_FLAG)

    assert child.wait(EXIT_DEADLINE_S) == 0, child.output()
    assert not tree.attendance_socket.exists()
    assert not any(token_of(principal) in child.output() for principal in PRINCIPALS)


def test_attendance_check_refuses_a_bad_token_file(owui_prepared: OwuiStack) -> None:
    """A unit whose `ExecStartPre` fails never reaches `ExecStart`."""
    tree = owui_prepared.tree
    _replace(tree.token_file("view-ro"), None)

    child = owui_prepared.spawn(Service.ATTENDANCE, attendance_env(tree), CHECK_FLAG)

    assert child.wait(EXIT_DEADLINE_S) != 0


def test_the_socket_takes_the_mode_the_chaperone_needs(owui: OwuiStack) -> None:
    """Contract 02 §3 rule 1. `connect(2)` needs group write on the socket."""
    mode = stat.S_IMODE(owui.tree.attendance_socket.stat().st_mode)

    assert mode == SOCKET_MODE, oct(mode)


async def test_a_request_with_no_token_is_refused(
    owui: OwuiStack, attendance_api: httpx.AsyncClient
) -> None:
    """Contract 02 §3 rule 4 and §14: 401 `unauthorized`, in the one error body."""
    unknown = {"Authorization": "Bearer " + "u" * 40}

    async with owui.attendance_client() as client:
        client.headers.pop("Authorization")
        missing = await client.get(SESSIONS_PATH)

    wrong = await attendance_api.get(SESSIONS_PATH, headers=unknown)

    for response in (missing, wrong):
        assert response.status_code == httpx.codes.UNAUTHORIZED
        assert response.json()["error"]["code"] == "unauthorized"


async def test_the_door_refuses_a_wrong_key(door: httpx.AsyncClient) -> None:
    """The door's own bearer check. A refusal is a status, never a stream."""
    response = await door.get("/v1/models", headers={"Authorization": "Bearer " + "w" * 40})

    assert response.status_code == httpx.codes.UNAUTHORIZED
    assert response.json()["error"]["code"] == "unauthorized"


async def test_sighup_reloads_the_token_files(
    owui: OwuiStack, attendance_api: httpx.AsyncClient
) -> None:
    """Contract 02 §3 rule 8. A reload changes the tokens and keeps the sessions."""
    assert owui.attendance is not None
    session = session_of(chat_id())
    session_path = f"{SESSIONS_PATH}/{FAMILY}/{session}"
    created = await attendance_api.post(SESSIONS_PATH, json={"family": FAMILY, "session": session})
    assert created.status_code == httpx.codes.CREATED

    _replace(owui.tree.token_file("door-owui"), ROTATED_TOKEN)
    owui.attendance.send(signal.SIGHUP)
    await _answers_ok(attendance_api, session_path, {"Authorization": f"Bearer {ROTATED_TOKEN}"})

    old = await attendance_api.get(session_path)

    assert old.status_code == httpx.codes.UNAUTHORIZED
    assert owui.attendance.exit_code() is None, "a reload must not end the service"


def test_the_door_refuses_a_short_key(owui_prepared: OwuiStack) -> None:
    """Contract 02 §3 rule 7, as the door applies it to its own key."""
    tree = owui_prepared.tree
    port = free_port()
    _replace(tree.door_key_file, SHORT_SECRET)

    child = owui_prepared.spawn(Service.DOOR_OWUI, door_env(tree, f"{LOOPBACK}:{port}"))

    assert child.wait(EXIT_DEADLINE_S) != 0
    assert not is_listening(TcpAddress(port))
    assert SHORT_SECRET not in child.output()


def test_the_door_refuses_to_bind_every_interface(owui_prepared: OwuiStack) -> None:
    """Contract 02 §3 rule 9: a door refuses `0.0.0.0` whatever its config says."""
    tree = owui_prepared.tree
    port = free_port()

    child = owui_prepared.spawn(Service.DOOR_OWUI, door_env(tree, f"{EVERY_INTERFACE}:{port}"))

    assert child.wait(EXIT_DEADLINE_S) != 0
    assert not is_listening(TcpAddress(port))


def test_the_door_check_validates_and_binds_nothing(owui_prepared: OwuiStack) -> None:
    """`ExecStartPre` of the unit: `--check` reads the config and exits 0."""
    tree = owui_prepared.tree
    port = free_port()
    env = door_env(tree, f"{LOOPBACK}:{port}")

    child = owui_prepared.spawn(Service.DOOR_OWUI, env, CHECK_FLAG)

    assert child.wait(EXIT_DEADLINE_S) == 0, child.output()
    assert not is_listening(TcpAddress(port))
    assert DOOR_KEY not in child.output()
    assert token_of("door-owui") not in child.output()


async def _answers_ok(client: httpx.AsyncClient, path: str, headers: dict[str, str]) -> None:
    """Ask until the answer is 200. A reload is not instant."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + RELOAD_DEADLINE_S

    while True:
        response = await client.get(path, headers=headers)

        if response.is_success:
            return

        if loop.time() > deadline:
            raise AssertionError(f"{path} still answers {response.status_code}")

        await asyncio.sleep(RELOAD_POLL_S)


def _replace(path: Path, content: str | None) -> None:
    """Remove a secret file, or put another in its place by rename."""
    if content is None:
        path.unlink()
        return

    temp = path.with_name(f".{path.name}.tmp")
    temp.write_text(content, encoding="utf-8")
    temp.chmod(SECRET_MODE)
    temp.replace(path)
