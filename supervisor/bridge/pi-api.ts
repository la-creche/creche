// The slice of pi's extension API this bridge uses, declared here rather than
// imported (pi 0.99.1, `docs/extensions.md` and
// `dist/core/extensions/types.d.ts`).
//
// Why a local declaration and not the real types:
//
//  1. `AGENTS.md` rule 17 keeps this package free of runtime dependencies.
//  2. `AGENTS.md` rule 18 keeps exactly ONE pi pin, in
//     `workbench/package.json`. Adding `@earendil-works/pi-coding-agent` here
//     for its types alone would create a second pin that can drift from the pi
//     the image actually installs.
//
// `pi.registerTool` takes a plain object. `defineTool` in pi's own API is a
// type-inference helper for standalone definitions, not a constructor, so
// nothing is lost by passing the literal directly.
//
// The image's build-time smoke check is what proves this shape still loads
// under the installed pi. See `Dockerfile`.

/** One text block of a tool result. pi also takes images; this bridge does not. */
export interface PiTextContent {
  readonly type: "text";
  readonly text: string;
}

/**
 * pi's `AgentToolResult`. `details` is required, not optional.
 *
 * `structuredContent` is what a codemode script receives instead of the text,
 * for a tool that declares an `outputSchema`. The model still reads `content`.
 */
export interface PiToolResult {
  readonly content: readonly PiTextContent[];
  readonly structuredContent?: unknown;
  readonly details: unknown;
}

/**
 * How the model reaches a tool (pi 0.99.1 `docs/extensions.md`, "Tool
 * exposure"). The bridge uses three of pi's five.
 */
export enum PiExposure {
  /** Declared to the model, and callable from codemode scripts. pi's default. */
  Direct = "direct",
  /** Declared to the model, and never callable from a script. */
  ModelOnly = "model-only",
  /** Registered, declared to nobody and callable by nobody. */
  Hidden = "hidden",
}

/** A group of tools, listed under one heading in the codemode description. */
export interface PiNamespace {
  readonly name: string;
}

export interface PiToolDefinition {
  readonly name: string;
  readonly label: string;
  readonly description: string;
  /**
   * pi validates arguments with TypeBox, and a TypeBox schema IS a plain JSON
   * Schema object, so the manifest's schema goes through unchanged.
   */
  readonly parameters: unknown;
  readonly exposure?: PiExposure;
  readonly namespace?: PiNamespace;
  /** The JSON Schema of `structuredContent`. */
  readonly outputSchema?: unknown;
  execute(toolCallId: string, params: unknown): Promise<PiToolResult>;
}

/** The slice of pi's `tool_result` event the bridge reads. */
export interface PiToolResultEvent {
  readonly toolName: string;
  readonly content: readonly { readonly type: string; readonly text?: string }[];
}

/** What a `tool_result` handler hands back: a replacement for `content`. */
export interface PiToolResultChange {
  readonly content: readonly unknown[];
}

/**
 * The methods this bridge calls. pi's real API has many more.
 *
 * pi 0.99.1 offers no way to UNREGISTER a tool. A grant the operator removes is
 * registered again with `PiExposure.Hidden`, which leaves the definition in
 * pi's registry and takes it from the model and from every codemode script.
 * Both are measured against the real pi in `test/real-pi.test.ts`.
 *
 * `on` is optional because a pi that lacks it must still load this bridge:
 * such a pi has no codemode whose output needs wrapping.
 */
export interface PiExtensionApi {
  registerTool(tool: PiToolDefinition): void;
  on?(
    event: "tool_result",
    handler: (event: PiToolResultEvent) => PiToolResultChange | undefined,
  ): void;
}
