"""What `caregiver` publishes, read as the next program reads it.

`integration/tests_manager` runs the writers of `caregiver` inside the test
process, with a fake driver and a fake LiteLLM. Here `caregiver` is a
process. An assertion reads a file that another program reads, the record or
the state of a stand-in, or an HTTP answer of the chaperone.

A scenario that `integration/tests_manager` also has keeps its name. Such a
name can say `apply`: here `caregiver serve` does that work.

Most scenarios run `caregiver` alone, where no `attendance` answers and each
sandbox stays `creating`. A scenario that needs a turn or a replacement runs
the house.
"""

from __future__ import annotations

import hashlib
import json
import re
import signal
import stat
import time
from pathlib import Path
from typing import Any

import httpx
from proc_caregiver import (
    CANARIES,
    CREATING,
    DEGRADED,
    EXIT_OK,
    IN_SYNC,
    PLANE_ENDPOINTS,
    PYTHON_IMAGE,
    RECONCILING,
    WRITE_FLAG,
    CaregiverStack,
    litellm_url,
    wait_until,
)
from proc_chat import await_settled, chat_id, run_stream, session_of
from proc_standins import (
    ALLOW,
    DENY,
    LITELLM,
    MASTER_KEY,
    SBX,
    SYSTEMCTL,
    calls_of,
    enabled_units,
    litellm_calls,
    litellm_keys,
    sbx_rows,
    sbx_sandboxes,
    tune,
    untune,
)
from proc_tree import (
    BUDGET_USD,
    FAMILY,
    GRANT_MODE,
    IMAGE,
    MODEL_ALIAS,
    PLAYPEN_ENV_MODE,
    SANDBOX,
    SECRET_MODE,
    STATUS_MODE,
    Tree,
    family_body,
    first_sandbox,
    publish_family,
    remove_family,
    write_family_file,
    write_family_prose,
    write_skill,
)

MANIFEST_PATH = "/manifest"
CALL_PATH = "/call"

OTHER = "ops"
PLOTS = "plots"
WAKER = "waker"
GHOST = "ghost"
NEXT_SANDBOX = "chat-s2"
SKILL = "kitchen"
SKILL_TEXT = "# kitchen\n\nA fixture skill.\n"
NEW_INSTRUCTIONS = "Answer only in metric units.\n"

EMBED = "embed"
HA_CALL = "ha_call"
HA_ALLOW: list[dict[str, str]] = [{"domain": "notify", "service": "mobile_app_example_phone"}]
VERBS: dict[str, dict[str, object]] = {EMBED: {}, HA_CALL: {"allow": HA_ALLOW}}

#: Granted by no fixture family.
UNGRANTED_VERB = "release"
UNGRANTED_MCP_TOOL = "kagi__kagi_search_fetch"

#: Two hosts for an egress list. `.example` names resolve nowhere.
FIRST_HOST = "first.example:443"
SECOND_HOST = "second.example:443"

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
    "sandboxes",
    "credentials",
    "spend",
    "limits",
    "triggers",
    "pep",
}

#: Contract 05 §4.1: each field of one sandbox in the status document.
SANDBOX_FIELDS = {
    "id",
    "state",
    "power",
    "image",
    "spec_hash",
    "cpus",
    "memory",
    "created_at",
    "ready_at",
    "channel",
    "supervisor_env",
}

RFC3339 = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
SHA256_HEX = re.compile(r"[0-9a-f]{64}")

#: pi has eight tools of its own. A family with `read` and `grep` denies the
#: other six by name. `powershell` is in no grant (contract 01 §3.8).
DENIED_BUILTINS = "bash,powershell,edit,write,find,ls"
PI_PROVIDER = "litellm"

#: A watch that finds a silent chaperone in about one second.
FAST_WATCH = ("--pep-probe-interval-s", "0.2", "--pep-unreachable-after-s", "1")

PASS_DEADLINE_S = 30.0
EXIT_DEADLINE_S = 30.0

#: How long a scenario waits to see that nothing happens: five looks.
QUIET_S = 1.0

HTTP_OK = 200
HTTP_FORBIDDEN = 403


# ------------------------------------------------------- the status document


def test_the_status_document_holds_each_field(caregiver_alone: CaregiverStack) -> None:
    """Contract 05 §2 rule 3, §2.1, §2.2 and §4.1. One whole document, mode 0644."""
    tree = caregiver_alone.tree
    document = _status(caregiver_alone)

    assert set(document) == STATUS_FIELDS
    assert _mode(tree.status_file()) == STATUS_MODE
    assert (document["family"], document["kind"]) == (FAMILY, "attended")
    assert RFC3339.fullmatch(document["written_at"])
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

    [box] = document["sandboxes"]
    assert set(box) == SANDBOX_FIELDS
    assert (box["id"], box["image"], box["cpus"], box["memory"]) == (SANDBOX, IMAGE, 2, "2g")
    assert box["supervisor_env"] == str(tree.playpen_env())


def test_apply_reports_creating_and_never_ready(caregiver_alone: CaregiverStack) -> None:
    """Contract 05 §4.2 rule 4. `ready` means that the handshake passed.

    No `attendance` runs here, so nothing ran a handshake. `sbx create`
    alone never makes a sandbox `ready`.
    """
    [box] = _status(caregiver_alone)["sandboxes"]

    assert box["state"] == CREATING
    assert box["ready_at"] is None
    assert caregiver_alone.dialled() == []


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


# ------------------------------------------------ the grant file and the key


def test_the_grant_file_holds_a_digest_and_never_the_token(caregiver_alone: CaregiverStack) -> None:
    """Contract 04 §1.1 and §2.2. A stolen grant file grants nothing."""
    tree = caregiver_alone.tree
    token = caregiver_alone.credentials()["pep_token"]
    text = tree.grant_file().read_text(encoding="utf-8")
    grants = caregiver_alone.grants()

    assert token not in text
    assert grants["token_sha256"] == [hashlib.sha256(token.encode("utf-8")).hexdigest()]
    assert (grants["family"], grants["model_alias"]) == (FAMILY, MODEL_ALIAS)
    assert _mode(tree.grant_file()) == GRANT_MODE


def test_the_key_is_minted_one_time(caregiver_alone: CaregiverStack) -> None:
    """Contract 05 §6.2: mint one time, with the alias, the model and the budget.

    A later pass mints no second key. The master key is the bearer of the
    mint, and the family key is the bearer of the spend read.
    """
    stack = caregiver_alone
    instructions = stack.tree.mounts().config / "instructions.md"

    write_family_prose(stack.tree, text=NEW_INSTRUCTIONS)
    wait_until(lambda: instructions.read_text(encoding="utf-8") == NEW_INSTRUCTIONS, "a new pass")

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
        "the grant file": stack.tree.grant_file().read_text(encoding="utf-8"),
        "the env file": stack.tree.playpen_env().read_text(encoding="utf-8"),
        "the validation report": stack.tree.validation_file().read_text(encoding="utf-8"),
        "an sbx command line": " ".join(word for call in stack.sbx_calls() for word in call.argv),
    }

    for place, text in places.items():
        for secret in secrets:
            assert secret not in text, f"a secret is in {place}"


# ------------------------------------------------------------- the three mounts


def test_apply_writes_the_three_mounts(caregiver_prepared: CaregiverStack) -> None:
    """Contract 03 §7.1 and §12, and contract 01 §6.1, before a sandbox reads them."""
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
    # Contract 05 §4.3 step 5: the control directory of a new sandbox is empty.
    assert list(mounts.control.iterdir()) == []


def test_the_env_file_names_the_directories_apply_mounted(caregiver_alone: CaregiverStack) -> None:
    """Contract 03 §7.1 and contract 05 §4.3 step 4.

    A mount has one path: its path on the host is its path in the sandbox. So
    the env file and `sbx create` name the same three directories. The mount
    order is the order of the contract, and only the first and the control
    directory are read-write.
    """
    tree = caregiver_alone.tree
    mounts = tree.mounts()
    made = sbx_sandboxes(tree)[SANDBOX]["mounts"]

    assert _env_of(tree.playpen_env()) == {
        "AGENT_CRED_DIR": str(mounts.creds),
        "AGENT_FAMILY_CONFIG_DIR": str(mounts.config),
        "AGENT_CONTROL_DIR": str(mounts.control),
        "AGENT_SANDBOX": SANDBOX,
    }
    assert _mode(tree.playpen_env()) == PLAYPEN_ENV_MODE
    assert made[1:] == [
        {"path": str(mounts.creds), "readonly": True},
        {"path": str(mounts.config), "readonly": True},
        {"path": str(mounts.control), "readonly": False},
    ]
    # The session store of the family is first, and it is read-write.
    assert made[0]["readonly"] is False
    assert Path(made[0]["path"]).name == FAMILY


async def test_the_family_file_reaches_the_flags_of_pi(house_prepared: CaregiverStack) -> None:
    """Contract 01 §3.8 and contract 03 §7.1, through the real playpen.

    The playpen reads the mounts that `caregiver` wrote, and it starts pi
    from them. The tools, the model, the instructions and the skills reach pi
    as flags. No credential is on the command line (invariant 13).
    """
    house = house_prepared
    write_skill(house.tree, SKILL, SKILL_TEXT)
    write_family_file(house.tree, family_body(skills=[SKILL], sandbox_tools=["read", "grep"]))
    house.start_house()
    house.await_serving()
    config = house.tree.mounts().config

    await _one_turn(house)
    [pi] = house.pi_starts()

    assert pi.value_after("--exclude-tools") == DENIED_BUILTINS
    assert pi.value_after("--model") == f"{PI_PROVIDER}/{MODEL_ALIAS}"
    assert pi.value_after("--append-system-prompt") == str(config / "instructions.md")
    assert pi.value_after("--skill") == str(config / "skills")
    assert house.credentials()["pep_token"] not in " ".join(pi.argv)
    assert house.credentials()["litellm_key"] not in " ".join(pi.argv)


# ----------------------------------------------------------- reach and egress


def test_a_new_sandbox_gets_the_two_planes_and_no_other_reach(
    caregiver_alone: CaregiverStack,
) -> None:
    """Contract 05 §4.3 steps 1 to 3. Deny by default, and each canary is proved.

    The `shell` kit allows one host with no flag. `caregiver` denies it at
    the create, and it probes each canary before it publishes the sandbox.
    """
    stack = caregiver_alone
    [create] = [command for command in stack.sbx_commands() if command[0] == "create"]
    probed = [command[-1] for command in stack.sbx_commands() if command[:2] == ("policy", "check")]

    assert create[create.index("--deny-network") + 1] == "openrouter.ai"
    assert create[create.index("-t") + 1] == IMAGE
    assert _reach(stack.tree, SANDBOX) == sorted(PLANE_ENDPOINTS)
    assert set(probed) == {*PLANE_ENDPOINTS, *CANARIES}


def test_an_egress_edit_lands_on_the_running_sandbox(caregiver_prepared: CaregiverStack) -> None:
    """Contract 05 §3.5 row 2 and invariant 9: no new sandbox, and the removal is first."""
    stack = caregiver_prepared
    write_family_file(stack.tree, family_body(egress=[FIRST_HOST]))
    stack.spawn_caregiver()
    stack.await_published()
    assert _reach(stack.tree, SANDBOX) == sorted([*PLANE_ENDPOINTS, FIRST_HOST])

    write_family_file(stack.tree, family_body(egress=[SECOND_HOST]))
    wait_until(
        lambda: _reach(stack.tree, SANDBOX) == sorted([*PLANE_ENDPOINTS, SECOND_HOST]),
        f"{SECOND_HOST} in the place of {FIRST_HOST}",
    )
    commands = stack.sbx_commands()
    removed = commands.index(
        ("policy", "rm", "network", "--sandbox", SANDBOX, "--resource", FIRST_HOST)
    )
    added = commands.index(("policy", "allow", "network", "--sandbox", SANDBOX, SECOND_HOST))

    assert removed < added, "the edit gave new reach before it took the old reach away"
    assert list(sbx_sandboxes(stack.tree)) == [SANDBOX]
    wait_until(lambda: stack.state() == IN_SYNC, f"{FAMILY} to be {IN_SYNC}")


# ---------------------------------------------------------------- two families


def test_the_second_family_serves_while_the_first_is_held(
    caregiver_prepared: CaregiverStack,
) -> None:
    """Contract 05 §2 rule 7. No family waits for another.

    The create of one family does not end. The other family converges, and
    the document of the first says which step it is in (§3.4).
    """
    stack = caregiver_prepared
    publish_family(stack.tree, family_body(OTHER))
    tune(stack.tree, SBX, f"hold-create-{SANDBOX}")

    stack.spawn_caregiver()
    stack.await_published(OTHER)
    held = _status(stack)

    assert held["state"] == RECONCILING
    assert held["reconcile"]["step"] == "create_sandbox"
    assert list(sbx_sandboxes(stack.tree)) == [first_sandbox(OTHER)]

    untune(stack.tree, SBX, f"hold-create-{SANDBOX}")
    stack.await_published()


def test_the_flavor_picks_the_image_and_a_change_of_it_replaces_the_sandbox(
    house_prepared: CaregiverStack,
) -> None:
    """Contract 01 §3.9 and contract 05 §3.5 last row.

    The family file names a flavor, and the platform names the image. A
    family with no flavor gets the base image. A change of the flavor is a
    replacement: a new sandbox, a switch, and the old sandbox destroyed.
    """
    house = house_prepared
    python = {"cpus": 2, "memory": "2g", "image": "python"}
    publish_family(house.tree, family_body(PLOTS, sandbox=python))
    house.start_house()
    house.await_serving()
    house.await_serving(PLOTS)

    assert sbx_sandboxes(house.tree)[first_sandbox(PLOTS)]["image"] == PYTHON_IMAGE
    assert sbx_sandboxes(house.tree)[SANDBOX]["image"] == IMAGE
    assert [box["image"] for box in _status(house, PLOTS)["sandboxes"]] == [PYTHON_IMAGE]

    write_family_file(house.tree, family_body(PLOTS))
    house.await_serving(PLOTS, f"{PLOTS}-s2")

    assert [box["image"] for box in _status(house, PLOTS)["sandboxes"]] == [IMAGE]
    assert sorted(sbx_sandboxes(house.tree)) == [SANDBOX, f"{PLOTS}-s2"]


def test_a_removed_family_file_ends_the_family(caregiver_prepared: CaregiverStack) -> None:
    """Contract 05 §4.4 and invariant 13. A family with no file is a family that is gone.

    Its key, its grant file, its sandbox, its policy rows and its state go.
    The other family keeps each of its own.
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
    assert not tree.grant_file(OTHER).exists()
    assert list(sbx_sandboxes(tree)) == [SANDBOX]
    assert _reach(tree, first_sandbox(OTHER)) == []
    assert stack.state() == IN_SYNC
    assert tree.grant_file().is_file()


def test_a_directory_with_no_family_file_is_ignored(caregiver_alone: CaregiverStack) -> None:
    """Contract 01 §5.6 rule 3. Such a directory is a warning, and it is no family.

    `caregiver` reads the new registry: the document of the real family gets
    the new revision. The directory gets no document, no key and no sandbox.
    """
    stack = caregiver_alone
    assert stack.caregiver is not None
    first_rev = _status(stack)["registry_rev"]

    write_family_prose(stack.tree, GHOST, "A directory with prose and no family file.\n")
    wait_until(lambda: _status(stack)["registry_rev"] != first_rev, "a look at the new registry")

    assert stack.state() == IN_SYNC
    assert stack.tree.status(GHOST) is None
    assert list(litellm_keys(stack.tree)) == [f"family-{FAMILY}"]
    assert list(sbx_sandboxes(stack.tree)) == [SANDBOX]
    assert stack.caregiver.exit_code() is None


def test_an_empty_registry_deletes_no_family(caregiver_alone: CaregiverStack) -> None:
    """A registry with no family can be a mount that went away. Nothing is deleted.

    The family file comes back with new instructions. `caregiver` lands the
    edit on the sandbox and the key that it had: it deleted neither.
    """
    stack = caregiver_alone
    tree = stack.tree
    body = family_body()
    instructions = tree.mounts().config / "instructions.md"

    remove_family(tree, FAMILY)
    time.sleep(QUIET_S)
    publish_family(tree, body, NEW_INSTRUCTIONS)
    wait_until(lambda: instructions.read_text(encoding="utf-8") == NEW_INSTRUCTIONS, "a new pass")

    mints = [call for call in litellm_calls(tree) if call["path"] == "/key/generate"]
    assert len(mints) == 1
    assert [call for call in litellm_calls(tree) if call["path"] == "/key/delete"] == []
    assert list(sbx_sandboxes(tree)) == [SANDBOX]
    assert stack.sandboxes() == [(SANDBOX, CREATING)]


# --------------------------------------------------------------------- faults


def test_a_refused_mint_is_a_fault_and_makes_no_sandbox(caregiver_prepared: CaregiverStack) -> None:
    """Contract 05 §3.3, `key_mint_failed`. Nothing depends on a key that does not exist."""
    stack = caregiver_prepared
    tune(stack.tree, LITELLM, "fail-generate")

    stack.spawn_caregiver()
    wait_until(lambda: stack.state() == DEGRADED, f"{FAMILY} to be {DEGRADED}")
    fault = _one_fault(_status(stack), "key_mint_failed")

    assert fault["blocks_turns"] is True
    assert stack.sbx_calls() == []
    assert not stack.tree.creds_file().exists()
    assert not stack.tree.grant_file().exists()


def test_a_failed_create_burns_its_id(caregiver_prepared: CaregiverStack) -> None:
    """Contract 05 §4.1: an id is never used again.

    `sbx create` fails, so the family has a fault that stops turns and no
    sandbox. An edit of the registry makes `caregiver` try again at once, and
    the new sandbox has the next id.
    """
    stack = caregiver_prepared
    tune(stack.tree, SBX, "fail-create", "the image store does not answer")
    stack.spawn_caregiver()
    wait_until(lambda: stack.state() == DEGRADED, f"{FAMILY} to be {DEGRADED}")
    fault = _one_fault(_status(stack), "sandbox_start_failed")

    assert fault["blocks_turns"] is True
    assert stack.sandboxes() == []

    untune(stack.tree, SBX, "fail-create")
    write_family_prose(stack.tree, text=NEW_INSTRUCTIONS)
    wait_until(
        lambda: stack.state() == IN_SYNC and stack.sandboxes() == [(NEXT_SANDBOX, CREATING)],
        f"{NEXT_SANDBOX} after the failed {SANDBOX}",
    )

    assert list(sbx_sandboxes(stack.tree)) == [NEXT_SANDBOX]
    assert _status(stack)["faults"] == []


# --------------------------------------------------------------------- timers


def test_a_cron_trigger_becomes_an_enabled_timer(caregiver_prepared: CaregiverStack) -> None:
    """Contract 01 §3.13. One timer unit for each cron trigger, enabled.

    The unit is in the directory of the user manager. It names the template
    service of the family, and `systemctl --user` enabled it.
    """
    stack = caregiver_prepared
    unit = f"creche-trigger-{WAKER}-t1.timer"
    publish_family(stack.tree, _autonomous([{"cron": CRON}]))

    stack.spawn_caregiver()
    stack.await_published(WAKER)
    text = (stack.tree.unit_dir / unit).read_text(encoding="utf-8")

    assert ON_CALENDAR in text.splitlines()
    assert f"Unit=creche-trigger@{WAKER}.service" in text.splitlines()
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
    unit = f"creche-trigger-{WAKER}-t1.timer"
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
    unit = f"creche-trigger-{WAKER}-t1.timer"
    tune(stack.tree, SYSTEMCTL, f"refuse-enable-{unit}")
    publish_family(stack.tree, _autonomous([{"cron": CRON}]))

    stack.spawn_caregiver()
    wait_until(lambda: stack.state(WAKER) == DEGRADED, f"{WAKER} to be {DEGRADED}")
    fault = _one_fault(_status(stack, WAKER), "timer_enable_failed")

    assert fault["blocks_turns"] is False
    assert enabled_units(stack.tree) == []


# ------------------------------------------------- the chaperone and the grants


async def test_the_manifest_tools_match_the_family_file(caregiver_prepared: CaregiverStack) -> None:
    """Contract 04 §4, on the grant file and the token that `caregiver` wrote.

    The token of the credential file names the family. The manifest holds the
    two verbs of the family file, the model alias and the revision of the
    grant file. No entry needs an approval.
    """
    stack = _with_chaperone(caregiver_prepared, family_body(verbs=VERBS))

    async with stack.sandbox_client() as sandbox:
        manifest = await sandbox.get(MANIFEST_PATH)

    body = manifest.json()
    assert manifest.status_code == HTTP_OK, manifest.text
    assert (body["family"], body["model_alias"]) == (FAMILY, MODEL_ALIAS)
    assert body["rev"] == stack.grants()["rev"]
    assert [tool["name"] for tool in body["tools"]] == [EMBED, HA_CALL]
    assert [tool["approval"] for tool in body["tools"]] == [False, False]


async def test_another_token_resolves_to_nothing(caregiver_prepared: CaregiverStack) -> None:
    """Contract 04 §5 row 1. Only the token that `caregiver` minted names the family."""
    stack = _with_chaperone(caregiver_prepared, family_body(verbs=VERBS))

    async with stack.sandbox_client() as sandbox:
        stranger = await sandbox.get(
            MANIFEST_PATH, headers={"Authorization": "Bearer not-the-family-token"}
        )

    assert stranger.status_code != HTTP_OK


async def test_an_ungranted_verb_is_refused(caregiver_prepared: CaregiverStack) -> None:
    """Contract 04 §5. A tool that the family file does not grant is refused and not offered."""
    stack = _with_chaperone(caregiver_prepared, family_body(verbs=VERBS))

    async with stack.sandbox_client() as sandbox:
        verb = await _call(sandbox, UNGRANTED_VERB, {"components": {"caregiver": "latest"}})
        tool = await _call(sandbox, UNGRANTED_MCP_TOOL, {"q": "boiler"})
        manifest = await sandbox.get(MANIFEST_PATH)

    for refused in (verb, tool):
        assert refused.status_code == HTTP_FORBIDDEN
        assert refused.json()["reason"] == "tool_not_granted"

    names = [entry["name"] for entry in manifest.json()["tools"]]
    assert UNGRANTED_VERB not in names
    assert UNGRANTED_MCP_TOOL not in names


async def test_the_token_survives_a_reapply(caregiver_prepared: CaregiverStack) -> None:
    """Contract 05 §6.2: mint one time. A narrower grant is no rotation.

    The sandbox keeps the token that it mounted, and the chaperone accepts
    it after the edit.
    """
    stack = _with_chaperone(caregiver_prepared, family_body(verbs=VERBS))
    before = stack.credentials()
    first_rev = stack.grants()["rev"]

    write_family_file(stack.tree, family_body(verbs={EMBED: {}}))
    wait_until(lambda: stack.grants()["rev"] != first_rev, "a new grant file", PASS_DEADLINE_S)

    async with stack.sandbox_client() as sandbox:
        manifest = await sandbox.get(MANIFEST_PATH)

    assert stack.credentials() == before
    assert manifest.status_code == HTTP_OK
    assert [tool["name"] for tool in manifest.json()["tools"]] == [EMBED]


async def test_deleting_the_grant_file_revokes_the_family(
    caregiver_prepared: CaregiverStack,
) -> None:
    """Contract 04 §1.3 rule 5. No grant file, no family, with no restart."""
    stack = _with_chaperone(caregiver_prepared, family_body(verbs=VERBS))
    assert stack.chaperone is not None

    async with stack.sandbox_client() as sandbox:
        before = await sandbox.get(MANIFEST_PATH)
        stack.tree.grant_file().unlink()
        after = await sandbox.get(MANIFEST_PATH)

    assert before.status_code == HTTP_OK
    assert after.status_code != HTTP_OK
    assert stack.chaperone.exit_code() is None


async def test_a_rotation_keeps_the_old_token_for_the_overlap(
    caregiver_prepared: CaregiverStack,
) -> None:
    """Contract 05 §6.3 and contract 04 §2.2. A graceful rotation ends no call.

    The `rotate` verb writes a new key and a new token with the next epoch.
    The grant file lists the digest of each token, so the chaperone accepts
    the old token and the new one until the grace ends.
    """
    stack = _with_chaperone(caregiver_prepared, family_body(verbs=VERBS))
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
        WRITE_FLAG,
    )
    after = stack.credentials()

    assert done.exit_code == EXIT_OK, done.stderr
    assert after["epoch"] == before["epoch"] + 1
    assert after["pep_token"] != before["pep_token"]
    assert after["litellm_key"] != before["litellm_key"]
    assert after["litellm_key"] in [entry["key"] for entry in litellm_keys(tree).values()]
    assert _mode(tree.creds_file()) == SECRET_MODE

    async with stack.sandbox_client() as sandbox:
        for token in (before["pep_token"], after["pep_token"]):
            manifest = await sandbox.get(
                MANIFEST_PATH, headers={"Authorization": f"Bearer {token}"}
            )
            assert manifest.status_code == HTTP_OK, manifest.text
            assert manifest.json()["family"] == FAMILY

    for secret in (after["pep_token"], after["litellm_key"], MASTER_KEY):
        assert secret not in done.stdout + done.stderr


def test_the_watch_reports_a_chaperone_that_stopped(caregiver_prepared: CaregiverStack) -> None:
    """Contract 05 §2.2 and §3.3, `pep_unreachable`. One watch for every family.

    The chaperone answers, and the document says `ok`. The chaperone stops,
    and the document says `unreachable` with a fault that stops no turn.
    """
    stack = caregiver_prepared
    stack.start_chaperone()
    assert stack.chaperone is not None
    stack.spawn_caregiver(*FAST_WATCH)
    stack.await_published()
    watched = _status(stack)["pep"]

    assert (watched["watch"], watched["url"]) == ("ok", stack.pep_url())
    assert RFC3339.fullmatch(watched["checked_at"])

    stack.chaperone.send(signal.SIGTERM)
    stack.chaperone.wait(EXIT_DEADLINE_S)
    wait_until(lambda: stack.state() == DEGRADED, f"{FAMILY} to be {DEGRADED}", PASS_DEADLINE_S)
    document = _status(stack)
    fault = _one_fault(document, "pep_unreachable")

    assert document["pep"]["watch"] == "unreachable"
    assert RFC3339.fullmatch(document["pep"]["unreachable_since"])
    assert fault["blocks_turns"] is False


# -------------------------------------------------------------------- helpers


def _with_chaperone(stack: CaregiverStack, body: dict[str, Any]) -> CaregiverStack:
    """Start the chaperone, then `caregiver` on one family file. No `attendance` runs."""
    write_family_file(stack.tree, body)
    stack.start_chaperone()
    stack.spawn_caregiver()
    stack.await_published()

    return stack


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


def _reach(tree: Tree, sandbox: str) -> list[str]:
    """Each host that one sandbox can reach now: an allow row and no deny row."""
    denied = sbx_rows(tree, sandbox, DENY)

    return [host for host in sbx_rows(tree, sandbox, ALLOW) if host not in denied]


def _systemctl(stack: CaregiverStack) -> list[tuple[str, ...]]:
    """The arguments of each `systemctl` command that changed something, in call order."""
    calls = [call.argv for call in calls_of(stack.tree, SYSTEMCTL)]

    return [argv for argv in calls if "is-enabled" not in argv]


async def _one_turn(house: CaregiverStack) -> None:
    """One streamed turn through the door of the house, to its end in the journal."""
    chat = chat_id()

    async with house.door_client() as door:
        frames = await run_stream(door, chat, "hello")

    assert frames.error_chunks == []
    await await_settled(house.tree, session_of(chat))


async def _call(sandbox: httpx.AsyncClient, tool: str, args: dict[str, object]) -> httpx.Response:
    """One `/call`, as the bridge sends it (contract 04 §5)."""
    return await sandbox.post(CALL_PATH, json={"tool": tool, "args": args})


def _env_of(path: Path) -> dict[str, str]:
    """An env file as a mapping: one `NAME=value` for each line."""
    lines = path.read_text(encoding="utf-8").splitlines()

    return dict(line.split("=", 1) for line in lines)


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)
