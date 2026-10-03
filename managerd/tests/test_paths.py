"""Every path template matches its contract exactly (contracts 03 section
7.1/section 12, 04 section 1.1/section 1.6, 05 section 2/section
3.3.1/section 4.1)."""

from __future__ import annotations

from pathlib import Path

from agent_managerd.paths import (
    config_dir,
    control_dir,
    control_root,
    creds_dir,
    creds_path,
    family_dir,
    fault_path,
    grant_path,
    playpen_env_path,
    read_sandbox_sequence,
    sandbox_id,
    sandbox_seq_path,
    session_dir,
    status_path,
    validation_path,
)

ROOT = Path("/srv/agents/state/rework")


def test_family_dir() -> None:
    assert family_dir(ROOT, "chat") == ROOT / "families" / "chat"


def test_status_path() -> None:
    assert status_path(ROOT, "chat") == ROOT / "families" / "chat" / "status.json"


def test_validation_path() -> None:
    assert validation_path(ROOT, "chat") == ROOT / "families" / "chat" / "validation.json"


def test_creds_dir_and_path() -> None:
    assert creds_dir(ROOT, "chat") == ROOT / "families" / "chat" / "creds"
    assert creds_path(ROOT, "chat") == ROOT / "families" / "chat" / "creds" / "creds.json"


def test_config_dir() -> None:
    assert config_dir(ROOT, "chat") == ROOT / "families" / "chat" / "config"


def test_control_dir_is_per_sandbox() -> None:
    """Contract 03 §7.1: two sandboxes of one family are live at once, and
    §11.1's lock, §7.4's turn files and §7.5's records belong to one VM."""
    base = ROOT / "families" / "chat" / "control"
    assert control_root(ROOT, "chat") == base
    assert control_dir(ROOT, "chat", "chat-s2") == base / "chat-s2"


def test_playpen_env_path_is_per_sandbox() -> None:
    """It names AGENT_SANDBOX and that sandbox's control directory, so one
    file per family would misname the outgoing sandbox during a switch."""
    family = ROOT / "families" / "chat"
    assert playpen_env_path(ROOT, "chat", "chat-s2") == family / "supervisor-chat-s2.env"


def test_sandbox_seq_path() -> None:
    assert sandbox_seq_path(ROOT, "chat") == ROOT / "families" / "chat" / "sandbox-seq"


def test_read_sandbox_sequence_of_a_missing_counter_is_zero(tmp_path: Path) -> None:
    assert read_sandbox_sequence(tmp_path, "chat") == 0


def test_read_sandbox_sequence_reads_back_a_written_value(tmp_path: Path) -> None:
    path = sandbox_seq_path(tmp_path, "chat")
    path.parent.mkdir(parents=True)
    path.write_text("3\n", encoding="utf-8")
    assert read_sandbox_sequence(tmp_path, "chat") == 3


def test_read_sandbox_sequence_of_a_corrupt_counter_is_zero(tmp_path: Path) -> None:
    path = sandbox_seq_path(tmp_path, "chat")
    path.parent.mkdir(parents=True)
    path.write_text("not a number", encoding="utf-8")
    assert read_sandbox_sequence(tmp_path, "chat") == 0


def test_session_dir_is_outside_state_root() -> None:
    assert session_dir("chat") == Path("/srv/agents/sessions/chat")


def test_sandbox_id_form() -> None:
    assert sandbox_id("chat", 1) == "chat-s1"
    assert sandbox_id("chat", 42) == "chat-s42"


def test_grant_path() -> None:
    assert grant_path(ROOT, "chat") == ROOT / "grants" / "chat.json"


def test_fault_path_is_keyed_by_source_and_family() -> None:
    assert fault_path(ROOT, "pep", "chat") == ROOT / "faults" / "pep" / "chat.json"
    assert fault_path(ROOT, "sessiond", "chat") == ROOT / "faults" / "sessiond" / "chat.json"


def test_every_family_path_lives_under_the_family_directory() -> None:
    """A sandbox mount fences one directory per family (contract 03 section
    7.1); every path but the session and grant paths must stay under it."""
    base = family_dir(ROOT, "chat")
    for path in (
        status_path(ROOT, "chat"),
        validation_path(ROOT, "chat"),
        creds_dir(ROOT, "chat"),
        creds_path(ROOT, "chat"),
        config_dir(ROOT, "chat"),
        control_dir(ROOT, "chat", "chat-s1"),
        playpen_env_path(ROOT, "chat", "chat-s1"),
        sandbox_seq_path(ROOT, "chat"),
    ):
        assert path.is_relative_to(base)
