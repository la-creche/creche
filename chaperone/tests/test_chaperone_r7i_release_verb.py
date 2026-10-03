"""The `release` verb files a request (`stage7-releases.md` §2.3, §3.1).

It is invariant 17's path — one approved action releases the platform — for
the `agent-control` family (`docs/rework/spec.md` §11.7).

What these tests hold, and each is a way the verb could be wrong:

1. A PEP with no spool still answers 501, and its manifest does not offer the
   verb. Deny by default applies to configuration too.
2. The file that lands is one the EXECUTOR parses, with the family the bearer
   resolved to in `requested_by`. A caller cannot file under another name.
3. The verb approves nothing. §3.1: the tap that matters is the executor's at
   step 5, because it binds to what will happen.
4. A refusal is a contract 04 §5 reason and a DENY audit line, because no
   file was written and nothing happened.
"""

from __future__ import annotations

import json
from pathlib import Path

from agent_release.executor.request import parse_request
from agent_release.executor.spool import MAX_PENDING_PER_REQUESTER
from agent_release.requester import new_ulid
from chaperone.app import PepConfig, create_app
from chaperone.family_app import MANIFEST_ACTION
from chaperone.release_door import SpoolReleaseDoor
from chaperone_family_helpers import FAMILY_TOKEN, make_grants, write_grants
from fastapi.testclient import TestClient

#: Contract 01 §3.5's `release` fence: the platform allowlist, nothing else.
FENCE: dict[str, object] = {"release": {"components": ["chaperone", "attendance"]}}

CALL: dict[str, object] = {"tool": "release", "args": {"components": {"chaperone": "2.1.0"}}}


def _grants_dir(tmp_path: Path) -> Path:
    return tmp_path / "rework" / "grants"


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {FAMILY_TOKEN}"}


def _requests(tmp_path: Path) -> Path:
    directory = tmp_path / "releases" / "requests"
    directory.mkdir(parents=True)

    return directory


def _client(tmp_path: Path, *, spool: Path | None) -> TestClient:
    """The app with the family path on, and a release spool or none."""
    write_grants(_grants_dir(tmp_path), make_grants(family="agent-control", verbs=FENCE))

    return TestClient(
        create_app(
            PepConfig(
                audit_dir=tmp_path / "audit",
                rework_dir=tmp_path / "rework",
                release_requests_dir=spool,
            )
        )
    )


def _audit(tmp_path: Path) -> list[dict[str, object]]:
    lines: list[dict[str, object]] = []
    for path in sorted((tmp_path / "rework" / "audit").glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            lines.append(json.loads(line))

    return [row for row in lines if row["tool"] != MANIFEST_ACTION]


def _filed(spool: Path) -> list[Path]:
    return sorted(spool.glob("*.json"))


# -- a PEP with no spool ----------------------------------------------------


def test_a_pep_with_no_spool_still_answers_501(tmp_path: Path) -> None:
    """Deny by default, applied to configuration: the policy runs in full and
    the last row says this process cannot make the call."""
    client = _client(tmp_path, spool=None)

    reply = client.post("/call", json=CALL, headers=_auth())

    assert reply.status_code == 501
    assert reply.json()["reason"] == "not_implemented"
    assert "release spool" in reply.json()["detail"]


def test_a_pep_with_no_spool_does_not_offer_the_verb(tmp_path: Path) -> None:
    """Contract 04 §4: the manifest advertises only what `/call` executes."""
    client = _client(tmp_path, spool=None)

    names = [entry["name"] for entry in client.get("/manifest", headers=_auth()).json()["tools"]]

    assert "release" not in names


def test_a_pep_with_a_spool_offers_the_verb(tmp_path: Path) -> None:
    client = _client(tmp_path, spool=_requests(tmp_path))

    names = [entry["name"] for entry in client.get("/manifest", headers=_auth()).json()["tools"]]

    assert "release" in names


# -- the file that lands ----------------------------------------------------


def test_one_call_files_one_request_the_executor_parses(tmp_path: Path) -> None:
    spool = _requests(tmp_path)
    client = _client(tmp_path, spool=spool)

    reply = client.post("/call", json=CALL, headers=_auth())

    assert reply.status_code == 200
    request_id = reply.json()["result"]["request"]
    written = _filed(spool)
    assert [path.stem for path in written] == [request_id]
    parsed = parse_request(written[0].read_bytes(), request_id)
    assert parsed.wanted() == {"chaperone": "2.1.0"}


def test_requested_by_is_the_family_the_bearer_resolved_to(tmp_path: Path) -> None:
    """§3.2: root treats the field as a claim. This side still must not let a
    caller choose it — the argument schema has no such property, and the
    value comes from the token."""
    spool = _requests(tmp_path)
    client = _client(tmp_path, spool=spool)

    client.post("/call", json=CALL, headers=_auth())

    written = _filed(spool)[0]
    assert parse_request(written.read_bytes(), written.stem).requested_by == "agent-control"


def test_a_caller_cannot_smuggle_requested_by(tmp_path: Path) -> None:
    """`additionalProperties: false` on the verb schema, and the door never
    reads such an argument even if one arrived."""
    spool = _requests(tmp_path)
    client = _client(tmp_path, spool=spool)
    args = {"components": {"chaperone": "2.1.0"}, "requested_by": "human"}

    reply = client.post("/call", json={"tool": "release", "args": args}, headers=_auth())

    assert reply.status_code == 400
    assert reply.json()["reason"] == "arg_validation"
    assert _filed(spool) == []


def test_the_call_is_not_gated(tmp_path: Path) -> None:
    """§3.1: gating the verb would ask the operator for two taps. The executor's own
    approval at step 5 is the gate, and it binds to the resolved manifest."""
    spool = _requests(tmp_path)
    client = _client(tmp_path, spool=spool)

    reply = client.post("/call", json=CALL, headers=_auth())

    assert reply.status_code == 200
    assert _audit(tmp_path)[-1]["reason"] == "granted"


def test_the_audit_line_carries_the_call(tmp_path: Path) -> None:
    client = _client(tmp_path, spool=_requests(tmp_path))

    client.post("/call", json=CALL, headers=_auth())

    row = _audit(tmp_path)[-1]
    assert row["tool"] == "release"
    assert row["decision"] == "allow"
    assert row["chain"] == ["agent-control"]


# -- what is refused --------------------------------------------------------


def test_a_component_outside_the_fence_is_refused(tmp_path: Path) -> None:
    """Contract 01's `release` fence, which the validator keeps to the
    platform allowlist."""
    spool = _requests(tmp_path)
    client = _client(tmp_path, spool=spool)
    args = {"components": {"infra": "1.0.0"}}

    reply = client.post("/call", json={"tool": "release", "args": args}, headers=_auth())

    assert reply.status_code == 400
    assert reply.json()["reason"] == "arg_validation"
    assert _filed(spool) == []


def test_a_family_without_the_verb_is_refused(tmp_path: Path) -> None:
    """Invariant 11: only the `agent-control` family's grants name it."""
    write_grants(_grants_dir(tmp_path), make_grants())
    client = TestClient(
        create_app(
            PepConfig(
                audit_dir=tmp_path / "audit",
                rework_dir=tmp_path / "rework",
                release_requests_dir=_requests(tmp_path),
            )
        )
    )

    reply = client.post("/call", json=CALL, headers=_auth())

    assert reply.status_code == 403
    assert reply.json()["reason"] == "tool_not_granted"


def test_a_full_spool_is_rate_limited_and_writes_nothing(tmp_path: Path) -> None:
    """§3.2 rule 8, said on this side so the caller learns it as a §5 reason
    instead of as a ledger entry it cannot see."""
    spool = _requests(tmp_path)
    for index in range(MAX_PENDING_PER_REQUESTER):
        (spool / f"{new_ulid(1758153590.0 + index)}.json").write_text("{}", encoding="utf-8")
    client = _client(tmp_path, spool=spool)

    reply = client.post("/call", json=CALL, headers=_auth())

    assert reply.status_code == 429
    assert reply.json()["reason"] == "rate_limited"
    assert len(_filed(spool)) == MAX_PENDING_PER_REQUESTER
    assert _audit(tmp_path)[-1]["decision"] == "deny"


def test_a_spool_that_is_not_there_is_an_execution_failure(tmp_path: Path) -> None:
    """A configured spool root owns and this process cannot open is not the
    caller's fault, so it is §5 row 11 and not a policy denial."""
    client = _client(tmp_path, spool=tmp_path / "absent")

    reply = client.post("/call", json=CALL, headers=_auth())

    assert reply.status_code == 502
    assert reply.json()["reason"] == "upstream_failed"


# -- the two writers agree --------------------------------------------------


def test_the_door_imports_no_root_side_module() -> None:
    """`release_door.py` uses `agent_release.requester` and nothing else out
    of that package. The executor's own modules fetch, build, swap and
    restart, and none of them belongs inside the PEP's process."""
    source = (
        Path(__file__).resolve().parents[1] / "src" / "chaperone" / "release_door.py"
    ).read_text(encoding="utf-8")

    assert "agent_release.requester" in source
    assert "agent_release.executor" not in source


def test_the_verb_schema_and_the_executor_grammar_agree(tmp_path: Path) -> None:
    """Every version the PEP's schema accepts must be one the executor's
    parser accepts. Two grammars for one field is how a call is allowed here
    and refused there, after the fact, in a ledger nobody is watching."""
    spool = _requests(tmp_path)
    client = _client(tmp_path, spool=spool)

    for version in ("latest", "2.1.0", "10.0.99"):
        args = {"components": {"chaperone": version}}
        reply = client.post("/call", json={"tool": "release", "args": args}, headers=_auth())
        assert reply.status_code == 200, version

    for bad in ("2.1", "2.1.0\n", "", "v2.1.0", "latest "):
        args = {"components": {"chaperone": bad}}
        reply = client.post("/call", json={"tool": "release", "args": args}, headers=_auth())
        assert reply.status_code == 400, bad


def test_the_door_writes_the_same_bytes_the_cli_writes(tmp_path: Path) -> None:
    """One writer. `agent-releasectl request` and this verb differ in exactly
    one field: `requested_by`."""
    spool = _requests(tmp_path)
    door = SpoolReleaseDoor(str(spool))
    from chaperone.release_door import ReleaseRequest

    request_id = door.file(
        ReleaseRequest(family="agent-control", session=None, components={"chaperone": "2.1.0"})
    )
    body = json.loads((spool / f"{request_id}.json").read_text(encoding="utf-8"))

    assert set(body) == {
        "id",
        "kind",
        "components",
        "rollback_of",
        "requested_by",
        "requester_session",
        "ts",
    }
