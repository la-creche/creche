"""The component manifest and the release request (contract 06).

Eleven surfaces, all of `handover`:

- `manifest.component`: the text of one `component.yaml` to `parse_manifest`.
- `manifest.operator`: the two values of the site file that `parse_manifest`
  reads.
- `manifest.catalog`: the fixed component list and each closed set, as data.
- `manifest.refusal_code`: one text to `RefusalCode`.
- `manifest.request.parse`: the bytes of one request file to `parse_request`.
- `manifest.request.plan`: the arguments of the requester to the bytes that
  `file_request` writes.
- `manifest.request.ulid`: a time and ten bytes to `new_ulid`.
- `manifest.state`: the text of one live-state document to `parse_state`.
- `manifest.resolved`: one resolution to the resolved manifest, its hash
  input and its hash.
- `manifest.gate`: a manifest hash and a request id to the gate id and the
  action string.
- `manifest.summary`: the seven fields of the approval summary.

A float that the Python code takes or gives is written twice where a reader
must hold it exactly: as a JSON number, and as the 16 hexadecimal digits of
its IEEE 754 bits. A JSON reader in another language can round the number.
"""

from __future__ import annotations

import json
import os
import struct
import tempfile
from collections.abc import Callable, Generator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Final, cast

from handover.catalog import Action, ContractId
from handover.contracts import Consumer, ContractRow
from handover.errors import Refusal, RefusalCode
from handover.executor import approval
from handover.executor.request import MAX_REQUEST_BYTES, Request, parse_request
from handover.manifest import parse_manifest
from handover.requester.file import file_request, new_ulid, plan_request
from handover.resolve import (
    Resolution,
    ResolvedComponent,
    build_document,
    canonical_json,
    manifest_hash,
)
from handover.state import MAX_STATE_BYTES, ReleaseState, SourceFacts, parse_state

from handover import catalog, site
from vectors.core import (
    Json,
    Raised,
    Surface,
    Vector,
    accepted,
    attempt,
    bytes_input,
    expand,
    raised,
    refused,
    repeat_input,
    text_input,
)
from vectors.surfaces.manifest_cases import (
    CASES,
    OPERATOR_HOME,
    OPERATOR_USER,
    SITE_MISSING,
    SITE_SET,
    Case,
)

CONTRACT: Final = "contract 06"

REPO_ROOT: Final = Path(__file__).resolve().parents[2]

#: The label that `parse_manifest` and `parse_state` name in a refusal.
MANIFEST_SUBJECT: Final = "attendance/component.yaml"
STATE_SUBJECT: Final = "live-state.json"

#: The mode of the temporary site file: only its owner writes it.
SITE_FILE_MODE: Final = 0o644

#: What stands for the path of the temporary site file in a refusal.
SITE_MARK: Final = "<site>"

ULID: Final = "01K5J8M2Q7V3X9R4T6N0B8C2DE"
OTHER_ULID: Final = "01K5J8M2Q7V3X9R4T6N0B8C2DF"
#: A request id that is not a ULID and is safe as a file name.
NOT_A_ULID: Final = "not-a-ulid"

SHA: Final = "9d1f0c7a5b2e4438a6c0d19f37be5a2c48e1067b"
DIGEST: Final = "sha256:" + "0123456789abcdef" * 4
OTHER_DIGEST: Final = "sha256:" + "fedcba9876543210" * 4

ARABIC_INDIC_VERSION: Final = "\u0661.\u0662.\u0663"

type Outcome[T] = T | Refusal | Raised


def _bits(value: float) -> str:
    """The IEEE 754 bits of a float, as 16 hexadecimal digits."""
    return struct.pack(">d", value).hex()


def _float(bits: str) -> float:
    (value,) = struct.unpack(">d", bytes.fromhex(bits))

    return float(value)


def _run[T](call: Callable[[], T]) -> Outcome[T]:
    """What the product code returns, the refusal it raises, or another exception."""

    def refusing() -> T | Refusal:
        try:
            return call()
        except Refusal as refusal:
            return refusal

    return attempt(refusing)


def _refusal(refusal: Refusal, hide: str = "") -> dict[str, str]:
    """A refusal as the ledger holds it. `hide` is a path of this machine."""
    body = refusal.as_dict()
    if hide:
        body = {key: value.replace(hide, SITE_MARK) for key, value in body.items()}

    return body


# --- the site file -----------------------------------------------------------


@contextmanager
def site_file(lines: tuple[str, ...] | None) -> Generator[str]:
    """A site file of these lines for the calls inside, or no site file."""
    previous = os.environ.get(site.SITE_FILE_ENV)
    with tempfile.TemporaryDirectory() as root:
        path = Path(root) / "site.env"
        if lines is not None:
            path.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")
            # The reader refuses a site file that its group can write. The mask
            # of the caller must not decide that.
            path.chmod(SITE_FILE_MODE)

        os.environ[site.SITE_FILE_ENV] = str(path)
        site.forget()
        try:
            yield str(path)
        finally:
            site.forget()
            if previous is None:
                del os.environ[site.SITE_FILE_ENV]
            else:
                os.environ[site.SITE_FILE_ENV] = previous


def _site_lines(user: str, home: str) -> tuple[str, ...]:
    return (f"AGENT_OPERATOR_USER={user}", f"AGENT_OPERATOR_HOME={home}")


_SITES: Final[dict[str, tuple[str, ...] | None]] = {
    SITE_SET: _site_lines(OPERATOR_USER, OPERATOR_HOME),
    SITE_MISSING: None,
}


# --- component.yaml ----------------------------------------------------------


def _repo_cases() -> tuple[Case, ...]:
    """Each `component.yaml` that this repository holds."""
    return tuple(
        Case(f"repo-{path.parent.name}", path.read_text(encoding="utf-8"))
        for path in sorted(REPO_ROOT.glob("*/component.yaml"))
    )


def _component_vector(case: Case) -> Vector:
    given = repeat_input(case.parts) if case.parts else text_input(case.text)
    text = expand(case.parts) if case.parts else case.text
    params = {"site": case.site}
    with site_file(_SITES[case.site]) as path:
        outcome = _run(lambda: parse_manifest(text, MANIFEST_SUBJECT))

    if isinstance(outcome, Raised):
        return raised(case.id, given, outcome.exc, params=params)

    if isinstance(outcome, Refusal):
        return refused(case.id, given, _refusal(outcome, path), params=params)

    return accepted(case.id, given, outcome, params=params)


def _component_surface() -> Surface:
    return Surface(
        name="manifest.component",
        path="manifest/component.json",
        entry="handover.manifest.parse_manifest",
        contract=f"{CONTRACT} \u00a78, \u00a710",
        notes=(
            "The input is the text of one component.yaml. context.subject is the label that "
            "the entry point takes as its second argument.",
            "params.site is the site file of the vector. For `set`, the site file holds the "
            "account and the home of context.operator. For `missing`, the host has no site "
            "file.",
            "value is the parsed manifest. A path that starts with `~/` holds the home of the "
            "operator in place of `~`.",
            "refusal holds the check, the subject and the detail of the refusal, as the ledger "
            "holds them. The subject of a nested mapping ends with `: ` and the field.",
            "A detail of the form `does not parse: line N` gives the line of the mark of "
            "PyYAML, from 1.",
            "A refusal with the check `site` names the site file. Its detail holds <site> in "
            "place of the path of that file.",
            "The vectors with the id `repo-<directory>` hold each component.yaml of this "
            "repository.",
        ),
        context={
            "subject": MANIFEST_SUBJECT,
            "operator": {"user": OPERATOR_USER, "home": OPERATOR_HOME},
        },
        vectors=tuple(_component_vector(case) for case in (*CASES, *_repo_cases())),
    )


# --- the operator account of the site file -----------------------------------

#: A user and a home, by vector id. No value holds a space, a quote, `#` or a
#: line break: the reader of the site file changes such a value first.
OPERATORS: Final[tuple[tuple[str, str, str], ...]] = (
    ("both", OPERATOR_USER, OPERATOR_HOME),
    ("user-one-char", "k", OPERATOR_HOME),
    ("user-underscore-first", "_keeper", OPERATOR_HOME),
    ("user-32-chars", "k" * 32, OPERATOR_HOME),
    ("user-33-chars", "k" * 33, OPERATOR_HOME),
    ("user-digits-and-hyphen", "keeper-2_x", OPERATOR_HOME),
    ("user-leading-digit", "2keeper", OPERATOR_HOME),
    ("user-leading-hyphen", "-keeper", OPERATOR_HOME),
    ("user-upper", "Keeper", OPERATOR_HOME),
    ("user-dot", "kee.per", OPERATOR_HOME),
    ("user-empty", "", OPERATOR_HOME),
    ("user-root", "root", "/root"),
    ("user-not-ascii", "k\u00e9eper", OPERATOR_HOME),
    ("home-one-segment", OPERATOR_USER, "/keeper"),
    ("home-deep", OPERATOR_USER, "/srv/homes/a.b/keeper_2-x"),
    ("home-root", OPERATOR_USER, "/"),
    ("home-empty", OPERATOR_USER, ""),
    ("home-relative", OPERATOR_USER, "home/keeper"),
    ("home-final-slash", OPERATOR_USER, "/home/keeper/"),
    ("home-empty-segment", OPERATOR_USER, "/home//keeper"),
    ("home-parent", OPERATOR_USER, "/home/../keeper"),
    ("home-dot-segment", OPERATOR_USER, "/home/./keeper"),
    ("home-hidden-segment", OPERATOR_USER, "/home/.keeper"),
    ("home-hyphen-first", OPERATOR_USER, "/home/-keeper"),
    ("home-colon", OPERATOR_USER, "/home/kee:per"),
    ("home-not-ascii", OPERATOR_USER, "/home/k\u00e9eper"),
    ("home-tilde", OPERATOR_USER, "~/keeper"),
    ("both-bad", "Keeper", "home"),
)


def _operator_vector(vector_id: str, user: str, home: str) -> Vector:
    given: dict[str, Json] = {"args": {"user": user, "home": home}}
    with site_file(_site_lines(user, home)) as path:
        outcome = _run(lambda: {"user": site.operator_user(), "home": site.operator_home()})

    if isinstance(outcome, Raised):
        return raised(vector_id, given, outcome.exc)

    if isinstance(outcome, Refusal):
        return refused(vector_id, given, _refusal(outcome, path))

    return accepted(vector_id, given, outcome)


def _operator_surface() -> Surface:
    return Surface(
        name="manifest.operator",
        path="manifest/operator.json",
        entry="handover.site.operator_user, handover.site.operator_home",
        contract=f"{CONTRACT} \u00a78",
        notes=(
            "The input is the account and the home of the operator. The generator writes a "
            "site file with AGENT_OPERATOR_USER and AGENT_OPERATOR_HOME, calls operator_user "
            "and then operator_home.",
            "value holds the two values. refusal is the first refusal, with the check `site`.",
            "These two values are what parse_manifest reads from the site file. No vector "
            "here is about another line of the site file or about its form.",
        ),
        vectors=tuple(_operator_vector(*row) for row in OPERATORS),
    )


# --- the catalog and the refusal codes ---------------------------------------


def _catalog_values() -> dict[str, object]:
    """Each public constant of `handover.catalog`, by vector id."""
    return {
        "components": catalog.CATALOG,
        "releasable-names": catalog.releasable_names(),
        "contract-owners": catalog.CONTRACT_OWNER,
        "retiring": catalog.RETIRING,
        "arriving": catalog.ARRIVING,
        "binary-build-files": catalog.BINARY_BUILD_FILES,
        "manifest-contract-version": {
            "major": catalog.MANIFEST_CONTRACT_MAJOR,
            "minor": catalog.MANIFEST_CONTRACT_MINOR,
        },
        "last-in-order": catalog.LAST_IN_ORDER,
        "max-request-components": catalog.MAX_REQUEST_COMPONENTS,
        "set-repo": list(catalog.Repo),
        "set-kind": list(catalog.Kind),
        "set-runs-as": list(catalog.RunsAs),
        "set-verify-user": list(catalog.VerifyUser),
        "set-restore-mode": list(catalog.RestoreMode),
        "set-action": list(catalog.Action),
        "set-contract-id": list(catalog.ContractId),
        "set-releases": list(catalog.Releases),
    }


def _catalog_surface() -> Surface:
    no_input: dict[str, Json] = {"args": {}}

    return Surface(
        name="manifest.catalog",
        path="manifest/catalog.json",
        entry="handover.catalog",
        contract=f"{CONTRACT} \u00a71, \u00a73",
        notes=(
            "Each vector is one public constant of the module. The id names the constant. "
            "The input has no argument.",
            "value is the constant. A vector with an id that starts with `set-` holds each "
            "value of one closed set, in the order of the Python enum.",
            "`retiring` and `arriving` are sets. value is the sorted list.",
        ),
        vectors=tuple(
            accepted(vector_id, no_input, value) for vector_id, value in _catalog_values().items()
        ),
    )


_NOT_CODES: Final = (
    ("empty", ""),
    ("lower-c1", "c1"),
    ("upper-request", "Request"),
    ("final-newline", "request\n"),
    ("final-space", "manifest "),
    ("p6", "P6"),
    ("c5", "C5"),
    ("other-word", "unknown"),
    ("enum-name", "MANIFEST"),
)


def _code_vector(vector_id: str, text: str) -> Vector:
    given = text_input(text)
    try:
        code = RefusalCode(text)
    except ValueError:
        return refused(vector_id, given)

    return accepted(vector_id, given, code)


def _refusal_code_surface() -> Surface:
    codes = tuple((f"code-{code.name.lower()}", code.value) for code in RefusalCode)

    return Surface(
        name="manifest.refusal_code",
        path="manifest/refusal_code.json",
        entry="handover.errors.RefusalCode",
        contract=f"{CONTRACT} \u00a73.2, \u00a710",
        notes=(
            "The input is one text. The entry point is the call of the enum with the text.",
            "The accepted vectors are every member of the enum, in its order. No other text "
            "is a refusal code.",
        ),
        vectors=tuple(_code_vector(*row) for row in (*codes, *_NOT_CODES)),
    )


# --- the request file --------------------------------------------------------

REQUEST_FIELDS: Final[dict[str, object]] = {
    "id": ULID,
    "kind": "release",
    "components": {"chaperone": "2.1.0"},
    "rollback_of": None,
    "requested_by": "agent-control",
    "requester_session": "tui-01K5J7Z9R0P2M4C6H8K1N3V5W7",
    "ts": 1758153590.0,
}

_REQUEST_TEXT: Final = json.dumps(REQUEST_FIELDS)

#: Every releasable component, which is the largest set that a request names.
_EIGHT: Final = {name: "latest" for name in catalog.releasable_names()}
_NINE: Final = {**_EIGHT, "registry-data": "latest"}


@dataclass(frozen=True)
class RequestFile:
    """One input of `parse_request`: an id, the bytes and the id of the file name."""

    id: str
    raw: bytes = b""
    request_id: str = ULID
    #: A long ASCII input, written as repeated parts in place of `raw`.
    parts: tuple[tuple[str, int], ...] = ()

    def data(self) -> bytes:
        return expand(self.parts).encode("ascii") if self.parts else self.raw

    def given(self) -> dict[str, Json]:
        return repeat_input(self.parts) if self.parts else bytes_input(self.raw)


def _body(**fields: object) -> dict[str, object]:
    return {**REQUEST_FIELDS, **fields}


def _req(vector_id: str, **fields: object) -> RequestFile:
    """A request file that is the valid one but for `fields`."""
    return RequestFile(vector_id, json.dumps(_body(**fields)).encode("utf-8"))


def _req_without(vector_id: str, field: str) -> RequestFile:
    body = _body()
    del body[field]

    return RequestFile(vector_id, json.dumps(body).encode("utf-8"))


def _req_raw(vector_id: str, old: str, new: str) -> RequestFile:
    """The valid request file with one piece of its JSON text replaced."""
    if old not in _REQUEST_TEXT:
        raise ValueError(f"{vector_id}: the request text holds no {old}")

    return RequestFile(vector_id, _REQUEST_TEXT.replace(old, new).encode("utf-8"))


def _req_ts(vector_id: str, literal: str) -> RequestFile:
    """The valid request file with this JSON text as its `ts`."""
    return _req_raw(vector_id, '"ts": 1758153590.0', f'"ts": {literal}')


_ROLLBACK: Final = {"kind": "rollback", "rollback_of": OTHER_ULID}

REQUEST_FILES: Final[tuple[RequestFile, ...]] = (
    # --- accepted ---------------------------------------------------------
    _req("full"),
    _req("no-session", requester_session=None),
    _req("latest", components={"attendance": "latest"}),
    _req("eight-components", components=_EIGHT),
    _req("components-not-sorted", components={"noticeboard": "1.0.0", "attendance": "2.0.0"}),
    _req("component-not-in-catalog", components={"nobody": "1.0.0"}),
    _req("component-never-releases", components={"registry-data": "1.0.0"}),
    _req("version-leading-zeros", components={"chaperone": "01.002.0003"}),
    _req("version-30-digits", components={"chaperone": "1.2." + "9" * 30}),
    _req("rollback", **_ROLLBACK),
    _req("rollback-of-own-id", kind="rollback", rollback_of=ULID),
    _req("requested-by-human", requested_by="human"),
    _req("requested-by-ci", requested_by="ci"),
    _req("requested-by-31-chars", requested_by="a" * 31),
    _req("session-one-char", requester_session="s"),
    _req("session-128-chars", requester_session="s" * 128),
    _req("session-dots", requester_session="a..b_-."),
    RequestFile("pretty", json.dumps(REQUEST_FIELDS, indent=2).encode("utf-8") + b"\n"),
    RequestFile("compact", json.dumps(REQUEST_FIELDS, separators=(",", ":")).encode("utf-8")),
    RequestFile("spaces-around", b" \r\n\t" + _REQUEST_TEXT.encode("utf-8") + b"\n \t\r"),
    RequestFile("keys-in-reverse", json.dumps(dict(reversed(REQUEST_FIELDS.items()))).encode()),
    _req_raw("escaped-text", '"agent-control"', '"\\u0061gent-control"'),
    _req_raw("escaped-key", '"kind"', '"k\\u0069nd"'),
    _req_raw("duplicate-key-bad-then-good", '"kind": "release"', '"kind": 5, "kind": "release"'),
    _req_raw(
        "duplicate-component-last-wins",
        '{"chaperone": "2.1.0"}',
        '{"chaperone": "bad", "chaperone": "2.1.0"}',
    ),
    _req_ts("ts-integer", "1758153590"),
    _req_ts("ts-zero", "0"),
    _req_ts("ts-zero-float", "0.0"),
    _req_ts("ts-negative-zero", "-0.0"),
    _req_ts("ts-negative-zero-integer", "-0"),
    _req_ts("ts-fraction", "1758153590.123456"),
    _req_ts("ts-17-digits", "0.30000000000000004"),
    _req_ts("ts-more-digits-than-a-float", "0.1000000000000000055511151231257827"),
    _req_ts("ts-exponent", "1.5e3"),
    _req_ts("ts-exponent-upper", "15E+2"),
    _req_ts("ts-small", "1e-7"),
    _req_ts("ts-smallest", "5e-324"),
    _req_ts("ts-below-smallest", "1e-400"),
    _req_ts("ts-large", "1e22"),
    _req_ts("ts-largest", "1.7976931348623157e308"),
    _req_ts("ts-integer-past-64-bits", "36893488147419103232"),
    _req_ts("ts-integer-40-digits", "1" + "0" * 39),
    _req_ts("ts-rounds-half-even", "9007199254740993"),
    RequestFile(
        "size-at-cap",
        parts=((_REQUEST_TEXT, 1), (" ", MAX_REQUEST_BYTES - len(_REQUEST_TEXT))),
    ),
    RequestFile(
        "id-not-a-ulid",
        json.dumps(_body(id=NOT_A_ULID)).encode("utf-8"),
        NOT_A_ULID,
    ),
    _req("version-arabic-indic", components={"chaperone": ARABIC_INDIC_VERSION}),
    # --- the bytes and the JSON ------------------------------------------
    RequestFile(
        "size-over-cap",
        parts=((_REQUEST_TEXT, 1), (" ", MAX_REQUEST_BYTES - len(_REQUEST_TEXT) + 1)),
    ),
    RequestFile("size-over-cap-not-json", parts=(("x", MAX_REQUEST_BYTES + 1),)),
    RequestFile("bytes-empty", b""),
    RequestFile("bytes-spaces", b"  \n"),
    RequestFile("bytes-not-utf8", b'{"id": "\xff"}'),
    RequestFile("bytes-utf16", _REQUEST_TEXT.encode("utf-16")),
    RequestFile("bytes-bom", b"\xef\xbb\xbf" + _REQUEST_TEXT.encode("utf-8")),
    RequestFile("bytes-nul-after", _REQUEST_TEXT.encode("utf-8") + b"\x00"),
    RequestFile("json-text", b"not json"),
    RequestFile("json-truncated", _REQUEST_TEXT.encode("utf-8")[:-1]),
    RequestFile("json-trailing-text", _REQUEST_TEXT.encode("utf-8") + b" x"),
    RequestFile("json-two-documents", _REQUEST_TEXT.encode("utf-8") * 2),
    RequestFile("json-single-quotes", _REQUEST_TEXT.replace('"', "'").encode("utf-8")),
    RequestFile("json-trailing-comma", _REQUEST_TEXT[:-1].encode("utf-8") + b",}"),
    RequestFile("json-comment", b"// a request\n" + _REQUEST_TEXT.encode("utf-8")),
    _req_raw("json-raw-tab-in-text", '"agent-control"', '"agent\tcontrol"'),
    _req_raw("json-bad-escape", '"agent-control"', '"agent\\qcontrol"'),
    _req_raw("json-short-unicode-escape", '"agent-control"', '"agent\\u00"'),
    _req_raw("json-key-not-text", '"kind":', "5:"),
    _req_ts("json-leading-zero", "01"),
    _req_ts("json-plus-sign", "+1"),
    _req_ts("json-no-fraction-digits", "1."),
    _req_ts("json-no-integer-digits", ".5"),
    _req_ts("json-hex", "0x10"),
    _req_ts("json-no-exponent-digits", "1e"),
    _req_ts("json-upper-true", "True"),
    RequestFile("top-array", b"[" + _REQUEST_TEXT.encode("utf-8") + b"]"),
    RequestFile("top-null", b"null"),
    RequestFile("top-text", b'"release"'),
    RequestFile("top-number", b"7"),
    RequestFile("top-true", b"true"),
    RequestFile("top-nan", b"NaN"),
    RequestFile("top-empty-object", b"{}"),
    # --- the key set (\u00a73.2 rule 2) ---------------------------------------
    *(_req_without(f"missing-{name.replace('_', '-')}", name) for name in REQUEST_FIELDS),
    _req("unknown-key", path="/etc"),
    _req("unknown-key-manifest", manifest_sha256=DIGEST),
    _req_raw("key-upper", '"kind"', '"Kind"'),
    _req_raw("key-lone-surrogate", '"kind"', '"\\ud800"'),
    _req_raw("duplicate-key-good-then-bad", '"kind": "release"', '"kind": "release", "kind": 5'),
    # --- id ---------------------------------------------------------------
    _req("id-other", id=OTHER_ULID),
    _req("id-lower", id=ULID.lower()),
    _req("id-number", id=5),
    _req("id-null", id=None),
    _req("id-list", id=[ULID]),
    _req("id-final-newline", id=ULID + "\n"),
    # --- kind ---------------------------------------------------------------
    _req("kind-other", kind="deploy"),
    _req("kind-upper", kind="Release"),
    _req("kind-final-newline", kind="release\n"),
    _req("kind-number", kind=5),
    _req("kind-null", kind=None),
    _req("kind-true", kind=True),
    _req("kind-list", kind=["release"]),
    _req("kind-object", kind={"release": True}),
    # --- components (\u00a72.3) ------------------------------------------------
    _req("components-empty", components={}),
    _req("components-nine", components=_NINE),
    _req("components-list", components=["chaperone"]),
    _req("components-text", components="chaperone=2.1.0"),
    _req("components-null", components=None),
    _req("components-number", components=1),
    _req("component-name-upper", components={"Chaperone": "2.1.0"}),
    _req("component-name-one-char", components={"c": "2.1.0"}),
    _req("component-name-32-chars", components={"c" * 32: "2.1.0"}),
    _req("component-name-underscore", components={"mcp_servers": "2.1.0"}),
    _req("component-name-empty", components={"": "2.1.0"}),
    _req("component-name-final-newline", components={"chaperone\n": "2.1.0"}),
    _req("component-name-path", components={"../chaperone": "2.1.0"}),
    _req("component-name-not-ascii", components={"chap\u00e9rone": "2.1.0"}),
    _req("component-version-number", components={"chaperone": 2}),
    _req("component-version-float", components={"chaperone": 2.1}),
    _req("component-version-null", components={"chaperone": None}),
    _req("component-version-true", components={"chaperone": True}),
    _req("component-version-object", components={"chaperone": {"to": "2.1.0"}}),
    _req("component-version-two-numbers", components={"chaperone": "2.1"}),
    _req("component-version-four-numbers", components={"chaperone": "2.1.0.1"}),
    _req("component-version-v", components={"chaperone": "v2.1.0"}),
    _req("component-version-latest-upper", components={"chaperone": "Latest"}),
    _req("component-version-latest-space", components={"chaperone": "latest "}),
    _req("component-version-final-newline", components={"chaperone": "2.1.0\n"}),
    _req("component-version-empty", components={"chaperone": ""}),
    _req("component-version-range", components={"chaperone": ">=2.1.0"}),
    _req("component-version-digest", components={"chaperone": DIGEST}),
    _req("components-first-bad-in-name-order", components={"zeta": 5, "Alpha": "1.0.0"}),
    _req("components-version-before-next-name", components={"alpha": 5, "beta!": "1.0.0"}),
    _req("components-nine-and-bad", components={**_NINE, "X": 1}),
    # --- rollback_of ------------------------------------------------------
    _req("rollback-of-on-release", rollback_of=OTHER_ULID),
    _req("rollback-of-unset", kind="rollback"),
    _req("rollback-of-lower", kind="rollback", rollback_of=OTHER_ULID.lower()),
    _req("rollback-of-25-chars", kind="rollback", rollback_of=OTHER_ULID[:-1]),
    _req("rollback-of-letter-u", kind="rollback", rollback_of="U" + OTHER_ULID[1:]),
    _req("rollback-of-number", kind="rollback", rollback_of=5),
    _req("rollback-of-bad-on-release", rollback_of="x"),
    _req("rollback-of-false", rollback_of=False),
    # --- requested_by and requester_session -------------------------------
    _req("requested-by-upper", requested_by="Human"),
    _req("requested-by-one-char", requested_by="h"),
    _req("requested-by-32-chars", requested_by="a" * 32),
    _req("requested-by-underscore", requested_by="agent_control"),
    _req("requested-by-final-newline", requested_by="human\n"),
    _req("requested-by-number", requested_by=5),
    _req("requested-by-null", requested_by=None),
    _req("requested-by-lone-surrogate", requested_by="\ud800"),
    _req("session-empty", requester_session=""),
    _req("session-129-chars", requester_session="s" * 129),
    _req("session-leading-dot", requester_session=".hidden"),
    _req("session-slash", requester_session="a/b"),
    _req("session-number", requester_session=5),
    _req("session-false", requester_session=False),
    # --- ts ---------------------------------------------------------------
    _req("ts-text", ts="1758153590.0"),
    _req("ts-true", ts=True),
    _req("ts-false", ts=False),
    _req("ts-null", ts=None),
    _req("ts-list", ts=[1]),
    _req("ts-object", ts={}),
    _req("ts-negative", ts=-1),
    _req("ts-negative-small", ts=-1e-9),
    _req_ts("ts-nan", "NaN"),
    _req_ts("ts-infinity", "Infinity"),
    _req_ts("ts-negative-infinity", "-Infinity"),
    _req_ts("ts-overflows-to-infinity", "1e999"),
    _req_ts("ts-nested-64", "[" * 64 + "]" * 64),
    # --- the order of the checks -----------------------------------------
    _req("order-keys-before-id", id=OTHER_ULID, extra=1),
    _req("order-id-before-kind", id=OTHER_ULID, kind="deploy"),
    _req("order-kind-before-components", kind="deploy", components={}),
    _req("order-components-before-rollback", components={}, rollback_of=5),
    _req("order-rollback-before-requested-by", rollback_of=OTHER_ULID, requested_by="H"),
    _req("order-requested-by-before-session", requested_by="H", requester_session=""),
    _req("order-session-before-ts", requester_session="", ts="x"),
)


@contextmanager
def _requests_dir() -> Generator[str]:
    with tempfile.TemporaryDirectory() as root:
        yield root


def _written(request: Request, requests_dir: str) -> bytes:
    """The bytes that `file_request` writes for one request."""
    path = Path(file_request(request, requests_dir))
    try:
        return path.read_bytes()
    finally:
        path.unlink()


def _request_fields(request: Request, requests_dir: str) -> dict[str, object]:
    """What an accepted request vector holds beside its value."""
    return {
        "ts_bits": _bits(request.ts),
        "output": bytes_input(_written(request, requests_dir)),
    }


def _parse_vector(document: RequestFile, requests_dir: str) -> Vector:
    given = document.given()
    raw = document.data()
    params = {"request_id": document.request_id}
    outcome = _run(lambda: parse_request(raw, document.request_id))
    if isinstance(outcome, Raised):
        return raised(document.id, given, outcome.exc, params=params)

    if isinstance(outcome, Refusal):
        return refused(document.id, given, _refusal(outcome), params=params)

    return accepted(
        document.id, given, outcome, params=params, **_request_fields(outcome, requests_dir)
    )


_REQUEST_NOTES: Final = (
    "value is the parsed request. components holds each pair of a component name and a "
    "version, in the order of the names.",
    "ts_bits is value.ts as the 16 hexadecimal digits of its IEEE 754 bits, most significant "
    "first. Compare ts with ts_bits: a JSON reader can round the number in value.",
    "output is the bytes that handover.requester.file.file_request writes for the parsed "
    "request. The requester and the executor write the same bytes for one request.",
    "refusal holds the check, the subject and the detail of the refusal, as the ledger holds them.",
)


def _parse_surface() -> Surface:
    with _requests_dir() as requests_dir:
        vectors = tuple(_parse_vector(document, requests_dir) for document in REQUEST_FILES)

    return Surface(
        name="manifest.request.parse",
        path="manifest/request_parse.json",
        entry="handover.executor.request.parse_request",
        contract=f"{CONTRACT} \u00a79, stage7-releases.md \u00a72.3, \u00a73.2",
        notes=(
            "The input is the bytes of one request file. params.request_id is the id of the "
            "file name, which the entry point takes as its second argument.",
            *_REQUEST_NOTES,
            "The entry point does not check params.request_id against the grammar of a ULID. "
            "Its caller reads the id from a file name that it checked.",
        ),
        vectors=vectors,
    )


@dataclass(frozen=True)
class Plan:
    """One call of `plan_request`."""

    id: str
    components: Mapping[str, str]
    requested_by: str = "human"
    now_bits: str = _bits(1758153590.0)
    kind: str = "release"
    rollback_of: str | None = None
    requester_session: str | None = None
    request_id: str = ULID

    def args(self) -> dict[str, object]:
        return {
            "components": dict(self.components),
            "requested_by": self.requested_by,
            "now_bits": self.now_bits,
            "kind": self.kind,
            "rollback_of": self.rollback_of,
            "requester_session": self.requester_session,
            "request_id": self.request_id,
        }


_ONE: Final = {"chaperone": "2.1.0"}
_LONG_VERSION: Final = "1.2." + "3" * 600

PLANS: Final[tuple[Plan, ...]] = (
    Plan("one", _ONE),
    Plan("latest", {"attendance": "latest"}),
    Plan("not-sorted", {"noticeboard": "1.0.0", "attendance": "latest", "chaperone": "0.0.1"}),
    Plan("eight", _EIGHT),
    Plan("requested-by-family", _ONE, requested_by="agent-control"),
    Plan("with-session", _ONE, requester_session="tui-01K5J7Z9R0P2M4C6H8K1N3V5W7"),
    Plan("rollback", _ONE, kind="rollback", rollback_of=OTHER_ULID),
    Plan("now-zero", _ONE, now_bits=_bits(0.0)),
    Plan("now-negative-zero", _ONE, now_bits=_bits(-0.0)),
    Plan("now-fraction", _ONE, now_bits=_bits(1758153590.123456)),
    Plan("now-17-digits", _ONE, now_bits=_bits(1758153590.1234567)),
    Plan("now-small", _ONE, now_bits=_bits(1e-7)),
    Plan("now-large", _ONE, now_bits=_bits(1e22)),
    Plan("long-version", _ONE | {"attendance": _LONG_VERSION}),
    Plan("id-not-a-ulid", _ONE, request_id=NOT_A_ULID),
    Plan("empty", {}),
    Plan("nine", _NINE),
    Plan("name-upper", {"Chaperone": "2.1.0"}),
    Plan("name-empty", {"": "2.1.0"}),
    Plan("version-two-numbers", {"chaperone": "2.1"}),
    Plan("version-latest-upper", {"chaperone": "LATEST"}),
    Plan("version-not-ascii", {"chaperone": "2.1.\u00b2"}),
    Plan("requested-by-upper", _ONE, requested_by="Human"),
    Plan("requested-by-empty", _ONE, requested_by=""),
    Plan("requested-by-quote", _ONE, requested_by='hu"man'),
    Plan("session-empty", _ONE, requester_session=""),
    Plan("session-129-chars", _ONE, requester_session="s" * 129),
    Plan("kind-other", _ONE, kind="deploy"),
    Plan("rollback-of-on-release", _ONE, rollback_of=OTHER_ULID),
    Plan("rollback-of-unset", _ONE, kind="rollback"),
    Plan("rollback-of-lower", _ONE, kind="rollback", rollback_of=OTHER_ULID.lower()),
    Plan("now-negative", _ONE, now_bits=_bits(-1.0)),
    Plan("now-nan", _ONE, now_bits=_bits(float("nan"))),
    Plan("now-infinity", _ONE, now_bits=_bits(float("inf"))),
    Plan("over-the-size-cap", {name: _LONG_VERSION for name in catalog.releasable_names()}),
)


def _plan_vector(plan: Plan, requests_dir: str) -> Vector:
    given: dict[str, Json] = {"args": _plan_args(plan)}
    outcome = _run(
        lambda: plan_request(
            plan.components,
            requested_by=plan.requested_by,
            now=_float(plan.now_bits),
            kind=plan.kind,
            rollback_of=plan.rollback_of,
            requester_session=plan.requester_session,
            request_id=plan.request_id,
        )
    )
    if isinstance(outcome, Raised):
        return raised(plan.id, given, outcome.exc)

    if isinstance(outcome, Refusal):
        return refused(plan.id, given, _refusal(outcome))

    return accepted(plan.id, given, outcome, **_request_fields(outcome, requests_dir))


def _plan_args(plan: Plan) -> dict[str, Json]:
    args: dict[str, Json] = {}
    for key, value in plan.args().items():
        if isinstance(value, dict):
            args[key] = dict(cast("dict[str, Json]", value))
        elif value is None or isinstance(value, str):
            args[key] = value
        else:
            raise TypeError(f"{plan.id}: no JSON form for the argument {key}")

    return args


def _plan_surface() -> Surface:
    with _requests_dir() as requests_dir:
        vectors = tuple(_plan_vector(plan, requests_dir) for plan in PLANS)

    return Surface(
        name="manifest.request.plan",
        path="manifest/request_plan.json",
        entry="handover.requester.file.plan_request",
        contract=f"{CONTRACT} \u00a79, stage7-releases.md \u00a72.3",
        notes=(
            "The input is the arguments of one call. now_bits is the argument `now` as the 16 "
            "hexadecimal digits of its IEEE 754 bits, most significant first.",
            "request_id is always given. The entry point then mints no id, so the output does "
            "not change between two runs. manifest.request.ulid covers the mint.",
            "The order of the keys of `components` in this file is not the order of the call. "
            "The entry point gives the same result for each order.",
            *_REQUEST_NOTES,
            "The entry point does not check request_id against the grammar of a ULID.",
        ),
        vectors=vectors,
    )


#: The count of random bytes in a ULID.
ENTROPY_BYTES: Final = 10

#: The largest time that fits the 48 bits of a ULID, in milliseconds.
ULID_MS_MAX: Final = 2**48 - 1

ULIDS: Final[tuple[tuple[str, float, bytes], ...]] = (
    ("zero", 0.0, bytes(ENTROPY_BYTES)),
    ("zero-time-full-entropy", 0.0, b"\xff" * ENTROPY_BYTES),
    ("one-millisecond", 0.001, bytes(ENTROPY_BYTES)),
    ("below-one-millisecond", 0.0009999, bytes(ENTROPY_BYTES)),
    ("a-clock", 1758153590.123456, bytes(range(ENTROPY_BYTES))),
    ("a-clock-other-entropy", 1758153590.123456, bytes(range(ENTROPY_BYTES, 0, -1))),
    ("product-rounds", 1.0005, b"\x80" + bytes(ENTROPY_BYTES - 1)),
    ("whole-seconds", 1758153590.0, b"\x01" * ENTROPY_BYTES),
    ("largest-time", ULID_MS_MAX / 1000, b"\xff" * ENTROPY_BYTES),
    ("every-letter", 1469918176.385, bytes.fromhex("0123456789abcdef0f1e")),
)


def _ulid_vector(vector_id: str, now: float, entropy: bytes) -> Vector:
    if int(now * 1000) > ULID_MS_MAX:
        raise ValueError(f"{vector_id}: the time does not fit a ULID")

    given: dict[str, Json] = {"args": {"now_bits": _bits(now), "entropy": entropy.hex()}}
    outcome = attempt(lambda: new_ulid(now, lambda count: entropy[:count]))
    if isinstance(outcome, Raised):
        return raised(vector_id, given, outcome.exc)

    return accepted(vector_id, given, {"ulid": outcome})


def _ulid_surface() -> Surface:
    return Surface(
        name="manifest.request.ulid",
        path="manifest/request_ulid.json",
        entry="handover.requester.file.new_ulid",
        contract="contract 02 \u00a72",
        notes=(
            "The input is the arguments of one call. now_bits is the time in seconds, as the "
            "16 hexadecimal digits of its IEEE 754 bits. entropy is the ten random bytes, as "
            "20 hexadecimal digits.",
            "value.ulid is the id. Its first ten characters hold the time in milliseconds: "
            "the product of the time and 1000, with the fraction cut.",
            "Each time fits the 48 bits of a ULID. The entry point does not check that.",
        ),
        vectors=tuple(_ulid_vector(*row) for row in ULIDS),
    )


# --- the live-state document -------------------------------------------------

FULL_STATE: Final[dict[str, object]] = {
    "live": {"chaperone": "2.0.3", "attendance": None, "registry-data": None},
    "provided": {"pep-grant": "2.0", "session-api": "1.4"},
    "latest": {"chaperone": "2.1.0"},
    "facts": {
        "chaperone": {"sha": SHA, "input_digest": DIGEST, "artifact_digest": None},
        "playpen": {"sha": SHA, "input_digest": DIGEST, "artifact_digest": OTHER_DIGEST},
    },
}


@dataclass(frozen=True)
class StateFile:
    """One input of `parse_state`: an id and the text."""

    id: str
    text: str = ""
    parts: tuple[tuple[str, int], ...] = ()


def _state(vector_id: str, **keys: object) -> StateFile:
    return StateFile(vector_id, json.dumps(keys))


def _facts(vector_id: str, fact: object, name: str = "chaperone") -> StateFile:
    return _state(vector_id, facts={name: fact})


_FULL_STATE_TEXT: Final = json.dumps(FULL_STATE)

STATE_FILES: Final[tuple[StateFile, ...]] = (
    # --- accepted ---------------------------------------------------------
    StateFile("full", _FULL_STATE_TEXT),
    StateFile("empty-object", "{}"),
    _state("empty-maps", live={}, provided={}, latest={}, facts={}),
    _state("live-only", live={"chaperone": "2.0.3"}),
    _state("live-null", live={"attendance": None}),
    _state("live-every-component", live={row.name: "1.0.0" for row in catalog.CATALOG}),
    _state("live-leading-zeros", live={"chaperone": "02.00.03"}),
    _state("provided-every-contract", provided={str(one): "1.0" for one in ContractId}),
    _state("provided-leading-zeros", provided={"pep-grant": "02.010"}),
    _state("provided-past-64-bits", provided={"pep-grant": "9" * 30 + ".0"}),
    _state("latest-only", latest={"chaperone": "2.1.0"}),
    _facts("facts-all-null", {"sha": None, "input_digest": None, "artifact_digest": None}),
    _facts("facts-no-key", {}),
    _facts("facts-sha-only", {"sha": SHA}),
    StateFile("pretty", json.dumps(FULL_STATE, indent=2) + "\n"),
    StateFile("spaces-around", " \r\n\t" + _FULL_STATE_TEXT + "\n "),
    StateFile(
        "duplicate-key-last-wins",
        '{"live": {"chaperone": "bad"}, "live": {"chaperone": "1.0.0"}}',
    ),
    StateFile("duplicate-name-last-wins", '{"live": {"chaperone": "1.0.0", "chaperone": null}}'),
    StateFile("escaped-key", '{"l\\u0069ve": {"ch\\u0061perone": "1.0.0"}}'),
    StateFile(
        "size-at-cap", parts=((_FULL_STATE_TEXT, 1), (" ", MAX_STATE_BYTES - len(_FULL_STATE_TEXT)))
    ),
    _state("live-arabic-indic", live={"chaperone": ARABIC_INDIC_VERSION}),
    _state("provided-arabic-indic", provided={"pep-grant": "\u0662.\u0660"}),
    # --- the text and the JSON -------------------------------------------
    StateFile(
        "size-over-cap",
        parts=((_FULL_STATE_TEXT, 1), (" ", MAX_STATE_BYTES - len(_FULL_STATE_TEXT) + 1)),
    ),
    StateFile(
        "size-two-byte-chars-over-cap",
        parts=(('{"x": "', 1), ("\u00e9", MAX_STATE_BYTES // 2), ('"}', 1)),
    ),
    StateFile("text-empty", ""),
    StateFile("text-bom", "\ufeff{}"),
    StateFile("json-text", "not json"),
    StateFile("json-truncated", _FULL_STATE_TEXT[:-1]),
    StateFile("json-trailing-text", _FULL_STATE_TEXT + " x"),
    StateFile("json-two-documents", "{}{}"),
    StateFile("json-single-quotes", "{'live': {}}"),
    StateFile("json-trailing-comma", '{"live": {},}'),
    StateFile("top-array", "[]"),
    StateFile("top-null", "null"),
    StateFile("top-text", '"live"'),
    StateFile("top-number", "7"),
    StateFile("top-nan", "NaN"),
    # --- the key set ------------------------------------------------------
    _state("unknown-field", approved=True),
    _state("unknown-fields-sorted", zeta=1, alpha=2),
    StateFile("unknown-field-unsafe", '{"not a token": 1}'),
    StateFile("unknown-field-lone-surrogate", '{"\\ud800": 1}'),
    _state("unknown-field-and-bad-live", live=5, approved=True),
    # --- live and latest --------------------------------------------------
    _state("live-list", live=["chaperone"]),
    _state("live-null-map", live=None),
    _state("live-text", live="chaperone"),
    StateFile("live-nan", '{"live": NaN}'),
    _state("live-unknown-name", live={"nobody": "1.0.0"}),
    _state("live-first-bad-in-file-order", live={"nobody": "1.0.0", "chaperone": 5}),
    _state("live-first-bad-in-file-order-reversed", live={"chaperone": 5, "nobody": "1.0.0"}),
    StateFile(
        "live-duplicate-keeps-first-place",
        '{"live": {"chaperone": 5, "nobody": "1.0.0", "chaperone": "1.0.0"}}',
    ),
    _state("live-unknown-name-unsafe", live={"no body": "1.0.0"}),
    _state("live-name-upper", live={"Chaperone": "1.0.0"}),
    _state("live-version-two-numbers", live={"chaperone": "1.0"}),
    _state("live-version-number", live={"chaperone": 1}),
    _state("live-version-false", live={"chaperone": False}),
    _state("live-version-list", live={"chaperone": ["1.0.0"]}),
    _state("live-version-latest", live={"chaperone": "latest"}),
    _state("live-version-final-newline", live={"chaperone": "1.0.0\n"}),
    _state("live-nested-64", live={"chaperone": json.loads("[" * 64 + "]" * 64)}),
    _state("latest-null", latest={"chaperone": None}),
    _state("latest-unknown-name", latest={"nobody": "1.0.0"}),
    _state("latest-version-v", latest={"chaperone": "v1.0.0"}),
    _state("latest-list", latest=[]),
    # --- provided ---------------------------------------------------------
    _state("provided-unknown-contract", provided={"other": "1.0"}),
    _state("provided-contract-upper", provided={"Pep-Grant": "1.0"}),
    _state("provided-component-name", provided={"chaperone": "1.0"}),
    _state("provided-one-number", provided={"pep-grant": "2"}),
    _state("provided-three-numbers", provided={"pep-grant": "2.0.1"}),
    _state("provided-number", provided={"pep-grant": 2.0}),
    _state("provided-null", provided={"pep-grant": None}),
    _state("provided-list", provided={"pep-grant": [2, 0]}),
    _state("provided-final-newline", provided={"pep-grant": "2.0\n"}),
    _state("provided-text", provided="pep-grant"),
    # --- facts ------------------------------------------------------------
    _facts("facts-unknown-name", {"sha": SHA}, "nobody"),
    _facts("facts-unknown-field", {"sha": SHA, "tag": "chaperone-v1.0.0"}),
    _facts("facts-list", [SHA]),
    _facts("facts-null", None),
    _facts("facts-sha-upper", {"sha": SHA.upper()}),
    _facts("facts-sha-39-chars", {"sha": SHA[:-1]}),
    _facts("facts-sha-41-chars", {"sha": SHA + "0"}),
    _facts("facts-sha-number", {"sha": 5}),
    _facts("facts-sha-false", {"sha": False}),
    _facts("facts-sha-final-newline", {"sha": SHA + "\n"}),
    _facts("facts-digest-no-prefix", {"input_digest": DIGEST.removeprefix("sha256:")}),
    _facts("facts-digest-upper-prefix", {"input_digest": DIGEST.replace("sha256", "SHA256")}),
    _facts("facts-digest-upper-hex", {"input_digest": DIGEST.upper().replace("SHA", "sha")}),
    _facts("facts-digest-63-chars", {"input_digest": DIGEST[:-1]}),
    _facts("facts-digest-sha512", {"input_digest": DIGEST.replace("sha256", "sha512")}),
    _facts("facts-digest-number", {"input_digest": 5}),
    _facts("facts-artifact-digest-bad", {"artifact_digest": "latest"}),
    _facts("facts-artifact-digest-false", {"artifact_digest": False}),
    _state("facts-text", facts="chaperone"),
    # --- the order of the checks -----------------------------------------
    _state("order-live-before-provided", live=5, provided=5),
    _state("order-provided-before-latest", provided=5, latest=5),
    _state("order-latest-before-facts", latest=5, facts=5),
    _facts("order-sha-before-digest", {"sha": 5, "input_digest": 5}),
    _facts("order-digest-before-artifact", {"input_digest": 5, "artifact_digest": 5}),
)


def _state_value(state: ReleaseState) -> dict[str, object]:
    """A live state with each contract version as its two numbers."""
    return {
        "live": state.live,
        "provided": {str(contract): list(version) for contract, version in state.provided.items()},
        "latest": state.latest,
        "facts": state.facts,
    }


def _state_vector(document: StateFile) -> Vector:
    given = repeat_input(document.parts) if document.parts else text_input(document.text)
    text = expand(document.parts) if document.parts else document.text
    outcome = _run(lambda: parse_state(text, STATE_SUBJECT))
    if isinstance(outcome, Raised):
        return raised(document.id, given, outcome.exc)

    if isinstance(outcome, Refusal):
        return refused(document.id, given, _refusal(outcome))

    return accepted(document.id, given, _state_value(outcome))


def _state_surface() -> Surface:
    return Surface(
        name="manifest.state",
        path="manifest/state.json",
        entry="handover.state.parse_state",
        contract=f"{CONTRACT} \u00a711",
        notes=(
            "The input is the text of one live-state document. context.subject is the label "
            "that the entry point takes as its second argument.",
            "value is the parsed document. Each key is present. value.provided holds each "
            "contract version as its two numbers, which is how the Python code holds it.",
            "refusal holds the check, the subject and the detail of the refusal.",
        ),
        context={"subject": STATE_SUBJECT},
        vectors=tuple(_state_vector(document) for document in STATE_FILES),
    )


# --- the resolved manifest ---------------------------------------------------

type Row = tuple[str, str, str | None, str | None]
type ConsumerRow = tuple[str, int, int]
type Contract = tuple[str, str, int, int, tuple[ConsumerRow, ...]]
type Fact = tuple[str | None, str | None, str | None]


@dataclass(frozen=True)
class Resolved:
    """One call of `build_document`."""

    id: str
    components: tuple[Row, ...] = ()
    order: tuple[str, ...] = ()
    contracts: tuple[Contract, ...] = ()
    facts: Mapping[str, Fact] | None = None
    release_id: str = ULID
    requested_by: str = "agent-control"
    resolved_at_bits: str = _bits(1758153600.0)

    def args(self) -> dict[str, Json]:
        facts = self.facts or {}

        return {
            "components": [
                {"name": name, "action": action, "from_version": was, "to_version": to}
                for name, action, was, to in self.components
            ],
            "order": list(self.order),
            "contracts": [
                {
                    "contract": contract,
                    "provider": provider,
                    "major": major,
                    "minor": minor,
                    "consumers": [
                        {"name": name, "major": floor_major, "min_minor": floor}
                        for name, floor_major, floor in consumers
                    ],
                }
                for contract, provider, major, minor, consumers in self.contracts
            ],
            "facts": {
                name: {"sha": sha, "input_digest": digest, "artifact_digest": artifact}
                for name, (sha, digest, artifact) in facts.items()
            },
            "id": self.release_id,
            "requested_by": self.requested_by,
            "resolved_at_bits": self.resolved_at_bits,
        }

    def resolution(self) -> Resolution:
        components = tuple(
            ResolvedComponent(name, Action(action), was, to)
            for name, action, was, to in self.components
        )
        contracts = tuple(
            ContractRow(
                ContractId(contract),
                provider,
                major,
                minor,
                tuple(Consumer(*consumer) for consumer in consumers),
            )
            for contract, provider, major, minor, consumers in self.contracts
        )
        deploying = frozenset(row.name for row in components if row.action is Action.DEPLOY)

        return Resolution(components, self.order, contracts, deploying)

    def state(self) -> ReleaseState:
        facts = self.facts or {}

        return ReleaseState(facts={name: SourceFacts(*fact) for name, fact in facts.items()})


_UNCHANGED: Final[tuple[Row, ...]] = (
    ("attendance", "unchanged", "1.4.7", "1.4.7"),
    ("caregiver", "unchanged", "1.2.0", "1.2.0"),
    ("chaperone", "deploy", "2.0.3", "2.1.0"),
    ("handover", "unchanged", "0.3.0", "0.3.0"),
    ("infra", "unchanged", None, None),
    ("mcp-servers", "unchanged", "0.9.3", "0.9.3"),
    ("noticeboard", "unchanged", "0.7.0", "0.7.0"),
    ("playpen", "unchanged", "3.1.2", "3.1.2"),
    ("registry-data", "unchanged", None, None),
)

_CONTRACTS: Final[tuple[Contract, ...]] = (
    ("channel", "playpen", 1, 3, (("attendance", 1, 3),)),
    ("component-manifest", "handover", 0, 6, ()),
    ("family-file", "caregiver", 1, 0, ()),
    ("manager-status", "caregiver", 1, 1, (("noticeboard", 1, 1),)),
    (
        "pep-grant",
        "chaperone",
        2,
        1,
        (("attendance", 2, 0), ("caregiver", 2, 0), ("noticeboard", 2, 0)),
    ),
    ("session-api", "attendance", 1, 4, (("chaperone", 1, 2), ("noticeboard", 1, 4))),
)

_ONE_ROW: Final[tuple[Row, ...]] = (("chaperone", "deploy", "2.0.3", "2.1.0"),)

_FIRST_INSTALL: Final[tuple[Row, ...]] = tuple(
    (name, "unchanged", None, None)
    if name in {"infra", "registry-data"}
    else (name, "deploy", None, "1.0.0")
    for name, _, _, _ in _UNCHANGED
)

#: A time for each form that Python writes a float in.
_TIMES: Final[tuple[tuple[str, float], ...]] = (
    ("zero", 0.0),
    ("negative-zero", -0.0),
    ("negative", -1.5),
    ("fraction", 1758153600.123456),
    ("17-digits", 1758153600.1234567),
    ("one-tenth", 0.1),
    ("one-third", 1 / 3),
    ("smallest-plain", 0.0001),
    ("largest-exponent-form", 9.999e-05),
    ("small-exponent", 1.5e-07),
    ("smallest", 5e-324),
    ("largest-plain", 9999999999999998.0),
    ("smallest-exponent-form", 1e16),
    ("exponent-with-digits", 1.2345678901234568e16),
    ("large", 1e22),
    ("larger", 1.5e300),
    ("largest", 1.7976931348623157e308),
    ("whole-15-digits", 123456789012345.0),
    ("half", 0.5),
    ("two-to-the-53", 9007199254740992.0),
)

RESOLVED: Final[tuple[Resolved, ...]] = (
    Resolved(
        "release-one",
        _UNCHANGED,
        ("chaperone",),
        _CONTRACTS,
        {"chaperone": (SHA, DIGEST, None)},
    ),
    Resolved(
        "release-a-set",
        (
            ("attendance", "deploy", "1.4.7", "1.5.0"),
            *_UNCHANGED[1:7],
            ("playpen", "deploy", "3.1.2", "3.2.0"),
            _UNCHANGED[8],
        ),
        ("playpen", "chaperone", "attendance"),
        _CONTRACTS,
        {
            "attendance": (SHA, DIGEST, None),
            "chaperone": (SHA, OTHER_DIGEST, None),
            "playpen": (SHA, DIGEST, OTHER_DIGEST),
        },
    ),
    Resolved(
        "first-install",
        _FIRST_INSTALL,
        tuple(row[0] for row in _FIRST_INSTALL if row[1] == "deploy"),
        (),
        {row[0]: (SHA, DIGEST, None) for row in _FIRST_INSTALL if row[1] == "deploy"},
    ),
    Resolved("restore-row", (("chaperone", "restore", "2.1.0", "2.0.3"),), ("chaperone",)),
    Resolved("no-facts", _UNCHANGED, ("chaperone",), _CONTRACTS),
    Resolved("facts-all-null", _ONE_ROW, ("chaperone",), (), {"chaperone": (None, None, None)}),
    Resolved("facts-for-no-row", _ONE_ROW, ("chaperone",), (), {"playpen": (SHA, DIGEST, DIGEST)}),
    Resolved("empty"),
    Resolved("contract-numbers-0-and-999", _ONE_ROW, (), (("channel", "playpen", 0, 999, ()),)),
    Resolved("requested-by-human", _ONE_ROW, ("chaperone",), requested_by="human"),
    Resolved("requested-by-ci", _ONE_ROW, ("chaperone",), requested_by="ci"),
    Resolved("other-id", _ONE_ROW, ("chaperone",), release_id=OTHER_ULID),
    Resolved("version-leading-zeros", (("chaperone", "deploy", "02.00.03", "2.01.0"),)),
    *(
        Resolved(f"resolved-at-{name}", _ONE_ROW, ("chaperone",), resolved_at_bits=_bits(time))
        for name, time in _TIMES
    ),
    Resolved("resolved-at-nan", _ONE_ROW, resolved_at_bits=_bits(float("nan"))),
    Resolved("resolved-at-infinity", _ONE_ROW, resolved_at_bits=_bits(float("inf"))),
    Resolved("resolved-at-negative-infinity", _ONE_ROW, resolved_at_bits=_bits(float("-inf"))),
    Resolved("version-arabic-indic", (("chaperone", "deploy", None, ARABIC_INDIC_VERSION),)),
    Resolved("id-lower", _ONE_ROW, release_id=ULID.lower()),
    Resolved("id-25-chars", _ONE_ROW, release_id=ULID[:-1]),
    Resolved("id-final-newline", _ONE_ROW, release_id=ULID + "\n"),
    Resolved("id-empty", _ONE_ROW, release_id=""),
    Resolved("requested-by-upper", _ONE_ROW, requested_by="Human"),
    Resolved("requested-by-one-char", _ONE_ROW, requested_by="h"),
    Resolved("requested-by-32-chars", _ONE_ROW, requested_by="a" * 32),
    Resolved("requested-by-final-newline", _ONE_ROW, requested_by="human\n"),
    Resolved("id-bad-and-requested-by-bad", _ONE_ROW, release_id="x", requested_by="X"),
)


def _resolved_vector(case: Resolved) -> Vector:
    given: dict[str, Json] = {"args": case.args()}
    outcome = _run(
        lambda: build_document(
            case.resolution(),
            case.state(),
            case.release_id,
            case.requested_by,
            _float(case.resolved_at_bits),
        )
    )
    if isinstance(outcome, Raised):
        return raised(case.id, given, outcome.exc)

    if isinstance(outcome, Refusal):
        return refused(case.id, given, _refusal(outcome))

    hashed = {key: value for key, value in outcome.items() if key != "manifest_sha256"}
    if manifest_hash(hashed) != outcome["manifest_sha256"]:
        raise ValueError(f"{case.id}: the hash is not over every other field")

    return accepted(case.id, given, outcome, output=text_input(canonical_json(hashed)))


def _resolved_surface() -> Surface:
    return Surface(
        name="manifest.resolved",
        path="manifest/resolved.json",
        entry="handover.resolve.build_document",
        contract=f"{CONTRACT} \u00a79",
        notes=(
            "The input is the arguments of one call. components, order and contracts are the "
            "fields of the Resolution. facts is the `facts` of the ReleaseState.",
            "resolved_at_bits is the argument `resolved_at` as the 16 hexadecimal digits of "
            "its IEEE 754 bits, most significant first. The argument is a float in each "
            "vector, as each caller gives one.",
            "value is the resolved manifest. Do not compare value.resolved_at as a number: a "
            "JSON reader can round it. output holds its exact text.",
            "output is the text that the hash is over: handover.resolve.canonical_json of "
            "each field but manifest_sha256. Its UTF-8 bytes are the input of SHA-256.",
            "value.manifest_sha256 is `sha256:` and the hash of output in lower-case hex.",
            "The entry point checks the id and requested_by. It does not check another "
            "argument: the resolver made each one.",
        ),
        vectors=tuple(_resolved_vector(case) for case in RESOLVED),
    )


# --- the gate and the summary ------------------------------------------------

GATES: Final[tuple[tuple[str, str, str], ...]] = (
    ("one", DIGEST, ULID),
    ("other-hash", OTHER_DIGEST, ULID),
    ("other-id", DIGEST, OTHER_ULID),
    ("id-of-digits", DIGEST, "0" * 26),
    ("id-of-letters", DIGEST, "ZZZZZZZZZZZZZZZZZZZZZZZZZZ"),
)


def _gate_vector(vector_id: str, manifest_sha256: str, release_id: str) -> Vector:
    given: dict[str, Json] = {"args": {"manifest_sha256": manifest_sha256, "id": release_id}}
    gate = approval.gate_id(manifest_sha256)
    action_id = approval.action_id_of(release_id)
    action = approval.release_action(action_id, gate)
    value = {
        "gate": gate,
        "action_id": action_id,
        "action": action,
        "action_matches": approval.ACTION_RE.fullmatch(action) is not None,
    }

    return accepted(vector_id, given, value)


def _gate_surface() -> Surface:
    return Surface(
        name="manifest.gate",
        path="manifest/gate.json",
        entry="handover.executor.approval.gate_id",
        contract="stage7-releases.md \u00a72.5",
        notes=(
            "The input is the hash of one resolved manifest and the id of its request.",
            "value.gate is gate_id of the hash. value.action_id is action_id_of of the id. "
            "value.action is release_action of the two. value.action_matches says whether "
            "ACTION_RE matches the whole action.",
        ),
        vectors=tuple(_gate_vector(*row) for row in GATES),
    )


_SUMMARY_FIELDS: Final = (
    "review",
    "components",
    "contracts",
    "restarts",
    "restore",
    "requested_by",
    "manifest",
)

_SHORT_SUMMARY: Final[dict[str, str]] = {
    "review": "safe: every manifest verified",
    "components": "chaperone 2.0.3 \u2192 2.1.0",
    "contracts": "6 contracts satisfied",
    "restarts": "creche-chaperone.service",
    "restore": "automatic",
    "requested_by": "agent-control",
    "manifest": "0123456789ab",
}

SUMMARIES: Final[tuple[tuple[str, dict[str, str]], ...]] = (
    ("short", _SHORT_SUMMARY),
    ("empty-fields", dict.fromkeys(_SUMMARY_FIELDS, "")),
    ("120-chars", _SHORT_SUMMARY | {"review": "r" * 120}),
    ("121-chars", _SHORT_SUMMARY | {"review": "r" * 121}),
    ("every-field-long", dict.fromkeys(_SUMMARY_FIELDS, "x" * 300)),
    ("120-two-byte-chars", _SHORT_SUMMARY | {"components": "\u00e9" * 120}),
    ("121-two-byte-chars", _SHORT_SUMMARY | {"components": "\u00e9" * 121}),
    ("121-astral-chars", _SHORT_SUMMARY | {"components": "\U0001f600" * 121}),
    ("cut-inside-arrow", _SHORT_SUMMARY | {"components": "c" * 118 + "\u2192\u2192\u2192"}),
    ("line-breaks", _SHORT_SUMMARY | {"restarts": "a.service\nb.service\n" * 8}),
)


def _summary_vector(vector_id: str, fields: dict[str, str]) -> Vector:
    given: dict[str, Json] = {"args": {name: fields[name] for name in _SUMMARY_FIELDS}}
    summary = approval.Summary(**fields)

    return accepted(vector_id, given, summary.as_dict())


def _summary_surface() -> Surface:
    return Surface(
        name="manifest.summary",
        path="manifest/summary.json",
        entry="handover.executor.approval.Summary.as_dict",
        contract="stage7-releases.md \u00a72.5",
        notes=(
            "The input is the seven fields of one summary.",
            "value is the summary as the phone gets it. A field of more than 120 characters "
            "is cut to 119 characters and one ellipsis. A character is one code point.",
        ),
        vectors=tuple(_summary_vector(*row) for row in SUMMARIES),
    )


def surfaces() -> tuple[Surface, ...]:
    return (
        _component_surface(),
        _operator_surface(),
        _catalog_surface(),
        _refusal_code_surface(),
        _parse_surface(),
        _plan_surface(),
        _ulid_surface(),
        _state_surface(),
        _resolved_surface(),
        _gate_surface(),
        _summary_surface(),
    )
