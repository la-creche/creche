"""The reach diff finds each gain and each narrowing (`reach.py`).

Each test copies the fixture registry to a base and a head root, edits the
head as one registry PR would, and reads the findings of `reach_diff`. The
fixture registry is the one the other tests of this package read."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from agent_family.cli import EXIT_INVALID, EXIT_OK, EXIT_USAGE, STEP_SUMMARY_ENV, main
from agent_family.reach import Diff, Finding, Gain, Narrowing, markdown, reach_diff
from family_helpers import FIXTURES

CODE = "families/code/family.toml"
CHAT = "families/chat/family.toml"
SCRUM = "families/scrum-lead/family.toml"
KAGI = "mcp/kagi/server.toml"
PLATFORM = "mcp/github-platform/server.toml"
GITHUB_CODE = "mcp/github-code/server.toml"
VIKUNJA_HA = "mcp/vikunja-home-assistant/server.toml"
PUSH = "github-platform__push_files"
DEV = '["main", "dev"]'


@pytest.fixture
def roots(tmp_path: Path) -> tuple[Path, Path]:
    base = tmp_path / "base"
    head = tmp_path / "head"
    shutil.copytree(FIXTURES, base)
    shutil.copytree(FIXTURES, head)
    return base, head


def edit(root: Path, file: str, old: str, new: str) -> None:
    path = root / file
    text = path.read_text(encoding="utf-8")
    assert text.count(old) == 1, f"{old!r} is not once in {file}"
    path.write_text(text.replace(old, new), encoding="utf-8")


def found(diff: Diff) -> set[tuple[str, str, str]]:
    return {(one.family, one.kind, one.detail) for one in (*diff.gains, *diff.narrowings)}


def only(diff: Diff, *expected: tuple[str, Gain | Narrowing, str]) -> None:
    assert found(diff) == {(family, str(kind), detail) for family, kind, detail in expected}


# --- no change ----------------------------------------------------------------


def test_same_registry_has_no_finding(roots: tuple[Path, Path]) -> None:
    diff = reach_diff(*roots)
    assert diff == Diff((), (), ())
    assert not diff.widens


def test_text_only_change_has_no_finding(roots: tuple[Path, Path]) -> None:
    _, head = roots
    edit(head, CODE, "egress = [\n", "# A new comment.\negress = [\n")
    (head / "families/code/instructions.md").write_text("New prose.\n", encoding="utf-8")
    assert found(reach_diff(*roots)) == set()


def test_egress_spelling_is_one_entry(roots: tuple[Path, Path]) -> None:
    _, head = roots
    edit(head, CODE, '    "github.com",\n', '    "GitHub.com:443",\n')
    assert found(reach_diff(*roots)) == set()


# --- credentials --------------------------------------------------------------


def test_server_secret_counts_against_each_granting_family(roots: tuple[Path, Path]) -> None:
    _, head = roots
    edit(
        head,
        KAGI,
        'run.env.KAGI_API_KEY = "secret:kagi_api_key"\n',
        'run.env.KAGI_API_KEY = "secret:kagi_api_key"\n'
        'run.env.KAGI_ADMIN_KEY = "secret:kagi_admin"\n',
    )
    only(
        reach_diff(*roots),
        ("chat", Gain.NEW_CREDENTIAL, "kagi_admin through kagi"),
        ("code", Gain.NEW_CREDENTIAL, "kagi_admin through kagi"),
    )


def test_known_secret_through_a_new_server_is_new(roots: tuple[Path, Path]) -> None:
    """`code` holds `github_token_platform` through `github-platform`. A
    second server with the same token is a second reach of it."""
    _, head = roots
    edit(head, KAGI, '"secret:kagi_api_key"', '"secret:github_token_platform"')
    only(
        reach_diff(*roots),
        ("chat", Gain.NEW_CREDENTIAL, "github_token_platform through kagi"),
        ("code", Gain.NEW_CREDENTIAL, "github_token_platform through kagi"),
        ("chat", Narrowing.CREDENTIAL_DROPPED, "kagi_api_key through kagi"),
        ("code", Narrowing.CREDENTIAL_DROPPED, "kagi_api_key through kagi"),
    )


# --- tools --------------------------------------------------------------------


def test_catalog_add_under_all_is_a_write_tool(roots: tuple[Path, Path]) -> None:
    """`chat` and `code` grant `all`. `home-assistant-worker` names its
    tools, so the catalog add does not reach it."""
    _, head = roots
    edit(
        head,
        VIKUNJA_HA,
        "tools = [\n",
        "tools = [\n"
        '    { name = "close_ticket", description = "Close one ticket.", write = true },\n',
    )
    only(
        reach_diff(*roots),
        ("chat", Gain.NEW_WRITE_TOOL, "vikunja-home-assistant__close_ticket"),
        ("code", Gain.NEW_WRITE_TOOL, "vikunja-home-assistant__close_ticket"),
    )


def test_write_flag_flip_is_a_write_tool(roots: tuple[Path, Path]) -> None:
    _, head = roots
    edit(
        head,
        KAGI,
        'description = "Fetch one URL and return its readable text." }',
        'description = "Fetch one URL and return its readable text.", write = true }',
    )
    detail = "kagi__kagi_extract is now write = true"
    only(
        reach_diff(*roots),
        ("chat", Gain.NEW_WRITE_TOOL, detail),
        ("code", Gain.NEW_WRITE_TOOL, detail),
    )


def test_named_read_tool_is_a_new_tool(roots: tuple[Path, Path]) -> None:
    _, head = roots
    edit(
        head,
        CHAT,
        'tools.ha-read = ["ha_get_state", "ha_search", "ha_get_overview", "ha_get_history"]',
        'tools.ha-read = ["ha_get_state", "ha_search", "ha_get_overview", "ha_get_history"]\n'
        'tools.github-code = ["get_commit"]',
    )
    only(
        reach_diff(*roots),
        ("chat", Gain.NEW_TOOL, "github-code__get_commit"),
        ("chat", Gain.NEW_CREDENTIAL, "github_token_code through github-code"),
    )


def test_dropped_grant_only_narrows(roots: tuple[Path, Path]) -> None:
    _, head = roots
    edit(head, CHAT, 'tools.kagi = ["kagi_search_fetch", "kagi_extract"]\n', "")
    diff = reach_diff(*roots)
    assert not diff.widens
    only(
        diff,
        ("chat", Narrowing.TOOL_DROPPED, "kagi__kagi_search_fetch"),
        ("chat", Narrowing.TOOL_DROPPED, "kagi__kagi_extract"),
        ("chat", Narrowing.CREDENTIAL_DROPPED, "kagi_api_key through kagi"),
    )


def test_all_on_an_undeclared_server_fails_closed(roots: tuple[Path, Path]) -> None:
    _, head = roots
    edit(head, CHAT, 'tools.ha = "all"\n', 'tools.ha = "all"\ntools.browser = "all"\n')
    only(reach_diff(*roots), ("chat", Gain.NEW_WRITE_TOOL, "browser__*"))


def test_undeclared_tool_counts_as_write(roots: tuple[Path, Path]) -> None:
    _, head = roots
    edit(
        head,
        CHAT,
        '"kagi_search_fetch", "kagi_extract"]',
        '"kagi_search_fetch", "kagi_extract", "kagi_admin"]',
    )
    only(reach_diff(*roots), ("chat", Gain.NEW_WRITE_TOOL, "kagi__kagi_admin"))


# --- gates --------------------------------------------------------------------


def test_approval_removed_drops_a_gate(roots: tuple[Path, Path]) -> None:
    _, head = roots
    edit(head, SCRUM, 'approval = ["enqueue"]', "approval = []")
    only(
        reach_diff(*roots),
        ("scrum-lead", Gain.GATE_DROPPED, "enqueue no longer waits for approval"),
    )


def test_approval_wildcard_added_only_narrows(roots: tuple[Path, Path]) -> None:
    _, head = roots
    edit(head, CHAT, "approval = []", 'approval = ["kagi__*"]')
    diff = reach_diff(*roots)
    assert not diff.widens
    only(
        diff,
        ("chat", Narrowing.GATE_ADDED, "kagi__kagi_search_fetch now waits for approval"),
        ("chat", Narrowing.GATE_ADDED, "kagi__kagi_extract now waits for approval"),
    )


# --- fences -------------------------------------------------------------------


def test_deny_value_removed_loosens(roots: tuple[Path, Path]) -> None:
    _, head = roots
    edit(
        head,
        PLATFORM,
        'update_pull_request_branch"], arg = "branch", values = ["main", "master"]',
        'update_pull_request_branch"], arg = "branch", values = ["main"]',
    )
    diff = reach_diff(*roots)
    assert (
        "code",
        "fence_loosened",
        "github-platform__push_files branch: 'master' is no longer denied",
    ) in found(diff)
    assert {one.kind for one in diff.gains} == {Gain.FENCE_LOOSENED}
    assert len(diff.gains) == 5


def test_allow_value_added_loosens(roots: tuple[Path, Path]) -> None:
    _, head = roots
    edit(
        head, PLATFORM, '"example-owner/mcp"] },', '"example-owner/mcp", "example-owner/vault"] },'
    )
    diff = reach_diff(*roots)
    expected = "github-platform__get_commit owner/repo: 'example-owner/vault' is allowed"
    assert ("code", "fence_loosened", expected) in found(diff)
    assert {one.kind for one in diff.gains} == {Gain.FENCE_LOOSENED}


def test_allow_list_removed_loosens(roots: tuple[Path, Path]) -> None:
    _, head = roots
    edit(
        head,
        PLATFORM,
        '    { tools = "all", arg = "owner/repo", values = ["example-owner/creche", '
        '"example-owner/registry", "example-owner/mcp"] },\n',
        "",
    )
    diff = reach_diff(*roots)
    expected = "github-platform__get_commit owner/repo: the allow list is gone, so any value passes"
    assert ("code", "fence_loosened", expected) in found(diff)


def test_deny_value_added_only_narrows(roots: tuple[Path, Path]) -> None:
    _, head = roots
    edit(
        head,
        GITHUB_CODE,
        '"example-owner/mcp"] },',
        '"example-owner/mcp", "example-owner/vault"] },',
    )
    diff = reach_diff(*roots)
    assert not diff.widens
    assert {one.kind for one in diff.narrowings} == {Narrowing.FENCE_TIGHTENED}


def test_deny_to_allow_admits_a_denied_value(roots: tuple[Path, Path]) -> None:
    """Base denies `main` and `master` on `branch`. Head allows `main` and
    `dev` only: `main` was denied and is now allowed. `dev` was allowed
    before, as every value that is not denied."""
    _, head = roots
    path = head / PLATFORM
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    deny = next(i for i, line in enumerate(lines) if line.startswith("arg_denies = ["))
    allow = next(i for i, line in enumerate(lines) if line.startswith("arg_allows = ["))
    lines[deny : deny + 3] = ["arg_denies = []\n"]
    lines.insert(allow + 1, f'    {{ tools = ["push_files"], arg = "branch", values = {DEV} }},\n')
    path.write_text("".join(lines), encoding="utf-8")
    diff = reach_diff(*roots)
    assert {one.detail for one in diff.gains if one.detail.startswith(PUSH)} == {
        f"{PUSH} branch: 'main' is allowed"
    }


# --- sandbox reach ------------------------------------------------------------


@pytest.mark.parametrize(
    ("file", "old", "new", "detail"),
    [
        (CHAT, "egress = []", 'egress = ["example.org:8443"]', "egress example.org:8443"),
        (
            CODE,
            'delegates = ["vault-oracle"]',
            'delegates = ["vault-oracle", "coding-sandbox"]',
            "delegate coding-sandbox",
        ),
        (
            CHAT,
            'verbs.enqueue.targets = ["scrum-lead"]',
            'verbs.enqueue.targets = ["scrum-lead", "code-worker"]',
            "verb enqueue:code-worker",
        ),
        (CHAT, "shell = false", "shell = true", "shell"),
        (CHAT, '"glob", "write", "edit"]', '"glob", "write", "edit", "bash"]', "sandbox_tool bash"),
    ],
)
def test_sandbox_gain_is_new_reach(
    roots: tuple[Path, Path], file: str, old: str, new: str, detail: str
) -> None:
    _, head = roots
    edit(head, file, old, new)
    family = file.split("/")[1]
    only(reach_diff(*roots), (family, Gain.NEW_REACH, detail))


def test_new_repo_is_new_reach(roots: tuple[Path, Path]) -> None:
    _, head = roots
    edit(head, CHAT, "shell = false", 'shell = false\nrepos = ["notes"]')
    only(reach_diff(*roots), ("chat", Gain.NEW_REACH, "repo notes"))


def test_entity_scope_widened_on_ha_call(roots: tuple[Path, Path]) -> None:
    _, head = roots
    edit(
        head,
        SCRUM,
        '{ domain = "notify", service = "mobile_app_example_phone" },',
        '{ domain = "notify", service = "mobile_app_example_phone" },\n'
        '    { domain = "light", service = "turn_on", entity_id = "light.office" },',
    )
    only(
        reach_diff(*roots),
        ("scrum-lead", Gain.NEW_REACH, "verb ha_call:light.turn_on:light.office"),
    )


# --- families and files -------------------------------------------------------


def test_new_family_gains_all_its_reach(roots: tuple[Path, Path]) -> None:
    base, _ = roots
    shutil.rmtree(base / "families/vault-oracle")
    # `code` and `chat` delegate to it, and their delegate list is unchanged.
    diff = reach_diff(*roots)
    assert {one.family for one in diff.gains} == {"vault-oracle"}
    assert (Gain.NEW_REACH, "verb embed") in {(one.kind, one.detail) for one in diff.gains}


def test_removed_family_only_narrows(roots: tuple[Path, Path]) -> None:
    _, head = roots
    shutil.rmtree(head / "families/scrum-lead")
    diff = reach_diff(*roots)
    assert not diff.widens
    assert {one.family for one in diff.narrowings} == {"scrum-lead"}


def test_unread_head_family_widens(roots: tuple[Path, Path]) -> None:
    _, head = roots
    (head / CHAT).write_text("name = [\n", encoding="utf-8")
    diff = reach_diff(*roots)
    assert diff.unread == (CHAT,)
    assert diff.widens


def test_unread_head_server_widens(roots: tuple[Path, Path]) -> None:
    _, head = roots
    edit(head, KAGI, 'name = "kagi"', 'name = "kagi"\nunknown = 1')
    assert reach_diff(*roots).unread == (KAGI,)


def test_unread_base_family_counts_as_no_reach(roots: tuple[Path, Path]) -> None:
    base, _ = roots
    (base / CHAT).write_text("name = [\n", encoding="utf-8")
    diff = reach_diff(*roots)
    assert diff.unread == ()
    assert {one.family for one in diff.gains} == {"chat"}


def test_directory_with_no_family_file_is_not_unread(roots: tuple[Path, Path]) -> None:
    _, head = roots
    (head / "families/notes").mkdir()
    assert reach_diff(*roots) == Diff((), (), ())


# --- the report and the command -----------------------------------------------


def test_markdown_escapes_a_pipe() -> None:
    finding = Finding("chat", Gain.NEW_REACH, "repo a|b")
    report = markdown(Diff((finding,), (), ()))
    assert "| chat | `new_reach` | repo a\\|b |" in report


def test_markdown_lists_narrowings_under_their_heading() -> None:
    finding = Finding("chat", Narrowing.REACH_DROPPED, "shell")
    report = markdown(Diff((), (finding,), ()))
    assert "No family gains reach." in report
    assert "### Narrowings (1)" in report
    assert "### Gains" not in report


def test_command_exits_one_on_a_gain_and_writes_the_summary(
    roots: tuple[Path, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    base, head = roots
    edit(head, CHAT, "shell = false", "shell = true")
    summary = tmp_path / "summary.md"
    summary.write_text("earlier step\n", encoding="utf-8")
    monkeypatch.setenv(STEP_SUMMARY_ENV, str(summary))
    assert main(["reach", "--base", str(base), str(head)]) == EXIT_INVALID
    out = capsys.readouterr().out
    assert "| chat | `new_reach` | shell |" in out
    assert summary.read_text(encoding="utf-8") == "earlier step\n" + out


def test_command_exits_zero_on_a_narrowing(
    roots: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    base, head = roots
    monkeypatch.delenv(STEP_SUMMARY_ENV, raising=False)
    edit(head, CODE, "shell = true", "shell = false")
    assert main(["reach", "--base", str(base), str(head)]) == EXIT_OK


def test_command_refuses_a_missing_root(tmp_path: Path) -> None:
    assert main(["reach", "--base", str(tmp_path / "none"), str(FIXTURES)]) == EXIT_USAGE
