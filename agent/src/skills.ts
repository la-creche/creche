// The family's granted skills, from the read-only config mount:
//
//   <config dir>/skills/<name>/SKILL.md      frontmatter name, description; then the body
//   <config dir>/skills/<name>/<file>        supporting files, served by Flue
//
// Each one becomes a `defineSkill` value at process start. A skill that is
// not valid is skipped and named, never mounted half. `caregiver` writes
// the mount, and the agent cannot write it.

import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative, sep } from "node:path";

import { defineSkill } from "@flue/runtime";
import type { SkillDefinition } from "@flue/runtime";

const SKILL_FILE = "SKILL.md";

/** Bounds on one skill. The mount is trusted, and a bound still costs nothing. */
const MAX_FILES = 64;
const MAX_FILE_BYTES = 1048576;

/** `---` on its own line opens and closes the frontmatter. */
const FRONTMATTER_RE = /^---\r?\n([\s\S]*?)\r?\n---\r?\n?([\s\S]*)$/;
const FIELD_RE = /^([a-z][a-z-]*):[ \t]*(.*)$/;

export interface SkillsRead {
  readonly skills: readonly SkillDefinition[];
  readonly skipped: readonly string[];
}

/** A one-line value, without the quotes of a YAML scalar. */
function scalar(raw: string): string {
  const text = raw.trim();
  const quoted = text.length >= 2 && (text[0] === '"' || text[0] === "'") && text.at(-1) === text[0];

  return quoted ? text.slice(1, -1) : text;
}

/** The one-line fields of the frontmatter, and the body. */
function splitSkill(text: string): { readonly fields: Map<string, string>; readonly body: string } | null {
  const match = FRONTMATTER_RE.exec(text);
  if (match === null) {
    return null;
  }

  const fields = new Map<string, string>();
  for (const line of (match[1] ?? "").split(/\r?\n/)) {
    const field = FIELD_RE.exec(line);
    if (field !== null) {
      fields.set(field[1] ?? "", scalar(field[2] ?? ""));
    }
  }

  return { fields, body: (match[2] ?? "").trim() };
}

/** Every file under `dir` but `SKILL.md`, by its path relative to `dir`. */
function supportingFiles(dir: string): Record<string, Uint8Array> | null {
  const files: Record<string, Uint8Array> = {};
  const pending = [dir];
  let count = 0;
  for (let next = pending.pop(); next !== undefined; next = pending.pop()) {
    for (const entry of readdirSync(next, { withFileTypes: true })) {
      const path = join(next, entry.name);
      if (entry.isDirectory()) {
        pending.push(path);
        continue;
      }

      const name = relative(dir, path).split(sep).join("/");
      if (!entry.isFile() || name === SKILL_FILE) {
        continue;
      }

      count += 1;
      if (count > MAX_FILES || statSync(path).size > MAX_FILE_BYTES) {
        return null;
      }

      files[name] = readFileSync(path);
    }
  }

  return files;
}

function readSkill(dir: string, name: string): SkillDefinition | null {
  try {
    const split = splitSkill(readFileSync(join(dir, SKILL_FILE), "utf8"));
    const files = supportingFiles(dir);
    if (split === null || files === null) {
      return null;
    }

    return defineSkill({
      name: split.fields.get("name") ?? name,
      description: split.fields.get("description") ?? "",
      instructions: split.body,
      ...(Object.keys(files).length > 0 ? { files } : {}),
    });
  } catch {
    return null;
  }
}

/** Null `skillsPath`: the family mounts no skills. */
export function readSkills(skillsPath: string | null): SkillsRead {
  if (skillsPath === null) {
    return { skills: [], skipped: [] };
  }

  let names: string[];
  try {
    const entries = readdirSync(skillsPath, { withFileTypes: true });
    names = entries.filter((entry) => entry.isDirectory()).map((entry) => entry.name);
  } catch {
    return { skills: [], skipped: [] };
  }

  const skills: SkillDefinition[] = [];
  const skipped: string[] = [];
  for (const name of names.sort()) {
    const skill = readSkill(join(skillsPath, name), name);
    if (skill === null) {
      skipped.push(name);
      continue;
    }

    skills.push(skill);
  }

  return { skills, skipped };
}
