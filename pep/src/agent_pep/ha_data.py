"""The `ha_call.data` fence (contract 04 §4.1 rule 3).

Home Assistant reads a target out of the service data payload as readily as
out of the URL. An unfenced `data` therefore lets a caller holding a grant for
one entity act on another:

    grant:  notify.mobile_app_example_phone
    call:   {"domain": "notify", "service": "mobile_app_example_phone",
             "data": {"entity_id": "lock.front_door"}}

The triple matches the fence and the effect lands somewhere else. A `notify.*`
body reaches a phone as a push, so its keys are allowlisted rather than merely
size-capped: `url` makes the push an arbitrary link and `actions` makes it a
button. A narrow autonomous family depends on both checks.

This module is the only copy of the check, and `pep/tests/test_ha_data.py`
pins its verdicts. Weakening one of them reopens the fault this fence closes.
"""

from __future__ import annotations

import json
from typing import Final, cast

#: Every key Home Assistant accepts as a target inside `data`.
TARGETING_KEYS: Final = frozenset(
    {"entity_id", "target", "device_id", "area_id", "floor_id", "label_id"}
)

#: The one domain whose `data` keys are allowlisted, because its effect is a
#: push notification on the operator's phone.
NOTIFY_DOMAIN: Final = "notify"
NOTIFY_ALLOWED_KEYS: Final = frozenset({"title", "message"})
NOTIFY_TITLE_MAX_CHARS: Final = 128
NOTIFY_MESSAGE_MAX_CHARS: Final = 1024

#: The whole object's serialized size. The verb schema caps the key count
#: (§4.1) and this caps the bytes, so neither one alone has to hold.
DATA_MAX_BYTES: Final = 8 * 1024

#: Key -> its own character cap, for the two keys `notify` allows.
_NOTIFY_CAPS: Final[dict[str, int]] = {
    "title": NOTIFY_TITLE_MAX_CHARS,
    "message": NOTIFY_MESSAGE_MAX_CHARS,
}


def refuse(domain: object, data: object) -> str | None:
    """Why `data` is refused, or None when it passes.

    `domain` and `data` arrive already checked against the verb schema, so a
    shape this does not recognize is one the schema let through on purpose:
    a call with no `data` at all.
    """
    if not isinstance(data, dict):
        return None

    body = cast("dict[str, object]", data)
    smuggled = sorted(TARGETING_KEYS & body.keys())
    if smuggled:
        return (
            f"data must not carry targeting keys {smuggled}; the entity comes "
            "from the granted {domain, service, entity_id} triple only"
        )

    oversized = _refuse_size(body)
    if oversized is not None:
        return oversized

    if domain != NOTIFY_DOMAIN:
        return None

    return _refuse_notify(body)


def _refuse_size(body: dict[str, object]) -> str | None:
    """The serialized size, not the key count: one key can hold a megabyte."""
    try:
        size = len(json.dumps(body, ensure_ascii=False).encode("utf-8"))
    except (TypeError, ValueError) as exc:
        return f"data must be JSON-serializable: {exc}"

    if size > DATA_MAX_BYTES:
        return f"data is {size} bytes; the cap is {DATA_MAX_BYTES}"

    return None


def _refuse_notify(body: dict[str, object]) -> str | None:
    """A push body is `title` and `message` only, each a bounded string."""
    extra = sorted(body.keys() - NOTIFY_ALLOWED_KEYS)
    if extra:
        return f"notify data may only carry {sorted(NOTIFY_ALLOWED_KEYS)}; got {extra}"

    for key, cap in _NOTIFY_CAPS.items():
        value = body.get(key)
        if value is None:
            continue
        if not isinstance(value, str) or len(value) > cap:
            return f"notify {key!r} must be a string up to {cap} chars"

    return None
