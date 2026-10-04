//! The states of a session and of a turn, and the moves of a turn (contract
//! 02 §4).
//!
//! The module holds data and no service logic. [`Turn`] is one turn as the
//! state that it is in. [`Turn::apply`] is the one way from a state to the
//! next one, and a move that the contract does not permit is an error value.

use std::error::Error;
use std::fmt;
use std::str::FromStr;

use serde::{Deserialize, Serialize};

use super::error::TurnReason;
use super::fields::words;
use super::time::Timestamp;
use crate::ids::SandboxName;

words! {
    /// The kind of a family, copied to each of its sessions (contract 02
    /// §4.2).
    ///
    /// The set is closed. `attendance` refuses another word in a query. A door
    /// that reads another word in an answer refuses the answer.
    SessionKind,
    /// Why a text is not the kind of a session.
    SessionKindError,
    "the kind of a session",
    {
        /// A person talks to the family.
        Attended => "attended",
        /// The family runs one job for another family, then forgets it.
        Thin => "thin",
        /// A timer, a webhook or another family starts the family.
        Autonomous => "autonomous",
    }
}

words! {
    /// The state of a session (contract 02 §4.1).
    ///
    /// The set is closed. `attendance` refuses another word in a query. The
    /// Python terminal door reads another word in an answer as an unknown
    /// state. This type has no such variant: a reader refuses the answer.
    SessionState,
    /// Why a text is not the state of a session.
    SessionStateError,
    "the state of a session",
    {
        /// No turn is in flight.
        Idle => "idle",
        /// A turn of the session is in the queue.
        Queued => "queued",
        /// A turn runs.
        Running => "running",
        /// A turn of the session has the state `waiting-approval`.
        WaitingApproval => "waiting-approval",
        /// The newest turn that ended is `failed`, and no turn came after it.
        Failed => "failed",
    }
}

words! {
    /// The state of a turn (contract 02 §4.3).
    ///
    /// The set is closed. A reader refuses a turn with another word.
    TurnState,
    /// Why a text is not the state of a turn.
    TurnStateError,
    "the state of a turn",
    {
        /// The turn is in the queue of its family.
        Queued => "queued",
        /// pi runs the turn.
        Running => "running",
        /// The chaperone holds a tool call of the turn until a person decides.
        WaitingApproval => "waiting-approval",
        /// pi settled. This is the only success state.
        Settled => "settled",
        /// The turn ended and did not settle.
        Failed => "failed",
        /// A caller or the platform stopped the turn.
        Aborted => "aborted",
    }
}

impl TurnState {
    /// Whether a turn in this state never moves again.
    #[must_use]
    pub const fn is_terminal(self) -> bool {
        matches!(self, Self::Settled | Self::Failed | Self::Aborted)
    }
}

/// Whether the last turn of a session that ended has the state `failed`, and
/// no turn started after it. That state of a session stays until the next turn
/// starts (contract 02 §4.1).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum LastEnded {
    /// The last turn that ended has the state `failed`.
    Failed,
    /// No turn ended, or the last one that ended did not fail.
    NotFailed,
}

/// The state of a session, from the states of its turns (contract 02 §4.1).
/// The checks run in order. The first one that holds gives the state.
#[must_use]
pub fn session_state(turns: &[TurnState], last_ended: LastEnded) -> SessionState {
    if turns.contains(&TurnState::WaitingApproval) {
        return SessionState::WaitingApproval;
    }

    if turns.contains(&TurnState::Running) {
        return SessionState::Running;
    }

    if turns.contains(&TurnState::Queued) {
        return SessionState::Queued;
    }

    match last_ended {
        LastEnded::Failed => SessionState::Failed,
        LastEnded::NotFailed => SessionState::Idle,
    }
}

/// What a turn that runs has: when it started, and the sandbox that serves it.
///
/// ```
/// use creche_contracts::session::{Run, Timestamp};
///
/// let run = Run::new("2026-10-05T19:22:05Z".parse::<Timestamp>()?, "chat-s2".parse()?);
/// assert_eq!(run.sandbox().as_str(), "chat-s2");
/// # Ok::<(), Box<dyn std::error::Error>>(())
/// ```
///
/// Code outside this module cannot change the fields of a run:
///
/// ```compile_fail,E0451
/// use creche_contracts::session::{Run, Timestamp};
///
/// fn restart(run: Run, started_at: Timestamp) -> Run {
///     Run { started_at, ..run }
/// }
/// ```
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Run {
    started_at: Timestamp,
    sandbox: SandboxName,
}

impl Run {
    /// A run that started at `started_at` on `sandbox`.
    #[must_use]
    pub fn new(started_at: Timestamp, sandbox: SandboxName) -> Self {
        Self {
            started_at,
            sandbox,
        }
    }

    /// When `attendance` sent `start_turn`.
    #[must_use]
    pub fn started_at(&self) -> Timestamp {
        self.started_at
    }

    /// The sandbox that serves the turn.
    #[must_use]
    pub fn sandbox(&self) -> &SandboxName {
        &self.sandbox
    }
}

/// What a settled turn has: its run, and when it ended. Only [`Turn::apply`]
/// makes one.
///
/// ```
/// use creche_contracts::session::{SettledTurn, Timestamp};
///
/// fn ended_at(settled: &SettledTurn) -> Timestamp {
///     settled.ended_at()
/// }
/// ```
///
/// Code outside this module cannot change the fields of a settled turn:
///
/// ```compile_fail,E0451
/// use creche_contracts::session::{SettledTurn, Timestamp};
///
/// fn ended_at(settled: SettledTurn, ended_at: Timestamp) -> SettledTurn {
///     SettledTurn { ended_at, ..settled }
/// }
/// ```
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SettledTurn {
    run: Run,
    ended_at: Timestamp,
}

impl SettledTurn {
    /// The run of the turn.
    #[must_use]
    pub fn run(&self) -> &Run {
        &self.run
    }

    /// When the turn ended.
    #[must_use]
    pub fn ended_at(&self) -> Timestamp {
        self.ended_at
    }
}

/// What a turn that failed has: its run, when it ended, and the reason. Only
/// [`Turn::apply`] makes one.
///
/// ```
/// use creche_contracts::session::{FailedTurn, TurnReason};
///
/// fn reason(failed: &FailedTurn) -> TurnReason {
///     failed.reason()
/// }
/// ```
///
/// Code outside this module cannot change the fields of a turn that failed:
///
/// ```compile_fail,E0451
/// use creche_contracts::session::{FailedTurn, TurnReason};
///
/// fn reason(failed: FailedTurn, reason: TurnReason) -> FailedTurn {
///     FailedTurn { reason, ..failed }
/// }
/// ```
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FailedTurn {
    run: Run,
    ended_at: Timestamp,
    reason: TurnReason,
}

impl FailedTurn {
    /// The run of the turn.
    #[must_use]
    pub fn run(&self) -> &Run {
        &self.run
    }

    /// When the turn ended.
    #[must_use]
    pub fn ended_at(&self) -> Timestamp {
        self.ended_at
    }

    /// Why the turn failed.
    #[must_use]
    pub fn reason(&self) -> TurnReason {
        self.reason
    }
}

/// What a turn that stopped has: its run when it had one, when it ended, and
/// the reason. Only [`Turn::apply`] makes one.
///
/// ```
/// use creche_contracts::session::{AbortedTurn, TurnReason};
///
/// fn reason(aborted: &AbortedTurn) -> TurnReason {
///     aborted.reason()
/// }
/// ```
///
/// Code outside this module cannot change the fields of a turn that stopped:
///
/// ```compile_fail,E0451
/// use creche_contracts::session::{AbortedTurn, TurnReason};
///
/// fn reason(aborted: AbortedTurn, reason: TurnReason) -> AbortedTurn {
///     AbortedTurn { reason, ..aborted }
/// }
/// ```
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AbortedTurn {
    run: Option<Run>,
    ended_at: Timestamp,
    reason: TurnReason,
}

impl AbortedTurn {
    /// The run of the turn. A turn that stopped in the queue has no run.
    #[must_use]
    pub fn run(&self) -> Option<&Run> {
        self.run.as_ref()
    }

    /// When the turn ended.
    #[must_use]
    pub fn ended_at(&self) -> Timestamp {
        self.ended_at
    }

    /// Why the turn stopped.
    #[must_use]
    pub fn reason(&self) -> TurnReason {
        self.reason
    }
}

/// One turn, as the state that it is in (contract 02 §4.3).
///
/// A value starts as [`Turn::Queued`] or as [`Turn::Running`]. Each later
/// state comes from [`Turn::apply`]: code outside this module cannot build a
/// turn that waits for an approval, or a turn that ended.
///
/// ```
/// use creche_contracts::session::{Run, Step, Timestamp, Turn, TurnState};
///
/// let started: Timestamp = "2026-10-05T19:22:05Z".parse()?;
/// let ended: Timestamp = "2026-10-05T19:22:31Z".parse()?;
/// let turn = Turn::Queued
///     .apply(Step::Start(Run::new(started, "chat-s2".parse()?)))?
///     .apply(Step::Settle { at: ended })?;
/// assert_eq!(turn.state(), TurnState::Settled);
///
/// let refused = turn.apply(Step::OpenGate);
/// assert!(refused.is_err());
/// # Ok::<(), Box<dyn std::error::Error>>(())
/// ```
///
/// Each state that ends a turn holds its own type. The data of one state does
/// not fit another state, so a `match` cannot make a move that `apply`
/// refuses. This compiles:
///
/// ```
/// use creche_contracts::session::Turn;
///
/// fn keep(turn: Turn) -> Turn {
///     match turn {
///         Turn::Failed(failed) => Turn::Failed(failed),
///         other => other,
///     }
/// }
/// # assert_eq!(keep(Turn::Queued), Turn::Queued);
/// ```
///
/// This does not, because a turn that failed cannot become a settled turn:
///
/// ```compile_fail,E0308
/// use creche_contracts::session::Turn;
///
/// fn forge(turn: Turn) -> Turn {
///     match turn {
///         Turn::Failed(failed) => Turn::Settled(failed),
///         other => other,
///     }
/// }
/// ```
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Turn {
    /// The turn is in the queue of its family. Only an autonomous family has
    /// a queue.
    Queued,
    /// pi runs the turn.
    Running(Run),
    /// The chaperone holds a tool call of the turn until a person decides.
    WaitingApproval(Gated),
    /// pi settled.
    Settled(SettledTurn),
    /// The turn ended and did not settle.
    Failed(FailedTurn),
    /// A caller or the platform stopped the turn.
    Aborted(AbortedTurn),
}

/// A run with a tool call that the chaperone holds. Only [`Turn::apply`] makes
/// one.
///
/// ```
/// use creche_contracts::session::{Gated, Run};
///
/// fn run(gated: &Gated) -> &Run {
///     gated.run()
/// }
/// ```
///
/// Code outside this module cannot build one from a run:
///
/// ```compile_fail,E0451
/// use creche_contracts::session::{Gated, Run};
///
/// fn gate(run: Run) -> Gated {
///     Gated { run }
/// }
/// ```
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Gated {
    run: Run,
}

impl Gated {
    /// The run that waits.
    #[must_use]
    pub fn run(&self) -> &Run {
        &self.run
    }
}

/// One move of a turn (contract 02 §4.3).
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Step {
    /// A slot is free: `queued` to `running`.
    Start(Run),
    /// The chaperone holds a tool call: `running` to `waiting-approval`.
    OpenGate,
    /// The phone answered, or the wait ended: `waiting-approval` to `running`.
    CloseGate,
    /// pi settled: `running` to `settled`.
    Settle {
        /// When the turn ended.
        at: Timestamp,
    },
    /// The turn ended and did not settle: `running` or `waiting-approval` to
    /// `failed`.
    Fail {
        /// When the turn ended.
        at: Timestamp,
        /// Why the turn failed.
        reason: TurnReason,
    },
    /// A caller or the platform stopped the turn: each state that is not
    /// terminal to `aborted`.
    Abort {
        /// When the turn ended.
        at: Timestamp,
        /// Why the turn stopped.
        reason: TurnReason,
    },
}

impl Step {
    /// The state that the move leads to.
    #[must_use]
    pub const fn target(&self) -> TurnState {
        match self {
            Self::Start(_) | Self::CloseGate => TurnState::Running,
            Self::OpenGate => TurnState::WaitingApproval,
            Self::Settle { .. } => TurnState::Settled,
            Self::Fail { .. } => TurnState::Failed,
            Self::Abort { .. } => TurnState::Aborted,
        }
    }
}

impl Turn {
    /// The state of the turn.
    #[must_use]
    pub const fn state(&self) -> TurnState {
        match self {
            Self::Queued => TurnState::Queued,
            Self::Running(_) => TurnState::Running,
            Self::WaitingApproval(_) => TurnState::WaitingApproval,
            Self::Settled(_) => TurnState::Settled,
            Self::Failed(_) => TurnState::Failed,
            Self::Aborted(_) => TurnState::Aborted,
        }
    }

    /// The turn after one move. The move takes the old state away.
    ///
    /// A move that contract 02 §4.3 does not permit is an [`IllegalMove`]. The
    /// error holds the turn, in the state that it had.
    pub fn apply(self, step: Step) -> Result<Self, Box<IllegalMove>> {
        match (self, step) {
            (Self::Queued, Step::Start(run)) => Ok(Self::Running(run)),
            (Self::Running(run), Step::OpenGate) => Ok(Self::WaitingApproval(Gated { run })),
            (Self::WaitingApproval(gated), Step::CloseGate) => Ok(Self::Running(gated.run)),
            (Self::Running(run), Step::Settle { at }) => {
                Ok(Self::Settled(SettledTurn { run, ended_at: at }))
            }
            (Self::Running(run), Step::Fail { at, reason })
            | (Self::WaitingApproval(Gated { run }), Step::Fail { at, reason }) => {
                Ok(Self::Failed(FailedTurn {
                    run,
                    ended_at: at,
                    reason,
                }))
            }
            (Self::Queued, Step::Abort { at, reason }) => Ok(Self::Aborted(AbortedTurn {
                run: None,
                ended_at: at,
                reason,
            })),
            (Self::Running(run), Step::Abort { at, reason })
            | (Self::WaitingApproval(Gated { run }), Step::Abort { at, reason }) => {
                Ok(Self::Aborted(AbortedTurn {
                    run: Some(run),
                    ended_at: at,
                    reason,
                }))
            }
            (turn, step) => Err(Box::new(IllegalMove {
                wanted: step.target(),
                turn,
            })),
        }
    }
}

/// A move that contract 02 §4.3 does not permit.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct IllegalMove {
    turn: Turn,
    wanted: TurnState,
}

impl IllegalMove {
    /// The state that the turn has.
    #[must_use]
    pub fn state(&self) -> TurnState {
        self.turn.state()
    }

    /// The state that the move leads to.
    #[must_use]
    pub fn wanted(&self) -> TurnState {
        self.wanted
    }

    /// The turn, in the state that it had before the move.
    #[must_use]
    pub fn into_turn(self) -> Turn {
        self.turn
    }
}

impl fmt::Display for IllegalMove {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            f,
            "a turn in the state {} cannot become {}",
            self.state(),
            self.wanted
        )
    }
}

impl Error for IllegalMove {}

#[cfg(test)]
mod tests {
    use super::*;

    fn at(text: &str) -> Timestamp {
        text.parse().unwrap()
    }

    fn run() -> Run {
        Run::new(at("2026-10-05T19:22:05Z"), "chat-s2".parse().unwrap())
    }

    #[test]
    fn a_turn_settles_through_an_approval() {
        let turn = Turn::Queued
            .apply(Step::Start(run()))
            .and_then(|turn| turn.apply(Step::OpenGate))
            .and_then(|turn| turn.apply(Step::CloseGate))
            .and_then(|turn| {
                turn.apply(Step::Settle {
                    at: at("2026-10-05T19:22:31Z"),
                })
            })
            .unwrap();
        let Turn::Settled(settled) = &turn else {
            panic!("the turn settled");
        };

        assert_eq!(settled.run(), &run());
        assert_eq!(settled.ended_at(), at("2026-10-05T19:22:31Z"));
        assert!(turn.state().is_terminal());
    }

    #[test]
    fn a_turn_in_the_queue_stops_with_no_run() {
        let step = Step::Abort {
            at: at("2026-10-05T19:22:31Z"),
            reason: TurnReason::QueueLost,
        };
        let Turn::Aborted(aborted) = Turn::Queued.apply(step).unwrap() else {
            panic!("the turn stopped");
        };

        assert_eq!(aborted.run(), None);
        assert_eq!(aborted.ended_at(), at("2026-10-05T19:22:31Z"));
        assert_eq!(aborted.reason(), TurnReason::QueueLost);
    }

    #[test]
    fn a_turn_that_waits_can_fail() {
        let step = Step::Fail {
            at: at("2026-10-05T19:22:31Z"),
            reason: TurnReason::ApprovalDenied,
        };
        let waiting = Turn::Running(run()).apply(Step::OpenGate).unwrap();
        let Turn::WaitingApproval(gated) = &waiting else {
            panic!("the turn waits");
        };

        assert_eq!(gated.run(), &run());

        let Turn::Failed(failed) = waiting.apply(step).unwrap() else {
            panic!("the turn failed");
        };

        assert_eq!(failed.run(), &run());
        assert_eq!(failed.ended_at(), at("2026-10-05T19:22:31Z"));
        assert_eq!(failed.reason(), TurnReason::ApprovalDenied);
    }

    #[test]
    fn an_illegal_move_is_an_error_that_keeps_the_turn() {
        let refused = Turn::Queued.apply(Step::Settle {
            at: at("2026-10-05T19:22:31Z"),
        });
        let error = refused.unwrap_err();

        assert_eq!(error.state(), TurnState::Queued);
        assert_eq!(error.wanted(), TurnState::Settled);
        assert_eq!(
            error.to_string(),
            "a turn in the state queued cannot become settled"
        );
        assert_eq!(error.into_turn(), Turn::Queued);
    }

    #[test]
    fn a_turn_that_ended_never_moves() {
        let ended = Turn::Running(run())
            .apply(Step::Settle {
                at: at("2026-10-05T19:22:31Z"),
            })
            .unwrap();
        let steps = [
            Step::Start(run()),
            Step::OpenGate,
            Step::CloseGate,
            Step::Settle {
                at: at("2026-10-05T19:23:00Z"),
            },
            Step::Fail {
                at: at("2026-10-05T19:23:00Z"),
                reason: TurnReason::Internal,
            },
            Step::Abort {
                at: at("2026-10-05T19:23:00Z"),
                reason: TurnReason::UserStopped,
            },
        ];

        for step in steps {
            let error = ended.clone().apply(step).unwrap_err();

            assert_eq!(error.into_turn(), ended);
        }
    }

    #[test]
    fn the_state_of_a_session_is_the_first_rule_that_matches() {
        use TurnState::{Aborted, Failed, Queued, Running, Settled, WaitingApproval};

        let states = [
            (&[][..], LastEnded::NotFailed, SessionState::Idle),
            (&[][..], LastEnded::Failed, SessionState::Failed),
            (
                &[Settled, Aborted],
                LastEnded::NotFailed,
                SessionState::Idle,
            ),
            (&[Failed, Queued], LastEnded::Failed, SessionState::Queued),
            (&[Queued, Running], LastEnded::Failed, SessionState::Running),
            (
                &[Running, WaitingApproval, Queued],
                LastEnded::NotFailed,
                SessionState::WaitingApproval,
            ),
        ];

        for (turns, last_ended, state) in states {
            assert_eq!(session_state(turns, last_ended), state, "{turns:?}");
        }
    }
}
