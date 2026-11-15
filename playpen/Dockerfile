# agent-sandbox: the image every family's microVM runs.
#
# It holds Node, ripgrep and the ACP facade with the agent module it
# imports: `creche-facade` on PATH. Nothing else. There is no
# ingress into a sandbox, so whatever is not in this image and not reachable
# through the chaperone or the family's egress allowlist does not exist for
# the agent.
#
# The build context is the REPO ROOT, not playpen/, because the facade and
# the agent module are two sibling directories, `facade/` and `agent/`. The
# image installs them from the facade's lockfile, so the Flue pin is the one
# that `facade/pnpm-lock.yaml` holds and no second copy exists.
#
# Four image targets and one build-only stage:
#   agent-sandbox         every family
#   agent-sandbox-python  the python flavor: pandas, numpy and matplotlib
#                         for the analysis stack, plus the 3.14 test venv
#                         that runs this repository's own test suites. All
#                         installed at BUILD time, so a run needs no network
#   agent-sandbox-coding  the coding flavor of code and creche (CH7-1): the
#                         python flavor plus uv, pnpm, cargo and the git
#                         platform spool helper
#   service-browser       not a family flavor: the root disk of the browser
#                         service VM (ServiceImage::Browser), with Chrome,
#                         agent-browser and creche-browser-facade
#   creche-test-venv      build-only: it resolves uv.lock and installs the
#                         test venv and the 3.14 interpreter. It ships in
#                         no image. Only the coding flavor copies its uv
ARG NODE_IMAGE=node:24-bookworm@sha256:e7add2a490bb17d97b485b8641bd88455fff7839ade0f5eba493c3508124d413
ARG PYTHON_IMAGE=python:3.14-slim-bookworm@sha256:3fe1a0b6c2d6b2ebb572906cdabeca75aeb558c0c17db5ffefbcf8094871c32b
ARG UV_VERSION=0.13.6
ARG UV_WHEEL_SHA256=22759b8895e1103578de6fb4cd27e94b4399d021023378d29f01333717751fba

# --- The facade, deployed with its production dependencies ------------------
# facade/pnpm-workspace.yaml names `../agent`, so one install in facade/
# installs both, and `pnpm run build` builds agent/ first. The two
# directories sit side by side here as they do in the repository.
FROM ${NODE_IMAGE} AS build
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
FROM ${NODE_IMAGE} AS agent-sandbox

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

# The facade. guest init runs the shim at `FACADE_PROGRAM`
# (rust/crates/wire/src/guest.rs).
COPY --from=build /opt/creche-facade /opt/creche-facade
RUN printf '#!/bin/sh\nexec node /opt/creche-facade/dist/src/bin/creche-facade.js "$@"\n' \
      > /usr/local/bin/creche-facade \
    && chmod 0755 /usr/local/bin/creche-facade

# The mount points guest init needs on the read-only root disk (CH62
# rules 4 and 5). It mounts the session disk at /var/lib/creche/session
# and makes no dir itself. The dir is root:root 0755, as guest init makes
# a missing dir. A python and a coding image inherit it from this stage.
# Only this file makes it (CH81 rule 1): playpen/disk/Dockerfile builds
# the disk tools, and the dir reaches the disk in the flavor tar.
# The tmpfs targets (/tmp /run /root /var/cache /var/tmp) come with the
# parent image, and the loop fails the build on a parent that drops one.
# The build mounts /dev, /proc and /sys in each RUN step, so this loop
# cannot see them. playpen-disk reads all nine mount points back from the
# disk, the kernel ones too.
RUN mkdir -p -m 0755 /var/lib/creche/session \
    && for dir in /tmp /run /root /var/cache /var/tmp; do \
         [ -d "$dir" ] || { echo "no mount point $dir" >&2; exit 1; }; \
       done

ENV NODE_ENV=production
ENV HOME=/root
ENV TMPDIR=/tmp

# Fail the BUILD, not the first start, when the facade cannot start. With no
# CRECHE_SESSION_ID the facade exits 17 and writes exactly one stderr line,
# `creche-facade: CRECHE_SESSION_ID is not set`, before it opens anything.
# The build fails on any other status or any other stderr text, an empty
# one included. That proves the tree loads under this Node.
RUN err="$(creche-facade </dev/null 2>&1 >/dev/null)"; \
    status=$?; \
    if [ "$status" -ne 17 ] || [ "$err" != "creche-facade: CRECHE_SESSION_ID is not set" ]; then \
      printf 'creche-facade: exit %s, stderr:\n%s\n' "$status" "$err" >&2; exit 1; \
    fi

# The image env for guest init, because a root disk carries no image config
# (CH7-7, CH49): playpen/bin/creche-env-write. The list names every variable
# of the image config, the parent image's too. CI compares the file with the
# built config (playpen/bin/playpen-env-check), so a new ENV name fails there
# until it is listed. This RUN stays the last step of the flavor.
RUN --mount=type=bind,source=playpen/bin/creche-env-write,target=/run/creche-env-write \
    mkdir -p /etc/creche \
    && sh /run/creche-env-write HOME NODE_ENV NODE_VERSION PATH TMPDIR YARN_VERSION \
       > /etc/creche/env

# No CMD (fact 59). A Cloud Hypervisor guest boots guest init as PID 1 from a
# root disk that carries no image config. The node parent's CMD, `node`,
# stays in the config, and the guest does not read it.

# --- creche-test-venv -------------------------------------------------------
# Build-only stage. It resolves the Python workspace of this repository
# from uv.lock and installs its test environment into /opt/test, on
# CPython 3.14. The workspace needs 3.14: its `requires-python` names
# the Python of the host.
#
# The pins are the pins of uv.lock, the same file the host's test
# environment uses. A dependency change for the test venv is a lockfile
# change plus a disks release, never an install inside a sandbox.
FROM ${PYTHON_IMAGE} AS creche-test-venv
ARG UV_VERSION
ARG UV_WHEEL_SHA256

# Download one wheel for the amd64 image. Check its content before pip reads
# it. uv exists in this stage only. No image ships it.
RUN uv_wheel="/tmp/uv-${UV_VERSION}-py3-none-manylinux_2_17_x86_64.manylinux2014_x86_64.whl" \
    && pip download --no-deps --only-binary=:all: --dest /tmp "uv==${UV_VERSION}" \
    && printf '%s  %s\n' "${UV_WHEEL_SHA256}" "${uv_wheel}" | sha256sum -c - \
    && pip install --no-cache-dir --no-deps "${uv_wheel}" \
    && rm -f "${uv_wheel}"

WORKDIR /src
# The workspace definition: the root pyproject names the members, and each
# member's pyproject carries its dependencies. The source stays out: a
# test imports the code from the clone under test, never from the image.
COPY pyproject.toml uv.lock ./
COPY family/pyproject.toml family/
COPY caregiver/pyproject.toml caregiver/
COPY attendance/pyproject.toml attendance/
COPY door-trigger/pyproject.toml door-trigger/
COPY door-acp/pyproject.toml door-acp/
COPY noticeboard/pyproject.toml noticeboard/
COPY handover/pyproject.toml handover/
COPY env/pyproject.toml env/
COPY wire/pyproject.toml wire/

# --locked installs the locked versions and hashes, and refuses a lockfile
# that the workspace definition has outrun. --no-install-workspace installs
# no member of the workspace. The sandbox-test group excludes lint tools and
# nodeenv, so the venv holds no installer for another runtime.
RUN UV_PROJECT_ENVIRONMENT=/opt/test uv sync --locked --no-build --no-default-groups --group sandbox-test --no-install-workspace

# One wrapper per console script of the workspace members. A wrapper runs
# the entry point of its name, and the entry point comes from the clone
# that the session's path file names, so the wrapper of the image runs the
# code of the tree under test. A host run finds the same names in its own
# `.venv`, which `uv sync` fills the same way.
RUN printf '%s\n' \
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
        printf '#!/bin/sh\nexec /opt/test/bin/python -P -c "from %s import %s as f; raise SystemExit(f())" "$@"\n' \
          "${entry%%:*}" "${entry#*:}" > "/opt/test/bin/$name" \
          && chmod 0755 "/opt/test/bin/$name" \
          || exit 1; \
      done

# The interpreter ships without an installer. pip and its friends leave
# the site-packages of the stage, and ensurepip leaves the stdlib, so no
# venv of the image can rebuild pip from its bundled wheels either.
RUN rm -rf /usr/local/lib/python3.14/site-packages/pip* \
           /usr/local/lib/python3.14/site-packages/setuptools* \
           /usr/local/lib/python3.14/site-packages/uv* \
           /usr/local/lib/python3.14/site-packages/wheel* \
           /usr/local/lib/python3.14/ensurepip \
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

# A venv rather than --break-system-packages: bookworm marks its python3 as
# externally managed (PEP 668), and one prefix on PATH is simpler to reason
# about than a patched system site-packages. The venv ships with no pip:
# the same RUN installs the venv, removes its pip, and removes Debian's
# ensurepip and seed wheels. No layer of the image holds an installer.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       python3 python3-venv libssl3 libsqlite3-0 \
    && python3 -m venv /opt/py \
    && /opt/py/bin/pip install --no-cache-dir \
    "numpy==${NUMPY_VERSION}" \
    "pandas==${PANDAS_VERSION}" \
    "matplotlib==${MATPLOTLIB_VERSION}" \
    && /opt/py/bin/pip uninstall -y pip \
    && apt-get purge -y --auto-remove \
       python3-venv python3.11-venv \
       python3-pip-whl python3-setuptools-whl \
    && rm -rf /var/lib/apt/lists/* \
              /usr/lib/python3.11/ensurepip \
              /usr/share/python-wheels \
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
# A second venv, /opt/test, holds CPython 3.14 and the locked test
# dependencies of this repository: pytest, its plugins, and the third-party
# dependencies the suites import. The creche-test-venv stage built it and
# stripped the interpreter it needs. `/opt/test/bin/pytest` runs it; on
# PATH, `pytest` finds it and `python3` stays the 3.11 analysis venv above.
#
# Copy only files that the Python image owns below /usr/local. Keep the two
# interpreter links that the venv uses. The final stage installs its Debian
# runtime libraries above. The self-test updates the loader cache.
COPY --from=creche-test-venv /usr/local/bin/python /usr/local/bin/
COPY --from=creche-test-venv /usr/local/bin/python3 /usr/local/bin/
COPY --from=creche-test-venv /usr/local/bin/python3.14 /usr/local/bin/
COPY --from=creche-test-venv /usr/local/lib/python3.14 /usr/local/lib/python3.14
COPY --from=creche-test-venv /usr/local/lib/libpython3.14.so.1.0 /usr/local/lib/
COPY --from=creche-test-venv /opt/test /opt/test
ENV PATH="/opt/py/bin:/opt/test/bin:${PATH}"

# Fail the BUILD, not the first switch: pytest loads, the interpreter
# imports its two most load-bearing extension modules, each wrapper ignores
# the current directory for imports, and no Python installer ships in the
# image. pip, uv, ensurepip and nodeenv must all stay unavailable.
RUN ldconfig \
    && /opt/test/bin/pytest --version \
    && /usr/local/bin/python3.14 -c "import sqlite3, ssl" \
    && grep -F 'exec /opt/test/bin/python -P -c' /opt/test/bin/creche-env >/dev/null \
    && if /usr/local/bin/python3.14 -m pip --version >/dev/null 2>&1; then \
         echo "the image ships pip in /usr/local" >&2; exit 1; \
       fi \
    && if /usr/local/bin/python3.14 -m uv --version >/dev/null 2>&1; then \
         echo "the image ships uv in /usr/local" >&2; exit 1; \
       fi \
    && if [ -e /opt/test/bin/pip ]; then \
         echo "the test venv ships pip" >&2; exit 1; \
       fi \
    && if python3 -m ensurepip --version >/dev/null 2>&1; then \
         echo "the analysis interpreter ships ensurepip" >&2; exit 1; \
       fi \
    && if command -v nodeenv >/dev/null 2>&1; then \
         echo "the test venv ships nodeenv" >&2; exit 1; \
       fi

# The image env for guest init, as in agent-sandbox, with the names this
# flavor adds. This RUN stays the last step of the flavor.
RUN --mount=type=bind,source=playpen/bin/creche-env-write,target=/run/creche-env-write \
    sh /run/creche-env-write HOME MPLBACKEND MPLCONFIGDIR NODE_ENV NODE_VERSION PATH \
       TMPDIR YARN_VERSION > /etc/creche/env

# --- agent-sandbox-coding ---------------------------------------------------
# The coding flavor of code and creche (CH7-1, for now). It is the python
# flavor plus the installers that a repo's checks need: uv, pnpm through
# corepack, and cargo with Rust 1.92.0, rustfmt and clippy. Python 3.14 is
# the python flavor's. Agents install a repo's locked dependencies through
# the egress proxy (CH6-1), then run its checks.
#
# This flavor ALONE breaks the no-installer rule of the python flavor. Every
# other flavor keeps it. "For now": revisit with the mirror trigger of
# CH6-1.
FROM agent-sandbox-python AS agent-sandbox-coding
ARG RUST_VERSION=1.92.0
ARG RUST_SHA256=d2ccef59dd9f7439f2c694948069f789a044dc1addcc0803613232af8f88ee0c
ARG PNPM_VERSION=11.20.0

# uv: the binary that the creche-test-venv stage installed from the pinned
# wheel. It needs no Python of its own.
COPY --from=creche-test-venv /usr/local/bin/uv /usr/local/bin/uv

# Rust from the standalone installer, pinned by its SHA-256, into
# /usr/local. No rustup: the toolchain is the one version, and no ENV names a
# rustup or cargo home.
RUN tarball="rust-${RUST_VERSION}-x86_64-unknown-linux-gnu" \
    && curl --proto '=https' --tlsv1.2 -fsSL -o "/tmp/${tarball}.tar.xz" \
       "https://static.rust-lang.org/dist/${tarball}.tar.xz" \
    && printf '%s  %s\n' "${RUST_SHA256}" "/tmp/${tarball}.tar.xz" | sha256sum -c - \
    && tar -xJf "/tmp/${tarball}.tar.xz" -C /tmp \
    && "/tmp/${tarball}/install.sh" --prefix=/usr/local --disable-ldconfig \
       --components=rustc,cargo,rust-std-x86_64-unknown-linux-gnu,rustfmt-preview,clippy-preview \
    && rm -rf "/tmp/${tarball}" "/tmp/${tarball}.tar.xz" /usr/local/share/doc/rust

# pnpm through corepack, with the version of facade/package.json in its
# cache, so a repo on that version starts with no download.
RUN corepack enable pnpm \
    && corepack prepare "pnpm@${PNPM_VERSION}" --activate

# The guest end of the git platform spool (CH3, phase 12): an agent moves
# a branch as a git bundle through guest 127.0.0.1:1105, and the
# git-platform tools push and fetch it. It runs on python3.14 with -I, so a
# module in the repo under work never loads. playpen/AGENTS.md has the flow.
# The mode is set by chmod, not by a COPY flag: the bundles test of
# handover reads each COPY word as a copied path.
COPY playpen/guest/creche_git_spool.py /usr/local/bin/creche-git-spool
RUN chmod 0755 /usr/local/bin/creche-git-spool

# Fail the BUILD, not the first switch: each installer starts, and so does
# the spool helper.
RUN uv --version \
    && cargo --version && rustc --version \
    && cargo fmt --version && cargo clippy --version \
    && pnpm --version \
    && python3.14 --version \
    && creche-git-spool --help >/dev/null

# The egress proxy of the tools: guest init bridges guest 127.0.0.1:3128 to
# the host proxy (CH46 rule 1). No tool reads this name. The agent gives its
# value to each tool as HTTPS_PROXY and https_proxy, and as nothing else
# (CH46 rules 2 and 3, CH49 rule 1). agent/test/tool-env.test.ts holds the
# port equal to creche_wire::guest::EGRESS_PROXY_PORT.
ENV CRECHE_TOOL_HTTPS_PROXY=http://127.0.0.1:3128

RUN --mount=type=bind,source=playpen/bin/creche-env-write,target=/run/creche-env-write \
    sh /run/creche-env-write CRECHE_TOOL_HTTPS_PROXY HOME MPLBACKEND MPLCONFIGDIR NODE_ENV \
       NODE_VERSION PATH TMPDIR YARN_VERSION > /etc/creche/env

# --- service-browser: the root disk of the browser service VM ---------------
# Not a family flavor: `ServiceImage::Browser` of the wire, `browser` of the
# disks row (browser design note, section 10). The vmm boots it for the
# `browser` MCP upstream. Guest init starts creche-browser-facade at the
# facade path, and the facade runs agent-browser and Chrome as the user
# `browser`, with Chrome's own sandbox on. The guest has no network device:
# Chrome reaches the web only through the egress row, guest 127.0.0.1:3128,
# and the vmm checks each name there.
FROM ${PYTHON_IMAGE} AS service-browser
ARG AGENT_BROWSER_VERSION=0.43.0
ARG AGENT_BROWSER_SHA256=8e69c9fb662514c9cf36e4911e94e913742ba066e55102486350e049dd847612
# The fingerprint of Google's Linux package signing key. A key with any
# other fingerprint fails the build.
ARG GOOGLE_SIGNER_FPR=EB4C1BFD4F042F6DDDCCEC917721F63BD38B4796

# Chrome comes from Google's signed apt repo at BUILD time. Chrome for
# Testing publishes no sha256 (decision B5), so a new Chrome stable reaches
# the host as a new disks release (decision V3).
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl gnupg procps \
    && curl -fsSL -o /tmp/google.pub https://dl.google.com/linux/linux_signing_key.pub \
    && gpg --show-keys --with-colons /tmp/google.pub | grep -qx "fpr:::::::::${GOOGLE_SIGNER_FPR}:" \
    && install -d -m 0755 /etc/apt/keyrings \
    && gpg --dearmor -o /etc/apt/keyrings/google-chrome.gpg /tmp/google.pub \
    && rm -f /tmp/google.pub \
    && echo 'deb [arch=amd64 signed-by=/etc/apt/keyrings/google-chrome.gpg] https://dl.google.com/linux/chrome/deb/ stable main' \
       > /etc/apt/sources.list.d/google-chrome.list \
    && apt-get update && apt-get install -y --no-install-recommends google-chrome-stable \
    && apt-get purge -y gnupg && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

# Chrome starts its sandbox from user namespaces alone. The setuid helper
# goes, so no setuid file of Chrome stays in the image.
RUN chmod u-s /opt/google/chrome/chrome-sandbox

# agent-browser, the bare release asset at its GitHub digest.
RUN curl -fsSL -o /usr/local/bin/agent-browser \
      "https://github.com/vercel-labs/agent-browser/releases/download/v${AGENT_BROWSER_VERSION}/agent-browser-linux-x64" \
    && echo "${AGENT_BROWSER_SHA256}  /usr/local/bin/agent-browser" | sha256sum -c - \
    && chmod 0755 /usr/local/bin/agent-browser

# The mount points of guest init, as the agent-sandbox stage makes them
# (CH62 rules 4 and 5, CH81 rule 1). playpen-disk reads them back.
RUN mkdir -p -m 0755 /var/lib/creche/session \
    && for dir in /tmp /run /root /var/cache /var/tmp; do \
         [ -d "$dir" ] || { echo "no mount point $dir" >&2; exit 1; }; \
       done

# The user of agent-browser and Chrome. Its home is made fresh on the guest
# tmpfs at each start (decision V4), so the root disk holds none.
RUN useradd --system --user-group --no-create-home --home-dir /nonexistent \
      --shell /usr/sbin/nologin browser

# The facade at the facade path of guest init, and the Chrome wrapper. The
# mode is set by chmod, not by a COPY flag: the bundles test of handover
# reads each COPY word as a copied path.
COPY playpen/guest/creche_browser_facade.py /usr/local/bin/creche-facade
COPY playpen/guest/creche-chrome /usr/local/bin/creche-chrome
RUN chmod 0755 /usr/local/bin/creche-facade /usr/local/bin/creche-chrome

# Fail the BUILD, not the first boot: Chrome and agent-browser start, and
# the facade refuses to run outside guest init.
RUN google-chrome-stable --version && agent-browser --version
RUN status=0; /usr/local/bin/creche-facade 2>/dev/null || status=$?; \
    [ "$status" -eq 17 ] || { echo "creche-facade: exit $status, want 17" >&2; exit 1; }

# The image env for guest init (CH7-7, CH49). This RUN stays the last step.
# The python 3.14 image sets no GPG_KEY: its release is checked with
# PYTHON_SHA256 only.
RUN --mount=type=bind,source=playpen/bin/creche-env-write,target=/run/creche-env-write \
    mkdir -p /etc/creche \
    && sh /run/creche-env-write LANG PATH PYTHON_SHA256 PYTHON_VERSION \
       > /etc/creche/env
