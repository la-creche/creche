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

The image holds no pi, no fd, no playpen bundle, no PEP bridge and no
`agent-pi-launch`.

The image is built on the host, from the repository root, with
`playpen/Dockerfile`. Do not build it on a development machine.

## The release

1. `bin/playpen-build` runs as root at release step 8, out of the fetched
   source. It builds the two images and pushes them to the registry of the
   host. It writes the staged tree: `images.env` and the verify hook.
2. `images.env` names each image by digest, never by tag. A tag can move.
   The phone tap approves exact bytes.
3. The build stages the tree only after both images are in the registry.
   A failed build stages nothing, and the host stays as it was.
4. The build script reads nothing outside `playpen/`. The image build also
   copies `agent/` and `facade/`, so the catalog row must bundle those two.
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
- No test builds an image. The tests give `bin/playpen-build` a stub
  `docker` (`bin/tests/test_playpen_release.py`).
- A release never removes an image from the registry (`bin/playpen-build`).
