//! The reader of `vectors/data/ids/disagreements.json`.
//!
//! The Python code holds more than one copy of most id grammars. The file
//! lists each input on which two copies of one grammar give different
//! results. `rust/AGENTS.md`, "When two Python copies of a grammar disagree",
//! says what a Rust type does with such an input.
//!
//! The reader refuses the file in each of these cases:
//!
//! 1. The format is not 1, or the kind is not `disagreements`.
//! 2. The file or a row holds a key that the format does not name, or lacks
//!    a required one.
//! 3. A row has an empty grammar or an empty id.
//! 4. The input of a row is not a text.
//! 5. A row names a surface that the index does not hold.
//! 6. A row names the surfaces of less than two results. Such a row is no
//!    disagreement.
//!
//! The reader has no Python origin. `render_disagreements` of
//! `vectors/surfaces/ids.py` writes the file.

use std::collections::HashSet;

use serde::Deserialize;

use super::{
    FORMAT, Input, Outcome, RawInput, VectorsError, object, object_of, objects, read_text,
    wrong_format,
};

/// The file, relative to `vectors/data`.
const DISAGREEMENTS_FILE: &str = "ids/disagreements.json";

/// The `kind` of the file.
const DISAGREEMENTS_KIND: &str = "disagreements";

/// The least count of results in one row. `disagreements` of
/// `vectors/surfaces/ids.py` writes no row for an input with one result.
const RESULTS_MIN: usize = 2;

/// One input on which two Python copies of one id grammar give different
/// results. A row thus names the surfaces of two results or more.
///
/// ```
/// use creche_vectors::{Disagreement, Outcome};
///
/// let rows: Vec<Disagreement> = creche_vectors::disagreements()?;
/// let index = creche_vectors::index()?;
///
/// for row in &rows {
///     let named = row.surfaces(Outcome::Accepted).len()
///         + row.surfaces(Outcome::Refused).len()
///         + row.surfaces(Outcome::Raised).len();
///
///     // Two results or more, so two surfaces or more.
///     assert!(named >= 2, "{} {}: {:?}", row.grammar(), row.id(), row.text());
///     for surface in row.surfaces(Outcome::Refused) {
///         assert!(index.iter().any(|one| one.surface() == surface));
///     }
/// }
/// # Ok::<(), Box<dyn std::error::Error>>(())
/// ```
///
/// Code outside this crate cannot build a row. Each row comes from the file,
/// and each surface that it names is a surface of the index:
///
/// ```compile_fail,E0451
/// use creche_vectors::{Disagreement, Outcome};
///
/// let row = Disagreement {
///     grammar: String::from("tool_name"),
///     id: String::from("len-65"),
///     text: String::new(),
///     accepted: Vec::new(),
///     refused: Vec::new(),
///     raised: Vec::new(),
/// };
/// ```
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Disagreement {
    grammar: String,
    id: String,
    text: String,
    accepted: Vec<String>,
    refused: Vec<String>,
    raised: Vec<String>,
}

impl Disagreement {
    /// The name of the grammar, for example `tool_name`.
    #[must_use]
    pub fn grammar(&self) -> &str {
        &self.grammar
    }

    /// The id of the vector in each file of the grammar.
    #[must_use]
    pub fn id(&self) -> &str {
        &self.id
    }

    /// The input. The input of an id grammar is one text.
    #[must_use]
    pub fn text(&self) -> &str {
        &self.text
    }

    /// The surfaces whose Python copy gave the result `outcome`, in the
    /// order of the file.
    #[must_use]
    pub fn surfaces(&self, outcome: Outcome) -> &[String] {
        match outcome {
            Outcome::Accepted => &self.accepted,
            Outcome::Refused => &self.refused,
            Outcome::Raised => &self.raised,
        }
    }

    /// The row of a raw row. `known` holds each surface of the index. The
    /// error is the reason of a refusal.
    fn checked(raw: RawDisagreement, known: &HashSet<&str>) -> Result<Self, String> {
        if raw.grammar.is_empty() || raw.id.is_empty() {
            return Err(format!(
                "the row {:?} {:?} has an empty grammar or an empty id",
                raw.grammar, raw.id
            ));
        }

        let at = |reason: String| format!("the row {} {}: {reason}", raw.grammar, raw.id);
        let text = match Input::checked(raw.input).map_err(at)? {
            Input::Text(text) | Input::Repeat(text) => text,
            Input::Base64(_) | Input::Args(_) | Input::Chunks(_) => {
                return Err(at(String::from("the input is not a text")));
            }
        };
        let row = Self {
            grammar: raw.grammar,
            id: raw.id,
            text,
            accepted: raw.results.accepted,
            refused: raw.results.refused,
            raised: raw.results.raised,
        };

        let mut named = Outcome::EACH
            .into_iter()
            .flat_map(|outcome| row.surfaces(outcome));
        if let Some(unknown) = named.find(|surface| !known.contains(surface.as_str())) {
            return Err(format!(
                "the row {} {}: the index has no surface {unknown}",
                row.grammar, row.id
            ));
        }

        let results = Outcome::EACH
            .into_iter()
            .filter(|outcome| !row.surfaces(*outcome).is_empty())
            .count();
        if results < RESULTS_MIN {
            return Err(format!(
                "the row {} {} is no disagreement: its surfaces have less than {RESULTS_MIN} \
                 results",
                row.grammar, row.id
            ));
        }

        Ok(row)
    }
}

/// The file, as `serde` reads it. It checks no rule.
#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct RawDisagreements {
    format: u64,
    kind: String,
    #[serde(deserialize_with = "objects")]
    rows: Vec<RawDisagreement>,
}

/// One row of the file, as `serde` reads it.
#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct RawDisagreement {
    grammar: String,
    id: String,
    input: RawInput,
    #[serde(deserialize_with = "object")]
    results: RawResults,
}

/// The surfaces of one row by their result, as `serde` reads them. The
/// generator writes a key only for a result that a copy gave.
#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct RawResults {
    #[serde(default)]
    accepted: Vec<String>,
    #[serde(default)]
    refused: Vec<String>,
    #[serde(default)]
    raised: Vec<String>,
}

/// Each row of `vectors/data/ids/disagreements.json`, in the order of the
/// file.
///
/// The function has no Python origin. `render_disagreements` of
/// `vectors/surfaces/ids.py` writes the file.
///
/// # Errors
///
/// [`VectorsError`] for a file that the reader cannot read, for a format
/// that is not 1 and for a row that names a surface outside the index. The
/// error names the index when the reader refuses the index. The doc comment
/// of this module lists each other case.
pub fn disagreements() -> Result<Vec<Disagreement>, VectorsError> {
    let index = super::index()?;
    let known = index.iter().map(|row| row.surface()).collect();

    disagreements_of(&read_text(DISAGREEMENTS_FILE)?, &known)
}

/// The rows of the file whose JSON text is `text`. `known` holds each
/// surface of the index.
fn disagreements_of(text: &str, known: &HashSet<&str>) -> Result<Vec<Disagreement>, VectorsError> {
    let refused = |reason: String| VectorsError::new(DISAGREEMENTS_FILE, reason);
    let raw: RawDisagreements = object_of(text).map_err(|error| refused(error.to_string()))?;

    if raw.format != FORMAT {
        return Err(refused(wrong_format(raw.format)));
    }
    if raw.kind != DISAGREEMENTS_KIND {
        return Err(refused(format!(
            "the kind is {:?}, and the kind of the file is {DISAGREEMENTS_KIND:?}",
            raw.kind
        )));
    }

    raw.rows
        .into_iter()
        .map(|row| Disagreement::checked(row, known))
        .collect::<Result<_, _>>()
        .map_err(refused)
}

#[cfg(test)]
mod tests {
    use serde_json::{Value, json};

    use super::*;

    /// The surfaces of the index of these tests.
    fn known() -> HashSet<&'static str> {
        HashSet::from(["id.test.one", "id.test.two", "id.test.three"])
    }

    fn row_json() -> Value {
        json!({
            "grammar": "test",
            "id": "len-65",
            "input": {"text": "aaa"},
            "results": {"accepted": ["id.test.one", "id.test.three"], "refused": ["id.test.two"]},
        })
    }

    /// The JSON text of a file with one row, with one member of the file or
    /// of the row replaced.
    fn file_with(key: &str, member: Value) -> String {
        let mut file = json!({"format": 1, "kind": "disagreements", "rows": [row_json()]});
        if file.get(key).is_some() {
            file[key] = member;
        } else {
            file["rows"][0][key] = member;
        }

        file.to_string()
    }

    #[test]
    fn a_file_in_the_form_of_the_generator_reads() {
        let text = json!({"format": 1, "kind": "disagreements", "rows": [row_json()]}).to_string();
        let rows = disagreements_of(&text, &known()).unwrap();
        let [row] = rows.as_slice() else {
            panic!("the file holds one row");
        };

        assert_eq!(row.grammar(), "test");
        assert_eq!(row.id(), "len-65");
        assert_eq!(row.text(), "aaa");
        assert_eq!(
            row.surfaces(Outcome::Accepted),
            ["id.test.one", "id.test.three"]
        );
        assert_eq!(row.surfaces(Outcome::Refused), ["id.test.two"]);
        assert!(row.surfaces(Outcome::Raised).is_empty());
    }

    #[test]
    fn a_file_with_no_row_and_a_long_text_read() {
        let none = r#"{"format": 1, "kind": "disagreements", "rows": []}"#;
        let long = file_with("input", json!({"repeat": [["ab", 2], ["c", 1]]}));
        let raised = file_with(
            "results",
            json!({"raised": ["id.test.one"], "refused": ["id.test.two"]}),
        );

        assert_eq!(disagreements_of(none, &known()), Ok(Vec::new()));
        assert_eq!(
            disagreements_of(&long, &known()).unwrap()[0].text(),
            "ababc"
        );
        assert_eq!(
            disagreements_of(&raised, &known()).unwrap()[0].surfaces(Outcome::Raised),
            ["id.test.one"]
        );
    }

    #[test]
    fn a_file_that_breaks_a_rule_is_refused() {
        let refused: [(String, &str); 23] = [
            (
                file_with("format", json!(2)),
                "the format is 2, and the reader takes 1",
            ),
            (file_with("format", json!(true)), "invalid type"),
            (
                file_with("kind", json!("index")),
                "the kind is \"index\", and the kind of the file is \"disagreements\"",
            ),
            (file_with("extra", json!(1)), "unknown field `extra`"),
            (
                String::from(r#"{"format": 1, "rows": []}"#),
                "missing field `kind`",
            ),
            (
                String::from(
                    r#"{"format": 1, "kind": "disagreements", "kind": "disagreements", "rows": []}"#,
                ),
                "duplicate field `kind`",
            ),
            // An array in the place of an object.
            (
                String::from(r#"[1, "disagreements", []]"#),
                "invalid type: sequence, expected a JSON object",
            ),
            (
                file_with("rows", json!([["test", "len-65", {"text": "aaa"}, {}]])),
                "invalid type: sequence, expected a JSON object",
            ),
            (
                file_with("results", json!([["id.test.one"], ["id.test.two"], []])),
                "invalid type: sequence, expected a JSON object",
            ),
            (
                file_with("grammar", json!("")),
                "the row \"\" \"len-65\" has an empty grammar or an empty id",
            ),
            (
                file_with("id", json!("")),
                "the row \"test\" \"\" has an empty grammar or an empty id",
            ),
            (
                file_with("input", json!({"base64": "/w=="})),
                "the row test len-65: the input is not a text",
            ),
            (
                file_with("input", json!({"args": {}})),
                "the row test len-65: the input is not a text",
            ),
            (
                file_with("input", json!({"chunks": [{"text": "a"}]})),
                "the row test len-65: the input is not a text",
            ),
            (
                file_with("input", json!({"base64": "Zg"})),
                "the row test len-65: the text is no base64 with padding",
            ),
            (file_with("input", json!("aaa")), "unknown variant `aaa`"),
            (
                file_with("input", json!({"text": "aaa", "base64": "YWFh"})),
                // `serde` names no form here. The reader refuses the file.
                "",
            ),
            (
                file_with(
                    "results",
                    json!({"accepted": ["id.test.one"], "ignored": []}),
                ),
                "unknown field `ignored`",
            ),
            (
                file_with("results", json!({"accepted": ["id.test.four"]})),
                "the row test len-65: the index has no surface id.test.four",
            ),
            (
                file_with(
                    "results",
                    json!({"accepted": ["id.test.one"], "raised": ["id.test"]}),
                ),
                "the row test len-65: the index has no surface id.test",
            ),
            (
                file_with(
                    "results",
                    json!({"accepted": ["id.test.one", "id.test.two"]}),
                ),
                "the row test len-65 is no disagreement: its surfaces have less than 2 results",
            ),
            (
                file_with(
                    "results",
                    json!({"accepted": ["id.test.one"], "refused": []}),
                ),
                "the row test len-65 is no disagreement: its surfaces have less than 2 results",
            ),
            (
                file_with("results", json!({})),
                "the row test len-65 is no disagreement: its surfaces have less than 2 results",
            ),
        ];

        for (text, reason) in refused {
            let error = disagreements_of(&text, &known()).unwrap_err();

            assert_eq!(error.file(), "ids/disagreements.json", "{text}");
            assert!(
                error.reason().starts_with(reason),
                "{}: {text}",
                error.reason()
            );
        }
    }

    /// The walk of the committed file. The test demands no row: the file is
    /// empty when each copy of each grammar gives the same results.
    #[test]
    fn each_row_of_the_committed_file_names_surfaces_of_the_index() {
        let index = crate::index().unwrap();

        for row in disagreements().unwrap() {
            for surface in Outcome::EACH
                .into_iter()
                .flat_map(|outcome| row.surfaces(outcome))
            {
                assert!(
                    index.iter().any(|one| one.surface() == surface),
                    "{} {}: {surface}",
                    row.grammar(),
                    row.id()
                );
            }
        }
    }
}
