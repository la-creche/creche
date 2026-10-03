# Library sandbox image: python + the library package.
# Pushed to the loopback registry; per-scope sandboxes run `index-scope`.
FROM python:3.12-slim
# The site's LAN address, where TEI answers. A sandbox has no site file, so
# the build fixes it: `--build-arg AGENT_LAN_ADDRESS=...`, or the build fails.
ARG AGENT_LAN_ADDRESS
RUN test -n "$AGENT_LAN_ADDRESS" \
    || { echo "set --build-arg AGENT_LAN_ADDRESS (/etc/agent-control/site.env)" >&2; exit 1; }
ENV AGENT_LAN_ADDRESS=$AGENT_LAN_ADDRESS
WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir .
# curl for gate probes; nothing else.
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*
CMD ["index-scope", "--help"]
