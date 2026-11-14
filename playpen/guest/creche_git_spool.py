#!/usr/local/bin/python3.12 -I
"""creche-git-spool: move one git branch through the git platform spool.

A VM of the coding flavor has no route to GitHub and holds no token. The
`git-platform` tools of chaperone push and fetch for it, and git bundles
travel between the VM and those tools through the spool of the host
(decision CH3, phase 12). This program is the guest end of the spool. It
ships in the coding flavor as `/usr/local/bin/creche-git-spool`.

A push takes two steps:

1. `creche-git-spool push <branch>` makes a bundle of the head commit of
   the branch, over the commits that the remote-tracking refs already
   hold, and puts it in the spool. It prints the bundle id.
2. The agent calls the `push` tool of `git-platform` with the repo, the
   branch and that bundle id. The tool pushes the head of the bundle to
   `refs/heads/<branch>` on GitHub.

A fetch takes two steps too:

1. The agent calls the `fetch` tool of `git-platform` with the repo and
   the branch. The tool answers with a bundle id.
2. `creche-git-spool fetch <bundle id>` gets the bundle from the spool,
   runs `git bundle verify` and fetches its one branch into a local
   branch. When that branch is checked out, a fast-forward merge moves
   the branch and its work tree.

The link: guest init splices each TCP link of guest 127.0.0.1:1105 to host
port 1105, and the vmm serves the spool there (`creche_wire::spool`,
`rust/crates/wire/src/spool.rs`). A link carries one request line, `put
<size>` and the bundle bytes or `get <id>`, and one reply line, `ok <id>`,
`ok <size>` and the bundle bytes, or `err <reason>`. The vmm listens only
for a family that holds a `git-platform` grant. For any other family guest
init closes the link with no reply.

This program checks the size cap and the line cap before it dials. The vmm
and the upstream check every rule again, so nothing here is a guard.
"""

from __future__ import annotations

import argparse
import re
import socket
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import BinaryIO, Final

#: Guest loopback, where guest init listens for the spool row.
SPOOL_HOST: Final = "127.0.0.1"

#: `creche_wire::guest::GIT_SPOOL_PORT`. A playpen test holds the two equal.
SPOOL_PORT: Final = 1105

#: `creche_wire::spool::BUNDLE_CAP`: the most bytes of one bundle.
BUNDLE_CAP: Final = 64 * 1024 * 1024

#: `creche_wire::spool::LINE_CAP`: the most bytes of one line, the LF not
#: counted.
LINE_CAP: Final = 128

#: `creche_wire::spool::BODY_WAIT`: the bound of each wait on the link.
LINK_WAIT_S: Final = 120.0

#: The bytes of one read of a fetched bundle.
CHUNK: Final = 256 * 1024

#: The end of each bundle id: `<family>-<ulid>.bundle`.
BUNDLE_SUFFIX: Final = ".bundle"

#: A size of the protocol: decimal, with no sign and no leading zero.
SIZE: Final = re.compile(r"[1-9][0-9]*")

#: What each reason word of an `err` reply means for the agent
#: (`creche_wire::spool::Refusal`).
REASONS: Final = {
    "bad_request": "the spool refused the request line",
    "not_granted": "this family holds no git-platform grant for this step",
    "too_large": f"the bundle passes the cap of {BUNDLE_CAP} bytes",
    "short_body": "the spool got fewer bundle bytes than the size",
    "bad_id": "the bundle id is not <family>-<ulid>.bundle",
    "other_family": "the bundle id names another family",
    "not_found": "the spool holds no such bundle: call the git-platform fetch tool again",
    "spool_fault": "the host spool failed: try again later",
}

#: The reply of a link that closed before its reply line.
NO_REPLY: Final = (
    "the spool closed the link with no reply: this family holds no "
    "git-platform grant, or the host does not serve the spool now"
)

DESCRIPTION: Final = """\
Move one git branch through the git platform spool of this VM.

push: make a bundle of the head of BRANCH over the commits that the
remote-tracking refs already hold, and put it in the spool. The bundle id
goes to stdout. Then call the git-platform push tool with the repo, the
branch and that bundle id.

fetch: first call the git-platform fetch tool with the repo and the branch.
Then pass the bundle id that it returns. The bundle is verified and its one
branch is fetched into a local branch. The local branch and its commit go
to stdout. The fetch never forces: a local branch that does not
fast-forward to the bundle is refused.
"""


class Failed(Exception):
    """A refusal or a fault. The message is the one stderr line."""


def _git(repo: Path, *args: str) -> str:
    """The stdout of one git command in `repo`."""
    done = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=False
    )
    if done.returncode != 0:
        detail = done.stderr.strip() or f"exit {done.returncode}"
        raise Failed(f"git {args[0]} failed: {detail}")

    return done.stdout


def _branch(repo: Path, name: str) -> str:
    """`name` when it is a plain branch name. The name of `@{-1}` and the
    like is refused, because git would expand it."""
    if not name or name.startswith("-"):
        raise Failed(f"{name!r} is not a branch name")

    done = subprocess.run(
        ["git", "-C", str(repo), "check-ref-format", "--branch", name],
        capture_output=True,
        text=True,
        check=False,
    )
    if done.returncode != 0 or done.stdout.strip() != name:
        raise Failed(f"{name!r} is not a branch name")

    return name


def _bundle_id(text: str) -> str:
    """`text` when it can be the id of a get line. The vmm checks the id."""
    fits = len(f"get {text}") <= LINE_CAP
    plain = text.isascii() and text.isprintable() and " " not in text
    if not (fits and plain and text.endswith(BUNDLE_SUFFIX)):
        raise Failed(f"{text!r} is not a bundle id: <family>-<ulid>{BUNDLE_SUFFIX}")

    return text


def _dial() -> socket.socket:
    """A new link to the spool row of guest init."""
    try:
        return socket.create_connection((SPOOL_HOST, SPOOL_PORT), timeout=LINK_WAIT_S)
    except OSError as error:
        raise Failed(
            f"no spool on {SPOOL_HOST}:{SPOOL_PORT} ({error}): run this in a creche VM"
        ) from error


def _reply(reader: BinaryIO, sent: OSError | None = None) -> str:
    """The value of the `ok` reply line of `reader`. An `err` line, a bad
    line and a close with no line fail. `sent` is the error of a send that
    failed first: a refused put can close the link before its body."""
    try:
        line = reader.readline(LINE_CAP + 1)
    except OSError:
        line = b""

    if not line:
        detail = f" (the send failed: {sent})" if sent else ""
        raise Failed(f"{NO_REPLY}{detail}")

    text = line.decode("ascii", errors="replace")
    word, _, value = text.removesuffix("\n").partition(" ")
    if not text.endswith("\n") or not value:
        raise Failed(f"the spool sent a bad reply: {text!r}")

    if word == "err":
        raise Failed(f"the spool refused: {value}: {REASONS.get(value, 'an unknown reason')}")

    if word != "ok":
        raise Failed(f"the spool sent a bad reply: {text!r}")

    return value


def push(repo: Path, branch: str) -> tuple[str, str]:
    """Puts the bundle of `branch` and returns its id and its commit."""
    branch = _branch(repo, branch)
    ref = f"refs/heads/{branch}"
    head = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if head.returncode != 0:
        raise Failed(f"the repo has no branch {branch}")

    sha = head.stdout.strip()

    # The remote-tracking refs name what the remote already holds, so the
    # bundle leaves those commits out and names them as prerequisites. A
    # head that the remote already holds gets a bundle of that one commit
    # over its parents, so a new branch can start at a commit of the remote.
    new = int(_git(repo, "rev-list", "--count", ref, "--not", "--remotes", "--"))
    held = ["--remotes"] if new else [f"{sha}^@"]

    with tempfile.TemporaryDirectory(prefix="creche-git-spool-") as work:
        bundle = Path(work) / "push.bundle"
        _git(repo, "bundle", "create", "--quiet", str(bundle), ref, "--not", *held, "--")
        size = bundle.stat().st_size
        if size > BUNDLE_CAP:
            raise Failed(f"the bundle has {size} bytes, past the cap of {BUNDLE_CAP}")

        with _dial() as link, link.makefile("rb") as reader, bundle.open("rb") as body:
            sent: OSError | None = None
            try:
                link.sendall(f"put {size}\n".encode("ascii"))
                link.sendfile(body)
            except OSError as error:
                sent = error

            # The vmm answers `ok` only after the last byte of the body.
            bundle_id = _reply(reader, sent)

    return _bundle_id(bundle_id), sha


def _receive(reader: BinaryIO, size: int, into: Path) -> None:
    """Copies exactly `size` bytes of `reader` into the new file `into`."""
    left = size
    with into.open("xb") as out:
        while left:
            chunk = reader.read(min(CHUNK, left))
            if not chunk:
                raise Failed(f"the spool sent {size - left} of {size} bytes")

            out.write(chunk)
            left -= len(chunk)


def _source(repo: Path, bundle: Path) -> tuple[str, str]:
    """The one branch of `bundle` and its commit."""
    heads = _git(repo, "bundle", "list-heads", str(bundle)).splitlines()
    if len(heads) != 1:
        raise Failed(f"the bundle must hold one branch, it holds {len(heads)} refs")

    sha, _, ref = heads[0].partition(" ")
    if not ref.startswith("refs/heads/"):
        raise Failed(f"the bundle holds {ref!r}, not a branch")

    return ref, sha


def fetch(repo: Path, bundle_id: str, branch: str | None) -> tuple[str, str]:
    """Gets bundle `bundle_id` and fetches its branch into `branch`, or into
    a local branch of the same name. Returns the local branch and its
    commit."""
    bundle_id = _bundle_id(bundle_id)
    if branch is not None:
        branch = _branch(repo, branch)

    with tempfile.TemporaryDirectory(prefix="creche-git-spool-") as work:
        bundle = Path(work) / "fetch.bundle"
        with _dial() as link, link.makefile("rb") as reader:
            link.sendall(f"get {bundle_id}\n".encode("ascii"))
            value = _reply(reader)
            if not SIZE.fullmatch(value) or int(value) > BUNDLE_CAP:
                raise Failed(f"the spool sent a bad size: {value!r}")

            _receive(reader, int(value), bundle)

        _git(repo, "bundle", "verify", "--quiet", str(bundle))
        ref, sha = _source(repo, bundle)
        local = branch if branch is not None else _branch(repo, ref.removeprefix("refs/heads/"))
        if _checked_out(repo, local):
            # git refuses to fetch into the checked-out branch, so the
            # fetch takes only the objects and a merge moves the branch.
            _git(repo, "fetch", "--quiet", "--no-tags", str(bundle), ref)
            _fast_forward(repo, local, sha)
        else:
            # No `+`: git refuses an update that is not a fast-forward.
            _git(repo, "fetch", "--quiet", "--no-tags", str(bundle), f"{ref}:refs/heads/{local}")

    return local, sha


def _checked_out(repo: Path, local: str) -> bool:
    """True when `local` is the branch of HEAD in `repo`."""
    done = subprocess.run(
        ["git", "-C", str(repo), "symbolic-ref", "--quiet", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    return done.returncode == 0 and done.stdout.strip() == f"refs/heads/{local}"


def _fast_forward(repo: Path, local: str, sha: str) -> None:
    """Moves the checked-out branch `local` and its work tree to `sha`. A
    branch that does not fast-forward to `sha` stays."""
    held = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", "HEAD^{commit}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if held.returncode == 0:
        ahead = subprocess.run(
            ["git", "-C", str(repo), "merge-base", "--is-ancestor", "HEAD", sha],
            capture_output=True,
            text=True,
            check=False,
        )
        if ahead.returncode != 0:
            raise Failed(f"the checked-out branch {local} does not fast-forward to {sha}")

    _git(repo, "merge", "--quiet", "--ff-only", sha)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="creche-git-spool",
        description=DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    steps = parser.add_subparsers(dest="step", required=True)

    put = steps.add_parser("push", help="put a bundle of BRANCH, print the bundle id")
    put.add_argument("-C", dest="repo", type=Path, default=Path(), help="the repo (default: .)")
    put.add_argument("branch", help="the local branch, without refs/heads/")

    get = steps.add_parser("fetch", help="get a bundle by id, fetch its branch")
    get.add_argument("-C", dest="repo", type=Path, default=Path(), help="the repo (default: .)")
    get.add_argument(
        "--branch", help="the local branch to fetch into (default: the branch of the bundle)"
    )
    get.add_argument("bundle", help="the bundle id that the git-platform fetch tool returned")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    repo: Path = args.repo
    try:
        if args.step == "push":
            bundle_id, sha = push(repo, args.branch)
            print(bundle_id)
            print(
                f"creche-git-spool: put {sha} as {bundle_id}. Next: call the git-platform "
                f"push tool with branch {args.branch} and bundle {bundle_id}.",
                file=sys.stderr,
            )
        else:
            local, sha = fetch(repo, args.bundle, args.branch)
            print(f"{local} {sha}")
    except Failed as error:
        print(f"creche-git-spool: {error}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
