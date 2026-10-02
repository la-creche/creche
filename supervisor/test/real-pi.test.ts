// The one test in this package that runs the REAL pi.
//
// AGENTS.md rule 16 says `test/fake-pi.mjs` is not a pi emulator, and a fake
// pi is exactly what hid contract 01 §3.8's enabling half: every family
// granted `grep`, `find` or `ls` went without them, because `--exclude-tools`
// only removes and pi activates four of its seven built-ins by default
// (0.84.2 `dist/core/sdk.js:132`). Every supervisor test agreed with the
// supervisor, and pi disagreed with both.
//
//   planLaunch ──► the argv and the two files a real pi start uses
//        │
//        ▼
//   node <pinned pi>/dist/cli.js --mode rpc …  ──► probe extension
//                                                    │
//        the active tool names ◄── one JSON file ◄────┘
//
// It needs no model, no network and no service: pi reports its own tool set
// through the extension API before any prompt. The probe extension stands
// where the PEP bridge stands on the host, at the one `--extension` path the
// supervisor names, so the argv is the supervisor's own.

import { build } from "esbuild";
import { afterEach, describe, expect, it } from "vitest";
import { spawn } from "node:child_process";
import type { ChildProcess } from "node:child_process";
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { CRED_DIR_ENV, CONTROL_DIR_ENV, FAMILY_CONFIG_DIR_ENV } from "../src/mounts.js";
import { LaunchState, planLaunch } from "../src/pi-launch.js";
import { RPC_MODE_ARGS } from "../src/pi-args.js";
import { SESSIONS_MOUNT_ENV } from "../src/constants.js";
import { closeMarker } from "../bridge/untrusted.js";
import { FakeModel } from "./fake-model.js";
import { FakePep } from "./fake-pep.js";
import { Harness, until } from "./harness.js";

const SANDBOX = "vault-oracle-s1";
const SESSION = "real-01JBQ7WZ0X4T9V6K2H8M3N5PQR";

/** Where a probe writes what pi told it. The test reads this path back. */
const REPORT_ENV = "AGENT_TOOL_PROBE_REPORT";

/** The name the probe registers, standing in for one PEP tool. */
const PROBE_TOOL = "probe_pep_tool";

/** `workbench/package.json` is the ONE pi pin (AGENTS.md rule 18). */
const WORKBENCH_PACKAGE_JSON = fileURLToPath(
  new URL("../../workbench/package.json", import.meta.url),
);

/**
 * The dev copy's package root, by path rather than by resolution.
 *
 * pi's `exports` map publishes neither `./package.json` nor a `require`
 * condition, and vitest's transform removes `import.meta.resolve`. The
 * install location of a DIRECT dependency is the one thing both package
 * managers agree on, so the test names it.
 */
const PI_ROOT = fileURLToPath(
  new URL("../node_modules/@earendil-works/pi-coding-agent/", import.meta.url),
);

function piManifest(): { readonly version: string; readonly bin: Record<string, string> } {
  return JSON.parse(readFileSync(join(PI_ROOT, "package.json"), "utf8")) as {
    version: string;
    bin: Record<string, string>;
  };
}

/** pi's own CLI entry, from the dev copy this package installs. */
function piCli(): string {
  return join(PI_ROOT, piManifest().bin["pi"] ?? "");
}

function pinnedVersion(): string {
  const pins = JSON.parse(readFileSync(WORKBENCH_PACKAGE_JSON, "utf8")) as {
    dependencies: Record<string, string>;
  };

  return pins.dependencies["@earendil-works/pi-coding-agent"] ?? "";
}

/**
 * How each probe hands its report back, atomically.
 *
 * The test polls for the path and then reads it, so a plain `writeFileSync`
 * lets a read land between the create and the data: `existsSync` answers true
 * on an empty file and `JSON.parse` fails. Temp file and `rename` is the same
 * fence `src/turn-file.ts` and `bridge/tool-state.ts` use for the same
 * reason.
 */
const REPORT_PRELUDE = `import { renameSync, writeFileSync } from "node:fs";

function report(data) {
  const path = process.env[${JSON.stringify(REPORT_ENV)}];
  writeFileSync(path + ".tmp", JSON.stringify(data));
  renameSync(path + ".tmp", path);
}
`;

/**
 * A pi extension that reports the tool set and registers one tool of its own.
 *
 * It stands where `pep-bridge.js` stands, so this argv is the supervisor's
 * own: one `--extension` at a path outside the session store, loaded while
 * `--no-extensions` keeps discovery off. Registering a tool is what proves
 * the enabling mechanism leaves EXTENSION tools alone, which is the whole
 * reason `--tools` and `--no-tools` are forbidden (AGENTS.md rule 22).
 */
const PROBE_EXTENSION = `${REPORT_PRELUDE}
export default function (pi) {
  pi.registerTool({
    name: ${JSON.stringify(PROBE_TOOL)},
    label: "probe",
    description: "Stands in for one tool the PEP bridge registers.",
    parameters: { type: "object", properties: {}, additionalProperties: false },
    execute: async () => ({ content: [{ type: "text", text: "" }] }),
  });

  pi.on("session_start", () => {
    report({ active: pi.getActiveTools(), all: pi.getAllTools().map((tool) => tool.name) });
  });
}
`;

/** How long after start-up the late probe registers its tool. */
const LATE_REGISTER_MS = 1500;

/**
 * A pi extension that registers NOTHING at load and one tool later.
 *
 * It stands where a PEP bridge stands when the manifest fetch failed at
 * process start: the factory returned with no tool, and the PEP answered
 * afterwards. `before` is what the model could call at `session_start` and
 * `after` is what it can call once the late `registerTool` has run. The gap
 * between them is the whole question — pi 0.99.1's loader calls
 * `runtime.refreshTools()` on every `registerTool`, and this test is what says
 * the refresh is real rather than a no-op left over from load time.
 */
const LATE_EXTENSION = `${REPORT_PRELUDE}
export default function (pi) {
  let before = null;

  pi.on("session_start", () => {
    before = pi.getActiveTools();
  });

  setTimeout(() => {
    pi.registerTool({
      name: ${JSON.stringify(PROBE_TOOL)},
      label: "probe",
      description: "Stands in for one tool the PEP bridge registers late.",
      parameters: { type: "object", properties: {}, additionalProperties: false },
      execute: async () => ({ content: [{ type: "text", text: "" }] }),
    });

    report({
      before,
      after: pi.getActiveTools(),
      all: pi.getAllTools().map((tool) => tool.name),
    });
  }, ${LATE_REGISTER_MS});
}
`;

/** The name a grant REMOVES in the probe below. */
const GONE_TOOL = "probe_pep_gone";

/** The name a grant ADDS in the probe below. */
const NEW_TOOL = "probe_pep_new";

/** What a re-registered tool's description reads after the grant moved. */
const SECOND_DESCRIPTION = "The same tool, described the way the new grant describes it.";

/**
 * A pi extension that reshapes its tool set after start-up, three ways at once.
 *
 * It stands where the PEP bridge stands when the family's grants moved under a
 * running process: one tool GRANTED, one REMOVED, one kept with a new
 * description. pi's loader keys an extension's tools by name
 * (0.99.1 `dist/core/extensions/loader.js`, `extension.tools.set`), so the
 * re-registration is a replacement, and `setActiveTools` is the only lever
 * that takes a tool away again. Whether either is real at run time is what
 * this probe answers, so the bridge never has to guess it.
 *
 * `setActiveTools` runs LAST and from the CURRENT active list, because
 * `registerTool` refreshes the tool set and a refresh re-activates every name
 * the registry did not hold before.
 */
const RESHAPE_EXTENSION = `${REPORT_PRELUDE}
function tool(name, description) {
  return {
    name,
    label: "probe",
    description,
    parameters: { type: "object", properties: {}, additionalProperties: false },
    execute: async () => ({ content: [{ type: "text", text: "" }] }),
  };
}

export default function (pi) {
  pi.registerTool(tool(${JSON.stringify(PROBE_TOOL)}, "The first grant's words."));
  pi.registerTool(tool(${JSON.stringify(GONE_TOOL)}, "A tool the next grant takes away."));

  let before = null;

  pi.on("session_start", () => {
    before = pi.getActiveTools();
  });

  setTimeout(() => {
    pi.registerTool(tool(${JSON.stringify(NEW_TOOL)}, "A tool the next grant adds."));
    pi.registerTool(tool(${JSON.stringify(PROBE_TOOL)}, ${JSON.stringify(SECOND_DESCRIPTION)}));
    pi.setActiveTools(pi.getActiveTools().filter((name) => name !== ${JSON.stringify(GONE_TOOL)}));

    report({
      before,
      after: pi.getActiveTools(),
      described: pi.getAllTools().map((one) => [one.name, one.description]),
    });
  }, ${LATE_REGISTER_MS});
}
`;

/**
 * A pi extension that withdraws a tool and then grants it back.
 *
 * pi 0.99 still has no unregister. Its `docs/extensions.md` names one way to
 * withdraw a tool instead: register it again with `exposure: "hidden"`. A
 * hidden tool stays registered, is declared to nobody and is callable by
 * nobody, codemode scripts included, which `setActiveTools` alone cannot
 * promise for a tool a script may reach. The second re-registration is a
 * grant that comes back.
 */
const WITHDRAW_EXTENSION = `${REPORT_PRELUDE}
function tool(name, exposure) {
  return {
    name,
    label: "probe",
    description: "A tool one grant takes away and the next gives back.",
    parameters: { type: "object", properties: {}, additionalProperties: false },
    exposure,
    execute: async () => ({ content: [{ type: "text", text: "" }] }),
  };
}

function exposures() {
  return pi.getAllTools().map((one) => [one.name, one.exposure]);
}

let pi = null;

export default function (api) {
  pi = api;
  pi.registerTool(tool(${JSON.stringify(PROBE_TOOL)}, "direct"));
  pi.registerTool(tool(${JSON.stringify(GONE_TOOL)}, "direct"));

  setTimeout(() => {
    pi.registerTool(tool(${JSON.stringify(GONE_TOOL)}, "hidden"));
    const withdrawn = { active: pi.getActiveTools(), exposures: exposures() };

    pi.registerTool(tool(${JSON.stringify(GONE_TOOL)}, "direct"));
    report({ withdrawn, returned: { active: pi.getActiveTools(), exposures: exposures() } });
  }, ${LATE_REGISTER_MS});
}
`;

/** Every probe's family carries these instructions in its config mount. */
const FAMILY_INSTRUCTIONS = "You are the operator's general assistant. Answer in plain words.";

/** pi's own preamble, which a family with `system_prompt: replace` must not get. */
const PI_PREAMBLE = "You are an expert coding assistant";

/** A pi extension that reports the system prompt pi built at session start. */
const PROMPT_EXTENSION = `${REPORT_PRELUDE}
export default function (pi) {
  pi.on("session_start", (_event, ctx) => {
    report({ prompt: ctx.getSystemPrompt() });
  });
}
`;

/** What `WITHDRAW_EXTENSION` writes: the tool set after each re-registration. */
interface WithdrawReport {
  readonly withdrawn: { readonly active: readonly string[]; readonly exposures: readonly (readonly [string, string])[] };
  readonly returned: { readonly active: readonly string[]; readonly exposures: readonly (readonly [string, string])[] };
}

interface ToolReport {
  /** The tool names the model may call right now. */
  readonly active: readonly string[];
  /** Every tool pi configured, active or not. */
  readonly all: readonly string[];
}

/** What `LATE_EXTENSION` writes: the active set on each side of one late call. */
interface LateReport {
  readonly before: readonly string[] | null;
  readonly after: readonly string[];
  readonly all: readonly string[];
}

/** What `RESHAPE_EXTENSION` writes: the active set and every description. */
interface ReshapeReport {
  readonly before: readonly string[] | null;
  readonly after: readonly string[];
  readonly described: readonly (readonly [string, string])[];
}

const live: { harness?: Harness; child?: ChildProcess }[] = [];

afterEach(async () => {
  while (live.length > 0) {
    const one = live.pop();
    one?.child?.kill("SIGKILL");
    await one?.harness?.dispose();
  }
});

/**
 * Starts the real pi the way the supervisor starts it, and reports its tools.
 *
 * The environment is `buildTurnEnv`'s own. pi's update check and install
 * ping would reach the network from a suite that must not (AGENTS.md rule
 * 15), and the supervisor's own `--offline` is what stops them.
 */
async function probeReport<ReportT>(
  extension: string,
  sandboxTools: readonly string[],
  shell: boolean,
  waitMs: number,
  runtime: Record<string, unknown> = {},
): Promise<ReportT> {
  const harness = new Harness({
    sandbox: SANDBOX,
    runtime: { shell, sandbox_tools: sandboxTools, ...runtime },
  });
  const slot: { harness?: Harness; child?: ChildProcess } = { harness };
  live.push(slot);

  const probe = join(harness.root, "config", "probe-extension.js");
  writeFileSync(probe, extension);
  writeFileSync(join(harness.root, "config", "instructions.md"), FAMILY_INSTRUCTIONS);

  const plan = await planLaunch({
    argv: ["--sandbox", SANDBOX, "--session", SESSION, "--new"],
    env: {
      [CRED_DIR_ENV]: join(harness.root, "creds"),
      [FAMILY_CONFIG_DIR_ENV]: join(harness.root, "config"),
      [CONTROL_DIR_ENV]: join(harness.root, "control"),
      [SESSIONS_MOUNT_ENV]: join(harness.root, "sessions"),
    },
    bridgePath: probe,
  });
  expect(plan.state).toBe(LaunchState.Ready);

  const home = join(harness.root, "home");
  const report = join(harness.root, "tools.json");
  mkdirSync(home, { recursive: true });

  // `planLaunch` answers `PiMode.Interactive`, and §7.6's whole promise is
  // that the rpc line is that line plus these two argv entries. Putting them
  // back here is how this test runs the SUPERVISOR's command line.
  const stderr: string[] = [];
  const child = spawn(process.execPath, [piCli(), ...RPC_MODE_ARGS, ...plan.args], {
    cwd: plan.cwd,
    env: { ...plan.env, HOME: home, [REPORT_ENV]: report },
    stdio: ["pipe", "pipe", "pipe"],
  });
  slot.child = child;
  child.stderr?.on("data", (chunk: Buffer) => stderr.push(chunk.toString("utf8")));

  await until(
    () => existsSync(report),
    `pi to report its tools; its stderr said: ${stderr.join("")}`,
    waitMs,
  );

  return JSON.parse(readFileSync(report, "utf8")) as ReportT;
}

const PROBE_WAIT_MS = 30000;

function toolsOf(sandboxTools: readonly string[], shell: boolean): Promise<ToolReport> {
  return probeReport<ToolReport>(PROBE_EXTENSION, sandboxTools, shell, PROBE_WAIT_MS);
}

describe("the real pi of the pinned version", () => {
  it("runs the version workbench/package.json pins", () => {
    // Rule 18 keeps ONE pin. This dev copy exists for the test below, and a
    // guard against a pi the image does not run is worth nothing.
    expect(piManifest().version).toBe(pinnedVersion());
  });

  it(
    "loads none of pi's built-in extensions, so codemode and MCP stay out",
    { timeout: 60000 },
    async () => {
      // pi 0.99 ships `mcp`, `codemode`, `tool-search` and `llama.cpp` as
      // built-in extensions, and `--no-extensions` now disables them too. A
      // family gains one only by name (`-e builtin:<name>`), never by default.
      const report = await toolsOf(["read", "bash"], true);

      expect(report.all).not.toContain("codemode");
      expect(report.all).not.toContain("tool_search");
      expect(report.all).not.toContain("list_mcp_resources");
    },
  );

  it(
    "activates every built-in a shell-less family was granted, and no other",
    { timeout: 60000 },
    async () => {
      // `vault-oracle`'s own grant. On production it answered a delegate call
      // with "my tools here are limited to read and embed" after 114 s.
      const report = await toolsOf(["read", "grep", "find", "ls"], false);

      expect([...report.active].sort()).toEqual(
        ["find", "grep", "ls", PROBE_TOOL, "read"].sort(),
      );
      expect(report.all).not.toContain("bash");
      expect(report.all).not.toContain("edit");
      expect(report.all).not.toContain("write");
      // pi 0.99's eighth built-in. No family can be granted it, so it must be
      // denied, or it waits in the registry for something to switch it on.
      expect(report.all).not.toContain("powershell");
    },
  );

  it(
    "leaves the extension's tools standing when the family granted no built-in",
    { timeout: 60000 },
    async () => {
      // Contract 01 §3.8 rule 2. An empty list is seven denials and NOT
      // `--no-tools`: the bridge's tools are the grant's business.
      const report = await toolsOf([], false);

      expect(report.active).toEqual([PROBE_TOOL]);
    },
  );

  it(
    "activates a tool an extension registers after start-up",
    { timeout: 60000 },
    async () => {
      // A pi process that started with no manifest can still gain its tools,
      // so a PEP outage does not sentence that process to zero tools for the
      // rest of its life. Without this, the only honest fix is to end the
      // process and start a new one.
      const report = await probeReport<LateReport>(
        LATE_EXTENSION,
        [],
        false,
        PROBE_WAIT_MS + LATE_REGISTER_MS,
      );

      expect(report.before).toEqual([]);
      expect(report.after).toEqual([PROBE_TOOL]);
      expect(report.all).toContain(PROBE_TOOL);
    },
  );

  it(
    "adds, removes and re-describes a tool after start-up",
    { timeout: 60000 },
    async () => {
      // The three facts the bridge needs before it may act on a moved
      // grants revision. A grant ADDED must appear, a grant REMOVED must stop
      // being callable, and a tool whose description or schema changed must
      // carry the new one. pi offers `registerTool` and `setActiveTools` and
      // no way to unregister, so "removed" can only mean "deactivated".
      const report = await probeReport<ReshapeReport>(
        RESHAPE_EXTENSION,
        [],
        false,
        PROBE_WAIT_MS + LATE_REGISTER_MS,
      );

      expect([...(report.before ?? [])].sort()).toEqual([GONE_TOOL, PROBE_TOOL].sort());
      expect([...report.after].sort()).toEqual([NEW_TOOL, PROBE_TOOL].sort());

      // The kept tool carries the second description, so a re-registration
      // REPLACES rather than duplicating.
      const described = new Map(report.described.map(([name, text]) => [name, text]));
      expect(described.get(PROBE_TOOL)).toBe(SECOND_DESCRIPTION);
      expect(report.described.filter(([name]) => name === PROBE_TOOL)).toHaveLength(1);
    },
  );
});

// ── codemode, with the real bridge ──────────────────────────────────────────
//
// pi 0.99's `codemode` runs model-written JavaScript that calls the other
// tools, and only the script's output reaches the model. Every PEP tool is
// one of those tools, so three things must hold before any family gets it:
//
//   fake model ──► codemode { script } ──► tools.<open tool>  ──► fake PEP
//                        │                 tools.<gated tool> ✗ unreachable
//                        ▼
//   the model reads the script's output inside ONE untrusted block
//
// 1. A gated tool is unreachable from a script, so no script can queue a
//    phone tap per call. The model still calls it directly, once.
// 2. A script gets data, not a wrapped string: `{ text, json }`.
// 3. The script's output is invariant 14's data, so it arrives wrapped.
//
// The bundle is the bridge's own source, built the way `pnpm build` builds
// it, and the argv and settings are the supervisor's own for a family whose
// `sandbox_tools` names `codemode`.

const OPEN_TOOL = "kagi__search";
const GATED_TOOL = "vikunja__create_task";

/** What the fake PEP answers every call with: an MCP tool's text, which is JSON. */
const PEP_TEXT = '{"items":[1,2,3]}';

const CODEMODE_SCRIPT = `const found = await tools.${OPEN_TOOL}({ query: "pi" });
let gated;
try {
  await tools.${GATED_TOOL}({});
  gated = "called";
} catch (error) {
  gated = "unreachable";
}
return { found, gated, callable: ALL_TOOLS.map((tool) => tool.name) };`;

const CODEMODE_MANIFEST = {
  family: "chat",
  rev: "rev-codemode",
  tools: [
    {
      name: OPEN_TOOL,
      description: "Search the web.",
      schema: { type: "object", properties: { query: { type: "string" } }, required: ["query"] },
      approval: false,
    },
    {
      name: GATED_TOOL,
      description: "Create a task.",
      schema: { type: "object", properties: {}, additionalProperties: false },
      approval: true,
    },
  ],
};

const BRIDGE_SOURCE = fileURLToPath(new URL("../bridge/index.ts", import.meta.url));

/** The bridge's own bundle, built with `pnpm build:bridge`'s flags. */
async function buildBridge(dir: string): Promise<string> {
  const outfile = join(dir, "pep-bridge.js");
  await build({
    entryPoints: [BRIDGE_SOURCE],
    bundle: true,
    platform: "node",
    target: "node24",
    format: "esm",
    outfile,
    logLevel: "silent",
  });

  return outfile;
}

/**
 * Points pi's one provider at the fake model. It is the one thing this test
 * changes: the codemode grant is the supervisor's own, from `sandbox_tools`.
 */
function pointAtModel(sessionDir: string, modelUrl: string): void {
  const modelsPath = join(sessionDir, "models.json");
  const models = JSON.parse(readFileSync(modelsPath, "utf8")) as {
    providers: Record<string, { baseUrl: string }>;
  };
  for (const provider of Object.values(models.providers)) {
    provider.baseUrl = modelUrl;
  }
  writeFileSync(modelsPath, JSON.stringify(models));
}

/** The object a codemode script returned, read back out of its output. */
function scriptValue(output: string): Record<string, unknown> {
  const start = output.indexOf("{", output.indexOf("Output:"));
  const end = output.lastIndexOf("}");

  return JSON.parse(output.slice(start, end + 1)) as Record<string, unknown>;
}

describe("the real pi, given what a family file asks for", () => {
  it("activates codemode for a family that grants it, with no other help", { timeout: 60000 }, async () => {
    // The supervisor's own argv and settings: `--extension builtin:codemode`
    // and `codemode` in `defaultTools`. Neither alone would do.
    const report = await toolsOf(["read", "codemode"], false);

    expect(report.active).toContain("codemode");
    expect(report.active).toContain(PROBE_TOOL);
  });

  it("replaces pi's coding preamble for a family that asks", { timeout: 60000 }, async () => {
    const report = await probeReport<{ prompt: string }>(PROMPT_EXTENSION, ["read"], false, PROBE_WAIT_MS, {
      system_prompt: "replace",
    });

    expect(report.prompt).toContain(FAMILY_INSTRUCTIONS);
    expect(report.prompt).not.toContain(PI_PREAMBLE);
  });

  it("keeps pi's coding preamble for a family that appends", { timeout: 60000 }, async () => {
    const report = await probeReport<{ prompt: string }>(PROMPT_EXTENSION, ["read"], false, PROBE_WAIT_MS);

    expect(report.prompt).toContain(FAMILY_INSTRUCTIONS);
    expect(report.prompt).toContain(PI_PREAMBLE);
  });
});

describe("the real pi withdraws a tool without an unregister", () => {
  it(
    "hides a tool registered again as hidden, and restores it registered again as direct",
    { timeout: 60000 },
    async () => {
      const report = await probeReport<WithdrawReport>(
        WITHDRAW_EXTENSION,
        [],
        false,
        PROBE_WAIT_MS + LATE_REGISTER_MS,
      );

      expect(report.withdrawn.active).toEqual([PROBE_TOOL]);
      expect(new Map(report.withdrawn.exposures).get(GONE_TOOL)).toBe("hidden");
      expect([...report.returned.active].sort()).toEqual([GONE_TOOL, PROBE_TOOL].sort());
      expect(new Map(report.returned.exposures).get(GONE_TOOL)).toBe("direct");
    },
  );
});

describe("the real pi with codemode and the real bridge", () => {
  it(
    "keeps gated tools out of scripts, gives scripts data, and wraps what the model reads",
    { timeout: 60000 },
    async () => {
      const pep = new FakePep();
      pep.manifest = { status: 200, body: CODEMODE_MANIFEST };
      pep.answer = { status: 200, body: { result: { text: PEP_TEXT } } };
      const model = new FakeModel(CODEMODE_SCRIPT);
      const harness = new Harness({
        sandbox: SANDBOX,
        runtime: { shell: false, sandbox_tools: ["read", "codemode"] },
      });
      const slot: { harness?: Harness; child?: ChildProcess } = { harness };
      live.push(slot);

      const pepUrl = await pep.start();
      const modelUrl = await model.start();
      try {
        const plan = await planLaunch({
          argv: ["--sandbox", SANDBOX, "--session", SESSION, "--new"],
          env: {
            [CRED_DIR_ENV]: join(harness.root, "creds"),
            [FAMILY_CONFIG_DIR_ENV]: join(harness.root, "config"),
            [CONTROL_DIR_ENV]: join(harness.root, "control"),
            [SESSIONS_MOUNT_ENV]: join(harness.root, "sessions"),
          },
          bridgePath: await buildBridge(harness.root),
        });
        expect(plan.state).toBe(LaunchState.Ready);
        pointAtModel(plan.env["PI_CODING_AGENT_DIR"] ?? "", modelUrl);

        const home = join(harness.root, "home");
        mkdirSync(home, { recursive: true });
        const args = [piCli(), ...RPC_MODE_ARGS, ...plan.args];
        const child = spawn(process.execPath, args, {
          cwd: plan.cwd,
          env: { ...plan.env, HOME: home, PEP_URL: pepUrl },
          stdio: ["pipe", "pipe", "pipe"],
        });
        slot.child = child;

        let stderr = "";
        let stdout = "";
        child.stderr?.on("data", (chunk: Buffer) => {
          stderr += chunk.toString("utf8");
        });
        child.stdout?.on("data", (chunk: Buffer) => {
          stdout += chunk.toString("utf8");
        });

        await until(() => stderr.includes("registered 2 PEP tools"), `the bridge; stderr: ${stderr}`, 30000);
        child.stdin?.write(`${JSON.stringify({ id: "p1", type: "prompt", message: "go" })}\n`);
        await until(() => stdout.includes('"agent_settled"'), `the turn; stderr: ${stderr}`, 30000);

        // 1. Declared to the model: both, the gated one as a direct call.
        expect(model.seen[0]?.tools).toEqual(expect.arrayContaining(["codemode", OPEN_TOOL, GATED_TOOL]));

        // The PEP saw the open tool once and the gated tool never.
        expect(pep.calls.map((call) => call.body["tool"])).toEqual([OPEN_TOOL]);

        // 3. One untrusted block around everything the script produced.
        const output = model.toolResults[0] ?? "";
        expect(output.startsWith('[untrusted output from "codemode"')).toBe(true);
        expect(output.endsWith(closeMarker("codemode"))).toBe(true);

        // 2. The script got data, and could not see the gated tool.
        const value = scriptValue(output);
        expect(value["found"]).toEqual({ text: PEP_TEXT, json: { items: [1, 2, 3] } });
        expect(value["gated"]).toBe("unreachable");
        expect(value["callable"]).not.toContain(GATED_TOOL);
      } finally {
        await pep.stop();
        await model.stop();
      }
    },
  );
});
