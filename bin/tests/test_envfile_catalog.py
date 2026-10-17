"""The catalog of the product: `default.env`, `example.env` and `root-keys.txt`.

The three files are the package data of `creche-env` (`env/AGENTS.md`).
A release puts one copy of them into each tree that it installs. These
tests keep the copies of two trees compatible and keep the public
repository clean:

1. The pair reads, and each required key has an example value that the
   reader of its key takes.
2. The two files name no IPv4 address but a documentation address and
   loopback.
3. Each key of `root-keys.txt` is a catalog key.
4. A key of `default.env` keeps each value that an earlier version on the
   base branch gave it. Another value needs another key.
5. A copy of `example.env` as the `.env` stops the command with status 78
   and one line for each required key.
"""

from __future__ import annotations

import ipaddress
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Final

import pytest
from creche_env.envfile import (
    DEFAULT_FILE,
    EXAMPLE_FILE,
    FileKind,
    Text,
    load,
    pair_in,
    parse,
    root_keys_in,
)

from handover import site

REPO: Final = Path(__file__).resolve().parents[2]
PAIR_DIR: Final = REPO / "env" / "src" / "creche_env"
PROGRAM: Final = REPO / ".venv" / "bin" / "creche-env"
DEFAULT_PATH: Final = f"env/src/creche_env/{DEFAULT_FILE}"

#: The branch that a pull request merges into.
BASE_BRANCH: Final = "origin/main"

#: The reader of the type of each required key: the readers of the site
#: file in `handover`. A new required key needs a reader here.
TYPES: Final = {
    "AGENT_GITHUB_OWNER": site.github_owner,
    "AGENT_OPERATOR_USER": site.operator_user,
    "AGENT_LAN_ADDRESS": site.lan_address,
}

#: Each address of the two files is in one of these networks.
ALLOWED_NETWORKS: Final = (
    ipaddress.ip_network("192.0.2.0/24"),
    ipaddress.ip_network("127.0.0.0/8"),
)

#: Four numbers with dots, and no digit or dot on either side.
DOTTED_QUAD: Final = re.compile(r"(?<![0-9.])[0-9]+(?:\.[0-9]+){3}(?![0-9.])")

#: The home of the example account, for the account home layer.
EXAMPLE_HOME: Final = "/home/operator"


def _example_env() -> str:
    pair = pair_in(PAIR_DIR)

    return "".join(f"{one.key}={one.value}\n" for one in pair.required)


def test_the_required_keys_are_the_keys_with_a_reader() -> None:
    assert [one.key for one in pair_in(PAIR_DIR).required] == list(TYPES)


def test_the_pair_loads_with_the_example_values() -> None:
    pair = pair_in(PAIR_DIR)
    settings = load(pair, Text(".env", _example_env().encode()), {"operator": EXAMPLE_HOME}.get)

    assert settings.values["AGENT_OPERATOR_HOME"] == EXAMPLE_HOME
    assert settings.values["AGENT_PEP_URL"] == "http://192.0.2.10:8300"
    with_a_default = {one.key for one in pair.defaults if one.value}
    assert with_a_default <= set(settings.values)


def test_each_example_value_is_a_value_of_its_type(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "site.env"
    path.write_text(_example_env(), encoding="utf-8")
    path.chmod(0o600)
    monkeypatch.setenv(site.SITE_FILE_ENV, str(path))
    site.forget()
    try:
        for one in pair_in(PAIR_DIR).required:
            assert TYPES[one.key]() == one.value, one.key
    finally:
        site.forget()


@pytest.mark.parametrize("name", [DEFAULT_FILE, EXAMPLE_FILE])
def test_no_address_outside_the_documentation_and_loopback_networks(name: str) -> None:
    text = (PAIR_DIR / name).read_text(encoding="utf-8")
    for match in DOTTED_QUAD.finditer(text):
        address = ipaddress.ip_address(match.group())
        assert any(address in network for network in ALLOWED_NETWORKS), match.group()


def test_each_root_key_is_a_catalog_key() -> None:
    keys = root_keys_in(PAIR_DIR, pair_in(PAIR_DIR))

    assert "AGENT_OPERATOR_USER" in keys


def _git(*words: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(REPO), *words], capture_output=True, text=True, check=False, timeout=60
    )


def _assignments(text: str) -> dict[str, str]:
    """Each `KEY=VALUE` line of one earlier version of `default.env`. A
    later rule of the grammar can refuse an earlier version, so this reads
    only the form of an assignment."""
    found: dict[str, str] = {}
    for line in text.splitlines():
        if line and not line.startswith("#"):
            key, _, value = line.partition("=")
            found[key] = value

    return found


def test_no_default_differs_from_an_earlier_value() -> None:
    """Two trees of two releases hold two copies of `default.env`. They
    read one key in the same way only when its value never moves. The test
    reads each version of the file in the history of the merge base, so a
    key that a change removed cannot come back with another value."""
    base = _git("merge-base", "HEAD", BASE_BRANCH)
    assert base.returncode == 0, f"no merge base with {BASE_BRANCH}: {base.stderr}"

    commits = _git("log", "--format=%H", base.stdout.strip(), "--", DEFAULT_PATH)
    assert commits.returncode == 0, commits.stderr

    now = parse(Text(DEFAULT_FILE, (PAIR_DIR / DEFAULT_FILE).read_bytes()), FileKind.DEFAULT)
    changed: list[str] = []
    for commit in commits.stdout.split():
        old = _git("show", f"{commit}:{DEFAULT_PATH}")
        if old.returncode != 0:
            continue

        before = _assignments(old.stdout)
        changed.extend(
            f"{one.key} ({commit})"
            for one in now
            if one.key in before and before[one.key] != one.value
        )

    assert changed == [], "another value needs another key"


def test_a_copy_of_example_env_names_each_required_key(tmp_path: Path) -> None:
    assert PROGRAM.is_file(), "run `uv sync`: the venv has no creche-env"
    copy = tmp_path / "site.env"
    shutil.copy(PAIR_DIR / EXAMPLE_FILE, copy)
    copy.chmod(0o600)

    run = subprocess.run(
        [str(PROGRAM), "check", "--env", str(copy)],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )

    assert (run.returncode, run.stdout) == (os.EX_CONFIG, "")
    named = [line.rsplit(" ", 1)[-1] for line in run.stderr.splitlines()]
    assert named == list(TYPES)
    assert all(": required-unset " in line for line in run.stderr.splitlines())
