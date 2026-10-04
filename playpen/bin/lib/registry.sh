#!/usr/bin/env bash
# Sourced only, never executed. `playpen-build` and `playpen-verify` both ask
# the host's own image registry one question with it, so the two cannot read
# a different answer.
#
#   registry_digest <registry> <name> <tag or digest>
#     Print the digest the registry holds for that reference, and nothing
#     when it holds none. It asks the registry itself (the OCI distribution
#     API), so the answer does not depend on a docker client or a builder.
#
# The registry is the host's own, on loopback, and it speaks plain HTTP.
# Nothing here leaves the host (contract 06 §4 rule 2).

# Every manifest form a build can push. Without the header a registry
# answers an index with the digest of a converted manifest.
REGISTRY_ACCEPT='application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json, application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.v2+json'

# One request. The registry is local, so a slow answer is a broken one.
REGISTRY_TIMEOUT_S=20

DIGEST_RE='^sha256:[0-9a-f]{64}$'

registry_digest() {  # registry_digest <registry> <name> <tag or digest>
  local found
  found="$(curl -fsS -I --max-time "$REGISTRY_TIMEOUT_S" -H "Accept: $REGISTRY_ACCEPT" \
    "http://$1/v2/$2/manifests/$3" 2>/dev/null \
    | tr -d '\r' \
    | awk 'tolower($1) == "docker-content-digest:" { print $2; exit }')"

  [[ "$found" =~ $DIGEST_RE ]] || return 1

  printf '%s\n' "$found"
}
