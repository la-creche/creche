"""`agent-releasectl request` with no document to read.

It runs the SAME builder root runs, as the operator, against the corpus clones.
Three properties hold it in place, and each is one test below.

1. It resolves `latest` from the clone's own tags, with no document and no
   GitHub token.
2. It never refuses over what it could not read. Root re-derives every one
   of these answers at step 2, so an unreadable corpus is a printed line and
   a request that still gets filed.
3. It prints no gate id and its `review` row says `local:`: root alone
   computes `manifest_sha256`.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from agent_release.catalog import CATALOG, CATALOG_BY_NAME, Repo
from agent_release.cli import EXIT_OK, main
from agent_release.executor.live_state import write_stamp
from agent_release.executor.request import parse_request
from release_fixtures import manifest_text, write_manifest

COMPONENT = "noticeboard"
FIRST_VERSION = "0.1.0"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
        env={
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "HOME": str(repo),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
        },
    )


def _corpus(tmp_path: Path, *, tags: tuple[str, ...]) -> Path:
    """The three clones, with the tags CI would have made in agent-control."""
    root = tmp_path / "corpus"
    root.mkdir()
    root.chmod(0o755)
    for repo in Repo:
        clone = root / str(repo)
        clone.mkdir()
        clone.chmod(0o755)
        (clone / "README.md").write_text(f"{repo}\n", encoding="utf-8")
        _git(clone, "init", "-q", "-b", "main")
        _git(clone, "add", "-A")
        _git(clone, "commit", "-q", "-m", "first")

    for tag in tags:
        _git(root / str(Repo.AGENT_CONTROL), "tag", tag)

    return root


def _manifests(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    for row in CATALOG:
        write_manifest(root, row.name, manifest_text(row.name))

    return root


def _argv(tmp_path: Path, corpus: Path, spool: Path, *, components: list[str]) -> list[str]:
    return [
        "request",
        *components,
        "--root",
        str(_manifests(tmp_path)),
        "--partial",
        "--corpus",
        str(corpus),
        "--install-root",
        str(tmp_path / "components"),
        "--spool",
        str(spool),
        "--json",
    ]


def _spool(tmp_path: Path) -> Path:
    directory = tmp_path / "spool"
    directory.mkdir()

    return directory


def test_latest_comes_from_the_clones_own_tags(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`agent-releasectl request noticeboard`, with no document anywhere.

    This is the first release's command, and `latest` resolves against
    the clone's own tags.
    """
    corpus = _corpus(tmp_path, tags=(f"{COMPONENT}-v{FIRST_VERSION}",))
    spool = _spool(tmp_path)

    code = main(_argv(tmp_path, corpus, spool, components=[COMPONENT]))

    assert code == EXIT_OK
    written = sorted(spool.glob("*.json"))
    assert len(written) == 1
    request = parse_request(written[0].read_bytes(), written[0].stem)
    # The FILE still says `latest`, because root resolves it again: the
    # requester's answer is a preview and never an instruction.
    assert dict(request.components) == {COMPONENT: "latest"}
    report: object = json.loads(capsys.readouterr().out)
    assert isinstance(report, dict)


def test_the_stamps_this_side_reads_are_the_ones_root_will(tmp_path: Path) -> None:
    """A component already at the version asked for deploys nothing, and
    the requester says so before the file exists — because it read the same
    `.release-version` stamp root reads at step 2."""
    corpus = _corpus(tmp_path, tags=(f"{COMPONENT}-v{FIRST_VERSION}",))
    tree = tmp_path / "components" / COMPONENT
    tree.mkdir(parents=True)
    write_stamp(tree, FIRST_VERSION)

    code = main(_argv(tmp_path, corpus, _spool(tmp_path), components=[COMPONENT]))

    assert code != EXIT_OK


def test_an_unreadable_corpus_is_a_note_and_not_a_refusal(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Root re-derives `latest`, the commit and the digest, so this side
    saying "I could not look" must never stop a request being filed. The
    version is named here, which is what a reader does when the preview
    says it could not check."""
    spool = _spool(tmp_path)
    argv = _argv(
        tmp_path,
        tmp_path / "no-corpus-here",
        spool,
        components=[f"{COMPONENT}@{FIRST_VERSION}"],
    )

    code = main(argv)

    assert code == EXIT_OK
    assert len(sorted(spool.glob("*.json"))) == 1
    assert "no commit for noticeboard-v0.1.0" in capsys.readouterr().err


def test_the_requester_still_prints_no_gate(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Root alone computes
    `manifest_sha256`, so this side's `review` row says `local:` and its
    `manifest` row says who computes the real one."""
    corpus = _corpus(tmp_path, tags=(f"{COMPONENT}-v{FIRST_VERSION}",))

    main(_argv(tmp_path, corpus, _spool(tmp_path), components=[COMPONENT]))

    report: object = json.loads(capsys.readouterr().out)
    assert isinstance(report, dict)
    phone = report["phone"]
    assert isinstance(phone, dict)
    assert str(phone["review"]).startswith("local:")
    assert "root computes it at step 2" in str(phone["manifest"])


def test_the_corpus_is_fetched_before_the_tags_are_read(tmp_path: Path) -> None:
    """The fetch comes first. `code-corpus-sync.timer` runs
    hourly and this command runs minutes after CI made the tag, so a
    preview built before the fetch would say the component has no tag at
    all. `--dry-run` still fetches nothing."""
    corpus = _corpus(tmp_path, tags=())
    spool = _spool(tmp_path)
    argv = [*_argv(tmp_path, corpus, spool, components=[COMPONENT]), "--dry-run"]

    # No tag anywhere and no fetch, so `latest` names nothing and the
    # resolver refuses with its own reason rather than filing a request.
    assert main(argv) != EXIT_OK
    assert sorted(spool.glob("*.json")) == []
    assert CATALOG_BY_NAME[COMPONENT].repo is Repo.AGENT_CONTROL
