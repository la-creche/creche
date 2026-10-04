//! The JSON text that the Python program writes: `json.dumps(value, indent=2)`.
//!
//! The Rust program writes the same bytes. Python writes each character
//! outside printable ASCII as an escape, and puts each item on its own line.

use std::fmt::Write;

use creche_contracts::family::Issue;

use crate::report::{Applied, Report};

/// The indentation of one level.
const INDENT: &str = "  ";

/// A JSON string, as Python writes it with `ensure_ascii`.
fn string(text: &str) -> String {
    let mut out = String::with_capacity(text.len() + 2);
    out.push('"');
    for c in text.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{8}' => out.push_str("\\b"),
            '\u{c}' => out.push_str("\\f"),
            ' '..='~' => out.push(c),
            _ => {
                let mut units = [0_u16; 2];
                for unit in c.encode_utf16(&mut units) {
                    // A write to a `String` cannot fail.
                    let _ = write!(out, "\\u{unit:04x}");
                }
            }
        }
    }

    out.push('"');
    out
}

fn issue(issue: &Issue, indent: &str) -> String {
    let inner = format!("{indent}{INDENT}");

    format!(
        "{indent}{{\n{inner}\"severity\": {},\n{inner}\"loc\": {},\n{inner}\"msg\": \
         {},\n{inner}\"downgraded\": {}\n{indent}}}",
        string(issue.severity.as_str()),
        string(&issue.loc),
        string(&issue.msg),
        issue.downgraded
    )
}

/// A list with one item on each line, or `[]`.
fn list(items: &[String], indent: &str) -> String {
    if items.is_empty() {
        return "[]".to_owned();
    }

    format!("[\n{}\n{indent}]", items.join(",\n"))
}

fn report(report: &Report, indent: &str) -> String {
    let inner = format!("{indent}{INDENT}");
    let item_indent = format!("{inner}{INDENT}");
    let issues: Vec<String> = report
        .issues()
        .iter()
        .map(|one| issue(one, &item_indent))
        .collect();

    format!(
        "{indent}{{\n{inner}\"family\": {},\n{inner}\"file\": {},\n{inner}\"status\": \
         {},\n{inner}\"applied\": {},\n{inner}\"issues\": {},\n{inner}\"errors\": \
         {},\n{inner}\"warnings\": {}\n{indent}}}",
        string(report.family()),
        string(report.file()),
        string(report.state().as_str()),
        report.applied() == Applied::Yes,
        list(&issues, &inner),
        report.errors(),
        report.warnings()
    )
}

/// The document that `agent-family validate --json` prints, with no final
/// newline.
pub(crate) fn document(registry: &str, revision: &str, reports: &[&Report]) -> String {
    let item_indent = format!("{INDENT}{INDENT}");
    let reports: Vec<String> = reports
        .iter()
        .map(|one| report(one, &item_indent))
        .collect();

    format!(
        "{{\n{INDENT}\"registry\": {},\n{INDENT}\"revision\": {},\n{INDENT}\"reports\": {}\n}}",
        string(registry),
        string(revision),
        list(&reports, INDENT)
    )
}

#[cfg(test)]
mod tests {
    use super::string;

    /// What Python 3.13 answers for `json.dumps(text)`.
    #[test]
    fn a_string_is_the_python_json_text() {
        let cases = [
            ("plain", "\"plain\""),
            ("it's \"x\"", "\"it's \\\"x\\\"\""),
            ("a\\b", "\"a\\\\b\""),
            ("a\nb\tc\r", "\"a\\nb\\tc\\r\""),
            ("\u{8}\u{c}", "\"\\b\\f\""),
            ("\u{0}\u{1f}", "\"\\u0000\\u001f\""),
            ("\u{7f}", "\"\\u007f\""),
            ("\u{e9}", "\"\\u00e9\""),
            ("\u{2028}", "\"\\u2028\""),
            ("\u{1f600}", "\"\\ud83d\\ude00\""),
        ];
        for (text, wanted) in cases {
            assert_eq!(string(text), wanted, "{text:?}");
        }
    }
}
