//! The session API: the requests and the answers of `attendance`, the journal
//! and the event stream (contract 02).
//!
//! | What | Types |
//! |---|---|
//! | A request body | [`RawBody`], then [`CreateRequest`], [`RunTurnRequest`], [`WriterRequest`], [`SteerRequest`], [`StopRequest`], [`DispatchRequest`], [`JobsQuery`], [`DelegateRequest`], [`SwitchRequest`] |
//! | A query | [`ListQuery`], [`TurnsWanted`], [`EventsQuery`] |
//! | A refusal | [`ApiError`], [`ErrorCode`], [`ErrorDetail`] |
//! | An object of an answer | [`SessionView`], [`TurnView`], [`Lease`] |
//! | The states | [`SessionState`], [`TurnState`], [`Turn`], [`Step`] |
//! | The journal and the stream | [`JournalLine`], [`JournalBody`], [`StreamRecord`], [`StoredLine`] |
//! | The outcome record | [`OutcomeRecord`], [`JobStatus`] |
//!
//! The Python implementation is the authority for each type here until a Rust
//! release replaces it. `vectors/data/session` records what the Python code
//! does, and the test module `python` walks each vector. Where the Python code
//! is lax against the contract, the type is lax in the same way, and a
//! `CONTRACT-QUESTION` comment marks it.
//!
//! Four things differ from the Python code on purpose. The test holds each one
//! as a row of its `DEVIATIONS` table, and `rust/AGENTS.md` lists them.
//!
//! - A JSON text is UTF-8 with no byte order mark. It holds no `NaN` and no
//!   `Infinity`. A key, and a text that a parser reads, holds no lone
//!   surrogate.
//! - A JSON text nests 128 levels at most.
//! - A number of a query is written with the digits 0 to 9.
//! - A sequence number fits 64 bits.

mod error;
mod fields;
mod journal;
mod json;
mod outcome;
mod query;
mod request;
mod state;
mod time;
mod view;

#[cfg(test)]
mod python;

pub use error::{
    ApiError, ErrorCode, ErrorCodeError, ErrorDetail, HolderBlock, OtherDetail, TurnReason,
    TurnReasonError,
};
pub use fields::{
    DeadlineS, DeadlineSError, DispatchKey, DispatchKeyError, Follow, FollowError, Holder,
    HolderError, IdempotencyKey, IdempotencyKeyError, Intent, IntentError, JobMessage,
    JobMessageError, JournalSeq, JournalSeqError, Labels, LabelsError, LeaseReason,
    LeaseReasonError, PageLimit, PageLimitError, Prompt, PromptError, QueueDepth, QueueDepthError,
    SteerMessage, SteerMessageError, StopReason, StopReasonError, SwitchMode, SwitchModeError,
    Title, TitleError, TriggerKind, TriggerKindError, TurnRef, TurnsWanted, TurnsWantedError, Wait,
    WaitError,
};
pub use journal::{
    ApprovalRequested, ApprovalResolved, BodyError, BranchFallback, GateReason, JournalBody,
    JournalLine, LineError, LineKind, LineKindError, Note, OtherGateReason, OtherNote, ServiceNote,
    SessionTitled, StatusAge, StoredLine, StreamRecord, TerminalExchange, TurnEnded, TurnQueued,
    TurnSettled, TurnStarted, WriterChanged,
};
pub use json::EncodeError;
pub use outcome::{
    Approvals, JobStatus, JobStatusError, OutcomeRecord, RawOutcome, Spend, cut_error,
};
pub use query::{EventsQuery, ListQuery, RawEventsQuery, RawListQuery};
pub use request::{
    CreateRequest, DelegateRequest, DispatchRequest, JobsQuery, RawBody, RunTurnRequest,
    SteerRequest, StopRequest, SwitchRequest, WriterRequest,
};
pub use state::{
    AbortedTurn, FailedTurn, Gated, IllegalMove, LastEnded, Run, SessionKind, SessionKindError,
    SessionState, SessionStateError, SettledTurn, Step, Turn, TurnState, TurnStateError,
    session_state,
};
pub use time::{Timestamp, TimestampError};
pub use view::{
    FieldError, Lease, OwuiRefs, RawLease, RawOwuiRefs, RawSession, RawTurn, RawUsage, SessionView,
    Trigger, TurnView, Usage,
};
