# agent-sandbox: the image every family's microVM runs.
#
# It holds Node, ripgrep and the ACP facade with the agent module it
# imports: `creche-facade` and `acp-relay` on PATH. Nothing else. There is no
# ingress into a sandbox, so whatever is not in this image and not reachable
# through the chaperone or the family's egress allowlist does not exist for
# the agent.
#
# The build context is the REPO ROOT, not playpen/, because the facade and
# the agent module are two sibling directories, `facade/` and `agent/`. The
# image installs them from the facade's lockfile, so the Flue pin is the one
# that `facade/pnpm-lock.yaml` holds and no second copy exists.
#
# Two targets:
#   agent-sandbox         every family
#   agent-sandbox-python  the code-sandbox family, plus pandas, numpy,
#                         matplotlib, all installed at BUILD time so a run
#                         needs no network
ARG NODE_TAG=24-bookworm

# --- The facade, deployed with its production dependencies ------------------
# facade/pnpm-workspace.yaml names `../agent`, so one install in facade/
# installs both, and `pnpm run build` builds agent/ first. The two
# directories sit side by side here as they do in the repository.
FROM node:${NODE_TAG} AS build
ENV COREPACK_ENABLE_DOWNLOAD_PROMPT=0
RUN corepack enable
WORKDIR /src/facade
COPY facade/package.json facade/pnpm-lock.yaml facade/pnpm-workspace.yaml ./
COPY agent/package.json /src/agent/
RUN pnpm install --frozen-lockfile
COPY agent/tsconfig.json agent/tsconfig.build.json /src/agent/
COPY agent/src /src/agent/src
COPY facade/tsconfig.json facade/tsconfig.build.json ./
COPY facade/src ./src
COPY facade/vectors ./vectors
# `pnpm deploy` copies the facade and the agent module it imports into one
# tree with no dev dependency. pnpm 10 and later deploy only with injected
# workspace packages. The flag sets that for this one command, so the
# lockfile and the development install keep their links.
RUN pnpm run build \
    && pnpm --filter facade --config.inject-workspace-packages=true \
       deploy --prod --frozen-lockfile /opt/creche-facade

# --- agent-sandbox ----------------------------------------------------------
FROM node:${NODE_TAG} AS agent-sandbox

# Flue's `grep` runs ripgrep when `rg --version` answers, else grep. Its
# `glob` runs find. Nothing else is added: a shell exists because the image
# has one, and whether the agent may use it is the family's grant, not a
# property of this image.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ripgrep ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# The facade. Each shim is a program that the host names in `sbx exec`.
COPY --from=build /opt/creche-facade /opt/creche-facade
RUN for name in creche-facade acp-relay; do \
      printf '#!/bin/sh\nexec node /opt/creche-facade/dist/src/bin/%s.js "$@"\n' "$name" \
        > "/usr/local/bin/$name" \
      && chmod 0755 "/usr/local/bin/$name"; \
    done

ENV NODE_ENV=production
ENV HOME=/root
ENV TMPDIR=/tmp

# Fail the BUILD, not the first switch, when a program cannot start. The four
# mount variables name directories that do not exist, and AGENT_SANDBOX is
# absent: the facade then exits 2 and writes exactly one stderr line,
# `creche-facade: sandbox_unset`, before it opens anything. The build fails
# on any other status or any other stderr text, an empty one included.
# That proves the tree loads under this Node.
RUN err="$(AGENT_SESSIONS_DIR=/nonexistent/sessions AGENT_CRED_DIR=/nonexistent/creds \
      AGENT_FAMILY_CONFIG_DIR=/nonexistent/config AGENT_CONTROL_DIR=/nonexistent/control \
      creche-facade </dev/null 2>&1 >/dev/null)"; \
    status=$?; \
    if [ "$status" -ne 2 ] || [ "$err" != "creche-facade: sandbox_unset" ]; then \
      printf 'creche-facade: exit %s, stderr:\n%s\n' "$status" "$err" >&2; exit 1; \
    fi
# No facade listens in a build, so the relay exits 1.
RUN acp-relay </dev/null; [ "$?" = "1" ]

# The VM stays up because the host holds one long-lived `sbx exec` open.
# This CMD only keeps PID 1 alive.
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
