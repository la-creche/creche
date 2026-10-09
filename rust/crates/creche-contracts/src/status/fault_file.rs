//! The fault file of contract 05 §3.3.1.
//!
//! `attendance` and the chaperone each write one file for each family: the
//! open faults that the service sees. `caregiver` reads both files.
//!
//! - [`FaultFile`] is the valid file that a writer writes. Its bytes are the
//!   bytes of the Python writer of the same source.
//! - [`caregiver`] is the view of the one reader.

use std::error::Error;
use std::fmt;

use super::json::{ByteOrderMark, Charset, Json, Layout, Object};
use super::raw::{FAULT_KEYS, RawFault, ReadError, list, read_object, text};
use super::time::{Freshness, Timestamp, freshness};
use super::words::{FaultCode, FaultSource};
use crate::ids::FamilyName;

/// One open fault of a fault file: a record of valid values. [`FaultFile`]
/// holds the rules between a fault and its file.
///
/// ```
/// use creche_contracts::status::fault_file::OpenFault;
/// use creche_contracts::status::words::FaultCode;
///
/// let since = "2031-04-18T06:42:58Z".parse().unwrap();
/// let fault = OpenFault::new(FaultCode::GrantsStale, since);
/// assert_eq!(fault.code(), FaultCode::GrantsStale);
/// assert_eq!(fault.since(), since);
/// assert!(fault.detail().is_empty());
/// ```
///
/// Code outside this module cannot set a field:
///
/// ```compile_fail,E0451
/// use creche_contracts::status::fault_file::OpenFault;
/// use creche_contracts::status::words::FaultCode;
///
/// fn stale_grants(fault: OpenFault) -> OpenFault {
///     OpenFault { code: FaultCode::GrantsStale, ..fault }
/// }
/// ```
#[derive(Debug, Clone, PartialEq)]
pub struct OpenFault {
    code: FaultCode,
    since: Timestamp,
    detail: Object,
}

impl OpenFault {
    /// The open fault with the code `code` and no extra key.
    #[must_use]
    pub fn new(code: FaultCode, since: Timestamp) -> Self {
        Self {
            code,
            since,
            detail: Object::new(),
        }
    }

    /// The same fault with these extra keys.
    #[must_use]
    pub fn with_detail(mut self, detail: Object) -> Self {
        self.detail = detail;

        self
    }

    /// The code.
    #[must_use]
    pub fn code(&self) -> FaultCode {
        self.code
    }

    /// When the writer first saw the fault.
    #[must_use]
    pub fn since(&self) -> Timestamp {
        self.since
    }

    /// Each extra key of the fault, in the order that the writer writes.
    /// `attendance` writes `message` and `sandbox`. The chaperone writes
    /// `message` and `rev`.
    #[must_use]
    pub fn detail(&self) -> &Object {
        &self.detail
    }
}

/// The open faults of one family, as one service writes them (contract 05
/// §3.3.1).
///
/// The file holds the faults that are open now. It is not a log: a file with
/// no fault clears each fault of its writer.
///
/// The file holds one fault at most for each code. The faults are in the
/// order of the text of their codes, which is the order of the Python writer
/// of `attendance`. The chaperone has one code, so its file holds one fault
/// at most.
///
/// ```
/// use creche_contracts::status::fault_file::{FaultFile, OpenFault};
/// use creche_contracts::status::json::Object;
/// use creche_contracts::status::words::{FaultCode, FaultSource};
///
/// let fault = OpenFault::new(FaultCode::GrantsStale, "2031-04-18T06:42:58Z".parse()?)
///     .with_detail(Object::new());
/// let written_at = "2031-04-18T06:43:10Z".parse()?;
/// let family = "chat".parse().unwrap();
/// let file = FaultFile::new(family, FaultSource::Pep, written_at, vec![fault]);
/// assert!(file.is_ok());
/// # Ok::<(), creche_contracts::status::time::TimestampError>(())
/// ```
///
/// Code outside this module cannot build a value from raw parts:
///
/// ```compile_fail,E0451
/// use creche_contracts::status::fault_file::{FaultFile, OpenFault};
/// use creche_contracts::status::json::Object;
/// use creche_contracts::status::words::{FaultCode, FaultSource};
///
/// let file = FaultFile {
///     family: "chat".parse().unwrap(),
///     source: FaultSource::Managerd,
///     written_at: "2031-04-18T06:43:10Z".parse().unwrap(),
///     faults: Vec::new(),
/// };
/// ```
#[derive(Debug, Clone, PartialEq)]
pub struct FaultFile {
    family: FamilyName,
    source: FaultSource,
    written_at: Timestamp,
    faults: Vec<OpenFault>,
}

impl FaultFile {
    /// The fault file of one family. The caller gives the faults in each
    /// order, and the file holds them in the order of the text of their codes.
    ///
    /// # Errors
    ///
    /// [`FaultFileError`] when `caregiver` is the source, when a fault has a
    /// code that the source does not detect, when a detail has the name of a
    /// field, and when two faults have one code.
    pub fn new(
        family: FamilyName,
        source: FaultSource,
        written_at: Timestamp,
        mut faults: Vec<OpenFault>,
    ) -> Result<Self, FaultFileError> {
        if source == FaultSource::Managerd {
            return Err(FaultFileError::SourceWritesNoFile);
        }

        for (item, fault) in faults.iter().enumerate() {
            if !fault.code.is_detected_by(source) {
                return Err(FaultFileError::SourceDoesNotDetect { item });
            }

            if fault
                .detail
                .iter()
                .any(|(key, _)| FAULT_KEYS.contains(&key))
            {
                return Err(FaultFileError::DetailNamesField { item });
            }

            let earlier = faults.iter().take(item);
            if earlier
                .into_iter()
                .any(|earlier| earlier.code == fault.code)
            {
                return Err(FaultFileError::CodeTwice { item });
            }
        }

        // `attendance.faults.FaultReporter` writes its faults in this order.
        // The noticeboard names the first fault of a document, so the order
        // has an effect.
        faults.sort_by_key(|fault| fault.code.as_str());

        Ok(Self {
            family,
            source,
            written_at,
            faults,
        })
    }

    /// The family.
    #[must_use]
    pub fn family(&self) -> &FamilyName {
        &self.family
    }

    /// The service that writes the file.
    #[must_use]
    pub fn source(&self) -> FaultSource {
        self.source
    }

    /// When the service wrote the file.
    #[must_use]
    pub fn written_at(&self) -> Timestamp {
        self.written_at
    }

    /// The open faults, in the order of the file.
    #[must_use]
    pub fn faults(&self) -> &[OpenFault] {
        &self.faults
    }

    /// The bytes of the file, as the Python writer of the source writes them.
    /// `attendance` writes no white space and ASCII only. The chaperone
    /// writes a space after each comma and each colon, and does not escape a
    /// character outside ASCII. Each file ends with one newline.
    #[must_use]
    pub fn encode(&self) -> Vec<u8> {
        let (layout, charset) = match self.source {
            FaultSource::Pep => (Layout::Spaced, Charset::Unicode),
            FaultSource::Sessiond | FaultSource::Managerd => (Layout::Compact, Charset::Ascii),
        };
        let word = |text: &str| Json::String(text.to_owned());
        let source = word(self.source.as_str());
        let faults = self.faults.iter().map(|fault| {
            let mut entry = Object::new();
            entry.insert("code", word(fault.code.as_str()));
            entry.insert("blocks_turns", Json::Bool(fault.code.blocks_turns()));
            entry.insert("since", word(&fault.since.to_rfc3339()));
            entry.insert("source", source.clone());
            // `FaultFile::new` refuses a detail with the name of a field.
            entry.append(&fault.detail);

            Json::Object(entry)
        });
        let mut file = Object::new();
        file.insert("family", word(self.family.as_str()));
        file.insert("source", source.clone());
        file.insert("written_at", word(&self.written_at.to_rfc3339()));
        file.insert("faults", Json::Array(faults.collect()));
        let mut bytes = Json::Object(file).encode(layout, charset).into_bytes();
        bytes.push(b'\n');

        bytes
    }
}

/// Why the parts of a fault file are not a fault file of contract 05 §3.3.1.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum FaultFileError {
    /// `caregiver` reads the fault files. It writes none.
    SourceWritesNoFile,
    /// The source does not detect the code of one fault.
    SourceDoesNotDetect {
        /// The place of the fault, from 0.
        item: usize,
    },
    /// A key of the detail of one fault is one of the five keys that §3.3
    /// names.
    DetailNamesField {
        /// The place of the fault, from 0.
        item: usize,
    },
    /// An earlier fault has the code of this fault. A writer holds one open
    /// fault for each code.
    CodeTwice {
        /// The place of the second fault, from 0.
        item: usize,
    },
}

impl fmt::Display for FaultFileError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::SourceWritesNoFile => f.write_str("caregiver writes no fault file"),
            Self::SourceDoesNotDetect { item } => {
                write!(f, "fault {item}: the writer does not detect the code")
            }
            Self::DetailNamesField { item } => {
                write!(f, "fault {item}: a detail has the name of a field")
            }
            Self::CodeTwice { item } => {
                write!(f, "fault {item}: an earlier fault has the same code")
            }
        }
    }
}

impl Error for FaultFileError {}

// --- the reader ---

// CONTRACT-QUESTION: contract 05 §3.3.1 gives no size cap for a fault file.
// The Python reader reads a file of each size. The reader here has the cap of
// the other readers of contract 05. A fault file with one fault has less than
// 1 KiB. To take a larger file, raise the constant.
/// The largest fault file that the reader parses: 1 MiB.
pub const FAULT_FILE_CAP_BYTES: usize = 1 << 20;

/// One fault that `caregiver` takes from a fault file.
#[derive(Debug, Clone, PartialEq)]
pub struct FoldedFault {
    /// The code. It is a code that the source detects.
    pub code: FaultCode,
    /// Whether the fault stops new turns: the table of §3.3, and not what the
    /// file says.
    pub blocks_turns: bool,
    /// The `since` text of the entry. The reader does not check that it is a
    /// time.
    pub since: String,
    /// The service whose directory holds the file, and not what the entry
    /// says.
    pub source: FaultSource,
    /// Whether the file is stale (§3.3.1 rule 7), and not what the entry
    /// says.
    pub stale: bool,
    /// Each key of the entry that §3.3 does not name, in the order of the
    /// file.
    pub detail: Object,
}

/// What `caregiver` takes from one fault file:
/// `caregiver.faults.read_fault_file`.
#[derive(Debug, Clone, PartialEq)]
pub struct FoldedFaults {
    /// The `family` text of the file. The reader does not check it.
    pub family: String,
    /// The service whose directory holds the file.
    pub source: FaultSource,
    /// The `written_at` text of the file.
    pub written_at: String,
    /// Each entry with a code that the source detects and a `since` text. The
    /// reader drops each other entry and keeps the file (§3.3.1 rule 6).
    pub faults: Vec<FoldedFault>,
}

/// Why `caregiver` does not use a fault file.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum FaultFileRefusal {
    /// The bytes are not one JSON object.
    Unreadable(ReadError),
    /// `family` or `written_at` is not a text, or `faults` is not a list.
    Field {
        /// The name of the field.
        field: &'static str,
    },
}

impl fmt::Display for FaultFileRefusal {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Unreadable(error) => write!(f, "{error}"),
            Self::Field { field } => write!(f, "{field}: the fault file has no such field"),
        }
    }
}

impl Error for FaultFileRefusal {}

/// The view of `caregiver`: the bytes of the fault file that `source` wrote.
/// A file with a `written_at` that is more than 90 seconds before `now`, or
/// that is not a time, is stale: each of its faults is then stale.
///
/// # Errors
///
/// [`FaultFileRefusal`] when `caregiver` does not use the file. The source
/// then has no open fault that `caregiver` knows.
pub fn caregiver(
    bytes: &[u8],
    source: FaultSource,
    now: Timestamp,
) -> Result<FoldedFaults, FaultFileRefusal> {
    let object = read_object(bytes, FAULT_FILE_CAP_BYTES, ByteOrderMark::Refuse)
        .map_err(FaultFileRefusal::Unreadable)?;
    let missing = |field| FaultFileRefusal::Field { field };
    let family = text(&object, "family");
    let family = family.value().ok_or(missing("family"))?;
    let written_at = text(&object, "written_at");
    let written_at = written_at.value().ok_or(missing("written_at"))?;
    let entries = list(&object, "faults", RawFault::read);
    let entries = entries.value().ok_or(missing("faults"))?;
    let stale = freshness(written_at.parse().ok(), now) == Freshness::Stale;
    let folded = |entry: &RawFault| {
        let code: FaultCode = entry.code.value()?.parse().ok()?;
        if !code.is_detected_by(source) || source == FaultSource::Managerd {
            return None;
        }

        Some(FoldedFault {
            code,
            blocks_turns: code.blocks_turns(),
            since: entry.since.value()?.clone(),
            source,
            stale,
            detail: entry.detail.clone(),
        })
    };

    Ok(FoldedFaults {
        family: family.clone(),
        source,
        written_at: written_at.clone(),
        faults: entries.iter().flatten().filter_map(folded).collect(),
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn time(text: &str) -> Timestamp {
        text.parse().unwrap()
    }

    fn fault(code: FaultCode, detail: &[(&str, Json)]) -> OpenFault {
        let detail = detail
            .iter()
            .map(|(key, value)| ((*key).to_owned(), value.clone()))
            .collect();

        OpenFault::new(code, time("2031-04-18T06:42:58Z")).with_detail(detail)
    }

    fn chat() -> FamilyName {
        "chat".parse().unwrap()
    }

    fn file(source: FaultSource, faults: Vec<OpenFault>) -> Result<FaultFile, FaultFileError> {
        FaultFile::new(chat(), source, time("2031-04-18T06:43:10Z"), faults)
    }

    #[test]
    fn a_writer_writes_only_the_codes_that_it_detects() {
        let orphan = || fault(FaultCode::OrphanProcesses, &[]);
        let stale = || fault(FaultCode::GrantsStale, &[]);
        let start = || fault(FaultCode::SandboxStartFailed, &[]);

        assert!(file(FaultSource::Sessiond, vec![orphan(), start()]).is_ok());
        assert!(file(FaultSource::Pep, vec![stale()]).is_ok());
        assert_eq!(
            file(FaultSource::Sessiond, vec![orphan(), stale()]),
            Err(FaultFileError::SourceDoesNotDetect { item: 1 })
        );
        assert_eq!(
            file(FaultSource::Pep, vec![start()]),
            Err(FaultFileError::SourceDoesNotDetect { item: 0 })
        );
        assert_eq!(
            file(FaultSource::Managerd, Vec::new()),
            Err(FaultFileError::SourceWritesNoFile)
        );
    }

    #[test]
    fn a_detail_cannot_take_the_name_of_a_field() {
        for key in FAULT_KEYS {
            let shadow = fault(FaultCode::OrphanProcesses, &[(key, Json::Null)]);

            assert_eq!(
                file(FaultSource::Sessiond, vec![shadow]),
                Err(FaultFileError::DetailNamesField { item: 0 }),
                "{key}"
            );
        }
    }

    #[test]
    fn a_file_holds_one_fault_for_each_code_in_the_order_of_the_codes() {
        let orphan = || fault(FaultCode::OrphanProcesses, &[]);
        let start = || fault(FaultCode::SandboxStartFailed, &[]);
        let audit = || fault(FaultCode::AuditUnreadable, &[]);
        let stale = || fault(FaultCode::GrantsStale, &[]);
        let sorted = file(FaultSource::Sessiond, vec![start(), orphan(), audit()]).unwrap();
        let codes: Vec<&str> = sorted
            .faults()
            .iter()
            .map(|fault| fault.code().as_str())
            .collect();

        assert_eq!(
            codes,
            [
                "audit_unreadable",
                "orphan_processes",
                "sandbox_start_failed"
            ]
        );
        assert_eq!(
            file(FaultSource::Sessiond, vec![orphan(), start(), orphan()]),
            Err(FaultFileError::CodeTwice { item: 2 })
        );
        assert_eq!(
            file(FaultSource::Pep, vec![stale(), stale()]),
            Err(FaultFileError::CodeTwice { item: 1 })
        );
    }

    #[test]
    fn each_writer_has_its_layout() {
        let message = Json::String("caf\u{e9}".to_owned());
        let of_attendance = file(
            FaultSource::Sessiond,
            vec![fault(
                FaultCode::OrphanProcesses,
                &[("message", message.clone())],
            )],
        )
        .unwrap();
        let of_chaperone = file(
            FaultSource::Pep,
            vec![fault(
                FaultCode::GrantsStale,
                &[("message", message), ("rev", Json::Null)],
            )],
        )
        .unwrap();

        assert_eq!(
            String::from_utf8(of_attendance.encode()).unwrap(),
            concat!(
                r#"{"family":"chat","source":"sessiond","written_at":"2031-04-18T06:43:10Z","#,
                r#""faults":[{"code":"orphan_processes","blocks_turns":false,"#,
                r#""since":"2031-04-18T06:42:58Z","source":"sessiond","message":"caf\"#,
                "u00e9\"}]}\n",
            )
        );
        assert_eq!(
            String::from_utf8(of_chaperone.encode()).unwrap(),
            concat!(
                r#"{"family": "chat", "source": "pep", "written_at": "2031-04-18T06:43:10Z", "#,
                r#""faults": [{"code": "grants_stale", "blocks_turns": true, "#,
                r#""since": "2031-04-18T06:42:58Z", "source": "pep", "message": "caf"#,
                "\u{e9}\", \"rev\": null}]}\n",
            )
        );
        assert_eq!(of_attendance.family().as_str(), "chat");
        assert_eq!(of_attendance.source(), FaultSource::Sessiond);
        assert_eq!(of_attendance.written_at(), time("2031-04-18T06:43:10Z"));
        assert_eq!(of_attendance.faults().len(), 1);
    }

    #[test]
    fn a_file_that_a_writer_made_reads_back() {
        let now = time("2031-04-18T06:43:33Z");
        let written = file(
            FaultSource::Sessiond,
            vec![fault(
                FaultCode::SandboxStartFailed,
                &[("sandbox", Json::String("chat-s4".to_owned()))],
            )],
        )
        .unwrap();
        let read = caregiver(&written.encode(), FaultSource::Sessiond, now).unwrap();

        assert_eq!(read.family, "chat");
        assert_eq!(read.written_at, "2031-04-18T06:43:10Z");
        assert_eq!(read.faults.len(), 1);
        assert_eq!(read.faults[0].code, FaultCode::SandboxStartFailed);
        assert!(read.faults[0].blocks_turns);
        assert!(!read.faults[0].stale);
        assert_eq!(read.faults[0].since, "2031-04-18T06:42:58Z");
        assert_eq!(&read.faults[0].detail, written.faults()[0].detail());
        assert_eq!(written.faults()[0].since(), time("2031-04-18T06:42:58Z"));
    }

    #[test]
    fn a_file_with_a_time_that_names_no_offset_is_stale() {
        let now = time("2031-04-18T06:43:33Z");
        let bytes = br#"{"family": "chat", "written_at": "2031-04-18T06:43:10",
            "faults": [{"code": "orphan_processes", "since": "2031-04-18T06:42:58Z"}]}"#;
        let read = caregiver(bytes, FaultSource::Sessiond, now).unwrap();

        assert!(read.faults[0].stale);
    }

    #[test]
    fn a_file_of_exactly_the_cap_is_read() {
        // The test writes the cap a second time, as a number. A constant
        // that moves then fails the test.
        let cap = 1 << 20;
        let file = |padding: usize| {
            format!(
                r#"{{"family": "chat", "written_at": "", "faults": [], "x": "{}"}}"#,
                "a".repeat(padding)
            )
        };
        let padding = cap - file(0).len();
        let now = time("2031-04-18T06:43:33Z");

        assert_eq!(file(padding).len(), cap);
        assert!(caregiver(file(padding).as_bytes(), FaultSource::Pep, now).is_ok());
        assert_eq!(
            caregiver(file(padding + 1).as_bytes(), FaultSource::Pep, now),
            Err(FaultFileRefusal::Unreadable(ReadError::TooLarge { cap }))
        );
        assert_eq!(
            caregiver(b"{}", FaultSource::Pep, now),
            Err(FaultFileRefusal::Field { field: "family" })
        );
    }

    #[test]
    fn the_error_is_a_std_error() {
        let boxed: Box<dyn Error> = Box::new(FaultFileError::SourceDoesNotDetect { item: 2 });
        let refusal: Box<dyn Error> = Box::new(FaultFileRefusal::Field { field: "faults" });

        assert_eq!(
            boxed.to_string(),
            "fault 2: the writer does not detect the code"
        );
        assert_eq!(
            refusal.to_string(),
            "faults: the fault file has no such field"
        );
    }
}
