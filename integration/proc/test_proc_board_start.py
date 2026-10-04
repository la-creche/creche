"""How the noticeboard starts, refuses to start and stops.

`docs/rework/spec.md` §8.3 gives the perimeter: never a wildcard bind, and
never a LAN bind with no key. Each scenario reads a process boundary: an exit
code, a port that nothing listens on, what the process wrote, a signal.

CONTRACT-QUESTION: §8.3 rule 2 names exit code 2 for a LAN bind with no key.
No section names a code for the other refusals. Reading taken: any code that
is not 0, as for each other service of this suite. A change to one fixed
code costs one assertion per scenario here.
"""

from __future__ import annotations

import signal

import httpx
import pytest
from proc_board import BoardStack, board_env
from proc_harness import LOOPBACK, TcpAddress, is_listening
from proc_services import Service
from proc_tree import LAN_ADDRESS, SECRET_MODE, VIEW_KEY, token_of

CHECK_FLAG = "--check"
EVERY_INTERFACE = "0.0.0.0"
EXIT_DEADLINE_S = 30.0

#: §8.3 rule 2: what the process exits with on a LAN bind with no key.
EXIT_NO_KEY = 2

#: Under the 32-byte floor. Its text is one a test can find in an output.
SHORT_KEY = "short-view-key"

#: `creche-noticeboard.service`: `TimeoutStopSec`. A stop takes less.
STOP_DEADLINE_S = 20.0

KEY_FILE_ENV = "VIEW_ACCESS_KEY_FILE"
BIND_ENV = "VIEW_BIND"


def test_the_check_validates_and_binds_nothing(board_prepared: BoardStack) -> None:
    """`ExecStartPre` of the unit: `--check` reads the config and exits 0."""
    tree = board_prepared.tree
    port = board_prepared.supervisor.free_port()

    child = board_prepared.spawn(Service.NOTICEBOARD, board_env(tree, LOOPBACK, port), CHECK_FLAG)

    assert child.wait(EXIT_DEADLINE_S) == 0, child.output()
    assert not is_listening(TcpAddress(port))
    # Invariant 13: the check says whether a key is set, never what it is.
    assert VIEW_KEY not in child.output()
    assert token_of("view-ro") not in child.output()


@pytest.mark.parametrize("key", [None, "", SHORT_KEY], ids=["missing", "empty", "short"])
def test_a_lan_bind_with_no_full_key_refuses_to_start(
    board_prepared: BoardStack, key: str | None
) -> None:
    """§8.3 rule 2. A LAN bind with no key is an open admin surface: exit 2.

    The bind is the LAN address of the site file, as in the unit. The
    process refuses before it binds, so nothing here needs that address.
    """
    tree = board_prepared.tree
    port = board_prepared.supervisor.free_port()
    env = board_env(tree, LAN_ADDRESS, port)
    del env[BIND_ENV]

    if key is None:
        del env[KEY_FILE_ENV]
    else:
        tree.view_key_file.write_text(key, encoding="utf-8")
        tree.view_key_file.chmod(SECRET_MODE)

    child = board_prepared.spawn(Service.NOTICEBOARD, env)

    assert child.wait(EXIT_DEADLINE_S) == EXIT_NO_KEY, child.output()
    assert SHORT_KEY not in child.output()


def test_a_wildcard_bind_refuses_to_start(board_prepared: BoardStack) -> None:
    """§8.3: never a wildcard, with a full key too."""
    tree = board_prepared.tree
    port = board_prepared.supervisor.free_port()

    child = board_prepared.spawn(Service.NOTICEBOARD, board_env(tree, EVERY_INTERFACE, port))

    assert child.wait(EXIT_DEADLINE_S) != 0
    assert not is_listening(TcpAddress(port))
    assert VIEW_KEY not in child.output()


def test_a_key_file_that_is_not_there_refuses_to_start(board_prepared: BoardStack) -> None:
    """Fail closed. A named key file that cannot be read is no key, on loopback too."""
    tree = board_prepared.tree
    port = board_prepared.supervisor.free_port()
    tree.view_key_file.unlink()

    child = board_prepared.spawn(Service.NOTICEBOARD, board_env(tree, LOOPBACK, port))

    assert child.wait(EXIT_DEADLINE_S) != 0
    assert not is_listening(TcpAddress(port))


def test_no_bind_and_no_site_address_refuses_to_start(board_prepared: BoardStack) -> None:
    """No default address exists. A default would be the address of one host."""
    tree = board_prepared.tree
    port = board_prepared.supervisor.free_port()
    env = board_env(tree, LOOPBACK, port)
    del env[BIND_ENV]
    del env["AGENT_LAN_ADDRESS"]

    child = board_prepared.spawn(Service.NOTICEBOARD, env)

    assert child.wait(EXIT_DEADLINE_S) != 0
    assert not is_listening(TcpAddress(port))


async def test_sigterm_ends_the_service(board_alone: BoardStack) -> None:
    """`KillSignal=SIGTERM` of the unit. The process ends, and the port closes."""
    assert board_alone.board is not None

    async with board_alone.client() as browser:
        before = await browser.get("/healthz")

    board_alone.board.send(signal.SIGTERM)
    board_alone.board.wait(STOP_DEADLINE_S)

    assert before.status_code == httpx.codes.OK
    assert not is_listening(TcpAddress(board_alone.board_port))
