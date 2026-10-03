// Every fixed name, path and number the bridge takes from a contract. Each
// line names the section that fixes it, so a reader can check the code against
// the contract without reading the code.

/** Contract 03 §7. The playpen sets both from the credential mount. */
export const PEP_URL_ENV = "PEP_URL";
export const PEP_TOKEN_ENV = "PEP_TOKEN";

/** Contract 03 §7. The session, and the turn the process was STARTED for. */
export const SESSION_ENV = "AGENT_SESSION";
export const TURN_ENV = "AGENT_TURN";

/**
 * The per-session file the playpen rewrites at every turn (`src/turn-file.ts`).
 * A held-open process outlives its first turn, so the environment alone cannot
 * carry the current turn id.
 */
export const TURN_FILE_ENV = "AGENT_TURN_FILE";

/**
 * Where this bridge states how many PEP tools it holds (`tool-state.ts`). The
 * playpen reads it to tell a turn's reader that the turn ran without tools.
 */
export const TOOL_STATE_FILE_ENV = "AGENT_TOOL_STATE_FILE";
export const TOOL_STATE_FILE = "tools.json";

/**
 * Contract 03 §7. Contract 01 §5.4's index root, a HOST path, which is also
 * its path inside the VM (§7.1). The playpen names it for every family,
 * and only a family that mounts an index has the directory.
 */
export const INDEX_ROOT_ENV = "AGENT_INDEX_ROOT";

/** Contract 03 §7.3 rule 6. The one tool the bridge serves itself. */
export const SEARCH_TOOL = "index_search";

/** Contract 04 §4.1. The verb that gives `index_search` its semantic half. */
export const EMBED_VERB = "embed";

/** `library/README.md`: one store per index directory. */
export const STORE_FILE = "store.db";

/** Contract 04 §3. Advisory: the PEP audits them and decides nothing on them. */
export const SESSION_HEADER = "X-Session-Id";
export const TURN_HEADER = "X-Turn-Id";
export const DELEGATION_HEADER = "X-Delegation-Id";

/** Contract 04 §4 and §7.1. The two paths a sandbox may call. */
export const MANIFEST_PATH = "/manifest";
export const CALL_PATH = "/call";

/** HTTP statuses this bridge reads by name. */
export const HTTP_OK = 200;
export const HTTP_ACCEPTED = 202;
export const HTTP_NOT_MODIFIED = 304;
export const HTTP_SERVER_ERROR = 500;

/**
 * Contract 04 §4.2. What the bridge sends to revalidate a manifest it holds.
 *
 * RFC 9110 §13.1.2, with the grants revision as the entity tag. The PEP
 * answers 304 and no body when that revision is still the one it serves.
 */
export const IF_NONE_MATCH_HEADER = "If-None-Match";

/**
 * Which revisions may ride in that header.
 *
 * The PEP's own `rev` is `uuid4().hex`, and contract 04 §1.2 promises only
 * that it is opaque, so a value that could not survive being quoted in a
 * header is not sent at all. A poll then costs a full body, the same as a
 * poll with no header.
 */
export const REV_HEADER_RE = /^[A-Za-z0-9._:+/=-]{1,128}$/;

/**
 * Contract 04 §8.8 rule 3: a gated call blocks at the PEP for up to 15
 * minutes, and the sandbox's client timeout has to exceed that. One minute of
 * margin covers the PEP's own deny-and-answer.
 */
export const CALL_TIMEOUT_MS = 16 * 60 * 1000;

/** The manifest is one local GET. A slow one means the PEP is unwell. */
export const MANIFEST_TIMEOUT_MS = 10000;

/**
 * Manifest attempts at process start. Short by design: this blocks the
 * session's first turn, and the PEP is on the same host.
 */
export const MANIFEST_ATTEMPTS = 3;
export const MANIFEST_RETRY_MS = 1000;

/**
 * After those attempts fail, the bridge keeps asking in the BACKGROUND.
 *
 * A pi process is held open across turns (contract 03 §6 rule 4), so without
 * this one a process born during a PEP outage holds zero tools for its whole
 * life, however long the PEP has been back. pi 0.99.1 takes a tool registered
 * after start-up and activates it (`test/real-pi.test.ts`), so the process
 * gains its tools instead of dying for them.
 *
 * The cost while the PEP stays down is fixed and small: ONE `GET /manifest`
 * per interval per pi process, never two at once, on a timer that does not
 * hold the process open. 15 s against contract 05 §3.3's 90 s threshold means
 * the tools are back within one turn of the PEP answering, and the fleet's 11
 * sandboxes add well under one request a second between them.
 */
export const MANIFEST_BACKGROUND_MS = 15000;

/**
 * Once a manifest is in hand the same loop keeps running, four times slower.
 *
 * This is what makes "a permission change is one action" true for the MODEL's
 * tool list and not only for the PEP's decision (contract 04 §4.2). A grant
 * the operator adds reaches the chat they are in, instead of only the next pi process
 * of that family, which for an attended session is up to
 * `pi_idle_ttl_s` = 900 s away.
 *
 * Why 60 s and not 15 s: a family's `pep_rpm` is 60 by default and a sandbox
 * holds up to 12 pi processes (`attendance/wire.py`), so the poll's worst case
 * is 12 requests a minute out of 60. At the toolless cadence that worst case
 * would be 48 of 60 and the poll could starve a real tool call. A toolless
 * process makes no other call, which is why it may ask faster.
 */
export const MANIFEST_WATCH_MS = 60000;

/**
 * Bounds on everything the PEP sends. It crosses a process boundary, so it is
 * validated for shape and size before use (invariants 12 and 14) even though
 * the PEP is the trusted end of the call.
 */
export const MAX_MANIFEST_BYTES = 1048576;
export const MAX_TOOLS = 256;
export const MAX_TOOL_NAME_LENGTH = 128;
export const MAX_DESCRIPTION_LENGTH = 8192;

/**
 * A tool result becomes a pi event, and contract 03 §8 caps a wrapped event at
 * 256 KiB. Cutting here with a visible marker beats letting the channel drop
 * the whole body.
 */
export const MAX_RESULT_BYTES = 262144;

/** A PEP error detail lands in the model's context. Keep it short and flat. */
export const MAX_DETAIL_LENGTH = 512;

/**
 * Contract 04 §4. A manifest name is `<server>__<tool>`, a verb name, or
 * `invoke_agent`. A server name carries hyphens (contract 01b §1) and a verb
 * name carries underscores, so both are allowed and nothing else is: the name
 * also becomes the quoted label of the untrusted block, and a quote, a space
 * or a line break there would let a payload argue with its own frame.
 */
export const TOOL_NAME_RE = /^[a-z][a-z0-9_-]{0,127}$/;

/** Contract 04 §4. What joins a server's name to its tool's: `kagi__search`. */
export const NAMESPACE_SEPARATOR = "__";
