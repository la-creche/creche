"""The line of a request that names no family.

The chaperone appends one line to the log of `PEP_AUDIT_DIR` for such a
request. The line says that no family matched: its second key is `family`,
and the value of that key is null. This module holds the keys of a line and
their order.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from chaperone.app import MAX_REQUEST_BODY_BYTES, PepConfig, create_app
from chaperone_helpers import FakePool
from fastapi import FastAPI
from fastapi.testclient import TestClient

HTTP_FORBIDDEN = 403
HTTP_TOO_LARGE = 413
HTTP_TOO_MANY = 429

JSON_HEADERS = {"content-type": "application/json"}

#: The keys of the line of a refused request, in the order of the line.
REFUSAL_KEYS = (
    "ts",
    "family",
    "tool",
    "decision",
    "reason",
    "detail",
    "args_bytes",
    "args_sha256",
)

#: The keys of the line of a body over the cap. The chaperone reads no
#: argument of such a body, so the line holds no digest.
LARGE_BODY_KEYS = REFUSAL_KEYS[:-1]

#: How each line starts after its time.
NO_FAMILY = '"family": null, "tool": '


def build(tmp_path: Path) -> tuple[TestClient, FastAPI]:
    app = create_app(
        PepConfig(audit_dir=tmp_path / "audit", rework_dir=tmp_path / "rework"),
        pool=FakePool(),
    )
    client = TestClient(app)
    client.__enter__()  # run lifespan

    return client, app


def the_one_line(tmp_path: Path) -> str:
    """The text of the one line of the log, with no line end."""
    (path,) = sorted((tmp_path / "audit").glob("*.jsonl"))
    (line,) = path.read_text(encoding="utf-8").splitlines()

    return line


def keys_of(line: str) -> tuple[str, ...]:
    record: dict[str, object] = json.loads(line)

    return tuple(record)


def after_the_time(line: str) -> str:
    """The text of a line after the value of `ts` and its comma."""
    record: dict[str, object] = json.loads(line)
    start = f'{{"ts": "{record["ts"]}", '
    assert line.startswith(start), line

    return line.removeprefix(start)


@pytest.mark.parametrize(
    "headers",
    [{}, {"Authorization": "Bearer not-a-family-token"}],
    ids=["no-bearer", "unknown-bearer"],
)
def test_a_refused_call_says_that_no_family_matched(
    tmp_path: Path, headers: dict[str, str]
) -> None:
    client, _ = build(tmp_path)
    answer = client.post("/call", json={"tool": "embed", "args": {"input": "hi"}}, headers=headers)

    line = the_one_line(tmp_path)
    record = json.loads(line)

    assert answer.status_code == HTTP_FORBIDDEN
    assert keys_of(line) == REFUSAL_KEYS
    assert after_the_time(line).startswith(NO_FAMILY + '"embed", ')
    assert record["family"] is None
    assert record["reason"] == "unknown_token"


def test_a_refused_manifest_fetch_says_that_no_family_matched(tmp_path: Path) -> None:
    client, _ = build(tmp_path)
    answer = client.get("/manifest")

    line = the_one_line(tmp_path)

    assert answer.status_code == HTTP_FORBIDDEN
    assert keys_of(line) == REFUSAL_KEYS
    assert after_the_time(line).startswith(NO_FAMILY + '"$manifest", ')
    assert json.loads(line)["family"] is None


def test_a_request_past_the_bucket_says_that_no_family_matched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, app = build(tmp_path)
    # A bucket fills again with time. The test holds it empty.
    monkeypatch.setattr(app.state.unauth_bucket, "take", lambda: False)

    answer = client.post("/call", json={"tool": "embed", "args": {}})
    line = the_one_line(tmp_path)
    record = json.loads(line)

    assert answer.status_code == HTTP_TOO_MANY
    assert keys_of(line) == REFUSAL_KEYS
    assert after_the_time(line).startswith(NO_FAMILY + '"embed", ')
    assert record["family"] is None
    assert record["reason"] == "rate_limited"


def test_a_body_over_the_cap_says_that_no_family_matched(tmp_path: Path) -> None:
    client, _ = build(tmp_path)
    answer = client.post("/call", content=b" " * (MAX_REQUEST_BODY_BYTES + 1), headers=JSON_HEADERS)

    line = the_one_line(tmp_path)
    record = json.loads(line)

    assert answer.status_code == HTTP_TOO_LARGE
    assert keys_of(line) == LARGE_BODY_KEYS
    assert after_the_time(line).startswith(NO_FAMILY + '"$oversized", ')
    assert record["family"] is None
    assert record["args_bytes"] == MAX_REQUEST_BODY_BYTES + 1
