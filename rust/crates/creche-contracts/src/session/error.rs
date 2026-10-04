//! The error model: one body for each refusal (contract 02 §14).

use std::error::Error;
use std::fmt;
use std::str::FromStr;

use serde::{Deserialize, Serialize, Serializer};
use serde_json::{Map, Value};

use super::fields::{Holder, words};
use super::json::{self, Charset, EncodeError};
use super::time::{self, Timestamp};
use crate::ids::Ulid;

words! {
    /// The code of one refusal of `attendance` (contract 02 §14).
    ///
    /// The set is closed for the writer. `attendance` writes no other code. A
    /// door can meet a code that a newer `attendance` writes. A door refuses
    /// that code: it reads the answer as a failure with no known cause.
    ErrorCode,
    /// Why a text is not an error code.
    ErrorCodeError,
    "an error code",
    {
        /// A parser refused a field of the request.
        BadRequest => "bad_request",
        /// The same idempotency key came again with another prompt.
        IdempotencyMismatch => "idempotency_mismatch",
        /// The request holds no token that `attendance` knows.
        Unauthorized => "unauthorized",
        /// The token gives no right to the family or to the session prefix.
        Forbidden => "forbidden",
        /// The session does not exist.
        NotFound => "not_found",
        /// The session holds no turn with that id.
        TurnNotFound => "turn_not_found",
        /// The registry holds no family with that name.
        FamilyUnknown => "family_unknown",
        /// No trigger of the target family has `enqueue: true`.
        DispatchNotDeclared => "dispatch_not_declared",
        /// Another door holds the writer lease, or the session runs a turn.
        SessionBusy => "session_busy",
        /// The lease went to a second door before this door renewed it.
        LeaseTakenOver => "lease_taken_over",
        /// The family file never passed validation.
        FamilyInvalid => "family_invalid",
        /// A text or a list of the request is larger than its cap.
        PayloadTooLarge => "payload_too_large",
        /// The turn queue of the family has no room.
        QueueFull => "queue_full",
        /// The family has a fault that stops each new turn.
        FamilyDegraded => "family_degraded",
        /// No sandbox can serve the family now.
        SandboxUnavailable => "sandbox_unavailable",
        /// A defect in `attendance`.
        Internal => "internal",
        /// This build of `attendance` lacks the operation.
        NotImplemented => "not_implemented",
    }
}

const RETRY_SOON_S: u32 = 5;
const RETRY_LATER_S: u32 = 10;

impl ErrorCode {
    /// The HTTP status of an answer with this code (contract 02 §14).
    #[must_use]
    pub const fn http_status(self) -> u16 {
        match self {
            Self::BadRequest | Self::IdempotencyMismatch => 400,
            Self::Unauthorized => 401,
            Self::Forbidden | Self::DispatchNotDeclared => 403,
            Self::NotFound | Self::TurnNotFound | Self::FamilyUnknown => 404,
            Self::SessionBusy | Self::LeaseTakenOver | Self::FamilyInvalid => 409,
            Self::PayloadTooLarge => 413,
            Self::QueueFull => 429,
            Self::Internal => 500,
            Self::NotImplemented => 501,
            Self::FamilyDegraded | Self::SandboxUnavailable => 503,
        }
    }

    /// How many seconds a caller waits before it tries again. `None` for a
    /// code with no retry (contract 02 §14).
    #[must_use]
    pub const fn retry_after_s(self) -> Option<u32> {
        match self {
            Self::SessionBusy | Self::QueueFull | Self::Internal => Some(RETRY_SOON_S),
            Self::FamilyDegraded | Self::SandboxUnavailable => Some(RETRY_LATER_S),
            Self::BadRequest
            | Self::IdempotencyMismatch
            | Self::Unauthorized
            | Self::Forbidden
            | Self::NotFound
            | Self::TurnNotFound
            | Self::FamilyUnknown
            | Self::DispatchNotDeclared
            | Self::LeaseTakenOver
            | Self::FamilyInvalid
            | Self::PayloadTooLarge
            | Self::NotImplemented => None,
        }
    }
}

words! {
    /// Why a turn has the state `failed` or `aborted` (contract 02 §14, the
    /// second table).
    ///
    /// The set is closed for the writer. A reader of a turn or of a journal
    /// line can meet a reason that a newer `attendance` writes. The reader
    /// refuses the typed form of that turn or of that line.
    TurnReason,
    /// Why a text is not the reason of a turn.
    TurnReasonError,
    "the reason of a turn",
    {
        /// The deadline of the turn came before its end.
        TurnTimeout => "turn_timeout",
        /// The caller of a gated call went away.
        ApprovalDenied => "approval_denied",
        /// A gate got no answer in time. An older `attendance` wrote it.
        ApprovalTimeout => "approval_timeout",
        /// `attendance` lost its connection to the sandbox.
        ChannelLost => "channel_lost",
        /// The host refused a line of the playpen.
        ProtocolViolation => "protocol_violation",
        /// A sandbox switch with the mode `interrupt` stopped the turn.
        PermissionRemoved => "permission_removed",
        /// The sandbox went away, or a sandbox switch with the mode `drain`
        /// ran out of time.
        SandboxLost => "sandbox_lost",
        /// pi got no answer from the model.
        ModelError => "model_error",
        /// The family has no budget left at the model gateway.
        BudgetExceeded => "budget_exceeded",
        /// A caller stopped the turn.
        UserStopped => "user_stopped",
        /// A restart of `attendance` emptied the queue that held the turn.
        QueueLost => "queue_lost",
        /// The job did not start, because the chaperone gave no answer.
        PepUnreachable => "pep_unreachable",
        /// A defect in `attendance`.
        Internal => "internal",
    }
}

/// Who holds the writer lease, as a refusal names it (contract 02 §7.2).
///
/// ```
/// use creche_contracts::session::{Holder, HolderBlock, Timestamp};
///
/// let since: Timestamp = "2026-10-05T19:21:47Z".parse()?;
/// let expires_at: Timestamp = "2026-10-05T19:22:47Z".parse()?;
/// let block = HolderBlock::new(Holder::Owui, since, expires_at, None);
/// assert_eq!(block.holder(), Holder::Owui);
/// # Ok::<(), creche_contracts::session::TimestampError>(())
/// ```
///
/// Code outside this module cannot build a value from raw fields:
///
/// ```compile_fail,E0451
/// use creche_contracts::session::{Holder, HolderBlock};
///
/// let block = HolderBlock { holder: Holder::Owui, turn: None };
/// ```
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct HolderBlock {
    holder: Holder,
    #[serde(serialize_with = "time::seconds")]
    since: Timestamp,
    #[serde(serialize_with = "time::seconds")]
    expires_at: Timestamp,
    turn: Option<Ulid>,
}

impl HolderBlock {
    /// The holder of a lease, from when, until when, and the turn under it.
    #[must_use]
    pub fn new(
        holder: Holder,
        since: Timestamp,
        expires_at: Timestamp,
        turn: Option<Ulid>,
    ) -> Self {
        Self {
            holder,
            since,
            expires_at,
            turn,
        }
    }

    /// The door that holds the lease.
    #[must_use]
    pub fn holder(&self) -> Holder {
        self.holder
    }

    /// When the holder took the lease.
    #[must_use]
    pub fn since(&self) -> Timestamp {
        self.since
    }

    /// When the lease ends.
    #[must_use]
    pub fn expires_at(&self) -> Timestamp {
        self.expires_at
    }

    /// The turn that runs under the lease.
    #[must_use]
    pub fn turn(&self) -> Option<&Ulid> {
        self.turn.as_ref()
    }
}

/// The four fields of a holder block, as an error body holds them.
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct RawHolderBlock {
    holder: Holder,
    since: String,
    expires_at: String,
    turn: Option<Ulid>,
}

impl TryFrom<RawHolderBlock> for HolderBlock {
    type Error = super::time::TimestampError;

    fn try_from(raw: RawHolderBlock) -> Result<Self, Self::Error> {
        Ok(Self::new(
            raw.holder,
            raw.since.parse()?,
            raw.expires_at.parse()?,
            raw.turn,
        ))
    }
}

/// A detail that is not the holder block: its members, in the order of the
/// writer. `attendance` writes each of its details with a fixed order of
/// keys, and the writer here keeps the order that it gets.
///
/// Only [`ErrorDetail::from_members`] and the conversion from a JSON object
/// make one. A value thus never holds the four fields of a holder block.
///
/// ```
/// use creche_contracts::session::{ErrorDetail, OtherDetail};
/// use serde_json::json;
///
/// let detail = ErrorDetail::from_members([
///     ("principal".to_owned(), json!("door-owui")),
///     ("kind".to_owned(), json!("thin")),
/// ]);
/// let ErrorDetail::Other(other) = &detail else {
///     return;
/// };
/// let other: &OtherDetail = other;
/// assert_eq!(other.get("kind"), Some(&json!("thin")));
/// ```
///
/// Code outside this module cannot build one from raw members:
///
/// ```compile_fail,E0423
/// use creche_contracts::session::{ErrorDetail, OtherDetail};
/// use serde_json::json;
///
/// let detail = ErrorDetail::Other(OtherDetail(vec![("holder".to_owned(), json!("tui"))]));
/// ```
#[derive(Debug, Clone, PartialEq)]
pub struct OtherDetail(Vec<(String, Value)>);

impl OtherDetail {
    /// The value of one key.
    #[must_use]
    pub fn get(&self, key: &str) -> Option<&Value> {
        self.members()
            .find(|(name, _)| *name == key)
            .map(|(_, value)| value)
    }

    /// Each member, in the order of the writer.
    pub fn members(&self) -> impl Iterator<Item = (&str, &Value)> {
        self.0.iter().map(|(key, value)| (key.as_str(), value))
    }
}

impl Serialize for OtherDetail {
    fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        serializer.collect_map(self.members())
    }
}

/// What the `detail` of an error body holds (contract 02 §14).
// CONTRACT-QUESTION: contract 02 §14 shows `detail` as an object and gives no
// closed set of forms. §7.2 gives one form, the holder block. `attendance`
// writes eight more forms, and a later release can add one. This type has the
// holder block as a typed form and keeps each other object as it is. A closed
// set costs a change to this type for each new form.
#[derive(Debug, Clone, PartialEq)]
pub enum ErrorDetail {
    /// No detail. The body holds an empty object.
    None,
    /// The holder of the writer lease (contract 02 §7.2, §7.4).
    Holder(HolderBlock),
    /// An object of another form. The writer keeps the order of its keys.
    Other(OtherDetail),
}

impl ErrorDetail {
    /// The detail with these members, in this order.
    ///
    /// No member is no detail. The four fields of a holder block, in each
    /// order, are the holder block. A key that comes two times keeps its
    /// first place and takes its last value, as a Python `dict` does.
    #[must_use]
    pub fn from_members(members: impl IntoIterator<Item = (String, Value)>) -> Self {
        let mut ordered: Vec<(String, Value)> = Vec::new();
        for (key, value) in members {
            match ordered.iter_mut().find(|(name, _)| *name == key) {
                Some((_, held)) => *held = value,
                None => ordered.push((key, value)),
            }
        }

        if ordered.is_empty() {
            return Self::None;
        }

        let object: Map<String, Value> = ordered.iter().cloned().collect();
        let block = RawHolderBlock::deserialize(&Value::Object(object))
            .ok()
            .and_then(|raw| HolderBlock::try_from(raw).ok());

        block.map_or(Self::Other(OtherDetail(ordered)), Self::Holder)
    }
}

impl From<Map<String, Value>> for ErrorDetail {
    /// The detail with the members of a JSON object. A JSON object of this
    /// crate holds its keys in sorted order.
    fn from(detail: Map<String, Value>) -> Self {
        Self::from_members(detail)
    }
}

impl Serialize for ErrorDetail {
    fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        match self {
            Self::None => Map::new().serialize(serializer),
            Self::Holder(block) => block.serialize(serializer),
            Self::Other(detail) => detail.serialize(serializer),
        }
    }
}

/// One refusal of `attendance`: the code, and the fields of the error body
/// (contract 02 §14).
///
/// The family, the session and the turn are free text. A refusal names what
/// the caller sent, and that text can be the invalid name itself.
///
/// ```
/// use creche_contracts::session::{ApiError, ErrorCode};
///
/// let refusal = ApiError::new(ErrorCode::NotFound, "no such session").in_family("chat");
/// assert_eq!(refusal.http_status(), 404);
/// assert_eq!(
///     String::from_utf8(refusal.body()?).ok().as_deref(),
///     Some(concat!(
///         r#"{"error":{"code":"not_found","message":"no such session","#,
///         r#""family":"chat","session":null,"turn":null,"detail":{}}}"#
///     ))
/// );
/// # Ok::<(), creche_contracts::session::EncodeError>(())
/// ```
///
/// Code outside this module cannot build a refusal from raw fields:
///
/// ```compile_fail,E0423
/// use creche_contracts::session::ApiError;
///
/// let refusal = ApiError(Box::new(()));
/// ```
#[derive(Debug, Clone, PartialEq)]
pub struct ApiError(Box<Fields>);

/// The fields of an [`ApiError`]. They are behind one pointer, so a `Result`
/// with this error stays small.
#[derive(Debug, Clone, PartialEq)]
struct Fields {
    code: ErrorCode,
    message: String,
    family: Option<String>,
    session: Option<String>,
    turn: Option<String>,
    detail: ErrorDetail,
}

impl ApiError {
    /// A refusal with a code and a message, and no other field.
    pub fn new(code: ErrorCode, message: impl Into<String>) -> Self {
        Self(Box::new(Fields {
            code,
            message: message.into(),
            family: None,
            session: None,
            turn: None,
            detail: ErrorDetail::None,
        }))
    }

    /// The same refusal, with the family that it is about.
    #[must_use]
    pub fn in_family(mut self, family: impl Into<String>) -> Self {
        self.0.family = Some(family.into());
        self
    }

    /// The same refusal, with the session that it is about.
    #[must_use]
    pub fn in_session(mut self, session: impl Into<String>) -> Self {
        self.0.session = Some(session.into());
        self
    }

    /// The same refusal, with the turn that it is about.
    #[must_use]
    pub fn in_turn(mut self, turn: impl Into<String>) -> Self {
        self.0.turn = Some(turn.into());
        self
    }

    /// The same refusal, with a detail.
    #[must_use]
    pub fn with_detail(mut self, detail: ErrorDetail) -> Self {
        self.0.detail = detail;
        self
    }

    /// The code.
    #[must_use]
    pub fn code(&self) -> ErrorCode {
        self.0.code
    }

    /// The message for a person.
    #[must_use]
    pub fn message(&self) -> &str {
        &self.0.message
    }

    /// The family that the refusal is about.
    #[must_use]
    pub fn family(&self) -> Option<&str> {
        self.0.family.as_deref()
    }

    /// The session that the refusal is about.
    #[must_use]
    pub fn session(&self) -> Option<&str> {
        self.0.session.as_deref()
    }

    /// The turn that the refusal is about.
    #[must_use]
    pub fn turn(&self) -> Option<&str> {
        self.0.turn.as_deref()
    }

    /// The detail.
    #[must_use]
    pub fn detail(&self) -> &ErrorDetail {
        &self.0.detail
    }

    /// The HTTP status of the answer.
    #[must_use]
    pub fn http_status(&self) -> u16 {
        self.0.code.http_status()
    }

    /// The bytes of the answer: the error body, as `attendance` writes it.
    pub fn body(&self) -> Result<Vec<u8>, EncodeError> {
        json::encode(self, Charset::Utf8)
    }
}

/// The error body: `error`, and `retry_after_s` for a code with a retry.
#[derive(Serialize)]
struct Body<'a> {
    error: Inner<'a>,
    #[serde(skip_serializing_if = "Option::is_none")]
    retry_after_s: Option<u32>,
}

#[derive(Serialize)]
struct Inner<'a> {
    code: ErrorCode,
    message: &'a str,
    family: Option<&'a str>,
    session: Option<&'a str>,
    turn: Option<&'a str>,
    detail: &'a ErrorDetail,
}

impl Serialize for ApiError {
    fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        let body = Body {
            error: Inner {
                code: self.code(),
                message: self.message(),
                family: self.family(),
                session: self.session(),
                turn: self.turn(),
                detail: self.detail(),
            },
            retry_after_s: self.code().retry_after_s(),
        };

        body.serialize(serializer)
    }
}

impl fmt::Display for ApiError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}: {}", self.code(), self.message())
    }
}

impl Error for ApiError {}

#[cfg(test)]
mod tests {
    use serde_json::json;

    use super::*;

    fn members<const N: usize>(members: [(&str, Value); N]) -> ErrorDetail {
        ErrorDetail::from_members(members.map(|(key, value)| (key.to_owned(), value)))
    }

    #[test]
    fn a_detail_keeps_the_order_of_its_members() {
        let detail = members([("principal", json!("door-owui")), ("kind", json!("thin"))]);
        let twice = members([("b", json!(1)), ("a", json!(2)), ("b", json!(3))]);

        assert_eq!(
            serde_json::to_string(&detail).unwrap(),
            r#"{"principal":"door-owui","kind":"thin"}"#
        );
        assert_eq!(serde_json::to_string(&twice).unwrap(), r#"{"b":3,"a":2}"#);
    }

    #[test]
    fn the_four_fields_in_each_order_are_the_holder_block() {
        let detail = members([
            ("turn", Value::Null),
            ("expires_at", json!("2026-10-06T08:16:20Z")),
            ("since", json!("2026-10-06T08:15:20Z")),
            ("holder", json!("tui")),
        ]);
        let not_a_time = members([
            ("holder", json!("tui")),
            ("since", json!("soon")),
            ("expires_at", json!("2026-10-06T08:16:20Z")),
            ("turn", Value::Null),
        ]);

        assert!(matches!(&detail, ErrorDetail::Holder(block) if block.holder() == Holder::Tui));
        assert_eq!(
            serde_json::to_string(&detail).unwrap(),
            concat!(
                r#"{"holder":"tui","since":"2026-10-06T08:15:20Z","#,
                r#""expires_at":"2026-10-06T08:16:20Z","turn":null}"#
            )
        );
        assert!(matches!(not_a_time, ErrorDetail::Other(_)));
    }

    #[test]
    fn each_code_has_the_status_of_the_contract() {
        let statuses = [
            ("bad_request", 400, None),
            ("idempotency_mismatch", 400, None),
            ("unauthorized", 401, None),
            ("forbidden", 403, None),
            ("not_found", 404, None),
            ("turn_not_found", 404, None),
            ("family_unknown", 404, None),
            ("dispatch_not_declared", 403, None),
            ("session_busy", 409, Some(5)),
            ("lease_taken_over", 409, None),
            ("family_invalid", 409, None),
            ("payload_too_large", 413, None),
            ("queue_full", 429, Some(5)),
            ("family_degraded", 503, Some(10)),
            ("sandbox_unavailable", 503, Some(10)),
            ("internal", 500, Some(5)),
            ("not_implemented", 501, None),
        ];

        assert_eq!(statuses.len(), ErrorCode::ALL.len());
        for (word, status, retry) in statuses {
            let code: ErrorCode = word.parse().unwrap();

            assert_eq!(code.as_str(), word);
            assert_eq!(code.http_status(), status, "{word}");
            assert_eq!(code.retry_after_s(), retry, "{word}");
        }
    }

    #[test]
    fn a_word_outside_the_two_sets_is_refused() {
        assert_eq!("stream_overrun".parse::<ErrorCode>(), Err(ErrorCodeError));
        assert_eq!("Bad_Request".parse::<ErrorCode>(), Err(ErrorCodeError));
        assert_eq!("bad_request".parse::<TurnReason>(), Err(TurnReasonError));
        assert_eq!("stream_overrun".parse::<TurnReason>(), Err(TurnReasonError));
        assert_eq!(TurnReason::ALL.len(), 13);
        assert_eq!(
            serde_json::to_string(&TurnReason::QueueLost).unwrap(),
            "\"queue_lost\""
        );
    }

    #[test]
    fn a_body_has_a_retry_only_for_a_code_with_one() {
        let busy = ApiError::new(ErrorCode::SessionBusy, "busy");
        let missing = ApiError::new(ErrorCode::NotFound, "missing");

        assert_eq!(
            serde_json::to_value(&busy).unwrap(),
            json!({
                "error": {
                    "code": "session_busy", "message": "busy", "family": null,
                    "session": null, "turn": null, "detail": {}
                },
                "retry_after_s": 5
            })
        );
        assert_eq!(
            serde_json::to_value(&missing).unwrap().get("retry_after_s"),
            None
        );
        assert_eq!(busy.to_string(), "session_busy: busy");
    }

    #[test]
    fn a_detail_with_the_four_fields_is_the_holder_block() {
        let Value::Object(block) = json!({
            "holder": "tui", "since": "2026-10-06T08:15:20Z",
            "expires_at": "2026-10-06T08:16:20Z", "turn": null
        }) else {
            panic!("the block is an object");
        };
        let Value::Object(other) = json!({"holder": "tui", "turn": null}) else {
            panic!("the detail is an object");
        };
        let refusal =
            ApiError::new(ErrorCode::LeaseTakenOver, "taken").with_detail(ErrorDetail::from(block));

        assert!(
            matches!(refusal.detail(), ErrorDetail::Holder(block) if block.holder() == Holder::Tui)
        );
        assert!(matches!(ErrorDetail::from(other), ErrorDetail::Other(_)));
        assert_eq!(ErrorDetail::from(Map::new()), ErrorDetail::None);
        assert_eq!(ErrorDetail::from_members([]), ErrorDetail::None);
        assert_eq!(
            String::from_utf8(refusal.body().unwrap()).unwrap(),
            concat!(
                r#"{"error":{"code":"lease_taken_over","message":"taken","family":null,"#,
                r#""session":null,"turn":null,"detail":{"holder":"tui","#,
                r#""since":"2026-10-06T08:15:20Z","expires_at":"2026-10-06T08:16:20Z","#,
                r#""turn":null}}}"#
            )
        );
    }
}
