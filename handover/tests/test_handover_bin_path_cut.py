"""The cut that `handover/bin/allocate-tags.sh` gives each changed path.

One range can hold thousands of changed files, so the script hands the
planner a prefix of each path and not the path. The prefix was the first
segment, which reaches a component only when each of its directories is a
top-level entry. A crate under `rust/crates/` is not one.

The planner makes the cut now, because the planner holds the catalog. The
property held here: a cut path reaches exactly the components that the whole
path reaches.
"""

from __future__ import annotations

import subprocess
from dataclasses import replace
from pathlib import Path

import pytest
from handover.allocate import cut_path, cut_paths, is_prose, touched
from handover.catalog import BINARY_BUILD_FILES, CATALOG, CATALOG_BY_NAME, Kind, Repo
from handover.cli import EXIT_OK, main
from handover_bin_fixtures import BINARY_NAME, NESTED_BUNDLE, catalog_with, use_binary_catalog

from handover import allocate

CONTROL = Repo.AGENT_CONTROL

#: Changed paths of every shape a range can hold: under a component, under
#: a bundle, under the Cargo workspace, a root file and a document.
SAMPLES = (
    "chaperone/src/chaperone/call.py",
    "family/src/agent_family/model.py",
    "door-trigger/src/agent_door_trigger/cli.py",
    "noticeboard/component.yaml",
    "uv.lock",
    "pyproject.toml",
    "docs/rework/design.md",
    "rust",
    "rust/README.md",
    "rust/crates",
    "rust/crates/other/src/lib.rs",
    "rust/crates/creche-contracts-extra/src/lib.rs",
    NESTED_BUNDLE,
    f"{NESTED_BUNDLE}/Cargo.toml",
    f"{NESTED_BUNDLE}/src/deep/er/lib.rs",
    *BINARY_BUILD_FILES,
)


def _reached(path: str) -> tuple[str, ...]:
    return touched((path,), CONTROL)


# -- the catalog as it is ----------------------------------------------------


@pytest.mark.parametrize("path", SAMPLES)
def test_todays_catalog_cuts_to_the_first_segment(path: str) -> None:
    """No row of today's catalog names a nested directory, so the cut is
    the one the script made before."""
    assert cut_path(path, CONTROL) == path.split("/", 1)[0]


@pytest.mark.parametrize("path", SAMPLES)
def test_a_cut_path_reaches_what_the_whole_path_reaches(path: str) -> None:
    assert _reached(cut_path(path, CONTROL)) == _reached(path)


# -- a nested bundle ---------------------------------------------------------


@pytest.mark.parametrize("path", SAMPLES)
def test_the_cut_keeps_what_a_nested_bundle_reaches(
    monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    use_binary_catalog(monkeypatch)

    assert _reached(cut_path(path, CONTROL)) == _reached(path)


def test_a_path_under_a_nested_bundle_is_cut_to_the_bundle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    use_binary_catalog(monkeypatch)
    path = f"{NESTED_BUNDLE}/src/lib.rs"

    assert cut_path(path, CONTROL) == NESTED_BUNDLE
    assert _reached(path) == (BINARY_NAME,)


def test_a_path_beside_a_nested_bundle_is_cut_to_the_first_segment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A crate that no build bundles moves no tag, and its line stays one
    short line however many of its files changed."""
    use_binary_catalog(monkeypatch)

    assert cut_path("rust/crates/other/src/lib.rs", CONTROL) == "rust"
    assert cut_path("rust/crates/creche-contracts-extra/src/lib.rs", CONTROL) == "rust"
    assert _reached("rust/crates/other/src/lib.rs") == ()


@pytest.mark.parametrize("path", BINARY_BUILD_FILES)
def test_a_workspace_file_is_not_cut(monkeypatch: pytest.MonkeyPatch, path: str) -> None:
    """The rule for a workspace file matches the whole path, so a cut to
    `rust` would lose the one line that moves a binary component."""
    use_binary_catalog(monkeypatch)

    assert cut_path(path, CONTROL) == path
    assert _reached(path) == (BINARY_NAME,)


def test_the_longest_holding_path_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    """One component bundles `rust/crates`, a second one a crate under it.
    A change in that crate reaches both, before the cut and after it."""
    outer = replace(CATALOG_BY_NAME["caregiver"], bundles=("rust/crates",))
    inner = replace(CATALOG_BY_NAME[BINARY_NAME], bundles=(NESTED_BUNDLE,))
    rows = tuple({outer.name: outer, inner.name: inner}.get(row.name, row) for row in CATALOG)
    monkeypatch.setattr(allocate, "CATALOG", rows)
    path = f"{NESTED_BUNDLE}/src/lib.rs"

    assert cut_path(path, CONTROL) == NESTED_BUNDLE
    assert _reached(cut_path(path, CONTROL)) == _reached(path) == ("caregiver", BINARY_NAME)
    assert cut_path("rust/crates/other/src/lib.rs", CONTROL) == "rust/crates"


def test_a_nested_component_path_is_reached(monkeypatch: pytest.MonkeyPatch) -> None:
    """The cut reads a row's own `path` as it reads a bundle."""
    row = replace(CATALOG_BY_NAME[BINARY_NAME], path="rust/crates/noticeboard", kind=Kind.BINARY)
    monkeypatch.setattr(allocate, "CATALOG", catalog_with(row))
    path = "rust/crates/noticeboard/src/main.rs"

    assert cut_path(path, CONTROL) == "rust/crates/noticeboard"
    assert _reached(cut_path(path, CONTROL)) == _reached(path) == (BINARY_NAME,)


def test_another_repositorys_nested_path_does_not_shape_the_cut(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cut is scoped by repository, as every decision of the planner is."""
    use_binary_catalog(monkeypatch)

    assert cut_path(f"{NESTED_BUNDLE}/src/lib.rs", Repo.AGENT_MCP) == "rust"


# -- many lines in, few lines out --------------------------------------------


def test_the_cut_of_a_range_is_sorted_and_holds_each_line_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    use_binary_catalog(monkeypatch)
    changed = [
        f"{NESTED_BUNDLE}/src/lib.rs",
        f"{NESTED_BUNDLE}/src/wire.rs",
        "rust/crates/other/src/lib.rs",
        "rust/Cargo.lock",
        "",
        "chaperone/a.py",
        "chaperone/b.py",
    ]

    assert cut_paths(changed, CONTROL) == ("chaperone", "rust", "rust/Cargo.lock", NESTED_BUNDLE)


def test_a_long_range_is_cut_to_a_few_lines() -> None:
    """The reason for the cut: a range of fifty thousand files is past the
    planner's own input cap, and its cut is one line per directory."""
    changed = (f"chaperone/src/chaperone/file_{index}.py" for index in range(50_000))

    assert cut_paths(changed, CONTROL) == ("chaperone",)


# -- the command the script runs ---------------------------------------------


def test_the_command_prints_the_cut_of_a_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    changed = tmp_path / "changed"
    lines = "chaperone/a.py\nchaperone/b.py\n\ndocs/x.md\ndocs/figure.svg\nplaypen/AGENTS.md\n"
    changed.write_text(lines, encoding="utf-8")

    code = main(["allocate-tags", "--repo", str(CONTROL), "--cut", str(changed)])

    assert code == EXIT_OK
    # Prose is dropped before the cut: neither `.md` line prints anything.
    assert capsys.readouterr().out == "chaperone\ndocs\n"


def test_the_command_cuts_a_file_past_the_planners_line_cap(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The planner refuses an input of more than `MAX_INPUT_LINES` lines.
    The cut is what keeps a long range under that cap, so it has none."""
    changed = tmp_path / "changed"
    lines = (f"noticeboard/file_{index}.py\n" for index in range(allocate.MAX_INPUT_LINES + 1))
    changed.write_text("".join(lines), encoding="utf-8")

    code = main(["allocate-tags", "--repo", str(CONTROL), "--cut", str(changed)])

    assert code == EXIT_OK
    assert capsys.readouterr().out == "noticeboard\n"


def test_the_command_reads_a_path_that_is_not_utf8(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A changed path is bytes that a committer chose. One that does not
    decode is still cut by its first segment, and stops no run."""
    changed = tmp_path / "changed"
    changed.write_bytes(b"chaperone/caf\xff.py\n")

    code = main(["allocate-tags", "--repo", str(CONTROL), "--cut", str(changed)])

    assert code == EXIT_OK
    assert capsys.readouterr().out == "chaperone\n"


# -- prose moves no tag ------------------------------------------------------

#: `bin/lib/docsrule.sh`, the one copy of the rule the hook and CI source.
DOCS_RULE = Path(__file__).resolve().parents[2] / "bin" / "lib" / "docsrule.sh"

#: Paths of every shape the rule must judge: prose under a component, prose
#: at the root, a document under `tests/` or `fixtures/`, and code.
PROSE_SAMPLES = (
    "playpen/AGENTS.md",
    "README.md",
    "docs/writing-standard.md",
    "rust/crates/agent-family/AGENTS.md",
    "caregiver/tests/notes.md",
    "integration/fixtures/eq-registry/families/chat/instructions.md",
    "tests/README.md",
    "playpen/src/index.ts",
    "playpen/component.yaml",
    "noticeboard/src/noticeboard/pages.py",
    "handover/md",
    "chaperone/notes.md.py",
    "x.MD",
)


def test_a_prose_only_range_is_cut_to_nothing() -> None:
    """A `playpen` release rebuilds both sandbox images, and the fleet then
    replaces every sandbox. An edit to `playpen/AGENTS.md` changes nothing
    that runs, so it must not ask for one."""
    assert cut_paths(["playpen/AGENTS.md", "caregiver/AGENTS.md", "README.md"], CONTROL) == ()


def test_a_range_of_prose_and_code_keeps_the_code() -> None:
    """The window of a component is measured from its own newest tag. A
    code change that follows a prose change tags the component once, and
    that tag covers both."""
    changed = ["playpen/AGENTS.md", "playpen/src/index.ts", "caregiver/AGENTS.md"]

    assert cut_paths(changed, CONTROL) == ("playpen",)


def test_a_document_under_tests_or_fixtures_is_not_prose() -> None:
    """It is a fixture: the instructions of a registry family, or a skill.
    A test loads it, so a change to it is a change to what the suite proves."""
    assert cut_paths(["caregiver/tests/notes.md"], CONTROL) == ("caregiver",)


def test_prose_at_the_root_moves_no_whole_repository_component() -> None:
    """`mcp-servers` is the whole MCP repository, so each path reaches it.
    Its README must not."""
    assert cut_paths(["README.md", "docs/servers.md"], Repo.AGENT_MCP) == ()
    assert cut_paths(["README.md", "src/agent_mcp/x.py"], Repo.AGENT_MCP) == ("src",)


def _hook_calls_it_prose(path: str) -> bool:
    done = subprocess.run(
        ["bash", "-c", '. "$1" && docs_path "$2"', "docs_path", str(DOCS_RULE), path],
        capture_output=True,
        check=False,
    )

    return done.returncode == 0


@pytest.mark.parametrize("path", PROSE_SAMPLES)
def test_the_planner_and_the_hook_hold_one_prose_rule(path: str) -> None:
    """Two languages, one rule. The hook and CI ask `docs_path` whether a
    change is prose and run fewer tests when it is. The planner asks
    `is_prose` whether a change moves a tag. A path the two judged
    differently would be tested as prose and released as code."""
    assert is_prose(path) is _hook_calls_it_prose(path)
