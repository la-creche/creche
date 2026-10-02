"""Tags allocated at merge: contract 06 §2.1's four cases, and pain 1 itself."""

from __future__ import annotations

import pytest
from agent_release.allocate import (
    MAX_INPUT_LINES,
    REPO_ROOT_PATH,
    BumpLevel,
    TagOutcome,
    TagPlan,
    next_version,
    plan_tags,
    read_level,
    read_lines,
    tagged_here,
    touched,
    untagged,
)
from agent_release.catalog import CATALOG, CATALOG_BY_NAME, RETIRING, Kind, Repo
from agent_release.errors import Refusal, RefusalCode

#: The tags the repository already carries under the OLD schemes. Contract 06
#: §2 keeps them as history and reuses neither prefix.
OLD_TAGS = ("v0.12.43", "v0.12.42", "schema-v0.11.3")

#: Every agent-control component at its first version. A repository in this
#: state has nothing left to bootstrap, so the path rule alone decides.
#: The components a run in agent-control may tag: its rows, minus a retiring
#: one, which is never tagged.
TAGGED = tuple(
    row.name for row in CATALOG if row.repo is Repo.AGENT_CONTROL and row.name not in RETIRING
)
ALL_TAGGED = tuple(f"{name}-v0.1.0" for name in TAGGED)


def _plan(
    paths: tuple[str, ...],
    tags: tuple[str, ...] = (),
    levels: tuple[str, ...] = (),
    at_sha: tuple[str, ...] = (),
) -> tuple[TagPlan, ...]:
    return plan_tags(paths, levels, tags, at_sha, Repo.AGENT_CONTROL)


def _one(plans: tuple[TagPlan, ...], component: str) -> TagPlan:
    return next(item for item in plans if item.component == component)


def test_a_changed_file_names_its_component() -> None:
    assert touched(("pep/src/agent_pep/call.py",), Repo.AGENT_CONTROL) == ("pep",)


def test_the_view_directory_is_the_ui_component() -> None:
    assert touched(("view/src/agent_view/__init__.py",), Repo.AGENT_CONTROL) == ("ui",)


def test_a_docs_only_change_touches_nothing() -> None:
    assert touched(("docs/rework/design.md", "README.md"), Repo.AGENT_CONTROL) == ()


def test_a_whole_repo_component_takes_every_path() -> None:
    assert touched(("anything/at/all.py",), Repo.AGENT_MCP) == ("mcp-servers",)


def test_components_come_back_in_catalog_order() -> None:
    changed = ("release/src/x.py", "pep/src/y.py", "infra/compose.yaml")

    assert touched(changed, Repo.AGENT_CONTROL) == ("pep", "infra", "releasectl")


def test_a_path_that_climbs_out_is_refused() -> None:
    with pytest.raises(Refusal) as caught:
        touched(("../other-repo/pep/x.py",), Repo.AGENT_CONTROL)

    assert caught.value.code is RefusalCode.REQUEST
    assert "not repo-relative" in caught.value.detail


def test_an_absolute_path_is_refused() -> None:
    with pytest.raises(Refusal):
        touched(("/etc/passwd",), Repo.AGENT_CONTROL)


def test_the_default_level_is_patch() -> None:
    assert read_level(("documentation", "needs-review")) is BumpLevel.PATCH


def test_a_label_raises_the_level() -> None:
    assert read_level(("bump:minor",)) is BumpLevel.MINOR


def test_major_beats_minor_on_one_request() -> None:
    assert read_level(("bump:minor", "bump:major")) is BumpLevel.MAJOR


def test_an_unknown_bump_label_stays_patch() -> None:
    assert read_level(("bump:enormous",)) is BumpLevel.PATCH


@pytest.mark.parametrize(
    ("level", "expected"),
    [
        (BumpLevel.PATCH, (2, 0, 4)),
        (BumpLevel.MINOR, (2, 1, 0)),
        (BumpLevel.MAJOR, (3, 0, 0)),
    ],
)
def test_each_level_moves_one_number(level: BumpLevel, expected: tuple[int, int, int]) -> None:
    assert next_version((2, 0, 3), level) == expected


def test_a_component_with_no_tag_starts_at_its_first() -> None:
    assert next_version(None, BumpLevel.MAJOR) == (0, 1, 0)


def test_the_first_tag_of_a_component_is_created() -> None:
    plan = _one(_plan(("pep/src/x.py",), tags=OLD_TAGS), "pep")

    assert plan.outcome is TagOutcome.CREATE
    assert plan.tag == "pep-v0.1.0"
    assert plan.from_version is None


def test_an_old_scheme_tag_is_not_a_version() -> None:
    """`v0.12.43` and `schema-v0.11.3` must not read as any component's."""
    plan = _one(_plan(("schema/src/x.py", "pep/src/x.py"), tags=OLD_TAGS), "pep")

    assert plan.tag == "pep-v0.1.0"


def test_a_patch_bump_follows_the_newest_tag() -> None:
    tags = (*OLD_TAGS, "pep-v0.9.9", "pep-v0.10.0", "pep-v0.2.0")
    plan = _one(_plan(("pep/src/x.py",), tags=tags), "pep")

    assert plan.outcome is TagOutcome.CREATE
    assert plan.tag == "pep-v0.10.1"
    assert plan.from_version == "0.10.0"


def test_a_rerun_on_the_same_commit_is_a_noop() -> None:
    tags = ("pep-v0.1.0", "pep-v0.1.1")
    plan = _one(_plan(("pep/src/x.py",), tags=tags, at_sha=("pep-v0.1.1",)), "pep")

    assert plan.outcome is TagOutcome.NOOP
    assert plan.tag == "pep-v0.1.1"


def test_the_target_is_always_above_every_existing_tag() -> None:
    """Why the planner never emits `CONFLICT`: see `TagOutcome`'s docstring."""
    tags = ("pep-v0.1.0", "pep-v0.1.1", "pep-v0.0.9", "pep-v0.2.0")
    plan = _one(_plan(("pep/src/x.py",), tags=tags), "pep")

    assert plan.outcome is TagOutcome.CREATE
    assert plan.tag == "pep-v0.2.1"
    assert plan.tag not in tags


def test_two_merges_in_a_row_never_collide() -> None:
    """Pain 1, tested directly: two pull requests merged back to back."""
    tags = [*OLD_TAGS, "pep-v0.5.0"]

    first = _one(_plan(("pep/src/one.py",), tags=tuple(tags)), "pep")
    assert first.outcome is TagOutcome.CREATE
    assert first.tag == "pep-v0.5.1"

    # The workflow's concurrency group serializes the two runs, so the second
    # reads a tag list that already holds the first's tag.
    tags.append(first.tag)
    second = _one(_plan(("pep/src/two.py",), tags=tuple(tags)), "pep")

    assert second.outcome is TagOutcome.CREATE
    assert second.tag == "pep-v0.5.2"
    assert second.tag != first.tag


def test_two_merges_of_different_components_are_independent() -> None:
    tags = ("pep-v0.5.0", "sessiond-v1.2.3")

    levels = ("pep\tbump:minor\tpep", "sessiond\tbump:minor\tsessiond")

    plans = _plan(("pep/src/x.py", "sessiond/src/y.py"), tags=tags, levels=levels)

    assert _one(plans, "pep").tag == "pep-v0.6.0"
    assert _one(plans, "sessiond").tag == "sessiond-v1.3.0"


def test_a_label_raises_only_the_component_its_pull_request_changed() -> None:
    """A merge queue lands two pull requests in one push. One carries
    `bump:minor` and changed `sessiond/`, the other changed `pep/`. The
    script writes the label into both ranges, because both hold that merge,
    and only `sessiond` may take it."""
    tags = ("pep-v0.5.0", "sessiond-v1.2.3")
    levels = ("pep\tbump:minor\tsessiond", "sessiond\tbump:minor\tsessiond")

    plans = _plan(("pep/src/x.py", "sessiond/src/y.py"), tags=tags, levels=levels)

    assert _one(plans, "pep").tag == "pep-v0.5.1"
    assert _one(plans, "sessiond").tag == "sessiond-v1.3.0"


def test_the_highest_level_in_a_range_wins() -> None:
    """A range can hold several merged pull requests. The tip's own label
    is not the only one that counts."""
    tags = ("pep-v0.5.0",)
    levels = ("pep\tbump:minor\tpep", "pep\tbump:major\tpep", "pep\tbump:minor\tpep")

    assert _one(_plan(("pep/src/x.py",), tags=tags, levels=levels), "pep").tag == "pep-v1.0.0"


def test_a_label_counts_through_a_bundled_package() -> None:
    """`family/` is `managerd`'s own path (contract 06 §1 rule 9), so a
    labelled pull request that changed only `family/` raises it."""
    tags = ("managerd-v0.1.9",)
    levels = ("managerd\tbump:minor\tfamily",)

    plans = _plan(("managerd\tfamily",), tags=(*ALL_TAGGED, *tags), levels=levels)

    assert _one(plans, "managerd").tag == "managerd-v0.2.0"


@pytest.mark.parametrize(
    "line",
    ["bump:minor", "pep\tbump:minor", "mcp-servers\tbump:minor\tpep", "pep\tbump:minor\t../x"],
)
def test_a_level_line_the_script_would_never_write_is_refused(line: str) -> None:
    with pytest.raises(Refusal):
        _plan(("pep/src/x.py",), tags=ALL_TAGGED, levels=(line,))


def test_the_lock_file_moves_every_venv_component_and_no_other() -> None:
    """`uv sync --frozen` installs what `uv.lock` pins, into every venv
    tree. A dependency upgrade that changed nothing else used to move no
    tag, and shipped only when something else changed the component."""
    plans = _plan(("uv.lock",), tags=ALL_TAGGED)

    ours = [row for row in CATALOG if row.repo is Repo.AGENT_CONTROL]
    venvs = {row.name for row in ours if row.kind is Kind.VENV}

    assert {plan.component for plan in plans} == venvs
    assert "sandbox-image" not in venvs and "infra" not in venvs


def test_a_members_lock_file_is_not_the_roots() -> None:
    """Only the root `uv.lock` is read by every build. A file of that name
    deeper in the tree belongs to whatever directory holds it."""
    plans = _plan(("supervisor/uv.lock",), tags=ALL_TAGGED)

    assert {plan.component for plan in plans} == {"sandbox-image"}


def test_a_merge_that_touches_nothing_plans_nothing() -> None:
    """Contract 06 §2.1's fourth row, once every component carries a tag."""
    assert _plan(("docs/rework/design.md",), tags=ALL_TAGGED) == ()


# ---- the first tags --------------------------------------------------------


def test_a_repository_with_no_component_tag_has_them_all_untagged() -> None:
    assert untagged(OLD_TAGS, Repo.AGENT_CONTROL) == tuple(
        row.name for row in CATALOG if row.repo is Repo.AGENT_CONTROL
    )


def test_a_component_that_carries_a_tag_is_not_untagged() -> None:
    assert "pep" not in untagged((*OLD_TAGS, "pep-v0.1.0"), Repo.AGENT_CONTROL)


def test_another_repositorys_components_are_never_bootstrapped() -> None:
    """`touched` is scoped by repo and so is this. A run in agent-control
    must not invent a tag for `mcp-servers`, whose repo it cannot write."""
    assert untagged((), Repo.AGENT_CONTROL) == tuple(
        row.name for row in CATALOG if row.repo is Repo.AGENT_CONTROL
    )
    assert untagged((), Repo.AGENT_MCP) == ("mcp-servers",)


def test_the_first_run_tags_every_component_the_commit_did_not_touch() -> None:
    """Item 1: `main` carries no component tag, so the operator has no `ui-v…` to
    name. One dispatch gives every component its first version."""
    plans = _plan(("infra/secrets.enc.env", "pyproject.toml"), tags=OLD_TAGS)

    made = {item.tag for item in plans}
    assert made == set(ALL_TAGGED)
    assert all(item.outcome is TagOutcome.CREATE for item in plans)


def test_a_retiring_component_is_never_tagged() -> None:
    """A tag is a version somebody can release. A component on its way out
    of the catalog gets no new one: not a first tag, and not a bump when a
    file under its path changes (deleting its directory changes them all)."""
    (retiring,) = RETIRING
    row = CATALOG_BY_NAME[retiring]

    first = _plan(("pyproject.toml",), tags=OLD_TAGS)
    bumped = _plan((f"{row.path}/compose.yaml",), tags=ALL_TAGGED)

    assert retiring not in {item.component for item in first}
    assert retiring not in {item.component for item in bumped}


def test_a_docs_only_merge_still_gives_an_untagged_component_its_first() -> None:
    """The fourth row of §2.1 holds for a component that HAS a version. One
    that has none is not releasable at all, so it is not a release this
    withholds — see `plan_tags`'s docstring."""
    plans = _plan(("docs/rework/design.md",), tags=OLD_TAGS)

    assert {item.tag for item in plans} == set(ALL_TAGGED)


def test_a_bump_label_never_raises_a_first_tag() -> None:
    """A component that has never released has no number to bump."""
    plan = _one(_plan((), tags=OLD_TAGS, levels=("pep\tbump:major\tpep",)), "pep")

    assert plan.tag == "pep-v0.1.0"
    assert plan.detail == "first tag"


def test_a_second_run_on_the_first_commit_creates_nothing() -> None:
    """The bootstrap is self-extinguishing: the tags it made are on this
    commit, so a re-run reads them and plans a noop for each.

    The noop rows are VISIBLE: a second run that printed nothing would
    read the same as a docs-only merge.
    """
    plans = _plan((), tags=ALL_TAGGED, at_sha=ALL_TAGGED)

    assert [item.outcome for item in plans] == [TagOutcome.NOOP] * len(ALL_TAGGED)
    assert {item.tag for item in plans} == set(ALL_TAGGED)


def test_a_rerun_before_the_tags_are_fetched_is_still_a_noop() -> None:
    """`--at-sha` is what makes a re-run idempotent. The tag list and the
    at-sha list come from the same checkout, so both hold the first tags."""
    plans = _plan(("pep/src/x.py",), tags=ALL_TAGGED, at_sha=ALL_TAGGED)

    assert all(item.outcome is TagOutcome.NOOP for item in plans)
    assert _one(plans, "pep").tag == "pep-v0.1.0"


def test_a_component_added_later_gets_its_first_tag_unasked() -> None:
    """The reason this is not a `--bootstrap` flag. A catalog row added in a
    year is untagged, and the next run tags it with no flag to remember."""
    every = tuple(row.name for row in CATALOG if row.repo is Repo.AGENT_CONTROL)
    already = tuple(f"{name}-v1.0.0" for name in every if name != "ui")

    plans = _plan(("docs/rework/design.md",), tags=already)

    assert [item.tag for item in plans] == ["ui-v0.1.0"]


def test_read_lines_drops_blank_lines() -> None:
    assert read_lines("a\n\n  b  \n\n", "tags") == ("a", "b")


def test_read_lines_caps_the_line_count() -> None:
    with pytest.raises(Refusal) as caught:
        read_lines("x\n" * (MAX_INPUT_LINES + 1), "tags")

    assert "more than" in caught.value.detail


def test_read_lines_caps_one_lines_length() -> None:
    with pytest.raises(Refusal) as caught:
        read_lines("x" * 513, "tags")

    assert "a line over" in caught.value.detail


# ---- one range per component -----------------------------------------------


def test_a_line_that_names_a_component_counts_for_that_one_only() -> None:
    """A line of `<component>`, a TAB and a path is one line of that
    component's OWN range, not of the head commit's diff."""
    assert touched(("pep\tpep",), Repo.AGENT_CONTROL) == ("pep",)


def test_a_range_line_whose_path_is_another_components_counts_nothing() -> None:
    """`sessiond`'s range holds a change under `pep/`. That is a change to
    `pep` that happened inside `sessiond`'s window, and it tags neither."""
    assert touched(("sessiond\tpep",), Repo.AGENT_CONTROL) == ()


def test_two_ranges_in_one_file_each_tag_their_own_component() -> None:
    lines = ("pep\tpep", "pep\tdocs", "ui\tview", "ui\tdocs")

    assert touched(lines, Repo.AGENT_CONTROL) == ("pep", "ui")


def test_a_range_lines_path_is_checked_like_any_other() -> None:
    with pytest.raises(Refusal) as caught:
        touched(("pep\t../other-repo/pep/x.py",), Repo.AGENT_CONTROL)

    assert "not repo-relative" in caught.value.detail


def test_a_prefix_this_repo_does_not_own_is_read_as_a_path() -> None:
    """Only a component of THIS repo is a prefix. Everything else stays the
    bare path every caller writes."""
    assert touched(("mcp-servers\tanything",), Repo.AGENT_CONTROL) == ()
    assert touched(("mcp-servers\tanything",), Repo.AGENT_MCP) == ("mcp-servers",)


def test_the_gap_a_missed_dispatch_leaves() -> None:
    """The gap a missed run leaves. A red `main` tags nothing, so a missed
    run is a normal case. Merge A changes `view/` and its run is missed.
    Merge B changes only `docs/`. The head commit's own diff names no
    component, so a planner reading it alone would never tag `ui`, and
    nothing would say so.
    """
    head_commit_only = _plan(("docs/rework/design.md",), tags=ALL_TAGGED)
    assert head_commit_only == ()

    since_each_tag = _plan(("ui\tview", "ui\tdocs", "pep\tdocs"), tags=ALL_TAGGED)

    assert [item.tag for item in since_each_tag] == ["ui-v0.1.1"]


def test_a_component_whose_range_holds_nothing_is_not_tagged() -> None:
    """The rule is still per component. A range that changed none of the
    component's own paths allocates nothing for it."""
    assert _plan(("pep\tdocs", "ui\tdocs"), tags=ALL_TAGGED) == ()


# ---- the packages a component's build installs -----------------------------


def test_a_door_change_tags_sessiond() -> None:
    """Contract 06 §1 rule 1: the doors ship inside `sessiond`'s tree."""
    changed = ("door-trigger/src/agent_door_trigger/cli.py",)

    assert touched(changed, Repo.AGENT_CONTROL) == ("sessiond",)


def test_a_family_change_tags_every_component_that_installs_it() -> None:
    """`agent-family` is in `sessiond`'s, `managerd`'s and `ui`'s trees."""
    changed = ("family/src/agent_family/model.py",)

    assert touched(changed, Repo.AGENT_CONTROL) == ("sessiond", "managerd", "ui")


def test_a_release_package_change_tags_pep_too() -> None:
    """`agent-pep` imports the requester, so `agent-release` is in its tree."""
    changed = ("release/src/agent_release/requester/file.py",)

    assert touched(changed, Repo.AGENT_CONTROL) == ("pep", "releasectl")


def test_a_range_that_changed_only_installed_packages_still_tags() -> None:
    """The gap rule 9 closes. A merge changed `door-trigger/` and `family/`
    only. Measured against their own directories, `sessiond` and `ui` found
    nothing and kept tags older than the code their trees would install."""
    lines = ("sessiond\tdoor-trigger", "sessiond\tfamily", "managerd\tfamily", "ui\tfamily")

    plans = _plan(lines, tags=ALL_TAGGED)

    assert [item.tag for item in plans] == ["sessiond-v0.1.1", "managerd-v0.1.1", "ui-v0.1.1"]


# ---- a component already tagged on this commit -----------------------------


def test_a_component_tagged_on_this_commit_is_still_reported() -> None:
    """A re-run's range is empty, because the tag the first run made IS the
    range's base. Without this row the second dispatch would print nothing
    and read exactly like a docs-only merge."""
    plans = _plan((), tags=ALL_TAGGED, at_sha=("pep-v0.1.0",))

    assert [(item.component, item.outcome) for item in plans] == [("pep", TagOutcome.NOOP)]


def test_tagged_here_is_scoped_by_repo() -> None:
    assert tagged_here(("mcp-servers-v0.1.0",), Repo.AGENT_CONTROL) == ()
    assert tagged_here(("mcp-servers-v0.1.0",), Repo.AGENT_MCP) == ("mcp-servers",)


def test_tagged_here_reads_no_old_scheme_tag() -> None:
    assert tagged_here(OLD_TAGS, Repo.AGENT_CONTROL) == ()


# ---- the guard the script's reduction needs --------------------------------


def test_every_component_path_is_one_top_level_name() -> None:
    """`release/bin/allocate-tags.sh` reduces every changed path in a range
    to its FIRST segment before it hands the line in, so one range costs a
    handful of lines and not one per changed file. That reduction can only
    reach a component whose path is a top-level entry, or the whole repo.

    A nested path added to the catalog would be tagged by nothing, and
    nothing would say so.
    Deepen the reduction in the script before you add such a row.
    """
    for row in CATALOG:
        assert row.path == REPO_ROOT_PATH or "/" not in row.path, row.name


def test_every_installed_directory_is_one_top_level_name() -> None:
    """The same reduction, for `CatalogRow.bundles`: a nested directory
    there would be reached by no line, and its changes would tag nothing."""
    for row in CATALOG:
        for top in row.bundles:
            assert top != REPO_ROOT_PATH and "/" not in top, row.name
