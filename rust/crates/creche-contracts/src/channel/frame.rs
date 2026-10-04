//! The framing of the channel: one record for each line (contract 03 §2).
//!
//! LF is the only delimiter. A generic line reader is wrong here: such a
//! reader can also split on U+2028 and U+2029, and a JSON string can hold
//! them (§2 rule 4).

use std::error::Error;
use std::fmt;

/// The largest count of bytes in one line, with no LF (contract 03 §2 rule
/// 5). The limit applies in both directions.
pub const MAX_LINE_BYTES: usize = 1_048_576;

/// The one delimiter of a record.
const LF: u8 = b'\n';

/// The one character that a reader strips from the end of a record.
const CR: char = '\r';

/// Why the host drops a line from the playpen (contract 03 §13).
///
/// The host makes this value itself. It does not read the value from a wire,
/// so no reader meets an unknown one.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum Refusal {
    /// The line has more bytes than the limit (§2 rule 7).
    TooLarge,
    /// The line is not UTF-8.
    BadUtf8,
    /// The line is not JSON.
    NotJson,
    /// The line is JSON and not an object.
    NotObject,
    /// The line has no `type`, or a `type` that §5 does not name.
    UnknownType,
    /// A field that the message needs is absent or has the wrong type. Also
    /// each line on which the Python host raises an exception.
    Malformed,
    /// The line names a turn that the host does not have in flight (§13
    /// rule 3). The parser does not give this reason. It needs the state of
    /// a channel.
    UnknownAddress,
    /// The `turn_seq` of the line is not one above the last (§13 rule 4).
    /// The parser does not give this reason. It needs the state of a
    /// channel.
    SequenceGap,
}

impl Refusal {
    /// The name of the reason, as the Python host writes it.
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::TooLarge => "too_large",
            Self::BadUtf8 => "bad_utf8",
            Self::NotJson => "not_json",
            Self::NotObject => "not_object",
            Self::UnknownType => "unknown_type",
            Self::Malformed => "malformed",
            Self::UnknownAddress => "unknown_address",
            Self::SequenceGap => "sequence_gap",
        }
    }
}

impl fmt::Display for Refusal {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_str())
    }
}

impl Error for Refusal {}

/// One record that the splitter framed, or the reason it refused the record.
///
/// ```
/// use creche_contracts::channel::frame::{LineSplitter, RawLine};
///
/// let lines: Vec<RawLine> = LineSplitter::new().feed(b"{}\n");
/// assert_eq!(lines[0].size(), 2);
/// ```
///
/// Code outside this module cannot build a value from raw parts:
///
/// ```compile_fail,E0451
/// use creche_contracts::channel::frame::{LineSplitter, RawLine};
///
/// let Some(line) = LineSplitter::new().feed(b"{}\n").pop() else { return };
/// let line = RawLine { size: 2, ..line };
/// ```
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RawLine {
    content: Result<String, Refusal>,
    size: usize,
}

impl RawLine {
    /// The text of the record, with no LF and with one CR at its end taken
    /// away. The error is [`Refusal::TooLarge`] or [`Refusal::BadUtf8`].
    ///
    /// # Errors
    ///
    /// The reason the splitter refused the record.
    pub fn text(&self) -> Result<&str, Refusal> {
        match &self.content {
            Ok(text) => Ok(text),
            Err(refusal) => Err(*refusal),
        }
    }

    /// The count of bytes of the record, with no LF and with its CR.
    #[must_use]
    pub fn size(&self) -> usize {
        self.size
    }
}

/// Splits a stream of bytes into records, on LF only (contract 03 §2).
///
/// The splitter refuses a record of more bytes than its limit. It then
/// discards the bytes up to the next LF, so the channel stays usable.
///
/// ```
/// use creche_contracts::channel::frame::LineSplitter;
///
/// let mut splitter = LineSplitter::new();
/// assert!(splitter.feed(b"{\"type\":").is_empty());
///
/// let lines = splitter.feed(b"\"pong\"}\r\n");
/// assert_eq!(lines[0].text(), Ok("{\"type\":\"pong\"}"));
/// ```
///
/// Code outside this module cannot build a value from raw parts:
///
/// ```compile_fail,E0451
/// use creche_contracts::channel::frame::LineSplitter;
///
/// let splitter = LineSplitter { dropping: false, ..LineSplitter::new() };
/// ```
#[derive(Debug)]
pub struct LineSplitter {
    max: usize,
    buffer: Vec<u8>,
    dropping: bool,
}

impl Default for LineSplitter {
    fn default() -> Self {
        Self::new()
    }
}

impl LineSplitter {
    /// A splitter with the limit of the contract, [`MAX_LINE_BYTES`].
    #[must_use]
    pub fn new() -> Self {
        Self::with_limit(MAX_LINE_BYTES)
    }

    /// A splitter that refuses a record of more than `max_line_bytes` bytes.
    #[must_use]
    pub fn with_limit(max_line_bytes: usize) -> Self {
        Self {
            max: max_line_bytes,
            buffer: Vec::new(),
            dropping: false,
        }
    }

    /// Takes the next bytes of the stream. The result holds each record that
    /// these bytes complete, and each refusal.
    pub fn feed(&mut self, chunk: &[u8]) -> Vec<RawLine> {
        let mut lines = Vec::new();
        let mut rest = chunk;
        while let Some(at) = rest.iter().position(|byte| *byte == LF) {
            let (head, tail) = rest.split_at(at);
            rest = tail.get(1..).unwrap_or_default();
            if self.dropping {
                self.dropping = false;
                continue;
            }

            lines.push(self.record(head));
            self.buffer.clear();
        }

        if self.dropping {
            return lines;
        }

        // A partial record over the limit is refused now. To hold it would
        // let one sender grow the buffer with no limit.
        let size = self.buffer.len().saturating_add(rest.len());
        if size > self.max {
            lines.push(refused(Refusal::TooLarge, size));
            self.buffer.clear();
            self.dropping = true;
            return lines;
        }

        self.buffer.extend_from_slice(rest);

        lines
    }

    /// The count of bytes that the splitter holds for a record with no LF
    /// yet.
    #[must_use]
    pub fn pending_bytes(&self) -> usize {
        self.buffer.len()
    }

    /// The record that `tail` completes.
    fn record(&mut self, tail: &[u8]) -> RawLine {
        let size = self.buffer.len().saturating_add(tail.len());
        if size > self.max {
            return refused(Refusal::TooLarge, size);
        }

        self.buffer.extend_from_slice(tail);
        let Ok(text) = str::from_utf8(&self.buffer) else {
            return refused(Refusal::BadUtf8, size);
        };

        // One CR at the end goes. No other byte does (§2 rule 3).
        let text = text.strip_suffix(CR).unwrap_or(text);

        RawLine {
            content: Ok(text.to_owned()),
            size,
        }
    }
}

fn refused(refusal: Refusal, size: usize) -> RawLine {
    RawLine {
        content: Err(refusal),
        size,
    }
}

/// Why the encoder gave no line.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum EncodeError {
    /// The line has more bytes than the limit. A sender never writes such a
    /// line (contract 03 §2 rule 6).
    TooLarge {
        /// The count of bytes of the line, with no LF.
        size: usize,
        /// The limit.
        max_line_bytes: usize,
    },
}

impl fmt::Display for EncodeError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::TooLarge {
                size,
                max_line_bytes,
            } => write!(
                f,
                "outbound line is {size} bytes, over the {max_line_bytes} cap"
            ),
        }
    }
}

impl Error for EncodeError {}

/// Makes one line of a record: the body and one LF.
///
/// # Errors
///
/// [`EncodeError::TooLarge`] when the body has more than `max_line_bytes`
/// bytes.
pub(super) fn framed(mut body: String, max_line_bytes: usize) -> Result<String, EncodeError> {
    let size = body.len();
    if size > max_line_bytes {
        return Err(EncodeError::TooLarge {
            size,
            max_line_bytes,
        });
    }

    body.push(char::from(LF));

    Ok(body)
}

#[cfg(test)]
mod tests {
    use serde_json::{Value, json};

    use super::*;
    use crate::vectors::{self, Chunk, Input, Outcome};

    const SURFACE: &str = "channel.frame";

    fn texts(lines: &[RawLine]) -> Vec<Result<&str, Refusal>> {
        lines.iter().map(RawLine::text).collect()
    }

    #[test]
    fn a_record_ends_at_each_lf_and_nowhere_else() {
        let mut splitter = LineSplitter::new();

        assert_eq!(texts(&splitter.feed(b"a\rb\n\n")), [Ok("a\rb"), Ok("")]);
        assert_eq!(texts(&splitter.feed("a\u{2028}b".as_bytes())), []);
        assert_eq!(splitter.pending_bytes(), 5);
        assert_eq!(texts(&splitter.feed(b"\r\r\n")), [Ok("a\u{2028}b\r")]);
        assert_eq!(splitter.pending_bytes(), 0);
    }

    #[test]
    fn a_record_over_the_limit_is_refused_and_the_next_one_is_read() {
        let mut splitter = LineSplitter::with_limit(4);
        let lines = splitter.feed(b"abcde");

        assert_eq!(texts(&lines), [Err(Refusal::TooLarge)]);
        assert_eq!(lines[0].size(), 5);
        assert_eq!(splitter.pending_bytes(), 0);
        assert_eq!(texts(&splitter.feed(b"fgh")), []);
        assert_eq!(splitter.pending_bytes(), 0);
        assert_eq!(
            texts(&splitter.feed(b"i\nabcd\nabcd\r\n")),
            [Ok("abcd"), Err(Refusal::TooLarge)]
        );
    }

    #[test]
    fn a_record_that_is_not_utf8_is_refused() {
        let mut splitter = LineSplitter::default();
        let lines = splitter.feed(b"\xed\xa0\x80\n\xc0\xaf\nok\n");

        assert_eq!(
            texts(&lines),
            [Err(Refusal::BadUtf8), Err(Refusal::BadUtf8), Ok("ok")]
        );
        assert_eq!(lines[0].size(), 3);
    }

    #[test]
    fn a_line_is_its_body_and_one_lf() {
        assert_eq!(framed("{}".to_owned(), 2), Ok("{}\n".to_owned()));

        let error = framed("{\"a\":1}".to_owned(), 6).unwrap_err();
        assert_eq!(
            error,
            EncodeError::TooLarge {
                size: 7,
                max_line_bytes: 6
            }
        );
        assert_eq!(
            error.to_string(),
            "outbound line is 7 bytes, over the 6 cap"
        );
    }

    #[test]
    fn each_refusal_has_the_name_of_the_python_host() {
        let names = [
            (Refusal::TooLarge, "too_large"),
            (Refusal::BadUtf8, "bad_utf8"),
            (Refusal::NotJson, "not_json"),
            (Refusal::NotObject, "not_object"),
            (Refusal::UnknownType, "unknown_type"),
            (Refusal::Malformed, "malformed"),
            (Refusal::UnknownAddress, "unknown_address"),
            (Refusal::SequenceGap, "sequence_gap"),
        ];

        for (refusal, name) in names {
            assert_eq!(refusal.as_str(), name);
            assert_eq!(refusal.to_string(), name);
        }
    }

    /// One record as the vector file writes it.
    fn raw_line(line: &RawLine) -> Value {
        json!({
            "text": line.text().ok(),
            "size": line.size(),
            "refusal": line.text().err().map(Refusal::as_str),
        })
    }

    #[test]
    fn the_splitter_frames_each_stream_as_the_python_host_does() {
        let surface = vectors::surface(SURFACE);

        assert_eq!(surface.entry, "attendance.wire.LineSplitter.feed");
        assert!(surface.contract.starts_with("contract 03"));
        assert!(!surface.notes.is_empty());
        assert!(surface.context.is_empty());
        for vector in &surface.vectors {
            let Input::Chunks(chunks) = &vector.input else {
                panic!("{}: the input is no list of chunks", vector.id);
            };
            let limit = vector.field("params").unwrap()["max_line_bytes"]
                .as_u64()
                .unwrap();
            let mut splitter = LineSplitter::with_limit(usize::try_from(limit).unwrap());
            let feeds: Vec<Value> = chunks
                .iter()
                .map(Chunk::bytes)
                .map(|chunk| splitter.feed(&chunk).iter().map(raw_line).collect())
                .collect();
            let value = json!({"feeds": feeds, "pending_bytes": splitter.pending_bytes()});

            assert_eq!(vector.result, Outcome::Accepted, "{}", vector.id);
            assert_eq!(Some(&value), vector.value(), "{}", vector.id);
            assert_eq!(
                vector.input.bytes().unwrap(),
                chunks.iter().flat_map(Chunk::bytes).collect::<Vec<u8>>()
            );
        }
    }
}
