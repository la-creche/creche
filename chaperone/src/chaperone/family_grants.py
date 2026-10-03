"""Family identity: the per-family grant file, re-read on every call.

Contract 04 §1. `caregiver` writes `/srv/agents/state/rework/grants/<family>.json`
atomically and deletes it to revoke. The PEP `stat`s the file on every call and
re-parses on any change, so a permission change needs no restart, no signal and
no apply command (invariant 9).

Fail closed, three ways (§1.4):

1. Absent -> every call for that family is `unknown_token`.
2. Malformed, or a `version` this PEP does not know -> the same.
3. Either state raises `grants_stale` in the fault file (§1.6), so `caregiver`
   can show that the PEP is not serving the family's current grants.

A grant file is input from another process, so its size and shape are checked
before use (invariant 12). It holds digests, never a token (§2.2), so a stolen
grant file grants nothing.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Final, cast

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

from .family_ids import is_family_name
from .faults import FaultWriter

log = logging.getLogger("chaperone.family_grants")

#: Contract 04 §1.2. A version this PEP does not know fails that family closed.
GRANT_FILE_VERSION: Final = 2

#: A grant file is small: one family's tools, verbs and two digests. Anything
#: larger is corruption, and reading it into memory before deciding that would
#: be the corruption's win.
MAX_GRANT_FILE_BYTES: Final = 256 * 1024

#: Contract 04 §2.3: two digests is the maximum, so an abandoned rotation
#: cannot leave a third valid token.
MAX_TOKEN_DIGESTS: Final = 2

#: What this PEP reads when a grant file carries no `limits` block, which a
#: file written before contract 01 §3.6.1 landed does not. `caregiver` writes
#: all three on every file: `pep_rpm` and `max_open_gates` from its own
#: defaults, `max_inflight_delegations` from the caller family's own field.
DEFAULT_PEP_RPM: Final = 60
DEFAULT_MAX_INFLIGHT_DELEGATIONS: Final = 2
DEFAULT_MAX_OPEN_GATES: Final = 10

_ServerName = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9-]{1,30}$")]
_ToolName = Annotated[str, StringConstraints(pattern=r"^[A-Za-z][A-Za-z0-9_-]*$", max_length=128)]
_ActionName = Annotated[str, StringConstraints(min_length=1, max_length=256)]
_VerbName = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,31}$")]
_FamilyName = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9-]{1,30}$")]
_Digest = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class HaAllow(_Strict):
    """One triple in `verbs.ha_call.allow` (contract 04 §4.1)."""

    domain: str = Field(max_length=64)
    service: str = Field(max_length=64)
    entity_id: str | None = None


class VerbFence(_Strict):
    """The fence a family file granted one verb (contract 01 §3.5).

    One model for all five, because each verb takes at most one fence key and
    `extra="forbid"` then refuses a key no verb reads. A verb whose fence is
    `{}` carries none.
    """

    allow: tuple[HaAllow, ...] | None = Field(default=None, max_length=256)
    targets: tuple[_FamilyName, ...] | None = Field(default=None, max_length=64)
    components: tuple[str, ...] | None = Field(default=None, max_length=64)


class FamilyLimits(_Strict):
    pep_rpm: int = Field(default=DEFAULT_PEP_RPM, ge=1)
    max_inflight_delegations: int = Field(default=DEFAULT_MAX_INFLIGHT_DELEGATIONS, ge=1)
    max_open_gates: int = Field(default=DEFAULT_MAX_OPEN_GATES, ge=1)


class FamilyGrants(_Strict):
    """The on-disk contract between `caregiver` (writer) and the PEP (reader).

    `caregiver` expands `all` and `<server>__*` before it writes, so the PEP
    never reads a server file and never resolves a wildcard (contract 04 §1.2).
    """

    version: int
    family: _FamilyName
    rev: str = Field(min_length=1, max_length=128)
    token_sha256: tuple[_Digest, ...] = Field(min_length=1, max_length=MAX_TOKEN_DIGESTS)
    model_alias: str = Field(min_length=1, max_length=128)
    tools: dict[_ServerName, tuple[_ToolName, ...]] = Field(
        default_factory=dict[str, tuple[str, ...]], max_length=64
    )
    verbs: dict[_VerbName, VerbFence] = Field(default_factory=dict[str, VerbFence], max_length=16)
    delegates: tuple[_FamilyName, ...] = Field(default=(), max_length=32)
    approval: tuple[_ActionName, ...] = Field(default=(), max_length=256)
    limits: FamilyLimits = Field(default_factory=FamilyLimits)


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def parse_grants(raw: bytes, family: str) -> tuple[FamilyGrants | None, str]:
    """Parse one grant file. Returns the grants, or None and why not.

    The reason is written into the fault file, so it names the file's own
    problem and never quotes a digest.
    """
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        return None, f"grants/{family}.json: not JSON ({exc})"

    if not isinstance(data, dict):
        return None, f"grants/{family}.json: the top level is not an object"

    # The version decides whether this PEP understands the rest, so it is read
    # before the shape and reported on its own (contract 04 §1.4's last row).
    document = cast("dict[str, object]", data)
    version = document.get("version")
    if not isinstance(version, int) or isinstance(version, bool):
        return None, f"grants/{family}.json: version is not an integer"
    if version != GRANT_FILE_VERSION:
        return None, f"grants/{family}.json: unknown version {version}"

    try:
        grants = FamilyGrants.model_validate(document)
    except ValidationError as exc:
        return None, f"grants/{family}.json: invalid ({exc.error_count()} errors)"

    # §1.2: `family` equals the filename stem. A file that disagrees names a
    # family other than the one whose permissions it is filed under.
    if grants.family != family:
        return None, f"grants/{family}.json: names family {grants.family!r}"

    return grants, ""


@dataclass
class _Entry:
    """One family's cached state. `grants` is None while the family is
    denied; `last_rev` survives so the fault can name the last good
    revision (contract 04 §1.6 rule 2)."""

    dev: int
    ino: int
    size: int
    mtime_ns: int
    grants: FamilyGrants | None
    message: str
    last_rev: str | None


class FamilyStore:
    """Directory-backed family identity.

    Every lookup re-scans, so a rewritten file decides the next call and a
    deleted file denies it (invariant 9). `directory` is None only in a test
    that needs no grant files, and then nothing here ever runs.
    """

    def __init__(self, directory: Path | None, faults: FaultWriter) -> None:
        self._dir = directory
        self._faults = faults
        self._entries: dict[str, _Entry] = {}

    def _assert_fault(self, family: str, entry: _Entry) -> None:
        """Keep the fault file in step with what this family is being served."""
        if entry.grants is not None:
            self._faults.clear(family)
            return
        self._faults.raise_stale(family, entry.message, entry.last_rev)

    def _store(self, family: str, entry: _Entry) -> None:
        self._entries[family] = entry
        self._assert_fault(family, entry)

    def _mark_absent(self, family: str) -> None:
        """The file is gone: revoked, or never written. Contract 04 §1.4 row 3
        denies every call, and §1.6 rule 1 raises the fault."""
        previous = self._entries.get(family)
        last_rev = previous.last_rev if previous is not None else None
        self._store(
            family,
            _Entry(
                dev=0,
                ino=0,
                size=0,
                mtime_ns=0,
                grants=None,
                message=f"grants/{family}.json: absent",
                last_rev=last_rev,
            ),
        )

    def _load(self, family: str, path: Path) -> None:
        try:
            info = path.stat()
        except OSError:
            self._mark_absent(family)
            return

        cached = self._entries.get(family)
        unchanged = cached is not None and (
            cached.dev,
            cached.ino,
            cached.size,
            cached.mtime_ns,
        ) == (
            info.st_dev,
            info.st_ino,
            info.st_size,
            info.st_mtime_ns,
        )
        if unchanged:
            assert cached is not None  # unchanged implies a cached entry
            self._assert_fault(family, cached)
            return

        if info.st_size > MAX_GRANT_FILE_BYTES:
            message = f"grants/{family}.json: {info.st_size} bytes exceeds the cap"
            grants, raw_ok = None, message
        else:
            try:
                grants, raw_ok = parse_grants(path.read_bytes(), family)
            except OSError as exc:
                grants, raw_ok = None, f"grants/{family}.json: unreadable ({exc})"

        last_rev = grants.rev if grants is not None else (cached.last_rev if cached else None)
        self._store(
            family,
            _Entry(
                dev=info.st_dev,
                ino=info.st_ino,
                size=info.st_size,
                mtime_ns=info.st_mtime_ns,
                grants=grants,
                message=raw_ok,
                last_rev=last_rev,
            ),
        )

    def scan(self) -> None:
        """Re-read every grant file. Cheap: one `stat` per family, and a parse
        only when device, inode, size or modification time moved (§1.4)."""
        if self._dir is None:
            return
        try:
            present = {p.stem: p for p in self._dir.glob("*.json")}
        except OSError as exc:
            log.error("grants directory %s is unreadable: %s", self._dir, exc)
            return

        for family, path in present.items():
            if not is_family_name(family):
                # No family name means no fault path to report it on, so the
                # log is the whole report.
                log.error("ignoring grant file %s: the stem is not a family name", path)
                continue
            self._load(family, path)

        # A family the PEP has served before keeps its entry after its file
        # goes away, so the fault stays raised rather than vanishing with the
        # file that caused it.
        for family in [f for f in self._entries if f not in present]:
            self._mark_absent(family)

    def sweep_faults(self) -> frozenset[str]:
        """Re-read the grant file of every family that carries a fault, and
        answer the families whose fault that cleared (contract 04 §1.6 rule 7).

        Only the faulted families, because a call already re-reads the rest and
        a healthy fleet must cost nothing. A family whose file is still absent
        keeps its fault: absence may be an accident, and `caregiver` decides
        what it means (contract 05 §3.3.1 rule 9).
        """
        if self._dir is None:
            return frozenset()

        cleared: set[str] = set()
        for family in self._faults.open_families():
            # The names come from this process's own fault files, so they are
            # family names already. Checked anyway: a name builds a path here.
            if not is_family_name(family):
                continue

            self._load(family, self._dir / f"{family}.json")
            entry = self._entries.get(family)
            if entry is not None and entry.grants is not None:
                cleared.add(family)

        return frozenset(cleared)

    def lookup(self, token: str) -> FamilyGrants | None:
        """The family a bearer token resolves to, or None.

        Contract 04 §2.2 rule 4: constant-time comparison, never `==` on the
        digest. Every entry is compared, so the time taken says nothing about
        which family matched.
        """
        self.scan()
        if not token:
            return None
        digest = token_digest(token)
        found: FamilyGrants | None = None
        for entry in self._entries.values():
            if entry.grants is None:
                continue
            for known in entry.grants.token_sha256:
                if hmac.compare_digest(known, digest):
                    found = entry.grants
        return found

    def current(self, family: str) -> FamilyGrants | None:
        """What this PEP would serve that family right now, by name.

        `lookup` answers the same question from a bearer, and every call takes
        that path. This one exists for a held approval gate (contract 04
        §1.5.3): the gate outlives the decision that opened it, and re-reading
        by name keeps the token out of the object that waits.
        """
        self.scan()
        entry = self._entries.get(family)
        if entry is None:
            return None
        return entry.grants

    def served_rev(self, family: str) -> str | None:
        """The revision this PEP is serving for a family, or None when it is
        serving none. For the manifest and for tests."""
        entry = self._entries.get(family)
        if entry is None or entry.grants is None:
            return None
        return entry.grants.rev
