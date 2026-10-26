// The family's grant of Flue's built-in sandbox tools. `local()` from
// `@flue/runtime/node` gives every agent six tools on the host: `read`,
// `write`, `edit`, `bash`, `grep`, `glob`. This factory wraps `local({cwd})`
// and its `tools()` returns only the tools that `runtime.json`'s
// `sandbox_tools` grants. Flue replaces its built-in set with that list, so
// an empty grant gives the model no built-in tool.
//
//   runtime.json sandbox_tools ──► SandboxToolName[] (src/runtime-config.ts)
//                              ──► tools(sandbox) = the granted subset
//
// The framework group (`task`, `activate_skill`) is not part of the
// built-in set. A grant does not change it.

import {
  createBashTool,
  createEditTool,
  createGlobTool,
  createGrepTool,
  createReadTool,
  createWriteTool,
} from "@flue/runtime";
import type { Sandbox, SandboxFactory, SandboxToolFactory } from "@flue/runtime";
import { local } from "@flue/runtime/node";

import type { SandboxToolName } from "./runtime-config.js";

type ToolFactory = (sandbox: Sandbox) => ReturnType<SandboxToolFactory>[number];

/** One factory per name. The map is total, so a new name breaks the build. */
const FACTORIES: { readonly [Name in SandboxToolName]: ToolFactory } = {
  read: createReadTool,
  write: createWriteTool,
  edit: createEditTool,
  bash: createBashTool,
  grep: createGrepTool,
  glob: createGlobTool,
};

/** `local({cwd})`, with only the granted built-in tools. */
export function grantedSandbox(grant: readonly SandboxToolName[], cwd: string): SandboxFactory {
  const host = local({ cwd });

  return {
    createSandbox: (options) => host.createSandbox(options),
    tools: (sandbox) => grant.map((name) => FACTORIES[name](sandbox)),
  };
}
