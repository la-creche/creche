"""The two paths that cross the channel (contract 03 §4.1, §7.1).

Every path in `paths.py` is a host path, and these two are host paths as
well: sbx mounts a host directory at that SAME path inside the VM, so the
path this service sends is the path the supervisor opens. The one override
exists because the integration harness runs the supervisor as a plain child
under a temp directory that no sbx mount put there.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from agent_sessiond.paths import (
    SANDBOX_MOUNT_ENV,
    sandbox_cwd,
    sandbox_session_dir,
)

SESSION = "owui-3f2a9c41-77b0-4a1e-9a4c-1d0e5f8b2c33"
FAMILY = "chat"
SESSIONS_ROOT = Path("/srv/agents/sessions")


def test_the_sandbox_path_is_the_host_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """Contract 03 §7.1's identity rule. Nothing rewrites the root, because
    `sbx create` takes one path per mount and offers no target."""
    monkeypatch.delenv(SANDBOX_MOUNT_ENV, raising=False)

    assert sandbox_cwd(SESSIONS_ROOT, FAMILY, SESSION) == f"/srv/agents/sessions/chat/{SESSION}"
    assert (
        sandbox_session_dir(SESSIONS_ROOT, FAMILY, SESSION)
        == f"/srv/agents/sessions/chat/{SESSION}/pi"
    )


def test_it_never_names_the_old_work_mount(monkeypatch: pytest.MonkeyPatch) -> None:
    """`/work/sessions` was the defect: no such directory exists under sbx."""
    monkeypatch.delenv(SANDBOX_MOUNT_ENV, raising=False)

    assert "/work/sessions" not in sandbox_cwd(SESSIONS_ROOT, FAMILY, SESSION)


def test_each_family_gets_its_own_root(monkeypatch: pytest.MonkeyPatch) -> None:
    """`managerd` mounts `/srv/agents/sessions/<family>/`, one per family."""
    monkeypatch.delenv(SANDBOX_MOUNT_ENV, raising=False)

    assert sandbox_cwd(SESSIONS_ROOT, "code-sandbox", SESSION).startswith(
        "/srv/agents/sessions/code-sandbox/"
    )


def test_mount_override_moves_both_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SANDBOX_MOUNT_ENV, "/tmp/agi/sessions")

    assert sandbox_cwd(SESSIONS_ROOT, FAMILY, SESSION) == f"/tmp/agi/sessions/{SESSION}"
    assert sandbox_session_dir(SESSIONS_ROOT, FAMILY, SESSION) == f"/tmp/agi/sessions/{SESSION}/pi"


def test_blank_override_is_not_an_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty variable is what an unset one looks like in a unit file."""
    monkeypatch.setenv(SANDBOX_MOUNT_ENV, "   ")

    assert sandbox_cwd(SESSIONS_ROOT, FAMILY, SESSION) == f"/srv/agents/sessions/chat/{SESSION}"
