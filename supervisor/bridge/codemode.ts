// Invariant 14 for pi's `codemode` tool.
//
// A codemode script calls PEP tools and returns whatever it chooses, and pi
// hands that output to the model as ONE tool result. Each PEP result reached
// the script as data (`tools.ts`, `structuredContent`), so no untrusted frame
// survives into the output on its own:
//
//   script ──► tools.kagi__search() ──► { text, json }   no frame: a script parses it
//     │
//     └──► "Script completed … {…}"  ──► this file  ──►  [untrusted output from "codemode" …]
//
// Framing the whole output is the one place the frame can go back on. It is
// framed whether or not a PEP tool ran: a script may also have read a file
// or run a command, and that output is no more trusted.

import type { PiToolResultChange, PiToolResultEvent } from "./pi-api.js";
import { wrapUntrusted } from "./untrusted.js";

/** pi 0.99.1's built-in orchestrating tool, loaded as `builtin:codemode`. */
export const CODEMODE_TOOL = "codemode";

/**
 * A `tool_result` handler: frames a codemode result's text, and leaves every
 * other tool's result alone.
 *
 * Text blocks are joined into one frame. Any other block, such as an image a
 * script forwarded with `image()`, follows the frame unchanged: it carries no
 * text a model could read as an instruction.
 */
export function frameCodemode(event: PiToolResultEvent): PiToolResultChange | undefined {
  if (event.toolName !== CODEMODE_TOOL) {
    return undefined;
  }

  const text = event.content
    .filter((block) => block.type === "text")
    .map((block) => block.text ?? "")
    .join("\n");
  const others = event.content.filter((block) => block.type !== "text");

  return { content: [{ type: "text", text: wrapUntrusted(CODEMODE_TOOL, text) }, ...others] };
}
