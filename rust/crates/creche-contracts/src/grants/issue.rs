//! The report of a document that is not valid: each issue and where it is.
//!
//! The conversion of a grant file or of a request body does not stop at the
//! first fault. It collects each issue, as the Python code does, so a report
//! names the same count of issues.

use std::fmt;

use crate::ids::{FamilyNameError, ServerNameError, Sha256HexError, ToolNameError};

use super::file::VerbNameError;

/// One step of the path from the top of a document to a value.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Step {
    /// The value of this key of an object. The key is text of the document.
    Key(String),
    /// The item at this place of an array, from 0.
    Index(usize),
    /// The key itself, and not its value. It follows the [`Step::Key`] of
    /// that key.
    KeyItself,
}

impl fmt::Display for Step {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            // The key is untrusted text. `{:?}` escapes each control
            // character, so a key cannot start a new line in a log.
            Self::Key(key) => write!(f, "{key:?}"),
            Self::Index(index) => write!(f, "{index}"),
            Self::KeyItself => f.write_str("the key"),
        }
    }
}

/// Why a text is not a name of the grammar that its field has.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum NameError {
    /// The text is not a family name.
    Family(FamilyNameError),
    /// The text is not the name of an MCP server.
    Server(ServerNameError),
    /// The text is not the name of a tool.
    Tool(ToolNameError),
    /// The text is not the name of a verb.
    Verb(VerbNameError),
    /// The text is not a SHA-256 digest in lower-case hex.
    Digest(Sha256HexError),
}

impl fmt::Display for NameError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Family(error) => error.fmt(f),
            Self::Server(error) => error.fmt(f),
            Self::Tool(error) => error.fmt(f),
            Self::Verb(error) => error.fmt(f),
            Self::Digest(error) => error.fmt(f),
        }
    }
}

/// What is wrong with one value of a document.
///
/// The set is closed. The chaperone makes each value, and no process reads
/// one from a wire.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum IssueKind {
    /// The object does not have this key, and the key has no default.
    Missing,
    /// The contract does not name this key.
    UnknownKey,
    /// The value is not a string.
    NotText,
    /// The value is not an array.
    NotList,
    /// The value is not an object with names as its keys.
    NotMap,
    /// The value is not an object with the keys of its type.
    NotObject,
    /// The value is not an integer, and not a value that the Python code
    /// reads as one.
    NotInteger,
    /// The text has fewer characters than this.
    TextTooShort {
        /// The smallest count of characters.
        min: usize,
    },
    /// The text has more characters than this.
    TextTooLong {
        /// The largest count of characters.
        max: usize,
    },
    /// The text does not have the grammar of the name.
    Name(NameError),
    /// The array has fewer valid items than this.
    TooFew {
        /// The smallest count of items.
        min: usize,
    },
    /// The array or the object has more valid items than this.
    TooMany {
        /// The largest count of items.
        max: usize,
    },
    /// The number has a fraction.
    Fraction,
    /// The number is a NaN or an infinity.
    NotFinite,
    /// The text is not an integer in a form that the Python code reads.
    NotIntegerText,
    /// The integer is too large for the form that holds it: a float of 2^63
    /// or more, or a text of more than 4300 digits.
    IntegerTooLarge,
    /// The integer is below this.
    BelowMinimum {
        /// The smallest value.
        min: u64,
    },
    /// The body is not JSON as the Python reader takes it.
    NotJson,
    /// The text is not one of the words that the field takes.
    NotChoice,
}

impl fmt::Display for IssueKind {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Missing => f.write_str("the key is missing"),
            Self::UnknownKey => f.write_str("the contract does not name the key"),
            Self::NotText => f.write_str("the value is not a string"),
            Self::NotList => f.write_str("the value is not an array"),
            Self::NotMap | Self::NotObject => f.write_str("the value is not an object"),
            Self::NotInteger => f.write_str("the value is not an integer"),
            Self::TextTooShort { min: 1 } => f.write_str("the text has 1 character or more"),
            Self::TextTooShort { min } => write!(f, "the text has {min} characters or more"),
            Self::TextTooLong { max } => write!(f, "the text has {max} characters or less"),
            Self::Name(error) => error.fmt(f),
            Self::TooFew { min: 1 } => f.write_str("the array has 1 valid item or more"),
            Self::TooFew { min } => write!(f, "the array has {min} valid items or more"),
            Self::TooMany { max } => write!(f, "the value has {max} valid items or less"),
            Self::Fraction => f.write_str("the number has a fraction"),
            Self::NotFinite => f.write_str("the number is not finite"),
            Self::NotIntegerText => f.write_str("the text is not an integer"),
            Self::IntegerTooLarge => f.write_str("the integer is too large for its form"),
            Self::BelowMinimum { min } => write!(f, "the integer is {min} or more"),
            Self::NotJson => f.write_str("the body is not JSON"),
            Self::NotChoice => f.write_str("the text is not a word that the field takes"),
        }
    }
}

/// One issue of a document: what is wrong, and with which value.
///
/// A path can hold a key of the document. A key is untrusted text. Write the
/// count of the issues to a file that another process reads, and not a path.
///
/// ```
/// use creche_contracts::grants::{ApprovalBody, BodyError, Issue, IssueKind, Step};
///
/// let Err(BodyError::Invalid(issues)) = ApprovalBody::parse(b"{}") else {
///     panic!("the body has no decision");
/// };
/// let [issue]: [Issue; 1] = issues.try_into().expect("the body has one issue");
///
/// assert_eq!(issue.kind(), IssueKind::Missing);
/// assert_eq!(issue.path(), [Step::Key("decision".to_owned())]);
/// ```
///
/// Code outside this module cannot build an issue:
///
/// ```compile_fail,E0451
/// use creche_contracts::grants::{Issue, IssueKind};
///
/// let issue = Issue { path: Vec::new(), kind: IssueKind::Missing };
/// ```
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Issue {
    path: Vec<Step>,
    kind: IssueKind,
}

impl Issue {
    /// The path from the top of the document to the value.
    #[must_use]
    pub fn path(&self) -> &[Step] {
        &self.path
    }

    /// What is wrong with the value.
    #[must_use]
    pub fn kind(&self) -> IssueKind {
        self.kind
    }
}

impl fmt::Display for Issue {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("at ")?;
        if self.path.is_empty() {
            f.write_str("the top")?;
        }

        for (place, step) in self.path.iter().enumerate() {
            if place > 0 {
                f.write_str(", ")?;
            }

            step.fmt(f)?;
        }

        write!(f, ": {}", self.kind)
    }
}

/// The issues of one document while a conversion collects them, and the path
/// to the value that the conversion reads now.
pub(super) struct Report {
    path: Vec<Step>,
    issues: Vec<Issue>,
}

impl Report {
    pub(super) fn new() -> Self {
        Self {
            path: Vec::new(),
            issues: Vec::new(),
        }
    }

    /// Records one issue of the value that the path names.
    pub(super) fn issue(&mut self, kind: IssueKind) {
        self.issues.push(Issue {
            path: self.path.clone(),
            kind,
        });
    }

    /// Runs `read` with one more step on the path.
    pub(super) fn at<T>(&mut self, step: Step, read: impl FnOnce(&mut Self) -> T) -> T {
        self.path.push(step);
        let result = read(self);
        self.path.pop();

        result
    }

    /// Runs `read` on the value of one key.
    pub(super) fn at_key<T>(&mut self, key: &str, read: impl FnOnce(&mut Self) -> T) -> T {
        self.at(Step::Key(key.to_owned()), read)
    }

    /// The count of issues so far. [`Report::forget_after`] takes it.
    pub(super) fn mark(&self) -> usize {
        self.issues.len()
    }

    /// Whether the report has an issue that came after `mark`.
    pub(super) fn has_after(&self, mark: usize) -> bool {
        self.issues.len() > mark
    }

    /// Drops each issue that came after `mark`.
    pub(super) fn forget_after(&mut self, mark: usize) {
        self.issues.truncate(mark);
    }

    /// The issues, in the order in which the conversion found them.
    pub(super) fn finish(self) -> Vec<Issue> {
        self.issues
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_report_keeps_each_issue_with_its_path() {
        let mut report = Report::new();
        report.issue(IssueKind::NotObject);
        report.at_key("tools", |report| {
            report.at_key("kagi", |report| {
                report.at(Step::KeyItself, |report| report.issue(IssueKind::NotText));
                report.at(Step::Index(2), |report| report.issue(IssueKind::Missing));
            });
        });
        let mark = report.mark();
        report.issue(IssueKind::Fraction);

        assert!(report.has_after(mark));
        report.forget_after(mark);
        assert!(!report.has_after(mark));

        let issues = report.finish();
        let texts: Vec<String> = issues.iter().map(Issue::to_string).collect();

        assert_eq!(issues.len(), 3);
        assert_eq!(
            texts,
            [
                "at the top: the value is not an object",
                "at \"tools\", \"kagi\", the key: the value is not a string",
                "at \"tools\", \"kagi\", 2: the key is missing",
            ]
        );
    }

    #[test]
    fn a_key_with_a_control_character_stays_on_one_line() {
        let mut report = Report::new();
        report.at_key("a\nb", |report| report.issue(IssueKind::UnknownKey));
        let issues = report.finish();

        assert_eq!(
            issues[0].to_string(),
            "at \"a\\nb\": the contract does not name the key"
        );
    }

    #[test]
    fn each_kind_says_which_rule_failed() {
        let said = [
            (
                IssueKind::TextTooShort { min: 1 },
                "the text has 1 character or more",
            ),
            (
                IssueKind::TextTooShort { min: 2 },
                "the text has 2 characters or more",
            ),
            (
                IssueKind::TextTooLong { max: 128 },
                "the text has 128 characters or less",
            ),
            (
                IssueKind::TooFew { min: 1 },
                "the array has 1 valid item or more",
            ),
            (
                IssueKind::TooFew { min: 2 },
                "the array has 2 valid items or more",
            ),
            (
                IssueKind::TooMany { max: 64 },
                "the value has 64 valid items or less",
            ),
            (IssueKind::NotList, "the value is not an array"),
            (IssueKind::NotMap, "the value is not an object"),
            (IssueKind::NotInteger, "the value is not an integer"),
            (IssueKind::NotFinite, "the number is not finite"),
            (IssueKind::NotIntegerText, "the text is not an integer"),
            (
                IssueKind::IntegerTooLarge,
                "the integer is too large for its form",
            ),
            (
                IssueKind::BelowMinimum { min: 1 },
                "the integer is 1 or more",
            ),
            (IssueKind::NotJson, "the body is not JSON"),
            (
                IssueKind::NotChoice,
                "the text is not a word that the field takes",
            ),
        ];
        for (kind, text) in said {
            assert_eq!(kind.to_string(), text);
        }
    }
}
