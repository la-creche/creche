"""§4.1 step 3's CALLER: one reconcile pass, one MCP release request.

This module is the pass that calls `mcp_release.py`, and `loop.look` is
what calls this one. The reconciler does exactly two things and installs
nothing.

```
  loop.look  ->  load_registry  ->  mcp_pass  ->  open_gaps
                                             ->  request_servers
```

Five rules, each with the reason it is a rule.

1. **A pass never raises, and one broken read costs only its own
   answer.** Invariant 19, and the loop it runs inside converges eleven
   families. A planted gap directory, a full disk or a spool root
   somebody removed must cost one report line, never the reconcile loop
   and never the OTHER half of this pass.

   `Path.is_dir` re-raises EACCES where it swallows ENOENT, so a stat of
   `requests/` outside the guard, under a parent root left
   `root:root 0750`, would end the loop. And a `secrets/` left
   `root:root 0700` makes `open_gaps` raise on every pass: with both calls
   under ONE `try`, the raise would also skip `request_servers`, and
   invariant 18 would file nothing at all under a log line that blames a
   secret. So each half keeps its own guard whatever a directory's mode
   becomes.
2. **A host with no spool is quiet.** `requests/` is root's, created by
   hand, and a manager can run before root creates it. Logging an error
   every two seconds would
   teach the reader to skip the log.
3. **A secret named by two servers opens no gap, unless both files
   declared the sharing.** `stage7-releases.md` §6 row 8.
   `mcp_release._write_gap` keys the file on the SECRET name, so the
   second declaration would overwrite the first one's gap and the operator would
   be pushed one link naming one of the two servers. The far end is
   `read_registry`, which refuses the RELEASE. This is the near end:
   root must not mint a token for a name whose owner is ambiguous, and
   the refusal happens before any file is written.

   Contract 01b §4.3 is the exception, and `_agreed` is where it is
   applied. `ha` and `ha-read` are one Home Assistant user reached two
   ways, each file names the other under `shared_secrets`, and the owner
   is then not ambiguous at all: it is the pair. They get ONE gap, keyed
   on the secret (§4.3 rule 5). Without it the shared HA token would open
   no gap, the intake would never push the operator its link, and both HA
   upstreams would fail closed with nobody told.
4. **Only a server that PARSED is declared.** `load_registry` puts a file
   it could not read into `server_reports` and leaves it out of
   `servers`, so a broken declaration asks for nothing and says why.
5. **A held-back request is SAID in a document, not only a journal.**
   `mcp_release` rule 3 stops a refusal loop, and a
   request nobody files is a server that never arrives. Without a fault
   the only trace would be a log line every twenty seconds, which is the
   silence `pep_unreachable` was added to end. `fault()` is contract 05
   §3.3's `mcp_install_held`, `blocks_turns: false`, raised on every
   family because one registry and one `/opt/mcp` serve the whole fleet.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

from agent_family import Registry
from agent_family.server import McpServerFile

from .clock import now_rfc3339, rfc3339
from .faults import FaultEntry
from .mcp_release import (
    SECRET_PREFIX,
    Answer,
    McpPaths,
    RequestOutcome,
    open_gaps,
    request_servers,
)

#: The fixed reason a duplicate binding reports. Fixed, never built from
#: input: the names go in beside it, through `safe` below.
SECRET_TWICE: Final = "two servers name one secret"

#: How much of a hostile name a report line carries. A registry file is
#: operator-written and a report line reaches a log and the one view.
MAX_NAME_CHARS: Final = 64

#: Contract 05 §3.3's code for rule 5. A declared MCP server is not
#: installed, and the release that would install it has been answered, so
#: `managerd` holds the next identical request back.
MCP_INSTALL_HELD: Final = "mcp_install_held"

#: How many server names the fault's message carries. The rest are
#: counted, not named: a status document is read by a person on a phone.
MAX_SERVERS_NAMED: Final = 5

#: How many servers one shared secret may bind (contract 01b §4.3 rule 4).
#: One secret has at most one partner, so two is the whole of "shared".
PAIR: Final = 2


@dataclass(frozen=True)
class McpReport:
    """What one pass did, in the one view's terms.

    `problems` is what a reader acts on. It is never an exception and never
    a traceback: a registry the loop cannot use is a report about the
    registry, not a failure of the manager (invariant 19).
    """

    gaps: tuple[str, ...] = ()
    requested: tuple[str, ...] = ()
    problems: tuple[str, ...] = ()
    #: Root's answer to the last request, while it holds the next one
    #: back (rule 5). None means nothing is held: either a request went
    #: out, or the host matches the registry, or root has yet to answer.
    held: Answer | None = None

    def log_line(self) -> str:
        """One line for the loop, or an empty one when nothing happened.

        `gaps` is every gap that is OPEN after this pass, not the ones this
        pass opened: `open_gaps` answers the whole set each time. The loop
        logs the line when it changes (`loop._mcp_look`), so a gap that
        stays open for a day is one line, not 4300.
        """
        parts: list[str] = []
        if self.requested:
            parts.append(f"mcp-servers requested for {', '.join(self.requested)}")

        if self.gaps:
            parts.append(f"secret gaps open: {', '.join(self.gaps)}")

        held_line = self.held_line()
        if held_line:
            parts.append(held_line)

        parts.extend(self.problems)

        return "; ".join(parts)

    def held_line(self) -> str:
        """Why no request went out, in one bounded sentence.

        It names root's own reason, the ledger id and the servers still
        unserved, because those are the three things a reader needs to
        decide what to do. Two shapes, because two things went wrong and
        the repairs differ:

        - `succeeded`: the release RAN and the host still does not match
          the registry, so the released `mcp-servers` does not carry that
          server. The repair is a tag in agent-mcp.
        - anything else: root would not, or could not, run it. The repair
          is whatever root's reason names.
        """
        held = self.held
        if held is None:
            return ""

        names = _named(held.servers)
        if held.ran:
            return (
                f"mcp-servers release {held.request_id} succeeded and {names} still not "
                f"installed: the released component does not carry it. "
                f"No new request before {_stamp(held.retry_at)}"
            )

        check = f" ({held.check})" if held.check else ""
        reason = held.reason or "root wrote no reason"

        return (
            f"mcp-servers release {held.request_id} was {held.status}{check}: {reason}. "
            f"{names} still not installed, no new request before {_stamp(held.retry_at)}"
        )

    def fault(self) -> FaultEntry | None:
        """Contract 05 §3.3's `mcp_install_held`, or None.

        Fleet-wide, the same reading §3.3 gives `audit_unreadable` and
        `pep_unreachable`: one registry and one `/opt/mcp` serve every
        family, so a server nobody can install belongs to all of them.

        It does NOT block turns. A family whose grants name that server
        has one tool fewer, and every other turn it takes is unaffected.
        Stopping eleven families over one missing upstream would turn a
        missing tool into an outage the platform caused.
        """
        held = self.held
        if held is None:
            return None

        return FaultEntry(
            code=MCP_INSTALL_HELD,
            blocks_turns=False,
            since=_stamp(held.at) or now_rfc3339(),
            source="managerd",
            detail={
                "release_id": held.request_id,
                "release_status": held.status,
                "servers": list(held.servers[:MAX_SERVERS_NAMED]),
                "retry_after": _stamp(held.retry_at),
                "message": self.held_line(),
            },
        )


def mcp_pass(registry: Registry, paths: McpPaths, now: float) -> McpReport:
    """Open the gaps this registry needs, then file at most one request."""
    try:
        spool_is_there = paths.requests.is_dir()
    except OSError as exc:
        # Rule 1: `Path.is_dir` re-raises EACCES, so this stat sits inside
        # a guard.
        return McpReport(problems=(said(exc),))

    if not spool_is_there:
        # Rule 2. This host has no release spool.
        return McpReport()

    problems = _broken(registry)
    declared = tuple(sorted(registry.servers))
    secrets, clashes = _secrets_by_server(registry.servers)
    # Two calls, two guards, on purpose. They read different directories
    # with different owners, so one of them failing says nothing about
    # the other and must not answer for it (rule 1).
    gaps, gap_trouble = _gaps_of(paths, secrets, now)
    outcome, ask_trouble = _servers_asked_for(paths, declared, registry.revision, now)

    queued = (outcome.queued,) if outcome.queued else ()

    return McpReport(
        gaps=gaps,
        requested=outcome.servers,
        problems=(*problems, *clashes, *gap_trouble, *ask_trouble, *queued),
        held=outcome.held,
    )


def _gaps_of(
    paths: McpPaths, secrets: Mapping[str, tuple[str, ...]], now: float
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """§4.3 step 1's half: what was opened, and what went wrong.

    `secrets/` is root's and `secret-gaps/` is the operator's, so a mode on
    either is somebody else's decision and neither is this process's to
    repair.
    """
    try:
        return open_gaps(paths, secrets, now), ()
    except OSError as exc:
        return (), (said(exc),)


def _servers_asked_for(
    paths: McpPaths, declared: tuple[str, ...], revision: str, now: float
) -> tuple[RequestOutcome, tuple[str, ...]]:
    """§4.1 step 3's half: what was requested, or what holds a request
    back, and what went wrong."""
    try:
        return request_servers(paths, declared, now, revision), ()
    except OSError as exc:
        return RequestOutcome(), (said(exc),)


def said(exc: OSError) -> str:
    """One report line for a path this process could not use.

    The error's class and its own message, which carries the errno and
    the path. Contract 05's fault table is per FAMILY and holds no code
    for "the manager cannot read a fleet-wide path", so this line and the
    journal are the whole answer. Inventing a code no reader could clear
    is what §3.3.1 rule 8 exists to prevent.
    """
    return f"mcp: {type(exc).__name__}: {exc}"


def _broken(registry: Registry) -> tuple[str, ...]:
    """Rule 4. One line per server file that did not parse or did not
    validate. The one view reads the same reports through
    `Registry.all_reports`, so this line is a pointer and not a second
    copy of the issue list."""
    return tuple(
        f"mcp/{safe(name)}/server.yaml: {len(report.issues)} issue(s), see the registry report"
        for name, report in sorted(registry.server_reports.items())
        if not report.ok
    )


def _secrets_by_server(
    servers: Mapping[str, McpServerFile],
) -> tuple[dict[str, tuple[str, ...]], tuple[str, ...]]:
    """Every `secret:<name>` each server declares, and rule 3's refusals.

    A name two servers claim is dropped from BOTH of them, unless they
    agreed (`_agreed`). Dropping one would pick a winner, and which one
    it picked would depend on the directory order — the attacker's own
    input.
    """
    declared = _declared(servers)
    owners: dict[str, list[str]] = {}
    for server, names in declared.items():
        for name in names:
            owners.setdefault(name, []).append(server)

    clashing = {
        name
        for name, holders in owners.items()
        if len(holders) > 1 and not _agreed(servers, name, holders)
    }
    kept = {
        server: tuple(name for name in names if name not in clashing)
        for server, names in declared.items()
    }
    problems = tuple(
        f"{SECRET_TWICE}: {safe(name)} is named by {', '.join(safe(one) for one in owners[name])}"
        for name in sorted(clashing)
    )

    return kept, problems


def _agreed(servers: Mapping[str, McpServerFile], name: str, holders: list[str]) -> bool:
    """Contract 01b §4.3: did these servers agree to share this secret?

    True only for EXACTLY two servers whose `shared_secrets` each name
    this secret and the OTHER server. `ha` and `ha-read` are one Home
    Assistant user reached two ways, and §5 rule 5 makes that two files.

    Three tests, each one a rule of §4.3:

    1. Two holders, never three (rule 4). Three would need every member
       to name every other one, and no reader could then say which
       agreement was missing.
    2. Both sides, never one (rule 1). A one-sided declaration is the
       same refusal as none, because the attacker's own new file would
       otherwise bind a name whose owner never agreed (§6 row 8).
    3. Named, never inferred (rule 2). Nothing is read off the package,
       the version or the entrypoint. `github-code` and `github-platform`
       are also one binary and hold two credentials on purpose.

    A pair logs nothing. This pass is stateless and the loop runs it
    every few seconds, so there is no first sighting to hang an INFO
    line on and it would repeat for ever — rule 2's noise. The gap
    itself is already reported, under `gaps`.
    """
    if len(holders) != PAIR:
        return False

    first, second = holders

    return _side_agrees(servers, first, name, second) and _side_agrees(servers, second, name, first)


def _side_agrees(
    servers: Mapping[str, McpServerFile], server: str, secret: str, partner: str
) -> bool:
    """One side of the agreement: does `server` name this secret AND this
    partner in one `shared_secrets` entry?

    Both fields of the one entry, never two entries: a file that named
    the secret in one and the partner in another agreed to nothing.
    """
    file = servers.get(server)
    if file is None:
        return False

    return any(one.secret == secret and one.server == partner for one in file.shared_secrets)


def _declared(servers: Mapping[str, McpServerFile]) -> dict[str, tuple[str, ...]]:
    """`run.env`'s `secret:` references, per server, sorted and unique."""
    found: dict[str, tuple[str, ...]] = {}
    for name, server in sorted(servers.items()):
        names = {
            value.removeprefix(SECRET_PREFIX)
            for value in server.run.env.values()
            if value.startswith(SECRET_PREFIX)
        }
        found[name] = tuple(sorted(names))

    return found


def _named(servers: tuple[str, ...]) -> str:
    """The servers still unserved and their verb, named up to the cap
    and counted past it. A status document is read on a phone, and the
    registry's server list is operator-written."""
    if not servers:
        return "no server is"

    shown = ", ".join(safe(one) for one in servers[:MAX_SERVERS_NAMED])
    rest = len(servers) - MAX_SERVERS_NAMED
    if rest > 0:
        return f"{shown} and {rest} more are"

    return f"{shown} is" if len(servers) == 1 else f"{shown} are"


def _stamp(epoch: float) -> str:
    """Contract 05 §2.1's time shape, from this module's epoch seconds.

    An empty string when the number cannot be a time. `mcp_release`
    already refuses a stamp that is negative or in the future, so this
    is the second guard on one value, and it is here because the first
    one is in another module.
    """
    try:
        return rfc3339(datetime.fromtimestamp(epoch, UTC))
    except (OverflowError, OSError, ValueError):
        return ""


def safe(text: str) -> str:
    """A hostile name, made printable and capped.

    A registry directory name reaches a log line and the one view. It has
    already matched a pattern by the time a release reads it, but this
    runs BEFORE that: `load_registry` walks whatever directories exist.
    """
    kept = "".join(one if one.isalnum() or one in "._-" else "?" for one in text)

    return kept[:MAX_NAME_CHARS] if kept else "?"
