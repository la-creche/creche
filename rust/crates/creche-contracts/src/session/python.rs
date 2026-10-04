//! The differential test of the session module: each vector of each surface
//! `session.*` of `vectors/data`, against the Rust types.
//!
//! `SURFACES` names each surface and the function that replays one vector of
//! it. A vector outside `DEVIATIONS` must be equal: the Rust code accepts and
//! refuses what the Python code does, with the same value, the same refusal
//! and the same bytes.

use std::collections::HashSet;

use serde::de::DeserializeOwned;
use serde_json::{Map, Value, json};

use super::*;
use crate::ids::{FamilyName, SandboxName, SessionId, Ulid};
use crate::vectors::{self, Marker, Outcome, Vector};

/// What the code did with one input, in the fields of a vector.
#[derive(Debug, Clone, PartialEq)]
enum Did {
    /// The code took the input. `value` is what it parsed the input into, and
    /// `output` is the text of the bytes that it wrote.
    Accepted {
        value: Option<Value>,
        output: Option<String>,
    },
    /// The code refused the input, with this refusal when it gives one.
    Refused(Option<Value>),
}

/// The facts that each vector of one file shares: the parameters of the path.
type Context = Map<String, Value>;

/// One surface, and how the Rust code replays one vector of it.
struct Against {
    surface: &'static str,
    replay: fn(&Vector, &Context) -> Did,
}

const fn against(surface: &'static str, replay: fn(&Vector, &Context) -> Did) -> Against {
    Against { surface, replay }
}

const SURFACES: &[Against] = &[
    against("session.request.create", create),
    against("session.request.run_turn", run_turn),
    against("session.request.writer", writer),
    against("session.request.steer", steer),
    against("session.request.stop", stop),
    against("session.request.dispatch", dispatch),
    against("session.request.jobs", jobs),
    against("session.request.delegate", delegate),
    against("session.request.switch", switch),
    against("session.query.list", list),
    against("session.query.get", get),
    against("session.query.events", events),
    against("session.error_body", error_body),
    against("session.answer.session", answer_session),
    against("session.answer.turn", answer_turn),
    against("session.answer.lease", answer_lease),
    against("session.stream.encode", stream_encode),
    against("session.journal.read", journal_read),
    against("session.journal.write", journal_write),
    against("session.stream.live", stream_live),
    against("session.outcome.write", outcome_write),
    against("session.turn.move", turn_move),
    against("session.state.derive", state_derive),
    against("session.outcome.status", outcome_status),
];

/// The surface whose vectors hold the HTTP status of the answer beside the
/// bytes. The Rust type gives that status.
const ERROR_BODY: &str = "session.error_body";

// --- how the test reads a vector ---

fn utf8(bytes: Vec<u8>) -> String {
    String::from_utf8(bytes).unwrap()
}

fn object(value: Value) -> Map<String, Value> {
    match value {
        Value::Object(object) => object,
        other => panic!("{other} is not an object"),
    }
}

fn args(vector: &Vector) -> &Map<String, Value> {
    vector.input.args().unwrap()
}

/// One argument that is a text or null.
fn text<'a>(args: &'a Map<String, Value>, name: &str) -> Option<&'a str> {
    match &args[name] {
        Value::Null => None,
        Value::String(text) => Some(text),
        other => panic!("{name} is {other}"),
    }
}

/// One parameter of a query. A query does not send a parameter that is null
/// or absent.
fn param<'a>(args: &'a Map<String, Value>, name: &str) -> Option<&'a str> {
    args.get(name).and_then(Value::as_str)
}

fn time(text: &str) -> Timestamp {
    text.parse().unwrap()
}

/// A time as Python's `isoformat` writes it in UTC: `+00:00`, and six digits
/// of fraction when the fraction is not zero.
fn python_iso(instant: Timestamp) -> String {
    let seconds = instant.rfc3339();
    let seconds = seconds.strip_suffix('Z').unwrap();
    let micros = instant.unix_micros().rem_euclid(1_000_000);

    if micros == 0 {
        format!("{seconds}+00:00")
    } else {
        format!("{seconds}.{micros:06}+00:00")
    }
}

/// A value of a vector with each `$int` marker as a float: what a reader with
/// no integer past 64 bits holds.
fn unmark(value: &Value) -> Value {
    match (Marker::of(value), value) {
        (Some(Marker::Int(digits)), _) => json!(digits.parse::<f64>().unwrap()),
        (Some(marker), _) => panic!("{marker:?} has no value here"),
        (None, Value::Array(items)) => items.iter().map(unmark).collect(),
        (None, Value::Object(fields)) => Value::Object(
            fields
                .iter()
                .map(|(key, value)| (key.clone(), unmark(value)))
                .collect(),
        ),
        (None, plain) => plain.clone(),
    }
}

/// The value of a vector. A value that nests deep is in a `$json` marker. The
/// marker stays when its text nests deeper than this reader goes.
fn value_of(vector: &Vector) -> Option<Value> {
    let value = vector.value()?;
    let Some(Marker::Json(text)) = Marker::of(value) else {
        return Some(value.clone());
    };

    Some(serde_json::from_str(&text).unwrap_or_else(|_| value.clone()))
}

/// What the Python code did with one input.
fn python_did(vector: &Vector, surface: &str) -> Did {
    if vector.result != Outcome::Accepted {
        return Did::Refused(vector.refusal().cloned());
    }

    let value = match vector.field("http_status") {
        Some(status) if surface == ERROR_BODY => Some(json!({"http_status": status})),
        _ => value_of(vector),
    };
    let output = vector
        .field("output")
        .map(|output| output["text"].as_str().unwrap().to_owned());

    Did::Accepted { value, output }
}

/// What the Python code did, with a value that this reader can hold.
///
/// A `$json` marker that nests deeper than this reader goes stays a marker.
/// Its text is compact JSON with sorted keys, and so is the text of the Rust
/// value. When the two texts are equal, the two values are equal.
fn readable(python: Did, rust: &Did) -> Did {
    let (
        Did::Accepted {
            value: Some(marker),
            output,
        },
        Did::Accepted {
            value: Some(value), ..
        },
    ) = (&python, rust)
    else {
        return python;
    };
    match Marker::of(marker) {
        Some(Marker::Json(text)) if serde_json::to_string(value).unwrap() == text => {
            Did::Accepted {
                value: Some(value.clone()),
                output: output.clone(),
            }
        }
        _ => python,
    }
}

// --- how the test writes what the Rust code did ---

fn refusal_of(refusal: &ApiError) -> Did {
    let body: Value = serde_json::from_slice(&refusal.body().unwrap()).unwrap();

    Did::Refused(Some(
        json!({"http_status": refusal.http_status(), "body": body}),
    ))
}

/// A request that the Rust code parsed, as the value of a vector.
fn parsed<T>(read: Result<T, ApiError>, value: impl Fn(&T) -> Value) -> Did {
    match read {
        Ok(request) => Did::Accepted {
            value: Some(value(&request)),
            output: None,
        },
        Err(refusal) => refusal_of(&refusal),
    }
}

/// The bytes that the Rust code wrote, as the output of a vector.
fn written(bytes: Result<Vec<u8>, EncodeError>) -> Did {
    Did::Accepted {
        value: None,
        output: Some(utf8(bytes.unwrap())),
    }
}

fn labels(labels: &Labels) -> Value {
    Value::Object(
        labels
            .iter()
            .map(|(key, value)| (key.to_owned(), json!(value)))
            .collect(),
    )
}

fn names(names: &[FamilyName]) -> Vec<&str> {
    names.iter().map(FamilyName::as_str).collect()
}

fn trigger(trigger: &Trigger) -> Value {
    json!({
        "kind": trigger.kind().as_str(),
        "name": trigger.name(),
        "fired_at": trigger.fired_at().map(python_iso),
        "chain": names(trigger.chain()),
    })
}

fn owui(refs: &OwuiRefs) -> Value {
    json!({
        "chat_id": refs.chat_id(),
        "message_id": refs.message_id(),
        "user_message_id": refs.user_message_id(),
        "parent_id": refs.parent_id(),
    })
}

fn path<'a>(context: &'a Context, name: &str) -> &'a str {
    context[name].as_str().unwrap()
}

// --- the request bodies ---

fn body(vector: &Vector) -> Vec<u8> {
    vector.input.bytes().unwrap()
}

fn create(vector: &Vector, _: &Context) -> Did {
    parsed(CreateRequest::from_body(&body(vector)), |request| {
        json!({
            "family": request.family().as_str(),
            "session": request.session().as_str(),
            "title": request.title().as_str(),
            "labels": labels(request.labels()),
            "owner_session": request.owner_session().map(SessionId::as_str),
        })
    })
}

fn run_turn(vector: &Vector, context: &Context) -> Did {
    let read = RunTurnRequest::from_body(
        &body(vector),
        path(context, "family"),
        path(context, "session"),
    );

    parsed(read, |request| {
        let attachments: Vec<&str> = request
            .attachments()
            .iter()
            .map(|name| name.as_str())
            .collect();

        json!({
            "prompt": request.prompt().as_str(),
            "idempotency_key": request.idempotency_key().map(IdempotencyKey::as_str),
            "wait": request.wait().as_str(),
            "persona_text": request.persona_text(),
            "attachments": attachments,
            "owui": request.owui().map(owui),
            "trigger": request.trigger().map(trigger),
            "deadline_s": request.deadline_s().get(),
            "labels": labels(request.labels()),
            "delegation": null,
        })
    })
}

fn writer(vector: &Vector, context: &Context) -> Did {
    let read = WriterRequest::from_body(
        &body(vector),
        path(context, "family"),
        path(context, "session"),
    );

    parsed(read, |request| {
        json!({
            "holder": request.holder().map(Holder::as_str),
            "force": request.force(),
            "intent": request.intent().as_str(),
        })
    })
}

fn steer(vector: &Vector, context: &Context) -> Did {
    let read = SteerRequest::from_body(
        &body(vector),
        path(context, "family"),
        path(context, "session"),
    );

    parsed(
        read,
        |request| json!({"message": request.message().as_str()}),
    )
}

fn stop(vector: &Vector, context: &Context) -> Did {
    let read = StopRequest::from_body(
        &body(vector),
        path(context, "family"),
        path(context, "session"),
    );

    parsed(read, |request| json!({"reason": request.reason().as_str()}))
}

fn dispatch(vector: &Vector, _: &Context) -> Did {
    parsed(DispatchRequest::from_body(&body(vector)), |request| {
        json!({
            "caller_family": request.caller_family().as_str(),
            "target_family": request.target_family().as_str(),
            "delegation_id": request.delegation_id().as_str(),
            "message": request.message().as_str(),
            "chain": names(request.chain()),
            "claimed_session_id": request.claimed_session_id().map(SessionId::as_str),
            "idempotency_key": request.idempotency_key().map(DispatchKey::as_str),
        })
    })
}

fn jobs(vector: &Vector, _: &Context) -> Did {
    parsed(JobsQuery::from_body(&body(vector)), |query| {
        json!({
            "caller_family": query.caller_family().as_str(),
            "session": query.session().map(SessionId::as_str),
            "since": query.since().map(python_iso),
            "limit": query.limit().get(),
        })
    })
}

fn delegate(vector: &Vector, _: &Context) -> Did {
    parsed(DelegateRequest::from_body(&body(vector)), |request| {
        json!({
            "caller_family": request.caller_family().as_str(),
            "target_family": request.target_family().as_str(),
            "delegation_id": request.delegation_id().as_str(),
            "message": request.message().as_str(),
            "claimed_session_id": request.claimed_session_id().map(SessionId::as_str),
        })
    })
}

fn switch(vector: &Vector, _: &Context) -> Did {
    parsed(SwitchRequest::from_body(&body(vector)), |request| {
        json!({
            "family": request.family(),
            "to": request.to(),
            "mode": request.mode().as_str(),
            "reason": request.reason(),
            "outgoing": request.outgoing(),
            "deadline_s": request.deadline_s().get(),
        })
    })
}

// --- the queries ---

fn list(vector: &Vector, _: &Context) -> Did {
    let args = args(vector);
    let raw = RawListQuery {
        family: param(args, "family"),
        state: param(args, "state"),
        kind: param(args, "kind"),
        limit: param(args, "limit"),
        cursor: param(args, "cursor"),
    };

    parsed(ListQuery::try_from(raw), |query| {
        json!({
            "family": query.family().map(FamilyName::as_str),
            "state": query.state().map(SessionState::as_str),
            "kind": query.kind().map(SessionKind::as_str),
            "limit": query.limit().get(),
            "cursor": query.cursor(),
        })
    })
}

fn get(vector: &Vector, _: &Context) -> Did {
    let read = TurnsWanted::from_param(param(args(vector), "turns"));

    parsed(read, |turns| json!({"turns": turns.get()}))
}

fn events(vector: &Vector, _: &Context) -> Did {
    let args = args(vector);
    let raw = RawEventsQuery {
        from_seq: param(args, "from_seq"),
        turn: param(args, "turn"),
        follow: param(args, "follow"),
    };

    parsed(EventsQuery::try_from(raw), |query| {
        json!({
            "from_seq": query.from_seq(),
            "turn": query.turn().map(TurnRef::as_str),
            "follow": query.follow().as_str(),
        })
    })
}

// --- the answers ---

fn error_body(vector: &Vector, _: &Context) -> Did {
    let args = args(vector);
    let code: ErrorCode = args["code"].as_str().unwrap().parse().unwrap();
    let mut refusal = ApiError::new(code, args["message"].as_str().unwrap());
    if let Some(family) = text(args, "family") {
        refusal = refusal.in_family(family);
    }
    if let Some(session) = text(args, "session") {
        refusal = refusal.in_session(session);
    }
    if let Some(turn) = text(args, "turn") {
        refusal = refusal.in_turn(turn);
    }
    if let Value::Object(detail) = &args["detail"] {
        refusal = refusal.with_detail(ErrorDetail::from(detail.clone()));
    }

    Did::Accepted {
        value: Some(json!({"http_status": refusal.http_status()})),
        output: Some(utf8(refusal.body().unwrap())),
    }
}

/// The arguments of a vector as a raw object of an answer.
fn raw<T: DeserializeOwned>(vector: &Vector) -> T {
    serde_json::from_value(Value::Object(args(vector).clone())).unwrap()
}

fn answer_session(vector: &Vector, _: &Context) -> Did {
    written(
        SessionView::try_from(raw::<RawSession>(vector))
            .unwrap()
            .body(),
    )
}

fn answer_turn(vector: &Vector, _: &Context) -> Did {
    written(TurnView::try_from(raw::<RawTurn>(vector)).unwrap().body())
}

fn answer_lease(vector: &Vector, _: &Context) -> Did {
    written(Lease::try_from(raw::<RawLease>(vector)).unwrap().body())
}

// --- the journal and the event stream ---

fn turn_id(args: &Map<String, Value>) -> Option<Ulid> {
    text(args, "turn").map(|turn| turn.parse().unwrap())
}

fn line(seq: u64, ts: &str, args: &Map<String, Value>) -> JournalLine {
    let kind: LineKind = args["kind"].as_str().unwrap().parse().unwrap();
    let body = JournalBody::read(kind, object(unmark(&args["body"]))).unwrap();

    JournalLine::new(
        JournalSeq::try_from(seq).unwrap(),
        time(ts),
        turn_id(args),
        body,
    )
}

fn journal_write(vector: &Vector, _: &Context) -> Did {
    let args = args(vector);
    let seq = args["last_seq"].as_u64().unwrap() + 1;

    written(line(seq, args["moment"].as_str().unwrap(), args).encode())
}

/// The record of the stream that has these fields.
fn record(seq: Option<u64>, ts: &str, args: &Map<String, Value>) -> StreamRecord {
    let last_seq = || args["body"]["last_seq"].as_u64().unwrap();
    match (seq, args["kind"].as_str().unwrap()) {
        (Some(seq), _) => StreamRecord::Line(line(seq, ts, args)),
        (None, "heartbeat") => StreamRecord::Heartbeat {
            ts: time(ts),
            last_seq: last_seq(),
        },
        (None, "note") if args["body"]["note"] == "stream_overrun" => StreamRecord::Overrun {
            ts: time(ts),
            last_seq: last_seq(),
        },
        (None, kind) => panic!("a {kind} record has a sequence number"),
    }
}

fn stream_encode(vector: &Vector, _: &Context) -> Did {
    let args = args(vector);
    let ts = args["ts"].as_str().unwrap();

    written(record(args["journal_seq"].as_u64(), ts, args).encode())
}

/// Any time. The vectors of `session.stream.live` hold no time.
const ANY_TIME: &str = "2026-10-05T19:22:31.070Z";

/// The Rust code has no stream. The test makes the record that the Python
/// stream made, with the Rust type, and compares each field but the time.
fn stream_live(vector: &Vector, _: &Context) -> Did {
    let python = object(vector.value().unwrap().clone());
    let record = record(None, ANY_TIME, &python);
    let mut value: Value = serde_json::from_slice(&record.encode().unwrap()).unwrap();

    assert_eq!(record.kind().as_str(), python["kind"]);
    assert_eq!(object(value.clone()).remove("ts"), Some(json!(ANY_TIME)));
    value.as_object_mut().unwrap().remove("ts");

    Did::Accepted {
        value: Some(value),
        output: None,
    }
}

fn journal_read(vector: &Vector, _: &Context) -> Did {
    let Ok(line) = StoredLine::parse(&body(vector)) else {
        return Did::Refused(None);
    };
    let body: Value = serde_json::from_str(line.body_json()).unwrap_or(Value::Null);

    Did::Accepted {
        value: Some(json!({
            "journal_seq": line.journal_seq().get(),
            "ts": line.ts().map(python_iso),
            "kind": line.kind().as_str(),
            "turn": line.turn().map(TurnRef::as_str),
            "body": body,
        })),
        output: None,
    }
}

// --- the outcome record ---

fn outcome_write(vector: &Vector, _: &Context) -> Did {
    let args = args(vector);
    let trigger = match &args["trigger"] {
        Value::Null => None,
        Value::Object(trigger) => {
            let chain = trigger.get("chain").and_then(Value::as_array);
            let chain = chain.into_iter().flatten();

            Some(
                Trigger::new(
                    trigger["kind"].as_str().unwrap().parse().unwrap(),
                    text(trigger, "name").map(str::to_owned),
                    text(trigger, "fired_at").map(time),
                    chain
                        .map(|name| name.as_str().unwrap().parse().unwrap())
                        .collect(),
                )
                .unwrap(),
            )
        }
        other => panic!("the trigger is {other}"),
    };
    let spend = match args["spend_usd"].as_f64() {
        Some(usd) => Spend::Known(usd),
        None => Spend::Unknown(args["spend_reason"].as_str().unwrap().to_owned()),
    };
    let sandbox = Some(args["sandbox"].as_str().unwrap()).filter(|sandbox| !sandbox.is_empty());
    let raw = RawOutcome {
        id: args["id"].as_str().unwrap().parse().unwrap(),
        family: args["family"].as_str().unwrap().parse().unwrap(),
        session: args["session"].as_str().unwrap().parse().unwrap(),
        trigger,
        started_at: time(args["started_at"].as_str().unwrap()),
        ended_at: time(args["ended_at"].as_str().unwrap()),
        status: args["status"].as_str().unwrap().parse().unwrap(),
        error: text(args, "error").map(str::to_owned),
        turns: args["turns"].as_u64().unwrap(),
        approvals: serde_json::from_value(args["approvals"].clone()).unwrap(),
        spend,
        sandbox: sandbox.map(|sandbox| sandbox.parse::<SandboxName>().unwrap()),
    };

    written(OutcomeRecord::try_from(raw).unwrap().encode())
}

// --- the states ---

fn run() -> Run {
    Run::new(time("2026-10-05T19:22:05Z"), "chat-s2".parse().unwrap())
}

const END: &str = "2026-10-05T19:22:31Z";

/// A turn in one state. The reason is the reason of a turn that failed or
/// stopped.
fn turn_in(state: TurnState, reason: TurnReason) -> Turn {
    let at = time(END);
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

/// Each move that leads to one state.
fn steps_to(wanted: TurnState) -> Vec<Step> {
    let at = time(END);
    let steps = [
        Step::Start(run()),
        Step::OpenGate,
        Step::CloseGate,
        Step::Settle { at },
        Step::Fail {
            at,
            reason: TurnReason::Internal,
        },
        Step::Abort {
            at,
            reason: TurnReason::UserStopped,
        },
    ];

    steps
        .into_iter()
        .filter(|step| step.target() == wanted)
        .collect()
}

fn turn_move(vector: &Vector, _: &Context) -> Did {
    let args = args(vector);
    let current: TurnState = args["current"].as_str().unwrap().parse().unwrap();
    let wanted: TurnState = args["wanted"].as_str().unwrap().parse().unwrap();
    let moved: Vec<Turn> = steps_to(wanted)
        .into_iter()
        .filter_map(|step| turn_in(current, TurnReason::Internal).apply(step).ok())
        .collect();

    assert!(moved.iter().all(|turn| turn.state() == wanted));
    if moved.is_empty() {
        return Did::Refused(None);
    }

    Did::Accepted {
        value: None,
        output: None,
    }
}

fn state_derive(vector: &Vector, _: &Context) -> Did {
    let args = args(vector);
    let turns: Vec<TurnState> = args["turns"]
        .as_array()
        .unwrap()
        .iter()
        .map(|state| state.as_str().unwrap().parse().unwrap())
        .collect();
    let last_ended = match args["last_settled_failed"].as_bool().unwrap() {
        true => LastEnded::Failed,
        false => LastEnded::NotFailed,
    };

    Did::Accepted {
        value: Some(json!(session_state(&turns, last_ended).as_str())),
        output: None,
    }
}

/// The Python entry point takes each state with each reason. A [`Turn`] holds
/// less: a settled turn has no reason, and a turn that failed or stopped has
/// one. The test gives such a turn the reason `internal` or `user_stopped`
/// where the vector has none. The status of the job is the same.
fn outcome_status(vector: &Vector, _: &Context) -> Did {
    let args = args(vector);
    let state: TurnState = args["state"].as_str().unwrap().parse().unwrap();
    let reason = text(args, "reason").map(|reason| reason.parse::<TurnReason>().unwrap());
    let absent = match state {
        TurnState::Aborted => TurnReason::UserStopped,
        _ => TurnReason::Internal,
    };
    let last = turn_in(state, reason.unwrap_or(absent));

    Did::Accepted {
        value: Some(json!(JobStatus::of_last_turn(&last).unwrap().as_str())),
        output: None,
    }
}

// --- each difference on purpose ---

/// How the Rust code differs from the Python code on one vector.
#[derive(Debug, Clone, Copy)]
enum Differs {
    /// The Python code accepts the input. The Rust code refuses it.
    Refuses,
    /// Both accept the input. The Rust value holds this JSON value in this
    /// field, and the two values are equal in each other field.
    FieldIs(&'static str, &'static str),
    /// Both accept the input. The bytes that they write differ.
    WritesOtherBytes,
    /// Both accept the line, and the two records are equal. The Rust reader
    /// keeps the body as this JSON text. The reader of this test has no value
    /// for that text, so the test compares the text.
    KeepsBodyText(&'static str),
}

/// One decision to differ from the Python code. It holds for each vector of
/// `vectors` in each surface of `surfaces`.
struct Deviation {
    surfaces: &'static [&'static str],
    vectors: &'static [&'static str],
    differs: Differs,
    /// The contract section that the decision reads.
    contract: &'static str,
    /// The decision, and its reason.
    decision: &'static str,
}

const STRICT_JSON: &str = "The contract says that a body is JSON. Python's reader takes more: a \
    byte order mark, UTF-16 and UTF-32, the words NaN and Infinity, and a lone surrogate in a \
    text. The Rust reader is serde_json, which takes JSON in UTF-8 only. A Rust text cannot \
    hold a lone surrogate.";

const DEPTH_LIMIT: &str = "The contract gives no nesting limit. Python reads a text until the \
    recursion limit of the interpreter, which differs between two versions. The Rust reader \
    stops at 128 levels, the limit of serde_json for a typed value.";

const ASCII_DIGITS: &str = "The contract says that the parameter is an int. Python's int reads \
    a decimal digit of each script. The Rust code reads the digits 0 to 9 (rust/AGENTS.md, \
    rule 9).";

const SEQ_FITS_64_BITS: &str = "The contract says that journal_seq is an integer from 1. \
    Python holds an integer of each size. The Rust type holds 64 bits, and no journal has a \
    longer number.";

/// Each vector on which the Rust code differs from the Python code on
/// purpose. A vector outside this table must be equal.
const DEVIATIONS: &[Deviation] = &[
    Deviation {
        surfaces: &["session.request.create"],
        vectors: &[
            "json-nan-unknown-field",
            "json-infinity-unknown-field",
            "bytes-bom",
            "bytes-utf16",
            "bytes-utf16-le-no-bom",
            "bytes-utf32",
        ],
        differs: Differs::Refuses,
        contract: "contract 02 §3 rule 3",
        decision: STRICT_JSON,
    },
    Deviation {
        surfaces: &["session.request.run_turn"],
        vectors: &["deadline-nan"],
        differs: Differs::Refuses,
        contract: "contract 02 §3 rule 3",
        decision: STRICT_JSON,
    },
    Deviation {
        surfaces: &["session.request.create"],
        vectors: &["json-deep-129"],
        differs: Differs::Refuses,
        contract: "contract 02 §3 rule 3",
        decision: DEPTH_LIMIT,
    },
    Deviation {
        surfaces: &["session.query.list"],
        vectors: &["limit-arabic-digits", "limit-fullwidth-digit"],
        differs: Differs::Refuses,
        contract: "contract 02 §5.2",
        decision: ASCII_DIGITS,
    },
    Deviation {
        surfaces: &["session.query.get"],
        vectors: &["turns-arabic-digits", "turns-fullwidth-digit"],
        differs: Differs::Refuses,
        contract: "contract 02 §5.3",
        decision: ASCII_DIGITS,
    },
    Deviation {
        surfaces: &["session.query.events"],
        vectors: &["from-seq-arabic-digits", "from-seq-fullwidth-digit"],
        differs: Differs::Refuses,
        contract: "contract 02 §5.5",
        decision: ASCII_DIGITS,
    },
    Deviation {
        surfaces: &["session.query.events"],
        vectors: &["from-seq-past-64-bits", "from-seq-4300-digits"],
        differs: Differs::Refuses,
        contract: "contract 02 §2, §5.5",
        decision: SEQ_FITS_64_BITS,
    },
    Deviation {
        surfaces: &["session.journal.read"],
        vectors: &["seq-past-64-bits"],
        differs: Differs::Refuses,
        contract: "contract 02 §2, §8",
        decision: SEQ_FITS_64_BITS,
    },
    Deviation {
        surfaces: &["session.journal.read"],
        vectors: &["seq-true"],
        differs: Differs::FieldIs("journal_seq", "1"),
        contract: "contract 02 §8",
        decision: "The contract says that journal_seq is an int. Python reads the JSON value \
            true as an int and keeps it as true. The Rust reader accepts the line too, and \
            keeps the number 1.",
    },
    Deviation {
        surfaces: &["session.journal.read"],
        vectors: &["body-nan", "line-bom"],
        differs: Differs::Refuses,
        contract: "contract 02 §8",
        decision: STRICT_JSON,
    },
    Deviation {
        surfaces: &["session.journal.read"],
        vectors: &["body-deep-129"],
        differs: Differs::Refuses,
        contract: "contract 02 §8",
        decision: DEPTH_LIMIT,
    },
    Deviation {
        surfaces: &["session.journal.read"],
        vectors: &["body-past-64-bits"],
        differs: Differs::KeepsBodyText(r#"{"a":18446744073709551616}"#),
        contract: "contract 02 §8",
        decision: "No difference in what the reader accepts. The body holds an integer past \
            64 bits, and the Rust reader keeps each digit of it.",
    },
    Deviation {
        surfaces: &["session.journal.read"],
        vectors: &["body-lone-surrogate"],
        differs: Differs::KeepsBodyText("{\"a\":\"\\ud800\"}"),
        contract: "contract 02 §8",
        decision: "No difference in what the reader accepts. The body holds a lone surrogate \
            escape, and the Rust reader keeps the escape.",
    },
    Deviation {
        surfaces: &["session.journal.write"],
        vectors: &["pi-event-past-64-bits"],
        differs: Differs::WritesOtherBytes,
        contract: "contract 02 §8.1",
        decision: "The contract says that the body of a pi_event is the event of pi, with no \
            change. Python keeps an integer of each size. The body here is a JSON value of \
            serde_json, which holds an integer past 64 bits as a float.",
    },
];

/// The decision that covers one vector of one surface.
fn deviation_of(surface: &str, vector: &str) -> Option<&'static Deviation> {
    DEVIATIONS.iter().find(|deviation| {
        deviation.surfaces.contains(&surface) && deviation.vectors.contains(&vector)
    })
}

/// The count of vectors of one surface that `DEVIATIONS` covers.
fn deviations_in(surface: &str) -> usize {
    DEVIATIONS
        .iter()
        .filter(|deviation| deviation.surfaces.contains(&surface))
        .map(|deviation| deviation.vectors.len())
        .sum()
}

fn accepted_value(did: &Did, at: &str) -> Map<String, Value> {
    match did {
        Did::Accepted {
            value: Some(value), ..
        } => object(value.clone()),
        other => panic!("{at}: {other:?} has no value"),
    }
}

/// Makes sure that the Rust code differs from the vector as the decision says,
/// and in no other way.
fn differs_as_decided(differs: Differs, vector: &Vector, rust: &Did, python: &Did, at: &str) {
    assert_eq!(vector.result, Outcome::Accepted, "{at}: the Python code");
    match differs {
        Differs::Refuses => assert!(
            matches!(rust, Did::Refused(_)),
            "{at}: the Rust code accepts"
        ),
        Differs::FieldIs(field, value) => {
            let rust = accepted_value(rust, at);
            let mut python = accepted_value(python, at);
            let value: Value = serde_json::from_str(value).unwrap();

            assert_ne!(python[field], value, "{at}: the Python field");
            python.insert(field.to_owned(), value);
            assert_eq!(rust, python, "{at}");
        }
        Differs::WritesOtherBytes => {
            let (
                Did::Accepted {
                    output: Some(rust), ..
                },
                Did::Accepted {
                    output: Some(python),
                    ..
                },
            ) = (rust, python)
            else {
                panic!("{at}: one side wrote no bytes");
            };

            assert_ne!(rust, python, "{at}: the bytes are equal");
        }
        Differs::KeepsBodyText(text) => {
            let mut rust = accepted_value(rust, at);
            let mut python = accepted_value(python, at);
            let line = StoredLine::parse(&body(vector)).unwrap();

            assert_ne!(
                rust["body"], python["body"],
                "{at}: the test reads the body"
            );
            assert_eq!(line.body_json(), text, "{at}");
            rust.remove("body");
            python.remove("body");
            assert_eq!(rust, python, "{at}");
        }
    }
}

/// Walks each vector of one surface. It prints the counts, and a run with
/// `--nocapture` shows them.
fn walk(name: &str) {
    let against = SURFACES
        .iter()
        .find(|against| against.surface == name)
        .unwrap();
    let surface = vectors::surface(against.surface);
    let mut equal = 0;
    let mut deviated = 0;
    for vector in &surface.vectors {
        let at = format!("{} {}", against.surface, vector.id);
        let rust = (against.replay)(vector, &surface.context);
        let python = python_did(vector, against.surface);

        if let Some(deviation) = deviation_of(against.surface, &vector.id) {
            assert!(!deviation.contract.is_empty() && !deviation.decision.is_empty());
            differs_as_decided(deviation.differs, vector, &rust, &python, &at);
            deviated += 1;
        } else if vector.result == Outcome::Raised {
            assert!(
                matches!(rust, Did::Refused(_)),
                "{at}: the Python code raises"
            );
            equal += 1;
        } else {
            assert_eq!(rust, readable(python, &rust), "{at}");
            equal += 1;
        }
    }

    assert_eq!(
        deviated,
        deviations_in(against.surface),
        "{}",
        against.surface
    );
    println!(
        "{name}: {} vectors: {equal} equal, {deviated} deviations",
        equal + deviated
    );
}

#[test]
fn the_table_holds_each_session_surface_of_the_index_one_time() {
    let listed: Vec<&str> = SURFACES.iter().map(|against| against.surface).collect();
    let unique: HashSet<&str> = listed.iter().copied().collect();
    let index = vectors::index();
    let in_index: HashSet<&str> = index
        .iter()
        .map(|row| row.surface.as_str())
        .filter(|surface| surface.starts_with("session."))
        .collect();

    assert_eq!(
        unique.len(),
        listed.len(),
        "the table holds one surface twice"
    );
    assert_eq!(unique, in_index);
}

#[test]
fn each_deviation_names_a_surface_of_the_table() {
    let listed: HashSet<&str> = SURFACES.iter().map(|against| against.surface).collect();
    for deviation in DEVIATIONS {
        assert!(deviation.contract.starts_with("contract "));
        for surface in deviation.surfaces {
            assert!(listed.contains(surface), "{surface}");
        }
    }
}

#[test]
fn a_create_request_is_what_the_python_parser_reads() {
    walk("session.request.create");
}

#[test]
fn a_run_turn_request_is_what_the_python_parser_reads() {
    walk("session.request.run_turn");
}

#[test]
fn a_writer_request_is_what_the_python_parser_reads() {
    walk("session.request.writer");
}

#[test]
fn a_steer_request_is_what_the_python_parser_reads() {
    walk("session.request.steer");
}

#[test]
fn a_stop_request_is_what_the_python_parser_reads() {
    walk("session.request.stop");
}

#[test]
fn a_dispatch_request_is_what_the_python_parser_reads() {
    walk("session.request.dispatch");
}

#[test]
fn a_jobs_query_is_what_the_python_parser_reads() {
    walk("session.request.jobs");
}

#[test]
fn a_delegate_request_is_what_the_python_parser_reads() {
    walk("session.request.delegate");
}

#[test]
fn a_switch_request_is_what_the_python_parser_reads() {
    walk("session.request.switch");
}

#[test]
fn a_list_query_is_what_the_python_route_reads() {
    walk("session.query.list");
}

#[test]
fn a_count_of_turns_is_what_the_python_route_reads() {
    walk("session.query.get");
}

#[test]
fn an_events_query_is_what_the_python_route_reads() {
    walk("session.query.events");
}

#[test]
fn an_error_body_is_the_bytes_that_the_python_code_writes() {
    walk("session.error_body");
}

#[test]
fn a_session_object_is_the_bytes_that_the_python_code_writes() {
    walk("session.answer.session");
}

#[test]
fn a_turn_object_is_the_bytes_that_the_python_code_writes() {
    walk("session.answer.turn");
}

#[test]
fn a_lease_object_is_the_bytes_that_the_python_code_writes() {
    walk("session.answer.lease");
}

#[test]
fn a_stream_record_is_the_bytes_that_the_python_code_writes() {
    walk("session.stream.encode");
}

#[test]
fn a_stored_line_is_what_the_python_replay_gives() {
    walk("session.journal.read");
}

#[test]
fn a_journal_line_is_the_bytes_that_the_python_code_writes() {
    walk("session.journal.write");
}

#[test]
fn a_record_of_the_stream_has_the_fields_of_the_python_record() {
    walk("session.stream.live");
}

#[test]
fn an_outcome_record_is_the_bytes_that_the_python_code_writes() {
    walk("session.outcome.write");
}

#[test]
fn a_turn_moves_where_the_python_table_permits() {
    walk("session.turn.move");
}

#[test]
fn the_state_of_a_session_is_what_the_python_code_derives() {
    walk("session.state.derive");
}

#[test]
fn the_status_of_a_job_is_what_the_python_code_gives() {
    walk("session.outcome.status");
}

/// A line that the Python code wrote, read and written again by the Rust code,
/// is the same bytes.
#[test]
fn a_line_that_python_wrote_reads_back_as_the_same_bytes() {
    let read_at = time("2000-01-01T00:00:00Z");
    for name in ["session.journal.write", "session.stream.encode"] {
        for vector in vectors::surface(name).vectors {
            let Did::Accepted {
                output: Some(written),
                ..
            } = python_did(&vector, name)
            else {
                panic!("{name} {}: no output", vector.id);
            };
            let Ok(line) = StoredLine::parse(written.as_bytes()) else {
                // A record that the stream makes itself has no sequence number.
                assert!(
                    written.starts_with(r#"{"journal_seq":null"#),
                    "{name} {}",
                    vector.id
                );
                continue;
            };

            assert_eq!(
                utf8(line.encode(read_at).unwrap()),
                written,
                "{name} {}",
                vector.id
            );
        }
    }
}

/// The typed body of each line that the Python code wrote is the body of its
/// kind, and it is written back as the same bytes.
#[test]
fn the_typed_body_of_a_line_that_python_wrote_is_of_its_kind() {
    for vector in vectors::surface("session.journal.write").vectors {
        if deviation_of("session.journal.write", &vector.id).is_some() {
            continue;
        }

        let Did::Accepted {
            output: Some(written),
            ..
        } = python_did(&vector, "")
        else {
            panic!("{}: no output", vector.id);
        };
        let stored = StoredLine::parse(written.as_bytes()).unwrap();
        let body = stored.body().unwrap();
        let turn = stored.turn().map(|turn| turn.id().unwrap().clone());
        let line = JournalLine::new(stored.journal_seq(), stored.ts().unwrap(), turn, body);

        assert_eq!(line.kind(), stored.kind(), "{}", vector.id);
        assert_eq!(utf8(line.encode().unwrap()), written, "{}", vector.id);
    }
}
