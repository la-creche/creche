"""One image reference per sandbox flavor (contract 01 §3.9).

    family.toml `sandbox.image`   the flavor, a closed set
             |
             v
    SandboxImages.resolve()       the reference this HOST was started with
             |
             v
    sbx create -t <reference>

The family file names a flavor; the platform names the images. Invariant
10 draws the line there: a registry file that could name an image would
choose what code runs inside the microVM.

A flavor with no reference resolves to `None`, never to `base`. The
caller raises `image_flavor_unconfigured` and creates nothing. A family
running on a filesystem its file does not ask for, with nothing anywhere
saying so, is the failure nobody goes looking for.

A leaf: it holds a value and answers questions about it, and imports no
sibling."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from agent_family import SandboxFlavor

#: The flavors that have an sbx image, one field of `SandboxImages` each.
#: `coding` has none: it is a root disk of the disks row only (CH7-1, CH53
#: rule 1), so a family that names it on sbx gets
#: `IMAGE_FLAVOR_UNCONFIGURED`. No family names it before the cut (CH79
#: rule 1).
SBX_FLAVORS: Final = (str(SandboxFlavor.BASE), str(SandboxFlavor.PYTHON))

#: Contract 05 §3.3. This host was given no image for a flavor a family
#: asked for. Not `sandbox_start_failed`: nothing was attempted, and the
#: fix is a platform argument, not a retry.
IMAGE_FLAVOR_UNCONFIGURED: Final = "image_flavor_unconfigured"


@dataclass(frozen=True)
class SandboxImages:
    """What this `caregiver` may create a sandbox from, by flavor.

    One field per member of `SBX_FLAVORS`, because a flavor with an sbx
    image is a `playpen/Dockerfile` target, a build in the cutover and an
    argument — a platform change, released together, never a mapping a
    caller can widen at runtime.

    An empty string means "this host was not given one", which is what
    every deployment before the second image is."""

    base: str
    python: str = ""

    def resolve(self, flavor: str) -> str | None:
        """The reference for that flavor, or None when there is none.

        None for an unknown flavor too. The validator refuses one, so a
        file that reaches here with one came another way, and guessing
        is the one answer this module may not give."""
        return self._by_flavor().get(flavor) or None

    def configured(self) -> tuple[str, ...]:
        """The flavors this host can actually serve, in contract order.
        It goes into the fault's detail, so a reader sees what is missing
        beside what is there."""
        return tuple(name for name, reference in self._by_flavor().items() if reference)

    def _by_flavor(self) -> dict[str, str]:
        return dict(zip(SBX_FLAVORS, (self.base, self.python), strict=True))
