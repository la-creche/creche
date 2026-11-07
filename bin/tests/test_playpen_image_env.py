"""`/etc/creche/env`: `playpen/bin/creche-env-write` and
`playpen/bin/playpen-env-check`, with no image (CH7-7, CH49).

The writer runs here under a fixed env. The check runs against a `docker`
stub on `PATH` that answers `image inspect` with a config env, the `cat`
run with a file, and the `node` run with a report: the count of lines
read, then the names that no agent list holds (CH49 rule 1). CI runs the
same check on each built flavor, where node runs the agent's own
`placeEnvFile` (`agent/test/tool-env.test.ts`).
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
WRITE = REPO_ROOT / "playpen" / "bin" / "creche-env-write"
CHECK = REPO_ROOT / "playpen" / "bin" / "playpen-env-check"
DOCKERFILE = REPO_ROOT / "playpen" / "Dockerfile"

#: The config env of a built flavor, in the order `image inspect` gives it.
CONFIG = (
    "PATH=/opt/py/bin:/opt/test/bin:/opt/py/bin:/usr/local/bin:/usr/bin:/bin\n"
    "NODE_VERSION=24.22.2\n"
    "YARN_VERSION=1.22.22\n"
    "NODE_ENV=production\n"
    "HOME=/root\n"
    "EMPTY=\n"
)

#: What the writer prints for that config: sorted, each PATH folder once.
FILE = (
    "EMPTY=\n"
    "HOME=/root\n"
    "NODE_ENV=production\n"
    "NODE_VERSION=24.22.2\n"
    "PATH=/opt/py/bin:/opt/test/bin:/usr/local/bin:/usr/bin:/bin\n"
    "YARN_VERSION=1.22.22\n"
)

#: `docker image inspect` prints STUB_CONFIG. The `cat` run prints
#: STUB_FILE. The `node` run prints STUB_REPORT and exits STUB_NODE_EXIT.
DOCKER_STUB = """#!/usr/bin/env bash
case "$*" in
  image*) cat "$STUB_CONFIG" ;;
  run*" --entrypoint /bin/cat "*) cat "$STUB_FILE" ;;
  run*" --workdir /opt/creche-facade --entrypoint node "*)
    cat "$STUB_REPORT"
    exit "$STUB_NODE_EXIT" ;;
  *) exit 2 ;;
esac
"""


def _config_env() -> dict[str, str]:
    pairs = (line.split("=", 1) for line in CONFIG.splitlines())
    return {name: value for name, value in pairs}


def _write(*names: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/bin/sh", str(WRITE), *names],
        env=_config_env() if env is None else {"PATH": "/usr/bin:/bin", **env},
        capture_output=True,
        text=True,
        check=False,
    )


def _check(
    tmp_path: Path, config: str, file: str, report: str | None = None, node_exit: int = 0
) -> subprocess.CompletedProcess[str]:
    """Run the check. With no `report`, node reports each line of `file`
    read and no unplaced name."""
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    docker = stubs / "docker"
    docker.write_text(DOCKER_STUB, encoding="utf-8")
    docker.chmod(docker.stat().st_mode | stat.S_IXUSR)
    (tmp_path / "config").write_text(config, encoding="utf-8")
    (tmp_path / "file").write_text(file, encoding="utf-8")
    if report is None:
        report = f"{len(file.splitlines())}\n"
    (tmp_path / "report").write_text(report, encoding="utf-8")
    env = {
        "PATH": f"{stubs}:/usr/bin:/bin",
        "STUB_CONFIG": str(tmp_path / "config"),
        "STUB_FILE": str(tmp_path / "file"),
        "STUB_REPORT": str(tmp_path / "report"),
        "STUB_NODE_EXIT": str(node_exit),
        "TMPDIR": os.environ.get("TMPDIR", "/tmp"),
    }
    return subprocess.run(
        ["bash", str(CHECK), "image:tag"], env=env, capture_output=True, text=True, check=False
    )


def test_the_writer_sorts_on_the_name_alone() -> None:
    """On whole lines `A1=2` sorts before `A=1`, because a digit sorts before
    `=`. Sorted on the name, `A` comes first."""
    done = _write("A1", "A", env={"A": "1", "A1": "2"})

    assert done.returncode == 0, done.stderr
    assert done.stdout == "A=1\nA1=2\n"


def test_the_writer_prints_sorted_lines_with_each_path_folder_once() -> None:
    done = _write("YARN_VERSION", "PATH", "NODE_VERSION", "HOME", "NODE_ENV", "EMPTY")

    assert done.returncode == 0, done.stderr
    assert done.stdout == FILE


@pytest.mark.parametrize(
    ("names", "env", "error"),
    [
        ((), None, "no name"),
        (("UNSET",), None, "UNSET is not set"),
        (("1BAD",), None, "1BAD is not a variable name"),
        (("A-B",), None, "A-B is not a variable name"),
        (("TWO",), {"TWO": "a\nb"}, "the value of TWO holds a line break"),
        (("END",), {"END": "a\n"}, "the value of END holds a line break"),
        (("HOME", "HOME"), None, "HOME is listed twice"),
    ],
)
def test_the_writer_refuses_what_guest_init_cannot_read(
    names: tuple[str, ...], env: dict[str, str] | None, error: str
) -> None:
    """A refusal prints nothing to stdout, so the build never keeps half a
    file under the name of a whole one."""
    done = _write(*names, env=env)

    assert done.returncode == 1
    assert done.stdout == ""
    assert done.stderr == f"creche-env-write: {error}\n"


def test_the_check_passes_the_file_that_the_writer_prints(tmp_path: Path) -> None:
    done = _check(tmp_path, CONFIG, FILE)

    assert done.returncode == 0, done.stdout + done.stderr
    assert done.stdout == (
        "playpen-env-check: image:tag: /etc/creche/env equals the config env,"
        " and each name sits in an agent list (6 names)\n"
    )


def test_the_check_names_each_name_that_no_agent_list_holds(tmp_path: Path) -> None:
    """A parent-image name, or a new ENV name, that sits in no list of
    `agent/src/tool-env.ts` fails the flavor (CH49 rule 1)."""
    done = _check(tmp_path, CONFIG, FILE, report="6\nEMPTY\nNODE_VERSION\n")

    assert done.returncode == 1
    assert done.stderr == (
        "playpen-env-check: image:tag: no agent list holds EMPTY NODE_VERSION."
        " Put each name in one list of agent/src/tool-env.ts (CH49 rule 1).\n"
    )


def test_the_check_fails_when_node_cannot_run_the_agent(tmp_path: Path) -> None:
    """An image whose deploy tree has no `agent/tool-env` fails, and never
    passes as a flavor with no unplaced name."""
    done = _check(tmp_path, CONFIG, FILE, node_exit=1)

    assert done.returncode == 1
    assert done.stderr == (
        "playpen-env-check: node in image:tag cannot run placeEnvFile of the agent\n"
    )


@pytest.mark.parametrize(
    ("report", "read"),
    [
        pytest.param("", "no", id="no-report"),
        pytest.param("5\n", "5", id="a-short-count"),
        pytest.param("EMPTY\n", "EMPTY", id="no-count"),
    ],
)
def test_the_check_fails_a_report_that_did_not_read_each_line(
    tmp_path: Path, report: str, read: str
) -> None:
    """A node program that prints nothing, prints to stderr, or reads
    fewer lines than the file holds fails, and never passes as a flavor
    with no unplaced name."""
    done = _check(tmp_path, CONFIG, FILE, report=report)

    assert done.returncode == 1
    assert done.stderr == (
        f"playpen-env-check: placeEnvFile in image:tag read {read} lines of"
        " /etc/creche/env, not 6\n"
    )


@pytest.mark.parametrize(
    "file",
    [
        pytest.param(FILE.replace("YARN_VERSION=1.22.22\n", ""), id="a-name-is-missing"),
        pytest.param(FILE + "EXTRA=1\n", id="an-extra-name"),
        pytest.param(
            FILE.replace("/opt/test/bin:", "/opt/test/bin:/opt/py/bin:"), id="a-path-folder-twice"
        ),
        pytest.param(
            FILE.replace("NODE_ENV=production", "NODE_ENV=development"), id="a-value-differs"
        ),
        pytest.param("".join(reversed(FILE.splitlines(keepends=True))), id="not-sorted"),
    ],
)
def test_the_check_fails_a_file_that_is_not_the_config_env(tmp_path: Path, file: str) -> None:
    done = _check(tmp_path, CONFIG, file)

    assert done.returncode == 1
    assert "--- config of image:tag" in done.stdout
    assert "is not the env of its config" in done.stderr


def test_the_check_sorts_on_the_name_as_the_writer_does(tmp_path: Path) -> None:
    """The writer prints `A=1` before `A1=2`. A whole-line sort would put
    `A1=2` first and fail a correct file."""
    written = _write("A1", "A", env={"A": "1", "A1": "2"})

    done = _check(tmp_path, "A1=2\nA=1\n", written.stdout)

    assert done.returncode == 0, done.stdout + done.stderr


def test_the_check_refuses_a_config_line_that_is_no_variable(tmp_path: Path) -> None:
    done = _check(tmp_path, CONFIG + "not a variable\n", FILE)

    assert done.returncode == 1
    assert "no NAME=value line: not a variable" in done.stderr


def _flavor_steps(stage: str) -> list[str]:
    """The instructions of one flavor stage, continuation lines joined."""
    text = DOCKERFILE.read_text(encoding="utf-8")
    start = text.index(f" AS {stage}\n")
    end = text.find("\nFROM ", start)
    body = text[start:] if end == -1 else text[start:end]
    joined = re.sub(r"\\\n", " ", body)
    return [line for line in joined.splitlines()[1:] if line.strip() and not line.startswith("#")]


@pytest.mark.parametrize("stage", ["agent-sandbox", "agent-sandbox-python", "agent-sandbox-coding"])
def test_each_flavor_writes_the_env_file_in_its_last_step(stage: str) -> None:
    """An ENV line after the write would set a name the file does not hold."""
    steps = _flavor_steps(stage)

    assert "/run/creche-env-write" in steps[-1]
    assert steps[-1].endswith("> /etc/creche/env")
    assert steps[-1].startswith(
        "RUN --mount=type=bind,source=playpen/bin/creche-env-write,target=/run/creche-env-write "
    )
