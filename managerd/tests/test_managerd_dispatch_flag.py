"""`managerd` publishes whether a family may be dispatched to.

Contract 02 §13.4.1 rule 3 refuses a dispatch to a family that declares no
`enqueue: true` trigger (contract 01 §3.13). `attendance` never reads a family
file, so the fact reaches it through the status document's `triggers.enqueue`
(contract 05 §2.1). The reader defaults it to `False`: without the key,
every `enqueue` call is refused `dispatch_not_declared`.
"""

from __future__ import annotations

import json
from pathlib import Path

from agent_managerd import paths
from agent_managerd.apply import apply_once
from agent_managerd.driver import FakeDriver
from agent_managerd.litellm_keys import FakeLiteLLMKeys
from agent_managerd.status import TriggersBlock
from managerd_helpers import write_registry

WORKER = "worker-docs"
IMAGE = "sha256:" + "0" * 63 + "1"


def _published_triggers(tmp_path: Path, triggers: list[dict[str, object]]) -> dict[str, object]:
    registry = write_registry(
        tmp_path / "registry", name=WORKER, kind="autonomous", triggers=triggers
    )
    state_root = tmp_path / "state"
    result = apply_once(
        registry,
        WORKER,
        state_root=state_root,
        image=IMAGE,
        driver=FakeDriver(),
        litellm=FakeLiteLLMKeys(),
    )
    assert result.ok, result.status

    document = json.loads(paths.status_path(state_root, WORKER).read_text(encoding="utf-8"))
    block = document["triggers"]
    assert isinstance(block, dict)

    return block


def test_a_dispatch_trigger_is_published(tmp_path: Path) -> None:
    block = _published_triggers(tmp_path, [{"enqueue": True}])

    assert block["enqueue"] is True
    assert block["webhooks"] == []


def test_a_family_with_no_dispatch_trigger_publishes_false(tmp_path: Path) -> None:
    """Absence is denial, and the key is written either way, so a reader can
    tell "this managerd says no" from "this managerd is too old to say"."""
    block = _published_triggers(tmp_path, [{"cron": "0 6 * * *"}])

    assert block["enqueue"] is False


def test_the_block_defaults_to_no_dispatch() -> None:
    assert TriggersBlock().as_json() == {"webhooks": [], "enqueue": False}
