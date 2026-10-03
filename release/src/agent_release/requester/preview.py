"""What the operator will see on the phone, printed before the request is filed.

`stage7-releases.md` §2.5 fixes seven fields and says **root is the only
actor that ever computes `manifest_sha256`**. So this module prints the seven
fields and never a gate id, and it says plainly which two rows root will
recompute. A requester that printed a gate would be claiming the approval
binds to its own arithmetic, which is exactly what §2.5 exists to stop.

Two rows differ from root's, always, and both say so on the line:

1. `review`. Root's says `safe:` only after the provenance predicate (§2.4
   step 3) has passed at the tag. This side has run no predicate, so it says
   `local:` and nothing stronger.
2. `manifest`. `build_document` hashes `resolved_at` and the request id into
   `manifest_sha256`, so root's clock alone makes its hash a different one.
   The local value is printed as an aid to reading the ledger afterwards,
   never as something to compare a tap against.

`Summary` itself comes from `executor/approval.py`, so the field list and the
120-character cut are the executor's own and not a second copy.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from ..catalog import Action, RestoreMode
from ..executor.approval import Summary, contracts_row
from ..executor.live_state import Chosen
from ..manifest import ComponentManifest
from ..resolve import Resolution

#: §2.5's `manifest` row is the first 12 hex of the hash.
HASH_CHARS: Final = 12

NO_UNIT: Final = "no unit"

#: The one line above the table. It is not a field: it is what keeps the two
#: recomputed rows from reading as promises.
HEADER: Final = (
    "what the operator's phone will show (root re-resolves at the tag and recomputes the hash)"
)


@dataclass(frozen=True)
class Preview:
    """The seven rows, and the local hash that is not a gate."""

    summary: Summary
    local_hash: str

    def rows(self) -> list[tuple[str, str]]:
        fields = self.summary.as_dict()

        return [(name, fields[name]) for name in fields]

    def lines(self) -> list[str]:
        return [f"  {name:<14}{value}" for name, value in self.rows()]


def preview_of(
    resolution: Resolution,
    chosen: Chosen,
    requested_by: str,
    local_hash: str | None = None,
) -> Preview:
    """§2.5's seven fields, from the resolution this side computed.

    `chosen` is `live_state.select_manifests`'s answer, which is the SAME
    function root runs over the same install trees, so this side never
    prints `6 contracts satisfied` for a set root refuses.
    """
    short = _short(local_hash)
    manifests = chosen.manifests

    return Preview(
        summary=Summary(
            review=_review(resolution, manifests, chosen.unverified),
            components="; ".join(_moves(resolution)),
            contracts=contracts_row(resolution),
            restarts=", ".join(_units(resolution, manifests)),
            restore=_restore(resolution, manifests),
            requested_by=requested_by,
            manifest=_manifest_row(short),
        ),
        local_hash=short,
    )


def _manifest_row(short: str) -> str:
    if not short:
        return "root computes it at step 2"

    return f"root computes it at step 2; this tree hashes to {short}"


def _short(local_hash: str | None) -> str:
    """Empty when this side could not hash: no live state, no facts."""
    if local_hash is None:
        return ""

    return local_hash.removeprefix("sha256:")[:HASH_CHARS]


def _review(
    resolution: Resolution, manifests: dict[str, ComponentManifest], unverified: tuple[str, ...]
) -> str:
    """Root's first field, hedged. See the module docstring, point 1.

    It carries root's `suspect` count too, because root computes it from
    the same install trees this side just walked. A preview that said
    nothing about it would leave the operator reading `suspect: 3` on the phone
    for the first time, with no way to tell it from a fault.
    """
    kinds = sorted({str(manifests[name].kind) for name in resolution.order if name in manifests})
    joined = "/".join(kinds) or "nothing"
    head = f"local: {len(resolution.order)} component(s), {joined}"
    if not unverified:
        return f"{head}; root checks provenance"

    return f"{head}; {len(unverified)} manifest(s) not verified ({', '.join(unverified)})"


def _moves(resolution: Resolution) -> list[str]:
    """`chaperone 2.0.3 → 2.1.0`, one per deploying component, in deploy order."""
    rows = {item.name: item for item in resolution.components}

    return [
        f"{name} {rows[name].from_version or 'absent'} → {rows[name].to_version}"
        for name in resolution.order
        if name in rows and rows[name].action is Action.DEPLOY
    ]


def _units(resolution: Resolution, manifests: dict[str, ComponentManifest]) -> list[str]:
    return [
        manifests[name].unit or f"{name}: {NO_UNIT}"
        for name in resolution.order
        if name in manifests
    ]


def _restore(resolution: Resolution, manifests: dict[str, ComponentManifest]) -> str:
    """§2.5: `manual` on the phone is the signal that a failed verify stops
    rather than reverses, and the operator must read it before the tap."""
    manual = [
        name
        for name in resolution.order
        if name in manifests and manifests[name].restore.mode is not RestoreMode.AUTOMATIC
    ]
    if not manual:
        return "automatic"

    return f"manual: {', '.join(manual)}"
