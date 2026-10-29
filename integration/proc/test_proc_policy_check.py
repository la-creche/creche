"""caregiver's `policy.toml` for the whole fleet, against the real `chaperone check`.

caregiver renders one app table for each family of the fixture registry of
the package `family`. Its golden files hold each rendering, and its test
`test_fixture_family_matches_its_golden` holds each golden equal to the
output of `render_policy`. A test here imports no package of a service
(`integration/proc/AGENTS.md`, rule 2). So the test reads the golden files
as text and joins them into one file, as `render_policy` joins the apps of
the fleet.

The install file holds one effect plugin for each server of the fixture
registry, plus the plugins `attendance` and `release` (chaperone
`docs/design.md` §12). `chaperone check` is the only judge of the two files
(§6). Each negative scenario changes one thing in the joined file and reads
the error line of the check.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Final

from proc_chaperone_rs import ChaperoneRsStack
from proc_harness import Finished

_REPO: Final = Path(__file__).resolve().parents[2]

#: The rendering of each fixture family, one file per family.
GOLDEN_DIR: Final = _REPO / "caregiver" / "tests" / "policy_golden"

#: The fixture registry: one directory per family and one per server
#: (contract 01 §1).
_REGISTRY: Final = _REPO / "family" / "tests" / "fixtures" / "registry"
FAMILIES_DIR: Final = _REGISTRY / "families"
SERVERS_DIR: Final = _REGISTRY / "mcp"

#: The first line of each golden file and of the joined file.
VERSION_LINE: Final = "version = 1\n"

#: A plugin name that no install file of this test holds.
UNKNOWN_UPSTREAM: Final = "weather"

#: The app instances of the fixture registry that delegate, and the one
#: target that each of them names. No target delegates.
DELEGATE_CALLER: Final = "code"
DELEGATE_TARGET: Final = "vault-oracle"
SECOND_HOP: Final = "coding-sandbox"

#: The caller that the refusal names: the check reads the apps in name
#: order, and `chat` delegates to the same target before `code` does.
FIRST_CALLER: Final = "chat"


def golden_files() -> list[Path]:
    """One golden file per family, in app ID order (the order of `render_policy`)."""
    return sorted(GOLDEN_DIR.glob("*.toml"), key=lambda path: path.stem)


def fleet_policy() -> str:
    """The golden files joined: one version line, then each app table."""
    parts = [VERSION_LINE]

    for path in golden_files():
        text = path.read_text(encoding="utf-8")
        assert text.startswith(VERSION_LINE), f"{path} does not start with {VERSION_LINE!r}"
        parts.append(text.removeprefix(VERSION_LINE))

    return "".join(parts)


def subdirectories(parent: Path) -> tuple[str, ...]:
    return tuple(sorted(path.name for path in parent.iterdir() if path.is_dir()))


def check_fleet(stack: ChaperoneRsStack, policy: str) -> tuple[Finished, Finished]:
    """Write the tree, then run `chaperone check` on `install.toml` and on `policy.toml`."""
    stack.prepare_fleet(subdirectories(SERVERS_DIR), policy)

    return stack.check("install.toml"), stack.check("policy.toml")


def check_fails(stack: ChaperoneRsStack, policy: str) -> str:
    """The error line of a policy file that the check refuses. The install file passes."""
    install, checked = check_fleet(stack, policy)

    assert install.exit_code == 0, install.stderr
    assert checked.exit_code != 0, checked.stdout

    return checked.stderr.strip()


def test_caregiver_policy_passes_check(chaperone_rs_files: ChaperoneRsStack) -> None:
    """Every fixture family is one app of the joined file, and both files pass."""
    policy = fleet_policy()
    install, checked = check_fleet(chaperone_rs_files, policy)

    assert tuple(sorted(tomllib.loads(policy)["apps"])) == subdirectories(FAMILIES_DIR)
    assert (install.exit_code, install.stdout.strip()) == (0, "install.toml: ok"), install.stderr
    assert (checked.exit_code, checked.stdout.strip()) == (0, "policy.toml: ok"), checked.stderr


def test_a_digest_of_two_apps_fails_check(chaperone_rs_files: ChaperoneRsStack) -> None:
    """`code` takes the digest of `chat`: no token may open two apps (§9)."""
    policy = fleet_policy()
    apps = tomllib.loads(policy)["apps"]
    shared = policy.replace(apps["code"]["token_sha256"][0], apps["chat"]["token_sha256"][0])

    assert check_fails(chaperone_rs_files, shared) == (
        "policy.toml: the apps chat and code share a token digest"
    )


def test_a_grant_of_an_unknown_upstream_fails_check(chaperone_rs_files: ChaperoneRsStack) -> None:
    """`chat` grants a tool of a plugin that the install file does not hold."""
    policy = fleet_policy() + f"\n[apps.chat.grants.{UNKNOWN_UPSTREAM}.tools]\nforecast = {{}}\n"

    assert check_fails(chaperone_rs_files, policy) == (
        f"policy.toml: the app chat grants tools of {UNKNOWN_UPSTREAM}, "
        "which is not an active upstream"
    )


def test_a_two_hop_delegate_fails_check(chaperone_rs_files: ChaperoneRsStack) -> None:
    """The target of a delegate delegates too: the `attendance` fence allows one hop (§12)."""
    policy = fleet_policy()
    assert tomllib.loads(policy)["apps"][DELEGATE_CALLER]["plugins"]["attendance"] == {
        "delegates": [DELEGATE_TARGET]
    }
    policy += f'\n[apps.{DELEGATE_TARGET}.plugins.attendance]\ndelegates = ["{SECOND_HOP}"]\n'

    assert check_fails(chaperone_rs_files, policy) == (
        "policy.toml: the fence attendance refuses the policy: "
        f"the caller app {FIRST_CALLER} and the target app {DELEGATE_TARGET} break a rule: "
        "one_hop: an app instance that another app instance delegates to must not delegate"
    )
