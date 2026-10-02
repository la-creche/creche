"""`bin/provision-indexer.sh` takes the LAN address from the site file.

The indexer's sandbox may reach TEI on the host's LAN address and nothing
else, and the image it runs fixes that address at build time. The address
is the site's (`/etc/agent-control/site.env`). `LAN_ADDRESS` still wins.

The script acts on absolute host paths, so every case here stops at the
first one: the corpus directory, which no test machine has. A case that
gets that far got past the address.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Final

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
SCRIPT: Final = REPO_ROOT / "bin" / "provision-indexer.sh"

#: A corpus no machine has, so the script stops before it builds anything.
ABSENT_CORPUS: Final = "lan-test-absent-corpus"
NO_CORPUS: Final = "no corpus dir"


def _run(**overrides: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env.pop("LAN_ADDRESS", None)
    env.update(overrides)

    return subprocess.run(
        ["bash", str(SCRIPT), ABSENT_CORPUS],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )


def test_the_site_file_supplies_the_address() -> None:
    """The root conftest points `AGENT_SITE_FILE` at the example site."""
    done = _run()

    assert NO_CORPUS in done.stderr, done.stderr


def test_no_site_file_stops_with_one_line_naming_it(tmp_path: Path) -> None:
    absent = tmp_path / "absent.env"

    done = _run(AGENT_SITE_FILE=str(absent))

    assert done.returncode != 0
    assert done.stderr.count("\n") == 1, done.stderr
    assert "AGENT_LAN_ADDRESS" in done.stderr
    assert str(absent) in done.stderr


def test_a_site_file_without_the_key_stops_the_same_way(tmp_path: Path) -> None:
    """`set -e` must not end the script in silence when the key is absent."""
    site = tmp_path / "site.env"
    site.write_text("AGENT_GITHUB_OWNER=example-owner\n", encoding="utf-8")

    done = _run(AGENT_SITE_FILE=str(site))

    assert done.returncode != 0
    assert "AGENT_LAN_ADDRESS" in done.stderr
    assert str(site) in done.stderr


def test_an_explicit_address_still_wins(tmp_path: Path) -> None:
    done = _run(AGENT_SITE_FILE=str(tmp_path / "absent.env"), LAN_ADDRESS="192.0.2.99")

    assert NO_CORPUS in done.stderr, done.stderr


def test_the_image_is_built_with_the_address() -> None:
    """A sandbox has no site file: the image carries the address
    (`indexer/Dockerfile`), and its build fails without it."""
    text = SCRIPT.read_text(encoding="utf-8")

    assert '--build-arg "AGENT_LAN_ADDRESS=$LAN_ADDRESS"' in text
