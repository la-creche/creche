"""A supervisor that speaks contract 03, and the fixtures around it.

Every external thing has a fake behind a small protocol, so no test needs
the host, `sbx`, LiteLLM or the PEP. This module is the far side of
`FakeChannel`: it answers the handshake, plays pi's real event order, and can
misbehave in each of the ways contract 03 §13 names.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agent_sessiond.auth import MIN_TOKEN_BYTES, Principal
from agent_sessiond.channel import Channel, FakeChannel, SandboxDial
from agent_sessiond.clock import now, rfc3339
from agent_sessiond.config import Bind, Config
from agent_sessiond.paths import status_file, token_file
from agent_sessiond.wire import PROTOCOL_VERSION

FAMILY = "chat"
SANDBOX = "chat-s1"

# Contract 05 §4.1: the status document publishes one env file per sandbox,
# and `sessiond` refuses to dial without it. An obvious fixture path: nothing
# in these tests opens the file, because `sbx exec --env-file` is what reads
# it on the real host.
SUPERVISOR_ENV = "/srv/agents/state/rework/families/chat/supervisor.env"
CHAT_SESSION = "owui-3f2a9c41-77b0-4a1e-9a4c-1d0e5f8b2c33"
CONFIG_REV = "reg-9f21c4"
EPOCH = 7
SETTLE_TIMEOUT_S = 2.0

# Contract 01 §3.12's default, mirrored into the status document's limits.
JOB_TIMEOUT_S = 120

# pi's observed order on a real turn (contract 02 §15.1).
PI_TURN_EVENTS = (
    "agent_start",
    "turn_start",
    "message_start",
    "message_end",
    "turn_end",
    "agent_end",
)


def write_tokens(state_root: Path, skip: Principal | None = None) -> dict[Principal, str]:
    """One distinct token per principal, at the mode contract 02 §3 names."""
    made: dict[Principal, str] = {}

    for principal in Principal:
        if principal is skip:
            continue

        value = f"{principal.value}-{'x' * MIN_TOKEN_BYTES}"
        path = token_file(state_root, principal.value)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value + "\n", encoding="utf-8")
        os.chmod(path, 0o600)
        made[principal] = value

    return made


def write_status(
    state_root: Path,
    family: str = FAMILY,
    kind: str = "attended",
    state: str = "in_sync",
    sandboxes: tuple[tuple[str, str], ...] = ((SANDBOX, "ready"),),
    supervisor_env: str = SUPERVISOR_ENV,
    never_valid: bool = False,
    faults: tuple[dict[str, Any], ...] = (),
    epoch: int = EPOCH,
    job_timeout_s: int | None = JOB_TIMEOUT_S,
    max_running_turns: int | None = None,
    config_rev: str = CONFIG_REV,
    accepts_dispatch: bool = False,
) -> None:
    """One status document, as `managerd` writes it (contract 05 §2)."""
    path = status_file(state_root, family)
    path.parent.mkdir(parents=True, exist_ok=True)
    limits: dict[str, Any] = {} if job_timeout_s is None else {"job_timeout_s": job_timeout_s}

    if max_running_turns is not None:
        limits["max_running_turns"] = max_running_turns
    payload: dict[str, Any] = {
        "family": family,
        "kind": kind,
        "state": state,
        "written_at": rfc3339(now()),
        "config_rev": config_rev,
        "validation": {"never_valid": never_valid},
        "faults": list(faults),
        "sandboxes": [
            {"id": box, "state": box_state, "supervisor_env": supervisor_env}
            for box, box_state in sandboxes
        ],
        "credentials": {"epoch": epoch},
        "limits": limits,
        # Contract 05 §2.1's triggers block. `enqueue` is what contract 02
        # §13.4.1 rule 3 reads, and a family without it is not dispatchable.
        "triggers": {"webhooks": [], "enqueue": accepts_dispatch},
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def make_config(tmp_path: Path) -> Config:
    """A config rooted entirely inside one temporary directory."""
    return Config(
        sessions_root=tmp_path / "sessions",
        state_root=tmp_path / "state",
        work_root=tmp_path / "work",
        socket_path=tmp_path / "sessiond.sock",
        bind=Bind.SOCKET_ONLY,
        lan_address="192.0.2.10",
        lan_port=8350,
        channel_command="true",
        log_dir=tmp_path / "log",
    )


@dataclass(slots=True)
class SupervisorPlan:
    """What one fake supervisor claims in its `ready` line."""

    protocol: str = PROTOCOL_VERSION
    sandbox: str | None = None
    foreign_pi_processes: int = 0
    max_resident_processes: int = 12
    answer_ping: bool = True
    # Contract 03 §3. What this image says it serves. Drop `get_entries` to
    # stand in for a sandbox built before that message existed.
    caps: tuple[str, ...] = ("steer", "coalesce", "workspace_link", "get_entries")
    # Contract 03 §5.7. A reason name here makes this supervisor send `fatal`
    # in place of `ready`, which is what an unwritable control mount does.
    fatal: str | None = None
    # Contract 03 §4.6: `shutdown` ends the process, and with it the stream.
    # False stands in for a supervisor that never goes.
    exits_on_shutdown: bool = True
    # `ready` waits for this, standing in for a cold VM start (§10 rule 5).
    ready_gate: asyncio.Event | None = None


class FakeSupervisor:
    """The far side of one channel. It speaks contract 03 and nothing else."""

    def __init__(self, channel: FakeChannel, plan: SupervisorPlan) -> None:
        self._channel = channel
        self._plan = plan
        self._sandbox = plan.sandbox if plan.sandbox is not None else channel.sandbox
        self._seq: dict[str, int] = {}
        self._task: asyncio.Task[None] | None = None
        self._arrivals: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.hello: dict[str, Any] | None = None
        self.started: list[dict[str, Any]] = []
        self.opens: list[dict[str, Any]] = []
        self.steers: list[dict[str, Any]] = []
        self.aborts: list[dict[str, Any]] = []
        self.stops: list[dict[str, Any]] = []
        self.reads: list[dict[str, Any]] = []
        # Contract 03 §5.8. What the next `get_entries` answers with: the pi
        # store this fake stands for. A test appends to it the way a terminal
        # would, and `ok` false stands in for a refused read.
        self.entries: list[dict[str, Any]] = []
        self.entries_ok = True
        #: Entries one answer may carry. 0 means no cap.
        self.entries_cap = 0
        self.pings = 0
        self.shutdowns = 0

    def serve(self) -> None:
        """Send `ready`, then read host messages until the channel closes."""
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None

    async def next_start(self, timeout: float = SETTLE_TIMEOUT_S) -> dict[str, Any]:
        """The next `start_turn` the host sent, in arrival order."""
        return await asyncio.wait_for(self._arrivals.get(), timeout)

    async def emit_text(self, session: str, turn: str, delta: str) -> None:
        """One `message_update` carrying an assistant text delta."""
        await self._event(
            session,
            turn,
            {
                "type": "message_update",
                "assistantMessageEvent": {
                    "type": "text_delta",
                    "contentIndex": 0,
                    "delta": delta,
                },
            },
        )

    async def play_turn(self, session: str, turn: str, text: str) -> None:
        """pi's observed event order, with one text delta in the middle."""
        for name in PI_TURN_EVENTS[:3]:
            await self._event(session, turn, {"type": name})

        await self.emit_text(session, turn, text)

        for name in PI_TURN_EVENTS[3:]:
            await self._event(session, turn, {"type": name})

    async def settle(
        self,
        session: str,
        turn: str,
        user_entry_id: str = "a1b2c3d4",
        leaf_id: str = "e5f6a7b8",
        cost_usd: float = 0.014,
    ) -> None:
        """`agent_settled`, the only event that means the turn ended."""
        await self._send(
            {
                "type": "turn_settled",
                "session": session,
                "turn": turn,
                "turn_seq": self._next(turn),
                "resident": self._holds(),
                "user_entry_id": user_entry_id,
                "leaf_id": leaf_id,
                "entry_count": 4,
                "usage": {
                    "input": 4120,
                    "output": 188,
                    "cache_read": 0,
                    "cache_write": 0,
                    "cost_usd": cost_usd,
                },
                "settled_ms": 3120,
            }
        )

    def write_entry(self, role: str, text: str) -> str:
        """Add one pi entry, the way a terminal's own pi process would."""
        entry_id = f"e{len(self.entries) + 1}"
        self.entries.append({"id": entry_id, "role": role, "text": text})
        return entry_id

    async def answer_entries(self, request: dict[str, Any]) -> None:
        """Contract 03 §5.8. Everything after `since`, or the whole store.

        `entries_cap` stands in for §8's own cap: the answer carries that
        many entries and says `truncated`, which is what makes the host read
        again.
        """
        since = request.get("since")
        ids = [one["id"] for one in self.entries]
        at = ids.index(since) if since in ids else -1
        found = self.entries[at + 1 :] if since is None or at != -1 else list(self.entries)
        capped = found[: self.entries_cap] if self.entries_cap else found

        await self._send(
            {
                "type": "entries",
                "request": request.get("request", ""),
                "session": request.get("session", ""),
                "ok": self.entries_ok,
                "since_matched": since is None or at != -1,
                "truncated": len(capped) < len(found),
                "leaf_id": ids[-1] if ids else None,
                "entries": capped if self.entries_ok else [],
            }
        )

    async def fail(self, session: str, turn: str, reason: str, message: str = "") -> None:
        await self._send(
            {
                "type": "turn_failed",
                "session": session,
                "turn": turn,
                "turn_seq": self._next(turn),
                "reason": reason,
                "message": message,
            }
        )

    async def kill_process(self, session: str, turn: str) -> None:
        """A pi process died mid-turn (contract 03 §5.4)."""
        await self._send(
            {
                "type": "process_exit",
                "session": session,
                "pid": 41,
                "code": None,
                "signal": "SIGKILL",
                "turn": turn,
                "reason": "crashed",
            }
        )
        await self.fail(session, turn, "process_died", "the pi process died")

    async def reap(self, session: str) -> None:
        """An idle process passed `pi_idle_ttl_s` (contract 03 §6 rule 5)."""
        await self._send(
            {
                "type": "process_exit",
                "session": session,
                "pid": 41,
                "code": 0,
                "signal": None,
                "turn": None,
                "reason": "reaped",
            }
        )

    async def skip_sequence(self, session: str, turn: str) -> None:
        """Leave a gap in `turn_seq` (contract 03 §13 rule 4)."""
        self._next(turn)
        await self._event(session, turn, {"type": "message_update"})

    async def open_session(self, session: str, resident: bool = True) -> None:
        """The answer to `open_session` (contract 03 §5.6). It names no turn."""
        await self._send(
            {
                "type": "session_opened",
                "session": session,
                "resident": resident,
                "reason": None if resident else "pi_start_failed",
                "message": "the pi process is resident" if resident else "it would not start",
            }
        )

    async def send_fatal(self, reason: str, message: str = "the mount is read-only") -> None:
        """Contract 03 §5.7. A last line, never a state."""
        await self._send({"type": "fatal", "reason": reason, "message": message})

    async def send_malformed(self) -> None:
        await self._channel.feed("{not json at all")

    async def send_oversized(self, size: int) -> None:
        await self._channel.feed("x" * size)

    async def send_raw(self, text: str) -> None:
        await self._channel.feed(text)

    async def drop(self) -> None:
        """The channel vanished. The host sees end of stream."""
        await self._channel.drop()

    async def _loop(self) -> None:
        if self._plan.fatal is not None:
            # §5.7 rule 3: the control mount is read before the first protocol
            # line, so the cause precedes the handshake it prevents.
            await self.send_fatal(self._plan.fatal)
            return

        if self._plan.ready_gate is not None:
            await self._plan.ready_gate.wait()

        await self._send(self._ready())

        while True:
            line = await self._channel.next_host_line(timeout=30.0)
            message = json.loads(line)

            if message.get("type") == "shutdown":
                self.shutdowns += 1

                if self._plan.exits_on_shutdown:
                    await self._channel.drop()
                    return

                continue

            await self._handle(message)

    async def _handle(self, message: dict[str, Any]) -> None:
        kind = message.get("type")

        if kind == "hello":
            self.hello = message
            return

        if kind == "open_session":
            # Contract 03 §4.7 rule 1: the supervisor answers, whatever else
            # happens. Nothing on the host waits for it.
            self.opens.append(message)
            await self._answer_open(str(message.get("session", "")))
            return

        if kind == "start_turn":
            self.started.append(message)
            await self._arrivals.put(message)
            return

        if kind == "steer":
            self.steers.append(message)
            return

        if kind == "abort":
            self.aborts.append(message)
            return

        if kind == "stop_process":
            self.stops.append(message)
            return

        if kind == "get_entries":
            self.reads.append(message)
            await self.answer_entries(message)
            return

        if kind == "ping":
            self.pings += 1

            if self._plan.answer_ping:
                await self._send({"type": "pong", "nonce": message.get("nonce", "")})

    async def _answer_open(self, session: str) -> None:
        """§4.7 rule 7: a family that holds nothing warms nothing."""
        if self._holds():
            await self.open_session(session)
            return

        await self._send(
            {
                "type": "session_opened",
                "session": session,
                "resident": False,
                "reason": "not_held",
                "message": "this family holds no process between turns",
            }
        )

    def _holds(self) -> bool:
        """§6 rule 4: a process stays resident only when `pi_idle_ttl_s` > 0."""
        ttl = self.hello.get("pi_idle_ttl_s") if self.hello is not None else None
        return isinstance(ttl, int) and ttl > 0

    def _ready(self) -> dict[str, Any]:
        return {
            "type": "ready",
            "protocol": self._plan.protocol,
            "supervisor": "agent-supervisor/0.1.0",
            "pi": "0.84.2",
            "node": "22.11.0",
            "sandbox": self._sandbox,
            "max_resident_processes": self._plan.max_resident_processes,
            "foreign_pi_processes": self._plan.foreign_pi_processes,
            "caps": list(self._plan.caps),
        }

    async def _event(self, session: str, turn: str, event: dict[str, Any]) -> None:
        await self._send(
            {
                "type": "event",
                "session": session,
                "turn": turn,
                "turn_seq": self._next(turn),
                "event": event,
            }
        )

    async def _send(self, message: dict[str, Any]) -> None:
        await self._channel.feed(json.dumps(message, separators=(",", ":")))

    def _next(self, turn: str) -> int:
        seq = self._seq.get(turn, 0) + 1
        self._seq[turn] = seq
        return seq


@dataclass(slots=True)
class FakeFleet:
    """Hands the service a channel per sandbox and keeps the far side."""

    plans: dict[str, SupervisorPlan] = field(default_factory=dict[str, SupervisorPlan])
    channels: dict[str, FakeChannel] = field(default_factory=dict[str, FakeChannel])
    supervisors: dict[str, FakeSupervisor] = field(default_factory=dict[str, FakeSupervisor])
    dials: list[str] = field(default_factory=list[str])
    env_files: list[str] = field(default_factory=list[str])

    def plan(self, sandbox: str, plan: SupervisorPlan) -> None:
        """Set what the supervisor in this sandbox will claim."""
        self.plans[sandbox] = plan

    def factory(self, dial: SandboxDial) -> Channel:
        """A fresh channel per dial, with its supervisor already serving."""
        self.dials.append(dial.sandbox)
        self.env_files.append(dial.env_file)
        channel = FakeChannel(dial.sandbox)
        supervisor = FakeSupervisor(channel, self.plans.get(dial.sandbox, SupervisorPlan()))
        self.channels[dial.sandbox] = channel
        self.supervisors[dial.sandbox] = supervisor
        supervisor.serve()
        return channel

    def supervisor(self, sandbox: str = SANDBOX) -> FakeSupervisor:
        return self.supervisors[sandbox]

    async def stop(self) -> None:
        for supervisor in self.supervisors.values():
            await supervisor.stop()


async def settle_now(live_done: asyncio.Event, timeout: float = SETTLE_TIMEOUT_S) -> None:
    """Wait for a turn to reach a terminal state, or fail the test."""
    await asyncio.wait_for(live_done.wait(), timeout)


async def wait_until(test: Callable[[], bool], timeout: float = SETTLE_TIMEOUT_S) -> None:
    """Spin until a condition holds. Used where nothing else signals."""
    deadline = asyncio.get_running_loop().time() + timeout

    while not test():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition never held")

        await asyncio.sleep(0.005)
