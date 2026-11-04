# disk-tools: the pinned trixie stage that makes a root disk from the tar
# of one flavor (CH7-2, fact 59). playpen/bin/playpen-disk builds it and
# runs it with no network.
#
# `mke2fs -d -` reads a tar on stdin, so each file keeps its owner and mode
# from the tar and no step needs root on the host. It needs e2fsprogs 1.47.1
# or later, and it loads libarchive at run time to read the tar. Trixie
# ships 1.47.2. zstd compresses the disk, because a GitHub asset must stay
# under 2 GiB.
#
# The digest pins the multi-platform index of debian:trixie-slim. The build
# takes linux/amd64 from it.
FROM debian:trixie-slim@sha256:f6b08b09a05725f9431502d3b5714f5c4576e82c1a84fa1debe0c24a7e0c3ec4 AS disk-tools

RUN apt-get update \
    && apt-get install -y --no-install-recommends e2fsprogs libarchive13t64 zstd \
    && rm -rf /var/lib/apt/lists/*

RUN version="$(mke2fs -V 2>&1 | sed -n 's/^mke2fs \([0-9][0-9.]*\) .*/\1/p')"; \
    if ! printf '%s\n%s\n' 1.47.1 "$version" | sort -C -V; then \
      echo "disk-tools: mke2fs ${version:-of no version} is older than 1.47.1" >&2; exit 1; \
    fi
