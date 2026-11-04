"""`/etc/creche/env`: `playpen/bin/creche-env-write`, with no image
(CH7-7, CH49). The writer runs here under a fixed env, and the Dockerfile is
read as text.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
WRITE = REPO_ROOT / "playpen" / "bin" / "creche-env-write"
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


def _flavor_steps(stage: str) -> list[str]:
    """The instructions of one flavor stage, continuation lines joined."""
    text = DOCKERFILE.read_text(encoding="utf-8")
    start = text.index(f" AS {stage}\n")
    end = text.find("\nFROM ", start)
    body = text[start:] if end == -1 else text[start:end]
    joined = re.sub(r"\\\n", " ", body)
    return [line for line in joined.splitlines()[1:] if line.strip() and not line.startswith("#")]


@pytest.mark.parametrize("stage", ["agent-sandbox", "agent-sandbox-python"])
def test_each_flavor_writes_the_env_file_in_its_last_step(stage: str) -> None:
    """An ENV line after the write would set a name the file does not hold.
    Only CMD, which sets no variable, may follow it."""
    steps = [step for step in _flavor_steps(stage) if not step.startswith("CMD ")]

    assert "/run/creche-env-write" in steps[-1]
    assert steps[-1].endswith("> /etc/creche/env")
    assert steps[-1].startswith(
        "RUN --mount=type=bind,source=playpen/bin/creche-env-write,target=/run/creche-env-write "
    )
