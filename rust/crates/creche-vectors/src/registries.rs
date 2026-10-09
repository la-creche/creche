//! The reader of `vectors/data/family_file.registries.json`.
//!
//! A vector of `family_file`, of `server_file` and of `family_file.cli` names
//! a registry of this repository. A Rust test reads no file outside `rust/`
//! and `vectors/data`, so this file holds each file of each such registry.
//! `vectors/README.md`, "The registry file", holds the format.
//!
//! The reader refuses the file in each of these cases:
//!
//! 1. The format is not 1, or the kind is not `registries`.
//! 2. The file or a row holds a key that the format does not name, or lacks
//!    a required one.
//! 3. A row does not hold exactly one of the keys `text` and `base64`, or
//!    its `base64` text is no base64 with padding.
//! 4. The registry or the path of a row is no path of names: a part is
//!    empty, `.` or `..`.
//! 5. Two rows have the same registry and the same path.
//!
//! A test writes each file below a directory of its own. Case 4 thus keeps
//! each write inside that directory.
//!
//! The reader has no Python origin. `render_registries` of
//! `vectors/surfaces/family_file.py` writes the file.

use std::collections::HashSet;

use serde::{Deserialize, Deserializer};

use super::{
    FORMAT, VectorsError, base64_decode, is_data_path, object_of, objects, read_text, wrong_format,
};

/// The file, relative to `vectors/data`.
const REGISTRIES_FILE: &str = "family_file.registries.json";

/// The `kind` of the file.
const REGISTRIES_KIND: &str = "registries";

/// One file of one registry that a vector names.
///
/// ```
/// use creche_vectors::RegistryFile;
///
/// let files: Vec<RegistryFile> = creche_vectors::registries()?;
///
/// for file in &files {
///     let _bytes: &[u8] = file.bytes();
///
///     // A path of names: a test can write the bytes below a directory of its own.
///     assert!(!file.registry().starts_with('/'));
///     assert!(!file.path().is_empty());
///     assert!(!file.path().split('/').any(|part| part == ".."));
/// }
/// # Ok::<(), creche_vectors::VectorsError>(())
/// ```
///
/// Code outside this crate cannot build a row. Each row comes from the file,
/// and its path has no part that leaves the registry:
///
/// ```compile_fail,E0451
/// use creche_vectors::RegistryFile;
///
/// let file = RegistryFile {
///     registry: String::from("family/tests/fixtures/registry"),
///     path: String::from("../../../../Cargo.toml"),
///     bytes: Vec::new(),
/// };
/// ```
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RegistryFile {
    registry: String,
    path: String,
    bytes: Vec<u8>,
}

impl RegistryFile {
    /// The path of the registry from the repository root. It is the value of
    /// `params.registry` in a vector.
    #[must_use]
    pub fn registry(&self) -> &str {
        &self.registry
    }

    /// The path of the file from the root of its registry.
    #[must_use]
    pub fn path(&self) -> &str {
        &self.path
    }

    /// The bytes of the file.
    #[must_use]
    pub fn bytes(&self) -> &[u8] {
        &self.bytes
    }

    /// The row of a raw row. The error is the reason of a refusal.
    fn checked(raw: RawRegistryFile) -> Result<Self, String> {
        if !is_data_path(&raw.registry) {
            return Err(format!("{:?} is no path of a registry", raw.registry));
        }
        if !is_data_path(&raw.path) {
            return Err(format!(
                "{:?} is no path of a file in the registry {}",
                raw.path, raw.registry
            ));
        }

        let at = |reason: &str| format!("the file {} of {}: {reason}", raw.path, raw.registry);
        let bytes = match (raw.text, raw.base64) {
            (Some(text), None) => text.into_bytes(),
            (None, Some(encoded)) => base64_decode(&encoded).map_err(|reason| at(&reason))?,
            (Some(_), Some(_)) | (None, None) => {
                return Err(at(
                    "a row holds exactly one of the keys `text` and `base64`",
                ));
            }
        };

        Ok(Self {
            registry: raw.registry,
            path: raw.path,
            bytes,
        })
    }
}

/// The file, as `serde` reads it. It checks no rule.
#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct RawRegistries {
    format: u64,
    kind: String,
    #[serde(deserialize_with = "objects")]
    files: Vec<RawRegistryFile>,
}

/// One row of the file, as `serde` reads it. The bytes of the file are an
/// input form: `text` or `base64`.
#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct RawRegistryFile {
    registry: String,
    path: String,
    #[serde(default, deserialize_with = "present")]
    text: Option<String>,
    #[serde(default, deserialize_with = "present")]
    base64: Option<String>,
}

/// The text of a key that the row holds. An `Option` of `serde` also reads
/// `null` as an absent key. This reader refuses `null`.
fn present<'de, D: Deserializer<'de>>(deserializer: D) -> Result<Option<String>, D::Error> {
    String::deserialize(deserializer).map(Some)
}

/// Each row of `vectors/data/family_file.registries.json`, in the order of
/// the file.
///
/// The function has no Python origin. `render_registries` of
/// `vectors/surfaces/family_file.py` writes the file.
///
/// # Errors
///
/// [`VectorsError`] for a file that the reader cannot read, for a format
/// that is not 1 and for a path that leaves its registry. The doc comment of
/// this module lists each other case.
pub fn registries() -> Result<Vec<RegistryFile>, VectorsError> {
    registries_of(&read_text(REGISTRIES_FILE)?)
}

/// The rows of the file whose JSON text is `text`.
fn registries_of(text: &str) -> Result<Vec<RegistryFile>, VectorsError> {
    let refused = |reason: String| VectorsError::new(REGISTRIES_FILE, reason);
    let raw: RawRegistries = object_of(text).map_err(|error| refused(error.to_string()))?;

    if raw.format != FORMAT {
        return Err(refused(wrong_format(raw.format)));
    }
    if raw.kind != REGISTRIES_KIND {
        return Err(refused(format!(
            "the kind is {:?}, and the kind of the file is {REGISTRIES_KIND:?}",
            raw.kind
        )));
    }

    let files: Vec<RegistryFile> = raw
        .files
        .into_iter()
        .map(RegistryFile::checked)
        .collect::<Result<_, _>>()
        .map_err(refused)?;
    let mut places = HashSet::new();
    if let Some(twice) = files
        .iter()
        .find(|file| !places.insert((file.registry.as_str(), file.path.as_str())))
    {
        return Err(refused(format!(
            "two rows have the file {} of {}",
            twice.path, twice.registry
        )));
    }

    Ok(files)
}

#[cfg(test)]
mod tests {
    use serde_json::{Value, json};

    use super::*;

    fn row_json() -> Value {
        json!({"registry": "made/registry", "path": "families/chat/family.yaml", "text": "a: 1\n"})
    }

    /// The JSON text of a file with these rows.
    fn file(rows: &Value) -> String {
        json!({"format": 1, "kind": "registries", "files": rows}).to_string()
    }

    /// The JSON text of a file with one row, with one member of the file or
    /// of the row replaced.
    fn file_with(key: &str, member: Value) -> String {
        let mut file = json!({"format": 1, "kind": "registries", "files": [row_json()]});
        if file.get(key).is_some() {
            file[key] = member;
        } else {
            file["files"][0][key] = member;
        }

        file.to_string()
    }

    #[test]
    fn a_file_in_the_form_of_the_generator_reads() {
        let text = file(&json!([
            row_json(),
            {"registry": "made/registry", "path": "logo.bin", "base64": "/w=="},
            {"registry": "made/other", "path": "families/chat/family.yaml", "text": ""},
        ]));
        let files = registries_of(&text).unwrap();
        let [first, second, third] = files.as_slice() else {
            panic!("the file holds three rows");
        };

        assert_eq!(first.registry(), "made/registry");
        assert_eq!(first.path(), "families/chat/family.yaml");
        assert_eq!(first.bytes(), b"a: 1\n");
        assert_eq!(second.bytes(), [0xff]);
        assert_eq!(third.registry(), "made/other");
        assert_eq!(third.bytes(), b"");
        assert_eq!(registries_of(&file(&json!([]))), Ok(Vec::new()));
    }

    #[test]
    fn a_file_that_breaks_a_rule_is_refused() {
        let no_form = json!([{"registry": "made/registry", "path": "a.yaml"}]);
        let two_forms =
            json!([{"registry": "made/registry", "path": "a.yaml", "text": "a", "base64": "YQ=="}]);
        let refused: [(String, &str); 22] = [
            (
                file_with("format", json!(2)),
                "the format is 2, and the reader takes 1",
            ),
            (file_with("format", json!(1.0)), "invalid type"),
            (
                file_with("kind", json!("index")),
                "the kind is \"index\", and the kind of the file is \"registries\"",
            ),
            (file_with("extra", json!(1)), "unknown field `extra`"),
            (
                String::from(r#"{"format": 1, "kind": "registries"}"#),
                "missing field `files`",
            ),
            (String::from("{"), "EOF while parsing"),
            // An array in the place of an object.
            (
                String::from(r#"[1, "registries", []]"#),
                "invalid type: sequence, expected a JSON object",
            ),
            (
                file(&json!([["made/registry", "a.yaml", "a", null]])),
                "invalid type: sequence, expected a JSON object",
            ),
            (
                file(&json!([row_json(), row_json()])),
                "two rows have the file families/chat/family.yaml of made/registry",
            ),
            (
                file(&no_form),
                "the file a.yaml of made/registry: a row holds exactly one of the keys `text` and `base64`",
            ),
            (
                file(&two_forms),
                "the file a.yaml of made/registry: a row holds exactly one of the keys `text` and `base64`",
            ),
            (file_with("text", json!(null)), "invalid type"),
            (file_with("text", json!(7)), "invalid type"),
            (
                file(&json!([{"registry": "made/registry", "path": "a.bin", "base64": "Zg"}])),
                "the file a.bin of made/registry: the text is no base64 with padding",
            ),
            (
                file(
                    &json!([{"registry": "made/registry", "path": "a.bin", "repeat": [["a", 2]]}]),
                ),
                "unknown field `repeat`",
            ),
            (
                file_with("path", json!("../../Cargo.toml")),
                "\"../../Cargo.toml\" is no path of a file in the registry made/registry",
            ),
            (
                file_with("path", json!("/etc/hosts")),
                "\"/etc/hosts\" is no path of a file in the registry made/registry",
            ),
            (
                file_with("path", json!("families/./family.yaml")),
                "\"families/./family.yaml\" is no path of a file in the registry made/registry",
            ),
            (
                file_with("path", json!("")),
                "\"\" is no path of a file in the registry made/registry",
            ),
            (
                file_with("registry", json!("../registry")),
                "\"../registry\" is no path of a registry",
            ),
            (
                file_with("registry", json!("/made/registry")),
                "\"/made/registry\" is no path of a registry",
            ),
            (
                file_with("registry", json!("")),
                "\"\" is no path of a registry",
            ),
        ];

        for (text, reason) in refused {
            let error = registries_of(&text).unwrap_err();

            assert_eq!(error.file(), "family_file.registries.json", "{text}");
            assert!(
                error.reason().starts_with(reason),
                "{}: {text}",
                error.reason()
            );
        }
    }

    /// The walk of the committed file. Each vector of `family_file` names a
    /// registry, so the file holds a row.
    #[test]
    fn each_row_of_the_committed_file_reads() {
        let files = registries().unwrap();

        assert!(!files.is_empty(), "the file holds no row");

        for file in &files {
            assert!(!file.registry().is_empty());
            assert!(!file.path().is_empty(), "{}", file.registry());
        }
    }
}
