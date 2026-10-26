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
import type { AgentApp, AnyMessage, AnyResponse, InitializeResponse, Stream } from "@agentclientprotocol/sdk";
import { Readable } from "node:stream";

import { describeRefusal, encodeLine } from "./line.js";
import type { Say } from "./stdio.js";
import { MAX_LINE_BYTES, METHOD_BEAT, PROTOCOL_VERSION } from "./vectors.js";

export type { AgentApp, Stream };

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

export function facadeApp(): AgentApp {
  return agent({ name: "creche-facade" })
    .onRequest(methods.agent.initialize, () => INITIALIZE_RESPONSE)
    .onRequest(METHOD_BEAT, noParams, () => ({}));
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

