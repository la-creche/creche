"""`supervisor.env`: the file `sbx exec --env-file` hands the playpen
(contract 03 §7.1).

sbx mounts a host directory at its own host path inside the VM, so the
playpen cannot be told a target path and cannot guess one. `caregiver`
writes the three host paths, plus the sandbox id, into one file per
sandbox beside the control directory. `sbx exec --env-file` is the only
thing that carries a value into the VM, because `sbx exec` forwards no
host environment."""

from __future__ import annotations

import stat
from pathlib import Path

from caregiver.playpen_env import (
    PLAYPEN_ENV_MODE,
    read_playpen_env,
    write_playpen_env,
)

from caregiver import paths

FAMILY = "chat"
SANDBOX = "chat-s1"


def write(state_root: Path, sandbox: str = SANDBOX) -> Path:
    path = paths.playpen_env_path(state_root, FAMILY, sandbox)
    write_playpen_env(path, state_root=state_root, family=FAMILY, sandbox=sandbox)
    return path


def test_it_sits_beside_the_control_directory(tmp_path: Path) -> None:
    """Beside, never inside: the control directory is mounted read-write
    into the sandbox and `apply_once` empties it at every create."""
    path = write(tmp_path)

    assert path.parent == paths.control_root(tmp_path, FAMILY).parent
    assert not path.is_relative_to(paths.control_root(tmp_path, FAMILY))


def test_it_names_the_three_directories_by_host_path(tmp_path: Path) -> None:
    """Contract 03 §7.1: the in-VM path IS the host path, so each value is
    the host path `_sandbox_spec` mounts and nothing else."""
    values = read_playpen_env(write(tmp_path))

    assert values["AGENT_CRED_DIR"] == str(paths.creds_dir(tmp_path, FAMILY))
    assert values["AGENT_FAMILY_CONFIG_DIR"] == str(paths.config_dir(tmp_path, FAMILY))
    assert values["AGENT_CONTROL_DIR"] == str(paths.control_dir(tmp_path, FAMILY, SANDBOX))


def test_it_names_the_sandbox(tmp_path: Path) -> None:
    """The playpen takes its id from `--sandbox` first and from this
    variable second (`playpen/src/index.ts`)."""
    assert read_playpen_env(write(tmp_path))["AGENT_SANDBOX"] == SANDBOX


def test_it_names_no_path_under_run(tmp_path: Path) -> None:
    """The defect this file exists to fix: /run/family, /run/family-config
    and /run/control do not exist inside an sbx sandbox."""
    body = write(tmp_path).read_text(encoding="utf-8")

    assert "/run/family" not in body
    assert "/run/control" not in body


def test_it_holds_no_secret(tmp_path: Path) -> None:
    """Invariant 13. The family key and the PEP token live in `creds.json`
    at mode 0600. This file names that directory and never opens it."""
    body = write(tmp_path).read_text(encoding="utf-8")

    assert "LITELLM" not in body
    assert "PEP_TOKEN" not in body
    assert "TOKEN" not in body


def test_it_is_group_readable_and_no_wider(tmp_path: Path) -> None:
    """0640. `attendance` never reads it — it hands the path to `sbx` — but
    the file is a host artefact and the world has no business in it."""
    mode = stat.S_IMODE(write(tmp_path).stat().st_mode)

    assert mode == PLAYPEN_ENV_MODE


def test_every_line_is_one_assignment(tmp_path: Path) -> None:
    """`sbx exec --env-file` reads `NAME=value` lines. A comment or a blank
    line is one more thing that could be parsed differently there than
    here, so neither is written."""
    lines = write(tmp_path).read_text(encoding="utf-8").splitlines()

    assert len(lines) == 4
    assert all(line.count("=") >= 1 and not line.startswith("#") for line in lines)


def test_a_replacement_leaves_the_first_file_alone(tmp_path: Path) -> None:
    """Contract 03 §7.1 rule 1: one file per sandbox. The outgoing sandbox
    keeps serving during a replacement, and a shared file would hand its
    playpen the incoming sandbox's id and control directory."""
    first = write(tmp_path, "chat-s1")
    second = write(tmp_path, "chat-s2")

    assert first != second
    assert read_playpen_env(first)["AGENT_SANDBOX"] == "chat-s1"
    assert read_playpen_env(second)["AGENT_SANDBOX"] == "chat-s2"


def test_reading_a_missing_file_yields_nothing(tmp_path: Path) -> None:
    assert read_playpen_env(tmp_path / "absent.env") == {}


def test_reading_a_file_that_is_not_utf8_yields_nothing(tmp_path: Path) -> None:
    path = tmp_path / "supervisor.env"
    path.write_bytes(b"AGENT_SANDBOX=chat-s1\xff\n")
    assert read_playpen_env(path) == {}
