"""The one identity kind (contract 04).

Every test here drives the real app through a `TestClient` and holds it to
contract 04 §4, §5 and §6.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from chaperone.app import PepConfig, create_app
from chaperone.family_app import MANIFEST_ACTION, FamilyDeps, FamilyGate
from chaperone.family_audit import FamilyAudit
from chaperone.family_decisions import Executor, FamilyDecision
from chaperone.family_grants import FamilyStore
from chaperone.faults import FaultWriter
from chaperone.fences import ArgDeny
from chaperone.mcp_client import UpstreamSpec
from chaperone.reload_pool import serving_of
from chaperone_family_helpers import FAMILY_TOKEN, make_grants, write_grants, write_grants_raw
from chaperone_helpers import FakePool
from fastapi.testclient import TestClient

from chaperone import family_app

TURN = "01K5J9QWB9R1V3T5Y7H9J2K4P6"
SESSION = "owui-8f1c2e"
HA_TOKEN = "test-ha-token"
#: What the local embedding service really reports (`/info`, measured 2026-09).
MODEL_ID = "BAAI/bge-base-en-v1.5"

KAGI = UpstreamSpec(name="kagi", command="x", args=(), env={})
GITHUB_FENCED = UpstreamSpec(
    name="github",
    command="x",
    args=(),
    env={},
    arg_denies=(
        ArgDeny(tools=frozenset({"push_files"}), arg="branch", values=frozenset({"main"})),
    ),
)

EMBED_CALL: dict[str, object] = {"tool": "embed", "args": {"input": "hi"}}

#: Where the mock transport answers. `.invalid` names no host.
TEI_URL = "http://tei.invalid:8085"
HA_URL = "http://ha.invalid:8123"

JSON_BODY = {"content-type": "application/json"}

#: More levels than the JSON reader of each supported interpreter reads on
#: a stack of the default size. Python 3.14 reads more than 100,000 levels.
#: The vectors use the same number.
TOO_DEEP = 400_000
TOO_DEEP_JSON = b"[" * TOO_DEEP + b"]" * TOO_DEEP


def build(
    tmp_path: Path,
    *,
    pool: FakePool | None = None,
    upstreams: dict[str, UpstreamSpec] | None = None,
    ha_status: int = 200,
    tei_status: int = 200,
    info_status: int = 200,
    model_id: str = MODEL_ID,
    secrets: dict[str, str] | None = None,
    tei_url: str = TEI_URL,
    ha_url: str = HA_URL,
    embed_body: bytes | None = None,
    info_body: bytes | None = None,
) -> TestClient:
    """The app with the family path configured and no live service.

    `embed_body` and `info_body` are the bytes of a reply of the embedding
    service, for a test of a reply that is not the shape the service gives.
    """
    pool = pool or FakePool()

    def handle(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/embed"):
            if embed_body is not None:
                return httpx.Response(tei_status, content=embed_body, headers=JSON_BODY)
            return httpx.Response(tei_status, json=[[0.1] * 768])
        if url.endswith("/info"):
            if info_body is not None:
                return httpx.Response(info_status, content=info_body, headers=JSON_BODY)
            if info_status != 200:
                return httpx.Response(info_status, text="boom")
            return httpx.Response(info_status, json={"model_id": model_id})
        if "/api/services/" in url:
            return httpx.Response(ha_status, text="[]")
        return httpx.Response(404, text="nope")

    app = create_app(
        PepConfig(
            audit_dir=tmp_path / "audit",
            rework_dir=tmp_path / "rework",
            upstreams=upstreams if upstreams is not None else {"kagi": KAGI},
            secrets={"HA_TOKEN": HA_TOKEN} if secrets is None else secrets,
            tei_url=tei_url,
            ha_url=ha_url,
        ),
        pool=pool,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
    )
    client = TestClient(app)
    client.__enter__()  # run lifespan
    return client


def grants_dir(tmp_path: Path) -> Path:
    return tmp_path / "rework" / "grants"


def faults_dir(tmp_path: Path) -> Path:
    return tmp_path / "rework" / "faults" / "pep"


def v2_lines(tmp_path: Path) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for path in sorted((tmp_path / "rework" / "audit").glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            records.append(json.loads(line))
    return records


def unidentified_lines(tmp_path: Path) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for path in sorted((tmp_path / "audit").glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            records.append(json.loads(line))
    return records


def family_auth(token: str = FAMILY_TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def last_call(tmp_path: Path) -> dict[str, object]:
    calls = [r for r in v2_lines(tmp_path) if r["tool"] != MANIFEST_ACTION]
    return calls[-1]


# ---- a family token is served ---------------------------------------------


def test_a_granted_verb_is_allowed(tmp_path: Path) -> None:
    write_grants(grants_dir(tmp_path), make_grants())
    client = build(tmp_path)
    reply = client.post("/call", json=EMBED_CALL, headers=family_auth())
    assert reply.status_code == 200
    assert reply.json()["result"]["dims"] == 768


def test_a_granted_mcp_tool_reaches_the_upstream(tmp_path: Path) -> None:
    write_grants(grants_dir(tmp_path), make_grants())
    pool = FakePool()
    client = build(tmp_path, pool=pool)
    reply = client.post(
        "/call",
        json={"tool": "kagi__kagi_search_fetch", "args": {"query": "boiler"}},
        headers=family_auth(),
    )
    assert reply.status_code == 200
    assert pool.calls == [("kagi", "kagi_search_fetch", {"query": "boiler"})]


def test_a_granted_ha_call_reaches_home_assistant(tmp_path: Path) -> None:
    write_grants(grants_dir(tmp_path), make_grants())
    client = build(tmp_path)
    reply = client.post(
        "/call",
        json={
            "tool": "ha_call",
            "args": {"domain": "notify", "service": "mobile_app_example_phone"},
        },
        headers=family_auth(),
    )
    assert reply.status_code == 200


def test_a_home_assistant_error_is_an_allow_that_failed(tmp_path: Path) -> None:
    write_grants(grants_dir(tmp_path), make_grants())
    client = build(tmp_path, ha_status=502)
    reply = client.post(
        "/call",
        json={
            "tool": "ha_call",
            "args": {"domain": "notify", "service": "mobile_app_example_phone"},
        },
        headers=family_auth(),
    )
    assert reply.status_code == 502
    assert last_call(tmp_path)["reason"] == "upstream_failed"


def test_an_upstream_failure_is_an_allow_that_failed(tmp_path: Path) -> None:
    """§5 row 11: the effect may already have happened, so it is not a deny."""
    write_grants(grants_dir(tmp_path), make_grants())
    pool = FakePool()
    pool.fail_with = "kagi is down"
    client = build(tmp_path, pool=pool)
    reply = client.post(
        "/call",
        json={"tool": "kagi__kagi_search_fetch", "args": {"query": "x"}},
        headers=family_auth(),
    )
    assert reply.status_code == 502
    record = last_call(tmp_path)
    assert record["decision"] == "allow"
    assert record["reason"] == "upstream_failed"


# ---- denials ---------------------------------------------------------------


def test_a_tool_that_is_not_granted(tmp_path: Path) -> None:
    write_grants(grants_dir(tmp_path), make_grants())
    client = build(tmp_path)
    reply = client.post(
        "/call",
        json={"tool": "kagi__kagi_summarizer", "args": {"url": "x"}},
        headers=family_auth(),
    )
    assert reply.status_code == 403
    assert reply.json()["reason"] == "tool_not_granted"


def test_an_argument_the_catalog_refuses(tmp_path: Path) -> None:
    write_grants(grants_dir(tmp_path), make_grants())
    client = build(tmp_path)
    reply = client.post(
        "/call", json={"tool": "embed", "args": {"input": "hi", "nope": 1}}, headers=family_auth()
    )
    assert reply.status_code == 400
    assert reply.json()["reason"] == "arg_validation"


def test_an_upstream_fence_refuses_before_any_grant(tmp_path: Path) -> None:
    """§5 row 3 before row 4: the fence is on the server, not on the family."""
    write_grants(grants_dir(tmp_path), make_grants())
    client = build(tmp_path, upstreams={"kagi": KAGI, "github": GITHUB_FENCED})
    reply = client.post(
        "/call",
        json={"tool": "github__push_files", "args": {"branch": "main"}},
        headers=family_auth(),
    )
    assert reply.status_code == 400
    assert reply.json()["reason"] == "arg_validation"


def test_a_verb_whose_execution_is_a_later_stage(tmp_path: Path) -> None:
    write_grants(grants_dir(tmp_path), make_grants(verbs={"enqueue": {"targets": ["worker"]}}))
    client = build(tmp_path)
    reply = client.post(
        "/call",
        json={"tool": "enqueue", "args": {"family": "worker", "message": "go"}},
        headers=family_auth(),
    )
    assert reply.status_code == 501
    assert reply.json()["reason"] == "not_implemented"


def test_a_gated_action_is_the_stage_five_seam(tmp_path: Path) -> None:
    write_grants(grants_dir(tmp_path), make_grants(approval=["embed"]))
    client = build(tmp_path)
    reply = client.post("/call", json=EMBED_CALL, headers=family_auth())
    assert reply.status_code == 501
    assert reply.json()["reason"] == "not_implemented"


def test_over_the_rate_limit(tmp_path: Path) -> None:
    write_grants(grants_dir(tmp_path), make_grants(limits={"pep_rpm": 2}))
    client = build(tmp_path)
    body = EMBED_CALL
    codes = [client.post("/call", json=body, headers=family_auth()).status_code for _ in range(3)]
    assert codes == [200, 200, 429]


# ---- the grant file decides every call -------------------------------------


def test_a_rewrite_between_two_calls_adds_reach(tmp_path: Path) -> None:
    write_grants(grants_dir(tmp_path), make_grants(tools={}))
    client = build(tmp_path)
    body = {"tool": "kagi__kagi_search_fetch", "args": {"query": "x"}}
    assert client.post("/call", json=body, headers=family_auth()).status_code == 403

    write_grants(grants_dir(tmp_path), make_grants(tools={"kagi": ["kagi_search_fetch"]}))
    assert client.post("/call", json=body, headers=family_auth()).status_code == 200


def test_a_rewrite_between_two_calls_removes_reach(tmp_path: Path) -> None:
    write_grants(grants_dir(tmp_path), make_grants())
    client = build(tmp_path)
    body = {"tool": "kagi__kagi_search_fetch", "args": {"query": "x"}}
    assert client.post("/call", json=body, headers=family_auth()).status_code == 200

    write_grants(grants_dir(tmp_path), make_grants(tools={}))
    assert client.post("/call", json=body, headers=family_auth()).status_code == 403


def test_a_deleted_grant_file_revokes_at_once(tmp_path: Path) -> None:
    path = write_grants(grants_dir(tmp_path), make_grants())
    client = build(tmp_path)
    body = EMBED_CALL
    assert client.post("/call", json=body, headers=family_auth()).status_code == 200

    path.unlink()
    reply = client.post("/call", json=body, headers=family_auth())
    assert reply.status_code == 403
    assert reply.json()["reason"] == "unknown_token"


def test_a_malformed_grant_file_fails_closed_and_faults(tmp_path: Path) -> None:
    write_grants_raw(grants_dir(tmp_path), "chat", "{ this is not json")
    client = build(tmp_path)
    reply = client.post("/call", json=EMBED_CALL, headers=family_auth())
    assert reply.status_code == 403
    assert reply.json()["reason"] == "unknown_token"

    fault = json.loads((faults_dir(tmp_path) / "chat.json").read_text(encoding="utf-8"))
    assert [f["code"] for f in fault["faults"]] == ["grants_stale"]
    assert fault["source"] == "pep"


def test_a_repaired_grant_file_clears_the_fault(tmp_path: Path) -> None:
    write_grants_raw(grants_dir(tmp_path), "chat", "{ this is not json")
    client = build(tmp_path)
    body = EMBED_CALL
    assert client.post("/call", json=body, headers=family_auth()).status_code == 403

    write_grants(grants_dir(tmp_path), make_grants())
    assert client.post("/call", json=body, headers=family_auth()).status_code == 200
    fault = json.loads((faults_dir(tmp_path) / "chat.json").read_text(encoding="utf-8"))
    assert fault["faults"] == []


# ---- the advisory headers --------------------------------------------------


def test_good_headers_land_in_the_claimed_object(tmp_path: Path) -> None:
    write_grants(grants_dir(tmp_path), make_grants())
    client = build(tmp_path)
    client.post(
        "/call",
        json=EMBED_CALL,
        headers={**family_auth(), "X-Session-Id": SESSION, "X-Turn-Id": TURN},
    )
    record = last_call(tmp_path)
    assert record["claimed"] == {"session_id": SESSION, "turn_id": TURN, "delegation_id": None}


def test_a_malformed_header_never_denies_the_call(tmp_path: Path) -> None:
    write_grants(grants_dir(tmp_path), make_grants())
    client = build(tmp_path)
    reply = client.post(
        "/call",
        json=EMBED_CALL,
        headers={**family_auth(), "X-Turn-Id": "not-a-ulid"},
    )
    assert reply.status_code == 200
    claimed = last_call(tmp_path)["claimed"]
    assert isinstance(claimed, dict)
    assert claimed["turn_id"] is None


def test_an_oversized_header_is_dropped(tmp_path: Path) -> None:
    write_grants(grants_dir(tmp_path), make_grants())
    client = build(tmp_path)
    huge = "a" * 4096
    reply = client.post(
        "/call",
        json=EMBED_CALL,
        headers={**family_auth(), "X-Session-Id": huge},
    )
    assert reply.status_code == 200
    record = last_call(tmp_path)
    assert record["claimed"] == {"session_id": None, "turn_id": None, "delegation_id": None}
    assert huge not in json.dumps(record)


def test_a_forged_session_id_changes_no_decision(tmp_path: Path) -> None:
    """§3.1 rule 1. The header names another family's session and is ignored."""
    write_grants(grants_dir(tmp_path), make_grants(tools={}))
    client = build(tmp_path)
    reply = client.post(
        "/call",
        json={"tool": "kagi__kagi_search_fetch", "args": {"query": "x"}},
        headers={**family_auth(), "X-Session-Id": "auto-vault-oracle"},
    )
    assert reply.status_code == 403


# ---- audit v2 --------------------------------------------------------------


def test_every_decision_writes_one_v2_line(tmp_path: Path) -> None:
    write_grants(grants_dir(tmp_path), make_grants())
    client = build(tmp_path)
    client.post("/call", json=EMBED_CALL, headers=family_auth())
    client.post("/call", json={"tool": "nope", "args": {}}, headers=family_auth())
    client.get("/manifest", headers=family_auth())

    records = v2_lines(tmp_path)
    assert [(r["tool"], r["decision"], r["reason"]) for r in records] == [
        ("embed", "allow", "granted"),
        ("nope", "deny", "tool_not_granted"),
        (MANIFEST_ACTION, "allow", "granted"),
    ]
    for record in records:
        assert record["family"] == "chat"
        assert record["chain"] == ["chat"]
        assert record["sandbox_id_trusted"] is False


def test_an_allowed_call_records_its_latency_and_revision(tmp_path: Path) -> None:
    grants = make_grants()
    write_grants(grants_dir(tmp_path), grants)
    client = build(tmp_path)
    client.post("/call", json=EMBED_CALL, headers=family_auth())
    record = last_call(tmp_path)
    assert record["grants_rev"] == grants.rev
    assert isinstance(record["latency_ms"], int)
    assert record["waited_ms"] == 0


def test_the_family_audit_never_lands_in_the_unidentified_log(tmp_path: Path) -> None:
    """Two directories, one per record shape. A resolved call never writes a
    line into the log that exists for requests naming no family."""
    write_grants(grants_dir(tmp_path), make_grants())
    client = build(tmp_path)
    client.post("/call", json=EMBED_CALL, headers=family_auth())
    assert unidentified_lines(tmp_path) == []


# ---- the manifest ----------------------------------------------------------


def test_the_manifest_answers_contract_four(tmp_path: Path) -> None:
    grants = make_grants(approval=["ha_call"])
    write_grants(grants_dir(tmp_path), grants)
    client = build(tmp_path)
    body = client.get("/manifest", headers=family_auth()).json()

    assert body["family"] == "chat"
    assert body["rev"] == grants.rev
    assert body["model_alias"] == "agent-router"
    assert body["limits"] == {"pep_rpm": 60, "max_inflight_delegations": 2}
    by_name = {t["name"]: t for t in body["tools"]}
    # §4 rule 4: this PEP has no phone rail, so the gated `ha_call` is one
    # more action `/call` cannot execute and the manifest leaves it out.
    # `test_chaperone_approval_hold.py` holds the flag on a PEP that has one.
    assert set(by_name) == {"kagi__kagi_search_fetch", "embed"}
    assert by_name["embed"]["approval"] is False
    assert by_name["embed"]["schema"]["required"] == ["input"]


def test_the_manifest_hides_a_server_this_pep_has_not_loaded(tmp_path: Path) -> None:
    write_grants(grants_dir(tmp_path), make_grants(tools={"mail": ["send_standup_email"]}))
    client = build(tmp_path)
    body = client.get("/manifest", headers=family_auth()).json()
    assert [t["name"] for t in body["tools"]] == ["embed", "ha_call"]


def test_the_manifest_hides_a_seam(tmp_path: Path) -> None:
    write_grants(
        grants_dir(tmp_path),
        make_grants(verbs={"embed": {}, "release": {"components": ["agent-control"]}}),
    )
    client = build(tmp_path)
    body = client.get("/manifest", headers=family_auth()).json()
    assert [t["name"] for t in body["tools"]] == ["kagi__kagi_search_fetch", "embed"]


# ---- a bearer that resolves to nothing -------------------------------------


def test_an_unknown_token_reaches_no_family(tmp_path: Path) -> None:
    """Contract 04 §5 row 1. The refusal is recorded in the unidentified log,
    because there is no family to write a §6 record for."""
    write_grants(grants_dir(tmp_path), make_grants())
    client = build(tmp_path)
    reply = client.post("/call", json=EMBED_CALL, headers={"Authorization": "Bearer nobody"})

    assert reply.status_code == 403
    assert v2_lines(tmp_path) == []
    assert [r["reason"] for r in unidentified_lines(tmp_path)] == ["unknown_token"]


# ---- fail closed -----------------------------------------------------------


def test_a_manifest_over_the_rate_limit_is_refused(tmp_path: Path) -> None:
    write_grants(grants_dir(tmp_path), make_grants(limits={"pep_rpm": 1}))
    client = build(tmp_path)
    assert client.get("/manifest", headers=family_auth()).status_code == 200
    reply = client.get("/manifest", headers=family_auth())
    assert reply.status_code == 429
    assert reply.json()["reason"] == "rate_limited"


def test_a_broken_audit_blocks_the_effect(tmp_path: Path) -> None:
    """The effect must not happen where it cannot be recorded."""
    write_grants(grants_dir(tmp_path), make_grants())
    pool = FakePool()
    client = build(tmp_path, pool=pool)
    audit_dir = tmp_path / "rework" / "audit"
    audit_dir.chmod(0o500)
    try:
        reply = client.post(
            "/call",
            json={"tool": "kagi__kagi_search_fetch", "args": {"query": "x"}},
            headers=family_auth(),
        )
    finally:
        audit_dir.chmod(0o750)
    assert reply.status_code == 500
    assert pool.calls == []


def test_an_execution_that_raises_anything_is_internal_error(tmp_path: Path) -> None:
    class Exploding(FakePool):
        async def call(self, server: str, tool: str, args: dict[str, object]) -> str:
            raise RuntimeError("something nobody predicted")

    write_grants(grants_dir(tmp_path), make_grants())
    client = build(tmp_path, pool=Exploding())
    reply = client.post(
        "/call",
        json={"tool": "kagi__kagi_search_fetch", "args": {"query": "x"}},
        headers=family_auth(),
    )
    assert reply.status_code == 500
    assert last_call(tmp_path)["reason"] == "internal_error"


def test_a_decision_that_raises_is_internal_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_grants(grants_dir(tmp_path), make_grants())
    client = build(tmp_path)

    def explode(*_args: object, **_kwargs: object) -> FamilyDecision:
        raise RuntimeError("the decision core is broken")

    monkeypatch.setattr(family_app, "decide_family", explode)
    reply = client.post("/call", json=EMBED_CALL, headers=family_auth())
    assert reply.status_code == 500
    assert reply.json()["reason"] == "internal_error"
    record = last_call(tmp_path)
    assert (record["decision"], record["reason"]) == ("deny", "internal_error")
    assert len(v2_lines(tmp_path)) == 1


def test_an_allow_this_stage_cannot_execute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The decision core and the executor agree today. If they ever stop, the
    call fails closed instead of falling through to something else.

    `release` has an executor branch (`stage7-releases.md` §2.3), and a PEP
    with no spool answers 502 `upstream_failed` from it rather than 500 — an
    allowed call that failed, which is a different row of contract 04 §5. So
    the test forces a name `_execute` has no branch for at all.
    """
    write_grants(grants_dir(tmp_path), make_grants())
    client = build(tmp_path)

    def allow_a_seam(*_args: object, **_kwargs: object) -> FamilyDecision:
        return FamilyDecision(allow=True, executor=Executor.VERB, tool="no_such_verb", args={})

    monkeypatch.setattr(family_app, "decide_family", allow_a_seam)
    reply = client.post("/call", json={"tool": "release", "args": {}}, headers=family_auth())
    assert reply.status_code == 500
    assert last_call(tmp_path)["reason"] == "internal_error"


def test_a_model_argument_is_an_unknown_argument(tmp_path: Path) -> None:
    """The PEP serves one pinned model (§4.1). A caller does not choose
    it, so `model` is refused as an unknown argument, not run and failed."""
    write_grants(grants_dir(tmp_path), make_grants())
    client = build(tmp_path)
    reply = client.post(
        "/call",
        json={"tool": "embed", "args": {"input": "hi", "model": MODEL_ID}},
        headers=family_auth(),
    )
    assert reply.status_code == 400
    assert reply.json()["reason"] == "arg_validation"
    assert last_call(tmp_path)["reason"] == "arg_validation"


def test_the_reply_reports_the_served_model_id(tmp_path: Path) -> None:
    """The id the service reports, as it reports it, slash and all: a caller
    checks it against the id its own index recorded (§4.1)."""
    write_grants(grants_dir(tmp_path), make_grants())
    client = build(tmp_path)
    reply = client.post("/call", json=EMBED_CALL, headers=family_auth())
    assert reply.status_code == 200
    assert reply.json()["result"]["model"] == MODEL_ID


def test_an_embedding_service_error(tmp_path: Path) -> None:
    write_grants(grants_dir(tmp_path), make_grants())
    client = build(tmp_path, tei_status=503)
    assert client.post("/call", json=EMBED_CALL, headers=family_auth()).status_code == 502


def test_an_unreadable_model_id_does_not_block_the_call(tmp_path: Path) -> None:
    write_grants(grants_dir(tmp_path), make_grants())
    client = build(tmp_path, info_status=500)
    reply = client.post("/call", json=EMBED_CALL, headers=family_auth())
    assert reply.status_code == 200
    assert reply.json()["result"]["model"] == "unknown"


@pytest.mark.parametrize(
    "embed_body",
    [
        pytest.param(b"not json", id="not-json"),
        pytest.param(TOO_DEEP_JSON, id="nested-too-deep"),
        pytest.param(b'{"error": "boom"}', id="an-object"),
        pytest.param(b"[]", id="no-vector"),
        pytest.param(b"[5]", id="a-vector-that-is-a-number"),
        pytest.param(b'[["a"]]', id="a-text-in-the-vector"),
        pytest.param(b"[[true]]", id="a-boolean-in-the-vector"),
        pytest.param(b"[[null]]", id="a-null-in-the-vector"),
        pytest.param(b"[[0.1, NaN]]", id="not-a-number-in-the-vector"),
        pytest.param(b"[[0.1, Infinity]]", id="an-infinity-in-the-vector"),
    ],
)
def test_an_embed_reply_of_another_shape_is_an_upstream_failure(
    tmp_path: Path, embed_body: bytes
) -> None:
    """§4.1 fixes the reply of `embed`: one list of numbers. A reply of the
    embedding service that holds no such list is §5 row 11: an allowed call
    that failed, with its own audit line."""
    write_grants(grants_dir(tmp_path), make_grants())
    client = build(tmp_path, embed_body=embed_body)
    reply = client.post("/call", json=EMBED_CALL, headers=family_auth())

    assert reply.status_code == 502
    assert reply.json()["reason"] == "upstream_failed"
    record = last_call(tmp_path)
    assert (record["decision"], record["reason"]) == ("allow", "upstream_failed")
    assert len(v2_lines(tmp_path)) == 1


def test_an_embed_reply_of_whole_numbers_is_a_vector(tmp_path: Path) -> None:
    """A JSON integer is a number, of any length."""
    write_grants(grants_dir(tmp_path), make_grants())
    client = build(tmp_path, embed_body=b"[[1, -2, 0.5, 1" + b"0" * 400 + b"]]")
    reply = client.post("/call", json=EMBED_CALL, headers=family_auth())

    assert reply.status_code == 200
    assert reply.json()["result"]["dims"] == 4


@pytest.mark.parametrize(
    "info_body",
    [
        pytest.param(TOO_DEEP_JSON, id="nested-too-deep"),
        pytest.param(b'["BAAI/bge-base-en-v1.5"]', id="a-list"),
        pytest.param(b"null", id="a-null"),
        pytest.param(b"{}", id="no-model-id"),
        pytest.param(b'{"model_id": 5}', id="a-number-for-the-id"),
        pytest.param(b'{"model_id": null}', id="a-null-for-the-id"),
        pytest.param(b'{"model_id": {"name": "x"}}', id="an-object-for-the-id"),
    ],
)
def test_a_model_id_of_another_shape_reads_as_unknown(tmp_path: Path, info_body: bytes) -> None:
    """The id is a string (§4.1). A reply of `/info` that holds no string id
    reads as a reply that cannot be read: the call goes on, and the reply
    says `unknown`."""
    write_grants(grants_dir(tmp_path), make_grants())
    client = build(tmp_path, info_body=info_body)
    reply = client.post("/call", json=EMBED_CALL, headers=family_auth())

    assert reply.status_code == 200
    assert reply.json()["result"]["model"] == "unknown"


def test_ha_call_without_a_token_fails_closed(tmp_path: Path) -> None:
    write_grants(grants_dir(tmp_path), make_grants())
    client = build(tmp_path, secrets={})
    reply = client.post(
        "/call",
        json={
            "tool": "ha_call",
            "args": {"domain": "notify", "service": "mobile_app_example_phone"},
        },
        headers=family_auth(),
    )
    assert reply.status_code == 502


def test_ha_call_on_a_site_with_no_home_assistant_says_so(tmp_path: Path) -> None:
    """A site with no Home Assistant sets no URL. The call fails closed with
    a line naming the two variables, and never dials a relative URL."""
    write_grants(grants_dir(tmp_path), make_grants())
    client = build(tmp_path, ha_url="")
    reply = client.post(
        "/call",
        json={
            "tool": "ha_call",
            "args": {"domain": "notify", "service": "mobile_app_example_phone"},
        },
        headers=family_auth(),
    )
    assert reply.status_code == 502
    assert "HA_URL" in reply.json()["detail"]
    assert "AGENT_HA_URL" in reply.json()["detail"]


def test_embed_on_a_pep_with_no_tei_url_says_so(tmp_path: Path) -> None:
    write_grants(grants_dir(tmp_path), make_grants())
    client = build(tmp_path, tei_url="")
    reply = client.post("/call", json=EMBED_CALL, headers=family_auth())
    assert reply.status_code == 502
    assert "AGENT_LAN_ADDRESS" in reply.json()["detail"]


def test_ha_call_carries_the_entity_and_the_data(tmp_path: Path) -> None:
    """§4.1 rule 3: `data` is not fenced and rides as sent."""
    fence = {"domain": "light", "service": "turn_on", "entity_id": "light.desk"}
    write_grants(grants_dir(tmp_path), make_grants(verbs={"ha_call": {"allow": [fence]}}))
    client = build(tmp_path)
    reply = client.post(
        "/call",
        json={"tool": "ha_call", "args": {**fence, "data": {"brightness": 40}}},
        headers=family_auth(),
    )
    assert reply.status_code == 200


def test_the_manifest_skips_a_tool_the_server_does_not_list(tmp_path: Path) -> None:
    write_grants(grants_dir(tmp_path), make_grants(tools={"kagi": ["kagi_retired"]}))
    client = build(tmp_path)
    body = client.get("/manifest", headers=family_auth()).json()
    assert [t["name"] for t in body["tools"]] == ["embed", "ha_call"]


# ---- the layer with no process around it -----------------------------------


def bare_gate(tmp_path: Path, *, secrets: dict[str, str] | None = None) -> FamilyGate:
    """No pool and no HTTP client: what the layer holds before `app.py`'s
    lifespan has built either."""
    grants_dir(tmp_path).mkdir(parents=True, exist_ok=True)
    faults = FaultWriter(tmp_path / "rework" / "faults" / "pep")
    return FamilyGate(
        FamilyDeps(
            store=FamilyStore(grants_dir(tmp_path), faults),
            audit=FamilyAudit(tmp_path / "rework" / "audit"),
            rate=lambda _key, _limit: False,
            pool=lambda: None,
            client=lambda: None,
            serving=lambda: serving_of({"kagi": KAGI}),
            secrets=secrets or {},
        )
    )


async def test_no_pool_means_no_server_is_known(tmp_path: Path) -> None:
    reply = await bare_gate(tmp_path).call(
        make_grants(), "kagi__kagi_search_fetch", {"query": "x"}, {}
    )
    assert reply.status == 501
    assert reply.payload["reason"] == "not_implemented"


async def test_no_pool_hides_every_mcp_tool_from_the_manifest(tmp_path: Path) -> None:
    tools = bare_gate(tmp_path).manifest(make_grants(), {}).payload["tools"]
    assert isinstance(tools, list)
    assert [t["name"] for t in tools] == ["embed", "ha_call"]


async def test_no_http_client_fails_a_verb_closed(tmp_path: Path) -> None:
    reply = await bare_gate(tmp_path).call(make_grants(), "embed", {"input": "hi"}, {})
    assert reply.status == 502
    assert reply.payload["reason"] == "upstream_failed"


async def test_no_http_client_fails_ha_call_closed(tmp_path: Path) -> None:
    gate = bare_gate(tmp_path, secrets={"HA_TOKEN": HA_TOKEN})
    args: dict[str, object] = {"domain": "notify", "service": "mobile_app_example_phone"}
    reply = await gate.call(make_grants(), "ha_call", args, {})
    assert reply.status == 502
