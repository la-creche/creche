"""A stand-in for the vmm: the host session list and one fake facade per stream.

`FakeVmm` keeps the rows that `create` makes, answers `list` newest stored
first, and opens one `FakeChannel` per `acp` call, with a `FakeFacade` on its
far side. A test sets `refuse` to make a route answer a vmm code.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
from typing import Any

from attendance.vmm import NOT_FOUND, VmmRefused
from creche_wire import CreateSession, ListCursor, ListSessions, SessionPage, SessionRow
from fake_channel import FakeChannel
from fake_facade import FakeFacade, PromptMode, serve

#: The vmm code of a second stream of one session (decision CH33 rule 7).
BUSY = "busy"

#: The most rows of one page of the stand-in.
PAGE_ROWS = 2


@dataclass(slots=True)
class FakeVmm:
    """The vmm of a test. Each call is in process."""

    prompt_mode: PromptMode = PromptMode.OK
    #: The facade of each stream speaks this protocol version.
    protocol_version: int = 1
    #: The vmm code that each call of a route answers, by route name.
    refuse: dict[str, str] = field(default_factory=dict[str, str])
    #: `session/load` updates by session id, for each facade.
    replays: dict[str, list[dict[str, Any]]] = field(
        default_factory=dict[str, list[dict[str, Any]]]
    )
    #: An open of a session with a gate here waits for the gate.
    open_gates: dict[str, asyncio.Event] = field(default_factory=dict[str, asyncio.Event])
    #: The sessions whose stream has no facade on its far side: nothing
    #: answers, the handshake included.
    mute: set[str] = field(default_factory=set[str])
    rows: dict[str, SessionRow] = field(default_factory=dict[str, SessionRow])
    creates: list[CreateSession] = field(default_factory=list[CreateSession])
    lists: list[ListSessions] = field(default_factory=list[ListSessions])
    drops: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])
    opened: list[str] = field(default_factory=list[str])
    channels: dict[str, FakeChannel] = field(default_factory=dict[str, FakeChannel])
    facades: dict[str, FakeFacade] = field(default_factory=dict[str, FakeFacade])
    _tasks: list[asyncio.Task[None]] = field(default_factory=list[asyncio.Task[None]])

    async def create(self, session: CreateSession) -> SessionRow:
        self.creates.append(session)
        self._refused("create")
        row = SessionRow(
            created_at=session.created_at,
            cwd=session.cwd,
            family=session.family,
            id=session.id,
            origin=session.origin,
            title=None,
            updated_at=session.created_at,
        )
        self.rows[session.id.root] = row
        return row

    async def list_page(self, query: ListSessions) -> SessionPage:
        self.lists.append(query)
        self._refused("list")
        rows = [
            row
            for row in reversed(self.rows.values())
            if row.family == query.family and (query.cwd is None or row.cwd == query.cwd)
        ]
        start = query.cursor.root if query.cursor is not None else 0
        page = rows[start : start + PAGE_ROWS]
        more = start + PAGE_ROWS < len(rows)
        return SessionPage(next=ListCursor(start + PAGE_ROWS) if more else None, rows=page)

    async def drop(self, family: str, session: str) -> None:
        self.drops.append((family, session))
        self._refused("drop")
        row = self.rows.get(session)

        if row is None or row.family.root != family:
            raise VmmRefused(NOT_FOUND, "no row")

        del self.rows[session]

    async def open_acp(self, family: str, session: str, max_line_bytes: int) -> FakeChannel:
        self.opened.append(session)
        gate = self.open_gates.get(session)

        if gate is not None:
            await gate.wait()

        self._refused("acp")
        row = self.rows.get(session)

        if row is None or row.family.root != family:
            raise VmmRefused(NOT_FOUND, "no row")

        held = self.channels.get(session)

        if held is not None and not held.closed:
            raise VmmRefused(BUSY, "the session has a stream")

        channel = FakeChannel(max_line_bytes)
        facade = FakeFacade(
            prompt_mode=self.prompt_mode,
            protocol_version=self.protocol_version,
            replays=self.replays,
        )
        self.channels[session] = channel
        self.facades[session] = facade

        if session not in self.mute:
            self._tasks.append(asyncio.create_task(serve(channel, facade)))

        return channel

    def only_session(self) -> str:
        """The id of the one stream a test opened."""
        (session,) = self.channels
        return session

    async def settle(self, session: str, stop_reason: str = "end_turn") -> None:
        """End one held prompt with `stop_reason`."""
        for one in self.facades[session].settle(session, stop_reason):
            await self.channels[session].feed(one)

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()

        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task

    def _refused(self, route: str) -> None:
        code = self.refuse.get(route)

        if code is not None:
            raise VmmRefused(code, f"the stand-in refuses {route}")
