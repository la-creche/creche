"""The vmm manifest, `rust/component.toml`, against the files it names.

The manifest says what CI builds and what root installs: one archive with
four programs (decision CH7-5). Four other files name the same programs or
the same build, and each one can move alone:

1. `bin/lib/chbuild.sh` holds the one CH build of decision CH35 rule 3.
   The kvm job runs it (CH75). The third build argv of the manifest must
   be the same words, so the kvm job tests the CH that ships.
2. `env/src/creche_env/default.env` names three programs of the tree by
   their VMM_*_PROGRAM keys (CH62 rule 17).
3. `systemd/creche-vmm.service` starts the vmm and its stop hook from the
   tree (CH36 G17).
4. `rust/crates/vmm-confine/apparmor/cloud-hypervisor-slots.profile` does
   not ship with the release. A change to it is a one-off sudo step on the
   host (CH76 rule 9), so its SHA-256 is pinned here.

The verify `--env-file` of the manifest is held equal to the unit's
`EnvironmentFile=` by handover/tests/test_handover_r7r_verify_env.py.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Final

from handover.discovery import read_one
from handover.manifest import ComponentManifest
from test_artifactrows import CH_BUILD_WORDS

REPO: Final = Path(__file__).resolve().parents[2]
DEFAULT_ENV: Final = REPO / "env" / "src" / "creche_env" / "default.env"
UNIT: Final = REPO / "systemd" / "creche-vmm.service"
SLOT_PROFILE: Final = (
    REPO / "rust" / "crates" / "vmm-confine" / "apparmor" / "cloud-hypervisor-slots.profile"
)

#: The SHA-256 of the slot profile file (CH76 rule 9). A PR that changes
#: the profile changes this pin, and its description names the one-off
#: sudo step of design section 6.1 step 2: the `install` of the file to
#: /etc/apparmor.d/opt.components.vmm.bin.cloud-hypervisor and
#: `apparmor_parser -r`. The vmm release never runs sudo (CH36 G20).
SLOT_PROFILE_SHA256: Final = "3bc768e0e4eb3239922181502593c56864c431c81560142bcdc43ba7dab00532"

#: The four programs of the vmm row, in build order (CH7-5).
PROGRAMS: Final = ("vmm", "guest-init", "cloud-hypervisor", "ch-remote")

#: The keys of default.env that name a program of the tree, with the
#: program each one names. No key names the vmm: the unit starts it.
PROGRAM_KEYS: Final = {
    "VMM_GUEST_INIT_PROGRAM": "guest-init",
    "VMM_CH_PROGRAM": "cloud-hypervisor",
    "VMM_CH_REMOTE_PROGRAM": "ch-remote",
}

#: The site key of the install root, and how default.env writes it.
INSTALL_ROOT_KEY: Final = "AGENT_INSTALL_ROOT"


def _manifest() -> ComponentManifest:
    return read_one(REPO, "vmm").manifest


def _bin_dir() -> str:
    return f"{_manifest().install.to}/bin"


def _defaults() -> dict[str, str]:
    """Each `KEY=value` line of default.env, comments skipped."""
    found: dict[str, str] = {}
    for line in DEFAULT_ENV.read_text(encoding="utf-8").splitlines():
        key, sign, value = line.partition("=")
        if sign and not key.startswith("#"):
            found[key] = value

    return found


def _unit_programs(key: str) -> list[str]:
    """The first word of each value of `key` in the unit, comments
    skipped."""
    found: list[str] = []
    for line in UNIT.read_text(encoding="utf-8").splitlines():
        name, sign, value = line.partition("=")
        if sign and name == key:
            found.append(value.split()[0])

    return found


def _built_programs() -> list[str]:
    """The word after each `--bin` of each build argv, in order."""
    return [
        argv[at + 1]
        for argv in _manifest().build
        for at, word in enumerate(argv[:-1])
        if word == "--bin"
    ]


def test_the_third_argv_is_the_chbuild_words() -> None:
    """One source for the CH argv: bin/lib/chbuild.sh (CH35 (3), CH75).
    The first two argv lists install the vmm and guest-init from the
    workspace."""
    vmm, guest_init, ch = _manifest().build

    assert ch == CH_BUILD_WORDS
    assert vmm[-4:] == ("--path", "rust/crates/vmm", "--bin", "vmm")
    assert guest_init[-4:] == ("--path", "rust/crates/guest-init", "--bin", "guest-init")


def test_the_build_makes_the_four_programs() -> None:
    assert tuple(_built_programs()) == PROGRAMS


def test_the_programs_match_the_defaults_and_the_unit() -> None:
    """Three programs are the VMM_*_PROGRAM defaults under `install.to/bin`
    (CH62 rule 17). The vmm is the program of the unit's ExecStart=,
    because no key names it."""
    defaults = _defaults()
    root = defaults[INSTALL_ROOT_KEY]
    named = {
        defaults[key].replace(f"${{{INSTALL_ROOT_KEY}}}", root): program
        for key, program in PROGRAM_KEYS.items()
    }

    assert _manifest().install.to == f"{root}/vmm"
    assert named == {f"{_bin_dir()}/{program}": program for program in PROGRAMS[1:]}
    assert _unit_programs("ExecStart") == [f"{_bin_dir()}/{PROGRAMS[0]}"]


def test_the_unit_starts_programs_of_the_tree() -> None:
    """Each ExecStart= and ExecStopPost= of the unit starts a program that
    the build makes (handover/AGENTS.md, Binary builds rule 4)."""
    started = [*_unit_programs("ExecStart"), *_unit_programs("ExecStopPost")]
    built = {f"{_bin_dir()}/{program}" for program in _built_programs()}

    assert started
    assert set(started) <= built
    assert _manifest().unit == UNIT.name


def test_the_verify_program_is_the_vmm_of_the_tree() -> None:
    """`vmm verify` runs out of the new tree, as the operator (CH76 rule 2)."""
    verify = _manifest().verify

    assert verify.command[:2] == (f"{_bin_dir()}/vmm", "verify")
    assert str(verify.user) == "operator"


def test_the_slot_profile_is_pinned() -> None:
    """A profile change is never silent (CH76 rule 9). When this fails,
    change the pin and name the sudo step in the PR description."""
    found = hashlib.sha256(SLOT_PROFILE.read_bytes()).hexdigest()

    assert found == SLOT_PROFILE_SHA256, "the slot profile changed: see SLOT_PROFILE_SHA256"
