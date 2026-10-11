// The two planes of a sandbox: the chaperone and LiteLLM. `planes.ts` reads
// the URL of each one from the environment that the env file carries.
//
//   AGENT_PEP_URL=http://192.0.2.10:8300
//   AGENT_LITELLM_BASE_URL=http://192.0.2.10:4000
//
// A plane URL is `http://<host>:<port>` and no other byte. The Rust type
// `PlaneUrl` holds the same grammar
// (`rust/crates/creche-contracts/src/config/endpoints.rs`). `ACCEPTED` and
// `REFUSED` below hold each text of the tests of that file, so the two
// readers take and refuse the same texts.

import { describe, expect, it } from "vitest";

import { LITELLM_BASE_URL, MAX_LOG_BYTES, PEP_URL } from "../src/constants.js";
import { byteLength } from "../src/framing.js";
import {
  LITELLM_PLANE_ENV,
  PEP_PLANE_ENV,
  Plane,
  PlaneState,
  PlaneUrl,
  PlaneUrlFault,
  planeLog,
  readPlaneUrls,
} from "../src/planes.js";
import type { PlaneNotice } from "../src/planes.js";

const PEP = "http://192.0.2.20:8301";
const LITELLM = "http://192.0.2.20:4001";

const ACCEPTED: readonly string[] = [
  "http://192.0.2.10:8300",
  "http://192.0.2.10:4000",
  "http://127.0.0.1:1",
  "http://localhost:65535",
  "http://host-1.example:80",
  "http://A1:8300",
  "http://LOCALHOST:80",
];

const LONG_PORT = `http://192.0.2.10:${"9".repeat(5000)}`;

const REFUSED: readonly (readonly [string, PlaneUrlFault])[] = [
  ["", PlaneUrlFault.NotHttp],
  ["192.0.2.10:8300", PlaneUrlFault.NotHttp],
  ["https://192.0.2.10:8300", PlaneUrlFault.NotHttp],
  ["HTTP://192.0.2.10:8300", PlaneUrlFault.NotHttp],
  ["http:/192.0.2.10:8300", PlaneUrlFault.NotHttp],
  [" http://192.0.2.10:8300", PlaneUrlFault.NotHttp],
  ["http://user@192.0.2.10:8300", PlaneUrlFault.UserPart],
  ["http://user:word@192.0.2.10:8300", PlaneUrlFault.UserPart],
  ["http://user@192.0.2.10:8300/v1", PlaneUrlFault.UserPart],
  ["http://192.0.2.10:8300/", PlaneUrlFault.AfterAuthority],
  ["http://192.0.2.10:8300/v1", PlaneUrlFault.AfterAuthority],
  ["http://192.0.2.10:8300?a=b", PlaneUrlFault.AfterAuthority],
  ["http://192.0.2.10:8300#top", PlaneUrlFault.AfterAuthority],
  ["http://192.0.2.10/:8300", PlaneUrlFault.AfterAuthority],
  ["http://", PlaneUrlFault.Host],
  ["http://:8300", PlaneUrlFault.Host],
  ["http://0.0.0.0:8300", PlaneUrlFault.Host],
  ["http://192.0.2:8300", PlaneUrlFault.Host],
  ["http://[::1]:8300", PlaneUrlFault.Host],
  ["http://::1:8300", PlaneUrlFault.Host],
  ["http://my_host:8300", PlaneUrlFault.Host],
  ["http://192.0.2.10 :8300", PlaneUrlFault.Host],
  ["http://192.0.2.10:8300:8300", PlaneUrlFault.Host],
  ["http://１９２.0.2.10:8300", PlaneUrlFault.Host],
  ["http://192.0.2.10", PlaneUrlFault.NoPort],
  ["http://192.0.2.10:", PlaneUrlFault.NoPort],
  ["http://localhost", PlaneUrlFault.NoPort],
  ["http://192.0.2.10:8300\n", PlaneUrlFault.PortForm],
  ["http://192.0.2.10:８３００", PlaneUrlFault.PortForm],
  ["http://192.0.2.10:+8300", PlaneUrlFault.PortForm],
  ["http://192.0.2.10:-1", PlaneUrlFault.PortForm],
  ["http://192.0.2.10:8_300", PlaneUrlFault.PortForm],
  ["http://192.0.2.10: 8300", PlaneUrlFault.PortForm],
  ["http://192.0.2.10:8300 ", PlaneUrlFault.PortForm],
  ["http://192.0.2.10:08300", PlaneUrlFault.PortForm],
  ["http://192.0.2.10:0", PlaneUrlFault.PortForm],
  ["http://192.0.2.10:0x50", PlaneUrlFault.PortForm],
  ["http://192.0.2.10:http", PlaneUrlFault.PortForm],
  ["http://192.0.2.10:65536", PlaneUrlFault.PortRange],
  ["http://192.0.2.10:99999999999999999999", PlaneUrlFault.PortRange],
  [LONG_PORT, PlaneUrlFault.PortRange],
];

// The host of a plane URL has the grammar of the Rust type `LanAddress`
// (`rust/crates/creche-contracts/src/config/values.rs`). The two tables
// below are the tables of the tests of that type, without the two texts
// that hold a `/`. In a URL such a text is a path, so it does not reach the
// host rule.
const ACCEPTED_HOSTS: readonly string[] = [
  "192.0.2.10",
  "127.0.0.1",
  "255.255.255.255",
  "localhost",
  "host-1.example",
  "a",
  "A1",
  "1a",
  "0.a",
  "x.0y",
  "a".repeat(63),
];

const REFUSED_HOSTS: readonly string[] = [
  "",
  "192.0.2.10\n",
  " 192.0.2.10",
  "192.0.2.10:8300",
  "a..b",
  "a.",
  ".a",
  "-a",
  "a-",
  "my_host",
  "::1",
  "１９２.0.2.10",
  "a".repeat(64),
  "0.0.0.0",
  "0",
  "0.0",
  "192.0.2",
  "192.0.2.256",
  "192.0.2.010",
  "192.0.2.10.5",
  "0x0",
  "host.0x7f",
  "host.123",
];

function text(of: string): string | null {
  const parsed = PlaneUrl.parse(of);

  return parsed.ok ? parsed.url.text : null;
}

function fault(of: string): PlaneUrlFault | null {
  const parsed = PlaneUrl.parse(of);

  return parsed.ok ? null : parsed.fault;
}

describe("PlaneUrl.parse", () => {
  it.each(ACCEPTED)("takes %j and keeps each byte", (url) => {
    expect(text(url)).toBe(url);
  });

  it.each(REFUSED)("refuses %j", (url, why) => {
    expect(fault(url)).toBe(why);
  });

  it.each(ACCEPTED_HOSTS)("takes the host %j", (host) => {
    expect(text(`http://${host}:8300`)).toBe(`http://${host}:8300`);
  });

  it.each(REFUSED_HOSTS)("refuses the host %j", (host) => {
    expect(fault(`http://${host}:8300`)).toBe(PlaneUrlFault.Host);
  });

  it("reads a last label of `0x` alone as a number", () => {
    // The C resolver reads `0x` and hexadecimal digits as a number. The
    // rule takes zero digits or more after the prefix.
    expect(fault("http://0x:8300")).toBe(PlaneUrlFault.Host);
    expect(fault("http://host.0X:8300")).toBe(PlaneUrlFault.Host);
    expect(text("http://0xg:8300")).toBe("http://0xg:8300");
  });

  it("keeps the letter case of a host name", () => {
    expect(text("http://LOCALHOST:80")).not.toBe(text("http://localhost:80"));
  });

  it("is the only way to a value from a text", () => {
    // The type check is the test: `pnpm run typecheck` fails when one of the
    // two marked lines builds. At run time a class has no private member.
    // @ts-expect-error The constructor is private.
    const built: PlaneUrl = new PlaneUrl("http://192.0.2.10:8300/v1");
    // @ts-expect-error An object of the same shape is not a plane URL.
    const shaped: PlaneUrl = { text: "http://192.0.2.10:8300/v1" };

    expect(fault(built.text)).toBe(PlaneUrlFault.AfterAuthority);
    expect(fault(shaped.text)).toBe(PlaneUrlFault.AfterAuthority);
  });

  it("takes each of the two URLs of the build", () => {
    // The tests run with the example address in place of the address of an
    // image (`vitest.config.ts`).
    expect(PlaneUrl.ofBuild(Plane.Pep).text).toBe(PEP_URL);
    expect(PlaneUrl.ofBuild(Plane.Litellm).text).toBe(LITELLM_BASE_URL);
    expect(text(PEP_URL)).toBe("http://192.0.2.10:8300");
    expect(text(LITELLM_BASE_URL)).toBe("http://192.0.2.10:4000");
  });
});

describe("readPlaneUrls", () => {
  it("takes the two URLs from the environment", () => {
    const reading = readPlaneUrls({ [PEP_PLANE_ENV]: PEP, [LITELLM_PLANE_ENV]: LITELLM });

    expect(reading.urls.pep.text).toBe(PEP);
    expect(reading.urls.litellm.text).toBe(LITELLM);
    expect(reading.notices).toEqual([]);
  });

  it("names the two variables as the env file names them", () => {
    expect(PEP_PLANE_ENV).toBe("AGENT_PEP_URL");
    expect(LITELLM_PLANE_ENV).toBe("AGENT_LITELLM_BASE_URL");
  });

  it("takes the URL of the build for the chaperone when its variable is absent", () => {
    const reading = readPlaneUrls({ [LITELLM_PLANE_ENV]: LITELLM });

    expect(reading.urls.pep.text).toBe(PEP_URL);
    expect(reading.urls.litellm.text).toBe(LITELLM);
    expect(reading.notices).toEqual([{ plane: Plane.Pep, state: PlaneState.Unset }]);
  });

  it("takes the URL of the build for LiteLLM when its variable is absent", () => {
    const reading = readPlaneUrls({ [PEP_PLANE_ENV]: PEP });

    expect(reading.urls.pep.text).toBe(PEP);
    expect(reading.urls.litellm.text).toBe(LITELLM_BASE_URL);
    expect(reading.notices).toEqual([{ plane: Plane.Litellm, state: PlaneState.Unset }]);
  });

  it("reads an empty value as an absent variable", () => {
    const reading = readPlaneUrls({ [PEP_PLANE_ENV]: "", [LITELLM_PLANE_ENV]: LITELLM });

    expect(reading.urls.pep.text).toBe(PEP_URL);
    expect(reading.notices).toEqual([{ plane: Plane.Pep, state: PlaneState.Unset }]);
  });

  it("takes the URL of the build for a variable that is not valid", () => {
    const reading = readPlaneUrls({ [PEP_PLANE_ENV]: `${PEP}/`, [LITELLM_PLANE_ENV]: LITELLM });

    expect(reading.urls.pep.text).toBe(PEP_URL);
    expect(reading.urls.litellm.text).toBe(LITELLM);
    expect(reading.notices).toEqual([
      { plane: Plane.Pep, state: PlaneState.NotValid, fault: PlaneUrlFault.AfterAuthority },
    ]);
  });

  it("does not remove a space from a value", () => {
    const reading = readPlaneUrls({ [PEP_PLANE_ENV]: PEP, [LITELLM_PLANE_ENV]: ` ${LITELLM}` });

    expect(reading.urls.litellm.text).toBe(LITELLM_BASE_URL);
    expect(reading.notices).toEqual([
      { plane: Plane.Litellm, state: PlaneState.NotValid, fault: PlaneUrlFault.NotHttp },
    ]);
  });

  it("gives one notice for each variable, the chaperone first", () => {
    const reading = readPlaneUrls({ [LITELLM_PLANE_ENV]: "http://192.0.2.20" });

    expect(reading.urls.pep.text).toBe(PEP_URL);
    expect(reading.urls.litellm.text).toBe(LITELLM_BASE_URL);
    expect(reading.notices).toEqual([
      { plane: Plane.Pep, state: PlaneState.Unset },
      { plane: Plane.Litellm, state: PlaneState.NotValid, fault: PlaneUrlFault.NoPort },
    ]);
  });
});

/** Each notice that the type permits: for each plane, one absent state and each fault. */
const EACH_NOTICE: readonly PlaneNotice[] = Object.values(Plane).flatMap(
  (plane): PlaneNotice[] => [
    { plane, state: PlaneState.Unset },
    ...Object.values(PlaneUrlFault).map(
      (fault): PlaneNotice => ({ plane, state: PlaneState.NotValid, fault }),
    ),
  ],
);

describe("planeLog", () => {
  it("writes a warn line that names an absent variable", () => {
    const line = planeLog({ plane: Plane.Pep, state: PlaneState.Unset });

    expect(line.type).toBe("log");
    expect(line.level).toBe("warn");
    expect(line.session).toBeNull();
    expect(line.message).toContain(PEP_PLANE_ENV);
    expect(line.message).not.toContain(LITELLM_PLANE_ENV);
  });

  it("writes an error line that names a variable that is not valid", () => {
    const notice: PlaneNotice = {
      plane: Plane.Litellm,
      state: PlaneState.NotValid,
      fault: PlaneUrlFault.PortForm,
    };
    const line = planeLog(notice);

    expect(line.level).toBe("error");
    expect(line.session).toBeNull();
    expect(line.message).toContain(LITELLM_PLANE_ENV);
    expect(line.message).toContain(PlaneUrlFault.PortForm);
  });

  it("holds no part of a value that is not valid", () => {
    // A value can hold a user part. The line names the variable and the
    // fault, so no byte of the value reaches the channel.
    const secret = "http://operator:FIXTURE-WORD@192.0.2.20:8301";
    const reading = readPlaneUrls({ [PEP_PLANE_ENV]: secret, [LITELLM_PLANE_ENV]: LITELLM });
    const lines = reading.notices.map(planeLog);

    expect(lines).toHaveLength(1);
    expect(lines[0]?.level).toBe("error");
    expect(lines[0]?.message).toContain(PEP_PLANE_ENV);
    expect(JSON.stringify(lines)).not.toContain("FIXTURE-WORD");
    expect(JSON.stringify(lines)).not.toContain("192.0.2.20");
    expect(JSON.stringify(reading.notices)).not.toContain("FIXTURE-WORD");
  });

  it.each(EACH_NOTICE)("stays inside the cap of a log message for %j", (notice) => {
    expect(byteLength(planeLog(notice).message)).toBeLessThanOrEqual(MAX_LOG_BYTES);
  });

  it("takes a plane of the closed set, so a notice has no place for a value", () => {
    // The type check is the test: `pnpm run typecheck` fails when the marked
    // line builds.
    const value = "http://operator:FIXTURE-WORD@192.0.2.20:8301";
    // @ts-expect-error A notice names a plane, not a text.
    const line = planeLog({ plane: value, state: PlaneState.Unset });

    expect(line.message).not.toContain("FIXTURE-WORD");
  });
});
