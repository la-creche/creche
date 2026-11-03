"""The wire types of the memory socket.

The `memory` upstream of the `mcp` repository sends requests to the service
over one Unix socket. This module is the one definition of each wire type.
The upstream writes its requests by hand, and the parser here is the judge
(`memory/AGENTS.md`). This module holds the newtypes and the refusal codes.
The request and reply types use them."""

import re
from collections.abc import Callable
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import AfterValidator, RootModel

MAX_PATH_BYTES = 512
MAX_PART_BYTES = 255

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
