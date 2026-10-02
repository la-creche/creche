"""The requester half of `stage7-releases.md` §2.1.

§2.1 names three actors. The EXECUTOR is `executor/`, it is root, and it does
everything that touches the host. The APPROVER is the operator's phone. This package
is the third one: it states intent, and it may do exactly one thing — write
one request file into `requests/`.

Two rules hold this package's shape, and both have their reason beside them
in `file.py`.

1. **One writer, two callers.** The operator's CLI (`agent-releasectl request`) and
   the PEP's `release` verb file the same bytes through `file_request`. A
   third caller writes no second copy.
2. **It refuses what the executor would refuse, with the executor's own
   reason.** `plan_request` runs the executor's own `parse_request` over the
   bytes it is about to write, so a rule added to `executor/request.py`
   reaches both callers with no second edit.
"""

from __future__ import annotations

from ..executor.live_state import (
    INSTALL_ROOT,
    BuiltState,
    Chosen,
    Readers,
    build_state,
    default_install_roots,
    operator_install_root,
    select_manifests,
)
from .file import (
    MAX_PENDING,
    REQUEST_FILE_MODE,
    REQUESTS_PATH,
    RequesterError,
    file_request,
    new_ulid,
    pending_count,
    plan_request,
)
from .preview import Preview, preview_of

#: `cli.py` reads these from here and never from `executor/`: the pure half
#: imports nothing out of root's half (`release/AGENTS.md`), and
#: `bin/rework-release-gate.sh` greps both halves for it.
#:
#: `build_state` is re-exported for the same reason the path constants are:
#: the requester resolves against a
#: document built by the SAME code root builds its own with, so a preview
#: that disagrees with the phone is a reader that answered differently and
#: never a second implementation.
__all__ = [
    "INSTALL_ROOT",
    "MAX_PENDING",
    "REQUESTS_PATH",
    "REQUEST_FILE_MODE",
    "BuiltState",
    "Chosen",
    "Preview",
    "Readers",
    "RequesterError",
    "build_state",
    "default_install_roots",
    "file_request",
    "new_ulid",
    "operator_install_root",
    "pending_count",
    "plan_request",
    "preview_of",
    "select_manifests",
]
