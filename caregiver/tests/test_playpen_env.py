"""`supervisor.env`: the file `sbx exec --env-file` hands the playpen
(contract 03 §7.1).

sbx mounts a host directory at its own host path inside the VM, so the
playpen cannot be told a target path and cannot guess one. `caregiver`
writes the three host paths, plus the sandbox id, into one file per
sandbox beside the control directory. `sbx exec --env-file` is the only
thing that carries a value into the VM, because `sbx exec` forwards no
host environment.

The file also names the URL of each plane: the chaperone and LiteLLM. The
caller of the writer gives the two URLs."""

from __future__ import annotations

import stat
from pathlib import Path

from caregiver.playpen_env import (
    INDEX_ROOT_VAR,
    PLAYPEN_ENV_MODE,
    read_playpen_env,
    write_playpen_env,
)
from caregiver_helpers import chat_family

from caregiver import paths

FAMILY = "chat"
SANDBOX = "chat-s1"

#: The URL of each plane, on a TEST-NET-1 address (RFC 5737).
PEP_URL = "http://192.0.2.10:8300"
LITELLM_BASE_URL = "http://192.0.2.10:4000"


def write(state_root: Path, sandbox: str = SANDBOX, index_root: Path | None = None) -> Path:
    path = paths.playpen_env_path(state_root, FAMILY, sandbox)
    write_playpen_env(
        path,
        state_root=state_root,
        family=FAMILY,
        sandbox=sandbox,
        pep_url=PEP_URL,
        litellm_base_url=LITELLM_BASE_URL,
        index_root=index_root,
    )
    return path


def test_it_holds_seven_lines_in_one_order(tmp_path: Path) -> None:
    """The sessions directory, the three directories, the sandbox, then the
    URL of each plane: the chaperone first, LiteLLM second."""
    lines = write(tmp_path).read_text(encoding="utf-8").splitlines()

    assert lines == [
        "AGENT_SESSIONS_DIR=/srv/agents/sessions/chat",
        f"AGENT_CRED_DIR={paths.creds_dir(tmp_path, FAMILY)}",
        f"AGENT_FAMILY_CONFIG_DIR={paths.config_dir(tmp_path, FAMILY)}",
        f"AGENT_CONTROL_DIR={paths.control_dir(tmp_path, FAMILY, SANDBOX)}",
        f"AGENT_SANDBOX={SANDBOX}",
        f"AGENT_PEP_URL={PEP_URL}",
        f"AGENT_LITELLM_BASE_URL={LITELLM_BASE_URL}",
    ]


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
    """The facade takes its id from this variable (`agent/src/config.ts`)."""
    assert read_playpen_env(write(tmp_path))["AGENT_SANDBOX"] == SANDBOX


def test_it_names_each_plane_by_the_url_of_the_caller(tmp_path: Path) -> None:
    """The writer holds no address and no port. Each URL is the text that
    the caller gave, with no change."""
    values = read_playpen_env(write(tmp_path))

    assert values["AGENT_PEP_URL"] == PEP_URL
    assert values["AGENT_LITELLM_BASE_URL"] == LITELLM_BASE_URL


def test_it_names_no_path_under_run(tmp_path: Path) -> None:
    """The defect this file exists to fix: /run/family, /run/family-config
    and /run/control do not exist inside an sbx sandbox."""
    body = write(tmp_path).read_text(encoding="utf-8")

    assert "/run/family" not in body
    assert "/run/control" not in body


def test_it_holds_no_secret(tmp_path: Path) -> None:
    """Invariant 13. The family key and the PEP token live in `creds.json`
    at mode 0600. This file names that directory and never opens it. It
    names the URL of LiteLLM and of the chaperone, and no key and no token
    of either one."""
    body = write(tmp_path).read_text(encoding="utf-8")

    assert "KEY" not in body
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

    assert len(lines) == 7
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


def test_it_names_the_sessions_directory_of_the_host(tmp_path: Path) -> None:
    """The agent module reads its session store from this variable. The
    store is the host's sessions root, outside the state root."""
    values = read_playpen_env(write(tmp_path))

    assert values["AGENT_SESSIONS_DIR"] == str(paths.session_dir(FAMILY))


def test_an_index_root_is_the_last_line(tmp_path: Path) -> None:
    lines = write(tmp_path, index_root=paths.INDEX_ROOT).read_text(encoding="utf-8").splitlines()

    assert len(lines) == 8
    assert lines[-1] == "AGENT_INDEX_ROOT=/srv/agents/state/index"


def test_no_index_root_writes_no_index_line(tmp_path: Path) -> None:
    assert INDEX_ROOT_VAR not in read_playpen_env(write(tmp_path))


def test_a_read_only_index_mount_gives_the_index_root() -> None:
    family = chat_family(files=[{"path": "/srv/agents/state/index", "mode": "ro"}])

    assert paths.index_root(family) == paths.INDEX_ROOT


def test_a_family_with_no_index_mount_has_no_index_root() -> None:
    family = chat_family(files=[{"path": "/srv/agents/vault", "mode": "ro"}])

    assert paths.index_root(family) is None


def test_an_index_mount_that_is_not_read_only_gives_no_index_root() -> None:
    family = chat_family(files=[{"path": "/srv/agents/state/index", "mode": "rw"}])

    assert paths.index_root(family) is None


def test_a_mount_under_the_index_root_gives_no_index_root() -> None:
    family = chat_family(files=[{"path": "/srv/agents/state/index/vault", "mode": "ro"}])

    assert paths.index_root(family) is None
