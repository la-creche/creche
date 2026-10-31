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
# Two image targets and one build-only stage:
#   agent-sandbox         every family
#   agent-sandbox-python  the python flavor: pandas, numpy and matplotlib
#                         for the analysis stack, plus the 3.12 test venv
#                         that runs this repository's own test suites. All
#                         installed at BUILD time, so a run needs no network
#   creche-test-venv      build-only: it resolves uv.lock and installs the
#                         test venv and the 3.12 interpreter. It ships in
#                         no image, and neither does uv
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
# The facade's patch of Flue: the install applies it (facade/AGENTS.md rule 8).
COPY facade/patches ./patches
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

# Python 3.11 rides in with the node parent image (buildpack-deps). It is an
# accident of the parent, not a tool of this image: it has no pip and it
# cannot build a venv. A family that needs Python asks for the `python`
# flavor (contract 01 §3.9).

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

# --- creche-test-venv -------------------------------------------------------
# Build-only stage. It resolves the Python workspace of this repository
# from uv.lock and installs its test environment into /opt/test, on
# CPython 3.12. The workspace needs 3.12: its code uses PEP 695 type
# parameters, which 3.11 cannot read.
#
# The pins are the pins of uv.lock, the same file the host's test
# environment uses. A dependency change for the test venv is a lockfile
# change plus a playpen release, never an install inside a sandbox.
ARG UV_VERSION=0.13.6
FROM python:3.12-slim-bookworm AS creche-test-venv
ARG UV_VERSION

# uv exists in this stage only. No image ships it.
RUN pip install --no-cache-dir "uv==${UV_VERSION}"

WORKDIR /src
# The workspace definition: the root pyproject names the members, and each
# member's pyproject carries its dependencies. The source stays out: a
# test imports the code from the clone under test, never from the image.
COPY pyproject.toml uv.lock ./
COPY library/pyproject.toml library/
COPY family/pyproject.toml family/
COPY caregiver/pyproject.toml caregiver/
COPY attendance/pyproject.toml attendance/
COPY door-trigger/pyproject.toml door-trigger/
COPY door-acp/pyproject.toml door-acp/
COPY noticeboard/pyproject.toml noticeboard/
COPY handover/pyproject.toml handover/
COPY env/pyproject.toml env/

# --locked installs the locked versions and hashes, and refuses a lockfile
# that the workspace definition has outrun: a dependency change without a
# lockfile change fails this build. --no-install-workspace installs no
# member of the workspace: the venv holds the third-party dependencies and
# the dev group, nothing else.
RUN UV_PROJECT_ENVIRONMENT=/opt/test uv sync --locked --all-groups --no-install-workspace

# One wrapper per console script of the workspace members. A wrapper runs
# the entry point of its name, and the entry point comes from the clone
# that the session's path file names, so the wrapper of the image runs the
# code of the tree under test. A host run finds the same names in its own
# `.venv`, which `uv sync` fills the same way.
RUN printf '%s\n' \
    'index-scope=library.__main__:main' \
    'agent-family=agent_family.cli:main' \
    'caregiver=caregiver.cli:main' \
    'caregiver-verify=caregiver.verify:main' \
    'attendance-verify=attendance.verify:main' \
    'agent-trigger=agent_door_trigger.cli:main' \
    'agent-door-acp=agent_door_acp.__main__:main' \
    'creche-acp-connect=agent_door_acp.connect:main' \
    'noticeboard=noticeboard.__main__:main' \
    'noticeboard-verify=noticeboard.verify:main' \
    'handover=handover.cli:main' \
    'handover-exec=handover.executor.drain:main' \
    'creche-env=creche_env.cli:main' \
    | while IFS== read -r name entry; do \
        printf '#!/bin/sh\nexec /opt/test/bin/python -c "from %s import %s as f; raise SystemExit(f())" "$@"\n' \
          "${entry%%:*}" "${entry#*:}" > "/opt/test/bin/$name" \
          && chmod 0755 "/opt/test/bin/$name" \
          || exit 1; \
      done

# The interpreter ships without an installer. pip and its friends leave
# the site-packages of the stage, and ensurepip leaves the stdlib, so no
# venv of the image can rebuild pip from its bundled wheels either.
RUN rm -rf /usr/local/lib/python3.12/site-packages/pip* \
           /usr/local/lib/python3.12/site-packages/setuptools* \
           /usr/local/lib/python3.12/site-packages/wheel* \
           /usr/local/lib/python3.12/ensurepip \
    && /opt/test/bin/pytest --version

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
# about than a patched system site-packages. The venv ships with no pip:
# the same RUN installs the wheels and removes the installer, so no layer
# of the image holds one. A sandbox runs code; a sandbox installs nothing.
RUN python3 -m venv /opt/py \
    && /opt/py/bin/pip install --no-cache-dir \
    "numpy==${NUMPY_VERSION}" \
    "pandas==${PANDAS_VERSION}" \
    "matplotlib==${MATPLOTLIB_VERSION}" \
    && /opt/py/bin/pip uninstall -y pip \
    && [ ! -e /opt/py/bin/pip ]
ENV PATH="/opt/py/bin:${PATH}"

# Agg because no sandbox has a display, and a writable config dir because
# matplotlib builds its font cache on first import and a failure there aborts
# the import itself. Building the cache now keeps that cost off the first turn.
ENV MPLBACKEND=Agg
ENV MPLCONFIGDIR=/var/cache/matplotlib
RUN mkdir -p "${MPLCONFIGDIR}" \
    && python3 -c "import matplotlib.pyplot, pandas, numpy"

# --- The test venv of this repository's own workspace -----------------------
# A second venv, /opt/test, holds CPython 3.12 and the locked test
# dependencies of this repository: pytest, its plugins, and the third-party
# dependencies the suites import. The creche-test-venv stage built it and
# stripped the interpreter it needs. `/opt/test/bin/pytest` runs it; on
# PATH, `pytest` finds it and `python3` stays the 3.11 analysis venv above.
#
# The copied files are the ones the interpreter needs: the binary, its
# stdlib and shared library, the OpenSSL libraries it links, and the
# SQLite runtime library of the stdlib module. The loader cache learns
# them in the self-test below.
COPY --from=creche-test-venv /usr/local/bin/python3.12 /usr/local/bin/
COPY --from=creche-test-venv /usr/local/lib/python3.12 /usr/local/lib/python3.12
COPY --from=creche-test-venv /usr/local/lib/libpython3.12.so.1.0 /usr/local/lib/
COPY --from=creche-test-venv /usr/local/lib/libssl.so.3 /usr/local/lib/
COPY --from=creche-test-venv /usr/local/lib/libcrypto.so.3 /usr/local/lib/
COPY --from=creche-test-venv /usr/lib/x86_64-linux-gnu/libsqlite3.so.0 \
     /usr/lib/x86_64-linux-gnu/
COPY --from=creche-test-venv /opt/test /opt/test
ENV PATH="/opt/py/bin:/opt/test/bin:${PATH}"

# Fail the BUILD, not the first switch: pytest loads, the interpreter
# imports its two most load-bearing extension modules, a wrapper's entry
# point imports, and no installer ships in the image. `python3.12 -m pip`
# must fail, because pip is gone from the stdlib's site-packages and
# ensurepip is gone from the stdlib.
RUN ldconfig \
    && /opt/test/bin/pytest --version \
    && /usr/local/bin/python3.12 -c "import sqlite3, ssl" \
    && /opt/test/bin/python -c "from creche_env.cli import main" \
    && if /usr/local/bin/python3.12 -m pip --version >/dev/null 2>&1; then \
         echo "the image ships pip in /usr/local" >&2; exit 1; \
       fi \
    && if [ -e /opt/test/bin/pip ]; then \
         echo "the test venv ships pip" >&2; exit 1; \
       fi

CMD ["sleep", "infinity"]
