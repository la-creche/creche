// `agent-pi-launch`, the TUI door's end inside the sandbox. A person gets pi's own
// interactive UI, inside the family's sandbox, on a session the playpen
// also serves:
//
//   sbx exec -it --env-file <supervisor.env> <sandbox> -- \
//     node /opt/agent-supervisor/agent-pi-launch.js \
//       --sandbox <family>-s<N> --session <session id> [--cwd <dir>] [--new]
//
// There is no ingress into a sandbox and no second protocol here. The terminal
// IS the client: `sbx exec -it` carries the tty in, pi runs in the foreground
// of that exec, and the person quits when they are done.
//
// This file is the composition root and nothing else, the way `index.ts` is
// for the playpen. It reads the real argv and the real environment, asks
// `pi-launch.ts` for the plan, and runs what the plan names. Everything a test
// needs to replace is an option to `planLaunch`, so nothing below this line is
// ever mocked.

import { spawn } from "node:child_process";
import { constants } from "node:os";

import { piBinary } from "./launcher.js";
import { launchExitCode, LaunchState, planLaunch } from "./pi-launch.js";
import type { LaunchPlan } from "./pi-launch.js";

/** What this program calls itself on stderr. The operator reads these lines. */
const PROGRAM = "agent-pi-launch";

/** The shell's own convention for a child that a signal ended. */
const SIGNAL_EXIT_BASE = 128;

/** pi is not on PATH, or the image carries none. Distinct from every refusal. */
const EXIT_PI_START_FAILED = 11;

/**
 * pi in the foreground of this exec, with the terminal attached.
 *
 * Node offers no `execve`, so this is a child with inherited stdio rather than
 * a replaced process image. One extra process in the VM is the whole
 * difference the operator can see, and forwarding the child's exit keeps a
 * script around the `sbx exec` reading pi's own code rather than this one's.
 *
 * SIGINT and SIGTERM are deliberately not trapped. Inherited stdio leaves the
 * child in this process's foreground group, so the terminal delivers both to
 * pi directly, and a handler here would only add a second, later death.
 */
function runPi(plan: LaunchPlan): void {
  const child = spawn(piBinary(), [...plan.args], {
    cwd: plan.cwd,
    env: plan.env,
    stdio: "inherit",
  });

  child.on("error", (error: Error) => {
    process.stderr.write(`${PROGRAM}: cannot start pi: ${error.message}\n`);
    process.exitCode = EXIT_PI_START_FAILED;
  });

  child.on("exit", (code: number | null, signal: NodeJS.Signals | null) => {
    if (signal !== null) {
      process.exitCode = SIGNAL_EXIT_BASE + (constants.signals[signal] ?? 0);

      return;
    }

    process.exitCode = code ?? 0;
  });
}

async function main(): Promise<void> {
  const plan = await planLaunch({ argv: process.argv.slice(2), env: process.env });

  // Every refusal is one line and a code. `detail` never carries a credential
  // value: §12 rule 6 holds for a terminal's stderr as much as for argv.
  if (plan.state !== LaunchState.Ready) {
    process.stderr.write(`${PROGRAM}: ${plan.detail}\n`);
    process.exitCode = launchExitCode(plan.state);

    return;
  }

  runPi(plan);
}

await main();
