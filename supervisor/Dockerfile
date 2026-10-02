# agent-sandbox: the image every family's microVM runs (contract 03).
#
# It holds Node, pi at the version workbench/package.json pins, the supervisor
# bundle and the PEP bridge. Nothing else. There is no ingress into a sandbox,
# so whatever is not in this image and not reachable through the PEP or the
# family's egress allowlist does not exist for the agent.
#
# The build context is the REPO ROOT, not supervisor/, because the pi pin and
# its lock file live in workbench/. Copying those two files rather than
# restating the version is what keeps the two images on one pi. README.md
# carries the two exact build commands, which run on the host.
#
# Two targets:
#   agent-sandbox         every family
#   agent-sandbox-python  the code-sandbox family, plus pandas, numpy,
#                         matplotlib, all installed at BUILD time so a run
#                         needs no network
ARG NODE_TAG=24-bookworm

# --- The supervisor and bridge bundles --------------------------------------
# esbuild produces ONE file each, with no runtime dependency beyond Node's
# standard library, so the final stage installs nothing for either.
FROM node:${NODE_TAG} AS build
# The site's LAN address, where LiteLLM and the PEP answer. A sandbox has no
# site file, so the bundle fixes it (build.mjs): `docker build --build-arg
# AGENT_LAN_ADDRESS=...`. Without it `pnpm build` below fails.
ARG AGENT_LAN_ADDRESS
WORKDIR /app
ENV COREPACK_ENABLE_DOWNLOAD_PROMPT=0
RUN corepack enable
COPY supervisor/package.json supervisor/pnpm-lock.yaml supervisor/pnpm-workspace.yaml ./
RUN pnpm install --frozen-lockfile
COPY supervisor/tsconfig.json supervisor/build.mjs ./
COPY supervisor/src ./src
COPY supervisor/bridge ./bridge
RUN pnpm typecheck && pnpm build

# --- agent-sandbox ----------------------------------------------------------
FROM node:${NODE_TAG} AS agent-sandbox

# ripgrep backs pi's `grep` and fd backs pi's `find`. pi DOWNLOADS a missing
# one from GitHub, which a sandbox with no egress cannot do, so a granted,
# active `find` fails every call with "fd not present". Nothing else is
# added: a shell exists because the
# image has one, and whether the agent may use it is the family's `shell` flag
# in runtime.json, not a property of this image.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ripgrep ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

# fd from its own release, pinned and checked. NOT Debian's `fd-find`: bookworm
# ships 8.6.0, pi's `find` passes `--no-require-git`, and fd learned that flag
# in 9.0. With 8.6.0 in the image pi finds `fdfind`, uses it, and every call
# fails on the flag. pi 0.99.1 itself fetches
# the newest release, so the newest is what its wrapper is written against.
# The musl build is static, so it does not care which glibc the base has. The
# digest is the one GitHub publishes for the asset, and the build stops when
# the download does not match it.
ARG FD_VERSION=10.5.0
ARG FD_SHA256=761c72dc8e120d85b22292063be8a796e2eeb20eb3e4f38b8fa2343ccf3514a7
RUN curl -fsSL -o /tmp/fd.tar.gz \
      "https://github.com/sharkdp/fd/releases/download/v${FD_VERSION}/fd-v${FD_VERSION}-x86_64-unknown-linux-musl.tar.gz" \
    && echo "${FD_SHA256}  /tmp/fd.tar.gz" | sha256sum -c - \
    && tar -xzf /tmp/fd.tar.gz -C /tmp \
    && install -m 0755 "/tmp/fd-v${FD_VERSION}-x86_64-unknown-linux-musl/fd" /usr/local/bin/fd \
    && rm -rf /tmp/fd.tar.gz "/tmp/fd-v${FD_VERSION}-x86_64-unknown-linux-musl" \
    && fd --version

# pi as one npm project, the same way workbench/ installs it. package.json is
# also the file `sandbox-facts.ts` reads to report the pi version in `ready`,
# so the pin and the reported version cannot drift.
WORKDIR /opt/pi
COPY workbench/package.json workbench/package-lock.json ./
RUN npm ci --omit=dev --ignore-scripts \
    && ln -s /opt/pi/node_modules/.bin/pi /usr/local/bin/pi

# The supervisor. The shim is what the host names in `sbx exec`.
COPY --from=build /app/dist/agent-supervisor.js /opt/agent-supervisor/agent-supervisor.js
RUN printf '#!/bin/sh\nexec node /opt/agent-supervisor/agent-supervisor.js "$@"\n' \
    > /usr/local/bin/agent-supervisor \
    && chmod 0755 /usr/local/bin/agent-supervisor

# The TUI door's launcher (contract 03 §7.6). A second bundle beside the
# supervisor, not a second image: it reads the same `supervisor.env`, the same
# mounts and the same session store, and asks the same builder for pi's command
# line. The door opens it with `sbx exec -it`, so the terminal reaches pi.
COPY --from=build /app/dist/agent-pi-launch.js /opt/agent-supervisor/agent-pi-launch.js
RUN printf '#!/bin/sh\nexec node /opt/agent-supervisor/agent-pi-launch.js "$@"\n' \
    > /usr/local/bin/agent-pi-launch \
    && chmod 0755 /usr/local/bin/agent-pi-launch

# The PEP bridge. It carries no credential and no policy: it presents whatever
# the family's manifest says, so a grant changes without touching this image.
#
# Read-only, and outside every mount a session can write. `--no-extensions`
# keeps discovery off, so the only extension pi loads is the one the supervisor
# names by this path. Discovery would also read the session store, which is
# agent-writable and outlives the sandbox.
COPY --from=build /app/dist/pep-bridge.js /opt/agent-bridge/pep-bridge.js
RUN chmod 0444 /opt/agent-bridge/pep-bridge.js

# Fail the BUILD, not the operator's first channel, when a bundle cannot start.
# --sandbox is absent on purpose: index.ts then exits 2 without opening
# anything, which proves the file parses and runs under this Node. Importing
# the bridge only evaluates the module; its factory is never called, so there
# is no network and no side effect.
RUN node /opt/agent-supervisor/agent-supervisor.js; [ "$?" = "2" ]
# The launcher answers an empty argument list the same way, before it reads a
# mount or touches a session store, so this proves the bundle and nothing else.
RUN node /opt/agent-supervisor/agent-pi-launch.js; [ "$?" = "2" ]
RUN node --input-type=module \
    -e "const m = await import('/opt/agent-bridge/pep-bridge.js'); if (typeof m.default !== 'function') { throw new Error('pep bridge has no default factory'); }"

# Mount points, so a missing mount is an empty directory and not a surprise.
RUN mkdir -p /work/sessions /run/family /run/family-config /run/control

ENV NODE_ENV=production
ENV HOME=/root
ENV TMPDIR=/tmp
# The VM stays up because the host holds one long-lived `sbx exec` open
# (contract 03 §10 rule 1). This CMD only keeps PID 1 alive.
CMD ["sleep", "infinity"]

# --- agent-sandbox-python ---------------------------------------------------
# The code-sandbox family runs analysis code in a per-chat directory. Every
# wheel is installed here, at build time, so a turn needs no network.
FROM agent-sandbox AS agent-sandbox-python

# UNVERIFIED PINS. Nothing in this repository has resolved these three
# against PyPI. They are ARGs precisely so a build can correct one on the
# command line instead of editing this file:
#   --build-arg NUMPY_VERSION=<what pip resolves>
# README.md §"The image" repeats this.
ARG PANDAS_VERSION=2.3.3
ARG NUMPY_VERSION=2.3.4
ARG MATPLOTLIB_VERSION=3.10.7

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-venv \
    && rm -rf /var/lib/apt/lists/*

# A venv rather than --break-system-packages: bookworm marks its python3 as
# externally managed (PEP 668), and one prefix on PATH is simpler to reason
# about than a patched system site-packages.
RUN python3 -m venv /opt/py \
    && /opt/py/bin/pip install --no-cache-dir --upgrade pip \
    && /opt/py/bin/pip install --no-cache-dir \
    "numpy==${NUMPY_VERSION}" \
    "pandas==${PANDAS_VERSION}" \
    "matplotlib==${MATPLOTLIB_VERSION}"
ENV PATH="/opt/py/bin:${PATH}"

# Agg because no sandbox has a display, and a writable config dir because
# matplotlib builds its font cache on first import and a failure there aborts
# the import itself. Building the cache now keeps that cost off the first turn.
ENV MPLBACKEND=Agg
ENV MPLCONFIGDIR=/var/cache/matplotlib
RUN mkdir -p "${MPLCONFIGDIR}" \
    && python3 -c "import matplotlib.pyplot, pandas, numpy"

CMD ["sleep", "infinity"]
