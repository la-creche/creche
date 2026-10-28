"""The ACP door starts the family's facade before it starts a relay.

`acp-relay` only pipes to the facade's in-VM socket. An idle attended
family has no facade, so a relay alone finds no socket and exits. The door
therefore readies the supervisor link first, on the sandbox the relay goes
to. A facade that cannot start refuses the connection with
`sandbox_unavailable`, and no relay starts.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from attendance.acp_door import refusal_code
from attendance.acp_link import LinkState
from attendance.auth import Principal
from attendance.errors import ApiError, ErrorCode
from attendance.service import SessionService
from attendance_harness import FAMILY, SANDBOX, make_config, write_status
from fake_facade import FacadeFleet

#: 4000 plus HTTP 503: the close code of a `sandbox_unavailable` refusal.
SANDBOX_UNAVAILABLE_CLOSE = 4503


@dataclass(slots=True)
class Rig:
    service: SessionService
    facades: FacadeFleet


@pytest.fixture
async def rig(tmp_path: Path) -> AsyncIterator[Rig]:
    config = make_config(tmp_path)
    write_status(config.state_root)
    facades = FacadeFleet()
    service = SessionService(
        config, acp_factory=facades.factory, relay_factory=facades.relay, cold_start_wait_s=1.0
    )
    service.start()

    yield Rig(service, facades)

    await service.close()
    await facades.stop()


async def test_the_door_starts_the_family_facade(rig: Rig) -> None:
    relay = await rig.service.open_acp_relay(Principal.DOOR_ACP, FAMILY)

    link = rig.service.acp_link(SANDBOX)
    assert link is not None
    assert link.status.state is LinkState.READY
    assert rig.facades.dials == [SANDBOX]
    assert relay.sandbox == SANDBOX

    await relay.close()


async def test_a_second_connection_reuses_the_facade(rig: Rig) -> None:
    first = await rig.service.open_acp_relay(Principal.DOOR_ACP, FAMILY)
    second = await rig.service.open_acp_relay(Principal.DOOR_ACP, FAMILY)

    assert rig.facades.dials == [SANDBOX]
    assert len(rig.facades.relays) == 2

    await first.close()
    await second.close()


async def test_a_facade_that_cannot_start_starts_no_relay(rig: Rig) -> None:
    rig.facades.mismatched.add(SANDBOX)

    with pytest.raises(ApiError) as refused:
        await rig.service.open_acp_relay(Principal.DOOR_ACP, FAMILY)

    assert refused.value.code is ErrorCode.SANDBOX_UNAVAILABLE
    assert refusal_code(refused.value) == SANDBOX_UNAVAILABLE_CLOSE
    assert rig.facades.relays == []
