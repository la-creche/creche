//! The signing pairs (section 4.3 item 6): each repository with its
//! release workflow file. The list is `signing-pairs.tsv` in this crate.
//! The owner comes from the host config of the signer, so a rehearsal can
//! name a shadow owner.

use std::collections::BTreeMap;
use std::fmt;

use crate::names::{NameError, Owner, RepoFullName, RepoName, WorkflowFile};

/// The built-in list: one `repo<TAB>workflow` line each, with a final LF.
const BUILTIN: &str = include_str!("../signing-pairs.tsv");

/// Why a list of pairs is refused.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum PairsError {
    /// The text does not end with one LF.
    NoFinalLf,
    /// A line is empty.
    BlankLine(usize),
    /// A line holds a CR.
    CarriageReturn(usize),
    /// A line has not exactly two fields.
    Fields(usize),
    /// A field breaks its grammar.
    Name(usize, NameError),
    /// A repository is in two lines.
    Duplicate(usize),
}

impl fmt::Display for PairsError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::NoFinalLf => formatter.write_str("the pairs do not end with a LF"),
            Self::BlankLine(line) => write!(formatter, "line {line} is empty"),
            Self::CarriageReturn(line) => write!(formatter, "line {line} holds a CR"),
            Self::Fields(line) => write!(formatter, "line {line} has not two fields"),
            Self::Name(line, error) => write!(formatter, "line {line}: {error}"),
            Self::Duplicate(line) => write!(formatter, "line {line} repeats a repository"),
        }
    }
}

impl std::error::Error for PairsError {}

/// The signing pairs of one owner.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SigningPairs {
    owner: Owner,
    workflows: BTreeMap<RepoName, WorkflowFile>,
}

impl SigningPairs {
    /// The pairs of `text` for the repositories of `owner`.
    ///
    /// # Errors
    ///
    /// Each `PairsError`. The line numbers start at 1.
    pub fn parse(owner: Owner, text: &str) -> Result<Self, PairsError> {
        let body = text.strip_suffix('\n').ok_or(PairsError::NoFinalLf)?;

        let mut workflows = BTreeMap::new();
        for (index, line) in body.split('\n').enumerate() {
            let number = index + 1;
            if line.is_empty() {
                return Err(PairsError::BlankLine(number));
            }
            if line.contains('\r') {
                return Err(PairsError::CarriageReturn(number));
            }

            let mut fields = line.split('\t');
            let (Some(repo), Some(workflow), None) = (fields.next(), fields.next(), fields.next())
            else {
                return Err(PairsError::Fields(number));
            };
            let repo: RepoName = repo
                .parse()
                .map_err(|error| PairsError::Name(number, error))?;
            let workflow: WorkflowFile = workflow
                .parse()
                .map_err(|error| PairsError::Name(number, error))?;
            if workflows.insert(repo, workflow).is_some() {
                return Err(PairsError::Duplicate(number));
            }
        }
        Ok(Self { owner, workflows })
    }

    /// The pairs of `signing-pairs.tsv` for `owner`.
    ///
    /// # Errors
    ///
    /// Each `PairsError`. The test `builtin_pairs` holds the file valid.
    pub fn builtin(owner: Owner) -> Result<Self, PairsError> {
        Self::parse(owner, BUILTIN)
    }

    /// The release workflow file of `repository`. `None` for a repository
    /// of another owner and for a repository with no line. The match is
    /// exact, byte for byte.
    #[must_use]
    pub fn workflow_for(&self, repository: &RepoFullName) -> Option<&WorkflowFile> {
        if repository.owner() != &self.owner {
            return None;
        }
        self.workflows.get(repository.name())
    }

    /// Each pair, sorted by repository name.
    pub fn pairs(&self) -> impl Iterator<Item = (&RepoName, &WorkflowFile)> {
        self.workflows.iter()
    }
}
