// The names of the family config that the agent renders. caregiver writes
// them in `runtime.json`. The vmm reads that file and sends them in each
// pull as the wire type `FamilyConfig` (`src/family-state.ts`).
//
// CONTRACT-QUESTION: contract 01 §3.8 names `read`, `write`, `edit`, `grep`,
// `find`, `ls` and `codemode`, defaults to `read`, `grep`, `find`, `ls`, and
// makes `shell` the only door to `bash`. The owner's decision for row 6 of
// the Phase 2 plan: the names are Flue's six, the default is empty, and the
// edge validates the list. The vmm takes the owner's reading. It refuses a
// `runtime.json` with a name of the contract as `not_valid`. The pull
// carries no `shell`, so `shell` grants nothing.

/** The six built-in tools of Flue's `local()` sandbox, by Flue's names. */
export const SANDBOX_TOOL_NAMES = ["read", "write", "edit", "bash", "grep", "glob"] as const;

export type SandboxToolName = (typeof SANDBOX_TOOL_NAMES)[number];

/** How `instructions.md` meets the agent's own prompt. */
export type SystemPrompt = "append" | "replace";
