// The one module that imports the ACP SDK. The SDK's types are the one
// definition of each ACP message, and the SDK validates the params of every
// request before a handler runs.
//
//   stdin ──► ndJsonStream ──► AgentApp ──► strictWriter ──► one line ──► stdout
//
// The SDK writes a message with `JSON.stringify`. The facade writes it with
// `encodeLine` instead: a strict text under the line cap. An answer that
// fails becomes a JSON-RPC error with the same id. Another message that
// fails is dropped, with a line on stderr.

import { agent, methods, ndJsonStream, RequestError } from "@agentclientprotocol/sdk";
import type {
  AgentApp,
  AnyMessage,
  AnyResponse,
  InitializeResponse,
  ListSessionsRequest,
  ListSessionsResponse,
  NewSessionRequest,
  NewSessionResponse,
  SessionInfo,
  SessionInfoUpdate,
  Stream,
} from "@agentclientprotocol/sdk";
import { Readable } from "node:stream";

import { describeRefusal, encodeLine } from "./line.js";
import type { Say } from "./stdio.js";
import { MAX_LINE_BYTES, METHOD_BEAT, METHOD_SHUTDOWN, PROTOCOL_VERSION } from "./vectors.js";

export type {
  AgentApp,
  ListSessionsRequest,
  ListSessionsResponse,
  NewSessionRequest,
  NewSessionResponse,
  SessionInfo,
  SessionInfoUpdate,
  Stream,
};

/** Why the facade refuses a request. Each kind is one JSON-RPC error. */
export type Refusal =
  | { readonly kind: "invalid_params"; readonly detail: string }
  | { readonly kind: "unknown_session"; readonly sessionId: string };

export type Outcome<T> = { readonly ok: true; readonly value: T } | { readonly ok: false; readonly refusal: Refusal };

/** The session methods. Null on a link that serves no session yet. */
export interface SessionService {
  newSession(params: NewSessionRequest): Promise<Outcome<NewSessionResponse>>;
  listSessions(params: ListSessionsRequest): Promise<Outcome<ListSessionsResponse>>;
}

function toError(refusal: Refusal): RequestError {
  switch (refusal.kind) {
    case "invalid_params":
      return RequestError.invalidParams(undefined, refusal.detail);
    case "unknown_session":
      return RequestError.resourceNotFound(refusal.sessionId);
    default: {
      const unknown: never = refusal;
      return unknown;
    }
  }
}

async function answer<T>(outcome: Promise<Outcome<T>>): Promise<T> {
  const settled = await outcome;
  if (!settled.ok) {
    throw toError(settled.refusal);
  }

  return settled.value;
}

/** JSON-RPC's code for an error inside the server. */
const INTERNAL_ERROR = -32603;

/**
 * The answer to `initialize`, whatever the client offers. The facade reads
 * no client file system and opens no client terminal, so it needs neither
 * capability.
 */
export const INITIALIZE_RESPONSE: InitializeResponse = {
  protocolVersion: PROTOCOL_VERSION,
  agentCapabilities: { loadSession: true, sessionCapabilities: { list: {} } },
  authMethods: [],
};

/** The params of a creche extension: absent or an empty object. */
function noParams(params: unknown): Record<string, never> {
  const empty =
    params === undefined ||
    (typeof params === "object" && params !== null && !Array.isArray(params) && Object.keys(params).length === 0);
  if (!empty) {
    throw RequestError.invalidParams(undefined, "a creche extension takes no params");
  }

  return {};
}

/** What the creche extensions do on the supervisor link. */
export interface SupervisorHooks {
  /** A `_creche/beat` arrived. */
  readonly onBeat: () => void;
  /** A `_creche/shutdown` arrived. */
  readonly onShutdown: () => void;
}

export function facadeApp(hooks: SupervisorHooks, sessions: SessionService | null): AgentApp {
  const app = agent({ name: "creche-facade" })
    .onRequest(methods.agent.initialize, () => INITIALIZE_RESPONSE)
    .onRequest(METHOD_BEAT, noParams, () => {
      hooks.onBeat();
      return {};
    })
    .onNotification(METHOD_SHUTDOWN, noParams, () => {
      hooks.onShutdown();
    });

  if (sessions === null) {
    return app;
  }

  return app
    .onRequest(methods.agent.session.new, ({ params }) => answer(sessions.newSession(params)))
    .onRequest(methods.agent.session.list, ({ params }) => answer(sessions.listSessions(params)));
}

function isAnswer(message: AnyMessage): message is AnyResponse {
  return "id" in message && !("method" in message);
}

/** Writes each message as one strict line, or answers the refusal. */
export function strictWriter(writeLine: (line: string) => void, say: Say): (message: AnyMessage) => void {
  return (message) => {
    const encoded = encodeLine(message);
    if (encoded.ok) {
      writeLine(encoded.line);
      return;
    }

    const detail = describeRefusal(encoded.refusal);
    if (!isAnswer(message)) {
      say(`dropped an outbound message: ${detail}`);
      return;
    }

    say(`answered request ${String(message.id)} with an error: ${detail}`);
    const refused = encodeLine({ jsonrpc: "2.0", id: message.id, error: { code: INTERNAL_ERROR, message: detail } });
    if (refused.ok) {
      writeLine(refused.line);
    }
  };
}

/**
 * The ACP stream of one connection. Inbound bytes go through the SDK's
 * NDJSON reader, capped at `MAX_LINE_BYTES`. The SDK writes its own parse
 * errors as bytes, so those bytes are read back and written strict too.
 */
export function strictStream(input: Readable, write: (message: AnyMessage) => void): Stream {
  const decoder = new TextDecoder();
  const sdkBytes = new WritableStream<Uint8Array>({
    write: (chunk) => {
      write(JSON.parse(decoder.decode(chunk)) as AnyMessage);
    },
  });

  const { readable } = ndJsonStream(sdkBytes, Readable.toWeb(input) as ReadableStream<Uint8Array>, {
    maxMessageBytes: MAX_LINE_BYTES,
  });

  return { readable, writable: new WritableStream<AnyMessage>({ write }) };
}

