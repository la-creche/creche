"""Every path template matches its contract exactly (contracts 03 section
12, 04 section 1.1/section 1.6, 05 section 2/section 3.3.1)."""

from __future__ import annotations

from pathlib import Path

from caregiver.paths import (
    config_dir,
    creds_dir,
    creds_path,
    egress_path,
    family_dir,
    fault_path,
    repos_path,
    status_path,
    validation_path,
)

ROOT = Path("/srv/agents/state/creche")


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


def test_egress_path() -> None:
    """Contract 01 §6.1.1: beside `config/`, never inside it."""
    assert egress_path(ROOT, "chat") == ROOT / "families" / "chat" / "egress.json"
    assert not egress_path(ROOT, "chat").is_relative_to(config_dir(ROOT, "chat"))


def test_repos_path() -> None:
    """Contract 01 §6.1.1: beside `egress.json`, never inside `config/`."""
    assert repos_path(ROOT, "chat") == ROOT / "families" / "chat" / "repos.json"
    assert not repos_path(ROOT, "chat").is_relative_to(config_dir(ROOT, "chat"))


def test_fault_path_is_keyed_by_source_and_family() -> None:
    assert fault_path(ROOT, "pep", "chat") == ROOT / "faults" / "pep" / "chat.json"
    assert fault_path(ROOT, "attendance", "chat") == ROOT / "faults" / "attendance" / "chat.json"
    # `VMM_FAULTS_ROOT` is `${CAREGIVER_STATE_ROOT}/faults` (env/default.env),
    # and the vmm writes `vmm/<family>.json` under it (decision CH21-1).
    assert fault_path(ROOT, "vmm", "chat") == ROOT / "faults" / "vmm" / "chat.json"


def test_every_family_path_lives_under_the_family_directory() -> None:
    """The delete of a family removes one directory (contract 05 §4.4);
    every path but the grant and fault paths must stay under it."""
    base = family_dir(ROOT, "chat")
    for path in (
        status_path(ROOT, "chat"),
        validation_path(ROOT, "chat"),
        creds_dir(ROOT, "chat"),
        creds_path(ROOT, "chat"),
        config_dir(ROOT, "chat"),
        egress_path(ROOT, "chat"),
        repos_path(ROOT, "chat"),
    ):
        assert path.is_relative_to(base)
