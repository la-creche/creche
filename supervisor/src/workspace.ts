// Contract 03 §7.2. A mount cannot join a running sandbox, so the
// `code-sandbox` family mounts ONE parent, the work root, and each turn gets a
// link to its own chat's directory beneath it:
//
//   /tmp/code-sandbox-<owner session>  ──►  <work root>/<owner session>
//
// The host never sends a host path. The supervisor derives the target from its
// own mount, so a compromised host message cannot aim the link somewhere else.
// The directory belongs to the calling chat session, not to the job, and it
// lives under /srv/agents, so a reboot or a /tmp cleaner does not touch it.
//
// The LINK is this process's to remove, and the DIRECTORY is the host's
// (contract 02 §12.1 rule 4). Several jobs of one chat share one link, so the
// last holder removes it and an earlier one never does.

import { lstatSync, mkdirSync, readlinkSync, rmSync, symlinkSync } from "node:fs";
import { isAbsolute, join } from "node:path";

import {
  CODE_SANDBOX_LINK_PREFIX,
  CODE_SANDBOX_ROOT,
  CODE_SANDBOX_ROOT_ENV,
  MAX_SESSION_ID_LENGTH,
  SESSION_ID_RE,
} from "./constants.js";

export class WorkspaceLinkError extends Error {}

/**
 * The work root this process links into (§7.1 rule 6, §7.2).
 *
 * The contract path by default, because the identity rule makes a mount's
 * in-VM path its host path. A relative or blank override is ignored rather
 * than joined: a relative root would resolve against whatever directory this
 * process happens to be in, and link every chat somewhere new.
 */
export function codeSandboxRoot(env: NodeJS.ProcessEnv): string {
  const override = (env[CODE_SANDBOX_ROOT_ENV] ?? "").trim();

  return isAbsolute(override) ? override : CODE_SANDBOX_ROOT;
}

/**
 * The owner session, checked beside the join that turns it into a path.
 *
 * `validate.ts` applies the same shape at the channel's door, which is where
 * §7.2 rule 1's refusal of `/`, `.` and `..` belongs. This repeats it because
 * the value is about to become a path, and a check beside the join is the one
 * a later reader can see.
 */
function isOwnerSession(value: string): boolean {
  if (value.length === 0 || value.length > MAX_SESSION_ID_LENGTH) {
    return false;
  }
  if (value === "." || value === "..") {
    return false;
  }

  return SESSION_ID_RE.test(value);
}

/** The link's target, or null when nothing is there. A file is not a link. */
function readExistingLink(link: string): string | null {
  try {
    if (!lstatSync(link).isSymbolicLink()) {
      return "";
    }

    return readlinkSync(link);
  } catch {
    return null;
  }
}

/**
 * Every `code-sandbox` link this supervisor made, and who holds each one.
 *
 *   session id ──► owner session ──► one link, held by 1..n sessions
 *
 * Two jobs of one chat share one link (§7.2 rule 2 creates it only when it is
 * absent), so removing it when the FIRST job ends would pull the working
 * directory out from under the second. The count is what makes the removal
 * safe, and it is per supervisor because the link lives in the sandbox's own
 * /tmp and dies with the VM anyway.
 */
export class CodeSandboxLinks {
  private readonly owners = new Map<string, string>();
  private readonly holders = new Map<string, Set<string>>();

  public constructor(
    private readonly root: string = CODE_SANDBOX_ROOT,
    private readonly linkPrefix: string = CODE_SANDBOX_LINK_PREFIX,
  ) {}

  /** The link path for an owner session, or null when the id is not one. */
  public pathFor(ownerSession: string): string | null {
    if (!isOwnerSession(ownerSession)) {
      return null;
    }

    return `${this.linkPrefix}${ownerSession}`;
  }

  /**
   * Makes the link, records `session` as a holder, and answers the cwd.
   *
   * The work root is the mount. The per-session directory below it may not
   * exist yet on a first turn, and a cwd that does not exist fails the spawn.
   */
  public hold(session: string, ownerSession: string): string {
    const link = this.pathFor(ownerSession);
    if (link === null) {
      throw new WorkspaceLinkError("owner_session is not a session id");
    }

    const target = join(this.root, ownerSession);
    this.make(link, target);

    this.owners.set(session, ownerSession);
    const held = this.holders.get(ownerSession) ?? new Set<string>();
    held.add(session);
    this.holders.set(ownerSession, held);

    return link;
  }

  /** Drops one holder. The last one out removes the link. */
  public release(session: string): void {
    const ownerSession = this.owners.get(session);
    if (ownerSession === undefined) {
      return;
    }

    this.owners.delete(session);

    const held = this.holders.get(ownerSession);
    held?.delete(session);
    if (held !== undefined && held.size > 0) {
      return;
    }

    this.holders.delete(ownerSession);
    this.remove(ownerSession);
  }

  private make(link: string, target: string): void {
    try {
      mkdirSync(target, { recursive: true });
    } catch (error) {
      throw new WorkspaceLinkError(`cannot create the work directory: ${String(error)}`);
    }

    const existing = readExistingLink(link);
    if (existing === target) {
      return;
    }
    if (existing !== null) {
      throw new WorkspaceLinkError("the link exists and points somewhere else");
    }

    try {
      symlinkSync(target, link);
    } catch (error) {
      // A racing turn of the same chat may have won. Accept its identical link.
      if (readExistingLink(link) !== target) {
        throw new WorkspaceLinkError(`cannot create the link: ${String(error)}`);
      }
    }
  }

  /**
   * Removes the link, and only when it is still the one this process made.
   *
   * `rm` on the LINK never touches what it points at, so the chat's working
   * directory survives (contract 02 §12.1 rule 4). Anything else at that path
   * belongs to somebody else and is left alone.
   */
  private remove(ownerSession: string): void {
    const link = this.pathFor(ownerSession);
    if (link === null || readExistingLink(link) !== join(this.root, ownerSession)) {
      return;
    }

    try {
      rmSync(link, { force: true });
    } catch {
      // /tmp went away with the VM. Nothing left to clean.
    }
  }
}
