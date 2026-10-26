// The grant of Flue's built-in sandbox tools. `runtime.json`'s
// `sandbox_tools` is read at the edge into a list of Flue's six names, and
// the factory around `local({cwd})` gives the model only those. An empty
// grant gives no built-in tool.

import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { init, useModel, useSandbox } from "@flue/runtime";
import { start } from "@flue/runtime/node";
import { afterAll, describe, expect, it } from "vitest";

import { litellmModel } from "../src/litellm.js";
import { readRuntimeConfig } from "../src/runtime-config.js";
import type { SandboxToolName } from "../src/runtime-config.js";
import { grantedSandbox } from "../src/sandbox-tools.js";
import { FakeModel } from "./fake-model.js";
import { familyConfig } from "./family.js";

const GRANT: readonly SandboxToolName[] = ["read", "grep"];

function configDir(runtime: string | null): string {
  const dir = mkdtempSync(join(tmpdir(), "agent-runtime-"));
  if (runtime !== null) {
    writeFileSync(join(dir, "runtime.json"), runtime);
  }

  return dir;
}

function grantOf(runtime: string | null): readonly string[] | string {
  const read = readRuntimeConfig(configDir(runtime));

  return read.ok ? read.config.sandboxTools : read.field;
}

async function toolNames(grant: readonly SandboxToolName[]): Promise<string[]> {
  const factory = grantedSandbox(grant, mkdtempSync(join(tmpdir(), "agent-cwd-")));
  const sandbox = await factory.createSandbox({ id: "agent-test" });

  return (factory.tools?.(sandbox, { subagents: {} }) ?? []).map((tool) => tool.name);
}

const cwd = mkdtempSync(join(tmpdir(), "agent-cwd-"));
const fake = new FakeModel([{ text: "done" }]);
const config = await familyConfig({ litellmUrl: await fake.start(), runtime: { model_alias: "chat" } });
const read = litellmModel(config);
if (!read.ok) {
  throw new Error(read.reason);
}

const { model } = read;

function Empty(): string {
  useModel(model.spec);
  useSandbox(grantedSandbox([], cwd));

  return "Answer.";
}

function Granted(): string {
  useModel(model.spec);
  useSandbox(grantedSandbox(GRANT, cwd));

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

describe("sandbox_tools in runtime.json", () => {
  it("grants nothing when the field or the file is absent", () => {
    expect(grantOf("{}")).toEqual([]);
    expect(grantOf(null)).toEqual([]);
    expect(grantOf("not json")).toEqual([]);
  });

  it("keeps the granted names in order, each one time", () => {
    expect(grantOf('{"sandbox_tools": ["grep", "read", "grep", "bash"]}')).toEqual(["grep", "read", "bash"]);
  });

  it("refuses a name that is not one of Flue's six", () => {
    expect(grantOf('{"sandbox_tools": ["read", "ls"]}')).toBe("sandbox_tools");
    expect(grantOf('{"sandbox_tools": ["codemode"]}')).toBe("sandbox_tools");
    expect(grantOf('{"sandbox_tools": "read"}')).toBe("sandbox_tools");
  });

  it("refuses the family config as runtime_not_valid", async () => {
    await expect(familyConfig({ runtime: { sandbox_tools: ["find"] } })).rejects.toThrow("runtime_not_valid");
  });
});

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
});
