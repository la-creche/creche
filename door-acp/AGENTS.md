# door-acp

The ACP front door. A client opens `WS /acp/<family>`. The door forwards ACP
frames between that client and `attendance`'s `WS /acp/{family}`. It
translates nothing. The package is `agent_door_acp`. The programs are
`agent-door-acp`, the door, and `creche-acp-connect`, a stdio bridge for a
terminal client. The root `AGENTS.md` applies here too.

Rows 26 and 27 of the Phase 2 plan built this package.

## Who connects

1. A browser client connects through NPM. Authentik forward auth guards the
   path. After Authentik passes, NPM sets `X-authentik-username` and
   `Authorization: Bearer <door key>`. A browser WebSocket cannot set
   `Authorization` itself.
2. A terminal client sends `Authorization: Bearer <door key>` itself. It
   does not pass Authentik. Toad and acpx speak stdio only, so each starts
   `creche-acp-connect`.

The door always checks the bearer. It also requires `X-authentik-username`
while `DOOR_ACP_REQUIRE_AUTHENTIK` is `true`, the default. A site whose
terminal clients reach the door sets it to `false`. Then the bearer is the
only check.

## Two secrets

| Secret | File | Who sends it | To whom |
|---|---|---|---|
| The door key | `DOOR_ACP_KEY_FILE` | NPM, or a terminal client | the door |
| attendance's `door-acp` token | `DOOR_ACP_ATTENDANCE_TOKEN_FILE` | the door | attendance's `WS /acp/{family}` |

The door refuses to start when both files hold one key. A client never sees
attendance's token. attendance reads its token from
`<state root>/tokens/door-acp.token`. The door reads the same file.

## The forward

1. The door accepts the client, then opens one WebSocket to attendance's
   Unix socket at `WS /acp/<family>`, with attendance's token.
2. If attendance cannot be reached, the door closes the client with 1014
   `attendance unreachable` before any frame.
3. Each text frame passes as it is, in both directions. Each frame gets
   attendance's envelope check: UTF-8 at most 1 MiB, one line, strict JSON,
   an object with `"jsonrpc": "2.0"`. A frame over the cap closes both sides
   with 1009. Any other bad frame, or a binary frame, closes both with 1007.
4. When one side closes, the door closes the other side with the same code
   and reason. A close with no code becomes 1000, and a lost link 1001. A
   lost link to attendance closes the client with 1014.

A close code from attendance reaches the client as it is: 4401 means that
the door's token for attendance is wrong, not the client's bearer.

## Rules

- Never parse a frame beyond the envelope. A frame is bytes for the other
  side.
- Never log a frame, a header value or the key.
- Never bind `0.0.0.0`, `::` or a multicast address. The bind is one IP
  address: the host's LAN address, or loopback for a test.
- Fail closed on the key file. A missing file, a key under 32 bytes, a key
  that is not ASCII, or a file with a group or other bit refuses to start.
- Refuse before the upgrade. A wrong or missing bearer, or a missing
  Authentik header, gets `401`. A family outside the grammar gets `404`.
  Both auth checks run on each request, so the time taken does not show
  which check failed.
- Read the family from the path itself, with `fullmatch`. Starlette's route
  parameter drops a final newline.
- This package imports nothing from `attendance`.

## The family path grammar

`/acp/<family>`, where `<family>` matches `[a-z][a-z0-9-]{1,30}` in full.
Any other path under `/acp/` gets `404` before the upgrade.

## Configuration

| Variable | Meaning |
|---|---|
| `DOOR_ACP_KEY_FILE` | the door's own bearer key. Mode 0600, 32 bytes or more. No default. |
| `DOOR_ACP_ATTENDANCE_TOKEN_FILE` | attendance's `door-acp` token. The same file rules. No default. |
| `DOOR_ACP_ATTENDANCE_SOCKET` | attendance's Unix socket. The default is `/srv/agents/state/rework/sock/sessiond.sock`. |
| `DOOR_ACP_REQUIRE_AUTHENTIK` | `true` or `false`. The default is `true`. |
| `DOOR_ACP_BIND` | the listen address, `host:port`. The default is port 8390 on `AGENT_LAN_ADDRESS`. |
| `AGENT_LAN_ADDRESS` | the host's LAN address, from the site file. |

`agent-door-acp --check` validates the config and both key files, then exits.
It binds no port. `systemd/creche-door-acp.service` runs it as
`ExecStartPre`.

## Routes

| Route | Result |
|---|---|
| `GET /healthz` | `200`, `{"status": "ok"}`. No auth. |
| `WS /acp/<family>` | `401` or `404` before the upgrade, else accept, then the forward |

## `creche-acp-connect`

```bash
creche-acp-connect --key-file <door key file> ws://<door address>:8390/acp/<family>
```

Each stdin line goes to the door as one text frame. Each frame from the door
goes to stdout as one line. The key comes from a file with the door's file
rules, never from argv.

| Exit code | Cause |
|---|---|
| 0 | stdin ended, or the door closed with 1000 |
| 1 | the door is unreachable, or it closed with another code. stderr names it |
| 2 | a bad key file, or a stdin that is not a pipe |

## Layout

| Module | Owns |
|---|---|
| `config.py` | the environment, the key file, the bind rules |
| `app.py` | the two routes, the header checks |
| `forward.py` | the forward, the envelope check, the close codes |
| `connect.py` | `creche-acp-connect` |
| `__main__.py` | `--check` and the server |

## Tests

```bash
uv run pytest door-acp/tests
```

Each test basename starts with `test_acp_`. Basenames are unique across the
workspace.

## Known gaps

- The door ships in no component. The handover catalog has no row for it,
  so no release installs `~/.local/components/door-acp`, and no script
  installs the unit.
- No contract names the Authentik header. The door requires a value in
  `X-authentik-username`. A client on the LAN with the bearer can set that
  header itself. The bearer is the real check.
- A terminal client that must pass Authentik too cannot use this door:
  Authentik and the door would both read `Authorization`.
- `creche-acp-connect` reads stdin as a pipe. It refuses a regular file on
  stdin with exit code 2.
- uvicorn 0.52.4 logs "ASGI callable returned without completing
  handshake." after each refusal. The client still gets the status.
