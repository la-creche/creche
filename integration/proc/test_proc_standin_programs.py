"""The stand-in programs of this directory, each run by itself.

Two of them belong to the `caregiver` topology: `systemctl` and LiteLLM.
The vmm stand-in serves `attendance` and `caregiver`. The fourth one is the
TEI stand-in of the index builder.

A stand-in is not under test. A stand-in that is too kind makes a wrong
service look right, so each rule that a scenario relies on has one test here.
No test here starts a service: each one runs a stand-in as a service runs it,
through its wrapper or over its port.

The last test holds one rule of the pi wrapper, which each topology uses.
"""

from __future__ import annotations

import json
import socket
import struct
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import httpx
import pytest
import standin_tei
from creche_wire.routes import (
    ACP,
    CREATE,
    DROP,
    HOST_API_HEADER,
    HOST_API_PROTOCOL,
    LIST,
    MISMATCH_STATUS,
)
from proc_caregiver import wait_until
from proc_harness import LOOPBACK, Finished, ProcError, Supervisor
from proc_stack import base_env
from proc_standins import (
    LITELLM,
    MASTER_KEY,
    SYSTEMCTL,
    TEI,
    TEI_DIMS,
    TEI_FAIL_EMBED,
    TEI_FAIL_INFO,
    TEI_HOLD_EMBED,
    TEI_MODEL,
    TEI_NO_MODEL_ID,
    VMM,
    enabled_units,
    install_systemctl,
    litellm_calls,
    litellm_keys,
    start_litellm,
    start_tei,
    start_vmm,
    tei_calls,
    tune,
    untune,
    vmm_calls,
)
from proc_tree import Tree, build_bare_tree

#: Three session ids (contract 02 §5.3: a UUID of version 4).
FIRST_ID = "0b9d6f4e-8a51-4c3e-9f57-3d2b1c0a9e81"
SECOND_ID = "0b9d6f4e-8a51-4c3e-9f57-3d2b1c0a9e82"
THIRD_ID = "0b9d6f4e-8a51-4c3e-9f57-3d2b1c0a9e83"
ALIAS = "family-chat"
UNIT = "creche-door-trigger-chat-t1.timer"

GENERATE = "/key/generate"
UPDATE = "/key/update"
DELETE = "/key/delete"
INFO = "/key/info"

EXIT_USAGE = 64
EXIT_NOT_FOUND = 4
HOLD_CHECK_S = 0.3

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

#: What each answer of `/info` holds beside the model.
BATCH_LIMIT = {"max_client_batch_size": standin_tei.MAX_BATCH}

#: A request with a body in chunks: it has no `Content-Length` header.
NO_LENGTH_REQUEST = (
    b"POST /embed HTTP/1.1\r\nHost: tei\r\nContent-Type: application/json\r\n"
    b"Transfer-Encoding: chunked\r\n\r\n"
)

#: The value of `SO_LINGER` that makes a close send a reset: linger for 0 seconds.
RESET_AT_CLOSE = struct.pack("ii", 1, 0)

TEI_TIMEOUT_S = 30.0
VMM_TIMEOUT_S = 10.0
ANSWER_BYTES = 65_536


@pytest.fixture
def programs(tree: Tree, supervisor: Supervisor) -> Tree:
    """A root with the `systemctl` stand-in, and no service."""
    build_bare_tree(tree)
    install_systemctl(tree)

    return tree


@pytest.fixture
def vmm(tree: Tree, supervisor: Supervisor) -> Tree:
    """A root with the vmm stand-in, listening. The `supervisor` fixture ends it."""
    build_bare_tree(tree)
    start_vmm(tree, supervisor, base_env(tree))

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


# ------------------------------------------------------------------------ vmm


def test_vmm_drops_a_family_and_records_the_call(vmm: Tree) -> None:
    """A drop of a family is `Dropped`, rows or not (`FamilyDrop` of `creche_wire`)."""
    answer = _vmm_post(vmm, DROP.path, {"family": {"family": "chat"}})

    assert (answer.status_code, answer.json()) == (HTTP_OK, {})
    assert vmm_calls(vmm) == [{"route": "drop", "body": {"family": {"family": "chat"}}}]


def test_vmm_answers_the_mismatch_reply_to_another_protocol(vmm: Tree) -> None:
    """Decision CH33 rule 1: the header comes before the route, and no line is written."""
    answer = _vmm_post(vmm, DROP.path, {"family": {"family": "chat"}}, protocol="1")

    assert answer.status_code == MISMATCH_STATUS
    assert answer.json() == {"code": "protocol_mismatch", "protocol": HOST_API_PROTOCOL}
    assert vmm_calls(vmm) == []


def test_vmm_refuses_a_body_of_another_shape(vmm: Tree) -> None:
    answer = _vmm_post(vmm, DROP.path, {"family": "chat"})

    assert answer.json() == {"code": "not_valid"}
    assert vmm_calls(vmm) == []


def test_vmm_lists_the_rows_of_a_family_newest_first(vmm: Tree) -> None:
    """Decision CH21-5: one page, newest created first. A `cwd` keeps its rows."""
    first = _create_row(vmm, FIRST_ID, "/work/a", 1_000)
    second = _create_row(vmm, SECOND_ID, "/work/b", 2_000)
    _create_row(vmm, THIRD_ID, "/work/a", 3_000, family="ops")

    every = _vmm_post(vmm, LIST.path, {"family": "chat", "cursor": None, "cwd": None})
    kept = _vmm_post(vmm, LIST.path, {"family": "chat", "cursor": None, "cwd": "/work/a"})

    assert every.json() == {"next": None, "rows": [second, first]}
    assert kept.json() == {"next": None, "rows": [first]}


def test_vmm_drops_one_session_and_then_finds_none(vmm: Tree) -> None:
    """Decision CH33 rule 5: a session with no row of its family is `not_found`."""
    _create_row(vmm, FIRST_ID, "/work/a", 1_000)
    key = {"session": {"family": "chat", "id": FIRST_ID}}

    dropped = _vmm_post(vmm, DROP.path, key)
    again = _vmm_post(vmm, DROP.path, key)

    assert (dropped.status_code, dropped.json()) == (HTTP_OK, {})
    assert again.json() == {"code": "not_found"}


def test_vmm_boots_no_vm_for_a_row(vmm: Tree) -> None:
    """The `acp` route of a row is `sandbox_start_failed`, and of no row `not_found`."""
    _create_row(vmm, FIRST_ID, "/work/a", 1_000)

    held = _vmm_post(vmm, ACP.path, {"family": "chat", "id": FIRST_ID})
    unknown = _vmm_post(vmm, ACP.path, {"family": "chat", "id": SECOND_ID})

    assert held.json() == {"code": "sandbox_start_failed"}
    assert unknown.json() == {"code": "not_found"}


def test_vmm_fails_a_drop_when_a_test_says_so(vmm: Tree) -> None:
    tune(vmm, VMM, "fail-drop")

    answer = _vmm_post(vmm, DROP.path, {"family": {"family": "chat"}})

    assert (answer.status_code, answer.json()) == (HTTP_FAILED, {"code": "internal"})


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
    """The answer holds one key more than the model, as the real service answers more keys.

    A reader that refuses a key that it does not know fails on this answer.
    """
    with _tei_client(tei) as client:
        first = client.get(INFO_ROUTE)
        tune(tei.tree, TEI, TEI_MODEL, "another/model")
        second = client.get(INFO_ROUTE)

    assert (first.status_code, first.json()) == (
        HTTP_OK,
        {"model_id": standin_tei.DEFAULT_MODEL, **BATCH_LIMIT},
    )
    assert (second.status_code, second.json()) == (
        HTTP_OK,
        {"model_id": "another/model", **BATCH_LIMIT},
    )


def test_tei_names_no_model_when_a_test_says_so(tei: Tei) -> None:
    tune(tei.tree, TEI, TEI_NO_MODEL_ID)

    with _tei_client(tei) as client:
        info = client.get(INFO_ROUTE)

    assert (info.status_code, info.json()) == (HTTP_OK, BATCH_LIMIT)


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
    """A scenario kills the program inside a held call. The stand-in must serve the next run.

    The caller of this test leaves as a killed program can: its connection
    ends with a reset. The stand-in then cannot write the answer of the held
    call. It answers the next call, and it writes no traceback. The wait
    after the next call gives the held call the time to fail.
    """
    held_body = {"inputs": [MARK]}
    tune(tei.tree, TEI, TEI_HOLD_EMBED, MARK)

    with socket.create_connection((LOOPBACK, tei.port), timeout=TEI_TIMEOUT_S) as conn:
        conn.sendall(_embed_request(held_body))
        wait_until(lambda: _embed_bodies(tei) == [held_body], "the held call", TEI_TIMEOUT_S)
        conn.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, RESET_AT_CLOSE)

    untune(tei.tree, TEI, TEI_HOLD_EMBED)

    with _tei_client(tei) as client:
        answered = client.post(EMBED_ROUTE, json=held_body)

    [child] = supervisor.children

    with pytest.raises(ProcError, match="did not exit"):
        child.wait(HOLD_CHECK_S)

    assert answered.status_code == HTTP_OK
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


# -------------------------------------------------------------------- helpers


def _words(tree: Tree, name: str, *args: str) -> list[str]:
    """One stand-in through its wrapper, as a service finds it on its PATH."""
    return [str(tree.bin_dir / name), *args]


def _systemctl(tree: Tree, supervisor: Supervisor, *args: str) -> Finished:
    return supervisor.run(SYSTEMCTL, _words(tree, SYSTEMCTL, *args), base_env(tree), tree.root)


def _vmm_post(
    tree: Tree, path: str, body: object, protocol: str = str(HOST_API_PROTOCOL)
) -> httpx.Response:
    """One request to the vmm stand-in, as a client of `vmm.sock` sends it."""
    transport = httpx.HTTPTransport(uds=str(tree.vmm_socket))
    headers = {HOST_API_HEADER: protocol, "Content-Type": "application/json"}

    with httpx.Client(transport=transport, timeout=VMM_TIMEOUT_S) as client:
        return client.post(f"http://vmm{path}", json=body, headers=headers)


def _create_row(
    tree: Tree, session: str, cwd: str, created_at: int, family: str = "chat"
) -> dict[str, object]:
    """One row made through the create route. Answers the row of the reply."""
    body = {
        "created_at": created_at,
        "cwd": cwd,
        "family": family,
        "id": session,
        "origin": {"door": {}},
    }
    answer = _vmm_post(tree, CREATE.path, body)
    assert answer.status_code == HTTP_OK, answer.text
    row: dict[str, object] = answer.json()

    return row


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


def _embed_request(body: dict[str, list[str]]) -> bytes:
    """One embed call as the bytes of an HTTP request, for a test that holds the socket."""
    raw = json.dumps(body).encode("utf-8")
    head = (
        f"POST {EMBED_ROUTE} HTTP/1.1\r\nHost: tei\r\nContent-Type: application/json\r\n"
        f"Content-Length: {len(raw)}\r\n\r\n"
    )

    return head.encode("ascii") + raw


def _embed_bodies(tei: Tei) -> list[object]:
    return [call["body"] for call in tei_calls(tei.tree) if call["path"] == EMBED_ROUTE]


def _generate_body() -> dict[str, object]:
    return {
        "key_alias": ALIAS,
        "models": ["agent-router"],
        "max_budget": 15.0,
        "budget_duration": "1d",
    }
