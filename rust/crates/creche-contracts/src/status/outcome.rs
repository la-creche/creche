//! The outcome record, as the noticeboard reads it (contract 05 §8).
//!
//! `attendance` writes one record for each job of an autonomous family.
//! Contract 02 §13.1 owns the record and its writer. This module holds the
//! view of the one reader that contract 05 names.

use super::json::{ByteOrderMark, Integer, Object};
use super::raw::{ReadError, block, integer, number, read_object, text};
use super::views::TEXT_CHARS_MAX;

/// The largest outcome record that the noticeboard parses: 1 MiB.
pub const OUTCOME_CAP_BYTES: usize = 1 << 20;

/// One outcome record on the noticeboard:
/// `noticeboard.statusdocs.read_outcomes`.
///
/// The noticeboard checks no field. A field that is missing or of another
/// JSON type reads as empty.
#[derive(Debug, Clone, PartialEq)]
pub struct OutcomeRow {
    /// The `id` text. The stem of the file name when the record has none.
    pub id: String,
    /// The `family` text.
    pub family: String,
    /// The `session` text.
    pub session: String,
    /// The kind and the name of the trigger, with one space between them.
    /// One of the two when the record has only one.
    pub trigger: String,
    /// The `started_at` text.
    pub started_at: String,
    /// The `ended_at` text.
    pub ended_at: String,
    /// The `status` text.
    pub status: String,
    /// The `error` text: 500 characters at most.
    pub error: String,
    /// The count of turns: each integer. 0 when the record has none.
    pub turns: Integer,
    /// The spend. `None` when the field is not a number. The number can be
    /// `NaN` or an infinity.
    pub spend_usd: Option<f64>,
    /// The `sandbox` text.
    pub sandbox: String,
    /// Why the noticeboard cannot read the file. `None` for a file that is
    /// one JSON object.
    pub problem: Option<ReadError>,
}

/// A text field of the record, as the noticeboard shows it.
fn shown(object: &Object, key: &str) -> String {
    text(object, key)
        .text()
        .chars()
        .take(TEXT_CHARS_MAX)
        .collect()
}

/// The view of the noticeboard: the bytes of one outcome record. `stem` is
/// the name of the file with no `.json`. The noticeboard makes a row for each
/// file.
#[must_use]
pub fn noticeboard(bytes: &[u8], stem: &str) -> OutcomeRow {
    let object = match read_object(bytes, OUTCOME_CAP_BYTES, ByteOrderMark::Skip) {
        Ok(object) => object,
        Err(problem) => return unreadable(stem, problem),
    };
    let trigger = block(&object, "trigger", |trigger| {
        let kind = shown(trigger, "kind");
        let name = shown(trigger, "name");
        if kind.is_empty() || name.is_empty() {
            return kind + &name;
        }

        format!("{kind} {name}")
    });
    let id = shown(&object, "id");

    OutcomeRow {
        id: if id.is_empty() { stem.to_owned() } else { id },
        family: shown(&object, "family"),
        session: shown(&object, "session"),
        trigger: trigger.text().to_owned(),
        started_at: shown(&object, "started_at"),
        ended_at: shown(&object, "ended_at"),
        status: shown(&object, "status"),
        error: shown(&object, "error"),
        turns: integer(&object, "turns")
            .value()
            .cloned()
            .unwrap_or(Integer::from(0)),
        spend_usd: number(&object, "spend_usd")
            .value()
            .and_then(|number| number.to_f64()),
        sandbox: shown(&object, "sandbox"),
        problem: None,
    }
}

fn unreadable(stem: &str, problem: ReadError) -> OutcomeRow {
    OutcomeRow {
        id: stem.to_owned(),
        family: String::new(),
        session: String::new(),
        trigger: String::new(),
        started_at: String::new(),
        ended_at: String::new(),
        status: String::new(),
        error: String::new(),
        turns: Integer::from(0),
        spend_usd: None,
        sandbox: String::new(),
        problem: Some(problem),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::status::json::{JsonError, JsonKind};

    const STEM: &str = "01JBQ7WZ0X4T9V6K2H8M3N5PQR";

    #[test]
    fn a_record_reads_as_its_texts_and_its_numbers() {
        let row = noticeboard(
            br#"{"id": "A", "family": "chat", "turns": 3, "spend_usd": 2,
                "trigger": {"kind": "timer", "name": "morning"}, "error": null}"#,
            STEM,
        );

        assert_eq!(row.id, "A");
        assert_eq!(row.family, "chat");
        assert_eq!(row.trigger, "timer morning");
        assert_eq!(row.turns, Integer::from(3));
        assert_eq!(row.spend_usd, Some(2.0));
        assert_eq!(row.error, "");
        assert_eq!(row.problem, None);
    }

    #[test]
    fn a_record_with_no_id_takes_the_stem_of_its_file() {
        assert_eq!(noticeboard(b"{}", STEM).id, STEM);
        assert_eq!(noticeboard(br#"{"id": ""}"#, STEM).id, STEM);
        assert_eq!(noticeboard(br#"{"id": 5}"#, STEM).id, STEM);
    }

    #[test]
    fn a_trigger_with_one_part_shows_that_part() {
        let trigger = |text: &str| noticeboard(text.as_bytes(), STEM).trigger;

        assert_eq!(trigger(r#"{"trigger": {"kind": "timer"}}"#), "timer");
        assert_eq!(trigger(r#"{"trigger": {"name": "morning"}}"#), "morning");
        assert_eq!(trigger(r#"{"trigger": {}}"#), "");
        assert_eq!(trigger(r#"{"trigger": null}"#), "");
        assert_eq!(trigger(r#"{"trigger": ["timer"]}"#), "");
    }

    #[test]
    fn a_spend_past_the_range_of_a_float_reads_as_no_number() {
        let text = format!(r#"{{"spend_usd": 1{}}}"#, "0".repeat(400));

        assert_eq!(noticeboard(text.as_bytes(), STEM).spend_usd, None);
    }

    #[test]
    fn a_file_that_is_not_an_object_gives_a_row_with_a_problem() {
        let problems = [
            (&b""[..], ReadError::NotJson(JsonError::Syntax { at: 0 })),
            (b"[]", ReadError::NotAnObject(JsonKind::Array)),
            (b"\"\xff\"", ReadError::NotJson(JsonError::NotUtf8)),
        ];

        for (bytes, problem) in problems {
            let row = noticeboard(bytes, STEM);

            assert_eq!(row.problem, Some(problem));
            assert_eq!(row.id, STEM);
        }
    }

    #[test]
    fn a_file_of_exactly_the_cap_is_read() {
        // The test writes the cap a second time, as a number. A constant
        // that moves then fails the test.
        let cap = 1 << 20;
        let padding = cap - r#"{"x": ""}"#.len();
        let at_cap = format!(r#"{{"x": "{}"}}"#, "a".repeat(padding));
        let over_cap = format!(r#"{{"x": "{}"}}"#, "a".repeat(padding + 1));

        assert_eq!(at_cap.len(), cap);
        assert_eq!(noticeboard(at_cap.as_bytes(), STEM).problem, None);
        assert_eq!(
            noticeboard(over_cap.as_bytes(), STEM).problem,
            Some(ReadError::TooLarge { cap })
        );
    }
}
