# door-acp

The ACP front door. A client opens `WS /acp/<family>`. The door forwards ACP
frames between that client and `attendance`'s `WS /acp/{family}`. It
translates nothing. The package is `agent_door_acp`, and the program is
`agent-door-acp`. The root `AGENTS.md` applies here too.

This package is the scaffold, row 26 of the Phase 2 plan. It has no forward
yet. A connection that passes every check is accepted, then closed with code
1013 and the reason `not wired`. Row 27 replaces `_forward` in `app.py`.

## Who connects

1. A browser client connects through NPM. Authentik forward auth guards the
   path. After Authentik passes, NPM sets the Authentik user header and adds
   the door's bearer as a header.
2. A terminal client sends the bearer itself.

The door checks both headers on each connection.

## Rules

- Never parse a frame beyond the envelope. The door reads the request line
  and the headers only. A frame is bytes for the other side.
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
| `DOOR_ACP_BIND` | the listen address, `host:port`. The default is port 8390 on `AGENT_LAN_ADDRESS`. |
| `AGENT_LAN_ADDRESS` | the host's LAN address, from the site file. |

`agent-door-acp --check` validates the config and the key file, then exits.
It binds no port. `systemd/creche-door-acp.service` runs it as
`ExecStartPre`.

## Routes

| Route | Result |
|---|---|
| `GET /healthz` | `200`, `{"status": "ok"}`. No auth. |
| `WS /acp/<family>` | `401` or `404` before the upgrade, else accept, then close 1013 `not wired` |

## Layout

| Module | Owns |
|---|---|
| `config.py` | the environment, the key file, the bind rules |
| `app.py` | the two routes, the header checks, `_forward` |
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
- No contract names the header of the bearer. The door reads
  `Authorization: Bearer`. A terminal client that also passes Authentik can
  need that header for Authentik. Row 27 decides.
- No contract names the Authentik header. The door requires a value in
  `X-authentik-username`. A client on the LAN with the bearer can set that
  header itself.
- uvicorn 0.52.4 logs "ASGI callable returned without completing
  handshake." after each refusal. The client still gets the status.
