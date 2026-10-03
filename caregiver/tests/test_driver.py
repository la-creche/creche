"""`SbxDriver` issues the exact `sbx` commands README §6 proves work, and
`FakeDriver` records calls in order for a test that never touches a real
sandbox."""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from typing import Any

import pytest
from caregiver.driver import (
    KIT_EXTRA_EGRESS,
    SBX_ENV,
    DriverError,
    FakeDriver,
    Mount,
    SandboxSpec,
    SbxDriver,
)


class FakeProcess:
    """Records every `subprocess.run` call this test's fake makes, and
    answers by prefix: the LONGEST registered prefix that matches a call
    wins, so a test can set a general default (`"sbx policy check"`) and
    override one specific destination (`"... check ... openrouter.ai"`)."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.envs: list[dict[str, str]] = []
        self._answers: dict[tuple[str, ...], subprocess.CompletedProcess[str]] = {}
        self._default = subprocess.CompletedProcess([], 0, stdout="", stderr="")

    def answer(self, prefix: Sequence[str], result: subprocess.CompletedProcess[str]) -> None:
        self._answers[tuple(prefix)] = result

    def __call__(self, args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append(args)
        self.envs.append(kwargs.get("env", {}))
        best: tuple[str, ...] | None = None
        for prefix in self._answers:
            if tuple(args[: len(prefix)]) == prefix and (best is None or len(prefix) > len(best)):
                best = prefix
        return self._answers[best] if best is not None else self._default


@pytest.fixture
def fake_run(monkeypatch: pytest.MonkeyPatch) -> FakeProcess:
    fake = FakeProcess()
    monkeypatch.setattr(subprocess, "run", fake)
    return fake


SPEC = SandboxSpec(
    name="chat-s1",
    image="sha256:deadbeef",
    mounts=(
        Mount("/srv/agents/sessions/chat", readonly=False),
        Mount("/srv/agents/vault", readonly=True),
    ),
    cpus=4,
    memory="8g",
)


def test_create_issues_the_proven_sbx_command(fake_run: FakeProcess) -> None:
    SbxDriver().create(SPEC)
    assert fake_run.calls == [
        [
            "sbx",
            "create",
            "shell",
            "/srv/agents/sessions/chat",
            "/srv/agents/vault:ro",
            "-t",
            "sha256:deadbeef",
            "--name",
            "chat-s1",
            "--cpus",
            "4",
            "-m",
            "8g",
            "--deny-network",
            "openrouter.ai",
            "-q",
        ]
    ]


def test_every_call_disables_sbx_telemetry(fake_run: FakeProcess) -> None:
    SbxDriver().create(SPEC)
    assert fake_run.envs[0]["SBX_NO_TELEMETRY"] == SBX_ENV["SBX_NO_TELEMETRY"]


def test_create_failure_raises_with_stderr(fake_run: FakeProcess) -> None:
    fake_run.answer(
        ["sbx", "create"],
        subprocess.CompletedProcess([], 1, stdout="", stderr="template pull timed out"),
    )
    with pytest.raises(DriverError, match="template pull timed out"):
        SbxDriver().create(SPEC)


def test_set_egress_allows_then_denies_the_kit_hole(fake_run: FakeProcess) -> None:
    SbxDriver().set_egress("chat-s1", ("github.com", "api.github.com"))
    assert fake_run.calls == [
        ["sbx", "policy", "allow", "network", "--sandbox", "chat-s1", "github.com"],
        ["sbx", "policy", "allow", "network", "--sandbox", "chat-s1", "api.github.com"],
        ["sbx", "policy", "deny", "network", "--sandbox", "chat-s1", "openrouter.ai"],
    ]


def test_remove_egress_drops_one_row_per_host(fake_run: FakeProcess) -> None:
    """An egress removal lands on the RUNNING sandbox, in the same VM boot
    (contract 05 §3.5). No sandbox is replaced for it."""
    SbxDriver().remove_egress("chat-s1", ("github.com",))
    assert fake_run.calls == [
        ["sbx", "policy", "rm", "network", "--sandbox", "chat-s1", "--resource", "github.com"]
    ]


def test_remove_egress_fails_loudly(fake_run: FakeProcess) -> None:
    """A removal that did not take leaves reach the family file no longer
    grants. It must not pass silently (invariant 9)."""
    fake_run.answer(
        ["sbx", "policy", "rm"],
        subprocess.CompletedProcess([], 1, stdout="", stderr="no such rule"),
    )
    with pytest.raises(DriverError):
        SbxDriver().remove_egress("chat-s1", ("github.com",))


def test_assert_egress_passes_when_the_probe_agrees(fake_run: FakeProcess) -> None:
    fake_run.answer(
        ["sbx", "policy", "check", "network", "--sandbox", "chat-s1", "github.com"],
        subprocess.CompletedProcess([], 0, stdout="Allowed\n", stderr=""),
    )
    fake_run.answer(
        ["sbx", "policy", "check", "network", "--sandbox", "chat-s1", "openrouter.ai"],
        subprocess.CompletedProcess([], 0, stdout="Denied\n", stderr=""),
    )
    SbxDriver().assert_egress("chat-s1", ("github.com",), ())


def test_assert_egress_fails_closed_when_a_denied_host_is_reachable(fake_run: FakeProcess) -> None:
    fake_run.answer(
        ["sbx", "policy", "check"],
        subprocess.CompletedProcess([], 0, stdout="Allowed\n", stderr=""),
    )
    with pytest.raises(DriverError, match="REACHABLE"):
        SbxDriver().assert_egress("chat-s1", (), ("evil.example",))


def test_assert_egress_always_probes_the_kit_hole_as_denied(fake_run: FakeProcess) -> None:
    fake_run.answer(
        ["sbx", "policy", "check"], subprocess.CompletedProcess([], 0, stdout="Denied\n", stderr="")
    )
    SbxDriver().assert_egress("chat-s1", (), ())
    probed = [call[-1] for call in fake_run.calls if call[:3] == ["sbx", "policy", "check"]]
    assert KIT_EXTRA_EGRESS[0] in probed


def test_destroy_removes_policy_rows_before_the_vm(fake_run: FakeProcess) -> None:
    SbxDriver().destroy("chat-s1", ("github.com",))
    assert fake_run.calls == [
        ["sbx", "policy", "rm", "network", "--sandbox", "chat-s1", "--resource", "github.com"],
        ["sbx", "rm", "-f", "chat-s1"],
    ]


def test_destroy_of_an_already_gone_sandbox_does_not_raise(fake_run: FakeProcess) -> None:
    fake_run.answer(
        ["sbx", "rm"], subprocess.CompletedProcess([], 1, stdout="", stderr="sandbox not found")
    )
    SbxDriver().destroy("chat-s1", ())  # must not raise


def test_list_names_drops_the_header_row(fake_run: FakeProcess) -> None:
    fake_run.answer(
        ["sbx", "ls"],
        subprocess.CompletedProcess(
            [], 0, stdout="NAME       STATE\nchat-s1    running\nchat-s2    stopped\n", stderr=""
        ),
    )
    assert SbxDriver().list_names() == {"chat-s1", "chat-s2"}


def _raise_timeout(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    raise subprocess.TimeoutExpired(args, 30)


def test_list_names_answers_empty_on_a_wedged_daemon(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subprocess, "run", _raise_timeout)
    assert SbxDriver().list_names() == set()


def test_a_wedged_daemon_names_the_recovery_command(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subprocess, "run", _raise_timeout)
    with pytest.raises(DriverError, match="sbx daemon restart"):
        SbxDriver().create(SPEC)


# --- FakeDriver ---------------------------------------------------------------


def test_fake_driver_records_calls_in_order() -> None:
    fake = FakeDriver()
    fake.create(SPEC)
    fake.set_egress("chat-s1", ("github.com",))
    fake.assert_egress("chat-s1", ("github.com",), ())
    fake.destroy("chat-s1", ("github.com",))
    assert fake.ops() == ("create", "set_egress", "assert_egress", "destroy")


def test_fake_driver_tracks_existing_names() -> None:
    fake = FakeDriver()
    fake.create(SPEC)
    assert fake.list_names() == {"chat-s1"}
    fake.destroy("chat-s1", ())
    assert fake.list_names() == set()
