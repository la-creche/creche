# door-owui

The Open WebUI door. An OpenAI-compatible HTTP surface, `GET /v1/models`
and `POST /v1/chat/completions`, over `attendance`. It turns
`model: "agent:<family>"` plus a chat id into one turn: find or create the
session, run the turn, stream the events back. The package is
`agent_door_owui`. The root `AGENTS.md` applies here too.

The door holds no session state. `attendance` owns the transcript.

## Rules

- No session state here. Do not add a cache, a session map or anything keyed
  on a chat id that outlives one request.
- An empty chat id is a hard refusal, never a fallback. Open WebUI delivers an
  absent custom header present but empty. A shared default session would put
  every chat in one conversation. See `headers.py`.
- No custom header may be named `Authorization`. Open WebUI applies custom
  headers last, so one with that name replaces the bearer key.
- Tool output is never HTML or `<details>` in `content`. It travels as a
  JSON string inside a Responses-API item, so a hostile result cannot forge a
  closing tag.
- Golden vectors pin the frames Open WebUI renders. `test_translate.py` fixes
  the SSE envelope, the item shape and `CHIP_TEXT_LIMIT`.
- `with_keepalive`'s source opens and closes inside one task. Do not hand the
  raw `stream_turn` context manager to code that enters it in one task and
  exits it in another.
- Prime, do not buffer. `app._streamed` pulls one frame before it builds the
  `StreamingResponse`, so a refusal is a real HTTP status, not a `200` stream
  with an error frame. A refusal after the first frame cannot be a status.
  `app._after_first` writes it as the frames of a failed turn. A stream
  that has its `[DONE]` gets no second ending.
- A client disconnect never stops the turn. `SessiondClient` has no stop
  method. Keep it that way.
- The NDJSON stream splits on LF and nothing else. `_iter_lines` reads bytes.
- No secret on argv, in a URL or in a log line. `__main__.py` quiets `httpx`
  and `httpcore` to `WARNING`, because their `INFO` lines carry request URLs.
- Fail closed. A key file missing, empty, under 32 bytes or not UTF-8, or a
  bind on `0.0.0.0`, refuses to start.

## Behaviour under failure

| Situation | Result |
|---|---|
| chat id header absent or empty | `400 missing_chat_id` |
| a chat id of more than 123 characters | `400 bad_id`, before the door calls `attendance` |
| a second door holds the writer lease | `409 session_busy`, also for a streamed request |
| `attendance` does not answer | `502 attendance_unreachable`, also for a streamed request |
| `attendance` refuses a streamed turn after the first keepalive frame | a visible OpenAI-shaped error chunk with the code of the refusal, then `[DONE]` |
| a failure that no handler names, before the first frame | `500 internal` in the OpenAI error shape. The log holds the traceback. |
| a failure that no handler names, after the first frame | a visible OpenAI-shaped error chunk with the code `internal`, then `[DONE]`. The log holds the traceback. |
| the family is reconciling, degraded, or invalid with a last good definition | still served |
| a turn ends any way other than `settled` | a visible OpenAI-shaped error chunk |
| the client disconnects mid-stream | streaming stops, the turn keeps running |
| `attendance` answers `not_implemented` to `parent_id` | one retry without `parent_id` |
| Open WebUI's own background request | refused before any turn, `background_task_not_supported` |

## Configuration

| Variable | Meaning |
|---|---|
| `DOOR_OWUI_BIND` | the listen address, default `127.0.0.1:8340`. Refuses `0.0.0.0`. |
| `DOOR_OWUI_KEY_FILE` | required. This door's own bearer key, 32 bytes or more. |
| `DOOR_OWUI_SESSIOND_TOKEN_FILE` | this door's `attendance` bearer token |
| `DOOR_OWUI_SESSIOND_SOCKET`, `DOOR_OWUI_SESSIOND_URL` | the transport. Set one, not both. |
| `DOOR_OWUI_FAMILIES_DIR` | where `caregiver` publishes `<family>/status.json` |

`python -m agent_door_owui --check` validates the environment and exits.

## Layout

| Module | Owns |
|---|---|
| `config.py` | the environment to `DoorConfig`, fail closed |
| `headers.py`, `openai_api.py` | the custom headers, the request and response shapes |
| `families.py` | the picker: status documents to `/v1/models` |
| `attendance.py`, `journal.py`, `untrusted.py` | the `attendance` client, one validated journal line, shape checks |
| `translate.py`, `sse.py`, `stream.py` | journal lines to SSE frames, the frame shapes, the keepalive relay |
| `errors.py`, `app.py`, `__main__.py` | OpenAI-shaped refusals, the routes, the entry point |

## Tests

```bash
uv run pytest door-owui/tests
```

`fake_attendance.FakeSessiond` stands in for `attendance`. Nothing touches a
live service.

Test directories in this workspace have no `__init__.py`, so a test module's
basename must be unique across the whole workspace.
`bin/tests/test_unique_test_basenames.py` enforces it. Run
`uv run pytest -n auto` before you trust a new test file's name.

`bin/quality-gate.sh` runs pyright over `door-owui/src` only. Keep the tests
typed anyway.

## Known gaps

- Contract 02 §2 caps a session id at 128 characters and gives no cap for a
  chat id. The session id of a chat is `owui-<chat id>`, so the door caps a
  chat id at 123 characters (`headers.py`).
- Contract 02 §14 gives the codes of `attendance`. No contract gives the
  status or the code that a door answers for a failure of its own. The door
  answers `502 attendance_unreachable` when `attendance` does not answer. It
  answers `500 internal` for a failure that no handler names (`errors.py`,
  `app.py`).
- Contract 02 §5.4 and §14 give no form for a refusal of `attendance` that
  comes after the first frame of a streamed answer. The door writes the
  frames of a failed turn, with the code and the text of the status answer
  (`app.py`, `translate.py`).
