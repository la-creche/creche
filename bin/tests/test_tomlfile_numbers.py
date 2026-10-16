"""The Python TOML reader holds the numbers and the texts of the Rust reader.

`rust/crates/creche-contracts/src/tomlfile.rs` is the one source of the four
byte limits, of the level limit and of the first four texts of a refusal
(`rust/AGENTS.md`, "TOML"). `agent_family.tomlfile` copies them. This test
reads the Rust file as text and holds each copy equal to its source.

`handover.tomlfile` is a second copy of the whole Python module, because
`handover` imports no module of `agent_family`. The last test holds the two
files equal, byte for byte."""

from __future__ import annotations

import re
from pathlib import Path

from agent_family import tomlfile
from agent_family.tomlfile import DEPTH, FileKind

REPO = Path(__file__).resolve().parents[2]
RUST_FILE = REPO / "rust" / "crates" / "creche-contracts" / "src" / "tomlfile.rs"

#: The two copies of the Python module: the reader of `agent_family` and the
#: reader of the release tool.
PYTHON_FILES = (
    REPO / "family" / "src" / "agent_family" / "tomlfile.py",
    REPO / "handover" / "src" / "handover" / "tomlfile.py",
)

#: `const FAMILY_BYTES: usize = 65536;`: the byte limit of one file kind.
BYTE_LIMIT = re.compile(r"^const ([A-Z]+)_BYTES: usize = ([0-9_]+);$", re.MULTILINE)

#: `const LEVEL_MAX: u8 = 8;`: the level limit.
LEVEL_MAX = re.compile(r"^const LEVEL_MAX: u8 = ([0-9_]+);$", re.MULTILINE)

#: The arm of one fault in the `Display` of `TomlFault`, with its text.
FAULT_ARM = re.compile(
    r"Self::(\w+)(?:\s*\{[^}]*\})?\s*=>\s*(?:write!\(\s*f,|f\.write_str\()\s*\"([^\"]*)\""
)

#: The Rust names of the four faults that a Python reader gives.
FOUR_FAULTS = ("TooLarge", "NotUtf8", "ByteOrderMark", "NotToml")


def _rust_text() -> str:
    return RUST_FILE.read_text(encoding="utf-8")


def _number(text: str) -> int:
    return int(text.replace("_", ""))


def _rust_limits() -> dict[str, int]:
    return {name: _number(value) for name, value in BYTE_LIMIT.findall(_rust_text())}


def _rust_level_max() -> int:
    found = LEVEL_MAX.findall(_rust_text())

    assert len(found) == 1, f"{RUST_FILE} has no single LEVEL_MAX"
    return _number(found[0])


def _rust_fault_texts() -> dict[str, str]:
    """The text of each fault, as the `Display` of `TomlFault` writes it."""
    text = _rust_text()
    start = text.index("impl fmt::Display for TomlFault")
    end = text.index("impl Error for TomlFault")

    return dict(FAULT_ARM.findall(text[start:end]))


def test_each_byte_limit_equals_the_rust_limit_of_the_same_kind() -> None:
    limits = _rust_limits()

    assert sorted(limits) == sorted(kind.name for kind in FileKind)
    for kind in FileKind:
        assert kind.byte_limit == limits[kind.name], kind.name


def test_the_level_limit_equals_the_rust_limit() -> None:
    assert _rust_level_max() == DEPTH


def test_the_rust_display_names_the_four_faults() -> None:
    assert set(FOUR_FAULTS) <= set(_rust_fault_texts())


def test_the_text_of_a_file_that_is_too_large_equals_the_rust_text() -> None:
    rust = _rust_fault_texts()["TooLarge"]

    assert rust == tomlfile.TOO_LARGE
    for kind in FileKind:
        assert tomlfile.TOO_LARGE.format(limit=kind.byte_limit) == rust.replace(
            "{limit}", str(_rust_limits()[kind.name])
        )


def test_the_text_of_a_file_that_is_no_utf8_equals_the_rust_text() -> None:
    assert _rust_fault_texts()["NotUtf8"] == tomlfile.NOT_UTF8


def test_the_text_of_a_byte_order_mark_equals_the_rust_text() -> None:
    assert _rust_fault_texts()["ByteOrderMark"] == tomlfile.BYTE_ORDER_MARK


def test_the_text_of_a_text_that_is_no_toml_equals_the_rust_text() -> None:
    rust = _rust_fault_texts()["NotToml"]

    assert rust.replace("{LEVEL_MAX}", str(_rust_level_max())) == tomlfile.NOT_TOML


def test_the_two_python_readers_hold_equal_bytes() -> None:
    family, handover = (path.read_bytes() for path in PYTHON_FILES)

    assert family == handover
