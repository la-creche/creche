"""The wire types of the memory socket.

The `memory` upstream of the `mcp` repository writes one `Request` line on a
connection. The service answers with one `Reply` line. This module is the one
definition of each type. The upstream writes the request shapes by hand, and
the parser here is the judge (`memory/AGENTS.md`)."""

import re
from collections.abc import Callable
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, RootModel

MAX_PATH_BYTES = 512
MAX_PART_BYTES = 255
MAX_QUERY_CHARS = 512
MIN_QUERY_CHARS = 2
MAX_RESULTS = 12
MAX_TITLE_CHARS = 200
MAX_BODY_BYTES = 16 * 1024
MAX_TEXT_BYTES = 512 * 1024
MAX_SNIPPET_CHARS = 500
MAX_REASON_CHARS = 500
MAX_LIST_ENTRIES = 2000

NOTE_SUFFIX = ".md"

FAMILY_RE = re.compile(r"[a-z][a-z0-9-]{0,62}")
SLUG_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
SHA256_RE = re.compile(r"[0-9a-f]{64}")
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


def _matches(pattern: re.Pattern[str]) -> Callable[[str], str]:
    def check(text: str) -> str:
        if not pattern.fullmatch(text):
            raise ValueError(f"does not match {pattern.pattern}")

        return text

    return check


def _folder(text: str) -> str:
    if text == "":
        return text

    if len(text.encode()) > MAX_PATH_BYTES:
        raise ValueError(f"longer than {MAX_PATH_BYTES} bytes")

    if CONTROL_RE.search(text) or "\\" in text:
        raise ValueError("holds a control character or a backslash")

    for part in text.split("/"):
        if part == "" or part.startswith("."):
            raise ValueError("holds an empty part or a part that starts with a dot")

        if len(part.encode()) > MAX_PART_BYTES:
            raise ValueError(f"holds a part longer than {MAX_PART_BYTES} bytes")

    return text


def _note(text: str) -> str:
    if text == "" or not text.endswith(NOTE_SUFFIX):
        raise ValueError(f"does not end in {NOTE_SUFFIX}")

    return _folder(text)


def _max_bytes(limit: int) -> Callable[[str], str]:
    def check(text: str) -> str:
        if len(text.encode()) > limit:
            raise ValueError(f"longer than {limit} bytes")

        return text

    return check


def _one_line(text: str) -> str:
    if CONTROL_RE.search(text):
        raise ValueError("holds a control character")

    return text


def _utc(value: datetime) -> datetime:
    if value.utcoffset() != timedelta(0):
        raise ValueError("is not in UTC")

    return value


class Family(RootModel[str]):
    """The app ID of the caller, from `_meta["chaperone.app"]` (D1.2)."""

    root: Annotated[str, AfterValidator(_matches(FAMILY_RE))]


class VaultPath(RootModel[str]):
    """A note, relative to the vault root. No part is `.`, `..` or hidden."""

    root: Annotated[str, AfterValidator(_note)]


class FolderPath(RootModel[str]):
    """A folder, relative to the vault root. The empty text is the root."""

    root: Annotated[str, AfterValidator(_folder)]


class Slug(RootModel[str]):
    """The name part of a new inbox note."""

    root: Annotated[str, AfterValidator(_matches(SLUG_RE))]


class Sha256(RootModel[str]):
    """The SHA-256 of the bytes of a file, in lowercase hex."""

    root: Annotated[str, AfterValidator(_matches(SHA256_RE))]


class IfMatch(RootModel[Sha256 | Literal["new"]]):
    """The `sha256` the agent read, or `new` for a file that must not exist."""


Query = Annotated[str, Field(min_length=MIN_QUERY_CHARS, max_length=MAX_QUERY_CHARS)]
Count = Annotated[int, Field(ge=1, le=MAX_RESULTS)]
Title = Annotated[str, Field(min_length=1, max_length=MAX_TITLE_CHARS), AfterValidator(_one_line)]
Body = Annotated[str, AfterValidator(_max_bytes(MAX_BODY_BYTES))]
Text = Annotated[str, AfterValidator(_max_bytes(MAX_TEXT_BYTES))]
UtcTime = Annotated[datetime, AfterValidator(_utc)]
Bytes = Annotated[int, Field(ge=0)]


class _Wire(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class Refusal(StrEnum):
    """Why the service refused a request. `service_down` is the upstream's own."""

    BAD_REQUEST = "bad_request"
    NOT_FOUND = "not_found"
    NOT_ALLOWED_HERE = "not_allowed_here"
    EXISTS = "exists"
    CHANGED = "changed"
    TOO_LARGE = "too_large"
    NOT_READY = "not_ready"
    INTERNAL = "internal"


class SearchRequest(_Wire):
    v: Literal[1]
    op: Literal["search"]
    caller: Family
    query: Query
    k: Count


class ReadRequest(_Wire):
    v: Literal[1]
    op: Literal["read"]
    caller: Family
    path: VaultPath


class ListRequest(_Wire):
    v: Literal[1]
    op: Literal["list"]
    caller: Family
    folder: FolderPath


class CreateRequest(_Wire):
    v: Literal[1]
    op: Literal["create"]
    caller: Family
    slug: Slug
    title: Title
    body: Body


class RewriteRequest(_Wire):
    v: Literal[1]
    op: Literal["rewrite"]
    caller: Family
    path: VaultPath
    text: Text
    if_match: IfMatch


AnyRequest = Annotated[
    SearchRequest | ReadRequest | ListRequest | CreateRequest | RewriteRequest,
    Field(discriminator="op"),
]


class Request(RootModel[AnyRequest]):
    """One request line. The `op` field picks the shape."""


class Passage(_Wire):
    path: VaultPath
    snippet: Annotated[str, Field(max_length=MAX_SNIPPET_CHARS)]


class SearchReply(_Wire):
    ok: Literal[True]
    op: Literal["search"]
    facts: Text
    passages: Annotated[list[Passage], Field(max_length=MAX_RESULTS)]


class ReadReply(_Wire):
    ok: Literal[True]
    op: Literal["read"]
    path: VaultPath
    text: Text
    sha256: Sha256
    bytes: Bytes
    modified: UtcTime


class Note(_Wire):
    path: VaultPath
    bytes: Bytes
    modified: UtcTime


class ListReply(_Wire):
    ok: Literal[True]
    op: Literal["list"]
    notes: Annotated[list[Note], Field(max_length=MAX_LIST_ENTRIES)]
    folders: Annotated[list[FolderPath], Field(max_length=MAX_LIST_ENTRIES)]


class WriteReply(_Wire):
    ok: Literal[True]
    op: Literal["create", "rewrite"]
    path: VaultPath
    sha256: Sha256


class RefusedReply(_Wire):
    ok: Literal[False]
    refused: Refusal
    reason: Annotated[str, Field(max_length=MAX_REASON_CHARS)]


AnyReply = SearchReply | ReadReply | ListReply | WriteReply | RefusedReply


class Reply(RootModel[AnyReply]):
    """One reply line: one result shape for each op, or a refusal."""
