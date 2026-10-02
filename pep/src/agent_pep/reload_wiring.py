"""The trigger that makes `reload_pool` reload (`stage7-releases.md` §4.4).

`ReloadablePool` can move the live roster while the process runs. This
module is what calls it, and nothing else: without a trigger, invariant 18
stops one step short of a server answering.

```
  root writes upstreams.yaml   ->   systemctl kill -s HUP agent-pep
                                          |
                                    SIGHUP handler
                                          |
                        re-read the roster, re-decrypt the secrets
                                          |
                                    pool.reload(...)
```

**Why a signal and not an endpoint.** Probe A7's README calls a trigger "a
new authenticated path into the crown jewel". A signal adds no path at all:
the kernel already decides who may send one, and only root or the PEP's own
uid can. No port, no bearer, no listener, and nothing a sandbox can reach.
`reload_pool.TRIGGER` records the same choice beside the pool.

## Every assumption this module makes

1. **Every roster file is root's.** The PEP reads them, never writes them,
   and a file it cannot parse leaves the live pool untouched. Nothing here
   treats a parse failure as "no upstreams". There are two of them: the
   base roster the unit names as `PEP_UPSTREAMS`, which a deploy moves and
   which holds no row, and the one root generates from the servers an
   `mcp-servers` release installed (`agent_release.executor.roster`).
   `upstreams` says why two and why the GENERATED one wins a name.
2. **A reload that fails leaves the old pool serving, and says so.** Every
   path out of `reload_once` is caught and logged. A raise inside a signal
   handler would otherwise reach the event loop's exception handler and the
   operator would learn nothing. `UNREADABLE` enumerates what a read can
   raise, YAML's own errors included: without them a syntax error would
   raise out of the signal task.
3. **Reloads serialize.** `ReloadablePool.reload` holds a lock, and this
   module adds a second guard: one reload task at a time, with a repeat flag
   for a signal that arrived while one ran. Two tasks would queue on the
   pool's lock and each swap against a roster the other had already read.
4. **The secrets are re-decrypted with the roster** (probe condition 2). A
   new server almost always brings a new key, and `load_sops_secrets` runs
   once at startup, so without this an add of any server with a credential
   fails its probe on "missing secret". A decryption that fails keeps the
   secrets the process already had rather than dropping every credential.
5. **BOTH secret layouts are read.** The monolith, the `PEP_SECRETS` file,
   holds every credential that predates the intake.
   The per-secret store `/var/lib/agent-release/secrets/<name>.enc` is
   what the intake writes when the operator pastes a value, and it is the only one
   the host can author, because sealing needs public keys alone. Reading
   only the monolith would leave a pasted value where the PEP never looks,
   and the new upstream would start with no credential. A name in both
   takes the pasted value: the paste is the newer intent.
6. **A signal that arrives before the loop is running does nothing.** The
   handler is installed inside the lifespan, and removed when it ends.
7. **Installing the handler may fail, and the PEP still boots.** A loop in a
   worker thread (`TestClient`) cannot take signal handlers. The failure is
   logged and `reload_now` stays callable, which is what a test drives.
8. **The start is a reload.** The lifespan reads the roster
   through `start`, on this same path, with the credentials `__main__` just
   decrypted. A file that will not parse then does what it does at a
   reload: one log line, and the set serving before it keeps serving, which
   at start is no MCP server at all. It never stops the one enforcement
   point, and the next signal tries again. `health` is what `/healthz`
   shows of it. The start also runs §4.4 step 2's declared-tool check, so
   a restart never serves a row the next reload would refuse.
"""

from __future__ import annotations

import asyncio
import logging
import signal
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum, StrEnum
from pathlib import Path
from typing import Final

import yaml

from .mcp_client import UpstreamError, UpstreamSpec, load_upstreams
from .reload_pool import ReloadablePool, ReloadReport
from .secrets import SecretsError, load_secret_dir, load_sops_secrets

_log = logging.getLogger("agent_pep.reload")

#: §4.4 step 1's signal, and `reload_pool.TRIGGER` as a number.
TRIGGER_SIGNAL = signal.SIGHUP

#: How reading one YAML file fails, enumerated:
#:
#:   OSError         a file that will not open: gone, a directory, no permission
#:   ValueError      bytes that are not UTF-8, or a scalar YAML cannot build
#:                   (`2001-02-30` is a date with no day 30 in February)
#:   YAMLError       YAML that will not parse: the syntax, a tab, an alias
#:   RecursionError  nesting deeper than the parser can recurse
FILE_FAILURES: Final = (OSError, ValueError, yaml.YAMLError, RecursionError)

#: Everything `RosterSource.read` raises: a file failure, or a row of the
#: wrong shape. `reload_once` catches exactly these.
UNREADABLE: Final = (UpstreamError, *FILE_FAILURES)


class Credentials(Enum):
    """Where one read of the roster takes the upstream credentials from."""

    #: Both secret layouts again. A reload needs it: a new server brings a
    #: new key (assumption 4).
    DECRYPT = "decrypt"
    #: The map in memory. The read at start takes it, because `__main__`
    #: decrypted both layouts a moment before.
    IN_MEMORY = "in_memory"


class RosterState(StrEnum):
    """`/healthz`'s `roster.state`."""

    #: No read has run: the PEP has no roster source, or has not read it yet.
    OFF = "off"
    #: The last read parsed, and the pool applied it.
    OK = "ok"
    #: The last read failed. The set serving before it keeps serving, which
    #: at start is no MCP server at all.
    UNREADABLE = "unreadable"


@dataclass(frozen=True)
class RosterHealth:
    """What the last read of the roster found, and since when."""

    state: RosterState
    #: When `state` began: the first read of this streak. None while `OFF`.
    since: datetime | None = None


@dataclass(frozen=True)
class Roster:
    """One reading of what the PEP should be running."""

    specs: dict[str, UpstreamSpec]
    declared: dict[str, tuple[str, ...]]
    secrets: dict[str, str]


@dataclass(frozen=True)
class RosterSource:
    """Where a reload re-reads the roster. Every file here is root's.

    `secrets_file` is optional: a PEP whose upstreams need no credential
    still reloads, and a decryption failure keeps the credentials the
    process already holds (assumption 4).
    """

    upstreams_file: Path
    #: The monolith, the `PEP_SECRETS` file. Every upstream that predates
    #: the intake has its credential here.
    secrets_file: Path | None = None
    #: `stage7-releases.md` §4.3's layout: one `<name>.enc` per secret,
    #: which is what the release executor's intake writes when the operator pastes
    #: a value. Without this the two halves of "one action" do not meet —
    #: the value lands in a file the PEP never opens.
    secrets_dir: Path | None = None
    #: §4.4's second roster: what root wrote at the end of the last
    #: verified `mcp-servers` release (`agent_release.executor.roster`).
    #: Absent means no such release has run, which is not an error.
    generated_file: Path | None = None

    def read(
        self, current: dict[str, str], credentials: Credentials = Credentials.DECRYPT
    ) -> Roster:
        """The roster now. A roster file that will not read raises one of
        `UNREADABLE`, naming the file. The secrets never raise: a layout
        that will not read keeps `current` (assumption 4)."""
        specs = self.upstreams()
        declared = {name: spec.tools for name, spec in specs.items() if spec.tools}
        secrets = self._secrets(current) if credentials is Credentials.DECRYPT else current

        return Roster(specs=specs, declared=declared, secrets=secrets)

    def upstreams(self) -> dict[str, UpstreamSpec]:
        """The base roster, then the generated one. The GENERATED wins.

        Two files rather than one, because each has exactly one writer:
        the base is the deployed tree's `pep/upstreams.yaml` and moves
        with a deploy, and the generated one is root's and moves when an
        `mcp-servers` release lands. One file would need whichever writer
        ran last to know the other's content.

        The release wins a name because it is the newer, reviewed and
        verified fact. A registry writer does not write the generated
        file. Root does, at step 9 of a release the operator approved on
        their phone, from the `server.yaml` files of exactly the tree it just
        installed, with every `command` built by root from a validated
        name (`agent_release.executor.roster`, rule 1). A base row that
        won would keep serving a tree no release installed, with a
        success in the ledger.

        **The base file holds no row** (`stage7-releases.md` §4.4): the
        generated roster is the only source for a server the registry
        declares. An unreadable roster raises here, so a reload keeps the
        live pool and a start serves no MCP server. The merge stays for
        any row a base file does hold, and a superseded one is logged,
        once, by name, because an upstream that moves with nothing in the
        journal is invisible.
        """
        specs = _rows(self.upstreams_file)
        if self.generated_file is None or not self.generated_file.exists():
            return specs

        for name, spec in _rows(self.generated_file).items():
            if name in specs:
                _log.info("reload: the release supersedes the base row for %s", name)

            specs[name] = spec

        return specs

    def _secrets(self, current: dict[str, str]) -> dict[str, str]:
        """Both layouts, with the per-secret store winning a tie.

        It wins because it is the one the operator can write. A name that exists
        in both was pasted after the monolith was authored, and the paste
        is the newer intent.

        A layout that will not read keeps `current` whole: a
        decrypted monolith that is not YAML, a store that will not list, or
        a value that is not UTF-8 is assumption 4's failed decryption by
        another name.
        """
        if self.secrets_file is None and self.secrets_dir is None:
            return current

        try:
            found = dict(self._monolith(current))
            if self.secrets_dir is not None:
                found |= load_secret_dir(self.secrets_dir)
        except FILE_FAILURES as exc:
            # The type and never the text: a YAML error quotes the line it
            # failed on, and a decrypted line is a credential (invariant 13).
            _log.error(
                "reload: secrets unreadable (%s); keeping the ones in memory", type(exc).__name__
            )

            return current

        return found

    def _monolith(self, current: dict[str, str]) -> dict[str, str]:
        if self.secrets_file is None:
            return {} if self.secrets_dir is not None else current

        try:
            return load_sops_secrets(self.secrets_file)
        except SecretsError as exc:
            # Assumption 4's second half. Dropping every credential because
            # one decryption failed would take every running upstream down
            # on the next replace, which is the opposite of fail closed.
            _log.error("reload: secrets unavailable (%s); keeping the ones in memory", exc)

            return current


def _rows(path: Path) -> dict[str, UpstreamSpec]:
    """One roster file's rows. A file failure names the file:
    YAML's own errors do not, and an operator with two rosters must know
    which one to fix."""
    try:
        return load_upstreams(path)
    except FILE_FAILURES as exc:
        raise UpstreamError(f"{path}: {type(exc).__name__}: {exc}") from exc


class ReloadTrigger:
    """The handler, and the one reload it runs at a time (assumption 3)."""

    def __init__(self, pool: ReloadablePool, source: RosterSource) -> None:
        self.pool = pool
        self.source = source
        self._running: asyncio.Task[None] | None = None
        self._again = False
        self.reports: list[ReloadReport] = []
        self._health = RosterHealth(RosterState.OFF)

    @property
    def health(self) -> RosterHealth:
        """What the last read found, and since when (`/healthz`)."""
        return self._health

    async def start(self) -> None:
        """The first read, run by the lifespan before the PEP answers
        (assumption 8). A signal that lands meanwhile costs one more pass,
        as it does during any reload (assumption 3)."""
        if self._running is None or self._running.done():
            self._running = asyncio.create_task(self._run(Credentials.IN_MEMORY))

        await self._running

    async def reload_once(
        self, credentials: Credentials = Credentials.DECRYPT
    ) -> ReloadReport | None:
        """Read the roster and apply it. Never raises (assumption 2)."""
        try:
            roster = self.source.read(self.pool.secrets, credentials)
        except UNREADABLE as exc:
            self._mark(RosterState.UNREADABLE)
            _log.error(
                "reload: the roster is unreadable (%s); the set serving now keeps serving: %s",
                exc,
                sorted(self.pool.live_specs),
            )

            return None

        report = await self.pool.reload(roster.specs, roster.declared, roster.secrets)
        self._mark(RosterState.OK)
        self.reports.append(report)
        _log.info(
            "reload: added=%s replaced=%s removed=%s unchanged=%s failed=%s in %.2fs",
            list(report.added),
            list(report.replaced),
            list(report.removed),
            list(report.unchanged),
            [one.name for one in report.failed],
            report.duration_s,
        )
        for failure in report.failed:
            # Invariant 19: a bad definition yields a report. The upstream
            # it would have replaced is still serving.
            _log.error("reload: %s refused (%s): %s", failure.name, failure.action, failure.detail)

        return report

    def signal_arrived(self) -> None:
        """What the loop calls on `SIGHUP`. It must not block or raise."""
        if self._running is not None and not self._running.done():
            # A signal during a reload means the roster changed again. One
            # more pass after this one, and never a second task.
            self._again = True

            return

        self._running = asyncio.create_task(self._run())

    async def _run(self, first: Credentials = Credentials.DECRYPT) -> None:
        await self.reload_once(first)
        while self._again:
            self._again = False
            await self.reload_once()

    def _mark(self, state: RosterState) -> None:
        """A state keeps the time it began: "unreadable since" is the first
        failed read of a streak, not the latest."""
        if self._health.state is state:
            return

        self._health = RosterHealth(state, datetime.now(UTC))


def install(pool: ReloadablePool, source: RosterSource) -> tuple[ReloadTrigger, Callable[[], None]]:
    """Install the `SIGHUP` handler on the running loop.

    Returns the trigger and a remover the lifespan calls on the way out. The
    handler is installed on a best-effort basis (assumption 7): a loop that
    cannot take one leaves the trigger callable and the PEP serving.
    """
    trigger = ReloadTrigger(pool, source)
    try:
        loop = asyncio.get_running_loop()
        loop.add_signal_handler(TRIGGER_SIGNAL, trigger.signal_arrived)
    except (NotImplementedError, RuntimeError, ValueError) as exc:
        _log.warning("reload: no SIGHUP handler on this loop (%s)", exc)

        return trigger, _do_nothing

    def remove() -> None:
        try:
            loop.remove_signal_handler(TRIGGER_SIGNAL)
        except (NotImplementedError, RuntimeError, ValueError):
            # Shutting down. A handler on a loop that is closing cannot
            # fire, and failing here would mask the real shutdown path.
            _log.debug("reload: the SIGHUP handler was already gone")

    return trigger, remove


def _do_nothing() -> None:
    """The remover for a loop that never took a handler."""
