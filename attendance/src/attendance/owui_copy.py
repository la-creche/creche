"""Writing a session into Open WebUI (contract 02 §10.4, §10.5).

`attendance` is the source of truth for transcripts and Open WebUI holds a copy.
A session born in Open WebUI already has a chat. A `tui-<ulid>` session has
none, so this module creates one and keeps it fed, and the phone then shows
what was typed in a terminal.

Two kinds of exchange reach this module. A settled TURN, for a session with
no chat of its own (§10.4 rule 6). And a TERMINAL exchange, for any attended
session, because the terminal ran pi itself and neither the journal nor the
chat would otherwise hold it (§10.5). `chat_of` is what decides which chat an
exchange lands in, and it answers for both.

```
  first settled turn --> POST /api/v1/chats/new       --> chat id
                     `-> POST /api/v1/chats/{id}/folder
  later turns        --> POST /api/v1/chats/{id}      (PARTIAL history)
```

Five rules, each measured or stated by contract 02 §10.4:

1. **A partial history is safe.** An append sends only the new messages, the
   new `currentId` and the parent's patched `childrenIds`. Open WebUI
   deep-merges `history`, so earlier turns survive and nothing reads the
   whole chat back.
2. **The response nests the chat.** Every field sits under `.chat`.
3. **A failed write never fails a turn.** The host journal is the record, so
   a failure is logged and retried on the next turn. A restart drops what is
   still waiting: a copy that lags is not an outage.
4. **Off is a whole configuration.** With no base URL or no key file the
   feature is off and nothing else changes.
5. **The cost sits off the turn's path.** Two calls on a new session, one per
   turn after that, 20 to 50 ms each (probe 0c). Measured, so bounded on a
   healthy day and unbounded on a bad one: `submit` therefore hands the turn
   to a writer task and returns, and the writer does the HTTP in a worker
   thread. One `attendance` process serves every session of every family on
   one event loop, so a write on that loop is every chat on the host frozen
   for as long as Open WebUI takes to answer.

One writer, not one per session. It gives the whole copy one order, and one
order per session is what a transcript needs. A session whose write hangs
delays other sessions' COPIES and no session's TURN, which is the trade rule
3 already makes: a copy that lags is not an outage.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Protocol

import httpx

from .atomic import as_object
from .clock import now
from .models import Session
from .tasks import report_failure

_LOG = logging.getLogger("attendance.owui")

# Probe 0c's three paths, measured on the host against Open WebUI v0.11.3.
CHATS_NEW = "/api/v1/chats/new"
CHAT_PATH = "/api/v1/chats/{chat}"
CHAT_FOLDER_PATH = "/api/v1/chats/{chat}/folder"

# The model id Open WebUI knows a family by (`docs/rework/spec.md` §11.1).
# No family file field carries one, and the door registers exactly this name.
MODEL_PREFIX = "agent:"

# Contract 02 §2. A session born in Open WebUI carries its chat id here.
OWUI_SESSION_PREFIX = "owui-"

# §10.5 rule 4. What a reader sees beside a terminal exchange's answer.
TERMINAL_MODEL_SUFFIX = " (terminal)"

ROLE_USER = "user"
ROLE_ASSISTANT = "assistant"

_OK = (200, 201)
_TIMEOUT_S = 10.0

# How many settled turns wait for a working Open WebUI before the oldest is
# dropped. It bounds memory on a long outage; the journal still has them all.
PENDING_MAX = 50


class Source(Enum):
    """Where an exchange happened. §10.5 rule 4 puts it in front of a reader."""

    TURN = "turn"
    TERMINAL = "terminal"


@dataclass(slots=True)
class MessagePair:
    """The two message ids one exchange adds.

    They are minted BEFORE the write, by the caller, because contract 02
    §10.5 rule 5 records them in `owui_map` in exchange order and the map's
    order is what §10.2 reads. Minting them inside the writer would put a
    terminal exchange's rows after the next turn's whenever Open WebUI was
    slow, and §10.2 would then fork where it should continue.
    """

    user: str = field(default_factory=lambda: str(uuid.uuid4()))
    assistant: str = field(default_factory=lambda: str(uuid.uuid4()))


@dataclass(frozen=True, slots=True)
class TurnCopy:
    """One settled exchange, as two Open WebUI messages."""

    prompt: str
    answer: str
    source: Source = Source.TURN
    pair: MessagePair = field(default_factory=MessagePair)


class ChatApi(Protocol):
    """The three calls probe 0c proved. A fake implements this in tests."""

    def create_chat(self, chat: dict[str, Any]) -> str | None:
        """The new chat's id, or None when the write failed."""
        ...

    def append_chat(self, chat_id: str, history: dict[str, Any]) -> bool: ...

    def file_chat(self, chat_id: str, folder_id: str) -> bool: ...


class HttpChatApi:
    """`ChatApi` over Open WebUI's own HTTP API, under a bearer key."""

    def __init__(self, base_url: str, api_key: str, client: httpx.Client | None = None) -> None:
        self._client = client if client is not None else httpx.Client(base_url=base_url)
        # Invariant 13: the key is read from a file into memory and rides in
        # one header. It never reaches argv, a URL or a log line.
        self._auth = {"Authorization": f"Bearer {api_key}"}

    def close(self) -> None:
        self._client.close()

    def create_chat(self, chat: dict[str, Any]) -> str | None:
        body = self._post(CHATS_NEW, {"chat": chat, "folder_id": None})

        if body is None:
            return None

        # The chat's OWN id is at the top level: probe 0c step 2 read it
        # from `.id`. Rule 2's "under `.chat`" is about the chat BODY, which
        # is what the caller submitted and carries no id. `.chat.id` is read
        # after it, so a build that answers the other way is not a silent
        # failure either.
        nested = as_object(body.get("chat")) or {}
        return _first_id(body.get("id"), nested.get("id"))

    def append_chat(self, chat_id: str, history: dict[str, Any]) -> bool:
        path = CHAT_PATH.format(chat=chat_id)
        return self._post(path, {"chat": {"history": history}}) is not None

    def file_chat(self, chat_id: str, folder_id: str) -> bool:
        path = CHAT_FOLDER_PATH.format(chat=chat_id)
        return self._post(path, {"folder_id": folder_id}) is not None

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any] | None:
        """One request. Every failure is None: rule 3 forbids raising here."""
        try:
            answer = self._client.post(path, json=body, headers=self._auth, timeout=_TIMEOUT_S)
        except httpx.HTTPError as error:
            _LOG.warning("open webui did not answer %s: %s", path, error)
            return None

        if answer.status_code not in _OK:
            _LOG.warning("open webui answered %s on %s", answer.status_code, path)
            return None

        try:
            parsed: object = answer.json()
        except (ValueError, RecursionError):
            return {}

        found = as_object(parsed)
        return found if found is not None else {}


class ChatCopy:
    """Keeps one Open WebUI chat per session that was not born in one."""

    def __init__(
        self,
        api: ChatApi | None,
        folder_id: str = "",
        saved: Callable[[Session], None] | None = None,
    ) -> None:
        self._api = api
        self._folder_id = folder_id
        # The writer learns the chat id AFTER the session was last saved, so
        # the owner of the store is told to save it again. A restart that
        # lost `owui_chat` would create a second chat for a session that
        # already has one.
        self._saved = saved
        self._pending: dict[tuple[str, str], list[TurnCopy]] = {}
        # `add_turn` runs on a worker thread and `pending`, `forget` and
        # `submit` run on the event loop, so the map is shared between two.
        self._lock = threading.Lock()
        self._queue: asyncio.Queue[tuple[Session, TurnCopy]] | None = None
        self._writer: asyncio.Task[None] | None = None

    @property
    def enabled(self) -> bool:
        """Rule 4. No base URL or no key file means the feature is off."""
        return self._api is not None

    def start(self) -> None:
        """Begin the writer. Call it inside the event loop."""
        if self._api is None or self._writer is not None:
            return

        self._queue = asyncio.Queue()
        self._writer = asyncio.create_task(self._write_loop(), name="owui writer")
        self._writer.add_done_callback(report_failure)

    async def close(self) -> None:
        """End the writer, dropping whatever it had not written.

        Rule 3 already says a restart drops what is still waiting, because
        the host journal is the record and the next settled turn sends what
        it finds. A shutdown that waited for Open WebUI would be an outage
        of `attendance` caused by an outage of the copy.
        """
        writer = self._writer
        self._writer = None
        self._queue = None

        if writer is None:
            return

        writer.cancel()

        with contextlib.suppress(asyncio.CancelledError):
            await writer

    async def drained(self) -> None:
        """Wait until every turn handed to `submit` was written AND kept.

        The write runs on a worker thread and the record is saved only after
        it returns, so "the API saw the call" is one step too early: a test
        that reads the record at that point races the save, and a slower
        runner loses. Nothing in `attendance` waits on this: rule 5 stands, a
        turn never waits for a copy.
        """
        queue = self._queue

        if queue is not None:
            await queue.join()

    def submit(self, record: Session, turn: TurnCopy) -> None:
        """Hand one settled turn to the writer. Never blocks, never raises.

        Rule 5. This is called from the turn-settle path, so it does no
        HTTP and no waiting of any kind.
        """
        queue = self._queue

        if self._api is None or queue is None:
            return

        queue.put_nowait((record, turn))

    def pending(self, family: str, session: str) -> int:
        """How many settled turns have not reached Open WebUI yet."""
        with self._lock:
            return len(self._pending.get((family, session), ()))

    def forget(self, family: str, session: str) -> None:
        with self._lock:
            self._pending.pop((family, session), None)

    def add_turn(self, record: Session, turn: TurnCopy) -> bool:
        """Write this turn, and whatever earlier turns are still waiting.

        True means the chat is level with the session. False means something
        is still waiting, which the next settled turn retries (rule 3).

        It blocks on HTTP, so the writer task runs it on a worker thread.
        """
        if self._api is None:
            return True

        with self._lock:
            waiting = self._pending.setdefault((record.family, record.session), [])
            waiting.append(turn)
            del waiting[:-PENDING_MAX]

        while self._head(record) is not None:
            head = self._head(record)

            if head is None or not self._write(self._api, record, head):
                return False

            self._drop_head(record)

        return True

    def _head(self, record: Session) -> TurnCopy | None:
        with self._lock:
            waiting = self._pending.get((record.family, record.session), [])

            return waiting[0] if waiting else None

    def _drop_head(self, record: Session) -> None:
        with self._lock:
            waiting = self._pending.get((record.family, record.session))

            if waiting:
                waiting.pop(0)

    async def _write_loop(self) -> None:
        """One writer, one order. Each write goes to a worker thread.

        A failure is already logged and retried by `add_turn`, so nothing
        here decides policy. It only keeps the loop free.
        """
        queue = self._queue

        if queue is None:
            return

        while True:
            record, turn = await queue.get()

            try:
                await asyncio.to_thread(self.add_turn, record, turn)
                self._keep(record)
            except Exception:
                # Rule 3: a failed write never fails anything else. A writer
                # that died would silently stop every session's copy.
                _LOG.exception("open webui copy failed for %s/%s", record.family, record.session)
            finally:
                # `drained` counts on it, whether the write worked or not.
                queue.task_done()

    def _keep(self, record: Session) -> None:
        """Save the session, back on the event loop, with its chat id."""
        if self._saved is None:
            return

        self._saved(record)

    def _write(self, api: ChatApi, record: Session, turn: TurnCopy) -> bool:
        if chat_of(record) is None:
            return self._create(api, record, turn)

        return self._append(api, record, turn)

    def _create(self, api: ChatApi, record: Session, turn: TurnCopy) -> bool:
        """Step 1 and step 2 of §10.4's table, on the session's first turn."""
        pair = turn.pair
        messages = _messages(record.family, turn, pair, parent=None)
        chat_id = api.create_chat(
            {
                "title": record.title or record.session,
                "models": [MODEL_PREFIX + record.family],
                "messages": list(messages.values()),
                "history": {"currentId": pair.assistant, "messages": messages},
            }
        )

        if chat_id is None:
            return False

        record.owui_chat = chat_id
        record.owui_leaf = pair.assistant

        if self._folder_id:
            # A chat that is not filed is still a whole chat, so a refused
            # folder call is not a failed write.
            api.file_chat(chat_id, self._folder_id)

        return True

    def _append(self, api: ChatApi, record: Session, turn: TurnCopy) -> bool:
        """Step 3. Only what changed, which rule 1 proved safe."""
        chat_id = chat_of(record)
        leaf = record.owui_leaf

        if chat_id is None:
            return False

        pair = turn.pair
        messages = _messages(record.family, turn, pair, parent=leaf)

        if leaf is not None:
            # The parent's `childrenIds` is patched, never rewritten whole.
            messages[leaf] = {"childrenIds": [pair.user]}

        if not api.append_chat(chat_id, {"currentId": pair.assistant, "messages": messages}):
            return False

        record.owui_leaf = pair.assistant
        return True


def chat_of(record: Session) -> str | None:
    """The Open WebUI chat this session is copied into, or None for no chat.

    Contract 02 §10.4 rule 6. A session BORN in Open WebUI carries its chat
    id in its own session id and always has, so an append reaches that chat
    with no create and no stored field. A `tui-` session has `owui_chat`
    once the first write made one.
    """
    if record.owui_chat is not None:
        return record.owui_chat

    if not record.session.startswith(OWUI_SESSION_PREFIX):
        return None

    return record.session[len(OWUI_SESSION_PREFIX) :] or None


def _first_id(*candidates: object) -> str | None:
    """The first candidate that is a non-empty string, or None."""
    for one in candidates:
        if isinstance(one, str) and one:
            return one

    return None


def read_api(base_url: str, key_file: Path) -> HttpChatApi | None:
    """Build the client, or None when either half of rule 4 is missing."""
    if not base_url:
        return None

    try:
        key = key_file.read_text(encoding="utf-8").strip()
    except OSError as error:
        _LOG.warning("no open webui key at %s: %s", key_file, error)
        return None

    if not key:
        return None

    return HttpChatApi(base_url, key)


def _messages(
    family: str, turn: TurnCopy, pair: MessagePair, parent: str | None
) -> dict[str, dict[str, Any]]:
    """One exchange as Open WebUI's two message objects (probe 0c's shape)."""
    stamp = int(now().timestamp())
    terminal = turn.source is Source.TERMINAL
    # §10.5 rule 4. Open WebUI ignores a key it does not know (probe 0c), so
    # the flag costs nothing and the model name is what a human actually
    # reads under the answer.
    model = MODEL_PREFIX + family + (TERMINAL_MODEL_SUFFIX if terminal else "")

    return {
        pair.user: {
            "id": pair.user,
            "role": ROLE_USER,
            "content": turn.prompt,
            "parentId": parent,
            "childrenIds": [pair.assistant],
            "model": None,
            "timestamp": stamp,
            "done": True,
            "terminal": terminal,
        },
        pair.assistant: {
            "id": pair.assistant,
            "role": ROLE_ASSISTANT,
            "content": turn.answer,
            "parentId": pair.user,
            "childrenIds": [],
            "model": model,
            "timestamp": stamp,
            "done": True,
            "terminal": terminal,
        },
    }
