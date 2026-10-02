"""What `bin/rework-registry-sync.sh` can be held to without the host.

This script is the one thing that pulls `/srv/agents/registry`. When
nothing pulls, the checkout freezes, the host does not know, and every
family change the operator merges reaches nothing. So the cases that matter most
are the ones about MOVING the checkout and about REFUSING to move it.

Six behaviours, each with its own reason to exist.

1. **A fast-forward moves `HEAD` and says so once.** One journal line per
   move, none when nothing moved: a line a minute for ever is how a real
   refusal gets missed.
2. **A DIVERGED checkout is reported and left alone.** The view commits
   into this checkout and never pushes (`view/src/agent_view/
   registrywrite.py`), so a local commit is a normal Tuesday, not
   corruption. The old sync rebased and pushed. This one never does: a
   rebase of the operator's own commit under them is unrecoverable from a timer.
3. **A dirty tracked file refuses too.** Stricter than git, on purpose —
   git fast-forwards happily over a dirty path the incoming commits do not
   touch (pinned below), which would leave somebody's half-finished edit
   sitting on top of a moved tree.
4. **`live-manifest.json` is the one file that never blocks a merge.**
   `managerd/src/agent_managerd/live_manifest.py` would write it INTO this
   checkout, and nothing calls it today. A family change must not be held
   up by a derived file.
5. **A failed fetch is a refusal, not a silent pass.** A wrong deploy key
   fails exactly here, and `bin/rework-cutover.sh up` runs the service once
   so that it is found at cutover time rather than a day later.
6. **Two runs never overlap.** `flock` on a dedicated descriptor.

Every external binary that macOS lacks is a PATH-injected binstub, the way
`bin/tests/test_rework_watchdog.py` already does it. `git` is the REAL one:
a test of a git operation runs against a real repository under `tmp_path`,
never against a stub and never over the network.

Two portability rules this file keeps, because it runs on a Mac and the
script runs on Linux:

1. Each stub's own BACKEND is portable. `timeout` and `flock` are GNU
   binaries macOS does not ship, so the stubs here stand in for them; each
   does the portable half of the job and says which half it drops.
2. Every fixture file and directory gets an explicit mode. Nothing here
   depends on the umask, on who owns a file, or on an account name.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Final

import pytest

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
SCRIPT: Final = REPO_ROOT / "bin" / "rework-registry-sync.sh"

#: The derived file `managerd` would write into the checkout (contract 06 §3.4).
MANIFEST: Final = "live-manifest.json"

DIR_MODE: Final = 0o700
FILE_MODE: Final = 0o600
STUB_MODE: Final = 0o755

STUB: Final = """#!/bin/sh
printf '%s\\n' "{name} $*" >> "$RS_STUB_LOG"
{body}
exit 0
"""

#: macOS has no `timeout`. The stub drops the ceiling and runs the command.
#: The ceiling is a production guard against a hung fetch, and there is
#: nothing to hang on a local repository.
TIMEOUT_BODY: Final = """shift
exec "$@"
"""

#: macOS has no `flock` either. This stub takes NO lock: what the cases
#: below pin is what the script DOES with flock's answer, which is the half
#: that can be wrong in the script. `RS_LOCK_HELD=1` is the other answer —
#: flock's own exit 1 for "somebody else holds it, and -n said do not wait".
FLOCK_BODY: Final = """if [ -n "${RS_LOCK_HELD:-}" ]; then exit 1; fi
"""


def _stub(directory: Path, name: str, body: str = ":") -> None:
    path = directory / name
    path.write_text(STUB.format(name=name, body=body), encoding="utf-8")
    path.chmod(STUB_MODE)


def _git(cwd: Path, *args: str) -> str:
    """Real git, with an identity of its own so no case depends on the
    machine's `user.name`."""
    done = subprocess.run(
        [
            "git",
            "-c",
            "user.email=rsy@test",
            "-c",
            "user.name=rsy",
            "-c",
            "commit.gpgsign=false",
            "-c",
            "init.defaultBranch=main",
            "-C",
            str(cwd),
            *args,
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert done.returncode == 0, f"git {args}: {done.stdout}{done.stderr}"

    return done.stdout.strip()


class Rig:
    """A fake host: a prefixed root, a real `origin` bare repository and
    a real checkout of it at the production path."""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.prefix = tmp_path / "host"
        self.registry = self.prefix / "srv/agents/registry"
        self.state = self.prefix / "srv/agents/state/rework"
        self.stamp = self.state / "registry-sync/last-success"
        self.stub_log = tmp_path / "stub.log"

        self.origin = tmp_path / "origin.git"
        self.author = tmp_path / "author"
        self.origin.mkdir()
        subprocess.run(
            ["git", "init", "-q", "--bare", "--initial-branch=main", str(self.origin)],
            check=True,
            capture_output=True,
        )

        # The upstream side: one clone nobody on the host ever sees, which
        # is where a merged pull request lands in these cases.
        self.author.mkdir()
        _git(tmp_path, "clone", "-q", str(self.origin), str(self.author))
        self.write_family("chat", "one")
        (self.author / MANIFEST).write_text('{"managerd": "0.1.0"}\n', encoding="utf-8")
        _git(self.author, "add", "-A")
        _git(self.author, "commit", "-qm", "the first registry commit")
        _git(self.author, "push", "-q", "origin", "HEAD:main")

        self.registry.parent.mkdir(parents=True, exist_ok=True)
        _git(tmp_path, "clone", "-q", "-b", "main", str(self.origin), str(self.registry))

        self.stubs = tmp_path / "stubs"
        self.stubs.mkdir()
        self.stubs.chmod(STUB_MODE)
        _stub(self.stubs, "timeout", TIMEOUT_BODY)
        _stub(self.stubs, "flock", FLOCK_BODY)

    def write_family(self, name: str, body: str, *, where: Path | None = None) -> Path:
        directory = (where or self.author) / "families" / name
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "family.yaml"
        path.write_text(f"name: {name}\nbody: {body}\n", encoding="utf-8")
        path.chmod(FILE_MODE)

        return path

    def merge_upstream(self, *, subject: str = "a family change", touch_manifest: str = "") -> str:
        """One commit on `origin/main`, the way a merged pull request
        arrives. Answers its short sha."""
        self.write_family("chat", subject)
        if touch_manifest:
            (self.author / MANIFEST).write_text(touch_manifest, encoding="utf-8")

        _git(self.author, "add", "-A")
        _git(self.author, "commit", "-qm", subject)
        _git(self.author, "push", "-q", "origin", "HEAD:main")

        return _git(self.author, "rev-parse", "--short", "HEAD")

    def head(self) -> str:
        return _git(self.registry, "rev-parse", "--short", "HEAD")

    def run(self, *args: str, **overrides: str) -> subprocess.CompletedProcess[str]:
        env = dict(os.environ)
        env["REGISTRY_SYNC_TEST_PREFIX"] = str(self.prefix)
        env["RS_STUB_LOG"] = str(self.stub_log)
        env["PATH"] = f"{self.stubs}{os.pathsep}{env['PATH']}"
        env.update(overrides)

        return subprocess.run(
            ["bash", str(SCRIPT), *args], capture_output=True, text=True, env=env, timeout=180
        )


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    return Rig(tmp_path)


# --- a checkout that is already current --------------------------------------


def test_a_current_checkout_moves_nothing_and_says_nothing(rig: Rig) -> None:
    """One line a minute for ever is how a real refusal gets missed. A run
    that changed nothing is silent."""
    was = rig.head()
    done = rig.run()

    assert done.returncode == 0, done.stdout + done.stderr
    assert rig.head() == was
    assert "->" not in done.stdout
    assert "REFUSED" not in done.stdout


def test_a_run_that_ended_well_stamps_its_own_file(rig: Rig) -> None:
    """The watchdog's fifth check reads this file's age, so a sync that
    stopped firing is found without dialling GitHub."""
    rig.run()

    assert rig.stamp.is_file()
    assert rig.stamp.read_text(encoding="utf-8").strip().endswith("Z")


# --- the move ----------------------------------------------------------------


def test_a_behind_checkout_fast_forwards_and_names_both_shas(rig: Rig) -> None:
    """THE REGRESSION. The checkout froze at 03:58 UTC and `main` moved at
    07:46. This is the run that would have closed that gap."""
    was = rig.head()
    rig.merge_upstream()
    done = rig.run()

    assert done.returncode == 0, done.stdout + done.stderr
    assert rig.head() != was
    assert f"registry: {was} -> {rig.head()}, 1 commit(s)" in done.stdout


def test_the_line_counts_every_commit_it_brought_over(rig: Rig) -> None:
    rig.merge_upstream(subject="two")
    rig.merge_upstream(subject="three")
    rig.merge_upstream(subject="four")
    done = rig.run()

    assert done.returncode == 0, done.stdout + done.stderr
    assert "3 commit(s)" in done.stdout


def test_the_working_tree_carries_the_new_bytes(rig: Rig) -> None:
    """`managerd` hashes the FILES, never the sha (`family/src/agent_family/
    registry.py`'s `revision_of`), so a moved ref with stale bytes would
    reconcile nothing."""
    rig.merge_upstream(subject="the new body")
    rig.run()

    body = (rig.registry / "families/chat/family.yaml").read_text(encoding="utf-8")
    assert "the new body" in body


# --- a checkout that diverged ------------------------------------------------


def test_a_local_commit_is_reported_and_never_rebased(rig: Rig) -> None:
    """The view commits into this checkout and never pushes, so a local
    commit is normal. The old sync rebased it and pushed. Rebasing the operator's
    own commit under them, from a timer, is not recoverable."""
    rig.write_family("chat", "an edit through the view", where=rig.registry)
    _git(rig.registry, "commit", "-qam", "a view edit")
    mine = rig.head()
    rig.merge_upstream()

    done = rig.run()

    assert done.returncode == 1, done.stdout + done.stderr
    assert "REFUSED" in done.stdout
    assert rig.head() == mine, "the sync moved a checkout that had a local commit"


def test_a_modified_tracked_file_is_reported_and_left_alone(rig: Rig) -> None:
    """Stricter than git on purpose: see the next case for what git alone
    would do here."""
    rig.write_family("chat", "a half-finished edit", where=rig.registry)
    was = rig.head()
    rig.merge_upstream()

    done = rig.run()

    assert done.returncode == 1, done.stdout + done.stderr
    assert "REFUSED" in done.stdout
    assert "families/chat/family.yaml" in done.stdout
    assert rig.head() == was
    body = (rig.registry / "families/chat/family.yaml").read_text(encoding="utf-8")
    assert "a half-finished edit" in body, "the sync discarded somebody's edit"


def test_git_alone_would_fast_forward_over_that_edit(rig: Rig) -> None:
    """MEASURED, and the reason the script checks for itself. `git merge
    --ff-only` refuses only for a path the incoming commits also change. A
    dirty path they do not touch is carried across and kept."""
    (rig.author / "families/chat/other.yaml").write_text("clean\n", encoding="utf-8")
    _git(rig.author, "add", "-A")
    _git(rig.author, "commit", "-qm", "track a second file")
    _git(rig.author, "push", "-q", "origin", "HEAD:main")
    rig.run()
    (rig.registry / "families/chat/other.yaml").write_text("locally changed\n", encoding="utf-8")
    rig.merge_upstream()
    _git(rig.registry, "fetch", "-q", "origin")

    done = subprocess.run(
        ["git", "-C", str(rig.registry), "merge", "--ff-only", "origin/main"],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )

    assert done.returncode == 0, done.stdout + done.stderr


def test_a_refusal_leaves_the_stamp_where_it_was(rig: Rig) -> None:
    """A refused run is not a successful one, so the watchdog's freshness
    check must go stale on it."""
    rig.run()
    first = rig.stamp.read_text(encoding="utf-8")
    rig.write_family("chat", "an edit through the view", where=rig.registry)
    _git(rig.registry, "commit", "-qam", "a view edit")
    rig.merge_upstream()

    rig.run()

    assert rig.stamp.read_text(encoding="utf-8") == first


def test_a_checkout_on_another_branch_is_refused(rig: Rig) -> None:
    """`merge --ff-only origin/main` on a branch that is not `main` is a
    different question with the same answer shape, so it is asked first."""
    _git(rig.registry, "checkout", "-q", "-b", "a-side-branch")
    rig.merge_upstream()

    done = rig.run()

    assert done.returncode == 1, done.stdout + done.stderr
    assert "a-side-branch" in done.stdout


def test_a_missing_checkout_is_refused(rig: Rig, tmp_path: Path) -> None:
    done = rig.run(REGISTRY_SYNC_TEST_PREFIX=str(tmp_path / "nothing-here"))

    assert done.returncode == 1, done.stdout + done.stderr
    assert "REFUSED" in done.stdout


def test_a_fetch_that_fails_is_a_refusal(rig: Rig) -> None:
    """A wrong deploy key fails exactly here. `bin/rework-cutover.sh up`
    runs this service once so the cutover finds it, not a day later."""
    _git(rig.registry, "remote", "set-url", "origin", str(rig.tmp / "no-such-repository.git"))

    done = rig.run()

    assert done.returncode == 1, done.stdout + done.stderr
    assert "REFUSED" in done.stdout
    assert "fetch" in done.stdout


# --- the one derived file ----------------------------------------------------


def test_a_rewritten_manifest_never_blocks_a_family_change(rig: Rig) -> None:
    """`live-manifest.json` is derived from root's ledger. Without
    this step the first commit that touches the file would wedge every
    later family change behind it."""
    (rig.registry / MANIFEST).write_text('{"managerd": "9.9.9"}\n', encoding="utf-8")
    was = rig.head()
    rig.merge_upstream(touch_manifest='{"managerd": "0.2.0"}\n')

    done = rig.run()

    assert done.returncode == 0, done.stdout + done.stderr
    assert rig.head() != was
    assert MANIFEST in done.stdout
    assert (rig.registry / MANIFEST).read_text(encoding="utf-8") == '{"managerd": "0.2.0"}\n'


def test_an_untracked_manifest_the_merge_adds_is_removed(rig: Rig) -> None:
    """The other half, and the one that is live TODAY: agent-registry does
    not track the file, so `managerd`'s copy is untracked. The first commit
    that adds it upstream would otherwise abort every merge with "untracked
    working tree files would be overwritten"."""
    _git(rig.author, "rm", "-q", "--cached", MANIFEST)
    _git(rig.author, "commit", "-qm", "the registry does not track it")
    _git(rig.author, "push", "-q", "origin", "HEAD:main")
    rig.run()
    (rig.registry / MANIFEST).write_text('{"managerd": "9.9.9"}\n', encoding="utf-8")
    was = rig.head()
    rig.merge_upstream(touch_manifest='{"managerd": "0.3.0"}\n')

    done = rig.run()

    assert done.returncode == 0, done.stdout + done.stderr
    assert rig.head() != was
    assert MANIFEST in done.stdout


def test_a_rewritten_manifest_alone_is_not_a_reason_to_move(rig: Rig) -> None:
    """Restoring the file is a step of a merge, never a verb of its own. A
    current checkout stays silent even when `managerd` has just rewritten
    it."""
    (rig.registry / MANIFEST).write_text('{"managerd": "9.9.9"}\n', encoding="utf-8")

    done = rig.run()

    assert done.returncode == 0, done.stdout + done.stderr
    assert done.stdout.strip() == ""


# --- what the script may never do --------------------------------------------


def test_the_script_never_rebases_and_never_resets() -> None:
    """The old `registrysync` rebased local commits and pushed them. Both
    verbs are gone with it, and a later edit must not bring one back.

    Read off the CALLS, not off the whole file: the refusal text says the
    word "push" because that is what the operator has to do by hand, and a scan of
    the prose would forbid the script from explaining itself.
    """
    body = SCRIPT.read_text(encoding="utf-8")
    calls = [
        one for one in body.splitlines() if "reg_git " in one and not one.lstrip().startswith("#")
    ]

    assert calls, "no git call found: this test can no longer see the script"
    for verb in ("rebase", "reset", "push", "clean", "checkout -B", "--force", "--hard"):
        assert not [one for one in calls if verb in one], f"{verb} is in a git call"


def test_a_leaked_git_dir_cannot_redirect_the_sync(rig: Rig, tmp_path: Path) -> None:
    """A crashed hook or worktree tooling leaves `GIT_DIR` in the
    environment, and a unit inherits whatever started it. `-C <dir>` then
    reads a different repository entirely (`flowsync/src/agent_flowsync/
    gitenv.py` names the same five variables)."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    subprocess.run(
        ["git", "init", "-q", "--initial-branch=main", str(elsewhere)],
        check=True,
        capture_output=True,
    )
    was = rig.head()
    rig.merge_upstream()

    done = rig.run(GIT_DIR=str(elsewhere / ".git"), GIT_WORK_TREE=str(elsewhere))

    assert done.returncode == 0, done.stdout + done.stderr
    assert rig.head() != was, "the sync read a repository its caller named"


def test_a_hook_in_the_checkout_never_runs(rig: Rig) -> None:
    """A checkout is untrusted input: anything that can write a family file
    can write `.git/hooks/post-merge`, which a fast-forward runs."""
    hooks = rig.registry / ".git/hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    marker = rig.tmp / "the-hook-ran"
    for name in ("post-merge", "post-checkout"):
        hook = hooks / name
        hook.write_text(f'#!/bin/sh\ntouch "{marker}"\n', encoding="utf-8")
        hook.chmod(STUB_MODE)

    rig.merge_upstream()
    done = rig.run()

    assert done.returncode == 0, done.stdout + done.stderr
    assert not marker.exists(), "a hook in the registry checkout ran"


# --- the lock ----------------------------------------------------------------


def test_a_second_run_steps_aside_while_the_first_holds_the_lock(rig: Rig) -> None:
    """A oneshot behind a one-minute timer, whose fetch can be slower than
    a minute. Two of them in one checkout is a fetch racing a merge."""
    rig.merge_upstream()
    was = rig.head()

    done = rig.run(RS_LOCK_HELD="1")

    assert done.returncode == 0, done.stdout + done.stderr
    assert "lock" in done.stdout
    assert rig.head() == was, "a run that could not take the lock still merged"


def test_the_lock_is_taken_before_the_fetch(rig: Rig) -> None:
    """A lock taken after the fetch leaves the expensive half racing."""
    rig.merge_upstream()
    rig.run(RS_LOCK_HELD="1")
    lines = rig.stub_log.read_text(encoding="utf-8").splitlines()

    assert lines, "no stub ran at all"
    assert lines[0].startswith("flock"), lines


# --- what `status` reads -----------------------------------------------------


def test_last_prints_the_head_and_its_age(rig: Rig) -> None:
    """`bin/rework-cutover.sh status` prints this line, the way it prints
    the watchdog's verdict."""
    rig.run()
    done = rig.run("--last")

    assert done.returncode == 0, done.stdout + done.stderr
    assert rig.head() in done.stdout
    assert "ago" in done.stdout


def test_last_answers_even_with_no_checkout(rig: Rig, tmp_path: Path) -> None:
    """`status` must print a line on a host that never took the cutover,
    not a stack of git errors."""
    done = rig.run("--last", REGISTRY_SYNC_TEST_PREFIX=str(tmp_path / "nothing-here"))

    assert done.returncode == 0, done.stdout + done.stderr
    assert done.stdout.strip() != ""


def test_an_unknown_argument_is_refused(rig: Rig) -> None:
    done = rig.run("--pull")

    assert done.returncode == 2, done.stdout + done.stderr
    assert "usage" in done.stdout
