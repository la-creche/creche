"""Shared identifiers and timestamp forms for the family path.

Contracts 02 §2 and 04 fix these shapes once; every module on the family side
reads them from here so one name means one thing (`docs/rework/contracts/`).
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Final

#: Every pattern here ends in `\Z`, never `$`. Python's `$` also matches just
#: before a final newline, so `"chat\n"` would pass a `$` anchor and become a
#: family name that names a file. These values arrive from another process
#: (invariant 12), so the end of the string is the end of the string.
#:
#: Contract 02 §2. A family name carries no underscore, because contract 04
#: §8.4 splits the approval action string on one.
FAMILY_NAME_RE: Final = re.compile(r"^[a-z][a-z0-9-]{1,30}\Z")

#: Contract 02 §2. The prefix names the door: `owui-`, `tui-`, `job-`, `auto-`.
SESSION_ID_RE: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*\Z")

#: Contract 02 §2 and contract 04 §4.1: a ULID, 26 characters of Crockford
#: base32, one upper-case alphabet only. The examples in §6.3 and §7.3
#: match; a lower-case value is dropped,
#: same as any other malformed header (§3.1 rule 4).
ULID_RE: Final = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}\Z")

#: A server name (contract 01b §1) joined to its tool by a double underscore.
MCP_TOOL_SEPARATOR: Final = "__"

#: The one delegate tool the PEP synthesizes; never granted by name
#: (contract 04 §4 rule 1).
INVOKE_AGENT: Final = "invoke_agent"


def rfc3339_s(when: datetime) -> str:
    """UTC, second resolution, `Z` suffix — contract 05 §3.3's `since`."""
    return when.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def rfc3339_ms(when: datetime) -> str:
    """UTC, millisecond resolution, `Z` suffix — contract 04 §6.1's `ts`."""
    return when.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.") + f"{when.microsecond // 1000:03d}Z"


def is_family_name(value: str) -> bool:
    return FAMILY_NAME_RE.match(value) is not None
