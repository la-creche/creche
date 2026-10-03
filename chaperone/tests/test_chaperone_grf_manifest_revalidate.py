"""Contract 04 §4.2: revalidating a manifest the sandbox already holds.

A sandbox now asks for the manifest for the life of its pi process, not once
(contract 03 §7.3 rule 4), because a grant the operator ADDS must reach the chat
they are in. The poll has to be cheap enough to run forever, so the PEP answers 304
to a request that names the revision it is already serving.

`If-None-Match` is RFC 9110 §13.1.2. Nothing else about the route moves: the
rate window still counts the request, and a 200 is the same 200 it was.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
from chaperone.app import PepConfig, create_app
from chaperone.family_app import MANIFEST_ACTION
from chaperone.mcp_client import UpstreamSpec
from chaperone_family_helpers import FAMILY_TOKEN, make_grants, write_grants
from chaperone_helpers import FakePool
from fastapi.testclient import TestClient

HTTP_OK = 200
HTTP_NOT_MODIFIED = 304
HTTP_TOO_MANY = 429

KAGI = UpstreamSpec(name="kagi", command="x", args=(), env={})


def build(tmp_path: Path) -> TestClient:
    """The family path alone, with no upstream this test needs to reach."""
    pool = FakePool()

    def handle(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"no test here calls an upstream: {request.url}")

    cfg = PepConfig(
        audit_dir=tmp_path / "audit",
        rework_dir=tmp_path / "rework",
        upstreams={"kagi": KAGI},
    )
    app = create_app(
        cfg, pool=pool, http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle))
    )
    client = TestClient(app)
    client.__enter__()  # run lifespan
    return client


def grants_dir(tmp_path: Path) -> Path:
    return tmp_path / "rework" / "grants"


def family_auth(token: str = FAMILY_TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def manifest_records(tmp_path: Path) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for path in sorted((tmp_path / "rework" / "audit").glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            record = json.loads(line)
            if record["tool"] == MANIFEST_ACTION:
                records.append(record)
    return records


def test_the_revision_it_serves_answers_not_modified(tmp_path: Path) -> None:
    grants = make_grants()
    write_grants(grants_dir(tmp_path), grants)
    client = build(tmp_path)

    answer = client.get("/manifest", headers={**family_auth(), "If-None-Match": f'"{grants.rev}"'})

    assert answer.status_code == HTTP_NOT_MODIFIED
    assert answer.content == b""


def test_a_moved_revision_serves_the_whole_manifest(tmp_path: Path) -> None:
    write_grants(grants_dir(tmp_path), make_grants(rev="rev-the-sandbox-has-not-seen"))
    client = build(tmp_path)

    answer = client.get("/manifest", headers={**family_auth(), "If-None-Match": '"rev-old"'})

    assert answer.status_code == HTTP_OK
    assert answer.json()["rev"] == "rev-the-sandbox-has-not-seen"
    assert "kagi__kagi_search_fetch" in [t["name"] for t in answer.json()["tools"]]


def test_a_weak_tag_and_a_bare_value_both_match(tmp_path: Path) -> None:
    # RFC 9110 §8.8.3: a weak comparison is what a revision string deserves,
    # and a bridge that sends the rev unquoted still means the same thing.
    grants = make_grants()
    write_grants(grants_dir(tmp_path), grants)
    client = build(tmp_path)

    for value in (f'W/"{grants.rev}"', grants.rev):
        answer = client.get("/manifest", headers={**family_auth(), "If-None-Match": value})
        assert answer.status_code == HTTP_NOT_MODIFIED, value


def test_a_star_never_matches(tmp_path: Path) -> None:
    # `*` means "any current representation" in RFC 9110 and would turn every
    # poll into a 304, so a sandbox would never learn a grant moved. The route
    # compares one revision and nothing else.
    write_grants(grants_dir(tmp_path), make_grants())
    client = build(tmp_path)

    answer = client.get("/manifest", headers={**family_auth(), "If-None-Match": "*"})

    assert answer.status_code == HTTP_OK


def test_nothing_is_audited_when_nothing_was_served(tmp_path: Path) -> None:
    # One line a minute per pi process would bury the log §6 exists to make
    # readable, and a `$manifest` allow would claim a manifest the family
    # never received.
    grants = make_grants()
    write_grants(grants_dir(tmp_path), grants)
    client = build(tmp_path)

    client.get("/manifest", headers=family_auth())
    before = len(manifest_records(tmp_path))
    client.get("/manifest", headers={**family_auth(), "If-None-Match": f'"{grants.rev}"'})

    assert before == 1
    assert len(manifest_records(tmp_path)) == 1


def test_the_rate_window_still_counts_a_revalidation(tmp_path: Path) -> None:
    # The bound on the poll, and the reason it needs no new one: a
    # revalidation costs the family's window exactly what a fetch costs.
    grants = make_grants(limits={"pep_rpm": 2, "max_inflight_delegations": 2, "max_open_gates": 1})
    write_grants(grants_dir(tmp_path), grants)
    client = build(tmp_path)
    conditional = {**family_auth(), "If-None-Match": f'"{grants.rev}"'}

    first = client.get("/manifest", headers=conditional)
    second = client.get("/manifest", headers=conditional)
    third = client.get("/manifest", headers=conditional)

    assert [first.status_code, second.status_code] == [HTTP_NOT_MODIFIED, HTTP_NOT_MODIFIED]
    assert third.status_code == HTTP_TOO_MANY


def test_an_oversize_header_is_not_a_match(tmp_path: Path) -> None:
    # Untrusted input with a bound (invariant 12). A header longer than any
    # revision can be is refused before it is compared.
    grants = make_grants()
    write_grants(grants_dir(tmp_path), grants)
    client = build(tmp_path)

    answer = client.get(
        "/manifest", headers={**family_auth(), "If-None-Match": '"' + "x" * 4096 + '"'}
    )

    assert answer.status_code == HTTP_OK
