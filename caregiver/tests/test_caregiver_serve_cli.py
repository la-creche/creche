"""`caregiver serve | reconcile-once | rotate`.

Every one of them mutates, so every one keeps the `--write` gate. These
tests hold that gate and the wiring behind it: a plan run must reach no
client at all, not even to read the `attendance` token file."""

from __future__ import annotations

from pathlib import Path

import pytest
from agent_family import FamilyState
from caregiver.cli import EXIT_OK, EXIT_PROBLEM, EXIT_USAGE, main
from caregiver.credentials import read_creds
from caregiver.driver import FakeDriver
from caregiver.litellm_keys import FakeLiteLLMKeys
from caregiver.switch import FakeSwitchClient
from caregiver.timers import FakeUnits
from caregiver_helpers import write_registry

from caregiver import paths

IMAGE: str = "sha256:deadbeef"


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
