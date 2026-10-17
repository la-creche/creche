"""The catalog of the product: `default.env`, `example.env` and `root-keys.txt`.

The three files are the package data of `creche-env` (`env/AGENTS.md`).
Each tree of a release carries its own copy, so these tests hold the rules
that keep two copies in agreement and the public repository clean:

1. The pair reads, and each required key has an example value that the
   reader of its key takes.
2. The two files name no IPv4 address but a documentation address and
   loopback.
3. Each key of `root-keys.txt` is a catalog key.
4. No value of `default.env` differs from its value at the merge base: a
   new value takes a new key.
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


def test_no_default_differs_from_the_merge_base() -> None:
    """Each tree carries its own copy of `default.env`, so a key that two
    copies hold must have one value in both. A new value takes a new key."""
    base = _git("merge-base", "HEAD", BASE_BRANCH)
    assert base.returncode == 0, f"no merge base with {BASE_BRANCH}: {base.stderr}"

    old = _git("show", f"{base.stdout.strip()}:{DEFAULT_PATH}")
    if old.returncode != 0:
        assert "exists on disk, but not in" in old.stderr or "does not exist" in old.stderr
        return

    before = {
        one.key: one.value
        for one in parse(Text(DEFAULT_FILE, old.stdout.encode()), FileKind.DEFAULT)
    }
    now = parse(Text(DEFAULT_FILE, (PAIR_DIR / DEFAULT_FILE).read_bytes()), FileKind.DEFAULT)
    changed = [one.key for one in now if one.key in before and before[one.key] != one.value]

    assert changed == [], "a new value takes a new key"


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
