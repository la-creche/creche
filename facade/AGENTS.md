# facade

The ACP facade: the ACP v1 agent of one family sandbox. attendance starts
one `creche-facade` per sandbox with `sbx exec` and speaks ACP over its
stdio. The root `AGENTS.md` applies here too.

ACP is the Agent Client Protocol. The SDK is `@agentclientprotocol/sdk`.
Flue is the agent runtime `@flue/runtime`.

## Build and test

```bash
pnpm install
pnpm test          # builds dist/ first. No sbx, no VM, no model call
pnpm run typecheck
pnpm run build     # src/ to dist/src/, the vector file to dist/vectors/
```

The tests run the built bin `dist/src/bin/creche-facade.js` as a child
process, as attendance runs it.

## The wire

`vectors/creche-extensions.json` holds the creche extensions of ACP: the
answer to `initialize`, `_creche/beat` and `_creche/shutdown`, the line cap
and the beat cadence. The facade tests read it. The attendance tests read it
too. Change a value there, and both sides change.

## Rules

1. Pin each runtime dependency to one exact version. Do not use a range.
   ACP SDK 2.0 is new, and a patch of Flue can change a record that the
   facade reads.
2. Only `src/acp.ts` imports the ACP SDK. Other modules import the ACP
   types from `src/acp.ts`.
3. Write to stdout only through the writer of `src/stdio.ts`. Each line is
   strict JSON under the line cap (`src/line.ts`). Free text goes to
   stderr.
4. A line that is not strict JSON, or that is over the cap, never reaches
   stdout. An answer becomes a JSON-RPC error with the same id. Another
   message is dropped, with a line on stderr.

## Known gaps

None.
