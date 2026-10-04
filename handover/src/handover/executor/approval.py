"""Step 5, the phone approval (`stage7-releases.md` §2.5).

**The approval binds to `manifest_sha256`, and root is the only actor that
ever computes it.** The requester sends intent. Root resolves, hashes its own
resolution, and builds the summary from that same resolution. Two
consequences, both load bearing:

1. A requester cannot show the operator one thing and deploy another. The summary
   and the deployed set come from one computation (§6 row 3).
2. An approval cannot be replayed onto a changed set. Step 8 re-resolves and
   compares, and `check_decision` below refuses a decision whose gate id is
   not this manifest's (§6 row 4).

This module builds the seam, not a second approval system. The phone
transport is the existing one: `Transport` is one function, the production
implementation posts to the PEP's approval hook, and the tests answer it
from a fake.

Nothing here holds a secret. A gate id is a digest of a public hash, so it
authorizes nothing on its own.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from ..errors import Refusal, RefusalCode
from ..resolve import Resolution

#: The live Node-RED flow's own rule, copied from `docs/node-red-mcp.md`
#: §A.2. The flow drops
#: any action string this refuses BEFORE the tap reaches anything, so root
#: checks its own string against it rather than discovering the drop as a
#: fifteen minute silence.
ACTION_RE: Final = re.compile(r"^AGENT_(APPROVE|DENY)_([0-9a-z]{26})_([0-9a-f]{16})$")

#: What the flow prefixes an approval with, and what root builds to check
#: its own string before it pushes.
APPROVE_PREFIX: Final = "AGENT_APPROVE"

#: §2.5: the shape the existing approval path already uses (contract 04 §8.2
#: takes the same first 16 hexadecimal characters).
GATE_ID_HEX_LEN: Final = 16

#: §2.5: what the manifest hash is salted with, so a release gate id can
#: never collide with a tool-call gate id computed the same way.
GATE_PURPOSE: Final = "release"

#: §2.5: decisions expire after 15 minutes, and step 5 waits that long.
APPROVAL_TTL_S: Final = 900.0

#: §2.5: the summary is field first, at most this many characters per field.
FIELD_MAX_CHARS: Final = 120

#: What a cut field ends with, so a reader never sees a short, clean line
#: that quietly left out where the release lands.
CUT_MARK: Final = "…"


class Verdict(StrEnum):
    """What came back from the phone. Anything but `granted` stops step 5."""

    GRANTED = "granted"
    DENIED = "denied"
    EXPIRED = "expired"
    TIMEOUT = "timeout"


@dataclass(frozen=True)
class Decision:
    """One answer. `gate` is what the phone was asked about, and it is
    checked against this release's own gate id before it is believed."""

    verdict: Verdict
    gate: str
    at: float | None = None


def not_granted(verdict: Verdict) -> str:
    """The ledger's `reason` for a gate that did not grant.

    One function, because two sides read the words: `check_decision` writes
    them and `follow` reads them back out of `done/<ULID>.json`, to tell a
    tap nobody gave from a tap that said no.
    """
    return f"the gate answered {verdict}"


def contracts_row(resolution: Resolution) -> str:
    """§2.5's `contracts` field, for root AND for the requester's preview.

    It is here rather than in either caller because the two must say the
    same thing: a preview that reads `6 contracts satisfied` for a set root
    will call `5 satisfied, 3 not verified` is a preview that promised what
    root did not do.

    "not verified" and never "failed": contract 06 §3.2's C1 reports a
    requirement whose contract has no provider in the set rather than
    refusing it, because root cannot tell "nothing provides it" from "its
    provider is not a tree under an install root".
    """
    satisfied = f"{len(resolution.contracts)} contracts satisfied"
    if not resolution.unprovided:
        return satisfied

    return f"{len(resolution.contracts)} satisfied, {len(resolution.unprovided)} not verified"


@dataclass(frozen=True)
class Summary:
    """§2.5's seven fields, in the order the operator reads them."""

    review: str
    components: str
    contracts: str
    restarts: str
    restore: str
    requested_by: str
    manifest: str

    def as_dict(self) -> dict[str, str]:
        return {
            "review": _cut(self.review),
            "components": _cut(self.components),
            "contracts": _cut(self.contracts),
            "restarts": _cut(self.restarts),
            "restore": _cut(self.restore),
            "requested_by": _cut(self.requested_by),
            "manifest": _cut(self.manifest),
        }


#: The seam. One function: ask the phone, wait, answer. The production
#: implementation is the existing transport; a test passes a fake.
#:
#: The first argument is the ACTION id, the second the gate. They are two
#: things and the live flow reads both: it builds
#: `AGENT_APPROVE_<action id>_<gate>` and drops the push unless the action
#: id is 26 lower-case Crockford characters (`phone.ACTION_RE`). The gate
#: alone decides which release a verdict belongs to.
Transport = Callable[[str, str, Summary, float], Decision]


def _cut(value: str) -> str:
    if len(value) <= FIELD_MAX_CHARS:
        return value

    return value[: FIELD_MAX_CHARS - len(CUT_MARK)] + CUT_MARK


def action_id_of(request_id: str) -> str:
    """The release id as the live flow's `[0-9a-z]{26}` field accepts it.

    Contract 06 §9 and contract 02 §2 fix every rework ULID as UPPER-case
    Crockford. The flow's pattern is lower case, so the id is lower-cased
    here and nowhere else. The conversion is exact: Crockford's alphabet
    lower-cased is a subset of `[0-9a-z]`, and it carries no underscore, so
    the action string still splits into exactly three fields.

    The release's own id goes here rather than a freshly minted one, so
    the operator reads an id they can grep for in `done/`. The requester writes it,
    and that costs nothing: `request.ULID_RE` has already fixed every byte
    to Crockford's 26, and the GATE is what a verdict is matched on.
    """
    return request_id.lower()


def release_action(action_id: str, gate: str) -> str:
    """The string root checks against the live tab's pattern before it
    pushes. The release leg itself builds `RWPOLL_APPROVE_<id>_<gate>`
    (`docs/rework/nodered/README.md`)."""
    return f"{APPROVE_PREFIX}_{action_id}_{gate}"


def gate_id(manifest_sha256: str) -> str:
    """§2.5: `sha256(manifest_sha256 + "release")`, truncated to 16 hex.

    One gate id per REQUEST, not per resolved set: `build_document` hashes
    the request id and `resolved_at` into `manifest_sha256`, so two
    requests for the same components ask two different questions. That is
    the safer property and it is what the code has always done — the
    docstring that claimed the opposite misled a reader into thinking a
    replay across requests was possible.
    """
    joined = f"{manifest_sha256}{GATE_PURPOSE}".encode()

    return hashlib.sha256(joined).hexdigest()[:GATE_ID_HEX_LEN]


def check_decision(decision: Decision, expected_gate: str, now: float | None = None) -> float:
    """Believe one decision, or refuse. Returns when the tap arrived.

    Three ways a decision is not believed, in order.

    1. It names another gate. That is a REPLAY: an approval the operator gave for
       a different manifest, presented for this one.
    2. It is not `granted`.
    3. It is OLDER than `APPROVAL_TTL_S`. §2.5 says decisions expire after
       fifteen minutes, and a transport that answers with an old granted
       decision was believed without this. `now` is the
       caller's clock; None skips the age check, which is what a caller
       with no clock to offer gets.

    The refusal names `approval`, never the value.
    """
    if decision.gate != expected_gate:
        raise Refusal(RefusalCode.APPROVAL, "approval", "the decision names another gate")

    if decision.verdict is not Verdict.GRANTED:
        raise Refusal(RefusalCode.APPROVAL, "approval", not_granted(decision.verdict))

    if decision.at is None:
        raise Refusal(RefusalCode.APPROVAL, "approval", "the decision carries no time")

    if now is not None and now - decision.at > APPROVAL_TTL_S:
        raise Refusal(RefusalCode.APPROVAL, "approval", "the decision is older than the gate")

    return decision.at


def deny_all(action_id: str, gate: str, summary: Summary, wait_s: float) -> Decision:
    """The transport a host with no phone path configured gets.

    It denies, it never hangs, and it never passes by default. An
    unconfigured approval path must stop a release, not wave it through
    (invariant 10).
    """
    del action_id, summary, wait_s

    return Decision(Verdict.DENIED, gate)
