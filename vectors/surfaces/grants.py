"""The grant file and the two request bodies of the chaperone (contract 04).

Three surfaces:

- `grants.parse`: the bytes of one grant file to `parse_grants`, the typed
  grants or the reason the family fails closed.
- `chaperone.call_body`: the bytes of a `POST /call` body to `CallBody`.
- `chaperone.approval_body`: the bytes of a `POST /approval/<gate>` body to
  `ApprovalBody`.

The two bodies are read the way the chaperone reads them: by FastAPI, as a
JSON body parameter. The generator mounts the two model classes on an
application of its own, so no vector needs a token, a grant file or an
upstream.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final, cast

from chaperone.app import ApprovalBody, CallBody
from chaperone.family_grants import FamilyGrants, parse_grants
from fastapi import FastAPI
from pydantic import BaseModel, ValidationError
from starlette.testclient import TestClient

from vectors.core import (
    Json,
    Surface,
    Vector,
    accepted,
    bytes_input,
    expand,
    normalize,
    refused,
    repeat_input,
    run,
)

CONTRACT: Final = "contract 04"

FAMILY: Final = "chat"
DIGEST: Final = "0123456789abcdef" * 4
OTHER_DIGEST: Final = "fedcba9876543210" * 4

#: Deeper than a JSON reader of any supported interpreter goes.
VERY_DEEP: Final = 400_000
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

    def call() -> Vector:
        grants, message = parse_grants(raw, document.family)
        if grants is not None:
            return accepted(document.id, given, grants, params=params)

        kind = _reason_kind(message, document.family)
        refusal: dict[str, object] = {"kind": kind, "message": message}
        if kind == "invalid":
            refusal["errors"] = _model_errors(raw)

        return refused(document.id, given, refusal, params=params)

    return run(document.id, given, call, params=params)


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


def _call_reader() -> BodyReader:
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

    def call() -> Vector:
        status, body, detail = reader.read(document.data())
        if body is not None:
            return accepted(document.id, given, body, status=status)

        return refused(document.id, given, {"status": status, "detail": _detail(detail)})

    return run(document.id, given, call)


_BODY_NOTES: Final = (
    "The input is the bytes of a request body. The content type is application/json.",
    "The entry point is FastAPI, with the model as the body parameter of a route. That is "
    "how the chaperone reads the body. The generator mounts the model on a route of its own.",
    "refusal.status is the HTTP status that FastAPI answers with. refusal.detail is the detail "
    "of its answer. A validation error keeps its type, location and message.",
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
        _body_surface("call_body", "chaperone.app.CallBody", "§5", CALL_BODIES, _call_reader),
        _body_surface(
            "approval_body", "chaperone.app.ApprovalBody", "§8.4", APPROVAL_BODIES, _approval_reader
        ),
    )
