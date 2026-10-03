"""Seam 6: the image flavor a family file names reaches `sbx create`.

Packet SIM. `playpen/Dockerfile` has built two targets since stage 1
and nothing pulled the second one, so `code-sandbox` — `egress: []`, no
way to install anything — answered "plot my spending by month" with
`ModuleNotFoundError: No module named 'pandas'`.

    families/plots/family.yaml  sandbox.image: python
    families/chat/family.yaml   (no image field)
              |
              v  the REAL loop, one pass per family
    sbx create plots-s1 -t <the python image>
    sbx create chat-s1  -t <the base image>
              |
              v  the document sessiond's OWN reader says it may serve
    plots.sandboxes[0].image == the python image

Three things in one case, because they are one behaviour: the flavor
picks the image, the default picks the other, and moving the field
replaces the sandbox rather than leaving a family on a filesystem its
file no longer asks for.

`loop.look` with a `Passes` of its own is what `serve` runs each tick.
Only the two things that would touch a host are faked: the sandbox
driver and LiteLLM."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Final

import pytest
from agent_managerd import paths
from agent_managerd.driver import FakeDriver, SandboxSpec
from agent_managerd.egress import EgressConfig
from agent_managerd.litellm_keys import FakeLiteLLMKeys
from agent_managerd.loop import LoopConfig, LoopState, Passes, look
from agent_managerd.reconcile import Actors
from agent_managerd.switch import FakeSwitchClient
from agent_managerd.timers import FakeUnits
from agent_sessiond.family_status import FamilyState, StatusReader, check_may_serve

from .conftest import CHAT_FAMILY, FAMILY, IMAGE, write_family

#: The family that needs pandas. The name is the point of the packet.
PLOTS: Final = "plots"

#: The second flavor's reference. Distinct from `conftest.IMAGE`, so a
#: sandbox built from the wrong one is visible rather than equal.
PYTHON_IMAGE: Final = "sha256:0000000000000000000000000000000000000000000000000000000000000002"

WAIT_S: Final = 30.0


@pytest.fixture
def two_flavors(tmp_path: Path) -> Path:
    """`chat` takes the default. `plots` asks for `python`."""
    root = tmp_path / "registry"
    write_family(root, CHAT_FAMILY)
    write_family(
        root,
        {
            **CHAT_FAMILY,
            "name": PLOTS,
            "sandbox": {"cpus": 2, "memory": "2g", "image": "python"},
        },
    )
    return root


def _images_of(driver: FakeDriver) -> dict[str, str]:
    """Every `sbx create`, as {sandbox id: image reference}."""
    made: dict[str, str] = {}
    for call in driver.calls:
        spec = call.args[0] if call.op == "create" else None
        if isinstance(spec, SandboxSpec):
            made[spec.name] = spec.image

    return made


def _published_images(state_root: Path, family: str) -> list[str]:
    """Each sandbox's `image`, out of the document itself.

    `sessiond`'s reader drops the field — which image a sandbox came from
    is not its business — so this is the one reader that can see it, and
    contract 05 §4.1 says the document must carry it."""
    body = json.loads(paths.status_path(state_root, family).read_text(encoding="utf-8"))
    return [str(one["image"]) for one in body["sandboxes"]]


def _converge(registry: Path, state_root: Path, driver: FakeDriver) -> None:
    config = LoopConfig(
        registry_root=registry,
        state_root=state_root,
        image=IMAGE,
        python_image=PYTHON_IMAGE,
    )
    actors = Actors(driver, FakeLiteLLMKeys(), FakeSwitchClient(), EgressConfig(), FakeUnits())
    with Passes(config.max_concurrent_passes, grace_s=WAIT_S) as passes:
        look(config, actors, LoopState(), passes)
        assert passes.drain(WAIT_S) is True


def test_the_flavor_picks_the_image_and_a_change_of_it_replaces_the_sandbox(
    two_flavors: Path, tmp_path: Path
) -> None:
    state_root = tmp_path / "state"
    driver = FakeDriver()
    reader = StatusReader(state_root)

    _converge(two_flavors, state_root, driver)

    # 1 and 2: the flavor picks the image, the absent field picks `base`.
    assert _images_of(driver) == {f"{PLOTS}-s1": PYTHON_IMAGE, f"{FAMILY}-s1": IMAGE}

    # `sessiond`'s own reader, not an assertion about JSON: what it serves
    # is a family whose sandbox says which image it came from.
    served = reader.require(PLOTS)
    check_may_serve(served)
    assert served.state is FamilyState.IN_SYNC
    assert _published_images(state_root, PLOTS) == [PYTHON_IMAGE]

    # 3: the field moves, so the sandbox is replaced rather than left on a
    # filesystem the file no longer asks for.
    back_to_base: dict[str, Any] = {**CHAT_FAMILY, "name": PLOTS}
    write_family(two_flavors, back_to_base)
    driver.calls.clear()

    _converge(two_flavors, state_root, driver)

    assert _images_of(driver) == {f"{PLOTS}-s2": IMAGE}
    check_may_serve(reader.require(PLOTS))
    assert _published_images(state_root, PLOTS) == [IMAGE]
    assert "destroy" in driver.ops()
