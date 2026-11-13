"""The `cwd` of a family's sessions (contract 02 §6 rule 1).

The row of a session keeps the `cwd` as a display value, never a guest path
(decision CH21-4). It is the host path of the family's sessions directory,
and nothing rewrites it.
"""

from __future__ import annotations

from pathlib import Path

from attendance.paths import session_cwd

FAMILY = "chat"
SESSIONS_ROOT = Path("/srv/agents/sessions")


def test_the_cwd_is_the_host_path() -> None:
    assert session_cwd(SESSIONS_ROOT, FAMILY) == "/srv/agents/sessions/chat"


def test_it_never_names_the_old_work_mount() -> None:
    """`/work/sessions` was a defect of an earlier release."""
    assert "/work/sessions" not in session_cwd(SESSIONS_ROOT, FAMILY)


def test_each_family_gets_its_own_root() -> None:
    """One sessions directory per family: `/srv/agents/sessions/<family>/`."""
    assert session_cwd(SESSIONS_ROOT, "coding-sandbox") == "/srv/agents/sessions/coding-sandbox"
