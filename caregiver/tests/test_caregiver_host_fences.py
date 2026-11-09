"""The fence of the host (contract 01 §5.5 rule 1). caregiver reads it from
its environment, which the unit fills from the site file. Each verb that
applies the registry reads it. Contract 01 minor 18 drops the mount roots
and `AGENT_MOUNT_ROOTS` (decision CH10-10)."""

from __future__ import annotations

from pathlib import Path

import pytest
from caregiver.cli import EXIT_CONFIG, EXIT_OK, EXIT_PROBLEM
from caregiver.litellm_keys import FakeLiteLLMKeys
from caregiver_helpers import FENCE_ENV, main, write_registry


def _apply_once(registry: Path, state: Path, environ: dict[str, str]) -> int:
    return main(
        [
            "apply-once",
            str(registry),
            "chat",
            "--state-root",
            str(state),
        ],
        litellm=FakeLiteLLMKeys(),
        environ=environ,
    )


@pytest.mark.parametrize("verb", ["apply-once", "reconcile-once", "serve", "rotate"])
def test_a_verb_that_applies_stops_on_a_bad_platform_family(
    tmp_path: Path, verb: str, capsys: pytest.CaptureFixture[str]
) -> None:
    registry = write_registry(tmp_path / "registry")
    argv = [verb, str(registry)]
    if verb != "serve":
        argv.append("chat")
    if verb == "rotate":
        argv += ["--reason", "suspicion"]

    environ = {"AGENT_PLATFORM_FAMILIES": "Code"}
    code = main([*argv, "--state-root", str(tmp_path / "state")], environ=environ)

    assert code == EXIT_CONFIG
    assert "AGENT_PLATFORM_FAMILIES" in capsys.readouterr().err


def test_status_needs_no_fences(tmp_path: Path) -> None:
    code = main(["status", "--state-root", str(tmp_path / "state")], environ={})

    assert code == EXIT_OK


def test_a_release_outside_the_platform_fence_refuses_the_family(tmp_path: Path) -> None:
    registry = write_registry(
        tmp_path / "registry", verbs={"release": {"components": ["chaperone"]}}
    )
    state = tmp_path / "state"
    widened = FENCE_ENV | {"AGENT_PLATFORM_FAMILIES": "chat"}

    assert _apply_once(registry, state, widened) == EXIT_OK
    assert _apply_once(registry, state, FENCE_ENV) == EXIT_PROBLEM
