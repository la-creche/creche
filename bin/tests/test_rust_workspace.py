"""The Cargo workspace stays where it is and keeps its lint gate.

Three things about `rust/` could change without one red line, and each gets
a check here:

1. **A lint lifted for every crate at once.** `[workspace.lints]` in
   `rust/Cargo.toml` is one table that every crate inherits. One edit there
   allows `unwrap` in all Rust code, and every build still passes. The table
   is pinned entry for entry. An exception belongs on one item, as
   `#[expect(clippy::<lint>, reason = "...")]` (`rust/AGENTS.md`, rule 5).
2. **A Cargo file outside `rust/`.** The tag allocator gives a path under no
   component to no component, so a change under `rust/` mints no tag and
   starts no release. A crate inside a package directory would move that
   package's component with every Rust commit (`rust/AGENTS.md`, rule 11).
3. **Two toolchain versions.** `rust-toolchain.toml` names the compiler, and
   `rust-version` in `Cargo.toml` names the oldest one a crate builds with.
   They are one version here.

A push that changes only `rust/` runs no pytest suite (`bin/lib/rustrule.sh`),
so for such a change these checks run in CI.
"""

from __future__ import annotations

import subprocess
import tomllib
from pathlib import Path
from typing import Any

from handover.allocate import touched
from handover.catalog import Repo

REPO = Path(__file__).resolve().parents[2]

#: The one directory that holds every Cargo file.
RUST_DIR = "rust"

#: The compiler lints of the gate.
RUST_LINTS = {
    "unsafe_code": "forbid",
    "unused_must_use": "deny",
    "missing_debug_implementations": "warn",
}

#: Every clippy lint of the gate. Each one is `deny`.
CLIPPY_DENIED = (
    "unwrap_used",
    "expect_used",
    "panic",
    "todo",
    "unimplemented",
    "unreachable",
    "indexing_slicing",
    "string_slice",
    "as_conversions",
    "unwrap_in_result",
    "panic_in_result_fn",
    "await_holding_lock",
    "dbg_macro",
    "exit",
    "mem_forget",
    # The two that hold the exception rule itself: an `#[allow]`, or an
    # `#[expect]` with no reason, would lift any lint above for one item or
    # for a whole crate.
    "allow_attributes",
    "allow_attributes_without_reason",
)

#: The whole of `clippy.toml`: the four lints a test may break.
IN_TESTS = {
    "allow-unwrap-in-tests": True,
    "allow-expect-in-tests": True,
    "allow-panic-in-tests": True,
    "allow-indexing-slicing-in-tests": True,
}

#: A file that makes its directory a part of a Cargo build.
CARGO_FILES = ("Cargo.toml", "Cargo.lock", "rust-toolchain.toml", "rust-toolchain")

#: Paths a Rust commit changes: a manifest, a source file and a document.
RUST_PATHS = ("rust/Cargo.lock", "rust/crates/creche-contracts/src/ids.rs", "rust/AGENTS.md")


def _toml(name: str) -> dict[str, Any]:
    return tomllib.loads((REPO / RUST_DIR / name).read_text(encoding="utf-8"))


WORKSPACE = _toml("Cargo.toml")["workspace"]


def test_the_gate_forbids_unsafe_code() -> None:
    assert WORKSPACE["lints"]["rust"] == RUST_LINTS


def test_the_gate_denies_every_clippy_lint_it_names() -> None:
    assert WORKSPACE["lints"]["clippy"] == dict.fromkeys(CLIPPY_DENIED, "deny")


def test_only_a_test_can_break_a_lint_and_only_these_four() -> None:
    assert _toml("clippy.toml") == IN_TESTS


def test_the_toolchain_is_the_rust_version_of_the_workspace() -> None:
    toolchain = _toml("rust-toolchain.toml")["toolchain"]
    version = WORKSPACE["package"]["rust-version"]

    assert toolchain["channel"].rsplit(".", 1)[0] == version
    assert set(toolchain["components"]) == {"rustfmt", "clippy"}


def test_no_crate_carries_a_version_to_bump() -> None:
    """No number lives in a file (`test_gate_workflow.py` holds the Python
    half). cargo reads a crate with no version as 0.0.0."""
    assert "version" not in WORKSPACE["package"]
    for manifest in sorted((REPO / RUST_DIR / "crates").glob("*/Cargo.toml")):
        package = tomllib.loads(manifest.read_text(encoding="utf-8"))["package"]

        assert "version" not in package, f"{manifest.parent.name} carries a version"


def test_every_cargo_file_is_under_rust() -> None:
    tracked = subprocess.run(
        ["git", "ls-files"], cwd=REPO, capture_output=True, text=True, check=True
    ).stdout.splitlines()
    outside = [
        path
        for path in tracked
        if path.rsplit("/", 1)[-1] in CARGO_FILES and not path.startswith(f"{RUST_DIR}/")
    ]

    assert outside == [], f"a Cargo file outside {RUST_DIR}/: {outside}"


def test_a_change_under_rust_mints_no_tag() -> None:
    """The reason the workspace sits in a directory of its own. When a
    component is released from Rust, its catalog row changes this on
    purpose."""
    assert touched(RUST_PATHS, Repo.AGENT_CONTROL) == ()
