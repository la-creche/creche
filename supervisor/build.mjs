// Bundle one entry point with the site's LAN address fixed in it.
//
//   AGENT_LAN_ADDRESS=192.0.2.10 node build.mjs src/index.ts dist/agent-supervisor.js
//
// The supervisor runs inside a sandbox, with no site file and no such
// environment, and contract 03 §7 fixes LiteLLM's and the PEP's URLs for the
// life of an image. So the address is fixed here, when the image is built, as
// the compile-time constant `__AGENT_LAN_ADDRESS__` (src/constants.ts). A build
// without it fails: a default would be somebody's host.
//
// A script and not an esbuild flag in package.json: the flag would need the
// value quoted twice, once for JSON and once for the shell.
import { build } from "esbuild";

const VARIABLE = "AGENT_LAN_ADDRESS";
const EXIT_USAGE = 2;
const EXIT_NO_ADDRESS = 1;

const [entry, outfile] = process.argv.slice(2);
if (!entry || !outfile) {
  process.stderr.write("usage: node build.mjs <entry> <outfile>\n");
  process.exit(EXIT_USAGE);
}

const address = (process.env[VARIABLE] ?? "").trim();
if (!address) {
  process.stderr.write(
    `build.mjs: ${VARIABLE} is not set. Set it to the host's LAN address ` +
      "(/etc/agent-control/site.env); the image cannot read it at run time.\n",
  );
  process.exit(EXIT_NO_ADDRESS);
}

await build({
  entryPoints: [entry],
  bundle: true,
  platform: "node",
  target: "node24",
  format: "esm",
  outfile,
  define: { __AGENT_LAN_ADDRESS__: JSON.stringify(address) },
});
