"""The gate id and the phone summary (contract 04 §8.2 and §8.3).

Both are pure functions of the call. The gate id is what makes a re-issue of
the *same* call land on the open gate and a re-issue with a changed argument
open a fresh one. The summary is what the operator reads before tapping.

```
  family + tool + canonical(args) --sha256--> first 16 hex = the gate id
                                 \
                                  `--render--> "ha_call domain=… service=…"
```

Three rules, each with its reason:

1. **No model writes either value** (invariant 14). A summary a model produced
   is a summary an agent can steer, and the tap authorizes the call the
   summary describes.
2. **The three fields are joined on NUL**, which no family name, no tool name
   and no JSON text can carry. Joining on nothing would let `chat` +
   `ha_call` and `chatha_call` + `` hash alike.
3. **The cut is visible.** §8.3 caps one field at 120 characters and the whole
   summary at 200. A cut field carries its full length and a digest, and a cut
   summary says how many fields it hides — a human must never read a short,
   clean sentence that quietly left out where the call lands.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from re import Pattern
from typing import Final

#: Contract 04 §8.2: the first 16 hexadecimal characters of the digest. Long
#: enough that a family cannot search for a collision with an action it was
#: not granted, short enough for the phone action string (§8.4).
GATE_ID_HEX_LEN: Final = 16

#: What `POST /approval/<gate>` accepts in its path. `\Z`, never `$`: Python's
#: `$` also matches before a final newline (`chaperone/AGENTS.md`).
GATE_ID_RE: Final[Pattern[str]] = re.compile(r"^[0-9a-f]{16}\Z")

#: §8.3's two caps.
SUMMARY_MAX_CHARS: Final = 200
FIELD_MAX_CHARS: Final = 120

#: How much of a cut field's digest is shown. Enough to compare against the
#: audit record's full `args`, and it authorizes nothing.
FIELD_HASH_CHARS: Final = 8

#: What separates the three hashed fields. A NUL cannot occur in any of them.
_FIELD_SEPARATOR: Final = "\x00"

#: Appended when the 200 character cap hides fields, so the reader sees that
#: something is missing rather than a sentence that reads complete.
_MORE_TEMPLATE: Final = " …(+{count} more)"


def canonical_args(args: Mapping[str, object]) -> str:
    """The argument object as one deterministic string.

    Sorted keys and no spaces, so a client that reorders its JSON re-issues
    the same call. `ensure_ascii=False` keeps text whole: a digest over
    escaped text and a digest over the text itself are different digests, and
    the choice must not be a default.
    """
    return json.dumps(args, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def gate_id(family: str, tool: str, args: Mapping[str, object]) -> str:
    """Contract 04 §8.2. Deterministic, and it carries no secret."""
    joined = _FIELD_SEPARATOR.join((family, tool, canonical_args(args)))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:GATE_ID_HEX_LEN]


def _render(value: object) -> str:
    """One argument value as JSON, on one line. A newline inside a string
    arrives escaped, so a caller cannot draw a second line under the real
    one in the push."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _clip(value: object) -> str:
    """§8.3's field rule: 120 characters, then the full length and a digest.

    A string is counted and hashed raw, the way a human counts it, so the
    number in the summary matches what the caller sent.
    """
    text = _render(value)
    raw = value if isinstance(value, str) else text
    if len(raw) <= FIELD_MAX_CHARS:
        return text

    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:FIELD_HASH_CHARS]
    return f"{text[:FIELD_MAX_CHARS]}…({len(raw)} chars, sha256:{digest})"


def _fit(text: str, tail: str) -> str:
    """`text` with `tail` appended, inside the 200 character cap."""
    return text[: SUMMARY_MAX_CHARS - len(tail)] + tail


def approval_summary(tool: str, args: Mapping[str, object]) -> str:
    """Contract 04 §8.3. Tool name first, then the arguments in canonical
    order, each capped on its own, and the whole capped at 200.

    Fields are added while they fit. The first one that does not turns into
    the marker, so a reader is told what the cap hid instead of reading a
    sentence that stops early and looks complete.
    """
    fields = [f"{key}={_clip(value)}" for key, value in sorted(args.items())]
    text = tool
    for shown, field in enumerate(fields):
        marker = _MORE_TEMPLATE.format(count=len(fields) - shown)
        candidate = f"{text} {field}"
        if len(candidate) + len(marker) > SUMMARY_MAX_CHARS:
            return _fit(text, marker)
        text = candidate

    return _fit(text, "")
