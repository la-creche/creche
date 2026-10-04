"""A family edit, as one validated git commit.

Invariant 16: one file defines a family, and a change to it is ONE
action. So a save here is one commit or nothing at all, and the bytes on
disk after a refused save are the bytes that were there before.

The noticeboard never applies anything. It writes the registry and stops.
`caregiver` watches the registry and converges (contract 05 §5), so there
is no `agentctl`, no systemd call and no daemon verb in this module.

The order is fixed, and every step after the write can undo it.

1. Refuse a name, a path or a link that does not belong to this family.
2. Validate the WHOLE registry with the new text, not just this file. A
   family's delegates, skills and MCP servers are other files, so a change
   here can break a neighbour (contract 01 §7). The validator reads a copy
   of the checkout, so the checkout never holds a text that it refuses.
3. Snapshot every file in the family's directory.
4. Write the new text to a temporary file in the same directory and rename
   it into place. `caregiver` reads the checkout at any time, and a rename
   shows it the old file or the new one, never a part of one.
5. Commit, scoped to this family's pathspec alone.
6. Restore the snapshot on any failure in steps 4 or 5, including one
   nobody predicted: the restore runs from a `finally`, so a git that
   times out or a git that is not installed cannot leave a half-save on
   disk.

What this module does NOT do, on purpose: it takes no lock. Git's own
`index.lock` makes a concurrent writer fail, and a failed commit restores.
"Fail and roll back" is the behaviour under a race, never "corrupt".
"""

from __future__ import annotations

import os
import re
import secrets
import shutil
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from agent_family import Issue, Registry, Report, Severity, load_registry

FAMILY_FILE: Final = "family.yaml"
FAMILIES_DIR: Final = "families"

#: A family name, contract 01 §2.
NAME_RE: Final = re.compile(r"^[a-z][a-z0-9-]{1,30}\Z")

#: Who the commit is by. Pinned, so authorship never depends on the
#: checkout's own `user.name` (which may be unset, or the operator's).
AUTHOR_NAME: Final = "noticeboard"
AUTHOR_EMAIL: Final = "noticeboard@localhost"

#: A real git trailer, so `git interpret-trailers` and
#: `%(trailers)` can read it, unlike a bare body line.
COMMIT_TRAILER: Final = "Via: noticeboard"

#: A subject is operator text and reaches a log. One line, bounded.
MAX_SUBJECT: Final = 200

#: A family file is a few kilobytes. Anything near this is not one.
MAX_TEXT_BYTES: Final = 256 * 1024

GIT_TIMEOUT_S: Final = 30

#: git's own directory. The validator reads none of it, so the copy that it
#: reads leaves it out.
GIT_METADATA: Final = ".git"

#: The random part of a temporary file name, in bytes. Two saves at one time
#: then never share a name.
TEMP_NAME_BYTES: Final = 8

#: The mode a new file asks for. The umask of the process narrows it, as it
#: does for every file this service creates.
NEW_FILE_MODE: Final = 0o666

#: Git's environment is built from scratch, not filtered. This process
#: may carry the family's LiteLLM key and PEP token, and git has no
#: business seeing either (invariant 13). Building an allowlist also
#: means a stray `GIT_DIR` or `GIT_WORK_TREE` cannot redirect the write.
GIT_ENV_KEEP: Final = ("PATH", "HOME", "LANG", "LC_ALL")


@dataclass(frozen=True)
class SaveResult:
    """What the edit page renders after a save."""

    ok: bool
    issues: tuple[Issue, ...] = ()
    #: The new commit's short sha, when one was made.
    commit: str = ""
    #: A failure that is not a validation issue: a bad name, a link, git.
    problem: str = ""
    #: The text matched the file byte for byte, so nothing was written
    #: and nothing was committed. Not an error, and not a save either.
    unchanged: bool = False

    @property
    def errors(self) -> tuple[Issue, ...]:
        return tuple(one for one in self.issues if one.severity is Severity.ERROR)


def family_path(registry_dir: Path, name: str) -> Path:
    """Where one family's file lives. One spelling, so a caller that wants
    to read the bytes before a save does not build the path itself."""
    return registry_dir / FAMILIES_DIR / name / FAMILY_FILE


def save_family(registry_dir: Path, name: str, text: str, subject: str) -> SaveResult:
    """Write one `family.yaml` and commit it, or change nothing."""
    refusal = _refuse(registry_dir, name, text)

    if refusal:
        return SaveResult(ok=False, problem=refusal)

    base = registry_dir / FAMILIES_DIR / name
    target = base / FAMILY_FILE

    if target.is_file() and target.read_bytes() == text.encode("utf-8"):
        return SaveResult(ok=True, unchanged=True, commit=head_sha(registry_dir))

    return _write_and_commit(registry_dir, base, target, text, subject)


def _write_and_commit(
    registry_dir: Path, base: Path, target: Path, text: str, subject: str
) -> SaveResult:
    """Steps 2 to 6. The snapshot is restored unless the save succeeds."""
    try:
        registry = _with_new_text(registry_dir, base.name, text)
    except OSError as error:
        return SaveResult(
            ok=False, problem=f"cannot copy the registry to validate it: {error.strerror or error}"
        )

    if not registry.ok:
        # Every report, not only this family's: the point of validating
        # the whole registry is to catch the neighbour this edit broke.
        # Nothing in the checkout changed, so there is nothing to restore.
        return SaveResult(ok=False, issues=_issues(registry.all_reports()))

    existed = base.is_dir()
    snapshot = _snapshot(base)
    done = False

    try:
        base.mkdir(parents=True, exist_ok=True)
        _replace(target, text.encode("utf-8"))

        problem = _commit(registry_dir, base.name, subject)

        if problem:
            return SaveResult(ok=False, problem=problem)

        done = True

        return SaveResult(
            ok=True,
            # A save with warnings is still a save. They are shown, not thrown.
            issues=_warnings(registry.all_reports()),
            commit=head_sha(registry_dir),
        )
    except OSError as error:
        return SaveResult(
            ok=False, problem=f"cannot write {target.name}: {error.strerror or error}"
        )
    finally:
        # A `return` above, an unexpected exception, a git that hangs:
        # every path that is not a successful save ends here.
        if not done:
            _restore(base, existed, snapshot)


def _with_new_text(registry_dir: Path, name: str, text: str) -> Registry:
    """Step 2: the whole registry as it reads with the new text in place.

    The validator reads a copy. The checkout keeps its bytes until the new
    text validates, so `caregiver` never reads a text that is refused here.
    """
    with tempfile.TemporaryDirectory(prefix="noticeboard-save-") as scratch:
        copy = Path(scratch) / "registry"
        # A link is copied as a link. Nothing outside the checkout is read.
        shutil.copytree(
            registry_dir, copy, symlinks=True, ignore=shutil.ignore_patterns(GIT_METADATA)
        )
        base = copy / FAMILIES_DIR / name

        # A copied link can still point into the checkout. The new text must
        # land in the copy, or step 2 is a write with no validation.
        if _escapes(copy, base):
            raise OSError(f"{FAMILIES_DIR}/{name} resolves outside the copy")

        base.mkdir(parents=True, exist_ok=True)
        (base / FAMILY_FILE).write_text(text, encoding="utf-8")

        return load_registry(copy)


def _replace(path: Path, body: bytes) -> None:
    """Step 4: put `body` at `path` with one rename.

    The bytes go to a temporary file in the same directory, so the rename
    stays on one file system and is atomic. An existing file keeps its mode.
    """
    temp = path.with_name(f".{path.name}.{secrets.token_hex(TEMP_NAME_BYTES)}.tmp")

    try:
        handle = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, NEW_FILE_MODE)

        with os.fdopen(handle, "wb") as out:
            out.write(body)
            out.flush()
            os.fsync(out.fileno())

        if path.is_file():
            temp.chmod(stat.S_IMODE(path.stat().st_mode))

        temp.replace(path)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise


def _refuse(registry_dir: Path, name: str, text: str) -> str:
    """Everything that is refused before a single byte is written."""
    if not NAME_RE.match(name):
        return f"{name!r} is not a family name"

    if len(text.encode("utf-8")) > MAX_TEXT_BYTES:
        return f"the family file is over {MAX_TEXT_BYTES} bytes"

    base = registry_dir / FAMILIES_DIR / name

    if _escapes(registry_dir, base):
        return f"{name!r} resolves outside the registry"

    # A symlink anywhere on the last two steps turns a scoped write into
    # a write somewhere else. `resolve()` above does not catch one that
    # points back INSIDE the registry, which is the interesting case.
    if base.is_symlink() or (base / FAMILY_FILE).is_symlink():
        return f"{name!r} is a symlink; refusing to write through it"

    return ""


def _escapes(root: Path, target: Path) -> bool:
    try:
        return not target.resolve().is_relative_to(root.resolve())
    except OSError:
        return True


def _snapshot(base: Path) -> dict[Path, bytes]:
    """Every regular file in the family's directory, with its bytes.

    The whole directory, not just `family.yaml`: a save can add a file,
    and restoring two known paths would leave the new one behind as an
    untracked file in the registry.
    """
    if not base.is_dir():
        return {}

    kept: dict[Path, bytes] = {}

    for path in sorted(base.rglob("*")):
        if path.is_file() and not path.is_symlink():
            kept[path] = path.read_bytes()

    return kept


def _restore(base: Path, existed: bool, snapshot: dict[Path, bytes]) -> None:
    """Put the bytes back exactly as they were."""
    if not existed:
        shutil.rmtree(base, ignore_errors=True)
        return

    # Anything now present that was not in the snapshot was added by the
    # save, so it goes. Otherwise a failed save leaves a new file behind.
    for path in sorted(base.rglob("*"), reverse=True):
        if path.is_file() and path not in snapshot:
            path.unlink(missing_ok=True)

    for path, body in snapshot.items():
        path.parent.mkdir(parents=True, exist_ok=True)

        # Only a file that the save changed is put back, and with a rename.
        # A write in place would show a reader a part of a file.
        if not path.is_file() or path.read_bytes() != body:
            _replace(path, body)


def _commit(registry_dir: Path, name: str, subject: str) -> str:
    """One commit, scoped to one family's pathspec. Empty on success.

    The pathspec is what keeps a stray edit elsewhere in the registry out
    of this commit. A whole-tree commit would carry it along, and the
    operator would have no way to tell what the noticeboard actually changed.
    """
    spec = f"{FAMILIES_DIR}/{name}"
    message = f"{_subject(subject, name)}\n\n{COMMIT_TRAILER}\n"

    added = _git(registry_dir, "add", "--all", "--", spec)

    if added.returncode != 0:
        return f"git add failed: {_detail(added)}"

    made = _git(registry_dir, "commit", "-m", message, "--", spec)

    if made.returncode == 0:
        return ""

    # Leave the index as it was found, or the next reader sees this
    # family staged and cannot tell a failed save from a pending one.
    _git(registry_dir, "reset", "--", spec)

    return f"git commit failed: {_detail(made)}"


def head_sha(registry_dir: Path) -> str:
    done = _git(registry_dir, "rev-parse", "--short", "HEAD")

    return done.stdout.strip() if done.returncode == 0 else ""


def _git(registry_dir: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Every git call the noticeboard makes, with its identity and env pinned."""
    command = [
        "git",
        "-C",
        str(registry_dir),
        "-c",
        f"user.name={AUTHOR_NAME}",
        "-c",
        f"user.email={AUTHOR_EMAIL}",
        # A registry commit must never wait for a signing passphrase.
        "-c",
        "commit.gpgsign=false",
        *args,
    ]

    try:
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_S,
            check=False,
            env={key: value for key, value in os.environ.items() if key in GIT_ENV_KEEP},
        )
    except (subprocess.TimeoutExpired, OSError) as error:
        # A hung git and a missing git both become a failed call, so the
        # caller's `finally` restores instead of the exception escaping.
        return subprocess.CompletedProcess(command, returncode=-1, stdout="", stderr=str(error))


def _detail(done: subprocess.CompletedProcess[str]) -> str:
    text = (done.stderr or done.stdout).strip()

    return f"rc={done.returncode} {text}"[:MAX_SUBJECT]


def _subject(subject: str, name: str) -> str:
    """One line, bounded. Operator text reaches `git log`."""
    flat = " ".join(subject.split())[:MAX_SUBJECT]

    return flat or f"update family {name}"


def _issues(reports: tuple[Report, ...]) -> tuple[Issue, ...]:
    found: list[Issue] = []

    for report in reports:
        found.extend(report.issues)

    return tuple(found)


def _warnings(reports: tuple[Report, ...]) -> tuple[Issue, ...]:
    return tuple(one for one in _issues(reports) if one.severity is not Severity.ERROR)
