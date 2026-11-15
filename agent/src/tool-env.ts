// The env of each tool process (decision CH49 rules 1 and 2). Flue's
// `local()` copies only its own 14 names from `process.env` into a tool.
// The agent adds the names below, read one time from its env at the edge.
//
//   facade env ──► FLUE_ENV        Flue copies these itself
//              ──► TOOL_ENV        the agent copies each one that is set
//              ──► TOOL_RENAME     the agent copies a set source to each target
//              ──► FACADE_ENV      never reaches a tool
//
// Each name of each built flavor's env sits in exactly one of the four
// lists (CH49 rule 1). `test/tool-env.test.ts` holds the lists disjoint,
// and places each `ENV` name of `playpen/Dockerfile`. CI places the whole
// built env, the parent image's names too: `playpen/bin/playpen-env-check`
// runs `placeEnvFile` in each flavor on its `/etc/creche/env`. A new name
// fails CI until someone picks a list.

/**
 * Flue 2.3.0 `DEFAULT_LOCAL_ENV_ALLOWLIST`. Flue does not export it. A test
 * compares this copy with Flue's installed dist file.
 */
export const FLUE_ENV = [
  "PATH",
  "HOME",
  "USER",
  "LOGNAME",
  "HOSTNAME",
  "SHELL",
  "LANG",
  "LC_ALL",
  "LC_CTYPE",
  "TZ",
  "TERM",
  "TMPDIR",
  "TMP",
  "TEMP",
] as const;

/** The names that the agent copies from its env into each tool, when set. */
export const TOOL_ENV = ["MPLBACKEND"] as const;

/**
 * An image name, and the tool names it becomes. The coding flavor names
 * the egress proxy in `CRECHE_TOOL_HTTPS_PROXY`, and tools get it as
 * `HTTPS_PROXY` and `https_proxy` only (decision CH46 rules 2 and 3): no
 * `HTTP_PROXY`, no `NO_PROXY`, no proxy CA and no `NODE_USE_ENV_PROXY`.
 */
export const TOOL_RENAME: Readonly<Record<string, readonly string[]>> = {
  CRECHE_TOOL_HTTPS_PROXY: ["HTTPS_PROXY", "https_proxy"],
};

/**
 * The names that never reach a tool. npm and pnpm skip devDependencies
 * under `NODE_ENV=production`. `MPLCONFIGDIR` stays here until the guest
 * write layout gives it a writable path (CH52 rule 4, CH49 rule 2, CH8-8).
 * The root disk is read-only.
 */
export const FACADE_ENV = ["NODE_ENV", "NODE_VERSION", "YARN_VERSION", "MPLCONFIGDIR"] as const;

/**
 * The names that no list holds, in the order given. The lists are
 * disjoint, so a name outside this result sits in exactly one list.
 */
export function unplacedEnv(names: readonly string[]): string[] {
  const placed: readonly string[] = [...FLUE_ENV, ...TOOL_ENV, ...Object.keys(TOOL_RENAME), ...FACADE_ENV];
  return names.filter((name) => !placed.includes(name));
}

/**
 * The report of `playpen/bin/playpen-env-check` on the text of one
 * `/etc/creche/env` file (CH49 rule 1). The first line is the count of
 * `NAME=value` lines that this function read. Each further line is one
 * name that no list holds. The check compares the count with the line
 * count of the file, so an empty or short report fails the flavor.
 */
export function placeEnvFile(text: string): string {
  const names = text
    .split("\n")
    .filter((line) => line !== "")
    .map((line) => line.split("=", 1)[0] ?? line);
  return [String(names.length), ...unplacedEnv(names)].map((line) => `${line}\n`).join("");
}

/** The names and values that the agent adds to each tool's env. */
export type ToolEnv = Readonly<Record<string, string>>;

/** The tool env from the agent's env. Only set names go in. */
export function readToolEnv(
  env: NodeJS.ProcessEnv,
  names: readonly string[] = TOOL_ENV,
  rename: Readonly<Record<string, readonly string[]>> = TOOL_RENAME,
): ToolEnv {
  const out: Record<string, string> = {};
  for (const name of names) {
    const value = env[name];
    if (value !== undefined) {
      out[name] = value;
    }
  }

  for (const [source, targets] of Object.entries(rename)) {
    const value = env[source];
    if (value !== undefined) {
      for (const target of targets) {
        out[target] = value;
      }
    }
  }

  return out;
}
