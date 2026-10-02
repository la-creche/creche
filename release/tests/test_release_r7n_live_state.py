"""Where root's answers come from, and what a planted one can do.

Three readers make contract 06 §11's document, and each is proved here
against the thing it really reads.

1. `latest` — the GitHub API, answered by a fixture.
2. `facts.sha` — the GitHub API again, which is `provenance.tag_commit`.
3. `facts.input_digest` — a REAL git repository under `tmp_path`, because a
   digest a fixture computes proves the fixture. `release/AGENTS.md` allows
   exactly this and nothing further: no host, no network, no remote.

The one attack this module exists for is the corpus clone. It is the operator's,
`code-corpus-sync.timer` fetches it with `--tags`, and anything running as
the operator can write a tag into it. `test_a_planted_tag_in_the_clone_stops_at_
the_predicate` is what that buys an attacker.
"""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import pytest
from agent_release.catalog import CATALOG_BY_NAME
from agent_release.errors import Refusal, RefusalCode
from agent_release.executor.drain import root_readers
from agent_release.executor.live_state import (
    Readers,
    build_state,
    write_manifest_stamp,
    write_stamp,
)
from agent_release.executor.provenance import REFS_MAX, Accept, ApiReply, newest_version
from agent_release.executor.source import (
    ABSENT_OBJECT,
    digest_of,
    digest_paths,
    input_digest,
    newest_tagged_version,
    tag_names,
    tag_sha,
)
from release_executor_fixtures import FakeApi, FakeRun, fake_host, fake_readers, stamp_tree
from release_fixtures import manifest_text, provides_entry

#: The component this module reads a digest for. `view/` is its subtree and
#: `uv.lock` is the lock file that decides what its build installs, so its
#: digest has two inputs and proves the ordering as well as the hash.
COMPONENT = "ui"
SUBTREE = "view"

OTHER_SHA = "0" * 40


# -- a real repository, small enough to make in a test --------------------


def _git(repo: Path, *args: str) -> str:
    """One git command with an environment that reads no user's config, so
    the test answers the same on a machine whose owner has a `~/.gitconfig`
    and on one that has none."""
    done = subprocess.run(
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

    return done.stdout


def _run_git(argv: list[str], cwd: Path) -> str | None:
    """`source.GitRunFn`, as a test provides it: the same shape root's and
    the requester's runners have, answering stdout or None."""
    done = subprocess.run(list(argv), cwd=str(cwd), capture_output=True, text=True, check=False)
    if done.returncode != 0:
        return None

    return done.stdout


def _make_clone(tmp_path: Path, *, lock: str = "one\n") -> Path:
    """A repository shaped like agent-control: a `view/` subtree, a
    `uv.lock` beside it, and a file under neither."""
    repo = tmp_path / "agent-control"
    (repo / SUBTREE).mkdir(parents=True)
    repo.chmod(0o755)
    (repo / SUBTREE / "component.yaml").write_text("name: ui\n", encoding="utf-8")
    (repo / "uv.lock").write_text(lock, encoding="utf-8")
    (repo / "README.md").write_text("outside both\n", encoding="utf-8")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "first")

    return repo


def _head(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD").strip()


# -- facts.input_digest ---------------------------------------------------


@pytest.mark.slow
def test_the_digest_is_the_git_object_ids_of_the_declared_paths(tmp_path: Path) -> None:
    """Contract 06 §9's recipe, computed independently by the test.

    A second machine reproduces it with git alone: one `rev-parse` per
    path, sorted, one `<path> <object id>` line each, SHA-256 of the bytes.
    """
    repo = _make_clone(tmp_path)
    sha = _head(repo)
    rows = [
        (path, _git(repo, "rev-parse", f"{sha}:{path}").strip()) for path in ("uv.lock", SUBTREE)
    ]
    text = "".join(f"{path} {found}\n" for path, found in rows)
    expected = "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()

    assert input_digest(_run_git, repo, COMPONENT, sha) == expected
    assert digest_paths(COMPONENT) == ("uv.lock", SUBTREE)


@pytest.mark.slow
def test_a_change_under_the_component_changes_its_digest(tmp_path: Path) -> None:
    """The point of the field: a source change is a different digest, so a
    reader of two ledger entries can tell one build's inputs from the
    other's without cloning either."""
    repo = _make_clone(tmp_path)
    before = input_digest(_run_git, repo, COMPONENT, _head(repo))
    (repo / SUBTREE / "component.yaml").write_text("name: ui\nkind: venv\n", encoding="utf-8")
    _git(repo, "commit", "-qam", "second")

    assert input_digest(_run_git, repo, COMPONENT, _head(repo)) != before


@pytest.mark.slow
def test_a_change_outside_the_component_and_its_lock_does_not(tmp_path: Path) -> None:
    """`README.md` is in neither the subtree nor the lock file, so it is
    not an input to what this component builds."""
    repo = _make_clone(tmp_path)
    before = input_digest(_run_git, repo, COMPONENT, _head(repo))
    (repo / "README.md").write_text("still outside both\n", encoding="utf-8")
    _git(repo, "commit", "-qam", "second")

    assert input_digest(_run_git, repo, COMPONENT, _head(repo)) == before


@pytest.mark.slow
def test_a_commit_the_clone_does_not_hold_has_no_digest(tmp_path: Path) -> None:
    """The corpus is refreshed hourly, so a release filed minutes after a
    merge names a commit it has not got. That is None and a note, and the
    fetch at step 8 refuses with the command that fixes it."""
    repo = _make_clone(tmp_path)

    assert input_digest(_run_git, repo, COMPONENT, OTHER_SHA) is None


@pytest.mark.slow
def test_a_path_that_is_absent_at_that_commit_is_written_down(tmp_path: Path) -> None:
    """A component that GAINS a lock file must not hash the same as it did
    before, so an absent path is a marker rather than a skipped line."""
    repo = _make_clone(tmp_path)
    (repo / "uv.lock").unlink()
    _git(repo, "commit", "-qam", "no lock")
    sha = _head(repo)
    subtree = _git(repo, "rev-parse", f"{sha}:{SUBTREE}").strip()

    assert input_digest(_run_git, repo, COMPONENT, sha) == digest_of(
        [("uv.lock", ABSENT_OBJECT), (SUBTREE, subtree)]
    )


# -- the requester's tag reader, and what a planted tag buys --------------


@pytest.mark.slow
def test_the_clones_own_tags_answer_the_newest_version(tmp_path: Path) -> None:
    repo = _make_clone(tmp_path)
    for tag in ("ui-v0.1.0", "ui-v0.2.0", "ui-v0.10.0", "pep-v9.9.9", "v0.13.5"):
        _git(repo, "tag", tag)

    found = tag_names(_run_git, repo, COMPONENT)

    assert newest_tagged_version(found, COMPONENT) == "0.10.0"
    assert tag_sha(_run_git, repo, COMPONENT, "0.10.0") == _head(repo)


@pytest.mark.slow
def test_a_planted_tag_in_the_clone_stops_at_the_predicate(tmp_path: Path) -> None:
    """The corpus is the operator's and anything running as the operator can write a tag
    into it. So: plant `ui-v9.9.9` and read the whole path it travels.

    The requester believes it, because the requester is advisory and says
    so. ROOT does not read the clone's tags at all — `latest` and the
    commit both come from GitHub — so the planted version gets exactly as
    far as asking GitHub for a Release that does not exist, which is the
    provenance predicate's own P2, at step 2 rather than step 3. Nothing
    is fetched, nothing is built and nothing is swapped.
    """
    repo = _make_clone(tmp_path)
    _git(repo, "tag", "ui-v9.9.9")

    planted = newest_tagged_version(tag_names(_run_git, repo, COMPONENT), COMPONENT)
    assert planted == "9.9.9"

    # Root, handed that version by a request naming it. Its readers ask
    # GitHub, which has no such tag: `FakeApi` answers 404 for every path
    # it was not given, and `tag_commit` is P2.
    host = fake_host(tmp_path, FakeRun())
    readers = root_readers(host, FakeApi({}))

    with pytest.raises(Refusal) as raised:
        build_state(host.install_roots, {COMPONENT: planted}, readers)

    assert raised.value.code is RefusalCode.P2


# -- root's tag reader ----------------------------------------------------


def _refs(*names: str) -> FakeApi:
    base = "/repos/example-owner/agent-control/git/matching-refs/tags/ui-v"

    return FakeApi({base: [{"ref": f"refs/tags/{one}"} for one in names]})


def test_root_reads_the_newest_matching_ref(tmp_path: Path) -> None:
    del tmp_path

    assert newest_version(_refs("ui-v0.1.0", "ui-v0.10.0", "ui-v0.9.0"), "ui", "agent-control")


def test_a_component_with_no_tag_answers_nothing() -> None:
    """A component with no tag at all is not a failure: `latest` names
    nothing and `resolve` refuses the request with its own reason."""
    assert newest_version(_refs(), "ui", "agent-control") is None


def test_a_ref_that_is_not_this_components_tag_is_ignored() -> None:
    """The repository's own history holds `v*` and `schema-v*`, which
    contract 06 §2 does not reuse."""
    assert newest_version(_refs("v0.13.5", "schema-v0.11.3"), "ui", "agent-control") is None


def test_a_full_page_of_refs_is_refused_rather_than_guessed() -> None:
    """GitHub sorts refs alphabetically, so a full page means the newest
    may be on a page root did not read. It says so instead of naming the
    highest of a prefix it cannot bound."""
    many = _refs(*[f"ui-v0.0.{index}" for index in range(REFS_MAX)])

    with pytest.raises(Refusal) as raised:
        newest_version(many, "ui", "agent-control")

    assert raised.value.code is RefusalCode.STATE
    assert "one page" in raised.value.detail


def test_an_unreachable_api_is_a_refusal_and_never_an_empty_answer() -> None:
    """Invariant 19's fail-closed end. A reader that cannot ask has not
    answered `no tag`."""

    def unreachable(path: str, accept: Accept) -> ApiReply:
        del path, accept
        raise OSError("no route to host")

    with pytest.raises(Refusal) as raised:
        newest_version(unreachable, "ui", "agent-control")

    assert raised.value.code is RefusalCode.STATE


# -- the document root builds ---------------------------------------------


def test_provided_comes_from_the_stamped_manifests(tmp_path: Path) -> None:
    """Contract 06 §11's `provided`: what the LIVE provider of a contract
    provides. It is read out of the `component.yaml` the last release
    stamped into the tree, which is a file root wrote into a tree it owns.
    """
    roots = (tmp_path / "components",)
    stamp_tree(roots[0], "sessiond", "1.4.7")
    write_manifest_stamp(
        roots[0] / "sessiond",
        manifest_text("sessiond", provides=provides_entry("session-api", 1, 4)),
    )

    built = build_state(roots, {}, Readers())

    assert built.state.provided == {"session-api": (1, 4)}
    assert built.trees["sessiond"].present is True


def test_a_stamped_manifest_claiming_another_components_contract_is_dropped(
    tmp_path: Path,
) -> None:
    """Rule C3 applied to what is live: only the component contract 06 §3
    names as a contract's owner is read for it."""
    roots = (tmp_path / "components",)
    stamp_tree(roots[0], "ui", "0.1.0")
    write_manifest_stamp(
        roots[0] / "ui", manifest_text("ui", provides=provides_entry("session-api", 9, 9))
    )

    assert build_state(roots, {}, Readers()).state.provided == {}


def test_an_unstamped_tree_is_present_and_has_no_version(tmp_path: Path) -> None:
    """Every tree the cutover or the visit made, until a release stamps
    it. `present` is what keeps it from
    reading as "this component is not installed", which made the first
    release refuse at step 4 with C1."""
    roots = (tmp_path / "components",)
    (roots[0] / "sessiond").mkdir(parents=True)

    tree = build_state(roots, {}, Readers()).trees["sessiond"]

    assert tree.present is True
    assert tree.version is None
    assert tree.manifest is None


def test_facts_are_built_only_for_what_the_release_acts_on(tmp_path: Path) -> None:
    """A tag read for a component this release does not touch is a call
    that can fail and stop a release it has nothing to do with."""
    roots = (tmp_path / "components",)
    asked: list[str] = []

    def newest(component: str) -> str | None:
        asked.append(component)

        return "0.1.0"

    readers = Readers(
        newest_version=newest,
        tag_sha=fake_readers().tag_sha,
        input_digest=fake_readers().input_digest,
    )
    built = build_state(roots, {"ui": "latest"}, readers)

    assert asked == ["ui"]
    assert set(built.state.facts) == {"ui"}
    assert built.state.facts["ui"].sha is not None


def test_a_version_that_resolves_to_no_commit_is_a_note_and_no_facts(tmp_path: Path) -> None:
    """The requester's end: it says what it could not check rather than
    refusing, because root re-derives all of it at step 2."""
    roots = (tmp_path / "components",)
    stamp_tree(roots[0], "ui", "0.1.0")
    write_stamp(roots[0] / "ui", "0.1.0")

    built = build_state(roots, {"ui": "0.2.0"}, Readers(newest_version=lambda _: "0.2.0"))

    assert built.state.facts == {}
    assert built.notes == ("no commit for ui-v0.2.0: its source facts are unknown here",)


def test_the_catalog_decides_which_paths_a_digest_covers() -> None:
    """A component whose subtree IS the whole repository already carries
    the lock file inside it, so it has one input and not two."""
    assert digest_paths("mcp-servers") == (".",)
    assert CATALOG_BY_NAME["mcp-servers"].path == "."
