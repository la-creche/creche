// The tool env lists (decision CH49 rules 1 and 2). Each `ENV` name of a
// shipped playpen stage sits in exactly one list, and the agent copies only
// the `TOOL_ENV` and `TOOL_RENAME` names that are set.
//
// This test reads the `ENV` lines of `playpen/Dockerfile` only. The built
// env also holds the names of the parent image, such as `NODE_VERSION`,
// and this test does not read it.

import { readFileSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

import { FACADE_ENV, FLUE_ENV, readToolEnv, TOOL_ENV, TOOL_RENAME } from "../src/tool-env.js";

const ROOT = join(import.meta.dirname, "..");
const DOCKERFILE = join(ROOT, "..", "playpen", "Dockerfile");
const FLUE_DIST = join(ROOT, "node_modules", "@flue", "runtime", "dist", "node", "index.mjs");
const GUEST_VECTORS = join(ROOT, "..", "rust", "crates", "wire", "vectors", "guest.json");

/** The stages that ship, and the build-only stages of `bin/tests/test_playpen_image.py`. */
const SHIPPED = ["agent-sandbox", "agent-sandbox-python", "agent-sandbox-coding"];
const BUILD_ONLY = ["build", "creche-test-venv"];

/** `FROM [--flag=...] <parent> AS <stage>`, and `ENV <pairs>`. Docker reads both words in any case. */
const STAGE_RE = /^\s*FROM\s+(?:--\S+\s+)*(\S+)\s+AS\s+(\S+)\s*$/i;
const ENV_RE = /^\s*ENV\s+(.*)$/i;
const FROM_RE = /^\s*FROM\s/i;
const COMMENT_RE = /^\s*#/;
const CONTINUED_RE = /\\\s*$/;
const PAIR_RE = /([A-Za-z_][A-Za-z0-9_]*)=("(?:[^"\\]|\\.)*"|'[^']*'|\S*)\s*/y;

/** Each name of an `ENV` line, with its value as written: quotes stay. */
type EnvLine =
  | { readonly ok: true; readonly pairs: readonly (readonly [string, string])[] }
  | { readonly ok: false };

/** `ENV K=V` or `ENV K=V K2=V2`. The legacy `ENV K V` form is a refusal. */
function envPairs(rest: string): EnvLine {
  const pairs: [string, string][] = [];
  PAIR_RE.lastIndex = 0;
  while (PAIR_RE.lastIndex < rest.length) {
    const match = PAIR_RE.exec(rest);
    if (match?.[1] === undefined || match[2] === undefined) {
      return { ok: false };
    }

    pairs.push([match[1], match[2]]);
  }

  return pairs.length > 0 ? { ok: true, pairs } : { ok: false };
}

/**
 * Each instruction, with the number of its first line. Docker drops a
 * comment line before it joins a continued line, and never continues a
 * comment.
 */
function instructions(text: string): { readonly line: string; readonly number: number }[] {
  const out: { line: string; number: number }[] = [];
  let joined: { line: string; number: number } | null = null;
  for (const [index, raw] of text.split("\n").entries()) {
    if (COMMENT_RE.test(raw)) {
      continue;
    }

    joined ??= { line: "", number: index + 1 };
    if (CONTINUED_RE.test(raw)) {
      joined.line += raw.replace(CONTINUED_RE, " ");
      continue;
    }

    joined.line += raw;
    out.push(joined);
    joined = null;
  }

  return joined === null ? out : [...out, joined];
}

/** Each stage's `ENV` names and values, its parent's included when the parent is a stage. */
function stageEnv(text: string): Map<string, Map<string, string>> {
  const stages = new Map<string, Map<string, string>>();
  let current: Map<string, string> | null = null;
  for (const { line, number } of instructions(text)) {
    const from = STAGE_RE.exec(line);
    if (from?.[1] !== undefined && from[2] !== undefined) {
      current = new Map(stages.get(from[1]) ?? []);
      stages.set(from[2], current);
      continue;
    }

    if (FROM_RE.test(line)) {
      throw new Error(`Dockerfile line ${number}: name each stage, FROM <image> AS <stage>`);
    }

    const env = ENV_RE.exec(line);
    if (env?.[1] !== undefined && current !== null) {
      const read = envPairs(env[1].trim());
      if (!read.ok) {
        throw new Error(`Dockerfile line ${number}: write ENV K=V, not the legacy ENV K V form`);
      }

      read.pairs.forEach(([name, value]) => current?.set(name, value));
    }
  }

  return stages;
}

const LISTS: Readonly<Record<string, readonly string[]>> = {
  FLUE_ENV,
  TOOL_ENV,
  TOOL_RENAME: Object.keys(TOOL_RENAME),
  FACADE_ENV,
};

/** Each rename target that a second source also names, or that another list holds. */
function renameClashes(rename: Readonly<Record<string, readonly string[]>>): string[] {
  const others: readonly string[] = [...FLUE_ENV, ...TOOL_ENV, ...FACADE_ENV];
  const seen = new Set<string>();
  const clashes: string[] = [];
  for (const target of Object.values(rename).flat()) {
    if (seen.has(target) || others.includes(target)) {
      clashes.push(target);
    }

    seen.add(target);
  }

  return clashes;
}

describe("the tool env lists", () => {
  it("copies Flue's installed allowlist exactly", () => {
    const list = /const DEFAULT_LOCAL_ENV_ALLOWLIST = \[([^\]]*)\]/.exec(readFileSync(FLUE_DIST, "utf8"));

    expect(list?.[1]).toBeDefined();
    expect([...(list?.[1] ?? "").matchAll(/"([^"]+)"/g)].map((match) => match[1])).toEqual([...FLUE_ENV]);
  });

  it("are disjoint", () => {
    const all = Object.values(LISTS).flat();

    expect(new Set(all).size).toBe(all.length);
  });

  it("give each rename target one source, outside the other lists", () => {
    expect(renameClashes(TOOL_RENAME)).toEqual([]);
    expect(renameClashes({ A: ["HTTPS_PROXY", "PATH"], B: ["HTTPS_PROXY"], C: ["NODE_ENV"] })).toEqual([
      "PATH",
      "HTTPS_PROXY",
      "NODE_ENV",
    ]);
  });

  it("place each ENV name of a shipped stage in exactly one list", () => {
    const stages = stageEnv(readFileSync(DOCKERFILE, "utf8"));
    expect([...stages.keys()].sort()).toEqual([...SHIPPED, ...BUILD_ONLY].sort());

    const names = new Set(SHIPPED.flatMap((stage) => [...(stages.get(stage)?.keys() ?? [])]));
    expect([...names]).toEqual(
      expect.arrayContaining(["NODE_ENV", "HOME", "TMPDIR", "PATH", "MPLBACKEND", "MPLCONFIGDIR"]),
    );
    for (const name of names) {
      const homes = Object.keys(LISTS).filter((list) => LISTS[list]?.includes(name));
      expect(homes, name).toHaveLength(1);
    }
  });

  it("the coding flavor proxy names the guest egress port", () => {
    const coding = stageEnv(readFileSync(DOCKERFILE, "utf8")).get("agent-sandbox-coding");
    const port = (JSON.parse(readFileSync(GUEST_VECTORS, "utf8")) as Record<string, unknown>)["egress_proxy_port"];

    expect(typeof port).toBe("number");
    expect(coding?.get("CRECHE_TOOL_HTTPS_PROXY")).toBe(`http://127.0.0.1:${String(port)}`);
  });
});

describe("the Dockerfile ENV reader", () => {
  it("reads one pair, several pairs, a quoted value and a parent stage", () => {
    const text = 'FROM x AS a\nENV A=1\nFROM a AS b\nENV B="p q" C=3 \\\n  D=4\n';

    expect([...(stageEnv(text).get("b") ?? [])]).toEqual([
      ["A", "1"],
      ["B", '"p q"'],
      ["C", "3"],
      ["D", "4"],
    ]);
  });

  it("reads either case, a leading space, a tab and a FROM flag", () => {
    const text = "from --platform=linux/amd64 x as a\n  env A=1\nENV\tB=2\n";

    expect([...(stageEnv(text).get("a")?.keys() ?? [])]).toEqual(["A", "B"]);
  });

  it("drops a comment line before it joins a continued line", () => {
    const text = "FROM x AS a\n# a comment \\\nENV A=1\nRUN true \\\n  # inside\n  && true\nENV B=2\n";

    expect([...(stageEnv(text).get("a")?.keys() ?? [])]).toEqual(["A", "B"]);
  });

  it("refuses a FROM line with no stage name", () => {
    expect(() => stageEnv("FROM x AS a\nFROM scratch\nENV A=1\n")).toThrow("line 2: name each stage");
  });

  it("refuses the legacy ENV K V form", () => {
    expect(() => stageEnv("FROM x AS a\nENV A 1\n")).toThrow("line 2: write ENV K=V");
  });
});

describe("readToolEnv", () => {
  it("copies a set TOOL_ENV name and nothing else", () => {
    const env = { MPLBACKEND: "Agg", NODE_ENV: "production", MPLCONFIGDIR: "/c", AGENT_CRED_DIR: "/k" };

    expect(readToolEnv(env)).toEqual({ MPLBACKEND: "Agg" });
    expect(readToolEnv({ NODE_VERSION: "24" })).toEqual({});
  });

  it("copies a set rename source to each target", () => {
    const rename = { CRECHE_TOOL_HTTPS_PROXY: ["HTTPS_PROXY", "https_proxy"] };
    const env = { CRECHE_TOOL_HTTPS_PROXY: "http://10.0.0.1:3128" };

    expect(readToolEnv(env, TOOL_ENV, rename)).toEqual({
      HTTPS_PROXY: "http://10.0.0.1:3128",
      https_proxy: "http://10.0.0.1:3128",
    });
    expect(readToolEnv({}, TOOL_ENV, rename)).toEqual({});
  });

  it("the proxy rename gives tools exactly HTTPS_PROXY and https_proxy", () => {
    const proxy = "http://127.0.0.1:3128";
    const env = { CRECHE_TOOL_HTTPS_PROXY: proxy, HTTP_PROXY: proxy, NO_PROXY: "*", NODE_USE_ENV_PROXY: "1" };

    expect(TOOL_RENAME).toEqual({ CRECHE_TOOL_HTTPS_PROXY: ["HTTPS_PROXY", "https_proxy"] });
    expect(readToolEnv(env)).toEqual({ HTTPS_PROXY: proxy, https_proxy: proxy });
  });
});
