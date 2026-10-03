"""A real `managerd` beside the running stack (packet I2, stage 2).

Stage 1's harness holds the door, `sessiond` and the playpen. Stage 2
adds the reconciler, so that a change to a family FILE is what drives the
scenario, exactly as it does on the host.

    family.yaml ──► reconcile_family() ──► grants/<family>.json  (the PEP reads it)
                            │          ──► config/                (the playpen reads it)
                            │          ──► status.json            (`sessiond` reads it)
                            │
                            └──HttpSwitchClient──uds──► sessiond /internal/switch-sandbox

Two things are fakes, and neither is under test: `FakeDriver`, because a
Mac has no `sbx`, and `FakeLiteLLMKeys`, because no test may mint a key.
Everything between the file and the answer is the real code.

`reconcile_family` is synchronous and `HttpSwitchClient` blocks on the
socket `sessiond` serves, so a pass runs in a worker thread. Calling it
inline would deadlock the listener it is calling.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import yaml
from agent_family import load_registry
from agent_managerd import paths as managerd_paths
from agent_managerd import sandboxes
from agent_managerd.driver import FakeDriver
from agent_managerd.egress import EgressConfig
from agent_managerd.litellm_keys import FakeLiteLLMKeys, key_alias
from agent_managerd.reconcile import Actors, ReconcileResult, reconcile_family
from agent_managerd.switch import HttpSwitchClient, SwitchClient, SwitchRequest, SwitchResult
from agent_managerd.switch import read_token as read_managerd_token
from agent_managerd.timers import FakeUnits
from stack import FAMILY, FIXTURE_LITELLM_KEY, Stack

#: An obvious fixture. Contract 06 owns digest selection, and `managerd`
#: resolves none of its own (contract 05 §4.1).
IMAGE = "sha256:" + "0" * 63 + "1"

#: The family file's default, from `family_body` below.
BUDGET_USD = 15.0

SWITCH_TIMEOUT_S = 60.0


def family_body(**overrides: object) -> dict[str, Any]:
    """The `chat` family the stack already serves, as a registry body."""
    body: dict[str, Any] = {
        "name": FAMILY,
        "kind": "attended",
        "description": "The integration stack's chat family.",
        "model": {"router": "agent-router", "budget_usd_per_day": int(BUDGET_USD)},
        "egress": [],
    }
    body.update(overrides)
    return body


def write_registry(root: Path, *, instructions: str = "Be helpful.\n", **overrides: object) -> Path:
    """Write the one family, replacing whatever was there."""
    family_dir = root / "families" / FAMILY
    family_dir.mkdir(parents=True, exist_ok=True)
    body = family_body(**overrides)
    (family_dir / "family.yaml").write_text(yaml.safe_dump(body), encoding="utf-8")
    (family_dir / "instructions.md").write_text(instructions, encoding="utf-8")
    return root


class Manager:
    """One `managerd` wired to the running `sessiond`.

    The switch client is the real one, over the same Unix socket the door
    uses, carrying the real token file `sessiond` minted.
    """

    def __init__(self, stack: Stack, registry_root: Path, switch: SwitchClient | None = None):
        self.registry_root = registry_root
        self.state_root = stack.state_root
        self.driver = FakeDriver()
        self.actors = Actors(
            driver=self.driver,
            litellm=_litellm_holding_the_fixture_key(),
            switch=switch if switch is not None else http_switch_client(stack),
            egress=EgressConfig(),
            units=FakeUnits(),
        )

    async def pass_once(self) -> ReconcileResult:
        """One reconcile pass, off the event loop `sessiond` serves on."""
        return await asyncio.to_thread(self._pass_once)

    def _pass_once(self) -> ReconcileResult:
        return reconcile_family(
            load_registry(self.registry_root),
            FAMILY,
            state_root=self.state_root,
            image=IMAGE,
            actors=self.actors,
        )

    def live_ids(self) -> tuple[str, ...]:
        """Every sandbox the ledger still counts as this family's."""
        return tuple(
            one.id
            for one in sandboxes.read_ledger(self.state_root, FAMILY)
            if one.state in sandboxes.LIVE_STATES
        )

    def status(self) -> dict[str, Any]:
        """The status document as `sessiond` and the view read it."""
        path = managerd_paths.status_path(self.state_root, FAMILY)
        body: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        return body

    def published_ids(self) -> list[str]:
        return [str(one["id"]) for one in self.status()["sandboxes"]]

    def reach_of(self, sandbox: str) -> tuple[str, ...]:
        """Every host the driver was ever told to allow for one sandbox.

        Cumulative, and never narrowed by a later call: the question a
        replacement has to answer is whether any MOMENT of the pass granted
        more than the family file allows (invariant 9's "narrowing before
        widening").
        """
        granted: list[str] = []
        for call in self.driver.calls:
            if call.op == "set_egress" and call.args[0] == sandbox:
                granted.extend(str(one) for one in call.args[1])  # pyright: ignore[reportGeneralTypeIssues]

        return tuple(dict.fromkeys(granted))

    def grants(self) -> bytes:
        """The grant file the PEP reads live, per call (contract 04 §1.1)."""
        return managerd_paths.grant_path(self.state_root, FAMILY).read_bytes()


def http_switch_client(stack: Stack) -> SwitchClient:
    """The real client, with the real token file `sessiond` reads back."""
    socket = stack.sessiond_socket

    if socket is None:
        raise AssertionError("the stack is not serving yet")

    token = read_managerd_token(managerd_paths.managerd_token_path(stack.state_root))

    return HttpSwitchClient(
        "http://sessiond",
        token,
        httpx.Client(
            transport=httpx.HTTPTransport(uds=str(socket)),
            timeout=SWITCH_TIMEOUT_S,
        ),
    )


class BreakTheIncomingEnv:
    """A switch client that ruins the incoming sandbox's `supervisor.env`
    ONCE, one instant before the call, then makes the real call.

    This is how a scenario reaches "the new sandbox never completes its
    handshake" with the real bundle. `sbx exec --env-file` carries the
    three mount paths, and a playpen that finds one unset answers
    `fatal` with `mount_dir_unset` rather than `ready` (contract 03 §5.7).

    The moment matters: `managerd` writes the file during the create, and
    the create and the call are one pass. A test that broke the file before
    the pass would watch the create write it again.

    Once, because the damage is an event and not a property of the host. A
    client that broke the file on every call would prove only that a
    reconciler cannot converge against a saboteur.

    Only a REPLACEMENT, which is what `from` being set means (contract 05
    §5.1: it is "null on a first create"). `managerd` also calls §5 to ask
    for the first handshake of a sandbox that is not yet `ready` (§4.3 step
    7), and that call comes first. Without this guard the one shot lands on
    the sandbox the stack is already serving on, and every scenario that
    takes a turn afterwards fails with `sandbox_lost`.
    """

    def __init__(self, stack: Stack, inner: SwitchClient) -> None:
        self._stack = stack
        self._inner = inner
        self._spent = False

    def switch(self, request: SwitchRequest) -> SwitchResult:
        if not self._spent and request.outgoing is not None:
            self._spent = True
            self._stack.playpen_env_of(request.to).write_text(
                "AGENT_SANDBOX=" + request.to + "\n", encoding="utf-8"
            )

        return self._inner.switch(request)


def _litellm_holding_the_fixture_key() -> FakeLiteLLMKeys:
    """The stack's `creds.json` already names a key. `managerd` re-applying
    onto a family it once minted for refreshes that key rather than minting
    a second one, so the fake has to know it — exactly as LiteLLM would."""
    fake = FakeLiteLLMKeys()
    fake.keys[key_alias(FAMILY)] = FIXTURE_LITELLM_KEY
    fake.budgets[key_alias(FAMILY)] = (["agent-router"], BUDGET_USD)
    return fake
