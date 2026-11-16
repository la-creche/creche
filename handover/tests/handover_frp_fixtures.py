"""Fixtures of the Forgejo release path (`frp`), the reading side of
`docs/design/forgejo-release-path.md` section 5.

1. `reply` and `changed`: a recorded reply of Forgejo 17.0.1. The body
   comes from the file and the status from the `index.tsv` of its
   directory, never from a literal. Most files are in
   `fixtures/forgejo-17.0.1/` of this package. Four files are read in place
   from the fixtures of the signer crate, which recorded them first.
2. `KeyStandIn`: the runner of a verifier child. It gives the child to the
   real check of `host.verifier_argv`, and then starts a stand-in of
   `cosign verify-blob --key` as a child with the words of the check.
3. `signed_bundle` and `tag_message`: a bundle that the stand-in passes,
   and the message of an annotated tag as `git/tags/{sha}` gives it.
"""

from __future__ import annotations

import base64
import hashlib
import json
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from handover.executor.forge_api import ForgeReply
from handover.executor.host import NO_VERIFIER_EXIT_CODE, UNIT_END, Command, Result, verifier_argv
from handover.executor.release_statement import MESSAGE_HEADER, RELEASE_SIGNING_KEY
from handover_bin_fixtures import ROOT_OWNED_VERIFIER, site_text

from handover import site

#: The root of the repository.
REPO_ROOT = Path(__file__).resolve().parents[2]

#: The fixtures of this package, and the crate of the signer.
FIXTURES = Path(__file__).parent / "fixtures" / "forgejo-17.0.1"
SIGNER = REPO_ROOT / "rust" / "crates" / "ci-signer"
SIGNER_FIXTURES = SIGNER / "tests" / "fixtures" / "forgejo-17.0.1"

#: The four recordings that this package reads in place from the signer.
SIGNER_FILES = frozenset(
    {
        "p1-commit-pull-merge-sha.json",
        "p1-commit-pull-pr-head.json",
        "p2-git-tags-sha.json",
        "p2-tags-get.json",
    }
)

#: The owner and the repository of every recording.
OWNER = "example-owner"
REPOSITORY = "runner-test"

#: The merge commit of the recorded pull request 2, its head, and the tag
#: object of the recorded tag `rec-ann`.
MERGE_COMMIT = "5791a8cdae55087207f12ab919c5eb964494d654"
PR_HEAD = "bfe53d3a42c4526741e6890f997d52916fbc381d"
TAG_OBJECT = "4da7bc96263366317ed51fe24d424e1cb1770fc7"


def _directory(name: str) -> Path:
    return SIGNER_FIXTURES if name in SIGNER_FILES else FIXTURES


def index_row(name: str) -> list[str]:
    """The fields of the `index.tsv` row of `name`: file, method, path,
    status and token kind."""
    index = (_directory(name) / "index.tsv").read_text(encoding="utf-8")
    for line in index.splitlines():
        fields = line.split("\t")
        if fields[0] == name:
            return fields

    raise AssertionError(f"{name} has no row in index.tsv")


def reply(name: str) -> ForgeReply:
    """The recorded reply of `name`."""
    status = int(index_row(name)[3])

    return ForgeReply(status, (_directory(name) / name).read_bytes())


def recorded(name: str) -> Any:
    """The recorded body of `name` as JSON."""
    return json.loads(reply(name).body)


def changed(name: str, change: Callable[[Any], None]) -> ForgeReply:
    """The recorded reply of `name` with a change that the test makes and
    names. The status stays the recorded one."""
    value = recorded(name)
    change(value)

    return ForgeReply(reply(name).status, json.dumps(value).encode("utf-8"))


#: The marker line of the job log that holds the create reply of a Release.
CREATE_MARKER = " M release_body="


def create_reply() -> ForgeReply:
    """The create reply of the Release, as the job log of the recording run
    printed it. Its author is the Actions user, not the operator."""
    log = (FIXTURES / "p3-job-log-markers.txt").read_text(encoding="utf-8")
    line = next(one for one in log.splitlines() if CREATE_MARKER in one)

    return ForgeReply(201, line.split(CREATE_MARKER, 1)[1].encode("utf-8"))


#: A stand-in of `cosign verify-blob --key` with no log. It passes only the
#: words of `release_statement.statement_words` and a bundle that holds the
#: key path and the SHA-256 of the statement. It opens no socket.
KEY_STAND_IN = """
import hashlib, json, sys
words = sys.argv[1:]
if len(words) != 7 or words[0:2] != ["verify-blob", "--key"] or words[3] != "--bundle":
    raise SystemExit(2)
if words[5] != "--insecure-ignore-tlog=true":
    raise SystemExit(2)
with open(words[4], "rb") as handle:
    bundle = json.load(handle)
with open(words[6], "rb") as handle:
    digest = hashlib.sha256(handle.read()).hexdigest()
raise SystemExit(0 if bundle == {"key": words[2], "sha256": digest} else 1)
"""


def signed_bundle(statement: bytes) -> bytes:
    """A bundle that `KEY_STAND_IN` passes for `statement` and the key of
    FR10."""
    claims = {"key": RELEASE_SIGNING_KEY, "sha256": hashlib.sha256(statement).hexdigest()}

    return json.dumps(claims).encode("utf-8")


def tag_message(statement: bytes, bundle: bytes) -> str:
    """The message of a release tag as `git/tags/{sha}` gives it: three
    lines and one LF that the reply adds."""
    statement_line = base64.b64encode(statement).decode("ascii")
    bundle_line = base64.b64encode(bundle).decode("ascii")

    return f"{MESSAGE_HEADER}\n{statement_line}\n{bundle_line}\n"


@dataclass
class KeyStandIn:
    """The runner of each verifier child: the real check of
    `host.verifier_argv`, then `KEY_STAND_IN` as a child. `seen` records
    each command, and `words` each whole argv that the check made."""

    seen: list[Command] = field(default_factory=list[Command])
    words: list[tuple[str, ...]] = field(default_factory=list[tuple[str, ...]])

    def __call__(self, command: Command) -> Result:
        self.seen.append(command)
        argv = verifier_argv(command)
        if argv is None:
            return Result(NO_VERIFIER_EXIT_CODE)

        self.words.append(argv)
        child = argv[argv.index(UNIT_END) + 2 :]
        done = subprocess.run(
            [sys.executable, "-I", "-c", KEY_STAND_IN, *child],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=60.0,
            check=False,
        )

        return Result(done.returncode)


def use_verifier_site(
    where: Path, monkeypatch: pytest.MonkeyPatch, verifier: str = ROOT_OWNED_VERIFIER
) -> None:
    """A site file that names `verifier` and a root-owned `systemd-run`. With
    the default verifier, `host.verifier_argv` passes the check of the two
    programs."""
    path = where / "site.env"
    path.write_text(site_text(where, verifier), encoding="utf-8")
    monkeypatch.setenv(site.SITE_FILE_ENV, str(path))
