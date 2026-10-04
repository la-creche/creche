"""Adding an MCP server is one action — the reconciler's half.

`stage7-releases.md` §4.1 step 3. A server file lands in the registry, CI
validates it (contract 01b), and the reconciler sees a declared server the
host does not have. It then does **exactly two things, and it installs
nothing**:

1. Opens a secret gap for every named secret that has no value, so root can
   mint a token and push the operator one actionable notification (§4.3).
2. Files a release request for `mcp-servers` at `latest`,
   `requested_by: caregiver`.

Five rules, each with the reason it is a rule.

1. **The reconciler knows no secret value.** It asks whether a NAME has a
   file, never what is in one. §4.3 rule 4: no read verb exists anywhere.
   That is also why the GAP goes into a file root drains and not to the
   phone: root mints the token, so the token never passes through an
   operator-side process, which is what §4.3 step 2 puts the phone between.
2. **One request per gap, not one per tick.** The loop runs every few
   seconds and §3.2 rule 8 caps pending requests per requester, so a
   reconciler that filed one per pass would refuse itself and fill `done/`
   with `rate` entries. A marker names the outstanding request. The next
   one waits until that id is ledgered, or until it ages out — and a
   request that is STILL IN `requests/` never ages out, because root has
   not taken it. With the path unit off, an hour rule alone would file a
   copy of the same request every hour, and root drains in id order, so
   every copy after the first would be refused with `the request moves no
   component` and one phone push each. Waiting is the
   only thing a second copy could not improve on; the wait is REPORTED
   once it passes the hour, so an undrained spool is never quiet.
3. **A ledgered request is an ANSWER, not a clean slate.** Root can refuse
   `{"mcp-servers": "latest"}` with `mcp-servers=latest names no released
   tag: name a version instead`. A pass that treated the request as
   settled the moment `done/<id>.json` existed would file the same request
   again two seconds later. With the path
   unit enabled that is a loop — request, refuse, push, request — at the
   reconciler's cadence, to the operator's phone. So an answer holds the next
   identical request back until something that could CHANGE the answer
   has changed: the set of servers the request is for, the registry
   revision, or `ANSWER_FLOOR_S`.
4. **Every write is atomic and never leaves a `.json` behind.** §2.2:
   `creche-handover.path` is a `PathExistsGlob` that re-fires while any match
   remains, and `atomic_write`'s temporary name starts with a dot and ends
   with `.tmp`, so it neither matches the glob nor is touched.
5. **The marker goes down BEFORE the request.** A request that lands with
   no marker behind it is rule 3's loop in another shape: the next pass
   finds no marker and files the same request. In this order the worst a
   failed write costs is one request an hour (rule 2's age-out), and a
   marker this process cannot write at all costs no spool file.
"""

from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Final, cast

import yaml

from .atomic import atomic_write
from .paths import RELEASE_ROOT, STATE_ROOT

#: Contract 06 §7: one component carries every MCP server, so one release
#: installs every declared server the host is missing.
REQUEST_COMPONENT: Final = "mcp-servers"

#: §2.3: the version a request may name instead of a number.
LATEST: Final = "latest"

#: §2.3's `requested_by`. Recorded, never trusted for a decision. It keeps
#: the old name, so the ledger names one requester across the rename.
REQUESTED_BY: Final = "managerd"

#: §2.3's `kind`.
REQUEST_KIND: Final = "release"

#: Where the executor's per-server trees land (§4.2 step 5). Root owns
#: them, the operator reads the NAMES, and a name is all this module needs.
INSTALLED_ROOT: Final = Path("/opt/mcp")

#: §4.3's layout: one file per secret, root owned, the operator reads no value.
#: The operator reads the NAMES here, so the directory is `root:agents 0750`
#: (`bin/rework-release-visit.sh`'s fixed table) and each file inside it is
#: `root:root 0600`. No write bit for the group: the operator plants nothing. Under
#: root's own root, so the operator cannot rename it aside either.
SECRETS_NAME: Final = "secrets"
SECRETS_DIR: Final = RELEASE_ROOT / SECRETS_NAME

#: A SIBLING of the secrets directory, never a child of it. This one is
#: operator-writable, because the reconciler writes it. A gap directory
#: inside the secrets directory would make that directory operator-writable
#: too, and a writer of it can author a `<name>.enc` of its own: sealing
#: needs PUBLIC keys only, so the attacker pre-empts the name the operator is
#: about to fill.
GAPS_NAME: Final = "secret-gaps"
GAPS_DIR: Final = STATE_ROOT / GAPS_NAME
SECRET_SUFFIX: Final = ".enc"
GAP_SUFFIX: Final = ".json"

#: Contract 01b §4.1's `secret:<name>`. `mcp_wire` reads the names out of
#: `run.env` with it, and `release`'s own reader uses the same prefix, so
#: the two ends of invariant 18 agree on what a reference looks like.
SECRET_PREFIX: Final = "secret:"

#: §2.2's spool, under root's own root.
RELEASES_DIR: Final = "releases"
REQUESTS_DIR: Final = RELEASE_ROOT / RELEASES_DIR / "requests"
DONE_DIR: Final = RELEASE_ROOT / RELEASES_DIR / "done"

#: Where this module remembers the request it filed. Its own state, beside
#: every other thing the reconciler remembers. The NAME is separate so that
#: `caregiver serve` can put the marker under ITS state root rather than
#: under root's: the marker is the operator's own memory, not a spool file.
MARKER_NAME: Final = "mcp-request.json"
MARKER_FILE: Final = STATE_ROOT / MARKER_NAME

#: What the PEP actually serves, as root last wrote it
#: (`handover.executor.host.ROSTER_FILE`). Spelled twice on purpose:
#: the two packages share no module, and a wrong path here costs one
#: unnecessary release rather than anything unsafe.
ROSTER_NAME: Final = "upstreams.yaml"
ROSTER_FILE: Final = RELEASE_ROOT / ROSTER_NAME

#: A roster root wrote is not hostile input, and it is still read with a
#: cap: an unbounded read on a loop that runs every two seconds is a way to
#: spend the manager.
MAX_ROSTER_BYTES: Final = 1024 * 1024

#: The longest chain of merge keys that the roster reader follows, and the
#: most pairs that the merge keys of one roster copy.
#:
#: CONTRACT-QUESTION: `stage7-releases.md` §4.4 gives no limit for a merge
#: key in the roster, and PyYAML has none. A merge key copies the pairs of
#: its value, so a short text can make the reader use much memory and
#: time. The reading here is two limits, and a roster past one of them does
#: not read. The numbers are those of the Rust reader of a component
#: manifest (`rust/crates/creche-contracts/src/manifest/yaml.rs`). The
#: writer of the roster makes no merge key, so its file copies no pair. A
#: larger limit costs memory and time in each look.
MERGE_DEPTH_MAX: Final = 128
MERGE_PAIRS_MAX: Final = 65_536

#: How long an un-ledgered request that is GONE from `requests/` holds the
#: next one back. Past it, the reconciler asks again: root took the first
#: and never answered (a crash between `running/` and the ledger), so a
#: lost request must not silently stop every future install. A request
#: that is still in `requests/` is not lost, only untaken (rule 2): it is
#: waited on past this age, and reported from this age on.
MAX_REQUEST_AGE_S: Final = 3600.0

#: How long root's ANSWER holds the next identical request back.
#:
#: Twenty-four hours, and it is a choice. Three things could turn the
#: `no released tag` refusal into a success, and only two of them are visible
#: from this host: a server named or unnamed in the registry (the request's
#: own set), and an edit to any registry file (the revision). The third is
#: a `mcp-servers` tag appearing in agent-mcp, which is a repository this
#: process never reads. Without a floor that fault needs a person. With
#: one it is retried unattended, and a standing refusal costs ONE request
#: and one phone push a day rather than one per reconcile pass — about
#: 4300 a day at the 20-second heartbeat.
ANSWER_FLOOR_S: Final = 86_400.0

#: One ledger entry carries a whole resolved manifest and a 200-line log
#: tail (`executor/ledger.py`). The cap is `live_manifest.py`'s, spelled
#: again here because the two modules share nothing.
MAX_ENTRY_BYTES: Final = 1 << 20

#: How much of root's reason a report line, a status document and the
#: noticeboard carry. Root wrote it, so it is trustworthy about the release. It is
#: still DATA: it is bounded and made printable, never interpreted.
MAX_REASON_CHARS: Final = 200

#: An outcome word (`succeeded`, `restored`, `failed`, `refused`) and a
#: refusal's check name. Both are short by construction, and both are read
#: from a file, so both are capped.
MAX_STATUS_CHARS: Final = 32

#: How many server names the marker's own list is read with. The set it is
#: compared against is what the registry declares, and eleven families
#: between them declare a handful.
MAX_SERVERS_READ: Final = 64

#: What this module calls an answer it could not read. The entry EXISTS, so
#: root settled the request, and this pass does not know what it said.
UNKNOWN_STATUS: Final = "unknown"

#: `executor/ledger.py`'s `Outcome.SUCCEEDED`. A succeeded release whose
#: servers are STILL missing is a different message, not a different rule:
#: the release ran, the host still does not match the registry, so the
#: released `mcp-servers` does not carry that server and asking again at
#: `latest` runs the same release.
SUCCEEDED_STATUS: Final = "succeeded"

#: Group readable, like every other file the reconciler publishes.
FILE_MODE: Final = 0o640

#: Contract 01b §4.1's `secret:<key>`. It becomes a file name root reads
#: next, so it is checked here with `fullmatch` before it is used.
SECRET_NAME_RE: Final = re.compile(r"[a-z][a-z0-9_]{1,62}")

#: Contract 01b §1: a server name.
SERVER_NAME_RE: Final = re.compile(r"[a-z][a-z0-9-]{1,30}")

#: Contract 02 §2 and contract 06 §9: 26 characters of UPPER-case Crockford
#: base32, I, L, O and U absent.
_ALPHABET: Final = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_ULID_CHARS: Final = 26

#: The same 26 characters, read back. The marker is operator-written, so the
#: id it names is checked before it is believed.
ULID_RE: Final = re.compile(rf"[{_ALPHABET}]{{{_ULID_CHARS}}}")
_RANDOM_BITS: Final = 80
_CHAR_BITS: Final = 5


class Hold(StrEnum):
    """Why this pass files no request. Four values, not two: "root has
    said nothing yet", "root has not even taken it, and that has gone on
    for an hour" and "root has answered" wait different lengths of time
    and say different things to a reader."""

    NONE = "none"
    WAITING = "waiting"
    QUEUED = "queued"
    ANSWERED = "answered"


@dataclass(frozen=True)
class Answer:
    """Root's answer to one `mcp-servers` request, as the ledger wrote it.

    `at` is when THIS process read the answer, not a time root stamped.
    The floor runs off a clock this service owns, so a ledger with no
    time field and a root clock that disagrees both cost nothing. It
    lives in the marker, so it survives a restart.
    """

    request_id: str
    status: str
    reason: str
    check: str
    at: float
    servers: tuple[str, ...]

    @property
    def retry_at(self) -> float:
        """When the same request may be filed again with nothing else
        changed."""
        return self.at + ANSWER_FLOOR_S

    @property
    def ran(self) -> bool:
        """Whether the release RAN and left the servers missing anyway.
        A different sentence for a reader, and the same rule."""
        return self.status == SUCCEEDED_STATUS


@dataclass(frozen=True)
class RequestOutcome:
    """What one pass did about the `mcp-servers` request.

    At most one of the three is ever set. `servers` names what a NEW
    request asks for. `held` is the answer that stopped one. `queued` is
    one sentence about a request root has not taken for an hour or more:
    a problem for the log, because the release path is not draining, and
    not a fault for the status document, because nothing is held back —
    the request is there and runs the moment the path drains.
    """

    servers: tuple[str, ...] = ()
    held: Answer | None = None
    queued: str = ""


@dataclass(frozen=True)
class McpPaths:
    """Every path this module touches. A test passes temp directories."""

    requests: Path = REQUESTS_DIR
    done: Path = DONE_DIR
    secrets: Path = SECRETS_DIR
    gaps: Path = GAPS_DIR
    installed_root: Path = INSTALLED_ROOT
    marker: Path = MARKER_FILE
    #: The roster root writes at the end of an `mcp-servers` release
    #: (`handover.executor.roster`). Read-only here, and root's, so
    #: this process learns what is SERVED without asking the PEP anything.
    roster: Path = ROSTER_FILE


def paths_under(state_root: Path, release_root: Path) -> McpPaths:
    """Every path this module touches, derived from TWO roots.

    `state_root` is the operator's: the gaps this process writes and its own
    marker. `release_root` is root's: the spool, the sealed
    secrets and the roster. `caregiver serve` takes both as arguments, and
    on the host they ARE `STATE_ROOT` and `RELEASE_ROOT`. Deriving the
    spool from an argument rather than from the module's own constants is
    what keeps a scratch run out of root's real spool, and `cli` refuses a
    scratch state root paired with the real release root. `installed_root`
    keeps its absolute default: it is read, never written, and `/opt/mcp`
    is root's.
    """
    return McpPaths(
        requests=release_root / RELEASES_DIR / "requests",
        done=release_root / RELEASES_DIR / "done",
        secrets=release_root / SECRETS_NAME,
        gaps=state_root / GAPS_NAME,
        marker=state_root / MARKER_NAME,
        roster=release_root / ROSTER_NAME,
    )


def missing_servers(paths: McpPaths, declared: tuple[str, ...]) -> tuple[str, ...]:
    """Declared in the registry, absent from the host. Names only."""
    return tuple(
        sorted(
            name
            for name in declared
            if SERVER_NAME_RE.fullmatch(name) and not (paths.installed_root / name).is_dir()
        )
    )


def open_gaps(
    paths: McpPaths,
    secrets: Mapping[str, tuple[str, ...]],
    now: float,
) -> tuple[str, ...]:
    """§4.3 step 1: report every named secret that has no value.

    `secrets` maps a server name to the `secret:` names its file declares.
    A gap is opened for a server that is already installed too: a secret
    can be revoked after an install, and the server then runs
    `running: false` (contract 01b §4.1 rule 4) with nobody told.

    **One secret name is one gap, whatever the map says.** A name two
    servers share (contract 01b §4.3, `ha` and `ha-read` hold one Home
    Assistant token on purpose) arrives here under BOTH of them. There
    is one value to paste, `_write_gap` writes one file keyed on the
    name, and the operator gets one link — so this reports it once too. Reporting
    it twice would lose the second server's name to `_write_gap`'s early
    return, and count work nobody has to do.

    The servers are walked in sorted order so the one the gap names, and
    therefore the one the operator reads on their phone, is the same on every
    pass. Directory order is the declaring party's own input.
    """
    opened: list[str] = []
    for server in sorted(secrets):
        for name in _gaps_for(paths, server, secrets[server], now):
            if name in opened:
                continue

            opened.append(name)

    return tuple(opened)


def served_servers(paths: McpPaths) -> tuple[str, ...]:
    """The upstream names root's roster carries, or none.

    A roster this process cannot read is read as "nothing is served". That
    is the direction that asks for a release rather than the one that
    silently keeps a server running, and a release re-writes the roster
    from the registry, which is the repair.
    """
    try:
        with paths.roster.open("rb") as handle:
            # Read the cap, not the file and then the cap: slicing what
            # `read_bytes` returned meant the whole file was already in
            # memory, and the comment above claimed a control the code did
            # not have.
            raw = handle.read(MAX_ROSTER_BYTES + 1)

        if len(raw) > MAX_ROSTER_BYTES:
            return ()

        loaded: Any = _load_roster(raw.decode("utf-8"))
    except (OSError, ValueError, yaml.YAMLError, RecursionError):
        # ValueError covers bytes that are not UTF-8 and an integer past
        # the digit limit of the interpreter. Nesting past the limit of
        # the reader raises RecursionError. A merge key past a bound
        # raises a YAML error.
        return ()

    if not isinstance(loaded, dict):
        return ()

    names = cast("dict[object, object]", loaded)
    try:
        return tuple(sorted(str(name) for name in names))
    except ValueError:
        # A name is an integer past the digit limit of the interpreter.
        # The reader makes such an integer from a scalar in base 16.
        return ()


class _RosterLoader(yaml.SafeLoader):
    """The safe loader of PyYAML, with the two bounds on merge keys.

    `flatten_mapping` puts the pairs of each `<<` value into its mapping.
    It calls itself for a `<<` value, and its caller then copies the pairs
    of that value. This class counts the levels of those calls and the
    pairs before each copy, so the loader stops before the copy that
    passes a bound. An alias with no merge key copies nothing: each node
    has one value.

    A `<<` value with no pair counts as one pair. It copies nothing, and
    the loader still does work for it, so a count of zero leaves the time
    of a read with no bound. The Rust reader counts zero there."""

    def __init__(self, stream: str) -> None:
        super().__init__(stream)
        self._merge_depth = 0
        self._merged_pairs = 0

    def flatten_mapping(self, node: yaml.MappingNode) -> None:
        if self._merge_depth > MERGE_DEPTH_MAX:
            raise _merge_refusal("the merge keys nest too deep", node)

        self._merge_depth += 1
        try:
            super().flatten_mapping(node)
        finally:
            self._merge_depth -= 1

        if self._merge_depth == 0:
            return

        # The caller is the `<<` key of another mapping. It copies these
        # pairs next.
        self._merged_pairs += max(1, len(node.value))
        if self._merged_pairs > MERGE_PAIRS_MAX:
            raise _merge_refusal("the merge keys copy too many pairs", node)


def _merge_refusal(problem: str, node: yaml.MappingNode) -> yaml.YAMLError:
    return yaml.constructor.ConstructorError(None, None, problem, node.start_mark)


def _load_roster(text: str) -> Any:
    """The value of the one document of `text`, as `yaml.safe_load` gives
    it. Raises a YAML error for a document past a merge bound."""
    loader = _RosterLoader(text)
    try:
        # The loader makes the node graph first. An alias shares the node
        # of its anchor, so that step costs no more than the text. The
        # bounds apply to the step after it, which makes the value.
        return loader.get_single_data()
    finally:
        loader.dispose()


def stale_servers(paths: McpPaths, declared: tuple[str, ...]) -> tuple[str, ...]:
    """Served, and no longer declared. The REMOVE half of invariant 18.

    Without it, deleting `mcp/<name>/server.yaml` asked for nothing: the
    tree stayed installed, the roster kept naming it, and the upstream
    served for ever. "One file" has to mean the removal too, and a
    removal is the same one release — the executor writes the roster from
    the registry as it is, so a name that is gone from the registry is
    gone from the roster.
    """
    wanted = set(declared)

    return tuple(name for name in served_servers(paths) if name not in wanted)


def request_servers(
    paths: McpPaths,
    declared: tuple[str, ...],
    now: float,
    revision: str = "",
) -> RequestOutcome:
    """§4.1 step 3: file ONE request for `mcp-servers`, or nothing.

    `revision` is the registry's content revision. It is half of what
    "something changed" means (rule 3): an edit to any `mcp/<name>/
    server.yaml` — a corrected pin, a rebuilt lock — moves it without
    moving a single server NAME, and it is exactly the kind of edit that
    turns root's refusal into a success.
    """
    absent = set(missing_servers(paths, declared))
    missing = tuple(sorted(absent | set(stale_servers(paths, declared))))
    if not missing:
        # The host matches the registry. There is nothing to ask for and
        # nothing to hold back, whatever the marker still says.
        return RequestOutcome()

    hold, answer = _hold(paths, missing, revision, now)
    if hold is Hold.WAITING:
        return RequestOutcome()

    if hold is Hold.QUEUED:
        return RequestOutcome(queued=_queued_line(paths, now))

    if hold is Hold.ANSWERED:
        return RequestOutcome(held=answer)

    _file_request(paths, missing, revision, now)

    return RequestOutcome(servers=missing)


def _queued_line(paths: McpPaths, now: float) -> str:
    """One sentence for a request root has not taken in an hour or more.

    Whole hours, on purpose: the loop logs a problem line when it CHANGES,
    so a line that carried minutes would be logged every minute. The
    marker was read a moment ago by `_hold`; it is read again here rather
    than threaded through four return sites, and it is small.
    """
    marker = _read_marker(paths.marker, now)
    if marker is None:
        return ""

    hours = int(marker.age(now) // MAX_REQUEST_AGE_S)

    return (
        f"mcp-servers request {marker.request_id} has waited {hours} h in the spool "
        f"untaken: the release path is not draining"
    )


def new_ulid(now: float) -> str:
    """26 characters of upper-case Crockford base32: 48 bits of
    milliseconds, then 80 bits of randomness (contract 02 §2)."""
    value = (int(now * 1000) << _RANDOM_BITS) | int.from_bytes(os.urandom(10), "big")
    first = (_ULID_CHARS * _CHAR_BITS) - _CHAR_BITS

    return "".join(
        _ALPHABET[(value >> shift) & 0b11111] for shift in range(first, -_CHAR_BITS, -_CHAR_BITS)
    )


def _gaps_for(
    paths: McpPaths,
    server: str,
    names: tuple[str, ...],
    now: float,
) -> list[str]:
    if SERVER_NAME_RE.fullmatch(server) is None:
        return []

    opened: list[str] = []
    for name in names:
        if SECRET_NAME_RE.fullmatch(name) is None:
            continue

        if (paths.secrets / f"{name}{SECRET_SUFFIX}").exists():
            continue

        opened.append(name)
        _write_gap(paths, server, name, now)

    return opened


def _write_gap(paths: McpPaths, server: str, name: str, now: float) -> None:
    """One gap file for root to drain. It names a server and a secret, and
    it carries no value, because this process holds none.

    For a declared pair (contract 01b §4.3) `server` is the first name in
    sorted order, and the second name is NOT carried. Three fields is not
    a habit, it is the far end's contract: `handover.intake.gaps`
    holds a closed `GAP_KEYS` and refuses a body with any other key, and
    its `server` must be one name matching `SERVER_NAME_RE`. A gap that
    named the pair would reach the operator as no gap at all.
    """
    target = paths.gaps / f"{name}{GAP_SUFFIX}"
    if target.exists():
        return

    atomic_write(target, _encode({"server": server, "secret": name, "at": now}), mode=FILE_MODE)


def _write_request(paths: McpPaths, request_id: str, body: dict[str, object]) -> None:
    """§2.2: the file lands as `<ULID>.json` and by no other name."""
    atomic_write(paths.requests / f"{request_id}.json", _encode(body), mode=FILE_MODE)


def _file_request(paths: McpPaths, missing: tuple[str, ...], revision: str, now: float) -> None:
    """Rule 5: the marker first, the request second.

    Both writes are atomic, so the only failure that matters is a write
    that does not happen. The marker missing while the request exists is
    rule 3's loop; the request missing while the marker exists costs one
    hour of silence and then one retry (rule 2's age-out).
    """
    request_id = new_ulid(now)
    atomic_write(
        paths.marker,
        _encode(
            {"id": request_id, "servers": list(missing), "revision": revision, "at": now},
        ),
        mode=FILE_MODE,
    )
    _write_request(
        paths,
        request_id,
        {
            "id": request_id,
            "kind": REQUEST_KIND,
            "components": {REQUEST_COMPONENT: LATEST},
            "rollback_of": None,
            "requested_by": REQUESTED_BY,
            "requester_session": None,
            "ts": now,
        },
    )


def _hold(
    paths: McpPaths, missing: tuple[str, ...], revision: str, now: float
) -> tuple[Hold, Answer | None]:
    """What, if anything, holds the next request back.

    The marker is an operator-written file, so every field is read as hostile.
    Two shapes would hold the path back for ever: an `at` in the FUTURE,
    which makes `now - at` negative and so always under the cap, and an
    `id` no executor can ever write, which `done/` can therefore never
    hold. Either one would stop every `mcp-servers` request from then on,
    with nothing to say why.

    So EVERY unreadable field answers `Hold.NONE`, which files a request.
    That is the safe direction twice over: the path never goes quiet, and
    it cannot loop either, because filing a request rewrites the marker,
    so this module's own file takes over on the very next pass.
    """
    marker = _read_marker(paths.marker, now)
    if marker is None:
        return Hold.NONE, None

    if marker.servers != missing or marker.revision != revision:
        # The question moved, so root's answer was about a different one.
        # Checked before the ledger is read: a changed question files a
        # request whatever the old answer said.
        return Hold.NONE, None

    if marker.answer is not None:
        return _verdict(marker.answer, now)

    path = paths.done / f"{marker.request_id}.json"
    entry = _read_entry(path)
    if entry is not None:
        # First sighting of an answer this pass could READ. It goes into
        # the marker, so the floor runs off a time on disk and the ledger
        # entry — a whole resolved manifest and a 200-line log tail — is
        # read once rather than every two seconds for a day.
        answer = _answer_from(entry, marker, now)
        _remember(paths, marker, answer)

        return _verdict(answer, now)

    if path.exists():
        # The entry exists and this pass could not read it. Nothing is
        # written down, so a half-written entry resolves on the next pass
        # two seconds later and root's own words are reported then.
        return _verdict(_unread(marker, now), now)

    # Root has not answered. One outstanding request at a time.
    if marker.age(now) < MAX_REQUEST_AGE_S:
        return Hold.WAITING, None

    # An hour on. Two shapes, and only one of them is a lost request. Root
    # removes a request from `requests/` the moment it takes it (`spool.
    # start`), so a file still there means root has not looked yet: the
    # path unit is off, or the executor is down. A second copy would sit
    # behind the first and be refused once the first lands, so the wait
    # goes on and is REPORTED (rule 2). A file that is GONE with no entry
    # in `done/` was taken and never answered: that one ages out.
    if _still_queued(paths, marker.request_id):
        return Hold.QUEUED, None

    return Hold.NONE, None


def _still_queued(paths: McpPaths, request_id: str) -> bool:
    """Whether root has yet to take the marker's request.

    `requests/` is root's directory and this process writes into it, so
    it can look; the id is this module's own 26 characters (`_read_marker`
    refused anything else), so the path is never hostile. A directory
    this pass cannot read answers False, which files a request: the safe
    direction, as in `_hold` — one spare request an hour, never silence.
    """
    try:
        return (paths.requests / f"{request_id}.json").exists()
    except OSError:
        return False


def _verdict(answer: Answer, now: float) -> tuple[Hold, Answer | None]:
    """An answer holds the next identical request until its floor runs
    out. Past that the pass asks again, unattended."""
    if now < answer.retry_at:
        return Hold.ANSWERED, answer

    return Hold.NONE, None


@dataclass(frozen=True)
class _Marker:
    """The operator-written file, after every field has been checked."""

    request_id: str
    servers: tuple[str, ...]
    revision: str
    at: float
    answer: Answer | None

    def age(self, now: float) -> float:
        return now - self.at


def _read_marker(path: Path, now: float) -> _Marker | None:
    """None means "nothing holds the next request": the file is absent,
    or a field in it cannot be believed."""
    body = _read(path)
    if body is None:
        return None

    request_id = body.get("id")
    at = body.get("at")
    if not isinstance(request_id, str) or ULID_RE.fullmatch(request_id) is None:
        return None

    if not _is_past(at, now):
        # An `at` that is not a number, or is in the future. Both would
        # read as "younger than every cap" and wedge the path.
        return None

    revision = body.get("revision")

    return _Marker(
        request_id=request_id,
        servers=_server_list(body.get("servers")),
        revision=revision if isinstance(revision, str) else "",
        at=float(cast("float", at)),
        answer=_answer_in(body, request_id, now),
    )


def _answer_in(body: dict[str, object], request_id: str, now: float) -> Answer | None:
    """The answer this module wrote into the marker on an earlier pass.

    An `answered.at` that is not a past number makes the floor negative
    and the hold eternal, so it answers None: the pass then re-reads the
    ledger entry, and if that is gone it files a request.
    """
    answered = body.get("answered")
    if not isinstance(answered, dict):
        return None

    fields = cast("dict[str, object]", answered)
    at = fields.get("at")
    if not _is_past(at, now):
        return None

    return Answer(
        request_id=request_id,
        status=_word(fields.get("status"), MAX_STATUS_CHARS) or UNKNOWN_STATUS,
        reason=_word(fields.get("reason"), MAX_REASON_CHARS),
        check=_word(fields.get("check"), MAX_STATUS_CHARS),
        at=float(cast("float", at)),
        servers=_server_list(body.get("servers")),
    )


def _answer_from(entry: dict[str, object], marker: _Marker, now: float) -> Answer:
    """One ledger entry, read into this module's three words.

    `executor/ledger.py` writes `status`, `reason` and `refused_check`.
    Each is root-written and each is still bounded and made printable
    here, at the read, so nothing unbounded ever enters this process's
    own state or the marker it writes.
    """
    return Answer(
        request_id=marker.request_id,
        status=_word(entry.get("status"), MAX_STATUS_CHARS) or UNKNOWN_STATUS,
        reason=_word(entry.get("reason"), MAX_REASON_CHARS),
        check=_word(entry.get("refused_check"), MAX_STATUS_CHARS),
        at=now,
        servers=marker.servers,
    )


def _unread(marker: _Marker, now: float) -> Answer:
    """An answer that exists and could not be read. The floor runs from
    the REQUEST, not from now: a `done/` entry this process can never
    read would otherwise restart its own day on every pass."""
    return Answer(
        request_id=marker.request_id,
        status=UNKNOWN_STATUS,
        reason="",
        check="",
        at=min(marker.at, now),
        servers=marker.servers,
    )


def _remember(paths: McpPaths, marker: _Marker, answer: Answer) -> None:
    """Fold root's answer into the marker, keeping every other field."""
    atomic_write(
        paths.marker,
        _encode(
            {
                "id": marker.request_id,
                "servers": list(marker.servers),
                "revision": marker.revision,
                "at": marker.at,
                "answered": {
                    "status": answer.status,
                    "reason": answer.reason,
                    "check": answer.check,
                    "at": answer.at,
                },
            }
        ),
        mode=FILE_MODE,
    )


def _read_entry(path: Path) -> dict[str, object] | None:
    """One ledger entry, read the way `live_manifest.py` reads one: a
    symlink and an oversized file are refused, and a half-written entry
    answers None rather than raising inside the reconcile loop."""
    try:
        if path.is_symlink() or path.stat().st_size > MAX_ENTRY_BYTES:
            return None

        loaded: Any = json.loads(path.read_text("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError, RecursionError):
        return None

    if not isinstance(loaded, dict):
        return None

    return {str(key): value for key, value in cast("dict[object, object]", loaded).items()}


def _is_past(value: object, now: float) -> bool:
    """A time that is a number and is not in the future. NaN fails both
    comparisons, which is the answer this wants. So does an integer past
    the range of a float: the comparison takes it as it is, where `float`
    of it raises."""
    if not isinstance(value, int | float) or isinstance(value, bool):
        return False

    return 0.0 <= value <= now


def _server_list(value: object) -> tuple[str, ...]:
    """The marker's own set of server names, capped. A value that is not
    a list of names reads as the empty set, which equals no set the
    registry can declare, so the pass asks instead of believing it."""
    if not isinstance(value, list):
        return ()

    items = cast("list[object]", value)[: MAX_SERVERS_READ + 1]
    if len(items) > MAX_SERVERS_READ:
        return ()

    return tuple(one for one in items if isinstance(one, str))


def _word(value: object, cap: int) -> str:
    """A root-written string, made printable and bounded.

    It reaches a log line, a status document and the noticeboard. A newline
    in it is a second line a reader may believe, and a megabyte of it is
    a megabyte in every document this manager publishes.
    """
    if not isinstance(value, str):
        return ""

    kept = "".join(one if one.isprintable() else " " for one in value)

    return " ".join(kept.split())[:cap]


def _read(path: Path) -> dict[str, object] | None:
    try:
        loaded: Any = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError, RecursionError):
        return None

    if not isinstance(loaded, dict):
        return None

    return {str(key): value for key, value in cast("dict[object, object]", loaded).items()}


def _encode(body: dict[str, object]) -> bytes:
    return (json.dumps(body, sort_keys=True) + "\n").encode("utf-8")


def clock() -> float:
    """The reconciler's own clock, so a caller need not import `time`."""
    return time.time()
