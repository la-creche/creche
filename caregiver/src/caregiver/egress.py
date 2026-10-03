"""The manager's egress config: the two plane endpoints, and the canaries
that prove deny-by-default still holds (contract 01 §3.7, invariant 11).

Contract 01 §3.7 rule 4 keeps both plane endpoints out of every family
file, and rule 5 makes an empty `egress` list the normal attended case. So
the family file is never the whole allow list. This module holds the rest
of it, once, in one place a host can override.

    family.yaml egress: []          the normal attended case
              +
    EgressConfig.litellm/.chaperone always allowed, never listed
              =
    sbx policy allow network        what the sandbox may reach

Both defaults are IP literals on purpose. A family file may not name one
(§3.7 rule 1, so an egress list can never quietly reach a LAN host), and
The host's services bind the LAN IP rather than loopback, so this is the
address that answers. The address is the site's (`lan.py`)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from .lan import Port, endpoint


def litellm_endpoint() -> str:
    """Contract 01 §3.7 rule 4."""
    return endpoint(Port.LITELLM)


def pep_endpoint() -> str:
    """Contract 01 §3.7 rule 4."""
    return endpoint(Port.PEP)


def default_canaries() -> tuple[str, ...]:
    """Hosts that must stay unreachable from every family sandbox. One
    off-LAN name, the sbx `shell` kit's own non-editable allow, and one LAN
    neighbour (Open WebUI) that no sandbox has business reaching."""
    return ("example.com:443", "openrouter.ai", endpoint(Port.OPEN_WEBUI))


@dataclass(frozen=True)
class EgressConfig:
    """What `caregiver` adds to, and asserts about, every family's egress.

    Defaults are the addresses the contract names, on the site's LAN
    address, read when the config is built. A test host overrides them; no
    family file ever can."""

    litellm: str = field(default_factory=litellm_endpoint)
    chaperone: str = field(default_factory=pep_endpoint)
    canaries: tuple[str, ...] = field(default_factory=default_canaries)

    def allowed(self, family_egress: Sequence[str]) -> tuple[str, ...]:
        """The two planes first, then the family's own list in file order.
        Planes first so a truncated log still shows them."""
        planes = (self.litellm, self.chaperone)
        extra = tuple(one for one in family_egress if one not in planes)
        return (*planes, *extra)

    def denied(self, family_egress: Sequence[str]) -> tuple[str, ...]:
        """The canaries this family was not granted.

        A host the family file legitimately names is not a canary for that
        family: probing it as allowed and denied at once would fail every
        apply of such a family. Dropping it narrows the probe, never the
        policy -- the allow list is unchanged, and every remaining canary
        is still proved denied."""
        granted = {*family_egress, self.litellm, self.chaperone}
        return tuple(one for one in self.canaries if one not in granted)
