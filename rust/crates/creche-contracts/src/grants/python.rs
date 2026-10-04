//! The differential test against the Python implementation.
//!
//! `vectors/data/chaperone` records what the Python code accepts, refuses and
//! writes. Each test here walks each vector of each surface of one type. A
//! vector on which the Rust code differs on purpose is a row of
//! [`DEVIATIONS`].

use std::collections::{BTreeMap, HashSet};
use std::time::{Duration, UNIX_EPOCH};

use serde_json::Value as Json;

use crate::ids::{FamilyName, SandboxName, Sha256Hex, ToolNameError};
use crate::vectors::{self, Outcome, Surface, Vector};

use super::json::{Sign, read_utf8};
use super::*;

// --- the surfaces of this module ---

const GRANT_READER: &str = "grants.parse";
const GRANT_WRITER: &str = "grants.write";
const CALL_BODY: &str = "chaperone.call_body";
const APPROVAL_BODY: &str = "chaperone.approval_body";
const AUDIT_LINE: &str = "chaperone.audit_line";
const UNIDENTIFIED_LINE: &str = "chaperone.unidentified_line";
const REASON: &str = "chaperone.reason";
const VERB: &str = "chaperone.verb";

/// Each type of this module and the surfaces that it implements.
const TYPES: &[(&str, &[&str])] = &[
    ("GrantFile", &[GRANT_READER, GRANT_WRITER]),
    ("CallBody", &[CALL_BODY]),
    ("ApprovalBody", &[APPROVAL_BODY]),
    ("AuditRecord", &[AUDIT_LINE]),
    ("UnidentifiedRecord", &[UNIDENTIFIED_LINE]),
    ("Reason", &[REASON]),
    ("Verb", &[VERB]),
];

/// The start of the name of each surface of this module. An id surface starts
/// with `id.` and belongs to the module `ids`.
const PREFIXES: [&str; 2] = ["grants.", "chaperone."];

/// The surfaces of one type, from [`TYPES`].
fn surfaces_of(name: &str) -> &'static [&'static str] {
    let found = TYPES.iter().find(|(type_name, _)| *type_name == name);

    found.unwrap().1
}

#[test]
fn the_table_holds_each_surface_of_this_module_one_time() {
    let listed: Vec<&str> = TYPES
        .iter()
        .flat_map(|(_, surfaces)| surfaces.iter().copied())
        .collect();
    let unique: HashSet<&str> = listed.iter().copied().collect();
    let index = vectors::index();
    let in_index: HashSet<&str> = index
        .iter()
        .map(|row| row.surface.as_str())
        .filter(|surface| PREFIXES.iter().any(|prefix| surface.starts_with(prefix)))
        .collect();

    assert_eq!(unique.len(), listed.len(), "two types hold one surface");
    assert_eq!(unique, in_index);
}

// --- the differences on purpose ---

/// What the Rust code answers on one vector on which it differs from the
/// Python code. A row holds the exact refusal. A defect that refuses the same
/// vector for another reason then fails the test.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Refusal {
    /// The document has more bytes than the cap of its reader.
    TooLarge,
    /// The reader cannot read the bytes as a document, for this reason.
    Unreadable(JsonError),
    /// The document is not valid. The row holds each issue, as [`Issue`]
    /// shows it.
    Invalid(&'static [&'static str]),
    /// The text is not a value of the closed set of the type.
    NotInSet,
}

/// One decision to differ from the Python code on one vector.
///
/// The Python code accepts the input of the vector, or it refuses the input
/// in another way: with another kind for a grant file, with another HTTP
/// status for a body.
struct Deviation {
    surface: &'static str,
    vector: &'static str,
    /// What the Rust code answers.
    rust: Refusal,
    /// The contract section that the decision reads.
    contract: &'static str,
    /// The decision, and its reason.
    decision: &'static str,
}

/// Why the Rust code refuses a string that the Python reader keeps.
const TEXT_IS_UNICODE: &str = "A string of this crate is Unicode text. A lone surrogate is not \
    a character, and it has no UTF-8 form. The Python reader keeps a lone surrogate in a string. \
    The Rust reader refuses the document.";

/// Why the Rust code refuses a document that nests deeper than 256 levels.
const NESTING_HAS_A_CAP: &str = "The contract gives no cap on the nesting of a document. The \
    Python reader stops at a limit of its interpreter, which is not one number. The Rust reader \
    stops at 256 levels, before it checks a field.";

/// Each vector on which the Rust code differs from the Python code on
/// purpose. Each other vector must be equal.
const DEVIATIONS: &[Deviation] = &[
    Deviation {
        surface: GRANT_READER,
        vector: "tools-tool-128-chars",
        rust: Refusal::Invalid(&["at \"tools\", \"kagi\", 0: a tool name has 64 bytes or less"]),
        contract: "contract 01 §3.4 and contract 01b §5",
        decision: "A tool name has 64 bytes or less: `ids::ToolName` takes the strictest of \
            the three Python copies of the grammar (rust/AGENTS.md). The grant file reader of \
            the Python chaperone takes 128 characters.",
    },
    Deviation {
        surface: GRANT_READER,
        vector: "json-lone-surrogate",
        rust: Refusal::Unreadable(JsonError::LoneSurrogate),
        contract: "contract 04 §1.2, §1.4",
        decision: TEXT_IS_UNICODE,
    },
    Deviation {
        surface: GRANT_READER,
        vector: "json-lone-surrogate-in-components",
        rust: Refusal::Unreadable(JsonError::LoneSurrogate),
        contract: "contract 04 §1.2, §1.4",
        decision: TEXT_IS_UNICODE,
    },
    Deviation {
        surface: GRANT_READER,
        vector: "json-deep-300-unknown-field",
        rust: Refusal::Unreadable(JsonError::TooDeep),
        contract: "contract 04 §1.2, §1.4",
        decision: NESTING_HAS_A_CAP,
    },
    Deviation {
        surface: GRANT_READER,
        vector: "json-very-deep",
        rust: Refusal::TooLarge,
        contract: "contract 04 §1.2, §1.4",
        decision: "A grant file has 256 KiB or less. The chaperone refuses a longer file \
            before a reader sees it. The Rust reader holds the cap itself. The vector records \
            the reader of the Python code with no cap before it.",
    },
    Deviation {
        surface: CALL_BODY,
        vector: "args-lone-surrogate",
        rust: Refusal::Unreadable(JsonError::LoneSurrogate),
        contract: "contract 04 §7.1",
        decision: TEXT_IS_UNICODE,
    },
    Deviation {
        surface: CALL_BODY,
        vector: "json-very-deep",
        rust: Refusal::TooLarge,
        contract: "contract 04 §6.1",
        decision: "The request body limit is 256 KiB. The chaperone refuses a longer body \
            before a reader sees it. The Rust reader holds the cap itself. The vector records \
            the reader of the Python code with no cap before it.",
    },
    Deviation {
        surface: CALL_BODY,
        vector: "args-deep-300",
        rust: Refusal::Unreadable(JsonError::TooDeep),
        contract: "contract 04 §7.1",
        decision: NESTING_HAS_A_CAP,
    },
    Deviation {
        surface: CALL_BODY,
        vector: "unknown-field-deep-300",
        rust: Refusal::Unreadable(JsonError::TooDeep),
        contract: "contract 04 §7.1",
        decision: NESTING_HAS_A_CAP,
    },
    Deviation {
        surface: APPROVAL_BODY,
        vector: "unknown-field-deep-300",
        rust: Refusal::Unreadable(JsonError::TooDeep),
        contract: "contract 04 §8.4",
        decision: NESTING_HAS_A_CAP,
    },
    Deviation {
        surface: VERB,
        vector: "invoke-agent",
        rust: Refusal::NotInSet,
        contract: "contract 01 §3.5, §3.6 and contract 04 §4.1",
        decision: "`Verb` holds the five verbs that the `verbs` block of a grant file can \
            name. The Python catalog holds one more entry, `invoke_agent`. A family file grants \
            it through `delegates`, and `Executor::Delegate` stands for it.",
    },
];

fn deviation_of(surface: &str, vector: &str) -> Option<&'static Deviation> {
    DEVIATIONS
        .iter()
        .find(|deviation| deviation.surface == surface && deviation.vector == vector)
}

/// The count of rows of [`DEVIATIONS`] for one surface.
fn deviations_in(surface: &str) -> usize {
    DEVIATIONS
        .iter()
        .filter(|deviation| deviation.surface == surface)
        .count()
}

#[test]
fn each_deviation_names_a_vector_of_a_surface_of_the_table() {
    let listed: HashSet<&str> = TYPES
        .iter()
        .flat_map(|(_, surfaces)| surfaces.iter().copied())
        .collect();
    let mut seen = HashSet::new();
    for deviation in DEVIATIONS {
        assert!(deviation.contract.starts_with("contract "));
        assert!(!deviation.decision.is_empty());
        assert!(listed.contains(deviation.surface), "{}", deviation.surface);
        assert!(
            seen.insert((deviation.surface, deviation.vector)),
            "{} {}: two rows",
            deviation.surface,
            deviation.vector
        );

        let surface = vectors::surface(deviation.surface);
        let named = surface
            .vectors
            .iter()
            .any(|vector| vector.id == deviation.vector);

        assert!(named, "{} {}", deviation.surface, deviation.vector);
    }
}

// --- a vector value as a value of this module ---

/// A JSON value of a vector file as a [`Value`]. A marker object stays a map.
fn plain(json: &Json) -> Value {
    match json {
        Json::Null => Value::Null,
        Json::Bool(flag) => Value::Bool(*flag),
        Json::Number(number) => match (number.as_u64(), number.as_i64(), number.as_f64()) {
            (Some(whole), _, _) => Value::Integer(Integer::from(whole)),
            (_, Some(whole), _) => Value::Integer(Integer::from(whole)),
            (_, _, Some(float)) => Value::Float(float),
            _ => panic!("a number with no form: {number}"),
        },
        Json::String(text) => Value::Text(text.clone()),
        Json::Array(items) => Value::List(items.iter().map(plain).collect()),
        Json::Object(entries) => {
            let mut map = Map::new();
            for (key, item) in entries {
                map.insert(key, plain(item));
            }

            Value::Map(map)
        }
    }
}

/// `value` with each marker object replaced by the value that it stands for
/// (`vectors/README.md`). `None` when the value holds a string with a lone
/// surrogate, or a map with such a key: no [`Value`] holds one.
fn resolved(value: &Value) -> Option<Value> {
    let Value::Map(map) = value else {
        return match value {
            Value::List(items) => items
                .iter()
                .map(resolved)
                .collect::<Option<_>>()
                .map(Value::List),
            _ => Some(value.clone()),
        };
    };
    let mut entries = map.iter();
    if let (Some((key, marked)), None) = (entries.next(), entries.next()) {
        match (key, marked) {
            ("$int", Value::Text(digits)) => {
                let (sign, digits) = match digits.strip_prefix('-') {
                    Some(digits) => (Sign::Minus, digits),
                    None => (Sign::Plus, digits.as_str()),
                };

                return Some(Value::Integer(Integer::from_digits(sign, digits)));
            }
            ("$float", Value::Text(name)) => {
                let float = match name.as_str() {
                    "NaN" => f64::NAN,
                    "Infinity" => f64::INFINITY,
                    "-Infinity" => f64::NEG_INFINITY,
                    other => panic!("a $float marker with {other}"),
                };

                return Some(Value::Float(float));
            }
            ("$json", Value::Text(text)) => {
                let inner = read_utf8(text.as_bytes()).unwrap();

                return resolved(&inner);
            }
            ("$utf16" | "$entries", _) => return None,
            ("$base64", _) => panic!("a $base64 marker in a value of this module"),
            _ => {}
        }
    }

    let mut plain = Map::new();
    for (key, item) in map.iter() {
        plain.insert(key, resolved(item)?);
    }

    Some(Value::Map(plain))
}

/// `value` with the entries of each map in the order of their keys. Two
/// values with the same data are then equal: a vector file keeps no key order.
fn sorted(value: &Value) -> Value {
    match value {
        Value::List(items) => Value::List(items.iter().map(sorted).collect()),
        Value::Map(map) => {
            let entries: BTreeMap<&str, Value> =
                map.iter().map(|(key, item)| (key, sorted(item))).collect();
            let mut in_order = Map::new();
            for (key, item) in entries {
                in_order.insert(key, item);
            }

            Value::Map(in_order)
        }
        _ => value.clone(),
    }
}

/// The `value` of a vector, in sorted order.
fn value_of(vector: &Vector, at: &str) -> Value {
    let value = vector.value().unwrap_or_else(|| panic!("{at}: no value"));
    let value = resolved(&plain(value)).unwrap_or_else(|| panic!("{at}: a lone surrogate"));

    sorted(&value)
}

fn text(value: &str) -> Value {
    Value::Text(value.to_owned())
}

fn map(entries: Vec<(&str, Value)>) -> Value {
    let mut map = Map::new();
    for (key, item) in entries {
        map.insert(key, item);
    }

    Value::Map(map)
}

// --- the issues of a document, as the Python code names them ---

/// The type and the location of one error, as the vector files hold them.
type PythonError = (String, Json);

/// Which document an issue is in.
#[derive(Clone, Copy, PartialEq, Eq)]
enum Document {
    /// A grant file.
    File,
    /// A request body. The Python code gives its top level a name of its own
    /// and starts each location with `body`.
    Body,
}

/// The name that the Python code gives the type of an issue.
fn python_type(issue: &Issue, document: Document) -> &'static str {
    let body = document == Document::Body;
    match issue.kind() {
        IssueKind::Missing => "missing",
        IssueKind::UnknownKey => "extra_forbidden",
        IssueKind::NotText => "string_type",
        IssueKind::NotList => "tuple_type",
        IssueKind::NotMap => "dict_type",
        IssueKind::NotObject if body && issue.path().is_empty() => "model_attributes_type",
        IssueKind::NotObject => "model_type",
        IssueKind::NotInteger => "int_type",
        IssueKind::TextTooShort { .. } => "string_too_short",
        IssueKind::TextTooLong { .. }
        | IssueKind::Name(NameError::Tool(ToolNameError::TooLong)) => "string_too_long",
        IssueKind::Name(_) => "string_pattern_mismatch",
        IssueKind::TooFew { .. } => "too_short",
        IssueKind::TooMany { .. } => "too_long",
        IssueKind::Fraction => "int_from_float",
        IssueKind::NotFinite => "finite_number",
        IssueKind::NotIntegerText => "int_parsing",
        IssueKind::IntegerTooLarge => "int_parsing_size",
        IssueKind::BelowMinimum { .. } => "greater_than_equal",
        IssueKind::NotJson => "json_invalid",
        IssueKind::NotChoice => "literal_error",
    }
}

/// The issues of the Rust code in the form of the vector files.
fn rust_errors(issues: &[Issue], document: Document) -> Vec<PythonError> {
    issues
        .iter()
        .map(|issue| {
            let top = (document == Document::Body).then(|| Json::from("body"));
            let steps = issue.path().iter().map(|step| match step {
                Step::Key(key) => Json::from(key.as_str()),
                Step::Index(index) => Json::from(*index),
                Step::KeyItself => Json::from("[key]"),
            });
            let location: Vec<Json> = top.into_iter().chain(steps).collect();

            (
                python_type(issue, document).to_owned(),
                Json::from(location),
            )
        })
        .collect()
}

/// The errors of a vector: the type and the location of each one. The test
/// does not compare the message: the Rust code has its own text.
fn python_errors(errors: &Json) -> Vec<PythonError> {
    let errors = errors.as_array().unwrap();

    errors
        .iter()
        .map(|error| {
            assert!(error["msg"].is_string());

            (
                error["type"].as_str().unwrap().to_owned(),
                error["loc"].clone(),
            )
        })
        .collect()
}

// --- the grant file ---

/// The kind that the vector files give the refusal of a grant file.
fn grant_kind(error: &GrantError) -> &'static str {
    match error {
        GrantError::TooLarge { .. } => "too_large",
        GrantError::NotJson(_) => "not_json",
        GrantError::NotObject => "not_object",
        GrantError::VersionNotInteger => "version_not_integer",
        GrantError::UnknownVersion(_) => "unknown_version",
        GrantError::Invalid(_) => "invalid",
        GrantError::OtherFamily(_) => "other_family",
    }
}

/// A grant file as the vector files hold the Python model: each key, with
/// `null` for a fence key that the file does not have.
fn grant_value(grants: &GrantFile) -> Value {
    let names =
        |names: &[FamilyName]| Value::List(names.iter().map(|name| text(name.as_str())).collect());
    let limit = |limit: &Limit| Value::Integer(limit.as_integer().clone());
    let tools = grants.tools().iter().map(|(server, tools)| {
        let tools = tools.iter().map(|tool| text(tool.as_str()));

        (server.as_str(), Value::List(tools.collect()))
    });
    let verbs = grants.verbs().iter().map(|(verb, fence)| {
        let allow = fence.allow().map_or(Value::Null, |triples| {
            let triples = triples.iter().map(|triple| {
                map(vec![
                    ("domain", text(triple.domain().as_str())),
                    ("service", text(triple.service().as_str())),
                    ("entity_id", triple.entity_id().map_or(Value::Null, text)),
                ])
            });

            Value::List(triples.collect())
        });
        let targets = fence.targets().map_or(Value::Null, names);
        let components = fence.components().map_or(Value::Null, |components| {
            Value::List(components.iter().map(|component| text(component)).collect())
        });
        let fence = map(vec![
            ("allow", allow),
            ("targets", targets),
            ("components", components),
        ]);

        (verb.as_str(), fence)
    });
    let digests = grants
        .token_sha256()
        .iter()
        .map(|digest| text(digest.as_str()));
    let approval = grants.approval().iter().map(|action| text(action.as_str()));
    let limits = grants.limits();

    sorted(&map(vec![
        ("version", Value::Integer(Integer::from(GRANT_FILE_VERSION))),
        ("family", text(grants.family().as_str())),
        ("rev", text(grants.rev().as_str())),
        ("token_sha256", Value::List(digests.collect())),
        ("model_alias", text(grants.model_alias().as_str())),
        ("tools", map(tools.collect())),
        ("verbs", map(verbs.collect())),
        ("delegates", names(grants.delegates())),
        ("approval", Value::List(approval.collect())),
        (
            "limits",
            map(vec![
                ("pep_rpm", limit(limits.pep_rpm())),
                (
                    "max_inflight_delegations",
                    limit(limits.max_inflight_delegations()),
                ),
                ("max_open_gates", limit(limits.max_open_gates())),
            ]),
        ),
    ]))
}

/// Makes sure that the Rust code refuses a grant file as the vector says.
fn grant_refused_as_python(vector: &Vector, error: &GrantFileError, at: &str) {
    let refusal = vector.refusal().unwrap();
    let kind = refusal["kind"].as_str().unwrap();

    assert_eq!(grant_kind(error.error()), kind, "{at}");

    // The message of `not_json` ends with the text of the JSON reader, and
    // each reader has its own text.
    if kind != "not_json" {
        assert_eq!(
            error.to_string(),
            refusal["message"].as_str().unwrap(),
            "{at}"
        );
    }

    match error.error() {
        GrantError::Invalid(issues) => {
            assert_eq!(
                rust_errors(issues, Document::File),
                python_errors(&refusal["errors"]),
                "{at}"
            );
        }
        _ => assert!(refusal.get("errors").is_none(), "{at}"),
    }
}

/// Whether the issues are the ones that a row of [`DEVIATIONS`] holds.
fn issues_are(issues: &[Issue], wanted: &[&str]) -> bool {
    let shown: Vec<String> = issues.iter().map(Issue::to_string).collect();

    shown == wanted
}

/// Makes sure that the Rust code differs on a grant file as the row says, and
/// in no other way.
fn grant_differs_as_decided(
    deviation: &Deviation,
    vector: &Vector,
    rust: &Result<GrantFile, GrantFileError>,
    at: &str,
) {
    let Err(error) = rust else {
        panic!("{at}: the Rust code accepts the file");
    };
    let as_the_row_says = match (error.error(), deviation.rust) {
        (GrantError::TooLarge { .. }, Refusal::TooLarge) => true,
        (GrantError::NotJson(found), Refusal::Unreadable(wanted)) => *found == wanted,
        (GrantError::Invalid(issues), Refusal::Invalid(wanted)) => issues_are(issues, wanted),
        _ => false,
    };

    assert!(as_the_row_says, "{at}: another refusal: {error:?}");
    match vector.result {
        Outcome::Accepted => {}
        Outcome::Refused => assert_ne!(
            vector.refusal().unwrap()["kind"],
            grant_kind(error.error()),
            "{at}: no difference"
        ),
        Outcome::Raised => panic!("{at}: a raised vector needs no row"),
    }
}

fn walk_grant_reader() -> (usize, usize) {
    let surface = vectors::surface(GRANT_READER);
    let mut equal = 0;
    let mut deviated = 0;
    for vector in &surface.vectors {
        let at = format!("{GRANT_READER} {}", vector.id);
        let bytes = vector.input.bytes().unwrap();
        let stem = vector.field("params").unwrap()["family"].as_str().unwrap();
        let stem: FamilyName = stem.parse().unwrap();
        let rust = GrantFile::parse(&bytes, &stem);
        if let Some(deviation) = deviation_of(GRANT_READER, &vector.id) {
            grant_differs_as_decided(deviation, vector, &rust, &at);
            deviated += 1;

            continue;
        }

        match (vector.result, rust) {
            (Outcome::Accepted, Ok(grants)) => {
                assert_eq!(grant_value(&grants), value_of(vector, &at), "{at}");

                // What the reader took, the writer writes, and the reader
                // takes again.
                let written = grants
                    .to_bytes()
                    .unwrap_or_else(|error| panic!("{at}: {error}"));

                assert_eq!(GrantFile::parse(&written, &stem), Ok(grants), "{at}");
            }
            (Outcome::Refused, Err(error)) => grant_refused_as_python(vector, &error, &at),
            (Outcome::Raised, Err(_)) => {}
            (python, rust) => panic!("{at}: the Python code: {python:?}, the Rust code: {rust:?}"),
        }

        equal += 1;
    }

    assert_eq!(deviated, deviations_in(GRANT_READER));

    (equal, deviated)
}

fn strings(json: &Json) -> Vec<String> {
    let items = json.as_array().unwrap().iter();

    items
        .map(|item| item.as_str().unwrap().to_owned())
        .collect()
}

fn optional_strings(json: Option<&Json>) -> Option<Vec<String>> {
    json.filter(|json| !json.is_null()).map(strings)
}

/// The raw fields of a grant file, from the arguments of the Python writer.
fn raw_grant(args: &serde_json::Map<String, Json>) -> RawGrantFile {
    let tools = args["tools"].as_object().unwrap().iter();
    let verbs = args["verbs"]
        .as_object()
        .unwrap()
        .iter()
        .map(|(verb, fence)| {
            let allow = fence.get("allow").map(|allow| {
                let triples = allow.as_array().unwrap().iter().map(|triple| RawHaAllow {
                    domain: triple["domain"].as_str().unwrap().to_owned(),
                    service: triple["service"].as_str().unwrap().to_owned(),
                    entity_id: triple["entity_id"].as_str().map(str::to_owned),
                });

                triples.collect()
            });
            let fence = RawVerbFence {
                allow,
                targets: optional_strings(fence.get("targets")),
                components: optional_strings(fence.get("components")),
            };

            (verb.clone(), fence)
        });

    RawGrantFile {
        version: GRANT_FILE_VERSION,
        family: args["family"].as_str().unwrap().to_owned(),
        rev: args["rev"].as_str().unwrap().to_owned(),
        token_sha256: strings(&args["token_sha256"]),
        model_alias: args["model_alias"].as_str().unwrap().to_owned(),
        tools: tools
            .map(|(server, tools)| (server.clone(), strings(tools)))
            .collect(),
        verbs: verbs.collect(),
        delegates: strings(&args["delegates"]),
        approval: strings(&args["approval"]),
        limits: RawLimits {
            pep_rpm: Some(DEFAULT_PEP_RPM),
            max_inflight_delegations: args["max_inflight_delegations"].as_u64(),
            max_open_gates: Some(DEFAULT_MAX_OPEN_GATES),
        },
    }
}

fn walk_grant_writer() -> usize {
    let surface = vectors::surface(GRANT_WRITER);
    for vector in &surface.vectors {
        let at = format!("{GRANT_WRITER} {}", vector.id);
        let raw = raw_grant(vector.input.args().unwrap());
        let grants = GrantFile::try_from(raw).unwrap_or_else(|error| panic!("{at}: {error}"));
        let written = output_of(vector);

        assert_eq!(vector.result, Outcome::Accepted, "{at}");
        assert_eq!(
            String::from_utf8(grants.to_bytes().unwrap()).unwrap(),
            written,
            "{at}"
        );
        assert_eq!(grant_value(&grants), value_of(vector, &at), "{at}");
        assert_eq!(
            GrantFile::parse(written.as_bytes(), grants.family()).as_ref(),
            Ok(&grants),
            "{at}"
        );
    }

    assert_eq!(deviations_in(GRANT_WRITER), 0);

    surface.vectors.len()
}

#[test]
fn a_grant_file_is_what_the_python_code_reads_and_writes() {
    let (equal, deviated) = walk_grant_reader();
    let written = walk_grant_writer();

    assert_eq!(surfaces_of("GrantFile"), [GRANT_READER, GRANT_WRITER]);
    println!(
        "GrantFile: {} vectors of {GRANT_READER}: {equal} equal, {deviated} deviations. \
         {written} vectors of {GRANT_WRITER}: each one equal",
        equal + deviated
    );
}

// --- the two bodies ---

/// The `output` of a vector of a writer, as text. Each line of a log and each
/// grant file is UTF-8.
fn output_of(vector: &Vector) -> String {
    let output = vector.field("output").unwrap();

    output["text"].as_str().unwrap().to_owned()
}

/// Makes sure that the Rust code refuses a body as the vector says.
fn body_refused_as_python(vector: &Vector, error: &BodyError, at: &str) {
    let refusal = vector.refusal().unwrap();
    let detail = &refusal["detail"];

    assert_eq!(
        u64::from(error.http_status()),
        refusal["http_status"].as_u64().unwrap(),
        "{at}"
    );
    match error {
        BodyError::Invalid(issues) => {
            assert_eq!(
                rust_errors(issues, Document::Body),
                python_errors(detail),
                "{at}"
            );
        }
        // The Python code answers with one sentence and no list.
        BodyError::Unreadable(_) => assert!(detail.is_string(), "{at}"),
        BodyError::TooLarge { .. } => panic!("{at}: no vector is about the cap"),
    }
}

/// Makes sure that the Rust code differs on a body as the row says, and in
/// no other way.
fn body_differs_as_decided(deviation: &Deviation, vector: &Vector, error: &BodyError, at: &str) {
    let as_the_row_says = match (error, deviation.rust) {
        (BodyError::TooLarge { .. }, Refusal::TooLarge) => true,
        (BodyError::Unreadable(found), Refusal::Unreadable(wanted)) => *found == wanted,
        (BodyError::Invalid(issues), Refusal::Invalid(wanted)) => issues_are(issues, wanted),
        _ => false,
    };

    assert!(as_the_row_says, "{at}: another refusal: {error:?}");
    match vector.result {
        Outcome::Accepted => {}
        Outcome::Refused => {
            let python = vector.refusal().unwrap()["http_status"].as_u64().unwrap();

            assert_ne!(
                python,
                u64::from(error.http_status()),
                "{at}: no difference"
            );
        }
        Outcome::Raised => panic!("{at}: a raised vector needs no row"),
    }
}

/// Walks each vector of one body surface.
fn walk_body<T: std::fmt::Debug>(
    name: &str,
    parse: fn(&[u8]) -> Result<T, BodyError>,
    value: fn(&T) -> Value,
) -> (usize, usize) {
    let surface = vectors::surface(name);
    let mut equal = 0;
    let mut deviated = 0;
    for vector in &surface.vectors {
        let at = format!("{name} {}", vector.id);
        let rust = parse(&vector.input.bytes().unwrap());
        if let Some(deviation) = deviation_of(name, &vector.id) {
            let error = rust.expect_err(&at);
            body_differs_as_decided(deviation, vector, &error, &at);
            deviated += 1;

            continue;
        }

        match (vector.result, rust) {
            (Outcome::Accepted, Ok(body)) => {
                assert_eq!(vector.field("http_status"), Some(&Json::from(200)), "{at}");
                assert_eq!(sorted(&value(&body)), value_of(vector, &at), "{at}");
            }
            (Outcome::Refused, Err(error)) => body_refused_as_python(vector, &error, &at),
            (Outcome::Raised, Err(_)) => {}
            (python, rust) => panic!("{at}: the Python code: {python:?}, the Rust code: {rust:?}"),
        }

        equal += 1;
    }

    assert_eq!(deviated, deviations_in(name));

    (equal, deviated)
}

#[test]
fn a_call_body_is_what_the_python_chaperone_reads() {
    let value = |body: &CallBody| {
        map(vec![
            ("tool", text(body.tool().as_str())),
            ("args", Value::Map(body.args().as_map().clone())),
        ])
    };
    let (equal, deviated) = walk_body(CALL_BODY, CallBody::parse, value);

    assert_eq!(surfaces_of("CallBody"), [CALL_BODY]);
    println!(
        "CallBody: {} vectors: {equal} equal, {deviated} deviations",
        equal + deviated
    );
}

#[test]
fn an_approval_body_is_what_the_python_chaperone_reads() {
    let value = |body: &ApprovalBody| map(vec![("decision", text(body.decision().as_str()))]);
    let (equal, deviated) = walk_body(APPROVAL_BODY, ApprovalBody::parse, value);

    assert_eq!(surfaces_of("ApprovalBody"), [APPROVAL_BODY]);
    println!(
        "ApprovalBody: {} vectors: {equal} equal, {deviated} deviations",
        equal + deviated
    );
}

// --- the two logs ---

/// The time of a vector: `at_us` microseconds after the epoch, as a clock
/// gives it.
fn time_of(args: &serde_json::Map<String, Json>) -> AuditTime {
    let clock = UNIX_EPOCH + Duration::from_micros(args["at_us"].as_u64().unwrap());

    AuditTime::try_from(clock).unwrap()
}

/// The tool and the arguments of a vector: what the chaperone reads from the
/// body `call`, or a manifest fetch when `call` is null.
fn action_of(args: &serde_json::Map<String, Json>) -> (AuditAction, Arguments) {
    let Some(call) = args["call"].as_str() else {
        return (AuditAction::Manifest, Arguments::empty());
    };
    let (tool, arguments) = CallBody::parse(call.as_bytes()).unwrap().into_parts();

    (AuditAction::Call(tool), arguments)
}

/// The outcome that writes this decision and this reason.
fn outcome_of(decision: &str, reason: &str) -> AuditOutcome {
    let failures = [AfterAllow::UpstreamFailed, AfterAllow::DelegateTimeout];
    let outcomes = [
        AuditOutcome::Granted,
        AuditOutcome::Approved,
        AuditOutcome::Pending,
    ]
    .into_iter()
    .chain(failures.map(AuditOutcome::Failed))
    .chain(Reason::ALL.map(AuditOutcome::Denied));
    let mut found = outcomes
        .filter(|outcome| outcome.decision().as_str() == decision && outcome.reason() == reason);
    let outcome = found.next();

    assert!(found.next().is_none(), "{decision} {reason}: two outcomes");

    outcome.unwrap_or_else(|| panic!("{decision} {reason}: no outcome"))
}

fn audit_record(args: &serde_json::Map<String, Json>) -> AuditRecord {
    let parsed = |key: &str| args[key].as_str().map(|text| text.parse().unwrap());
    let family: FamilyName = args["family"].as_str().unwrap().parse().unwrap();
    let (action, arguments) = action_of(args);
    let sandbox = args["sandbox_id"]
        .as_str()
        .map(|name| name.parse::<SandboxName>().unwrap());
    let sandbox = match (sandbox, args["sandbox_id_trusted"].as_bool().unwrap()) {
        (None, false) => SandboxEvidence::Unknown,
        (Some(sandbox), false) => SandboxEvidence::Claimed(sandbox),
        (Some(sandbox), true) => SandboxEvidence::Trusted(sandbox),
        (None, true) => panic!("a trusted sandbox with no name"),
    };
    let claims = &args["claimed"];
    let (claimed, dropped) = Claimed::read(&RawClaimed {
        session_id: claims["session_id"].as_str(),
        turn_id: claims["turn_id"].as_str(),
        delegation_id: claims["delegation_id"].as_str(),
    });
    let mut callers: Vec<FamilyName> = strings(&args["chain"])
        .iter()
        .map(|name| name.parse().unwrap())
        .collect();

    // The chain of the Python code ends with the family of the record. An
    // empty chain stands for a chain of one.
    assert!(dropped.is_empty());
    assert!(callers.pop().is_none_or(|last| last == family));

    AuditRecord {
        at: time_of(args),
        family,
        sandbox,
        grants_rev: parsed("grants_rev"),
        action,
        args: arguments,
        outcome: outcome_of(
            args["decision"].as_str().unwrap(),
            args["reason"].as_str().unwrap(),
        ),
        latency_ms: args["latency_ms"].as_u64(),
        waited_ms: args["waited_ms"].as_u64().unwrap(),
        gate: args["gate"].as_str().map(|gate| gate.parse().unwrap()),
        claimed,
        callers,
    }
}

#[test]
fn an_audit_record_is_the_line_that_the_python_chaperone_writes() {
    let surface = vectors::surface(AUDIT_LINE);
    let outcomes: HashSet<AuditOutcome> = walk_lines(&surface, |args| {
        let record = audit_record(args);

        (record.to_line(), record.at, record.outcome)
    });
    let each_outcome = 3 + 2 + Reason::ALL.len();

    assert_eq!(surfaces_of("AuditRecord"), [AUDIT_LINE]);
    assert_eq!(outcomes.len(), each_outcome, "a vector for each outcome");
    println!(
        "AuditRecord: {} vectors: each one equal",
        surface.vectors.len()
    );
}

/// Walks each vector of one log surface. `write` gives the line, the time and
/// one more fact of the record. The function answers each such fact.
fn walk_lines<T: Eq + std::hash::Hash>(
    surface: &Surface,
    write: impl Fn(&serde_json::Map<String, Json>) -> (Vec<u8>, AuditTime, T),
) -> HashSet<T> {
    let mut facts = HashSet::new();
    for vector in &surface.vectors {
        let at = format!("{} {}", surface.surface, vector.id);
        let (line, time, fact) = write(vector.input.args().unwrap());

        assert_eq!(vector.result, Outcome::Accepted, "{at}");
        assert_eq!(String::from_utf8(line).unwrap(), output_of(vector), "{at}");
        assert_eq!(
            Some(&Json::from(time.file_name())),
            vector.field("file"),
            "{at}"
        );
        facts.insert(fact);
    }

    assert_eq!(deviations_in(&surface.surface), 0);

    facts
}

fn unidentified_record(args: &serde_json::Map<String, Json>) -> UnidentifiedRecord {
    let seen_bytes = args["oversized_bytes"].as_u64().unwrap();
    let limited = args["limited"].as_bool().unwrap();
    let request = if seen_bytes > 0 {
        UnidentifiedRequest::Oversized { seen_bytes }
    } else {
        let (action, arguments) = action_of(args);
        let input = arguments.digest_input();

        UnidentifiedRequest::NoFamily {
            action,
            refusal: if limited {
                Unidentified::RateLimited
            } else {
                Unidentified::UnknownToken
            },
            args: ArgsDigest {
                bytes: u64::try_from(input.len()).unwrap(),
                sha256: sha256_hex(&input).parse::<Sha256Hex>().unwrap(),
            },
        }
    };

    UnidentifiedRecord {
        at: time_of(args),
        request,
    }
}

#[test]
fn an_unidentified_record_is_the_line_that_the_python_chaperone_writes() {
    let surface = vectors::surface(UNIDENTIFIED_LINE);
    let kinds = walk_lines(&surface, |args| {
        let record = unidentified_record(args);
        let kind = match &record.request {
            UnidentifiedRequest::NoFamily { refusal, .. } => Some(*refusal),
            UnidentifiedRequest::Oversized { .. } => None,
        };

        (record.to_line(), record.at, kind)
    });

    assert_eq!(surfaces_of("UnidentifiedRecord"), [UNIDENTIFIED_LINE]);
    assert_eq!(kinds.len(), 3, "a vector for each kind of request");
    println!(
        "UnidentifiedRecord: {} vectors: each one equal",
        surface.vectors.len()
    );
}

// --- the reasons and the advisory headers ---

#[test]
fn a_reason_is_what_the_python_chaperone_answers_with() {
    let surface = vectors::surface(REASON);
    let mut accepted = HashSet::new();
    for vector in &surface.vectors {
        let at = format!("{REASON} {}", vector.id);
        let rust = vector.input.text().unwrap().parse::<Reason>();
        match (vector.result, rust) {
            (Outcome::Accepted, Ok(reason)) => {
                let status = vector.field("http_status").unwrap().as_u64().unwrap();

                assert_eq!(u64::from(reason.http_status()), status, "{at}");
                accepted.insert(reason);
            }
            (Outcome::Refused | Outcome::Raised, Err(UnknownReason)) => {}
            (python, rust) => panic!("{at}: the Python code: {python:?}, the Rust code: {rust:?}"),
        }
    }

    assert_eq!(surfaces_of("Reason"), [REASON]);
    assert_eq!(deviations_in(REASON), 0);
    assert_eq!(accepted, HashSet::from(Reason::ALL));
    println!("Reason: {} vectors: each one equal", surface.vectors.len());
}

#[test]
fn a_verb_is_an_entry_of_the_python_catalog() {
    let surface = vectors::surface(VERB);
    let mut accepted = Vec::new();
    let mut deviated = 0;
    for vector in &surface.vectors {
        let at = format!("{VERB} {}", vector.id);
        let text = vector.input.text().unwrap();
        let rust = text.parse::<VerbName>().ok().and_then(|name| name.verb());

        // The two readers of a verb agree: the name of a grant file and the
        // word on a wire.
        assert_eq!(
            serde_json::from_value::<Verb>(Json::from(text.as_str())).ok(),
            rust,
            "{at}"
        );
        if let Some(deviation) = deviation_of(VERB, &vector.id) {
            assert_eq!(deviation.rust, Refusal::NotInSet, "{at}");
            assert_eq!((vector.result, rust), (Outcome::Accepted, None), "{at}");
            deviated += 1;

            continue;
        }

        match (vector.result, rust) {
            (Outcome::Accepted, Some(verb)) => {
                assert_eq!(verb.as_str(), text, "{at}");
                accepted.push(verb);
            }
            (Outcome::Refused | Outcome::Raised, None) => {}
            (python, rust) => panic!("{at}: the Python code: {python:?}, the Rust code: {rust:?}"),
        }
    }

    // The catalog holds each verb, in the order of `Verb::ALL`.
    assert_eq!(surfaces_of("Verb"), [VERB]);
    assert_eq!(deviated, deviations_in(VERB));
    assert_eq!(accepted, Verb::ALL);
    println!(
        "Verb: {} vectors: {} equal, {deviated} deviations",
        surface.vectors.len(),
        surface.vectors.len() - deviated
    );
}

/// Two id surfaces record what `chaperone.headers.read_claimed` keeps. The
/// module `ids` owns the surfaces and holds the grammars. This test makes
/// sure that [`Claimed::read`] uses them as the Python code does.
#[test]
fn an_advisory_header_is_kept_when_the_python_chaperone_keeps_it() {
    for vector in vectors::surface("id.session_id.chaperone").vectors {
        let value = vector.input.text().unwrap();
        let (claimed, dropped) = Claimed::read(&RawClaimed {
            session_id: Some(&value),
            ..RawClaimed::default()
        });
        let kept = vector.result == Outcome::Accepted;

        assert_eq!(claimed.session_id().is_some(), kept, "{}", vector.id);
        assert_eq!(dropped.is_empty(), kept, "{}", vector.id);
    }

    for vector in vectors::surface("id.ulid.chaperone").vectors {
        let value = vector.input.text().unwrap();
        let (claimed, dropped) = Claimed::read(&RawClaimed {
            session_id: None,
            turn_id: Some(&value),
            delegation_id: Some(&value),
        });
        let kept = vector.result == Outcome::Accepted;

        assert_eq!(claimed.turn_id().is_some(), kept, "{}", vector.id);
        assert_eq!(claimed.delegation_id().is_some(), kept, "{}", vector.id);
        assert_eq!(dropped.len(), if kept { 0 } else { 2 }, "{}", vector.id);
    }
}

// --- SHA-256, for the test only ---

/// The 64 round constants of SHA-256 (FIPS 180-4 §4.2.2).
const SHA256_ROUNDS: [u32; 64] = [
    0x428a_2f98,
    0x7137_4491,
    0xb5c0_fbcf,
    0xe9b5_dba5,
    0x3956_c25b,
    0x59f1_11f1,
    0x923f_82a4,
    0xab1c_5ed5,
    0xd807_aa98,
    0x1283_5b01,
    0x2431_85be,
    0x550c_7dc3,
    0x72be_5d74,
    0x80de_b1fe,
    0x9bdc_06a7,
    0xc19b_f174,
    0xe49b_69c1,
    0xefbe_4786,
    0x0fc1_9dc6,
    0x240c_a1cc,
    0x2de9_2c6f,
    0x4a74_84aa,
    0x5cb0_a9dc,
    0x76f9_88da,
    0x983e_5152,
    0xa831_c66d,
    0xb003_27c8,
    0xbf59_7fc7,
    0xc6e0_0bf3,
    0xd5a7_9147,
    0x06ca_6351,
    0x1429_2967,
    0x27b7_0a85,
    0x2e1b_2138,
    0x4d2c_6dfc,
    0x5338_0d13,
    0x650a_7354,
    0x766a_0abb,
    0x81c2_c92e,
    0x9272_2c85,
    0xa2bf_e8a1,
    0xa81a_664b,
    0xc24b_8b70,
    0xc76c_51a3,
    0xd192_e819,
    0xd699_0624,
    0xf40e_3585,
    0x106a_a070,
    0x19a4_c116,
    0x1e37_6c08,
    0x2748_774c,
    0x34b0_bcb5,
    0x391c_0cb3,
    0x4ed8_aa4a,
    0x5b9c_ca4f,
    0x682e_6ff3,
    0x748f_82ee,
    0x78a5_636f,
    0x84c8_7814,
    0x8cc7_0208,
    0x90be_fffa,
    0xa450_6ceb,
    0xbef9_a3f7,
    0xc671_78f2,
];

/// The start of the eight words of the state (FIPS 180-4 §5.3.3).
const SHA256_START: [u32; 8] = [
    0x6a09_e667,
    0xbb67_ae85,
    0x3c6e_f372,
    0xa54f_f53a,
    0x510e_527f,
    0x9b05_688c,
    0x1f83_d9ab,
    0x5be0_cd19,
];

/// The SHA-256 of `data`, as lower-case hex. The crate has no SHA-256: the
/// chaperone gives the record a digest. The test needs one to make the
/// digest that the Python code wrote.
fn sha256_hex(data: &[u8]) -> String {
    let bits = u64::try_from(data.len()).unwrap() * 8;
    let mut message = data.to_vec();
    message.push(0x80);
    while message.len() % 64 != 56 {
        message.push(0);
    }

    message.extend_from_slice(&bits.to_be_bytes());

    let mut state = SHA256_START;
    for block in message.chunks_exact(64) {
        let mut words = [0_u32; 64];
        for (word, bytes) in words.iter_mut().zip(block.chunks_exact(4)) {
            *word = u32::from_be_bytes(bytes.try_into().unwrap());
        }

        for index in 16..64 {
            let early = words[index - 15];
            let late = words[index - 2];
            let small_0 = early.rotate_right(7) ^ early.rotate_right(18) ^ (early >> 3);
            let small_1 = late.rotate_right(17) ^ late.rotate_right(19) ^ (late >> 10);
            words[index] = words[index - 16]
                .wrapping_add(small_0)
                .wrapping_add(words[index - 7])
                .wrapping_add(small_1);
        }

        let [mut a, mut b, mut c, mut d, mut e, mut f, mut g, mut h] = state;
        for (round, word) in SHA256_ROUNDS.iter().zip(words) {
            let big_1 = e.rotate_right(6) ^ e.rotate_right(11) ^ e.rotate_right(25);
            let choice = (e & f) ^ (!e & g);
            let first = h
                .wrapping_add(big_1)
                .wrapping_add(choice)
                .wrapping_add(*round)
                .wrapping_add(word);
            let big_0 = a.rotate_right(2) ^ a.rotate_right(13) ^ a.rotate_right(22);
            let majority = (a & b) ^ (a & c) ^ (b & c);
            let second = big_0.wrapping_add(majority);
            h = g;
            g = f;
            f = e;
            e = d.wrapping_add(first);
            d = c;
            c = b;
            b = a;
            a = first.wrapping_add(second);
        }

        for (held, added) in state.iter_mut().zip([a, b, c, d, e, f, g, h]) {
            *held = held.wrapping_add(added);
        }
    }

    state.iter().map(|word| format!("{word:08x}")).collect()
}

#[test]
fn the_sha256_of_the_test_gives_the_digests_of_the_standard() {
    let two_blocks = "abcdbcdecdefdefgefghfghighijhijkijkljklmklmnlmnomnopnopq";

    assert_eq!(
        sha256_hex(b""),
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    );
    assert_eq!(
        sha256_hex(b"abc"),
        "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    );
    assert_eq!(
        sha256_hex(two_blocks.as_bytes()),
        "248d6a61d20638b8e5c026930c3e6039a33ce45964ff2167f6ecedd419db06c1"
    );
    assert_eq!(
        sha256_hex(&[b'a'; 1000]),
        "41edece42d63e8d9bf515a9ba6932e1c20cbc9f5a5d134645adb5db1b9737ea3"
    );
}
