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

/// A move that contract 02 §4.3 does not permit. Only [`Turn::apply`] makes
/// one.
///
/// ```
/// use creche_contracts::session::{IllegalMove, TurnState};
///
/// fn wanted(error: &IllegalMove) -> TurnState {
///     error.wanted()
/// }
/// ```
///
/// Code outside this module cannot change the turn or the move of an error:
///
/// ```compile_fail,E0451
/// use creche_contracts::session::{IllegalMove, TurnState};
///
/// fn wanted(error: IllegalMove) -> IllegalMove {
///     IllegalMove { wanted: TurnState::Settled, ..error }
/// }
/// ```
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

/// The two states of a refused move that `attendance` writes into a journal
/// as the note `illegal_transition` (contract 02 §4.3).
///
/// `attendance` writes that note when it ends a turn and its state table
/// refuses the move. The turn then keeps its state. A move that ends a turn
/// leads to a terminal state: `settled`, `failed` or `aborted`. A turn in a
/// terminal state gets no note. The table has each pair that is left, and a
/// value holds one of them:
///
/// | The state of the turn | The terminal state that it did not take |
/// |---|---|
/// | `queued` | `settled` |
/// | `queued` | `failed` |
/// | `waiting-approval` | `settled` |
///
/// A service gets a value only from [`RefusedMove::of`], so only a move that
/// [`Turn::apply`] refused becomes a note. A reader gets a value through
/// `serde`, which takes the members `from` and `to` and refuses each other
/// pair. The writer gives the two members in that order.
///
/// The set is closed. A newer `attendance` can write another pair. The
/// reader of a journal body does not refuse such a note: it keeps the body
/// as a note of free form, a [`Note::Other`](super::Note::Other).
///
/// ```
/// use creche_contracts::session::{RefusedMove, Step, Turn, TurnState};
///
/// let at = "2026-10-05T19:22:31Z".parse()?;
/// let refused = Turn::Queued.apply(Step::Settle { at });
/// let noted = refused.as_ref().err().and_then(|refused| RefusedMove::of(refused));
/// assert_eq!(
///     noted.map(|noted| (noted.from(), noted.to())),
///     Some((TurnState::Queued, TurnState::Settled))
/// );
/// # Ok::<(), Box<dyn std::error::Error>>(())
/// ```
///
/// Code outside this module cannot build one from two states:
///
/// ```compile_fail,E0451
/// use creche_contracts::session::{RefusedMove, Step, Turn, TurnState};
///
/// fn noted(turn: &Turn, step: &Step) -> RefusedMove {
///     let to: TurnState = step.target();
///     RefusedMove { from: turn.state(), to }
/// }
/// ```
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(try_from = "RawRefusedMove")]
pub struct RefusedMove {
    from: TurnState,
    to: TurnState,
}

/// The two members of the note of a refused move, before the check of the
/// pair. Only `serde` makes a value. The read refuses a third member and a
/// word that [`TurnState`] does not have.
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct RawRefusedMove {
    from: TurnState,
    to: TurnState,
}

impl TryFrom<RawRefusedMove> for RefusedMove {
    type Error = NoNote;

    fn try_from(raw: RawRefusedMove) -> Result<Self, Self::Error> {
        Self::noted(raw.from, raw.to).ok_or(NoNote {
            from: raw.from,
            to: raw.to,
        })
    }
}

impl RefusedMove {
    /// The pair of a refused move, for the note.
    ///
    /// `None` is a refused move that gets no note. Two kinds exist: a move of
    /// a turn in a terminal state, and a move to a state that is not
    /// terminal. The two states of the second kind can be a pair that the
    /// contract permits for another [`Step`]. For example, [`Step::CloseGate`]
    /// on a turn in the queue asks for `queued` to `running`.
    #[must_use]
    pub fn of(refused: &IllegalMove) -> Option<Self> {
        Self::noted(refused.state(), refused.wanted())
    }

    /// The state that the turn has.
    #[must_use]
    pub const fn from(&self) -> TurnState {
        self.from
    }

    /// The terminal state that the turn did not take.
    #[must_use]
    pub const fn to(&self) -> TurnState {
        self.to
    }

    /// The one copy of the set of pairs. The table of the type shows them.
    ///
    /// The set is each move from a state that is not terminal to a terminal
    /// state that [`Turn::apply`] refuses. A test holds the two equal.
    fn noted(from: TurnState, to: TurnState) -> Option<Self> {
        let noted = match from {
            TurnState::Queued => matches!(to, TurnState::Settled | TurnState::Failed),
            TurnState::WaitingApproval => matches!(to, TurnState::Settled),
            TurnState::Running | TurnState::Settled | TurnState::Failed | TurnState::Aborted => {
                false
            }
        };

        noted.then_some(Self { from, to })
    }
}

/// Why two states are not a pair of a [`RefusedMove`]: `attendance` writes
/// no note for that move.
#[derive(Debug)]
struct NoNote {
    from: TurnState,
    to: TurnState,
}

impl fmt::Display for NoNote {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            f,
            "no note holds a turn in the state {} that did not become {}",
            self.from, self.to
        )
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn at(text: &str) -> Timestamp {
        text.parse().unwrap()
    }

    fn run() -> Run {
        Run::new(at("2026-10-05T19:22:05Z"), "chat-s2".parse().unwrap())
    }

    /// A turn in one state.
    fn turn_in(state: TurnState) -> Turn {
        let at = at("2026-10-05T19:22:31Z");
        let reason = TurnReason::Internal;
        let step = match state {
            TurnState::Queued => return Turn::Queued,
            TurnState::Running => return Turn::Running(run()),
            TurnState::WaitingApproval => Step::OpenGate,
            TurnState::Settled => Step::Settle { at },
            TurnState::Failed => Step::Fail { at, reason },
            TurnState::Aborted => Step::Abort { at, reason },
        };

        Turn::Running(run()).apply(step).unwrap()
    }

    /// The three moves that end a turn.
    fn end_steps() -> [Step; 3] {
        let at = at("2026-10-05T19:23:00Z");

        [
            Step::Settle { at },
            Step::Fail {
                at,
                reason: TurnReason::Internal,
            },
            Step::Abort {
                at,
                reason: TurnReason::UserStopped,
            },
        ]
    }

    /// Each move: the three that end a turn, and the three that do not.
    fn each_step() -> Vec<Step> {
        let mut steps = vec![Step::Start(run()), Step::OpenGate, Step::CloseGate];
        steps.extend(end_steps());

        steps
    }

    fn pair(noted: Option<RefusedMove>) -> Option<(TurnState, TurnState)> {
        noted.map(|noted| (noted.from(), noted.to()))
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

    /// The set of [`RefusedMove`] against the state table: a change of
    /// `Turn::apply` that moves one of the nine results fails here.
    #[test]
    fn the_noted_pairs_are_the_ends_that_apply_refuses_a_live_turn() {
        use TurnState::{Queued, Running, WaitingApproval};

        for from in [Queued, Running, WaitingApproval] {
            for step in end_steps() {
                let to = step.target();
                let refused = turn_in(from).apply(step).is_err();

                assert!(!from.is_terminal() && to.is_terminal());
                assert_eq!(
                    RefusedMove::noted(from, to).is_some(),
                    refused,
                    "{from} to {to}"
                );
            }
        }
    }

    #[test]
    fn a_refused_end_of_a_live_turn_gives_its_pair() {
        use TurnState::{Failed, Queued, Settled, WaitingApproval};

        let at = at("2026-10-05T19:23:00Z");
        let reason = TurnReason::Internal;
        let moves = [
            (Queued, Step::Settle { at }, Settled),
            (Queued, Step::Fail { at, reason }, Failed),
            (WaitingApproval, Step::Settle { at }, Settled),
        ];

        for (from, step, to) in moves {
            let error = turn_in(from).apply(step).unwrap_err();

            assert_eq!(pair(RefusedMove::of(&error)), Some((from, to)));
        }
    }

    #[test]
    fn a_refused_move_of_another_kind_gives_no_pair() {
        use TurnState::{Aborted, Failed, Queued, Running, Settled, WaitingApproval};

        // A turn that ended refuses each move, and no move of it has a note.
        for ended in [Settled, Failed, Aborted] {
            for step in each_step() {
                let error = turn_in(ended).apply(step).unwrap_err();

                assert_eq!(RefusedMove::of(&error), None, "{error}");
            }
        }

        // The two states of the last two moves are a pair that the contract
        // permits for another step.
        let no_end = [
            (Running, Step::Start(run()), Running),
            (Running, Step::CloseGate, Running),
            (WaitingApproval, Step::OpenGate, WaitingApproval),
            (Queued, Step::OpenGate, WaitingApproval),
            (Queued, Step::CloseGate, Running),
            (WaitingApproval, Step::Start(run()), Running),
        ];
        for (from, step, to) in no_end {
            let error = turn_in(from).apply(step).unwrap_err();

            assert_eq!((error.state(), error.wanted()), (from, to));
            assert_eq!(RefusedMove::of(&error), None, "{error}");
        }
    }

    #[test]
    fn three_of_the_36_pairs_of_states_are_noted() {
        use TurnState::{Failed, Queued, Settled, WaitingApproval};

        let noted: Vec<(TurnState, TurnState)> = TurnState::ALL
            .iter()
            .flat_map(|from| TurnState::ALL.iter().map(move |to| (*from, *to)))
            .filter(|(from, to)| RefusedMove::noted(*from, *to).is_some())
            .collect();

        assert_eq!(
            noted,
            [
                (Queued, Settled),
                (Queued, Failed),
                (WaitingApproval, Settled)
            ]
        );
    }

    #[test]
    fn a_refused_move_reads_two_members_and_refuses_each_other_pair() {
        let read = |text: &str| serde_json::from_str::<RefusedMove>(text);
        let noted = read(r#"{"to":"settled","from":"waiting-approval"}"#).unwrap();

        assert_eq!(
            pair(Some(noted)),
            Some((TurnState::WaitingApproval, TurnState::Settled))
        );
        assert_eq!(
            serde_json::to_string(&noted).unwrap(),
            r#"{"from":"waiting-approval","to":"settled"}"#
        );
        assert_eq!(
            read(r#"{"from":"running","to":"settled"}"#)
                .unwrap_err()
                .to_string(),
            "no note holds a turn in the state running that did not become settled"
        );
        for refused in [
            r#"{"from":"queued","to":"aborted"}"#,
            r#"{"from":"waiting_approval","to":"settled"}"#,
            r#"{"from":"queued","to":"done"}"#,
            r#"{"from":"queued"}"#,
            r#"{"to":"settled"}"#,
            r#"{"from":"queued","to":"settled","turn":"x"}"#,
            r#"{"from":"queued","to":null}"#,
        ] {
            assert!(read(refused).is_err(), "{refused}");
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
