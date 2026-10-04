//! A position in the text, and the text of an error at that position.
//!
//! The Python reader counts characters, not bytes. The buffer here is one
//! `char` for each character, so each number is the number that PyYAML
//! writes.

use super::LoadError;

/// The name that PyYAML gives a text that is no file.
const STREAM_NAME: &str = "<unicode string>";

/// The count of spaces before a snippet.
const SNIPPET_INDENT: usize = 4;

/// A snippet shows this count of characters, at most, on each side of the
/// position. PyYAML computes it as `75 / 2 - 1`, which is 36.5.
const SNIPPET_SIDE: usize = 37;

/// What PyYAML puts in the place of the text that a snippet leaves out.
const ELLIPSIS: &str = " ... ";

/// One position in the text. Each count starts at 0.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(super) struct Mark {
    /// The count of characters before the position.
    pub(super) index: usize,
    pub(super) line: usize,
    pub(super) column: usize,
}

/// Whether `c` ends a line, or ends the text.
pub(super) fn ends_line(c: char) -> bool {
    matches!(c, '\0' | '\r' | '\n' | '\u{85}' | '\u{2028}' | '\u{2029}')
}

/// The line of the text around `mark`, and a caret below the position.
fn snippet(mark: Mark, buffer: &[char]) -> String {
    let pointer = mark.index;
    let mut head = "";
    let mut start = pointer;
    while start > 0 && !buffer.get(start - 1).is_some_and(|c| ends_line(*c)) {
        start -= 1;
        if pointer - start >= SNIPPET_SIDE {
            head = ELLIPSIS;
            start += ELLIPSIS.len();
            break;
        }
    }

    let mut tail = "";
    let mut end = pointer;
    while end < buffer.len() && !buffer.get(end).is_some_and(|c| ends_line(*c)) {
        end += 1;
        if end - pointer >= SNIPPET_SIDE {
            tail = ELLIPSIS;
            end -= ELLIPSIS.len();
            break;
        }
    }

    let shown: String = buffer.get(start..end).unwrap_or(&[]).iter().collect();
    let caret = SNIPPET_INDENT + pointer.saturating_sub(start) + head.len();

    format!(
        "{}{head}{shown}{tail}\n{}^",
        " ".repeat(SNIPPET_INDENT),
        " ".repeat(caret)
    )
}

/// The text that PyYAML writes for one position.
fn place(mark: Mark, buffer: &[char]) -> String {
    format!(
        "  in \"{STREAM_NAME}\", line {}, column {}:\n{}",
        mark.line + 1,
        mark.column + 1,
        snippet(mark, buffer)
    )
}

/// The error that PyYAML raises as a `MarkedYAMLError`: an optional context
/// with its position, then the problem with its position.
pub(super) fn marked(
    buffer: &[char],
    context: Option<(&str, Mark)>,
    problem: &str,
    at: Mark,
) -> LoadError {
    let mut lines: Vec<String> = Vec::new();
    if let Some((text, mark)) = context {
        lines.push(text.to_owned());
        if mark.line != at.line || mark.column != at.column {
            lines.push(place(mark, buffer));
        }
    }

    lines.push(problem.to_owned());
    lines.push(place(at, buffer));

    LoadError::Syntax(lines.join("\n"))
}

/// The error that PyYAML raises for a character that no YAML text can hold.
pub(super) fn unacceptable(character: char, position: usize) -> LoadError {
    LoadError::Syntax(format!(
        "unacceptable character #x{:04x}: special characters are not allowed\n  in \
         \"{STREAM_NAME}\", position {position}",
        u32::from(character)
    ))
}
