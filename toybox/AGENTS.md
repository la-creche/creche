# toybox

The one pi version pin. `package.json` and `package-lock.json` name the pi
package and its version, and nothing else. The root `AGENTS.md` applies here
too.

- `playpen/Dockerfile` copies these two files to build pi into the sandbox
  image. The Dockerfile restates no version.
- `playpen/test/real-pi.test.ts` asserts that the playpen's dev copy of pi
  matches this version. The first case in that file fails when the two
  differ.
- `playpen/src/sandbox-facts.ts` reads this file to report the pi version in
  the `ready` message.

A pi upgrade is one edit here, one edit of the dev dependency in
`playpen/package.json`, then one `playpen` release. `toybox/` builds nothing
and has no tests of its own.

The `name` and `version` fields match `package-lock.json` so that `npm ci`
accepts them. They are not an image tag.
