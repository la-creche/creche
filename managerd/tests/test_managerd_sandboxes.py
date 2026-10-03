"""The sandbox ledger and the one creation path (contract 05 §4.1 to §4.4).

A failed create burns its id: the counter moves BEFORE `sbx create`, so a
half-built VM is destroyed and never retried under the same name."""

from __future__ import annotations

from pathlib import Path

import pytest
from agent_family import FamilyFile, parse_family
from agent_managerd import paths
from agent_managerd.driver import DriverError, FakeDriver, SandboxSpec
from agent_managerd.egress import EgressConfig, litellm_endpoint, pep_endpoint
from agent_managerd.sandboxes import (
    create_sandbox,
    destroy_sandbox,
    live_record,
    read_ledger,
    sandbox_spec,
    set_allow,
    spec_hash_of,
    write_ledger,
)
from agent_managerd.status import SandboxLifecycle
from managerd_helpers import chat_family

IMAGE = "sha256:deadbeef"


@pytest.fixture
def family() -> FamilyFile:
    return chat_family()


@pytest.fixture
def state_root(tmp_path: Path) -> Path:
    return tmp_path / "state"


def create(
    state_root: Path, family: FamilyFile, driver: FakeDriver, *, image: str = IMAGE
) -> object:
    return create_sandbox(
        family,
        state_root=state_root,
        image=image,
        driver=driver,
        egress=EgressConfig(),
    )


class FailingCreate(FakeDriver):
    """`sbx create` that reports failure after making the VM. That is the
    case the destroy exists for: a create that left nothing behind loses
    nothing by being destroyed anyway."""

    def create(self, spec: SandboxSpec) -> None:
        super().create(spec)
        raise DriverError("simulated sbx outage")


class FailingAssert(FakeDriver):
    def assert_egress(self, name: str, allowed: tuple[str, ...], denied: tuple[str, ...]) -> None:
        super().assert_egress(name, allowed, denied)
        raise DriverError("openrouter.ai is REACHABLE")


# --- a good create ----------------------------------------------------------


def test_the_first_sandbox_is_s1(state_root: Path, family: FamilyFile) -> None:
    driver = FakeDriver()
    outcome = create(state_root, family, driver)
    assert outcome.record is not None
    assert outcome.record.id == "chat-s1"
    assert outcome.fault is None


def test_it_creates_then_allows_then_asserts(state_root: Path, family: FamilyFile) -> None:
    """Contract 05 §4.3 steps 1 to 3, in that order. Asserting before the
    allows would prove nothing about the sandbox that ends up running."""
    driver = FakeDriver()
    create(state_root, family, driver)
    assert driver.ops() == ("create", "set_egress", "assert_egress")


def test_it_writes_the_counter_and_the_ledger(state_root: Path, family: FamilyFile) -> None:
    driver = FakeDriver()
    create(state_root, family, driver)
    assert paths.sandbox_seq_path(state_root, "chat").read_text(encoding="utf-8").strip() == "1"
    records = read_ledger(state_root, "chat")
    assert [one.id for one in records] == ["chat-s1"]
    assert records[0].state is SandboxLifecycle.CREATING


def test_it_writes_playpen_env_for_the_new_id(state_root: Path, family: FamilyFile) -> None:
    driver = FakeDriver()
    outcome = create(state_root, family, driver)
    assert outcome.record is not None
    env_path = paths.playpen_env_path(state_root, "chat", "chat-s1")
    assert env_path.is_file()
    assert "AGENT_SANDBOX=chat-s1" in env_path.read_text(encoding="utf-8")


def test_the_allow_list_carries_both_planes(state_root: Path, family: FamilyFile) -> None:
    driver = FakeDriver()
    outcome = create(state_root, family, driver)
    assert outcome.record is not None
    assert outcome.record.allow[:2] == (litellm_endpoint(), pep_endpoint())


# --- a failed create burns the id -------------------------------------------


def test_a_failed_create_destroys_the_half_built_vm(state_root: Path, family: FamilyFile) -> None:
    driver = FailingCreate()
    outcome = create(state_root, family, driver)
    assert outcome.record is None
    assert outcome.fault is not None
    assert outcome.fault.code == "sandbox_start_failed"
    assert driver.ops() == ("create", "destroy")


def test_a_failed_assert_destroys_the_sandbox(state_root: Path, family: FamilyFile) -> None:
    """The VM exists by then, and it is the one whose egress could not be
    proved. Leaving it would leave a hole in deny-by-default (invariant 11)."""
    driver = FailingAssert()
    outcome = create(state_root, family, driver)
    assert outcome.record is None
    assert driver.ops() == ("create", "set_egress", "assert_egress", "destroy")


def test_a_failed_create_writes_no_playpen_env(state_root: Path, family: FamilyFile) -> None:
    create(state_root, family, FailingCreate())
    assert not paths.playpen_env_path(state_root, "chat", "chat-s1").exists()


def test_a_failed_create_burns_its_id(state_root: Path, family: FamilyFile) -> None:
    """`sbx create` against a name that already exists is unverified
    (contract 05 §10 row 7), so the retry never reuses the name."""
    create(state_root, family, FailingCreate())
    outcome = create(state_root, family, FakeDriver())
    assert outcome.record is not None
    assert outcome.record.id == "chat-s2"


def test_a_failed_create_is_recorded_as_failed(state_root: Path, family: FamilyFile) -> None:
    create(state_root, family, FailingCreate())
    records = read_ledger(state_root, "chat")
    assert [(one.id, one.state) for one in records] == [("chat-s1", SandboxLifecycle.FAILED)]


def test_a_failed_sandbox_is_not_the_live_one(state_root: Path, family: FamilyFile) -> None:
    create(state_root, family, FailingCreate())
    assert live_record(state_root, "chat") is None


# --- the control directory ---------------------------------------------------


def test_a_create_empties_its_own_control_directory(state_root: Path, family: FamilyFile) -> None:
    control = paths.control_dir(state_root, "chat", "chat-s1")
    control.mkdir(parents=True)
    (control / "supervisor.lock").write_text("stale\n", encoding="utf-8")

    create(state_root, family, FakeDriver())

    assert not (control / "supervisor.lock").exists()


def test_a_replacement_leaves_the_live_lock(state_root: Path, family: FamilyFile) -> None:
    """Contract 05 §4.3 rule 5 with contract 03: the directory is
    per sandbox, so emptying the new one cannot touch the lock the outgoing
    sandbox's playpen is still beating."""
    create(state_root, family, FakeDriver())
    live = paths.control_dir(state_root, "chat", "chat-s1") / "supervisor.lock"
    live.write_text("live\n", encoding="utf-8")

    create(state_root, family, FakeDriver())

    assert live.is_file()
    assert paths.control_dir(state_root, "chat", "chat-s2").is_dir()


def test_two_sandboxes_get_two_env_files(state_root: Path, family: FamilyFile) -> None:
    """Contract 03 §7.1 rule 1. One file per family would hand the outgoing
    playpen the incoming sandbox's id for the whole replacement."""
    create(state_root, family, FakeDriver())
    create(state_root, family, FakeDriver())

    first = paths.playpen_env_path(state_root, "chat", "chat-s1")
    second = paths.playpen_env_path(state_root, "chat", "chat-s2")

    assert "AGENT_SANDBOX=chat-s1" in first.read_text(encoding="utf-8")
    assert "AGENT_SANDBOX=chat-s2" in second.read_text(encoding="utf-8")


def test_a_destroy_removes_its_control_directory(state_root: Path, family: FamilyFile) -> None:
    """Contract 05 §4.4 step 5. An id is never reused, so a directory left
    behind is one more per sandbox for ever."""
    driver = FakeDriver()
    outcome = create(state_root, family, driver)
    assert outcome.record is not None

    destroy_sandbox(state_root, "chat", outcome.record, driver)

    assert not paths.control_dir(state_root, "chat", "chat-s1").exists()
    assert not paths.playpen_env_path(state_root, "chat", "chat-s1").exists()


# --- destroy ------------------------------------------------------------------


def test_destroy_removes_the_rows_then_the_vm(state_root: Path, family: FamilyFile) -> None:
    driver = FakeDriver()
    outcome = create(state_root, family, driver)
    assert outcome.record is not None

    destroy_sandbox(state_root, "chat", outcome.record, driver)

    assert driver.calls[-1].op == "destroy"
    assert driver.calls[-1].args == ("chat-s1", outcome.record.allow)
    assert read_ledger(state_root, "chat") == ()


def test_destroy_keeps_the_counter(state_root: Path, family: FamilyFile) -> None:
    """An id is never reused, even after its sandbox is gone (§4.1)."""
    driver = FakeDriver()
    outcome = create(state_root, family, driver)
    assert outcome.record is not None
    destroy_sandbox(state_root, "chat", outcome.record, driver)
    assert paths.sandbox_seq_path(state_root, "chat").read_text(encoding="utf-8").strip() == "1"


# --- spec hash ----------------------------------------------------------------


def test_the_spec_hash_moves_with_the_image(family: FamilyFile) -> None:
    assert spec_hash_of(family, "sha256:one") != spec_hash_of(family, "sha256:two")


def test_the_spec_hash_moves_with_the_resources(family: FamilyFile) -> None:
    bigger = chat_family(sandbox={"cpus": 8, "memory": "8g"})
    assert spec_hash_of(family, IMAGE) != spec_hash_of(bigger, IMAGE)


def test_the_spec_hash_moves_with_the_mounts(family: FamilyFile) -> None:
    mounted = chat_family(files=[{"path": "/srv/agents/vault", "mode": "rw"}])
    assert spec_hash_of(family, IMAGE) != spec_hash_of(mounted, IMAGE)


def test_the_spec_hash_ignores_a_live_field(family: FamilyFile) -> None:
    """`spec_hash` covers exactly the fields that force a replacement
    (§4.1). A description is not one of them."""
    reworded = chat_family(description="Reworded.")
    assert spec_hash_of(family, IMAGE) == spec_hash_of(reworded, IMAGE)


# --- the spec itself ------------------------------------------------------------


def test_the_spec_puts_sessions_first_read_write(state_root: Path, family: FamilyFile) -> None:
    spec = sandbox_spec("chat-s1", family, state_root, IMAGE)
    assert spec.mounts[0].path == "/srv/agents/sessions/chat"
    assert spec.mounts[0].readonly is False


def test_the_ledger_round_trips(state_root: Path, family: FamilyFile) -> None:
    driver = FakeDriver()
    outcome = create(state_root, family, driver)
    assert outcome.record is not None
    write_ledger(state_root, "chat", (outcome.record,))
    assert read_ledger(state_root, "chat") == (outcome.record,)


def test_an_unreadable_ledger_reads_empty(state_root: Path) -> None:
    path = paths.sandboxes_path(state_root, "chat")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    assert read_ledger(state_root, "chat") == ()


# --- a live egress edit moves the rows a destroy must remove --------------------


def test_set_allow_records_the_rows_a_live_sandbox_now_carries(
    state_root: Path, family: FamilyFile
) -> None:
    """Contract 05 §4.4 step 3 removes exactly `record.allow`. An egress
    edit lands on the running sandbox (§3.5), so a record left unwritten
    would leak every row the edit added."""
    create(state_root, family, FakeDriver())
    allow = (litellm_endpoint(), pep_endpoint(), "docs.python.org:443")
    set_allow(state_root, "chat", allow)
    record = live_record(state_root, "chat")
    assert record is not None
    assert record.allow == allow


def test_set_allow_leaves_a_burnt_id_alone(state_root: Path, family: FamilyFile) -> None:
    """A `failed` sandbox carries the rows its own create granted. Rewriting
    them with a later family's list would make a cleanup remove the wrong
    rows."""
    create(state_root, family, FailingCreate())
    set_allow(state_root, "chat", ("only-this",))
    burnt = read_ledger(state_root, "chat")[0]
    assert burnt.state is SandboxLifecycle.FAILED
    assert burnt.allow == EgressConfig().allowed(family.egress)


def test_a_family_file_that_will_not_parse_is_the_tests_problem() -> None:
    """Guards the helper itself: every fixture above assumes a parse."""
    family, _ = parse_family("name: chat\n")
    assert family is None
