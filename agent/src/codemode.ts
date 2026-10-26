// `codemode`, one of the two tools of the agent outside `mcp__ch__`. The
// model writes one JavaScript script. pi's `CodemodeSandbox` runs it in a
// QuickJS VM in a worker thread, where the only capability is a call to a
// `ch` tool. The nested calls do not enter the model's context. Only the
// script's output and its return value do.
//
//   script ──► CodemodeSandbox ──► tools.<upstream>__<tool>(args)
//                                    └──► the chaperone, `ch`, family bearer
//          ◄── [untrusted output from "codemode" ...] the whole output
//
// The chaperone decides each nested call, as it decides a direct one. A
// gated call rejects inside the script with the "Awaiting permission" text.
//
// Flue's `createMcpConnection` cannot serve here: the `run` of each tool
// it adapts throws ("MCP tools execute through the internal adapter"), and
// the adapter that runs the call is internal. So each script opens its own
// MCP client on the same definition as `ch`, Flue's own client package at
// Flue's own pin. A new connection per script lists the tools of the grant
// at that time.

import { CodemodeSandbox } from "@earendil-works/pi-codemode";
import type { CodemodeJsonSchema, CodemodeResult, CodemodeTool } from "@earendil-works/pi-codemode";
import { defineTool } from "@flue/runtime";
import type { McpConnectionDefinition, ToolDefinition } from "@flue/runtime";
import { Client, StreamableHTTPClientTransport } from "@modelcontextprotocol/client";
import * as v from "valibot";

import { wrapUntrusted } from "./untrusted.js";

export const CODEMODE_TOOL = "codemode";

/** The deadline of one script, as pi's coding agent sets it. */
const SCRIPT_TIMEOUT_MS = 60000;

/** The longest script the tool takes. */
const MAX_CODE_CHARS = 65536;

const CLIENT_INFO = { name: "agent-codemode", version: "0.0.0" };

const INPUT = v.strictObject({
  code: v.pipe(
    v.string(),
    v.minLength(1),
    v.maxLength(MAX_CODE_CHARS),
    v.description("The body of an async JavaScript function."),
  ),
});

const DESCRIPTION =
  "Run one JavaScript script that calls tools as `await tools.<name>(args)`. " +
  "Each mcp__ch__<name> tool is tools.<name> here, with the same arguments. " +
  "`ALL_TOOLS` lists them. Write output with text(value) or return a value. " +
  "The script has no file, network, timer or module access. " +
  "Use it to chain several calls, or to filter a large result, before it reaches you.";

interface McpCallResult {
  readonly content?: readonly { readonly type: string; readonly text?: string }[];
  readonly isError?: boolean;
}

/** The text of an MCP result. A block that is not text is a placeholder. */
function resultText(result: McpCallResult): string {
  const parts = (result.content ?? []).map((block) =>
    block.type === "text" && typeof block.text === "string" ? block.text : `[${block.type} omitted]`,
  );

  return parts.join("\n\n");
}

/** `auth` of the `ch` definition. The unwrap stays in `chaperone.ts`. */
function tokenOf(definition: McpConnectionDefinition): () => Promise<string> {
  const auth = definition.auth;

  return async () => (typeof auth === "function" ? auth() : (auth ?? ""));
}

interface ChSession {
  readonly tools: readonly CodemodeTool[];
  close(): Promise<void>;
}

/** One MCP client on the `ch` definition, and its tools as codemode tools. */
async function openCh(definition: McpConnectionDefinition): Promise<ChSession> {
  const transport = new StreamableHTTPClientTransport(new URL(String(definition.url)), {
    authProvider: { token: tokenOf(definition) },
  });
  const client = new Client(CLIENT_INFO);
  try {
    await client.connect(transport);
    const listed = [];
    let page = await client.listTools();
    listed.push(...page.tools);
    const cursors = new Set<string>();
    while (page.nextCursor !== undefined && !cursors.has(page.nextCursor)) {
      cursors.add(page.nextCursor);
      page = await client.listTools({ cursor: page.nextCursor });
      listed.push(...page.tools);
    }

    const tools = listed.map(
      (tool): CodemodeTool => ({
        name: tool.name,
        description: tool.description ?? "",
        inputSchema: tool.inputSchema as CodemodeJsonSchema,
        execute: async (args, { signal }) => {
          const result = (await client.callTool(
            { name: tool.name, arguments: args as Record<string, unknown> },
            { signal },
          )) as McpCallResult;
          const text = resultText(result);
          if (result.isError === true) {
            throw new Error(text);
          }

          return text;
        },
      }),
    );

    return { tools, close: () => client.close() };
  } catch (error) {
    await client.close().catch(() => undefined);
    throw error;
  }
}

/** Every item of the output, then the value or the error, as one text. */
function render(result: CodemodeResult): string {
  const lines = result.output.map((item) => (item.type === "text" ? item.text : "[image omitted]"));
  if (result.ok) {
    if (result.value !== undefined) {
      lines.push(JSON.stringify(result.value) ?? "");
    }
  } else {
    lines.push(`script failed (${result.error.kind}): ${result.error.message}`);
  }

  return lines.join("\n");
}

/**
 * The Flue tool. `ch` is the definition of `chaperoneConnection`. A script
 * that fails throws, so the model gets a tool error. Each way, the whole
 * output is inside the untrusted frame.
 */
export function codemodeTool(ch: McpConnectionDefinition): ToolDefinition {
  return defineTool({
    name: CODEMODE_TOOL,
    description: DESCRIPTION,
    input: INPUT,
    run: async ({ data, signal }) => {
      const session = await openCh(ch);
      const sandbox = new CodemodeSandbox({ tools: [...session.tools], timeoutMs: SCRIPT_TIMEOUT_MS });
      try {
        const result = await sandbox.execute(data.code, signal === undefined ? {} : { signal });
        const framed = wrapUntrusted(CODEMODE_TOOL, render(result));
        if (!result.ok) {
          throw new Error(framed);
        }

        return framed;
      } finally {
        await sandbox.close();
        await session.close();
      }
    },
  });
}
