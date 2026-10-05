"""The verify hook of the noticeboard: does the component work now?

Contract 06 §4. The release executor runs the hook after a release, and the
operator can run it at any time. A test plays the executor: it runs the
command of `noticeboard/component.yaml` to its end, then reads the exit code
and what the hook printed.

The hook gets `--env-file` with the env file of the unit (§4 rule 7). The
executor gives the hook no variable of the unit, so the file is where the
hook finds the noticeboard. The suite writes the file from the variables of
`proc_board.board_env`. The variable of the site file is not in it, because
the hook does not read the site file.

On the host the chaperone makes the audit directory. No chaperone runs in
this topology, so each fixture here writes one audit record first, as the
chaperone does.

CONTRACT-QUESTION: §4 rule 3 gives the exit code: 0 is a pass, and each
other code is a failure. No contract gives what a hook prints. Reading
taken: the hook of the noticeboard as it is. With `--json` its stdout is one
JSON object, and the boolean `ok` of the object says what the exit code
says. A change costs one assertion in each of the first two scenarios here.
"""

from __future__ import annotations

import json
from typing import Any, cast

import pytest
from proc_board import KEY_ENV, KEY_FILE_ENV, STATE_ROOT_ENV, BoardStack, view_env
from proc_harness import Finished
from proc_stack import base_env
from proc_tree import VIEW_KEY, token_of

#: Two paths in the root of a test that no fixture makes.
NO_STATE_ROOT = "no-such-state"
NO_ENV_FILE = "no-such.env"


@pytest.fixture
def serving(board_alone: BoardStack) -> BoardStack:
    """A noticeboard that serves, on a root with each directory that the host has."""
    board_alone.write_audit_record()

    return board_alone


@pytest.fixture
def silent(board_prepared: BoardStack) -> BoardStack:
    """The same root, with no noticeboard."""
    board_prepared.write_audit_record()

    return board_prepared


def report_of(done: Finished) -> dict[str, Any]:
    """The one JSON object that the hook printed on its stdout."""
    report: object = json.loads(done.stdout)

    assert isinstance(report, dict), done.stdout

    return cast("dict[str, Any]", report)


def test_the_hook_passes_beside_a_noticeboard_that_serves(serving: BoardStack) -> None:
    """§4 rule 3: exit 0 is a pass."""
    done = serving.verify(serving.write_view_env())

    assert done.exit_code == 0, done.stdout + done.stderr
    assert report_of(done)["ok"] is True


def test_the_hook_fails_when_no_noticeboard_serves(silent: BoardStack) -> None:
    """§4 rule 3. The root is whole, and nothing listens on the port of the env file."""
    port = silent.supervisor.free_port()
    values = view_env(silent.tree, port)

    done = silent.verify(silent.write_view_env(values))

    assert done.exit_code != 0
    assert report_of(done)["ok"] is False


def test_the_hook_fails_when_the_state_directory_is_missing(serving: BoardStack) -> None:
    """A noticeboard that answers is not enough. The hook also looks for what the service reads.

    The key file of the env file is there, so the missing directory is the
    one fault.
    """
    tree = serving.tree
    values = view_env(tree, serving.board_port)
    values[STATE_ROOT_ENV] = str(tree.root / NO_STATE_ROOT)

    done = serving.verify(serving.write_view_env(values))

    assert done.exit_code != 0


def test_the_hook_needs_no_variable_but_those_of_the_env_file(serving: BoardStack) -> None:
    """§4 rule 7: the environment of a hook holds nothing of its unit.

    The hook starts with no variable at all. It reads each value from the
    env file.
    """
    done = serving.verify(serving.write_view_env(), env={})

    assert done.exit_code == 0, done.stdout + done.stderr


def test_the_hook_fails_when_the_env_file_is_missing(serving: BoardStack) -> None:
    """§4 rule 7: the hook reads the file before it checks anything else.

    The noticeboard serves, and the environment of the hook holds each
    variable of the file. So the file that is not there is the one fault,
    and a hook that reads its environment in the place of the file passes
    by mistake.
    """
    tree = serving.tree
    env = base_env(tree) | view_env(tree, serving.board_port)

    done = serving.verify(tree.root / NO_ENV_FILE, env=env)

    assert done.exit_code != 0


@pytest.mark.parametrize("key_in", ["a-file", "the-env-file"])
def test_the_hook_prints_no_key_and_no_token(serving: BoardStack, key_in: str) -> None:
    """Invariant 13: no secret in an output. §4 rule 4: the executor keeps the output in its ledger.

    The unit can name a key file, or its env file can hold the key itself.
    The hook reads the key in both cases. It finds the token file of the
    `view-ro` principal in the state directory.
    """
    values = view_env(serving.tree, serving.board_port)

    if key_in == "the-env-file":
        del values[KEY_FILE_ENV]
        values[KEY_ENV] = VIEW_KEY

    done = serving.verify(serving.write_view_env(values))
    printed = done.stdout + done.stderr

    assert done.exit_code == 0, printed
    assert VIEW_KEY not in printed
    assert token_of("view-ro") not in printed
