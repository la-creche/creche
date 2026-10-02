// Contract 03 §7.2. The `code-sandbox` family mounts ONE parent, the work
// root, and each job gets a link to the CALLING chat's directory beneath it:
//
//   /tmp/code-sandbox-<owner session>  ──►  <work root>/<owner session>
//
// The link is the turn's cwd, the supervisor derives its target from its own
// mount, and the link goes when the last process holding it ends. The
// directory itself belongs to the chat session and is the host's to delete.

import { afterEach, describe, expect, it } from "vitest";
import { existsSync, lstatSync, mkdtempSync, readlinkSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { CodeSandboxLinks, WorkspaceLinkError } from "../src/workspace.js";
import { Harness, until } from "./harness.js";

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

describe("the code-sandbox link on the turn path", () => {
  it("creates the link, runs the job in it, and removes it when the process ends", async () => {
    // `pi_idle_ttl_s` 0 is the thin default: one job, then the process goes.
    const owner = "owui-3f2a9c41";
    const harness = open();
    harness.start();
    harness.hello({ pi_idle_ttl_s: 0 });
    harness.startTurn("job-code", turnId(1), {
      workspace: { kind: "code-sandbox", owner_session: owner },
    });

    await until(() => harness.spawns.length === 1, "the spawn");
    const link = join(harness.root, `link-${owner}`);

    expect(harness.spawns[0]?.cwd).toBe(link);
    expect(readlinkSync(link)).toBe(join(harness.root, "code-sandbox", owner));

    await until(() => harness.of("process_exit").length === 1, "the reap");
    await until(() => !existsSync(link), "the link to go");

    // The work directory outlives the job. It belongs to the chat session.
    expect(existsSync(join(harness.root, "code-sandbox", owner))).toBe(true);
  });

  it("keeps the link while another job of the same chat still holds it", async () => {
    // Two jobs of one chat share one link, so the first to finish must not
    // pull the directory out from under the second.
    const owner = "owui-shared";
    const harness = open({ piEnv: { "job-slow": { FAKE_PI_DELAY_MS: "150" } } });
    harness.start();
    harness.hello({ pi_idle_ttl_s: 0 });

    const workspace = { kind: "code-sandbox", owner_session: owner };
    harness.startTurn("job-slow", turnId(1), { workspace });
    harness.startTurn("job-fast", turnId(2), { workspace });

    await until(() => harness.of("process_exit").length === 1, "the first job to end");
    const link = join(harness.root, `link-${owner}`);

    expect(harness.of("process_exit")[0]?.session).toBe("job-fast");
    expect(existsSync(link)).toBe(true);

    await until(() => harness.of("process_exit").length === 2, "the second job to end", 20000);
    await until(() => !existsSync(link), "the link to go");
  });

  it("refuses an owner_session that is not a session id", async () => {
    const harness = open();
    harness.start();
    harness.hello();
    harness.startTurn("job-bad", turnId(1), {
      workspace: { kind: "code-sandbox", owner_session: "../../etc" },
    });

    await until(() => harness.of("turn_failed").length === 1, "the refusal");
    expect(harness.spawns).toHaveLength(0);
  });
});

describe("the link builder itself", () => {
  const roots: string[] = [];

  /** One temp root, and a builder rooted in it. `work` is the mounted parent. */
  function links(work = "work"): CodeSandboxLinks {
    const root = roots[roots.length - 1] ?? "";

    return new CodeSandboxLinks(join(root, work), join(root, "link-"));
  }

  function freshRoot(): void {
    roots.push(mkdtempSync(join(tmpdir(), "agent-supervisor-links-")));
  }

  afterEach(() => {
    while (roots.length > 0) {
      rmSync(roots.pop() ?? "", { recursive: true, force: true });
    }
  });

  it("refuses a hostile owner session before it becomes a path", () => {
    // §7.2 rule 1. `validate.ts` holds the same check at the channel's door.
    // This one sits beside the join, which is the check a later reader sees.
    freshRoot();
    const under = links();

    for (const value of ["..", ".", "../escape", "a/b", "", "-leading", "x\0y"]) {
      expect(under.pathFor(value)).toBeNull();
      expect(() => under.hold("job-1", value)).toThrow(WorkspaceLinkError);
    }
  });

  it("accepts a link a racing job of the same chat already made", () => {
    freshRoot();
    const under = links();
    const first = under.hold("job-1", "owui-race");
    const second = under.hold("job-2", "owui-race");

    expect(second).toBe(first);
    expect(lstatSync(first).isSymbolicLink()).toBe(true);
  });

  it("refuses a link that already points somewhere else", () => {
    // Two work roots, one link name. The name collides and the target does
    // not, which is the only case §7.2 rule 2 cannot just accept.
    freshRoot();
    links("work-a").hold("job-1", "owui-clash");

    expect(() => links("work-b").hold("job-2", "owui-clash")).toThrow(WorkspaceLinkError);
  });

  it("leaves a link no session of its own is holding", () => {
    freshRoot();
    const under = links();
    under.hold("job-1", "owui-keep");
    under.release("job-unknown");

    expect(existsSync(under.pathFor("owui-keep") ?? "")).toBe(true);
  });
});
