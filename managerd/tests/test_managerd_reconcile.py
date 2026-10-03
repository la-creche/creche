"""One reconcile pass over one family (contract 05).

Invariant 9 is what these tests hold in place: "A permission change needs
one action and no manual apply. It never ends a session. A removal applies
at once." Each test drives one family file to a second revision and asks
what the fakes saw — which sandbox, in which order, and whether any moment
of the pass left more reach than the target."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import yaml
from agent_family import FamilyState, SwitchMode, load_registry
from agent_managerd import paths, sandboxes
from agent_managerd.applied import read_applied, write_applied
from agent_managerd.clock import seconds_from_now
from agent_managerd.driver import DriverError, FakeDriver, SandboxSpec
from agent_managerd.egress import EgressConfig, litellm_endpoint, pep_endpoint
from agent_managerd.litellm_keys import FakeLiteLLMKeys, HttpLiteLLMKeys, LiteLLMError, Spend
from agent_managerd.reconcile import (
    DESTROY_STEP,
    SWITCH_STEP,
    Actors,
    ReconcileResult,
    SpendRead,
    reconcile_family,
)
from agent_managerd.switch import FakeSwitchClient, SwitchRequest
from agent_managerd.timers import FakeUnits
from managerd_helpers import write_registry

FAMILY: str = "chat"
IMAGE: str = "sha256:deadbeef"


def planes() -> tuple[str, ...]:
    return (litellm_endpoint(), pep_endpoint())


DOCS: str = "docs.python.org:443"
HOURLY: str = "0 * * * *"
SERVER: str = "kagi"


class FailingCreate(FakeDriver):
    """`sbx create` that reports failure after making the VM."""

    def create(self, spec: SandboxSpec) -> None:
        super().create(spec)
        raise DriverError(f"sbx create {spec.name} failed: out of memory")


class BlindSpend(FakeLiteLLMKeys):
    """LiteLLM that answers every spend read with an error."""

    def read_spend(self, key: str) -> Spend:
        raise LiteLLMError("key info failed: HTTP 503")


def _refuse_the_connection(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("[Errno 111] Connection refused", request=request)


class AwaySpend(FakeLiteLLMKeys):
    """Keys from the fake, the spend read from the REAL client, over a
    connection that is refused. `BlindSpend` raises the error the loop
    expects. This one proves the real client raises it too."""

    def read_spend(self, key: str) -> Spend:
        transport = httpx.MockTransport(_refuse_the_connection)
        real = HttpLiteLLMKeys(
            "http://litellm.test:4000", "sk-master", client=httpx.Client(transport=transport)
        )
        return real.read_spend(key)


@dataclass
class Fleet:
    """One family's whole world: a registry, a state root, three fakes."""

    registry_root: Path
    state_root: Path
    driver: FakeDriver = field(default_factory=FakeDriver)
    litellm: FakeLiteLLMKeys = field(default_factory=FakeLiteLLMKeys)
    switch: FakeSwitchClient = field(default_factory=FakeSwitchClient)
    units: FakeUnits = field(default_factory=FakeUnits)

    def write(self, **overrides: object) -> None:
        write_registry(self.registry_root, **overrides)

    def run(self, *, spend: SpendRead = SpendRead.SKIP, image: str = IMAGE) -> ReconcileResult:
        actors = Actors(self.driver, self.litellm, self.switch, EgressConfig(), self.units)
        return reconcile_family(
            load_registry(self.registry_root),
            FAMILY,
            state_root=self.state_root,
            image=image,
            actors=actors,
            spend=spend,
        )

    def ops(self) -> tuple[str, ...]:
        return self.driver.ops()

    def forget_calls(self) -> None:
        """Only the second pass's calls matter to most tests here."""
        self.driver.calls.clear()

    def allow_of(self, sandbox_id: str) -> tuple[str, ...]:
        for record in sandboxes.read_ledger(self.state_root, FAMILY):
            if record.id == sandbox_id:
                return record.allow

        raise AssertionError(f"no ledger row for {sandbox_id}")

    def live_ids(self) -> tuple[str, ...]:
        return tuple(
            one.id
            for one in sandboxes.read_ledger(self.state_root, FAMILY)
            if one.state in sandboxes.LIVE_STATES
        )


@pytest.fixture
def fleet(tmp_path: Path) -> Fleet:
    ready = Fleet(tmp_path / "registry", tmp_path / "state")
    ready.write()
    return ready


def write_fault_file(state_root: Path, source: str, code: str, since: float | None = None) -> None:
    path = paths.fault_path(state_root, source, FAMILY)
    path.parent.mkdir(parents=True, exist_ok=True)
    raised = seconds_from_now(-60) if since is None else _rfc3339(since)
    body = {
        "family": FAMILY,
        "written_at": seconds_from_now(0),
        "faults": [{"code": code, "since": raised, "source": source}],
    }
    path.write_text(json.dumps(body), encoding="utf-8")


def _rfc3339(epoch_s: float) -> str:
    return datetime.fromtimestamp(epoch_s, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def hosts_of(driver: FakeDriver, op: str) -> list[tuple[str, tuple[str, ...]]]:
    """Every `set_egress` or `remove_egress` call, as (sandbox, hosts)."""
    found: list[tuple[str, tuple[str, ...]]] = []
    for call in driver.calls:
        if call.op != op:
            continue

        sandbox, hosts = call.args[0], call.args[1]
        found.append((str(sandbox), tuple(str(one) for one in tuple(hosts))))  # type: ignore[call-overload]

    return found


def granted_hosts(driver: FakeDriver, sandbox_id: str) -> set[str]:
    """Every host this driver was ever asked to allow for one sandbox."""
    granted: set[str] = set()
    for name, hosts in hosts_of(driver, "set_egress"):
        if name == sandbox_id:
            granted.update(hosts)

    return granted


def family_text(fleet: Fleet) -> str:
    return (fleet.registry_root / "families" / FAMILY / "family.yaml").read_text(encoding="utf-8")


def roll_back(fleet: Fleet, *, rev: str, text: str) -> None:
    """Put the applied snapshot back where a crash would have left it: the
    steps ran, the snapshot did not move."""
    write_applied(fleet.state_root, FAMILY, rev=rev, image=IMAGE, family_text=text)


# --- a first pass brings the family up ------------------------------------------


def test_a_first_pass_creates_the_first_sandbox(fleet: Fleet) -> None:
    result = fleet.run()
    assert result.status.state is FamilyState.IN_SYNC
    assert fleet.live_ids() == ("chat-s1",)


def test_a_first_pass_records_the_revision_it_applied(fleet: Fleet) -> None:
    result = fleet.run()
    applied = read_applied(fleet.state_root, FAMILY)
    assert applied is not None
    assert applied.rev == result.status.registry_rev
    assert applied.image == IMAGE


def test_a_first_pass_asks_for_the_handshake(fleet: Fleet) -> None:
    """Contract 05 §5.1's `from` is "null on a first create" and §5.3 rule 8
    says "a first switch always names a `creating` sandbox, because running
    the handshake is what promotes it". That call is the only way `managerd`
    can learn the handshake passed (§1), so it is not optional."""
    fleet.run()

    assert [(one.outgoing, one.to) for one in fleet.switch.requests] == [(None, "chat-s1")]


def test_a_passing_handshake_makes_the_sandbox_ready(fleet: Fleet) -> None:
    """Contract 05 §4.3 step 7. Without it `ready_at` stays null and `power`
    reads `stopped` for a sandbox that is serving turns."""
    result = fleet.run()

    row = result.status.sandboxes[0]
    assert str(row.state) == "ready"
    assert row.ready_at is not None
    assert str(row.power) == "running"


def test_a_refused_handshake_leaves_the_sandbox_creating(fleet: Fleet) -> None:
    """A refusal changes nothing. `sessiond` still dials a `creating`
    sandbox (§4.2 rule 1), so the family keeps serving, and §3.3's
    `sandbox_start_failed` means "after the retry budget" — not after one
    refusal — so no fault is raised."""
    fleet.switch = FakeSwitchClient(refuse="sessiond is not listening")

    result = fleet.run()

    assert str(result.status.sandboxes[0].state) == "creating"
    assert result.status.sandboxes[0].ready_at is None
    assert result.status.faults == ()


def test_a_pass_after_a_refused_handshake_asks_again(fleet: Fleet) -> None:
    """Every step is idempotent, so an interrupted pass simply repeats."""
    fleet.switch = FakeSwitchClient(refuse="sessiond is not listening")
    fleet.run()
    fleet.switch = FakeSwitchClient()

    result = fleet.run()

    assert [one.to for one in fleet.switch.requests] == ["chat-s1"]
    assert str(result.status.sandboxes[0].state) == "ready"


def test_a_ready_sandbox_is_never_asked_again(fleet: Fleet) -> None:
    """`ready` means the handshake passed, which is a fact and not a state
    to re-prove. A second call per pass would cost an `sbx exec` every two
    seconds."""
    fleet.run()
    fleet.switch.requests.clear()

    fleet.run()

    assert fleet.switch.requests == []


def test_an_unchanged_second_pass_touches_nothing(fleet: Fleet) -> None:
    fleet.run()
    fleet.forget_calls()
    result = fleet.run()
    assert fleet.ops() == ()
    assert result.ran == ()
    assert result.status.state is FamilyState.IN_SYNC


def test_an_unchanged_pass_does_not_re_push_the_key(fleet: Fleet) -> None:
    """The loop runs every few seconds. Pushing an unchanged model and
    budget to LiteLLM on each one is a call per family per tick, forever."""
    fleet.run()
    fleet.litellm.budgets.clear()
    fleet.run()
    assert fleet.litellm.budgets == {}


# --- the live axes: egress -------------------------------------------------------


def test_an_added_egress_host_lands_on_the_running_sandbox(fleet: Fleet) -> None:
    fleet.run()
    fleet.forget_calls()
    fleet.write(egress=[DOCS])
    result = fleet.run()
    assert "create" not in fleet.ops()
    assert hosts_of(fleet.driver, "set_egress") == [("chat-s1", (DOCS,))]
    assert result.status.state is FamilyState.IN_SYNC


def test_a_removed_egress_host_is_dropped_before_anything_is_added(fleet: Fleet) -> None:
    """Invariant 9, narrowing before widening: an interruption between the
    halves must leave LESS reach than the target, never more."""
    fleet.write(egress=[DOCS])
    fleet.run()
    fleet.forget_calls()
    fleet.write(egress=["pypi.org:443"])
    fleet.run()
    assert fleet.ops() == ("remove_egress", "set_egress")
    assert hosts_of(fleet.driver, "remove_egress") == [("chat-s1", (DOCS,))]
    assert hosts_of(fleet.driver, "set_egress") == [("chat-s1", ("pypi.org:443",))]


def test_an_egress_edit_never_removes_a_plane_endpoint(fleet: Fleet) -> None:
    """Both planes are `managerd`'s own config. No family file names them,
    so no family edit may take them away (contract 01 §3.7 rule 4)."""
    fleet.write(egress=[DOCS])
    fleet.run()
    fleet.forget_calls()
    fleet.write(egress=[])
    fleet.run()
    removed = hosts_of(fleet.driver, "remove_egress")[0][1]
    assert litellm_endpoint() not in removed
    assert pep_endpoint() not in removed


def test_an_egress_edit_moves_the_rows_a_destroy_will_remove(fleet: Fleet) -> None:
    fleet.run()
    fleet.write(egress=[DOCS])
    fleet.run()
    assert fleet.allow_of("chat-s1") == (*planes(), DOCS)


def test_a_refused_egress_call_raises_a_blocking_fault(fleet: Fleet) -> None:
    fleet.write(egress=[DOCS])
    fleet.run()

    class RefusingPolicy(FakeDriver):
        def remove_egress(self, name: str, hosts: tuple[str, ...]) -> None:
            raise DriverError(f"policy rm for {name} failed")

    fleet.driver = RefusingPolicy()
    fleet.write(egress=[])
    result = fleet.run()
    assert result.status.state is FamilyState.DEGRADED
    assert [one.code for one in result.status.faults] == ["egress_assert_failed"]


def test_a_refused_egress_call_leaves_the_revision_unapplied(fleet: Fleet) -> None:
    """The snapshot moves only at the end of a successful pass, so the next
    pass computes the same diff and retries the same removal."""
    fleet.write(egress=[DOCS])
    first = fleet.run()

    class RefusingPolicy(FakeDriver):
        def remove_egress(self, name: str, hosts: tuple[str, ...]) -> None:
            raise DriverError(f"policy rm for {name} failed")

    fleet.driver = RefusingPolicy()
    fleet.write(egress=[])
    fleet.run()
    applied = read_applied(fleet.state_root, FAMILY)
    assert applied is not None
    assert applied.rev == first.status.registry_rev


# --- the live axes: model and budget ---------------------------------------------


def test_a_model_change_updates_the_key_and_creates_nothing(fleet: Fleet) -> None:
    fleet.run()
    fleet.forget_calls()
    fleet.write(model={"router": "agent-fast", "budget_usd_per_day": 15})
    fleet.run()
    assert fleet.ops() == ()
    assert fleet.litellm.budgets["family-chat"] == (["agent-fast"], 15.0)


def test_a_model_change_is_not_in_sync_until_the_cache_turns_over(fleet: Fleet) -> None:
    """Contract 05 §7 rule 6: a `/key/update` can be served stale by another
    LiteLLM worker for up to 10 s, so `in_sync` may not be claimed yet."""
    fleet.run()
    fleet.write(model={"router": "agent-fast", "budget_usd_per_day": 15})
    result = fleet.run()
    assert result.status.state is FamilyState.RECONCILING
    applied = read_applied(fleet.state_root, FAMILY)
    assert applied is not None
    assert applied.model_live_at is not None


def test_a_budget_change_is_in_sync_at_once(fleet: Fleet) -> None:
    """§7 rule 6 again, the other half: a budget drop refuses the next
    request at once, so there is no window to wait out."""
    fleet.run()
    fleet.write(model={"router": "agent-router", "budget_usd_per_day": 5})
    result = fleet.run()
    assert result.status.state is FamilyState.IN_SYNC
    assert fleet.litellm.budgets["family-chat"] == (["agent-router"], 5.0)


# --- the live axes: grants and config --------------------------------------------


def test_a_verb_grant_rewrites_the_grant_file(fleet: Fleet) -> None:
    fleet.run()
    before = paths.grant_path(fleet.state_root, FAMILY).read_text(encoding="utf-8")
    fleet.write(verbs={"embed": {}})
    fleet.run()
    after = paths.grant_path(fleet.state_root, FAMILY).read_text(encoding="utf-8")
    assert "embed" not in before
    assert "embed" in after


def test_a_missing_grant_file_is_written_again(fleet: Fleet) -> None:
    """Without the file the PEP refuses every call for this family, and no
    diff would ever ask for it: the family file did not move."""
    fleet.run()
    paths.grant_path(fleet.state_root, FAMILY).unlink()
    result = fleet.run()
    assert paths.grant_path(fleet.state_root, FAMILY).is_file()
    assert result.status.state is FamilyState.IN_SYNC


def test_an_unchanged_pass_does_not_rewrite_the_grant_file(fleet: Fleet) -> None:
    """`write_grants` mints a fresh `rev` on every call and the loop looks
    every two seconds."""
    fleet.run()
    before = paths.grant_path(fleet.state_root, FAMILY).read_text(encoding="utf-8")
    fleet.run()
    assert paths.grant_path(fleet.state_root, FAMILY).read_text(encoding="utf-8") == before


def test_a_malformed_grant_file_is_written_again(fleet: Fleet) -> None:
    """The PEP refuses every call on a file it cannot parse (contract 04
    §1.4), exactly as on a missing one."""
    fleet.run()
    paths.grant_path(fleet.state_root, FAMILY).write_text("{not json", encoding="utf-8")
    result = fleet.run()
    assert granted(fleet)["family"] == FAMILY
    assert result.status.state is FamilyState.IN_SYNC


def write_server(registry_root: Path, tools: tuple[str, ...], identity: str = "Search.") -> None:
    """`mcp/kagi/server.yaml`, cut to what a grant reads (contract 01b §8.1)."""
    body = {
        "name": SERVER,
        "identity": identity,
        "install": {"source": "agent-mcp"},
        "run": {"entrypoint": "kagi-mcp", "env": {"KAGI_API_KEY": "secret:kagi_api_key"}},
        "tools": [{"name": tool, "description": f"{tool}."} for tool in tools],
    }
    directory = registry_root / "mcp" / SERVER
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "server.yaml").write_text(yaml.safe_dump(body), encoding="utf-8")


def granted(fleet: Fleet) -> dict[str, object]:
    return json.loads(paths.grant_path(fleet.state_root, FAMILY).read_text(encoding="utf-8"))


def test_a_tool_a_server_file_drops_leaves_an_all_grant(fleet: Fleet) -> None:
    """`all` expands against the server file at read time (contract 01 §3.4
    rule 4), so a server file moves the grant while the family file stays
    put. A registry change dropped `merge_pull_request` from
    `github-platform`, and the live grant kept it: invariant 9's removal
    never applied."""
    write_server(fleet.registry_root, ("search", "extract"))
    fleet.write(tools={SERVER: "all"})
    fleet.run()
    write_server(fleet.registry_root, ("search",))
    result = fleet.run()
    assert granted(fleet)["tools"] == {SERVER: ["search"]}
    assert result.status.state is FamilyState.IN_SYNC


def test_a_tool_a_server_file_adds_reaches_an_all_grant(fleet: Fleet) -> None:
    write_server(fleet.registry_root, ("search",))
    fleet.write(tools={SERVER: "all"})
    fleet.run()
    write_server(fleet.registry_root, ("search", "extract"))
    fleet.run()
    assert granted(fleet)["tools"] == {SERVER: ["search", "extract"]}


def test_a_server_edit_that_moves_no_grant_rewrites_nothing(fleet: Fleet) -> None:
    """The revision moved, the grant did not, so neither does its `rev`."""
    write_server(fleet.registry_root, ("search",))
    fleet.write(tools={SERVER: "all"})
    fleet.run()
    before = paths.grant_path(fleet.state_root, FAMILY).read_text(encoding="utf-8")
    write_server(fleet.registry_root, ("search",), identity="Search, and nothing else.")
    fleet.run()
    assert paths.grant_path(fleet.state_root, FAMILY).read_text(encoding="utf-8") == before


def test_a_pass_leaves_the_token_digests_to_rotation(fleet: Fleet) -> None:
    """`rotate` runs in its own process and writes `creds.json`, then a
    grant file with two digests (contract 04 §2.3). A pass that read the
    credentials before that must not write the second digest away, so a
    pass compares the grants and never the digests."""
    fleet.run()
    body = granted(fleet)
    overlap = [*body["token_sha256"], "0" * 64]  # type: ignore[misc]
    body["token_sha256"] = overlap
    paths.grant_path(fleet.state_root, FAMILY).write_text(json.dumps(body), encoding="utf-8")
    fleet.run()
    assert granted(fleet)["token_sha256"] == overlap


def test_a_shell_change_rewrites_the_config_mount(fleet: Fleet) -> None:
    fleet.run()
    fleet.write(shell=True)
    fleet.run()
    runtime = json.loads(
        (paths.config_dir(fleet.state_root, FAMILY) / "runtime.json").read_text(encoding="utf-8")
    )
    assert runtime["shell"] is True


def test_a_system_prompt_change_rewrites_the_config_mount(fleet: Fleet) -> None:
    fleet.run()
    fleet.write(system_prompt="replace")
    fleet.run()
    runtime = json.loads(
        (paths.config_dir(fleet.state_root, FAMILY) / "runtime.json").read_text(encoding="utf-8")
    )
    assert runtime["system_prompt"] == "replace"


def test_an_instructions_edit_rewrites_the_config_mount(fleet: Fleet) -> None:
    fleet.run()
    (fleet.registry_root / "families" / FAMILY / "instructions.md").write_text(
        "Be brief.\n", encoding="utf-8"
    )
    result = fleet.run()
    config = paths.config_dir(fleet.state_root, FAMILY) / "instructions.md"
    assert config.read_text(encoding="utf-8") == "Be brief.\n"
    assert result.status.state is FamilyState.IN_SYNC


# --- the replace axis ------------------------------------------------------------


def test_more_cpus_builds_a_second_sandbox_and_drains_the_first(fleet: Fleet) -> None:
    """Contract 05 §5.4: an addition drains, so running turns finish."""
    fleet.run()
    fleet.forget_calls()
    fleet.write(sandbox={"cpus": 4})
    result = fleet.run()
    assert fleet.ops() == ("create", "set_egress", "assert_egress", "destroy")
    assert fleet.switch.requests[-1].mode is SwitchMode.DRAIN
    assert result.ran[-2:] == (SWITCH_STEP, DESTROY_STEP)
    assert fleet.live_ids() == ("chat-s2",)


def test_fewer_cpus_interrupts_instead_of_draining(fleet: Fleet) -> None:
    """A removal applies at once (invariant 9). A turn holding the reach the
    edit removed must not be allowed to finish (§5.3 rule 3)."""
    fleet.write(sandbox={"cpus": 4})
    fleet.run()
    fleet.write(sandbox={"cpus": 2})
    fleet.run()
    # The last call is the replacement's. The first is the promote every
    # family's first sandbox needs (§4.3 step 7).
    assert fleet.switch.requests[-1].mode is SwitchMode.INTERRUPT


def test_the_replacement_is_built_before_the_switch_is_asked_for(fleet: Fleet) -> None:
    """Make before break. `sessiond` may only be told to move onto a
    sandbox whose egress is already proved (§4.3 step 2)."""
    fleet.run()
    fleet.forget_calls()
    seen: list[str] = []

    class RecordingSwitch(FakeSwitchClient):
        def switch(self, request: SwitchRequest) -> object:
            seen.extend(fleet.ops())
            return super().switch(request)

    fleet.switch = RecordingSwitch()
    fleet.write(sandbox={"cpus": 4})
    fleet.run()
    assert seen == ["create", "set_egress", "assert_egress"]


def test_the_document_names_the_replacement_before_the_call(fleet: Fleet) -> None:
    """Contract 05 §4.3 step 5b. The status document is the only thing that
    crosses between the two services (§1), so a `to` it does not name is a
    `to` `sessiond` will not dial, and §5.3 rule 8 refuses the switch."""
    fleet.run()
    published: list[list[str]] = []

    class RecordingSwitch(FakeSwitchClient):
        def switch(self, request: SwitchRequest) -> object:
            published.append(_sandbox_ids(fleet.state_root))
            return super().switch(request)

    fleet.switch = RecordingSwitch()
    fleet.write(sandbox={"cpus": 4})
    fleet.run()

    assert published == [["chat-s1", "chat-s2"]]


def _sandbox_ids(state_root: Path) -> list[str]:
    """Every sandbox the published status document names, in file order."""
    body = json.loads(paths.status_path(state_root, FAMILY).read_text(encoding="utf-8"))
    return [str(one["id"]) for one in body["sandboxes"]]


def test_the_replacement_is_ready_once_the_switch_answers(fleet: Fleet) -> None:
    """Contract 05 §5.3 rule 8 runs the incoming handshake BEFORE anything
    moves, so an answer that switched is the same evidence §4.3 step 7 asks
    for. The replacement must not inherit `creating` from its create."""
    fleet.run()
    fleet.write(sandbox={"cpus": 4})

    result = fleet.run()

    row = result.status.sandboxes[0]
    assert (row.id, str(row.state)) == ("chat-s2", "ready")
    assert row.ready_at is not None


def test_the_replacement_carries_the_same_reach_as_the_outgoing_one(fleet: Fleet) -> None:
    fleet.write(egress=[DOCS])
    fleet.run()
    fleet.write(egress=[DOCS], sandbox={"cpus": 4})
    fleet.run()
    assert fleet.allow_of("chat-s2") == (*planes(), DOCS)


def test_a_refused_switch_keeps_the_outgoing_sandbox(fleet: Fleet) -> None:
    """`sessiond` owns the sessions. Destroying the sandbox it still sends
    every turn to would end every one of them (§5.3 rule 1)."""
    fleet.run()
    fleet.switch = FakeSwitchClient(refuse="sessiond answered 503")
    fleet.write(sandbox={"cpus": 4})
    result = fleet.run()
    assert set(fleet.live_ids()) == {"chat-s1", "chat-s2"}
    assert "destroy" not in fleet.ops()
    assert result.status.state is FamilyState.RECONCILING


def test_adopting_a_replacement_rewrites_its_env_file(fleet: Fleet) -> None:
    """Contract 05 §4.1.1 rule 4: a sandbox without the file publishes no
    path, and `sessiond` refuses every turn on it. A create writes it once,
    and `managerd` is its only writer, so the pass that adopts a sandbox an
    earlier pass left behind has to put it back."""
    fleet.run()
    fleet.switch = FakeSwitchClient(refuse="sessiond answered 503")
    fleet.write(sandbox={"cpus": 4})
    fleet.run()

    env_path = paths.playpen_env_path(fleet.state_root, FAMILY, "chat-s2")
    env_path.unlink()
    fleet.switch = FakeSwitchClient()
    fleet.run()

    assert "AGENT_SANDBOX=chat-s2" in env_path.read_text(encoding="utf-8")


def test_a_restart_after_a_refused_switch_converges(fleet: Fleet) -> None:
    """The applied snapshot never moved, so the next pass computes the same
    diff, finds the replacement it already built, and retries the one call
    §5.3 rule 7 makes idempotent."""
    fleet.run()
    fleet.switch = FakeSwitchClient(refuse="sessiond answered 503")
    fleet.write(sandbox={"cpus": 4})
    fleet.run()
    fleet.switch = FakeSwitchClient()
    result = fleet.run()
    assert fleet.live_ids() == ("chat-s2",)
    assert result.status.state is FamilyState.IN_SYNC
    assert fleet.switch.requests[0].outgoing == "chat-s1"
    assert fleet.switch.requests[0].to == "chat-s2"


def test_a_crash_between_the_switch_and_the_destroy_converges(fleet: Fleet) -> None:
    """Both sandboxes are alive and the snapshot names the old spec. The
    next pass must reuse the replacement it already built, retry the one
    call §5.3 rule 7 makes idempotent, and finish the destroy."""
    first = fleet.run()
    fleet.switch = FakeSwitchClient(refuse="killed before the answer landed")
    fleet.write(sandbox={"cpus": 4})
    fleet.run()
    assert set(fleet.live_ids()) == {"chat-s1", "chat-s2"}

    fleet.switch = FakeSwitchClient()
    result = fleet.run()
    assert fleet.live_ids() == ("chat-s2",)
    assert result.status.applied_rev != first.status.applied_rev
    assert result.status.state is FamilyState.IN_SYNC


def test_a_crash_between_the_destroy_and_the_snapshot_converges(fleet: Fleet) -> None:
    """Only the replacement is alive, and the snapshot still names the old
    spec. The next pass must adopt the survivor rather than build a third."""
    first = fleet.run()
    old_text = family_text(fleet)
    fleet.write(sandbox={"cpus": 4})
    fleet.run()
    roll_back(fleet, rev=first.status.applied_rev, text=old_text)

    fleet.forget_calls()
    result = fleet.run()
    assert fleet.live_ids() == ("chat-s2",)
    assert "create" not in fleet.ops()
    assert result.status.state is FamilyState.IN_SYNC


def test_the_replacement_of_a_narrowed_family_never_carries_the_old_reach(
    fleet: Fleet,
) -> None:
    """One revision that both narrows egress and forces a replacement. At no
    point may a sandbox of this family hold a row the new file dropped."""
    fleet.write(egress=[DOCS])
    fleet.run()
    fleet.write(egress=[], sandbox={"cpus": 4})
    fleet.run()
    assert fleet.allow_of("chat-s2") == planes()
    assert DOCS not in granted_hosts(fleet.driver, "chat-s2")


def test_a_reconciling_family_publishes_the_reconcile_block(fleet: Fleet) -> None:
    fleet.run()
    fleet.switch = FakeSwitchClient(refuse="sessiond answered 503")
    fleet.write(sandbox={"cpus": 4})
    result = fleet.run()
    assert result.status.reconcile is not None
    assert result.status.reconcile["needs_switch"] is True
    assert result.status.reconcile["to_rev"] == result.status.registry_rev


def test_the_published_step_is_one_of_the_contracts_names(fleet: Fleet) -> None:
    """Contract 05 §3.4 fixes eight step names. `ran` carries a refusal's
    reason after a colon for the log line, and that detail may not reach
    the document."""
    fleet.run()
    fleet.switch = FakeSwitchClient(refuse="sessiond answered 503")
    fleet.write(sandbox={"cpus": 4})
    result = fleet.run()
    assert result.status.reconcile is not None
    assert result.status.reconcile["step"] == SWITCH_STEP


def test_an_in_sync_family_publishes_no_reconcile_block(fleet: Fleet) -> None:
    """Contract 05 §2.1: null unless `reconciling`."""
    result = fleet.run()
    assert result.status.reconcile is None


# --- an invalid file, while the family serves ------------------------------------


def test_an_invalid_file_keeps_the_last_good_revision_serving(fleet: Fleet) -> None:
    first = fleet.run()
    fleet.forget_calls()
    fleet.write(kind="nonsense")
    result = fleet.run()
    assert result.status.state is FamilyState.INVALID
    assert result.status.applied_rev == first.status.registry_rev
    assert fleet.ops() == ()
    assert fleet.live_ids() == ("chat-s1",)


def test_an_invalid_file_keeps_publishing_the_last_good_limits(fleet: Fleet) -> None:
    """`sessiond` reads limits from this document. Dropping them would move
    a running family's ceiling for a change that was refused."""
    fleet.write(kind="autonomous", max_running_turns=3, triggers=[{"cron": HOURLY}])
    fleet.run()
    fleet.write(kind="autonomous", max_running_turns="three", triggers=[{"cron": HOURLY}])
    result = fleet.run()
    assert result.status.state is FamilyState.INVALID
    assert result.status.limits.max_running_turns == 3


def test_a_changed_kind_is_refused_and_nothing_moves(fleet: Fleet) -> None:
    """`kind` is immutable (contract 01 §3.1), and only the OLD revision
    proves it, so no single-file check can catch this."""
    first = fleet.run()
    fleet.forget_calls()
    fleet.write(kind="autonomous", triggers=[{"cron": HOURLY}])
    result = fleet.run()
    assert result.status.state is FamilyState.INVALID
    assert result.status.applied_rev == first.status.registry_rev
    assert fleet.ops() == ()


# --- a timer nothing could enable -------------------------------------------------


def test_a_timer_that_will_not_enable_raises_a_fault(fleet: Fleet) -> None:
    """A generated timer that is not enabled never fires, and nothing else
    says so."""
    fleet.units.refuse.add("agent-trigger-chat-t1.timer")
    fleet.write(kind="autonomous", triggers=[{"cron": HOURLY}])

    result = fleet.run()

    faults = [one for one in result.status.faults if one.code == "timer_enable_failed"]
    assert len(faults) == 1
    assert faults[0].detail["timers"] == ["agent-trigger-chat-t1.timer"]
    assert result.status.state is FamilyState.DEGRADED


def test_a_timer_that_will_not_enable_does_not_stop_turns(fleet: Fleet) -> None:
    """A schedule that does not fire is not a permission. Blocking every
    turn over it would take a family off the air for a missed wake."""
    fleet.units.refuse.add("agent-trigger-chat-t1.timer")
    fleet.write(kind="autonomous", triggers=[{"cron": HOURLY}])

    result = fleet.run()

    assert not result.status.faults[0].blocks_turns


def test_an_enabled_timer_raises_nothing(fleet: Fleet) -> None:
    fleet.write(kind="autonomous", triggers=[{"cron": HOURLY}])
    result = fleet.run()
    assert not result.status.faults


# --- a failed create --------------------------------------------------------------


def test_a_failed_create_degrades_and_burns_its_id(fleet: Fleet) -> None:
    fleet.driver = FailingCreate()
    result = fleet.run()
    assert result.status.state is FamilyState.DEGRADED
    assert [one.code for one in result.status.faults] == ["sandbox_start_failed"]
    assert read_applied(fleet.state_root, FAMILY) is None


def test_the_retry_after_a_failed_create_takes_the_next_id(fleet: Fleet) -> None:
    fleet.driver = FailingCreate()
    fleet.run()
    fleet.driver = FakeDriver()
    result = fleet.run()
    assert fleet.live_ids() == ("chat-s2",)
    assert result.status.state is FamilyState.IN_SYNC


def test_a_failed_replacement_keeps_the_outgoing_sandbox(fleet: Fleet) -> None:
    fleet.run()
    # The first pass's own §5 call promoted chat-s1 (§4.3 step 7). Only what
    # the REPLACEMENT pass asks for matters here.
    fleet.switch.requests.clear()
    fleet.driver = FailingCreate()
    fleet.write(sandbox={"cpus": 4})
    result = fleet.run()
    assert result.status.state is FamilyState.DEGRADED
    assert fleet.switch.requests == []
    assert "chat-s1" in fleet.live_ids()


# --- faults another service wrote -------------------------------------------------


def test_a_sessiond_fault_folds_into_the_document(fleet: Fleet) -> None:
    write_fault_file(fleet.state_root, "sessiond", "protocol_mismatch")
    result = fleet.run()
    assert [one.code for one in result.status.faults] == ["protocol_mismatch"]
    assert result.status.state is FamilyState.DEGRADED


def test_a_pep_fault_about_the_current_grant_file_degrades(fleet: Fleet) -> None:
    """Contract 05 section 3.3.1 rule 9 drops a complaint about a grant file
    that is gone. A complaint about the one on disk stops turns, as section
    3.3's table says."""
    fleet.run()
    written_at = paths.grant_path(fleet.state_root, FAMILY).stat().st_mtime
    write_fault_file(fleet.state_root, "pep", "grants_stale", since=written_at + 5)

    result = fleet.run()

    assert [one.code for one in result.status.faults] == ["grants_stale"]
    assert result.status.state is FamilyState.DEGRADED


def test_a_fault_older_than_the_grant_file_is_not_current(fleet: Fleet) -> None:
    """`grants_stale` stops every turn. A complaint from before the grant
    file was written is not about that file, and publishing it would hold
    the family down for nothing."""
    write_fault_file(fleet.state_root, "pep", "grants_stale")

    result = fleet.run()

    assert result.status.faults == ()
    assert result.status.state is FamilyState.IN_SYNC


def test_a_cleared_fault_file_clears_the_document(fleet: Fleet) -> None:
    write_fault_file(fleet.state_root, "pep", "grants_stale")
    fleet.run()
    paths.fault_path(fleet.state_root, "pep", FAMILY).unlink()
    result = fleet.run()
    assert result.status.faults == ()
    assert result.status.state is FamilyState.IN_SYNC


# --- spend --------------------------------------------------------------------------


def test_a_pass_that_skips_spend_publishes_none(fleet: Fleet) -> None:
    result = fleet.run()
    assert result.status.spend is None


def test_a_read_pass_publishes_the_window_and_the_budget(fleet: Fleet) -> None:
    result = fleet.run(spend=SpendRead.READ)
    assert result.status.spend is not None
    assert result.status.spend["window"] == "day"
    assert result.status.spend["budget_usd"] == 15.0
    assert result.status.spend["source"] == "litellm"


def test_a_failed_spend_read_reaches_the_document_as_a_fault(fleet: Fleet) -> None:
    """Contract 05 §7 rule 4. A fault the document does not carry is a
    fault nobody sees."""
    fleet.litellm = BlindSpend()
    result = fleet.run(spend=SpendRead.READ)
    assert result.status.spend is None
    codes = [one.code for one in result.status.faults]
    assert codes == ["spend_unknown"]
    assert all(one.blocks_turns is False for one in result.status.faults)


def test_a_litellm_that_is_away_is_a_fault_and_never_the_end_of_the_pass(fleet: Fleet) -> None:
    """An `ai-stack` restart refuses the real client's connection mid-read.
    A pass that cannot read the spend publishes the fault and goes on."""
    fleet.litellm = AwaySpend()
    result = fleet.run(spend=SpendRead.READ)
    assert result.status.spend is None
    assert [one.code for one in result.status.faults] == ["spend_unknown"]
    assert all(one.blocks_turns is False for one in result.status.faults)


class WatchingDriver(FakeDriver):
    """A driver that reads the family's status document from INSIDE the
    create, which is where a reader meets it on the real host: `sbx create`
    runs for one to three minutes."""

    def __init__(self, state_root: Path) -> None:
        super().__init__()
        self._status = paths.status_path(state_root, FAMILY)
        self.seen: list[dict[str, object]] = []

    def create(self, spec: SandboxSpec) -> None:
        if self._status.is_file():
            self.seen.append(json.loads(self._status.read_text(encoding="utf-8")))

        super().create(spec)


NEWER_IMAGE = "127.0.0.1:5000/agent-sandbox@sha256:" + "9" * 64


def test_a_new_image_is_reconciling_while_its_sandbox_is_built(fleet: Fleet) -> None:
    """An image move leaves the registry where it is, so `applied_rev ==
    registry_rev`, and a document written just before the create would say
    `in_sync` with `step` null while the new sandbox is `creating`.
    `rework-cutover.sh up` waits for every family to be `in_sync` and
    `ready` in a fresh document, so it would call the fleet converged before
    the first replacement finished. A pass
    that is inside a step is `reconciling`, whatever moved."""
    fleet.run()
    watching = WatchingDriver(fleet.state_root)
    watching.calls.extend(fleet.driver.calls)
    fleet.driver = watching

    result = fleet.run(image=NEWER_IMAGE)

    assert len(watching.seen) == 1, "the image move built one new sandbox"
    during = watching.seen[0]
    assert during["state"] == "reconciling"
    block = during["reconcile"]
    assert isinstance(block, dict)
    assert block["step"] == "create_sandbox"
    # And the pass ends where it always did.
    assert result.status.state is FamilyState.IN_SYNC
    assert [one.image for one in result.status.sandboxes if one.state == "ready"] == [NEWER_IMAGE]
