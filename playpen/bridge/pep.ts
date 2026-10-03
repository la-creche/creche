// The PEP is the action plane's only door (invariant 12). This client speaks
// the two paths contract 04 gives a sandbox and nothing else:
//
//   GET  /manifest   what this family may do, at process start and then on a
//                    conditional poll for the life of the process (§4, §4.2)
//   POST /call       one action, decided outside the sandbox (§5, §7.1)
//
// It holds no policy. Every decision is the PEP's, and a denial comes back as
// an ordinary answer, never as something to retry around.

import {
  CALL_PATH,
  CALL_TIMEOUT_MS,
  DELEGATION_HEADER,
  HTTP_ACCEPTED,
  HTTP_NOT_MODIFIED,
  HTTP_OK,
  HTTP_SERVER_ERROR,
  IF_NONE_MATCH_HEADER,
  MANIFEST_PATH,
  MANIFEST_TIMEOUT_MS,
  MAX_MANIFEST_BYTES,
  MAX_TOOLS,
  REV_HEADER_RE,
  SESSION_HEADER,
  TURN_HEADER,
} from "./constants.js";
import type { TurnContext } from "./turn-context.js";

/** Contract 04 §4. One entry of the manifest's `tools` list. */
export interface ManifestTool {
  readonly name: string;
  readonly description: string;
  readonly schema: Record<string, unknown>;
  readonly approval: boolean;
}

export interface Manifest {
  readonly family: string;
  readonly rev: string;
  readonly tools: readonly ManifestTool[];
}

/** How a manifest fetch ended. Contract 04 §4 and §4.2. */
export enum ManifestState {
  /** The PEP served a manifest. */
  Fresh = "fresh",
  /** 304: the revision the caller named is still the one being served. */
  Unchanged = "unchanged",
}

export type ManifestAnswer =
  | { readonly state: ManifestState.Fresh; readonly manifest: Manifest }
  | { readonly state: ManifestState.Unchanged };

/** How a call ended, when it did not end with a result. */
export enum CallFailure {
  /** Contract 04 §5 rows 1 to 9. The PEP said no. */
  Denied = "denied",
  /** Contract 04 §5 rows 11 and 12. An allowed call that did not complete. */
  Failed = "failed",
  /** A 202, which contract 04 §8.1 says the PEP never sends: a gate blocks in place. */
  Approval = "approval",
  /** The PEP could not be reached at all. */
  Unreachable = "unreachable",
}

export class PepCallError extends Error {
  public constructor(
    public readonly failure: CallFailure,
    public readonly status: number,
    public readonly reason: string,
    public readonly detail: string | null,
  ) {
    super(`PEP call ended ${failure} (${status} ${reason})`);
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function text(raw: Record<string, unknown>, key: string): string | null {
  const value = raw[key];

  return typeof value === "string" ? value : null;
}

/** Contract 04 §4's tool entry, or null when the PEP sent something else. */
function readTool(value: unknown): ManifestTool | null {
  if (!isRecord(value)) {
    return null;
  }

  const name = text(value, "name");
  const schema = value["schema"];
  if (name === null || !isRecord(schema)) {
    return null;
  }

  return {
    name,
    description: text(value, "description") ?? "",
    schema,
    approval: value["approval"] === true,
  };
}

function readManifest(value: unknown): Manifest | null {
  if (!isRecord(value)) {
    return null;
  }

  const raw = value["tools"];
  if (!Array.isArray(raw) || raw.length > MAX_TOOLS) {
    return null;
  }

  const tools: ManifestTool[] = [];
  for (const entry of raw) {
    const tool = readTool(entry);
    if (tool !== null) {
      tools.push(tool);
    }
  }

  return { family: text(value, "family") ?? "", rev: text(value, "rev") ?? "", tools };
}

export class PepClient {
  public constructor(
    private readonly baseUrl: string,
    private readonly token: string,
    private readonly context: () => TurnContext,
  ) {}

  /**
   * Contract 04 §4. The family's tool list. A failure is the caller's to own.
   *
   * `held` is the revision this process already registered, or null before
   * any manifest arrived. Naming it turns the fetch into §4.2's conditional
   * request, and the PEP then answers 304 and no body while that revision is
   * still current. A PEP that does not know the header answers the whole
   * manifest, which costs a body and changes no outcome.
   */
  public async manifest(held: string | null = null): Promise<ManifestAnswer> {
    const response = await fetch(`${this.baseUrl}${MANIFEST_PATH}`, {
      headers: this.manifestHeaders(held),
      signal: AbortSignal.timeout(MANIFEST_TIMEOUT_MS),
    });

    if (response.status === HTTP_NOT_MODIFIED) {
      return { state: ManifestState.Unchanged };
    }

    if (response.status !== HTTP_OK) {
      throw new Error(`manifest fetch answered HTTP ${response.status}`);
    }

    const body = await response.text();
    if (Buffer.byteLength(body, "utf8") > MAX_MANIFEST_BYTES) {
      throw new Error(`manifest is over ${MAX_MANIFEST_BYTES} bytes`);
    }

    const manifest = readManifest(JSON.parse(body));
    if (manifest === null) {
      throw new Error("manifest has no usable tools list");
    }

    return { state: ManifestState.Fresh, manifest };
  }

  /** §4.2's conditional request, or §4's plain one when nothing is held. */
  private manifestHeaders(held: string | null): Record<string, string> {
    const headers = this.headers();
    if (held === null || !REV_HEADER_RE.test(held)) {
      return headers;
    }

    headers[IF_NONE_MATCH_HEADER] = `"${held}"`;

    return headers;
  }

  /** Contract 04 §7.1. One action. The answer is the result, or a `PepCallError`. */
  public async call(tool: string, args: Record<string, unknown>): Promise<unknown> {
    const response = await this.post(tool, args);
    const body = await this.readBody(response);

    if (response.status === HTTP_OK) {
      return this.resultOf(body);
    }

    throw new PepCallError(
      failureFor(response.status),
      response.status,
      text(body, "reason") ?? `http_${response.status}`,
      text(body, "detail"),
    );
  }

  private async post(tool: string, args: Record<string, unknown>): Promise<Response> {
    try {
      return await fetch(`${this.baseUrl}${CALL_PATH}`, {
        method: "POST",
        headers: { ...this.headers(), "Content-Type": "application/json" },
        body: JSON.stringify({ tool, args }),
        signal: AbortSignal.timeout(CALL_TIMEOUT_MS),
      });
    } catch (error) {
      throw new PepCallError(
        CallFailure.Unreachable,
        0,
        "pep_unreachable",
        error instanceof Error ? error.message : String(error),
      );
    }
  }

  /** A body that is not a JSON object is no body. The status still decides. */
  private async readBody(response: Response): Promise<Record<string, unknown>> {
    try {
      const parsed: unknown = JSON.parse(await response.text());

      return isRecord(parsed) ? parsed : {};
    } catch {
      return {};
    }
  }

  /**
   * Contract 04 §5.1: on a 200, a `result` key is the result, and a body
   * without one is the result whole, which is what §7.6's wrapper looks like
   * when it arrives at the top level.
   */
  private resultOf(body: Record<string, unknown>): unknown {
    return "result" in body ? body["result"] : body;
  }

  /** Contract 04 §3. Advisory, and absent rather than guessed when unknown. */
  private headers(): Record<string, string> {
    const context = this.context();
    const headers: Record<string, string> = { Authorization: `Bearer ${this.token}` };
    if (context.session !== null) {
      headers[SESSION_HEADER] = context.session;
    }
    if (context.turn !== null) {
      headers[TURN_HEADER] = context.turn;
    }
    if (context.delegation !== null) {
      headers[DELEGATION_HEADER] = context.delegation;
    }

    return headers;
  }
}

function failureFor(status: number): CallFailure {
  if (status === HTTP_ACCEPTED) {
    return CallFailure.Approval;
  }

  return status >= HTTP_SERVER_ERROR ? CallFailure.Failed : CallFailure.Denied;
}
