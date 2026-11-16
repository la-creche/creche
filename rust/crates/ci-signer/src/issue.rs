//! One statement for each run (section 4.5 step 6, section 4.6), and the
//! one place that builds a statement to sign.
//!
//! `Signable` has a private field, so only this module makes one, and only
//! `IssuedRuns::issue` does. It takes an `Eligible` value, which only
//! `decide_merge` makes.
//!
//! The rule must hold across a restart of the signer, and section 4.7
//! restarts it on each rotation. So `IssuedRuns` has no empty constructor.
//! The program reads its journal of issued runs (section 4.3 item 7) with
//! `IssuedRuns::from_journal` once at start, and appends the `IssuedLine`
//! of each issue. A first start reads an empty journal.
//!
//! The program must hold exactly one `IssuedRuns`. The type is not `Clone`,
//! so a copy of the set cannot issue a second statement for a run. A second
//! `from_journal` of the same text would give a second set: the program
//! calls it once, at start.
//!
//! The set also refuses a second statement for a commit. A rerun that
//! Forgejo gives a new run id then gets no second statement either.
// CONTRACT-QUESTION: section 4.3 item 7 names one journal line for each
// statement, with the run id, the commit and the SHA-256 of the statement.
// It does not say whether the line lives in the systemd journal or in a
// file. The reading here gives the line a strict text form that a file of
// the unit's state directory can hold. The program of a later PR must
// append and sync the line before it calls cosign. A change of the form is
// `IssuedLine` and `from_journal`.
// CONTRACT-QUESTION: section 4.6 says that a rerun gets no second
// statement. Whether a Forgejo rerun keeps the run id is not measured. The
// reading here also refuses a second statement for the same commit.
// Section 4.6 lets the next green run on `main` tag what a run left, and
// that run builds a new merge commit. A change is the commit set.

use std::collections::BTreeSet;
use std::fmt;

use sha2::{Digest, Sha256};

use crate::eligibility::Eligible;
use crate::exchange::Request;
use crate::names::{CommitSha, NameError, RunId, RunIdError, Sha256Hex};
use crate::signed_at::SignedAt;
use crate::statement::Statement;

/// The run or its commit already got a statement.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum AlreadyIssued {
    /// The run id is in the journal.
    Run(RunId),
    /// The commit is in the journal, for another run id.
    Commit(CommitSha),
}

impl fmt::Display for AlreadyIssued {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Run(run) => write!(formatter, "run {run} already got its statement"),
            Self::Commit(commit) => write!(formatter, "commit {commit} already got a statement"),
        }
    }
}

impl std::error::Error for AlreadyIssued {}

/// One line of the journal of issued runs:
/// `<run id> TAB <commit> TAB <SHA-256 of the statement> LF`.
///
/// The fields are private. Only `IssuedRuns::issue` makes a line, so the
/// run and the commit of a line are always those of its statement. The
/// program appends `text`.
#[derive(Debug, PartialEq, Eq)]
pub struct IssuedLine {
    run: RunId,
    commit: CommitSha,
    statement: Sha256Hex,
}

impl IssuedLine {
    /// The text of the line to append to the journal, with its final LF.
    #[must_use]
    pub fn text(&self) -> String {
        self.to_string()
    }
}

impl fmt::Display for IssuedLine {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        writeln!(
            formatter,
            "{}\t{}\t{}",
            self.run, self.commit, self.statement
        )
    }
}

/// Why a journal is refused.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum JournalError {
    /// The text is not empty and does not end with one LF.
    NoFinalLf,
    /// A line has not exactly three fields.
    Fields(usize),
    /// A run id is wrong.
    Run(usize, RunIdError),
    /// A commit or a digest is wrong.
    Name(usize, NameError),
    /// A run id or a commit is in two lines.
    Duplicate(usize),
}

impl fmt::Display for JournalError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::NoFinalLf => formatter.write_str("the journal does not end with a LF"),
            Self::Fields(line) => write!(formatter, "journal line {line} has not three fields"),
            Self::Run(line, error) => write!(formatter, "journal line {line}: {error}"),
            Self::Name(line, error) => write!(formatter, "journal line {line}: {error}"),
            Self::Duplicate(line) => {
                write!(formatter, "journal line {line} repeats a run or a commit")
            }
        }
    }
}

impl std::error::Error for JournalError {}

/// The canonical bytes of one issued statement, ready for
/// `cosign sign-blob`. Only `IssuedRuns::issue` makes one.
#[derive(Debug, PartialEq, Eq)]
pub struct Signable {
    bytes: Vec<u8>,
}

impl Signable {
    /// The bytes that cosign signs.
    #[must_use]
    pub fn bytes(&self) -> &[u8] {
        &self.bytes
    }
}

/// What one issue gives: the bytes to sign and the line to append.
#[derive(Debug, PartialEq, Eq)]
pub struct Issued {
    /// The canonical statement, for `cosign sign-blob`.
    pub signable: Signable,
    /// The journal line of this statement.
    pub line: IssuedLine,
}

/// The runs and the commits that already got a statement. The program
/// holds one, loaded once at start.
#[derive(Debug)]
pub struct IssuedRuns {
    runs: BTreeSet<RunId>,
    commits: BTreeSet<CommitSha>,
}

impl IssuedRuns {
    /// The issued runs of a journal text. An empty text is a first start.
    ///
    /// # Errors
    ///
    /// Each `JournalError`. A bad journal refuses the start (fail closed).
    pub fn from_journal(text: &str) -> Result<Self, JournalError> {
        let mut issued = Self {
            runs: BTreeSet::new(),
            commits: BTreeSet::new(),
        };
        if text.is_empty() {
            return Ok(issued);
        }

        let body = text.strip_suffix('\n').ok_or(JournalError::NoFinalLf)?;
        for (index, line) in body.split('\n').enumerate() {
            let number = index + 1;
            let mut fields = line.split('\t');
            let (Some(run), Some(commit), Some(statement), None) =
                (fields.next(), fields.next(), fields.next(), fields.next())
            else {
                return Err(JournalError::Fields(number));
            };

            let run: RunId = run
                .parse()
                .map_err(|error| JournalError::Run(number, error))?;
            let commit: CommitSha = commit
                .parse()
                .map_err(|error| JournalError::Name(number, error))?;
            statement
                .parse::<Sha256Hex>()
                .map_err(|error| JournalError::Name(number, error))?;
            if issued.check(run, &commit).is_err() {
                return Err(JournalError::Duplicate(number));
            }
            issued.runs.insert(run);
            issued.commits.insert(commit);
        }
        Ok(issued)
    }

    /// Whether the run may still get its statement: the check at the
    /// `open` line.
    ///
    /// # Errors
    ///
    /// `AlreadyIssued` for a run or a commit in the set.
    pub fn admit(&self, eligible: &Eligible) -> Result<(), AlreadyIssued> {
        self.check(eligible.run(), eligible.commit())
    }

    /// `AlreadyIssued` when the run or the commit is in the set.
    fn check(&self, run: RunId, commit: &CommitSha) -> Result<(), AlreadyIssued> {
        if self.runs.contains(&run) {
            return Err(AlreadyIssued::Run(run));
        }
        if self.commits.contains(commit) {
            return Err(AlreadyIssued::Commit(commit.clone()));
        }
        Ok(())
    }

    /// The statement of an eligible run with the tags of its request. It
    /// records the run and the commit first, so a later failure still
    /// leaves them issued (fail closed). The repository, the workflow, the
    /// commit and the run come only from `eligible`.
    ///
    /// # Errors
    ///
    /// `AlreadyIssued` for a run or a commit in the set.
    pub fn issue(
        &mut self,
        eligible: Eligible,
        request: Request,
        signed_at: SignedAt,
    ) -> Result<Issued, AlreadyIssued> {
        let (repository, workflow, commit, run) = eligible.into_facts();
        self.check(run, &commit)?;
        self.runs.insert(run);
        self.commits.insert(commit.clone());

        let statement = Statement::new(
            repository,
            workflow,
            commit.clone(),
            run,
            signed_at,
            request.into_tags(),
        );
        let signable = Signable {
            bytes: statement.canonical_bytes(),
        };
        let line = IssuedLine {
            run,
            commit,
            statement: sha256_hex(signable.bytes()),
        };
        Ok(Issued { signable, line })
    }
}

/// The SHA-256 of `bytes`.
fn sha256_hex(bytes: &[u8]) -> Sha256Hex {
    Sha256Hex::from_digest(Sha256::digest(bytes).into())
}
