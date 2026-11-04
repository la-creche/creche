"""What `bin/lib/artifactrows.sh` reads, and each text that it refuses.

The library is the one reader of the line form of `handover artifacts`. The
tests source it in bash and call `read_rows` on a file. The rows are the
real line form of `handover.artifacts.ArtifactRow.lines`, so a change of
that form fails here.

Cases:

1. A real row, a row with two commands, two rows, no row, and a last row
   with no line feed.
2. Each text that is not in the line form, with the reason of its refusal.
3. A file that the reader cannot read.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Final

import pytest
from handover.artifacts import ArtifactRow, crate_row
from handover.catalog import (
    ARCHIVE_MAX_BYTES,
    ARCHIVE_MAX_MEMBERS,
    ARCHIVE_MAX_UNPACKED_BYTES,
    ARCHIVE_SUFFIX,
    BINARY_INSTALL_ENV,
    BINARY_TARGET,
    BUNDLE_SUFFIX,
)

REPO: Final = Path(__file__).resolve().parents[2]
READER: Final = REPO / "bin" / "lib" / "artifactrows.sh"

#: The crate of the row of each case.
COMPONENT: Final = "agent-family"
CRATE_DIR: Final = "rust/crates/agent-family"
ARCHIVE: Final = f"{COMPONENT}-{BINARY_TARGET}{ARCHIVE_SUFFIX}"

#: The vmm build of decision CH35 rule 3: one command of eleven words.
VMM_WORDS: Final = (
    *("cargo", "install", "--locked", "--no-track"),
    *("--git", "https://github.com/cloud-hypervisor/cloud-hypervisor"),
    *("--rev", "038ce5a72d761380a02f714a4eb4ca63e0ea073d", "cloud-hypervisor"),
    *("--bin", "cloud-hypervisor", "--bin", "ch-remote"),
)

#: The driver: it sources the library, reads the file and prints each row,
#: or the reason of the refusal. `$1` is a `set` line, so a case runs with
#: `set -e` and without it.
DRIVER: Final = """\
$1
. "$2"
if ! read_rows "$3"; then
  printf 'fault %s\\n' "$ROWS_FAULT"
  exit 1
fi
for ((row = 0; row < ${#ROW_COMPONENT[@]}; row++)); do
  printf 'row %s %s %s %s %s %s %s %s\\n' "${ROW_COMPONENT[$row]}" "${ROW_TARGET[$row]}" \\
    "${ROW_ARCHIVE[$row]}" "${ROW_BUNDLE[$row]}" "${ROW_ENV[$row]}" \\
    "${ROW_MAX_ARCHIVE[$row]}" "${ROW_MAX_MEMBERS[$row]}" "${ROW_MAX_UNPACKED[$row]}"
  first="${ROW_FIRST[$row]}"
  for ((command = first; command < first + ROW_COMMANDS[row]; command++)); do
    printf 'argv'
    printf ' [%s]' "${COMMAND_WORD[@]:${COMMAND_FIRST[$command]}:${COMMAND_COUNT[$command]}}"
    printf '\\n'
  done
done
"""

SET_LINES: Final = ["set -euo pipefail", "set -u"]


def _row(**changed: str) -> str:
    """The real row of the crate, with each key of `changed` given another
    value. A key is the key of the line form with `_` for `-`."""
    lines = list(crate_row(COMPONENT, CRATE_DIR).lines())
    for key, value in changed.items():
        name = key.replace("_", "-")
        (index,) = [at for at, line in enumerate(lines) if line.startswith(f"{name} ")]
        lines[index] = f"{name} {value}"

    return "".join(f"{line}\n" for line in lines)


def _read(tmp_path: Path, text: str, set_line: str = SET_LINES[0]) -> tuple[int, str]:
    rows = tmp_path / "rows"
    rows.write_text(text, encoding="utf-8")
    done = subprocess.run(
        ["bash", "-c", DRIVER, "driver", set_line, str(READER), str(rows)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert done.stderr == ""

    return done.returncode, done.stdout


def _head(component: str, archive: str, bundle: str) -> str:
    limits = f"{ARCHIVE_MAX_BYTES} {ARCHIVE_MAX_MEMBERS} {ARCHIVE_MAX_UNPACKED_BYTES}"

    return f"row {component} {BINARY_TARGET} {archive} {bundle} {BINARY_INSTALL_ENV} {limits}\n"


ROW_OUT: Final = (
    _head(COMPONENT, ARCHIVE, f"{ARCHIVE}{BUNDLE_SUFFIX}")
    + f"argv [cargo] [install] [--locked] [--no-track] [--path] [{CRATE_DIR}]\n"
)


@pytest.mark.parametrize("set_line", SET_LINES)
def test_the_reader_reads_the_row_that_handover_prints(tmp_path: Path, set_line: str) -> None:
    assert _read(tmp_path, _row(), set_line) == (0, ROW_OUT)


def test_a_row_with_two_commands_keeps_each_word_in_its_command(tmp_path: Path) -> None:
    """The vmm row of decision CH35 rule 3, then a second command with a
    space, a quote and a `$(...)` in its words. No shell reads a word."""
    other = ("cargo", "one two", "'q'", "$(touch made)")
    row = ArtifactRow("vmm", (VMM_WORDS, other))
    archive = f"vmm-{BINARY_TARGET}{ARCHIVE_SUFFIX}"
    text = "".join(f"{line}\n" for line in row.lines())

    assert _read(tmp_path, text) == (
        0,
        _head("vmm", archive, f"{archive}{BUNDLE_SUFFIX}")
        + "argv "
        + " ".join(f"[{word}]" for word in VMM_WORDS)
        + "\n"
        + "argv [cargo] [one two] ['q'] [$(touch made)]\n",
    )
    assert not (tmp_path / "made").exists()


def test_two_rows_and_no_row(tmp_path: Path) -> None:
    other = f"other-{BINARY_TARGET}{ARCHIVE_SUFFIX}"
    second = _row(component="other", archive=other, bundle=f"{other}{BUNDLE_SUFFIX}")
    status, out = _read(tmp_path, _row() + second)

    assert status == 0
    assert out.startswith(ROW_OUT)
    assert out.count("\nrow other ") == 1
    assert _read(tmp_path, "") == (0, "")


def test_a_last_row_with_no_line_feed_is_a_whole_row(tmp_path: Path) -> None:
    assert _read(tmp_path, _row().removesuffix("\n")) == (0, ROW_OUT)


NO_KEY_COMPONENT: Final = "a row has no key component where the line form puts it"
NO_WORD_COUNT: Final = "a command of a row has no count of words"
ROWS_END: Final = "the rows end inside a row"
NOT_PLAIN: Final = "the {} of a row is not a plain name"
NO_COUNT: Final = "the {} of a row is not a count above 0"
NO_VARIABLE: Final = "the install-env of a row is not a variable name"

#: Texts that are not in the line form, each with the reason of its
#: refusal. A case names one check of the reader.
BROKEN_ROWS: Final = {
    "a-key-out-of-order": (
        _row().replace(f"component {COMPONENT}\ntarget", "target"),
        NO_KEY_COMPONENT,
    ),
    "a-line-with-no-key": (
        _row().replace(f"component {COMPONENT}\n", f"{COMPONENT}\n"),
        NO_KEY_COMPONENT,
    ),
    "a-path-as-a-name": (_row(archive=f"../{ARCHIVE}"), NOT_PLAIN.format("archive")),
    "a-slash-in-a-name": (_row(archive="a/b.tar.gz"), NOT_PLAIN.format("archive")),
    "an-option-as-a-name": (_row(component="-rf"), NOT_PLAIN.format("component")),
    "a-limit-of-zero": (_row(max_members="0"), NO_COUNT.format("max-members")),
    "a-limit-of-nineteen-digits": (_row(max_members="9" * 19), NO_COUNT.format("max-members")),
    "a-limit-that-is-no-count": (
        _row(max_archive_bytes="12k"),
        NO_COUNT.format("max-archive-bytes"),
    ),
    "no-command": (
        re.sub(r"argv 6\n(.*\n){6}", "", _row()),
        "a row has no command where the line form puts one",
    ),
    "no-end": (_row().removesuffix("end\n"), ROWS_END),
    "a-short-command": (_row().replace("argv 6\n", "argv 9\n"), ROWS_END),
    "a-command-of-no-words": (_row().replace("argv 6\n", "argv 0\n"), NO_WORD_COUNT),
    "a-count-that-is-no-number": (_row().replace("argv 6\n", "argv x\n"), NO_WORD_COUNT),
    "a-count-of-five-digits": (_row().replace("argv 6\n", "argv 10000\n"), NO_WORD_COUNT),
    "an-empty-first-word": (
        _row().replace("argv 6\ncargo\n", "argv 6\n\n"),
        "a command of a row has an empty first word",
    ),
    "a-variable-that-is-no-name": (_row(install_env="CARGO INSTALL"), NO_VARIABLE),
    "a-variable-that-starts-with-a-digit": (_row(install_env="1VAR"), NO_VARIABLE),
    "two-rows-with-one-component": (
        _row() + _row(archive="other.tar.gz", bundle="other.json"),
        f"two rows name the component {COMPONENT}",
    ),
    "two-rows-with-one-archive": (
        _row() + _row(component="other", bundle="other.json"),
        f"two rows name the archive {ARCHIVE}",
    ),
    "two-rows-with-one-bundle": (
        _row() + _row(component="other", archive="other.tar.gz"),
        f"two rows name the bundle {ARCHIVE}{BUNDLE_SUFFIX}",
    ),
    "a-junk-line-first": (f"junk line\n{_row()}", NO_KEY_COMPONENT),
    "a-line-after-the-end-with-no-line-feed": (f"{_row()}component other", ROWS_END),
    "a-blank-line": (f"{_row()}\n", NO_KEY_COMPONENT),
}


@pytest.mark.parametrize("set_line", SET_LINES)
@pytest.mark.parametrize(("text", "fault"), BROKEN_ROWS.values(), ids=BROKEN_ROWS)
def test_a_text_that_is_not_in_the_line_form_is_refused_with_its_reason(
    tmp_path: Path, text: str, fault: str, set_line: str
) -> None:
    """The reader returns 1 and exits no shell, also under `set -e`."""
    assert _read(tmp_path, text, set_line) == (1, f"fault {fault}\n")


def test_a_file_that_the_reader_cannot_read_is_refused(tmp_path: Path) -> None:
    done = subprocess.run(
        ["bash", "-c", DRIVER, "driver", SET_LINES[0], str(READER), str(tmp_path / "absent")],
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert (done.returncode, done.stdout) == (1, "fault cannot read the file of the rows\n")
