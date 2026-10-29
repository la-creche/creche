# agent

The agent module: the one generic Flue agent of a family sandbox. It is a
library. The ACP facade imports it and runs it with Flue's `start()`. The
module has no bin and no HTTP surface. The root `AGENTS.md` applies here too.

Flue is the agent runtime `@flue/runtime`. pi is the model layer under it,
`@earendil-works/pi-ai` and `@earendil-works/pi-agent-core`.

## Build and test

```bash
pnpm install
pnpm test          # no sbx, no VM, no model call
pnpm run typecheck
pnpm run build     # src/ to dist/ with declarations, for the facade
```

The facade imports `agent/agent` and `agent/config` from `dist/`
(`package.json` `exports`). Its `build` and `typecheck` run this build
first.

The tests use `sqlite()` with no path, an in-memory store, and pi's
`fauxProvider()`. A test needs no network and no model key.

## Rules

1. Pin each runtime dependency to one exact version. Do not use a range.
   A patch of Flue or pi can change a record that the facade reads.
2. Do not put a credential in `useInitialData`. Flue writes that value one
   time into `flue.db`, on a mount that the agent can write, and never
   rotates it. Read the credentials from the mounts at process start.
3. Give the model only the tools of the grant. This rule applies also to
   the built-in tools of Flue's `local()`.
4. Do not write prose to stdout. The facade owns stdout for ACP. Free text
   goes to stderr.
5. Parse each input at the edge, one time. The environment, the mounts and
   each file there become typed values before other code reads them. A
   refusal is a typed value, not an exception.
6. Never log the value of a credential. Never put a real one in a fixture.

## Use

The facade reads the edge, composes the agent and starts Flue, one time,
at process start:

```ts
const read = await readFamilyConfig(process.env);       // src/config.ts
const composed = composeAgent(read.config, workspace);  // src/agent.ts
await start({ agents: [composed.agent], db: composed.db, providers: composed.providers });
```

Each step gives a typed refusal, not an exception. The store of
`start()` is `<AGENT_SESSIONS_DIR>/flue.db`. The facade writes the names
in `composed.skippedSkills` to stderr.

## Environment the agent expects

`src/config.ts` reads each variable one time, at process start, into one
`FamilyConfig`. A fault gives one refusal. The refusal names the variable or
the field, never the value.

| Variable | Meaning | Absent |
|---|---|---|
| `AGENT_SESSIONS_DIR` | the sessions directory, for `flue.db` | `mount_dir_unset` |
| `AGENT_CRED_DIR` | the credential directory, for `creds.json` | `mount_dir_unset` |
| `AGENT_FAMILY_CONFIG_DIR` | the family config directory | `mount_dir_unset` |
| `AGENT_CONTROL_DIR` | the control directory | `mount_dir_unset` |
| `AGENT_INDEX_ROOT` | the `library/` index store | no index |
| `AGENT_SANDBOX` | the sandbox id | `sandbox_unset` |
| `AGENT_PEP_URL` | the URL of the chaperone | `plane_unset` |
| `AGENT_LITELLM_BASE_URL` | the base URL of LiteLLM | `plane_unset` |

A credential is a `Secret` (`src/secret.ts`). Only the `auth` of the `ch`
connection and the `auth` of the LiteLLM provider unwrap it.
`test/secret.test.ts` holds that list.

## Known gaps

- Contract 03 §7 gives the two plane URLs as fixed values. The playpen
  takes the URL of its build for a plane variable that is absent or not
  valid. The agent has no build URL, so it refuses (`src/config.ts`).
- The plane grammar has no vector. `test/planes.test.ts` holds a copy of
  the tables of the tests of the Rust type `PlaneUrl`, and no check
  compares the two copies.
- Flue adds its tool `task` to every agent, also with no subagent. The
  model sees `task` beside the `mcp__ch__` tools
  (`test/chaperone.test.ts`). With no subagent, `task` can run nothing.
- The playpen framed each PEP tool result as untrusted data (invariant
  14). Flue's MCP adapter gives a `ch` result to the model as flat text,
  with no frame. A gated call reaches the model as a tool error with the
  chaperone's text.
- Flue gives the output of a `useTool` tool to the model as JSON text. A
  string output arrives in quotes, with each line break as `\n`. The
  untrusted frame of `index_search` arrives in that form too. An MCP
  result arrives as plain text.
- No contract names the embedding endpoint of `index_search`. Q38 drops
  `embed` from the chaperone. The agent takes no endpoint, so the vector
  half refuses with one note and the words half serves
  (`src/query-embed.ts`).
- Contract 01 §3.8 names other `sandbox_tools` (`find`, `ls`,
  `codemode`), a default of `read`, `grep`, `find`, `ls`, and `shell` as
  the only door to `bash`. The owner's decision for row 6: Flue's six
  names, an empty default, and a refusal for any other name
  (`runtime_not_valid`). `shell` is read and grants nothing
  (`src/runtime-config.ts`).
- With a granted sandbox tool, Flue attaches `local()` at the workspace.
  Flue then puts the workspace's `AGENTS.md` and a directory listing in
  the prompt, and offers each skill under `<workspace>/.agents/skills/`.
  For an empty grant, the agent must attach no sandbox.
- Codemode does not use `createMcpConnection`. The `run` of each tool
  that it adapts throws, because the adapter that runs a call is
  internal to Flue. So each script opens its own MCP client on the `ch`
  definition (`src/codemode.ts`), with Flue's client package at Flue's
  pin. The script gets the chaperone's names, `tools.<upstream>__<tool>`.
- The facade chooses the workspace of the granted sandbox tools:
  `$AGENT_SESSIONS_DIR/workspace`. The playpen gave `coding-sandbox` one
  directory for each chat. The agent has no chat id (plan risk 6).
