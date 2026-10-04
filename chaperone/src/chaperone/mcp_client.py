"""MCP upstreams: stdio child processes of the PEP.

Each server gets only its own credentials in env, resolved from the decrypted
secrets mapping via `secret:<key>` references. Servers are pinned and
pre-installed; nothing here fetches code at runtime beyond what the pinned
command itself does.

Each server also runs as its own user, `mcp-<name>`, and never as `chaperone`
(contract 01b §9). The PEP starts every one through `chaperone-as`
(`run_as.py`), which drops the child to that user before it execs the
row's command:

    chaperone-as mcp-<name> <command> <args...>     PEP_CHILD_ENV=<row env>

Upstreams are probed once at startup and then run on demand: a board that nobody
is talking to costs nothing, and the tools `/manifest` publishes still come from
that one boot probe (see `StdioUpstreamPool`)."""

from __future__ import annotations

import asyncio
import json
import logging
import sysconfig
import time
from contextlib import AsyncExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol, cast

from handover.mcpserver import DEFAULT_PYTHON, ServerFile, ServerPin, Source
from mcp import ClientSession, MCPError, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import ListToolsResult, TextContent, ToolAnnotations

from . import bounded_yaml
from .fences import ArgDeny
from .run_as import CHILD_ENV_VAR

log = logging.getLogger("chaperone.mcp")

# How long an upstream may sit unused before its process is closed, and how often
# an owner task checks. The TTL is a knob, not a policy: nothing about who may
# call what changes with it, only whether a process is already warm.
IDLE_TTL_S = 300.0
IDLE_POLL_S = 5.0

#: A stuck upstream must not hold `/call` (and a delegating caller's turn)
#: past this — without it the SDK default is no timeout at all, and idle
#: reaping only catches it ~300s later, misfiled as internal_error.
UPSTREAM_CALL_TIMEOUT_S = 60.0


class UpstreamError(RuntimeError):
    pass


@dataclass(frozen=True)
class UpstreamSpec:
    name: str
    command: str
    args: tuple[str, ...]
    env: dict[str, str]  # values may be `secret:<key>` references
    # Argument fences (docs/pep-decisions.md row 3a); absent = none.
    arg_denies: tuple[ArgDeny, ...] = ()
    # The closed tool set `mcp/<name>/server.yaml` declares (contract 01b
    # §5, `stage7-releases.md` §4.2). Root copies it here when it writes the
    # roster, so the PEP has ONE input file and a reload can refuse an
    # upstream whose probe disagrees with its own definition (§4.4 step 2).
    # Empty means the file declared none, and the probe decides alone.
    tools: tuple[str, ...] = ()


#: The one MCP annotation the warnings read (docs/ha-read-mcp.md §4).
READ_ONLY_HINT = "readOnlyHint"


@dataclass(frozen=True)
class UpstreamTool:
    name: str
    description: str
    input_schema: dict[str, object]
    # The upstream's own hints as it sent them; None = it sent none.
    annotations: dict[str, object] | None = None

    @property
    def write(self) -> bool | None:
        """Can this tool change state? None = the upstream never said.

        A hint for a human and a model to read, never a decision input:
        `decide()` does not see it. Anything short of `readOnlyHint: true` is
        a write — MCP's own default, and ha-mcp omits the key on
        `ha_call_service` rather than saying false. No annotations at all
        stays None: badging every such tool would teach the eye to skip the
        badge, and calling them read-only would be false reassurance.
        """
        if self.annotations is None:
            return None

        return self.annotations.get(READ_ONLY_HINT) is not True


def _wire_annotations(raw: ToolAnnotations | None) -> dict[str, object] | None:
    """Wire-shaped: camelCase keys, unset hints dropped. `{}` is not None."""
    if raw is None:
        return None

    return raw.model_dump(by_alias=True, exclude_none=True)


#: Exactly these keys per `arg_denies` entry, all required.
ARG_DENY_KEYS = frozenset({"tools", "arg", "values"})


def _str_list(raw: object) -> list[str] | None:
    """A non-empty list of strings, or None."""
    if not isinstance(raw, list) or not raw:
        return None
    items = cast("list[object]", raw)
    if any(not isinstance(x, str) for x in items):
        return None
    return cast("list[str]", items)


def _parse_arg_denies(path: Path, name: str, raw: object) -> tuple[ArgDeny, ...]:
    if not isinstance(raw, list):
        raise UpstreamError(f"{path}: upstream {name!r} arg_denies must be a list")
    rules: list[ArgDeny] = []
    for item in cast("list[object]", raw):
        if not isinstance(item, dict):
            raise UpstreamError(f"{path}: upstream {name!r} arg_denies entries must be mappings")
        entry = cast("dict[object, object]", item)
        if frozenset(str(k) for k in entry) != ARG_DENY_KEYS:
            raise UpstreamError(
                f"{path}: upstream {name!r} arg_denies entries need exactly {sorted(ARG_DENY_KEYS)}"
            )
        tools = _str_list(entry["tools"])
        values = _str_list(entry["values"])
        arg = entry["arg"]
        if tools is None or values is None or not isinstance(arg, str) or not arg:
            raise UpstreamError(
                f"{path}: upstream {name!r} arg_denies: tools and values are non-empty "
                "lists of strings, arg a non-empty string"
            )
        rules.append(ArgDeny(tools=frozenset(tools), arg=arg, values=frozenset(values)))
    return tuple(rules)


def load_upstreams(path: Path) -> dict[str, UpstreamSpec]:
    # Not `yaml.safe_load`: that reader has no limit for the copies of a
    # merge key (`bounded_yaml`).
    raw = bounded_yaml.load(path.read_text(encoding="utf-8"))
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise UpstreamError(f"{path}: top level must be a mapping")
    specs: dict[str, UpstreamSpec] = {}
    for name, body in cast("dict[object, object]", raw).items():
        if not isinstance(name, str) or not isinstance(body, dict):
            raise UpstreamError(f"{path}: upstream {name!r} must map a string to a mapping")
        entry = cast("dict[object, object]", body)
        command = entry.get("command")
        if not isinstance(command, str):
            raise UpstreamError(f"{path}: upstream {name!r} needs a string command")
        args_raw = entry.get("args", [])
        if not isinstance(args_raw, list) or any(
            not isinstance(a, str) for a in cast("list[object]", args_raw)
        ):
            raise UpstreamError(f"{path}: upstream {name!r} args must be a list of strings")
        env_raw = entry.get("env", {})
        if not isinstance(env_raw, dict):
            raise UpstreamError(f"{path}: upstream {name!r} env must be a mapping")
        env: dict[str, str] = {}
        for k, v in cast("dict[object, object]", env_raw).items():
            if not isinstance(k, str) or not isinstance(v, str):
                raise UpstreamError(f"{path}: upstream {name!r} env must map strings to strings")
            env[k] = v
        arg_denies = _parse_arg_denies(path, name, entry.get("arg_denies", []))
        tools_raw = entry.get("tools", [])
        if not isinstance(tools_raw, list) or any(
            not isinstance(t, str) for t in cast("list[object]", tools_raw)
        ):
            raise UpstreamError(f"{path}: upstream {name!r} tools must be a list of strings")
        unknown = set(entry) - {"command", "args", "env", "arg_denies", "tools"}
        if unknown:
            raise UpstreamError(
                f"{path}: upstream {name!r} has unknown keys {sorted(str(k) for k in unknown)}"
            )
        specs[name] = UpstreamSpec(
            name=name,
            command=command,
            args=tuple(cast("list[str]", args_raw)),
            env=env,
            arg_denies=arg_denies,
            tools=tuple(cast("list[str]", tools_raw)),
        )
    return specs


def resolve_env(spec: UpstreamSpec, secrets: dict[str, str]) -> dict[str, str]:
    resolved: dict[str, str] = {}
    for var, value in spec.env.items():
        if value.startswith("secret:"):
            key = value.removeprefix("secret:")
            if key not in secrets:
                raise UpstreamError(f"upstream {spec.name!r} needs missing secret {key!r}")
            resolved[var] = secrets[key]
        else:
            resolved[var] = value
    return resolved


#: The console script every upstream starts through (`run_as.py`).
LAUNCHER_SCRIPT: Final = "chaperone-as"

#: `ServerFile.user` reads `name` alone. The pin, the entrypoint and the
#: tools are what the dataclass requires, and nothing here reads them.
_NAME_ONLY_PIN: Final = ServerPin(source=Source.AGENT_MCP, python=DEFAULT_PYTHON)


def server_user(name: str) -> str:
    """Contract 01b §9's user for server `name`, in root's own spelling.

    Root builds each `/opt/mcp/<name>` as `ServerFile.user`
    (`executor/mcpbuild.py`). The PEP drops the child to what that same
    property says, rather than writing the prefix a second time. The name
    is not checked here: the launcher refuses a user outside its pattern.
    """
    return ServerFile(name=name, pin=_NAME_ONLY_PIN, entrypoint="", tools=()).user


@dataclass(frozen=True)
class Launcher:
    """How one upstream is started: `<prefix> mcp-<name> <command> <args>`.

    `installed_launcher()` is the real one and every pool's default. Tests
    pass a prefix that runs the same launcher with the switch faked.
    """

    prefix: tuple[str, ...]

    def params(self, spec: UpstreamSpec, env: dict[str, str]) -> StdioServerParameters:
        # The row's variables cross the launcher as ONE value, decoded only
        # after the switch (`run_as.py` says why). The SDK still merges six
        # of the PEP's own variables into the launcher's environment. The
        # server receives none of them.
        return StdioServerParameters(
            command=self.prefix[0],
            args=[*self.prefix[1:], server_user(spec.name), spec.command, *spec.args],
            env={CHILD_ENV_VAR: json.dumps(env)},
        )


def installed_launcher() -> Launcher:
    """`chaperone-as` beside this interpreter: `/opt/components/chaperone/bin` on a
    host, so the launcher is always the one this `chaperone` release shipped."""
    return Launcher((str(Path(sysconfig.get_path("scripts")) / LAUNCHER_SCRIPT),))


class UpstreamPool(Protocol):
    """What the app needs from MCP; tests inject a fake."""

    def tools(self, server: str) -> dict[str, UpstreamTool]: ...

    async def call(self, server: str, tool: str, args: dict[str, object]) -> str: ...


@dataclass
class _Live:
    """One upstream that currently has a process behind it."""

    session: ClientSession
    used: float


class StdioUpstreamPool:
    """Upstream processes, started on demand and closed once idle.

    Every upstream is probed once at startup, and that probe is what
    `/manifest` publishes. Fail-closed therefore holds: an upstream that
    cannot start is absent from `tools()` for the life of the process, so its
    tools reach no manifest and its calls deny. A *successful* probe does not
    keep a process resident.

        start()  spawn -> list_tools -> close     tools cached, nothing resident
        call()   spawn if cold, then dispatch     stays live while it is used
        stop()   every owner closes its own

    The task that opens a session is the task that closes it. anyio binds cancel
    scopes to tasks, so reaping from a shared background sweeper would tear a
    session down from the wrong one and raise on the way out; instead each
    upstream gets an owner task that holds it open and closes it when it goes
    idle.
    """

    def __init__(
        self,
        specs: dict[str, UpstreamSpec],
        secrets: dict[str, str],
        idle_ttl_s: float = IDLE_TTL_S,
        launcher: Launcher | None = None,
    ) -> None:
        self._specs = specs
        self._secrets = secrets
        # No launcher named is the real one: a pool never starts a server as
        # the PEP's own user because a caller left this out.
        self._launcher = launcher if launcher is not None else installed_launcher()
        self._idle_ttl_s = idle_ttl_s
        self._poll_s = min(IDLE_POLL_S, idle_ttl_s)
        self._tools: dict[str, dict[str, UpstreamTool]] = {}
        self._live: dict[str, _Live] = {}
        # Every owner task, a closing one included: `stop()` awaits them all.
        self._owners: set[asyncio.Task[None]] = set()
        self._locks: dict[str, asyncio.Lock] = {}
        self._closing = asyncio.Event()

    async def start(self) -> None:
        for name in self._specs:
            try:
                listed = await self._probe(name)
            except Exception as exc:
                # Fail closed per upstream: its tools stay absent from every
                # manifest and calls to it deny; the PEP still serves the rest.
                log.error("upstream %r failed to start: %s", name, exc)
                continue

            self._tools[name] = {
                t.name: UpstreamTool(
                    name=t.name,
                    description=t.description or "",
                    input_schema=cast("dict[str, object]", t.input_schema),
                    annotations=_wire_annotations(t.annotations),
                )
                for t in listed.tools
            }
            log.info("upstream %r up with tools %s", name, sorted(self._tools[name]))

    async def stop(self) -> None:
        self._closing.set()

        # Each owner closes its own stack on the way out, so waiting is all there
        # is to do here. The wait is prompt: owners watch this same event.
        for owner in list(self._owners):
            await owner

        self._live.clear()
        self._owners.clear()
        self._tools.clear()
        self._closing.clear()

    def tools(self, server: str) -> dict[str, UpstreamTool]:
        return self._tools.get(server, {})

    async def call(self, server: str, tool: str, args: dict[str, object]) -> str:
        # A boot probe that failed leaves no tools, and that is what "not running"
        # means for the life of this process — never a spawn attempt per call.
        if server not in self._tools:
            raise UpstreamError(f"upstream {server!r} is not running")

        session = await self._session(server)
        try:
            result = await session.call_tool(
                tool, args, read_timeout_seconds=UPSTREAM_CALL_TIMEOUT_S
            )
        except MCPError as exc:
            # Includes the SDK's own timeout: an upstream that never answers
            # is an upstream failure, not a policy denial.
            raise UpstreamError(f"{server}__{tool}: {exc}") from exc
        texts = [c.text for c in result.content if isinstance(c, TextContent)]
        joined = "\n".join(texts)

        if result.is_error:
            raise UpstreamError(joined or f"{server}__{tool} reported an error")

        return joined

    async def _connect(self, name: str, stack: AsyncExitStack) -> ClientSession:
        spec = self._specs[name]
        params = self._launcher.params(spec, resolve_env(spec, self._secrets))
        read, write = await stack.enter_async_context(stdio_client(params))
        session = await stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        return session

    async def _probe(self, name: str) -> ListToolsResult:
        """Start one upstream, read its tool list, and let it go again."""
        async with AsyncExitStack() as stack:
            session = await self._connect(name, stack)
            return await session.list_tools()

    async def _session(self, name: str) -> ClientSession:
        live = self._live.get(name)
        if live is not None:
            live.used = time.monotonic()
            return live.session

        # One lock per upstream: two calls arriving at a cold upstream together
        # must produce one process, not two.
        async with self._locks.setdefault(name, asyncio.Lock()):
            live = self._live.get(name)
            if live is None:
                await self._spawn(name)
                live = self._live.get(name)

            if live is None:
                raise UpstreamError(f"upstream {name!r} could not be started")

            live.used = time.monotonic()
            return live.session

    async def _spawn(self, name: str) -> None:
        ready: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        owner = asyncio.create_task(self._own(name, ready))
        self._owners.add(owner)
        owner.add_done_callback(self._owners.discard)
        try:
            await ready
        except Exception as exc:
            raise UpstreamError(f"upstream {name!r} failed to start: {exc}") from exc

    async def _own(self, name: str, ready: asyncio.Future[None]) -> None:
        """Hold one upstream open until it goes idle, then close it."""
        try:
            async with AsyncExitStack() as stack:
                session = await self._connect(name, stack)
                live = _Live(session=session, used=time.monotonic())
                self._live[name] = live
                try:
                    ready.set_result(None)
                    await self._until_idle(name)
                finally:
                    # Unpublish before the stack closes. The close takes up to
                    # ~2 s (stdin EOF, then the process's exit), and a call
                    # arriving in it must spawn afresh, not take a dying session.
                    self._unpublish(name, live)
        except Exception as exc:
            # Before ready, the caller is still waiting and deserves the reason.
            # After it, there is nobody to tell: an allowed call that lands on a
            # dead session fails as upstream_failed, which is the truth.
            if not ready.done():
                ready.set_exception(exc)
            else:
                log.error("upstream %r ended: %s", name, exc)

    def _unpublish(self, name: str, live: _Live) -> None:
        """Drop `live` only if it is still the entry: a successor may own `name`."""
        if self._live.get(name) is live:
            del self._live[name]

    async def _until_idle(self, name: str) -> None:
        """Wait until this upstream has gone unused for its TTL, or the pool closes."""
        while True:
            try:
                await asyncio.wait_for(self._closing.wait(), timeout=self._poll_s)
                return
            except TimeoutError:
                pass

            live = self._live.get(name)
            if live is None:
                return

            if time.monotonic() - live.used >= self._idle_ttl_s:
                log.info("upstream %r idle for %.0fs; closing it", name, self._idle_ttl_s)
                return
