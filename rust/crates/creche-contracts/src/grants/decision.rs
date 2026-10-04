//! The words of a decision (contract 04 §5, §6.1), and the proof that a
//! decision allowed a call.
//!
//! This module holds no decision logic. The port of the chaperone adds the
//! decision function. The types here are what that function gives and what
//! the audit record writes.

use std::error::Error;
use std::fmt;
use std::str::FromStr;

use serde::{Deserialize, Serialize};

use crate::ids::{FamilyName, ServerName, ToolName};

use super::body::Arguments;
use super::file::{GrantsRev, Verb};

/// The `decision` of one audit record (contract 04 §6.1).
///
/// The set is closed for one release of the chaperone. A reader of an audit
/// file that must show a record of a newer chaperone keeps the text of a
/// value that it does not know. This type refuses such a value.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Decision {
    /// The chaperone allowed the call.
    Allow,
    /// The chaperone denied the call.
    Deny,
    /// A gate is open for the call. A second record follows (contract 04
    /// §6.4).
    Pending,
}

impl Decision {
    /// Each decision.
    pub const ALL: [Self; 3] = [Self::Allow, Self::Deny, Self::Pending];

    /// The word on the wire.
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Allow => "allow",
            Self::Deny => "deny",
            Self::Pending => "pending",
        }
    }
}

impl fmt::Display for Decision {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_str())
    }
}

/// Why an answer of the chaperone is not a result: the `reason` of an answer
/// that is not HTTP 200 (contract 04 §5, §5.1, §6.1).
///
/// Each reason has one HTTP status. The set is closed for one release of the
/// chaperone, and a sandbox can run an older client. A client decides on the
/// HTTP status and shows the reason as text (contract 04 §5.1), so a client
/// does not need this type to read an answer. This type refuses a value that
/// it does not know.
///
/// ```
/// use creche_contracts::grants::Reason;
///
/// let reason: Reason = "tool_not_granted".parse()?;
/// assert_eq!(reason, Reason::ToolNotGranted);
/// assert_eq!(reason.http_status(), 403);
/// # Ok::<(), creche_contracts::grants::UnknownReason>(())
/// ```
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Reason {
    /// Row 1: no bearer, an unknown digest, or no valid grant file.
    UnknownToken,
    /// Rows 2, 7 and 8: over a limit of the family.
    RateLimited,
    /// Rows 3 and 5: an argument fails a fence or the schema.
    ArgValidation,
    /// Rows 4 and 6: the family does not hold the tool or the target.
    ToolNotGranted,
    /// Row 5a: the family holds the tool, and this chaperone cannot execute
    /// it.
    NotImplemented,
    /// Row 11: the upstream failed after the chaperone allowed the call.
    UpstreamFailed,
    /// Row 12: a fault that the chaperone did not expect.
    InternalError,
    /// The delegate did not answer in 120 seconds (contract 04 §7.5).
    DelegateTimeout,
    /// The operator denied the gate (contract 04 §8.4).
    ApprovalDenied,
    /// The operator did not answer in 15 minutes (contract 04 §8.5).
    ApprovalTimeout,
    /// The grant went away while the gate was open (contract 04 §1.5).
    ApprovalRevoked,
    /// The chaperone could not send the gate to the operator (contract 04
    /// §8.4).
    ApprovalUndeliverable,
    /// The caller went away while the gate was open (contract 04 §8.7).
    ApprovalAbandoned,
}

impl Reason {
    /// Each reason.
    pub const ALL: [Self; 13] = [
        Self::UnknownToken,
        Self::RateLimited,
        Self::ArgValidation,
        Self::ToolNotGranted,
        Self::NotImplemented,
        Self::UpstreamFailed,
        Self::InternalError,
        Self::DelegateTimeout,
        Self::ApprovalDenied,
        Self::ApprovalTimeout,
        Self::ApprovalRevoked,
        Self::ApprovalUndeliverable,
        Self::ApprovalAbandoned,
    ];

    /// The code on the wire.
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::UnknownToken => "unknown_token",
            Self::RateLimited => "rate_limited",
            Self::ArgValidation => "arg_validation",
            Self::ToolNotGranted => "tool_not_granted",
            Self::NotImplemented => "not_implemented",
            Self::UpstreamFailed => "upstream_failed",
            Self::InternalError => "internal_error",
            Self::DelegateTimeout => "delegate_timeout",
            Self::ApprovalDenied => "approval_denied",
            Self::ApprovalTimeout => "approval_timeout",
            Self::ApprovalRevoked => "approval_revoked",
            Self::ApprovalUndeliverable => "approval_undeliverable",
            Self::ApprovalAbandoned => "approval_abandoned",
        }
    }

    /// The HTTP status of an answer with this reason (contract 04 §5).
    #[must_use]
    pub const fn http_status(self) -> u16 {
        match self {
            Self::ArgValidation => 400,
            Self::UnknownToken
            | Self::ToolNotGranted
            | Self::ApprovalDenied
            | Self::ApprovalTimeout
            | Self::ApprovalRevoked
            | Self::ApprovalUndeliverable
            | Self::ApprovalAbandoned => 403,
            Self::RateLimited => 429,
            Self::InternalError => 500,
            Self::NotImplemented => 501,
            Self::UpstreamFailed => 502,
            Self::DelegateTimeout => 504,
        }
    }
}

impl fmt::Display for Reason {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_str())
    }
}

impl FromStr for Reason {
    type Err = UnknownReason;

    fn from_str(text: &str) -> Result<Self, Self::Err> {
        Self::ALL
            .into_iter()
            .find(|reason| reason.as_str() == text)
            .ok_or(UnknownReason)
    }
}

/// A text that is not a reason of this release.
///
/// The error does not hold the text. The text is untrusted, and a caller
/// writes this error to a log.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct UnknownReason;

impl fmt::Display for UnknownReason {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("the text is not a reason of contract 04")
    }
}

impl Error for UnknownReason {}

/// How an allowed call failed (contract 04 §5 row 11, §7.5).
///
/// The audit record of such a call says `allow`, because the effect can have
/// occurred. The answer to the caller is not HTTP 200.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum AfterAllow {
    /// The upstream failed.
    UpstreamFailed,
    /// The delegate did not answer in time.
    DelegateTimeout,
}

impl AfterAllow {
    /// The reason of the answer to the caller, and of the audit record.
    #[must_use]
    pub const fn reason(self) -> Reason {
        match self {
            Self::UpstreamFailed => Reason::UpstreamFailed,
            Self::DelegateTimeout => Reason::DelegateTimeout,
        }
    }
}

/// The `reason` of the record of an allowed call with no gate (contract 04
/// §6.1).
const REASON_GRANTED: &str = "granted";

/// The `reason` of the record of a call that the operator approved.
const REASON_APPROVED: &str = "approved";

// CONTRACT-QUESTION: contract 04 §6.4 names no reason for the first record of
// a gated call, and §6.1 does not list one. The Python chaperone writes
// `approval_required`, and this type writes the same word. A change costs the
// one constant here, and a reader of the audit then sees two words for one
// fact.
/// The `reason` of the first record of a gated call.
const REASON_PENDING: &str = "approval_required";

/// The `decision` and the `reason` of one audit record, as one value
/// (contract 04 §6.1, §6.4).
///
/// The type holds each pair that the chaperone writes, and no other pair: a
/// record cannot say `deny` with the reason `granted`.
///
/// ```
/// use creche_contracts::grants::{AuditOutcome, Decision, Reason};
///
/// let outcome = AuditOutcome::Denied(Reason::RateLimited);
/// assert_eq!(outcome.decision(), Decision::Deny);
/// assert_eq!(outcome.reason(), "rate_limited");
/// ```
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum AuditOutcome {
    /// `allow`, `granted`: the call ran with no gate. A manifest fetch has
    /// this outcome too.
    Granted,
    /// `allow`, `approved`: the operator approved the gate, and the call ran.
    Approved,
    /// `allow` with the reason of the failure: the chaperone allowed the
    /// call, and the execution failed.
    Failed(AfterAllow),
    /// `pending`, `approval_required`: the gate of the call is open.
    Pending,
    /// `deny` with its reason.
    Denied(Reason),
}

impl AuditOutcome {
    /// The `decision` of the record.
    #[must_use]
    pub const fn decision(self) -> Decision {
        match self {
            Self::Granted | Self::Approved | Self::Failed(_) => Decision::Allow,
            Self::Pending => Decision::Pending,
            Self::Denied(_) => Decision::Deny,
        }
    }

    /// The `reason` of the record.
    #[must_use]
    pub const fn reason(self) -> &'static str {
        match self {
            Self::Granted => REASON_GRANTED,
            Self::Approved => REASON_APPROVED,
            Self::Failed(failure) => failure.reason().as_str(),
            Self::Pending => REASON_PENDING,
            Self::Denied(reason) => reason.as_str(),
        }
    }
}

// --- the witness ---

/// Who executes an allowed call (contract 04 §5 rows 9 and 10).
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Executor {
    /// An MCP server that this chaperone loaded, and one tool of it.
    Mcp {
        /// The server.
        server: ServerName,
        /// The tool, with no server name before it.
        tool: ToolName,
    },
    /// The chaperone itself: one verb of the catalog.
    Verb(Verb),
    /// The delegate door: `invoke_agent`.
    Delegate,
}

/// Whether an allowed call waits for the operator (contract 04 §5 row 9, §8).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum Gate {
    /// The action is not in `approval`. The call runs at once.
    NotGated,
    /// The action is in `approval`. The call blocks until the operator
    /// approves it.
    Gated,
}

/// The proof that the decision function allowed one call (contract 04 §5).
///
/// This type is a sketch for the port of the chaperone. It states the design
/// and holds no logic:
///
/// 1. The decision function is the only code that builds a value. The type
///    has no constructor, no `Default`, no `Clone` and no `Deserialize`. The
///    port puts the decision function in this module, or moves this type to
///    the module of that function. In each other module the fields are
///    private, so no other code can build a value.
/// 2. The function that executes a call takes an `Allowed` by value. A call
///    that no decision allowed cannot reach an upstream, because no code can
///    name its arguments to the executor.
/// 3. The value moves into the executor. One decision executes one time.
/// 4. The value holds what the decision checked: the arguments, the executor
///    and the revision of the grant file. The executor reads them from the
///    value and not from the request, so the call that runs is the call that
///    the decision allowed.
///
/// What the type does not prove: that the grant file is still on the disk
/// when the call runs. A removal applies to the next call (contract 04 §1.5).
///
/// Code outside this module reads a value and cannot build one:
///
/// ```
/// use creche_contracts::grants::{Allowed, Gate};
///
/// fn dispatch(call: Allowed) -> bool {
///     call.gate() == Gate::NotGated
/// }
/// ```
///
/// ```compile_fail,E0451
/// use creche_contracts::grants::{Allowed, Arguments, Executor, Gate};
///
/// let call = Allowed {
///     family: "chat".parse().unwrap(),
///     grants_rev: "reg-9f21c4".parse().unwrap(),
///     executor: Executor::Delegate,
///     args: Arguments::empty(),
///     gate: Gate::NotGated,
/// };
/// ```
///
/// A value has no copy, so one decision cannot execute two times:
///
/// ```compile_fail,E0599
/// use creche_contracts::grants::Allowed;
///
/// fn twice(call: Allowed) -> (Allowed, Allowed) {
///     (call.clone(), call)
/// }
/// ```
#[derive(Debug)]
pub struct Allowed {
    family: FamilyName,
    grants_rev: GrantsRev,
    executor: Executor,
    args: Arguments,
    gate: Gate,
}

impl Allowed {
    /// The family that the bearer of the call named.
    #[must_use]
    pub fn family(&self) -> &FamilyName {
        &self.family
    }

    /// The revision of the grant file that the decision read.
    #[must_use]
    pub fn grants_rev(&self) -> &GrantsRev {
        &self.grants_rev
    }

    /// Who executes the call.
    #[must_use]
    pub fn executor(&self) -> &Executor {
        &self.executor
    }

    /// The arguments that the decision checked.
    #[must_use]
    pub fn args(&self) -> &Arguments {
        &self.args
    }

    /// Whether the call waits for the operator.
    #[must_use]
    pub fn gate(&self) -> Gate {
        self.gate
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn each_reason_reads_and_writes_its_wire_code() {
        for reason in Reason::ALL {
            let wire = serde_json::to_string(&reason).unwrap();

            assert_eq!(wire, format!("\"{reason}\""));
            assert_eq!(serde_json::from_str::<Reason>(&wire).unwrap(), reason);
            assert_eq!(reason.as_str().parse(), Ok(reason));
        }
    }

    #[test]
    fn a_text_that_is_not_a_reason_is_refused() {
        for text in [
            "",
            "granted",
            "approved",
            "approval_required",
            "body_too_large",
            "Unknown_Token",
            "unknown_token\n",
            " unknown_token",
        ] {
            assert_eq!(text.parse::<Reason>(), Err(UnknownReason), "{text:?}");
        }

        assert!(serde_json::from_str::<Reason>("\"granted\"").is_err());
        assert_eq!(
            UnknownReason.to_string(),
            "the text is not a reason of contract 04"
        );
    }

    #[test]
    fn each_decision_reads_and_writes_its_wire_word() {
        for decision in Decision::ALL {
            let wire = serde_json::to_string(&decision).unwrap();

            assert_eq!(wire, format!("\"{decision}\""));
            assert_eq!(serde_json::from_str::<Decision>(&wire).unwrap(), decision);
        }

        assert!(serde_json::from_str::<Decision>("\"Allow\"").is_err());
        assert!(serde_json::from_str::<Decision>("\"maybe\"").is_err());
    }

    #[test]
    fn an_outcome_is_one_decision_and_one_reason() {
        let pairs = [
            (AuditOutcome::Granted, Decision::Allow, "granted"),
            (AuditOutcome::Approved, Decision::Allow, "approved"),
            (
                AuditOutcome::Failed(AfterAllow::UpstreamFailed),
                Decision::Allow,
                "upstream_failed",
            ),
            (
                AuditOutcome::Failed(AfterAllow::DelegateTimeout),
                Decision::Allow,
                "delegate_timeout",
            ),
            (
                AuditOutcome::Pending,
                Decision::Pending,
                "approval_required",
            ),
            (
                AuditOutcome::Denied(Reason::ApprovalTimeout),
                Decision::Deny,
                "approval_timeout",
            ),
        ];
        for (outcome, decision, reason) in pairs {
            assert_eq!(outcome.decision(), decision);
            assert_eq!(outcome.reason(), reason);
        }
    }

    #[test]
    fn the_test_module_can_build_the_witness_and_read_it() {
        // Only this module can write the literal. The decision function of
        // the port will be the one place that does.
        let call = Allowed {
            family: "chat".parse().unwrap(),
            grants_rev: "reg-9f21c4".parse().unwrap(),
            executor: Executor::Verb(Verb::Embed),
            args: Arguments::empty(),
            gate: Gate::Gated,
        };

        assert_eq!(call.family().as_str(), "chat");
        assert_eq!(call.grants_rev().as_str(), "reg-9f21c4");
        assert_eq!(call.executor(), &Executor::Verb(Verb::Embed));
        assert!(call.args().as_map().is_empty());
        assert_eq!(call.gate(), Gate::Gated);
    }
}
