// The creche keys of `_meta` that attendance sends (vectors `session_meta`).
// Each value comes from another process, so this module checks its shape
// and size. An absent key sets nothing. The facade reads no other key.
//
//   session/new     creche/job         `job-<ulid>` or `auto-<ulid>`
//   session/prompt  creche/delegation  {id: <ulid>, caller_session}
//                   creche/trigger     {kind, name, fired_at, chain?}
//
// The facade sends `creche/created_at` back (`utcOf`), and reads it never.

import { META_DELEGATION, META_JOB, META_TRIGGER } from "./vectors.js";

/** The `_meta` of an ACP message, as the SDK gives it. */
export type Meta = { readonly [key: string]: unknown } | null | undefined;

export type Checked<T> = { readonly ok: true; readonly value: T } | { readonly ok: false; readonly detail: string };

/** Contract 02 §13.2's trigger, with the field names of the wire. */
export interface Trigger {
  readonly kind: "timer" | "webhook" | "dispatch";
  readonly name: string | null;
  readonly fired_at: string | null;
  readonly chain?: readonly string[];
}

export interface PromptMeta {
  /** The delegation ID. `caller_session` is advisory, and attendance keeps it. */
  readonly delegation: string | null;
  readonly trigger: Trigger | null;
}

const ULID = "[0-9A-HJKMNP-TV-Z]{26}";
const JOB = new RegExp(`^(job|auto)-${ULID}$`);
const DELEGATION_ID = new RegExp(`^${ULID}$`);
const SESSION = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/;
const FAMILY = /^[a-z][a-z0-9-]{1,30}$/;
const NAME = /^[^\u0000-\u001f]{1,128}$/;
const RFC3339 = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,9})?(Z|[+-]\d{2}:\d{2})$/;
const KINDS: readonly unknown[] = ["timer", "webhook", "dispatch"];
/** Contract 02 §13.2 rule 6. */
const MAX_CHAIN = 8;

/** An object with each of `fields`, and no key but those and `optional`. */
function shaped(value: unknown, fields: readonly string[], optional = ""): value is Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    return false;
  }

  const keys = Object.keys(value);

  return fields.every((field) => keys.includes(field)) && keys.every((key) => fields.includes(key) || key === optional);
}

function isText(value: unknown, pattern: RegExp): value is string {
  return typeof value === "string" && pattern.test(value);
}

function nullOr(value: unknown, pattern: RegExp): value is string | null {
  return value === null || isText(value, pattern);
}

function chainOf(value: unknown): value is string[] {
  return Array.isArray(value) && value.length > 0 && value.length <= MAX_CHAIN && value.every((family) => isText(family, FAMILY));
}

function fail<T>(key: string): Checked<T> {
  return { ok: false, detail: `_meta ${key} is not of its shape` };
}

/** The job id of `session/new`, or null when the key is absent. */
export function jobOf(meta: Meta): Checked<string | null> {
  const value = meta?.[META_JOB];
  if (value === undefined) {
    return { ok: true, value: null };
  }

  return isText(value, JOB) ? { ok: true, value } : fail(META_JOB);
}

/** `text` as RFC 3339 UTC, or null when it is not an RFC 3339 time. */
export function utcOf(text: string): string | null {
  const at = RFC3339.test(text) ? new Date(text) : null;

  return at === null || Number.isNaN(at.getTime()) ? null : at.toISOString();
}

/** A trigger rebuilt from its checked fields, so no other field passes. */
export function triggerOf(value: unknown): Trigger | null {
  if (!shaped(value, ["kind", "name", "fired_at"], "chain") || !KINDS.includes(value["kind"])) {
    return null;
  }

  const { kind, name, fired_at: firedAt, chain } = value;
  if (!nullOr(name, NAME) || !nullOr(firedAt, RFC3339) || (chain !== undefined && !chainOf(chain))) {
    return null;
  }

  const trigger: Trigger = { kind: kind as Trigger["kind"], name, fired_at: firedAt };

  return chain === undefined ? trigger : { ...trigger, chain };
}

/** The delegation and the trigger of `session/prompt`, each null when absent. */
export function promptMetaOf(meta: Meta): Checked<PromptMeta> {
  const sent = meta?.[META_DELEGATION];
  let delegation: string | null = null;
  if (sent !== undefined) {
    if (!shaped(sent, ["id", "caller_session"]) || !isText(sent["id"], DELEGATION_ID) || !nullOr(sent["caller_session"], SESSION)) {
      return fail(META_DELEGATION);
    }

    delegation = sent["id"];
  }

  const raw = meta?.[META_TRIGGER];
  const trigger = raw === undefined ? null : triggerOf(raw);
  if (raw !== undefined && trigger === null) {
    return fail(META_TRIGGER);
  }

  return { ok: true, value: { delegation, trigger } };
}
