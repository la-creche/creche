"""`ha_call.data` is fenced (contract 04 §4.1 rule 3).

Home Assistant accepts a target inside the service data payload, so an
unfenced `data` lets a caller holding a grant for one entity act on another.

Each case below pins a verdict, so a future edit that weakens `ha_data`
fails here.
"""

from __future__ import annotations

import pytest
from agent_pep import ha_data
from agent_pep.family_decisions import decide_family
from pep_family_helpers import make_grants

#: The `notify` triple both fixtures grant, so a refusal can only come from
#: `data`. `light.turn_on` is the second, for the checks `notify` short-cuts.
DOMAIN = "notify"
SERVICE = "mobile_app_example_phone"
LIGHT: dict[str, object] = {"domain": "light", "service": "turn_on"}

ALLOW: list[dict[str, object]] = [
    {"domain": DOMAIN, "service": SERVICE, "entity_id": None},
    {**LIGHT, "entity_id": None},
]

#: Every targeting key Home Assistant reads out of `data`. Each one alone is
#: enough to act on an entity outside the grant.
TARGETING_KEYS = ("entity_id", "target", "device_id", "area_id", "floor_id", "label_id")

#: One byte over the 8 KiB `data` cap.
OVER_THE_DATA_CAP = "x" * (8 * 1024)
#: One character over the `notify` message cap.
OVER_THE_MESSAGE_CAP = "x" * 1025


def family_verdict(args: dict[str, object]) -> tuple[bool, str | None]:
    grants = make_grants(verbs={"embed": {}, "ha_call": {"allow": ALLOW}})
    decision = decide_family(grants, "ha_call", args, rate_exceeded=False)
    return decision.allow, decision.reason


def notify(**data: object) -> dict[str, object]:
    return {"domain": DOMAIN, "service": SERVICE, "data": data}


# ---- the verdicts --------------------------------------------------------------------


@pytest.mark.parametrize("key", TARGETING_KEYS)
def test_the_fence_refuses_a_smuggled_target(key: str) -> None:
    """A grant for one entity must not reach another through `data`."""
    args = notify(**{key: "light.bedroom"})
    assert family_verdict(args) == (False, "arg_validation")


def test_the_fence_refuses_an_extra_notify_key() -> None:
    """A `notify.*` body reaches a phone as a push. `url` turns it into an
    arbitrary link, so the body is allowlisted, not merely size-capped."""
    args = notify(message="hi", url="https://evil.example")
    assert family_verdict(args) == (False, "arg_validation")


def test_the_fence_refuses_an_oversized_notify_message() -> None:
    args = notify(message=OVER_THE_MESSAGE_CAP)
    assert family_verdict(args) == (False, "arg_validation")


def test_the_fence_refuses_a_non_string_notify_title() -> None:
    args = notify(title=7)
    assert family_verdict(args) == (False, "arg_validation")


def test_the_fence_refuses_an_oversized_data_object() -> None:
    args: dict[str, object] = {**LIGHT, "data": {"note": OVER_THE_DATA_CAP}}
    assert family_verdict(args) == (False, "arg_validation")


def test_the_fence_allows_a_clean_notify_body() -> None:
    args = notify(title="Build", message="done")
    assert family_verdict(args) == (True, None)


def test_the_fence_allows_clean_data_outside_notify() -> None:
    args: dict[str, object] = {**LIGHT, "data": {"brightness_pct": 40}}
    assert family_verdict(args) == (True, None)


def test_the_fence_allows_a_call_with_no_data_at_all() -> None:
    args: dict[str, object] = {"domain": DOMAIN, "service": SERVICE}
    assert family_verdict(args) == (True, None)


# ---- the fence itself, held at 100% branch ---------------------------------


def test_a_smuggled_target_names_the_key_it_found() -> None:
    """The detail reaches the caller and the audit, so it names the key."""
    refused = ha_data.refuse(DOMAIN, {"device_id": "abc"})
    assert refused is not None
    assert "device_id" in refused


def test_absent_data_passes() -> None:
    assert ha_data.refuse(DOMAIN, None) is None


def test_an_unserializable_value_is_refused() -> None:
    """`data` comes from a JSON body today. It is still checked, because the
    only other outcome is a TypeError escaping into `internal_error`."""
    refused = ha_data.refuse("light", {"set": {1, 2}})
    assert refused is not None
    assert "JSON-serializable" in refused


def test_a_circular_value_is_refused() -> None:
    loop: dict[str, object] = {}
    loop["self"] = loop
    assert ha_data.refuse("light", loop) is not None


def test_data_at_the_cap_passes() -> None:
    """The cap is a limit, not a threshold: the last allowed byte is allowed."""
    room = ha_data.DATA_MAX_BYTES - len('{"note": ""}')
    assert ha_data.refuse("light", {"note": "x" * room}) is None
    assert ha_data.refuse("light", {"note": "x" * (room + 1)}) is not None


def test_an_empty_notify_body_passes() -> None:
    assert ha_data.refuse(DOMAIN, {}) is None


def test_a_notify_title_at_its_cap_passes() -> None:
    at_cap = "x" * ha_data.NOTIFY_TITLE_MAX_CHARS
    assert ha_data.refuse(DOMAIN, {"title": at_cap}) is None
    assert ha_data.refuse(DOMAIN, {"title": at_cap + "x"}) is not None


def test_a_non_string_notify_message_is_refused() -> None:
    refused = ha_data.refuse(DOMAIN, {"message": ["hi"]})
    assert refused is not None
    assert "message" in refused
