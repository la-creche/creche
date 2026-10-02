"""The process, not the decision: the lifespan, the routes FastAPI adds for
free, the audit file mode, and what a broken audit does to an allowed call.

Every per-call rule lives in `test_family_app.py` and the modules beside it.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from agent_pep.app import PepConfig, create_app
from agent_pep.audit import AuditError, AuditLog
from agent_pep.mcp_client import UpstreamSpec
from fastapi.testclient import TestClient
from pep_family_helpers import FAMILY_TOKEN, make_grants, write_grants
from pep_helpers import FakePool

HTTP_OK = 200
HTTP_NOT_FOUND = 404
HTTP_INTERNAL = 500

AUDIT_FILE_MODE = 0o640

#: A server this PEP has loaded. One the family is granted but the PEP does
#: not hold answers 501 instead, which is a different rule.
KAGI = UpstreamSpec(name="kagi", command="x", args=(), env={})


def build(tmp_path: Path, pool: FakePool | None = None) -> tuple[TestClient, FakePool]:
    pool = pool or FakePool()
    write_grants(tmp_path / "rework" / "grants", make_grants())

    def handle(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith("/embed"):
            return httpx.Response(HTTP_OK, json=[[0.1] * 768])
        if str(request.url).endswith("/info"):
            return httpx.Response(HTTP_OK, json={"model_id": "BAAI/bge-base-en-v1.5"})
        return httpx.Response(HTTP_NOT_FOUND, text="nope")

    app = create_app(
        PepConfig(
            audit_dir=tmp_path / "audit",
            rework_dir=tmp_path / "rework",
            upstreams={"kagi": KAGI},
        ),
        pool=pool,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
    )
    client = TestClient(app)
    client.__enter__()  # run lifespan
    return client, pool


def auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {FAMILY_TOKEN}"}


def test_lifespan_sweeps_both_logs_on_startup(tmp_path: Path) -> None:
    """One sweep list, two directories: contract 04 §6's audit and the log
    for a request that resolved to no family."""
    stale: list[Path] = []
    for directory in (tmp_path / "audit", tmp_path / "rework" / "audit"):
        directory.mkdir(parents=True)
        old = directory / "2000-01-01.jsonl"
        old.write_text("{}\n", encoding="utf-8")
        stale.append(old)

    build(tmp_path)  # entering the TestClient runs the lifespan

    assert [one.exists() for one in stale] == [False, False]


def test_no_interactive_docs(tmp_path: Path) -> None:
    client, _ = build(tmp_path)
    assert client.get("/docs").status_code == HTTP_NOT_FOUND
    assert client.get("/redoc").status_code == HTTP_NOT_FOUND
    assert client.get("/openapi.json").status_code == HTTP_NOT_FOUND


def test_unknown_body_field_rejected(tmp_path: Path) -> None:
    client, _ = build(tmp_path)
    resp = client.post("/call", headers=auth(), json={"tool": "embed", "args": {}, "extra": "nope"})
    assert resp.status_code == 422


def test_audit_failure_blocks_allowed_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = build(tmp_path)

    def broken_write(self: AuditLog, record: dict[str, object]) -> None:
        raise AuditError("disk full")

    monkeypatch.setattr(AuditLog, "write", broken_write)
    resp = client.post("/call", headers=auth(), json={"tool": "embed", "args": {"input": "x"}})

    assert resp.status_code == HTTP_INTERNAL
    assert resp.json()["detail"] == "audit unavailable"


def test_unwritable_audit_blocks_execution_before_it_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, pool = build(tmp_path)

    def unwritable(self: AuditLog) -> bool:
        return False

    monkeypatch.setattr(AuditLog, "probe_writable", unwritable)

    resp = client.post(
        "/call", headers=auth(), json={"tool": "kagi__kagi_search_fetch", "args": {"query": "x"}}
    )

    assert resp.status_code == HTTP_INTERNAL
    assert resp.json()["detail"] == "audit unavailable"
    assert pool.calls == []  # the effect never ran


def test_audit_files_are_group_readable_not_world(tmp_path: Path) -> None:
    client, _ = build(tmp_path)
    client.get("/manifest")  # no bearer -> denied -> recorded as unidentified

    files = list((tmp_path / "audit").glob("*.jsonl"))
    assert files
    mode = files[0].stat().st_mode & 0o777
    assert mode == AUDIT_FILE_MODE, oct(mode)


def test_the_family_audit_files_carry_the_same_mode(tmp_path: Path) -> None:
    client, _ = build(tmp_path)
    assert client.get("/manifest", headers=auth()).status_code == HTTP_OK

    files = list((tmp_path / "rework" / "audit").glob("*.jsonl"))
    assert files
    mode = files[0].stat().st_mode & 0o777
    assert mode == AUDIT_FILE_MODE, oct(mode)
