# playpen

The sandbox image and its release scripts. The root `AGENTS.md` applies
here too.

## The image

`Dockerfile` builds the image of every family's microVM. It holds Node 24,
ripgrep and the ACP facade (`facade/AGENTS.md`) with the agent module it
imports (`agent/AGENTS.md`). Two shims put the facade's two bins on `PATH`:

| Program | Runs | The build proves |
|---|---|---|
| `creche-facade` | `node /opt/creche-facade/dist/src/bin/creche-facade.js` | exit 2 and one stderr line with no `AGENT_SANDBOX` |
| `acp-relay` | `node /opt/creche-facade/dist/src/bin/acp-relay.js` | exit 1 with no facade |

The build stage installs `facade/` and `agent/` from the facade's frozen
lockfile, builds both and runs `pnpm deploy --prod` to `/opt/creche-facade`.
The Flue pin is the one in `facade/pnpm-lock.yaml`. The image fixes no
address: a sandbox reads the two plane URLs from the env file that
`caregiver` writes. The `python` flavor adds pandas, numpy and matplotlib.

The `python` flavor also adds a second venv, `/opt/test`. It has CPython
3.12, `pytest`, and the locked test dependencies of the Python workspace.
The workspace needs 3.12 because its code uses PEP 695 type parameters.
On `PATH`, `pytest` runs the venv. `python3` stays the 3.11 interpreter of
the analysis stack.

The `coding` flavor of `code` and `creche` is the python flavor plus uv,
pnpm through corepack, and cargo with Rust 1.92.0, rustfmt and clippy
(CH7-1, for now). Agents install a repo's locked dependencies through the
egress proxy and run its checks. It ships as a root disk only (CH53
rule 1): no sbx image and no `SandboxFlavor` member.

The Python flavor ships no Python package installer. `pip`, `uv` and
`ensurepip` exist in build steps only. The test venv excludes `nodeenv`.
A dependency change for a venv is a lockfile change plus a playpen release.

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
init mounts the session disk there and makes no dir (CH62 rule 5). The
other mount points (`/dev /proc /sys /tmp /run /root /var/cache /var/tmp`)
come with the parent image, and the same stage fails the build on a parent
that drops one. `bin/playpen-disk` reads all nine back from the disk with
`debugfs`, and `bin/tests/test_playpen_image.py` holds both lists equal to
the boot plan of guest init. sbx ignores the extra empty dir.

The kvm job builds the base disk from the tree of each run
(`bin/vm-smoke-setup.sh`), and `rust/crates/vmm/tests/kvm_facade.rs` boots
it under guest init. So a change to the facade, the agent or this image
gets one real facade proof on its own build (decisions CH62 rule 12,
CH80).

The image holds no pi, no fd, no playpen bundle, no PEP bridge, no
`agent-pi-launch`. Only the `coding` flavor holds a package installer.

The image is built on the host, from the repository root, with
`playpen/Dockerfile`. Do not build it on a development machine.
CI builds each flavor on each code change (`bin/playpen-ci`).

## The release

1. `bin/playpen-build` runs as root at release step 8, out of the fetched
   source. It builds the two images and pushes them to the registry of the
   host. It writes the staged tree: `images.env` and the verify hook.
2. `images.env` names each image by digest, never by tag. A tag can move.
   The phone tap approves exact bytes.
3. The build stages the tree only after both images are in the registry.
   A failed build stages nothing, and the host stays as it was.
4. The build script reads nothing outside `playpen/`. The image build also
   copies `agent/`, `facade/` and the workspace definition (the root
   pyproject, `uv.lock` and the pyproject of every member), so the catalog
   row bundles those.
5. `bin/playpen-verify` changes nothing and reaches nothing outside the
   host. It asks the registry for each digest in `images.env`.
6. Keep the flavor lists equal: the flavors in `bin/playpen-build` and
   `SandboxFlavor` in `family/`. `bin/playpen-ci` lists those, then the
   flavors that ship as a root disk only (today `coding`). The targets of
   `Dockerfile` are the `bin/playpen-ci` list.
7. `bin/tests/test_playpen_release.py` runs both scripts against a stub
   `docker` and a stub registry. `bin/tests/test_playpen_image.py` reads the
   Dockerfile as text. No test builds an image.

## Layout

| File | Owns |
|---|---|
| `Dockerfile` | the image: the build stage and the flavors |
| `component.yaml` | the release manifest of the component |
| `bin/playpen-build`, `bin/playpen-verify`, `bin/lib/registry.sh` | the release build, the verify hook, the one registry question |
| `bin/creche-env-write`, `bin/playpen-env-check` | `/etc/creche/env` in each flavor's last step, and the CI check that it equals the image config env and that an agent list holds each name |
| `bin/playpen-ci` | the steps of the `playpen-image` CI job: build each flavor, then check it |
| `bin/playpen-rootfs` | the tar export of one flavor, the input of its root disk |
| `bin/playpen-disk`, `disk/Dockerfile` | the root disk of one flavor from its tar, in the pinned trixie stage |
| `bin/playpen-kernel` | the pinned CH guest kernel, compressed to ship like the disks |

## Known gaps

- The verify hook does not start an image. The image build runs each program
  once and stops on a program that does not load (`bin/playpen-verify`).
- A release never removes an image from the registry (`bin/playpen-build`).
