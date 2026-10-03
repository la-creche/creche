"""One process, one identity kind.

No instance token is minted and no instance calls the PEP. This module pins
that, so a future editor cannot reintroduce a second identity kind by
accident.

The family path's own behaviour is pinned by `test_family_app.py` and the
rest of the suite. What is here is the ABSENCE of a second path, plus two
rules that belong to no path: the request-body cap and the log that records
a request nobody could identify.
"""

from __future__ import annotations

import importlib
import json
from dataclasses import fields
from pathlib import Path

import httpx
import pytest
from chaperone.app import MAX_REQUEST_BODY_BYTES, PepConfig, create_app
from chaperone_family_helpers import FAMILY_TOKEN, make_grants, write_grants
from chaperone_helpers import ECHO_SCHEMA, FakePool
from fastapi.testclient import TestClient

from chaperone import mcp_client

HTTP_OK = 200
HTTP_FORBIDDEN = 403
HTTP_NOT_FOUND = 404
HTTP_TOO_LARGE = 413

#: The instance path's modules. None of them may exist.
GONE_MODULES = (
    "chaperone.instances",
    "chaperone.decisions",
    "chaperone.approvals",
    "chaperone.jobs",
)

#: `PepConfig` fields that configured the instance path. A unit that still
#: sets their environment variables is not broken by their absence — nothing
#: reads them — but the dataclass must not accept them again.
GONE_CONFIG_FIELDS = (
    "instances_dir",
    "approvals_dir",
    "jobs_dir",
    "routes_dir",
    "daemon_socket",
    "catalog_token",
)

KAGI = mcp_client.UpstreamSpec(name="kagi", command="x", args=(), env={})


def build(tmp_path: Path, *, pool: FakePool | None = None) -> TestClient:
    """The app with a rework directory and nothing else."""

    def handle(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(HTTP_NOT_FOUND, text="nope")

    app = create_app(
        PepConfig(
            audit_dir=tmp_path / "audit",
            rework_dir=tmp_path / "rework",
            upstreams={"kagi": KAGI},
        ),
        pool=pool or FakePool(),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
    )
    client = TestClient(app)
    client.__enter__()  # run lifespan
    return client


def unidentified_lines(tmp_path: Path) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for path in sorted((tmp_path / "audit").glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            records.append(json.loads(line))
    return records


def family_auth(token: str = FAMILY_TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.parametrize("name", GONE_MODULES)
def test_the_instance_modules_are_gone(name: str) -> None:
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module(name)


def test_the_package_exports_no_instance_names() -> None:
    package = importlib.import_module("chaperone")
    assert "decide" not in dir(package)
    assert "InstanceStore" not in dir(package)


@pytest.mark.parametrize("name", GONE_CONFIG_FIELDS)
def test_pepconfig_carries_no_instance_field(name: str) -> None:
    assert name not in {one.name for one in fields(PepConfig)}


def test_the_rework_directory_is_required() -> None:
    """A PEP built without it would serve nothing at all."""
    with pytest.raises(TypeError):
        PepConfig(audit_dir=Path("/nonexistent"))  # pyright: ignore[reportCallIssue]


def test_an_unknown_bearer_is_refused_on_call(tmp_path: Path) -> None:
    client = build(tmp_path)
    answer = client.post(
        "/call",
        json={"tool": "embed", "args": {"input": "hi"}},
        headers={"Authorization": "Bearer not-a-family-token"},
    )

    assert answer.status_code == HTTP_FORBIDDEN
    assert answer.json()["reason"] == "unknown_token"


def test_an_unknown_bearer_is_refused_on_manifest(tmp_path: Path) -> None:
    client = build(tmp_path)
    answer = client.get("/manifest", headers={"Authorization": "Bearer not-a-family-token"})

    assert answer.status_code == HTTP_FORBIDDEN
    assert answer.json()["reason"] == "unknown_token"


def test_the_catalog_endpoint_is_gone(tmp_path: Path) -> None:
    """The registry's own `mcp/<name>/server.yaml` answers "what COULD be
    granted" (contract 01b §5), so the PEP serves no `/catalog`."""
    client = build(tmp_path)

    assert client.get("/catalog").status_code == HTTP_NOT_FOUND


def test_healthz_still_needs_no_auth(tmp_path: Path) -> None:
    """`roster` and `upstreams` are additive: `ok` and the status code do
    not depend on them. A pool that does not reload counts nothing, so
    `upstreams` is null."""
    client = build(tmp_path)
    answer = client.get("/healthz")

    assert answer.status_code == HTTP_OK
    assert answer.json() == {
        "ok": True,
        "roster": {"state": "off", "since": None},
        "upstreams": None,
    }


def test_an_unidentified_call_names_no_instance(tmp_path: Path) -> None:
    """The record a request nobody could identify writes. It carries the size
    and the digest of the arguments, never their content, and it carries no
    `instance` or `agent` key — a line with one was written by the retired
    instance path."""
    client = build(tmp_path)
    client.post(
        "/call",
        json={"tool": "embed", "args": {"input": "hi"}},
        headers={"Authorization": "Bearer not-a-family-token"},
    )

    lines = unidentified_lines(tmp_path)
    assert len(lines) == 1
    record = lines[0]
    assert record["tool"] == "embed"
    assert record["reason"] == "unknown_token"
    assert record["decision"] == "deny"
    assert isinstance(record["args_sha256"], str)
    assert "args" not in record
    assert "instance" not in record
    assert "agent" not in record


def test_an_oversized_body_is_still_capped(tmp_path: Path) -> None:
    client = build(tmp_path)
    answer = client.post(
        "/call",
        content=b"x" * (MAX_REQUEST_BODY_BYTES + 1),
        headers={"content-type": "application/json", **family_auth()},
    )

    assert answer.status_code == HTTP_TOO_LARGE
    assert answer.json()["reason"] == "body_too_large"
    assert unidentified_lines(tmp_path)[0]["tool"] == "$oversized"


def test_a_family_bearer_still_gets_its_manifest_and_call(tmp_path: Path) -> None:
    """The one path works end to end: the same fixtures the family suite
    uses, driven through the app."""
    pool = FakePool()
    client = build(tmp_path, pool=pool)
    write_grants(tmp_path / "rework" / "grants", make_grants())

    manifest = client.get("/manifest", headers=family_auth())
    assert manifest.status_code == HTTP_OK
    assert manifest.json()["family"] == "chat"
    names = {one["name"] for one in manifest.json()["tools"]}
    assert "kagi__kagi_search_fetch" in names

    answer = client.post(
        "/call",
        json={"tool": "kagi__kagi_search_fetch", "args": {"query": "hi"}},
        headers=family_auth(),
    )
    assert answer.status_code == HTTP_OK
    assert pool.calls == [("kagi", "kagi_search_fetch", {"query": "hi"})]
    # The family's own log, not the unidentified one.
    assert (tmp_path / "rework" / "audit").exists()
    assert unidentified_lines(tmp_path) == []


def test_the_manifest_schema_fixture_is_still_served(tmp_path: Path) -> None:
    """A guard on the helper this module shares with the family suite: the
    MCP double must keep serving a schema, or the assertions above pass for
    the wrong reason."""
    pool = FakePool()
    assert pool.tools("kagi")["kagi_search_fetch"].input_schema == ECHO_SCHEMA
