"""The `files:` declaration in a SKILL.md frontmatter.

The parser reads only the `files:` key, never raises, and drops what it
cannot read into faults that the validator can report."""

from __future__ import annotations

from agent_family.skillmd import parse_declared_files


def test_no_frontmatter_declares_nothing() -> None:
    assert parse_declared_files("# A skill with plain prose.\n") == ((), ())


def test_frontmatter_without_the_key_declares_nothing() -> None:
    text = "---\nname: tdd\nlicense: MIT\n---\n# TDD\n"
    assert parse_declared_files(text) == ((), ())


def test_block_list_reads_each_item() -> None:
    text = "---\nfiles:\n  - mocking.md\n  - tests.md\n---\n# TDD\n"
    assert parse_declared_files(text) == (("mocking.md", "tests.md"), ())


def test_block_list_stops_at_the_next_top_level_key() -> None:
    text = "---\nfiles:\n  - mocking.md\nname: tdd\n  - orphan.md\n---\n# TDD\n"
    declared, faults = parse_declared_files(text)
    assert declared == ("mocking.md",)
    assert faults == ()


def test_block_list_reads_nested_paths() -> None:
    text = "---\nfiles:\n  - scripts/loop.sh\n---\n# Wizard\n"
    assert parse_declared_files(text) == (("scripts/loop.sh",), ())


def test_empty_block_list_declares_nothing() -> None:
    text = "---\nfiles:\n---\n# TDD\n"
    assert parse_declared_files(text) == ((), ())


def test_inline_list_reads_each_item() -> None:
    text = '---\nfiles: ["mocking.md", \'tests.md\']\n---\n# TDD\n'
    assert parse_declared_files(text) == (("mocking.md", "tests.md"), ())


def test_empty_inline_list_declares_nothing() -> None:
    text = "---\nfiles: []\n---\n# TDD\n"
    assert parse_declared_files(text) == ((), ())


def test_unknown_frontmatter_keys_pass_through_unseen() -> None:
    text = "---\nname: tdd\ndisable-model-invocation: true\nfiles:\n  - mocking.md\n---\n"
    assert parse_declared_files(text) == (("mocking.md",), ())


def test_unterminated_frontmatter_is_a_fault_and_still_reads() -> None:
    text = "---\nfiles:\n  - mocking.md\n# TDD\n"
    declared, faults = parse_declared_files(text)
    assert declared == ("mocking.md",)
    assert faults == ("has a frontmatter that never closes",)


def test_a_non_list_value_is_a_fault() -> None:
    declared, faults = parse_declared_files("---\nfiles: mocking.md\n---\n")
    assert declared == ()
    assert len(faults) == 1
    assert "'files'" in faults[0]


def test_a_line_that_is_no_item_ends_the_block_with_a_fault() -> None:
    text = "---\nfiles:\n  - mocking.md\n  mocking.md again\n---\n"
    declared, faults = parse_declared_files(text)
    assert declared == ("mocking.md",)
    assert faults == ("has a 'files' list with a line that is no item: 'mocking.md again'",)


def test_the_skill_itself_is_not_a_helper() -> None:
    declared, faults = parse_declared_files("---\nfiles:\n  - SKILL.md\n---\n")
    assert declared == ()
    assert faults == ("declares 'SKILL.md', which is not a helper path (is the SKILL.md itself)",)


def test_a_path_that_leaves_the_directory_is_dropped_and_named() -> None:
    for path in ("/etc/passwd", "../escape.md", "scripts/../escape.md", "a\\b.md", "./here.md", ""):
        text = f'---\nfiles:\n  - "{path}"\n---\n'
        declared, faults = parse_declared_files(text)
        assert declared == ()
        assert len(faults) == 1, path


def test_each_unreadable_path_is_named_separately() -> None:
    text = "---\nfiles:\n  - ../escape.md\n  - mocking.md\n  - /absolute.md\n---\n"
    declared, faults = parse_declared_files(text)
    assert declared == ("mocking.md",)
    assert len(faults) == 2


def test_a_dotted_name_is_a_plain_relative_path() -> None:
    assert parse_declared_files("---\nfiles:\n  - .hidden.md\n---\n") == ((".hidden.md",), ())


def test_a_path_named_twice_is_a_fault() -> None:
    text = "---\nfiles:\n  - mocking.md\n  - mocking.md\n---\n"
    declared, faults = parse_declared_files(text)
    assert declared == ("mocking.md",)
    assert faults == ("declares 'mocking.md' twice",)
