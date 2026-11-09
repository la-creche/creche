"""The exit test: three scenarios of `caregiver` and the Rust chaperone.

`proc_exit.py` draws the topology, and says why the six scenarios of phase 2
left it. `caregiver` writes `policy.toml`, and the chaperone serves it. Each
scenario plays the agent module of one family at the front door of the
chaperone, with the token of its `creds.json`.

Each assertion reads what crossed a process boundary: an MCP result, the
audit file of the chaperone, a file of `caregiver`. The text of each failed
assertion names the log that explains it, and the report of a failed test
holds each of those logs (`integration/proc/AGENTS.md`, "Reading a
failure").

`conftest.py` marks every test of this directory `slow`. The fixture
`scenario_budget` fails a scenario that takes 60 seconds or more, the start of the
topology and its end included.
"""

from __future__ import annotations

import re
import time
from collections.abc import Iterator
from typing import Any, Final

import pytest
from proc_exit import (
    CALLER,
    ECHO_TOOL,
    FAMILIES,
    GATED_TOOL,
    TARGET,
    ExitStack,
    explain,
    target_body,
    token_digest,
    wait_until,
)
from proc_tree import (
    RS_ATTENDANCE,
    RS_UPSTREAM,
    end_token_overlap,
    write_family_file,
)

#: The budget of one scenario, in seconds.
BUDGET_S: Final = 60.0

#: A token that the `bearer` plugin takes: 64 lower-case hex characters (§9).
HEX_TOKEN: Final = re.compile(r"[0-9a-f]{64}")

#: The result of a call of `echo` that runs, and of a call whose token no
#: app table holds (§5.2 row 1).
ECHOED: Final = {"content": [{"type": "text", "text": '{"text":"t"}'}], "isError": False}
UNKNOWN: Final = {
    "content": [{"type": "text", "text": "chaperone denied this call: unknown_credential"}],
    "isError": True,
}

ECHO: Final = f"{RS_UPSTREAM}__echo"
GATED_ECHO: Final = f"{RS_UPSTREAM}__gated_echo"
INVOKE_AGENT: Final = f"{RS_ATTENDANCE}__invoke_agent"


@pytest.fixture(autouse=True)
def scenario_budget() -> Iterator[None]:
    """Each scenario, its topology included, ends within `BUDGET_S`."""
    started = time.monotonic()

    yield

    spent = time.monotonic() - started
    assert spent < BUDGET_S, f"the scenario took {spent:.1f} s, over its budget of {BUDGET_S} s"


# ------------------------------------------------------- the policy file


def test_caregiver_writes_the_policy_the_chaperone_serves(exit_stack: ExitStack) -> None:
    """`caregiver` writes one app table for each family, and `chaperone check` passes it.

    Each table holds one digest: the digest of the token of 64 hex
    characters in the `creds.json` of its family. The chaperone serves the
    file: `chat` lists each tool of its grants.
    """
    apps = exit_stack.policy()["apps"]
    checked = exit_stack.check("policy.toml")

    with exit_stack.mcp(CALLER) as session:
        names = sorted(session.tool_names())

    assert sorted(apps) == sorted(FAMILIES)

    for family in FAMILIES:
        token = exit_stack.tokens[family]
        assert HEX_TOKEN.fullmatch(token), f"the token of {family} is not 64 hex characters"
        assert apps[family]["token_sha256"] == [token_digest(token)], explain("caregiver")

    assert (checked.exit_code, checked.stdout.strip()) == (0, "policy.toml: ok"), checked.stderr
    assert names == sorted([ECHO, GATED_ECHO, INVOKE_AGENT]), explain("chaperone")


def test_a_registry_edit_reaches_the_chaperone_at_the_next_pass(exit_stack: ExitStack) -> None:
    """A tool that the registry adds to a grant runs after the next pass of `caregiver`.

    The family file of `vault-oracle` grants `echo` alone, so the chaperone
    denies its call of `gated_echo`. The test adds the tool to that file.
    The next pass of `caregiver` writes it into `policy.toml`. The
    chaperone reloads the file at the next call (§6), and the identical
    call runs.
    """
    with exit_stack.mcp(TARGET) as session:
        before = session.call(GATED_ECHO, {"text": "x"})

    write_family_file(exit_stack.tree, target_body((ECHO_TOOL, GATED_TOOL)))
    wait_until(
        lambda: _granted(exit_stack, TARGET) == [ECHO_TOOL, GATED_TOOL] or None,
        f"{GATED_TOOL} in the grant of {TARGET}",
        "caregiver",
    )

    with exit_stack.mcp(TARGET) as session:
        after = session.call(GATED_ECHO, {"text": "x"})

    assert before["isError"] is True, explain("chaperone")
    assert after == {"content": [{"type": "text", "text": '{"text":"x"}'}], "isError": False}, (
        explain("chaperone")
    )
    assert _events(exit_stack, GATED_ECHO) == [
        ("decided", "deny:tool_not_granted"),
        ("decided", "allow"),
        ("ran", "is_error:false"),
    ], explain("chaperone")


def test_a_rotation_serves_two_digests_until_it_settles(exit_stack: ExitStack) -> None:
    """`caregiver rotate` (graceful) serves the old and the new token, then the new one alone.

    The rotation puts both digests into the table of `vault-oracle`, and a
    call of `echo` runs with each token. The overlap lasts 300 seconds, and no flag
    of `caregiver` changes that. So the test moves its end into the past, as
    the 300 seconds would. A `SIGHUP` starts the next pass of `caregiver
    serve`, and that pass settles the rotation: one digest stays, and the
    chaperone denies a call with the old token.
    """
    old = exit_stack.tokens[TARGET]
    done = exit_stack.rotate(TARGET)
    new = str(exit_stack.credentials(TARGET)["pep_token"])
    overlap = _digests(exit_stack, TARGET)
    during = [_echo_as(exit_stack, token) for token in (old, new)]

    end_token_overlap(exit_stack.tree, TARGET)
    exit_stack.await_pass(
        lambda: _digests(exit_stack, TARGET) == [token_digest(new)] or None,
        f"the settle of {TARGET}",
    )
    after = [_echo_as(exit_stack, token) for token in (old, new)]

    assert done.exit_code == 0, done.stderr
    assert HEX_TOKEN.fullmatch(new) and new != old
    assert sorted(overlap) == sorted([token_digest(old), token_digest(new)]), explain("caregiver")
    assert during == [ECHOED, ECHOED], explain("chaperone")
    assert after == [UNKNOWN, ECHOED], explain("chaperone")
    assert exit_stack.credentials(TARGET)["previous_pep_token"] is None, explain("caregiver")


def _echo_as(stack: ExitStack, token: str) -> dict[str, Any]:
    """The result of one call of `echo` at the front door, with `token` as the bearer."""
    with stack.mcp_as(token) as session:
        return session.call(ECHO, {"text": "t"})


def _digests(stack: ExitStack, family: str) -> list[str]:
    """The token digests that `policy.toml` holds for one family."""
    return list(stack.policy().get("apps", {}).get(family, {}).get("token_sha256", []))


def _granted(stack: ExitStack, family: str) -> list[str]:
    """The tools of the upstream stand-in that `policy.toml` grants one family."""
    grants = stack.policy().get("apps", {}).get(family, {}).get("grants", {})

    return sorted(grants.get(RS_UPSTREAM, {}).get("tools", {}))


def _events(stack: ExitStack, tool: str) -> list[tuple[str, str]]:
    """The event and the detail of each audit record of one exposed tool."""
    return [
        (str(record["event"]), str(record["detail"]))
        for record in stack.audit()
        if record.get("tool") == tool
    ]
