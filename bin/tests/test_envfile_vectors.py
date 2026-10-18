"""The command `creche-env` gives the result of each vector of `envfile.*`.

`vectors/data/envfile/` records what `creche_env.envfile` does. A script
reads the env files only through the command, so this module runs the
command on each vector:

1. `envfile.layer`: `check` with the pair and the `.env` of the vector, then
   `get` for each catalog key of an accepted vector. A vector holds the
   home of each account it knows, so the test runs `main` in this process,
   with a lookup of those homes in place of `pwd.getpwnam`. The command
   reads no catalog key from its environment, so a vector that only the
   environment refuses exits with 0 here.
2. `envfile.raw`: `raw` for each key of an accepted vector, and for one key
   of a refused vector.
3. The bash path: `creche_env_check` and `creche_env_get` of
   `bin/lib/envfile.sh`, through the real program of the venv, for each
   vector whose pair asks no account. A stand-in of the program holds the
   words and the status of each call.

`envfile.trust` holds owners that a test cannot make. The tests of `env`
read a real file with each mode.
"""

from __future__ import annotations

import base64
import json
import os
import pwd
import shutil
import subprocess
from pathlib import Path
from typing import Any, Final

import pytest
from creche_env.cli import main
from creche_env.envfile import Fault, Text, read_pair

EX_CONFIG: Final = os.EX_CONFIG
REPO: Final = Path(__file__).resolve().parents[2]
DATA: Final = REPO / "vectors" / "data" / "envfile"
LIBRARY: Final = REPO / "bin" / "lib" / "envfile.sh"
PROGRAM: Final = REPO / ".venv" / "bin" / "creche-env"

#: The names that a vector gives its files.
DEFAULT_NAME: Final = "default.env"
EXAMPLE_NAME: Final = "example.env"
ENV_NAME: Final = ".env"
PLAIN_NAME: Final = "plain.env"

PREFIX: Final = "creche-env: "


def _vectors(name: str) -> list[dict[str, Any]]:
    document = json.loads((DATA / f"{name}.json").read_text(encoding="utf-8"))

    return document["vectors"]


LAYERS: Final = _vectors("layer")
RAWS: Final = _vectors("raw")
TRUSTS: Final = _vectors("trust")


def _bytes(form: dict[str, Any]) -> bytes:
    """The bytes of one input form of `vectors/README.md`."""
    if "text" in form:
        return form["text"].encode("utf-8")

    if "base64" in form:
        return base64.b64decode(form["base64"])

    return "".join(text * count for text, count in form["repeat"]).encode("utf-8")


def _write(path: Path, data: bytes, mode: int) -> Path:
    path.write_bytes(data)
    path.chmod(mode)

    return path


class Files:
    """The files of one layer vector, on disk."""

    def __init__(self, root: Path, args: dict[str, Any]) -> None:
        self.pair = root / "pair"
        self.pair.mkdir()
        self.env = _write(root / "site.env", _bytes(args["env"]), 0o600)
        _write(self.pair / DEFAULT_NAME, _bytes(args["default"]), 0o644)
        _write(self.pair / EXAMPLE_NAME, _bytes(args["example"]), 0o644)
        self.names = {
            DEFAULT_NAME: str(self.pair / DEFAULT_NAME),
            EXAMPLE_NAME: str(self.pair / EXAMPLE_NAME),
            ENV_NAME: str(self.env),
        }

    def lines(self, refusal: list[dict[str, Any]]) -> list[str]:
        """The lines that the command prints for the faults of a vector."""
        return [_line(self.names[one["file"]], one) for one in refusal]

    def options(self) -> list[str]:
        return ["--pair", str(self.pair), "--env", str(self.env)]


def _line(file: str, one: dict[str, Any]) -> str:
    where = f"{file}:{one['line']}" if one["line"] else file
    key = f" {one['key']}" if one["key"] else ""

    return f"{PREFIX}{where}: {one['fault']}{key}"


def _homes(monkeypatch: pytest.MonkeyPatch, homes: dict[str, str]) -> None:
    def lookup(name: str) -> pwd.struct_passwd:
        if name not in homes:
            raise KeyError(name)

        return pwd.struct_passwd((name, "x", 1234, 1234, "", homes[name], "/bin/sh"))

    monkeypatch.setattr(pwd, "getpwnam", lookup)


def _run(capsys: pytest.CaptureFixture[str], words: list[str]) -> tuple[int, str, str]:
    status = main(words)
    printed = capsys.readouterr()

    return status, printed.out, printed.err


def _only_the_environment(vector: dict[str, Any]) -> bool:
    return all(one["fault"] == Fault.ENVIRONMENT_DIFFERS for one in vector["refusal"])


def _catalog(args: dict[str, Any]) -> tuple[str, ...]:
    pair = read_pair(
        Text(DEFAULT_NAME, _bytes(args["default"])), Text(EXAMPLE_NAME, _bytes(args["example"]))
    )

    return pair.catalog()


def test_the_vectors_name_each_fault() -> None:
    named = {
        one["fault"] for vector in (*LAYERS, *RAWS, *TRUSTS) for one in vector.get("refusal", [])
    }

    assert named == {str(one) for one in Fault}


@pytest.mark.parametrize("vector", LAYERS, ids=lambda vector: vector["id"])
def test_the_command_gives_each_layer_vector(
    vector: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    args = vector["input"]["args"]
    files = Files(tmp_path, args)
    _homes(monkeypatch, args["homes"])
    status, out, err = _run(capsys, ["check", *files.options()])

    if vector["result"] == "refused" and not _only_the_environment(vector):
        assert (status, out) == (EX_CONFIG, "")
        assert err.splitlines() == files.lines(vector["refusal"])
        return

    assert (status, out, err) == (0, "", "")
    if vector["result"] != "accepted":
        return

    values: dict[str, str] = vector.get("value", {})
    for key in _catalog(args):
        wanted = (0, f"{values.get(key, '')}\n", "")
        assert _run(capsys, ["get", key, *files.options()]) == wanted, key


@pytest.mark.parametrize("vector", RAWS, ids=lambda vector: vector["id"])
def test_the_command_gives_each_raw_vector(
    vector: dict[str, Any], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _write(tmp_path / PLAIN_NAME, _bytes(vector["input"]), 0o600)

    if vector["result"] == "refused":
        status, out, err = _run(capsys, ["raw", str(path), "A"])
        assert (status, out) == (EX_CONFIG, "")
        assert err.splitlines() == [_line(str(path), one) for one in vector["refusal"]]
        return

    values: dict[str, str] = vector.get("value", {})
    for key, value in values.items():
        assert _run(capsys, ["raw", str(path), key]) == (0, f"{value}\n", "")

    assert _run(capsys, ["raw", str(path), "NOT_IN_THE_FILE"]) == (0, "", "")


# --- the bash path ---------------------------------------------------------------


def _asks_no_account(vector: dict[str, Any]) -> bool:
    return b"${home:" not in _bytes(vector["input"]["args"]["default"])


BASH_LAYERS: Final = [vector for vector in LAYERS if _asks_no_account(vector)]


def _bash(library: Path, script: str, *words: str) -> subprocess.CompletedProcess[str]:
    """`script` in bash, with the library sourced and `words` as `$@`."""
    return subprocess.run(
        ["bash", "-c", f'. "$0"; {script}', str(library), *words],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )


def test_some_vectors_ask_no_account() -> None:
    assert len(BASH_LAYERS) >= 5


@pytest.mark.parametrize("vector", BASH_LAYERS, ids=lambda vector: vector["id"])
def test_the_library_gives_each_vector_that_asks_no_account(
    vector: dict[str, Any], tmp_path: Path
) -> None:
    assert PROGRAM.is_file(), "run `uv sync`: the venv has no creche-env"
    files = Files(tmp_path, vector["input"]["args"])

    run = _bash(LIBRARY, 'creche_env_check "$@"', *files.options())

    if vector["result"] == "refused" and not _only_the_environment(vector):
        assert (run.returncode, run.stdout) == (EX_CONFIG, "")
        assert run.stderr.splitlines() == files.lines(vector["refusal"])
        return

    assert (run.returncode, run.stdout, run.stderr) == (0, "", "")
    for key, value in vector.get("value", {}).items():
        got = _bash(LIBRARY, 'creche_env_get "$@"', key, *files.options())
        assert (got.returncode, got.stdout) == (0, f"{value}\n"), key


#: A stand-in of the command: it prints its words and ends with the status
#: in `STUB_STATUS`.
STUB: Final = """#!/bin/sh
printf '%s\\n' "$@"
exit "${STUB_STATUS:-0}"
"""


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    """A tree with the library and a stand-in of the command in its venv."""
    root = tmp_path / "tree with a space"
    (root / "bin" / "lib").mkdir(parents=True)
    (root / ".venv" / "bin").mkdir(parents=True)
    shutil.copy(LIBRARY, root / "bin" / "lib" / "envfile.sh")
    _write(root / ".venv" / "bin" / "creche-env", STUB.encode(), 0o755)

    return root


def test_each_function_runs_the_command_of_its_own_tree(checkout: Path) -> None:
    library = checkout / "bin" / "lib" / "envfile.sh"

    got = _bash(library, 'creche_env_get "$@"', "AGENT_DATA_ROOT", "--env", "a file")
    checked = _bash(library, 'creche_env_check "$@"')

    assert (got.returncode, got.stdout) == (0, "get\nAGENT_DATA_ROOT\n--env\na file\n")
    assert (checked.returncode, checked.stdout) == (0, "check\n")


def test_a_cd_after_a_relative_source_changes_no_tree(checkout: Path) -> None:
    run = subprocess.run(
        ["bash", "-c", '. bin/lib/envfile.sh; cd /; creche_env_check "$@"', "bash", "--env", "x"],
        cwd=checkout,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )

    assert (run.returncode, run.stdout, run.stderr) == (0, "check\n--env\nx\n", "")


def test_a_fault_keeps_status_78(checkout: Path) -> None:
    library = checkout / "bin" / "lib" / "envfile.sh"
    env = {**os.environ, "STUB_STATUS": "78"}

    run = subprocess.run(
        [
            "bash",
            "-c",
            '. "$0"; value="$(creche_env_get KEY)" || exit "$?"; echo after',
            str(library),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
        env=env,
    )

    assert (run.returncode, run.stdout) == (EX_CONFIG, "")


def test_a_tree_with_no_command_returns_78(checkout: Path) -> None:
    (checkout / ".venv" / "bin" / "creche-env").unlink()

    run = _bash(checkout / "bin" / "lib" / "envfile.sh", "creche_env_check")

    assert run.returncode == EX_CONFIG
    assert run.stderr.splitlines() == [
        f"creche-env: {checkout.resolve()}/.venv/bin/creche-env is no program"
    ]
