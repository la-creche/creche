"""Audit truncation end to end, `/manifest` sharing the rate
window, and the global bucket for calls that resolve to no family.

The body cap itself and the shape of an unidentified record live in
`test_pep_s8p_one_path.py`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import httpx
from agent_pep.app import PepConfig, create_app
from agent_pep.family_audit import AUDIT_ARG_STRING_MAX_CHARS, AUDIT_TRUNCATION_MARKER
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pep_family_helpers import FAMILY_TOKEN, make_grants, write_grants
from pep_helpers import FakePool

HTTP_OK = 200
HTTP_RATE_LIMITED = 429


def build(tmp_path: Path, **grant_over: object) -> tuple[TestClient, Path, FastAPI]:
    write_grants(tmp_path / "rework" / "grants", make_grants(**grant_over))

    def handle(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith("/embed"):
            return httpx.Response(HTTP_OK, json=[[0.1] * 768])
        if str(request.url).endswith("/info"):
            return httpx.Response(HTTP_OK, json={"model_id": "m"})
        return httpx.Response(404)

    app = create_app(
        PepConfig(
            audit_dir=tmp_path / "audit",
            rework_dir=tmp_path / "rework",
            tei_url="http://tei.invalid:8085",
        ),
        pool=FakePool(),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
    )
    client = TestClient(app)
    client.__enter__()
    return client, tmp_path, app


def family_records(root: Path) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for path in sorted((root / "rework" / "audit").glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            records.append(json.loads(line))
    return records


def unidentified_records(root: Path) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for path in sorted((root / "audit").glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            records.append(json.loads(line))
    return records


def auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {FAMILY_TOKEN}"}


def test_granted_args_are_length_capped_not_dropped(tmp_path: Path) -> None:
    client, root, _ = build(tmp_path)
    long_text = "x" * (AUDIT_ARG_STRING_MAX_CHARS + 500)
    call: dict[str, object] = {"tool": "embed", "args": {"input": long_text}}
    resp = client.post("/call", headers=auth(), json=call)

    assert resp.status_code == HTTP_OK
    record = family_records(root)[-1]
    args = cast("dict[str, object]", record["args"])
    audited = cast("str", args["input"])
    assert audited.endswith(AUDIT_TRUNCATION_MARKER)
    assert len(audited) == AUDIT_ARG_STRING_MAX_CHARS + len(AUDIT_TRUNCATION_MARKER)


def test_manifest_shares_the_family_rate_window(tmp_path: Path) -> None:
    client, _root, _app = build(
        tmp_path, limits={"pep_rpm": 1, "max_inflight_delegations": 2, "max_open_gates": 10}
    )
    first = client.get("/manifest", headers=auth())
    # A second fetch of the SAME revision would answer 304, which skips the
    # audit but not the window. Change the revalidator so the window is what
    # answers, not the cache.
    second = client.get("/manifest", headers={**auth(), "If-None-Match": '"nope"'})

    assert first.status_code == HTTP_OK
    assert second.status_code == HTTP_RATE_LIMITED
    assert second.json()["reason"] == "rate_limited"


def test_global_bucket_bounds_unidentified_denials(tmp_path: Path) -> None:
    client, root, app = build(tmp_path)
    bucket = app.state.unauth_bucket
    while bucket.take():  # drain it directly rather than looping hundreds of calls
        pass

    resp = client.post("/call", json={"tool": "embed", "args": {}})

    assert resp.status_code == HTTP_RATE_LIMITED
    assert resp.json()["reason"] == "rate_limited"
    assert unidentified_records(root)[-1]["reason"] == "rate_limited"
