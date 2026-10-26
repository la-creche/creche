"""The install draft of `bin/chaperone-install-draft.py`, against the real `chaperone check`.

The script prints a draft of `install.toml` and one effect manifest for
each server of a registry. Its golden file holds the print for the fixture
registry of the package `family`, and `bin/tests` holds the print equal to
that file. This test reads the golden file as text, as
`test_proc_policy_check.py` reads the golden files of `caregiver`.

`chaperone check` is the only judge of the files (chaperone
`docs/design.md` §6). The test checks the draft's `install.toml` first.
Then it puts caregiver's fleet policy of `test_proc_policy_check.py` on top
of the draft and checks `policy.toml`: each upstream that the policy grants
is an active plugin of the draft.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Final

from proc_chaperone_rs import ChaperoneRsStack
from test_proc_policy_check import SERVERS_DIR, fleet_policy, subdirectories

_REPO: Final = Path(__file__).resolve().parents[2]

#: The print of the script for the fixture registry.
DRAFT_GOLDEN: Final = _REPO / "bin" / "tests" / "install_draft_golden.txt"

#: The line before each file of the print.
HEADER: Final = "--- "

#: The start of the path of each manifest that the draft adds.
ADDED: Final = "plugins/effect/added/"


def draft_files() -> dict[str, str]:
    """Each file of the golden print, by its path."""
    files: dict[str, str] = {}
    path = ""

    for line in DRAFT_GOLDEN.read_text(encoding="utf-8").splitlines(keepends=True):
        if line.startswith(HEADER):
            path = line.removeprefix(HEADER).rstrip("\n")
            files[path] = ""
            continue

        files[path] += line

    return files


def test_install_draft_passes_check(chaperone_rs_files: ChaperoneRsStack) -> None:
    """The draft's `install.toml` passes, and caregiver's fleet policy passes on top of it."""
    draft = draft_files()
    added = sorted(
        path.removeprefix(ADDED).split("/")[0] for path in draft if path != "install.toml"
    )
    chaperone_rs_files.prepare_draft(draft, fleet_policy())
    install = chaperone_rs_files.check("install.toml")
    checked = chaperone_rs_files.check("policy.toml")

    assert tuple(added) == subdirectories(SERVERS_DIR)
    assert tomllib.loads(draft["install.toml"])["slots"] == {"approval_channel": "ha-approval"}
    assert (install.exit_code, install.stdout.strip()) == (0, "install.toml: ok"), install.stderr
    assert (checked.exit_code, checked.stdout.strip()) == (0, "policy.toml: ok"), checked.stderr
