"""The session store and the host journal (contract 02 §8, §9)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from attendance.journal import Journal
from attendance.models import LineKind, OwuiRefs, Session, Turn, Usage
from attendance.paths import journal_file, session_file
from attendance.states import SessionKind, SessionState, TurnState
from attendance.store import SessionStore

_FAMILY = "chat"
_SESSION = "owui-3f2a9c41-77b0-4a1e-9a4c-1d0e5f8b2c33"
_MOMENT = datetime(2026, 9, 18, 19, 22, 5, tzinfo=UTC)
_LINE_COUNT = 40


def _store(tmp_path: Path) -> SessionStore:
    return SessionStore(tmp_path / "sessions")


def _session() -> Session:
    return Session(
        family=_FAMILY,
        session=_SESSION,
        kind=SessionKind.ATTENDED,
        created_at=_MOMENT,
        updated_at=_MOMENT,
        title="Kitchen sensor debug",
        labels={"door": "owui"},
    )


def test_create_lays_out_the_contract_layout(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.create(_session())

    base = store.root / _FAMILY / _SESSION

    assert (base / "session.json").is_file()
    for name in ("turns", "pi", "inbox", "outbox"):
        assert (base / name).is_dir()


def test_session_round_trip(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record = _session()
    record.owui_map = {"msg-1": "entry-1"}
    store.create(record)

    loaded = store.load(_FAMILY, _SESSION)

    assert loaded is not None
    assert loaded.title == "Kitchen sensor debug"
    assert loaded.kind is SessionKind.ATTENDED
    assert loaded.created_at == _MOMENT
    assert loaded.labels == {"door": "owui"}
    assert loaded.owui_map == {"msg-1": "entry-1"}


def test_owui_map_stays_off_the_api_shape(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record = _session()
    record.owui_map = {"msg-1": "entry-1"}
    store.create(record)

    raw = json.loads(session_file(store.root, _FAMILY, _SESSION).read_text())
    api = record.to_api(state=SessionState.IDLE, turns_running=0, writer=None)

    assert "owui_map" in raw
    assert "owui_map" not in api


def test_turn_round_trip_keeps_the_prompt_digest(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.create(_session())
    turn = Turn(
        turn="01JBQ7WZ0X4T9V6K2H8M3N5PQR",
        session=_SESSION,
        family=_FAMILY,
        state=TurnState.SETTLED,
        started_at=_MOMENT,
        sandbox="chat-s3",
        idempotency_key="b7c1e2d0",
        usage=Usage(input=4120, output=188, cost_usd=0.014),
        owui=OwuiRefs(chat_id="3f2a", message_id="b7c1"),
        prompt_sha256="abc123",
    )
    store.save_turn(turn)

    loaded = store.load_turns(_FAMILY, _SESSION)

    assert len(loaded) == 1
    assert loaded[0].state is TurnState.SETTLED
    assert loaded[0].usage.input == 4120
    assert loaded[0].prompt_sha256 == "abc123"
    assert loaded[0].owui is not None
    assert loaded[0].owui.chat_id == "3f2a"
    assert "prompt_sha256" not in turn.to_api()


def test_journal_is_gapless_and_replays(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.create(_session())

    for index in range(_LINE_COUNT):
        store.journal.append(_FAMILY, _SESSION, LineKind.NOTE, None, {"n": index})

    every = list(store.journal.replay(_FAMILY, _SESSION, from_seq=0))

    assert [line.journal_seq for line in every] == list(range(1, _LINE_COUNT + 1))
    assert every[0].body == {"n": 0}


def test_replay_from_a_sequence_number(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.create(_session())

    for index in range(_LINE_COUNT):
        store.journal.append(_FAMILY, _SESSION, LineKind.NOTE, None, {"n": index})

    tail = list(store.journal.replay(_FAMILY, _SESSION, from_seq=35))

    assert [line.journal_seq for line in tail] == [36, 37, 38, 39, 40]


def test_replay_filters_by_turn(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.create(_session())
    store.journal.append(_FAMILY, _SESSION, LineKind.NOTE, None, {})
    store.journal.append(_FAMILY, _SESSION, LineKind.PI_EVENT, "01JB", {"a": 1})
    store.journal.append(_FAMILY, _SESSION, LineKind.PI_EVENT, "01JC", {"b": 2})

    only = list(store.journal.replay(_FAMILY, _SESSION, from_seq=0, turn="01JB"))

    assert len(only) == 1
    assert only[0].body == {"a": 1}


def test_replay_keeps_the_written_timestamp(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.create(_session())
    store.journal.append(_FAMILY, _SESSION, LineKind.NOTE, None, {}, moment=_MOMENT)

    replayed = list(store.journal.replay(_FAMILY, _SESSION, from_seq=0))

    assert replayed[0].ts == _MOMENT


def test_a_torn_tail_does_not_stop_a_replay(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.create(_session())
    store.journal.append(_FAMILY, _SESSION, LineKind.NOTE, None, {"n": 1})
    store.journal.flush(_FAMILY, _SESSION)

    with journal_file(store.root, _FAMILY, _SESSION).open("ab") as handle:
        handle.write(b'{"journal_seq": 2, "kind": "no')

    replayed = list(store.journal.replay(_FAMILY, _SESSION, from_seq=0))

    assert [line.journal_seq for line in replayed] == [1]


def test_counter_resumes_after_a_restart(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record = _session()
    store.create(record)

    for _ in range(3):
        line = store.journal.append(_FAMILY, _SESSION, LineKind.NOTE, None, {})
        assert line.journal_seq is not None
        record.journal_seq = line.journal_seq

    store.save(record)
    store.journal.close_all()

    restarted = SessionStore(store.root, Journal(store.root))
    reloaded = restarted.load(_FAMILY, _SESSION)
    assert reloaded is not None
    restarted.journal.register(_FAMILY, _SESSION, reloaded.journal_seq)

    following = restarted.journal.append(_FAMILY, _SESSION, LineKind.NOTE, None, {})

    assert following.journal_seq == 4


def test_scan_finds_every_session(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.create(_session())
    other = _session()
    other.session = "tui-01JBQ7WZ0X4T9V6K2H8M3N5PQR"
    store.create(other)

    found = sorted(record.session for record in store.scan())

    assert found == sorted([_SESSION, other.session])


def test_delete_removes_the_directory(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.create(_session())
    store.journal.append(_FAMILY, _SESSION, LineKind.NOTE, None, {})

    assert store.delete(_FAMILY, _SESSION) is True
    assert not (store.root / _FAMILY / _SESSION).exists()
    assert store.delete(_FAMILY, _SESSION) is False
