// The one door of the agent to every granted tool: the chaperone's front
// door, MCP streamable HTTP, as the connection `ch`. The chaperone names an
// upstream tool `<upstream>__<tool>`, and Flue prefixes `mcp__ch__`, so the
// model calls `mcp__ch__<upstream>__<tool>`.
//
//   FamilyConfig.planes.pep ──► <pep>/mcp     (correction 11 of the plan)
//   FamilyConfig.creds.pepToken ──► Authorization: Bearer, per request
//
// The chaperone decides each call. A gated call does not wait: it answers at
// once with an MCP error result whose text starts "Awaiting permission".
// Flue gives that result to the model as a tool error. An identical call
// after the approval collects the kept result.

import { defineMcpConnection } from "@flue/runtime";
import type { McpConnectionDefinition } from "@flue/runtime";

import type { FamilyConfig } from "./config.js";
import { revealSecret } from "./secret.js";

/** The connection name, and so the `mcp__ch__` namespace of every tool. */
export const CH_CONNECTION = "ch";

/** The path of the chaperone's front door, after the plane URL. */
const MCP_PATH = "/mcp";

/**
 * The definition that `useMcpConnection` and `createMcpConnection` take.
 * `auth` is a function, so the frozen definition holds no credential text.
 * Flue calls it before each request.
 */
export function chaperoneConnection(config: FamilyConfig): McpConnectionDefinition {
  const token = config.creds.pepToken;

  return defineMcpConnection({
    name: CH_CONNECTION,
    url: `${config.planes.pep.text}${MCP_PATH}`,
    auth: () => revealSecret(token),
  });
}
