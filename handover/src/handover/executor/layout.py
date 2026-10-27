"""Root's own root: every path root writes during a release.

    /var/lib/creche-handover/      root:root   0755
      work/                        root:root   0755   fetched source, clones, staged closures
      releases/                    root:agents 0750   the spool (`spool.py`)

THE RULE. Root reads only from roots root owns, and that is a fact about the
PATH: every ancestor of these paths is root's — `/`, `/var`, `/var/lib` and
this directory. Until ROOTS three of them sat under `/srv/agents/state/rework`
and the fourth under `/srv/agents/work`, both the operator's, group `agents`. An operator-side
process could `mv` either root aside and make its own in its place, and root
would then fetch into, stage from and install a system unit out of a tree
the operator chose, or act on a planted switch note. Hash-pinned closures, the
self-contained walk and the tap cover none of that. `/srv/agents` itself is
the operator's (`bin/bootstrap-root.sh`), which is why the new root is not under it.

Two unprivileged accounts write `releases/requests/`, but its parent chain
is root's, so nothing the operator owns can be renamed under it.

`bin/rework-release-visit.sh` makes every directory here, from its own fixed
table, and `handover/tests/test_handover_roots_layout.py` holds the two to one
answer.

The module also holds `STATE_ROOT`, the state root of the operator. No
root of this module is below it, and root writes no file there. It also
holds the names of the state directory below the home of the operator, and
of the store of artifacts in it.

Constants only, stdlib only: `spool.py`, `host.py`, the requester and the
follower all import this module.
"""

from __future__ import annotations

from typing import Final

#: The state root of the operator account. It is the operator's, so no root
#: of this module is below it. The follower of the release tool runs as the
#: operator, and it writes its notice file below this directory, as each
#: other program of the operator account does.
STATE_ROOT: Final = "/srv/agents/state/rework"

#: The state directory of the release tool below the home of the operator.
#: The marker of the follower is in it, and so is the store of artifacts.
OPERATOR_STATE_DIR: Final = ".local/state/creche"

#: The store of the archives of binary components, in `OPERATOR_STATE_DIR`.
#: The operator side writes it, and root reads it (`source.artifact_store`).
ARTIFACT_STORE_DIR: Final = "artifacts"

#: `root:root 0755`. Under `/var/lib` and not `/srv/agents`, because
#: `/srv/agents` is the operator's, group `agents`, 0755, and a root under it could be renamed.
RELEASE_ROOT: Final = "/var/lib/creche-handover"

#: Where a release fetches and builds, before anything is swapped.
WORK_ROOT: Final = f"{RELEASE_ROOT}/work"

#: `stage7-releases.md` §2.2's spool.
SPOOL_ROOT: Final = f"{RELEASE_ROOT}/releases"
