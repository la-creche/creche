//! Reads the vector files under `vectors/data` for a differential test.
//!
//! A vector is one input and what the Python implementation did with it.
//! `vectors/README.md` holds the file format. `rust/AGENTS.md` says how a test
//! uses this module. The module is test code: it stops the test on a file
//! that it cannot read.

use std::collections::BTreeMap;
use std::fs;
use std::path::PathBuf;

use serde::Deserialize;
use serde::de::DeserializeOwned;
use serde_json::{Map, Value};

/// The version of the file format that this reader takes.
const FORMAT: u64 = 1;

/// The directory of the vector files. The crate is three levels below the
/// repository root.
const DATA_DIR: &str = concat!(env!("CARGO_MANIFEST_DIR"), "/../../../vectors/data");

/// The list of every surface, relative to [`DATA_DIR`].
const INDEX_FILE: &str = "index.json";

/// The inputs on which two Python copies of one id grammar differ.
const DISAGREEMENTS_FILE: &str = "ids/disagreements.json";

/// The `kind` of the index file.
const INDEX_KIND: &str = "index";

/// The count of the hexadecimal digits of a SHA-256.
const DIGEST_DIGITS: usize = 64;

/// The `kind` of the disagreements file.
const DISAGREEMENTS_KIND: &str = "disagreements";

/// The key of the value that the Python code parsed an input into.
const VALUE_KEY: &str = "value";

/// The key of the reason that the Python code gave for a refusal.
const REFUSAL_KEY: &str = "refusal";

/// What the Python code did with an input.
///
/// The set is closed by the file format. A reader refuses a file with another
/// result: the format version changes before a new result appears.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize)]
#[serde(rename_all = "lowercase")]
pub(crate) enum Outcome {
    /// The Python code took the input.
    Accepted,
    /// The Python code refused the input in the way its contract states.
    Refused,
    /// The Python code raised an exception that its contract does not state.
    /// The Rust code refuses such an input.
    Raised,
}

/// One row of the index: one surface and its counts.
#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct IndexRow {
    /// The name of the surface, for example `id.family_name.attendance`.
    pub(crate) surface: String,
    /// The vector file, relative to `vectors/data`.
    pub(crate) path: String,
    /// The Python entry point that the generator calls.
    pub(crate) entry: String,
    /// The count of vectors in the file.
    pub(crate) vectors: usize,
    /// The count of vectors with the result `accepted`.
    pub(crate) accepted: usize,
    /// The count of vectors with the result `refused`.
    pub(crate) refused: usize,
    /// The count of vectors with the result `raised`.
    pub(crate) raised: usize,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct Index {
    format: u64,
    /// The frozen files: the path of each one under `vectors/data`, and the
    /// SHA-256 of its bytes. `vectors/README.md` says what a frozen file is.
    frozen: BTreeMap<String, String>,
    kind: String,
    surfaces: Vec<IndexRow>,
}

/// One vector file: one entry point of the Python code and its vectors.
#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct Surface {
    format: u64,
    /// The name of the surface.
    pub(crate) surface: String,
    /// The Python entry point that the generator calls.
    pub(crate) entry: String,
    /// The design contract that the surface belongs to.
    pub(crate) contract: String,
    /// What a reader must know to replay a vector of this file.
    pub(crate) notes: Vec<String>,
    /// The facts that every vector of the file shares.
    pub(crate) context: Map<String, Value>,
    /// The vectors, in the order of the file.
    pub(crate) vectors: Vec<Vector>,
}

/// One input and what the Python code did with it.
#[derive(Debug, Deserialize)]
pub(crate) struct Vector {
    /// A name that is unique in the file.
    pub(crate) id: String,
    /// The input, in one of the five forms.
    pub(crate) input: Input,
    /// What the Python code did.
    pub(crate) result: Outcome,
    /// Each other key of the vector: `value`, `refusal`, `params`, `issues`,
    /// `status`, `http_status`, `output` and `exception`.
    #[serde(flatten)]
    fields: Map<String, Value>,
}

impl Vector {
    /// One key of the vector that is not `id`, `input` or `result`.
    pub(crate) fn field(&self, key: &str) -> Option<&Value> {
        self.fields.get(key)
    }

    /// The normalized value that the Python code parsed the input into.
    pub(crate) fn value(&self) -> Option<&Value> {
        self.field(VALUE_KEY)
    }

    /// The reason that the Python code gave for a refusal.
    pub(crate) fn refusal(&self) -> Option<&Value> {
        self.field(REFUSAL_KEY)
    }
}

/// The input of a vector. An `input` object has exactly one key.
#[derive(Debug, Deserialize)]
#[serde(rename_all = "lowercase")]
pub(crate) enum Input {
    /// This text. For an entry point that takes bytes, its UTF-8 bytes.
    Text(String),
    /// These bytes, as base64. The generator uses this form only for bytes
    /// that are not UTF-8.
    Base64(String),
    /// A long text. Each item is a text and the count of its repeats.
    Repeat(Vec<(String, usize)>),
    /// The named arguments of a builder. A value can be a marker object.
    Args(Map<String, Value>),
    /// The chunks of one byte stream, in order.
    Chunks(Vec<Chunk>),
}

impl Input {
    /// The input as text. `None` for a form that is not text.
    pub(crate) fn text(&self) -> Option<String> {
        match self {
            Self::Text(text) => Some(text.clone()),
            Self::Repeat(parts) => Some(expand(parts)),
            Self::Base64(_) | Self::Args(_) | Self::Chunks(_) => None,
        }
    }

    /// The named arguments of a builder. `None` for each other form.
    pub(crate) fn args(&self) -> Option<&Map<String, Value>> {
        match self {
            Self::Args(args) => Some(args),
            Self::Text(_) | Self::Base64(_) | Self::Repeat(_) | Self::Chunks(_) => None,
        }
    }

    /// The input as bytes. `None` for the named arguments of a builder.
    pub(crate) fn bytes(&self) -> Option<Vec<u8>> {
        match self {
            Self::Text(text) => Some(text.clone().into_bytes()),
            Self::Base64(encoded) => Some(base64_decode(encoded)),
            Self::Repeat(parts) => Some(expand(parts).into_bytes()),
            Self::Chunks(chunks) => Some(chunks.iter().flat_map(Chunk::bytes).collect()),
            Self::Args(_) => None,
        }
    }
}

/// One chunk of a byte stream.
#[derive(Debug, Deserialize)]
#[serde(rename_all = "lowercase")]
pub(crate) enum Chunk {
    /// The UTF-8 bytes of this text.
    Text(String),
    /// These bytes, as base64.
    Base64(String),
}

impl Chunk {
    /// The bytes of the chunk.
    pub(crate) fn bytes(&self) -> Vec<u8> {
        match self {
            Self::Text(text) => text.clone().into_bytes(),
            Self::Base64(encoded) => base64_decode(encoded),
        }
    }
}

/// The key of each marker object.
const MARKER_KEYS: [&str; 6] = ["$int", "$float", "$utf16", "$base64", "$entries", "$json"];

/// A value that has no JSON form that every strict reader accepts. The
/// generator writes it as an object with exactly one key, a marker object.
///
/// The set is closed by the file format. A plain value of a vector is never
/// an object that holds one of the six keys and no other key.
#[derive(Debug, Clone, PartialEq, Deserialize)]
pub(crate) enum Marker {
    /// An integer below -2^63 or above 2^64 - 1, as its decimal text.
    #[serde(rename = "$int")]
    Int(String),
    /// A float that is not finite.
    #[serde(rename = "$float")]
    Float(NotFinite),
    /// A string that holds a lone surrogate, as its UTF-16 code units.
    #[serde(rename = "$utf16")]
    Utf16(Vec<u16>),
    /// Bytes, as base64. [`Marker::bytes`] gives the bytes.
    #[serde(rename = "$base64")]
    Base64(String),
    /// A mapping with a key that is not a plain string, as its pairs in the
    /// order of the mapping. A key and a value can be a marker object.
    #[serde(rename = "$entries")]
    Entries(Vec<(Value, Value)>),
    /// A field that nests deeper than 96 levels, as its JSON text. The text
    /// can hold the other markers.
    #[serde(rename = "$json")]
    Json(String),
}

/// A float that is not finite.
///
/// The set is closed by the file format. A reader refuses another text.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize)]
pub(crate) enum NotFinite {
    /// Not a number.
    #[serde(rename = "NaN")]
    Nan,
    /// Positive infinity.
    Infinity,
    /// Negative infinity.
    #[serde(rename = "-Infinity")]
    NegativeInfinity,
}

impl Marker {
    /// The marker that `value` is. `None` for a plain value.
    ///
    /// The function stops the test on a marker object whose content has the
    /// wrong form.
    pub(crate) fn of(value: &Value) -> Option<Self> {
        let object = value.as_object()?;
        let mut keys = object.keys();
        let key = keys.next()?;
        if keys.next().is_some() || !MARKER_KEYS.contains(&key.as_str()) {
            return None;
        }

        match Self::deserialize(value) {
            Ok(marker) => Some(marker),
            Err(error) => panic!("the content of a {key} marker: {error}"),
        }
    }

    /// The bytes of a `$base64` marker. `None` for each other marker.
    pub(crate) fn bytes(&self) -> Option<Vec<u8>> {
        match self {
            Self::Base64(encoded) => Some(base64_decode(encoded)),
            Self::Int(_) | Self::Float(_) | Self::Utf16(_) | Self::Entries(_) | Self::Json(_) => {
                None
            }
        }
    }
}

/// One input on which two Python copies of one id grammar differ.
#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct Disagreement {
    /// The name of the grammar, for example `tool_name`.
    pub(crate) grammar: String,
    /// The id of the vector in each file of the grammar.
    pub(crate) id: String,
    /// The input.
    pub(crate) input: Input,
    /// The surfaces, by what each one did.
    pub(crate) results: Results,
}

/// The surfaces of one disagreement, by result.
#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct Results {
    /// The surfaces that took the input.
    #[serde(default)]
    pub(crate) accepted: Vec<String>,
    /// The surfaces that refused the input.
    #[serde(default)]
    pub(crate) refused: Vec<String>,
    /// The surfaces that raised on the input.
    #[serde(default)]
    pub(crate) raised: Vec<String>,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct Disagreements {
    format: u64,
    kind: String,
    rows: Vec<Disagreement>,
}

/// Every row of `vectors/data/index.json`, in the order of the file.
pub(crate) fn index() -> Vec<IndexRow> {
    let index: Index = read(INDEX_FILE);

    assert_eq!(index.format, FORMAT, "{INDEX_FILE}: the format");
    assert_eq!(index.kind, INDEX_KIND, "{INDEX_FILE}: the kind");
    for (path, digest) in &index.frozen {
        let lower_hex = |byte| matches!(byte, b'0'..=b'9' | b'a'..=b'f');

        assert!(
            digest.len() == DIGEST_DIGITS && digest.bytes().all(lower_hex),
            "{INDEX_FILE}: the digest of the frozen file {path}"
        );
    }

    index.surfaces
}

/// The vector file of one surface. The index gives the path.
///
/// The function stops the test when the index has no such surface, when the
/// format is not 1, and when a count differs from the index.
pub(crate) fn surface(name: &str) -> Surface {
    let Some(row) = index().into_iter().find(|row| row.surface == name) else {
        panic!("{INDEX_FILE} has no surface {name}");
    };
    let surface: Surface = read(&row.path);

    assert_eq!(surface.format, FORMAT, "{}: the format", row.path);
    assert_eq!(surface.surface, row.surface, "{}: the surface", row.path);
    assert_eq!(surface.entry, row.entry, "{}: the entry", row.path);
    assert_eq!(
        surface.vectors.len(),
        row.vectors,
        "{}: the count",
        row.path
    );
    for (outcome, count) in [
        (Outcome::Accepted, row.accepted),
        (Outcome::Refused, row.refused),
        (Outcome::Raised, row.raised),
    ] {
        let found = surface
            .vectors
            .iter()
            .filter(|vector| vector.result == outcome);

        assert_eq!(found.count(), count, "{}: the {outcome:?} count", row.path);
    }

    surface
}

/// Every row of `vectors/data/ids/disagreements.json`.
pub(crate) fn disagreements() -> Vec<Disagreement> {
    let file: Disagreements = read(DISAGREEMENTS_FILE);

    assert_eq!(file.format, FORMAT, "{DISAGREEMENTS_FILE}: the format");
    assert_eq!(
        file.kind, DISAGREEMENTS_KIND,
        "{DISAGREEMENTS_FILE}: the kind"
    );

    file.rows
}

/// Reads one JSON file under `vectors/data`.
fn read<T: DeserializeOwned>(path: &str) -> T {
    let file = PathBuf::from(DATA_DIR).join(path);
    let text = match fs::read_to_string(&file) {
        Ok(text) => text,
        Err(error) => panic!("{}: {error}", file.display()),
    };

    match serde_json::from_str(&text) {
        Ok(value) => value,
        Err(error) => panic!("{}: {error}", file.display()),
    }
}

/// The text that a `repeat` input stands for.
fn expand(parts: &[(String, usize)]) -> String {
    parts
        .iter()
        .map(|(text, count)| text.repeat(*count))
        .collect()
}

/// The count of bits in one base64 character.
const BASE64_BITS: u32 = 6;

/// The count of bits in one byte.
const BYTE_BITS: u32 = 8;

/// The character that fills the last group of a base64 text.
const BASE64_PAD: u8 = b'=';

/// The value of one character of the standard base64 alphabet.
fn base64_value(character: u8) -> u32 {
    let value = match character {
        b'A'..=b'Z' => character - b'A',
        b'a'..=b'z' => character - b'a' + 26,
        b'0'..=b'9' => character - b'0' + 52,
        b'+' => 62,
        b'/' => 63,
        _ => panic!("{character:#04x} is not in the base64 alphabet"),
    };

    u32::from(value)
}

/// The bytes of a base64 text in the standard alphabet, with padding.
fn base64_decode(encoded: &str) -> Vec<u8> {
    let mut decoded = Vec::new();
    let mut buffer = 0_u32;
    let mut bits = 0_u32;
    for character in encoded.bytes().filter(|byte| *byte != BASE64_PAD) {
        buffer = (buffer << BASE64_BITS) | base64_value(character);
        bits += BASE64_BITS;
        if bits < BYTE_BITS {
            continue;
        }

        bits -= BYTE_BITS;
        decoded.push(u8::try_from((buffer >> bits) & 0xff).unwrap());
    }

    decoded
}

#[cfg(test)]
mod tests {
    use std::collections::HashSet;

    use serde_json::json;

    use super::*;

    #[test]
    fn base64_decodes_each_length_of_the_last_group() {
        assert_eq!(base64_decode(""), b"");
        assert_eq!(base64_decode("Zg=="), b"f");
        assert_eq!(base64_decode("Zm8="), b"fo");
        assert_eq!(base64_decode("Zm9v"), b"foo");
        assert_eq!(base64_decode("Zm9vYg=="), b"foob");
        assert_eq!(base64_decode("+/8="), [0xfb, 0xff]);
    }

    #[test]
    fn a_repeat_input_joins_each_text_count_times() {
        let input = Input::Repeat(vec![
            ("[".to_owned(), 3),
            ("x".to_owned(), 0),
            ("]".to_owned(), 2),
        ]);

        assert_eq!(input.text().unwrap(), "[[[]]");
        assert_eq!(input.bytes().unwrap(), b"[[[]]");
    }

    #[test]
    fn the_chunks_of_a_stream_join_in_order() {
        let input: Input =
            serde_json::from_str(r#"{"chunks": [{"text": "ab"}, {"base64": "/w=="}]}"#).unwrap();

        assert_eq!(input.text(), None);
        assert_eq!(input.bytes().unwrap(), [b'a', b'b', 0xff]);
    }

    #[test]
    fn an_input_with_two_forms_is_refused() {
        let two = r#"{"text": "a", "base64": "YQ=="}"#;

        assert!(serde_json::from_str::<Input>(two).is_err());
        assert!(serde_json::from_str::<Input>(r#"{"words": "a"}"#).is_err());
    }

    #[test]
    fn every_file_of_the_index_reads() {
        let rows = index();
        let names: HashSet<&str> = rows.iter().map(|row| row.surface.as_str()).collect();

        assert_eq!(
            names.len(),
            rows.len(),
            "two rows of the index share a name"
        );
        for row in &rows {
            let surface = surface(&row.surface);
            let ids: HashSet<&str> = surface
                .vectors
                .iter()
                .map(|vector| vector.id.as_str())
                .collect();

            assert_eq!(
                ids.len(),
                surface.vectors.len(),
                "{}: an id is used twice",
                row.path
            );
            assert!(!surface.contract.is_empty(), "{}: no contract", row.path);
            assert!(
                surface.notes.iter().all(|note| !note.is_empty()),
                "{}",
                row.path
            );
            assert!(
                surface.context.keys().all(|key| !key.is_empty()),
                "{}",
                row.path
            );
        }
    }

    #[test]
    fn every_input_of_every_file_has_a_form_that_this_module_reads() {
        for row in index() {
            for vector in surface(&row.surface).vectors {
                let read = match &vector.input {
                    Input::Text(_) | Input::Repeat(_) => vector.input.text().is_some(),
                    // The generator writes base64 only for bytes that are not UTF-8.
                    Input::Base64(_) => String::from_utf8(vector.input.bytes().unwrap()).is_err(),
                    Input::Chunks(_) => vector.input.bytes().is_some(),
                    // A builder with no argument has an empty object.
                    Input::Args(_) => {
                        vector.input.args().is_some() && vector.input.bytes().is_none()
                    }
                };

                assert!(read, "{} {}", row.surface, vector.id);
            }
        }
    }

    #[test]
    fn a_value_and_a_refusal_are_fields_of_a_vector() {
        let vector: Vector = serde_json::from_str(
            r#"{"id": "one", "input": {"text": "a"}, "result": "refused", "refusal": {"code": "no"}}"#,
        )
        .unwrap();

        assert_eq!(vector.value(), None);
        assert_eq!(vector.refusal(), Some(&serde_json::json!({"code": "no"})));
        assert_eq!(vector.field("params"), None);
    }

    #[test]
    fn each_marker_object_reads_as_its_marker() {
        let pairs = vec![
            (json!(1), json!("one")),
            (json!({"$int": "-1"}), json!(null)),
        ];
        let read = [
            (
                json!({"$int": "18446744073709551616"}),
                Marker::Int("18446744073709551616".to_owned()),
            ),
            (json!({"$float": "NaN"}), Marker::Float(NotFinite::Nan)),
            (
                json!({"$float": "Infinity"}),
                Marker::Float(NotFinite::Infinity),
            ),
            (
                json!({"$float": "-Infinity"}),
                Marker::Float(NotFinite::NegativeInfinity),
            ),
            (
                json!({"$utf16": [97, 55296, 98]}),
                Marker::Utf16(vec![97, 0xd800, 98]),
            ),
            (
                json!({"$base64": "+/8="}),
                Marker::Base64("+/8=".to_owned()),
            ),
            (
                json!({"$entries": [[1, "one"], [{"$int": "-1"}, null]]}),
                Marker::Entries(pairs),
            ),
            (json!({"$json": "[[1]]"}), Marker::Json("[[1]]".to_owned())),
        ];
        let keys: HashSet<&str> = read
            .iter()
            .flat_map(|(value, _)| value.as_object().unwrap().keys())
            .map(String::as_str)
            .collect();

        for (value, marker) in &read {
            assert_eq!(Marker::of(value).as_ref(), Some(marker), "{value}");
        }

        assert_eq!(keys, HashSet::from(MARKER_KEYS));
        assert_eq!(
            Marker::Base64("+/8=".to_owned()).bytes(),
            Some(vec![0xfb, 0xff])
        );
        assert_eq!(Marker::Json("[[1]]".to_owned()).bytes(), None);
    }

    #[test]
    fn a_plain_value_is_no_marker() {
        for value in [
            json!(null),
            json!(7),
            json!("$int"),
            json!(["$int", "1"]),
            json!({}),
            json!({"int": "1"}),
            json!({"$other": "1"}),
            json!({"$int": "1", "$float": "NaN"}),
            json!({"$int": "1", "unit": "ms"}),
        ] {
            assert_eq!(Marker::of(&value), None, "{value}");
        }
    }

    #[test]
    #[should_panic(expected = "the content of a $float marker")]
    fn a_marker_with_a_wrong_content_stops_the_test() {
        let _ = Marker::of(&json!({"$float": "1.5"}));
    }

    /// The count of marker objects in `value`, at each level. The walk reads
    /// each marker, so a marker with a wrong content stops the test.
    fn markers_in(value: &Value) -> usize {
        let here = usize::from(Marker::of(value).is_some());
        let below: usize = match value {
            Value::Array(items) => items.iter().map(markers_in).sum(),
            Value::Object(fields) => fields.values().map(markers_in).sum(),
            Value::Null | Value::Bool(_) | Value::Number(_) | Value::String(_) => 0,
        };

        here + below
    }

    #[test]
    fn every_marker_of_every_file_reads() {
        let nested = json!({"value": [{"$int": "-1"}, {"$entries": [[{"$float": "NaN"}, 1]]}]});
        let found: usize = index()
            .iter()
            .map(|row| markers_in(&read::<Value>(&row.path)))
            .sum();

        assert_eq!(markers_in(&nested), 3);
        assert!(found > 0, "no file holds a marker object");
    }

    #[test]
    fn every_disagreement_names_surfaces_of_the_index() {
        let rows = index();
        let names: HashSet<&str> = rows.iter().map(|row| row.surface.as_str()).collect();
        for row in disagreements() {
            let results = &row.results;
            let named = results
                .accepted
                .iter()
                .chain(&results.refused)
                .chain(&results.raised);

            assert!(!row.grammar.is_empty());
            assert!(row.input.text().is_some(), "{} {}", row.grammar, row.id);
            for surface in named {
                assert!(
                    names.contains(surface.as_str()),
                    "{} {}: {surface}",
                    row.grammar,
                    row.id
                );
            }
        }
    }
}
