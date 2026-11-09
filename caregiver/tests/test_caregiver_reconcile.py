"""One reconcile pass over one family (contract 05).

Invariant 9 is what these tests hold in place: "A permission change needs
one action and no manual apply. It never ends a session. A removal applies
at once." Each test drives one family file to a second revision and asks
what the fakes and the files saw. A pass starts and stops no VM (decision
CH1): the vmm reads the files the pass writes."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

import httpx
import pytest
from agent_family import FamilyState, load_registry
from agent_family.registry import FAMILY_FILE
from caregiver.applied import read_applied
from caregiver.clock import seconds_from_now
from caregiver.litellm_keys import FakeLiteLLMKeys, HttpLiteLLMKeys, LiteLLMError, Spend
from caregiver.reach_files import EGRESS_CAP
from caregiver.reconcile import (
    Actors,
    FamilyNotFoundError,
    ReconcileResult,
    SpendRead,
    reconcile_family,
)
from caregiver.status import ReconcileStep
from caregiver.timers import FakeUnits
from caregiver_helpers import (
    fake_policy,
    fragment_table,
    toml_text,
    write_no_file_dir,
    write_registry,
)

from caregiver import paths

FAMILY: str = "chat"


DOCS: str = "docs.python.org:443"
HOURLY: str = "0 * * * *"
SERVER: str = "kagi"


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
    """One family's whole world: a registry, a state root, two fakes."""

    registry_root: Path
    state_root: Path
    litellm: FakeLiteLLMKeys = field(default_factory=FakeLiteLLMKeys)
    units: FakeUnits = field(default_factory=FakeUnits)

    def write(self, **overrides: object) -> None:
        write_registry(self.registry_root, **overrides)

    def run(self, *, spend: SpendRead = SpendRead.SKIP) -> ReconcileResult:
        actors = Actors(self.litellm, self.units, fake_policy(self.state_root))
        return reconcile_family(
            load_registry(self.registry_root),
            FAMILY,
            state_root=self.state_root,
            actors=actors,
            spend=spend,
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


def family_text(fleet: Fleet) -> str:
    return (fleet.registry_root / "families" / FAMILY / FAMILY_FILE).read_text(encoding="utf-8")


# --- a first pass brings the family up ------------------------------------------


def test_a_first_pass_reaches_in_sync(fleet: Fleet) -> None:
    result = fleet.run()
    assert result.status.state is FamilyState.IN_SYNC
    assert "sandboxes" not in result.status.as_json()


def test_a_first_pass_records_the_revision_it_applied(fleet: Fleet) -> None:
    result = fleet.run()
    applied = read_applied(fleet.state_root, FAMILY)
    assert applied is not None
    assert applied.rev == result.status.registry_rev


def test_an_unchanged_second_pass_touches_nothing(fleet: Fleet) -> None:
    fleet.run()
    result = fleet.run()
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


def test_an_egress_edit_is_the_step_set_egress(fleet: Fleet) -> None:
    """Contract 05 §3.4: `set_egress` writes `egress.json` and `repos.json`.
    The vmm proxy reads the file on every CONNECT, so no VM is replaced
    (§3.5)."""
    fleet.run()
    fleet.write(egress=[DOCS])
    result = fleet.run()
    assert "set_egress" in result.ran
    assert result.status.state is FamilyState.IN_SYNC


def test_a_repos_edit_is_the_step_set_egress(fleet: Fleet) -> None:
    fleet.run()
    fleet.write(repos=["creche"])
    result = fleet.run()
    assert "set_egress" in result.ran


def test_a_pass_writes_the_egress_file(fleet: Fleet) -> None:
    """Contract 01 §6.1.1, decision CH69 rule 6: every pass."""
    fleet.run()
    egress_path = paths.egress_path(fleet.state_root, FAMILY)
    assert json.loads(egress_path.read_bytes()) == {"hosts": []}
    fleet.write(egress=[DOCS])
    fleet.run()
    assert json.loads(egress_path.read_bytes()) == {
        "hosts": [{"host": "docs.python.org", "port": 443}]
    }


def test_a_pass_writes_the_repos_file(fleet: Fleet) -> None:
    """Contract 01 §6.1.1, decisions CH69 rules 4 and 6: every pass, in
    the sorted form."""
    fleet.run()
    repos_path = paths.repos_path(fleet.state_root, FAMILY)
    assert json.loads(repos_path.read_bytes()) == {"repos": []}
    fleet.write(repos=["mcp", "creche"])
    fleet.run()
    assert json.loads(repos_path.read_bytes()) == {"repos": ["creche", "mcp"]}


class Damage(StrEnum):
    """What happens to a reach file between two passes."""

    DELETED = "deleted"
    EDITED = "edited"


@pytest.mark.parametrize("damage", list(Damage))
@pytest.mark.parametrize(
    "reach_path", [paths.egress_path, paths.repos_path], ids=["egress.json", "repos.json"]
)
def test_a_pass_with_no_change_restores_a_reach_file(
    fleet: Fleet, reach_path: Callable[[Path, str], Path], damage: Damage
) -> None:
    """Decision CH69 rule 6: caregiver writes each reach file on every
    pass, not only on a pass that moves the family. The second pass has no
    registry change, so a write gated on a step of the diff leaves the
    damaged file."""
    fleet.write(egress=[DOCS], repos=["creche"])
    fleet.run()
    reach = reach_path(fleet.state_root, FAMILY)
    canonical = reach.read_bytes()
    match damage:
        case Damage.DELETED:
            reach.unlink()
        case Damage.EDITED:
            reach.write_bytes(b"{}\n")
    fleet.run()
    assert reach.read_bytes() == canonical


def test_an_invalid_edit_keeps_the_egress_file(fleet: Fleet) -> None:
    """Invariant 19: the last good definition keeps serving."""
    fleet.write(egress=[DOCS])
    fleet.run()
    egress_path = paths.egress_path(fleet.state_root, FAMILY)
    before = egress_path.read_bytes()
    fleet.write(egress=["*.python.org"])
    fleet.run()
    assert egress_path.read_bytes() == before


@pytest.mark.parametrize(
    "repos",
    [["creche", "creche"], [f"r{index}" for index in range(33)]],
    ids=["a name twice", "past the count cap"],
)
def test_an_invalid_repos_edit_keeps_the_repos_file(fleet: Fleet, repos: list[str]) -> None:
    """Decision CH73 rule 3: caregiver refuses a name given twice before it
    writes the file, and decision CH69 rule 15 caps the list at 32. The
    family reads `invalid`, and the last good file stays (invariant 19)."""
    fleet.write(repos=["creche"])
    fleet.run()
    repos_path = paths.repos_path(fleet.state_root, FAMILY)
    before = repos_path.read_bytes()
    fleet.write(repos=repos)
    result = fleet.run()
    assert result.status.state is FamilyState.INVALID
    assert repos_path.read_bytes() == before


def landed_bytes(fleet: Fleet) -> dict[Path, bytes]:
    """Every file a later step of the pass writes: the config mount, the
    fragments of `policy.toml`, the chaperone directory and both reach
    files."""
    roots = (
        paths.config_dir(fleet.state_root, FAMILY),
        paths.policy_fragments_dir(fleet.state_root),
        fake_policy(fleet.state_root).target.config_dir,
    )
    found = {one: one.read_bytes() for root in roots for one in root.rglob("*") if one.is_file()}
    for reach in (paths.egress_path, paths.repos_path):
        path = reach(fleet.state_root, FAMILY)
        found[path] = path.read_bytes()

    return found


def test_an_egress_list_over_the_cap_is_refused(fleet: Fleet) -> None:
    """Decision CH71: the vmm would allow nothing for a file over its cap,
    so the revision reads `invalid` with one error that names the cap.
    The refusal comes before any write, so the last good files stay byte
    for byte and no step runs (invariant 19). The revision also moves the
    model and grants a tool. Each one lands in a later step: the key
    update, `runtime.json` and the table of `policy.toml`. A refusal placed
    after any of those steps lets that change land, and this test fails."""
    write_server(fleet.registry_root, ("search",))
    fleet.write(egress=[DOCS])
    first = fleet.run()
    before = landed_bytes(fleet)
    budgets = dict(fleet.litellm.budgets)
    fleet.write(
        egress=[f"h{index:05}.example.com" for index in range(1000)],
        model={"router": "agent-fast", "budget_usd_per_day": 5},
        tools={SERVER: ["search"]},
    )
    result = fleet.run()
    assert result.status.state is FamilyState.INVALID
    assert result.status.applied_rev == first.status.registry_rev
    assert result.status.validation.error_count == 1
    first_error = result.status.validation.first_error
    assert first_error is not None
    assert first_error.endswith(f"the cap is {EGRESS_CAP}")
    assert landed_bytes(fleet) == before
    assert fleet.litellm.budgets == budgets


# --- the live axes: model and budget ---------------------------------------------


def test_a_model_change_updates_the_key(fleet: Fleet) -> None:
    fleet.run()
    fleet.write(model={"router": "agent-fast", "budget_usd_per_day": 15})
    fleet.run()
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


def fragment_text(fleet: Fleet) -> str:
    return paths.policy_fragment_path(fleet.state_root, FAMILY).read_text(encoding="utf-8")


def test_a_tool_grant_rewrites_the_fragment(fleet: Fleet) -> None:
    write_server(fleet.registry_root, ("search",))
    fleet.run()
    before = fragment_text(fleet)
    fleet.write(tools={SERVER: ["search"]})
    fleet.run()
    after = fragment_text(fleet)
    assert SERVER not in before
    assert SERVER in after


def test_a_missing_fragment_is_written_again(fleet: Fleet) -> None:
    """Without the table the chaperone refuses every call for this family,
    and no diff would ever ask for it: the family file did not move."""
    fleet.run()
    paths.policy_fragment_path(fleet.state_root, FAMILY).unlink()
    result = fleet.run()
    assert paths.policy_fragment_path(fleet.state_root, FAMILY).is_file()
    assert result.status.state is FamilyState.IN_SYNC


def test_an_unchanged_pass_does_not_rewrite_the_fragment(fleet: Fleet) -> None:
    fleet.run()
    before = fragment_text(fleet)
    fleet.run()
    assert fragment_text(fleet) == before


def test_a_malformed_fragment_is_written_again(fleet: Fleet) -> None:
    fleet.run()
    paths.policy_fragment_path(fleet.state_root, FAMILY).write_text("{not toml", encoding="utf-8")
    result = fleet.run()
    assert fragment_table(fleet.state_root, FAMILY)["token_sha256"]
    assert result.status.state is FamilyState.IN_SYNC


def write_server(registry_root: Path, tools: tuple[str, ...], identity: str = "Search.") -> None:
    """`mcp/kagi/server.toml`, cut to what a grant reads (contract 01b §8.1)."""
    body = {
        "name": SERVER,
        "identity": identity,
        "install": {"source": "agent-mcp"},
        "run": {"entrypoint": "kagi-mcp", "env": {"KAGI_API_KEY": "secret:kagi_api_key"}},
        "tools": [{"name": tool, "description": f"{tool}."} for tool in tools],
    }
    directory = registry_root / "mcp" / SERVER
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "server.toml").write_text(toml_text(body), encoding="utf-8")


def granted_tools(fleet: Fleet) -> set[str]:
    """The tools of the server that the table of the family grants."""
    return set(fragment_table(fleet.state_root, FAMILY)["grants"][SERVER]["tools"])


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
    assert granted_tools(fleet) == {"search"}
    assert result.status.state is FamilyState.IN_SYNC


def test_a_tool_a_server_file_adds_reaches_an_all_grant(fleet: Fleet) -> None:
    write_server(fleet.registry_root, ("search",))
    fleet.write(tools={SERVER: "all"})
    fleet.run()
    write_server(fleet.registry_root, ("search", "extract"))
    fleet.run()
    assert granted_tools(fleet) == {"search", "extract"}


def test_a_server_edit_that_moves_no_grant_rewrites_nothing(fleet: Fleet) -> None:
    """The revision moved, the grant did not, so the fragment stays."""
    write_server(fleet.registry_root, ("search",))
    fleet.write(tools={SERVER: "all"})
    fleet.run()
    before = fragment_text(fleet)
    write_server(fleet.registry_root, ("search",), identity="Search, and nothing else.")
    fleet.run()
    assert fragment_text(fleet) == before


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


@pytest.mark.parametrize("name", ["instructions.md", "SKILL.md"])
def test_a_registry_file_that_is_not_text_reads_as_an_empty_file(fleet: Fleet, name: str) -> None:
    """A pass must not raise on the content of a file. Bytes that are not
    UTF-8 read as a file that the pass cannot open: empty."""
    skill = fleet.registry_root / "skills" / "notes"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("Take notes.\n", encoding="utf-8")
    fleet.write(skills=["notes"])
    fleet.run()
    source = {
        "instructions.md": fleet.registry_root / "families" / FAMILY / "instructions.md",
        "SKILL.md": skill / "SKILL.md",
    }
    written = {
        "instructions.md": paths.config_dir(fleet.state_root, FAMILY) / "instructions.md",
        "SKILL.md": paths.config_dir(fleet.state_root, FAMILY) / "skills" / "notes" / "SKILL.md",
    }
    source[name].write_bytes(b"Be brief. \xff\n")

    result = fleet.run()

    assert written[name].read_bytes() == b""
    assert result.status.state is FamilyState.IN_SYNC


def test_a_directory_with_no_family_file_has_no_pass(fleet: Fleet) -> None:
    """Contract 01 §5.6 rule 3: `caregiver` ignores the directory. The
    caller gets the answer for a name that the registry does not hold, and
    the pass writes nothing."""
    write_no_file_dir(fleet.registry_root)
    actors = Actors(fleet.litellm, fleet.units, fake_policy(fleet.state_root))

    with pytest.raises(FamilyNotFoundError):
        reconcile_family(
            load_registry(fleet.registry_root),
            "stray",
            state_root=fleet.state_root,
            actors=actors,
        )

    assert not paths.family_dir(fleet.state_root, "stray").exists()


# --- the shape and the reconcile block -------------------------------------------


def test_a_shape_change_starts_no_vm(fleet: Fleet) -> None:
    """Contract 05 §3.5: a new shape reaches a VM at its next start. The
    vmm starts and stops each VM (decision CH1), so the pass converges."""
    fleet.run()
    fleet.write(sandbox={"cpus": 4})
    result = fleet.run()
    assert result.status.state is FamilyState.IN_SYNC
    assert result.status.reconcile is None


def test_a_reconciling_family_publishes_the_reconcile_block(fleet: Fleet) -> None:
    """A model change is `reconciling` until the LiteLLM cache turns over
    (contract 05 §7 rule 6). caregiver switches no VM (decision CH21-1)."""
    fleet.run()
    fleet.write(model={"router": "agent-fast", "budget_usd_per_day": 15})
    result = fleet.run()
    assert result.status.reconcile is not None
    assert "needs_switch" not in result.status.reconcile.as_json()
    assert result.status.reconcile.to_rev == result.status.registry_rev


def test_the_published_step_is_the_last_step_the_pass_ran(fleet: Fleet) -> None:
    """Contract 05 §3.4 fixes the step names (`ReconcileStep`). A model
    change updates the key and rewrites `runtime.json`."""
    fleet.run()
    fleet.write(model={"router": "agent-fast", "budget_usd_per_day": 15})
    result = fleet.run()
    assert result.ran == (ReconcileStep.UPDATE_KEY, ReconcileStep.WRITE_CONFIG)
    assert result.status.reconcile is not None
    assert result.status.reconcile.step is ReconcileStep.WRITE_CONFIG


def test_an_in_sync_family_publishes_no_reconcile_block(fleet: Fleet) -> None:
    """Contract 05 §2.1: null unless `reconciling`."""
    result = fleet.run()
    assert result.status.reconcile is None


# --- an invalid file, while the family serves ------------------------------------


def test_an_invalid_file_keeps_the_last_good_revision_serving(fleet: Fleet) -> None:
    first = fleet.run()
    fleet.write(kind="nonsense")
    result = fleet.run()
    assert result.status.state is FamilyState.INVALID
    assert result.status.applied_rev == first.status.registry_rev
    assert result.ran == ()


def test_an_invalid_file_keeps_publishing_the_last_good_limits(fleet: Fleet) -> None:
    """`attendance` reads limits from this document. Dropping them would move
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
    fleet.write(kind="autonomous", triggers=[{"cron": HOURLY}])
    result = fleet.run()
    assert result.status.state is FamilyState.INVALID
    assert result.status.applied_rev == first.status.registry_rev
    assert result.ran == ()


# --- a timer nothing could enable -------------------------------------------------


def test_a_timer_that_will_not_enable_raises_a_fault(fleet: Fleet) -> None:
    """A generated timer that is not enabled never fires, and nothing else
    says so."""
    fleet.units.refuse.add("creche-door-trigger-chat-t1.timer")
    fleet.write(kind="autonomous", triggers=[{"cron": HOURLY}])

    result = fleet.run()

    faults = [one for one in result.status.faults if one.code == "timer_enable_failed"]
    assert len(faults) == 1
    assert faults[0].detail["timers"] == ["creche-door-trigger-chat-t1.timer"]
    assert result.status.state is FamilyState.DEGRADED


def test_a_timer_that_will_not_enable_does_not_stop_turns(fleet: Fleet) -> None:
    """A schedule that does not fire is not a permission. Blocking every
    turn over it would take a family off the air for a missed wake."""
    fleet.units.refuse.add("creche-door-trigger-chat-t1.timer")
    fleet.write(kind="autonomous", triggers=[{"cron": HOURLY}])

    result = fleet.run()

    assert not result.status.faults[0].blocks_turns


def test_an_enabled_timer_raises_nothing(fleet: Fleet) -> None:
    fleet.write(kind="autonomous", triggers=[{"cron": HOURLY}])
    result = fleet.run()
    assert not result.status.faults


# --- faults another service wrote -------------------------------------------------


def test_a_attendance_fault_folds_into_the_document(fleet: Fleet) -> None:
    write_fault_file(fleet.state_root, "attendance", "vmm_protocol_mismatch")
    result = fleet.run()
    assert [one.code for one in result.status.faults] == ["vmm_protocol_mismatch"]
    assert result.status.state is FamilyState.DEGRADED


def test_a_vmm_fault_folds_into_the_document(fleet: Fleet) -> None:
    """Contract 05 §3.3.1: the vmm is the third writer (decision CH21-1).
    Its fault makes the family `degraded` and stops no turn (decision CH82
    rule 2)."""
    write_fault_file(fleet.state_root, "vmm", "serial_log_full")
    result = fleet.run()
    assert [(one.code, one.source) for one in result.status.faults] == [("serial_log_full", "vmm")]
    assert not any(one.blocks_turns for one in result.status.faults)
    assert result.status.state is FamilyState.DEGRADED


def test_a_pep_fault_folds_into_the_document(fleet: Fleet) -> None:
    """A fault of the PEP stops turns, as section 3.3's table says."""
    fleet.run()
    write_fault_file(fleet.state_root, "pep", "grants_stale")

    result = fleet.run()

    assert [one.code for one in result.status.faults] == ["grants_stale"]
    assert result.status.state is FamilyState.DEGRADED


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
    """A `creche-stack` restart refuses the real client's connection mid-read.
    A pass that cannot read the spend publishes the fault and goes on."""
    fleet.litellm = AwaySpend()
    result = fleet.run(spend=SpendRead.READ)
    assert result.status.spend is None
    assert [one.code for one in result.status.faults] == ["spend_unknown"]
    assert all(one.blocks_turns is False for one in result.status.faults)


# --- the applied copy has the name of the registry file ----------------------------


@pytest.mark.parametrize("name", ["family.json", "other.toml", "family.yaml"])
def test_a_registry_file_of_another_name_moves_no_copy(fleet: Fleet, name: str) -> None:
    """Only a registry file named `family.toml` gives a copy. Another name
    gives no copy. The pass is then not done: the snapshot stays, and the
    status document names the old revision."""
    fleet.run()
    fleet.write(description="Changed.")
    registry = load_registry(fleet.registry_root)
    report = replace(registry.reports[FAMILY], file=f"families/{FAMILY}/{name}")
    actors = Actors(fleet.litellm, fleet.units, fake_policy(fleet.state_root))

    reconcile_family(
        replace(registry, reports={**registry.reports, FAMILY: report}),
        FAMILY,
        state_root=fleet.state_root,
        actors=actors,
    )

    applied = read_applied(fleet.state_root, FAMILY)
    assert applied is not None
    assert applied.family.description == "Test family."
    copies = sorted(one.name for one in paths.applied_dir(fleet.state_root, FAMILY).iterdir())
    assert copies == [paths.APPLIED_META_FILE, FAMILY_FILE]
    body = json.loads(paths.status_path(fleet.state_root, FAMILY).read_text(encoding="utf-8"))
    assert body["state"] == str(FamilyState.RECONCILING)
    assert body["applied_rev"] == applied.rev
    assert body["applied_rev"] != body["registry_rev"]
