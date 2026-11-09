"""What `caregiver` publishes, read as the next program reads it.

`integration/tests_manager` runs the writers of `caregiver` inside the test
process, with a fake driver and a fake LiteLLM. Here `caregiver` is a
process. An assertion reads a file that another program reads, or the record
or the state of a stand-in.

A scenario that `integration/tests_manager` also has keeps its name. Such a
name can say `apply`: here `caregiver serve` does that work.

Each scenario runs `caregiver` alone, beside its stand-ins. It starts no
VM: the vmm starts one VM per session (decisions CH1, CH9).
"""

from __future__ import annotations

import hashlib
import json
import re
import stat
import time
from pathlib import Path
from typing import Any

from proc_caregiver import (
    DEGRADED,
    EXIT_OK,
    IN_SYNC,
    WRITE_FLAG,
    CaregiverStack,
    chaperone_flags,
    litellm_url,
    text_of,
    wait_until,
)
from proc_standins import (
    LITELLM,
    MASTER_KEY,
    SYSTEMCTL,
    calls_of,
    enabled_units,
    litellm_calls,
    litellm_keys,
    tune,
)
from proc_tree import (
    BUDGET_USD,
    FAMILY,
    MODEL_ALIAS,
    SECRET_MODE,
    STATUS_MODE,
    family_body,
    publish_family,
    remove_family,
    write_family_file,
    write_family_prose,
    write_skill,
)

OTHER = "ops"
WAKER = "waker"
GHOST = "ghost"
SKILL = "kitchen"
SKILL_TEXT = "# kitchen\n\nA fixture skill.\n"
NEW_INSTRUCTIONS = "Answer only in metric units.\n"

#: Two hosts for an egress list. `.example` names resolve nowhere. The
#: second is a bare host in upper case.
FIRST_HOST = "first.example:8443"
SECOND_HOST = "Second.example"

#: Contract 01 §6.1.1 rule 1: the mode of each reach file.
REACH_FILE_MODE = 0o640

#: Contract 01 §3.13: five fields. Each working day at nine.
CRON = "0 9 * * 1-5"
ON_CALENDAR = "OnCalendar=Mon..Fri *-*-* 09:00:00"
WEBHOOK = "wake"

#: Contract 05 §2.1: each field of a status document.
STATUS_FIELDS = {
    "family",
    "kind",
    "state",
    "written_at",
    "registry_rev",
    "applied_rev",
    "config_rev",
    "validation",
    "faults",
    "reconcile",
    "credentials",
    "spend",
    "limits",
    "triggers",
    "pep",
}

RFC3339 = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")

#: How long a scenario waits to see that nothing happens: five looks.
QUIET_S = 1.0


# ------------------------------------------------------- the status document


def test_the_status_document_holds_each_field(caregiver_alone: CaregiverStack) -> None:
    """Contract 05 §2 rule 3, §2.1, §2.2 and §4.1. One whole document, mode 0644."""
    tree = caregiver_alone.tree
    document = _status(caregiver_alone)

    assert set(document) == STATUS_FIELDS
    assert _mode(tree.status_file()) == STATUS_MODE
    assert (document["family"], document["kind"]) == (FAMILY, "attended")
    assert RFC3339.fullmatch(document["written_at"])
    # CONTRACT-QUESTION: contract 01 §6.1 gives the config mount one revision
    # counter of its own, and no contract says which value it holds. Reading
    # taken: at `in_sync` it holds the registry revision, in the form that
    # the document of contract 05 §9 shows. A service with another counter
    # costs the last term of this assertion.
    assert document["registry_rev"] == document["applied_rev"] == document["config_rev"]
    assert document["faults"] == []
    assert document["reconcile"] is None
    assert document["limits"]["max_queued_turns"] == 100
    assert document["triggers"] == {"webhooks": [], "enqueue": False}
    # §2.2 rule 1: a `caregiver` with no chaperone address says `off`, never nothing.
    assert document["pep"] == {
        "watch": "off",
        "url": "",
        "checked_at": None,
        "unreachable_since": None,
    }
    assert document["validation"]["ok"] is True
    assert document["validation"]["report_path"] == str(tree.validation_file())
    assert tree.validation_file().is_file()


def test_the_spend_of_litellm_is_in_the_document(caregiver_prepared: CaregiverStack) -> None:
    """Contract 05 §7. LiteLLM is the authority for spend, and the document names it."""
    stack = caregiver_prepared
    tune(stack.tree, LITELLM, f"spend-family-{FAMILY}", "2.5")

    stack.spawn_caregiver()
    stack.await_published()
    spend = _status(stack)["spend"]

    assert (spend["spend_usd"], spend["budget_usd"]) == (2.5, float(BUDGET_USD))
    assert (spend["window"], spend["source"]) == ("day", "litellm")
    assert RFC3339.fullmatch(spend["as_of"])


# ---------------------------------------------------- the policy and the key


def test_the_policy_holds_a_digest_and_never_the_token(caregiver_alone: CaregiverStack) -> None:
    """A stolen `policy.toml` or a stolen fragment grants nothing."""
    tree = caregiver_alone.tree
    token = caregiver_alone.credentials()["pep_token"]

    assert token not in tree.policy_file.read_text(encoding="utf-8")
    assert token not in tree.policy_fragment().read_text(encoding="utf-8")
    assert caregiver_alone.apps()[FAMILY]["token_sha256"] == [
        hashlib.sha256(token.encode("utf-8")).hexdigest()
    ]


def test_the_key_is_minted_one_time(caregiver_alone: CaregiverStack) -> None:
    """Contract 05 §6.2: mint one time, with the alias, the model and the budget.

    A later pass mints no second key. The master key is the bearer of the
    mint, and the family key is the bearer of the spend read.
    """
    stack = caregiver_alone
    instructions = stack.tree.mounts().config / "instructions.md"

    write_family_prose(stack.tree, text=NEW_INSTRUCTIONS)
    wait_until(lambda: text_of(instructions) == NEW_INSTRUCTIONS, "a new pass")

    mints = [call for call in litellm_calls(stack.tree) if call["path"] == "/key/generate"]
    assert [call["as"] for call in mints] == ["master"]
    assert mints[0]["body"] == {
        "key_alias": f"family-{FAMILY}",
        "models": [MODEL_ALIAS],
        "max_budget": BUDGET_USD,
        "budget_duration": "1d",
    }
    reads = [call for call in litellm_calls(stack.tree) if call["path"] == "/key/info"]
    assert {call["as"] for call in reads} == {"key"}
    assert stack.credentials()["litellm_key"] == litellm_keys(stack.tree)[f"family-{FAMILY}"]["key"]


def test_no_secret_is_in_an_output_or_in_a_shared_file(caregiver_alone: CaregiverStack) -> None:
    """Invariant 13. No secret on a command line, in a log line or in a shared file."""
    stack = caregiver_alone
    assert stack.caregiver is not None
    credentials = stack.credentials()
    secrets = [MASTER_KEY, credentials["litellm_key"], credentials["pep_token"]]
    places = {
        "the output of caregiver": stack.caregiver.output(),
        "the status document": stack.tree.status_file().read_text(encoding="utf-8"),
        "the policy file": stack.tree.policy_file.read_text(encoding="utf-8"),
        "the policy fragment": stack.tree.policy_fragment().read_text(encoding="utf-8"),
        "the validation report": stack.tree.validation_file().read_text(encoding="utf-8"),
        "the egress file": stack.tree.egress_file().read_text(encoding="utf-8"),
        "the repos file": stack.tree.repos_file().read_text(encoding="utf-8"),
    }

    for place, text in places.items():
        for secret in secrets:
            assert secret not in text, f"a secret is in {place}"


# ------------------------------------------------- the credentials and the config


def test_apply_writes_the_credentials_and_the_config(caregiver_prepared: CaregiverStack) -> None:
    """Contract 03 §12 and contract 01 §6.1, before the vmm reads them (decision CH5b)."""
    stack = caregiver_prepared
    tree = stack.tree
    write_skill(tree, SKILL, SKILL_TEXT)
    write_family_file(tree, family_body(skills=[SKILL], sandbox_tools=["read", "grep"]))

    stack.spawn_caregiver()
    stack.await_published()
    mounts = tree.mounts()
    credentials = stack.credentials()
    runtime = json.loads((mounts.config / "runtime.json").read_text(encoding="utf-8"))

    assert _mode(tree.creds_file()) == SECRET_MODE
    assert credentials["epoch"] == 1
    assert credentials["litellm_key"].startswith("sk-")
    assert credentials["pep_token"]
    assert RFC3339.fullmatch(credentials["written_at"])
    assert (runtime["shell"], runtime["sandbox_tools"]) == (False, ["read", "grep"])
    assert runtime["model_alias"] == MODEL_ALIAS
    assert (mounts.config / "instructions.md").is_file()
    assert (mounts.config / "skills" / SKILL / "SKILL.md").read_text(encoding="utf-8") == SKILL_TEXT


# ----------------------------------------------------------- the reach files


def test_an_egress_edit_lands_in_the_egress_file(caregiver_prepared: CaregiverStack) -> None:
    """Contract 01 §6.1.1, decisions CH6-2 and CH69 rules 3 and 5.

    Each host is in lower case with its port, and the planes never enter the
    file. The vmm reads it on each check and each CONNECT, so an edit needs
    no new VM. A family with no `repos` gets an empty clone list.
    """
    stack = caregiver_prepared
    tree = stack.tree
    write_family_file(tree, family_body(egress=[FIRST_HOST]))
    stack.spawn_caregiver()
    stack.await_published()
    first = _json(tree.egress_file())

    write_family_file(tree, family_body(egress=[SECOND_HOST]))
    second = {"hosts": [{"host": "second.example", "port": 443}]}
    wait_until(lambda: _json(tree.egress_file()) == second, f"{SECOND_HOST} in the egress file")

    assert first == {"hosts": [{"host": "first.example", "port": 8443}]}
    assert _json(tree.repos_file()) == {"repos": []}
    assert _mode(tree.egress_file()) == _mode(tree.repos_file()) == REACH_FILE_MODE
    wait_until(lambda: stack.state() == IN_SYNC, f"{FAMILY} to be {IN_SYNC}")


# ---------------------------------------------------------------- two families


def test_a_removed_family_file_ends_the_family(caregiver_prepared: CaregiverStack) -> None:
    """Contract 05 §4.4 and invariant 13. A family with no file is a family that is gone.

    Its key, its app table and its state go, and the vmm drops each of its
    sessions (decision CH21-10). The other family keeps each of its own.
    """
    stack = caregiver_prepared
    tree = stack.tree
    publish_family(tree, family_body(OTHER))
    stack.spawn_caregiver()
    stack.await_published()
    stack.await_published(OTHER)

    remove_family(tree, OTHER)
    wait_until(lambda: not tree.family_dir(OTHER).exists(), f"the state of {OTHER} to go")

    assert list(litellm_keys(tree)) == [f"family-{FAMILY}"]
    assert not tree.policy_fragment(OTHER).exists()
    assert list(stack.apps()) == [FAMILY]
    assert stack.dropped() == [OTHER]
    wait_until(lambda: stack.state() == IN_SYNC, f"{FAMILY} to be {IN_SYNC}")
    assert tree.policy_fragment().is_file()


def test_a_directory_with_no_family_file_is_ignored(caregiver_alone: CaregiverStack) -> None:
    """Contract 01 §5.6 rule 3. Such a directory is a warning, and it is no family.

    `caregiver` reads the new registry: the document of the real family gets
    the new revision. The directory gets no document and no key, and no
    family is dropped.
    """
    stack = caregiver_alone
    assert stack.caregiver is not None
    first_rev = _status(stack)["registry_rev"]

    write_family_prose(stack.tree, GHOST, "A directory with prose and no family file.\n")
    # `caregiver` publishes the new revision at the start of the pass, with
    # the state `reconciling`. So one wait holds the revision and the state.
    wait_until(
        lambda: _status(stack)["registry_rev"] != first_rev and stack.state() == IN_SYNC,
        f"{FAMILY} to be {IN_SYNC} at the new registry",
    )

    assert stack.tree.status(GHOST) is None
    assert list(litellm_keys(stack.tree)) == [f"family-{FAMILY}"]
    assert stack.dropped() == []
    assert stack.caregiver.exit_code() is None


def test_an_empty_registry_deletes_no_family(caregiver_alone: CaregiverStack) -> None:
    """A registry with no family can be a mount that went away. Nothing is deleted.

    The family file comes back with new instructions. `caregiver` lands the
    edit on the key that it had, and the vmm dropped nothing.
    """
    stack = caregiver_alone
    tree = stack.tree
    body = family_body()
    instructions = tree.mounts().config / "instructions.md"

    # CONTRACT-QUESTION: contract 05 §4.4 gives the order of a family
    # deletion, and no contract says what a registry with no family means.
    # Reading taken: the rule of `caregiver/AGENTS.md`, that it deletes no
    # family. A wrong delete ends each key and each session of the host. A
    # service that deletes costs each assertion of this scenario.
    remove_family(tree, FAMILY)
    time.sleep(QUIET_S)
    publish_family(tree, body, NEW_INSTRUCTIONS)
    wait_until(lambda: text_of(instructions) == NEW_INSTRUCTIONS, "a new pass")

    mints = [call for call in litellm_calls(tree) if call["path"] == "/key/generate"]
    assert len(mints) == 1
    assert [call for call in litellm_calls(tree) if call["path"] == "/key/delete"] == []
    assert stack.dropped() == []


# --------------------------------------------------------------------- faults


def test_a_refused_mint_is_a_fault_and_writes_no_credential(
    caregiver_prepared: CaregiverStack,
) -> None:
    """Contract 05 §3.3, `key_mint_failed`. Nothing depends on a key that does not exist."""
    stack = caregiver_prepared
    tune(stack.tree, LITELLM, "fail-generate")

    stack.spawn_caregiver()
    wait_until(lambda: stack.state() == DEGRADED, f"{FAMILY} to be {DEGRADED}")
    fault = _one_fault(_status(stack), "key_mint_failed")

    assert fault["blocks_turns"] is True
    assert not stack.tree.creds_file().exists()
    assert not stack.tree.policy_fragment().exists()
    assert stack.apps() == {}


# --------------------------------------------------------------------- timers


def test_a_cron_trigger_becomes_an_enabled_timer(caregiver_prepared: CaregiverStack) -> None:
    """Contract 01 §3.13. One timer unit for each cron trigger, enabled.

    The unit is in the directory of the user manager. It names the template
    service of the family, and `systemctl --user` enabled it.
    """
    stack = caregiver_prepared
    unit = f"creche-door-trigger-{WAKER}-t1.timer"
    publish_family(stack.tree, _autonomous([{"cron": CRON}]))

    stack.spawn_caregiver()
    stack.await_published(WAKER)
    text = (stack.tree.unit_dir / unit).read_text(encoding="utf-8")

    assert ON_CALENDAR in text.splitlines()
    assert f"Unit=creche-door-trigger@{WAKER}.service" in text.splitlines()
    assert enabled_units(stack.tree) == [unit]
    assert _systemctl(stack)[:2] == [
        ("--user", "daemon-reload"),
        ("--user", "enable", "--now", unit),
    ]
    assert _status(stack, WAKER)["faults"] == []


def test_a_removed_trigger_disables_its_timer(caregiver_prepared: CaregiverStack) -> None:
    """A trigger that the file no longer names stops at once: disable, then remove.

    The webhook that takes its place gets a bearer in a file, and the status
    document names the file and never the bearer (contract 05 §6.4).
    """
    stack = caregiver_prepared
    unit = f"creche-door-trigger-{WAKER}-t1.timer"
    publish_family(stack.tree, _autonomous([{"cron": CRON}]))
    stack.spawn_caregiver()
    stack.await_published(WAKER)

    write_family_file(stack.tree, _autonomous([{"webhook": WEBHOOK}]))
    wait_until(lambda: not (stack.tree.unit_dir / unit).exists(), f"{unit} to go")
    wait_until(lambda: _status(stack, WAKER)["triggers"]["webhooks"] != [], "the webhook row")

    assert enabled_units(stack.tree) == []
    assert ("--user", "disable", "--now", unit) in _systemctl(stack)
    [row] = _status(stack, WAKER)["triggers"]["webhooks"]
    token = Path(row["token_path"])
    assert row["name"] == WEBHOOK
    assert _mode(token) == SECRET_MODE
    assert token.read_text(encoding="utf-8").strip() not in json.dumps(_status(stack, WAKER))


def test_a_timer_that_does_not_enable_is_a_fault(caregiver_prepared: CaregiverStack) -> None:
    """Contract 05 §3.3, `timer_enable_failed`. A schedule that does not run is said.

    The fault stops no turn: a lost wake is no permission.
    """
    stack = caregiver_prepared
    unit = f"creche-door-trigger-{WAKER}-t1.timer"
    tune(stack.tree, SYSTEMCTL, f"refuse-enable-{unit}")
    publish_family(stack.tree, _autonomous([{"cron": CRON}]))

    stack.spawn_caregiver()
    wait_until(lambda: stack.state(WAKER) == DEGRADED, f"{WAKER} to be {DEGRADED}")
    fault = _one_fault(_status(stack, WAKER), "timer_enable_failed")

    assert fault["blocks_turns"] is False
    assert enabled_units(stack.tree) == []


# ------------------------------------------------------------- the rotation


def test_a_rotation_writes_a_new_token_and_a_new_key(caregiver_alone: CaregiverStack) -> None:
    """Contract 05 §6.3. The `rotate` verb writes a new key and a new token with the next epoch."""
    stack = caregiver_alone
    tree = stack.tree
    before = stack.credentials()

    done = stack.run_caregiver(
        "rotate",
        str(tree.registry_root),
        FAMILY,
        "--state-root",
        str(tree.state_root),
        "--litellm-base-url",
        litellm_url(stack.litellm_port),
        *chaperone_flags(tree),
        WRITE_FLAG,
    )
    after = stack.credentials()

    assert done.exit_code == EXIT_OK, done.stderr
    assert after["epoch"] == before["epoch"] + 1
    assert after["pep_token"] != before["pep_token"]
    assert after["litellm_key"] != before["litellm_key"]
    assert after["litellm_key"] in [entry["key"] for entry in litellm_keys(tree).values()]
    assert _mode(tree.creds_file()) == SECRET_MODE

    for secret in (after["pep_token"], after["litellm_key"], MASTER_KEY):
        assert secret not in done.stdout + done.stderr


# -------------------------------------------------------------------- helpers


def _autonomous(triggers: list[dict[str, str]]) -> dict[str, Any]:
    """An autonomous family (contract 01 §4). Only this kind has triggers."""
    return family_body(WAKER, "autonomous", triggers=triggers)


def _status(stack: CaregiverStack, family: str = FAMILY) -> dict[str, Any]:
    document = stack.tree.status(family)
    assert document is not None, f"caregiver published no status document for {family}"

    return document


def _one_fault(status: dict[str, Any], code: str) -> dict[str, Any]:
    """The one fault of that code in the status document (contract 05 §3.3)."""
    found = [fault for fault in status["faults"] if fault.get("code") == code]
    assert len(found) == 1, status["faults"]
    fault: dict[str, Any] = found[0]

    return fault


def _systemctl(stack: CaregiverStack) -> list[tuple[str, ...]]:
    """The arguments of each `systemctl` command that changed something, in call order."""
    calls = [call.argv for call in calls_of(stack.tree, SYSTEMCTL)]

    return [argv for argv in calls if "is-enabled" not in argv]


def _json(path: Path) -> object:
    """The JSON of one file that `caregiver` puts in place by rename, or
    None while the file is not there."""
    text = text_of(path)

    return None if text is None else json.loads(text)


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)
