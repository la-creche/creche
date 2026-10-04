"""Root's own root: every path root writes during a release.

    /var/lib/creche-handover/      root:root   0755
      work/                        root:root   0755   fetched source, clones, staged closures
      releases/                    root:agents 0750   the spool (`spool.py`)
      secrets/                     root:agents 0750   sealed values (`intake/store.py`)
      upstreams.yaml               root:root   0644   the roster (`roster.py`)

THE RULE. Root reads only from roots root owns, and that is a fact about the
PATH: every ancestor of these four is root's — `/`, `/var`, `/var/lib` and
this directory. Until ROOTS three of them sat under `/srv/agents/state/rework`
and the fourth under `/srv/agents/work`, both the operator's, group `agents`. An operator-side
process could `mv` either root aside and make its own in its place, and root
would then fetch into, stage from and install a system unit out of a tree
the operator chose, or act on a planted switch note. Hash-pinned closures, the
self-contained walk and the tap cover none of that. `/srv/agents` itself is
the operator's (`bin/bootstrap-root.sh`), which is why the new root is not under it.

`secret-gaps/` stays where it is. It is the operator's on purpose: `caregiver` writes
it and the intake reads it as hostile input (`intake/gaps.py`). So does
`releases/requests/`, which two unprivileged accounts write — but its parent
chain is root's, so nothing the operator owns can be renamed under it.

`bin/rework-release-visit.sh` makes every directory here, from its own fixed
table, and `handover/tests/test_handover_roots_layout.py` holds the two to one
answer.

Constants only, stdlib only: `spool.py`, `host.py`, `intake/store.py` and the
requester all import this module.
"""

from __future__ import annotations

from typing import Final

#: `root:root 0755`. Under `/var/lib` and not `/srv/agents`, because
#: `/srv/agents` is the operator's, group `agents`, 0755, and a root under it could be renamed.
RELEASE_ROOT: Final = "/var/lib/creche-handover"

#: Where a release fetches and builds, before anything is swapped.
WORK_ROOT: Final = f"{RELEASE_ROOT}/work"

#: `stage7-releases.md` §2.2's spool.
SPOOL_ROOT: Final = f"{RELEASE_ROOT}/releases"

#: `stage7-releases.md` §4.3's one-file-per-secret store.
SECRETS_DIR: Final = f"{RELEASE_ROOT}/secrets"

#: `executor/roster.py`'s file: the upstream roster root writes at the end of
#: an `mcp-servers` release, and the PEP reads (`PEP_UPSTREAMS_GENERATED`).
ROSTER_FILE: Final = f"{RELEASE_ROOT}/upstreams.yaml"
