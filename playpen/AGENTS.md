# playpen

Three programs, one package, one sandbox image. The root `AGENTS.md` applies
here too.

- `src/` is the playpen: the process inside each family's sandbox. The host
  opens one long-lived `sbx exec` and speaks the channel protocol over its
  stdio. The playpen starts one `pi --mode rpc` process per session.
- `src/launch-main.ts` is `agent-pi-launch`: the terminal door's launcher.
  It runs pi's interactive UI inside the same sandbox, on the same session
  store.
- `bridge/` is the chaperone bridge: a pi extension the playpen loads into
  each pi process. It turns the family's manifest into pi tools. It also
  serves one tool of its own, `index_search`.

```
host                                 sandbox (one microVM per family)
+------------+  sbx exec stdio       +----------------------+
| attendance |----- stdin ---------->| playpen              |
|            |<---- stdout ----------|   |   |   |          |
|            |<---- stderr (log) ----|   v   v   v          |
+------------+                       | pi  pi  pi  (rpc)    |
                                     |   bridge in each     |
                                     +----------------------+
                                           | every call decided by the chaperone
```

## Build and test

```bash
pnpm install
pnpm test          # no sbx, no VM, no model call
pnpm run typecheck
AGENT_LAN_ADDRESS=192.0.2.10 pnpm run build   # three bundles, no runtime dependency
```

The build fixes the LAN address in the bundle. A sandbox has no site file, so
`build.mjs` refuses to build without `AGENT_LAN_ADDRESS`. The image is built
on the host, from the repository root, with `playpen/Dockerfile`. Do not build
it on a development machine.

## The channel

1. Never write to stdout except through `Channel`. No `console.log`. Free
   text goes to stderr.
2. Never use a generic line reader. `framing.ts` splits on `\n` only.
3. A sender never emits a line over `MAX_LINE_BYTES`.

## pi processes

4. One pi process per session. Never two.
5. Never close a pi process's stdin while a turn runs.
6. A `prompt` response means accepted, never done. Only `agent_settled`
   settles a turn.
7. Read the config mount at process start, never per turn. A raised
   `config_rev` and a rotated credential epoch take the same exit: recycle at
   the next turn boundary.

## Credentials

8. Never put a credential on argv.
9. Never log a credential's value. Never put a real one in a fixture.
10. Keep the 250 ms retry in `creds.ts`. A file can be seen before its content
    is readable across a mount boundary.

## Liveness

11. Never make stdin EOF the only way this process ends. A client-side kill
    does not reach the in-VM process. The heartbeat deadline ends it.
12. Keep the lock file, its pid and its beat. `lock.ts` never throws. Never
    serve without the lock.

## Untrusted input

13. Nothing is acted on before `validate.ts` has passed it.
14. A refusal never deviates in silence. Record a contract gap as a
    `CONTRACT-QUESTION:` comment and under "Known gaps" below.

## Tests and the image

15. Every test runs on a development machine with no sbx and no VM.
16. `test/fake-pi.mjs` is not a pi emulator. An assertion against it is a
    statement about the playpen's logic, never about pi.
17. Do not add a runtime dependency. `esbuild` bundles each entry to one file.
18. Keep the pi pin in `toybox/package.json`. The Dockerfile copies that file.
    `sandbox-facts.ts` reads it to report the pi version.
19. `test/fake-pep.ts` is not a chaperone. Rule 16 applies to it.

## The bridge

20. Never write to stdout from the bridge. In rpc mode a pi process's stdout
    is the protocol.
21. The factory never throws. A chaperone that is down leaves the session
    able to converse without tools.
22. Never pass `--tools`, `--no-tools` or `--no-builtin-tools`. Never remove
    `--no-extensions`. Each would drop every granted tool.
23. `bridge/` imports nothing from `src/`, and neither imports pi.
    `bridge/pi-api.ts` declares the slice it uses.
24. Defang the closing marker before you wrap, never after.
    `bridge/untrusted.ts` owns the one implementation.
25. The turn file is not a security boundary. The three headers are advisory.

## Mounts

26. Never name a fixed in-VM path. `src/mounts.ts` reads the three directories
    from the environment the env file carries, and answers `fatal` with
    `mount_dir_unset` when one is missing.
27. Check an id again beside the join that makes it a path.
28. The `code-sandbox` link is counted, and the last holder removes it. Remove
    the link, never the directory.

## The terminal launcher

29. `pi-args.ts` is the only place a pi command line is built. The playpen
    asks for `PiMode.Rpc` and the launcher for `PiMode.Interactive`.
30. `process-record.ts` is a lease, not a flag. The reader tests the pid.
31. The launcher's refusal is the second fence. `attendance`'s writer lease is
    the first.
32. The launcher sets no `AGENT_TURN` and no `AGENT_TURN_FILE`.

## `sandbox_tools` and pi's settings

33. `sandbox_tools` is two instructions to pi. `--exclude-tools` only removes.
    `defaultTools` in `settings.json` enables. `runtime-config.ts` derives
    both from one granted list.
34. Rewrite `settings.json` at every pi start. Never read it back. Never merge
    it.
35. One test runs the real pi, `test/real-pi.test.ts`, and it stays. pi is a
    dev dependency here, at the version `toybox/package.json` pins.
36. No manifest is final. `bridge/index.ts` runs one loop for the life of the
    process. It asks every 15 s while it holds nothing. It asks every 60 s
    once it holds a manifest. It never sends two requests at once. The slow
    poll is conditional with `If-None-Match`.
37. The bridge states its tool count in `tools.json`. stderr stays prose. An
    absent file says nothing. Never add a second reader of stderr.
38. What a moved manifest does to pi's tool set is measured, never assumed.
    pi has no unregister. A removed tool is re-registered as `hidden`.
39. The state file moves only when the tool set moves, not on every `rev`.

## `index_search`

40. It reads only what the family mounts, read-only, with `node:sqlite`. It
    is offered only when `AGENT_INDEX_ROOT` names a directory that exists.
41. The vector half fails closed. The words half never does.
42. Never read `chunks_vec`. Read `chunks_emb`, the plain float32 copy. The
    store schema is a contract with `library/`. Change one side, change both.
43. `embed` is asked once per search, and only when the manifest offers it
    with no gate.
44. `index_search` is not a chaperone tool. `tools.json` never counts it.

## codemode

45. A gated tool is `model-only`. A script could queue a phone tap per loop
    item.
46. A script gets data. The model gets a frame. `codemode.ts` puts the frame
    back around the script's whole output.

## Environment the playpen expects

| Variable | Meaning | Absent |
|---|---|---|
| `AGENT_CRED_DIR` | the credential directory | `fatal`, `mount_dir_unset` |
| `AGENT_FAMILY_CONFIG_DIR` | the family config directory | `fatal`, `mount_dir_unset` |
| `AGENT_CONTROL_DIR` | the control directory | `fatal`, `mount_dir_unset` |
| `AGENT_SANDBOX` | the sandbox id | exit 2, unless `--sandbox` names it |

`caregiver` writes all four into the env file. `sbx exec --env-file`
delivers them. Three test seams exist and nothing in the image sets them:
`AGENT_PI_BIN`, `AGENT_LOCK_BEAT_MS` and `AGENT_CODE_SANDBOX_ROOT`.

## Layout

| File | Owns |
|---|---|
| `src/index.ts`, `src/launch-main.ts` | the two composition roots |
| `src/pi-launch.ts`, `src/pi-args.ts`, `src/process-record.ts` | the terminal plan, the one argv builder, the per-session record |
| `src/playpen.ts`, `src/pool.ts`, `src/session.ts`, `src/pi-process.ts` | the handshake and dispatch, the hold and reap rules, one session, one pi child |
| `src/channel.ts`, `src/framing.ts`, `src/validate.ts`, `src/protocol.ts`, `src/constants.ts` | the one stdout, LF records, every inbound line, the wire types, every fixed number |
| `src/mounts.ts`, `src/creds.ts`, `src/runtime-config.ts`, `src/models-json.ts`, `src/pi-settings.ts`, `src/env.ts` | the three directories, `creds.json`, the config mount, the two files pi reads, the per-turn environment |
| `src/lock.ts`, `src/turn-file.ts`, `src/tool-state.ts`, `src/workspace.ts`, `src/sandbox-facts.ts`, `src/launcher.ts` | the lock, the turn file, the tool state (read), the `code-sandbox` link, `ready`, the one spawn |
| `bridge/index.ts`, `bridge/pep.ts`, `bridge/tools.ts`, `bridge/tool-set.ts` | the factory, `GET /manifest` and `POST /call`, one pi tool per entry |
| `bridge/codemode.ts`, `bridge/untrusted.ts`, `bridge/turn-context.ts`, `bridge/pi-api.ts` | the frame, the marker, the three headers, the declared pi slice |
| `bridge/search.ts`, `bridge/index-store.ts`, `bridge/query-embed.ts`, `bridge/tool-state.ts`, `bridge/constants.ts` | `index_search`, one store, `embed`, the tool state (write), every fixed name |

## Known gaps

- The heartbeat deadline starts at `ready` with the default 90 s, and `hello`
  re-arms it (`src/playpen.ts`).
- A terminal runs with no persona (`src/pi-launch.ts`).
- `agent-pi-launch --new` creates a pi store that `attendance` has no record
  of (`src/pi-launch.ts`).
- The playpen reads three shapes for an entry's text and answers `""` for any
  other (`src/entry-text.ts`).
- A `playpen` release builds no image: the manifest's `build` cannot carry a
  version allocated at merge (`component.yaml`).
