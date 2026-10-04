"""No request makes a route raise: each one gets the body of contract 04
§5.1, and each `/call` of a family gets its audit line.

`chaperone/AGENTS.md`, "Fail closed". The tests here send what the body
reader takes and a later step cannot use: a string that is not Unicode
text, and arguments that nest past the limit of the interpreter. They also
make a layer raise a failure that no handler names.

The client does not re-raise an error of the server. A test then reads the
answer that a caller gets.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Final

import httpx
import pytest
from chaperone.app import PepConfig, create_app
from chaperone.family_audit import (
    AUDIT_ARG_STRING_MAX_CHARS,
    AUDIT_TRUNCATION_MARKER,
    UNRECORDABLE_ARGS,
)
from chaperone.gatekeeper import GateHeld, Gatekeeper, GateNotice, GateTicket, ReachCheck
from chaperone.gates import gate_id
from chaperone.mcp_client import UpstreamSpec
from chaperone_family_helpers import FAMILY_TOKEN, make_grants, write_grants
from chaperone_helpers import FakePool
from fastapi.testclient import TestClient

from chaperone import family_app

KAGI: Final = UpstreamSpec(name="kagi", command="x", args=(), env={})
SEARCH: Final = "kagi__kagi_search_fetch"

#: A JSON escape for half of a surrogate pair. The body reader takes it, and
#: the string that it gives has no UTF-8 form.
LONE: Final = "\\ud800"

#: More levels than the audit writer walks, and less than the body reader
#: reads.
DEEP: Final = 5000
DEEP_JSON: Final = "[" * DEEP + "]" * DEEP

INTERNAL_ERROR: Final = {"ok": False, "reason": "internal_error", "detail": None}


class Notified:
    """A phone rail that takes each push."""

    def __init__(self) -> None:
        self.notices: list[GateNotice] = []

    async def notify(self, notice: GateNotice) -> bool:
        self.notices.append(notice)

        return True


def build(
    tmp_path: Path,
    *,
    pool: FakePool | None = None,
    gatekeeper: Gatekeeper | None = None,
    requests: list[httpx.Request] | None = None,
) -> TestClient:
    def handle(request: httpx.Request) -> httpx.Response:
        if requests is not None:
            requests.append(request)
        if request.url.path.endswith("/info"):
            return httpx.Response(200, json={"model_id": "m"})

        return httpx.Response(200, json=[[0.1, 0.2]])

    app = create_app(
        PepConfig(
            audit_dir=tmp_path / "audit",
            rework_dir=tmp_path / "rework",
            upstreams={"kagi": KAGI},
            tei_url="http://tei.invalid:8085",
        ),
        pool=pool or FakePool(),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
        gatekeeper=gatekeeper,
    )
    client = TestClient(app, raise_server_exceptions=False)
    client.__enter__()  # run lifespan

    return client


def grants_dir(tmp_path: Path) -> Path:
    return tmp_path / "rework" / "grants"


def _lines(directory: Path) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for path in sorted(directory.glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            records.append(json.loads(line))

    return records


def family_lines(tmp_path: Path) -> list[dict[str, object]]:
    return _lines(tmp_path / "rework" / "audit")


def unidentified_lines(tmp_path: Path) -> list[dict[str, object]]:
    return _lines(tmp_path / "audit")


def call(client: TestClient, body: str, token: str = FAMILY_TOKEN) -> httpx.Response:
    """One `POST /call` with the exact text of a body."""
    return client.post(
        "/call",
        content=body.encode("ascii"),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )


def body_of(tool: str, args: str) -> str:
    return f'{{"tool":"{tool}","args":{args}}}'


#: What a line holds for a string that the audit cut before the part that
#: is not text.
CUT: Final = {"query": "a" * AUDIT_ARG_STRING_MAX_CHARS + AUDIT_TRUNCATION_MARKER}

#: Arguments that the body reader takes and that no audit line can hold in
#: full, each with what its line holds.
NOT_FOR_A_LINE: Final = [
    pytest.param(f'{{"query":"{LONE}"}}', UNRECORDABLE_ARGS, id="a-string-that-is-not-text"),
    pytest.param(f'{{"{LONE}":1}}', UNRECORDABLE_ARGS, id="a-key-that-is-not-text"),
    pytest.param(f'{{"query":{DEEP_JSON}}}', UNRECORDABLE_ARGS, id="nested-too-deep"),
    pytest.param(f'{{"query":"{"a" * 9000}{LONE}"}}', CUT, id="not-text-after-the-audit-cut"),
]


# ---- the audit line of a call of a family -----------------------------------


@pytest.mark.parametrize(("args", "held"), NOT_FOR_A_LINE)
def test_a_denied_call_keeps_its_reason_and_its_line(
    tmp_path: Path, args: str, held: dict[str, object]
) -> None:
    """Row 4 of §5 decides this call. The line says so, and it holds a
    marker in place of arguments that no line can hold."""
    write_grants(grants_dir(tmp_path), make_grants())
    client = build(tmp_path)

    reply = call(client, body_of("kagi__not_granted", args))

    assert reply.status_code == 403
    assert reply.json()["reason"] == "tool_not_granted"
    [record] = family_lines(tmp_path)
    assert (record["decision"], record["reason"]) == ("deny", "tool_not_granted")
    assert record["args"] == held


@pytest.mark.parametrize(("args", "held"), NOT_FOR_A_LINE)
def test_an_allowed_call_that_no_line_can_hold_has_no_effect(
    tmp_path: Path, args: str, held: dict[str, object]
) -> None:
    """The effect must not occur where the audit cannot record it in full.
    The decision is allow, so the answer is row 12: `internal_error`."""
    write_grants(grants_dir(tmp_path), make_grants())
    pool = FakePool()
    client = build(tmp_path, pool=pool)

    reply = call(client, body_of(SEARCH, args))

    assert reply.status_code == 500
    assert reply.json()["reason"] == "internal_error"
    assert pool.calls == []
    [record] = family_lines(tmp_path)
    assert (record["decision"], record["reason"]) == ("deny", "internal_error")
    assert record["args"] == held


def test_a_verb_with_a_string_that_is_not_text_reaches_no_service(tmp_path: Path) -> None:
    write_grants(grants_dir(tmp_path), make_grants())
    requests: list[httpx.Request] = []
    client = build(tmp_path, requests=requests)

    reply = call(client, body_of("embed", f'{{"input":"{LONE}"}}'))

    assert reply.status_code == 500
    assert reply.json()["reason"] == "internal_error"
    assert requests == []
    [record] = family_lines(tmp_path)
    assert (record["tool"], record["decision"]) == ("embed", "deny")
    assert record["args"] == UNRECORDABLE_ARGS


def test_a_gated_call_that_no_line_can_hold_opens_no_gate(tmp_path: Path) -> None:
    write_grants(grants_dir(tmp_path), make_grants(approval=[SEARCH]))
    rail = Notified()
    pool = FakePool()
    client = build(tmp_path, pool=pool, gatekeeper=Gatekeeper(rail))

    reply = call(client, body_of(SEARCH, f'{{"query":"{LONE}"}}'))

    assert reply.status_code == 500
    assert rail.notices == []
    assert pool.calls == []
    assert [record["decision"] for record in family_lines(tmp_path)] == ["deny"]


def test_a_detail_that_is_not_text_still_reaches_the_caller(tmp_path: Path) -> None:
    """The schema of a verb names the argument that it refuses. The name is
    text of the caller, and the reply must carry it."""
    write_grants(grants_dir(tmp_path), make_grants())
    client = build(tmp_path)

    reply = call(client, body_of("embed", f'{{"input":"x","{LONE}":1}}'))

    assert reply.status_code == 400
    assert reply.json()["reason"] == "arg_validation"
    assert "\\ud800 is not a known argument" in reply.json()["detail"]
    assert [record["reason"] for record in family_lines(tmp_path)] == ["arg_validation"]


# ---- a request that names no family -------------------------------------------


def test_a_request_of_no_family_gets_its_line_for_any_arguments(tmp_path: Path) -> None:
    """The line holds the size and the digest of the arguments. A string
    that is not text counts as the bytes that it has "as it is"."""
    write_grants(grants_dir(tmp_path), make_grants())
    client = build(tmp_path)
    args = f'{{"query":"{LONE}"}}'

    reply = call(client, body_of(SEARCH, args), token="nobody")

    assert reply.status_code == 403
    assert reply.json()["reason"] == "unknown_token"
    assert family_lines(tmp_path) == []
    raw = '{"query": "\ud800"}'.encode("utf-8", "surrogatepass")
    [record] = unidentified_lines(tmp_path)
    assert record["reason"] == "unknown_token"
    assert record["args_bytes"] == len(raw)
    assert record["args_sha256"] == hashlib.sha256(raw).hexdigest()


# ---- the result of an upstream -----------------------------------------------


def test_a_result_that_no_reply_can_carry_is_an_upstream_failure(tmp_path: Path) -> None:
    """A result is bytes of another process. One that is not JSON text is §5
    row 11, and the line says so before the answer goes out."""

    class Answers(FakePool):
        async def call(self, server: str, tool: str, args: dict[str, object]) -> str:
            return "\ud800"

    write_grants(grants_dir(tmp_path), make_grants())
    client = build(tmp_path, pool=Answers())

    reply = call(client, body_of(SEARCH, '{"query":"x"}'))

    assert reply.status_code == 502
    assert reply.json()["reason"] == "upstream_failed"
    [record] = family_lines(tmp_path)
    assert (record["decision"], record["reason"]) == ("allow", "upstream_failed")


# ---- a gate that ends by a failure -------------------------------------------


class BrokenHold(Gatekeeper):
    """A gate whose wait raises a failure that no handler names."""

    async def hold(self, ticket: GateTicket, reach: ReachCheck) -> GateHeld:
        raise RuntimeError("a failure nobody predicted")


def test_a_hold_that_raises_writes_the_second_record_and_denies(tmp_path: Path) -> None:
    """§6.4: a gated call writes two records. The second one says why the
    gate ended, also when the wait itself failed."""
    write_grants(grants_dir(tmp_path), make_grants(approval=[SEARCH]))
    pool = FakePool()
    client = build(tmp_path, pool=pool, gatekeeper=BrokenHold(Notified()))

    reply = call(client, body_of(SEARCH, '{"query":"x"}'))

    assert reply.status_code == 500
    assert reply.json() == INTERNAL_ERROR
    assert pool.calls == []
    gate = gate_id("chat", SEARCH, {"query": "x"})
    records = family_lines(tmp_path)
    assert [(r["decision"], r["reason"], r["gate"]) for r in records] == [
        ("pending", "approval_required", gate),
        ("deny", "internal_error", gate),
    ]


# ---- a failure that no layer names --------------------------------------------


def test_a_call_that_raises_anything_writes_one_line_and_denies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Row 12 of §5, for a failure outside the decision and the execution."""
    write_grants(grants_dir(tmp_path), make_grants())
    pool = FakePool()
    client = build(tmp_path, pool=pool)

    def explode(*_args: object, **_kwargs: object) -> bool:
        raise RuntimeError("a failure nobody predicted")

    monkeypatch.setattr(family_app, "can_hold", explode)
    reply = call(client, body_of(SEARCH, '{"query":"x"}'))

    assert reply.status_code == 500
    assert reply.json() == INTERNAL_ERROR
    assert pool.calls == []
    [record] = family_lines(tmp_path)
    assert (record["decision"], record["reason"]) == ("deny", "internal_error")
    assert record["args"] == {"query": "x"}
