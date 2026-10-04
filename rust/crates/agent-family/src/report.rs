//! The validation report of one file (contract 01 §7).
//!
//! Invariant 19: a bad definition gives a report. Nothing here fails on bad
//! input. A report is data, and the caller decides what to do with it.

use creche_contracts::family::{Issue, Severity};

/// The status document caps `first_error` at this count of characters
/// (contract 05 §3.2).
pub const FIRST_ERROR_CHARS: usize = 200;

/// The state of one family (contract 05 §2.1).
///
/// The set is closed. A reader refuses another value: a state that the
/// reader does not know cannot say whether the family runs.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum FamilyState {
    InSync,
    Reconciling,
    Invalid,
    Degraded,
}

impl FamilyState {
    /// The text of the state in JSON.
    #[must_use]
    pub fn as_str(self) -> &'static str {
        match self {
            Self::InSync => "in_sync",
            Self::Reconciling => "reconciling",
            Self::Invalid => "invalid",
            Self::Degraded => "degraded",
        }
    }
}

/// Whether the live state holds what a file says.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Applied {
    Yes,
    No,
}

/// The report of one family file or of one server file (contract 01 §7).
///
/// The validator gives the state `invalid` to a report with an error, and
/// `reconciling` to each other report. It gives `applied: false`, because a
/// validation changes no live state. `caregiver` gives the real pair with
/// [`Report::stamped`] after it applies the file.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Report {
    family: String,
    file: String,
    issues: Vec<Issue>,
    state: FamilyState,
    applied: Applied,
}

impl Report {
    /// The report that a validator makes: the issues decide the state.
    #[must_use]
    pub fn new(family: &str, file: &str, issues: Vec<Issue>) -> Self {
        let invalid = issues.iter().any(|issue| issue.severity == Severity::Error);
        let state = if invalid {
            FamilyState::Invalid
        } else {
            FamilyState::Reconciling
        };

        Self {
            family: family.to_owned(),
            file: file.to_owned(),
            issues,
            state,
            applied: Applied::No,
        }
    }

    /// The name of the directory of the file.
    #[must_use]
    pub fn family(&self) -> &str {
        &self.family
    }

    /// The path of the file, from the root of the registry.
    #[must_use]
    pub fn file(&self) -> &str {
        &self.file
    }

    #[must_use]
    pub fn issues(&self) -> &[Issue] {
        &self.issues
    }

    #[must_use]
    pub fn state(&self) -> FamilyState {
        self.state
    }

    #[must_use]
    pub fn applied(&self) -> Applied {
        self.applied
    }

    fn count(&self, severity: Severity) -> usize {
        self.issues
            .iter()
            .filter(|issue| issue.severity == severity)
            .count()
    }

    /// The count of errors.
    #[must_use]
    pub fn errors(&self) -> usize {
        self.count(Severity::Error)
    }

    /// The count of warnings.
    #[must_use]
    pub fn warnings(&self) -> usize {
        self.count(Severity::Warning)
    }

    /// Whether the report holds no error.
    #[must_use]
    pub fn ok(&self) -> bool {
        self.errors() == 0
    }

    /// The first error as `loc: msg`, cut to [`FIRST_ERROR_CHARS`]
    /// characters.
    #[must_use]
    pub fn first_error(&self) -> Option<String> {
        let first = self
            .issues
            .iter()
            .find(|issue| issue.severity == Severity::Error)?;
        let line = format!("{}: {}", first.loc, first.msg);

        Some(line.chars().take(FIRST_ERROR_CHARS).collect())
    }

    /// The same report with one more issue at its end.
    #[must_use]
    pub(crate) fn with_issue(&self, issue: Issue) -> Self {
        let mut issues = self.issues.clone();
        issues.push(issue);

        Self::new(&self.family, &self.file, issues)
    }

    /// The same report with the state that a reconciler knows and a
    /// validator does not.
    #[must_use]
    pub fn stamped(&self, state: FamilyState, applied: Applied) -> Self {
        Self {
            state,
            applied,
            ..self.clone()
        }
    }
}
