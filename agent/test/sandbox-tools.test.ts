// The grant of Flue's built-in sandbox tools. The factory around
// `local({cwd})` gives the model only the granted names of Flue's six. An
// empty grant gives no built-in tool.

import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { init, useModel, useSandbox } from "@flue/runtime";
import { start } from "@flue/runtime/node";
import { afterAll, describe, expect, it } from "vitest";

import { FALLBACK_LIMITS, LiveModel, specOf } from "../src/litellm.js";
import type { SandboxToolName } from "../src/runtime-config.js";
import { grantedSandbox } from "../src/sandbox-tools.js";
import { readToolEnv } from "../src/tool-env.js";
import { FakeModel } from "./fake-model.js";
import { familyConfig } from "./family.js";

const GRANT: readonly SandboxToolName[] = ["read", "grep"];
const ALIAS = "chat";

async function toolNames(grant: readonly SandboxToolName[]): Promise<string[]> {
  const factory = grantedSandbox(grant, mkdtempSync(join(tmpdir(), "agent-cwd-")), {});
  const sandbox = await factory.createSandbox({ id: "agent-test" });

  return (factory.tools?.(sandbox, { subagents: {} }) ?? []).map((tool) => tool.name);
}

const cwd = mkdtempSync(join(tmpdir(), "agent-cwd-"));
const fake = new FakeModel([{ text: "done" }]);
const config = familyConfig({ litellmUrl: await fake.start() });
const model = new LiveModel(config.planes, ALIAS, FALLBACK_LIMITS, () => config.creds);

function Empty(): string {
  useModel(specOf(ALIAS));
  useSandbox(grantedSandbox([], cwd, {}));

  return "Answer.";
}

function Granted(): string {
  useModel(specOf(ALIAS));
  useSandbox(grantedSandbox(GRANT, cwd, {}));

  return "Answer.";
}

const flue = await start({ agents: [Empty, Granted], providers: [model.provider] });
afterAll(async () => {
  await flue.stop();
  await fake.stop();
});

async function toolsSeen(agent: typeof Empty): Promise<readonly string[]> {
  const before = fake.seen.length;
  const handle = init(agent);
  await handle.read(await handle.dispatch("Go."));

  return fake.seen[before]?.tools ?? [];
}

describe("grantedSandbox", () => {
  it("gives no built-in tool for an empty grant", async () => {
    expect(await toolNames([])).toEqual([]);
  });

  it("gives the granted tools only", async () => {
    expect(await toolNames(GRANT)).toEqual(["read", "grep"]);
    expect(await toolNames(["read", "write", "edit", "bash", "grep", "glob"])).toHaveLength(6);
  });

  it("shows the model no built-in tool under an empty grant, and the grant under one", async () => {
    expect(await toolsSeen(Empty)).toEqual(["task"]);
    expect([...(await toolsSeen(Granted))].sort()).toEqual(["grep", "read", "task"]);
  });

  it("gives a tool process the tool env, and no facade name", async () => {
    // A copy of process.env into the tool would show this sentinel.
    const before = process.env["MPLCONFIGDIR"];
    process.env["MPLCONFIGDIR"] = "/var/cache/matplotlib";
    try {
      const env = readToolEnv({ MPLBACKEND: "Agg", MPLCONFIGDIR: "/var/cache/matplotlib", NODE_VERSION: "24" });
      const sandbox = await grantedSandbox(["bash"], cwd, env).createSandbox({ id: "agent-env" });
      const seen = await sandbox.exec('printf %s "$MPLBACKEND,${MPLCONFIGDIR-unset},${NODE_VERSION-unset}"');

      expect(seen.stdout).toBe("Agg,unset,unset");
    } finally {
      if (before === undefined) {
        delete process.env["MPLCONFIGDIR"];
      } else {
        process.env["MPLCONFIGDIR"] = before;
      }
    }
  });
});
