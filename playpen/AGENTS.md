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

The Python flavor ships no Python package installer. `pip`, `uv` and
`ensurepip` exist in build steps only. The test venv excludes `nodeenv`.
A dependency change for a venv is a lockfile change plus a playpen release.

Python 3.11 also rides in with the node parent image. It is an accident
of the parent, not a tool of this image: it has no `pip` and it cannot
build a venv.

The image holds no pi, no fd, no playpen bundle, no PEP bridge, no
`agent-pi-launch` and no Python package installer.

The image is built on the host, from the repository root, with
`playpen/Dockerfile`. Do not build it on a development machine.
CI builds the full Python flavor on each code change.

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
6. Keep three lists equal: the targets of `Dockerfile`, the flavors in
   `bin/playpen-build`, and `SandboxFlavor` in `family/`.
7. `bin/tests/test_playpen_release.py` runs both scripts against a stub
   `docker` and a stub registry. `bin/tests/test_playpen_image.py` reads the
   Dockerfile as text. No test builds an image.

## Layout

| File | Owns |
|---|---|
| `Dockerfile` | the image: the build stage and the flavors |
| `component.yaml` | the release manifest of the component |
| `bin/playpen-build`, `bin/playpen-verify`, `bin/lib/registry.sh` | the release build, the verify hook, the one registry question |

## Known gaps

- The verify hook does not start an image. The image build runs each program
  once and stops on a program that does not load (`bin/playpen-verify`).
- A release never removes an image from the registry (`bin/playpen-build`).
