"""Refusals. Invariant 19: a bad definition yields a report, never a half-apply.

Every failure in this package is one `Refusal`. It carries a code from a
CLOSED list, so the ledger's `refused_check` field (stage7-releases.md §2.6)
is never built from input. The human-readable `detail` may name a component,
a version or a field, but only after that value passed a strict pattern —
`safe_token` is the one door untrusted text goes through.
"""

from __future__ import annotations

import re
from enum import StrEnum


#: The refusal codes contract 06 names. `C1` to `C4` are its §3.2 rows; the
#: other four cover its §10 table. `STATE` is this package's own: the
#: live-state document is the resolver's second input and contract 06 does not
#: name it, so a bad one needs a code of its own rather than borrowing
#: `MANIFEST`, which would tell a reader the wrong file is broken. `RATE`,
#: `APPROVAL`, `DRIFT`, `P1` to `P5` and `MONOTONIC` are the executor's, and
#: `stage7-releases.md` §2.4 and §3.2 name every one of them. Nothing else may
#: reach the ledger.
class RefusalCode(StrEnum):
    REQUEST = "request"
    MANIFEST = "manifest"
    CATALOG = "catalog"
    CYCLE = "cycle"
    STATE = "state"
    C1 = "C1"
    C2 = "C2"
    C3 = "C3"
    C4 = "C4"
    #: §3.2 rule 8: more than eight pending requests from one requester.
    RATE = "rate"
    #: §2.4 step 5: a deny, a timeout, an expiry, or a decision bound to
    #: another manifest hash.
    APPROVAL = "approval"
    #: §2.5: the re-resolve at step 8 disagrees with what was approved.
    DRIFT = "drift"
    #: §2.4 step 3, the provenance predicate, one code per check.
    P1 = "P1"
    P2 = "P2"
    P3 = "P3"
    P4 = "P4"
    P5 = "P5"
    #: §2.4 step 3: a component may not go backwards outside a rollback.
    MONOTONIC = "monotonic"
    #: §4.2: one `mcp/<name>/server.yaml` root will not install from. Its
    #: own code and not `MANIFEST`, because the two files come from two
    #: repositories and a ledger reader has to know which one to open.
    SERVER = "server"
    #: Contract 06 §8.2: the staged tree reads code from outside itself. Its
    #: own code, because every other refusal says a DEFINITION is wrong and
    #: this one says the ARTIFACT is — the same manifest builds a good tree
    #: on the next run, and a reader who is told `manifest` opens the wrong
    #: file.
    EDITABLE = "editable"
    #: Contract 06 §1 rule 8: the unit this component's release restarts does not
    #: start a program inside `install.to`, so the release would swap a tree
    #: and change nothing that runs. Its own code, because neither the
    #: definition nor the artifact is wrong: the UNIT FILE on the host is,
    #: and it is installed by hand and never by a release.
    UNIT = "unit"
    #: Contract 06 §3.4: root will not read a repository on these terms — the
    #: wrong owner, a write bit for accounts root cannot enumerate, or a
    #: borrowed object store. Its own code, because no definition is wrong
    #: and no artifact is wrong: the DIRECTORY root was pointed at is, and a
    #: reader told `manifest` would open a file that is fine.
    SOURCE = "source"
    #: The site file is missing, unreadable, or holds a value this package
    #: will not use (`site.py`). Its own code, because nothing in any
    #: repository is wrong: the HOST's own configuration is, and a reader
    #: told `manifest` or `source` would look in a checkout.
    SITE = "site"


#: What a value may look like before it is quoted into a message. A manifest
#: from an agent's branch is hostile (stage7-releases.md §3.2), so an unknown
#: field name is still input. Anything outside this set prints as a placeholder
#: rather than reaching a log line, a terminal or the ledger.
_SAFE_TOKEN_RE = re.compile(r"[A-Za-z0-9_.:/=@-]{1,64}")

UNPRINTABLE = "<unprintable>"


def safe_token(value: object) -> str:
    """Quote an untrusted value, or replace it when it is not plainly safe."""
    if not isinstance(value, str):
        return UNPRINTABLE

    if not _SAFE_TOKEN_RE.fullmatch(value):
        return UNPRINTABLE

    return value


class Refusal(Exception):
    """One refusal, with the check that failed and what it was looking at."""

    def __init__(self, code: RefusalCode, subject: str, detail: str) -> None:
        super().__init__(f"{code}: {subject}: {detail}")
        self.code = code
        self.subject = subject
        self.detail = detail

    def as_dict(self) -> dict[str, str]:
        return {"check": str(self.code), "subject": self.subject, "detail": self.detail}

    def as_line(self) -> str:
        return f"refused [{self.code}] {self.subject}: {self.detail}"
