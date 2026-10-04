"""The release request, parsed as hostile bytes (`stage7-releases.md` §2.3).

A requester states INTENT and nothing else: which components, and a version
each. It cannot name a path, a command, a URL, a digest or a hash, because no
such field exists (§3.2 rule 7). Root resolves all of that itself, which is
what makes the design hold when the requester is hostile (§6 row 3).

Two rules here are load bearing and easy to lose in a refactor.

1. **Every pattern is `re.fullmatch`.** A trailing `$` also matches before a
   trailing newline, and these values become argv words and git tags (§3.2
   rule 3).
2. **A refusal's reason is a FIXED string.** It may land in the ledger, so it
   never quotes the file (§3.2 rule 4).

The id is upper-case Crockford base32, as contract 06 §9 and contract 02 §2
fix every ULID.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final, cast

from ..catalog import MAX_REQUEST_COMPONENTS
from ..errors import Refusal, RefusalCode

#: §3.2 rule 2. A complete request is about 300 bytes. Anything past this is
#: not one, and the cap is applied before the parse, never after.
MAX_REQUEST_BYTES: Final = 4096

REQUEST_SUFFIX: Final = ".json"

#: Contract 06 §9: 26 upper-case Crockford characters, I, L, O and U absent.
ULID_RE: Final = re.compile(r"[0-9A-HJKMNP-TV-Z]{26}")

#: Contract 06 §8's `name`, the grammar a family and a server name share.
COMPONENT_RE: Final = re.compile(r"[a-z][a-z0-9-]{1,30}")
VERSION_RE: Final = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")
SESSION_ID_RE: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")

#: §2.3: a family name, `human`, or `ci`.
REQUESTED_BY_RE: Final = re.compile(r"[a-z][a-z0-9-]{1,30}")

#: What a request writes instead of a number (§2.3).
LATEST: Final = "latest"

REQUEST_KEYS: Final = frozenset(
    {"id", "kind", "components", "rollback_of", "requested_by", "requester_session", "ts"}
)


class Kind(StrEnum):
    RELEASE = "release"
    #: §8 open question 6. Parsed, and refused at the first step until it is
    #: built: a deliberate reversal of a release that verified green is
    #: rarer and more dangerous than an automatic restore.
    ROLLBACK = "rollback"


@dataclass(frozen=True)
class Request:
    """One validated request. Every field has passed its own pattern."""

    id: str
    kind: Kind
    #: Component name to a version, or `latest`.
    components: tuple[tuple[str, str], ...]
    rollback_of: str | None
    requested_by: str
    requester_session: str | None
    ts: float

    def wanted(self) -> dict[str, str]:
        return dict(self.components)

    def as_dict(self) -> dict[str, object]:
        """What root re-serializes into `running/`. Root never copies the
        requester's bytes (§3.2 rule 4): this is built from the fields."""
        return {
            "id": self.id,
            "kind": str(self.kind),
            "components": dict(self.components),
            "rollback_of": self.rollback_of,
            "requested_by": self.requested_by,
            "requester_session": self.requester_session,
            "ts": self.ts,
        }


def request_id_of(name: str) -> str | None:
    """`<ULID>.json` to `<ULID>`, or None for any other file name."""
    if not name.endswith(REQUEST_SUFFIX):
        return None

    stem = name.removesuffix(REQUEST_SUFFIX)

    return stem if ULID_RE.fullmatch(stem) else None


def _refuse(detail: str) -> Refusal:
    return Refusal(RefusalCode.REQUEST, "request", detail)


def _object(raw: bytes) -> dict[str, Any]:
    try:
        loaded: Any = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise _refuse("is not a JSON object") from None

    if not isinstance(loaded, dict):
        raise _refuse("is not a JSON object")

    body: dict[str, Any] = {}
    for key, value in cast(dict[Any, Any], loaded).items():
        if not isinstance(key, str):
            raise _refuse("has a non-string key")

        body[key] = value

    return body


def _text(body: dict[str, Any], key: str, pattern: re.Pattern[str]) -> str:
    value = body[key]
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise _refuse(f"field '{key}' is malformed")

    return value


def _optional_text(body: dict[str, Any], key: str, pattern: re.Pattern[str]) -> str | None:
    if body[key] is None:
        return None

    return _text(body, key, pattern)


def _components(value: Any) -> tuple[tuple[str, str], ...]:
    """§2.3: at most eight entries, each a component name and a version."""
    if not isinstance(value, dict):
        raise _refuse("field 'components' must be an object")

    entries = cast(dict[Any, Any], value)
    if not entries:
        raise _refuse("field 'components' names nothing")

    if len(entries) > MAX_REQUEST_COMPONENTS:
        raise _refuse(f"field 'components' names more than {MAX_REQUEST_COMPONENTS}")

    wanted: list[tuple[str, str]] = []
    for name, version in sorted(entries.items(), key=lambda item: str(item[0])):
        if not isinstance(name, str) or not COMPONENT_RE.fullmatch(name):
            raise _refuse("field 'components' has a malformed component name")

        if not isinstance(version, str):
            raise _refuse("field 'components' holds a version or 'latest'")

        if version != LATEST and not VERSION_RE.fullmatch(version):
            raise _refuse("field 'components' holds a version or 'latest'")

        wanted.append((name, version))

    return tuple(wanted)


def _timestamp(value: Any) -> float:
    # A YAML or JSON bool is a Python int. Refuse it before the range check.
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise _refuse("field 'ts' is malformed")

    # A whole number past the largest float has no float. Python raises
    # there, and a request file holds such a number in a few hundred bytes.
    try:
        stamp = float(value)
    except OverflowError:
        raise _refuse("field 'ts' is malformed") from None

    if not math.isfinite(stamp) or stamp < 0:
        raise _refuse("field 'ts' is malformed")

    return stamp


def _kind(value: Any) -> Kind:
    try:
        return Kind(value)
    except ValueError:
        raise _refuse("field 'kind' is malformed") from None


def _rollback_of(body: dict[str, Any], kind: Kind) -> str | None:
    """§2.3: non-null only for `kind: rollback`, and null for it is a lie."""
    target = _optional_text(body, "rollback_of", ULID_RE)
    if kind is Kind.RELEASE and target is not None:
        raise _refuse("field 'rollback_of' is set on a release")

    if kind is Kind.ROLLBACK and target is None:
        raise _refuse("field 'rollback_of' is unset on a rollback")

    return target


def parse_request(raw: bytes, request_id: str) -> Request:
    """Strict: exactly `REQUEST_KEYS`, every value in its own narrow shape.

    `request_id` comes from the already-validated file name and must match:
    a file whose body names another id is a request root will not act on.
    A requester can give an id of its own, so the id has its pattern too:
    it is the name of the file that the requester writes.
    """
    if len(raw) > MAX_REQUEST_BYTES:
        raise _refuse(f"is larger than {MAX_REQUEST_BYTES} bytes")

    body = _object(raw)
    if frozenset(body) != REQUEST_KEYS:
        raise _refuse("keys differ from the request shape")

    if body["id"] != request_id:
        raise _refuse("field 'id' differs from the file name")

    if not ULID_RE.fullmatch(request_id):
        raise _refuse("field 'id' is malformed")

    kind = _kind(body["kind"])

    return Request(
        id=request_id,
        kind=kind,
        components=_components(body["components"]),
        rollback_of=_rollback_of(body, kind),
        requested_by=_text(body, "requested_by", REQUESTED_BY_RE),
        requester_session=_optional_text(body, "requester_session", SESSION_ID_RE),
        ts=_timestamp(body["ts"]),
    )
