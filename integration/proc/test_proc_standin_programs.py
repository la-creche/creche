"""The stand-in programs of this directory, each run by itself.

Three of them belong to the `caregiver` topology. The fourth one is the TEI
stand-in of the index builder.

A stand-in is not under test. A stand-in that is too kind makes a wrong
service look right, so each rule that a scenario relies on has one test here.
No test here starts a service: each one runs a stand-in as a service runs it,
through its wrapper or over its port.

The last test holds one rule of the pi wrapper, which each topology uses.
"""

from __future__ import annotations

import socket
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
import standin_tei
from proc_caregiver import wait_until
from proc_harness import LOOPBACK, Finished, ProcError, Supervisor
from proc_stack import base_env
from proc_standins import (
    ALLOW,
    DENY,
    LITELLM,
    MASTER_KEY,
    PI,
    SBX,
    SYSTEMCTL,
    TEI,
    TEI_DIMS,
    TEI_FAIL_EMBED,
    TEI_FAIL_INFO,
    TEI_HOLD_EMBED,
    TEI_MODEL,
    TEI_NO_MODEL_ID,
    calls_of,
    enabled_units,
    install_pi,
    install_sbx_verbs,
    install_systemctl,
    litellm_calls,
    litellm_keys,
    sbx_rows,
    sbx_sandboxes,
    start_litellm,
    start_tei,
    tei_calls,
    tune,
    untune,
)
from proc_tree import IMAGE, Tree, build_bare_tree

BOX = "chat-s1"
KIT_HOST = "openrouter.ai"
A_HOST = "192.0.2.10:4000"
ALIAS = "family-chat"
UNIT = "creche-trigger-chat-t1.timer"

GENERATE = "/key/generate"
UPDATE = "/key/update"
DELETE = "/key/delete"
INFO = "/key/info"

#: What the playpen gives each pi process of a session (contract 03 §6).
PI_WORDS = ("--mode", "rpc", "--session-id", "s1")

EXIT_USAGE = 64
EXIT_NOT_FOUND = 4
HOLD_CHECK_S = 0.3
EXIT_DEADLINE_S = 10.0

HTTP_OK = 200
HTTP_BAD_REQUEST = 400
HTTP_UNAUTHORIZED = 401
HTTP_NOT_FOUND = 404
HTTP_LENGTH_REQUIRED = 411
HTTP_TOO_LARGE = 413
HTTP_UNPROCESSABLE = 422
HTTP_FAILED = 500
HTTP_UNAVAILABLE = 503

INFO_ROUTE = "/info"
EMBED_ROUTE = "/embed"

#: A word that one text of a tuned embed call holds.
MARK = "quince"

#: A request with a body in chunks: it has no `Content-Length` header.
NO_LENGTH_REQUEST = (
    b"POST /embed HTTP/1.1\r\nHost: tei\r\nContent-Type: application/json\r\n"
    b"Transfer-Encoding: chunked\r\n\r\n"
)

TEI_TIMEOUT_S = 30.0
ANSWER_BYTES = 65_536


@pytest.fixture
def programs(tree: Tree, supervisor: Supervisor) -> Tree:
    """A root with the `sbx` and the `systemctl` stand-ins, and no service."""
    build_bare_tree(tree)
    install_sbx_verbs(tree)
    install_systemctl(tree)

    return tree


@dataclass(frozen=True, slots=True)
class Litellm:
    """The LiteLLM stand-in of one test: its root and its port."""

    tree: Tree
    port: int


@pytest.fixture
def litellm(tree: Tree, supervisor: Supervisor) -> Litellm:
    """A root with the LiteLLM stand-in, listening. The `supervisor` fixture ends it."""
    build_bare_tree(tree)
    _, port = start_litellm(tree, supervisor, base_env(tree))

    return Litellm(tree, port)


@dataclass(frozen=True, slots=True)
class Tei:
    """The TEI stand-in of one test: its root and its port."""

    tree: Tree
    port: int


@pytest.fixture
def tei(tree: Tree, supervisor: Supervisor) -> Tei:
    """A root with the TEI stand-in, listening. The `supervisor` fixture ends it."""
    _, port = start_tei(tree, supervisor, base_env(tree))

    return Tei(tree, port)


# ------------------------------------------------------------------------ sbx


def test_sbx_create_keeps_what_it_was_given(programs: Tree, supervisor: Supervisor) -> None:
    done = _create(programs, supervisor, "/a/primary", "/a/secret:ro")

    assert done.exit_code == 0, done.stderr
    assert sbx_sandboxes(programs)[BOX] == {
        "name": BOX,
        "image": IMAGE,
        "cpus": 2,
        "memory": "2g",
        "mounts": [
            {"path": "/a/primary", "readonly": False},
            {"path": "/a/secret", "readonly": True},
        ],
    }
    assert calls_of(programs, SBX)[0].argv[:2] == ("create", "shell")


def test_sbx_refuses_a_second_create_of_one_id(programs: Tree, supervisor: Supervisor) -> None:
    """Contract 05 §4.1: an id is never used again. A service that does so fails here."""
    assert _create(programs, supervisor).exit_code == 0

    assert _create(programs, supervisor).exit_code != 0


def test_sbx_denies_a_host_with_no_row(programs: Tree, supervisor: Supervisor) -> None:
    _create(programs, supervisor)

    done = _sbx(programs, supervisor, "policy", "check", "network", "--sandbox", BOX, A_HOST)

    assert done.stdout.strip() == "Denied"
    assert done.exit_code != 0


def test_sbx_allows_the_kit_host_until_a_row_denies_it(
    programs: Tree, supervisor: Supervisor
) -> None:
    """The `shell` kit allows one host on every create. Only a deny row closes it."""
    _create(programs, supervisor)
    open_kit = _sbx(programs, supervisor, "policy", "check", "network", "--sandbox", BOX, KIT_HOST)

    _sbx(programs, supervisor, "policy", "deny", "network", "--sandbox", BOX, KIT_HOST)
    closed = _sbx(programs, supervisor, "policy", "check", "network", "--sandbox", BOX, KIT_HOST)

    assert open_kit.stdout.strip() == "Allowed"
    assert closed.stdout.strip() == "Denied"


def test_sbx_create_takes_a_deny_flag(programs: Tree, supervisor: Supervisor) -> None:
    _create(programs, supervisor, deny=KIT_HOST)

    done = _sbx(programs, supervisor, "policy", "check", "network", "--sandbox", BOX, KIT_HOST)

    assert done.stdout.strip() == "Denied"
    assert sbx_rows(programs, BOX, DENY) == [KIT_HOST]


def test_sbx_rm_leaves_the_policy_rows(programs: Tree, supervisor: Supervisor) -> None:
    """`sbx rm` removes no row. A destroy that removes none leaves each row here."""
    _create(programs, supervisor)
    _sbx(programs, supervisor, "policy", "allow", "network", "--sandbox", BOX, A_HOST)

    removed = _sbx(programs, supervisor, "rm", "-f", BOX)

    assert removed.exit_code == 0, removed.stderr
    assert sbx_sandboxes(programs) == {}
    assert A_HOST in sbx_rows(programs, BOX, ALLOW)

    row = _sbx(
        programs, supervisor, "policy", "rm", "network", "--sandbox", BOX, "--resource", A_HOST
    )

    assert row.exit_code == 0, row.stderr
    assert A_HOST not in sbx_rows(programs, BOX, ALLOW)


def test_sbx_rm_of_a_missing_sandbox_says_not_found(programs: Tree, supervisor: Supervisor) -> None:
    done = _sbx(programs, supervisor, "rm", "-f", BOX)

    assert done.exit_code != 0
    assert "not found" in done.stderr


def test_sbx_ls_lists_each_sandbox_after_one_header(programs: Tree, supervisor: Supervisor) -> None:
    _create(programs, supervisor)

    lines = _sbx(programs, supervisor, "ls").stdout.splitlines()

    assert [line.split()[0] for line in lines[1:]] == [BOX]


def test_sbx_exec_refuses_a_sandbox_that_no_create_made(
    programs: Tree, supervisor: Supervisor
) -> None:
    env_file = _env_file(programs)

    done = _sbx(programs, supervisor, "exec", "--env-file", str(env_file), BOX, "--", "/bin/echo")

    assert done.exit_code != 0
    assert "not found" in done.stderr
    assert done.stdout == ""


def test_sbx_exec_gives_the_command_the_env_file_alone(
    programs: Tree, supervisor: Supervisor
) -> None:
    """Contract 03 §7.1: `sbx exec` forwards no variable of its caller."""
    _create(programs, supervisor)
    show = 'echo "$AGENT_CONTROL_DIR|$CALLER_ONLY"'

    done = _sbx(
        programs,
        supervisor,
        *("exec", "--env-file", str(_env_file(programs)), BOX, "--", "/bin/sh", "-c", show),
        env={"CALLER_ONLY": "leaked"},
    )

    assert done.exit_code == 0, done.stderr
    assert done.stdout.strip() == "/a/control|"


def test_sbx_exec_gives_no_mount_when_a_test_says_so(
    programs: Tree, supervisor: Supervisor
) -> None:
    _create(programs, supervisor)
    tune(programs, SBX, f"no-mounts/{BOX}")
    show = 'echo "$AGENT_CONTROL_DIR|$AGENT_SANDBOX"'

    done = _sbx(
        programs,
        supervisor,
        *("exec", "--env-file", str(_env_file(programs)), BOX, "--", "/bin/sh", "-c", show),
    )

    assert done.stdout.strip() == f"|{BOX}"


def test_sbx_fails_a_verb_when_a_test_says_so(programs: Tree, supervisor: Supervisor) -> None:
    tune(programs, SBX, "fail-create", "no space left on device")

    done = _create(programs, supervisor)

    assert done.exit_code != 0
    assert "no space left on device" in done.stderr
    assert sbx_sandboxes(programs) == {}


def test_sbx_holds_a_create_while_a_test_says_so(programs: Tree, supervisor: Supervisor) -> None:
    tune(programs, SBX, f"hold-create-{BOX}")
    child = supervisor.spawn(
        SBX, _words(programs, SBX, *_create_words()), base_env(programs), programs.root
    )

    with pytest.raises(ProcError, match="did not exit"):
        child.wait(HOLD_CHECK_S)

    assert sbx_sandboxes(programs) == {}
    untune(programs, SBX, f"hold-create-{BOX}")

    assert child.wait(EXIT_DEADLINE_S) == 0
    assert list(sbx_sandboxes(programs)) == [BOX]


def test_sbx_refuses_a_verb_it_does_not_know(programs: Tree, supervisor: Supervisor) -> None:
    """Fail closed. A service that runs another verb gets no silent success."""
    assert _sbx(programs, supervisor, "stop", BOX).exit_code == EXIT_USAGE


def test_sbx_create_refuses_another_kit(programs: Tree, supervisor: Supervisor) -> None:
    """Fail closed. Only the `shell` kit has the allow row that this stand-in copies."""
    words = ["create", "another-kit", *_create_words()[2:]]

    assert _sbx(programs, supervisor, *words).exit_code == EXIT_USAGE
    assert sbx_sandboxes(programs) == {}


def test_sbx_create_refuses_a_flag_it_does_not_know(programs: Tree, supervisor: Supervisor) -> None:
    """Fail closed. A service that passes a new flag gets no sandbox here."""
    done = _sbx(programs, supervisor, *_create_words(), "--publish", "8080:80")

    assert done.exit_code == EXIT_USAGE
    assert sbx_sandboxes(programs) == {}


def test_sbx_policy_rm_leaves_the_allow_row_of_the_kit(
    programs: Tree, supervisor: Supervisor
) -> None:
    """No command removes the allow row of the kit. A removed deny row opens the host again."""
    _create(programs, supervisor, deny=KIT_HOST)

    removed = _policy(programs, supervisor, "rm", "--resource", KIT_HOST)
    again = _policy(programs, supervisor, "rm", "--resource", KIT_HOST)
    check = _policy(programs, supervisor, "check", KIT_HOST)

    assert removed.exit_code == 0, removed.stderr
    assert again.exit_code != 0
    assert check.stdout.strip() == "Allowed"
    assert sbx_rows(programs, BOX, ALLOW) == [KIT_HOST]
    assert sbx_rows(programs, BOX, DENY) == []


def test_sbx_policy_rm_refuses_a_row_that_does_not_exist(
    programs: Tree, supervisor: Supervisor
) -> None:
    _create(programs, supervisor)

    assert _policy(programs, supervisor, "rm", "--resource", A_HOST).exit_code != 0
    assert sbx_rows(programs, BOX, ALLOW) == [KIT_HOST]


def test_sbx_policy_refuses_a_sandbox_that_no_create_made(
    programs: Tree, supervisor: Supervisor
) -> None:
    done = _policy(programs, supervisor, "allow", A_HOST)

    assert done.exit_code != 0
    assert "not found" in done.stderr
    assert sbx_rows(programs, BOX, ALLOW) == []


def test_sbx_refuses_a_policy_action_it_does_not_know(
    programs: Tree, supervisor: Supervisor
) -> None:
    """Fail closed. A service that runs another policy action gets no silent success."""
    _create(programs, supervisor)

    assert _policy(programs, supervisor, "reset", A_HOST).exit_code == EXIT_USAGE
    assert sbx_rows(programs, BOX, ALLOW) == [KIT_HOST]


# ------------------------------------------------------------------ systemctl


def test_systemctl_enables_a_unit_that_has_a_file(programs: Tree, supervisor: Supervisor) -> None:
    _unit_file(programs)

    enabled = _systemctl(programs, supervisor, "--user", "enable", "--now", UNIT)
    asked = _systemctl(programs, supervisor, "--user", "is-enabled", UNIT)

    assert enabled.exit_code == 0, enabled.stderr
    assert (asked.exit_code, asked.stdout.strip()) == (0, "enabled")
    assert enabled_units(programs) == [UNIT]


def test_systemctl_refuses_a_unit_with_no_file(programs: Tree, supervisor: Supervisor) -> None:
    enabled = _systemctl(programs, supervisor, "--user", "enable", "--now", UNIT)
    asked = _systemctl(programs, supervisor, "--user", "is-enabled", UNIT)

    assert enabled.exit_code != 0
    assert (asked.exit_code, asked.stdout.strip()) == (EXIT_NOT_FOUND, "not-found")
    assert enabled_units(programs) == []


def test_systemctl_says_disabled_with_a_failed_exit(programs: Tree, supervisor: Supervisor) -> None:
    """`is-enabled` exits with a failure for `disabled`. The word is the answer."""
    _unit_file(programs)
    _systemctl(programs, supervisor, "--user", "enable", "--now", UNIT)

    _systemctl(programs, supervisor, "--user", "disable", "--now", UNIT)
    asked = _systemctl(programs, supervisor, "--user", "is-enabled", UNIT)

    assert asked.stdout.strip() == "disabled"
    assert asked.exit_code != 0
    assert enabled_units(programs) == []


def test_systemctl_refuses_to_enable_when_a_test_says_so(
    programs: Tree, supervisor: Supervisor
) -> None:
    _unit_file(programs)
    tune(programs, SYSTEMCTL, f"refuse-enable-{UNIT}")

    assert _systemctl(programs, supervisor, "--user", "enable", "--now", UNIT).exit_code != 0
    assert enabled_units(programs) == []


def test_systemctl_refuses_a_call_with_no_user_flag(programs: Tree, supervisor: Supervisor) -> None:
    """Fail closed. `caregiver` runs as the operator and owns no system unit.

    The unit has a file, so only the flag makes each call fail.
    """
    _unit_file(programs)

    bare = _systemctl(programs, supervisor, "enable", "--now", UNIT)
    system = _systemctl(programs, supervisor, "--system", "enable", "--now", UNIT)

    assert (bare.exit_code, system.exit_code) == (EXIT_USAGE, EXIT_USAGE)
    assert enabled_units(programs) == []
    assert _systemctl(programs, supervisor, "--user", "daemon-reload").exit_code == 0


def test_systemctl_refuses_a_verb_it_does_not_know(programs: Tree, supervisor: Supervisor) -> None:
    """Fail closed. A service that runs another verb gets no silent success."""
    _unit_file(programs)

    assert _systemctl(programs, supervisor, "--user", "start", UNIT).exit_code == EXIT_USAGE
    assert enabled_units(programs) == []


def test_systemctl_refuses_to_disable_a_unit_with_no_file(
    programs: Tree, supervisor: Supervisor
) -> None:
    """A service that removes the unit file before the disable leaves the unit enabled."""
    _unit_file(programs)
    _systemctl(programs, supervisor, "--user", "enable", "--now", UNIT)
    (programs.unit_dir / UNIT).unlink()

    disabled = _systemctl(programs, supervisor, "--user", "disable", "--now", UNIT)

    assert disabled.exit_code != 0
    assert enabled_units(programs) == [UNIT]


# -------------------------------------------------------------------- LiteLLM


def test_litellm_mints_one_key_for_one_alias(litellm: Litellm) -> None:
    with _client(litellm) as client:
        first = client.post(GENERATE, json=_generate_body())
        second = client.post(GENERATE, json=_generate_body())

    assert first.status_code == HTTP_OK
    assert first.json()["key"].startswith("sk-")
    assert second.status_code == HTTP_BAD_REQUEST
    assert list(litellm_keys(litellm.tree)) == [ALIAS]


def test_litellm_refuses_a_request_with_another_bearer(litellm: Litellm) -> None:
    with _client(litellm, bearer="not-the-master-key") as client:
        answers = [client.post(path, json=_generate_body()) for path in (GENERATE, UPDATE, DELETE)]
        info = client.get(INFO)

    assert [answer.status_code for answer in [*answers, info]] == [HTTP_UNAUTHORIZED] * 4
    assert litellm_keys(litellm.tree) == {}


def test_litellm_reports_spend_to_the_key_itself(litellm: Litellm) -> None:
    tree = litellm.tree
    tune(tree, LITELLM, f"spend-{ALIAS}", "1.25")

    with _client(litellm) as client:
        key = client.post(GENERATE, json=_generate_body()).json()["key"]
        by_master = client.get(INFO)
        by_key = client.get(INFO, headers={"Authorization": f"Bearer {key}"})

    assert by_master.status_code == HTTP_UNAUTHORIZED
    assert by_key.json()["info"] == {"key_alias": ALIAS, "spend": 1.25, "max_budget": 15.0}


def test_litellm_updates_and_deletes_a_key(litellm: Litellm) -> None:
    tree = litellm.tree

    with _client(litellm) as client:
        key = client.post(GENERATE, json=_generate_body()).json()["key"]
        updated = client.post(UPDATE, json={"key": key, "models": ["other"], "max_budget": 2.0})
        stranger = client.post(UPDATE, json={"key": "sk-nobody", "models": []})
        after_update = litellm_keys(tree)[ALIAS]
        deleted = client.post(DELETE, json={"key_aliases": [ALIAS]})
        info = client.get(INFO, headers={"Authorization": f"Bearer {key}"})

    assert updated.status_code == HTTP_OK
    assert stranger.status_code == HTTP_NOT_FOUND
    assert (after_update["models"], after_update["max_budget"]) == (["other"], 2.0)
    assert deleted.json() == {"deleted_keys": [ALIAS]}
    assert info.status_code == HTTP_UNAUTHORIZED
    assert litellm_keys(tree) == {}


def test_litellm_records_each_call_and_no_secret(litellm: Litellm) -> None:
    tree = litellm.tree

    with _client(litellm) as client:
        key = client.post(GENERATE, json=_generate_body()).json()["key"]
        client.post(UPDATE, json={"key": key, "models": ["other"]})
        client.get(INFO, headers={"Authorization": f"Bearer {key}"})

    calls = litellm_calls(tree)
    text = str(calls)

    assert [(call["method"], call["path"], call["as"]) for call in calls] == [
        ("POST", GENERATE, "master"),
        ("POST", UPDATE, "master"),
        ("GET", INFO, "key"),
    ]
    assert calls[0]["body"] == _generate_body()
    assert MASTER_KEY not in text
    assert key not in text


def test_litellm_answers_not_found_on_a_route_it_does_not_know(litellm: Litellm) -> None:
    """Fail closed. A service that asks another route gets no silent success."""
    with _client(litellm) as client:
        posted = client.post("/key/regenerate", json=_generate_body())
        asked = client.get("/key/list")

    assert (posted.status_code, asked.status_code) == (HTTP_NOT_FOUND, HTTP_NOT_FOUND)
    assert litellm_keys(litellm.tree) == {}


def test_litellm_fails_a_request_when_a_test_says_so(litellm: Litellm) -> None:
    tune(litellm.tree, LITELLM, "fail-generate")

    with _client(litellm) as client:
        failed = client.post(GENERATE, json=_generate_body())

    assert failed.status_code == HTTP_FAILED
    assert litellm_keys(litellm.tree) == {}


# ------------------------------------------------------------------------ tei


def test_tei_names_its_model(tei: Tei) -> None:
    with _tei_client(tei) as client:
        first = client.get(INFO_ROUTE)
        tune(tei.tree, TEI, TEI_MODEL, "another/model")
        second = client.get(INFO_ROUTE)

    assert (first.status_code, first.json()) == (HTTP_OK, {"model_id": standin_tei.DEFAULT_MODEL})
    assert (second.status_code, second.json()) == (HTTP_OK, {"model_id": "another/model"})


def test_tei_names_no_model_when_a_test_says_so(tei: Tei) -> None:
    tune(tei.tree, TEI, TEI_NO_MODEL_ID)

    with _tei_client(tei) as client:
        info = client.get(INFO_ROUTE)

    assert (info.status_code, info.json()) == (HTTP_OK, {})


def test_tei_fails_info_when_a_test_says_so(tei: Tei) -> None:
    tune(tei.tree, TEI, TEI_FAIL_INFO)

    with _tei_client(tei) as client:
        info = client.get(INFO_ROUTE)
        embedded = client.post(EMBED_ROUTE, json={"inputs": ["a"]})

    assert info.status_code == HTTP_UNAVAILABLE
    assert embedded.status_code == HTTP_OK


def test_tei_answers_one_vector_for_each_text(tei: Tei) -> None:
    """A vector depends on its text alone, so a test can compute the vector of a chunk.

    The sum of the code points of `ab` is 195. Value 0 is then 195 / 1000 - 0.5,
    and value 5 is ((195 * 6) % 1000) / 1000 - 0.5.
    """
    texts = ["ab", "ab", "a longer text, with an \u00e9"]

    with _tei_client(tei) as client:
        answered = client.post(EMBED_ROUTE, json={"inputs": texts})

    vectors = answered.json()

    assert answered.status_code == HTTP_OK
    assert vectors == [standin_tei.vector_of(text) for text in texts]
    assert [len(vector) for vector in vectors] == [standin_tei.DEFAULT_DIMS] * len(texts)
    assert vectors[0][0] == 195 / 1000 - 0.5
    assert vectors[0][5] == 170 / 1000 - 0.5
    assert vectors[0] != vectors[2]


def test_tei_answers_another_count_of_values_when_a_test_says_so(tei: Tei) -> None:
    tune(tei.tree, TEI, TEI_DIMS, "4")

    with _tei_client(tei) as client:
        answered = client.post(EMBED_ROUTE, json={"inputs": ["ab"]})

    assert answered.json() == [standin_tei.vector_of("ab", 4)]
    assert len(answered.json()[0]) == 4


def test_tei_fails_a_call_that_holds_a_text(tei: Tei) -> None:
    """The tuning names a part of a text. A call with no such text gets its vectors."""
    tune(tei.tree, TEI, TEI_FAIL_EMBED, MARK)

    with _tei_client(tei) as client:
        failed = client.post(EMBED_ROUTE, json={"inputs": ["a pear", f"a ripe {MARK} here"]})
        answered = client.post(EMBED_ROUTE, json={"inputs": ["a pear"]})

    assert failed.status_code == HTTP_FAILED
    assert answered.status_code == HTTP_OK


def test_tei_holds_a_call_that_holds_a_text(tei: Tei) -> None:
    """The line of the held call is in the record while the call waits.

    A scenario waits for that line. Then it knows that the program waits for
    this answer.
    """
    tune(tei.tree, TEI, TEI_HOLD_EMBED, MARK)
    held_body = {"inputs": [f"a ripe {MARK} here"]}

    with ThreadPoolExecutor(max_workers=1) as pool, _tei_client(tei) as client:
        held = pool.submit(_post_embed, tei, held_body)
        wait_until(lambda: _embed_bodies(tei) == [held_body], "the held call", TEI_TIMEOUT_S)
        free = client.post(EMBED_ROUTE, json={"inputs": ["a pear"]})
        still_held = not held.done()
        untune(tei.tree, TEI, TEI_HOLD_EMBED)
        status = held.result(timeout=TEI_TIMEOUT_S)

    assert free.status_code == HTTP_OK
    assert still_held
    assert status == HTTP_OK


def test_tei_stays_up_when_a_held_caller_is_gone(tei: Tei, supervisor: Supervisor) -> None:
    """A scenario kills the program inside a held call. The stand-in must serve the next run."""
    tune(tei.tree, TEI, TEI_HOLD_EMBED, MARK)

    with pytest.raises(httpx.ReadTimeout), _tei_client(tei) as client:
        client.post(EMBED_ROUTE, json={"inputs": [MARK]}, timeout=HOLD_CHECK_S)

    untune(tei.tree, TEI, TEI_HOLD_EMBED)

    with _tei_client(tei) as client:
        answered = client.post(EMBED_ROUTE, json={"inputs": [MARK]})

    [child] = supervisor.children

    assert answered.status_code == HTTP_OK
    assert child.exit_code() is None
    assert child.stderr_path.read_bytes() == b""


def test_tei_records_each_request(tei: Tei) -> None:
    """One line for each request, in arrival order. The probe of the harness leaves none."""
    body = {"inputs": ["a pear", "an \u00e9clair"]}

    with _tei_client(tei) as client:
        client.get("/")
        client.get(INFO_ROUTE)
        client.post(EMBED_ROUTE, json=body)

    assert tei_calls(tei.tree) == [
        {"method": "GET", "path": INFO_ROUTE, "body": None},
        {"method": "POST", "path": EMBED_ROUTE, "body": body},
    ]


def test_tei_answers_not_found_on_a_route_it_does_not_know(tei: Tei) -> None:
    """Fail closed. A program that asks another route gets no silent success."""
    with _tei_client(tei) as client:
        posted = client.post("/embed_all", json={"inputs": ["a"]})
        asked = client.get("/health")

    assert (posted.status_code, asked.status_code) == (HTTP_NOT_FOUND, HTTP_NOT_FOUND)


def test_tei_refuses_a_body_of_another_shape(tei: Tei) -> None:
    """Fail closed. Only an object with a list of one text or more in `inputs` gets vectors."""
    bodies = [b"not json", b"[]", b'{"inputs": "a"}', b'{"inputs": []}', b'{"inputs": ["a", 1]}']

    with _tei_client(tei) as client:
        statuses = [client.post(EMBED_ROUTE, content=body).status_code for body in bodies]

    assert statuses == [HTTP_UNPROCESSABLE] * len(bodies)


def test_tei_refuses_a_call_with_more_texts_than_one_call_may_hold(tei: Tei) -> None:
    """The real service has a limit for one call. A program that ignores it fails here."""
    full = [f"text {index}" for index in range(standin_tei.MAX_BATCH)]

    with _tei_client(tei) as client:
        answered = client.post(EMBED_ROUTE, json={"inputs": full})
        refused = client.post(EMBED_ROUTE, json={"inputs": [*full, "one more"]})

    assert answered.status_code == HTTP_OK
    assert refused.status_code == HTTP_TOO_LARGE


def test_tei_refuses_a_body_with_no_length(tei: Tei) -> None:
    """Fail closed. This program reads a body only by its `Content-Length`."""
    with socket.create_connection((LOOPBACK, tei.port), timeout=TEI_TIMEOUT_S) as conn:
        conn.sendall(NO_LENGTH_REQUEST)
        answer = conn.recv(ANSWER_BYTES)

    assert answer.startswith(f"HTTP/1.1 {HTTP_LENGTH_REQUIRED} ".encode("ascii"))
    assert tei_calls(tei.tree) == [{"method": "POST", "path": EMBED_ROUTE, "body": None}]


# ------------------------------------------------------------------------- pi


def test_pi_runs_with_the_rpc_mode_as_one_word(tree: Tree, supervisor: Supervisor) -> None:
    """A sandbox of a test has no process table of its own.

    On Linux the playpen counts each process of the machine whose arguments
    hold `--mode` and `rpc` as two words (contract 03 §3 rule 4). The pi
    stand-in runs with one word, so the playpen of another sandbox does not
    count it. The record keeps the two words that the playpen sent.
    """
    build_bare_tree(tree)
    install_pi(tree)
    # The double of pi ends at the end of its stdin, and `sleep` holds it open.
    words = ["/bin/sh", "-c", 'sleep 30 | "$0" "$@"', str(tree.bin_dir / PI), *PI_WORDS]

    supervisor.spawn(PI, words, base_env(tree), tree.root)
    wait_until(lambda: calls_of(tree, PI) != [], "the pi stand-in to start", EXIT_DEADLINE_S)
    [call] = calls_of(tree, PI)
    wait_until(
        lambda: "fake-pi.mjs" in _command_line(call.pid),
        "the wrapper to become the program",
        EXIT_DEADLINE_S,
    )
    running = _command_line(call.pid).split()

    assert "--mode=rpc" in running
    assert "--mode" not in running
    assert "rpc" not in running
    assert call.argv == PI_WORDS


# -------------------------------------------------------------------- helpers


def _words(tree: Tree, name: str, *args: str) -> list[str]:
    """One stand-in through its wrapper, as a service finds it on its PATH."""
    return [str(tree.bin_dir / name), *args]


def _sbx(
    tree: Tree, supervisor: Supervisor, *args: str, env: dict[str, str] | None = None
) -> Finished:
    whole = base_env(tree) | (env or {})

    return supervisor.run(SBX, _words(tree, SBX, *args), whole, tree.root)


def _policy(tree: Tree, supervisor: Supervisor, action: str, *words: str) -> Finished:
    """One `sbx policy` command for the sandbox of this file."""
    return _sbx(tree, supervisor, "policy", action, "network", "--sandbox", BOX, *words)


def _systemctl(tree: Tree, supervisor: Supervisor, *args: str) -> Finished:
    return supervisor.run(SYSTEMCTL, _words(tree, SYSTEMCTL, *args), base_env(tree), tree.root)


def _create_words(*mounts: str, deny: str | None = None) -> list[str]:
    """`sbx create`, as contract 05 §4.3 step 1 and step 4 give it."""
    words = ["create", "shell", *(mounts or ("/a/primary",))]
    words += ["-t", IMAGE, "--name", BOX, "--cpus", "2", "-m", "2g"]

    if deny is not None:
        words += ["--deny-network", deny]

    return [*words, "-q"]


def _create(tree: Tree, supervisor: Supervisor, *mounts: str, deny: str | None = None) -> Finished:
    return _sbx(tree, supervisor, *_create_words(*mounts, deny=deny))


def _env_file(tree: Tree) -> Path:
    path = tree.root / "a.env"
    path.write_text(f"AGENT_CONTROL_DIR=/a/control\nAGENT_SANDBOX={BOX}\n", encoding="utf-8")

    return path


def _command_line(pid: int) -> str:
    """The arguments of one process as `ps` prints them, or no text for a pid that left."""
    done = subprocess.run(
        ["ps", "-ww", "-o", "args=", "-p", str(pid)],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
    )

    return done.stdout.strip()


def _unit_file(tree: Tree) -> None:
    tree.unit_dir.mkdir(parents=True, exist_ok=True)
    (tree.unit_dir / UNIT).write_text("[Timer]\nOnCalendar=daily\n", encoding="utf-8")


def _client(litellm: Litellm, bearer: str = MASTER_KEY) -> httpx.Client:
    return httpx.Client(
        base_url=f"http://{LOOPBACK}:{litellm.port}",
        headers={"Authorization": f"Bearer {bearer}"},
        timeout=5.0,
    )


def _tei_client(tei: Tei) -> httpx.Client:
    return httpx.Client(base_url=f"http://{LOOPBACK}:{tei.port}", timeout=TEI_TIMEOUT_S)


def _post_embed(tei: Tei, body: dict[str, list[str]]) -> int:
    """One embed call on a connection of its own. Returns the status."""
    with _tei_client(tei) as client:
        return client.post(EMBED_ROUTE, json=body).status_code


def _embed_bodies(tei: Tei) -> list[object]:
    return [call["body"] for call in tei_calls(tei.tree) if call["path"] == EMBED_ROUTE]


def _generate_body() -> dict[str, object]:
    return {
        "key_alias": ALIAS,
        "models": ["agent-router"],
        "max_budget": 15.0,
        "budget_duration": "1d",
    }
