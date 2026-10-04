//! What stays of an autonomous job after `attendance` deletes its session: the
//! outcome record (contract 02 §13.1).

use std::error::Error;
use std::fmt;
use std::str::FromStr;

use serde::{Deserialize, Serialize, Serializer};

use super::error::TurnReason;
use super::fields::words;
use super::json::{self, EncodeError};
use super::state::Turn;
use super::time::{self, Timestamp};
use super::view::{FieldError, Trigger};
use crate::ids::{FamilyName, SandboxName, SessionId, Ulid};

words! {
    /// How a job ended: the `status` of an outcome record (contract 02 §13.1).
    ///
    /// The set is closed for the writer. A reader of a record that a newer
    /// `attendance` wrote can meet another word. It refuses the record.
    JobStatus,
    /// Why a text is not the status of a job.
    JobStatusError,
    "the status of a job",
    {
        /// The last turn settled.
        Ok => "ok",
        /// The last turn failed.
        Failed => "failed",
        /// The caller of a gated call went away.
        Denied => "denied",
        /// The deadline of the last turn came before its end.
        Timeout => "timeout",
        /// A caller or the platform stopped the last turn.
        Cancelled => "cancelled",
    }
}

impl JobStatus {
    /// The status of a job whose last turn failed for this reason (contract 02
    /// §13.1.2).
    #[must_use]
    pub const fn of_failure(reason: TurnReason) -> Self {
        match reason {
            TurnReason::ApprovalDenied => Self::Denied,
            TurnReason::ApprovalTimeout | TurnReason::TurnTimeout => Self::Timeout,
            TurnReason::ChannelLost
            | TurnReason::ProtocolViolation
            | TurnReason::PermissionRemoved
            | TurnReason::SandboxLost
            | TurnReason::ModelError
            | TurnReason::BudgetExceeded
            | TurnReason::UserStopped
            | TurnReason::QueueLost
            | TurnReason::PepUnreachable
            | TurnReason::Internal => Self::Failed,
        }
    }

    /// The status of a job, from the turn that ended it (contract 02 §13.1).
    /// `None` for a turn that did not end: the job did not end.
    #[must_use]
    pub fn of_last_turn(last: &Turn) -> Option<Self> {
        match last {
            Turn::Settled(_) => Some(Self::Ok),
            Turn::Aborted(_) => Some(Self::Cancelled),
            Turn::Failed(failed) => Some(Self::of_failure(failed.reason())),
            Turn::Queued | Turn::Running(_) | Turn::WaitingApproval(_) => None,
        }
    }
}

/// How the gates of one job resolved (contract 02 §13.1.1). Each number is a
/// count of audit records of the chaperone.
///
/// `approved`, `denied` and `timed_out` can sum to less than `requested`: a
/// gate that ended with no decision is in `requested` only.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub struct Approvals {
    /// The count of gates that opened.
    pub requested: u64,
    /// The count of gates that the phone approved.
    pub approved: u64,
    /// The count of gates that the phone denied, or that no phone saw.
    pub denied: u64,
    /// The count of gates with no phone decision in time.
    pub timed_out: u64,
}

/// What the turns of one job cost (contract 02 §13.1).
#[derive(Debug, Clone, PartialEq)]
pub enum Spend {
    /// The sum of the costs of the turns, in US dollars.
    Known(f64),
    /// No cost is known: the turns report tokens and a total cost of zero.
    /// The text says why.
    Unknown(String),
}

/// The cap of the error text of an outcome record, in bytes of UTF-8: 4 KiB
/// (contract 02 §13.1).
const ERROR_MAX_BYTES: usize = 4_096;

const OUTCOME: &str = "an outcome record";

/// The fields of an [`OutcomeRecord`], before the check.
#[derive(Debug, Clone, PartialEq)]
pub struct RawOutcome {
    /// The id of the record, and the name of its file.
    pub id: Ulid,
    /// The family of the job.
    pub family: FamilyName,
    /// The session of the job. `attendance` deleted it.
    pub session: SessionId,
    /// The firing that started the job.
    pub trigger: Option<Trigger>,
    /// When the first turn started.
    pub started_at: Timestamp,
    /// When the job ended.
    pub ended_at: Timestamp,
    /// How the job ended.
    pub status: JobStatus,
    /// The error text.
    pub error: Option<String>,
    /// The count of turns.
    pub turns: u64,
    /// How the gates resolved.
    pub approvals: Approvals,
    /// What the turns cost.
    pub spend: Spend,
    /// The sandbox that served the job.
    pub sandbox: Option<SandboxName>,
}

/// One finished autonomous job (contract 02 §13.1).
///
/// The error text has 4 KiB at most. The cost is a finite number that is zero
/// or more.
///
/// ```
/// use creche_contracts::session::{Approvals, JobStatus, OutcomeRecord, RawOutcome, Spend};
///
/// let record = OutcomeRecord::try_from(RawOutcome {
///     id: "01JBQ80M4F7S2YQ1VZK6W3TDEN".parse()?,
///     family: "scrum-lead".parse()?,
///     session: "auto-01JBQ7ZZ9D6M0Q4RXT2J8HYVBK".parse()?,
///     trigger: None,
///     started_at: "2026-10-06T06:00:01Z".parse()?,
///     ended_at: "2026-10-06T06:05:12Z".parse()?,
///     status: JobStatus::Ok,
///     error: None,
///     turns: 3,
///     approvals: Approvals { requested: 0, approved: 0, denied: 0, timed_out: 0 },
///     spend: Spend::Known(0.21),
///     sandbox: Some("scrum-lead-s2".parse()?),
/// })?;
/// assert_eq!(record.status(), JobStatus::Ok);
/// # Ok::<(), Box<dyn std::error::Error>>(())
/// ```
///
/// Code outside this module cannot build a value from raw fields:
///
/// ```compile_fail,E0451
/// use creche_contracts::session::OutcomeRecord;
///
/// fn recount(record: OutcomeRecord) -> OutcomeRecord {
///     OutcomeRecord { turns: 0, ..record }
/// }
/// ```
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct OutcomeRecord {
    id: Ulid,
    family: FamilyName,
    session: SessionId,
    trigger: Option<Trigger>,
    #[serde(serialize_with = "time::seconds")]
    started_at: Timestamp,
    #[serde(serialize_with = "time::seconds")]
    ended_at: Timestamp,
    status: JobStatus,
    error: Option<String>,
    turns: u64,
    approvals: Approvals,
    #[serde(flatten)]
    spend: SpendFields,
    #[serde(serialize_with = "sandbox_or_empty")]
    sandbox: Option<SandboxName>,
}

/// The two members that a [`Spend`] is in the file.
#[derive(Debug, Clone, PartialEq, Serialize)]
struct SpendFields {
    spend_usd: Option<f64>,
    spend_reason: Option<String>,
}

impl From<Spend> for SpendFields {
    fn from(spend: Spend) -> Self {
        match spend {
            Spend::Known(usd) => Self {
                spend_usd: Some(usd),
                spend_reason: None,
            },
            Spend::Unknown(reason) => Self {
                spend_usd: None,
                spend_reason: Some(reason),
            },
        }
    }
}

/// Writes the sandbox of a job. A job that no sandbox served has the empty
/// text there, as the Python code writes it.
fn sandbox_or_empty<S: Serializer>(
    sandbox: &Option<SandboxName>,
    serializer: S,
) -> Result<S::Ok, S::Error> {
    serializer.serialize_str(sandbox.as_ref().map_or("", SandboxName::as_str))
}

impl TryFrom<RawOutcome> for OutcomeRecord {
    type Error = FieldError;

    fn try_from(raw: RawOutcome) -> Result<Self, Self::Error> {
        if raw
            .error
            .as_ref()
            .is_some_and(|error| error.len() > ERROR_MAX_BYTES)
        {
            return Err(FieldError::new(OUTCOME, "error"));
        }

        if matches!(raw.spend, Spend::Known(usd) if !usd.is_finite() || usd < 0.0) {
            return Err(FieldError::new(OUTCOME, "spend_usd"));
        }

        Ok(Self {
            id: raw.id,
            family: raw.family,
            session: raw.session,
            trigger: raw.trigger,
            started_at: raw.started_at,
            ended_at: raw.ended_at,
            status: raw.status,
            error: raw.error,
            turns: raw.turns,
            approvals: raw.approvals,
            spend: raw.spend.into(),
            sandbox: raw.sandbox,
        })
    }
}

impl OutcomeRecord {
    /// The id of the record, and the name of its file.
    #[must_use]
    pub fn id(&self) -> &Ulid {
        &self.id
    }

    /// The family of the job.
    #[must_use]
    pub fn family(&self) -> &FamilyName {
        &self.family
    }

    /// How the job ended.
    #[must_use]
    pub fn status(&self) -> JobStatus {
        self.status
    }

    /// The bytes of the file of the record: one JSON object and one LF.
    pub fn encode(&self) -> Result<Vec<u8>, EncodeError> {
        json::encode_line(self)
    }
}

/// The longest start of `error` that is 4 KiB or less and ends between two
/// characters: the error text of an outcome record (contract 02 §13.1).
#[must_use]
pub fn cut_error(error: &str) -> &str {
    let mut end = error.len().min(ERROR_MAX_BYTES);
    while !error.is_char_boundary(end) {
        end = end.saturating_sub(1);
    }

    error.get(..end).unwrap_or(error)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::session::{Run, Step};

    fn at(text: &str) -> Timestamp {
        text.parse().unwrap()
    }

    fn raw() -> RawOutcome {
        RawOutcome {
            id: "01JBQ80M4F7S2YQ1VZK6W3TDEN".parse().unwrap(),
            family: "scrum-lead".parse().unwrap(),
            session: "auto-01JBQ7ZZ9D6M0Q4RXT2J8HYVBK".parse().unwrap(),
            trigger: None,
            started_at: at("2026-10-06T06:00:01Z"),
            ended_at: at("2026-10-06T06:05:12Z"),
            status: JobStatus::Ok,
            error: None,
            turns: 3,
            approvals: Approvals {
                requested: 1,
                approved: 1,
                denied: 0,
                timed_out: 0,
            },
            spend: Spend::Known(0.21),
            sandbox: None,
        }
    }

    #[test]
    fn a_record_is_one_line_with_the_fields_in_the_order_of_the_contract() {
        let record = OutcomeRecord::try_from(raw()).unwrap();

        assert_eq!(
            String::from_utf8(record.encode().unwrap()).unwrap(),
            concat!(
                r#"{"id":"01JBQ80M4F7S2YQ1VZK6W3TDEN","family":"scrum-lead","#,
                r#""session":"auto-01JBQ7ZZ9D6M0Q4RXT2J8HYVBK","trigger":null,"#,
                r#""started_at":"2026-10-06T06:00:01Z","ended_at":"2026-10-06T06:05:12Z","#,
                r#""status":"ok","error":null,"turns":3,"#,
                r#""approvals":{"requested":1,"approved":1,"denied":0,"timed_out":0},"#,
                r#""spend_usd":0.21,"spend_reason":null,"sandbox":""}"#,
                "\n"
            )
        );
        assert_eq!(record.status(), JobStatus::Ok);
    }

    #[test]
    fn a_long_error_and_a_bad_cost_are_refused() {
        let long = RawOutcome {
            error: Some("e".repeat(ERROR_MAX_BYTES + 1)),
            ..raw()
        };
        let at_the_cap = RawOutcome {
            error: Some("e".repeat(ERROR_MAX_BYTES)),
            ..raw()
        };

        assert_eq!(OutcomeRecord::try_from(long).unwrap_err().field(), "error");
        assert!(OutcomeRecord::try_from(at_the_cap).is_ok());
        for usd in [f64::NAN, f64::INFINITY, -1.0] {
            let bad = RawOutcome {
                spend: Spend::Known(usd),
                ..raw()
            };

            assert_eq!(
                OutcomeRecord::try_from(bad).unwrap_err().field(),
                "spend_usd"
            );
        }
    }

    #[test]
    fn an_error_is_cut_between_two_characters() {
        let wide = "\u{e9}".repeat(ERROR_MAX_BYTES);
        let odd = format!("a{wide}");

        assert_eq!(cut_error("short"), "short");
        assert_eq!(cut_error(&wide).len(), ERROR_MAX_BYTES);
        assert_eq!(cut_error(&odd).len(), ERROR_MAX_BYTES - 1);
        assert!(
            OutcomeRecord::try_from(RawOutcome {
                error: Some(cut_error(&odd).to_owned()),
                ..raw()
            })
            .is_ok()
        );
    }

    #[test]
    fn a_job_with_a_turn_in_flight_has_no_status() {
        let run = || Run::new(at("2026-10-06T06:00:01Z"), "scrum-lead-s2".parse().unwrap());
        let running = Turn::Running(run());
        let failed = running
            .clone()
            .apply(Step::Fail {
                at: at("2026-10-06T06:05:12Z"),
                reason: TurnReason::TurnTimeout,
            })
            .unwrap();

        assert_eq!(JobStatus::of_last_turn(&Turn::Queued), None);
        assert_eq!(JobStatus::of_last_turn(&running), None);
        assert_eq!(JobStatus::of_last_turn(&failed), Some(JobStatus::Timeout));
    }
}
