"""Every entrypoint under bin/ must carry mode 100755 in git.

bin/AGENTS.md's header rule: a file under bin/ with a `#!` shebang is
executed, and `creche-deploy` syncs modes verbatim into
/opt/creche/bin, where the units and the operator run them. A script
committed 100644 therefore lands on the host unexecutable, and the exact
command its own header documents fails. Nothing else in the repo catches
it: quality-gate.sh lints Python, and bin/tests/*.sh test bash functions,
not file metadata.

The mode comes from `git ls-files -s`, never from a stat of the checkout.
Git records a file's mode when `git add` first sees it, so a later
`chmod +x` leaves the index — and the deploy — at 100644 while the working
tree looks right. The blob content is read from the index for the same
reason: mode and classification then describe one snapshot, the one a
commit would carry.

The exception is a sourced-only library, 100644 by design (bin/AGENTS.md,
Library: "never its own entrypoint") even though it carries a bash shebang.
Which files those are is derived from each header rather than listed here —
a library says so in its own first comment block, and a list of filenames
rots the next time one lands.
"""

from __future__ import annotations

import re
import subprocess
from functools import cache
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

#: git's two blob modes. A symlink (120000) or a gitlink under bin/ would be
#: neither, and is itself worth a failure.
EXEC_MODE = "100755"
PLAIN_MODE = "100644"

#: The sourced-only marker, as the three current libraries phrase it:
#: "Sourced only", "Sourced, never executed", "sourced by the host's
#: gates, never executed on its own". Both halves are required — "sourced"
#: alone also shows up in scripts that source a library.
SOURCED_ONLY = re.compile(r"\bsourced\b.{0,120}?(?:\bonly\b|never executed)", re.I | re.S)


def _header(blob: str) -> str:
    """The leading `#` comment block, shebang and comment markers dropped.

    Ends at the first line that is not a comment, which keeps a body
    comment out of it. A Python file whose header is a docstring has no
    such block, so it is never read as sourced-only — correct for every
    .py under bin/, all of which are entrypoints.
    """
    lines: list[str] = []
    for line in blob.splitlines():
        if line.startswith("#!"):
            continue

        if not line.startswith("#"):
            break

        lines.append(line.lstrip("#").strip())

    return "\n".join(lines)


def _index_blobs(shas: list[str]) -> dict[str, str]:
    """Contents of each blob, by sha, in one `git cat-file` pass.

    Each record is `<sha> blob <bytes>\\n<contents>\\n`, and the size is a
    byte count — hence bytes here, not text mode.
    """
    raw = subprocess.run(
        ["git", "cat-file", "--batch"],
        cwd=REPO,
        input="".join(f"{sha}\n" for sha in shas).encode(),
        capture_output=True,
        check=True,
    ).stdout

    blobs: dict[str, str] = {}
    at = 0
    while at < len(raw):
        eol = raw.index(b"\n", at)
        sha, _kind, size = raw[at:eol].split()
        body = eol + 1
        at = body + int(size) + 1  # + the newline git writes after the blob
        blobs[sha.decode()] = raw[body : body + int(size)].decode("utf-8", "replace")

    return blobs


@cache
def _entries() -> tuple[tuple[str, str, bool], ...]:
    """(path, mode, is_entrypoint) for every file git tracks under bin/."""
    listing = subprocess.run(
        ["git", "ls-files", "-s", "bin/"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()

    staged = [line.split("\t", 1) for line in listing]
    blobs = _index_blobs([meta.split()[1] for meta, _path in staged])

    entries: list[tuple[str, str, bool]] = []
    for meta, path in staged:
        mode, sha, _stage = meta.split()
        blob = blobs[sha]
        entries.append(
            (path, mode, blob.startswith("#!") and not SOURCED_ONLY.search(_header(blob)))
        )

    assert entries, "git tracks no files under bin/"
    return tuple(entries)


def test_entrypoints_are_executable() -> None:
    wrong = [f"{path} ({mode})" for path, mode, entry in _entries() if entry and mode != EXEC_MODE]
    assert not wrong, (
        f"shebang but not {EXEC_MODE} in git, so unexecutable once deployed: {wrong}. "
        f"Fix with `git update-index --chmod=+x <path>`, or declare the file sourced-only."
    )


def test_non_entrypoints_are_not_executable() -> None:
    wrong = [
        f"{path} ({mode})" for path, mode, entry in _entries() if not entry and mode != PLAIN_MODE
    ]
    assert not wrong, (
        f"no shebang, or sourced-only, yet not {PLAIN_MODE} in git: {wrong}. "
        f"An executable bit invites running a file that is not an entrypoint."
    )
