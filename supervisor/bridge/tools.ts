// Contract 04 §4 read as pi tools. Tool definitions exist in exactly one
// place, the PEP's manifest, and this file only presents them:
//
//   GET /manifest ──► one pi tool per entry ──► execute() = POST /call
//
// The bridge adds no capability of its own. Every execute() is one PEP call,
// every result the model reads comes back inside an untrusted block
// (invariant 14), and a denial reaches the model as a plain tool error rather
// than as something to loop on. A codemode script gets the same result as
// data, and `codemode.ts` frames what the script hands the model.

import {
  MAX_DESCRIPTION_LENGTH,
  MAX_DETAIL_LENGTH,
  MAX_TOOL_NAME_LENGTH,
  NAMESPACE_SEPARATOR,
  SEARCH_TOOL,
  TOOL_NAME_RE,
} from "./constants.js";
import { CallFailure, PepCallError } from "./pep.js";
import type { Manifest, ManifestTool, PepClient } from "./pep.js";
import { PiExposure } from "./pi-api.js";
import type { PiNamespace, PiToolDefinition, PiToolResult } from "./pi-api.js";
import { wrapUntrusted } from "./untrusted.js";

/** Contract 04 §4 rule 3. The agent can say a tap is coming before it blocks. */
const APPROVAL_NOTE = " This action waits for a phone approval before it runs.";

/** Said once, in the tool error, so the model reports instead of retrying. */
const FIXED_LIMIT = " This limit is fixed. Do not retry the same call.";

/**
 * Contract 04 §5 row 4. The one denial that is about the TOOL and not the
 * call, so it gets a sentence of its own.
 *
 * The manifest is a snapshot the bridge refreshes on a poll (§4.2), so a
 * grant the operator removed leaves the tool listed for up to one interval, and the
 * call inside that window is denied here. `FIXED_LIMIT` would send the model
 * looking for a smaller request, and no request is small enough. The hedge is
 * real: this reason also answers a name that was never granted at all.
 */
const NOT_GRANTED_REASON = "tool_not_granted";
const NOT_GRANTED =
  " This tool is not granted to this agent. The grant may have been removed." +
  " Do not retry it. Say in your answer that you could not use it.";

/**
 * The same kind of sentence, for the case a denial is not.
 *
 * A denial is about one call, so "do not retry the same call" is the whole
 * instruction. An unreachable PEP is about every call: contract 05 §3.3's
 * `pep_unreachable` needs 90 seconds of silence to be raised at all, and an
 * outage can last far longer. Without this sentence the model
 * reads "the call did not run" and tries again, which is what §3.3 rule 6
 * measured: the turn runs to the end and LiteLLM bills it.
 */
const DOOR_SHUT =
  " The policy service is away, so no tool call works right now." +
  " Do not retry. Say in your answer that you could not act.";

/**
 * What the model reads when the PEP answers 202.
 *
 * Contract 04 §5.1: a 202 is never an empty success. §8.1 says the PEP blocks
 * in place and sends no 202 at all, so this text only makes one legible to
 * the model rather than silent.
 */
const APPROVAL_SEAM =
  "This action needs a phone approval, and the PEP answered 202 instead of " +
  "holding the call. The call did not run.";

/** Every detail the PEP sends is flattened to one line before the model reads it. */
function flatten(detail: string | null): string {
  if (detail === null) {
    return "";
  }

  const oneLine = detail.replace(/[\x00-\x1f\x7f]+/g, " ").trim();

  return oneLine.length === 0 ? "" : `: ${oneLine.slice(0, MAX_DETAIL_LENGTH)}`;
}

/** Contract 04 §5. The model gets the boundary, never a reason to try again. */
function errorText(error: PepCallError): string {
  const detail = flatten(error.detail);
  if (error.failure === CallFailure.Approval) {
    return APPROVAL_SEAM;
  }
  if (error.failure === CallFailure.Unreachable) {
    return `The PEP could not be reached${detail}. The call did not run.${DOOR_SHUT}`;
  }
  if (error.failure === CallFailure.Failed) {
    return `The call failed at the enforcement point (${error.reason})${detail}. It may not have run.`;
  }
  if (error.reason === NOT_GRANTED_REASON) {
    return `Tool call denied by policy (${error.reason})${detail}.${NOT_GRANTED}`;
  }

  return `Tool call denied by policy (${error.reason})${detail}.${FIXED_LIMIT}`;
}

/** Plain text out of whatever shape a result takes. §7.6's wrapper names its family. */
function contentOf(result: unknown): string {
  if (typeof result === "string") {
    return result;
  }

  if (typeof result === "object" && result !== null && !Array.isArray(result)) {
    const raw = result as Record<string, unknown>;
    const source = raw["source"];
    const content = raw["content"];
    if (raw["untrusted"] === true && typeof content === "string") {
      const from = typeof source === "string" ? source : "unknown";

      return `(answered by ${from})\n${content}`;
    }
  }

  return JSON.stringify(result) ?? "null";
}

/**
 * Whether this entry's name can be registered and quoted safely.
 *
 * `SEARCH_TOOL` is the bridge's own (contract 03 §7.3 rule 6). pi keys tools
 * by name, so an entry under it would replace the search with a PEP call.
 */
function usable(tool: ManifestTool): boolean {
  return (
    tool.name.length <= MAX_TOOL_NAME_LENGTH && TOOL_NAME_RE.test(tool.name) && tool.name !== SEARCH_TOOL
  );
}

function describe(tool: ManifestTool): string {
  const text = tool.description.slice(0, MAX_DESCRIPTION_LENGTH);

  return tool.approval ? `${text}${APPROVAL_NOTE}` : text;
}

/**
 * A gated tool stays out of codemode scripts. A script could await one call
 * per list item, and each would hold the script for its own phone tap. The
 * model still calls it directly, one tap at a time, as it did before.
 */
function exposureOf(tool: ManifestTool): PiExposure {
  return tool.approval ? PiExposure.ModelOnly : PiExposure.Direct;
}

/**
 * The server part of `<server>__<tool>` (contract 04 §4), so codemode lists a
 * server's tools under one heading. A verb such as `enqueue` has none.
 */
function namespaceOf(tool: ManifestTool): PiNamespace | undefined {
  const at = tool.name.indexOf(NAMESPACE_SEPARATOR);

  return at > 0 ? { name: tool.name.slice(0, at) } : undefined;
}

/**
 * What a codemode script receives, where the model reads the wrapped text.
 *
 *   PEP result {"text": "{\"items\":[1]}"}  ──►  { text: "{\"items\":[1]}", json: { items: [1] } }
 *
 * No untrusted frame goes around it: a script cannot parse a framed string,
 * and `codemode.ts` frames the script's whole output before the model reads
 * it (invariant 14).
 */
const RESULT_SCHEMA = {
  type: "object",
  properties: {
    text: { type: "string", description: "The tool's output." },
    json: { description: "`text` parsed, when it is JSON." },
  },
  required: ["text"],
} as const;

/** An MCP tool's own text, or the text the model reads for any other shape. */
function rawText(result: unknown): string {
  if (typeof result === "object" && result !== null && !Array.isArray(result)) {
    const text = (result as Record<string, unknown>)["text"];
    if (typeof text === "string") {
      return text;
    }
  }

  return contentOf(result);
}

function structured(result: unknown): { readonly text: string; readonly json?: unknown } {
  const text = rawText(result);
  try {
    return { text, json: JSON.parse(text) as unknown };
  } catch {
    return { text };
  }
}

function define(tool: ManifestTool, pep: PepClient): PiToolDefinition {
  const namespace = namespaceOf(tool);

  return {
    name: tool.name,
    label: tool.name,
    description: describe(tool),
    parameters: tool.schema,
    exposure: exposureOf(tool),
    ...(namespace === undefined ? {} : { namespace }),
    outputSchema: RESULT_SCHEMA,
    execute: async (_toolCallId: string, params: unknown): Promise<PiToolResult> => {
      const args = typeof params === "object" && params !== null ? params : {};
      try {
        const result = await pep.call(tool.name, args as Record<string, unknown>);

        return {
          content: [{ type: "text", text: wrapUntrusted(tool.name, contentOf(result)) }],
          structuredContent: structured(result),
          details: result,
        };
      } catch (error) {
        if (error instanceof PepCallError) {
          throw new Error(errorText(error));
        }

        throw error;
      }
    },
  };
}

/**
 * One pi tool per manifest entry, named exactly as the manifest names it.
 *
 * An entry whose name would not survive being quoted inside an untrusted block
 * is dropped rather than renamed. Renaming would break the one rule that makes
 * this bridge safe to reason about: the name the model calls is the name the
 * PEP decides on.
 */
export function manifestToTools(
  manifest: Manifest,
  pep: PepClient,
): { readonly tools: readonly PiToolDefinition[]; readonly dropped: readonly string[] } {
  const tools: PiToolDefinition[] = [];
  const dropped: string[] = [];

  for (const tool of manifest.tools) {
    if (!usable(tool)) {
      dropped.push(tool.name.slice(0, MAX_TOOL_NAME_LENGTH));
      continue;
    }

    tools.push(define(tool, pep));
  }

  return { tools, dropped };
}
