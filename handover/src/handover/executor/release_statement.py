"""The release statement of a Forgejo release, and the cosign check of P1
(`docs/design/forgejo-release-path.md`, FR7, FR8, section 4.5 and section 5).

The signer `ci-signer` writes one statement for each eligible run and puts
it, with its cosign bundle, in the message of each annotated release tag:

```text
release-statement-1
eyJzdGF0ZW1lbnQiOjEs...      <- base64 of the canonical statement
eyJtZWRpYVR5cGUiOi...        <- base64 of the cosign bundle
```

Root reads that message in three steps, and only in this order:

1. `decode_message` splits the message into the statement bytes and the
   bundle bytes. It does not parse the statement.
2. `check_signature` writes both into root's work tree and starts one
   verifier child: `cosign verify-blob --key` with the public key of FR10
   and no network (FR12). A pass gives a `SignedStatement`. Only this
   check makes one.
3. `read_statement` parses a `SignedStatement` into a `ReleaseStatement`.
   Only this reader makes one, and only from a `SignedStatement`.

The format is the format of the signer crate `rust/crates/ci-signer`. That
crate is the authority. Its vectors and its JSON Schema drive the tests of
this module (`tests/test_handover_frp_vectors.py`), so the signer and this
reader cannot drift. Each constant and each pattern below has the value of
`limits.rs`, `names.rs` and `schema/release-statement.json`.

Every refusal has the code `P1` and one reason of `StatementReason`. No
byte of the message or of the statement reaches a refusal.
"""

from __future__ import annotations

import binascii
import os
import re
import stat
from base64 import b64decode, b64encode
from collections.abc import Sequence
from dataclasses import InitVar, dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Final, cast

from .. import notices, strictjson
from ..artifacts import ArtifactRefusal
from ..catalog import CATALOG_BY_NAME
from ..errors import Refusal, RefusalCode
from ..site import cosign_program
from .host import (
    NO_VERIFIER_EXIT_CODE,
    TIMEOUT_EXIT_CODE,
    VERIFIER_TIMEOUT_S,
    As,
    Command,
    Network,
    RunFn,
)

# -- the limits of `rust/crates/ci-signer/src/limits.rs` and `names.rs` -----

#: The first line of a tag message.
MESSAGE_HEADER: Final = "release-statement-1"

#: The most tags in one statement (section 4.5 step 4).
MAX_TAGS: Final = 32

#: The most assets of one tag (section 4.5 step 4).
MAX_ASSETS_PER_TAG: Final = 16

#: The most bytes of a tag name and of an asset name (section 4.5, decided
#: by the operator on 2026-11-16).
MAX_NAME_BYTES: Final = 64

#: The most bytes of a workflow file name (decided by the operator on
#: 2026-11-16).
MAX_WORKFLOW_BYTES: Final = 64

#: The most bytes of a cosign bundle (section 4.5, decided by the operator
#: on 2026-11-16). cosign v3.1.4 with a P-256 key writes 387 bytes.
MAX_BUNDLE_BYTES: Final = 4096

#: The most bytes of a tag message as the signer writes it (section 4.5,
#: decided by the operator on 2026-11-16).
MAX_TAG_MESSAGE_BYTES: Final = 120_000

#: The most bytes of an owner and of a repository name (`names.rs`).
MAX_OWNER_BYTES: Final = 39
MAX_REPO_NAME_BYTES: Final = 100

#: The bytes that `GET /repos/{o}/{r}/git/tags/{sha}` adds to the message as
#: written: one LF (measured, note section 3 M3).
GIT_TAG_OBJECT_SUFFIX: Final = "\n"

#: The version of the statement format.
STATEMENT_VERSION: Final = 1

#: The one event and the one ref of a statement (FR7, section 4.4).
STATEMENT_EVENT: Final = "push"
STATEMENT_REF: Final = "main"

#: The largest run id: 2^53 - 1.
MAX_RUN_ID: Final = 2**53 - 1

#: The decimal digits of `MAX_RUN_ID`, the bytes of a `signed_at` value, of
#: a commit id and of a SHA-256 in hex.
_MAX_RUN_DIGITS: Final = 16
_SIGNED_AT_BYTES: Final = 20
_COMMIT_BYTES: Final = 40
_SHA256_HEX_BYTES: Final = 64


def _list_bytes(count: int, item: int) -> int:
    """The bytes of `count` items of `item` bytes each, with a comma between
    two."""
    return 0 if count == 0 else count * item + (count - 1)


_WORST_ASSET_BYTES: Final = len('{"name":"","sha256":""}') + MAX_NAME_BYTES + _SHA256_HEX_BYTES
_WORST_TAG_BYTES: Final = (
    len('{"tag":"","assets":[]}')
    + MAX_NAME_BYTES
    + _list_bytes(MAX_ASSETS_PER_TAG, _WORST_ASSET_BYTES)
)

#: The bytes of the largest canonical statement (`limits.rs`,
#: `WORST_STATEMENT_BYTES`).
WORST_STATEMENT_BYTES: Final = (
    len(
        '{"statement":1,"repository":"/","workflow":"","event":"push","ref":"main",'
        '"commit":"","run":,"signed_at":"","tags":[]}'
    )
    + MAX_OWNER_BYTES
    + MAX_REPO_NAME_BYTES
    + MAX_WORKFLOW_BYTES
    + _COMMIT_BYTES
    + _MAX_RUN_DIGITS
    + _SIGNED_AT_BYTES
    + _list_bytes(MAX_TAGS, _WORST_TAG_BYTES)
)

# -- the patterns of `schema/release-statement.json` -------------------------
#
# Each pattern is the schema pattern without `^` and `$`, and each use is a
# `re.fullmatch`. A test holds each one equal to the schema.

OWNER_PATTERN: Final = r"[A-Za-z0-9][A-Za-z0-9._-]{0,38}"
REPO_NAME_PATTERN: Final = r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}"
RELEASE_TAG_PATTERN: Final = (
    r"[a-z][a-z0-9-]{1,30}-v(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})"
)
ASSET_NAME_PATTERN: Final = r"[A-Za-z0-9][A-Za-z0-9._+-]{0,63}"
REPO_FULL_NAME_PATTERN: Final = f"{OWNER_PATTERN}/{REPO_NAME_PATTERN}"
WORKFLOW_FILE_PATTERN: Final = (
    r"([A-Za-z0-9][A-Za-z0-9._-]{0,59}\.yml|[A-Za-z0-9][A-Za-z0-9._-]{0,58}\.yaml)"
)
COMMIT_SHA_PATTERN: Final = r"[0-9a-f]{40}"
SHA256_HEX_PATTERN: Final = r"[0-9a-f]{64}"
SIGNED_AT_PATTERN: Final = r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z"

RELEASE_TAG_RE: Final = re.compile(RELEASE_TAG_PATTERN)
COMMIT_SHA_RE: Final = re.compile(COMMIT_SHA_PATTERN)
OWNER_RE: Final = re.compile(OWNER_PATTERN)
REPO_NAME_RE: Final = re.compile(REPO_NAME_PATTERN)
_ASSET_NAME_RE: Final = re.compile(ASSET_NAME_PATTERN)
_REPO_FULL_NAME_RE: Final = re.compile(REPO_FULL_NAME_PATTERN)
_WORKFLOW_FILE_RE: Final = re.compile(WORKFLOW_FILE_PATTERN)
_SHA256_HEX_RE: Final = re.compile(SHA256_HEX_PATTERN)
_SIGNED_AT_RE: Final = re.compile(SIGNED_AT_PATTERN)

#: The first year of a `signed_at` value (`signed_at.rs`). The pattern
#: holds the last year, 9999.
_FIRST_YEAR: Final = 1970

#: The surface word of a notice for a statement that is not strict JSON.
SURFACE: Final = "release.statement"


class StatementReason(StrEnum):
    """The reasons of a refusal from this module, a closed list. Each one
    has the code P1, but `NOT_A_COMPONENT` has the code `catalog`. The two
    verifier reasons have the texts of `ArtifactRefusal`, so a later change
    of the follower needs only a check of the code."""

    TOO_LARGE = "the tag message is too large"
    SUFFIX = "the tag message has a wrong end"
    LINES = "the tag message has not three lines"
    HEADER = "the tag message has a wrong header"
    BASE64 = "a line of the tag message is no strict base64"
    STATEMENT_SIZE = "the statement of the tag message has a wrong size"
    BUNDLE_SIZE = "the bundle of the tag message has a wrong size"
    NOT_JSON = "the statement is no strict JSON text"
    FIELD = "the statement has a wrong field"
    NOT_CANONICAL = "the statement is not in its canonical form"
    VERIFIER_UNAVAILABLE = ArtifactRefusal.VERIFIER_UNAVAILABLE.value
    SIGNATURE_REFUSED = ArtifactRefusal.SIGNATURE_REFUSED.value
    NOT_A_COMPONENT = "not a catalog component"


def _refuse(component: str, reason: StatementReason) -> Refusal:
    return Refusal(RefusalCode.P1, component, reason.value)


# -- step 1: the message ------------------------------------------------------


@dataclass(frozen=True)
class UnverifiedMessage:
    """The two parts of a tag message. No signature covers them yet."""

    statement: bytes
    bundle: bytes


def written_message(component: str, message: str) -> str:
    """The message as the signer wrote it, from the `message` field of
    `GET /repos/{o}/{r}/git/tags/{sha}`. That reply adds exactly one LF
    (note section 3 M3). A message with no final LF, or with a second one,
    refuses. The rule is `MessageSource::GitTagObject` of `message.rs`.

    Root reads the message only from the tag object, the object that P2
    reads too. So this module has one source and no source parameter.
    """
    body = message.removesuffix(GIT_TAG_OBJECT_SUFFIX)
    if body == message or body.endswith(GIT_TAG_OBJECT_SUFFIX):
        raise _refuse(component, StatementReason.SUFFIX)

    return body


def _strict_base64(line: str) -> bytes | None:
    """The bytes of a strict base64 line of RFC 4648 section 4, or None.
    The decode refuses a character outside the alphabet. The encode of the
    bytes must give the line again, so pad bits that are not zero and a
    missing pad refuse too."""
    try:
        decoded = b64decode(line, validate=True)
    except binascii.Error, ValueError:
        return None

    if b64encode(decoded).decode("ascii") != line:
        return None

    return decoded


def decode_message(component: str, message: str) -> UnverifiedMessage:
    """The statement and the bundle of a tag message, from the `message`
    field of the git tag object. The checks run in the order of
    `UnverifiedTagMessage::decode` of `message.rs`. The statement is not
    parsed (rule 4 of `rust/crates/ci-signer/AGENTS.md`)."""
    size = len(message.encode("utf-8", "surrogatepass"))
    if size > MAX_TAG_MESSAGE_BYTES + len(GIT_TAG_OBJECT_SUFFIX):
        raise _refuse(component, StatementReason.TOO_LARGE)

    lines = written_message(component, message).split("\n")
    if len(lines) != 3:
        raise _refuse(component, StatementReason.LINES)

    header, statement_line, bundle_line = lines
    if header != MESSAGE_HEADER:
        raise _refuse(component, StatementReason.HEADER)

    statement = _strict_base64(statement_line)
    bundle = _strict_base64(bundle_line)
    if statement is None or bundle is None:
        raise _refuse(component, StatementReason.BASE64)

    if not 1 <= len(statement) <= WORST_STATEMENT_BYTES:
        raise _refuse(component, StatementReason.STATEMENT_SIZE)

    if not 1 <= len(bundle) <= MAX_BUNDLE_BYTES:
        raise _refuse(component, StatementReason.BUNDLE_SIZE)

    return UnverifiedMessage(statement=statement, bundle=bundle)


# -- step 2: the signature ----------------------------------------------------

#: The public key of FR10. A root-owned file outside every release.
RELEASE_SIGNING_KEY: Final = "/etc/creche/release-signing.pub"

#: The words that tell cosign that the bundle holds no entry of a
#: transparency log (FR12). Measured on cosign v3.1.4 on 2026-11-16 (section
#: 3 M7): `verify-blob --key <key> --bundle <bundle> --insecure-ignore-tlog
#: <statement>` passes with no network. Without this flag, cosign fetches
#: the trusted root from TUF and fails with no network.
VERIFY_SKIP_TLOG: Final = ("--insecure-ignore-tlog",)

#: The directory of the statement files below the work tree of a request,
#: and the names of the two files.
STATEMENT_DIR: Final = "statement"
STATEMENT_FILE: Final = "statement.json"
BUNDLE_FILE: Final = "statement.sigstore.json"

#: The modes of rule 12a of `handover/AGENTS.md`: the dynamic user of the
#: verifier reads root's files.
DIR_MODE: Final = 0o755
FILE_MODE: Final = 0o644

#: The exit status of a verifier child that passed.
PASSED: Final = 0

#: The exit status of cosign v3.1.4 for each measured failure: a changed
#: statement, a bundle of another statement, another key, a missing key, a
#: missing bundle, an empty or cut bundle and a key file that is no key
#: (measured on 2026-11-16).
REFUSED: Final = 1

#: The statuses that root reads as no answer: root started no child, or the
#: child did not end in root's time (the same set as check P6).
NO_ANSWER: Final = frozenset({NO_VERIFIER_EXIT_CODE, TIMEOUT_EXIT_CODE})


def statement_words(bundle: Path, statement: Path) -> tuple[str, ...]:
    """The words of the verifier after its program name (note section 5):
    `verify-blob --key <FR10 key> --bundle <bundle> <skip the log>
    <statement>`. Each path is absolute, so the child reads no path that
    its working directory decides."""
    if not bundle.is_absolute() or not statement.is_absolute():
        raise ValueError("the statement and the bundle need absolute paths")

    return (
        "verify-blob",
        "--key",
        RELEASE_SIGNING_KEY,
        "--bundle",
        str(bundle),
        *VERIFY_SKIP_TLOG,
        str(statement),
    )


class _Checked:
    """No module but this one names this type. `SignedStatement` takes a
    value of it first, so only `check_signature` makes one."""


_CHECKED: Final = _Checked()


@dataclass(frozen=True)
class SignedStatement:
    """The statement bytes of one component, after the verifier passed them
    with the key of FR10. `check_signature` is the one maker."""

    made: InitVar[_Checked]
    component: str
    data: bytes

    def __post_init__(self, made: _Checked) -> None:
        if made is not _CHECKED:
            raise TypeError("only check_signature makes a SignedStatement")


def _write_file(path: Path, data: bytes) -> None:
    """One new file of root, with the mode `FILE_MODE`. No link is
    followed."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW
    descriptor = os.open(path, flags, FILE_MODE)
    try:
        os.fchmod(descriptor, FILE_MODE)
        os.write(descriptor, data)
    finally:
        os.close(descriptor)


def _write_pair(component: str, message: UnverifiedMessage, work: Path) -> tuple[Path, Path]:
    """`work/statement/<component>/statement.json` and its bundle, each
    directory with the mode `DIR_MODE`. `component` is a catalog name, so
    the path comes from no field of the message."""
    if component not in CATALOG_BY_NAME:
        raise Refusal(RefusalCode.CATALOG, "component", StatementReason.NOT_A_COMPONENT.value)

    where = work / STATEMENT_DIR / component
    for directory in (work / STATEMENT_DIR, where):
        directory.mkdir(mode=DIR_MODE, exist_ok=True)

        # A link in root's own work tree is a fault, and the step fails.
        if not stat.S_ISDIR(os.lstat(directory).st_mode):
            raise NotADirectoryError(directory)

        os.chmod(directory, DIR_MODE)

    statement, bundle = where / STATEMENT_FILE, where / BUNDLE_FILE
    _write_file(statement, message.statement)
    _write_file(bundle, message.bundle)

    return statement, bundle


def _status(run: RunFn, command: Command) -> int:
    """The exit status of the verifier child. A child that the system does
    not start is root's own `NO_VERIFIER_EXIT_CODE`."""
    try:
        return run(command).code
    except OSError:
        return NO_VERIFIER_EXIT_CODE


def check_signature(
    run: RunFn, component: str, message: UnverifiedMessage, work: Path
) -> SignedStatement:
    """P1, the signature: one verifier child checks the bundle against the
    statement with the key of FR10, with no network and no trust root
    (FR1, FR12). Root reads the exit status and no text.

    1. 0 (`PASSED`): the signature passed.
    2. 1 (`REFUSED`), the status of each measured failure of cosign
       v3.1.4: `SIGNATURE_REFUSED`. A missing key file and a missing
       bundle file end with 1 too.
    3. Root starts no child, or the child does not end in root's time
       (`NO_ANSWER`): `VERIFIER_UNAVAILABLE`.
    4. Each other status: `VERIFIER_UNAVAILABLE`. No measurement gives
       cosign such a status, so it says nothing of the signature. P1
       refuses (fail closed), and the follower asks again.

    Through `systemd-run --wait`, a child that does not start also ends
    with 1 (the CONTRACT-QUESTION of `provenance.check_artifact`). Root
    checks the two program files before each child, so that case refuses
    as `VERIFIER_UNAVAILABLE` before a child starts.
    """
    # Decided by the operator on 2026-11-16: the check of the owner and the
    # mode of the key file of FR10 comes with the wiring PR (4b). Until
    # then, a missing key file makes cosign end with 1 (measured), and P1
    # refuses as a refused signature.
    statement, bundle = _write_pair(component, message, work)
    check = Command(
        argv=(cosign_program(), *statement_words(bundle, statement)),
        identity=As.VERIFIER,
        timeout_s=VERIFIER_TIMEOUT_S,
        network=Network.NONE,
    )
    status = _status(run, check)
    if status in NO_ANSWER:
        raise _refuse(component, StatementReason.VERIFIER_UNAVAILABLE)

    if status == REFUSED:
        raise _refuse(component, StatementReason.SIGNATURE_REFUSED)

    if status != PASSED:
        raise _refuse(component, StatementReason.VERIFIER_UNAVAILABLE)

    return SignedStatement(_CHECKED, component, message.statement)


# -- step 3: the statement ----------------------------------------------------


class _Parsed:
    """No module but this one names this type. The three value types below
    take a value of it first, so only `read_statement` makes them."""


_PARSED: Final = _Parsed()


def _require_parsed(made: _Parsed, what: str) -> None:
    if made is not _PARSED:
        raise TypeError(f"only read_statement makes a {what}")


@dataclass(frozen=True)
class StatementAsset:
    """One asset of a tag in a signed statement: its name and its SHA-256."""

    made: InitVar[_Parsed]
    name: str
    sha256: str

    def __post_init__(self, made: _Parsed) -> None:
        _require_parsed(made, "StatementAsset")


@dataclass(frozen=True)
class StatementTag:
    """One tag of a signed statement, with its assets sorted by name."""

    made: InitVar[_Parsed]
    tag: str
    assets: tuple[StatementAsset, ...]

    def __post_init__(self, made: _Parsed) -> None:
        _require_parsed(made, "StatementTag")

    @property
    def component(self) -> str:
        """The component of the tag: the text before the last `-v`."""
        return self.tag.rpartition("-v")[0]


@dataclass(frozen=True)
class ReleaseStatement:
    """A parsed statement whose signature passed (note section 5). The one
    type that holds a parsed statement. `event` is always `push` and `ref`
    is always `main`, so the type holds neither."""

    made: InitVar[_Parsed]
    repository: str
    workflow: str
    commit: str
    run: int
    signed_at: str
    tags: tuple[StatementTag, ...]

    def __post_init__(self, made: _Parsed) -> None:
        _require_parsed(made, "ReleaseStatement")


#: The keys of a statement, of a tag and of an asset, in canonical order.
_STATEMENT_KEYS: Final = frozenset(
    {"statement", "repository", "workflow", "event", "ref", "commit", "run", "signed_at", "tags"}
)
_TAG_KEYS: Final = frozenset({"tag", "assets"})
_ASSET_KEYS: Final = frozenset({"name", "sha256"})


class _FieldError(Exception):
    """A field of the statement is wrong. `read_statement` turns it into one
    refusal with the reason `FIELD`."""


def _object(value: object, keys: frozenset[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or frozenset(cast("dict[str, Any]", value)) != keys:
        raise _FieldError

    return cast("dict[str, Any]", value)


def _text(value: object, pattern: re.Pattern[str]) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise _FieldError

    return value


def _whole(value: object) -> int:
    """A JSON integer. `bool` is no integer here."""
    if type(value) is not int:
        raise _FieldError

    return value


def _month_days(year: int, month: int) -> int:
    """The days of a month of the proleptic Gregorian calendar
    (`month_days` of `signed_at.rs`)."""
    leap = year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)
    if month == 2:
        return 29 if leap else 28

    return 30 if month in (4, 6, 9, 11) else 31


def _signed_at(value: object) -> str:
    """A time `YYYY-MM-DDTHH:MM:SSZ` that exists, in the years 1970 to
    9999, as `SignedAt::from_str` of `signed_at.rs` reads it. A valid text
    is its own canonical form, so the reader keeps the text."""
    text = _text(value, _SIGNED_AT_RE)
    year, month, day = int(text[0:4]), int(text[5:7]), int(text[8:10])
    hour, minute, second = int(text[11:13]), int(text[14:16]), int(text[17:19])
    if year < _FIRST_YEAR or not 1 <= month <= 12:
        raise _FieldError

    if not 1 <= day <= _month_days(year, month):
        raise _FieldError

    if hour > 23 or minute > 59 or second > 59:
        raise _FieldError

    return text


def _unique(names: Sequence[str]) -> None:
    if len(set(names)) != len(names):
        raise _FieldError


def _assets(value: object) -> tuple[StatementAsset, ...]:
    """The assets of one tag, sorted by name, each name once (`Assets::new`
    of `statement.rs`)."""
    if not isinstance(value, list):
        raise _FieldError

    items = cast("list[object]", value)
    if len(items) > MAX_ASSETS_PER_TAG:
        raise _FieldError

    found: list[StatementAsset] = []
    for item in items:
        fields = _object(item, _ASSET_KEYS)
        name = _text(fields["name"], _ASSET_NAME_RE)
        digest = _text(fields["sha256"], _SHA256_HEX_RE)
        found.append(StatementAsset(_PARSED, name, digest))

    found.sort(key=lambda one: one.name)
    _unique([one.name for one in found])

    return tuple(found)


def _tags(value: object) -> tuple[StatementTag, ...]:
    """The tags of a statement: 1 to `MAX_TAGS`, sorted by tag, each tag
    once and each component once (`Tags::new` of `statement.rs`)."""
    if not isinstance(value, list):
        raise _FieldError

    items = cast("list[object]", value)
    if not 1 <= len(items) <= MAX_TAGS:
        raise _FieldError

    found: list[StatementTag] = []
    for item in items:
        fields = _object(item, _TAG_KEYS)
        tag = _text(fields["tag"], RELEASE_TAG_RE)
        found.append(StatementTag(_PARSED, tag, _assets(fields["assets"])))

    found.sort(key=lambda one: one.tag)
    _unique([one.tag for one in found])
    _unique([one.component for one in found])

    return tuple(found)


def _parse(value: object) -> ReleaseStatement:
    """Each field of a statement, by the types of `statement.rs`."""
    # Decided by the operator on 2026-11-16: no freshness check of
    # `signed_at`. An old release stays installable. The reader checks only
    # its grammar.
    # CONTRACT-QUESTION: forgejo-release-path.md section 5 names no check
    # of the run id. The reading here checks its grammar and nothing more.
    # A change compares it with a run of the forge.
    fields = _object(value, _STATEMENT_KEYS)
    if _whole(fields["statement"]) != STATEMENT_VERSION:
        raise _FieldError

    if fields["event"] != STATEMENT_EVENT or fields["ref"] != STATEMENT_REF:
        raise _FieldError

    run = _whole(fields["run"])
    if not 1 <= run <= MAX_RUN_ID:
        raise _FieldError

    return ReleaseStatement(
        _PARSED,
        repository=_text(fields["repository"], _REPO_FULL_NAME_RE),
        workflow=_text(fields["workflow"], _WORKFLOW_FILE_RE),
        commit=_text(fields["commit"], COMMIT_SHA_RE),
        run=run,
        signed_at=_signed_at(fields["signed_at"]),
        tags=_tags(fields["tags"]),
    )


def _quoted(text: str) -> str:
    """A JSON string of a value that a pattern checked: no character needs
    an escape."""
    return f'"{text}"'


def _asset_text(asset: StatementAsset) -> str:
    return f'{{"name":{_quoted(asset.name)},"sha256":{_quoted(asset.sha256)}}}'


def _tag_text(tag: StatementTag) -> str:
    assets = ",".join(_asset_text(one) for one in tag.assets)

    return f'{{"tag":{_quoted(tag.tag)},"assets":[{assets}]}}'


def canonical_bytes(statement: ReleaseStatement) -> bytes:
    """The canonical form of a statement: the key order of
    `Statement::canonical_bytes` of `statement.rs`, with no whitespace."""
    tags = ",".join(_tag_text(one) for one in statement.tags)
    text = (
        f'{{"statement":{STATEMENT_VERSION},'
        f'"repository":{_quoted(statement.repository)},'
        f'"workflow":{_quoted(statement.workflow)},'
        f'"event":{_quoted(STATEMENT_EVENT)},"ref":{_quoted(STATEMENT_REF)},'
        f'"commit":{_quoted(statement.commit)},'
        f'"run":{statement.run},'
        f'"signed_at":{_quoted(statement.signed_at)},'
        f'"tags":[{tags}]}}'
    )

    return text.encode("ascii")


def read_statement(signed: SignedStatement) -> ReleaseStatement:
    """The statement of a `SignedStatement`. The checks run in the order of
    `Statement::from_canonical` of `statement.rs`: the size, strict JSON,
    each field, then the canonical form. The text must be the canonical
    form byte for byte, so another key order, whitespace, an escape or an
    unsorted list refuses."""
    component, data = signed.component, signed.data
    if len(data) > WORST_STATEMENT_BYTES:
        raise _refuse(component, StatementReason.STATEMENT_SIZE)

    try:
        value = strictjson.loads(data, cap=WORST_STATEMENT_BYTES)
    except strictjson.NotStrictJson as error:
        notices.record(SURFACE, error.rule)
        raise _refuse(component, StatementReason.NOT_JSON) from None

    try:
        statement = _parse(value)
    except _FieldError:
        raise _refuse(component, StatementReason.FIELD) from None

    if canonical_bytes(statement) != data:
        raise _refuse(component, StatementReason.NOT_CANONICAL)

    return statement
