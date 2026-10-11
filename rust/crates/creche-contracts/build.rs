//! The build script of `creche-contracts`.
//!
//! The manifest of the release tool states the version of the
//! `component-manifest` contract, in one row of its `provides` block. That
//! row is the one source of the version (rust/AGENTS.md, rule 13). The script
//! reads the row and writes the two numbers and their text as Rust
//! constants, in a file of `OUT_DIR`. The `manifest` module includes that
//! file.
//!
//! The script holds no YAML reader. It reads one form of the block: the key
//! `provides:` at the start of a line, and one row on each line below it.
//!
//! ```text
//! provides:
//!   - { contract: component-manifest, major: 0, minor: 18 }
//! ```
//!
//! The three keys of a row can stand in each order. Each count of spaces can
//! stand between two words of a row. A comment and an empty line can stand
//! between two rows. A block in another form fails the build. A manifest
//! with no row for the contract fails the build, and so does a manifest with
//! two such rows.
//!
//! The test build of the crate reads this file a second time, as a module.
//! The tests at the end of the file run there. That build holds no program
//! part.

use std::fmt;

/// The contract whose version the script reads.
const CONTRACT: &str = "component-manifest";

/// The key of the block, at the start of its line.
const BLOCK_KEY: &str = "provides:";

/// The key of a row that holds the name of a contract.
const NAME_KEY: &str = "contract";

/// The key of a row that holds the first number of a version.
const MAJOR_KEY: &str = "major";

/// The key of a row that holds the second number of a version.
const MINOR_KEY: &str = "minor";

/// The two numbers of a contract version.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
struct Version {
    major: u16,
    minor: u16,
}

/// One row of the block: a contract and the version that the row gives it.
#[derive(Debug)]
struct Row<'a> {
    contract: &'a str,
    version: Version,
}

/// Why a manifest gives no version. Each fault fails the build. A line
/// number counts from 1.
#[derive(Debug, PartialEq, Eq)]
enum Fault {
    /// No line starts with the key of the block.
    NoBlock,
    /// A second line starts with the key of the block.
    SecondBlock { line: usize },
    /// A line of the block is not one row in the form that the script reads.
    NotARow { line: usize },
    /// No row of the block names the contract.
    NoRow,
    /// A second row of the block names the contract.
    SecondRow { line: usize },
}

impl fmt::Display for Fault {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::NoBlock => write!(f, "no line starts with `{BLOCK_KEY}`"),
            Self::SecondBlock { line } => {
                write!(f, "line {line} starts a second `{BLOCK_KEY}` block")
            }
            Self::NotARow { line } => write!(
                f,
                "line {line} is not a row of the form `- {{ {NAME_KEY}: <name>, {MAJOR_KEY}: \
                 <number>, {MINOR_KEY}: <number> }}`"
            ),
            Self::NoRow => write!(f, "no row of `{BLOCK_KEY}` names `{CONTRACT}`"),
            Self::SecondRow { line } => {
                write!(f, "line {line} is a second row for `{CONTRACT}`")
            }
        }
    }
}

/// Where the reader is in the manifest.
#[derive(Clone, Copy, PartialEq, Eq)]
enum Place {
    /// Above the block.
    Above,
    /// In the block.
    Inside,
    /// Below the block.
    Below,
}

/// The version that the text of a manifest gives the contract.
fn version_in(manifest: &str) -> Result<Version, Fault> {
    let mut place = Place::Above;
    let mut found = None;
    for (index, text) in manifest.lines().enumerate() {
        let line = index.saturating_add(1);
        if let Some(rest) = text.strip_prefix(BLOCK_KEY) {
            if place != Place::Above {
                return Err(Fault::SecondBlock { line });
            }

            if !ends_line(rest) {
                return Err(Fault::NotARow { line });
            }

            place = Place::Inside;
            continue;
        }

        let indented = text.trim_start_matches(' ');
        if place != Place::Inside || indented.is_empty() || indented.starts_with('#') {
            continue;
        }

        let Some(entry) = indented.strip_prefix('-') else {
            // The next key of the manifest ends the block. Each key of a
            // manifest starts with a lower-case letter, in the first column.
            if text.starts_with(|first: char| first.is_ascii_lowercase()) {
                place = Place::Below;
                continue;
            }

            return Err(Fault::NotARow { line });
        };
        let row = row_of(entry).ok_or(Fault::NotARow { line })?;
        if row.contract != CONTRACT {
            continue;
        }

        if found.is_some() {
            return Err(Fault::SecondRow { line });
        }

        found = Some(row.version);
    }

    match (found, place) {
        (Some(version), _) => Ok(version),
        (None, Place::Above) => Err(Fault::NoBlock),
        (None, Place::Inside | Place::Below) => Err(Fault::NoRow),
    }
}

/// The row of one line, from the text after the `-` of that line. `None` for
/// a text in another form.
fn row_of(entry: &str) -> Option<Row<'_>> {
    // One space or more stands between the `-` and the `{`.
    let open = entry.strip_prefix(' ')?.trim_start_matches(' ');
    let (pairs, rest) = open.strip_prefix('{')?.split_once('}')?;
    if !ends_line(rest) {
        return None;
    }

    let mut contract = None;
    let mut major = None;
    let mut minor = None;
    for pair in pairs.split(',') {
        let (key, value) = pair.split_once(':')?;
        // One space or more stands between the `:` and the value.
        let value = value.strip_prefix(' ')?.trim_matches(' ');
        let earlier = match key.trim_matches(' ') {
            NAME_KEY => contract.replace(name_of(value)?).is_some(),
            MAJOR_KEY => major.replace(number_of(value)?).is_some(),
            MINOR_KEY => minor.replace(number_of(value)?).is_some(),
            _ => return None,
        };
        // A row holds each key one time.
        if earlier {
            return None;
        }
    }

    Some(Row {
        contract: contract?,
        version: Version {
            major: major?,
            minor: minor?,
        },
    })
}

/// The name of a contract: a lower-case ASCII letter, then lower-case ASCII
/// letters, ASCII digits and hyphens.
fn name_of(text: &str) -> Option<&str> {
    let letter_first = text.starts_with(|first: char| first.is_ascii_lowercase());
    let known_bytes = text
        .bytes()
        .all(|byte| byte.is_ascii_lowercase() || byte.is_ascii_digit() || byte == b'-');

    (letter_first && known_bytes).then_some(text)
}

/// One number of a version: ASCII digits, with no zero before another digit.
fn number_of(text: &str) -> Option<u16> {
    let digits_only = text.bytes().all(|byte| byte.is_ascii_digit());
    let zero_first = text.len() > 1 && text.starts_with('0');
    if !digits_only || zero_first {
        return None;
    }

    text.parse().ok()
}

/// Whether a line ends after one of its words: `rest` is spaces only, or
/// spaces and then a comment. A comment needs a space before its `#`.
fn ends_line(rest: &str) -> bool {
    let after = rest.trim_start_matches(' ');

    after.is_empty() || (after.starts_with('#') && after.len() < rest.len())
}

/// The file of constants for one version. The `manifest` module includes it
/// in a module of its own.
fn constants_of(version: Version) -> String {
    let Version { major, minor } = version;

    format!(
        "// The build script of this crate wrote this file. Its source is the row\n\
         // for `{CONTRACT}` in the manifest of the release tool.\n\
         pub(super) const MAJOR: u16 = {major};\n\
         pub(super) const MINOR: u16 = {minor};\n\
         pub(super) const TEXT: &str = \"{major}.{minor}\";\n"
    )
}

/// The program part: it reads the manifest and writes the file of constants.
#[cfg(not(test))]
mod program {
    use std::io::{self, Write as _};
    use std::path::Path;
    use std::process::ExitCode;
    use std::{env, fmt, fs};

    use super::{Fault, constants_of, version_in};

    /// The manifest of the release tool, from the directory of this crate.
    /// cargo starts a build script in that directory.
    const MANIFEST: &str = "../../../handover/component.yaml";

    /// The name of the file of constants in `OUT_DIR`.
    const CONSTANTS: &str = "manifest_contract.rs";

    /// Why the script writes no file of constants.
    enum Failure {
        /// The system gives no text of the manifest.
        NotRead(io::ErrorKind),
        /// The manifest gives no version.
        NoVersion(Fault),
        /// cargo gave no `OUT_DIR`.
        NoOutDir,
        /// The system does not write the file.
        NotWritten(io::ErrorKind),
    }

    impl fmt::Display for Failure {
        fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
            match self {
                Self::NotRead(kind) => write!(f, "cannot read {MANIFEST}: {kind}"),
                Self::NoVersion(fault) => write!(f, "{MANIFEST}: {fault}"),
                Self::NoOutDir => write!(f, "cargo gave the build script no OUT_DIR"),
                Self::NotWritten(kind) => write!(f, "cannot write {CONSTANTS}: {kind}"),
            }
        }
    }

    fn write_constants() -> Result<(), Failure> {
        let manifest =
            fs::read_to_string(MANIFEST).map_err(|error| Failure::NotRead(error.kind()))?;
        let version = version_in(&manifest).map_err(Failure::NoVersion)?;
        let out_dir = env::var_os("OUT_DIR").ok_or(Failure::NoOutDir)?;

        fs::write(Path::new(&out_dir).join(CONSTANTS), constants_of(version))
            .map_err(|error| Failure::NotWritten(error.kind()))
    }

    /// Runs the script. cargo reads each line that the script writes to its
    /// standard output. The first line makes cargo run the script again
    /// after a change of the manifest. A failure is one `cargo::error` line,
    /// and cargo then fails the build with that line.
    pub(super) fn run() -> ExitCode {
        let mut lines = format!("cargo::rerun-if-changed={MANIFEST}\n");
        if let Err(failure) = write_constants() {
            lines.push_str(&format!("cargo::error={failure}\n"));
        }

        match io::stdout().write_all(lines.as_bytes()) {
            Ok(()) => ExitCode::SUCCESS,
            Err(_) => ExitCode::FAILURE,
        }
    }
}

#[cfg(not(test))]
fn main() -> std::process::ExitCode {
    program::run()
}

#[cfg(test)]
mod tests {
    use super::*;

    /// One row for the contract, as a line of a block.
    const ROW: &str = "  - { contract: component-manifest, major: 0, minor: 18 }\n";

    /// A manifest with the lines of one `provides` block between two other
    /// keys.
    fn manifest_of(block: &str) -> String {
        format!("name: handover\n{block}requires: []\n")
    }

    fn version(major: u16, minor: u16) -> Result<Version, Fault> {
        Ok(Version { major, minor })
    }

    #[test]
    fn the_row_gives_its_two_numbers() {
        let manifest = manifest_of(&format!("provides:\n{ROW}"));

        assert_eq!(version_in(&manifest), version(0, 18));
    }

    #[test]
    fn the_keys_of_a_row_can_stand_in_each_order() {
        let pairs = ["contract: component-manifest", "major: 7", "minor: 21"];
        let orders = [
            [0, 1, 2],
            [0, 2, 1],
            [1, 0, 2],
            [1, 2, 0],
            [2, 0, 1],
            [2, 1, 0],
        ];
        for order in orders {
            let row = order.map(|at| pairs[at]).join(", ");
            let manifest = manifest_of(&format!("provides:\n  - {{ {row} }}\n"));

            assert_eq!(version_in(&manifest), version(7, 21), "{row}");
        }
    }

    #[test]
    fn each_count_of_spaces_can_stand_between_two_words() {
        let rows = [
            "- {contract: component-manifest,major: 0,minor: 18}",
            "-    {   contract   :   component-manifest   ,   major   :   0   ,   minor   :   18   }",
            "  - { contract: component-manifest, major: 0, minor: 18 }   ",
            "        - { contract: component-manifest, major: 0, minor: 18 } # the newest one",
            "- { minor:  18,contract:   component-manifest ,major: 0}",
        ];
        for row in rows {
            let manifest = manifest_of(&format!("provides:\n{row}\n"));

            assert_eq!(version_in(&manifest), version(0, 18), "{row}");
        }
    }

    #[test]
    fn a_comment_and_an_empty_line_can_stand_in_the_block() {
        let block = format!(
            "provides:   # what this component gives\n\n  # the manifest contract\n   \n{ROW}  \
             - {{ contract: pep-grant, major: 2, minor: 11 }}\n#\n"
        );

        assert_eq!(version_in(&manifest_of(&block)), version(0, 18));
    }

    #[test]
    fn a_line_end_with_a_carriage_return_reads_as_a_line_end() {
        let manifest = manifest_of(&format!("provides:\n{ROW}")).replace('\n', "\r\n");

        assert_eq!(version_in(&manifest), version(0, 18));
    }

    #[test]
    fn only_a_row_of_the_block_counts() {
        // The first row is below another key, and the last row is below the
        // key that ends the block.
        let manifest = format!("requires:\n{ROW}provides:\n{ROW}depends_on:\n{ROW}");

        assert_eq!(version_in(&manifest), version(0, 18));
    }

    #[test]
    fn a_manifest_with_no_block_gives_no_version() {
        let absent = [
            String::new(),
            String::from("name: handover\n"),
            // The key is not at the start of its line.
            format!(" provides:\n{ROW}"),
            // The key is in quotes, or a space is before its `:`.
            format!("\"provides\":\n{ROW}"),
            format!("provides :\n{ROW}"),
        ];
        for manifest in absent {
            assert_eq!(version_in(&manifest), Err(Fault::NoBlock), "{manifest}");
        }
    }

    #[test]
    fn a_block_with_no_row_for_the_contract_gives_no_version() {
        let absent = [
            String::from("provides:\n"),
            manifest_of("provides:\n"),
            manifest_of("provides:\n  - { contract: pep-grant, major: 2, minor: 11 }\n"),
            manifest_of("provides:\n  - { contract: component-manifests, major: 0, minor: 18 }\n"),
        ];
        for manifest in absent {
            assert_eq!(version_in(&manifest), Err(Fault::NoRow), "{manifest}");
        }
    }

    #[test]
    fn a_second_row_for_the_contract_gives_no_version() {
        let manifest = manifest_of(&format!(
            "provides:\n{ROW}  - {{ contract: component-manifest, major: 0, minor: 19 }}\n"
        ));

        assert_eq!(version_in(&manifest), Err(Fault::SecondRow { line: 4 }));
        assert_eq!(
            version_in(&manifest_of(&format!("provides:\n{ROW}{ROW}"))),
            Err(Fault::SecondRow { line: 4 })
        );
    }

    #[test]
    fn a_second_block_gives_no_version() {
        let manifest = manifest_of(&format!("provides:\n{ROW}")) + "provides:\n";

        assert_eq!(version_in(&manifest), Err(Fault::SecondBlock { line: 5 }));
        assert_eq!(
            version_in(&format!("provides:\nprovides:\n{ROW}")),
            Err(Fault::SecondBlock { line: 2 })
        );
    }

    #[test]
    fn a_line_of_the_block_in_another_form_gives_no_version() {
        let lines = [
            // A row with its keys on more than one line.
            "  - contract: component-manifest",
            "    major: 0",
            // A row with a key two times, with a key too few and with one more key.
            "  - { contract: component-manifest, major: 0, major: 0, minor: 18 }",
            "  - { contract: component-manifest, major: 0 }",
            "  - { contract: component-manifest, major: 0, minor: 18, min_minor: 18 }",
            "  - { contract: component-manifest, major: 0, minor: 18, }",
            "  - { contract: component-manifest, major: 0, min_minor: 18 }",
            // A value in another form.
            "  - { contract: \"component-manifest\", major: 0, minor: 18 }",
            "  - { contract: Component-manifest, major: 0, minor: 18 }",
            "  - { contract: -manifest, major: 0, minor: 18 }",
            "  - { contract: , major: 0, minor: 18 }",
            "  - { contract: component-manifest, major: 0, minor: 018 }",
            "  - { contract: component-manifest, major: 0, minor: +18 }",
            "  - { contract: component-manifest, major: 0, minor: 0x12 }",
            "  - { contract: component-manifest, major: 0, minor: 1_8 }",
            "  - { contract: component-manifest, major: 0, minor: 18.0 }",
            "  - { contract: component-manifest, major: 0, minor: \u{0661}\u{0668} }",
            "  - { contract: component-manifest, major: 0, minor: 65536 }",
            "  - { contract: component-manifest, major: 0, minor: }",
            "  - { contract: component-manifest, major: 0, minor: 1 8 }",
            // A row with no space where the form needs one.
            "  -{ contract: component-manifest, major: 0, minor: 18 }",
            "  - { contract:component-manifest, major: 0, minor: 18 }",
            "  - { contract: component-manifest, major: 0, minor: 18 }# newest",
            // Other text.
            "  - { contract: component-manifest, major: 0, minor: 18 } and more",
            "  - { contract: component-manifest, major: 0, minor: 18",
            "  - [component-manifest, 0, 18]",
            "  -",
            "---",
            "  contract: component-manifest",
            "\t- { contract: component-manifest, major: 0, minor: 18 }",
            "  -\t{ contract: component-manifest, major: 0, minor: 18 }",
            "  - { contract: component-manifest,\tmajor: 0, minor: 18 }",
            "Provides: []",
            "[]",
        ];
        for line in lines {
            let manifest = manifest_of(&format!("provides:\n{ROW}{line}\n"));

            assert_eq!(
                version_in(&manifest),
                Err(Fault::NotARow { line: 4 }),
                "{line}"
            );
        }
    }

    #[test]
    fn a_value_on_the_line_of_the_key_gives_no_version() {
        let keys = [
            "provides: []",
            "provides: [{ contract: component-manifest, major: 0, minor: 18 }]",
            "provides:# no space",
            "provides:x",
        ];
        for key in keys {
            let manifest = manifest_of(&format!("{key}\n{ROW}"));

            assert_eq!(
                version_in(&manifest),
                Err(Fault::NotARow { line: 2 }),
                "{key}"
            );
        }
    }

    #[test]
    fn a_number_is_ascii_digits_with_no_zero_before_another_digit() {
        assert_eq!(number_of("0"), Some(0));
        assert_eq!(number_of("18"), Some(18));
        assert_eq!(number_of("65535"), Some(65535));
        for refused in [
            "", "00", "018", "65536", "-1", "+1", "1\n", " 1", "\u{0661}",
        ] {
            assert_eq!(number_of(refused), None, "{refused:?}");
        }
    }

    #[test]
    fn each_fault_is_one_line_with_its_place() {
        let faults = [
            Fault::NoBlock,
            Fault::SecondBlock { line: 7 },
            Fault::NotARow { line: 7 },
            Fault::NoRow,
            Fault::SecondRow { line: 7 },
        ];
        for fault in faults {
            let text = fault.to_string();

            assert!(!text.contains('\n'), "{text}");
            assert_eq!(
                text.contains("line 7"),
                !matches!(fault, Fault::NoBlock | Fault::NoRow),
                "{text}"
            );
        }
    }

    #[test]
    fn the_file_of_constants_holds_the_two_numbers_and_their_text() {
        let text = constants_of(Version {
            major: 0,
            minor: 18,
        });
        let code: Vec<&str> = text
            .lines()
            .filter(|line| !line.starts_with("//"))
            .collect();

        assert_eq!(
            code,
            [
                "pub(super) const MAJOR: u16 = 0;",
                "pub(super) const MINOR: u16 = 18;",
                "pub(super) const TEXT: &str = \"0.18\";",
            ]
        );
    }
}
