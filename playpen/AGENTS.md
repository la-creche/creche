# playpen

The image that each root disk of the `disks` row is built from, and its
scripts. The root `AGENTS.md` applies here too.

## The image

`Dockerfile` builds the image of every family's microVM. It holds Node 24,
ripgrep and the ACP facade (`facade/AGENTS.md`) with the agent module it
imports (`agent/AGENTS.md`). One shim puts the facade's one bin on `PATH`:

| Program | Runs | The build proves |
|---|---|---|
| `creche-facade` | `node /opt/creche-facade/dist/src/bin/creche-facade.js` | exit 17 and one stderr line with no `CRECHE_SESSION_ID` |

The build stage installs `facade/` and `agent/` from the facade's frozen
lockfile, builds both and runs `pnpm deploy --prod` to `/opt/creche-facade`.
The Flue pin is the one in `facade/pnpm-lock.yaml`. The image fixes no
host address: the facade reaches the two planes on guest loopback, which
guest init splices to the host. The `python` flavor adds pandas, numpy and matplotlib.

The `python` flavor also adds a second venv, `/opt/test`. It has CPython
3.12, `pytest`, and the locked test dependencies of the Python workspace.
The workspace needs 3.12 because its code uses PEP 695 type parameters.
On `PATH`, `pytest` runs the venv. `python3` stays the 3.11 interpreter of
the analysis stack.

The `coding` flavor of `code` and `creche` is the python flavor plus uv,
pnpm through corepack, cargo with Rust 1.92.0, rustfmt and clippy, and
Chromium (CH7-1, for now). Agents install a repo's locked dependencies
through the egress proxy and run its checks. Its `SandboxFlavor` member
joined before the cut (CH79 rule 1).

Chromium is two halves, both at build time: the Debian packages one needs,
and one browser at the Playwright version the image pins. The image sets
`PLAYWRIGHT_BROWSERS_PATH` to `/opt/ms-playwright` and places that name in
`TOOL_ENV` of `agent/src/tool-env.ts`. A browser matches the Playwright that
launches it, so a repo on another version overrides the name for its own
commands and fetches the revision it wants through the egress proxy. The
root disk is read-only, so the baked browser is read there and written
nowhere.

The Python flavor ships no Python package installer. `pip`, `uv` and
`ensurepip` exist in build steps only. The test venv excludes `nodeenv`.
A dependency change for a venv is a lockfile change plus a disks release.

Python 3.11 also rides in with the node parent image. It is an accident
of the parent, not a tool of this image: it has no `pip` and it cannot
build a venv.

Each flavor's last step writes `/etc/creche/env`: one `NAME=value` line
per variable of the image config, sorted, with each `PATH` folder once.
Guest init reads it, because a root disk carries no image config (CH7-7,
CH49). The step lists the names. `bin/playpen-env-check` compares the file
with the built config in CI, so a new `ENV` name fails until it is listed.
Then node in the image places each name of the file in one list of the
agent (`agent/src/tool-env.ts`, CH49 rule 1), so a new name, the parent
image's too, fails until it sits in a list.

The base stage makes `/var/lib/creche/session`, owner root, mode 0755. Guest
init mounts the session disk there and makes no dir (CH62 rule 5). Only
this stage makes it (CH81 rule 1). `disk/Dockerfile` builds the disk
tools, and the dir reaches the disk in the flavor tar. The
other mount points (`/dev /proc /sys /tmp /run /root /var/cache /var/tmp`)
come with the parent image. The same stage fails the build on a parent
that drops a tmpfs dir. The build mounts `/dev`, `/proc` and `/sys` in each
`RUN` step, so only the disk can show them missing. `bin/playpen-disk`
reads all nine back from the disk with `debugfs`, and
`bin/tests/test_playpen_image.py` holds both lists equal to the boot plan
of guest init.

The kvm job builds the base disk from the tree of each run
(`bin/vm-smoke-setup.sh`), and `rust/crates/vmm/tests/kvm_facade.rs` boots
it under guest init. So a change to the facade, the agent or this image
gets one real facade proof on its own build (decisions CH62 rule 12,
CH80).

The image holds no pi, no fd, no playpen bundle, no PEP bridge, no
`agent-pi-launch`. Only the `coding` flavor holds a package installer.

CI builds the image from the repository root, with `playpen/Dockerfile`.
Do not build it on a development machine. CI builds each flavor on each
code change (`bin/playpen-ci`).

## The release

This tree has no release of its own. The `disks` row ships what it builds
(`disks/AGENTS.md`), and its catalog row bundles `playpen`, `agent`,
`facade`, `pyproject.toml` and `uv.lock`. So a change here moves the
`disks` tag.

1. `bin/playpen-ci` builds each flavor and its root disk twice, and checks
   that the two disks are equal. It runs in the `playpen-image` job of CI.
2. `disks/bin/playpen-release-disks` runs `bin/playpen-rootfs`,
   `bin/playpen-disk` and `bin/playpen-kernel` in the `disks` job of
   `release.yml`. CI signs each file. Root installs each signed file and
   runs no build.
3. Keep the flavor lists equal: the targets of `Dockerfile` are the
   `bin/playpen-ci` list, and that list equals `FLAVORS` of
   `disks/bin/playpen-release-disks` and `catalog.DISK_FLAVORS`.
   `bin/tests/test_playpen_image.py` reads the Dockerfile as text, and
   `bin/tests/test_playpen_release_disks.py` holds the other lists equal.
   No test builds an image.

## Layout

| File | Owns |
|---|---|
| `Dockerfile` | the image: the build stage and the flavors |
| `bin/creche-env-write`, `bin/playpen-env-check` | `/etc/creche/env` in each flavor's last step, and the CI check that it equals the image config env and that an agent list holds each name |
| `bin/playpen-ci` | the steps of the `playpen-image` CI job: build each flavor, then check it |
| `bin/playpen-rootfs` | the tar export of one flavor, the input of its root disk |
| `bin/playpen-disk`, `disk/Dockerfile` | the root disk of one flavor from its tar, in the pinned trixie stage |
| `bin/playpen-kernel` | the pinned CH guest kernel, compressed to ship like the disks |
