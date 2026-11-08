// The one door of the agent to every granted tool: the chaperone's front
// door, MCP streamable HTTP, as the connection `ch`. The chaperone names an
// upstream tool `<upstream>__<tool>`, and Flue prefixes `mcp__ch__`, so the
// model calls `mcp__ch__<upstream>__<tool>`.
//
//   planes.pep ──► <pep>/mcp     (correction 11 of the plan). sbx:
//                  `AGENT_PEP_URL`. CH: guest 127.0.0.1:8300
//   creds().pepToken ──► Authorization: Bearer, per request. The facade
//                        pulls the creds before each prompt (decision CH5a).
//
// The chaperone decides each call. A gated call does not wait: it answers at
// once with an MCP error result whose text starts "Awaiting permission".
// Flue gives that result to the model as a tool error. An identical call
// after the approval collects the kept result.

import { defineMcpConnection } from "@flue/runtime";
import type { McpConnectionDefinition } from "@flue/runtime";

import type { FamilyConfig } from "./config.js";
import type { LiveCreds } from "./family-state.js";
import { revealSecret } from "./secret.js";

/** The connection name, and so the `mcp__ch__` namespace of every tool. */
export const CH_CONNECTION = "ch";

/** The path of the chaperone's front door, after the plane URL. */
const MCP_PATH = "/mcp";

/**
 * The definition that `useMcpConnection` and `createMcpConnection` take.
 * `auth` is a function, so the frozen definition holds no credential text.
 * Flue calls it before each request, so a rotated token reaches the next
 * request. `creds` gives the current creds. The default is the start creds.
 */
export function chaperoneConnection(
  config: Pick<FamilyConfig, "planes" | "creds">,
  creds: () => LiveCreds = () => config.creds,
): McpConnectionDefinition {
  return defineMcpConnection({
    name: CH_CONNECTION,
    url: `${config.planes.pep.text}${MCP_PATH}`,
    auth: () => revealSecret(creds().pepToken),
  });
}
