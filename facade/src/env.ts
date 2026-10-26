// The two variables the supervisor needs before it serves: the sandbox id,
// for the lock record, and the control mount, for the lock file. A refusal
// is a typed value with the exit status of the playpen for the same fault.
//
// CONTRACT-QUESTION: `agent/src/config.ts` (`readFamilyConfig`) parses the
// same variables with the rest of the family config. Row 15 of the Phase 2
// plan wires the agent module, and this file then goes.

export const EXIT_NO_SANDBOX = 2;
export const EXIT_MOUNT_UNSET = 7;

const SANDBOX_ENV = "AGENT_SANDBOX";
const CONTROL_ENV = "AGENT_CONTROL_DIR";

export type SupervisorEnv =
  | { readonly ok: true; readonly sandbox: string; readonly controlDir: string }
  | { readonly ok: false; readonly exitCode: number; readonly detail: string };

export function readSupervisorEnv(env: NodeJS.ProcessEnv): SupervisorEnv {
  const sandbox = env[SANDBOX_ENV] ?? "";
  if (sandbox.length === 0) {
    return { ok: false, exitCode: EXIT_NO_SANDBOX, detail: `${SANDBOX_ENV} is required` };
  }

  const controlDir = env[CONTROL_ENV] ?? "";
  if (controlDir.length === 0) {
    return { ok: false, exitCode: EXIT_MOUNT_UNSET, detail: `${CONTROL_ENV} is required` };
  }

  return { ok: true, sandbox, controlDir };
}
