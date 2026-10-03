"""Fixtures for the family path (contract 04). Nothing here is a real token."""

from __future__ import annotations

import json
import os
from pathlib import Path

from chaperone.family_grants import FamilyGrants, token_digest

#: Obviously fake, and never a secret: the digest is what the file holds
#: (contract 04 §2.2), so the fixture value authorizes nothing anywhere.
FAMILY_TOKEN = "test-family-token-chat"
OTHER_FAMILY_TOKEN = "test-family-token-vault"


def make_grants(**over: object) -> FamilyGrants:
    base: dict[str, object] = {
        "version": 2,
        "family": "chat",
        "rev": "01K5J9QW3R7T0ZP4YB2H6N8M1D",
        "token_sha256": [token_digest(FAMILY_TOKEN)],
        "model_alias": "agent-router",
        "tools": {"kagi": ["kagi_search_fetch"]},
        "verbs": {
            "embed": {},
            "ha_call": {
                "allow": [
                    {"domain": "notify", "service": "mobile_app_example_phone", "entity_id": None}
                ]
            },
        },
        "delegates": [],
        "approval": [],
        "limits": {"pep_rpm": 60, "max_inflight_delegations": 2, "max_open_gates": 10},
    }
    base.update(over)
    return FamilyGrants.model_validate(base)


def write_grants(directory: Path, grants: FamilyGrants) -> Path:
    """The atomic write contract 04 §1.3 describes, as `caregiver` does it."""
    return write_grants_raw(directory, grants.family, json.dumps(grants.model_dump()))


def write_grants_raw(directory: Path, family: str, payload: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{family}.json"
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(payload, encoding="utf-8")
    os.chmod(tmp, 0o640)
    tmp.replace(path)
    return path
