# door-tui

The terminal door. `agent-tui <family>` finds or creates a session through
`attendance`, takes the writer lease, and hands the terminal to pi inside the
family's sandbox. The package is `agent_door_tui`. The root `AGENTS.md`
applies here too.

One session, every UI. An Open WebUI chat and a terminal session in one
family are the same session. This door is the host side. The in-VM side is
`agent-pi-launch`, which `playpen/` ships. There is no ingress into a
sandbox. The terminal is the client.

## Rules

- The lease is taken before pi starts, never after. `attendance`'s writer
  lease is the first fence and the only one that stops two terminals on one
  session. Do not add a path that execs before `lease.take()` returns.
- `X-Door-Instance` is per process here: `tui.<pid>`. A second terminal is
  refused.
- The door never sets `force` on its own. `--force` is a word the operator
  types. A renewal is always polite.
- A signal releases the lease and never kills pi. pi had the same signal from
  the terminal driver.
- `signals.install` stays tolerant of a non-main thread.
- Only a `ready` sandbox serves a terminal. A terminal runs no handshake. Do
  not copy `attendance`'s rule into `status.py`.
- A blocking fault warns and still opens the terminal. A person at a keyboard
  is who wants a terminal when a family is broken.
- The status document is read twice. The second read is what the exec is
  built from, because a switch can start while the operator picks.
- A session title is untrusted input. `picker._safe` strips control
  characters and cuts to a width.
- No secret on argv, in a URL or in a message. Every word of the `sbx` argv
  is a path or an identifier.
- The door never reads the env file. It puts the path on the command line.
- `launch.py` builds the argv and nothing else builds one. The terminal runs
  the playpen's own pi command line minus `--mode rpc`.
- Fail closed. A token file missing, empty, under 32 bytes or not UTF-8
  refuses to start.

## Run it

```bash
agent-tui chat                              # pick from a numbered list
agent-tui chat --session <id>               # skip the picker
agent-tui chat --new --title "Boiler"       # start a new session
agent-tui chat --session <id> --force       # take an idle writer lease
agent-tui --check                           # validate the config, touch nothing
```

The command it runs:

```bash
sbx exec -it --env-file <env file> <sandbox> -- node /opt/agent-supervisor/agent-pi-launch.js --sandbox <id> --session <session id> [--new]
```

`-it` is what gives pi a terminal. `--new` is passed when the session has no
pi store yet.

## Exit codes

| Exit | Means |
|---|---|
| 0 | pi ran and exited 0 |
| 2, 7, 8, 9, 10, 11 | `agent-pi-launch`'s own codes, passed through |
| 64 | the command line, the family name or the configuration was wrong |
| 65 | another door holds the writer lease |
| 66 | a switch is in progress, or no sandbox serves |
| 67 | `attendance` refused the call, or did not answer |
| 68 | nothing was picked |
| 126 | `sbx` would not run |

## Configuration

| Variable | Meaning |
|---|---|
| `DOOR_TUI_SESSIOND_TOKEN_FILE` | this door's `attendance` bearer token, 32 bytes or more |
| `DOOR_TUI_SESSIOND_SOCKET`, `DOOR_TUI_SESSIOND_URL` | the transport. Set one, not both. |
| `DOOR_TUI_FAMILIES_DIR` | where `caregiver` publishes `<family>/status.json` |
| `DOOR_TUI_SBX` | the `sbx` program, resolved on `PATH` |
| `DOOR_TUI_PI_LAUNCH` | the launcher's path inside the sandbox |

This door is a program the operator types, not a service. It has no bind
address, no key of its own and no unit.

## Layout

| Module | Owns |
|---|---|
| `__main__.py`, `app.py` | the command line, and the seven steps with every collaborator injected |
| `config.py`, `errors.py`, `ids.py` | the environment, the exit codes, the `tui-<ulid>` this door mints |
| `launch.py`, `lease.py`, `picker.py`, `signals.py` | the argv, the lease and its renewal thread, the list, the three signals |
| `attendance.py`, `status.py`, `untrusted.py` | the five calls, which sandbox serves, shape checks |

## Tests

```bash
uv run pytest door-tui/tests
uv run pytest integration/tests/test_ct_tui_door.py -m slow
```

Test file basenames are prefixed `test_tui_`. `bin/quality-gate.sh` runs
pyright over `door-tui/src` only. Keep the tests typed anyway.

## Known gaps

- Contract 05 §2.1 does not say what a reader does with a `written_at` that
  has no UTC offset. This door reads it as no time. The status document is
  then stale, and the door warns. `attendance` and the noticeboard read such
  a time as UTC (`status.py`).
