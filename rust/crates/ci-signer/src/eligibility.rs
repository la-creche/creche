//! Which run gets a statement (section 4.4, "The checks of the signer").
//!
//! The decision is a typestate in two reads:
//!
//! ```text
//!   RunKey + run reply ──decide_run──► RunChecked ──decide_merge──► Eligible
//!                       (mismatch, checks 1, 2, 3, 5)   (check 4)
//! ```
//!
//! `Eligible` has no public constructor. So only a run that passed every
//! check reaches `IssuedRuns::issue`, and the statement takes its facts
//! from Forgejo, never from the job or the runner (FR7).

use std::fmt;

use crate::forgejo::{
    CommitPull, Event, FullRef, Reply, ReplyFault, RunStatus, TriggerEvent, parse_commit_pull,
    parse_run,
};
use crate::names::{CommitSha, RepoFullName, RunId, WorkflowFile};
use crate::pairs::SigningPairs;

/// The one branch that a release comes from.
const MAIN: &str = "main";

/// The key of the signer's own read: the repository and the run id of the
/// `open` line. The listener of a later PR fills it from the run id field
/// of that line, never from its session string.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RunKey {
    /// The repository as `owner/name`.
    pub repository: RepoFullName,
    /// The API id of the run.
    pub run: RunId,
}

/// Why check 4 refuses a run.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum MergeRefusal {
    /// The commit is no PR merge (404).
    NoPull,
    /// The PR is not merged.
    NotMerged,
    /// The PR merged into another branch.
    OtherBase,
    /// The merge commit of the PR is not the commit of the run.
    OtherCommit,
}

/// Why a run gets no statement.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Refusal {
    /// A reply gives no fact.
    Fault(ReplyFault),
    /// The run reply names another run or another repository than the key.
    ReplyMismatch,
    /// Check 1: the repository and the workflow file are no signing pair.
    NotAPair,
    /// Check 2: `event` or `trigger_event` is not `push`.
    NotPush,
    /// Check 3: `event_payload.ref` is not `refs/heads/main`.
    NotMain,
    /// Check 5: the run is not waiting or running.
    Finished,
    /// Check 4: the commit is no merge of a PR into `main`.
    NotAMerge(MergeRefusal),
}

impl Refusal {
    /// A stable word for the journal and the answer. No text of a reply
    /// gets into it.
    #[must_use]
    pub fn code(self) -> &'static str {
        match self {
            Self::Fault(_) => "fault",
            Self::ReplyMismatch => "reply_mismatch",
            Self::NotAPair => "not_a_pair",
            Self::NotPush => "not_push",
            Self::NotMain => "not_main",
            Self::Finished => "finished",
            Self::NotAMerge(MergeRefusal::NoPull) => "no_pull",
            Self::NotAMerge(MergeRefusal::NotMerged) => "not_merged",
            Self::NotAMerge(MergeRefusal::OtherBase) => "other_base",
            Self::NotAMerge(MergeRefusal::OtherCommit) => "other_commit",
        }
    }
}

impl fmt::Display for Refusal {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Fault(fault) => write!(formatter, "fault: {fault}"),
            _ => formatter.write_str(self.code()),
        }
    }
}

impl std::error::Error for Refusal {}

/// A run that passed the mismatch check and checks 1, 2, 3 and 5. Check 4
/// needs one more read, of `commits/{commit}/pull`.
#[derive(Debug, PartialEq, Eq)]
pub struct RunChecked {
    repository: RepoFullName,
    workflow: WorkflowFile,
    commit: CommitSha,
    run: RunId,
}

impl RunChecked {
    /// The repository of the next read.
    #[must_use]
    pub fn repository(&self) -> &RepoFullName {
        &self.repository
    }

    /// The commit of the next read: `GET /repos/{o}/{r}/commits/{this}/pull`.
    #[must_use]
    pub fn commit(&self) -> &CommitSha {
        &self.commit
    }
}

/// A run that passed every check of section 4.4. It is not `Clone`: one
/// decision feeds one `IssuedRuns::issue`.
#[derive(Debug, PartialEq, Eq)]
pub struct Eligible {
    repository: RepoFullName,
    workflow: WorkflowFile,
    commit: CommitSha,
    run: RunId,
}

impl Eligible {
    /// The repository of the run.
    #[must_use]
    pub fn repository(&self) -> &RepoFullName {
        &self.repository
    }

    /// The workflow file of the run.
    #[must_use]
    pub fn workflow(&self) -> &WorkflowFile {
        &self.workflow
    }

    /// The commit of the run.
    #[must_use]
    pub fn commit(&self) -> &CommitSha {
        &self.commit
    }

    /// The run id.
    #[must_use]
    pub fn run(&self) -> RunId {
        self.run
    }

    /// The facts, by value, for `IssuedRuns::issue`.
    pub(crate) fn into_facts(self) -> (RepoFullName, WorkflowFile, CommitSha, RunId) {
        (self.repository, self.workflow, self.commit, self.run)
    }
}

/// The first part of the decision: the run reply of
/// `GET /repos/{o}/{r}/actions/runs/{key.run}`.
///
/// # Errors
///
/// The first `Refusal` in this order: `Fault`, `ReplyMismatch`, `NotAPair`
/// (check 1), `NotPush` (check 2), `NotMain` (check 3), `Finished`
/// (check 5).
pub fn decide_run(
    pairs: &SigningPairs,
    key: &RunKey,
    reply: &Reply,
) -> Result<RunChecked, Refusal> {
    let run = parse_run(reply).map_err(Refusal::Fault)?;

    // CONTRACT-QUESTION: section 4.4 says that the signer takes the run id
    // from the open line. The recorded run 48 has `index_in_repo` 23 in its
    // `html_url`. The reading here takes the API id and refuses a reply
    // whose id or repository differs from the key. The full name is
    // compared byte for byte. A change is this one check.
    if run.id != key.run || run.repository != key.repository {
        return Err(Refusal::ReplyMismatch);
    }

    if pairs.workflow_for(&run.repository) != Some(&run.workflow) {
        return Err(Refusal::NotAPair);
    }
    if run.event != Event::Push || run.trigger != TriggerEvent::Push {
        return Err(Refusal::NotPush);
    }
    if run.payload_ref != Some(FullRef::Branch(MAIN.to_owned())) {
        return Err(Refusal::NotMain);
    }
    if !matches!(run.status, RunStatus::Waiting | RunStatus::Running) {
        return Err(Refusal::Finished);
    }

    Ok(RunChecked {
        repository: run.repository,
        workflow: run.workflow,
        commit: run.commit,
        run: run.id,
    })
}

/// The second part of the decision: check 4 on the reply of
/// `GET /repos/{o}/{r}/commits/{checked.commit()}/pull`.
///
/// # Errors
///
/// `Fault`, or `NotAMerge` with the first failed condition.
pub fn decide_merge(checked: RunChecked, reply: &Reply) -> Result<Eligible, Refusal> {
    let pull = match parse_commit_pull(reply).map_err(Refusal::Fault)? {
        CommitPull::Merge(pull) => pull,
        CommitPull::NoPull => return Err(Refusal::NotAMerge(MergeRefusal::NoPull)),
    };

    if !pull.merged {
        return Err(Refusal::NotAMerge(MergeRefusal::NotMerged));
    }
    if pull.base_ref != MAIN {
        return Err(Refusal::NotAMerge(MergeRefusal::OtherBase));
    }
    if pull.merge_commit.as_ref() != Some(&checked.commit) {
        return Err(Refusal::NotAMerge(MergeRefusal::OtherCommit));
    }

    let RunChecked {
        repository,
        workflow,
        commit,
        run,
    } = checked;
    Ok(Eligible {
        repository,
        workflow,
        commit,
        run,
    })
}
