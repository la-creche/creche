"""Per-webhook bearer tokens: one file per declared webhook trigger, mode
0600.

Contract 05 §6.4 names this file and gives it a writer: `caregiver` mints
one per declared webhook and removes it when the declaration goes. This
module only READS it, and the directory layout below is the one place this
door's half of that agreement lives. The 0600 requirement IS enforced here,
the same way `attendance`'s own token loader enforces it for a door's token
(`attendance.atomic.is_group_or_world_readable`, re-derived rather than
imported: this package imports nothing from `attendance`).
"""

from __future__ import annotations

import hmac
import logging
from pathlib import Path

_LOG = logging.getLogger(__name__)

#: Contract 02 §3 rule 7's floor, applied here for the same reason: a
#: webhook token this short would make guessing it cheap. A route whose
#: token file fails this check is treated as unconfigured, not as a
#: crash — the same "one bad file is not an outage" reasoning as contract
#: 01 §7 rule 5. It simply cannot be reached, the same as an undeclared
#: route, so this check never distinguishes "misconfigured" from "absent"
#: to a caller (`routes.py`, `webhooks.py`).
MIN_WEBHOOK_TOKEN_BYTES = 32


def webhook_token_file(webhooks_dir: Path, family: str, name: str) -> Path:
    return webhooks_dir / family / f"{name}.token"


def read_webhook_token(path: Path) -> str | None:
    """The token at `path`, or None when it cannot be trusted.

    None covers every failure the same way: missing, unreadable, empty,
    short, not text, or readable by more than its owner. The caller never
    learns WHICH, because that distinction is exactly what an
    unknown-route 404 must not leak.
    """
    if _group_or_world_readable(path):
        _LOG.warning("trigger token %s is not mode 0600; treating its route as unconfigured", path)
        return None

    try:
        raw = path.read_bytes()
    except OSError:
        return None

    value = raw.strip()
    if len(value) < MIN_WEBHOOK_TOKEN_BYTES:
        if value:
            _LOG.warning(
                "trigger token %s is under %d bytes; treating its route as unconfigured",
                path,
                MIN_WEBHOOK_TOKEN_BYTES,
            )
        return None

    try:
        return value.decode("utf-8")
    except UnicodeDecodeError:
        _LOG.warning("trigger token %s is not UTF-8 text; treating its route as unconfigured", path)
        return None


def _group_or_world_readable(path: Path) -> bool:
    """True when anyone but the owner can read the file, or when the file
    does not exist yet (checked again, and handled the ordinary way, by
    the `read_bytes` call right after this one returns False for it)."""
    try:
        stat = path.stat()
    except OSError:
        return False

    return bool(stat.st_mode & 0o077)


def tokens_match(offered: str, expected: str) -> bool:
    """Constant time. `hmac.compare_digest` is
    documented to resist timing analysis across inputs of different
    lengths too, so the two are compared as-is with no length pre-check.
    """
    return hmac.compare_digest(offered.encode("utf-8"), expected.encode("utf-8"))
