"""The reader of the release statement runs the vectors and the schema of
the signer crate (`rust/crates/ci-signer/AGENTS.md`, "Vectors"). The crate
is the authority. These tests read its files in place and copy none, so
the signer and root's reader cannot drift.
"""

from __future__ import annotations

import base64
import json
import re
from pathlib import Path
from typing import Any

import pytest
from handover.errors import Refusal, RefusalCode
from handover.executor import release_statement as rs
from handover.executor.release_statement import (
    StatementReason,
    UnverifiedMessage,
    canonical_bytes,
    check_signature,
    decode_message,
    read_statement,
)
from handover_executor_fixtures import FakeRun
from handover_frp_fixtures import SIGNER

VECTORS = SIGNER / "vectors"
SCHEMA = SIGNER / "schema" / "release-statement.json"
LIMITS = SIGNER / "src" / "limits.rs"
NAMES = SIGNER / "src" / "names.rs"

#: A component of the catalog. The vectors name no component of their own.
COMPONENT = "handover"


def _cases(file: str, key: str) -> list[dict[str, Any]]:
    return json.loads((VECTORS / file).read_text(encoding="utf-8"))[key]


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _text(field: str | list[list[Any]] | None) -> str | None:
    """A text of `tag-message.json`: a string, null, or a recipe of pairs
    `[piece, count]` (the key `texts` of that file)."""
    if field is None or isinstance(field, str):
        return field

    return "".join(str(piece) * int(count) for piece, count in field)


#: The shared word of each refusal (`rust/crates/ci-signer/AGENTS.md`,
#: "Vectors"). The map is closed: a new word fails the lookup.
REASONS: dict[str, StatementReason] = {
    "too_large": StatementReason.TOO_LARGE,
    "suffix": StatementReason.SUFFIX,
    "lines": StatementReason.LINES,
    "header": StatementReason.HEADER,
    "base64": StatementReason.BASE64,
    "statement_size": StatementReason.STATEMENT_SIZE,
    "bundle_size": StatementReason.BUNDLE_SIZE,
    "not_json": StatementReason.NOT_JSON,
    "field": StatementReason.FIELD,
    "not_canonical": StatementReason.NOT_CANONICAL,
}


def _refused(refusal: Refusal, case: dict[str, Any]) -> None:
    """A refusal has the code P1 and the reason of the vector."""
    assert refusal.code is RefusalCode.P1, case["note"]
    assert refusal.detail == REASONS[case["reason"]].value, case["note"]


def test_every_tag_message_vector_has_its_verdict() -> None:
    """Each `TagMessage` vector gives the verdict of the signer crate.

    Root reads the message only from `git/tags/{sha}`, which adds one LF
    to the message as written. A vector of the source `tags_api` is the
    message as written, so the test adds that LF. That is the reply of
    `git/tags/{sha}` for the same tag, and `message.rs` gives the two forms
    the same verdict.
    """
    cases = _cases("tag-message.json", "TagMessage")
    assert len(cases) == 24

    for case in cases:
        text = _text(case["message"])
        assert text is not None, case["note"]
        message = text + ("\n" if case["source"] == "tags_api" else "")
        try:
            decoded = decode_message(COMPONENT, message)
        except Refusal as refusal:
            assert case["statement_b64"] is None, case["note"]
            _refused(refusal, case)
            continue

        assert "reason" not in case, case["note"]
        assert (_b64(decoded.statement), _b64(decoded.bundle)) == (
            _text(case["statement_b64"]),
            _text(case["bundle_b64"]),
        ), case["note"]


def test_every_statement_vector_has_its_verdict(tmp_path: Path) -> None:
    """Each `Statement` vector gives the verdict of `Statement::from_canonical`.
    Each one goes through the real `check_signature`, with a runner whose
    verifier child passes, and then through `read_statement`. A pass gives
    the canonical bytes of the vector."""
    cases = _cases("statement.json", "Statement")
    assert len(cases) == 34

    for number, case in enumerate(cases):
        message = UnverifiedMessage(statement=case["json"].encode("utf-8"), bundle=b"{}")
        work = tmp_path / str(number)
        work.mkdir()
        signed = check_signature(FakeRun(), COMPONENT, message, work)
        try:
            statement = read_statement(signed)
        except Refusal as refusal:
            assert case["written"] is None, case["note"]
            _refused(refusal, case)
            continue

        assert "reason" not in case, case["note"]
        assert canonical_bytes(statement).decode("ascii") == case["written"], case["note"]


def _defs() -> dict[str, Any]:
    return json.loads(SCHEMA.read_text(encoding="utf-8"))["$defs"]


def test_patterns_equal_the_schema() -> None:
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    defs = schema["$defs"]
    patterns = {
        "ReleaseTag": rs.RELEASE_TAG_PATTERN,
        "AssetName": rs.ASSET_NAME_PATTERN,
        "RepoFullName": rs.REPO_FULL_NAME_PATTERN,
        "WorkflowFile": rs.WORKFLOW_FILE_PATTERN,
        "CommitSha": rs.COMMIT_SHA_PATTERN,
        "Sha256Hex": rs.SHA256_HEX_PATTERN,
        "SignedAt": rs.SIGNED_AT_PATTERN,
    }
    for name, pattern in patterns.items():
        assert defs[name]["pattern"] == f"^{pattern}$", name

    assert (defs["RunId"]["minimum"], defs["RunId"]["maximum"]) == (1, rs.MAX_RUN_ID)
    assert defs["Assets"]["maxItems"] == rs.MAX_ASSETS_PER_TAG
    assert (defs["Tags"]["minItems"], defs["Tags"]["maxItems"]) == (1, rs.MAX_TAGS)
    assert schema["properties"]["statement"]["const"] == rs.STATEMENT_VERSION
    assert defs["Event"]["oneOf"][0]["const"] == rs.STATEMENT_EVENT
    assert defs["Branch"]["oneOf"][0]["const"] == rs.STATEMENT_REF

    keys = rs._STATEMENT_KEYS  # pyright: ignore[reportPrivateUsage]
    assert frozenset(schema["required"]) == keys
    assert frozenset(defs["TagEntry"]["required"]) == rs._TAG_KEYS  # pyright: ignore[reportPrivateUsage]
    assert frozenset(defs["Asset"]["required"]) == rs._ASSET_KEYS  # pyright: ignore[reportPrivateUsage]
    for name in ("Asset", "TagEntry"):
        assert defs[name]["additionalProperties"] is False
    assert schema["additionalProperties"] is False


#: One line `pub const NAME: usize = N;` of a Rust file. A number may hold
#: the digit separator `_`, as `120_000` does.
CONST_RE = re.compile(r"pub const ([A-Z_]+): usize = ([0-9][0-9_]*);")
HEADER_RE = re.compile(r'pub const MESSAGE_HEADER: &str = "([^"]*)";')


def _constants(path: Path) -> dict[str, int]:
    found: dict[str, int] = {}
    for name, number in CONST_RE.findall(path.read_text(encoding="utf-8")):
        assert name not in found, name
        found[name] = int(number.replace("_", ""))

    return found


def test_limits_equal_the_signer() -> None:
    limits, names = _constants(LIMITS), _constants(NAMES)
    expected = {
        "MAX_TAGS": rs.MAX_TAGS,
        "MAX_ASSETS_PER_TAG": rs.MAX_ASSETS_PER_TAG,
        "MAX_NAME_BYTES": rs.MAX_NAME_BYTES,
        "MAX_WORKFLOW_BYTES": rs.MAX_WORKFLOW_BYTES,
        "MAX_BUNDLE_BYTES": rs.MAX_BUNDLE_BYTES,
        "MAX_TAG_MESSAGE_BYTES": rs.MAX_TAG_MESSAGE_BYTES,
    }
    for name, value in expected.items():
        assert limits[name] == value, name

    assert names["MAX_OWNER_BYTES"] == rs.MAX_OWNER_BYTES
    assert names["MAX_REPO_NAME_BYTES"] == rs.MAX_REPO_NAME_BYTES
    assert HEADER_RE.findall(LIMITS.read_text(encoding="utf-8")) == [rs.MESSAGE_HEADER]


def test_the_worst_message_fits_the_cap() -> None:
    """The compile-time assertion of `limits.rs`: the largest message that
    the limits allow is below the cap. The cap is thus no refusal of a
    message that the signer can write."""
    worst = (
        len(rs.MESSAGE_HEADER)
        + 1
        + len(_b64(b"x" * rs.WORST_STATEMENT_BYTES))
        + 1
        + len(_b64(b"x" * rs.MAX_BUNDLE_BYTES))
    )
    assert 110_000 <= worst <= rs.MAX_TAG_MESSAGE_BYTES


@pytest.mark.parametrize(
    ("signed_at", "verdict"),
    [
        ("2028-02-29T23:59:59Z", True),
        ("2000-02-29T00:00:00Z", True),
        ("9999-12-31T23:59:59Z", True),
        ("1970-01-01T00:00:00Z", True),
        ("2100-02-29T00:00:00Z", False),
        ("2026-02-30T00:00:00Z", False),
        ("2026-13-01T00:00:00Z", False),
        ("2026-11-15T24:00:00Z", False),
        ("2026-11-15T23:59:60Z", False),
        ("1969-12-31T23:59:59Z", False),
        ("0000-01-01T00:00:00Z", False),
    ],
)
def test_signed_at_follows_the_signer(tmp_path: Path, signed_at: str, verdict: bool) -> None:
    """The cases of `signed_at.rs`. The vectors hold the edges of
    `signed_at`. This test also holds the leap years, the month 13 and the
    hour 24 of the Rust reader."""
    first = _cases("statement.json", "Statement")[0]["json"]
    text = first.replace("2026-11-15T12:00:00Z", signed_at)
    message = UnverifiedMessage(statement=text.encode("utf-8"), bundle=b"{}")
    signed = check_signature(FakeRun(), COMPONENT, message, tmp_path)
    if verdict:
        assert read_statement(signed).signed_at == signed_at
        return

    with pytest.raises(Refusal) as refused:
        read_statement(signed)
    assert refused.value.detail == StatementReason.FIELD.value
