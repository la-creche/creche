"""The Cargo workspace stays where it is and keeps its lint gate.

Eight things about `rust/` could change without one red line, and each gets
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
4. **A panic that stops the process, or an overflow that does not.** The two
   `[profile]` tables keep `panic = "unwind"`, and the release one keeps
   `overflow-checks = true`. With `abort`, one panic in one request stops a
   service, and its edge layer cannot answer. Without the checks, a release
   build wraps a number and continues with a wrong value
   (`rust/AGENTS.md`, "The panic rule").
5. **A license or a source that no person decided.** `rust/deny.toml` is the
   policy that `cargo deny` checks the locked crates against. One edit there
   allows each license or a crate from a git repository, and the check still
   passes. Each table of the file is pinned entry for entry
   (`rust/AGENTS.md`, "Dependencies"). The checks here read the file and
   need no `cargo`.
6. **A table of differences in a test.** A differential test passes only
   when the Rust result equals the Python result for each vector. A table
   of the inputs on which the two differ is an exception that no type
   holds (`rust/AGENTS.md`, "The differential test"). No Rust source file
   holds such a table, in each crate that the list here does not name.
7. **A second reader of the vector files.** The crate `creche-vectors` is
   the one reader of `vectors/data`. A reader in another crate holds none
   of its checks, and two readers of one file drift (`rust/AGENTS.md`, "The
   differential test"). No Rust source file of another crate holds the name
   of that directory.
8. **A second reader of a TOML text.** The module `tomlfile` of
   `creche-contracts` is the one TOML reader of the workspace. It holds the
   size limits, the TOML 1.0 check and the level limit. Code in another file
   that calls the crate `toml` holds none of them (`rust/AGENTS.md`,
   "TOML"). Only the two files of that module name the crate `toml` or the
   crate `toml_parser`.

A push that changes only `rust/` runs no pytest suite (`bin/lib/rustrule.sh`),
so for such a change these checks run in CI.

Two more checks hold one crate each to a rule of its own:

- The crate file of `creche-util` names no dependency, because each other
  crate can depend on that crate (`rust/crates/creche-util/AGENTS.md`,
  rule 4).
- The crate file of `creche-vectors` names no crate outside a closed list,
  because each other crate can take that crate for its tests
  (`rust/crates/creche-vectors/AGENTS.md`, rule 1).
"""

from __future__ import annotations

import re
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

#: The two profile tables of the workspace, entry for entry.
PROFILES = {
    "release": {"panic": "unwind", "overflow-checks": True},
    "dev": {"panic": "unwind"},
}

#: The file that `cargo deny` reads, and each table that it can hold.
DENY_FILE = "deny.toml"
DENY_TABLES = {"graph", "advisories", "licenses", "bans", "sources"}

#: Each license that a locked crate can need. No other one is permitted.
LICENSES = ["MIT", "Apache-2.0", "BSD-3-Clause", "Unicode-3.0"]

#: The one source of a locked crate: the index of crates.io. No git source.
SOURCES = {
    "unknown-registry": "deny",
    "unknown-git": "deny",
    "allow-registry": ["https://github.com/rust-lang/crates.io-index"],
    "allow-git": [],
}

#: One version of each crate, and no `*` as a version. The second entry puts
#: a crate that only a test uses in the count of versions. The last entry
#: permits the path of one workspace crate in another, which has no version.
BANS = {
    "multiple-versions": "deny",
    "multiple-versions-include-dev": True,
    "wildcards": "deny",
    "allow-wildcard-paths": True,
}

#: A yanked version fails the check. The table ignores no advisory.
ADVISORIES = {"yanked": "deny"}

#: The crates that the checks read: what a build for a host or for a
#: development machine can use, with each feature on.
GRAPH = {
    "targets": [
        "x86_64-unknown-linux-gnu",
        "aarch64-unknown-linux-gnu",
        "aarch64-apple-darwin",
        "x86_64-apple-darwin",
    ],
    "all-features": True,
}

#: A file that makes its directory a part of a Cargo build.
CARGO_FILES = ("Cargo.toml", "Cargo.lock", "rust-toolchain.toml", "rust-toolchain")

#: The crate of the shared helpers. Each other crate can depend on it.
HELPER_CRATE = "creche-util"

#: Each table of a crate file that can name a dependency. `target` holds the
#: dependencies of one platform.
DEPENDENCY_TABLES = ("dependencies", "dev-dependencies", "build-dependencies", "target")

#: The crate of the vector reader. Each other crate can take it for its tests.
READER_CRATE = "creche-vectors"

#: Each crate that the vector reader can name. `creche-util` has no
#: dependency, so it is below each crate of the workspace.
READER_DEPENDENCIES = {"serde", "serde_json", "creche-util"}

#: The two tables in which the vector reader can name a crate.
READER_TABLES = ("dependencies", "dev-dependencies")

#: The directory of the vector files, as a Rust source file names it. Only
#: the source files of `READER_CRATE` hold this text.
DATA_DIR_TEXT = "vectors/data"

#: The two files of the one TOML reader, as paths below `CRATES_DIR`. Only
#: these two files name a TOML crate.
TOML_FILES = {
    "creche-contracts/src/tomlfile.rs",
    "creche-contracts/src/tomlfile/toml10.rs",
}

#: The start of the path of an item of a TOML crate, as a Rust source file
#: names it.
TOML_PATHS = ("toml::", "toml_parser::")

#: Paths a Rust commit changes: a manifest, a source file and a document.
RUST_PATHS = ("rust/Cargo.lock", "rust/crates/creche-contracts/src/ids.rs", "rust/AGENTS.md")

#: The directory of the crates, below `RUST_DIR`.
CRATES_DIR = "crates"

#: The crates whose tests still hold a table of differences. The test of
#: check 6 reads each other crate. The change that removes the last table of
#: a crate deletes its name here.
TABLE_CRATES = ("agent-family", "creche-contracts", "creche-runtime", "creche-testkit")

#: The name of a table of differences, and the start of the struct of one
#: row. A longer name that holds one of the two counts too.
TABLE_NAME = "DEVIATIONS"
TABLE_ROW = re.compile(r"\bstruct\s+Deviation")


def _toml(name: str) -> dict[str, Any]:
    return tomllib.loads((REPO / RUST_DIR / name).read_text(encoding="utf-8"))


WORKSPACE = _toml("Cargo.toml")["workspace"]

#: The `[profile]` tables. They are beside `[workspace]`, not inside it.
PROFILE = _toml("Cargo.toml").get("profile", {})


def test_the_gate_forbids_unsafe_code() -> None:
    assert WORKSPACE["lints"]["rust"] == RUST_LINTS


def test_the_gate_denies_every_clippy_lint_it_names() -> None:
    assert WORKSPACE["lints"]["clippy"] == dict.fromkeys(CLIPPY_DENIED, "deny")


def test_only_a_test_can_break_a_lint_and_only_these_four() -> None:
    assert _toml("clippy.toml") == IN_TESTS


def test_a_panic_unwinds_and_a_release_build_checks_each_overflow() -> None:
    assert PROFILE == PROFILES


def test_the_deny_file_holds_these_tables_and_no_other() -> None:
    """A table that leaves the file takes the defaults of cargo-deny, and
    most of those only warn. Each test below pins one table as a whole. A
    key or a table inside one then cannot arrive without a red line, for
    example `[licenses.private]` or `[sources.allow-org]`."""
    assert set(_toml(DENY_FILE)) == DENY_TABLES


def test_only_these_licenses_are_permitted() -> None:
    """No exception for one crate and no private registry: the table is the
    list, entry for entry. `include-dev` puts a crate that only a test uses
    in the check. Without it, cargo-deny skips the license of such a crate."""
    assert _toml(DENY_FILE)["licenses"] == {"allow": LICENSES, "include-dev": True}


def test_crates_io_is_the_one_source_of_a_crate() -> None:
    assert _toml(DENY_FILE)["sources"] == SOURCES


def test_no_crate_has_two_versions_or_a_wildcard_version() -> None:
    """No `skip` and no `allow`: an entry there is an exception for one
    crate."""
    assert _toml(DENY_FILE)["bans"] == BANS


def test_a_yanked_crate_fails_and_no_advisory_is_ignored() -> None:
    assert _toml(DENY_FILE)["advisories"] == ADVISORIES


def test_the_checks_read_each_crate_of_these_four_targets() -> None:
    """A target that leaves the list takes its crates out of each check."""
    assert _toml(DENY_FILE)["graph"] == GRAPH


def test_the_toolchain_is_the_rust_version_of_the_workspace() -> None:
    toolchain = _toml("rust-toolchain.toml")["toolchain"]
    version = WORKSPACE["package"]["rust-version"]

    assert toolchain["channel"].rsplit(".", 1)[0] == version
    assert set(toolchain["components"]) == {"rustfmt", "clippy"}


def test_every_crate_takes_the_lint_gate() -> None:
    """`bin/rust-gate.sh` reads the text of each crate file. This reads the
    TOML, so a spelling that the text check misreads still fails here."""
    manifests = sorted((REPO / RUST_DIR / "crates").glob("*/Cargo.toml"))

    assert manifests, f"no crate under {RUST_DIR}/crates"
    for manifest in manifests:
        lints = tomllib.loads(manifest.read_text(encoding="utf-8")).get("lints")

        assert lints == {"workspace": True}, f"{manifest.parent.name} is outside the lint gate"


def test_no_crate_carries_a_version_to_bump() -> None:
    """No number lives in a file (`test_gate_workflow.py` holds the Python
    half). cargo reads a crate with no version as 0.0.0."""
    assert "version" not in WORKSPACE["package"]
    for manifest in sorted((REPO / RUST_DIR / "crates").glob("*/Cargo.toml")):
        package = tomllib.loads(manifest.read_text(encoding="utf-8"))["package"]

        assert "version" not in package, f"{manifest.parent.name} carries a version"


def test_the_helper_crate_has_no_dependency() -> None:
    """A dependency of `creche-util` becomes a dependency of each crate that
    uses a shared helper. The crate needs `std` only
    (`rust/crates/creche-util/AGENTS.md`, rule 4)."""
    manifest = tomllib.loads(
        (REPO / RUST_DIR / "crates" / HELPER_CRATE / "Cargo.toml").read_text(encoding="utf-8")
    )
    named = [table for table in DEPENDENCY_TABLES if table in manifest]

    assert named == [], f"{HELPER_CRATE} names a dependency: {named}"


def test_the_vector_reader_names_no_other_crate() -> None:
    """`creche-contracts` can take `creche-vectors` for its tests, and so can
    each crate above it. A dependency of the reader on one of them makes a
    circle (`rust/crates/creche-vectors/AGENTS.md`, rule 1).

    An entry can give its crate another name with `package`. The test reads
    the name of the crate."""
    manifest = tomllib.loads(
        (REPO / RUST_DIR / "crates" / READER_CRATE / "Cargo.toml").read_text(encoding="utf-8")
    )
    elsewhere = [
        table for table in DEPENDENCY_TABLES if table in manifest and table not in READER_TABLES
    ]
    named = {
        entry.get("package", name) if isinstance(entry, dict) else name
        for table in READER_TABLES
        for name, entry in manifest.get(table, {}).items()
    }

    assert elsewhere == [], f"{READER_CRATE} names a dependency in {elsewhere}"
    assert named <= READER_DEPENDENCIES, (
        f"{READER_CRATE} names {sorted(named - READER_DEPENDENCIES)}"
    )


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


def test_no_test_holds_a_table_of_differences() -> None:
    """The test reads the text of each file, test code too: a table of
    differences lives in a test. A comment that holds the name counts.

    A wrong path of the crates reads no file, and a name of the list with
    no crate skips nothing. The test fails for both."""
    crates = REPO / RUST_DIR / CRATES_DIR
    absent = [name for name in TABLE_CRATES if not (crates / name).is_dir()]

    assert crates.is_dir(), f"no directory {RUST_DIR}/{CRATES_DIR}"
    assert absent == [], f"the list names a crate that is not there: {absent}"

    tables: list[str] = []
    for source in sorted(crates.rglob("*.rs")):
        if source.relative_to(crates).parts[0] in TABLE_CRATES:
            continue

        text = source.read_text(encoding="utf-8")
        if TABLE_NAME in text or TABLE_ROW.search(text):
            tables.append(str(source.relative_to(REPO)))

    assert tables == [], f"a table of differences: {tables}"


def test_only_the_vector_reader_names_the_vector_directory() -> None:
    """The test reads the text of each file, test code too: a reader of the
    vectors lives in a test. A comment that holds the text counts. In a
    comment of another crate, name the surfaces and not the directory.

    A reader crate that does not hold the text shows a wrong text or a wrong
    path here, and the search then proves nothing. The test fails for it."""
    crates = REPO / RUST_DIR / CRATES_DIR
    named = {
        source.relative_to(crates)
        for source in crates.rglob("*.rs")
        if DATA_DIR_TEXT in source.read_text(encoding="utf-8")
    }
    elsewhere = sorted(str(path) for path in named if path.parts[0] != READER_CRATE)

    assert len(named) > len(elsewhere), f"{READER_CRATE} does not name {DATA_DIR_TEXT}"
    assert elsewhere == [], f"a file outside {READER_CRATE} names {DATA_DIR_TEXT}: {elsewhere}"


def test_only_the_toml_reader_names_a_toml_crate() -> None:
    """The test reads the text of each file, test code too. A comment that
    holds such a path counts.

    A reader file that is absent, or that names no TOML crate, shows a wrong
    path or a wrong text here, and the search then proves nothing. The test
    fails for it."""
    crates = REPO / RUST_DIR / CRATES_DIR
    named: set[str] = set()
    for source in crates.rglob("*.rs"):
        text = source.read_text(encoding="utf-8")
        if any(path in text for path in TOML_PATHS):
            named.add(source.relative_to(crates).as_posix())

    assert named >= TOML_FILES, f"a file of the reader names no TOML crate: {TOML_FILES - named}"
    assert named == TOML_FILES, f"another file names a TOML crate: {named - TOML_FILES}"


def test_a_change_under_rust_mints_no_tag() -> None:
    """The reason the workspace sits in a directory of its own. When a
    component is released from Rust, its catalog row changes this on
    purpose."""
    assert touched(RUST_PATHS, Repo.AGENT_CONTROL) == ()
