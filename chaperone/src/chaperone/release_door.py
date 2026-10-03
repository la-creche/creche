"""The `release` verb's one action: file a request into the rework spool.

It is invariant 17's path ("one approved action releases the platform") for
the `agent-control` family (`docs/rework/spec.md` §11.7).

```
  sandbox --POST /call {tool: release} --> PEP --> requests/<ULID>.json
                                        <-- {request: <ULID>}       |
                                                                    v
                                            root's executor, one phone tap
```

Four rules, each with its reason.

1. **The verb approves nothing.** `stage7-releases.md` §3.1: gating this call
   would ask the operator for two taps, one to let the request exist and one to
   approve the release. The tap that matters is the executor's at step 5,
   because it binds to what will actually happen (§2.5).
2. **The bytes are `agent_release.requester`'s, not a second copy.** The
   `agent-releasectl` command the operator types and this verb file the same shape
   through the same function, so a refusal reads the same on both sides and
   a rule added to `executor/request.py` reaches both.
3. **`requested_by` is a CLAIM.** This module writes the family the bearer
   resolved to, which is a fact on this side of the boundary and still only a
   claim on root's (§3.2). Root decides nothing on it.
4. **A refused request never becomes an upstream failure.** The requester's
   own cap answers `rate_limited` and a malformed argument answers
   `arg_validation`, both of them contract 04 §5 reasons, so the audit line
   says what happened rather than that something broke.

This module holds no policy. `family_app` decides, then calls.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Final, Protocol

from agent_release.errors import Refusal, RefusalCode
from agent_release.requester import REQUESTS_PATH, RequesterError, file_request, plan_request

from .family_decisions import FamilyReason

log = logging.getLogger("chaperone.release_door")

#: §2.3's two kinds. The schema in `verbs.py` holds the same pair.
RELEASE_KIND: Final = "release"
ROLLBACK_KIND: Final = "rollback"

#: Contract 04 §5's reasons this door can answer.
ARG_VALIDATION: Final[FamilyReason] = "arg_validation"
RATE_LIMITED: Final[FamilyReason] = "rate_limited"

#: Root reads the release source out of
#: `/srv/agents/code/<repo>`, which `code-corpus-sync.timer` refreshes
#: hourly, so a release filed minutes after a merge names a SHA the corpus
#: may not hold. `agent-releasectl request` fetches first, because it runs as
#: the operator. **This verb runs as `chaperone`**, which owns no corpus, holds no git
#: credential and must start no child (`release/AGENTS.md`), so it cannot.
#:
#: It also cannot KNOW whether the corpus is behind without running git. So
#: the note is unconditional: the caller is told what the executor will
#: refuse with and what fixes it, once, in the answer to every `release`
#: call. An occasional unnecessary line beats a caller who reads
#: `done/<ULID>.json` an hour later and has to work it out.
CORPUS_NOTE: Final = (
    "root reads the source from /srv/agents/code, refreshed hourly. If the release refuses "
    "for a SHA the corpus has not got, run /opt/agent-control/bin/sync-code-corpus.sh as "
    "the operator and ask again."
)


class ReleaseRefused(Exception):
    """The request is one root would refuse, said before it is written.

    It carries a contract 04 §5 reason, so `family_app` writes a DENY line
    rather than §5 row 11's allowed call that failed.
    """

    def __init__(self, reason: FamilyReason, detail: str) -> None:
        super().__init__(detail)
        # Annotated, not inferred: an inferred attribute widens a Literal
        # union to `str`, and `deny` takes the union.
        self.reason: FamilyReason = reason
        self.detail: str = detail


@dataclass(frozen=True)
class ReleaseRequest:
    """What one `release` call asks for, after the schema and the fence."""

    family: str
    session: str | None
    components: dict[str, str]
    kind: str = RELEASE_KIND
    rollback_of: str | None = None


class ReleaseDoor(Protocol):
    """The seam. A PEP without one leaves `release` a named seam, the way a
    PEP without a dispatch door leaves `enqueue` one."""

    def file(self, request: ReleaseRequest) -> str:
        """File it. Answers the request id, which is what `done/` is named
        after so the caller can read its own outcome."""
        ...


@dataclass(frozen=True)
class SpoolReleaseDoor:
    """The production door: one directory, and one file per call."""

    requests_dir: str = REQUESTS_PATH

    def file(self, request: ReleaseRequest) -> str:
        """Plan and write in one `try`: both halves raise the same refusal,
        and splitting them would give two places to map one code."""
        try:
            planned = plan_request(
                request.components,
                requested_by=request.family,
                now=time.time(),
                kind=request.kind,
                rollback_of=request.rollback_of,
                requester_session=request.session,
            )
            file_request(planned, self.requests_dir)
        except Refusal as refusal:
            raise _refused(refusal) from None
        except RequesterError as error:
            # The spool is missing or unwritable. That is this host's fault
            # and not the caller's, so it stays an execution failure.
            raise ReleaseSpoolError(str(error)) from None

        log.info("release request %s filed by %s", planned.id, request.family)

        return planned.id


class ReleaseSpoolError(Exception):
    """No spool on this host, or root's directory is not writable."""


def _refused(refusal: Refusal) -> ReleaseRefused:
    """One refusal code to one contract 04 §5 reason.

    `rate` is the only one that is not the caller's argument being wrong, and
    it is the one a caller should retry later rather than differently.
    """
    if refusal.code is RefusalCode.RATE:
        return ReleaseRefused(RATE_LIMITED, refusal.detail)

    return ReleaseRefused(ARG_VALIDATION, refusal.detail)
