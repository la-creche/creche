"""The two fences of the host (contract 01 §5.4, §5.5 rule 1). caregiver
reads them from its environment, which the unit fills from the site file.
Each verb that applies the registry needs them."""

from __future__ import annotations

from pathlib import Path

import pytest
from caregiver.cli import EXIT_CONFIG, EXIT_OK, EXIT_PROBLEM
from caregiver.driver import FakeDriver
from caregiver.litellm_keys import FakeLiteLLMKeys
from caregiver_helpers import FENCE_ENV, main, write_registry


def _apply_once(registry: Path, state: Path, environ: dict[str, str]) -> int:
    return main(
        [
            "apply-once",
            str(registry),
            "chat",
            "--image",
            "sha256:x",
            "--state-root",
            str(state),
        ],
        driver=FakeDriver(),
        litellm=FakeLiteLLMKeys(),
        environ=environ,
    )


@pytest.mark.parametrize("verb", ["apply-once", "reconcile-once", "serve", "rotate"])
def test_a_verb_that_applies_stops_without_the_mount_roots(
    tmp_path: Path, verb: str, capsys: pytest.CaptureFixture[str]
) -> None:
    registry = write_registry(tmp_path / "registry")
    argv = [verb, str(registry)]
    if verb != "serve":
        argv.append("chat")
    if verb in ("apply-once", "reconcile-once", "serve"):
        argv += ["--image", "sha256:x"]
    if verb == "rotate":
        argv += ["--reason", "suspicion"]

    code = main([*argv, "--state-root", str(tmp_path / "state")], environ={})

    assert code == EXIT_CONFIG
    assert "AGENT_MOUNT_ROOTS" in capsys.readouterr().err


def test_status_needs_no_fences(tmp_path: Path) -> None:
    code = main(["status", "--state-root", str(tmp_path / "state")], environ={})

    assert code == EXIT_OK


def test_a_mount_outside_the_host_roots_refuses_the_family(tmp_path: Path) -> None:
    registry = write_registry(
        tmp_path / "registry", files=[{"path": "/srv/agents/vault", "mode": "ro"}]
    )
    state = tmp_path / "state"
    narrow = FENCE_ENV | {"AGENT_MOUNT_ROOTS": "/srv/agents/code"}

    assert _apply_once(registry, state, FENCE_ENV) == EXIT_OK
    assert _apply_once(registry, state, narrow) == EXIT_PROBLEM
