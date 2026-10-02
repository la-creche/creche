"""Runtime reload of MCP upstreams (`stage7-releases.md` §4.4).

**Without it, adding an upstream needs a restart.** A `StdioUpstreamPool`
refuses any name absent from its boot probe, and a restart drops every
approval blocked in place, because those gates live in memory for fifteen
minutes. That is not acceptable under a design where an approval blocks a
running turn. Invariant 18, "adding an MCP server is one action", rests on
this module.

Probe A7 (`probes/pep-reload/`) proved reload safe with five conditions, all
of which are kept here and each of which is a real failure it hit:

1. The drain is bounded, above `UPSTREAM_CALL_TIMEOUT_S` and below
   `IDLE_TTL_S`. Below, the reload cuts off a caller about to answer. Above,
   one stuck upstream holds its credential open for ever.
2. The credential map is re-decrypted with the roster. `load_sops_secrets`
   runs once at startup, so an add of any server with its own key fails its
   probe on "missing secret" however well the reload works.
3. Reloads serialize. Two at once swap against a roster neither read.
4. Retirement never cancels an owner task. It sets the pool's closing event
   and awaits the task object, which keeps anyio's cancel scopes in one task.
5. A secret rotation is expressed as a REPLACE. A live generation keeps the
   credential it was spawned with.

`ReloadablePool` satisfies the `UpstreamPool` protocol, and that is not
enough. The family path also reads WHICH names serve and each
one's fences, and a copy frozen at start would leave the decision behind when
a reload moves the pool. `serving` is that second read, taken off the same
live state the calls route by, so the family path has one reader.

It routes over the PEP's own `StdioUpstreamPool`, one pool per live
generation of one upstream:

    ReloadablePool
      "kagi"    -> generation 3 -> StdioUpstreamPool({"kagi": spec_v3})
      "weather" -> generation 1 -> StdioUpstreamPool({"weather": spec_v1})
      retiring:    generation 2 -> StdioUpstreamPool({"kagi": spec_v2})  draining

One generation owns one process, one owner task and one credential set. That is
what makes the four cases of `stage7-releases.md` §4.4 cheap:

    added      build a generation, probe it, publish it
    replaced   build the new one first, publish it, drain the old one after
    removed    unpublish, drain, stop
    unchanged  the same object. Nothing is touched, so nothing can break.

anyio binds a cancel scope to the task that entered it. Every scope here lives
inside `StdioUpstreamPool._own`, the generation's owner task, and that same task
exits it. Retirement only sets an event and awaits the owner task object. It
never cancels an owner and never closes a session from outside. README question
6 states the guarantee in full.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final, Protocol

from agent_pep.fences import NO_FENCES, ServerFences, from_upstreams
from agent_pep.mcp_client import (
    IDLE_TTL_S,
    UPSTREAM_CALL_TIMEOUT_S,
    StdioUpstreamPool,
    UpstreamError,
    UpstreamSpec,
    UpstreamTool,
)

#: How long a retiring generation may wait for its in-flight calls. One call
#: cannot outlive `UPSTREAM_CALL_TIMEOUT_S`, so a generation still busy past
#: this is stuck, and holding its credential open costs more than failing it.
#: Probe condition 1 also puts it BELOW `IDLE_TTL_S`, and the assertion below
#: is what stops a later edit from moving either past the other.
log = logging.getLogger("agent_pep.reload")

DRAIN_TIMEOUT_S = UPSTREAM_CALL_TIMEOUT_S + 5.0

#: Probe condition 1 and README item 7, as a check rather than a comment:
#: `_Live.used` is stamped when a call is dispatched and never refreshed, so
#: a call longer than the idle TTL would have its process closed under it.
assert UPSTREAM_CALL_TIMEOUT_S < DRAIN_TIMEOUT_S < IDLE_TTL_S

#: §4.4 step 1: "root writes the new upstream set and signals the PEP."
#: **The signal is `SIGHUP`, and that is a deliberate choice.** The probe's
#: README calls a trigger "a new authenticated path into the crown jewel". A
#: signal adds no path at all: the kernel already decides who may send one,
#: and only root or the PEP's own uid can. Root writes `upstreams.yaml`,
#: then `systemctl kill -s HUP agent-pep`. No port, no bearer, no listener,
#: and nothing a sandbox can reach. `reload_wiring` installs the handler in
#: `app.py`'s lifespan.
TRIGGER = "SIGHUP"


class Action(StrEnum):
    """What a reload did, or tried to do, to one upstream."""

    ADD = "add"
    REPLACE = "replace"
    REMOVE = "remove"
    UNCHANGED = "unchanged"


@dataclass(frozen=True)
class UpstreamFailure:
    """One upstream the reload refused. The rest of the reload still applied."""

    name: str
    action: Action
    detail: str


@dataclass(frozen=True)
class ReloadReport:
    """What one reload did (invariant 19: a bad definition yields a report)."""

    added: tuple[str, ...] = ()
    replaced: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    unchanged: tuple[str, ...] = ()
    failed: tuple[UpstreamFailure, ...] = ()
    duration_s: float = 0.0

    @property
    def ok(self) -> bool:
        return not self.failed


@dataclass(frozen=True)
class Serving:
    """What the family path reads on every decision: the names
    of the upstreams that serve, and each one's fences.

    One value comes from one state of the roster, so a decision never pairs
    a name from one reload with a fence from another.
    """

    names: frozenset[str]
    fences: ServerFences


#: Before anything serves: no name, and so no fence.
NOTHING_SERVES: Final = Serving(names=frozenset(), fences=NO_FENCES)


def serving_of(specs: Mapping[str, UpstreamSpec]) -> Serving:
    """The names and the fences of these rows. A fence sits on its row by
    name (contract 01b §7), so the two arrive and leave together."""
    return Serving(
        names=frozenset(specs),
        fences=from_upstreams({name: spec.arg_denies for name, spec in specs.items()}),
    )


class PoolFactory(Protocol):
    """How a generation's engine is built. Tests inject an instrumented one."""

    def __call__(
        self,
        specs: dict[str, UpstreamSpec],
        secrets: dict[str, str],
        idle_ttl_s: float,
    ) -> StdioUpstreamPool: ...


@dataclass
class _Generation:
    """One upstream at one definition: one process, one owner task, one key."""

    name: str
    spec: UpstreamSpec
    pool: StdioUpstreamPool
    inflight: int = 0
    retired: bool = False
    drained: asyncio.Event = field(default_factory=asyncio.Event)


class ReloadablePool:
    """An `UpstreamPool` whose roster can change while the process runs.

    Satisfies the `agent_pep.mcp_client.UpstreamPool` protocol, so `app.py`
    holds one of these in place of a `StdioUpstreamPool` with no other
    change. `reload()` is the added method `stage7-releases.md` §4.4 asks for.
    """

    def __init__(
        self,
        specs: dict[str, UpstreamSpec],
        secrets: dict[str, str],
        idle_ttl_s: float = IDLE_TTL_S,
        drain_timeout_s: float = DRAIN_TIMEOUT_S,
        make_pool: PoolFactory = StdioUpstreamPool,
    ) -> None:
        self._boot = dict(specs)
        self._secrets = secrets
        self._idle_ttl_s = idle_ttl_s
        self._drain_timeout_s = drain_timeout_s
        self._make_pool = make_pool
        self._live: dict[str, _Generation] = {}
        self._retiring: set[asyncio.Task[None]] = set()
        self._stuck: list[str] = []
        # Probe README item 3. After a reload a refused name has no live spec,
        # so without this map it would vanish with no explanation, which reads
        # as "that server was never declared" rather than "that server was
        # refused". `/healthz` counts these (contract 04 §10).
        self._refusals: dict[str, UpstreamFailure] = {}
        # Reloads run one at a time. Each one reads the live roster, awaits a
        # probe, then swaps. Two of them interleaved would swap against a
        # roster each read before the other wrote it, and the result matches
        # neither request — a half-apply, which invariant 19 forbids.
        self._one_at_a_time = asyncio.Lock()

    async def start(self) -> ReloadReport:
        """Boot probe. Identical to a reload from an empty roster."""
        return await self.reload(self._boot)

    async def stop(self) -> None:
        for gen in list(self._live.values()):
            self._retire(gen)

        self._live = {}
        await self.wait_for_retirements()

    def tools(self, server: str) -> dict[str, UpstreamTool]:
        gen = self._live.get(server)

        return gen.pool.tools(server) if gen is not None else {}

    async def call(self, server: str, tool: str, args: dict[str, object]) -> str:
        gen = self._live.get(server)
        if gen is None:
            # The words the PEP already uses for an upstream that is not there,
            # so nothing downstream of `/call` has to learn a second phrase.
            raise UpstreamError(f"upstream {server!r} is not running")

        # No await sits between the lookup and the increment, and the loop is
        # one thread, so a retirement cannot take this generation's process out
        # from under a call that already chose it.
        gen.inflight += 1
        try:
            return await gen.pool.call(server, tool, args)
        finally:
            gen.inflight -= 1
            if not gen.inflight and gen.retired:
                gen.drained.set()

    async def reload(
        self,
        wanted: dict[str, UpstreamSpec],
        declared: dict[str, tuple[str, ...]] | None = None,
        secrets: dict[str, str] | None = None,
    ) -> ReloadReport:
        """Move the live roster to `wanted`. Never raises, never half-applies.

        `declared` is the tool list each `mcp/<name>/server.yaml` promises
        (§4.2, contract 01b §5 rule 3). An upstream that does not serve a
        tool it declares is refused, and the one it replaces keeps serving;
        one that serves more than it declares is accepted, the extra tools
        logged once and grantable to nobody.

        `secrets` is a re-decrypted credential map. A new upstream almost
        always brings a new key, and `load_sops_secrets` runs once at startup,
        so without this an add of any server with its own credential fails its
        probe on "missing secret". A live generation keeps the credential it
        was spawned with: rotating one is a replace, not a reload.

        Reloads serialize. A second one waits out the first one's probes,
        which costs it about one spawn.
        """
        started = time.monotonic()
        added: list[str] = []
        replaced: list[str] = []
        unchanged: list[str] = []
        failed: list[UpstreamFailure] = []
        fresh: dict[str, _Generation] = {}

        async with self._one_at_a_time:
            if secrets is not None:
                self._secrets = secrets

            removed = [name for name in self._live if name not in wanted]
            for gone in [name for name in self._refusals if name not in wanted]:
                # A name the roster no longer declares is not refused any
                # more. It is simply not asked for.
                del self._refusals[gone]

            for name, spec in wanted.items():
                current = self._live.get(name)
                if current is not None and current.spec == spec:
                    unchanged.append(name)
                    continue

                action = Action.REPLACE if current is not None else Action.ADD
                try:
                    fresh[name] = await self._build(name, spec, (declared or {}).get(name))
                except UpstreamError as exc:
                    # Fail closed for this upstream only, and before anything
                    # is published: on a replace the old one keeps serving.
                    refusal = UpstreamFailure(name=name, action=action, detail=str(exc))
                    failed.append(refusal)
                    self._refusals[name] = refusal
                    continue

                self._refusals.pop(name, None)
                (replaced if action is Action.REPLACE else added).append(name)

            # The swap and the retirement list are built with no await between
            # them, so no call can land on a generation that is already gone.
            retiring = [self._live[n] for n in (*removed, *fresh) if n in self._live]
            self._live = {n: g for n, g in self._live.items() if n not in removed} | fresh

            for gen in retiring:
                self._retire(gen)

        return ReloadReport(
            added=tuple(added),
            replaced=tuple(replaced),
            removed=tuple(removed),
            unchanged=tuple(unchanged),
            failed=tuple(failed),
            duration_s=time.monotonic() - started,
        )

    async def wait_for_retirements(self) -> None:
        """Block until every retiring generation has stopped. Tests use it."""
        while self._retiring:
            await asyncio.gather(*list(self._retiring))

    @property
    def secrets(self) -> dict[str, str]:
        """The credential map a reload would start a new generation with.

        `reload_wiring` reads it so a failed decryption can keep what the
        process already holds rather than dropping every credential
        (probe condition 2, applied to the failure case)."""
        return dict(self._secrets)

    @property
    def live_specs(self) -> dict[str, UpstreamSpec]:
        """The definition now serving each name. Proves a refusal did not apply."""
        return {name: gen.spec for name, gen in self._live.items()}

    @property
    def serving(self) -> Serving:
        """The names and fences of the generations serving now.

        Read off `_live`, which a reload replaces in one assignment, so a
        row's fences come and go with its process: a replaced row's leave
        with the old process, and a refused replacement keeps the old row's.
        A refused add has no row serving and so no fence, and a call to it
        answers "not loaded" like any name nothing serves.
        """
        return serving_of(self.live_specs)

    @property
    def stuck(self) -> list[str]:
        """Generations whose drain timed out. Their callers were cut off."""
        return list(self._stuck)

    @property
    def refusals(self) -> dict[str, UpstreamFailure]:
        """Declared names that are not serving, with the reason each one is
        not. `/healthz` counts them as `upstreams.refused`, so a refused
        upstream is visible rather than absent (invariant 19)."""
        return dict(self._refusals)

    async def _build(
        self, name: str, spec: UpstreamSpec, declared: tuple[str, ...] | None
    ) -> _Generation:
        """Probe one new generation, or raise with the reason it cannot serve."""
        pool = self._make_pool({name: spec}, self._secrets, self._idle_ttl_s)
        await pool.start()
        found = pool.tools(name)
        if not found:
            # `StdioUpstreamPool.start` logs the cause and swallows it. An
            # empty tool map is the only signal that reaches a caller.
            await pool.stop()
            raise UpstreamError(f"upstream {name!r} failed its boot probe")

        if declared is not None:
            # Contract 01b §5 rule 3. A declared tool the server does not
            # serve is an error: the catalog promised something no call can
            # reach. A served tool the file does not declare is a warning,
            # and it stays ungrantable: the catalog is the declared list,
            # and a grant is checked against it. `ha`, `ha-read`,
            # `github-code` and `github-platform` each declare a handful of
            # the forty-odd tools their server serves, so the second shape
            # is the fence working as written, not a fault.
            missing = sorted(set(declared) - set(found))
            if missing:
                await pool.stop()
                raise UpstreamError(
                    f"upstream {name!r} declares {missing} and does not serve "
                    f"{'it' if len(missing) == 1 else 'them'}: probed {sorted(found)}"
                )

            undeclared = sorted(set(found) - set(declared))
            if undeclared:
                log.warning(
                    "reload: upstream %r serves %d tool(s) its definition does not declare, "
                    "ungrantable: %s",
                    name,
                    len(undeclared),
                    ", ".join(undeclared),
                )

        return _Generation(name=name, spec=spec, pool=pool)

    def _retire(self, gen: _Generation) -> None:
        """Stop sending new calls to this generation and drain it in the background."""
        gen.retired = True
        if not gen.inflight:
            gen.drained.set()

        task = asyncio.create_task(self._drain_then_stop(gen))
        self._retiring.add(task)
        task.add_done_callback(self._retiring.discard)

    async def _drain_then_stop(self, gen: _Generation) -> None:
        try:
            await asyncio.wait_for(gen.drained.wait(), self._drain_timeout_s)
        except TimeoutError:
            self._stuck.append(gen.name)

        # `stop()` sets the pool's closing event and awaits the owner task. It
        # enters no scope of its own, which is why calling it from this task is
        # safe (`probes/pep-reload/README.md`, question 6).
        await gen.pool.stop()
