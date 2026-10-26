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

/** How often attendance sends `_creche/beat`. */
export const BEAT_INTERVAL_MS = vectors.beat_interval_s * 1000;

/**
 * How long the facade waits for a beat. Two missed beats end the link on
 * attendance's side. The facade waits one more interval, so a late beat
 * never ends a live link: 90 s, attendance's `HOST_DEADLINE_S`.
 */
export const BEAT_DEADLINE_MS = BEAT_INTERVAL_MS * (vectors.missed_beats_allowed + 1);

/** The lock file in the control mount. attendance reads its `beat`. */
export const LOCK_FILE = vectors.lock.file;

/** How often the facade rewrites the lock with a counter one higher. */
export const LOCK_BEAT_MS = vectors.lock.beat_s * 1000;

/** The `_meta` key of the job id, on `session/new`. */
export const META_JOB = vectors.session_meta.job.key;

/** The `_meta` key of the delegation, on the first `session/prompt`. */
export const META_DELEGATION = vectors.session_meta.delegation.key;

/** The `_meta` key of the trigger, on the first `session/prompt`. */
export const META_TRIGGER = vectors.session_meta.trigger.key;

/** The `_meta` key of the creation time, on `session/list` and `session_info_update`. */
export const META_CREATED = vectors.session_meta.created_at.key;
