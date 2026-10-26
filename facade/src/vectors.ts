// The creche extensions of ACP v1, read from the one vector file that the
// facade tests and the attendance tests share. A method name, the line cap
// and the beat cadence have no second copy in this package.

import vectors from "../vectors/creche-extensions.json" with { type: "json" };

/** The one ACP version that the facade speaks. */
export const PROTOCOL_VERSION = vectors.protocol_version;

/** The largest line in UTF-8 bytes, the LF not counted. Each side checks it. */
export const MAX_LINE_BYTES = vectors.max_line_bytes;

/** A request. The facade answers `{}`. Two unanswered beats end the link. */
export const METHOD_BEAT = vectors.beat.request.method;

/** A notification. The facade stops Flue, then exits. */
export const METHOD_SHUTDOWN = vectors.shutdown.notification.method;
