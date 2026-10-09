//! The reader of the vector files under `vectors/data`, for a differential
//! test.
//!
//! A vector is one input and what the Python implementation did with it.
//! `vectors/README.md` holds the file format. `rust/AGENTS.md`, "The
//! differential test", says how a test uses a reader.
//!
//! Each crate of the workspace can take this crate as a dev dependency. The
//! crate uses `serde` and `serde_json` only, and no other crate of the
//! workspace. `creche-testkit` gives the crate to its users as
//! `creche_testkit::vectors`. The file `AGENTS.md` of the crate holds the
//! rules for a change here.
//!
//! The reader returns a [`VectorsError`] for a file that it cannot give, and
//! the test calls `unwrap`. The reader reads each file through a raw type of
//! `serde`. A conversion that can fail then makes the public type.
//!
//! | Function | What it gives |
//! |---|---|
//! | [`index`] | Each row of the index. |
//! | [`surface`] | The vector file of one surface. |
//!
//! The reader refuses a vector file in each of these cases:
//!
//! 1. The format of the file is not 1.
//! 2. The file holds a key that the format does not name.
//! 3. The file or a vector of it is no JSON object.
//! 4. The name or the entry point of the file differs from its index row.
//! 5. A count of the file differs from its index row.
//! 6. Two vectors of the file have the same id.
//! 7. An input, an `output` or a marker object has a form that the format
//!    does not name.
//!
//! The reader refuses the index in each of these cases:
//!
//! 1. The format is not 1, or the kind is not `index`.
//! 2. The index or a row holds a key that the format does not name, or lacks
//!    one.
//! 3. The index or a row is no JSON object.
//! 4. An object of the index holds one key two times.
//! 5. The path of a row is no path below `vectors/data`, or the three counts
//!    of a row do not add up.
//! 6. Two rows have the same surface.
//! 7. The index names a frozen file with a path or with a digest that the
//!    generator does not write in that form.
//!
//! A frozen file is a data file whose Python origin left the repository. The
//! index holds the SHA-256 of each one. The reader reads the form of that map
//! and compares no digest: `vectors/tests` holds each digest. The generator,
//! `vectors/generate.py`, refuses the same paths and the same digests.
//!
//! The index is strict JSON: `rust/AGENTS.md`, "JSON", has the rules of such
//! a text. `creche-contracts` holds the strict reader, and that crate can
//! take this one for its tests. This crate thus does not call that reader.
//! The raw types of the index hold the rules themselves:
//!
//! - Each raw type of the index is closed, and `serde` refuses a field that
//!   an object holds two times.
//! - The raw type of the map `frozen` refuses a path that the map holds two
//!   times.
//! - `serde_json` refuses a lone surrogate, a byte order mark, and the words
//!   `NaN` and `Infinity`.
//! - No field of the index takes a value that nests. Each number field is an
//!   integer with no sign, so `serde` refuses a float and an integer past 64
//!   bits.
//!
//! A vector file can nest 100 levels, so no reader of a strict text reads it.
//!
//! The reader has no Python origin. `vectors/core.py` writes the files that
//! it reads.

use std::collections::{BTreeMap, HashSet};
use std::error::Error;
use std::fmt;
use std::fs;
use std::marker::PhantomData;
use std::path::PathBuf;

use serde::de::value::MapAccessDeserializer;
use serde::de::{self, MapAccess, Visitor};
use serde::{Deserialize, Deserializer};
use serde_json::{Map, Value};

/// The version of the file format that this reader takes.
const FORMAT: u64 = 1;

/// The directory of the vector files. The crate is three levels below the
/// repository root.
const DATA_DIR: &str = concat!(env!("CARGO_MANIFEST_DIR"), "/../../../vectors/data");

/// The list of each surface, relative to [`DATA_DIR`].
const INDEX_FILE: &str = "index.json";

/// The `kind` of the index file.
const INDEX_KIND: &str = "index";

/// The end of the name of each file under `vectors/data` that the generator
/// reads.
const JSON_SUFFIX: &str = ".json";

/// The key of the value that the Python code parsed an input into.
const VALUE_KEY: &str = "value";

/// The key of the reason that the Python code gave for a refusal.
const REFUSAL_KEY: &str = "refusal";

/// The key of the other arguments of the entry point.
const PARAMS_KEY: &str = "params";

/// The key of the bytes that the Python code wrote.
const OUTPUT_KEY: &str = "output";

/// The most bytes that a `repeat` input stands for. The longest input of a
/// committed file has less than 3 MiB. A count past this limit is a damaged
/// file, and its text does not fit the memory of a test.
const REPEAT_MAX_BYTES: usize = 64 * 1024 * 1024;

/// Why the reader cannot give a file under `vectors/data`.
///
/// ```
/// use creche_vectors::VectorsError;
///
/// let error: VectorsError = creche_vectors::surface("runtime.no_such_surface").unwrap_err();
///
/// assert_eq!(error.file(), "index.json");
/// assert_eq!(error.reason(), "the index has no surface runtime.no_such_surface");
/// ```
///
/// Code outside this crate cannot build an error. Only a reader makes one,
/// for a file that it refused:
///
/// ```compile_fail,E0451
/// use creche_vectors::VectorsError;
///
/// let error = VectorsError {
///     file: String::from("index.json"),
///     reason: String::new(),
/// };
/// ```
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct VectorsError {
    file: String,
    reason: String,
}

impl VectorsError {
    fn new(file: &str, reason: impl Into<String>) -> Self {
        Self {
            file: file.to_owned(),
            reason: reason.into(),
        }
    }

    /// The file, as a path from `vectors/data`.
    #[must_use]
    pub fn file(&self) -> &str {
        &self.file
    }

    /// What is wrong with the file.
    #[must_use]
    pub fn reason(&self) -> &str {
        &self.reason
    }
}

impl fmt::Display for VectorsError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "vectors/data/{}: {}", self.file, self.reason)
    }
}

impl Error for VectorsError {}

/// What the Python code did with an input.
///
/// The file format closes the set. The reader refuses a file with another
/// result: the format version changes before a new result appears.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Outcome {
    /// The Python code took the input.
    Accepted,
    /// The Python code refused the input in the way that its contract
    /// states.
    Refused,
    /// The Python code raised an exception that its contract does not state.
    /// The Rust code refuses such an input.
    Raised,
}

impl Outcome {
    /// The three results, in the order of the counts of an index row.
    const EACH: [Self; 3] = [Self::Accepted, Self::Refused, Self::Raised];

    /// The word of the result in a file: `accepted`, `refused` or `raised`.
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Accepted => "accepted",
            Self::Refused => "refused",
            Self::Raised => "raised",
        }
    }
}

impl fmt::Display for Outcome {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_str())
    }
}

/// One row of `vectors/data/index.json`: a surface, its file and its counts.
///
/// ```
/// use creche_vectors::{IndexRow, Outcome};
///
/// let rows: Vec<IndexRow> = creche_vectors::index()?;
/// let row = rows.first().ok_or("the index holds no surface")?;
/// let sum = row.count(Outcome::Accepted) + row.count(Outcome::Refused) + row.count(Outcome::Raised);
///
/// assert_eq!(sum, row.vectors());
/// assert!(row.path().ends_with(".json"));
/// # Ok::<(), Box<dyn std::error::Error>>(())
/// ```
///
/// Code outside this crate cannot build a row. Each row comes from the
/// index, and its path is under `vectors/data`:
///
/// ```compile_fail,E0451
/// use creche_vectors::{IndexRow, Outcome};
///
/// let row = IndexRow {
///     surface: String::from("id.ulid.attendance"),
///     path: String::from("../../Cargo.toml"),
///     entry: String::new(),
///     vectors: 0,
///     accepted: 0,
///     refused: 0,
///     raised: 0,
/// };
/// ```
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct IndexRow {
    surface: String,
    path: String,
    entry: String,
    vectors: usize,
    accepted: usize,
    refused: usize,
    raised: usize,
}

impl IndexRow {
    /// The name of the surface, for example `id.family_name.attendance`.
    #[must_use]
    pub fn surface(&self) -> &str {
        &self.surface
    }

    /// The vector file, relative to `vectors/data`.
    #[must_use]
    pub fn path(&self) -> &str {
        &self.path
    }

    /// The Python entry point that the generator calls.
    #[must_use]
    pub fn entry(&self) -> &str {
        &self.entry
    }

    /// The count of the vectors in the file.
    #[must_use]
    pub const fn vectors(&self) -> usize {
        self.vectors
    }

    /// The count of the vectors with the result `outcome`.
    #[must_use]
    pub const fn count(&self, outcome: Outcome) -> usize {
        match outcome {
            Outcome::Accepted => self.accepted,
            Outcome::Refused => self.refused,
            Outcome::Raised => self.raised,
        }
    }

    /// The row of a raw row. The error is the reason of a refusal.
    fn checked(raw: RawIndexRow) -> Result<Self, String> {
        if !is_data_path(&raw.path) {
            return Err(format!(
                "the path {:?} of the surface {} is not a path under vectors/data",
                raw.path, raw.surface
            ));
        }

        let sum = raw
            .accepted
            .checked_add(raw.refused)
            .and_then(|sum| sum.checked_add(raw.raised));
        if sum != Some(raw.vectors) {
            return Err(format!(
                "the three counts of the surface {} do not add up to its {} vectors",
                raw.surface, raw.vectors
            ));
        }

        Ok(Self {
            surface: raw.surface,
            path: raw.path,
            entry: raw.entry,
            vectors: raw.vectors,
            accepted: raw.accepted,
            refused: raw.refused,
            raised: raw.raised,
        })
    }
}

/// Whether `path` names a file below the directory that it is relative to,
/// in the one form that the generator writes: one name or more with `/`
/// between them, and no part that is empty, `.` or `..`.
///
/// The check reads the text and not the parts of a [`std::path::Path`]: such
/// a path also reads `a//b.json` and `a/./b.json` as `a/b.json`.
fn is_data_path(path: &str) -> bool {
    !path.is_empty() && path.split('/').all(|part| !matches!(part, "" | "." | ".."))
}

/// Whether `path` can name a frozen file: a file below `vectors/data` with a
/// name that ends in `.json`. The index itself is no such file.
///
/// `_frozen_map` of `vectors/generate.py` holds the same rule.
fn is_frozen_path(path: &str) -> bool {
    path != INDEX_FILE && path.ends_with(JSON_SUFFIX) && is_data_path(path)
}

/// The count of the characters in the digest of a frozen file: the SHA-256
/// of its bytes, as hexadecimal digits.
const DIGEST_LEN: usize = 64;

/// Whether `text` has the form of the digest of a frozen file: 64 hexadecimal
/// digits in lower case (`vectors/README.md`, "The index file").
///
/// `_frozen_map` of `vectors/generate.py` holds the same rule, and so does
/// `ids::Sha256Hex` of `creche-contracts`. This crate does not use that
/// crate. A test holds this function equal to each vector of the id grammar
/// `sha256_hex`, as the test of `ids` does for that type.
fn is_digest(text: &str) -> bool {
    text.len() == DIGEST_LEN
        && text
            .bytes()
            .all(|byte| matches!(byte, b'0'..=b'9' | b'a'..=b'f'))
}

/// One vector file: each vector of one surface.
///
/// ```
/// use creche_vectors::{Outcome, Surface};
/// use serde_json::Map;
///
/// let surface: Surface = creche_vectors::surface("id.ulid.chaperone")?;
/// let context: &Map<String, serde_json::Value> = surface.context();
///
/// assert_eq!(surface.name(), "id.ulid.chaperone");
/// assert!(context.len() < 100);
/// for vector in surface.vectors() {
///     // The input of an id grammar is one text.
///     assert!(vector.input().text().is_some(), "{}", vector.id());
///     assert_ne!(vector.result(), Outcome::Raised, "{}", vector.id());
/// }
/// # Ok::<(), Box<dyn std::error::Error>>(())
/// ```
///
/// Code outside this crate cannot build a file. Each value passed the
/// checks of [`surface`]:
///
/// ```compile_fail,E0451
/// use creche_vectors::{Outcome, Surface};
/// use serde_json::Map;
///
/// let surface = Surface {
///     name: String::from("id.ulid.attendance"),
///     entry: String::new(),
///     contract: String::new(),
///     notes: Vec::new(),
///     context: Map::new(),
///     vectors: Vec::new(),
/// };
/// ```
#[derive(Debug, Clone, PartialEq)]
pub struct Surface {
    name: String,
    entry: String,
    contract: String,
    notes: Vec<String>,
    context: Map<String, Value>,
    vectors: Vec<Vector>,
}

impl Surface {
    /// The name of the surface.
    #[must_use]
    pub fn name(&self) -> &str {
        &self.name
    }

    /// The Python entry point that the generator calls.
    #[must_use]
    pub fn entry(&self) -> &str {
        &self.entry
    }

    /// The design contract that the surface belongs to.
    #[must_use]
    pub fn contract(&self) -> &str {
        &self.contract
    }

    /// What a reader must know to replay a vector of this file.
    #[must_use]
    pub fn notes(&self) -> &[String] {
        &self.notes
    }

    /// The facts that each vector of the file shares.
    #[must_use]
    pub const fn context(&self) -> &Map<String, Value> {
        &self.context
    }

    /// The vectors, in the order of the file.
    #[must_use]
    pub fn vectors(&self) -> &[Vector] {
        &self.vectors
    }

    /// The vector with the id `id`. `None` when the file holds no vector
    /// with that id.
    #[must_use]
    pub fn vector(&self, id: &str) -> Option<&Vector> {
        self.vectors.iter().find(|vector| vector.id == id)
    }
}

/// One vector: an input and what the Python code did with it.
///
/// ```
/// use creche_vectors::{Input, Outcome, Vector};
/// use serde_json::Map;
///
/// let surface = creche_vectors::surface("id.ulid.chaperone")?;
/// let vector: &Vector = surface.vectors().first().ok_or("the file holds no vector")?;
/// let no_fields: Map<String, serde_json::Value> = Map::new();
///
/// assert_eq!(surface.vector(vector.id()), Some(vector));
/// assert_eq!(vector.field("no such key"), no_fields.get("no such key"));
/// match vector.input() {
///     Input::Text(text) | Input::Repeat(text) => assert_eq!(vector.input().text(), Some(text.as_str())),
///     Input::Base64(_) | Input::Args(_) | Input::Chunks(_) => assert_eq!(vector.input().text(), None),
/// }
/// if vector.result() == Outcome::Accepted {
///     assert!(vector.refusal().is_none());
/// }
/// # Ok::<(), Box<dyn std::error::Error>>(())
/// ```
///
/// Code outside this crate cannot build a vector. Each value comes from a
/// file that passed the checks of [`surface`]:
///
/// ```compile_fail,E0451
/// use creche_vectors::{Input, Outcome, Vector};
/// use serde_json::Map;
///
/// let vector = Vector {
///     id: String::from("31-bytes"),
///     input: Input::Text(String::new()),
///     result: Outcome::Accepted,
///     output: None,
///     fields: Map::new(),
/// };
/// ```
#[derive(Debug, Clone, PartialEq)]
pub struct Vector {
    id: String,
    input: Input,
    result: Outcome,
    /// The `output` of the vector, as an input form.
    output: Option<Input>,
    /// Each key of the vector that is not `id`, `input` or `result`.
    fields: Map<String, Value>,
}

impl Vector {
    /// A name that is unique in the file.
    #[must_use]
    pub fn id(&self) -> &str {
        &self.id
    }

    /// The input, in one of the five forms.
    #[must_use]
    pub const fn input(&self) -> &Input {
        &self.input
    }

    /// What the Python code did.
    #[must_use]
    pub const fn result(&self) -> Outcome {
        self.result
    }

    /// One key of the vector that is not `id`, `input` or `result`, for
    /// example `issues`, `status`, `http_status`, `file` or `ts_bits`.
    #[must_use]
    pub fn field(&self, key: &str) -> Option<&Value> {
        self.fields.get(key)
    }

    /// The normalized value that the Python code parsed the input into.
    #[must_use]
    pub fn value(&self) -> Option<&Value> {
        self.field(VALUE_KEY)
    }

    /// The reason that the Python code gave for a refusal.
    #[must_use]
    pub fn refusal(&self) -> Option<&Value> {
        self.field(REFUSAL_KEY)
    }

    /// The other arguments of the entry point. The notes of the file name
    /// each one.
    #[must_use]
    pub fn params(&self) -> Option<&Value> {
        self.field(PARAMS_KEY)
    }

    /// The exact bytes that the Python code wrote, as an input form. `None`
    /// for a vector of a surface that writes no bytes.
    #[must_use]
    pub const fn output(&self) -> Option<&Input> {
        self.output.as_ref()
    }

    /// The vector of a raw vector. The error is the reason of a refusal.
    fn checked(raw: RawVector) -> Result<Self, String> {
        let at = |reason: String| format!("the vector {}: {reason}", raw.id);
        let input = Input::checked(raw.input).map_err(at)?;
        let output = match raw.fields.get(OUTPUT_KEY) {
            Some(output) => Some(
                RawInput::deserialize(output)
                    .map_err(|error| format!("the output is no input form: {error}"))
                    .and_then(Input::checked)
                    .map_err(at)?,
            ),
            None => None,
        };

        if let Input::Args(arguments) = &input {
            arguments.values().try_for_each(check_markers).map_err(at)?;
        }
        raw.fields
            .values()
            .try_for_each(check_markers)
            .map_err(at)?;

        Ok(Self {
            id: raw.id,
            input,
            result: raw.result,
            output,
            fields: raw.fields,
        })
    }
}

/// The input of a vector, in one of the five forms of `vectors/README.md`.
///
/// A value from the reader holds what its form stands for: the bytes of a
/// `base64` text, and the long text of a `repeat` list. Each reader below
/// thus has no error.
#[derive(Debug, Clone, PartialEq)]
pub enum Input {
    /// This text. For an entry point that takes bytes, its UTF-8 bytes.
    Text(String),
    /// The bytes of a `base64` input. The generator uses this form only for
    /// bytes that are not UTF-8.
    Base64(Vec<u8>),
    /// The long text of a `repeat` input: each text of the list, repeated
    /// its count of times, joined in order.
    Repeat(String),
    /// The named arguments of a builder. A value can be a marker object.
    Args(Map<String, Value>),
    /// The chunks of one byte stream, in order. Each chunk is its bytes.
    Chunks(Vec<Vec<u8>>),
}

impl Input {
    /// The input as text. `None` for a form that is not text.
    #[must_use]
    pub fn text(&self) -> Option<&str> {
        match self {
            Self::Text(text) | Self::Repeat(text) => Some(text),
            Self::Base64(_) | Self::Args(_) | Self::Chunks(_) => None,
        }
    }

    /// The input as bytes. The bytes of a text are its UTF-8 bytes. The
    /// bytes of a stream are its chunks, joined in order. `None` for the
    /// named arguments of a builder.
    #[must_use]
    pub fn bytes(&self) -> Option<Vec<u8>> {
        match self {
            Self::Text(text) | Self::Repeat(text) => Some(text.clone().into_bytes()),
            Self::Base64(bytes) => Some(bytes.clone()),
            Self::Chunks(chunks) => Some(chunks.concat()),
            Self::Args(_) => None,
        }
    }

    /// The named arguments of a builder. `None` for each other form.
    #[must_use]
    pub const fn args(&self) -> Option<&Map<String, Value>> {
        match self {
            Self::Args(arguments) => Some(arguments),
            Self::Text(_) | Self::Base64(_) | Self::Repeat(_) | Self::Chunks(_) => None,
        }
    }

    /// The chunks of a byte stream, in order. `None` for each other form.
    #[must_use]
    pub fn chunks(&self) -> Option<&[Vec<u8>]> {
        match self {
            Self::Chunks(chunks) => Some(chunks),
            Self::Text(_) | Self::Base64(_) | Self::Repeat(_) | Self::Args(_) => None,
        }
    }

    /// The input of a raw input. The error is the reason of a refusal.
    fn checked(raw: RawInput) -> Result<Self, String> {
        match raw {
            RawInput::Text(text) => Ok(Self::Text(text)),
            RawInput::Base64(encoded) => base64_decode(&encoded).map(Self::Base64),
            RawInput::Repeat(parts) => expand(&parts).map(Self::Repeat),
            RawInput::Args(arguments) => Ok(Self::Args(arguments)),
            RawInput::Chunks(chunks) => chunks
                .into_iter()
                .map(|chunk| match chunk {
                    RawChunk::Text(text) => Ok(text.into_bytes()),
                    RawChunk::Base64(encoded) => base64_decode(&encoded),
                })
                .collect::<Result<_, _>>()
                .map(Self::Chunks),
        }
    }
}

/// A value that has no JSON form that each strict reader accepts. The
/// generator writes it as an object with exactly one key, a marker object.
/// `vectors/README.md` lists the six markers.
///
/// The file format closes the set. A plain value of a vector is never an
/// object that holds one of the six keys and no other key.
#[derive(Debug, Clone, PartialEq)]
pub enum Marker {
    /// `$int`: an integer below -2^63 or above 2^64 - 1, as its decimal
    /// text.
    Int(String),
    /// `$float`: a float that is not finite.
    Float(NotFinite),
    /// `$utf16`: a string that holds a lone surrogate, as its UTF-16 code
    /// units.
    Utf16(Vec<u16>),
    /// `$base64`: bytes. The value holds the bytes and not their base64
    /// text.
    Base64(Vec<u8>),
    /// `$entries`: a mapping with a key that is not a plain string, as its
    /// pairs in the order of the mapping. A key and a value can be a marker
    /// object.
    Entries(Vec<(Value, Value)>),
    /// `$json`: a field that nests deeper than 96 levels, as its JSON text.
    /// The text can hold the other markers. Parse it with a reader that has
    /// no nesting limit.
    Json(String),
}

/// A float that is not finite.
///
/// The file format closes the set. The reader refuses another text.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum NotFinite {
    /// Not a number: `NaN`.
    Nan,
    /// Positive infinity: `Infinity`.
    Infinity,
    /// Negative infinity: `-Infinity`.
    NegativeInfinity,
}

/// Why a marker object is no marker: its content has a form that the file
/// format does not name.
///
/// ```
/// use creche_vectors::{Marker, MarkerError};
/// use serde_json::json;
///
/// let error: MarkerError = Marker::of(&json!({"$float": "1.5"})).unwrap_err();
///
/// assert_eq!(error.key(), "$float");
/// assert_eq!(error.reason(), "the text is not NaN, Infinity or -Infinity");
/// ```
///
/// Code outside this crate cannot build an error. Only [`Marker::of`] makes
/// one, for a marker object that it refused:
///
/// ```compile_fail,E0451
/// use creche_vectors::{Marker, MarkerError};
/// use serde_json::json;
///
/// let error = MarkerError {
///     key: String::from("$float"),
///     reason: String::new(),
/// };
/// ```
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct MarkerError {
    key: String,
    reason: String,
}

impl MarkerError {
    /// The key of the marker object, for example `$float`.
    #[must_use]
    pub fn key(&self) -> &str {
        &self.key
    }

    /// What is wrong with the content.
    #[must_use]
    pub fn reason(&self) -> &str {
        &self.reason
    }
}

impl fmt::Display for MarkerError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "the content of a {} marker: {}", self.key, self.reason)
    }
}

impl Error for MarkerError {}

impl Marker {
    /// The marker that `value` is. `None` for a plain value.
    ///
    /// A value of a file that [`surface`] gave has no error here: the reader
    /// refuses a file with a marker object that is no marker.
    ///
    /// ```
    /// use creche_vectors::{Marker, NotFinite};
    /// use serde_json::json;
    ///
    /// assert_eq!(Marker::of(&json!({"$float": "NaN"}))?, Some(Marker::Float(NotFinite::Nan)));
    /// assert_eq!(Marker::of(&json!({"$base64": "/w=="}))?, Some(Marker::Base64(vec![0xff])));
    /// assert_eq!(Marker::of(&json!({"float": "NaN"}))?, None);
    /// assert!(Marker::of(&json!({"$float": "1.5"})).is_err());
    /// # Ok::<(), creche_vectors::MarkerError>(())
    /// ```
    ///
    /// # Errors
    ///
    /// [`MarkerError`] for an object with one of the six keys and no other
    /// key, whose content has the wrong form.
    pub fn of(value: &Value) -> Result<Option<Self>, MarkerError> {
        let Some(object) = value.as_object() else {
            return Ok(None);
        };
        let mut members = object.iter();
        let (Some((key, content)), None) = (members.next(), members.next()) else {
            return Ok(None);
        };

        let marker = match key.as_str() {
            "$int" => integer_text(content).map(Self::Int),
            "$float" => not_finite(content).map(Self::Float),
            "$utf16" => code_units(content).map(Self::Utf16),
            "$base64" => text_of(content).and_then(base64_decode).map(Self::Base64),
            "$entries" => pairs(content).map(Self::Entries),
            "$json" => text_of(content).map(|text| Self::Json(text.to_owned())),
            _ => return Ok(None),
        };

        marker.map(Some).map_err(|reason| MarkerError {
            key: key.clone(),
            reason,
        })
    }

    /// The bytes of a `$base64` marker. `None` for each other marker.
    #[must_use]
    pub fn bytes(&self) -> Option<&[u8]> {
        match self {
            Self::Base64(bytes) => Some(bytes),
            Self::Int(_) | Self::Float(_) | Self::Utf16(_) | Self::Entries(_) | Self::Json(_) => {
                None
            }
        }
    }
}

/// The content of a marker as text.
fn text_of(content: &Value) -> Result<&str, String> {
    content
        .as_str()
        .ok_or_else(|| String::from("the content is no text"))
}

/// The decimal text of a `$int` marker: digits, with or without a minus
/// sign.
fn integer_text(content: &Value) -> Result<String, String> {
    let text = text_of(content)?;
    let digits = text.strip_prefix('-').unwrap_or(text);
    if digits.is_empty() || !digits.bytes().all(|byte| byte.is_ascii_digit()) {
        return Err(String::from("the text is no decimal integer"));
    }

    Ok(text.to_owned())
}

/// The float of a `$float` marker.
fn not_finite(content: &Value) -> Result<NotFinite, String> {
    match text_of(content)? {
        "NaN" => Ok(NotFinite::Nan),
        "Infinity" => Ok(NotFinite::Infinity),
        "-Infinity" => Ok(NotFinite::NegativeInfinity),
        _ => Err(String::from("the text is not NaN, Infinity or -Infinity")),
    }
}

/// The UTF-16 code units of a `$utf16` marker.
fn code_units(content: &Value) -> Result<Vec<u16>, String> {
    let no_units = || String::from("the content is no list of UTF-16 code units");

    content
        .as_array()
        .ok_or_else(no_units)?
        .iter()
        .map(|unit| {
            unit.as_u64()
                .and_then(|unit| u16::try_from(unit).ok())
                .ok_or_else(no_units)
        })
        .collect()
}

/// The pairs of a `$entries` marker.
fn pairs(content: &Value) -> Result<Vec<(Value, Value)>, String> {
    let no_pairs = || String::from("the content is no list of pairs");

    content
        .as_array()
        .ok_or_else(no_pairs)?
        .iter()
        .map(|pair| match pair.as_array().map(Vec::as_slice) {
            Some([key, value]) => Ok((key.clone(), value.clone())),
            _ => Err(no_pairs()),
        })
        .collect()
}

/// Reads each marker object in `value`, at each level. The error is the
/// first marker object that is no marker.
///
/// A file nests 100 levels at most (`vectors/README.md`), and `serde_json`
/// refuses a text past 128 levels. The walk thus has a small depth.
fn check_markers(value: &Value) -> Result<(), String> {
    Marker::of(value).map_err(|error| error.to_string())?;

    match value {
        Value::Array(items) => items.iter().try_for_each(check_markers),
        Value::Object(members) => members.values().try_for_each(check_markers),
        Value::Null | Value::Bool(_) | Value::Number(_) | Value::String(_) => Ok(()),
    }
}

/// A raw type that only a JSON object gives.
///
/// The derive of `serde` also reads a struct from an array that holds the
/// values of its fields in order. The format of each file names an object.
/// Each raw struct of this crate thus reads through this type: [`object_of`]
/// for the text of a file, and [`objects`] for a field.
struct ObjectOnly<T>(T);

impl<'de, T: Deserialize<'de>> Deserialize<'de> for ObjectOnly<T> {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        deserializer.deserialize_map(Members(PhantomData)).map(Self)
    }
}

/// The `serde` visitor of [`ObjectOnly`].
struct Members<T>(PhantomData<T>);

impl<'de, T: Deserialize<'de>> Visitor<'de> for Members<T> {
    type Value = T;

    fn expecting(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("a JSON object")
    }

    fn visit_map<A: MapAccess<'de>>(self, map: A) -> Result<T, A::Error> {
        T::deserialize(MapAccessDeserializer::new(map))
    }
}

/// The raw value of a file whose JSON text is `text`. The text is one JSON
/// object.
fn object_of<'de, T: Deserialize<'de>>(text: &'de str) -> Result<T, serde_json::Error> {
    serde_json::from_str::<ObjectOnly<T>>(text).map(|object| object.0)
}

/// Reads a field that is a list of JSON objects. A raw struct names this
/// function in `deserialize_with`.
fn objects<'de, D: Deserializer<'de>, T: Deserialize<'de>>(
    deserializer: D,
) -> Result<Vec<T>, D::Error> {
    let items = Vec::<ObjectOnly<T>>::deserialize(deserializer)?;

    Ok(items.into_iter().map(|object| object.0).collect())
}

/// The index file, as `serde` reads it. It checks no rule of the format.
/// `serde` refuses an object that holds one of these keys two times.
#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct RawIndex {
    format: u64,
    /// The frozen files. The key is required. An index with no frozen file
    /// holds an empty map.
    frozen: RawFrozen,
    kind: String,
    #[serde(deserialize_with = "objects")]
    surfaces: Vec<RawIndexRow>,
}

/// The map `frozen` of the index, as `serde` reads it: the path of each
/// frozen file under `vectors/data`, and the SHA-256 of its bytes.
///
/// A plain map of `serde` keeps the last value of a key that an object
/// holds two times. This type refuses such an object, as `serde` does for a
/// field of a struct. `_each_key_once` of `vectors/generate.py` holds the
/// same rule.
#[derive(Debug)]
struct RawFrozen(BTreeMap<String, String>);

impl<'de> Deserialize<'de> for RawFrozen {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        deserializer.deserialize_map(FrozenLines).map(Self)
    }
}

/// The `serde` visitor of [`RawFrozen`].
struct FrozenLines;

impl<'de> Visitor<'de> for FrozenLines {
    type Value = BTreeMap<String, String>;

    fn expecting(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("a map from the path of a frozen file to its digest")
    }

    fn visit_map<A: MapAccess<'de>>(self, mut map: A) -> Result<Self::Value, A::Error> {
        let mut lines = BTreeMap::new();
        while let Some((path, digest)) = map.next_entry::<String, String>()? {
            if lines.contains_key(&path) {
                return Err(de::Error::custom(format!(
                    "the map `frozen` holds the path {path:?} two times"
                )));
            }
            lines.insert(path, digest);
        }

        Ok(lines)
    }
}

/// One row of the index, as `serde` reads it.
#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct RawIndexRow {
    surface: String,
    path: String,
    entry: String,
    vectors: usize,
    accepted: usize,
    refused: usize,
    raised: usize,
}

/// One vector file, as `serde` reads it.
#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct RawSurface {
    format: u64,
    surface: String,
    entry: String,
    contract: String,
    notes: Vec<String>,
    context: Map<String, Value>,
    vectors: Vec<RawVector>,
}

/// One vector, as `serde` reads it. A struct with a flattened field reads
/// from a JSON object only, so it needs no [`ObjectOnly`].
#[derive(Debug, Deserialize)]
struct RawVector {
    id: String,
    input: RawInput,
    result: Outcome,
    /// Each other key of the vector: `value`, `refusal`, `params`, `issues`,
    /// `status`, `http_status`, `output`, `file`, `ts_bits` and `exception`.
    #[serde(flatten)]
    fields: Map<String, Value>,
}

/// The input of a vector, as `serde` reads it. An `input` object has exactly
/// one key.
#[derive(Debug, Deserialize)]
#[serde(rename_all = "lowercase")]
enum RawInput {
    Text(String),
    Base64(String),
    Repeat(Vec<(String, usize)>),
    Args(Map<String, Value>),
    Chunks(Vec<RawChunk>),
}

/// One chunk of a byte stream, as `serde` reads it.
#[derive(Debug, Deserialize)]
#[serde(rename_all = "lowercase")]
enum RawChunk {
    Text(String),
    Base64(String),
}

/// Each row of `vectors/data/index.json`, in the order of the file.
///
/// The function has no Python origin. `vectors/generate.py` writes the
/// index.
///
/// # Errors
///
/// [`VectorsError`] for an index that the reader cannot read, for a format
/// that is not 1, and for a map of the frozen files with a wrong form. The
/// doc comment of this crate lists each other case.
pub fn index() -> Result<Vec<IndexRow>, VectorsError> {
    index_of(&read_text(INDEX_FILE)?)
}

/// The vector file of the surface `name`. The index gives the path.
///
/// The function has no Python origin. `render` of `vectors/core.py` writes
/// the file.
///
/// # Errors
///
/// [`VectorsError`] for a surface that the index does not hold, for a format
/// that is not 1, and for a count of vectors that differs from the index.
/// The doc comment of this crate lists each other case.
pub fn surface(name: &str) -> Result<Surface, VectorsError> {
    let Some(row) = index()?.into_iter().find(|row| row.surface == name) else {
        return Err(VectorsError::new(
            INDEX_FILE,
            format!("the index has no surface {name}"),
        ));
    };

    surface_of(&row, &read_text(&row.path)?)
}

/// The rows of the index whose JSON text is `text`.
///
/// The index is a strict text. The raw types refuse each text that is not
/// strict, and the generator refuses it too, for example an index with two
/// `frozen` maps. The doc comment of this crate says how the raw types do
/// that with no strict reader.
// CONTRACT-QUESTION: `rust/AGENTS.md`, rule 13, gives each value one source.
// `creche-contracts` holds the strict JSON reader and `ids::Sha256Hex`, and
// rule 1 of `AGENTS.md` of this crate keeps that crate out of this one. The
// rules do not say what a crate below `creche-contracts` does. The reading
// here: the raw types of the index hold the rules of a strict text,
// `is_digest` holds the form of a digest, and `ObjectOnly` does what
// `slot::MapOnly` does. A change costs one helper in `creche-util` for the
// digest, and a `serde` dependency there for the adapter.
fn index_of(text: &str) -> Result<Vec<IndexRow>, VectorsError> {
    let refused = |reason: String| VectorsError::new(INDEX_FILE, reason);
    // The index holds no secret, and the message of `serde` names the field.
    let raw: RawIndex = object_of(text).map_err(|error| refused(error.to_string()))?;

    if raw.format != FORMAT {
        return Err(refused(wrong_format(raw.format)));
    }
    if raw.kind != INDEX_KIND {
        return Err(refused(format!(
            "the kind is {:?}, and the kind of the index is {INDEX_KIND:?}",
            raw.kind
        )));
    }
    for (path, digest) in &raw.frozen.0 {
        if !is_frozen_path(path) {
            return Err(refused(format!("{path:?} is no path of a frozen file")));
        }
        if !is_digest(digest) {
            return Err(refused(format!(
                "the digest of the frozen file {path} is not {DIGEST_LEN} hexadecimal digits in \
                 lower case"
            )));
        }
    }

    let rows: Vec<IndexRow> = raw
        .surfaces
        .into_iter()
        .map(IndexRow::checked)
        .collect::<Result<_, _>>()
        .map_err(refused)?;
    let mut names = HashSet::new();
    if let Some(twice) = rows.iter().find(|row| !names.insert(row.surface.as_str())) {
        return Err(refused(format!(
            "two rows have the surface {}",
            twice.surface
        )));
    }

    Ok(rows)
}

/// The vector file of the index row `row`, whose JSON text is `text`.
fn surface_of(row: &IndexRow, text: &str) -> Result<Surface, VectorsError> {
    let refused = |reason: String| VectorsError::new(&row.path, reason);
    let raw: RawSurface = object_of(text).map_err(|error| refused(error.to_string()))?;

    if raw.format != FORMAT {
        return Err(refused(wrong_format(raw.format)));
    }
    if raw.surface != row.surface {
        return Err(refused(format!(
            "the file holds the surface {}, and the index says {}",
            raw.surface, row.surface
        )));
    }
    if raw.entry != row.entry {
        return Err(refused(format!(
            "the file holds the entry point {:?}, and the index says {:?}",
            raw.entry, row.entry
        )));
    }
    if raw.vectors.len() != row.vectors {
        return Err(refused(format!(
            "the file holds {} vectors, and the index says {}",
            raw.vectors.len(),
            row.vectors
        )));
    }
    for outcome in Outcome::EACH {
        let found = raw
            .vectors
            .iter()
            .filter(|vector| vector.result == outcome)
            .count();
        if found != row.count(outcome) {
            return Err(refused(format!(
                "the file holds {found} {outcome} vectors, and the index says {}",
                row.count(outcome)
            )));
        }
    }

    let vectors: Vec<Vector> = raw
        .vectors
        .into_iter()
        .map(Vector::checked)
        .collect::<Result<_, _>>()
        .map_err(refused)?;
    let mut ids = HashSet::new();
    if let Some(twice) = vectors
        .iter()
        .find(|vector| !ids.insert(vector.id.as_str()))
    {
        return Err(refused(format!("two vectors have the id {}", twice.id)));
    }

    Ok(Surface {
        name: raw.surface,
        entry: raw.entry,
        contract: raw.contract,
        notes: raw.notes,
        context: raw.context,
        vectors,
    })
}

/// The reason for a format that the reader does not take.
fn wrong_format(format: u64) -> String {
    format!("the format is {format}, and the reader takes {FORMAT}")
}

/// The text of one file under `vectors/data`.
fn read_text(path: &str) -> Result<String, VectorsError> {
    fs::read_to_string(PathBuf::from(DATA_DIR).join(path))
        .map_err(|error| VectorsError::new(path, error.to_string()))
}

/// The text that a `repeat` input stands for.
fn expand(parts: &[(String, usize)]) -> Result<String, String> {
    let too_long = || format!("a repeat input stands for more than {REPEAT_MAX_BYTES} bytes");
    let mut total = 0_usize;
    for (text, count) in parts {
        total = text
            .len()
            .checked_mul(*count)
            .and_then(|bytes| total.checked_add(bytes))
            .filter(|total| *total <= REPEAT_MAX_BYTES)
            .ok_or_else(too_long)?;
    }

    Ok(parts
        .iter()
        .map(|(text, count)| text.repeat(*count))
        .collect())
}

/// The count of characters in one group of a base64 text.
const BASE64_GROUP: usize = 4;

/// The character that fills the last group of a base64 text.
const BASE64_PAD: u8 = b'=';

/// The bytes of a base64 text in the standard alphabet, with padding. The
/// error is the reason of a refusal.
fn base64_decode(encoded: &str) -> Result<Vec<u8>, String> {
    let no_base64 = || String::from("the text is no base64 with padding");
    let mut groups = encoded.as_bytes().chunks_exact(BASE64_GROUP);
    let mut decoded = Vec::new();

    while let Some(group) = groups.next() {
        let [first, second, third, fourth] = *group else {
            return Err(no_base64());
        };
        let last = groups.len() == 0;
        let (values, kept): ([u8; 4], usize) = match (third, fourth) {
            (BASE64_PAD, BASE64_PAD) if last => ([first, second, b'A', b'A'], 1),
            (_, BASE64_PAD) if last => ([first, second, third, b'A'], 2),
            _ => ([first, second, third, fourth], 3),
        };

        let mut word = 0_u32;
        for character in values {
            word = (word << 6) | base64_value(character).ok_or_else(no_base64)?;
        }
        let [_, bytes @ ..] = word.to_be_bytes();
        decoded.extend(bytes.into_iter().take(kept));
    }

    if !groups.remainder().is_empty() {
        return Err(no_base64());
    }

    Ok(decoded)
}

/// The value of one character of the standard base64 alphabet. `None` for
/// each other byte, and for the padding character.
fn base64_value(character: u8) -> Option<u32> {
    let value = match character {
        b'A'..=b'Z' => character - b'A',
        b'a'..=b'z' => character - b'a' + 26,
        b'0'..=b'9' => character - b'0' + 52,
        b'+' => 62,
        b'/' => 63,
        _ => return None,
    };

    Some(u32::from(value))
}

#[cfg(test)]
mod tests {
    use serde_json::json;

    use super::*;

    /// The index row of the surface `runtime.test` with the three counts.
    fn row(accepted: usize, refused: usize, raised: usize) -> IndexRow {
        IndexRow {
            surface: String::from("runtime.test"),
            path: String::from("runtime/test.json"),
            entry: String::from("package.entry"),
            vectors: accepted + refused + raised,
            accepted,
            refused,
            raised,
        }
    }

    /// The JSON text of a vector file with these vectors.
    fn file(vectors: &Value) -> String {
        json!({
            "format": 1,
            "surface": "runtime.test",
            "entry": "package.entry",
            "contract": "no contract",
            "notes": ["The input is one text."],
            "context": {"cap": 4},
            "vectors": vectors,
        })
        .to_string()
    }

    /// The JSON text of the file above with one member replaced.
    fn file_with(key: &str, member: Value) -> String {
        let mut file: Value = serde_json::from_str(&file(&two_vectors())).unwrap();
        file[key] = member;

        file.to_string()
    }

    fn two_vectors() -> Value {
        json!([
            {"id": "one", "input": {"text": "a"}, "result": "accepted", "value": "a"},
            {"id": "two", "input": {"text": ""}, "result": "refused", "refusal": "empty"},
        ])
    }

    /// The reason of the refusal of a file with these vectors.
    fn refused(row: &IndexRow, text: &str) -> String {
        let error = surface_of(row, text).unwrap_err();

        assert_eq!(error.file(), "runtime/test.json");

        error.reason().to_owned()
    }

    #[test]
    fn an_error_names_the_file_and_the_reason() {
        let error = VectorsError::new("index.json", "the format is 2, and the reader takes 1");

        assert_eq!(error.file(), "index.json");
        assert_eq!(error.reason(), "the format is 2, and the reader takes 1");
        assert_eq!(
            error.to_string(),
            "vectors/data/index.json: the format is 2, and the reader takes 1"
        );
    }

    #[test]
    fn base64_decodes_each_length_of_the_last_group() {
        assert_eq!(base64_decode("").unwrap(), b"");
        assert_eq!(base64_decode("Zg==").unwrap(), b"f");
        assert_eq!(base64_decode("Zm8=").unwrap(), b"fo");
        assert_eq!(base64_decode("Zm9v").unwrap(), b"foo");
        assert_eq!(base64_decode("Zm9vYg==").unwrap(), b"foob");
        assert_eq!(base64_decode("Zm9vYmE=").unwrap(), b"fooba");
        assert_eq!(base64_decode("Zm9vYmFy").unwrap(), b"foobar");
        assert_eq!(base64_decode("+/8=").unwrap(), [0xfb, 0xff]);
        assert_eq!(
            base64_decode("AAAA////").unwrap(),
            [0, 0, 0, 0xff, 0xff, 0xff]
        );
    }

    #[test]
    fn base64_refuses_a_text_that_is_no_base64_with_padding() {
        for text in [
            "Z",
            "Zg",
            "Zg=",
            "Zm8",
            "Zg==Zg==",
            "Zm8=Zm9v",
            "=g==",
            "Z===",
            "====",
            "Zm=v",
            "Zm9v\n",
            "Zm 9",
            "Zm9-",
            "Zm9_",
            "Zm9\u{e9}",
        ] {
            assert_eq!(
                base64_decode(text),
                Err(String::from("the text is no base64 with padding")),
                "{text:?}"
            );
        }
    }

    #[test]
    fn a_repeat_input_joins_each_text_count_times() {
        let input: RawInput =
            serde_json::from_value(json!({"repeat": [["[", 3], ["x", 0], ["]", 2]]})).unwrap();
        let input = Input::checked(input).unwrap();

        assert_eq!(input, Input::Repeat(String::from("[[[]]")));
        assert_eq!(input.text(), Some("[[[]]"));
        assert_eq!(input.bytes().unwrap(), b"[[[]]");
        assert_eq!(input.args(), None);
        assert_eq!(input.chunks(), None);
    }

    #[test]
    fn a_repeat_input_past_the_limit_is_refused_before_it_takes_memory() {
        let reason = "a repeat input stands for more than 67108864 bytes";

        for parts in [
            json!([["ab", REPEAT_MAX_BYTES / 2 + 1]]),
            json!([["a", REPEAT_MAX_BYTES], ["b", 1]]),
            json!([["ab", usize::MAX]]),
            json!([["a", usize::MAX], ["b", usize::MAX]]),
        ] {
            let input: RawInput = serde_json::from_value(json!({"repeat": parts})).unwrap();

            assert_eq!(Input::checked(input), Err(String::from(reason)), "{parts}");
        }

        assert!(expand(&[(String::from("a"), 1024), (String::new(), usize::MAX)]).is_ok());
    }

    #[test]
    fn the_chunks_of_a_stream_join_in_order() {
        let input: RawInput = serde_json::from_str(
            r#"{"chunks": [{"text": "ab"}, {"base64": "/w=="}, {"text": ""}]}"#,
        )
        .unwrap();
        let input = Input::checked(input).unwrap();

        assert_eq!(input.text(), None);
        assert_eq!(input.bytes().unwrap(), [b'a', b'b', 0xff]);
        assert_eq!(
            input.chunks().unwrap(),
            [b"ab".to_vec(), vec![0xff], Vec::new()]
        );
    }

    #[test]
    fn each_form_of_an_input_reads_through_its_readers() {
        let text = Input::Text(String::from("a\u{e9}"));
        let base64 = Input::checked(RawInput::Base64(String::from("/w=="))).unwrap();
        let args: RawInput = serde_json::from_value(json!({"args": {"family": "chat"}})).unwrap();
        let args = Input::checked(args).unwrap();

        assert_eq!(text.text(), Some("a\u{e9}"));
        assert_eq!(text.bytes().unwrap(), [b'a', 0xc3, 0xa9]);
        assert_eq!(base64, Input::Base64(vec![0xff]));
        assert_eq!(base64.text(), None);
        assert_eq!(base64.bytes().unwrap(), [0xff]);
        assert_eq!(args.args().unwrap()["family"], json!("chat"));
        assert_eq!(args.bytes(), None);
        assert_eq!(args.text(), None);
    }

    #[test]
    fn an_input_with_a_wrong_form_is_refused() {
        for input in [
            json!({"text": "a", "base64": "YQ=="}),
            json!({"words": "a"}),
            json!({}),
            json!("a"),
            json!({"text": 1}),
            json!({"repeat": [["a"]]}),
            json!({"repeat": [["a", -1]]}),
            json!({"chunks": [{"repeat": [["a", 1]]}]}),
        ] {
            assert!(
                serde_json::from_value::<RawInput>(input.clone()).is_err(),
                "{input}"
            );
        }

        let bad_base64: RawInput = serde_json::from_value(json!({"base64": "Zg"})).unwrap();
        let bad_chunk: RawInput =
            serde_json::from_value(json!({"chunks": [{"base64": "Zg"}]})).unwrap();

        assert!(Input::checked(bad_base64).is_err());
        assert!(Input::checked(bad_chunk).is_err());
    }

    /// A text in the form of a digest of the index.
    const DIGEST: &str = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef";

    #[test]
    fn the_index_reads_each_row() {
        let text = json!({
            "format": 1,
            "frozen": {"runtime/old.json": DIGEST, "old.json": DIGEST},
            "kind": "index",
            "surfaces": [{
                "surface": "runtime.test",
                "path": "runtime/test.json",
                "entry": "package.entry",
                "vectors": 6,
                "accepted": 1,
                "refused": 2,
                "raised": 3,
            }],
        })
        .to_string();
        let rows = index_of(&text).unwrap();

        assert_eq!(rows, [row(1, 2, 3)]);
        assert_eq!(rows[0].surface(), "runtime.test");
        assert_eq!(rows[0].path(), "runtime/test.json");
        assert_eq!(rows[0].entry(), "package.entry");
        assert_eq!(rows[0].vectors(), 6);
        assert_eq!(rows[0].count(Outcome::Accepted), 1);
        assert_eq!(rows[0].count(Outcome::Refused), 2);
        assert_eq!(rows[0].count(Outcome::Raised), 3);
    }

    #[test]
    fn an_index_with_no_surface_reads_as_no_row() {
        let rows =
            index_of(r#"{"format": 1, "frozen": {}, "kind": "index", "surfaces": []}"#).unwrap();

        assert_eq!(rows, []);
    }

    /// The digest of a frozen file is 64 hexadecimal digits in lower case.
    /// `vectors/tests/test_vectors_frozen.py` holds the same rule for the
    /// generator.
    #[test]
    fn a_frozen_digest_in_another_form_is_refused() {
        let refused = [
            String::new(),
            "0".repeat(63),
            "0".repeat(65),
            "g".repeat(64),
            DIGEST.to_uppercase(),
            format!("{}\n", "0".repeat(63)),
            format!(" {}", "0".repeat(63)),
            format!("{}g", "0".repeat(63)),
            format!("g{}", "0".repeat(63)),
            format!("{}`", "0".repeat(63)),
            format!("{}/", "0".repeat(63)),
            format!("{}:", "0".repeat(63)),
            // 64 bytes in UTF-8, and 32 characters.
            "\u{e9}".repeat(32),
            // 64 characters, and 128 bytes in UTF-8.
            "\u{e9}".repeat(64),
        ];

        for digest in refused {
            let text = index_with("frozen", json!({"runtime/old.json": digest}));
            let error = index_of(&text).unwrap_err();

            assert_eq!(error.file(), "index.json", "{digest:?}");
            assert_eq!(
                error.reason(),
                "the digest of the frozen file runtime/old.json is not 64 hexadecimal digits in \
                 lower case",
                "{digest:?}"
            );
        }

        for digest in [
            DIGEST.to_owned(),
            "0".repeat(64),
            "9".repeat(64),
            "a".repeat(64),
            "f".repeat(64),
        ] {
            let text = index_with("frozen", json!({"runtime/old.json": digest}));

            assert_eq!(index_of(&text).map(|rows| rows.len()), Ok(1), "{digest}");
        }
    }

    /// The start of the name of each surface of the id grammar of a SHA-256
    /// digest. Each surface is one Python copy of that grammar.
    const DIGEST_SURFACES: &str = "id.sha256_hex.";

    /// `is_digest` is a second Rust copy of the grammar of a SHA-256 digest:
    /// `ids::Sha256Hex` of `creche-contracts` holds the first one. The test
    /// of that type holds it equal to each vector of these surfaces. This
    /// test holds `is_digest` equal to the same vectors, so a change to one
    /// copy alone fails a test.
    #[test]
    fn the_form_of_a_digest_equals_each_vector_of_its_grammar() {
        let rows: Vec<IndexRow> = index()
            .unwrap()
            .into_iter()
            .filter(|row| row.surface().starts_with(DIGEST_SURFACES))
            .collect();

        assert!(
            !rows.is_empty(),
            "the index holds no surface of the grammar"
        );

        for row in rows {
            for vector in surface(row.surface()).unwrap().vectors() {
                let text = vector.input().text().unwrap();

                assert_eq!(
                    is_digest(text),
                    vector.result() == Outcome::Accepted,
                    "{} {}",
                    row.surface(),
                    vector.id()
                );
            }
        }
    }

    /// The JSON text of an index with one row, with one member of the index
    /// or of the row replaced.
    fn index_with(key: &str, member: Value) -> String {
        let mut index = json!({
            "format": 1,
            "frozen": {},
            "kind": "index",
            "surfaces": [{
                "surface": "runtime.test",
                "path": "runtime/test.json",
                "entry": "package.entry",
                "vectors": 2,
                "accepted": 1,
                "refused": 1,
                "raised": 0,
            }],
        });
        if index.get(key).is_some() {
            index[key] = member;
        } else {
            index["surfaces"][0][key] = member;
        }

        index.to_string()
    }

    /// The JSON text of an index with no surface, with `frozen` as the text
    /// of its map of frozen files, and of each other member that a test
    /// adds. The `json!` macro writes no key two times and no lone
    /// surrogate.
    fn index_text(frozen: &str) -> String {
        format!(r#"{{"format": 1, {frozen}, "kind": "index", "surfaces": []}}"#)
    }

    /// `vectors/tests/test_vectors_frozen.py` gives the generator the paths
    /// of this test and of the next one. The two programs must agree on each
    /// path.
    #[test]
    fn a_frozen_path_of_a_json_file_below_the_data_directory_reads() {
        for path in [
            "old.json",
            "runtime/old.json",
            "runtime/index.json",
            "runtime/a b.json",
            "runtime/.json",
            "runtime/..json",
        ] {
            let read = index_of(&index_with("frozen", json!({path: DIGEST})));

            assert_eq!(read.map(|rows| rows.len()), Ok(1), "{path:?}");
        }
    }

    #[test]
    fn a_frozen_path_in_another_form_is_refused() {
        for path in [
            "",
            ".",
            "..",
            "index.json",
            "/etc/hosts.json",
            "//old.json",
            "../../Cargo.json",
            "runtime/../old.json",
            "./old.json",
            "runtime/./old.json",
            "runtime//old.json",
            "old.json/",
            "old.json/.",
            "runtime/old.txt",
            "runtime/old.JSON",
            "runtime/old.json\n",
            "runtime/old.json ",
            "runtime",
            "json",
        ] {
            let error = index_of(&index_with("frozen", json!({path: DIGEST}))).unwrap_err();

            assert_eq!(error.file(), "index.json", "{path:?}");
            assert_eq!(
                error.reason(),
                format!("{path:?} is no path of a frozen file"),
                "{path:?}"
            );
        }
    }

    #[test]
    fn an_index_that_breaks_a_rule_is_refused() {
        let refused: [(String, &str); 36] = [
            (
                index_with("format", json!(2)),
                "the format is 2, and the reader takes 1",
            ),
            (index_with("format", json!(true)), "invalid type"),
            (index_with("format", json!(1.0)), "invalid type"),
            (
                index_with("kind", json!("registries")),
                "the kind is \"registries\", and the kind of the index is \"index\"",
            ),
            (
                index_with("path", json!("../../Cargo.toml")),
                "the path \"../../Cargo.toml\" of the surface runtime.test is not a path under vectors/data",
            ),
            (
                index_with("path", json!("/etc/hosts")),
                "the path \"/etc/hosts\" of the surface runtime.test is not a path under vectors/data",
            ),
            (
                index_with("path", json!("runtime/../../x.json")),
                "the path \"runtime/../../x.json\" of the surface runtime.test is not a path under vectors/data",
            ),
            (
                index_with("path", json!("")),
                "the path \"\" of the surface runtime.test is not a path under vectors/data",
            ),
            (
                index_with("vectors", json!(3)),
                "the three counts of the surface runtime.test do not add up to its 3 vectors",
            ),
            (
                index_with("raised", json!(usize::MAX)),
                "the three counts of the surface runtime.test do not add up to its 2 vectors",
            ),
            (
                index_with("surfaces", json!([index_row_json(), index_row_json()])),
                "two rows have the surface runtime.test",
            ),
            (index_with("extra", json!(1)), "unknown field `extra`"),
            (index_with("vectors", json!(-1)), "invalid value"),
            (String::from("{"), "EOF while parsing an object"),
            (
                String::from(r#"{"format": 1, "kind": "index", "surfaces": []}"#),
                "missing field `frozen`",
            ),
            (index_with("frozen", json!([])), "invalid type"),
            (index_with("frozen", json!(null)), "invalid type"),
            (
                index_with("frozen", json!({"runtime/old.json": 7})),
                "invalid type",
            ),
            // A key that an object holds two times. `serde` names the field
            // of a struct, and the raw type of the map names the path.
            (
                index_text(r#""frozen": {}, "frozen": {}"#),
                "duplicate field `frozen`",
            ),
            (
                index_text(&format!(
                    r#""frozen": {{"a.json": "{DIGEST}", "a.json": "{DIGEST}"}}"#
                )),
                "the map `frozen` holds the path \"a.json\" two times",
            ),
            (
                index_text(r#""frozen": {}, "format": 1"#),
                "duplicate field `format`",
            ),
            (
                index_text(r#""frozen": {}, "kind": "index""#),
                "duplicate field `kind`",
            ),
            (
                index_text(r#""frozen": {}, "surfaces": []"#),
                "duplicate field `surfaces`",
            ),
            (
                index_with("surfaces", json!([index_row_json()]))
                    .replace(r#""entry":"#, r#""raised":0,"entry":"#),
                "duplicate field `raised`",
            ),
            // A lone surrogate. `serde_json` has two texts for it.
            (
                index_text(&format!(r#""frozen": {{"\ud800.json": "{DIGEST}"}}"#)),
                "unexpected end of hex escape",
            ),
            (
                index_text(&format!(r#""frozen": {{"\udcff.json": "{DIGEST}"}}"#)),
                "lone leading surrogate in hex escape",
            ),
            (
                index_text(r#""frozen": {"a.json": "\udcff"}"#),
                "lone leading surrogate in hex escape",
            ),
            (
                index_text(r#""frozen": {}, "note\udcff": 1"#),
                "lone leading surrogate in hex escape",
            ),
            // A text that is no strict JSON for another cause.
            (
                format!("\u{feff}{}", index_text(r#""frozen": {}"#)),
                "expected value",
            ),
            (
                index_text(r#""frozen": {}"#).replace("1", "NaN"),
                "expected value",
            ),
            (
                index_text(r#""frozen": {}"#).replace("1", "1e999"),
                "number out of range",
            ),
            (
                index_text(r#""frozen": {}"#).replace("1", "18446744073709551616"),
                "invalid type",
            ),
            (
                format!("{} x", index_text(r#""frozen": {}"#)),
                "trailing characters",
            ),
            (
                index_text(r#""frozen": {}"#)
                    .replace("1", &format!("{}{}", "[".repeat(65), "]".repeat(65))),
                "invalid type",
            ),
            // An array in the place of an object, with the values of the
            // fields in order.
            (
                String::from(r#"[1, {}, "index", []]"#),
                "invalid type: sequence, expected a JSON object",
            ),
            (
                index_with(
                    "surfaces",
                    json!([[
                        "runtime.test",
                        "runtime/test.json",
                        "package.entry",
                        0,
                        0,
                        0,
                        0
                    ]]),
                ),
                "invalid type: sequence, expected a JSON object",
            ),
        ];

        for (text, reason) in refused {
            let error = index_of(&text).unwrap_err();

            assert_eq!(error.file(), "index.json", "{text}");
            assert!(
                error.reason().starts_with(reason),
                "{}: {text}",
                error.reason()
            );
        }
    }

    fn index_row_json() -> Value {
        json!({
            "surface": "runtime.test",
            "path": "runtime/test.json",
            "entry": "package.entry",
            "vectors": 0,
            "accepted": 0,
            "refused": 0,
            "raised": 0,
        })
    }

    #[test]
    fn a_path_under_the_data_directory_has_only_names() {
        for path in ["index.json", "ids/ulid.attendance.json", "a/b/c.json"] {
            assert!(is_data_path(path), "{path}");
        }

        for path in [
            "",
            ".",
            "..",
            "../a.json",
            "a/../b.json",
            "/a.json",
            "./a.json",
            "a//b.json",
            "a/./b.json",
            "a.json/",
        ] {
            assert!(!is_data_path(path), "{path}");
        }
    }

    #[test]
    fn a_file_that_agrees_with_its_index_row_reads() {
        let surface = surface_of(&row(1, 1, 0), &file(&two_vectors())).unwrap();
        let [one, two] = surface.vectors() else {
            panic!("the file holds two vectors");
        };

        assert_eq!(surface.name(), "runtime.test");
        assert_eq!(surface.entry(), "package.entry");
        assert_eq!(surface.contract(), "no contract");
        assert_eq!(surface.notes(), ["The input is one text."]);
        assert_eq!(surface.context()["cap"], json!(4));
        assert_eq!(surface.vector("two"), Some(two));
        assert_eq!(surface.vector("three"), None);

        assert_eq!(one.id(), "one");
        assert_eq!(one.input(), &Input::Text(String::from("a")));
        assert_eq!(one.result(), Outcome::Accepted);
        assert_eq!(one.value(), Some(&json!("a")));
        assert_eq!(one.refusal(), None);
        assert_eq!(one.params(), None);
        assert_eq!(one.output(), None);
        assert_eq!(two.result(), Outcome::Refused);
        assert_eq!(two.refusal(), Some(&json!("empty")));
        assert_eq!(two.field("refusal"), Some(&json!("empty")));
        assert_eq!(two.field("id"), None);
    }

    #[test]
    fn a_count_that_differs_from_the_index_stops_the_reader() {
        let text = file(&two_vectors());
        let counts = [
            (
                row(1, 2, 0),
                "the file holds 2 vectors, and the index says 3",
            ),
            (
                row(1, 0, 0),
                "the file holds 2 vectors, and the index says 1",
            ),
            (
                row(2, 0, 0),
                "the file holds 1 accepted vectors, and the index says 2",
            ),
            (
                row(0, 2, 0),
                "the file holds 1 accepted vectors, and the index says 0",
            ),
            (
                row(1, 0, 1),
                "the file holds 1 refused vectors, and the index says 0",
            ),
            (
                row(0, 0, 2),
                "the file holds 1 accepted vectors, and the index says 0",
            ),
        ];

        for (row, reason) in counts {
            assert_eq!(refused(&row, &text), reason);
        }

        let raised = file(&json!([
            {"id": "one", "input": {"text": "a"}, "result": "raised", "exception": "KeyError"},
        ]));

        assert_eq!(
            refused(&row(1, 0, 0), &raised),
            "the file holds 0 accepted vectors, and the index says 1"
        );
        assert_eq!(
            refused(&row(0, 1, 0), &raised),
            "the file holds 0 refused vectors, and the index says 1"
        );
        assert!(surface_of(&row(0, 0, 1), &raised).is_ok());
    }

    #[test]
    fn a_file_that_breaks_a_rule_is_refused() {
        let row = row(1, 1, 0);
        let same_id = file(&json!([
            {"id": "one", "input": {"text": "a"}, "result": "accepted"},
            {"id": "one", "input": {"text": ""}, "result": "refused"},
        ]));
        let rules: [(String, &str); 16] = [
            (
                file_with("format", json!(2)),
                "the format is 2, and the reader takes 1",
            ),
            (
                file_with("surface", json!("runtime.other")),
                "the file holds the surface runtime.other, and the index says runtime.test",
            ),
            (
                file_with("entry", json!("package.other")),
                "the file holds the entry point \"package.other\", and the index says \"package.entry\"",
            ),
            (same_id, "two vectors have the id one"),
            (file_with("extra", json!(1)), "unknown field `extra`"),
            (file_with("notes", json!("one note")), "invalid type"),
            (
                file(&json!([
                    {"id": "one", "input": {"text": "a"}, "result": "accepted"},
                    {"id": "two", "input": {"text": ""}, "result": "ignored"},
                ])),
                "unknown variant `ignored`",
            ),
            (
                file(&json!([
                    {"id": "one", "input": {"text": "a", "base64": "YQ=="}, "result": "accepted"},
                    {"id": "two", "input": {"text": ""}, "result": "refused"},
                ])),
                // `serde` names no form here. The reader refuses the file.
                "",
            ),
            (
                file(&json!([
                    {"id": "one", "input": {"base64": "YQ"}, "result": "accepted"},
                    {"id": "two", "input": {"text": ""}, "result": "refused"},
                ])),
                "the vector one: the text is no base64 with padding",
            ),
            (
                file(&json!([
                    {"id": "one", "input": {"text": "a"}, "result": "accepted", "output": "a"},
                    {"id": "two", "input": {"text": ""}, "result": "refused"},
                ])),
                "the vector one: the output is no input form: ",
            ),
            (
                file(&json!([
                    {"id": "one", "input": {"text": "a"}, "result": "accepted"},
                    {"id": "two", "input": {"text": ""}, "result": "refused", "refusal": {"at": [{"$float": "1.5"}]}},
                ])),
                "the vector two: the content of a $float marker: the text is not NaN, Infinity or -Infinity",
            ),
            (
                file(&json!([
                    {"id": "one", "input": {"args": {"n": {"$int": "0x10"}}}, "result": "accepted"},
                    {"id": "two", "input": {"text": ""}, "result": "refused"},
                ])),
                "the vector one: the content of a $int marker: the text is no decimal integer",
            ),
            // An array in the place of an object.
            (
                String::from("[]"),
                "invalid type: sequence, expected a JSON object",
            ),
            (
                String::from(r#"[1, "runtime.test", "package.entry", "no contract", [], {}, []]"#),
                "invalid type: sequence, expected a JSON object",
            ),
            (
                file(&json!([
                    ["one", {"text": "a"}, "accepted"],
                    {"id": "two", "input": {"text": ""}, "result": "refused"},
                ])),
                "invalid type: sequence, expected struct RawVector",
            ),
            (
                file(&two_vectors()).replace(r#""format":1"#, r#""format":1,"format":1"#),
                "duplicate field `format`",
            ),
        ];

        for (text, reason) in rules {
            let found = refused(&row, &text);

            assert!(found.starts_with(reason), "{found}: {text}");
        }
    }

    #[test]
    fn the_output_of_a_vector_is_an_input_form() {
        let text = file(&json!([
            {"id": "one", "input": {"args": {}}, "result": "accepted", "output": {"text": "{}\n"}},
            {"id": "two", "input": {"args": {}}, "result": "refused", "output": {"base64": "/w=="}},
        ]));
        let surface = surface_of(&row(1, 1, 0), &text).unwrap();
        let [one, two] = surface.vectors() else {
            panic!("the file holds two vectors");
        };

        assert_eq!(one.output(), Some(&Input::Text(String::from("{}\n"))));
        assert_eq!(one.field("output"), Some(&json!({"text": "{}\n"})));
        assert_eq!(two.output().unwrap().bytes().unwrap(), [0xff]);
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
                Marker::Int(String::from("18446744073709551616")),
            ),
            (
                json!({"$int": "-9223372036854775809"}),
                Marker::Int(String::from("-9223372036854775809")),
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
            (json!({"$utf16": []}), Marker::Utf16(Vec::new())),
            (json!({"$base64": "+/8="}), Marker::Base64(vec![0xfb, 0xff])),
            (
                json!({"$entries": [[1, "one"], [{"$int": "-1"}, null]]}),
                Marker::Entries(pairs),
            ),
            (json!({"$entries": []}), Marker::Entries(Vec::new())),
            (
                json!({"$json": "[[1]]"}),
                Marker::Json(String::from("[[1]]")),
            ),
        ];

        for (value, marker) in &read {
            assert_eq!(Marker::of(value).unwrap().as_ref(), Some(marker), "{value}");
        }

        assert_eq!(
            Marker::Base64(vec![0xfb, 0xff]).bytes(),
            Some([0xfb, 0xff].as_slice())
        );
        assert_eq!(Marker::Json(String::from("[[1]]")).bytes(), None);
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
            json!({"$float": "1.5", "unit": "ms"}),
        ] {
            assert_eq!(Marker::of(&value), Ok(None), "{value}");
        }
    }

    #[test]
    fn a_marker_object_with_a_wrong_content_is_an_error() {
        let wrong = [
            (json!({"$int": 7}), "$int", "the content is no text"),
            (
                json!({"$int": ""}),
                "$int",
                "the text is no decimal integer",
            ),
            (
                json!({"$int": "-"}),
                "$int",
                "the text is no decimal integer",
            ),
            (
                json!({"$int": "+1"}),
                "$int",
                "the text is no decimal integer",
            ),
            (
                json!({"$int": "1.0"}),
                "$int",
                "the text is no decimal integer",
            ),
            (
                json!({"$int": "\u{664}"}),
                "$int",
                "the text is no decimal integer",
            ),
            (
                json!({"$float": "1.5"}),
                "$float",
                "the text is not NaN, Infinity or -Infinity",
            ),
            (
                json!({"$float": "nan"}),
                "$float",
                "the text is not NaN, Infinity or -Infinity",
            ),
            (json!({"$float": null}), "$float", "the content is no text"),
            (
                json!({"$utf16": "a"}),
                "$utf16",
                "the content is no list of UTF-16 code units",
            ),
            (
                json!({"$utf16": [65536]}),
                "$utf16",
                "the content is no list of UTF-16 code units",
            ),
            (
                json!({"$utf16": [-1]}),
                "$utf16",
                "the content is no list of UTF-16 code units",
            ),
            (
                json!({"$utf16": ["a"]}),
                "$utf16",
                "the content is no list of UTF-16 code units",
            ),
            (
                json!({"$base64": "Zg"}),
                "$base64",
                "the text is no base64 with padding",
            ),
            (json!({"$base64": [1]}), "$base64", "the content is no text"),
            (
                json!({"$entries": {"a": 1}}),
                "$entries",
                "the content is no list of pairs",
            ),
            (
                json!({"$entries": [[1]]}),
                "$entries",
                "the content is no list of pairs",
            ),
            (
                json!({"$entries": [[1, 2, 3]]}),
                "$entries",
                "the content is no list of pairs",
            ),
            (json!({"$json": [1]}), "$json", "the content is no text"),
        ];

        for (value, key, reason) in wrong {
            let error = Marker::of(&value).unwrap_err();

            assert_eq!(error.key(), key, "{value}");
            assert_eq!(error.reason(), reason, "{value}");
            assert_eq!(
                error.to_string(),
                format!("the content of a {key} marker: {reason}")
            );
        }
    }

    #[test]
    fn the_name_of_a_result_is_its_word_in_a_file() {
        for outcome in Outcome::EACH {
            let read: Outcome = serde_json::from_value(json!(outcome.as_str())).unwrap();

            assert_eq!(read, outcome);
            assert_eq!(outcome.to_string(), outcome.as_str());
        }

        assert!(serde_json::from_value::<Outcome>(json!("Accepted")).is_err());
    }

    #[test]
    fn a_surface_that_the_index_does_not_hold_is_an_error() {
        let error = surface("runtime.no_such_surface").unwrap_err();

        assert_eq!(
            error.to_string(),
            "vectors/data/index.json: the index has no surface runtime.no_such_surface"
        );
    }

    #[test]
    fn a_file_that_is_absent_is_an_error_that_names_the_file() {
        let error = read_text("runtime/no-such-file.json").unwrap_err();

        assert_eq!(error.file(), "runtime/no-such-file.json");
        assert!(!error.reason().is_empty());
    }

    /// The walk of the proof of this reader: each surface of the index
    /// reads, so each count of each file is the count of its index row.
    #[test]
    fn each_surface_of_the_index_reads() {
        let rows = index().unwrap();

        assert!(!rows.is_empty(), "the index holds no surface");

        for row in &rows {
            let surface = surface(row.surface()).unwrap();

            assert_eq!(surface.name(), row.surface());
            assert_eq!(surface.entry(), row.entry());
            assert_eq!(surface.vectors().len(), row.vectors(), "{}", row.path());
            assert!(
                !surface.contract().is_empty(),
                "{}: no contract",
                row.path()
            );
            assert!(
                surface.notes().iter().all(|note| !note.is_empty()),
                "{}",
                row.path()
            );
            assert!(
                surface.context().keys().all(|key| !key.is_empty()),
                "{}",
                row.path()
            );
        }
    }

    #[test]
    fn each_input_of_each_file_has_a_form_that_this_module_reads() {
        for row in index().unwrap() {
            for vector in surface(row.surface()).unwrap().vectors() {
                let input = vector.input();
                let read = match input {
                    Input::Text(_) | Input::Repeat(_) => input.text().is_some(),
                    // The generator writes base64 only for bytes that are not UTF-8.
                    Input::Base64(bytes) => std::str::from_utf8(bytes).is_err(),
                    Input::Chunks(_) => input.bytes().is_some() && input.chunks().is_some(),
                    // A builder with no argument has an empty object.
                    Input::Args(_) => input.args().is_some() && input.bytes().is_none(),
                };

                assert!(read, "{} {}", row.surface(), vector.id());
            }
        }
    }

    /// The count of the marker objects in `value`, at each level.
    fn markers_in(value: &Value) -> usize {
        let here = usize::from(Marker::of(value).unwrap().is_some());
        let below: usize = match value {
            Value::Array(items) => items.iter().map(markers_in).sum(),
            Value::Object(members) => members.values().map(markers_in).sum(),
            Value::Null | Value::Bool(_) | Value::Number(_) | Value::String(_) => 0,
        };

        here + below
    }

    #[test]
    fn each_marker_of_each_file_reads() {
        let nested = json!({"value": [{"$int": "-1"}, {"$entries": [[{"$float": "NaN"}, 1]]}]});
        let found: usize = index()
            .unwrap()
            .iter()
            .map(|row| {
                let file: Value = serde_json::from_str(&read_text(row.path()).unwrap()).unwrap();

                markers_in(&file)
            })
            .sum();

        assert_eq!(markers_in(&nested), 3);
        assert!(check_markers(&nested).is_ok());
        assert!(found > 0, "no file holds a marker object");
    }
}
