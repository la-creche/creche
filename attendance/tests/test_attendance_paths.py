"""The path of a family's sessions mount inside the VM (contract 03 §7.1).

Every path in `paths.py` is a host path, and this one is a host path as
well: sbx mounts a host directory at that SAME path inside the VM, so the
path this service sends is the path the facade opens. The one override
exists because the integration harness runs a program as a plain child
under a temp directory that no sbx mount put there.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from attendance.paths import SANDBOX_MOUNT_ENV, sandbox_sessions_mount

FAMILY = "chat"
SESSIONS_ROOT = Path("/srv/agents/sessions")


def test_the_sandbox_path_is_the_host_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """Contract 03 §7.1's identity rule. Nothing rewrites the root, because
    `sbx create` takes one path per mount and offers no target."""
    monkeypatch.delenv(SANDBOX_MOUNT_ENV, raising=False)

    assert sandbox_sessions_mount(SESSIONS_ROOT, FAMILY) == "/srv/agents/sessions/chat"


def test_it_never_names_the_old_work_mount(monkeypatch: pytest.MonkeyPatch) -> None:
    """`/work/sessions` was the defect: no such directory exists under sbx."""
    monkeypatch.delenv(SANDBOX_MOUNT_ENV, raising=False)

    assert "/work/sessions" not in sandbox_sessions_mount(SESSIONS_ROOT, FAMILY)


def test_each_family_gets_its_own_root(monkeypatch: pytest.MonkeyPatch) -> None:
    """`caregiver` mounts `/srv/agents/sessions/<family>/`, one per family."""
    monkeypatch.delenv(SANDBOX_MOUNT_ENV, raising=False)

    assert sandbox_sessions_mount(SESSIONS_ROOT, "code-sandbox") == (
        "/srv/agents/sessions/code-sandbox"
    )


def test_mount_override_moves_the_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SANDBOX_MOUNT_ENV, "/tmp/agi/sessions")

    assert sandbox_sessions_mount(SESSIONS_ROOT, FAMILY) == "/tmp/agi/sessions"


def test_blank_override_is_not_an_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty variable is what an unset one looks like in a unit file."""
    monkeypatch.setenv(SANDBOX_MOUNT_ENV, "   ")

    assert sandbox_sessions_mount(SESSIONS_ROOT, FAMILY) == "/srv/agents/sessions/chat"
