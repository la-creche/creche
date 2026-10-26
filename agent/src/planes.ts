// A port of `playpen/src/planes.ts`: the grammar of a plane URL. A plane is a
// service of the host that each sandbox calls: the chaperone and LiteLLM.
//
// A plane URL is `http://<host>:<port>` and no other byte. The grammar is
// the grammar of the Rust type `PlaneUrl`, which the playpen cites
// (`rust/crates/creche-contracts/src/config/endpoints.rs`). The host is an
// IPv4 address or a host name and keeps its letter case. The port is 1 to
// 65535, decimal, with no sign and no zero at the start.

/** Why a text is not a plane URL. No member holds the text. */
export type PlaneUrlFault =
  | "not_http"
  | "user_part"
  | "after_authority"
  | "host"
  | "no_port"
  | "port_form"
  | "port_range";

export type PlaneUrlParse =
  | { readonly ok: true; readonly url: PlaneUrl }
  | { readonly ok: false; readonly fault: PlaneUrlFault };

const HTTP_SCHEME = "http://";
const AUTHORITY_END = /[/?#]/;
const LABEL_RE = /^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$/;
/** A label that the C resolver reads as a number: decimal, or `0x` and hexadecimal. */
const NUMBER_LABEL_RE = /^(?:[0-9]+|0[xX][0-9A-Fa-f]*)$/;
const OCTET_RE = /^(?:0|[1-9][0-9]{0,2})$/;
const OCTET_MAX = 255;
const OCTET_COUNT = 4;
/** The address of each interface. A URL with it names no one host. */
const EACH_INTERFACE = "0.0.0.0";
const PORT_RE = /^[1-9][0-9]*$/;
const PORT_MAX = 65535;

/** A text whose last label is a number must be four decimal octets. */
function isHost(text: string): boolean {
  const labels = text.split(".");
  if (!labels.every((label) => LABEL_RE.test(label))) {
    return false;
  }

  if (!NUMBER_LABEL_RE.test(labels.at(-1) ?? "")) {
    return true;
  }

  const octets = labels.every((label) => OCTET_RE.test(label) && Number(label) <= OCTET_MAX);

  return labels.length === OCTET_COUNT && octets && text !== EACH_INTERFACE;
}

function portFault(text: string): PlaneUrlFault | null {
  if (text.length === 0) {
    return "no_port";
  }

  if (!PORT_RE.test(text)) {
    return "port_form";
  }

  const inRange = text.length <= String(PORT_MAX).length && Number(text) <= PORT_MAX;

  return inRange ? null : "port_range";
}

/** Only `parse` makes a value, so a value is a text that the grammar took. */
export class PlaneUrl {
  private constructor(private readonly value: string) {}

  public static parse(text: string): PlaneUrlParse {
    if (!text.startsWith(HTTP_SCHEME)) {
      return { ok: false, fault: "not_http" };
    }

    const rest = text.slice(HTTP_SCHEME.length);
    const authority = rest.split(AUTHORITY_END, 1)[0] ?? rest;
    if (authority.includes("@")) {
      return { ok: false, fault: "user_part" };
    }

    if (authority.length !== rest.length) {
      return { ok: false, fault: "after_authority" };
    }

    const colon = rest.lastIndexOf(":");
    if (!isHost(colon === -1 ? rest : rest.slice(0, colon))) {
      return { ok: false, fault: "host" };
    }

    const fault = portFault(colon === -1 ? "" : rest.slice(colon + 1));

    return fault === null ? { ok: true, url: new PlaneUrl(text) } : { ok: false, fault };
  }

  public get text(): string {
    return this.value;
  }
}
