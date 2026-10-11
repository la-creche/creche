// Contract 03 §7. The two planes of a sandbox, and where each one listens.
//
// A plane is a service of the host that each sandbox calls: the chaperone and
// LiteLLM. The env file of a sandbox can carry the URL of each one:
//
//   AGENT_PEP_URL=http://192.0.2.10:8300
//   AGENT_LITELLM_BASE_URL=http://192.0.2.10:4000
//
// This file is the one reader of the two variables. The playpen reads them
// one time, when it builds its pool. The terminal launcher reads them when it
// plans a launch. A pi process gets the two URLs from `env.ts` and
// `models-json.ts`, which take them as parameters.
//
// CONTRACT-QUESTION: contract 03 §7 gives the two URLs as fixed values and
// names no variable for them. The playpen takes each URL from its variable
// when the value is a plane URL. For a variable that is absent or not valid
// it takes the URL of the build (`constants.ts`) and says so in one `log`
// line. A playpen that refuses to start without the two variables costs one
// reason of `fatal` and the removal of `PlaneUrl.ofBuild`.

import { LITELLM_BASE_URL, PEP_URL } from "./constants.js";
import type { LogMessage } from "./protocol.js";

/** Each names the URL of one plane, in the env file of a sandbox. */
export const PEP_PLANE_ENV = "AGENT_PEP_URL";
export const LITELLM_PLANE_ENV = "AGENT_LITELLM_BASE_URL";

/** A service of the host that each sandbox calls. The set is closed. */
export enum Plane {
  /** The chaperone, the Policy Enforcement Point (PEP). */
  Pep = "pep",
  /** LiteLLM, the one model provider of a sandbox. */
  Litellm = "litellm",
}

const VARIABLE_OF: Readonly<Record<Plane, string>> = {
  [Plane.Pep]: PEP_PLANE_ENV,
  [Plane.Litellm]: LITELLM_PLANE_ENV,
};

const BUILD_URL_OF: Readonly<Record<Plane, string>> = {
  [Plane.Pep]: PEP_URL,
  [Plane.Litellm]: LITELLM_BASE_URL,
};

/**
 * Why a text is not the URL of a plane. No member holds the text, so a log
 * line can name the member.
 */
export enum PlaneUrlFault {
  /** The text does not start with `http://`. */
  NotHttp = "not_http",
  /** The text has a user part before the host. */
  UserPart = "user_part",
  /** The text has a path, a query or a fragment after the host and the port. */
  AfterAuthority = "after_authority",
  /** The host is not an IPv4 address and not a host name. */
  Host = "host",
  /** The text gives no port after the host. */
  NoPort = "no_port",
  /** The port is not decimal digits only, or it starts with a zero. */
  PortForm = "port_form",
  /** The port is not 1 to 65535. */
  PortRange = "port_range",
}

export type PlaneUrlParse =
  | { readonly ok: true; readonly url: PlaneUrl }
  | { readonly ok: false; readonly fault: PlaneUrlFault };

const HTTP_SCHEME = "http://";
const AUTHORITY_END = /[/?#]/;
const USER_SEPARATOR = "@";
const PORT_SEPARATOR = ":";
const LABEL_SEPARATOR = ".";

/** One DNS label: 1 to 63 ASCII letters, digits and hyphens, no hyphen at an end. */
const LABEL_RE = /^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$/;

/** A label that the C resolver reads as a number: decimal, or `0x` and hexadecimal. */
const NUMBER_LABEL_RE = /^(?:[0-9]+|0[xX][0-9A-Fa-f]*)$/;

/** One number of an IPv4 address, with no zero before another digit. */
const OCTET_RE = /^(?:0|[1-9][0-9]{0,2})$/;
const OCTET_MAX = 255;
const OCTET_COUNT = 4;

/** The address of each interface. A URL with it names no one host. */
const EACH_INTERFACE = "0.0.0.0";

/** Decimal digits with no zero at the start, so one port has one text. */
const PORT_RE = /^[1-9][0-9]*$/;
const PORT_MAX = 65535;
const PORT_MAX_DIGITS = String(PORT_MAX).length;

function isIpv4(labels: readonly string[]): boolean {
  return (
    labels.length === OCTET_COUNT &&
    labels.every((label) => OCTET_RE.test(label) && Number(label) <= OCTET_MAX)
  );
}

/**
 * Whether `text` is an IPv4 address or a host name.
 *
 * A text whose last label is a number must be one IPv4 address of four
 * decimal numbers, and not the address of each interface. The resolver reads
 * `0` and `0.0` as that address.
 */
function isHost(text: string): boolean {
  const labels = text.split(LABEL_SEPARATOR);
  if (!labels.every((label) => LABEL_RE.test(label))) {
    return false;
  }

  if (!NUMBER_LABEL_RE.test(labels.at(-1) ?? "")) {
    return true;
  }

  return isIpv4(labels) && text !== EACH_INTERFACE;
}

/** What is wrong with the text after the last colon, or null for a port. */
function portFault(text: string): PlaneUrlFault | null {
  if (text.length === 0) {
    return PlaneUrlFault.NoPort;
  }

  if (!PORT_RE.test(text)) {
    return PlaneUrlFault.PortForm;
  }

  const inRange = text.length <= PORT_MAX_DIGITS && Number(text) <= PORT_MAX;

  return inRange ? null : PlaneUrlFault.PortRange;
}

/**
 * The URL of a plane: `http://<host>:<port>`, and no other byte.
 *
 * The grammar is the grammar of the Rust type `PlaneUrl`
 * (`rust/crates/creche-contracts/src/config/endpoints.rs`). The host is an
 * IPv4 address or a host name, and it keeps its letter case. The port is 1
 * to 65535, as decimal digits with no sign and no zero at the start. The type
 * refuses `https`, a user part, a path, a query and a fragment. A final slash
 * is a path.
 *
 * Only `parse` and `ofBuild` make a value. So a value is a text that the
 * grammar took, or one of the two URLs of the build.
 */
export class PlaneUrl {
  private constructor(private readonly value: string) {}

  public static parse(text: string): PlaneUrlParse {
    if (!text.startsWith(HTTP_SCHEME)) {
      return { ok: false, fault: PlaneUrlFault.NotHttp };
    }

    const rest = text.slice(HTTP_SCHEME.length);
    const authority = rest.split(AUTHORITY_END, 1)[0] ?? rest;
    if (authority.includes(USER_SEPARATOR)) {
      return { ok: false, fault: PlaneUrlFault.UserPart };
    }

    if (authority.length !== rest.length) {
      return { ok: false, fault: PlaneUrlFault.AfterAuthority };
    }

    const colon = rest.lastIndexOf(PORT_SEPARATOR);
    const host = colon === -1 ? rest : rest.slice(0, colon);
    if (!isHost(host)) {
      return { ok: false, fault: PlaneUrlFault.Host };
    }

    const fault = portFault(colon === -1 ? "" : rest.slice(colon + 1));
    if (fault !== null) {
      return { ok: false, fault };
    }

    return { ok: true, url: new PlaneUrl(text) };
  }

  /**
   * The URL that the build fixed for `plane` (`constants.ts`). No grammar
   * checks it: the image build made it from the LAN address of the site.
   */
  public static ofBuild(plane: Plane): PlaneUrl {
    return new PlaneUrl(BUILD_URL_OF[plane]);
  }

  /** The URL as text, with each byte that the parse took. */
  public get text(): string {
    return this.value;
  }
}

/** The URL of each plane, as a pi process gets it. */
export interface PlaneUrls {
  readonly pep: PlaneUrl;
  readonly litellm: PlaneUrl;
}

/** Why the playpen took the URL of the build for one plane. */
export enum PlaneState {
  /** The variable is absent, or its value is empty. */
  Unset = "unset",
  /** The value is not a plane URL. */
  NotValid = "not_valid",
}

/**
 * One plane whose variable gave no URL. A notice holds members of closed
 * sets only, so it has no place for the value of a variable.
 */
export type PlaneNotice =
  | { readonly plane: Plane; readonly state: PlaneState.Unset }
  | {
      readonly plane: Plane;
      readonly state: PlaneState.NotValid;
      readonly fault: PlaneUrlFault;
    };

export interface PlaneReading {
  readonly urls: PlaneUrls;
  /** One notice for each variable that gave no URL, the chaperone first. */
  readonly notices: readonly PlaneNotice[];
}

/** The URL of one plane, and the notice when the environment gave none. */
interface PlaneRead {
  readonly url: PlaneUrl;
  readonly notice: PlaneNotice | null;
}

function readPlane(env: NodeJS.ProcessEnv, plane: Plane): PlaneRead {
  const value = env[VARIABLE_OF[plane]] ?? "";
  if (value.length === 0) {
    return { url: PlaneUrl.ofBuild(plane), notice: { plane, state: PlaneState.Unset } };
  }

  const parsed = PlaneUrl.parse(value);
  if (!parsed.ok) {
    return {
      url: PlaneUrl.ofBuild(plane),
      notice: { plane, state: PlaneState.NotValid, fault: parsed.fault },
    };
  }

  return { url: parsed.url, notice: null };
}

/**
 * The URL of each plane, from the environment that the env file carries.
 *
 * An empty value counts as an absent variable, as it does for the three
 * directories of `mounts.ts`. A value keeps each byte: the reader removes no
 * space, so a value with a space is not valid.
 *
 * CONTRACT-QUESTION: contract 03 §7 does not say if a reader removes the
 * space at the two ends of a value. The reading here is the strict one: the
 * reader removes none. The Rust config reader of a daemon removes that space
 * before its parse (`rust/crates/creche-contracts/src/config.rs`). The other
 * reading costs one `trim` here and each test row with such a space.
 */
export function readPlaneUrls(env: NodeJS.ProcessEnv): PlaneReading {
  const pep = readPlane(env, Plane.Pep);
  const litellm = readPlane(env, Plane.Litellm);
  const notices = [pep.notice, litellm.notice].filter((notice) => notice !== null);

  return { urls: { pep: pep.url, litellm: litellm.url }, notices };
}

const USES_BUILD_URL = "The playpen uses the URL of its build.";

/**
 * Contract 03 §5.5. The `log` line for one notice: `warn` for a variable
 * that is absent, `error` for a value that is not valid. The line belongs to
 * the process, so it names no session.
 *
 * The message holds the name of one of the two variables and, for a value
 * that is not valid, one fault word. Each one is a member of a closed set.
 */
export function planeLog(notice: PlaneNotice): LogMessage {
  const variable = VARIABLE_OF[notice.plane];
  if (notice.state === PlaneState.Unset) {
    return {
      type: "log",
      level: "warn",
      session: null,
      message: `${variable} is not set. ${USES_BUILD_URL}`,
    };
  }

  return {
    type: "log",
    level: "error",
    session: null,
    message: `${variable} is not a plane URL: ${notice.fault}. ${USES_BUILD_URL}`,
  };
}
