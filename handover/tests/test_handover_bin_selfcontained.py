"""A tree of compiled programs holds its own code (contract 06 §8.2).

A venv tree that reads its code out of the repository changes what runs
with no release, and its verify hook still passes. `selfcontained.escapes`
refuses one. A `kind: binary` tree has no `.pth` to read, and it reaches
outside itself in other ways. Each one is built here by hand and refused:

1. A program that a unit or the verify command starts is missing, is not a
   regular file or is not executable.
2. A link has an absolute target, or resolves outside the tree.
3. A file holds the path of the fetched work tree, or has a second name.
4. The staged tree itself is a link, or is not there.

The first half drives the walk. The second half drives the executor's own
`build`, which decides WHICH programs the walk must find: the verify
command's, and those of every unit that step 9 leaves in force.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from handover.catalog import Kind
from handover.errors import Refusal, RefusalCode
from handover.executor.install import MAX_UNIT_BYTES, StepFailed
from handover.executor.selfcontained import (
    ABSOLUTE_LINK,
    LINK_LOOP,
    LINK_OUTSIDE,
    MAX_REPORTED,
    NAMES_WORK_TREE,
    NOT_A_FILE,
    NOT_A_PROGRAM,
    NOT_A_TREE,
    NOT_EXECUTABLE,
    NOT_STAGED,
    PROGRAM_OUTSIDE,
    SCAN_CHUNK_BYTES,
    SHARED_FILE,
    UNLISTED_DIRECTORY,
    UNREADABLE_FILE,
    binary_escapes,
    check_binary_tree,
)
from handover.site import SITE_FILE_ENV
from handover_bin_fixtures import (
    BINARY_NAME,
    PROGRAM_BYTES,
    SIBLING_UNIT,
    UNIT,
    Staging,
    make_staging,
    staged_binary_tree,
    unit_text,
    write_program,
)

VERIFY = f"{BINARY_NAME}-verify"


def _tree(tmp_path: Path) -> tuple[Path, Path]:
    """A staged tree as `cargo install` leaves it, and a fetched work tree
    beside it that holds what a build leaves behind."""
    tree = staged_binary_tree(tmp_path / "components" / f"{BINARY_NAME}.new")
    source = tmp_path / "work" / "01K5J8M2Q7V3X9R4T6N0B8C2DE" / BINARY_NAME
    write_program(source / "rust" / "target" / "release" / BINARY_NAME)

    return tree, source


def _programs(tree: Path, *names: str) -> tuple[Path, ...]:
    return tuple(tree / "bin" / name for name in names or (BINARY_NAME, VERIFY))


def _faults(tree: Path, source: Path, *names: str) -> tuple[str, ...]:
    return binary_escapes(tree, _programs(tree, *names), source)


def _built_in_the_work_tree(tree: Path, source: Path) -> Path:
    """Move the staged tree into the fetched work tree, where a build that
    ignores the output variable leaves its programs."""
    built = source / "rust" / "target" / "install"
    tree.rename(built)

    return built


# ---- the walk: the tree itself -----------------------------------------------


def test_a_tree_that_is_a_relative_link_is_found(tmp_path: Path) -> None:
    """The build left a link at `<install.to>.new`, and it names a
    directory of the work tree. Every program resolves, and every file
    that the walk lists is a clean one. Step 9 would rename the link, and
    the live tree would be the work tree."""
    tree, source = _tree(tmp_path)
    built = _built_in_the_work_tree(tree, source)
    tree.symlink_to(os.path.relpath(built, tree.parent))

    assert _faults(tree, source) == (f"{tree.name} {NOT_A_TREE}",)


def test_a_tree_that_is_an_absolute_link_is_found(tmp_path: Path) -> None:
    tree, source = _tree(tmp_path)
    tree.symlink_to(_built_in_the_work_tree(tree, source))

    assert _faults(tree, source) == (f"{tree.name} {NOT_A_TREE}",)


def test_a_tree_that_is_a_link_to_a_clean_directory_is_found(tmp_path: Path) -> None:
    """The rule has one reading: the staged tree is a directory. Where the
    link goes does not matter, because a rename moves the link and not the
    directory behind it."""
    tree, source = _tree(tmp_path)
    beside = tree.with_name("elsewhere")
    tree.rename(beside)
    tree.symlink_to(beside.name)

    assert _faults(tree, source) == (f"{tree.name} {NOT_A_TREE}",)


def test_a_tree_that_is_not_there_is_a_fault(tmp_path: Path) -> None:
    """With no program to find and nothing to walk, an absent tree had no
    fault. Nothing can call an absent tree self-contained."""
    _, source = _tree(tmp_path)
    absent = tmp_path / "components" / "absent.new"

    assert binary_escapes(absent, (), source) == (f"absent.new {NOT_A_TREE}",)


# ---- the walk: the programs --------------------------------------------------


def test_a_tree_that_holds_its_own_programs_passes(tmp_path: Path) -> None:
    tree, source = _tree(tmp_path)

    assert _faults(tree, source) == ()


def test_a_program_the_build_did_not_make_is_found(tmp_path: Path) -> None:
    tree, source = _tree(tmp_path)

    assert _faults(tree, source, "noticeboard-door") == (f"bin/noticeboard-door {NOT_STAGED}",)


def test_a_program_that_is_a_link_is_found_even_inside_the_tree(tmp_path: Path) -> None:
    """A named program must be the tree's own bytes. A link to a second
    file of the same tree is refused as well: the rule has one reading."""
    tree, source = _tree(tmp_path)
    (tree / "bin" / "alias").symlink_to(BINARY_NAME)

    assert _faults(tree, source, "alias") == (f"bin/alias {NOT_A_PROGRAM}",)


def test_a_program_that_is_a_directory_is_found(tmp_path: Path) -> None:
    tree, source = _tree(tmp_path)
    (tree / "bin" / "folder").mkdir()

    assert _faults(tree, source, "folder") == (f"bin/folder {NOT_A_PROGRAM}",)


def test_a_program_that_cannot_run_is_found(tmp_path: Path) -> None:
    """The mode pass gives everyone read and execute only where the owner
    can execute. A program without that bit starts for nobody."""
    tree, source = _tree(tmp_path)
    (tree / "bin" / BINARY_NAME).chmod(0o644)

    assert _faults(tree, source) == (f"bin/{BINARY_NAME} {NOT_EXECUTABLE}",)


def test_a_program_under_a_linked_directory_is_found(tmp_path: Path) -> None:
    """`bin` itself is a link to the build's output directory. The program
    is a regular file there, and it is not the tree's."""
    tree, source = _tree(tmp_path)
    target = source / "rust" / "target" / "release"
    write_program(target / VERIFY)
    for path in sorted((tree / "bin").iterdir()):
        path.unlink()

    (tree / "bin").rmdir()
    (tree / "bin").symlink_to(os.path.relpath(target, tree))

    found = _faults(tree, source)

    assert f"bin/{BINARY_NAME} {PROGRAM_OUTSIDE}" in found
    assert f"bin {LINK_OUTSIDE}" in found


def test_a_program_that_is_not_under_the_tree_is_found(tmp_path: Path) -> None:
    """The executor hands in paths under the staged tree only. A path
    that is not under it is never the tree's own program."""
    tree, source = _tree(tmp_path)
    elsewhere = write_program(tmp_path / "elsewhere" / "tool")

    assert binary_escapes(tree, (elsewhere,), source) == (f"tool {PROGRAM_OUTSIDE}",)


# ---- the walk: links ---------------------------------------------------------


def test_a_link_into_the_work_tree_is_found(tmp_path: Path) -> None:
    """The fault an editable venv has, as a binary tree has it: the tree
    holds a name, and the bytes are in the directory the build ran in."""
    tree, source = _tree(tmp_path)
    built = source / "rust" / "target" / "release" / BINARY_NAME
    (tree / "bin" / "extra").symlink_to(os.path.relpath(built, tree / "bin"))

    assert _faults(tree, source) == (f"bin/extra {LINK_OUTSIDE}",)


def test_a_link_with_an_absolute_target_is_found(tmp_path: Path) -> None:
    tree, source = _tree(tmp_path)
    (tree / "bin" / "extra").symlink_to("/usr/bin/env")

    assert _faults(tree, source) == (f"bin/extra {ABSOLUTE_LINK}",)


def test_an_absolute_link_into_the_staged_tree_is_found(tmp_path: Path) -> None:
    """It resolves inside the tree now. Step 9 renames the tree, and the
    link then names `<install.to>.new`, which the next release stages."""
    tree, source = _tree(tmp_path)
    (tree / "bin" / "extra").symlink_to(tree / "bin" / BINARY_NAME)

    assert _faults(tree, source) == (f"bin/extra {ABSOLUTE_LINK}",)


def test_a_relative_link_inside_the_tree_is_allowed(tmp_path: Path) -> None:
    """A link that no unit starts, and that stays inside, survives the
    rename and reads nothing from outside."""
    tree, source = _tree(tmp_path)
    (tree / "bin" / "alias").symlink_to(BINARY_NAME)
    (tree / "share").mkdir()
    (tree / "share" / "programs").symlink_to("../bin")

    assert _faults(tree, source) == ()


def test_a_linked_directory_is_not_walked(tmp_path: Path) -> None:
    """A link to a directory outside is one fault. What that directory
    holds is not the tree's, and the walk reads none of it."""
    tree, source = _tree(tmp_path)
    (tree / "target").symlink_to(os.path.relpath(source / "rust" / "target", tree))

    assert _faults(tree, source) == (f"target {LINK_OUTSIDE}",)


def test_a_link_loop_is_a_fault_and_never_an_error(tmp_path: Path) -> None:
    """Two links name each other. Python 3.12 raises `RuntimeError` when
    it resolves one, and that error would end the run with no ledger
    entry. Python 3.13 raises nothing and gives back the link at which it
    stopped. Each version reports the same fault."""
    tree, source = _tree(tmp_path)
    (tree / "bin" / "this").symlink_to("that")
    (tree / "bin" / "that").symlink_to("this")

    assert _faults(tree, source) == (f"bin/that {LINK_LOOP}", f"bin/this {LINK_LOOP}")


def test_a_link_to_itself_is_a_fault(tmp_path: Path) -> None:
    tree, source = _tree(tmp_path)
    (tree / "bin" / "self").symlink_to("self")

    assert _faults(tree, source) == (f"bin/self {LINK_LOOP}",)


def test_a_link_to_a_name_that_is_not_there_stays_allowed(tmp_path: Path) -> None:
    """The loop test must not catch a link whose target is absent. Such a
    link stays inside the tree and reads nothing."""
    tree, source = _tree(tmp_path)
    (tree / "bin" / "later").symlink_to("not-built")

    assert _faults(tree, source) == ()


@pytest.mark.skipif(os.geteuid() == 0, reason="root lists every directory")
def test_a_directory_that_cannot_be_listed_is_a_fault(tmp_path: Path) -> None:
    """The walk skipped a directory that it could not list, and reported
    nothing. What the walk cannot list, it cannot call clean."""
    tree, source = _tree(tmp_path)
    sealed = tree / "sealed"
    sealed.mkdir()
    (sealed / "extra").symlink_to("/usr/bin/env")
    sealed.chmod(0o000)
    try:
        found = _faults(tree, source)
    finally:
        sealed.chmod(0o755)

    assert found == (f"sealed {UNLISTED_DIRECTORY}",)


# ---- the walk: what a file holds ---------------------------------------------


def test_cargos_own_record_of_the_work_tree_is_found(tmp_path: Path) -> None:
    """`cargo install` without `--no-track` writes `.crates.toml`, and it
    names the directory the build ran in."""
    tree, source = _tree(tmp_path)
    record = f'[v1]\n"{BINARY_NAME} 0.1.0 (path+file://{source}/rust/crates/x)" = ["x"]\n'
    (tree / ".crates.toml").write_text(record, encoding="utf-8")

    assert _faults(tree, source) == (f".crates.toml {NAMES_WORK_TREE}",)


def test_a_wrapper_script_that_starts_the_work_tree_is_found(tmp_path: Path) -> None:
    """The program is a regular file, it is executable, and it is two
    lines that start the build's own output."""
    tree, source = _tree(tmp_path)
    script = f"#!/bin/sh\nexec {source}/rust/target/release/{BINARY_NAME}\n"
    write_program(tree / "bin" / BINARY_NAME, body=script.encode())

    assert _faults(tree, source) == (f"bin/{BINARY_NAME} {NAMES_WORK_TREE}",)


def test_a_program_that_holds_the_work_tree_path_is_found(tmp_path: Path) -> None:
    """A library search path, or the directory of a template that the
    program opens at run time. The verify hook cannot see either: it runs
    while the work tree is still on disk."""
    tree, source = _tree(tmp_path)
    body = PROGRAM_BYTES + b"\x00" * 64 + os.fsencode(str(source)) + b"/rust/templates\x00"
    write_program(tree / "bin" / BINARY_NAME, body=body)

    assert _faults(tree, source) == (f"bin/{BINARY_NAME} {NAMES_WORK_TREE}",)


def test_a_path_that_lies_across_two_chunks_is_found(tmp_path: Path) -> None:
    """The file is read a chunk at a time. The path starts in one chunk
    and ends in the next."""
    tree, source = _tree(tmp_path)
    needle = os.fsencode(str(source))
    before = b"\x00" * (SCAN_CHUNK_BYTES - len(needle) // 2)
    write_program(tree / "bin" / BINARY_NAME, body=before + needle + b"\x00" * 16)

    assert _faults(tree, source) == (f"bin/{BINARY_NAME} {NAMES_WORK_TREE}",)


def test_a_large_program_that_holds_no_such_path_passes(tmp_path: Path) -> None:
    tree, source = _tree(tmp_path)
    write_program(tree / "bin" / BINARY_NAME, body=b"\x7fELF" + b"\x00" * (3 * SCAN_CHUNK_BYTES))

    assert _faults(tree, source) == ()


def test_the_resolved_path_of_the_work_tree_is_found_too(tmp_path: Path) -> None:
    """The executor names the work tree through one path, and the kernel
    may resolve it to a second. A build can write down either."""
    tree, real = _tree(tmp_path)
    (tmp_path / "linked").symlink_to(tmp_path / "work")
    source = tmp_path / "linked" / real.relative_to(tmp_path / "work")
    (tree / "as-named").write_bytes(os.fsencode(str(source)))
    (tree / "as-resolved").write_bytes(os.fsencode(str(real)))

    assert _faults(tree, source) == (
        f"as-named {NAMES_WORK_TREE}",
        f"as-resolved {NAMES_WORK_TREE}",
    )


def test_a_file_with_a_second_name_is_found(tmp_path: Path) -> None:
    """A hard link shares its bytes with the build's own output. A write
    through that name changes the installed program with no release."""
    tree, source = _tree(tmp_path)
    (tree / "bin" / BINARY_NAME).unlink()
    os.link(source / "rust" / "target" / "release" / BINARY_NAME, tree / "bin" / BINARY_NAME)

    assert _faults(tree, source) == (f"bin/{BINARY_NAME} {SHARED_FILE}",)


def test_a_fifo_is_found_and_never_opened(tmp_path: Path) -> None:
    """An entry that is no file, no directory and no link cannot be read,
    so it cannot be called clean. Opening one would block the release."""
    tree, source = _tree(tmp_path)
    os.mkfifo(tree / "bin" / "pipe")

    assert _faults(tree, source) == (f"bin/pipe {NOT_A_FILE}",)


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads every file")
def test_a_file_that_cannot_be_read_is_a_fault(tmp_path: Path) -> None:
    tree, source = _tree(tmp_path)
    (tree / "bin" / "sealed").write_bytes(b"x")
    (tree / "bin" / "sealed").chmod(0o000)

    assert _faults(tree, source) == (f"bin/sealed {UNREADABLE_FILE}",)


def test_faults_come_back_in_name_order(tmp_path: Path) -> None:
    """Programs first, in the order they were named, then the walk."""
    tree, source = _tree(tmp_path)
    (tree / "zz").symlink_to("/usr")
    (tree / "aa").symlink_to("/usr")

    assert _faults(tree, source, "missing") == (
        f"bin/missing {NOT_STAGED}",
        f"aa {ABSOLUTE_LINK}",
        f"zz {ABSOLUTE_LINK}",
    )


# ---- the refusal -------------------------------------------------------------


def test_the_check_refuses_with_the_self_contained_code(tmp_path: Path) -> None:
    tree, source = _tree(tmp_path)
    (tree / "bin" / "extra").symlink_to("/usr/bin/env")

    with pytest.raises(Refusal) as caught:
        check_binary_tree(BINARY_NAME, tree, _programs(tree), source)

    assert caught.value.code is RefusalCode.EDITABLE
    assert caught.value.subject == BINARY_NAME
    assert "not self-contained (1 fault(s))" in caught.value.detail
    assert f"bin/extra {ABSOLUTE_LINK}" in caught.value.detail


def test_the_check_passes_a_clean_tree(tmp_path: Path) -> None:
    tree, source = _tree(tmp_path)

    check_binary_tree(BINARY_NAME, tree, _programs(tree), source)


def test_the_refusal_counts_every_fault_and_shows_a_few(tmp_path: Path) -> None:
    tree, source = _tree(tmp_path)
    total = MAX_REPORTED + 3
    for index in range(total):
        (tree / f"link-{index}").symlink_to("/usr")

    with pytest.raises(Refusal) as caught:
        check_binary_tree(BINARY_NAME, tree, _programs(tree), source)

    assert f"({total} fault(s))" in caught.value.detail
    assert caught.value.detail.count(ABSOLUTE_LINK) == MAX_REPORTED


def test_a_path_the_ledger_cannot_print_is_hidden(tmp_path: Path) -> None:
    """A file name is the build's own choice. One that is not a plain word
    never reaches the ledger."""
    tree, source = _tree(tmp_path)
    (tree / "a name with spaces").symlink_to("/usr")

    (fault,) = _faults(tree, source)

    assert fault == f"<unprintable> {ABSOLUTE_LINK}"


# ---- the executor: which programs the walk must find --------------------------


def _refusal(staging: Staging) -> Refusal:
    with pytest.raises(Refusal) as caught:
        staging.build()

    return caught.value


def test_the_executor_refuses_a_binary_tree_that_is_not_self_contained(tmp_path: Path) -> None:
    """After the build and before the swap. A `Refusal`, so the ledger
    carries the code and the live tree stays in service."""

    def made(new: Path) -> None:
        staged_binary_tree(new)
        (new / "bin" / "extra").symlink_to("/usr/bin/env")

    staging = make_staging(tmp_path, made)
    refusal = _refusal(staging)

    assert refusal.code is RefusalCode.EDITABLE
    assert f"bin/extra {ABSOLUTE_LINK}" in refusal.detail


def test_the_executor_names_the_work_tree_it_fetched(tmp_path: Path) -> None:
    """The path that the walk looks for is the directory the build ran in."""

    def made(new: Path) -> None:
        staged_binary_tree(new)
        (new / ".crates.toml").write_text(f"{staging.source}/rust\n", encoding="utf-8")

    staging = make_staging(tmp_path, made)

    assert f".crates.toml {NAMES_WORK_TREE}" in _refusal(staging).detail


def test_the_executor_refuses_a_staged_tree_that_is_a_link(tmp_path: Path) -> None:
    """The build argv wrote its programs into the work tree and left a
    link at `<install.to>.new`. The hook check passes, because the hook
    resolves inside the link's own target. The swap would rename the link
    into place."""

    def made(new: Path) -> None:
        new.symlink_to(staged_binary_tree(staging.source / "rust" / "target" / "install"))

    staging = make_staging(tmp_path, made)
    refusal = _refusal(staging)

    assert refusal.code is RefusalCode.EDITABLE
    assert f"{staging.paths.new.name} {NOT_A_TREE}" in refusal.detail
    assert not staging.paths.to.exists()


def test_the_executor_refuses_a_staged_tree_that_is_a_relative_link(tmp_path: Path) -> None:
    def made(new: Path) -> None:
        built = staged_binary_tree(staging.source / "rust" / "target" / "install")
        new.symlink_to(os.path.relpath(built, new.parent))

    staging = make_staging(tmp_path, made)
    refusal = _refusal(staging)

    assert refusal.code is RefusalCode.EDITABLE
    assert f"{staging.paths.new.name} {NOT_A_TREE}" in refusal.detail


def test_a_verify_hook_that_is_a_link_inside_the_tree_is_refused(tmp_path: Path) -> None:
    """The check every kind gets takes a hook that resolves to a file
    inside the tree. A binary tree's hook must be a regular file itself."""

    def made(new: Path) -> None:
        staged_binary_tree(new)
        (new / "bin" / VERIFY).unlink()
        (new / "bin" / VERIFY).symlink_to(BINARY_NAME)

    refusal = _refusal(make_staging(tmp_path, made))

    assert refusal.code is RefusalCode.EDITABLE
    assert f"bin/{VERIFY} {NOT_A_PROGRAM}" in refusal.detail


def test_a_verify_hook_that_cannot_run_is_refused(tmp_path: Path) -> None:
    def made(new: Path) -> None:
        staged_binary_tree(new)
        (new / "bin" / VERIFY).chmod(0o644)

    assert f"bin/{VERIFY} {NOT_EXECUTABLE}" in _refusal(make_staging(tmp_path, made)).detail


def test_a_missing_verify_hook_still_stops_the_stage(tmp_path: Path) -> None:
    """The check every kind gets runs first, and its answer is unchanged."""

    def made(new: Path) -> None:
        write_program(new / "bin" / BINARY_NAME)

    with pytest.raises(StepFailed, match="verify hook is missing"):
        make_staging(tmp_path, made).build()


def test_a_program_the_installed_unit_starts_must_be_staged(tmp_path: Path) -> None:
    """The unit starts `noticeboard-server`, and the build made no such
    program. The swap would restart the unit into a file that is not there."""
    staging = make_staging(tmp_path, unit=UNIT)
    staging.install_unit(unit_text(staging.program("noticeboard-server")))

    refusal = _refusal(staging)

    assert refusal.code is RefusalCode.EDITABLE
    assert f"bin/noticeboard-server {NOT_STAGED}" in refusal.detail


def test_every_program_of_the_unit_is_checked(tmp_path: Path) -> None:
    staging = make_staging(tmp_path, unit=UNIT)
    staging.install_unit(unit_text(staging.program(), staging.program("second")))

    assert f"bin/second {NOT_STAGED}" in _refusal(staging).detail


def test_a_program_that_is_named_twice_is_checked_once(tmp_path: Path) -> None:
    """Two `ExecStart=` lines name one program. The ledger says so once."""
    staging = make_staging(tmp_path, unit=UNIT)
    missing = staging.program("noticeboard-server")
    staging.install_unit(unit_text(missing, missing))

    refusal = _refusal(staging)

    assert "(1 fault(s))" in refusal.detail
    assert refusal.detail.count(NOT_STAGED) == 1


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads every file")
def test_a_carried_unit_that_cannot_be_read_stops_the_stage(tmp_path: Path) -> None:
    """Its programs are unknown, so nothing can say the tree holds them."""
    staging = make_staging(tmp_path, unit=UNIT)
    staging.carry_unit(unit_text(staging.program())).chmod(0o000)

    with pytest.raises(StepFailed, match="cannot read"):
        staging.build()


def _over_the_cap(staging: Staging) -> str:
    """A unit file longer than the cap. Its first program is staged. Its
    last `ExecStart=` line is after the cap and starts one that is not."""
    filler = "# filler\n" * (MAX_UNIT_BYTES // len("# filler\n") + 1)

    return unit_text(staging.program()) + filler + f"ExecStart={staging.program('unseen')}\n"


def test_a_carried_unit_over_the_cap_stops_the_stage(tmp_path: Path) -> None:
    """The reader took the first 64 KiB of the file and parsed that. A
    line after the cap started a program that no check saw."""
    staging = make_staging(tmp_path, unit=UNIT)
    staging.carry_unit(_over_the_cap(staging))

    with pytest.raises(StepFailed, match="is longer than"):
        staging.build()


def test_a_unit_in_force_over_the_cap_stops_the_stage(tmp_path: Path) -> None:
    """The same file, where a unit is installed and step 9 refreshes it
    from the release's own copy."""
    staging = make_staging(tmp_path, unit=UNIT)
    staging.install_unit(unit_text(staging.program()))
    staging.carry_unit(_over_the_cap(staging))

    with pytest.raises(StepFailed, match="is longer than"):
        staging.build()


def test_a_unit_at_the_cap_is_read_whole(tmp_path: Path) -> None:
    """The cap refuses a file that is longer, and no file that fits."""
    staging = make_staging(tmp_path, unit=UNIT)
    text = unit_text(staging.program())
    filler = "#" * (MAX_UNIT_BYTES - len(text) - 1) + "\n"
    staging.carry_unit(text + filler)

    staging.build()


def test_a_unit_whose_programs_are_staged_builds(tmp_path: Path) -> None:
    staging = make_staging(tmp_path, lambda new: staged_binary_tree(new, "second"), unit=UNIT)
    staging.install_unit(unit_text(staging.program(), staging.program("second")))

    staging.build()

    assert (staging.paths.new / "bin" / "second").is_file()


def test_the_unit_the_release_carries_is_the_one_that_is_read(tmp_path: Path) -> None:
    """Step 9 refreshes the installed unit from the release's own file.
    That file starts `noticeboard-next`, so that is the program to find."""
    staging = make_staging(tmp_path, unit=UNIT)
    staging.install_unit(unit_text(staging.program()))
    staging.carry_unit(unit_text(staging.program("noticeboard-next")))

    assert f"bin/noticeboard-next {NOT_STAGED}" in _refusal(staging).detail


def test_a_carried_unit_is_read_before_it_is_ever_installed(tmp_path: Path) -> None:
    """A first release: no unit is installed, so rule 8 has nothing to
    read. The unit file travels in the artifact, and the operator installs
    it from there. Its program must be in the tree it travels in."""
    staging = make_staging(tmp_path, unit=UNIT)
    staging.carry_unit(unit_text(staging.program("noticeboard-next")))

    assert f"bin/noticeboard-next {NOT_STAGED}" in _refusal(staging).detail


def test_a_carried_unit_that_starts_a_program_outside_the_tree_is_refused(
    tmp_path: Path,
) -> None:
    """Contract 06 §1 rule 8, for a unit no host has installed yet. A
    program outside `install.to` is in no staged tree."""
    staging = make_staging(tmp_path, unit=UNIT)
    staging.carry_unit(unit_text("/usr/local/bin/elsewhere"))

    refusal = _refusal(staging)

    assert refusal.code is RefusalCode.UNIT
    assert "is not inside" in refusal.detail


def test_a_component_with_no_unit_file_anywhere_checks_its_hook_alone(tmp_path: Path) -> None:
    """The manifest names a unit, no host has it and the release carries
    none. Step 9 lists it under `manual`, and nothing names a program."""
    staging = make_staging(tmp_path, unit=UNIT)

    staging.build()


def test_a_sibling_units_program_must_be_staged(tmp_path: Path) -> None:
    """A sibling unit restarts with the component's own unit, out of the
    same tree. Its program is one the walk must find."""
    staging = make_staging(tmp_path, unit=UNIT)
    own = unit_text(staging.program())
    door = unit_text(staging.program("noticeboard-door"))
    staging.install_unit(own)
    staging.install_unit(door, SIBLING_UNIT)
    staging.carry_unit(door, SIBLING_UNIT)

    assert f"bin/noticeboard-door {NOT_STAGED}" in _refusal(staging).detail


def test_a_user_unit_names_its_program_through_the_home_specifier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A user unit reaches its tree through `%h`. The program it names is
    read under the operator's home, which is where the tree is."""
    site = tmp_path / "site.env"
    site.write_text(
        f"AGENT_GITHUB_OWNER=example-owner\nAGENT_OPERATOR_USER=operator\n"
        f"AGENT_OPERATOR_HOME={tmp_path}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv(SITE_FILE_ENV, str(site))
    staging = make_staging(tmp_path, unit=UNIT)
    text = unit_text(f"%h/components/{BINARY_NAME}/bin/noticeboard-door")
    (tmp_path / "user-units" / UNIT).write_text(text, encoding="utf-8")

    assert f"bin/noticeboard-door {NOT_STAGED}" in _refusal(staging).detail


@pytest.mark.parametrize("kind", [Kind.COMPOSE, Kind.OCI_IMAGE])
def test_a_kind_with_no_program_tree_is_not_walked(tmp_path: Path, kind: Kind) -> None:
    """The walk is the binary kind's. A compose project and an image tree
    keep the checks they had."""

    def made(new: Path) -> None:
        staged_binary_tree(new)
        (new / "bin" / "extra").symlink_to("/usr/bin/env")

    make_staging(tmp_path, made, kind=kind).build()
