//! The readers of the Forgejo replies that the decision needs. This is the
//! one module that knows the field names of Forgejo 17.0.1. The rest of the
//! crate reads the typed facts.
//!
//! Each reader checks the status, the byte cap and the strict JSON check,
//! then reads only its named fields. An unknown field is ignored, as in
//! `handover/executor/provenance.py`.

use std::fmt;

use serde::Deserialize;

use crate::names::{CommitSha, RepoFullName, RunId, WorkflowFile};

/// The most bytes of one reply body: `API_MAX_BYTES` of
/// `handover/executor/provenance.py`, 2 MiB.
pub const REPLY_MAX_BYTES: usize = 2 * 1024 * 1024;

/// The status of a reply that holds the object.
const STATUS_OK: u16 = 200;

/// The status of `commits/{sha}/pull` for a commit that is no PR merge
/// (M5, measured).
const STATUS_NOT_FOUND: u16 = 404;

/// One HTTP reply of Forgejo: its status and its body. The HTTP client of a
/// later PR gives this value.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Reply {
    /// The HTTP status.
    pub status: u16,
    /// The body bytes.
    pub body: Vec<u8>,
}

/// Why a reply gives no fact.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ReplyFault {
    /// A status that the reader does not expect.
    Status(u16),
    /// A body above `REPLY_MAX_BYTES`.
    TooLarge,
    /// A body that is no strict JSON text.
    NotStrictJson,
    /// A field that is missing or wrong. The value names the field.
    Shape(&'static str),
}

impl ReplyFault {
    /// A stable word for the journal.
    #[must_use]
    pub fn code(self) -> &'static str {
        match self {
            Self::Status(_) => "status",
            Self::TooLarge => "too_large",
            Self::NotStrictJson => "not_strict_json",
            Self::Shape(_) => "shape",
        }
    }
}

impl fmt::Display for ReplyFault {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Status(status) => write!(formatter, "the reply has the status {status}"),
            Self::TooLarge => formatter.write_str("the reply is too large"),
            Self::NotStrictJson => formatter.write_str("the reply is no strict JSON text"),
            Self::Shape(field) => write!(formatter, "the reply field {field} is wrong"),
        }
    }
}

impl std::error::Error for ReplyFault {}

/// The body of a reply as `T`, after the cap and the strict check.
fn body_of<'a, T: Deserialize<'a>>(body: &'a [u8], name: &'static str) -> Result<T, ReplyFault> {
    if body.len() > REPLY_MAX_BYTES {
        return Err(ReplyFault::TooLarge);
    }
    if !creche_wire::strict(body) {
        return Err(ReplyFault::NotStrictJson);
    }
    serde_json::from_slice(body).map_err(|_| ReplyFault::Shape(name))
}

/// A field as `T`, or the fault that names the field.
fn field<T: std::str::FromStr>(value: Option<String>, name: &'static str) -> Result<T, ReplyFault> {
    value
        .and_then(|text| text.parse().ok())
        .ok_or(ReplyFault::Shape(name))
}

/// The `event` of a run reply.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Event {
    /// `push`.
    Push,
    /// `pull_request`: a `pull_request` run and a `pull_request_target` run.
    PullRequest,
    /// The empty text: a `workflow_dispatch` run (measured 2026-11-16).
    Empty,
    /// Any other text.
    Other,
}

/// The `trigger_event` of a run reply.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TriggerEvent {
    /// `push`.
    Push,
    /// `pull_request`.
    PullRequest,
    /// `pull_request_target`.
    PullRequestTarget,
    /// `workflow_dispatch`.
    WorkflowDispatch,
    /// Any other text.
    Other,
}

/// The `ref` of the `event_payload` of a run reply.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum FullRef {
    /// `refs/heads/<branch>`.
    Branch(String),
    /// `refs/tags/<tag>`.
    Tag(String),
    /// Any other text.
    Other,
}

impl FullRef {
    fn from_text(text: &str) -> Self {
        if let Some(branch) = text.strip_prefix("refs/heads/") {
            return Self::Branch(branch.to_owned());
        }
        if let Some(tag) = text.strip_prefix("refs/tags/") {
            return Self::Tag(tag.to_owned());
        }
        Self::Other
    }
}

/// The `status` of a run reply.
// CONTRACT-QUESTION: forgejo-release-path.md section 4.4 check 5 says "not
// finished". Every recorded run is `success`. The strings of a run that
// waits or runs are not measured on 17.0.1. The reading here takes the
// status words of the Forgejo API and refuses any other text as a fault
// (fail closed). The rehearsal of section 7 records the real value. A
// change is one match arm.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RunStatus {
    /// `waiting`.
    Waiting,
    /// `running`.
    Running,
    /// `blocked`.
    Blocked,
    /// `success`.
    Success,
    /// `failure`.
    Failure,
    /// `cancelled`.
    Cancelled,
    /// `skipped`.
    Skipped,
}

impl RunStatus {
    fn from_text(text: &str) -> Option<Self> {
        Some(match text {
            "waiting" => Self::Waiting,
            "running" => Self::Running,
            "blocked" => Self::Blocked,
            "success" => Self::Success,
            "failure" => Self::Failure,
            "cancelled" => Self::Cancelled,
            "skipped" => Self::Skipped,
            _ => return None,
        })
    }
}

/// The facts of `GET /repos/{o}/{r}/actions/runs/{id}` that section 4.4
/// reads.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RunReply {
    /// `id`: the API id of the run.
    pub id: RunId,
    /// `repository.full_name`.
    pub repository: RepoFullName,
    /// `workflow_id`: the workflow file name.
    pub workflow: WorkflowFile,
    /// `event`.
    pub event: Event,
    /// `trigger_event`.
    pub trigger: TriggerEvent,
    /// `ref` of the JSON text `event_payload`. `None` when the payload has
    /// no `ref`, as for a PR run.
    pub payload_ref: Option<FullRef>,
    /// `commit_sha`.
    pub commit: CommitSha,
    /// `status`.
    pub status: RunStatus,
}

#[derive(Deserialize)]
struct RawRepository {
    full_name: Option<String>,
}

#[derive(Deserialize)]
struct RawRun {
    id: Option<u64>,
    repository: Option<RawRepository>,
    workflow_id: Option<String>,
    event: Option<String>,
    trigger_event: Option<String>,
    event_payload: Option<String>,
    commit_sha: Option<String>,
    status: Option<String>,
}

#[derive(Deserialize)]
struct RawPayload {
    #[serde(rename = "ref")]
    ref_: Option<String>,
}

/// The run facts of a run reply. Only the status 200 passes.
///
/// # Errors
///
/// Each `ReplyFault`.
pub fn parse_run(reply: &Reply) -> Result<RunReply, ReplyFault> {
    if reply.status != STATUS_OK {
        return Err(ReplyFault::Status(reply.status));
    }
    let raw: RawRun = body_of(&reply.body, "run")?;

    let id = raw
        .id
        .and_then(|id| RunId::try_from(id).ok())
        .ok_or(ReplyFault::Shape("id"))?;
    let repository = field(
        raw.repository.and_then(|repo| repo.full_name),
        "repository.full_name",
    )?;
    let workflow = field(raw.workflow_id, "workflow_id")?;
    let commit = field(raw.commit_sha, "commit_sha")?;

    let event = match raw.event.ok_or(ReplyFault::Shape("event"))?.as_str() {
        "push" => Event::Push,
        "pull_request" => Event::PullRequest,
        "" => Event::Empty,
        _ => Event::Other,
    };
    let trigger = match raw
        .trigger_event
        .ok_or(ReplyFault::Shape("trigger_event"))?
        .as_str()
    {
        "push" => TriggerEvent::Push,
        "pull_request" => TriggerEvent::PullRequest,
        "pull_request_target" => TriggerEvent::PullRequestTarget,
        "workflow_dispatch" => TriggerEvent::WorkflowDispatch,
        _ => TriggerEvent::Other,
    };
    let status = raw
        .status
        .as_deref()
        .and_then(RunStatus::from_text)
        .ok_or(ReplyFault::Shape("status"))?;

    // `event_payload` is a JSON text inside a JSON string. It gets the same
    // strict check as a body.
    let payload = raw
        .event_payload
        .ok_or(ReplyFault::Shape("event_payload"))?;
    if !creche_wire::strict(payload.as_bytes()) {
        return Err(ReplyFault::Shape("event_payload"));
    }
    let payload: RawPayload =
        serde_json::from_str(&payload).map_err(|_| ReplyFault::Shape("event_payload"))?;
    let payload_ref = payload.ref_.as_deref().map(FullRef::from_text);

    Ok(RunReply {
        id,
        repository,
        workflow,
        event,
        trigger,
        payload_ref,
        commit,
        status,
    })
}

/// The facts of a PR that `commits/{sha}/pull` names.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PullFacts {
    /// `number`.
    pub number: u64,
    /// `merged`.
    pub merged: bool,
    /// `base.ref`: the branch name of the base.
    pub base_ref: String,
    /// `merge_commit_sha`. `None` for null or an empty text.
    pub merge_commit: Option<CommitSha>,
}

/// The answer of `GET /repos/{o}/{r}/commits/{sha}/pull` (M5, M17).
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum CommitPull {
    /// 200: the commit belongs to this PR.
    Merge(PullFacts),
    /// 404: the commit is no PR merge.
    NoPull,
}

#[derive(Deserialize)]
struct RawBase {
    #[serde(rename = "ref")]
    ref_: Option<String>,
}

#[derive(Deserialize)]
struct RawPull {
    number: Option<u64>,
    merged: Option<bool>,
    base: Option<RawBase>,
    merge_commit_sha: Option<String>,
}

/// The PR of a commit pull reply: 200 gives `Merge`, 404 gives `NoPull`.
///
/// # Errors
///
/// Each `ReplyFault`. The body of a 404 is not read.
pub fn parse_commit_pull(reply: &Reply) -> Result<CommitPull, ReplyFault> {
    if reply.status == STATUS_NOT_FOUND {
        return Ok(CommitPull::NoPull);
    }
    if reply.status != STATUS_OK {
        return Err(ReplyFault::Status(reply.status));
    }
    let raw: RawPull = body_of(&reply.body, "pull")?;

    let number = raw.number.ok_or(ReplyFault::Shape("number"))?;
    let merged = raw.merged.ok_or(ReplyFault::Shape("merged"))?;
    let base_ref = raw
        .base
        .and_then(|base| base.ref_)
        .ok_or(ReplyFault::Shape("base.ref"))?;
    let merge_commit = match raw.merge_commit_sha.as_deref() {
        None | Some("") => None,
        Some(text) => Some(
            text.parse()
                .map_err(|_| ReplyFault::Shape("merge_commit_sha"))?,
        ),
    };

    Ok(CommitPull::Merge(PullFacts {
        number,
        merged,
        base_ref,
        merge_commit,
    }))
}
