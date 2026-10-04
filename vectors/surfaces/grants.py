"""The grant file, the two request bodies of the chaperone and its verb
catalog (contract 04).

Five surfaces:

- `grants.parse`: the bytes of one grant file to `parse_grants`, the typed
  grants or the reason the family fails closed.
- `grants.write`: the fields of one grant file to the bytes that
  `write_grant_file` of the caregiver writes.
- `chaperone.call_body`: the bytes of a `POST /call` body to `CallBody`.
- `chaperone.approval_body`: the bytes of a `POST /approval/<gate>` body to
  `ApprovalBody`.
- `chaperone.verb`: one text to an entry of the verb catalog, or to a
  refusal when the catalog has no such entry.

The two bodies are read the way the chaperone reads them: by FastAPI, as a
JSON body parameter. The generator mounts the two model classes on an
application of its own, so no vector needs a token, a grant file or an
upstream.
"""

from __future__ import annotations

import json
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, cast

from caregiver.grants import GrantFile as WrittenGrantFile
from caregiver.grants import write_grant_file
from chaperone.app import ApprovalBody, CallBody
from chaperone.family_grants import FamilyGrants, parse_grants
from chaperone.verbs import VERB_CATALOG
from fastapi import FastAPI
from pydantic import BaseModel, ValidationError
from starlette.testclient import TestClient

from vectors.core import (
    Json,
    Raised,
    Surface,
    Vector,
    accepted,
    attempt,
    bytes_input,
    expand,
    normalize,
    raised,
    refused,
    repeat_input,
    text_input,
)

CONTRACT: Final = "contract 04"

FAMILY: Final = "chat"
DIGEST: Final = "0123456789abcdef" * 4
OTHER_DIGEST: Final = "fedcba9876543210" * 4

#: Deeper than a JSON reader of any supported interpreter goes.
VERY_DEEP: Final = 400_000
#: A nesting that each supported interpreter reads. The reader of the Rust
#: port stops at 256 levels.
DEEP: Final = 300
HUGE_DIGITS: Final = 5_000

HTTP_OK: Final = 200


@dataclass(frozen=True)
class Document:
    """One input: an id and the bytes of the file or of the body."""

    id: str
    raw: bytes = b""
    #: The filename stem `parse_grants` is given. A body has none.
    family: str = FAMILY
    #: A long ASCII input, written as repeated parts in place of `raw`.
    parts: tuple[tuple[str, int], ...] = ()

    def data(self) -> bytes:
        return expand(self.parts).encode("ascii") if self.parts else self.raw

    def given(self) -> dict[str, Json]:
        return repeat_input(self.parts) if self.parts else bytes_input(self.raw)


def _minimal(**fields: object) -> dict[str, object]:
    """The smallest grant file `parse_grants` takes, with fields replaced."""
    return {
        "version": 2,
        "family": FAMILY,
        "rev": "reg-9f21c4",
        "token_sha256": [DIGEST],
        "model_alias": "fast",
        **fields,
    }


def _json(value: object) -> bytes:
    return json.dumps(value).encode("utf-8")


def _doc(doc_id: str, **fields: object) -> Document:
    """A grant file that is minimal but for `fields`."""
    return Document(doc_id, _json(_minimal(**fields)))


def _without(doc_id: str, field: str) -> Document:
    body = _minimal()
    del body[field]

    return Document(doc_id, _json(body))


def _servers(count: int) -> dict[str, list[str]]:
    """`count` servers, each with no tool."""
    empty: list[str] = []

    return {f"server-{number}": empty for number in range(count)}


def _verbs(count: int) -> dict[str, dict[str, object]]:
    """`count` verbs, each with no fence."""
    empty: dict[str, object] = {}

    return {f"verb_{number}": empty for number in range(count)}


FULL_GRANTS: Final[dict[str, object]] = {
    "version": 2,
    "family": FAMILY,
    "rev": "reg-9f21c4",
    "token_sha256": [DIGEST, OTHER_DIGEST],
    "model_alias": "agent-router",
    "tools": {"kagi": ["kagi_search_fetch", "kagi_extract"], "ha-read": ["ha_get_state"]},
    "verbs": {
        "embed": {},
        "ha_call": {
            "allow": [
                {"domain": "notify", "service": "mobile_app_example_phone"},
                {"domain": "light", "service": "turn_on", "entity_id": "light.example_lamp"},
            ]
        },
        "enqueue": {"targets": ["scrum-lead"]},
        "job_status": {},
        "release": {"components": ["chaperone", "attendance"]},
    },
    "delegates": ["vault-oracle"],
    "approval": ["ha_call", "invoke_agent", "kagi__kagi_extract"],
    "limits": {"pep_rpm": 60, "max_inflight_delegations": 2, "max_open_gates": 10},
}

_MINIMAL_TEXT: Final = json.dumps(_minimal())

GRANT_DOCUMENTS: Final[tuple[Document, ...]] = (
    # --- accepted ---------------------------------------------------------
    Document("full", _json(FULL_GRANTS)),
    _doc("minimal"),
    Document("minimal-pretty", json.dumps(_minimal(), indent=2).encode("utf-8") + b"\n"),
    Document("minimal-crlf-and-spaces", b" \r\n" + _MINIMAL_TEXT.encode("utf-8") + b"\r\n\t "),
    _doc("two-digests", token_sha256=[DIGEST, OTHER_DIGEST]),
    _doc("same-digest-twice", token_sha256=[DIGEST, DIGEST]),
    _doc("empty-collections", tools={}, verbs={}, delegates=[], approval=[], limits={}),
    _doc("tools-empty-list", tools={"kagi": []}),
    # --- the bytes and the JSON (§1.4) ------------------------------------
    Document("bytes-empty", b""),
    Document("bytes-not-utf8", b"\xff\xfe{}"),
    Document("bytes-utf16", _MINIMAL_TEXT.encode("utf-16")),
    Document("bytes-bom", b"\xef\xbb\xbf" + _MINIMAL_TEXT.encode("utf-8")),
    Document("json-text", b"not json"),
    Document("json-truncated", _MINIMAL_TEXT.encode("utf-8")[:-1]),
    Document("json-trailing-text", _MINIMAL_TEXT.encode("utf-8") + b" x"),
    Document("json-two-documents", _MINIMAL_TEXT.encode("utf-8") * 2),
    Document("json-single-quotes", _MINIMAL_TEXT.replace('"', "'").encode("utf-8")),
    Document(
        "json-duplicate-key",
        _MINIMAL_TEXT.replace('"family": "chat"', '"family": "code", "family": "chat"').encode(),
    ),
    Document(
        "json-duplicate-version",
        _MINIMAL_TEXT.replace('"version": 2', '"version": 1, "version": 2').encode(),
    ),
    Document(
        "json-huge-integer",
        _MINIMAL_TEXT.replace('"version": 2', '"version": ' + "9" * HUGE_DIGITS).encode(),
    ),
    Document(
        "json-very-deep",
        parts=(('{"version":2,"x":', 1), ("[", VERY_DEEP), ("]", VERY_DEEP), ("}", 1)),
    ),
    Document(
        "json-deep-unknown-field",
        _MINIMAL_TEXT[:-1].encode() + b', "x": ' + b"[" * 200 + b"]" * 200 + b"}",
    ),
    Document("json-nan-limit", _MINIMAL_TEXT[:-1].encode() + b', "limits": {"pep_rpm": NaN}}'),
    Document(
        "json-infinity-limit", _MINIMAL_TEXT[:-1].encode() + b', "limits": {"pep_rpm": Infinity}}'
    ),
    # --- the top level -------------------------------------------------------
    Document("top-array", b"[]"),
    Document("top-null", b"null"),
    Document("top-string", b'"grants"'),
    Document("top-number", b"2"),
    Document("top-empty-object", b"{}"),
    # --- version (§1.2, §1.4) ---
    _without("version-missing", "version"),
    _doc("version-one", version=1),
    _doc("version-three", version=3),
    _doc("version-zero", version=0),
    _doc("version-negative", version=-2),
    _doc("version-text", version="2"),
    _doc("version-float", version=2.0),
    _doc("version-true", version=True),
    _doc("version-null", version=None),
    _doc("version-past-64-bits", version=2**70),
    Document("version-exponent", _MINIMAL_TEXT.replace('"version": 2', '"version": 2e0').encode()),
    # --- family ---
    Document("family-other-file", _json(_minimal()), "code"),
    _doc("family-upper", family="Chat"),
    _doc("family-underscore", family="agent_control"),
    _doc("family-one-char", family="a"),
    _doc("family-32-chars", family="a" * 32),
    _doc("family-trailing-newline", family="chat\n"),
    _doc("family-number", family=5),
    _without("family-missing", "family"),
    # --- rev and model_alias ---
    _doc("rev-empty", rev=""),
    _doc("rev-128-chars", rev="r" * 128),
    _doc("rev-129-chars", rev="r" * 129),
    _doc("rev-128-two-byte-chars", rev="\u00e9" * 128),
    _doc("rev-number", rev=5),
    _without("rev-missing", "rev"),
    _doc("alias-empty", model_alias=""),
    _doc("alias-129-chars", model_alias="m" * 129),
    _doc("alias-any-text", model_alias="Not An Alias *"),
    _without("alias-missing", "model_alias"),
    # --- token_sha256 (§2.3) ---
    _doc("digests-empty", token_sha256=[]),
    _doc("digests-three", token_sha256=[DIGEST, OTHER_DIGEST, DIGEST]),
    _doc("digests-text", token_sha256=DIGEST),
    _doc("digest-upper", token_sha256=[DIGEST.upper()]),
    _doc("digest-63-chars", token_sha256=[DIGEST[:-1]]),
    _doc("digest-65-chars", token_sha256=[DIGEST + "0"]),
    _doc("digest-trailing-newline", token_sha256=[DIGEST + "\n"]),
    _doc("digest-number", token_sha256=[5]),
    _without("digests-missing", "token_sha256"),
    # --- tools ---
    _doc("tools-server-upper", tools={"Kagi": ["search"]}),
    _doc("tools-server-underscore", tools={"ha_read": ["search"]}),
    _doc("tools-server-one-char", tools={"k": ["search"]}),
    _doc("tools-tool-leading-digit", tools={"kagi": ["1search"]}),
    _doc("tools-tool-dot", tools={"kagi": ["a.b"]}),
    _doc("tools-tool-128-chars", tools={"kagi": ["a" * 128]}),
    _doc("tools-tool-129-chars", tools={"kagi": ["a" * 129]}),
    _doc("tools-tool-twice", tools={"kagi": ["search", "search"]}),
    _doc("tools-all-word", tools={"kagi": "all"}),
    _doc("tools-list", tools=["kagi"]),
    _doc("tools-null", tools=None),
    _doc("tools-64-servers", tools=_servers(64)),
    _doc("tools-65-servers", tools=_servers(65)),
    # --- verbs ---
    _doc("verbs-unknown-name", verbs={"teleport": {}}),
    _doc("verbs-name-upper", verbs={"HaCall": {}}),
    _doc("verbs-name-hyphen", verbs={"ha-call": {}}),
    _doc("verbs-name-32-chars", verbs={"v" * 32: {}}),
    _doc("verbs-name-33-chars", verbs={"v" * 33: {}}),
    _doc("verbs-16", verbs=_verbs(16)),
    _doc("verbs-17", verbs=_verbs(17)),
    _doc("verbs-fence-unknown-key", verbs={"embed": {"extra": 1}}),
    _doc("verbs-fence-null", verbs={"embed": None}),
    _doc("verbs-fence-list", verbs={"embed": []}),
    _doc(
        "verbs-every-key-null",
        verbs={"embed": {"allow": None, "targets": None, "components": None}},
    ),
    _doc("verbs-allow-empty", verbs={"ha_call": {"allow": []}}),
    _doc("verbs-allow-no-service", verbs={"ha_call": {"allow": [{"domain": "light"}]}}),
    _doc(
        "verbs-allow-unknown-key",
        verbs={"ha_call": {"allow": [{"domain": "light", "service": "on", "area": "x"}]}},
    ),
    _doc(
        "verbs-allow-domain-65-chars",
        verbs={"ha_call": {"allow": [{"domain": "d" * 65, "service": "s" * 64}]}},
    ),
    _doc(
        "verbs-allow-any-text",
        verbs={"ha_call": {"allow": [{"domain": "Not A Domain", "service": "", "entity_id": ""}]}},
    ),
    _doc(
        "verbs-allow-257",
        verbs={"ha_call": {"allow": [{"domain": "light", "service": "on"}] * 257}},
    ),
    _doc("verbs-targets-bad-name", verbs={"enqueue": {"targets": ["Scrum_Lead"]}}),
    _doc("verbs-targets-65", verbs={"enqueue": {"targets": ["scrum-lead"] * 65}}),
    _doc("verbs-components-any-text", verbs={"release": {"components": ["", "Not A Component"]}}),
    _doc("verbs-components-65", verbs={"release": {"components": ["chaperone"] * 65}}),
    # --- delegates and approval ---
    _doc("delegates-bad-name", delegates=["Vault_Oracle"]),
    _doc("delegates-32", delegates=["vault-oracle"] * 32),
    _doc("delegates-33", delegates=["vault-oracle"] * 33),
    _doc("delegates-text", delegates="vault-oracle"),
    _doc("approval-empty-entry", approval=[""]),
    _doc("approval-256-chars", approval=["a" * 256]),
    _doc("approval-257-chars", approval=["a" * 257]),
    _doc("approval-any-text", approval=["kagi__*", "not granted", "\u00e9"]),
    _doc("approval-256", approval=["embed"] * 256),
    _doc("approval-257", approval=["embed"] * 257),
    # --- limits ---
    _doc("limits-one-field", limits={"max_open_gates": 3}),
    _doc("limits-zero", limits={"pep_rpm": 0}),
    _doc("limits-negative", limits={"max_inflight_delegations": -1}),
    _doc("limits-text-number", limits={"pep_rpm": "60"}),
    _doc("limits-float-whole", limits={"pep_rpm": 60.0}),
    _doc("limits-float-fraction", limits={"pep_rpm": 60.5}),
    _doc("limits-true", limits={"pep_rpm": True}),
    _doc("limits-null", limits={"pep_rpm": None}),
    _doc("limits-past-64-bits", limits={"pep_rpm": 2**70}),
    _doc("limits-unknown-key", limits={"burst": 5}),
    _doc("limits-list", limits=[60, 2, 10]),
    _doc("limits-null-block", limits=None),
    # --- unknown fields ---
    _doc("unknown-top-field", note="written by hand"),
    _doc("unknown-token-field", token="not-a-digest"),
    # --- a limit that is not a JSON integer -------------------------------
    _doc("limits-false", limits={"pep_rpm": False}),
    _doc("limits-list-value", limits={"pep_rpm": [60]}),
    _doc("limits-float-one", limits={"pep_rpm": 1.0}),
    _doc("limits-float-negative-zero", limits={"pep_rpm": -0.0}),
    _doc("limits-float-below-2-to-63", limits={"pep_rpm": 9.223372036854775e18}),
    _doc("limits-float-2-to-63", limits={"pep_rpm": 2.0**63}),
    _doc("limits-text-spaces", limits={"pep_rpm": " 60 "}),
    _doc("limits-text-wide-spaces", limits={"pep_rpm": "\u00a060\u3000"}),
    _doc("limits-text-file-separator", limits={"pep_rpm": "\x1c60"}),
    _doc("limits-text-plus", limits={"pep_rpm": "+60"}),
    _doc("limits-text-two-signs", limits={"pep_rpm": "+-60"}),
    _doc("limits-text-sign-then-space", limits={"pep_rpm": "+ 60"}),
    _doc("limits-text-underscore", limits={"pep_rpm": "6_0"}),
    _doc("limits-text-underscore-first", limits={"pep_rpm": "_60"}),
    _doc("limits-text-underscore-last", limits={"pep_rpm": "60_"}),
    _doc("limits-text-two-underscores", limits={"pep_rpm": "6__0"}),
    _doc("limits-text-sign-then-underscore", limits={"pep_rpm": "+_60"}),
    _doc("limits-text-zero-first", limits={"pep_rpm": "060"}),
    _doc("limits-text-zero-then-underscore", limits={"pep_rpm": "0_60"}),
    _doc("limits-text-decimal-zeros", limits={"pep_rpm": "60.000"}),
    _doc("limits-text-every-form", limits={"pep_rpm": " +0_6_0.0 "}),
    _doc("limits-text-dot-last", limits={"pep_rpm": "60."}),
    _doc("limits-text-dot-first", limits={"pep_rpm": ".0"}),
    _doc("limits-text-fraction", limits={"pep_rpm": "60.5"}),
    _doc("limits-text-decimal-underscore", limits={"pep_rpm": "60.0_0"}),
    _doc("limits-text-exponent", limits={"pep_rpm": "6e1"}),
    _doc("limits-text-hex", limits={"pep_rpm": "0x3c"}),
    _doc("limits-text-empty", limits={"pep_rpm": ""}),
    _doc("limits-text-spaces-only", limits={"pep_rpm": "  "}),
    _doc("limits-text-fullwidth-digits", limits={"pep_rpm": "\uff16\uff10"}),
    _doc("limits-text-negative", limits={"pep_rpm": "-5"}),
    _doc("limits-text-zeros", limits={"pep_rpm": "00"}),
    _doc("limits-text-past-64-bits", limits={"pep_rpm": "9" * 30}),
    _doc("limits-text-4300-digits", limits={"pep_rpm": "9" * 4300}),
    _doc("limits-text-4301-digits", limits={"pep_rpm": "9" * 4301}),
    # --- more than one error (the reason gives their count) ----------------
    _doc("errors-two-fields", family="Chat", rev=""),
    _doc("errors-digest-bad-then-good", token_sha256=["x", DIGEST]),
    _doc("errors-digests-all-bad", token_sha256=["x", "y", "z"]),
    _doc("errors-digests-two-good-one-bad", token_sha256=[DIGEST, DIGEST, "x"]),
    _doc("errors-digests-three-good-one-bad", token_sha256=[DIGEST, DIGEST, DIGEST, "x"]),
    _doc("errors-delegates-bad-then-33", delegates=["Bad", *["vault-oracle"] * 33]),
    _doc("errors-tools-bad-key-and-65", tools={"Bad": [], **_servers(65)}),
    _doc("errors-tools-bad-key-and-value", tools={"Bad": "all"}),
    _doc("errors-tools-two-bad-tools", tools={"kagi": ["1a", "ok", "2b"]}),
    _doc(
        "errors-fence-fields-then-unknown",
        verbs={"ha_call": {"zzz": 1, "allow": "x", "targets": ["Bad"]}},
    ),
    _doc("errors-allow-items-not-objects", verbs={"ha_call": {"allow": ["x", 5, None]}}),
    _doc(
        "errors-allow-item-every-field",
        verbs={"ha_call": {"allow": [{"zz": 1, "domain": 5, "entity_id": 7}]}},
    ),
    Document("errors-missing-and-unknown", _json({"zzz": 1, "version": 2, "family": FAMILY})),
    _doc(
        "errors-limits-every-field",
        limits={"zz": 1, "pep_rpm": 0, "max_inflight_delegations": "x", "max_open_gates": None},
    ),
    Document("family-other-file-and-invalid", _json(_minimal(rev="")), "code"),
    # --- more shapes ---------------------------------------------------------
    _doc(
        "verbs-allow-entity-null",
        verbs={
            "ha_call": {"allow": [{"domain": "light", "service": "turn_on", "entity_id": None}]}
        },
    ),
    _doc("verbs-list", verbs=[]),
    _doc("delegates-object", delegates={"vault-oracle": 1}),
    _doc("rev-true", rev=True),
    _doc("rev-128-astral-chars", rev="\U0001f600" * 128),
    _doc("rev-129-astral-chars", rev="\U0001f600" * 129),
    _doc("rev-nul", rev="a\x00b"),
    # --- more of the JSON reader ---------------------------------------------
    Document("json-control-char", _MINIMAL_TEXT.replace("reg-9f21c4", "a\tb").encode()),
    Document(
        "json-escapes",
        _MINIMAL_TEXT.replace("reg-9f21c4", '\\"\\\\\\/\\b\\f\\n\\r\\t\\u00e9\\u00E9').encode(),
    ),
    Document("json-bad-escape", _MINIMAL_TEXT.replace("reg-9f21c4", "a\\xb").encode()),
    Document("json-bad-u-escape", _MINIMAL_TEXT.replace("reg-9f21c4", "\\u12G4").encode()),
    Document("json-surrogate-pair", _MINIMAL_TEXT.replace("reg-9f21c4", "\\ud83d\\ude00").encode()),
    Document("json-lone-surrogate", _MINIMAL_TEXT.replace("reg-9f21c4", "\\ud800").encode()),
    Document(
        "json-lone-surrogate-in-components",
        _MINIMAL_TEXT[:-1].encode() + b', "verbs": {"release": {"components": ["\\udfff"]}}}',
    ),
    Document("json-form-feed", b"\x0c" + _MINIMAL_TEXT.encode()),
    Document("json-empty-key", _MINIMAL_TEXT[:-1].encode() + b', "": 1}'),
    Document(
        "json-version-minus-zero", _MINIMAL_TEXT.replace('"version": 2', '"version": -0').encode()
    ),
    Document("json-true-capital", _MINIMAL_TEXT[:-1].encode() + b', "limits": {"pep_rpm": True}}'),
    Document(
        "json-number-zero-first", _MINIMAL_TEXT[:-1].encode() + b', "limits": {"pep_rpm": 060}}'
    ),
    Document(
        "json-number-dot-last", _MINIMAL_TEXT[:-1].encode() + b', "limits": {"pep_rpm": 60.}}'
    ),
    Document("json-number-plus", _MINIMAL_TEXT[:-1].encode() + b', "limits": {"pep_rpm": +60}}'),
    Document(
        "json-number-exponent", _MINIMAL_TEXT[:-1].encode() + b', "limits": {"pep_rpm": 6E1}}'
    ),
    Document(
        "json-number-overflow", _MINIMAL_TEXT[:-1].encode() + b', "limits": {"pep_rpm": 1e400}}'
    ),
    Document(
        "json-number-underflow", _MINIMAL_TEXT[:-1].encode() + b', "limits": {"pep_rpm": 1e-400}}'
    ),
    Document(
        "json-minus-infinity-limit",
        _MINIMAL_TEXT[:-1].encode() + b', "limits": {"pep_rpm": -Infinity}}',
    ),
    Document("json-minus-nan", _MINIMAL_TEXT[:-1].encode() + b', "limits": {"pep_rpm": -NaN}}'),
    Document(
        "json-integer-4300-digits",
        _MINIMAL_TEXT[:-1].encode() + b', "limits": {"pep_rpm": ' + b"9" * 4300 + b"}}",
    ),
    Document(
        "json-negative-integer-4301-digits",
        _MINIMAL_TEXT[:-1].encode() + b', "limits": {"pep_rpm": -' + b"9" * 4301 + b"}}",
    ),
    Document(
        "json-float-5000-digits",
        _MINIMAL_TEXT[:-1].encode() + b', "limits": {"pep_rpm": ' + b"9" * HUGE_DIGITS + b".0}}",
    ),
    # --- a limit text with a minus after its zeros, and a long one ---------
    _doc("limits-text-zero-then-minus", limits={"pep_rpm": "0-8"}),
    _doc("limits-text-zero-then-minus-zero", limits={"pep_rpm": "0-08"}),
    _doc("limits-text-zero-then-minus-underscore", limits={"pep_rpm": "0_-_8_0"}),
    _doc("limits-text-plus-4301-digits", limits={"pep_rpm": "+" + "9" * 4301}),
    _doc("limits-text-zero-then-4301-digits", limits={"pep_rpm": "0" + "9" * 4301}),
    # --- a nesting of 300 levels ---------------------------------------------
    Document(
        "json-deep-300-unknown-field",
        _MINIMAL_TEXT[:-1].encode() + b', "x": ' + b"[" * DEEP + b"]" * DEEP + b"}",
    ),
)

#: The fixed start of each reason `parse_grants` gives, after the file name.
_REASON_KINDS: Final = (
    ("not JSON (", "not_json"),
    ("the top level is not an object", "not_object"),
    ("version is not an integer", "version_not_integer"),
    ("unknown version ", "unknown_version"),
    ("invalid (", "invalid"),
    ("names family ", "other_family"),
)


def _reason_kind(message: str, family: str) -> str:
    detail = message.removeprefix(f"grants/{family}.json: ")
    for start, kind in _REASON_KINDS:
        if detail.startswith(start):
            return kind

    raise ValueError(f"a reason with no kind: {message}")


def _errors_of(exc: ValidationError) -> list[dict[str, object]]:
    """Where a model refused a document, with no echo of the input."""
    return [
        {"type": error["type"], "loc": list(error["loc"]), "msg": error["msg"]}
        for error in exc.errors()
    ]


def _model_errors(raw: bytes) -> list[dict[str, object]]:
    """The errors behind an `invalid` reason. `parse_grants` gives their count only."""
    try:
        FamilyGrants.model_validate(json.loads(raw.decode("utf-8")))
    except ValidationError as exc:
        return _errors_of(exc)

    return []


def _grant_vector(document: Document) -> Vector:
    given = document.given()
    raw = document.data()
    params = {"family": document.family}
    outcome = attempt(lambda: parse_grants(raw, document.family))
    if isinstance(outcome, Raised):
        return raised(document.id, given, outcome.exc, params=params)

    grants, message = outcome
    if grants is not None:
        return accepted(document.id, given, grants, params=params)

    kind = _reason_kind(message, document.family)
    refusal: dict[str, object] = {"kind": kind, "message": message}
    if kind == "invalid":
        refusal["errors"] = _model_errors(raw)

    return refused(document.id, given, refusal, params=params)


# --- the two request bodies ------------------------------------------------------

CALL_BODIES: Final[tuple[Document, ...]] = (
    Document("full", b'{"tool":"kagi__kagi_search_fetch","args":{"query":"weather","limit":3}}'),
    Document("no-args", b'{"tool":"embed"}'),
    Document("args-empty", b'{"tool":"embed","args":{}}'),
    Document("args-null", b'{"tool":"embed","args":null}'),
    Document("args-list", b'{"tool":"embed","args":[1,2]}'),
    Document("args-text", b'{"tool":"embed","args":"x"}'),
    Document(
        "args-nested",
        b'{"tool":"ha_call","args":{"a":{"b":[1,2.5,"c",null,true]},"d":{}}}',
    ),
    Document("args-key-number-like", b'{"tool":"embed","args":{"1":1,"":2}}'),
    Document("args-duplicate-key", b'{"tool":"embed","args":{"a":1,"a":2}}'),
    Document("args-nan", b'{"tool":"embed","args":{"a":NaN,"b":Infinity,"c":-Infinity}}'),
    Document("args-big-numbers", b'{"tool":"embed","args":{"a":36893488147419103232,"b":1e400}}'),
    Document("args-float-forms", b'{"tool":"embed","args":{"a":1.0,"b":1e2,"c":-0,"d":-0.0}}'),
    Document("args-lone-surrogate", b'{"tool":"embed","args":{"a":"\\ud800"}}'),
    Document("args-nul", b'{"tool":"embed","args":{"a":"x\\u0000y"}}'),
    Document("args-deep-200", b'{"tool":"embed","args":{"a":' + b"[" * 200 + b"]" * 200 + b"}}"),
    Document("tool-missing", b'{"args":{}}'),
    Document("tool-empty", b'{"tool":""}'),
    Document("tool-200-chars", _json({"tool": "t" * 200})),
    Document("tool-201-chars", _json({"tool": "t" * 201})),
    Document("tool-200-two-byte-chars", json.dumps({"tool": "\u00e9" * 200}).encode("utf-8")),
    Document("tool-any-text", b'{"tool":"not a tool\\n../x"}'),
    Document("tool-number", b'{"tool":5}'),
    Document("tool-null", b'{"tool":null}'),
    Document("tool-list", b'{"tool":["embed"]}'),
    Document("tool-duplicate-key", b'{"tool":"embed","tool":"ha_call"}'),
    Document("unknown-field", b'{"tool":"embed","args":{},"family":"chat"}'),
    Document("top-array", b'[{"tool":"embed"}]'),
    Document("top-null", b"null"),
    Document("top-text", b'"embed"'),
    Document("top-empty-object", b"{}"),
    Document("body-empty", b""),
    Document("body-spaces", b"  "),
    Document("json-truncated", b'{"tool":"embed"'),
    Document("json-trailing-comma", b'{"tool":"embed",}'),
    Document("json-trailing-text", b'{"tool":"embed"} x'),
    Document("json-single-quotes", b"{'tool':'embed'}"),
    Document("json-spaces-around", b' \r\n{"tool":"embed"}\t\n'),
    Document("bytes-bom", b'\xef\xbb\xbf{"tool":"embed"}'),
    Document("bytes-utf16", '{"tool":"embed"}'.encode("utf-16")),
    Document("bytes-utf16-le-no-bom", '{"tool":"embed"}'.encode("utf-16-le")),
    Document("bytes-not-utf8", b'{"tool":"\xff"}'),
    Document("json-huge-integer", b'{"tool":"embed","args":{"a":' + b"9" * HUGE_DIGITS + b"}}"),
    Document(
        "json-very-deep",
        parts=(('{"tool":"embed","args":{"a":', 1), ("[", VERY_DEEP), ("]", VERY_DEEP), ("}}", 1)),
    ),
    Document("errors-every-field", b'{"tool":5,"args":null,"x":1,"a":2}'),
    Document("tool-true", b'{"tool":true}'),
    Document("tool-200-astral-chars", _json({"tool": "\U0001f600" * 200})),
    Document("tool-201-astral-chars", _json({"tool": "\U0001f600" * 201})),
    Document("args-key-order", b'{"tool":"embed","args":{"b":1,"a":2,"b":3}}'),
    Document(
        "args-float-edges",
        b'{"tool":"embed","args":{"a":1e16,"b":1e-5,"c":5e-324,"d":1.7976931348623157e308,'
        b'"e":0.1,"f":1E2,"g":1e+2,"h":123456789.125,"i":-1e-7}}',
    ),
    Document(
        "args-escapes",
        b'{"tool":"embed","args":{"a":"\\"\\\\\\/\\b\\f\\n\\r\\t\\u00e9\\ud83d\\ude00"}}',
    ),
    Document("top-true", b"true"),
    Document("top-number", b"5"),
    Document("top-empty-text", b'""'),
    Document("body-newline", b"\n"),
    Document("json-number-zero-first", b'{"tool":"embed","args":{"a":01}}'),
    Document("json-control-char", b'{"tool":"em\tbed"}'),
    Document("json-integer-4300-digits", b'{"tool":"embed","args":{"a":' + b"9" * 4300 + b"}}"),
    Document("json-integer-4301-digits", b'{"tool":"embed","args":{"a":' + b"9" * 4301 + b"}}"),
    Document("bytes-utf32", '{"tool":"embed"}'.encode("utf-32")),
    Document("bytes-utf32-be-no-bom", '{"tool":"embed"}'.encode("utf-32-be")),
    Document("bytes-utf32-le-no-bom", '{"tool":"embed"}'.encode("utf-32-le")),
    Document("bytes-utf32-odd-length", '{"tool":"embed"}'.encode("utf-32") + b"\x00"),
    Document("bytes-utf16-be-no-bom", '{"tool":"embed"}'.encode("utf-16-be")),
    Document("bytes-utf16-be-bom", b"\xfe\xff" + '{"tool":"embed"}'.encode("utf-16-be")),
    Document("bytes-utf16-odd-length", '{"tool":"embed"}'.encode("utf-16") + b"\x00"),
    Document("bytes-utf16-astral", '{"tool":"\U0001f600"}'.encode("utf-16")),
    Document("bytes-two-nul-first", b"\x005"),
    Document("bytes-two-nul-last", b"5\x00"),
    Document("bytes-three-nul-last", b"{}\x00"),
    Document("bytes-bom-twice", b'\xef\xbb\xbf\xef\xbb\xbf{"tool":"embed"}'),
    Document("bytes-bom-then-spaces", b'\xef\xbb\xbf  {"tool":"embed"}'),
    Document("args-deep-300", b'{"tool":"embed","args":{"a":' + b"[" * DEEP + b"]" * DEEP + b"}}"),
    Document("unknown-field-deep-300", b'{"tool":"embed","x":' + b"[" * DEEP + b"]" * DEEP + b"}"),
)

APPROVAL_BODIES: Final[tuple[Document, ...]] = (
    Document("approve", b'{"decision":"approve"}'),
    Document("deny", b'{"decision":"deny"}'),
    Document("upper", b'{"decision":"Approve"}'),
    Document("trailing-space", b'{"decision":"approve "}'),
    Document("other-word", b'{"decision":"yes"}'),
    Document("empty", b'{"decision":""}'),
    Document("true", b'{"decision":true}'),
    Document("number", b'{"decision":1}'),
    Document("null", b'{"decision":null}'),
    Document("list", b'{"decision":["approve"]}'),
    Document("missing", b"{}"),
    Document("unknown-field", b'{"decision":"approve","gate":"0123456789abcdef"}'),
    Document("duplicate-key", b'{"decision":"deny","decision":"approve"}'),
    Document("escaped", b'{"decision":"\\u0061pprove"}'),
    Document("top-text", b'"approve"'),
    Document("top-array", b'["approve"]'),
    Document("body-empty", b""),
    Document("json-truncated", b'{"decision":"approve"'),
    Document("bytes-utf16", '{"decision":"approve"}'.encode("utf-16")),
    Document("bytes-bom", b'\xef\xbb\xbf{"decision":"approve"}'),
    Document("errors-every-field", b'{"decision":1,"x":1}'),
    Document("top-null", b"null"),
    Document("bytes-utf16-le-no-bom", '{"decision":"deny"}'.encode("utf-16-le")),
    Document(
        "unknown-field-deep-300", b'{"decision":"approve","x":' + b"[" * DEEP + b"]" * DEEP + b"}"
    ),
)


@dataclass
class BodyReader:
    """A FastAPI application that reads one body model, as the chaperone does."""

    client: TestClient
    seen: list[BaseModel]

    def read(self, raw: bytes) -> tuple[int, BaseModel | None, object]:
        """The status, the parsed body when FastAPI took it, and the error detail."""
        self.seen.clear()
        response = self.client.post(
            "/body", content=raw, headers={"content-type": "application/json"}
        )
        if response.status_code == HTTP_OK:
            return HTTP_OK, self.seen[0], None

        return response.status_code, None, cast("dict[str, Any]", response.json()).get("detail")


def call_reader() -> BodyReader:
    app = FastAPI()
    seen: list[BaseModel] = []

    @app.post("/body")
    async def read(body: CallBody) -> dict[str, bool]:  # pyright: ignore[reportUnusedFunction]
        seen.append(body)
        return {"ok": True}

    return BodyReader(TestClient(app), seen)


def _approval_reader() -> BodyReader:
    app = FastAPI()
    seen: list[BaseModel] = []

    @app.post("/body")
    async def read(body: ApprovalBody) -> dict[str, bool]:  # pyright: ignore[reportUnusedFunction]
        seen.append(body)
        return {"ok": True}

    return BodyReader(TestClient(app), seen)


#: FastAPI's error type for a body that is not JSON. Its location ends with
#: the offset at which the interpreter's JSON reader stopped.
JSON_INVALID: Final = "json_invalid"


def _error_row(error: dict[str, Any]) -> dict[str, object]:
    """One validation error: its type, location and message.

    The `input` and `ctx` keys go: they repeat the request, and `ctx` holds
    a message of the interpreter's JSON reader. The offset of a JSON error
    goes too. Two versions of the interpreter stop at two offsets on one
    input, and a reader in another language stops at a third.
    """
    loc = cast("list[object]", error["loc"])
    if error["type"] == JSON_INVALID:
        loc = loc[:1]

    return {"type": error["type"], "loc": loc, "msg": error["msg"]}


def _detail(detail: object) -> Json:
    """FastAPI's error detail: a list of validation errors, or one sentence."""
    if not isinstance(detail, list):
        return normalize(detail)

    return normalize([_error_row(one) for one in cast("list[dict[str, Any]]", detail)])


def _body_vector(document: Document, reader: BodyReader) -> Vector:
    given = document.given()
    raw = document.data()
    outcome = attempt(lambda: reader.read(raw))
    if isinstance(outcome, Raised):
        return raised(document.id, given, outcome.exc)

    status, body, detail = outcome
    if body is not None:
        return accepted(document.id, given, body, http_status=status)

    return refused(document.id, given, {"http_status": status, "detail": _detail(detail)})


_BODY_NOTES: Final = (
    "The input is the bytes of a request body. The content type is application/json.",
    "The entry point is FastAPI, with the model as the body parameter of a route. That is "
    "how the chaperone reads the body. The generator mounts the model on a route of its own.",
    "http_status is the HTTP status that FastAPI answers with. On a refused vector it is "
    "refusal.http_status. refusal.detail is the detail of the answer. A validation error "
    "keeps its type, location and message.",
    "An error of type json_invalid keeps no offset in its location. The offset belongs to the "
    "JSON reader of one interpreter version.",
    "The body cap of the chaperone is a different layer. No vector here is about it.",
)


def _body_surface(
    name: str,
    entry: str,
    section: str,
    documents: tuple[Document, ...],
    make: Callable[[], BodyReader],
) -> Surface:
    reader = make()
    with reader.client:
        vectors = tuple(_body_vector(document, reader) for document in documents)

    return Surface(
        name=f"chaperone.{name}",
        path=f"chaperone/{name}.json",
        entry=entry,
        contract=f"{CONTRACT} {section}",
        notes=_BODY_NOTES,
        vectors=vectors,
    )


# --- the writer of the grant file ------------------------------------------------


@dataclass(frozen=True)
class Written:
    """The fields of one grant file, as `caregiver.grants.GrantFile` takes them."""

    id: str
    family: str = FAMILY
    rev: str = "reg-9f21c4"
    token_sha256: tuple[str, ...] = (DIGEST,)
    model_alias: str = "fast"
    #: In sorted order, as `build_grant_file` gives the servers.
    tools: dict[str, list[str]] = field(default_factory=dict[str, list[str]])
    #: In the order of `caregiver.grants._verbs_json`: embed, ha_call,
    #: enqueue, job_status, release.
    verbs: dict[str, Any] = field(default_factory=dict[str, Any])
    delegates: tuple[str, ...] = ()
    max_inflight_delegations: int = 2
    approval: tuple[str, ...] = ()

    def grant(self) -> WrittenGrantFile:
        return WrittenGrantFile(
            family=self.family,
            rev=self.rev,
            token_sha256=self.token_sha256,
            model_alias=self.model_alias,
            tools=self.tools,
            verbs=self.verbs,
            delegates=self.delegates,
            max_inflight_delegations=self.max_inflight_delegations,
            approval=self.approval,
        )

    def args(self) -> dict[str, object]:
        return {
            "family": self.family,
            "rev": self.rev,
            "token_sha256": self.token_sha256,
            "model_alias": self.model_alias,
            "tools": self.tools,
            "verbs": self.verbs,
            "delegates": self.delegates,
            "max_inflight_delegations": self.max_inflight_delegations,
            "approval": self.approval,
        }


WRITTEN: Final[tuple[Written, ...]] = (
    Written("minimal"),
    Written(
        "full",
        rev="01K5J9QW3R7T0ZP4YB2H6N8M1D",
        token_sha256=(DIGEST, OTHER_DIGEST),
        model_alias="agent-router",
        tools={"ha-read": ["ha_get_state"], "kagi": ["kagi_search_fetch", "kagi_extract"]},
        verbs={
            "embed": {},
            "ha_call": {
                "allow": [
                    {"domain": "notify", "service": "mobile_app_example_phone", "entity_id": None},
                    {"domain": "light", "service": "turn_on", "entity_id": "light.example_lamp"},
                ]
            },
            "enqueue": {"targets": ["scrum-lead", "issue-worker"]},
            "job_status": {},
            "release": {"components": ["chaperone", "attendance"]},
        },
        delegates=("vault-oracle",),
        max_inflight_delegations=8,
        approval=("ha_call", "invoke_agent", "kagi__kagi_extract"),
    ),
    Written("tools-empty-list", tools={"kagi": []}),
    Written("tools-one-server", tools={"kagi": ["search"]}),
    Written("verbs-embed-only", verbs={"embed": {}}),
    Written("verbs-allow-empty", verbs={"ha_call": {"allow": []}}),
    Written("verbs-targets-empty", verbs={"enqueue": {"targets": []}, "job_status": {}}),
    Written("verbs-components-empty", verbs={"release": {"components": []}}),
    Written("delegates-two", delegates=("vault-oracle", "scrum-lead")),
    Written("approval-one", approval=("enqueue",)),
    Written("inflight-one", max_inflight_delegations=1),
    Written(
        "text-outside-ascii",
        rev='r\u00e9v "1" \\ \U0001f600 \x7f\t',
        model_alias="mod\u00e8le",
        approval=("caf\u00e9__\u2028",),
    ),
)


def _written_vector(written: Written, scratch: Path) -> Vector:
    given: dict[str, Json] = {"args": normalize(written.args())}
    target = scratch / f"{written.id}.json"
    outcome = attempt(lambda: write_grant_file(target, written.grant()))
    if isinstance(outcome, Raised):
        return raised(written.id, given, outcome.exc)

    raw = target.read_bytes()
    grants, message = parse_grants(raw, written.family)
    if grants is None:
        raise ValueError(f"{written.id}: the chaperone refuses what the caregiver wrote: {message}")

    return accepted(written.id, given, grants, output=bytes_input(raw))


def _write_surface() -> Surface:
    with tempfile.TemporaryDirectory(prefix="vectors-grants-") as scratch_name:
        scratch = Path(scratch_name)
        vectors = tuple(_written_vector(written, scratch) for written in WRITTEN)

    return Surface(
        name="grants.write",
        path="chaperone/grants_write.json",
        entry="caregiver.grants.write_grant_file",
        contract=f"{CONTRACT} §1.2, §1.3",
        notes=(
            "The input is the fields of caregiver.grants.GrantFile.",
            "output is the exact bytes of the file that the entry point writes.",
            "value is what chaperone.family_grants.parse_grants reads from those bytes.",
            "The file holds the servers of tools in sorted order. It holds the verbs in this "
            "order: embed, ha_call, enqueue, job_status, release. "
            "caregiver.grants.build_grant_file gives the entry point that order.",
            "The entry point writes pep_rpm 60 and max_open_gates 10 into each file.",
            "The entry point does not validate a field. The caregiver validates the family "
            "file before it calls the entry point. Each vector here holds valid fields.",
        ),
        vectors=vectors,
    )


# --- the verb catalog ------------------------------------------------------------

#: Texts that are not the name of an entry of the catalog, each with the id
#: of its vector.
NOT_VERBS: Final = (
    ("unknown", "teleport"),
    ("empty", ""),
    ("upper-case", "Embed"),
    ("hyphen", "ha-call"),
    ("space-last", "embed "),
    ("newline-last", "embed\n"),
    ("tool-with-server", "kagi__kagi_search_fetch"),
    ("manifest", "$manifest"),
)


def _verb_surface() -> Surface:
    entries = tuple(accepted(name.replace("_", "-"), text_input(name)) for name in VERB_CATALOG)
    others = tuple(
        accepted(f"not-a-verb-{name}", text_input(text))
        if text in VERB_CATALOG
        else refused(f"not-a-verb-{name}", text_input(text))
        for name, text in NOT_VERBS
    )

    return Surface(
        name="chaperone.verb",
        path="chaperone/verb.json",
        entry="chaperone.verbs.VERB_CATALOG",
        contract=f"{CONTRACT} §4.1",
        notes=(
            "The input is one text. An accepted vector is the name of an entry of the verb "
            "catalog. A refused vector is a text that the catalog does not hold.",
            "The surface holds each entry that the catalog holds, in the order of the catalog.",
            "invoke_agent is an entry of the catalog. A family file grants it through "
            "delegates, and not through verbs.",
        ),
        vectors=(*entries, *others),
    )


def surfaces() -> tuple[Surface, ...]:
    return (
        Surface(
            name="grants.parse",
            path="chaperone/grants.json",
            entry="chaperone.family_grants.parse_grants",
            contract=f"{CONTRACT} §1",
            notes=(
                "The input is the bytes of one grant file. params.family is the stem of its "
                "file name, which the entry point takes as its second argument.",
                "value is the typed grants, with every default filled in.",
                "refusal.message is the reason the entry point returns. The chaperone writes "
                "it into the fault file.",
                "refusal.kind is not a value of the Python code. The generator derives it from "
                "the fixed start of the message, so that a reader need not match the text.",
                "For the kind not_json, the message ends with the text of Python's JSON "
                "reader. Compare the kind there, and not the message.",
                "refusal.errors is present when kind is invalid. The entry point gives the "
                "count of the errors only. The generator gets each error from "
                "FamilyGrants.model_validate on the same document.",
                "The size cap of a grant file is checked before the entry point runs. No "
                "vector here is about it.",
            ),
            vectors=tuple(_grant_vector(document) for document in GRANT_DOCUMENTS),
        ),
        _write_surface(),
        _body_surface("call_body", "chaperone.app.CallBody", "§5", CALL_BODIES, call_reader),
        _body_surface(
            "approval_body", "chaperone.app.ApprovalBody", "§8.4", APPROVAL_BODIES, _approval_reader
        ),
        _verb_surface(),
    )
