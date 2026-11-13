// The ACP v1 values of the facade, read from the one vector file that the
// facade tests read too. The protocol version and the line cap have no
// second copy in this package.

import vectors from "../vectors/creche-extensions.json" with { type: "json" };

/** The one ACP version that the facade speaks. */
export const PROTOCOL_VERSION = vectors.protocol_version;

/** The largest line in UTF-8 bytes, the LF not counted. Each side checks it. */
export const MAX_LINE_BYTES = vectors.max_line_bytes;
