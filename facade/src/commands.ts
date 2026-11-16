// The slash commands of a session: one command for each granted skill of
// the family. The name and the description are those of the skill's
// `SKILL.md`, as Flue holds them. A client sends a command as prompt text,
// `/<name> <input>`, and the agent's instructions tell it to activate that
// skill first (`agent/src/agent.ts`).
//
//   session/load   ──► replay ──► available_commands_update ──► answer
//   session/resume ──► available_commands_update ──► answer
//   session/prompt ──► pull ──► available_commands_update ──► the turn
//
// attendance sends the same list after its `session/new` answer, from the
// family config of the host (`attendance/src/attendance/commands.py`), so a
// new chat has the list before its VM boots.

import type { SkillDefinition } from "@flue/runtime";

import type { AvailableCommand, SessionUpdate } from "./acp.js";

/** The commands of the session's last pull. */
export type Commands = () => readonly AvailableCommand[];

/** One command for each skill, in the order of the names. */
export function commandsOf(skills: readonly SkillDefinition[]): AvailableCommand[] {
  const commands = skills.map(({ name, description }) => ({ name, description }));

  return commands.sort((one, other) => (one.name < other.name ? -1 : one.name > other.name ? 1 : 0));
}

/** The `available_commands_update` of `commands`. The client replaces its whole list with it. */
export function commandsUpdate(commands: Commands): SessionUpdate {
  return { sessionUpdate: "available_commands_update", availableCommands: [...commands()] };
}
