"""`caregiver serve | reconcile-once | rotate`.

Every one of them mutates, so every one keeps the `--write` gate. These
tests hold that gate and the wiring behind it: a plan run must reach no
client at all, not even to read the `attendance` token file."""

from __future__ import annotations

from pathlib import Path

import pytest
from agent_family import FamilyState, load_registry
from caregiver.cli import EXIT_OK, EXIT_PROBLEM, EXIT_USAGE, main
from caregiver.credentials import read_creds, token_sha256
from caregiver.driver import FakeDriver
from caregiver.litellm_keys import FakeLiteLLMKeys, key_alias
from caregiver.switch import FakeSwitchClient
from caregiver.timers import FakeUnits
from caregiver_helpers import (
    KIND_MOVED,
    REFUSED_TOOLS,
    accepted_digests,
    current_digest,
    grants_alone,
    published,
    published_epoch,
    write_no_file_dir,
    write_registry,
)

from caregiver import cli, paths

IMAGE: str = "sha256:deadbeef"

#: The variable that holds the master key of LiteLLM.
KEY_VARIABLE: str = "LITELLM_MASTER_KEY"

#: Two addresses of TEST-NET-1. No test dials them.
LITELLM_URL: str = "http://192.0.2.10:4000"
ATTENDANCE_URL: str = "http://192.0.2.10:8350"

#: What the default `chat` family asks LiteLLM for. An invalid or refused
#: file in these tests asks for `OTHER_MODEL`, which must reach no key.
APPLIED_KEY: tuple[list[str], float] = (["agent-router"], 15)
OTHER_MODEL: dict[str, object] = {"router": "agent-router", "budget_usd_per_day": 99}


class Bench:
    def __init__(self, tmp_path: Path) -> None:
        self.registry_root = tmp_path / "registry"
        self.state_root = tmp_path / "state"
        self.driver = FakeDriver()
        self.litellm = FakeLiteLLMKeys()
        self.switch = FakeSwitchClient()
        self.units = FakeUnits()

    def run(self, *args: str) -> int:
        return main(
            list(args),
            driver=self.driver,
            litellm=self.litellm,
            switch=self.switch,
            units=self.units,
        )

    def reconcile(self, *extra: str) -> int:
        return self.run(
            "reconcile-once",
            str(self.registry_root),
            "chat",
            "--image",
            IMAGE,
            "--state-root",
            str(self.state_root),
            *extra,
        )

    def rotate(self, *extra: str) -> int:
        return self.run(
            "rotate",
            str(self.registry_root),
            "chat",
            "--state-root",
            str(self.state_root),
            *extra,
        )


@pytest.fixture
def bench(tmp_path: Path) -> Bench:
    ready = Bench(tmp_path)
    write_registry(ready.registry_root)
    return ready


# --- reconcile-once ---------------------------------------------------------------


def test_a_plan_run_makes_no_change(bench: Bench, capsys: pytest.CaptureFixture[str]) -> None:
    assert bench.reconcile() == EXIT_OK
    assert "plan:" in capsys.readouterr().out
    assert bench.driver.calls == []
    assert not paths.creds_path(bench.state_root, "chat").exists()


def test_a_plan_run_never_reads_the_attendance_token(bench: Bench) -> None:
    """`HttpSwitchClient` reads the token file in its constructor. A plan
    run on a host where `attendance` has not started yet must still print."""
    assert bench.reconcile() == EXIT_OK


def test_a_write_run_brings_the_family_up(bench: Bench) -> None:
    assert bench.reconcile("--write") == EXIT_OK
    assert read_creds(paths.creds_path(bench.state_root, "chat")) is not None
    assert "create" in bench.driver.ops()


def test_a_write_run_prints_the_one_decision_line(
    bench: Bench, capsys: pytest.CaptureFixture[str]
) -> None:
    """One log line: family, change class, action, result."""
    bench.reconcile("--write")
    line = capsys.readouterr().out.strip().splitlines()[-1]
    assert line.startswith("chat: ")
    assert f"state={FamilyState.IN_SYNC}" in line


def test_an_unknown_family_is_a_usage_mistake(bench: Bench) -> None:
    assert (
        bench.run(
            "reconcile-once",
            str(bench.registry_root),
            "nope",
            "--image",
            IMAGE,
            "--state-root",
            str(bench.state_root),
        )
        == EXIT_USAGE
    )


def test_a_directory_with_no_family_file_is_a_usage_mistake(bench: Bench) -> None:
    """Contract 01 §5.6 rule 3: `caregiver` ignores the directory, so the
    verb has no family of that name."""
    write_no_file_dir(bench.registry_root)
    words = ["reconcile-once", str(bench.registry_root), "stray", "--image", IMAGE]

    assert bench.run(*words, "--state-root", str(bench.state_root), "--write") == EXIT_USAGE
    assert not paths.family_dir(bench.state_root, "stray").exists()


def test_a_degraded_family_exits_non_zero(bench: Bench) -> None:
    write_registry(bench.registry_root, kind="nonsense")
    assert bench.reconcile("--write") == EXIT_PROBLEM


# --- serve ------------------------------------------------------------------------


def test_serve_without_write_only_prints_the_plan(
    bench: Bench, capsys: pytest.CaptureFixture[str]
) -> None:
    code = bench.run(
        "serve",
        str(bench.registry_root),
        "--image",
        IMAGE,
        "--state-root",
        str(bench.state_root),
    )
    out = capsys.readouterr().out
    assert code == EXIT_OK
    assert "Pass --write to start" in out
    assert "families: chat" in out
    assert bench.driver.calls == []


def test_the_plan_of_serve_names_no_directory_with_no_family_file(
    bench: Bench, capsys: pytest.CaptureFixture[str]
) -> None:
    """Contract 01 §5.6 rule 3: `serve` ignores the directory, so the plan
    does not name it as a family."""
    write_no_file_dir(bench.registry_root)

    bench.run(
        "serve", str(bench.registry_root), "--image", IMAGE, "--state-root", str(bench.state_root)
    )

    assert "families: chat\n" in capsys.readouterr().out


def test_serve_names_the_release_root_it_would_use(
    bench: Bench, capsys: pytest.CaptureFixture[str]
) -> None:
    bench.run(
        "serve",
        str(bench.registry_root),
        "--image",
        IMAGE,
        "--state-root",
        str(bench.state_root),
    )
    assert f"release root: {paths.RELEASE_ROOT}" in capsys.readouterr().out


def test_serve_refuses_a_scratch_state_root_beside_roots_real_spool(bench: Bench) -> None:
    """`caregiver` has two roots. A scratch `--state-root`
    with the DEFAULT release root would file the scratch run's requests
    into the spool root drains for real, so `--write` refuses it before
    anything starts."""
    code = bench.run(
        "serve",
        str(bench.registry_root),
        "--image",
        IMAGE,
        "--state-root",
        str(bench.state_root),
        "--write",
    )
    assert code == EXIT_USAGE
    assert bench.driver.calls == []


# --- a verb with no master key ------------------------------------------------------


class NoSignals:
    """In place of `SignalControl`. The test process keeps its own signal
    handlers, and a loop that starts stops at once."""

    def install(self) -> None:
        return

    def stopped(self) -> bool:
        return True

    def wait(self, seconds: float) -> None:
        del seconds


def _words_with_no_key(bench: Bench, tmp_path: Path, verb: str) -> list[str]:
    """The words of one verb that acts, with each address given."""
    state = ["--state-root", str(bench.state_root), "--litellm-base-url", LITELLM_URL, "--write"]
    watch = [str(bench.registry_root), "--image", IMAGE, "--sessiond-url", ATTENDANCE_URL]
    words = {
        "serve": ["serve", *watch, "--release-root", str(tmp_path / "release"), "--pep-url", ""],
        "reconcile-once": ["reconcile-once", *watch, "chat"],
        "apply-once": ["apply-once", *watch, "chat"],
        "rotate": ["rotate", str(bench.registry_root), "chat"],
        "delete": ["delete", "chat"],
    }

    return [*words[verb], *state]


@pytest.mark.parametrize("verb", ["serve", "reconcile-once", "apply-once", "rotate", "delete"])
def test_a_verb_that_acts_refuses_to_start_with_no_master_key(
    bench: Bench,
    tmp_path: Path,
    verb: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Fail closed. The refusal is one line that names the variable. It is
    not an error that leaves `main`: the unit starts `serve` again after
    each exit."""
    monkeypatch.delenv(KEY_VARIABLE, raising=False)
    monkeypatch.setattr(cli, "SignalControl", NoSignals)

    code = main(
        _words_with_no_key(bench, tmp_path, verb),
        driver=bench.driver,
        switch=bench.switch,
        units=bench.units,
    )

    assert code == EXIT_PROBLEM
    assert capsys.readouterr().err.strip().splitlines() == [f"caregiver: {KEY_VARIABLE} is not set"]
    assert bench.driver.calls == []


# --- rotate -----------------------------------------------------------------------


def test_rotate_without_write_changes_no_epoch(bench: Bench) -> None:
    bench.reconcile("--write")
    before = read_creds(paths.creds_path(bench.state_root, "chat"))
    assert before is not None
    assert bench.rotate() == EXIT_OK
    after = read_creds(paths.creds_path(bench.state_root, "chat"))
    assert after is not None
    assert after.epoch == before.epoch


def test_rotate_with_write_raises_the_epoch(bench: Bench) -> None:
    bench.reconcile("--write")
    assert bench.rotate("--write") == EXIT_OK
    after = read_creds(paths.creds_path(bench.state_root, "chat"))
    assert after is not None
    assert after.epoch == 2


def test_rotate_publishes_the_new_epoch(bench: Bench) -> None:
    """Contract 05 §6.3 step 3. `attendance` reads the epoch from the status
    document alone, so the verb puts it there. It changes no other field:
    the verb did not look at the family, so `written_at` stays."""
    bench.reconcile("--write")
    before = published(bench.state_root)

    assert bench.rotate("--write") == EXIT_OK

    after = published(bench.state_root)
    assert after["credentials"]["epoch"] == 2
    assert after["credentials"]["rotated_at"] >= before["credentials"]["rotated_at"]
    assert {**after, "credentials": None} == {**before, "credentials": None}


def test_rotate_of_an_invalid_family_publishes_the_new_epoch(bench: Bench) -> None:
    """The rotation that keeps the grants as applied publishes too."""
    bench.reconcile("--write")
    write_registry(bench.registry_root, tools=REFUSED_TOOLS)

    assert bench.rotate("--write") == EXIT_OK

    assert published_epoch(bench.state_root) == 2


def test_rotate_writes_no_document_where_none_is(bench: Bench) -> None:
    """A family with no status document gets its first one from a pass."""
    bench.reconcile("--write")
    paths.status_path(bench.state_root, "chat").unlink()

    assert bench.rotate("--write") == EXIT_OK

    assert not paths.status_path(bench.state_root, "chat").exists()


def test_rotate_says_the_key_half_is_not_graceful(
    bench: Bench, capsys: pytest.CaptureFixture[str]
) -> None:
    bench.reconcile("--write")
    capsys.readouterr()
    bench.rotate("--write")
    assert "no overlap" in capsys.readouterr().out


def test_rotating_a_family_with_no_credentials_fails(bench: Bench) -> None:
    assert bench.rotate("--write") == EXIT_PROBLEM


def test_rotating_an_unknown_family_is_a_usage_mistake(bench: Bench) -> None:
    assert (
        bench.run("rotate", str(bench.registry_root), "nope", "--state-root", str(bench.state_root))
        == EXIT_USAGE
    )


def test_rotating_an_invalid_family_keeps_its_grants(bench: Bench) -> None:
    """A file the validator refused still parses. A rotation on suspicion
    must not wait for a valid file, so it rotates the credentials of the
    applied definition: no tool of the refused file reaches the grant file,
    and its budget reaches no key."""
    bench.reconcile("--write")
    grants = grants_alone(bench.state_root)
    old = current_digest(bench.state_root)

    write_registry(bench.registry_root, tools=REFUSED_TOOLS, model=OTHER_MODEL)

    assert bench.rotate("--write") == EXIT_OK
    after = read_creds(paths.creds_path(bench.state_root, "chat"))
    assert after is not None
    assert after.epoch == 2
    assert grants_alone(bench.state_root) == grants
    assert accepted_digests(bench.state_root) == [token_sha256(after.pep_token), old]
    assert bench.litellm.budgets[key_alias("chat")] == APPLIED_KEY


def test_rotating_a_refused_kind_move_keeps_its_grants(bench: Bench) -> None:
    """A file whose `kind` moved has an ok report, and the reconciler
    refuses it against the applied snapshot. Its grants must never reach
    the grant file, and its budget must never reach a new key."""
    bench.reconcile("--write")
    grants = grants_alone(bench.state_root)

    write_registry(bench.registry_root, **KIND_MOVED, model=OTHER_MODEL)

    assert load_registry(bench.registry_root).reports["chat"].ok
    assert bench.rotate("--write") == EXIT_OK
    assert grants_alone(bench.state_root) == grants
    assert bench.litellm.budgets[key_alias("chat")] == APPLIED_KEY


def test_a_rotation_of_an_invalid_family_publishes_its_epoch(bench: Bench) -> None:
    """`attendance` reads the epoch from the status document alone, and the
    playpen recycles a resident pi process only when that epoch rises. An
    `invalid` document with no epoch leaves that process on the deleted
    key."""
    bench.reconcile("--write")
    write_registry(bench.registry_root, tools=REFUSED_TOOLS)

    assert bench.rotate("--write") == EXIT_OK
    bench.reconcile("--write")

    assert published(bench.state_root)["state"] == FamilyState.INVALID
    assert published_epoch(bench.state_root) == 2


def test_a_rotation_of_a_refused_kind_move_publishes_its_epoch(bench: Bench) -> None:
    """The second `invalid` document: the report is ok and the applied
    snapshot refuses the edit."""
    bench.reconcile("--write")
    write_registry(bench.registry_root, **KIND_MOVED)

    assert bench.rotate("--write") == EXIT_OK
    bench.reconcile("--write")

    assert published(bench.state_root)["state"] == FamilyState.INVALID
    assert published_epoch(bench.state_root) == 2


def test_the_plan_says_when_the_grants_stay(
    bench: Bench, capsys: pytest.CaptureFixture[str]
) -> None:
    bench.reconcile("--write")
    write_registry(bench.registry_root, tools=REFUSED_TOOLS)
    capsys.readouterr()

    assert bench.rotate() == EXIT_OK
    assert "the grants stay as applied" in capsys.readouterr().out


def test_rotating_an_invalid_family_that_was_never_applied_is_a_usage_mistake(
    bench: Bench,
) -> None:
    """No snapshot means no definition serves, so there is nothing whose
    credentials a rotation could move."""
    write_registry(bench.registry_root, tools=REFUSED_TOOLS)

    assert bench.rotate("--write") == EXIT_USAGE
    assert bench.litellm.deleted == []


def test_a_kept_grant_file_that_is_gone_refuses_a_token_rotation(bench: Bench) -> None:
    """The digests have no file to go into. The refusal comes before the
    old key is deleted, so nothing was rotated and nothing was lost."""
    bench.reconcile("--write")
    write_registry(bench.registry_root, tools=REFUSED_TOOLS)
    paths.grant_path(bench.state_root, "chat").unlink()

    assert bench.rotate("--write") == EXIT_PROBLEM
    after = read_creds(paths.creds_path(bench.state_root, "chat"))
    assert after is not None
    assert after.epoch == 1
    assert bench.litellm.deleted == []


def test_a_key_rotation_of_an_invalid_family_needs_no_grant_file(bench: Bench) -> None:
    """The key is the half a leak makes urgent, and it has no digest."""
    bench.reconcile("--write")
    write_registry(bench.registry_root, tools=REFUSED_TOOLS)
    paths.grant_path(bench.state_root, "chat").unlink()

    assert bench.rotate("--scope", "key", "--write") == EXIT_OK
    assert bench.litellm.deleted == [key_alias("chat")]
    assert not paths.grant_path(bench.state_root, "chat").exists()
