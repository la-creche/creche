"""The verify hook's environment.

`noticeboard-verify`, `attendance-verify` and `caregiver-verify` take `--env-file
<path>` so root's executor never has to open a file the operator controls itself
(`handover/AGENTS.md` "Trust rules"): the hook reads it as its own user,
the same user the unit's `EnvironmentFile=` already trusts.

This is the ONE test that keeps a manifest's `--env-file` and its unit's
own `EnvironmentFile=` from drifting apart. The rule cuts both ways: a
component whose unit declares `EnvironmentFile=` must pass that same path
with `--env-file`, and one whose unit declares none must not claim one
either. `chaperone` reads the site file, `/etc/creche/site.env`. Its
manifest is at the root of the chaperone repository, and this repository
holds a copy (`handover_chaperone_component.toml`).
"""

from __future__ import annotations

import re
from pathlib import Path

from handover.discovery import read_one
from handover.manifest import Markup, parse_manifest

REPO_ROOT = Path(__file__).resolve().parents[2]
SYSTEMD_DIR = REPO_ROOT / "systemd"

#: The copy of the manifest that the chaperone repository holds.
CHAPERONE_MANIFEST = Path(__file__).resolve().parent / "handover_chaperone_component.toml"

ENV_FILE_RE = re.compile(r"^EnvironmentFile=(\S+)$", re.MULTILINE)

#: Component name -> the unit its verify hook's own user actually reads.
UNIT_OF = {
    "noticeboard": "creche-noticeboard.service",
    "attendance": "creche-attendance.service",
    "caregiver": "creche-caregiver.service",
}

CHAPERONE_UNIT = "creche-chaperone.service"


def _unit_env_file(unit: str) -> str | None:
    """The `EnvironmentFile=` value a unit source declares, or None."""
    text = (SYSTEMD_DIR / unit).read_text(encoding="utf-8")
    matched = ENV_FILE_RE.search(text)

    return matched.group(1) if matched else None


def _verify_env_file(command: tuple[str, ...]) -> str | None:
    """The `--env-file` value a manifest's `verify.command` carries, or
    None. `command[:-1]`: the flag always needs a word after it."""
    for index, word in enumerate(command[:-1]):
        if word == "--env-file":
            return command[index + 1]

    return None


def test_every_verify_hooks_env_file_matches_its_units_environment_file() -> None:
    """The drift-proof form: whatever `systemd/` says, `component.yaml`
    says the same, for every component either names one."""
    for component, unit in UNIT_OF.items():
        manifest = read_one(REPO_ROOT, component).manifest

        assert _verify_env_file(manifest.verify.command) == _unit_env_file(unit), component


def test_noticeboard_attendance_and_caregiver_each_name_a_real_env_file() -> None:
    """The literal paths, so a bug in the drift-proof test above (both
    sides silently None) cannot pass unnoticed."""
    expected = {
        "noticeboard": "/srv/agents/state/rework/view.env",
        "attendance": "/srv/agents/state/rework/sessiond.env",
        "caregiver": "/srv/agents/state/rework/managerd.env",
    }
    for component, path in expected.items():
        manifest = read_one(REPO_ROOT, component).manifest

        assert _verify_env_file(manifest.verify.command) == path, component
        assert _unit_env_file(UNIT_OF[component]) == path, component


def test_chaperone_names_the_site_file_on_both_sides() -> None:
    """`creche-chaperone.service` reads its LAN address from the site file,
    and `chaperone verify` reads the same file."""
    text = CHAPERONE_MANIFEST.read_text(encoding="utf-8")
    manifest = parse_manifest(text, CHAPERONE_MANIFEST.name, Markup.TOML)
    path = "/etc/creche/site.env"

    assert _unit_env_file(CHAPERONE_UNIT) == path
    assert _verify_env_file(manifest.verify.command) == path
