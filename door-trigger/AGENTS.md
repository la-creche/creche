# door-trigger

The trigger door. An autonomous family's sessions start from a timer or a
webhook, never from a person. This package turns either firing into one
`attendance` turn, as the principal `door-trigger`. The package is
`agent_door_trigger`. The root `AGENTS.md` applies here too.

Two processes, one shared core:

- `agent-trigger fire <family> [--trigger NAME] [--payload-file FILE]
  [--force]`: one firing, then exit. `caregiver`'s generated timers run it
  through `creche-trigger@.service`.
- `agent-trigger serve`: the webhook listener, `POST /triggers/<family>/<name>`,
  from `creche-trigger-webhooks.service`.

## Rules

- `fire.py` is the one place a firing becomes two `attendance` calls. The CLI
  and the webhook route both call `fire_trigger()`.
- The payload is data, never instructions. `payload.py` validates shape and
  size and hands the caller's bytes on unchanged. `fire.py` frames it as
  untrusted external data.
- Three ways to fail a webhook call collapse to one 404: unknown family,
  unknown trigger name, wrong token. Same status, same body.
- `_authorized` always calls `tokens_match`, route or no route. A missing
  route still compares against a fixed-length dummy.
- `RouteTable.refresh()` replaces the whole map, never merges. `get()` and
  `refresh()` share one lock.
- No secret on argv, in a URL or in a log line. `cli.py` quiets `httpx` and
  `httpcore` to `WARNING`.
- Fail closed. A missing or short `attendance` token, or a `serve` bind on
  `0.0.0.0`, refuses to start. A webhook token that fails the same floor never
  becomes a route, and does not crash the service.
- The `SIGHUP` route reload is best-effort. The periodic refresh is the
  mechanism that always works.
- ULIDs are minted here, in `ulid.py`. This package imports nothing from
  `attendance`.

## The quiet check (`quiet/`)

A cron firing of a family whose file carries `quiet:` asks whether anything
changed since the family's last good wake. It starts nothing when nothing
did.

1. Every read fails open. A read that fails answers the value the gate wakes
   on. A needless wake costs cents. A skipped firing is a stalled job found a
   day late.
2. The snapshot is taken before the firing, never after.
3. `good` moves only on an `ok` outcome.
4. Live first, then the outcome record.
5. The gate reads as the family, never wider. Its two chaperone calls carry
   the family's own token from `creds.json`. `decide.py` stays pure.

## Behaviour under failure

| Situation | Result |
|---|---|
| unknown family, unknown name or wrong token on a webhook | `404`, identical body |
| a family that never validated | no route, cron or webhook |
| `attendance` refuses the job | `fire`: one stderr line, exit 1. `serve`: the matching HTTP status. Never retried here. |
| an oversized or invalid payload | `fire`: exit 2. `serve`: `413` or `400`. |
| `attendance` cannot be reached | `fire`: exit 2, a different message than a refusal. `serve`: `502 attendance_unreachable`. |
| a `quiet:` family's cron firing finds nothing changed | exit 0, no session |

## Configuration

| Variable | Meaning | Used by |
|---|---|---|
| `DOOR_TRIGGER_SESSIOND_TOKEN_FILE` | this door's `attendance` bearer token | both |
| `DOOR_TRIGGER_SESSIOND_SOCKET`, `DOOR_TRIGGER_SESSIOND_URL` | the transport. Set one, not both. | both |
| `DOOR_TRIGGER_BIND` | the listen address, port 8360 on the LAN address. Refuses `0.0.0.0`. | serve |
| `DOOR_TRIGGER_FAMILIES_DIR` | where `caregiver` publishes status documents and `creds.json` | both |
| `DOOR_TRIGGER_REGISTRY_ROOT` | the registry, read for webhook names and `quiet:` only | both |
| `DOOR_TRIGGER_STATE_ROOT` | where the quiet check reads outcomes and audit, and keeps its record | fire |
| `DOOR_TRIGGER_PEP_URL` | the chaperone the quiet check calls as the family | fire |
| `DOOR_TRIGGER_WEBHOOKS_DIR` | one `<family>/<name>.token` per declared webhook. `caregiver` mints them. | serve |
| `DOOR_TRIGGER_REFRESH_S` | how often the route table re-reads, default 30 | serve |

`agent-trigger fire <family> --check` and `agent-trigger serve --check`
validate the config and exit.

## Layout

| Module | Owns |
|---|---|
| `ulid.py`, `payload.py`, `errors.py`, `untrusted.py` | the id, the payload, the code map, shape checks |
| `attendance.py`, `fire.py` | the client, one firing |
| `config.py`, `families.py`, `tokens.py`, `routes.py`, `webhooks.py` | the two configs, which families qualify, constant-time tokens, the route table, the listener |
| `cli.py`, `__main__.py` | `fire` and `serve` |
| `quiet/gate.py`, `decide.py`, `board.py`, `chaperone.py`, `records.py`, `state.py` | read everything, decide (pure), the board fingerprint, two calls as the family, outcomes and audit, the gate's record |

## Tests

```bash
uv run pytest door-trigger/tests
```

`trigger_fake_attendance.py` is named that way because `fake_attendance.py`
is `door-owui`'s. Basenames are unique across the workspace. Run
`uv run pytest -n auto` before you trust a new test file's name.

## Known gaps

- Routes read webhook names from the registry file, not from the status
  document, so a webhook added to a valid family can be routed before
  `caregiver` validates the edit (`routes.py`, `families.py`).
- A firing's kind, name and time travel as three `trigger_*` labels
  (`fire.py`).
- The quiet check reads `quiet:` from the registry file. An invalid file is
  not checked at all (`quiet/gate.py`).
- The quiet check is a second reader of the chaperone's audit. A user unit
  that cannot read the audit treats the daily call as owed (`quiet/records.py`).
