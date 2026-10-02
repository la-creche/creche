// Drives the REAL PEP bridge bundle against the REAL PEP.
//
//   node bridge_driver.mjs <pep-bridge.js> <report.json> [<tool> <args json>]
//
// `supervisor/test/bridge.test.ts` drives the bridge the same way: build a
// tiny pi that collects `registerTool`, call the default export, then execute
// one registered tool. The only difference is the far side. There it is
// `test/fake-pep.ts`, which has no grant file, no token check and no audit.
// Here it is the PEP itself, reading the grant file `managerd` wrote.
//
// The environment carries what the supervisor would set (contract 03 §7):
// PEP_URL, PEP_TOKEN, AGENT_SESSION, AGENT_TURN and AGENT_TURN_FILE.
//
// This file never writes to stdout except the report path, because the report
// is what the Python side reads. Anything the bridge itself notes goes to
// stderr, which is where the bridge writes by design.

import { writeFileSync } from "node:fs";

const [bundlePath, reportPath, toolName, argsJson] = process.argv.slice(2);

if (bundlePath === undefined || reportPath === undefined) {
  process.stderr.write("usage: bridge_driver.mjs <bundle> <report> [tool] [args]\n");
  process.exit(2);
}

/** The one method the bridge calls on pi (`bridge/pi-api.ts`). */
class CollectingPi {
  constructor() {
    this.tools = [];
  }

  registerTool(tool) {
    this.tools.push(tool);
  }

  named(name) {
    return this.tools.find((tool) => tool.name === name);
  }
}

/** A tool result is always one untrusted block; join its parts. */
function resultText(result) {
  if (result === undefined || result === null || !Array.isArray(result.content)) {
    return "";
  }

  return result.content.map((part) => part.text ?? "").join("");
}

/**
 * One tool call. A denial arrives as a thrown Error carrying the text the
 * model would read (`bridge/tools.ts`), so it is reported, not raised.
 */
async function runOne(pi, toolName, argsJson) {
  const tool = pi.named(toolName);
  if (tool === undefined) {
    return { ok: false, missing: true, text: "", error: "" };
  }

  const args = argsJson === undefined ? {} : JSON.parse(argsJson);
  try {
    const result = await tool.execute("call-1", args);

    return { ok: true, missing: false, text: resultText(result), error: "" };
  } catch (error) {
    return { ok: false, missing: false, text: "", error: error?.message ?? String(error) };
  }
}

async function main() {
  const bridge = (await import(bundlePath)).default;
  const pi = new CollectingPi();

  await bridge(pi);

  const report = {
    tools: pi.tools.map((tool) => ({
      name: tool.name,
      description: tool.description,
      parameters: tool.parameters,
    })),
    call: null,
  };

  if (toolName !== undefined) {
    report.call = await runOne(pi, toolName, argsJson);
  }

  writeFileSync(reportPath, `${JSON.stringify(report, null, 2)}\n`);
}

main().catch((error) => {
  process.stderr.write(`bridge_driver: ${error?.stack ?? String(error)}\n`);
  process.exit(1);
});
