// The two plane URLs, from the env file of a sandbox to a pi process.
//
//   supervisor-<family>-s<N>.env ──► sbx exec --env-file ──► the playpen
//                                                              │
//                 PEP_URL, LITELLM_BASE_URL and models.json ◄──┘
//
// The playpen reads `AGENT_PEP_URL` and `AGENT_LITELLM_BASE_URL` one time.
// Each pi process then gets the two URLs in its environment, and LiteLLM's
// also in `models.json`. A variable that the playpen does not take gives the
// URL of the build and one `log` line. The terminal launcher reads the same
// two variables.

import { afterEach, describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { join } from "node:path";

import { LITELLM_BASE_URL, PEP_URL, SESSIONS_MOUNT_ENV } from "../src/constants.js";
import { CONTROL_DIR_ENV, CRED_DIR_ENV, FAMILY_CONFIG_DIR_ENV } from "../src/mounts.js";
import { LaunchState, planLaunch } from "../src/pi-launch.js";
import { LITELLM_PLANE_ENV, PEP_PLANE_ENV } from "../src/planes.js";
import type { LogMessage } from "../src/protocol.js";
import { Harness, until } from "./harness.js";

const SANDBOX = "chat-s1";
const PEP = "http://192.0.2.20:8301";
const LITELLM = "http://192.0.2.20:4001";

const PLANE_VARIABLES = [PEP_PLANE_ENV, LITELLM_PLANE_ENV] as const;

/** A value with a user part. No byte of it may reach a line or a file. */
const NOT_VALID = "http://operator:FIXTURE-WORD@192.0.2.30:4002";
const NOT_VALID_PARTS = ["FIXTURE-WORD", "192.0.2.30"];

function turnId(n: number): string {
  return `01JBQ7WZ0X4T9V6K2H8M3N${String(n).padStart(4, "0")}`;
}

const live: Harness[] = [];

function open(options: ConstructorParameters<typeof Harness>[0] = {}): Harness {
  const harness = new Harness(options);
  live.push(harness);

  return harness;
}

afterEach(async () => {
  while (live.length > 0) {
    await live.pop()?.dispose();
  }
});

/** The `baseUrl` of the one provider in a session's `models.json`. */
function modelsBaseUrl(sessionDir: string): unknown {
  const doc = JSON.parse(readFileSync(join(sessionDir, "models.json"), "utf8")) as {
    providers: Record<string, Record<string, unknown>>;
  };

  return doc.providers["litellm"]?.["baseUrl"];
}

/** Each `log` line that names a plane variable. */
function planeLines(harness: Harness): LogMessage[] {
  return harness
    .of("log")
    .filter((line) => PLANE_VARIABLES.some((name) => line.message.includes(name)));
}

/** One turn on a new pi process, and the environment of that process. */
async function spawnEnv(
  harness: Harness,
  session: string,
  turn: number,
): Promise<NodeJS.ProcessEnv> {
  const before = harness.spawns.length;
  harness.startTurn(session, turnId(turn));
  await until(() => harness.spawns.length === before + 1, `the spawn for ${session}`);

  return harness.spawns[before]?.env ?? {};
}

describe("the plane URLs of a pi process", () => {
  it("come from the two variables of the environment", async () => {
    const harness = open({ planeEnv: { [PEP_PLANE_ENV]: PEP, [LITELLM_PLANE_ENV]: LITELLM } });
    harness.start();
    harness.hello();

    const env = await spawnEnv(harness, "owui-planes", 1);

    expect(env["PEP_URL"]).toBe(PEP);
    expect(env["LITELLM_BASE_URL"]).toBe(LITELLM);
    expect(modelsBaseUrl(harness.sessionDir("owui-planes"))).toBe(LITELLM);
    expect(planeLines(harness)).toEqual([]);
  });

  it("are the URLs of the build for a variable that is absent, with one warn line", async () => {
    const harness = open({ planeEnv: { [LITELLM_PLANE_ENV]: LITELLM } });
    harness.start();
    harness.hello();

    const env = await spawnEnv(harness, "owui-no-pep", 1);

    expect(env["PEP_URL"]).toBe(PEP_URL);
    expect(env["LITELLM_BASE_URL"]).toBe(LITELLM);
    expect(modelsBaseUrl(harness.sessionDir("owui-no-pep"))).toBe(LITELLM);

    const lines = planeLines(harness);
    expect(lines).toHaveLength(1);
    expect(lines[0]?.level).toBe("warn");
    expect(lines[0]?.session).toBeNull();
    expect(lines[0]?.message).toContain(PEP_PLANE_ENV);
    expect(lines[0]?.message).not.toContain(LITELLM_PLANE_ENV);
  });

  it("are the URLs of the build for a value that is not valid, with one error line", async () => {
    const planeEnv = { [PEP_PLANE_ENV]: PEP, [LITELLM_PLANE_ENV]: NOT_VALID };
    const harness = open({ planeEnv });
    harness.start();
    harness.hello();

    const env = await spawnEnv(harness, "owui-bad-llm", 1);

    expect(env["PEP_URL"]).toBe(PEP);
    expect(env["LITELLM_BASE_URL"]).toBe(LITELLM_BASE_URL);
    expect(modelsBaseUrl(harness.sessionDir("owui-bad-llm"))).toBe(LITELLM_BASE_URL);

    const lines = planeLines(harness);
    expect(lines).toHaveLength(1);
    expect(lines[0]?.level).toBe("error");
    expect(lines[0]?.session).toBeNull();
    expect(lines[0]?.message).toContain(LITELLM_PLANE_ENV);

    // The line names the variable and never the value.
    const sent = JSON.stringify(harness.lines);
    const born = JSON.stringify(harness.spawns);
    for (const part of NOT_VALID_PARTS) {
      expect(sent).not.toContain(part);
      expect(born).not.toContain(part);
    }
  });

  it("are the URLs of the build when the env file carries neither variable", async () => {
    const harness = open({ planeEnv: {} });
    harness.start();
    harness.hello();

    const env = await spawnEnv(harness, "owui-four-lines", 1);

    expect(env["PEP_URL"]).toBe(PEP_URL);
    expect(env["LITELLM_BASE_URL"]).toBe(LITELLM_BASE_URL);
    expect(modelsBaseUrl(harness.sessionDir("owui-four-lines"))).toBe(LITELLM_BASE_URL);
    expect(planeLines(harness).map((line) => line.level)).toEqual(["warn", "warn"]);
    expect(planeLines(harness)[0]?.message).toContain(PEP_PLANE_ENV);
    expect(planeLines(harness)[1]?.message).toContain(LITELLM_PLANE_ENV);
  });

  it("are read one time, so a second pi process adds no line", async () => {
    const harness = open({ planeEnv: {} });
    harness.start();
    harness.hello();

    await spawnEnv(harness, "owui-first", 1);

    // A variable that changes after the first read changes no URL. `dispose`
    // puts the variable back.
    process.env[PEP_PLANE_ENV] = PEP;
    const second = await spawnEnv(harness, "owui-second", 2);

    expect(second["PEP_URL"]).toBe(PEP_URL);
    expect(planeLines(harness)).toHaveLength(2);
  });

  it("leave `ready` as the first line of the channel", async () => {
    // Contract 03 §3 rule 2. The host refuses a channel whose first line is
    // another line.
    const harness = open({ planeEnv: {} });
    harness.start();

    await until(() => harness.lines.length > 0, "ready");
    expect(harness.lines[0]?.type).toBe("ready");
    expect(planeLines(harness)).toEqual([]);

    harness.hello();
    await until(() => planeLines(harness).length === 2, "the two lines");
    expect(harness.lines[0]?.type).toBe("ready");
    expect(harness.exitCode).toBeNull();
  });
});

describe("the plane URLs of the terminal launcher", () => {
  const SESSION = "tui-01JBQ7WZ0X4T9V6K2H8M3N5PQR";
  const ATTACH = ["--sandbox", SANDBOX, "--session", SESSION];

  /** The environment that the env file carries, for one harness. */
  function mountEnv(harness: Harness): NodeJS.ProcessEnv {
    return {
      [CRED_DIR_ENV]: join(harness.root, "creds"),
      [FAMILY_CONFIG_DIR_ENV]: join(harness.root, "config"),
      [CONTROL_DIR_ENV]: join(harness.root, "control"),
      [SESSIONS_MOUNT_ENV]: join(harness.root, "sessions"),
    };
  }

  function launch(harness: Harness, planes: NodeJS.ProcessEnv): ReturnType<typeof planLaunch> {
    return planLaunch({
      argv: ATTACH,
      env: { ...mountEnv(harness), ...planes },
      bridgePath: harness.bridgePath,
    });
  }

  it("come from the two variables of the environment", async () => {
    const harness = open();
    const sessionDir = harness.sessionDir(SESSION);

    const plan = await launch(harness, { [PEP_PLANE_ENV]: PEP, [LITELLM_PLANE_ENV]: LITELLM });

    expect(plan.state).toBe(LaunchState.Ready);
    expect(plan.env["PEP_URL"]).toBe(PEP);
    expect(plan.env["LITELLM_BASE_URL"]).toBe(LITELLM);
    expect(modelsBaseUrl(sessionDir)).toBe(LITELLM);
  });

  it("are the URLs of the build when the env file carries neither variable", async () => {
    const harness = open();
    const sessionDir = harness.sessionDir(SESSION);

    const plan = await launch(harness, {});

    expect(plan.state).toBe(LaunchState.Ready);
    expect(plan.env["PEP_URL"]).toBe(PEP_URL);
    expect(plan.env["LITELLM_BASE_URL"]).toBe(LITELLM_BASE_URL);
    expect(modelsBaseUrl(sessionDir)).toBe(LITELLM_BASE_URL);
  });

  it("are the URLs of the build for a variable that is not valid", async () => {
    const harness = open();
    const sessionDir = harness.sessionDir(SESSION);

    const planes = { [PEP_PLANE_ENV]: NOT_VALID, [LITELLM_PLANE_ENV]: LITELLM };
    const plan = await launch(harness, planes);

    expect(plan.state).toBe(LaunchState.Ready);
    expect(plan.env["PEP_URL"]).toBe(PEP_URL);
    expect(plan.env["LITELLM_BASE_URL"]).toBe(LITELLM);
    expect(modelsBaseUrl(sessionDir)).toBe(LITELLM);

    const planned = JSON.stringify(plan);
    for (const part of NOT_VALID_PARTS) {
      expect(planned).not.toContain(part);
    }
  });

  it("reads the environment of its caller, not the environment of the process", async () => {
    // The harness sets the two variables of this process. The launcher must
    // not read them from there.
    const harness = open({ planeEnv: { [PEP_PLANE_ENV]: PEP, [LITELLM_PLANE_ENV]: LITELLM } });
    harness.sessionDir(SESSION);

    const plan = await launch(harness, {});

    expect(plan.env["PEP_URL"]).toBe(PEP_URL);
    expect(plan.env["LITELLM_BASE_URL"]).toBe(LITELLM_BASE_URL);
  });
});
