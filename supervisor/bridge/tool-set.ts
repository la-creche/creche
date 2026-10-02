// What this pi process offers right now, and what one new manifest does to
// it (contract 04 §4.2).
//
//   rev-a: search, ha_call          rev-b: search', enqueue_job
//                    │                       │
//                    └───────────────────────┘
//                                │
//        registerTool(enqueue_job)   the grant the operator ADDED
//        registerTool(search')       the same name, a new description
//        registerTool(ha_call, hidden)  the grant the operator REMOVED
//
// pi 0.99.1 fixes what is possible here, and `test/real-pi.test.ts` measures
// each of the three rather than assuming it:
//
//  1. An extension's tools are a map keyed by NAME
//     (`dist/core/extensions/loader.js`). Registering a name twice replaces
//     the definition, so a changed description or schema needs no removal.
//  2. Registering refreshes the tool set, and the refresh ACTIVATES a name
//     the registry did not hold before. A grant added mid-session is usable.
//  3. There is no unregister. Registering a name again with
//     `PiExposure.Hidden` takes it from the model and from every codemode
//     script, and leaves the definition in the registry. So "removed" means
//     "withdrawn", and this file never pretends otherwise. Registering it
//     again as `direct` gives it back.
//
// `setActiveTools` is not enough since codemode. It changes only what the
// model is declared, and pi 0.99.1 lets a script call a `codemode` or
// `deferred` tool while it is inactive. The exposure is the lever that holds.
//
// Rule 3 is also why the PEP still matters: between the grant moving and the
// next poll the tool is listed, and the call is denied at the PEP
// (contract 04 §1.5 rule 1). The manifest is safe in one direction only.

import type { Manifest, PepClient } from "./pep.js";
import { PiExposure } from "./pi-api.js";
import type { PiExtensionApi, PiToolDefinition } from "./pi-api.js";
import { manifestToTools } from "./tools.js";

/** What a withdrawn tool says, if pi ever shows it to anyone. */
const WITHDRAWN_DESCRIPTION = "Withdrawn: the grant for this tool was removed.";

const NO_PARAMETERS = { type: "object", properties: {}, additionalProperties: false } as const;

/** What one manifest did to the set this process offers. */
export interface ToolSetChange {
  /** How many PEP tools this process offers now. */
  readonly count: number;
  readonly added: readonly string[];
  readonly removed: readonly string[];
  /** Names that stayed, with a new description or a new schema. */
  readonly changed: readonly string[];
  /** Manifest entries whose names could not be registered (`tools.ts`). */
  readonly dropped: readonly string[];
}

/** How the current manifest offers one name (contract 04 §4 rule 3). */
export enum Offer {
  Absent = "absent",
  Open = "open",
  /** Granted, and every call waits for a phone tap. */
  Gated = "gated",
}

/** Whether a change moved anything a model could notice. */
export function movedAnything(change: ToolSetChange): boolean {
  return change.added.length + change.removed.length + change.changed.length > 0;
}

/**
 * What the model sees of one tool, as one string.
 *
 * The description, the schema and the exposure are the whole of it: the name
 * is the key and `execute` is this bridge's own closure. Key order inside a
 * schema is the PEP's, and the PEP builds each schema the same way every
 * time, so a reordering would at worst cost one needless re-registration.
 */
function shapeOf(tool: PiToolDefinition): string {
  return JSON.stringify([tool.description, tool.parameters, tool.exposure]);
}

/**
 * A removed grant, registered again so that nothing can reach it. Only the
 * name matters: a hidden tool is declared to nobody and callable by nobody.
 * The rejection is for a pi that ever broke that promise, and the PEP would
 * deny the call anyway.
 */
function withdrawn(name: string): PiToolDefinition {
  return {
    name,
    label: name,
    description: WITHDRAWN_DESCRIPTION,
    parameters: NO_PARAMETERS,
    exposure: PiExposure.Hidden,
    execute: () => Promise.reject(new Error(WITHDRAWN_DESCRIPTION)),
  };
}

/** The PEP tools one pi process holds, across every manifest it is served. */
export class OfferedTools {
  /** Tool name to `shapeOf` of the definition currently registered. */
  private shapes = new Map<string, string>();
  /** Tool name to its manifest `approval` flag, for the tools registered. */
  private gates = new Map<string, boolean>();
  private revision: string | null = null;

  /** The grants revision the current set was built from, or null. */
  public get rev(): string | null {
    return this.revision;
  }

  /** Whether any manifest has ever been applied to this process. */
  public get empty(): boolean {
    return this.revision === null;
  }

  /** How the manifest this process holds offers `name`. `index_search` asks about `embed`. */
  public offer(name: string): Offer {
    const gated = this.gates.get(name);
    if (gated === undefined) {
      return Offer.Absent;
    }

    return gated ? Offer.Gated : Offer.Open;
  }

  /**
   * Applies one manifest: register what is new or changed, withdraw what is
   * gone, leave the rest alone.
   *
   * Leaving the rest alone is not only thrift. Each `registerTool` makes pi
   * rebuild its tool registry and its system prompt, so re-registering an
   * unchanged set of 30 tools every minute would rewrite the prompt 30 times
   * for nothing.
   */
  public take(pi: PiExtensionApi, manifest: Manifest, pep: PepClient): ToolSetChange {
    const { tools, dropped } = manifestToTools(manifest, pep);

    const next = new Map<string, string>();
    for (const tool of tools) {
      next.set(tool.name, shapeOf(tool));
    }

    const added: string[] = [];
    const changed: string[] = [];
    for (const tool of tools) {
      const before = this.shapes.get(tool.name);
      if (before === undefined) {
        added.push(tool.name);
      } else if (before !== next.get(tool.name)) {
        changed.push(tool.name);
      } else {
        continue;
      }

      pi.registerTool(tool);
    }

    const removed = [...this.shapes.keys()].filter((name) => !next.has(name));
    for (const name of removed) {
      pi.registerTool(withdrawn(name));
    }

    this.shapes = next;
    this.gates = new Map(
      manifest.tools.filter((tool) => next.has(tool.name)).map((tool) => [tool.name, tool.approval]),
    );
    this.revision = manifest.rev;

    return { count: next.size, added, removed, changed, dropped };
  }
}
