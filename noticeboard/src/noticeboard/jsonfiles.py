"""Reading a file another process wrote, without ever raising.

Every state file this noticeboard reads crosses a process boundary, so it is
untrusted input: shape and size are checked before use (invariants 12 and
14). A missing, oversized, truncated or malformed file yields a PROBLEM
STRING, which a page renders as a report. It never yields an exception,
because a stack trace on the noticeboard is the worst possible answer to "what
is running right now".

Nothing here interprets a field's meaning. `statusdocs.py` and its
neighbours do that, one layer up.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, cast

#: One parsed JSON object. Nothing below it is typed: every field is read
#: through a helper here, which is the point.
type Json = dict[str, object]

#: A status document or a validation report is a few kilobytes. A megabyte
#: is far past anything `caregiver` writes and still small enough to read in
#: one go on a page request.
MAX_DOC_BYTES: Final = 1 << 20

#: How much of any one untrusted string a page will show. Long enough for a
#: fault message or a mount list, short enough that a family cannot push a
#: page over by writing a novel into a field.
MAX_TEXT_CHARS: Final = 500

_MISSING: Final = "missing"

#: What a page says for a text that nests past the limit of the JSON reader.
_TOO_DEEP: Final = "it nests deeper than the reader allows"


def read_object(path: Path, limit: int = MAX_DOC_BYTES) -> tuple[Json | None, str | None]:
    """One JSON object from a file, or a reason it could not be read.

    Answers `(None, None)` for a file that does not exist: absence is not a
    problem to report on every page, it is the ordinary state of an outcome
    directory or a fault file (contract 05 §3.3.1 rule 5).
    """
    try:
        with path.open("rb") as handle:
            # One byte past the cap proves that the file is over it. The
            # size of a file then never sets the memory of a page.
            raw = handle.read(limit + 1)
    except FileNotFoundError:
        return None, None
    except OSError as error:
        # A permission error belongs here too: the noticeboard's user may not be in
        # the writer's group yet (README gotcha 4).
        return None, f"cannot read {path.name}: {error.strerror or error}"
    except ValueError:
        # A name with a NUL is no path, and `open` raises this type for it.
        # A status document names its report by path, so the name is input.
        return None, f"cannot read {path.name!r}: the system takes no such path"

    if len(raw) > limit:
        return None, f"{path.name} is over {limit} bytes; refusing to parse it"

    return parse_object(raw, path.name)


def parse_object(raw: bytes, name: str) -> tuple[Json | None, str | None]:
    """The same checks, on bytes already in hand."""
    # CONTRACT-QUESTION: contract 05 §2 names no encoding for a file.
    # `json.loads` takes UTF-8, UTF-16 and UTF-32, so this reader takes the
    # three. The reading stays, because a stricter reader would refuse a file
    # that a page shows today. A reader of UTF-8 alone costs one decode step.
    try:
        parsed: object = json.loads(raw)
    except RecursionError:
        # Deep nesting raises this type, not ValueError. Its text names the
        # interpreter's stack, which differs between two Python versions.
        return None, f"{name} is not JSON: {_TOO_DEEP}"
    except (ValueError, UnicodeDecodeError) as error:
        return None, f"{name} is not JSON: {error}"

    if not isinstance(parsed, dict):
        return None, f"{name} is {type(parsed).__name__}, not a JSON object"

    # `json.loads` answers `Any`, so pyright cannot see the key type. Every
    # JSON object has string keys by definition of the format.
    return cast("Json", parsed), None


def text(body: Json, key: str, limit: int = MAX_TEXT_CHARS) -> str:
    """A string field, truncated for display. A missing or wrong-typed field
    reads empty, so no page has to branch on a type it did not expect."""
    value = body.get(key)

    if not isinstance(value, str):
        return ""

    return value[:limit]


def whole(body: Json, key: str) -> str:
    """A string field that is a name, not prose: an id, a state, a path. It
    is still bounded, at the display cap, so an oversized one cannot grow a
    page either."""
    return text(body, key)


def integer(body: Json, key: str, fallback: int = 0) -> int:
    value = body.get(key)

    # A bool is an int in Python, and a `true` where a count belongs is a
    # malformed document, not the number one.
    if isinstance(value, bool) or not isinstance(value, int):
        return fallback

    # CONTRACT-QUESTION: contracts 02, 04 and 05 give no range for a count.
    # This reader takes an integer of any size that JSON can write. The
    # reading stays, because a range would refuse a value that a page shows
    # today. A range costs one comparison here.
    return value


def number(body: Json, key: str) -> float | None:
    value = body.get(key)

    if isinstance(value, bool) or not isinstance(value, int | float):
        return None

    try:
        return float(value)
    except OverflowError:
        # JSON writes an integer of any size, and no float holds one past
        # about 1.8e308. That is a malformed amount, not a number.
        return None


def flag(body: Json, key: str) -> bool:
    return body.get(key) is True


def block(body: Json, key: str) -> Json | None:
    """A nested object, or None when it is absent or null. Several contracts
    make a whole block nullable (`reconcile`, `spend`, `credentials`), and
    "not published" is a different sentence from "published empty"."""
    value = body.get(key)

    return cast("Json", value) if isinstance(value, dict) else None


def child(body: Json, key: str) -> Json:
    """The same nested object, as an empty one when it is absent. For a
    caller that reads fields out of it and needs no such distinction."""
    return block(body, key) or {}


def children(body: Json, key: str, limit: int) -> tuple[Json, ...]:
    """A list of objects, capped. Anything in the list that is not an object
    is dropped rather than rendered, because a page cannot show it and an
    exception would hide the rows that are fine."""
    value = body.get(key)

    if not isinstance(value, list):
        return ()

    entries = cast("list[object]", value)

    return tuple(cast("Json", one) for one in entries[:limit] if isinstance(one, dict))


def strings(body: Json, key: str, limit: int) -> tuple[str, ...]:
    value = body.get(key)

    if not isinstance(value, list):
        return ()

    entries = cast("list[object]", value)

    return tuple(one[:MAX_TEXT_CHARS] for one in entries[:limit] if isinstance(one, str))


def moment(body: Json, key: str) -> datetime | None:
    """An RFC 3339 timestamp, or None when the field is absent or unreadable.

    A naive value is read as UTC. Every writer in these contracts stamps UTC
    with a `Z`, and treating a missing offset as local time would make
    staleness depend on the reader's timezone.
    """
    raw = text(body, key)

    if not raw:
        return None

    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None

    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def age_s(stamp: datetime | None, now: datetime) -> float | None:
    """Seconds between `stamp` and `now`, or None when there is no stamp.

    A stamp in the future answers 0. Two hosts' clocks can disagree, and a
    negative age would read as "fresh by 40 seconds", which is a sentence
    nobody can act on.
    """
    if stamp is None:
        return None

    return max(0.0, (now - stamp).total_seconds())


def missing(label: str) -> str:
    """The one word every page uses for a file that is not there."""
    return f"{label} is {_MISSING}"
