// The tables are the tables of the tests of the Rust type `PlaneUrl`. Keep
// the two copies equal.

import { describe, expect, it } from "vitest";

import { PlaneUrl } from "../src/planes.js";

const ACCEPTED = [
  "http://192.0.2.10:8300", "http://192.0.2.10:4000", "http://127.0.0.1:1",
  "http://localhost:65535", "http://host-1.example:80", "http://A1:8300", "http://LOCALHOST:80",
];

const REFUSED: readonly (readonly [string, string])[] = [
  ["", "not_http"], ["192.0.2.10:8300", "not_http"], ["https://192.0.2.10:8300", "not_http"],
  ["HTTP://192.0.2.10:8300", "not_http"], ["http:/192.0.2.10:8300", "not_http"],
  [" http://192.0.2.10:8300", "not_http"], ["http://user@192.0.2.10:8300", "user_part"],
  ["http://user:word@192.0.2.10:8300", "user_part"], ["http://user@192.0.2.10:8300/v1", "user_part"],
  ["http://192.0.2.10:8300/", "after_authority"], ["http://192.0.2.10:8300/v1", "after_authority"],
  ["http://192.0.2.10:8300?a=b", "after_authority"], ["http://192.0.2.10:8300#top", "after_authority"],
  ["http://192.0.2.10/:8300", "after_authority"], ["http://", "host"], ["http://:8300", "host"],
  ["http://0.0.0.0:8300", "host"], ["http://192.0.2:8300", "host"], ["http://[::1]:8300", "host"],
  ["http://::1:8300", "host"], ["http://my_host:8300", "host"], ["http://192.0.2.10 :8300", "host"],
  ["http://192.0.2.10:8300:8300", "host"], ["http://１９２.0.2.10:8300", "host"],
  ["http://192.0.2.10", "no_port"], ["http://192.0.2.10:", "no_port"], ["http://localhost", "no_port"],
  ["http://192.0.2.10:8300\n", "port_form"], ["http://192.0.2.10:８３００", "port_form"],
  ["http://192.0.2.10:+8300", "port_form"], ["http://192.0.2.10:-1", "port_form"],
  ["http://192.0.2.10:8_300", "port_form"], ["http://192.0.2.10: 8300", "port_form"],
  ["http://192.0.2.10:8300 ", "port_form"], ["http://192.0.2.10:08300", "port_form"],
  ["http://192.0.2.10:0", "port_form"], ["http://192.0.2.10:0x50", "port_form"],
  ["http://192.0.2.10:http", "port_form"], ["http://192.0.2.10:65536", "port_range"],
  ["http://192.0.2.10:99999999999999999999", "port_range"],
  [`http://192.0.2.10:${"9".repeat(5000)}`, "port_range"],
];

// The tables of the Rust type `LanAddress`, with no text that holds a `/`.
const ACCEPTED_HOSTS = [
  "192.0.2.10", "127.0.0.1", "255.255.255.255", "localhost", "host-1.example",
  "a", "A1", "1a", "0.a", "x.0y", "0xg", "a".repeat(63),
];

const REFUSED_HOSTS = [
  "", "192.0.2.10\n", " 192.0.2.10", "192.0.2.10:8300", "a..b", "a.", ".a", "-a", "a-",
  "my_host", "::1", "１９２.0.2.10", "a".repeat(64), "0.0.0.0", "0", "0.0", "192.0.2",
  "192.0.2.256", "192.0.2.010", "192.0.2.10.5", "0x0", "host.0x7f", "host.123", "0x", "host.0X",
];

function outcome(text: string): string {
  const parsed = PlaneUrl.parse(text);

  return parsed.ok ? parsed.url.text : parsed.fault;
}

describe("PlaneUrl.parse", () => {
  it.each(ACCEPTED)("takes %j and keeps each byte", (url) => {
    expect(outcome(url)).toBe(url);
  });

  it.each(REFUSED)("refuses %j", (url, fault) => {
    expect(outcome(url)).toBe(fault);
  });

  it.each(ACCEPTED_HOSTS)("takes the host %j", (host) => {
    expect(outcome(`http://${host}:8300`)).toBe(`http://${host}:8300`);
  });

  it.each(REFUSED_HOSTS)("refuses the host %j", (host) => {
    expect(outcome(`http://${host}:8300`)).toBe("host");
  });

  it("is the only way to a value from a text", () => {
    // @ts-expect-error The constructor is private.
    const built: PlaneUrl = new PlaneUrl("http://192.0.2.10:8300/v1");
    // @ts-expect-error An object of the same shape is not a plane URL.
    const shaped: PlaneUrl = { text: "http://192.0.2.10:8300/v1" };

    expect([outcome(built.text), outcome(shaped.text)]).toEqual(["after_authority", "after_authority"]);
  });
});
