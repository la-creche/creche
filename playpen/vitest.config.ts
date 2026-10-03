// The tests run with the example site's address (TEST-NET-1, RFC 5737) in
// place of the one `build.mjs` fixes in an image, so they need no
// environment.
import { defineConfig } from "vitest/config";

export default defineConfig({
  define: { __AGENT_LAN_ADDRESS__: JSON.stringify("192.0.2.10") },
});
