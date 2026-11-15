"""`playpen/guest/creche_git_spool.py`, the guest end of the git platform
spool (CH3, phase 12 PR3), on real git repos and a real listener.

The helper dials guest 127.0.0.1:1105, the spool row of guest init. Here
nothing splices: `Spool` listens on 127.0.0.1:1105 of the test machine
itself and speaks the protocol of `creche_wire::spool` as the vmm does,
over two scratch folders `push/` and `fetch/`. The tests run the helper as
a program, as an agent in the VM does. The upstream steps (verify, fetch,
push) run as plain git commands in a clone of the remote.

Port 1105 is one port of the machine, so each test holds a lock file
while it listens, and the tests of all pytest workers take turns.
"""

from __future__ import annotations

import fcntl
import importlib.util
import json
import os
import re
import secrets
import socket
import subprocess
import sys
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import BinaryIO

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
HELPER = REPO_ROOT / "playpen" / "guest" / "creche_git_spool.py"
DOCKERFILE = REPO_ROOT / "playpen" / "Dockerfile"
GUEST_VECTORS = REPO_ROOT / "rust" / "crates" / "wire" / "vectors" / "guest.json"
WIRE_SPOOL = REPO_ROOT / "rust" / "crates" / "wire" / "src" / "spool.rs"

#: The family of the VM that `Spool` serves.
FAMILY = "creche"

#: The characters of Crockford base32, as `creche_wire::spool` mints.
CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

#: `BundleId` of `creche_wire::spool`, for a family of plain letters.
BUNDLE_ID = re.compile(r"([a-z][a-z0-9-]*)-[0-7][0-9A-HJKMNP-TV-Z]{25}\.bundle")

#: The one lock of port 1105 for every pytest worker of this machine.
PORT_LOCK = Path("/tmp") / "creche-git-spool-1105.lock"

#: The env of each git child and of the helper: no config of a person.
GIT_ENV = {
    "GIT_AUTHOR_NAME": "test",
    "GIT_AUTHOR_EMAIL": "test@invalid",
    "GIT_COMMITTER_NAME": "test",
    "GIT_COMMITTER_EMAIL": "test@invalid",
    "GIT_TERMINAL_PROMPT": "0",
}


def _helper() -> ModuleType:
    """The helper as a module, to read its constants."""
    spec = importlib.util.spec_from_file_location("creche_git_spool", HELPER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _env() -> dict[str, str]:
    return {**os.environ, **GIT_ENV}


def git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
        env=_env(),
    )
    assert done.returncode == 0, f"git {args}: {done.stderr}"
    return done.stdout.strip()


def commit(repo: Path, name: str) -> str:
    (repo / name).write_text(f"{name}\n", encoding="utf-8")
    git(repo, "add", name)
    git(repo, "commit", "--quiet", "-m", name)
    return git(repo, "rev-parse", "HEAD")


def run(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """The helper as the agent runs it, in the repo `cwd`."""
    return subprocess.run(
        [sys.executable, "-I", str(HELPER), *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
        env=_env(),
        timeout=60,
    )


def mint() -> str:
    """A new bundle id of `FAMILY`."""
    ulid = "0" + "".join(secrets.choice(CROCKFORD) for _ in range(25))
    return f"{FAMILY}-{ulid}.bundle"


@dataclass
class Spool:
    """A listener on 127.0.0.1:1105 that serves one link at a time, as the
    vmm serves a VM of `FAMILY`. `push` and `fetch` are the grant. With
    `closed`, each link closes at once with no reply, as guest init closes
    a link when the vmm has no listener. `short` cuts each get reply that
    many bytes short."""

    root: Path
    push: bool = True
    fetch: bool = True
    closed: bool = False
    short: int = 0
    links: int = 0
    requests: list[str] = field(default_factory=list[str])

    @property
    def push_dir(self) -> Path:
        return self.root / "push"

    @property
    def fetch_dir(self) -> Path:
        return self.root / "fetch"

    def serve(self, listener: socket.socket, stop: threading.Event) -> None:
        while True:
            link, _ = listener.accept()
            if stop.is_set():
                link.close()
                return

            self.links += 1
            with link:
                if not self.closed:
                    self._one(link)

    def _one(self, link: socket.socket) -> None:
        link.settimeout(10)
        with link.makefile("rb") as reader:
            line = reader.readline(129).decode("ascii")
            self.requests.append(line)
            word, _, value = line.removesuffix("\n").partition(" ")
            if word == "put":
                reply = self._put(reader, int(value))
            elif word == "get":
                reply = self._get(value)
            else:
                reply = b"err bad_request\n"

        try:
            link.sendall(reply)
        except OSError:
            return

    def _put(self, reader: BinaryIO, size: int) -> bytes:
        if not self.push:
            return b"err not_granted\n"

        body = reader.read(size)
        if len(body) != size:
            return b"err short_body\n"

        name = mint()
        (self.push_dir / name).write_bytes(body)
        return f"ok {name}\n".encode("ascii")

    def _get(self, text: str) -> bytes:
        if not self.fetch:
            return b"err not_granted\n"

        match = BUNDLE_ID.fullmatch(text)
        if match is None:
            return b"err bad_id\n"

        if match.group(1) != FAMILY:
            return b"err other_family\n"

        path = self.fetch_dir / text
        if not path.is_file():
            return b"err not_found\n"

        body = path.read_bytes()
        return f"ok {len(body)}\n".encode("ascii") + body[: len(body) - self.short]


@pytest.fixture
def port_1105() -> Iterator[None]:
    """Holds port 1105 for this test against every other worker."""
    with PORT_LOCK.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


@pytest.fixture
def spool(tmp_path: Path, port_1105: None) -> Iterator[Spool]:
    served = Spool(tmp_path / "spool")
    served.push_dir.mkdir(parents=True)
    served.fetch_dir.mkdir()
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 1105))
    listener.listen(4)
    stop = threading.Event()
    thread = threading.Thread(target=served.serve, args=(listener, stop), daemon=True)
    thread.start()
    try:
        yield served
    finally:
        # A close does not wake a blocked accept on every OS, so one more
        # link does.
        stop.set()
        socket.create_connection(("127.0.0.1", 1105), timeout=10).close()
        thread.join(timeout=10)
        listener.close()
        assert not thread.is_alive()


@dataclass(frozen=True)
class Repos:
    """A bare remote with `main`, and a clone of it in which an agent works."""

    remote: Path
    work: Path
    main: str


@pytest.fixture
def repos(tmp_path: Path) -> Repos:
    seed = tmp_path / "seed"
    seed.mkdir()
    git(seed, "init", "--quiet", "--initial-branch=main")
    commit(seed, "one")
    main = commit(seed, "two")
    remote = tmp_path / "remote.git"
    git(tmp_path, "clone", "--quiet", "--bare", str(seed), str(remote))
    work = tmp_path / "work"
    git(tmp_path, "clone", "--quiet", str(remote), str(work))
    return Repos(remote=remote, work=work, main=main)


def header(bundle: Path) -> tuple[list[str], list[str]]:
    """The head lines and the prerequisite commits of `bundle`."""
    text = bundle.read_bytes().split(b"\n\n", 1)[0].decode("ascii")
    lines = text.splitlines()[1:]
    prerequisites = [line[1:].split(" ")[0] for line in lines if line.startswith("-")]
    heads = [line for line in lines if not line.startswith(("-", "@"))]
    return heads, prerequisites


def take(remote: Path, bundle: Path, branch: str, scratch: Path) -> None:
    """What the upstream does with a pushed bundle: verify it in a clone of
    the remote, fetch its head, push the head to `refs/heads/<branch>`."""
    git(scratch.parent, "clone", "--quiet", str(remote), str(scratch))
    git(scratch, "bundle", "verify", "--quiet", str(bundle))
    (sha, ref) = header(bundle)[0][0].split(" ")
    git(scratch, "fetch", "--quiet", str(bundle), f"{ref}:refs/spool/head")
    git(scratch, "push", "--quiet", str(remote), f"{sha}:refs/heads/{branch}")


def published(spool: Spool, remote: Path, branch: str, scratch: Path) -> tuple[str, str]:
    """What the upstream fetch tool does: a bundle of the whole history of
    `branch`, in `fetch/` under a new id. Returns the id and the commit."""
    git(scratch.parent, "clone", "--quiet", "--bare", str(remote), str(scratch))
    made = scratch / "made.bundle"
    git(scratch, "bundle", "create", "--quiet", str(made), f"refs/heads/{branch}")
    bundle_id = mint()
    made.rename(spool.fetch_dir / bundle_id)
    return bundle_id, git(scratch, "rev-parse", f"refs/heads/{branch}")


def test_the_constants_are_the_wire_ones() -> None:
    """The port is `GIT_SPOOL_PORT` of `vectors/guest.json`, and the caps
    and reason words are the ones of `creche_wire::spool`."""
    helper = _helper()
    vectors = json.loads(GUEST_VECTORS.read_text(encoding="utf-8"))
    assert helper.SPOOL_PORT == vectors["git_spool_port"] == 1105

    wire = WIRE_SPOOL.read_text(encoding="utf-8")
    assert "pub const BUNDLE_CAP: u64 = 64 * 1024 * 1024;" in wire
    assert helper.BUNDLE_CAP == 64 * 1024 * 1024
    assert "pub const LINE_CAP: usize = 128;" in wire
    assert helper.LINE_CAP == 128
    assert "pub const BODY_WAIT: Duration = Duration::from_secs(120);" in wire
    assert helper.LINK_WAIT_S == 120.0
    words = re.findall(r'Self::\w+ => "(\w+)",', wire)
    assert sorted(helper.REASONS) == sorted(words)
    assert len(words) == 8


def test_the_coding_flavor_ships_the_helper() -> None:
    """Only the coding flavor copies the helper, and its build runs it."""
    text = DOCKERFILE.read_text(encoding="utf-8")
    line = "COPY playpen/guest/creche_git_spool.py /usr/local/bin/creche-git-spool"
    mode = "RUN chmod 0755 /usr/local/bin/creche-git-spool"
    assert text.count(line) == 1
    assert text.count(mode) == 1
    coding = text.split("FROM agent-sandbox-python AS agent-sandbox-coding", 1)[1]
    assert line in coding
    assert mode in coding
    assert "creche-git-spool --help >/dev/null" in coding
    assert HELPER.read_text(encoding="utf-8").startswith("#!/usr/local/bin/python3.12 -I\n")


def test_push_puts_a_bundle_over_the_remote_base(
    spool: Spool, repos: Repos, tmp_path: Path
) -> None:
    git(repos.work, "switch", "--quiet", "-c", "feature")
    commit(repos.work, "three")
    head = commit(repos.work, "four")

    done = run(repos.work, "push", "feature")
    assert done.returncode == 0, done.stderr
    bundle_id = done.stdout.strip()
    assert BUNDLE_ID.fullmatch(bundle_id)
    assert spool.requests[0].startswith("put ")
    assert f"bundle {bundle_id}" in done.stderr

    bundle = spool.push_dir / bundle_id
    # One head, and the commits of the remote stay out of the bundle.
    assert header(bundle) == ([f"{head} refs/heads/feature"], [repos.main])

    take(repos.remote, bundle, "feature", tmp_path / "upstream")
    assert git(repos.remote, "rev-parse", "refs/heads/feature") == head


def test_push_of_a_commit_the_remote_holds_bundles_that_commit(
    spool: Spool, repos: Repos, tmp_path: Path
) -> None:
    """A new branch at the head of `origin/main` has no new commit. The
    bundle holds that commit over its parent."""
    git(repos.work, "branch", "start")
    parent = git(repos.work, "rev-parse", "HEAD~1")

    done = run(repos.work, "push", "start")
    assert done.returncode == 0, done.stderr
    bundle = spool.push_dir / done.stdout.strip()
    assert header(bundle) == ([f"{repos.main} refs/heads/start"], [parent])

    take(repos.remote, bundle, "start", tmp_path / "upstream")
    assert git(repos.remote, "rev-parse", "refs/heads/start") == repos.main


def test_push_with_no_remote_refs_bundles_the_whole_branch(spool: Spool, tmp_path: Path) -> None:
    alone = tmp_path / "alone"
    alone.mkdir()
    git(alone, "init", "--quiet", "--initial-branch=solo")
    commit(alone, "one")
    head = commit(alone, "two")

    done = run(tmp_path, "push", "-C", str(alone), "solo")
    assert done.returncode == 0, done.stderr
    assert header(spool.push_dir / done.stdout.strip()) == ([f"{head} refs/heads/solo"], [])


def test_push_refusals_name_the_reason(spool: Spool, repos: Repos) -> None:
    """The stderr line of a refused put names the reason. A bad or missing
    branch fails before any link."""
    git(repos.work, "switch", "--quiet", "-c", "feature")
    commit(repos.work, "three")
    spool.push = False

    done = run(repos.work, "push", "feature")
    assert done.returncode == 1
    assert done.stdout == ""
    assert "the spool refused: not_granted: this family holds no git-platform grant" in (
        done.stderr
    )

    links = spool.links
    for branch, why in [
        ("absent", "the repo has no branch absent"),
        ("@{-1}", "'@{-1}' is not a branch name"),
        ("-x", "'-x' is not a branch name"),
        ("a..b", "'a..b' is not a branch name"),
    ]:
        done = run(repos.work, "push", "--", branch)
        assert done.returncode == 1
        assert f"creche-git-spool: {why}" in done.stderr
    assert spool.links == links


def test_a_link_with_no_host_listener_has_no_reply(spool: Spool, repos: Repos) -> None:
    """Guest init closes the link at once when the vmm has no listener for
    the family. Each step says so."""
    git(repos.work, "switch", "--quiet", "-c", "feature")
    commit(repos.work, "three")
    spool.closed = True

    pushed = run(repos.work, "push", "feature")
    fetched = run(repos.work, "fetch", mint())
    for done in [pushed, fetched]:
        assert done.returncode == 1
        assert "the spool closed the link with no reply" in done.stderr
    assert spool.links == 2


def test_no_spool_row_fails_the_dial(port_1105: None, repos: Repos) -> None:
    """Outside a creche VM nothing listens on 1105."""
    done = run(repos.work, "fetch", mint())
    assert done.returncode == 1
    assert "creche-git-spool: no spool on 127.0.0.1:1105" in done.stderr


def test_fetch_gets_a_branch_into_the_repo(spool: Spool, repos: Repos, tmp_path: Path) -> None:
    seed = tmp_path / "topic"
    git(tmp_path, "clone", "--quiet", str(repos.remote), str(seed))
    git(seed, "switch", "--quiet", "-c", "topic")
    commit(seed, "five")
    git(seed, "push", "--quiet", "origin", "topic")
    bundle_id, sha = published(spool, repos.remote, "topic", tmp_path / "upstream")

    done = run(repos.work, "fetch", bundle_id)
    assert done.returncode == 0, done.stderr
    assert done.stdout == f"topic {sha}\n"
    assert spool.requests == [f"get {bundle_id}\n"]
    assert git(repos.work, "rev-parse", "refs/heads/topic") == sha

    done = run(repos.work, "fetch", "--branch", "review/topic", bundle_id)
    assert done.returncode == 0, done.stderr
    assert git(repos.work, "rev-parse", "refs/heads/review/topic") == sha


def test_fetch_never_forces_a_branch(spool: Spool, repos: Repos, tmp_path: Path) -> None:
    """A local branch that does not fast-forward to the bundle stays."""
    bundle_id, _ = published(spool, repos.remote, "main", tmp_path / "upstream")
    git(repos.work, "branch", "other", "HEAD~1")
    git(repos.work, "switch", "--quiet", "other")
    commit(repos.work, "side")
    git(repos.work, "switch", "--quiet", "main")
    side = git(repos.work, "rev-parse", "other")

    done = run(repos.work, "fetch", "--branch", "other", bundle_id)
    assert done.returncode == 1
    assert "creche-git-spool: git fetch failed" in done.stderr
    assert git(repos.work, "rev-parse", "other") == side


def test_fetch_moves_the_checked_out_branch(spool: Spool, repos: Repos, tmp_path: Path) -> None:
    """A clone has `main` checked out. A fetch of `main` fast-forwards the
    branch and its work tree."""
    seed = tmp_path / "ahead"
    git(tmp_path, "clone", "--quiet", str(repos.remote), str(seed))
    commit(seed, "five")
    git(seed, "push", "--quiet", "origin", "main")
    bundle_id, sha = published(spool, repos.remote, "main", tmp_path / "upstream")

    done = run(repos.work, "fetch", bundle_id)
    assert done.returncode == 0, done.stderr
    assert done.stdout == f"main {sha}\n"
    assert git(repos.work, "symbolic-ref", "HEAD") == "refs/heads/main"
    assert git(repos.work, "rev-parse", "HEAD") == sha
    assert (repos.work / "five").read_text(encoding="utf-8") == "five\n"
    assert git(repos.work, "status", "--porcelain") == ""


def test_fetch_never_forces_the_checked_out_branch(
    spool: Spool, repos: Repos, tmp_path: Path
) -> None:
    """A checked-out branch that does not fast-forward to the bundle stays,
    and so does its work tree."""
    bundle_id, _ = published(spool, repos.remote, "main", tmp_path / "upstream")
    git(repos.work, "reset", "--quiet", "--hard", "HEAD~1")
    side = commit(repos.work, "side")

    done = run(repos.work, "fetch", bundle_id)
    assert done.returncode == 1
    assert "creche-git-spool: the checked-out branch main does not fast-forward" in done.stderr
    assert git(repos.work, "rev-parse", "HEAD") == side
    assert git(repos.work, "status", "--porcelain") == ""


def test_fetch_refusals_leave_the_repo_as_it_was(
    spool: Spool, repos: Repos, tmp_path: Path
) -> None:
    bundle_id, _ = published(spool, repos.remote, "main", tmp_path / "upstream")
    before = git(repos.work, "for-each-ref")
    other = "code-" + mint().removeprefix(f"{FAMILY}-")

    for args, why in [
        ([mint()], "the spool refused: not_found"),
        ([other], "the spool refused: other_family"),
        (["creche-x.bundle"], "the spool refused: bad_id"),
        (["a b.bundle"], "'a b.bundle' is not a bundle id"),
        (["creche-" + "0" * 130 + ".bundle"], "is not a bundle id"),
        (["--branch=-x", bundle_id], "'-x' is not a branch name"),
    ]:
        done = run(repos.work, "fetch", *args)
        assert done.returncode == 1, args
        assert why in done.stderr, done.stderr

    spool.short = 3
    done = run(repos.work, "fetch", bundle_id)
    assert done.returncode == 1
    size = (spool.fetch_dir / bundle_id).stat().st_size
    assert f"the spool sent {size - 3} of {size} bytes" in done.stderr

    spool.short = 0
    spool.fetch = False
    done = run(repos.work, "fetch", bundle_id)
    assert done.returncode == 1
    assert "the spool refused: not_granted" in done.stderr

    spool.fetch = True
    broken = mint()
    (spool.fetch_dir / broken).write_bytes(b"# v2 git bundle\nnot a bundle\n\n")
    done = run(repos.work, "fetch", broken)
    assert done.returncode == 1
    assert "creche-git-spool: git bundle failed" in done.stderr

    assert git(repos.work, "for-each-ref") == before


def test_help_names_both_steps() -> None:
    done = run(REPO_ROOT, "--help")
    assert done.returncode == 0
    for word in ["push", "fetch", "git-platform push tool", "git-platform fetch tool"]:
        assert word in done.stdout
