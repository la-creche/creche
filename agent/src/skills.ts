// The family's granted skills. Each pull of the family state carries the
// text of each `SKILL.md` (`FamilySkills`):
//
//   ---
//   name: <name>
//   description: <one line>
//   ---
//   <the body>
//
// Each text becomes one `defineSkill` value, with no supporting files. A
// skill that is not valid is skipped and named. A skill whose text does not
// change keeps its value (`src/family-state.ts`).

import { defineSkill } from "@flue/runtime";
import type { SkillDefinition } from "@flue/runtime";

/** `---` on its own line opens and closes the frontmatter. */
const FRONTMATTER_RE = /^---\r?\n([\s\S]*?)\r?\n---\r?\n?([\s\S]*)$/;
const FIELD_RE = /^([a-z][a-z-]*):[ \t]*(.*)$/;

/** One skill: its name in the pull, its `SKILL.md` text and its `defineSkill` value. */
export interface LiveSkill {
  readonly name: string;
  readonly text: string;
  readonly definition: SkillDefinition;
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

/**
 * One skill from the text of its `SKILL.md` alone, as the family state
 * carries it (`FamilySkills`). Null: the text is not a valid skill.
 */
export function skillOf(name: string, text: string): LiveSkill | null {
  const split = splitSkill(text);
  if (split === null) {
    return null;
  }

  try {
    const definition = defineSkill({
      name: split.fields.get("name") ?? name,
      description: split.fields.get("description") ?? "",
      instructions: split.body,
    });

    return { name, text, definition };
  } catch {
    return null;
  }
}
