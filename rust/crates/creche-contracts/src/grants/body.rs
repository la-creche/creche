//! The two request bodies that the chaperone reads: the body of `POST /call`
//! (contract 04 §5, §7.1) and the body of `POST /approval/<gate>` (contract 04
//! §8.4).
//!
//! Each reader does what the Python chaperone does with the bytes of a body
//! whose content type is JSON, or that has no content type. It answers with
//! the same HTTP status. For each other content type the Python chaperone
//! answers 422 and reads no JSON. The HTTP layer of the port holds that rule.

use std::error::Error;
use std::fmt;

use serde::{Deserialize, Serialize};

use super::file::{text, unknown_keys};
use super::issue::{Issue, IssueKind, Report};
use super::json::{self, Charset, JsonError, KeyOrder, Layout, Map, Style, Value};

/// The largest count of bytes in a request body: 256 KiB. The chaperone
/// counts the bytes as they come and refuses a longer body before it reads a
/// bearer.
pub const BODY_MAX_BYTES: usize = 256 * 1024;

// CONTRACT-QUESTION: contract 04 §5 has no row for a request body that the
// chaperone cannot read, and §5.1 lets the status stand for the reason when
// an answer has none. The Python chaperone answers 413 for a body over the
// cap, 400 for bytes that are no document and 422 for a document that is not
// valid. The types here give the same three statuses. A change to one row of
// §5 with one reason costs the three constants and [`BodyError`].
/// The HTTP status for a body over [`BODY_MAX_BYTES`].
const STATUS_TOO_LARGE: u16 = 413;

/// The HTTP status for a body that the reader cannot read as a document.
const STATUS_UNREADABLE: u16 = 400;

/// The HTTP status for a body that is not valid.
const STATUS_INVALID: u16 = 422;

const TOOL_KEY: &str = "tool";
const ARGS_KEY: &str = "args";

/// Each key of the body of a call.
const CALL_KEYS: &[&str] = &[TOOL_KEY, ARGS_KEY];

const DECISION_KEY: &str = "decision";

/// Each key of the body of an approval.
const APPROVAL_KEYS: &[&str] = &[DECISION_KEY];

bounded_text! {
    /// The `tool` of one call, as the caller wrote it: 1 to 200 characters of
    /// text (contract 04 §7.1).
    ///
    /// The type takes each text, as the Python code does. It is not the name
    /// of a tool that exists: the decision finds that out (contract 04 §5 row
    /// 4). The text is untrusted. It can hold a control character.
    ///
    /// ```
    /// use creche_contracts::grants::CallTool;
    ///
    /// let tool: CallTool = "kagi__kagi_search_fetch".parse()?;
    /// assert_eq!(tool.as_str(), "kagi__kagi_search_fetch");
    /// # Ok::<(), creche_contracts::grants::CallToolError>(())
    /// ```
    ///
    /// Code outside this module cannot build a value from a raw string:
    ///
    /// ```compile_fail,E0423
    /// use creche_contracts::grants::CallTool;
    ///
    /// let tool = CallTool(String::new());
    /// ```
    CallTool,
    /// Why a text is not the `tool` of a call.
    CallToolError,
    "the tool of a call",
    1,
    200
}

/// The arguments of one call: a JSON object (contract 04 §5, §6.1).
///
/// The arguments are opaque here. Their schema belongs to the tool, and the
/// decision checks them against it (contract 04 §5 row 5). This type makes
/// sure of three things: the top level is an object, each key is there one
/// time, and each string is Unicode text.
///
/// ```
/// use creche_contracts::grants::{Arguments, CallBody};
///
/// let body = CallBody::parse(br#"{"tool": "embed", "args": {"input": "a note"}}"#)?;
/// let input = body.args().as_map().get("input").and_then(|value| value.as_str());
///
/// assert_eq!(input, Some("a note"));
/// assert!(Arguments::empty().as_map().is_empty());
/// # Ok::<(), creche_contracts::grants::BodyError>(())
/// ```
///
/// Code outside this module cannot build the arguments from a raw map:
///
/// ```compile_fail,E0423
/// use creche_contracts::grants::{Arguments, Map};
///
/// let args = Arguments(Map::new());
/// ```
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Arguments(Map);

impl Arguments {
    /// The arguments of a request that has none: an empty object. A manifest
    /// fetch has these arguments in its audit record.
    #[must_use]
    pub fn empty() -> Self {
        Self(Map::new())
    }

    /// The entries of the object, in the order of the body.
    #[must_use]
    pub fn as_map(&self) -> &Map {
        &self.0
    }

    /// The bytes that stand for the arguments in the record of a request that
    /// names no family. That record holds the count of these bytes and their
    /// SHA-256, and never the arguments.
    ///
    /// The bytes are the JSON of the arguments with sorted keys, a space after
    /// each comma and each colon, and text outside ASCII as it is.
    #[must_use]
    pub fn digest_input(&self) -> Vec<u8> {
        let style = Style {
            layout: Layout::Line,
            charset: Charset::Unicode,
            keys: KeyOrder::Sorted,
        };
        let mut text = String::new();
        json::write(&Value::Map(self.0.clone()), style, &mut text);

        text.into_bytes()
    }
}

/// The body of `POST /call`: one tool and its arguments (contract 04 §7.1).
///
/// ```
/// use creche_contracts::grants::CallBody;
///
/// let body = CallBody::parse(br#"{"tool": "embed"}"#)?;
///
/// assert_eq!(body.tool().as_str(), "embed");
/// assert!(body.args().as_map().is_empty());
/// # Ok::<(), creche_contracts::grants::BodyError>(())
/// ```
///
/// Code outside this module cannot build a body from raw parts:
///
/// ```compile_fail,E0451
/// use creche_contracts::grants::{Arguments, CallBody};
///
/// let body = CallBody { tool: "embed".parse().unwrap(), args: Arguments::empty() };
/// ```
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CallBody {
    tool: CallTool,
    args: Arguments,
}

impl CallBody {
    /// Reads the bytes of a body whose content type is JSON.
    ///
    /// This is a process edge. When the parse fails, the chaperone answers
    /// with [`BodyError::http_status`] and executes nothing.
    pub fn parse(body: &[u8]) -> Result<Self, BodyError> {
        let document = read(body)?;
        let mut report = Report::new();
        let call = check_call(&document, &mut report);
        let issues = report.finish();
        match call {
            Some(call) if issues.is_empty() => Ok(call),
            _ => Err(BodyError::Invalid(issues)),
        }
    }

    /// The tool that the caller names.
    #[must_use]
    pub fn tool(&self) -> &CallTool {
        &self.tool
    }

    /// The arguments. A body with no `args` has an empty object.
    #[must_use]
    pub fn args(&self) -> &Arguments {
        &self.args
    }

    /// The tool and the arguments, as two values.
    #[must_use]
    pub fn into_parts(self) -> (CallTool, Arguments) {
        (self.tool, self.args)
    }
}

/// What the operator said about one gate (contract 04 §8.4 rule 4).
///
/// The set is closed. A reader refuses each other word: an approval that the
/// reader does not understand must not open a gate.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Verdict {
    /// `approve`: execute the call.
    Approve,
    /// `deny`: refuse the call.
    Deny,
}

impl Verdict {
    /// Each verdict.
    pub const ALL: [Self; 2] = [Self::Approve, Self::Deny];

    /// The word on the wire.
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Approve => "approve",
            Self::Deny => "deny",
        }
    }
}

impl fmt::Display for Verdict {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_str())
    }
}

/// The body of `POST /approval/<gate>`: the verdict of the operator (contract
/// 04 §8.4 rule 4).
///
/// ```
/// use creche_contracts::grants::{ApprovalBody, Verdict};
///
/// let body = ApprovalBody::parse(br#"{"decision": "approve"}"#)?;
///
/// assert_eq!(body.decision(), Verdict::Approve);
/// # Ok::<(), creche_contracts::grants::BodyError>(())
/// ```
///
/// Code outside this module cannot build a body from a raw verdict:
///
/// ```compile_fail,E0451
/// use creche_contracts::grants::{ApprovalBody, Verdict};
///
/// let body = ApprovalBody { decision: Verdict::Approve };
/// ```
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ApprovalBody {
    decision: Verdict,
}

impl ApprovalBody {
    /// Reads the bytes of a body whose content type is JSON.
    ///
    /// This is a process edge. When the parse fails, the chaperone answers
    /// with [`BodyError::http_status`] and resolves no gate.
    pub fn parse(body: &[u8]) -> Result<Self, BodyError> {
        let document = read(body)?;
        let mut report = Report::new();
        let approval = check_approval(&document, &mut report);
        let issues = report.finish();
        match approval {
            Some(approval) if issues.is_empty() => Ok(approval),
            _ => Err(BodyError::Invalid(issues)),
        }
    }

    /// The verdict.
    #[must_use]
    pub fn decision(&self) -> Verdict {
        self.decision
    }
}

/// Why the chaperone refuses a request body.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum BodyError {
    /// The body has more bytes than [`BODY_MAX_BYTES`]. HTTP 413.
    TooLarge {
        /// The count of bytes.
        bytes: usize,
    },
    /// The reader cannot read the bytes as a document: they are not text,
    /// they nest too deep, an integer is too long, or a string holds a lone
    /// surrogate. HTTP 400.
    Unreadable(JsonError),
    /// The body is not valid. The list holds each issue. HTTP 422.
    Invalid(Vec<Issue>),
}

impl BodyError {
    /// The HTTP status of the answer.
    #[must_use]
    pub fn http_status(&self) -> u16 {
        match self {
            Self::TooLarge { .. } => STATUS_TOO_LARGE,
            Self::Unreadable(_) => STATUS_UNREADABLE,
            Self::Invalid(_) => STATUS_INVALID,
        }
    }
}

impl fmt::Display for BodyError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::TooLarge { .. } => write!(f, "body exceeds {BODY_MAX_BYTES} bytes"),
            Self::Unreadable(error) => write!(f, "the body is not a document: {error}"),
            // The count and no issue: an issue can hold text of the body.
            Self::Invalid(issues) => write!(f, "the body has {} issues", issues.len()),
        }
    }
}

impl Error for BodyError {}

/// The document of a body. `Value::Null` stands for a body with no bytes and
/// for the JSON `null`: the Python code takes the two as "no body".
fn read(body: &[u8]) -> Result<Value, BodyError> {
    if body.len() > BODY_MAX_BYTES {
        return Err(BodyError::TooLarge { bytes: body.len() });
    }

    if body.is_empty() {
        return Ok(Value::Null);
    }

    match json::read_body(body) {
        Ok(document) => Ok(document),
        Err(JsonError::Syntax { .. }) => {
            let mut report = Report::new();
            report.issue(IssueKind::NotJson);

            Err(BodyError::Invalid(report.finish()))
        }
        Err(error) => Err(BodyError::Unreadable(error)),
    }
}

/// The object of a body.
fn object<'a>(document: &'a Value, report: &mut Report) -> Option<&'a Map> {
    match document {
        Value::Map(object) => Some(object),
        Value::Null => {
            report.issue(IssueKind::Missing);

            None
        }
        _ => {
            report.issue(IssueKind::NotObject);

            None
        }
    }
}

fn check_call(document: &Value, report: &mut Report) -> Option<CallBody> {
    let body = object(document, report)?;
    let tool = report.at_key(TOOL_KEY, |report| {
        let Some(value) = body.get(TOOL_KEY) else {
            report.issue(IssueKind::Missing);

            return None;
        };
        match text(report, value)?.parse::<CallTool>() {
            Ok(tool) => Some(tool),
            Err(error) => {
                report.issue(error.into());

                None
            }
        }
    });
    let args = match body.get(ARGS_KEY) {
        None => Some(Arguments::empty()),
        Some(Value::Map(args)) => Some(Arguments(args.clone())),
        Some(_) => {
            report.at_key(ARGS_KEY, |report| report.issue(IssueKind::NotMap));

            None
        }
    };
    unknown_keys(body, report, CALL_KEYS);

    Some(CallBody {
        tool: tool?,
        args: args?,
    })
}

fn check_approval(document: &Value, report: &mut Report) -> Option<ApprovalBody> {
    let body = object(document, report)?;
    let decision = report.at_key(DECISION_KEY, |report| {
        let Some(value) = body.get(DECISION_KEY) else {
            report.issue(IssueKind::Missing);

            return None;
        };
        let word = value.as_str();
        let verdict = Verdict::ALL
            .into_iter()
            .find(|verdict| word == Some(verdict.as_str()));
        if verdict.is_none() {
            report.issue(IssueKind::NotChoice);
        }

        verdict
    });
    unknown_keys(body, report, APPROVAL_KEYS);

    Some(ApprovalBody {
        decision: decision?,
    })
}

#[cfg(test)]
mod tests {
    use super::super::issue::Step;
    use super::*;

    fn issues(error: BodyError) -> Vec<String> {
        match error {
            BodyError::Invalid(issues) => issues.iter().map(Issue::to_string).collect(),
            other => panic!("not an invalid body: {other:?}"),
        }
    }

    #[test]
    fn a_call_has_a_tool_and_arguments() {
        let body = CallBody::parse(br#"{"args": {"b": 1, "a": [true, null]}, "tool": "embed"}"#);
        let (tool, args) = body.unwrap().into_parts();
        let keys: Vec<&str> = args.as_map().iter().map(|(key, _)| key).collect();

        assert_eq!(tool.as_str(), "embed");
        assert_eq!(keys, ["b", "a"]);
    }

    #[test]
    fn the_tool_of_a_call_has_1_to_200_characters() {
        // GRINNING FACE has four bytes and two UTF-16 code units.
        for one in ["t", "\u{e9}", "\u{1f600}"] {
            assert!(one.repeat(200).parse::<CallTool>().is_ok(), "{one}");
            assert_eq!(
                one.repeat(201).parse::<CallTool>(),
                Err(CallToolError::TooLong),
                "{one}"
            );
        }

        assert_eq!("".parse::<CallTool>(), Err(CallToolError::TooShort));
        assert_eq!(
            "not a tool\n../x".parse::<CallTool>().unwrap().as_str(),
            "not a tool\n../x"
        );
    }

    #[test]
    fn a_call_with_a_fault_gives_each_issue() {
        let error = CallBody::parse(br#"{"tool": 5, "args": null, "x": 1, "a": 2}"#).unwrap_err();

        assert_eq!(error.http_status(), 422);
        assert_eq!(error.to_string(), "the body has 4 issues");
        assert_eq!(
            issues(error),
            [
                "at \"tool\": the value is not a string",
                "at \"args\": the value is not an object",
                "at \"x\": the contract does not name the key",
                "at \"a\": the contract does not name the key",
            ]
        );
    }

    #[test]
    fn a_body_that_is_not_an_object_is_one_issue_at_the_top() {
        for (body, said) in [
            (&b""[..], "at the top: the key is missing"),
            (b"null", "at the top: the key is missing"),
            (b"[]", "at the top: the value is not an object"),
            (b"5", "at the top: the value is not an object"),
            (b"NaN", "at the top: the value is not an object"),
            (b"  ", "at the top: the body is not JSON"),
            (b"{", "at the top: the body is not JSON"),
        ] {
            assert_eq!(issues(CallBody::parse(body).unwrap_err()), [said]);
            assert_eq!(issues(ApprovalBody::parse(body).unwrap_err()), [said]);
        }
    }

    #[test]
    fn a_body_that_is_no_document_is_a_400() {
        let deep = format!(
            "{{\"tool\":\"embed\",\"args\":{{\"a\":{}}}}}",
            "[".repeat(1000)
        );
        let long = format!(
            "{{\"tool\":\"embed\",\"args\":{{\"a\":{}}}}}",
            "9".repeat(4301)
        );
        for (body, error) in [
            (&b"{\"tool\":\"\xff\"}"[..], JsonError::NotText),
            (deep.as_bytes(), JsonError::TooDeep),
            (long.as_bytes(), JsonError::LongInteger),
            (
                br#"{"tool":"embed","args":{"a":"\ud800"}}"#,
                JsonError::LoneSurrogate,
            ),
            (br#"{"tool":"\ud800"}"#, JsonError::LoneSurrogate),
        ] {
            let refused = CallBody::parse(body).unwrap_err();

            assert_eq!(refused, BodyError::Unreadable(error));
            assert_eq!(refused.http_status(), 400);
        }

        assert_eq!(
            BodyError::Unreadable(JsonError::TooDeep).to_string(),
            "the body is not a document: the document nests deeper than 256 levels"
        );
    }

    #[test]
    fn a_body_over_the_cap_is_a_413() {
        let body = vec![b' '; BODY_MAX_BYTES + 1];
        let error = CallBody::parse(&body).unwrap_err();

        assert_eq!(
            error,
            BodyError::TooLarge {
                bytes: BODY_MAX_BYTES + 1
            }
        );
        assert_eq!(error.http_status(), 413);
        assert_eq!(error.to_string(), "body exceeds 262144 bytes");
        assert_eq!(ApprovalBody::parse(&body).unwrap_err().http_status(), 413);
        assert_eq!(CallBody::parse(&body[1..]).unwrap_err().http_status(), 422);
    }

    #[test]
    fn an_approval_is_one_of_two_words() {
        for verdict in Verdict::ALL {
            let body = format!("{{\"decision\": \"{verdict}\"}}");

            assert_eq!(
                ApprovalBody::parse(body.as_bytes()).unwrap().decision(),
                verdict
            );
        }

        for body in [
            &br#"{"decision": "Approve"}"#[..],
            br#"{"decision": "approve "}"#,
            br#"{"decision": "approve\n"}"#,
            br#"{"decision": ""}"#,
            br#"{"decision": true}"#,
            br#"{"decision": null}"#,
            br#"{"decision": ["approve"]}"#,
        ] {
            let error = ApprovalBody::parse(body).unwrap_err();

            assert_eq!(error.http_status(), 422);
            assert_eq!(
                issues(error),
                ["at \"decision\": the text is not a word that the field takes"]
            );
        }
    }

    #[test]
    fn an_approval_with_a_fault_gives_each_issue() {
        let BodyError::Invalid(found) = ApprovalBody::parse(br#"{"gate": 1}"#).unwrap_err() else {
            panic!("the body is not valid");
        };

        assert_eq!(found.len(), 2);
        assert_eq!(found[0].kind(), IssueKind::Missing);
        assert_eq!(found[0].path(), [Step::Key("decision".to_owned())]);
        assert_eq!(found[1].kind(), IssueKind::UnknownKey);
        assert_eq!(found[1].path(), [Step::Key("gate".to_owned())]);
    }

    #[test]
    fn a_verdict_reads_and_writes_its_wire_word() {
        for verdict in Verdict::ALL {
            let wire = serde_json::to_string(&verdict).unwrap();

            assert_eq!(wire, format!("\"{}\"", verdict.as_str()));
            assert_eq!(serde_json::from_str::<Verdict>(&wire).unwrap(), verdict);
        }

        assert!(serde_json::from_str::<Verdict>("\"yes\"").is_err());
    }

    #[test]
    fn the_digest_input_has_sorted_keys_and_text_as_it_is() {
        let body = CallBody::parse(
            "{\"tool\": \"embed\", \"args\": {\"b\": [1, 2.0], \"a\": {\"d\": \"\u{e9}\", \"c\": null}}}"
                .as_bytes(),
        )
        .unwrap();

        assert_eq!(
            String::from_utf8(body.args().digest_input()).unwrap(),
            "{\"a\": {\"c\": null, \"d\": \"\u{e9}\"}, \"b\": [1, 2.0]}"
        );
        assert_eq!(Arguments::empty().digest_input(), b"{}");
    }
}
