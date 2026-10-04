//! The request bodies of the session API, one type for each operation
//! (contract 02 §5, §13.4).
//!
//! A door is another process, so each field of a body is untrusted. A parser
//! reads a body into [`RawBody`], then into the type of the operation. A
//! refusal is an [`ApiError`] with the code, the message and the fields that
//! the Python parser gives.
//!
//! The Python parser reads a member of the wrong type as an absent member, and
//! it ignores an unknown member. The parsers here do the same. The doc comment
//! of each type lists the members that this rule applies to.

use serde_json::value::RawValue;

use super::error::{ApiError, ErrorCode};
use super::fields::{
    DeadlineS, DispatchKey, Holder, IdempotencyKey, Intent, JobMessage, Labels, PageLimit, Prompt,
    SteerMessage, StopReason, SwitchMode, Title, TriggerKind, Wait,
};
use super::json::{self, Fault, Kind, Object};
use super::time::Timestamp;
use super::view::{OwuiRefs, Trigger};
use crate::ids::{AttachmentName, FamilyName, SessionId, Ulid};

/// The largest count of attachments of one turn (contract 02 §5.4).
const ATTACHMENTS_MAX: usize = 20;

/// The largest count of families in a chain (contract 02 §13.2 rule 6).
const CHAIN_MAX: usize = 8;

/// The three labels that the trigger door sends in place of a trigger object
/// (contract 02 §13.2 rule 2).
const LABEL_TRIGGER_KIND: &str = "trigger_kind";
const LABEL_TRIGGER_NAME: &str = "trigger_name";
const LABEL_TRIGGER_FIRED_AT: &str = "trigger_fired_at";

const NOT_JSON: &str = "body is not JSON";
const NOT_OBJECT: &str = "body is not a JSON object";

fn bad(message: impl Into<String>) -> ApiError {
    ApiError::new(ErrorCode::BadRequest, message)
}

impl From<Fault> for ApiError {
    fn from(fault: Fault) -> Self {
        match fault {
            Fault::NotJson => bad(NOT_JSON),
            Fault::NotObject => bad(NOT_OBJECT),
        }
    }
}

/// The family and the session that a refusal names.
#[derive(Clone, Copy)]
struct Scope<'a> {
    family: Option<&'a str>,
    session: Option<&'a str>,
}

impl<'a> Scope<'a> {
    const NONE: Self = Self {
        family: None,
        session: None,
    };

    const fn family(family: &'a str) -> Self {
        Self {
            family: Some(family),
            session: None,
        }
    }

    const fn session(family: &'a str, session: &'a str) -> Self {
        Self {
            family: Some(family),
            session: Some(session),
        }
    }

    fn refuse(self, code: ErrorCode, message: impl Into<String>) -> ApiError {
        let refusal = ApiError::new(code, message);
        let refusal = match self.family {
            Some(family) => refusal.in_family(family),
            None => refusal,
        };

        match self.session {
            Some(session) => refusal.in_session(session),
            None => refusal,
        }
    }

    fn bad(self, message: impl Into<String>) -> ApiError {
        self.refuse(ErrorCode::BadRequest, message)
    }

    fn too_large(self, message: impl Into<String>) -> ApiError {
        self.refuse(ErrorCode::PayloadTooLarge, message)
    }
}

/// One request body, read as a JSON object and not yet checked.
///
/// The body is JSON in UTF-8. It nests 128 levels at most, and each integer
/// has 4300 digits at most. A key that occurs twice has its last value.
///
/// ```
/// use creche_contracts::session::{CreateRequest, RawBody};
///
/// let raw = RawBody::parse(br#"{"family": "chat", "session": "owui-8f1c2e"}"#)?;
/// let request = CreateRequest::try_from(&raw)?;
/// assert_eq!(request.family().as_str(), "chat");
/// # Ok::<(), creche_contracts::session::ApiError>(())
/// ```
///
/// Code outside this module cannot build a raw body from another value:
///
/// ```compile_fail,E0423
/// use creche_contracts::session::RawBody;
///
/// let raw = RawBody(());
/// ```
#[derive(Debug)]
pub struct RawBody(Object);

impl RawBody {
    /// The JSON object that the bytes of a body hold.
    ///
    /// Each refusal has the code `bad_request`.
    pub fn parse(body: &[u8]) -> Result<Self, ApiError> {
        Ok(Self(Object::read(body)?))
    }
}

/// A member that is a text with one character or more, or the refusal.
fn required_text(raw: &Object, name: &str, scope: Scope<'_>) -> Result<String, ApiError> {
    raw.filled_text(name)?
        .ok_or_else(|| scope.bad(format!("{name} is missing or not a string")))
}

/// The refusal for a text over its cap in bytes.
fn over_bytes(name: &str, cap: usize, scope: Scope<'_>) -> ApiError {
    scope.too_large(format!("{name} is over its {cap} byte limit"))
}

/// A whole number in a range, or the value for an absent member.
fn bounded<T: TryFrom<i128>>(
    number: Option<i128>,
    absent: T,
    refusal: impl FnOnce() -> ApiError,
) -> Result<T, ApiError> {
    number.map_or(Ok(absent), |number| {
        T::try_from(number).map_err(|_| refusal())
    })
}

fn read_title(raw: &Object, scope: Scope<'_>) -> Result<Title, ApiError> {
    let title = raw.filled_text("title")?.unwrap_or_default();

    Title::try_from(title).map_err(|_| scope.bad("title is over its limit"))
}

fn read_labels(raw: &Object, scope: Scope<'_>) -> Result<Labels, ApiError> {
    let Some(labels) = raw.object("labels")? else {
        return Ok(Labels::none());
    };
    let too_many = || scope.bad(format!("labels carries over {} keys", Labels::KEYS_MAX));
    if labels.len() > Labels::KEYS_MAX {
        return Err(too_many());
    }

    let mut found = Vec::with_capacity(labels.len());
    for (key, value) in labels.members() {
        let value = json::text_of(value)?
            .filter(|value| value.chars().count() <= Labels::VALUE_MAX_CHARS)
            .ok_or_else(|| scope.bad(format!("label {key} is not a short string")))?;
        found.push((key.to_owned(), value));
    }

    Labels::try_from(found).map_err(|_| too_many())
}

/// A delegation chain. Only a dispatch has one, and it has 8 families at most.
fn read_chain(
    value: Option<&RawValue>,
    kind: TriggerKind,
    family: &str,
    session: &str,
) -> Result<Vec<FamilyName>, ApiError> {
    let scope = Scope::session(family, session);
    let Some(value) = value.filter(|value| json::kind_of(value) != Kind::Null) else {
        return Ok(Vec::new());
    };
    let entries = json::array_of(value)
        .filter(|_| kind == TriggerKind::Dispatch)
        .ok_or_else(|| scope.bad("only a dispatch trigger carries a chain"))?;
    if entries.len() > CHAIN_MAX {
        return Err(scope.bad(format!(
            "trigger chain holds more than {CHAIN_MAX} families"
        )));
    }

    let not_a_family =
        || Scope::family(family).bad("trigger chain holds a name that is not a family");
    let mut chain = Vec::with_capacity(entries.len());
    for entry in &entries {
        let name = json::text_of(entry)?.ok_or_else(not_a_family)?;
        chain.push(name.parse().map_err(|_| not_a_family())?);
    }

    Ok(chain)
}

fn read_fired_at(text: Option<&str>, scope: Scope<'_>) -> Result<Option<Timestamp>, ApiError> {
    text.map(|text| {
        text.parse()
            .map_err(|_| scope.bad("trigger fired_at is not RFC 3339"))
    })
    .transpose()
}

fn read_trigger_kind(text: Option<&str>, scope: Scope<'_>) -> Result<TriggerKind, ApiError> {
    text.ok_or_else(|| scope.bad("trigger needs a kind"))?
        .parse()
        .map_err(|_| scope.bad("trigger kind is not timer, webhook or dispatch"))
}

/// The trigger of a turn: the object of the body, or else the three labels.
fn read_trigger(
    raw: &Object,
    labels: &Labels,
    family: &str,
    session: &str,
) -> Result<Option<Trigger>, ApiError> {
    let scope = Scope::session(family, session);
    let chain_refused = || scope.bad("only a dispatch trigger carries a chain");
    let Some(trigger) = raw.object("trigger")? else {
        let Some(kind) = labels.get(LABEL_TRIGGER_KIND) else {
            return Ok(None);
        };
        let kind = read_trigger_kind(Some(kind), scope)?;
        let name = labels.get(LABEL_TRIGGER_NAME).map(str::to_owned);
        let fired_at = read_fired_at(labels.get(LABEL_TRIGGER_FIRED_AT), scope)?;

        return Trigger::new(kind, name, fired_at, Vec::new())
            .map(Some)
            .map_err(|_| chain_refused());
    };
    let kind = read_trigger_kind(trigger.filled_text("kind")?.as_deref(), scope)?;
    let name = trigger.filled_text("name")?;
    let fired_at = read_fired_at(trigger.filled_text("fired_at")?.as_deref(), scope)?;
    let chain = read_chain(trigger.get("chain"), kind, family, session)?;

    Trigger::new(kind, name, fired_at, chain)
        .map(Some)
        .map_err(|_| chain_refused())
}

// --- create or find a session ---

/// The body of `POST /v1/sessions`: create or find a session (contract 02
/// §5.1).
///
/// A `title`, a `labels` or an `owner_session` of the wrong type is absent. An
/// empty `title` and an empty `owner_session` are absent too.
///
/// ```
/// use creche_contracts::session::CreateRequest;
///
/// let request = CreateRequest::from_body(
///     br#"{"family": "chat", "session": "owui-8f1c2e", "title": 5, "color": "red"}"#,
/// )?;
/// assert_eq!(request.session().as_str(), "owui-8f1c2e");
/// assert_eq!(request.title().as_str(), "");
///
/// let refusal = CreateRequest::from_body(br#"{"family": "Chat"}"#).unwrap_err();
/// assert_eq!(refusal.http_status(), 400);
/// # Ok::<(), creche_contracts::session::ApiError>(())
/// ```
///
/// Code outside this module cannot build a value from raw fields:
///
/// ```compile_fail,E0451
/// use creche_contracts::session::CreateRequest;
///
/// fn untitled(request: CreateRequest) -> CreateRequest {
///     CreateRequest { owner_session: None, ..request }
/// }
/// ```
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CreateRequest {
    family: FamilyName,
    session: SessionId,
    title: Title,
    labels: Labels,
    owner_session: Option<SessionId>,
}

impl CreateRequest {
    /// The request that the bytes of a body hold.
    pub fn from_body(body: &[u8]) -> Result<Self, ApiError> {
        Self::try_from(&RawBody::parse(body)?)
    }

    /// The family of the session.
    #[must_use]
    pub fn family(&self) -> &FamilyName {
        &self.family
    }

    /// The id of the session.
    #[must_use]
    pub fn session(&self) -> &SessionId {
        &self.session
    }

    /// The title of a new session.
    #[must_use]
    pub fn title(&self) -> &Title {
        &self.title
    }

    /// The labels.
    #[must_use]
    pub fn labels(&self) -> &Labels {
        &self.labels
    }

    /// The session that asked for this job. Only a thin family has one.
    #[must_use]
    pub fn owner_session(&self) -> Option<&SessionId> {
        self.owner_session.as_ref()
    }
}

impl TryFrom<&RawBody> for CreateRequest {
    type Error = ApiError;

    fn try_from(raw: &RawBody) -> Result<Self, Self::Error> {
        let raw = &raw.0;
        let family_text = required_text(raw, "family", Scope::NONE)?;
        let session_text = required_text(raw, "session", Scope::NONE)?;
        let family: FamilyName = family_text
            .parse()
            .map_err(|_| Scope::family(&family_text).bad("family is not a family name"))?;
        let scope = Scope::session(&family_text, &session_text);
        let session: SessionId = session_text
            .parse()
            .map_err(|_| scope.bad("session is not a session id"))?;
        let owner_session = raw
            .filled_text("owner_session")?
            .map(|owner| owner.parse())
            .transpose()
            .map_err(|_| scope.bad("owner_session is not a session id"))?;

        Ok(Self {
            family,
            session,
            title: read_title(raw, scope)?,
            labels: read_labels(raw, scope)?,
            owner_session,
        })
    }
}

// --- run a turn ---

/// The body of `POST /v1/sessions/{family}/{session}/turns`: run a turn
/// (contract 02 §5.4).
///
/// An `idempotency_key`, a `wait`, an `attachments`, an `owui`, a `trigger`, a
/// `deadline_s` or a `labels` of the wrong type is absent. A `deadline_s` that
/// is a boolean or a number with a fraction or an exponent is absent. A
/// `persona_text` of the wrong type is refused.
///
/// ```
/// use creche_contracts::session::{RunTurnRequest, Wait};
///
/// let request = RunTurnRequest::from_body(
///     br#"{"prompt": "Which sensor dropped out?", "wait": 1, "deadline_s": "60"}"#,
///     "chat",
///     "owui-8f1c2e",
/// )?;
/// assert_eq!(request.wait(), Wait::Stream);
/// assert_eq!(request.deadline_s().get(), 3600);
/// # Ok::<(), creche_contracts::session::ApiError>(())
/// ```
///
/// Code outside this module cannot build a value from raw fields:
///
/// ```compile_fail,E0451
/// use creche_contracts::session::RunTurnRequest;
///
/// fn no_trigger(request: RunTurnRequest) -> RunTurnRequest {
///     RunTurnRequest { trigger: None, ..request }
/// }
/// ```
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RunTurnRequest {
    prompt: Prompt,
    idempotency_key: Option<IdempotencyKey>,
    wait: Wait,
    persona_text: String,
    attachments: Vec<AttachmentName>,
    owui: Option<OwuiRefs>,
    trigger: Option<Trigger>,
    deadline_s: DeadlineS,
    labels: Labels,
}

impl RunTurnRequest {
    /// The request that the bytes of a body hold. `family` and `session` are
    /// the two parameters of the path. The parser does not check them. It
    /// copies them into a refusal.
    pub fn from_body(body: &[u8], family: &str, session: &str) -> Result<Self, ApiError> {
        Self::read(&RawBody::parse(body)?, family, session)
    }

    /// The request that a raw body holds.
    pub fn read(raw: &RawBody, family: &str, session: &str) -> Result<Self, ApiError> {
        let raw = &raw.0;
        let scope = Scope::session(family, session);
        let prompt = required_text(raw, "prompt", scope)?;
        let prompt =
            Prompt::try_from(prompt).map_err(|_| over_bytes("prompt", Prompt::MAX_BYTES, scope))?;
        let labels = read_labels(raw, scope)?;
        let idempotency_key = raw
            .filled_text("idempotency_key")?
            .map(IdempotencyKey::try_from)
            .transpose()
            .map_err(|_| scope.bad("idempotency_key is over its limit"))?;
        let wait = raw
            .filled_text("wait")?
            .map_or(Ok(Wait::Stream), |wait| wait.parse())
            .map_err(|_| scope.bad("wait is not a known mode"))?;
        let persona_text = read_persona(raw, scope)?;
        let attachments = read_attachments(raw, scope)?;
        let owui = read_owui(raw, scope)?;
        let trigger = read_trigger(raw, &labels, family, session)?;
        let deadline_s = bounded(raw.int("deadline_s"), DeadlineS::TURN, || {
            scope.bad("deadline_s is outside 1 to 3600")
        })?;

        Ok(Self {
            prompt,
            idempotency_key,
            wait,
            persona_text,
            attachments,
            owui,
            trigger,
            deadline_s,
            labels,
        })
    }

    /// The user text.
    #[must_use]
    pub fn prompt(&self) -> &Prompt {
        &self.prompt
    }

    /// The idempotency key.
    #[must_use]
    pub fn idempotency_key(&self) -> Option<&IdempotencyKey> {
        self.idempotency_key.as_ref()
    }

    /// How the caller wants the answer.
    #[must_use]
    pub fn wait(&self) -> Wait {
        self.wait
    }

    /// The folder system prompt. It has no cap here: `attendance` cuts it at
    /// 16 KiB and says so on the turn (contract 02 §11 rule 6).
    #[must_use]
    pub fn persona_text(&self) -> &str {
        &self.persona_text
    }

    /// The names of the files in the inbox of the session.
    #[must_use]
    pub fn attachments(&self) -> &[AttachmentName] {
        &self.attachments
    }

    /// The Open WebUI ids.
    #[must_use]
    pub fn owui(&self) -> Option<&OwuiRefs> {
        self.owui.as_ref()
    }

    /// The firing that started the job.
    #[must_use]
    pub fn trigger(&self) -> Option<&Trigger> {
        self.trigger.as_ref()
    }

    /// The limit of the turn.
    #[must_use]
    pub fn deadline_s(&self) -> DeadlineS {
        self.deadline_s
    }

    /// The labels.
    #[must_use]
    pub fn labels(&self) -> &Labels {
        &self.labels
    }
}

fn read_persona(raw: &Object, scope: Scope<'_>) -> Result<String, ApiError> {
    match raw.kind("persona_text") {
        None | Some(Kind::Null) => Ok(String::new()),
        Some(Kind::Text) => Ok(raw.text("persona_text")?.unwrap_or_default()),
        Some(_) => Err(scope.bad("persona_text is not a string")),
    }
}

fn read_attachments(raw: &Object, scope: Scope<'_>) -> Result<Vec<AttachmentName>, ApiError> {
    let Some(entries) = raw.get("attachments").and_then(json::array_of) else {
        return Ok(Vec::new());
    };
    if entries.len() > ATTACHMENTS_MAX {
        return Err(scope.too_large(format!("attachments carries over {ATTACHMENTS_MAX} items")));
    }

    let unsafe_name = || scope.bad("an attachment name is not 120 safe characters");
    let mut names = Vec::with_capacity(entries.len());
    for entry in &entries {
        let name = json::text_of(entry)?.ok_or_else(unsafe_name)?;
        names.push(name.parse().map_err(|_| unsafe_name())?);
    }

    Ok(names)
}

fn read_owui(raw: &Object, scope: Scope<'_>) -> Result<Option<OwuiRefs>, ApiError> {
    let Some(owui) = raw.object("owui")? else {
        return Ok(None);
    };
    let needs_ids = || scope.bad("owui needs a chat_id and a message_id");
    let chat_id = owui.filled_text("chat_id")?;
    let message_id = owui.filled_text("message_id")?;
    let (Some(chat_id), Some(message_id)) = (chat_id, message_id) else {
        return Err(needs_ids());
    };
    let user_message_id = owui.filled_text("user_message_id")?;
    let parent_id = owui.filled_text("parent_id")?;

    OwuiRefs::new(chat_id, message_id, user_message_id, parent_id)
        .map(Some)
        .map_err(|_| needs_ids())
}

// --- the writer lease, steer and stop ---

/// The body of `POST .../writer`: take or renew the writer lease (contract 02
/// §5.9).
///
/// A `holder` or an `intent` of the wrong type is absent, and an empty one
/// too. A `force` that is not the JSON value `true` is false.
///
/// ```
/// use creche_contracts::session::{Intent, WriterRequest};
///
/// let request = WriterRequest::from_body(br#"{"force": "true"}"#, "chat", "owui-8f1c2e")?;
/// assert_eq!(request.holder(), None);
/// assert!(!request.force());
/// assert_eq!(request.intent(), Intent::Acquire);
/// # Ok::<(), creche_contracts::session::ApiError>(())
/// ```
///
/// Code outside this module cannot build a value from raw fields:
///
/// ```compile_fail,E0451
/// use creche_contracts::session::WriterRequest;
///
/// fn forced(request: WriterRequest) -> WriterRequest {
///     WriterRequest { force: true, ..request }
/// }
/// ```
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct WriterRequest {
    holder: Option<Holder>,
    force: bool,
    intent: Intent,
}

impl WriterRequest {
    /// The request that the bytes of a body hold. `family` and `session` are
    /// the two parameters of the path.
    pub fn from_body(body: &[u8], family: &str, session: &str) -> Result<Self, ApiError> {
        Self::read(&RawBody::parse(body)?, family, session)
    }

    /// The request that a raw body holds.
    pub fn read(raw: &RawBody, family: &str, session: &str) -> Result<Self, ApiError> {
        let raw = &raw.0;
        let scope = Scope::session(family, session);
        let holder = raw
            .filled_text("holder")?
            .map(|holder| holder.parse())
            .transpose()
            .map_err(|_| scope.bad("holder is not a door"))?;
        let intent = raw
            .filled_text("intent")?
            .map_or(Ok(Intent::Acquire), |intent| intent.parse())
            .map_err(|_| scope.bad("intent is not acquire or renew"))?;

        Ok(Self {
            holder,
            force: raw.kind("force") == Some(Kind::True),
            intent,
        })
    }

    /// The door that the caller says it is. `attendance` checks it against the
    /// token.
    #[must_use]
    pub fn holder(&self) -> Option<Holder> {
        self.holder
    }

    /// Whether the caller takes an idle lease of another instance of its own
    /// door (contract 02 §7.3 rule 6).
    #[must_use]
    pub fn force(&self) -> bool {
        self.force
    }

    /// Whether the caller takes the lease or keeps it.
    #[must_use]
    pub fn intent(&self) -> Intent {
        self.intent
    }
}

/// The body of `POST .../turns/{turn}/steer`: steer a running turn (contract
/// 02 §5.6).
///
/// ```
/// use creche_contracts::session::SteerRequest;
///
/// let request = SteerRequest::from_body(br#"{"message": "Stop."}"#, "chat", "owui-8f1c2e")?;
/// assert_eq!(request.message().as_str(), "Stop.");
/// # Ok::<(), creche_contracts::session::ApiError>(())
/// ```
///
/// Code outside this module cannot build a value from a raw message:
///
/// ```compile_fail,E0451
/// use creche_contracts::session::{SteerMessage, SteerRequest};
///
/// fn steer(message: SteerMessage) -> SteerRequest {
///     SteerRequest { message }
/// }
/// ```
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SteerRequest {
    message: SteerMessage,
}

impl SteerRequest {
    /// The request that the bytes of a body hold. `family` and `session` are
    /// the two parameters of the path.
    pub fn from_body(body: &[u8], family: &str, session: &str) -> Result<Self, ApiError> {
        Self::read(&RawBody::parse(body)?, family, session)
    }

    /// The request that a raw body holds.
    pub fn read(raw: &RawBody, family: &str, session: &str) -> Result<Self, ApiError> {
        let scope = Scope::session(family, session);
        let message = required_text(&raw.0, "message", scope)?;
        let message = SteerMessage::try_from(message)
            .map_err(|_| over_bytes("message", SteerMessage::MAX_BYTES, scope))?;

        Ok(Self { message })
    }

    /// The message for the model.
    #[must_use]
    pub fn message(&self) -> &SteerMessage {
        &self.message
    }
}

/// The body of `POST .../turns/{turn}/stop`: stop a turn (contract 02 §5.7).
///
/// A `reason` of the wrong type is absent, and an empty one too. An absent
/// reason is `user_stopped`.
///
/// ```
/// use creche_contracts::session::StopRequest;
///
/// let request = StopRequest::from_body(br#"{"reason": 5}"#, "chat", "owui-8f1c2e")?;
/// assert_eq!(request.reason().as_str(), "user_stopped");
/// # Ok::<(), creche_contracts::session::ApiError>(())
/// ```
///
/// Code outside this module cannot build a value from a raw reason:
///
/// ```compile_fail,E0451
/// use creche_contracts::session::{StopReason, StopRequest};
///
/// fn stop(reason: StopReason) -> StopRequest {
///     StopRequest { reason }
/// }
/// ```
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct StopRequest {
    reason: StopReason,
}

impl StopRequest {
    /// The request that the bytes of a body hold. `family` and `session` are
    /// the two parameters of the path.
    pub fn from_body(body: &[u8], family: &str, session: &str) -> Result<Self, ApiError> {
        Self::read(&RawBody::parse(body)?, family, session)
    }

    /// The request that a raw body holds.
    pub fn read(raw: &RawBody, family: &str, session: &str) -> Result<Self, ApiError> {
        let scope = Scope::session(family, session);
        let reason = raw.0.filled_text("reason")?.map_or_else(
            || Ok(StopReason::user_stopped()),
            |reason| {
                StopReason::try_from(reason).map_err(|_| scope.bad("reason is over its limit"))
            },
        )?;

        Ok(Self { reason })
    }

    /// Why the caller stops the turn.
    #[must_use]
    pub fn reason(&self) -> &StopReason {
        &self.reason
    }
}

// --- the dispatch door and the delegate door ---

/// The four members that `POST /dispatch` and `POST /delegate` share, and the
/// advisory session id.
struct JobCall {
    caller_family: FamilyName,
    target_family: FamilyName,
    target_text: String,
    delegation_id: Ulid,
    message: JobMessage,
    claimed_session_id: Option<SessionId>,
}

fn read_job_call(raw: &Object) -> Result<JobCall, ApiError> {
    let caller_text = required_text(raw, "caller_family", Scope::NONE)?;
    let target_text = required_text(raw, "target_family", Scope::NONE)?;
    let caller_family = caller_text
        .parse()
        .map_err(|_| Scope::family(&caller_text).bad("caller_family is not a family name"))?;
    let target_family = target_text
        .parse()
        .map_err(|_| Scope::family(&target_text).bad("target_family is not a family name"))?;
    let scope = Scope::family(&target_text);
    let delegation_id = required_text(raw, "delegation_id", Scope::NONE)?
        .parse()
        .map_err(|_| scope.bad("delegation_id is not a ULID"))?;
    let message = required_text(raw, "message", scope)?;
    let message = JobMessage::try_from(message).map_err(|_| {
        over_bytes(
            "message",
            JobMessage::MAX_BYTES,
            Scope::session(&target_text, ""),
        )
    })?;
    let claimed_session_id = raw
        .filled_text("claimed_session_id")?
        .map(|claimed| claimed.parse())
        .transpose()
        .map_err(|_| scope.bad("claimed_session_id is not a session id"))?;

    Ok(JobCall {
        caller_family,
        target_family,
        target_text,
        delegation_id,
        message,
        claimed_session_id,
    })
}

/// The body of `POST /dispatch`: one family enqueues a job in another
/// (contract 02 §13.4.1). Only the chaperone sends it.
///
/// A `chain` that is absent or null is the empty chain. A
/// `claimed_session_id` or an `idempotency_key` of the wrong type is absent,
/// and an empty one too.
///
/// ```
/// use creche_contracts::session::DispatchRequest;
///
/// let request = DispatchRequest::from_body(
///     br#"{"caller_family": "chat", "target_family": "scrum-lead",
///          "delegation_id": "01K5J9QWB2M4N6Q8S0V2W4Y6A8", "message": "Triage the issues.",
///          "chain": ["chat", "scrum-lead"]}"#,
/// )?;
/// assert_eq!(request.chain().len(), 2);
/// # Ok::<(), creche_contracts::session::ApiError>(())
/// ```
///
/// Code outside this module cannot build a value from raw fields:
///
/// ```compile_fail,E0451
/// use creche_contracts::session::DispatchRequest;
///
/// fn no_chain(request: DispatchRequest) -> DispatchRequest {
///     DispatchRequest { chain: Vec::new(), ..request }
/// }
/// ```
// CONTRACT-QUESTION: contract 02 §13.4.1 says that `chain` is required. The
// Python parser takes a body with no chain and reads the empty chain. This
// type does the same. A required chain refuses a body that the Python code
// takes today.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DispatchRequest {
    caller_family: FamilyName,
    target_family: FamilyName,
    delegation_id: Ulid,
    message: JobMessage,
    chain: Vec<FamilyName>,
    claimed_session_id: Option<SessionId>,
    idempotency_key: Option<DispatchKey>,
}

impl DispatchRequest {
    /// The request that the bytes of a body hold.
    pub fn from_body(body: &[u8]) -> Result<Self, ApiError> {
        Self::try_from(&RawBody::parse(body)?)
    }

    /// The family that enqueues the job. The chaperone took it from the token
    /// of the caller.
    #[must_use]
    pub fn caller_family(&self) -> &FamilyName {
        &self.caller_family
    }

    /// The family that runs the job.
    #[must_use]
    pub fn target_family(&self) -> &FamilyName {
        &self.target_family
    }

    /// The id that the chaperone gave the delegation.
    #[must_use]
    pub fn delegation_id(&self) -> &Ulid {
        &self.delegation_id
    }

    /// The message for the job.
    #[must_use]
    pub fn message(&self) -> &JobMessage {
        &self.message
    }

    /// The delegation chain, the caller first.
    #[must_use]
    pub fn chain(&self) -> &[FamilyName] {
        &self.chain
    }

    /// The session that the caller says it runs in. It is advisory.
    #[must_use]
    pub fn claimed_session_id(&self) -> Option<&SessionId> {
        self.claimed_session_id.as_ref()
    }

    /// The idempotency key.
    #[must_use]
    pub fn idempotency_key(&self) -> Option<&DispatchKey> {
        self.idempotency_key.as_ref()
    }
}

impl TryFrom<&RawBody> for DispatchRequest {
    type Error = ApiError;

    fn try_from(raw: &RawBody) -> Result<Self, Self::Error> {
        let raw = &raw.0;
        let call = read_job_call(raw)?;
        let chain = read_chain(
            raw.get("chain"),
            TriggerKind::Dispatch,
            &call.target_text,
            "",
        )?;
        let over = || {
            Scope::family(&call.target_text).bad(format!(
                "idempotency_key is over {} characters",
                DispatchKey::MAX_CHARS
            ))
        };
        let idempotency_key = raw
            .filled_text("idempotency_key")?
            .map(DispatchKey::try_from)
            .transpose()
            .map_err(|_| over())?;

        Ok(Self {
            caller_family: call.caller_family,
            target_family: call.target_family,
            delegation_id: call.delegation_id,
            message: call.message,
            chain,
            claimed_session_id: call.claimed_session_id,
            idempotency_key,
        })
    }
}

/// The body of `POST /delegate`: the chaperone runs one thin job (contract 02
/// §12, contract 04 §7.3).
///
/// A `claimed_session_id` of the wrong type is absent, and an empty one too.
///
/// ```
/// use creche_contracts::session::DelegateRequest;
///
/// let request = DelegateRequest::from_body(
///     br#"{"caller_family": "chat", "target_family": "vault-oracle",
///          "delegation_id": "01K5J9QWB2M4N6Q8S0V2W4Y6A8", "message": "What is the reading?"}"#,
/// )?;
/// assert_eq!(request.target_family().as_str(), "vault-oracle");
/// # Ok::<(), creche_contracts::session::ApiError>(())
/// ```
///
/// Code outside this module cannot build a value from raw fields:
///
/// ```compile_fail,E0451
/// use creche_contracts::session::DelegateRequest;
///
/// fn unclaimed(request: DelegateRequest) -> DelegateRequest {
///     DelegateRequest { claimed_session_id: None, ..request }
/// }
/// ```
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DelegateRequest {
    caller_family: FamilyName,
    target_family: FamilyName,
    delegation_id: Ulid,
    message: JobMessage,
    claimed_session_id: Option<SessionId>,
}

impl DelegateRequest {
    /// The request that the bytes of a body hold.
    pub fn from_body(body: &[u8]) -> Result<Self, ApiError> {
        Self::try_from(&RawBody::parse(body)?)
    }

    /// The family that delegates. The chaperone took it from the token of the
    /// caller.
    #[must_use]
    pub fn caller_family(&self) -> &FamilyName {
        &self.caller_family
    }

    /// The thin family that runs the job.
    #[must_use]
    pub fn target_family(&self) -> &FamilyName {
        &self.target_family
    }

    /// The id that the chaperone gave the delegation.
    #[must_use]
    pub fn delegation_id(&self) -> &Ulid {
        &self.delegation_id
    }

    /// The message for the job.
    #[must_use]
    pub fn message(&self) -> &JobMessage {
        &self.message
    }

    /// The session that the caller says it runs in. For `code-sandbox` it
    /// becomes the name of a directory (contract 02 §12.1).
    #[must_use]
    pub fn claimed_session_id(&self) -> Option<&SessionId> {
        self.claimed_session_id.as_ref()
    }
}

impl TryFrom<&RawBody> for DelegateRequest {
    type Error = ApiError;

    fn try_from(raw: &RawBody) -> Result<Self, Self::Error> {
        let call = read_job_call(&raw.0)?;

        Ok(Self {
            caller_family: call.caller_family,
            target_family: call.target_family,
            delegation_id: call.delegation_id,
            message: call.message,
            claimed_session_id: call.claimed_session_id,
        })
    }
}

/// The body of `POST /dispatch/jobs`: a family reads the jobs that it
/// dispatched (contract 02 §13.4.2).
///
/// A `session`, a `since` or a `limit` of the wrong type is absent. An empty
/// `session` and an empty `since` are absent too.
///
/// ```
/// use creche_contracts::session::JobsQuery;
///
/// let query = JobsQuery::from_body(br#"{"caller_family": "scrum-lead", "limit": "5"}"#)?;
/// assert_eq!(query.limit().get(), 20);
/// # Ok::<(), creche_contracts::session::ApiError>(())
/// ```
///
/// Code outside this module cannot build a value from raw fields:
///
/// ```compile_fail,E0451
/// use creche_contracts::session::JobsQuery;
///
/// fn all(query: JobsQuery) -> JobsQuery {
///     JobsQuery { since: None, ..query }
/// }
/// ```
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct JobsQuery {
    caller_family: FamilyName,
    session: Option<SessionId>,
    since: Option<Timestamp>,
    limit: PageLimit,
}

impl JobsQuery {
    /// The query that the bytes of a body hold.
    pub fn from_body(body: &[u8]) -> Result<Self, ApiError> {
        Self::try_from(&RawBody::parse(body)?)
    }

    /// The family that reads its jobs. It is the scope of the query.
    #[must_use]
    pub fn caller_family(&self) -> &FamilyName {
        &self.caller_family
    }

    /// One job. `None` means each job that the family dispatched.
    #[must_use]
    pub fn session(&self) -> Option<&SessionId> {
        self.session.as_ref()
    }

    /// The earliest dispatch time of a job in the answer.
    #[must_use]
    pub fn since(&self) -> Option<Timestamp> {
        self.since
    }

    /// The largest count of jobs in the answer.
    #[must_use]
    pub fn limit(&self) -> PageLimit {
        self.limit
    }
}

impl TryFrom<&RawBody> for JobsQuery {
    type Error = ApiError;

    fn try_from(raw: &RawBody) -> Result<Self, Self::Error> {
        let raw = &raw.0;
        let caller_text = required_text(raw, "caller_family", Scope::NONE)?;
        let scope = Scope::family(&caller_text);
        let caller_family = caller_text
            .parse()
            .map_err(|_| scope.bad("caller_family is not a family name"))?;
        let session = raw
            .filled_text("session")?
            .map(|session| {
                session.parse().map_err(|_| {
                    Scope::session(&caller_text, &session).bad("session is not a session id")
                })
            })
            .transpose()?;
        let since = raw
            .filled_text("since")?
            .map(|since| since.parse())
            .transpose()
            .map_err(|_| scope.bad("since is not RFC 3339"))?;
        let limit = bounded(raw.int("limit"), PageLimit::JOBS, || {
            bad("limit is outside 1 to 200")
        })?;

        Ok(Self {
            caller_family,
            session,
            since,
            limit,
        })
    }
}

// --- the sandbox switch ---

/// The body of `POST /internal/switch-sandbox`: `caregiver` replaces the
/// sandbox of a family (contract 05 §5.1).
///
/// A `reason`, a `from` or a `deadline_s` of the wrong type is absent. An
/// empty `from` is absent too.
///
/// The parser checks no grammar for `family`, `to` and `from`. Each one is a
/// text with one character or more.
///
/// ```
/// use creche_contracts::session::{SwitchMode, SwitchRequest};
///
/// let request = SwitchRequest::from_body(
///     br#"{"family": "chat", "to": "chat-s3", "mode": "drain"}"#,
/// )?;
/// assert_eq!(request.mode(), SwitchMode::Drain);
/// assert_eq!(request.deadline_s().get(), 300);
/// # Ok::<(), creche_contracts::session::ApiError>(())
/// ```
///
/// Code outside this module cannot build a value from raw fields:
///
/// ```compile_fail,E0451
/// use creche_contracts::session::{SwitchMode, SwitchRequest};
///
/// fn interrupt(request: SwitchRequest) -> SwitchRequest {
///     SwitchRequest { mode: SwitchMode::Interrupt, ..request }
/// }
/// ```
// CONTRACT-QUESTION: contract 05 §5.1 says that `family` is a family name and
// that `to` and `from` are sandbox ids. The Python parser checks none of the
// three. The service checks them after the parse. This type keeps each one as
// text, as the Python parser does. A check here refuses a body with another
// message than the service gives today.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SwitchRequest {
    family: String,
    to: String,
    mode: SwitchMode,
    reason: String,
    outgoing: Option<String>,
    deadline_s: DeadlineS,
}

impl SwitchRequest {
    /// The request that the bytes of a body hold.
    pub fn from_body(body: &[u8]) -> Result<Self, ApiError> {
        Self::try_from(&RawBody::parse(body)?)
    }

    /// The family, as the body gives it.
    #[must_use]
    pub fn family(&self) -> &str {
        &self.family
    }

    /// The sandbox that serves the family after the switch, as the body gives
    /// it.
    #[must_use]
    pub fn to(&self) -> &str {
        &self.to
    }

    /// Whether the running turns end first or stop.
    #[must_use]
    pub fn mode(&self) -> SwitchMode {
        self.mode
    }

    /// Why `caregiver` switches. It can be empty.
    #[must_use]
    pub fn reason(&self) -> &str {
        &self.reason
    }

    /// The sandbox that served the family before the switch: the member `from`
    /// of the body.
    #[must_use]
    pub fn outgoing(&self) -> Option<&str> {
        self.outgoing.as_deref()
    }

    /// How long a drain can take.
    #[must_use]
    pub fn deadline_s(&self) -> DeadlineS {
        self.deadline_s
    }
}

impl TryFrom<&RawBody> for SwitchRequest {
    type Error = ApiError;

    fn try_from(raw: &RawBody) -> Result<Self, Self::Error> {
        let raw = &raw.0;
        let family = required_text(raw, "family", Scope::NONE)?;
        let to = required_text(raw, "to", Scope::NONE)?;
        let mode = required_text(raw, "mode", Scope::NONE)?
            .parse()
            .map_err(|_| Scope::family(&family).bad("mode is not drain or interrupt"))?;
        let reason = raw.filled_text("reason")?.unwrap_or_default();
        let outgoing = raw.filled_text("from")?;
        let deadline_s = bounded(raw.int("deadline_s"), DeadlineS::SWITCH, || {
            bad("deadline_s is outside 1 to 3600")
        })?;

        Ok(Self {
            family,
            to,
            mode,
            reason,
            outgoing,
            deadline_s,
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_body_that_is_no_object_has_one_of_two_messages() {
        let not_json = RawBody::parse(b"not json").unwrap_err();
        let not_object = RawBody::parse(b"[]").unwrap_err();

        assert_eq!(not_json.message(), "body is not JSON");
        assert_eq!(not_object.message(), "body is not a JSON object");
        for refusal in [not_json, not_object] {
            assert_eq!(refusal.code(), ErrorCode::BadRequest);
            assert_eq!((refusal.family(), refusal.session()), (None, None));
        }
    }

    #[test]
    fn a_refusal_names_what_the_caller_sent() {
        let family = CreateRequest::from_body(br#"{"family": "Chat", "session": ".."}"#);
        let session = CreateRequest::from_body(br#"{"family": "chat", "session": ".."}"#);
        let prompt = RunTurnRequest::from_body(b"{}", "any family", "any session");
        let family = family.unwrap_err();
        let session = session.unwrap_err();
        let prompt = prompt.unwrap_err();

        assert_eq!(family.message(), "family is not a family name");
        assert_eq!((family.family(), family.session()), (Some("Chat"), None));
        assert_eq!(session.message(), "session is not a session id");
        assert_eq!(
            (session.family(), session.session()),
            (Some("chat"), Some(".."))
        );
        assert_eq!(prompt.message(), "prompt is missing or not a string");
        assert_eq!(
            (prompt.family(), prompt.session()),
            (Some("any family"), Some("any session"))
        );
    }

    #[test]
    fn a_text_over_its_cap_in_bytes_is_too_large() {
        let body = format!(r#"{{"message": "{}"}}"#, "m".repeat(4097));
        let refusal = SteerRequest::from_body(body.as_bytes(), "chat", "owui-1").unwrap_err();

        assert_eq!(refusal.code(), ErrorCode::PayloadTooLarge);
        assert_eq!(refusal.http_status(), 413);
        assert_eq!(refusal.message(), "message is over its 4096 byte limit");
    }

    #[test]
    fn the_message_of_a_job_names_an_empty_session() {
        let body = format!(
            r#"{{"caller_family": "chat", "target_family": "scrum-lead",
                "delegation_id": "01K5J9QWB2M4N6Q8S0V2W4Y6A8", "message": "{}"}}"#,
            "m".repeat(65_537)
        );
        let refusal = DispatchRequest::from_body(body.as_bytes()).unwrap_err();

        assert_eq!(refusal.code(), ErrorCode::PayloadTooLarge);
        assert_eq!(
            (refusal.family(), refusal.session()),
            (Some("scrum-lead"), Some(""))
        );
    }

    #[test]
    fn the_trigger_object_wins_over_the_three_labels() {
        let body = br#"{"prompt": "x", "trigger": {"kind": "webhook", "name": "from-the-object"},
            "labels": {"trigger_kind": "timer", "trigger_name": "from-the-labels"}}"#;
        let from_labels = br#"{"prompt": "x", "trigger": "webhook",
            "labels": {"trigger_kind": "timer", "trigger_name": ""}}"#;
        let object = RunTurnRequest::from_body(body, "chat", "auto-1").unwrap();
        let labels = RunTurnRequest::from_body(from_labels, "chat", "auto-1").unwrap();

        assert_eq!(
            object.trigger().map(Trigger::kind),
            Some(TriggerKind::Webhook)
        );
        assert_eq!(
            object.trigger().and_then(Trigger::name),
            Some("from-the-object")
        );
        assert_eq!(
            labels.trigger().map(Trigger::kind),
            Some(TriggerKind::Timer)
        );
        assert_eq!(labels.trigger().and_then(Trigger::name), Some(""));
    }

    #[test]
    fn a_chain_refusal_names_the_session_but_for_a_bad_name() {
        let body = |chain: &str| {
            format!(r#"{{"prompt": "x", "trigger": {{"kind": "dispatch", "chain": {chain}}}}}"#)
        };
        let read = |chain: &str| {
            RunTurnRequest::from_body(body(chain).as_bytes(), "chat", "auto-1").unwrap_err()
        };

        assert_eq!(read("\"chat\"").session(), Some("auto-1"));
        assert_eq!(read("[\"Chat\"]").session(), None);
        assert_eq!(read("[\"Chat\"]").family(), Some("chat"));
        assert_eq!(
            read("[5]").message(),
            "trigger chain holds a name that is not a family"
        );
    }
}
