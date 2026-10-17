//! YAML or TOML text to a raw file, or to issues. Never a panic (invariant
//! 19).
//!
//! A parse that fails is data. The caller gets `None` and each issue that
//! the reader and the shape check found. The family keeps its last good
//! state.
//!
//! The YAML path reads each field in the lax mode of pydantic. The TOML path
//! reads the text with `RawFamily::from_toml` or `RawServer::from_toml` of
//! `creche-contracts`, which reads each field in the strict mode: a field
//! takes the kind of its type, and a float field also takes an integer. This
//! module writes the message of each fault of that check, with the texts of
//! the YAML path.

pub use creche_contracts::family::Markup;
use creche_contracts::family::{
    GrantArm, Issue, LocPart, RawFamily, Severity, ShapeFault, ShapeProblem, TomlRefusal, Wants,
};
use creche_contracts::server::RawServer;

use crate::difflib::closest_name;
use crate::lax;
use crate::shape::{self, Fault, Part, Problem, Walk};
use crate::yaml::{self, LoadError, Value};

/// The location of an issue of the whole file.
pub const DOCUMENT: &str = "<document>";

/// The message for a file that the shape reader gives no value and no issue.
const NO_REASON: &str = "the file has no value, and the reader gave no reason";

/// A list index in the path of a container. Each index is this one key: the
/// index does not change which fields the container knows.
const ANY_INDEX: &str = "*";

/// The fields of a model, in the order of its Python declaration.
type Known = &'static [&'static str];

/// The fields of each container of a file, by the path of the container.
type Containers = &'static [(&'static [&'static str], Known)];

const FAMILY_FIELDS: Known = &[
    "name",
    "kind",
    "description",
    "model",
    "files",
    "tools",
    "verbs",
    "delegates",
    "max_inflight_delegations",
    "egress",
    "shell",
    "sandbox_tools",
    "system_prompt",
    "sandbox",
    "skills",
    "approval",
    "job",
    "triggers",
    "max_running_turns",
    "quiet",
];

const FAMILY_CONTAINERS: Containers = &[
    (&[], FAMILY_FIELDS),
    (&["model"], &["router", "budget_usd_per_day"]),
    (
        &["sandbox"],
        &["cpus", "memory", "max_resident_processes", "image"],
    ),
    (&["job"], &["timeout"]),
    (
        &["verbs"],
        &["embed", "ha_call", "enqueue", "job_status", "release"],
    ),
    (&["files", ANY_INDEX], &["path", "mode"]),
    (&["triggers", ANY_INDEX], &["cron", "webhook", "enqueue"]),
    (&["verbs", "ha_call"], &["allow"]),
    (
        &["verbs", "ha_call", "allow", ANY_INDEX],
        &["domain", "service", "entity_id"],
    ),
    (&["verbs", "enqueue"], &["targets"]),
    (&["verbs", "release"], &["components"]),
];

const FENCE_FIELDS: Known = &["tools", "arg", "values"];

const SERVER_CONTAINERS: Containers = &[
    (
        &[],
        &[
            "name",
            "identity",
            "install",
            "run",
            "tools",
            "arg_allows",
            "arg_denies",
            "shared_secrets",
        ],
    ),
    (
        &["install"],
        &[
            "source", "package", "version", "lock", "python", "repo", "asset", "sha256", "ref",
        ],
    ),
    (
        &["run"],
        &["entrypoint", "args", "env", "state_dir", "state_dir_env"],
    ),
    (&["tools", ANY_INDEX], &["name", "description", "write"]),
    (&["arg_allows", ANY_INDEX], FENCE_FIELDS),
    (&["arg_denies", ANY_INDEX], FENCE_FIELDS),
];

/// The path of a field, as the report writes it: `tools.kagi[1]`.
fn fmt_loc(loc: &[Part]) -> String {
    let mut out = String::new();
    for part in loc {
        match part {
            Part::Index(index) => {
                out.push('[');
                out.push_str(index);
                out.push(']');
            }
            Part::Key(key) if out.is_empty() => out.push_str(key),
            Part::Key(key) => {
                out.push('.');
                out.push_str(key);
            }
        }
    }

    if out.is_empty() {
        return DOCUMENT.to_owned();
    }

    out
}

fn part_text(part: &Part) -> &str {
    match part {
        Part::Index(_) => ANY_INDEX,
        Part::Key(key) => key,
    }
}

fn known_fields(container: &[Part], containers: Containers) -> Known {
    containers
        .iter()
        .find(|(path, _)| path.iter().copied().eq(container.iter().map(part_text)))
        .map_or(&[], |(_, known)| *known)
}

/// The message for an unknown field: the field and the nearest known name
/// (contract 01 §7 rule 1).
fn unknown_field_msg(loc: &[Part], containers: Containers) -> String {
    let Some((last, container)) = loc.split_last() else {
        return format!("unknown field '{DOCUMENT}'");
    };

    let field = match last {
        Part::Key(key) | Part::Index(key) => key,
    };
    let known = known_fields(container, containers);
    if let Some(near) = closest_name(field, known) {
        return format!("unknown field '{field}'; did you mean '{near}'?");
    }

    if known.is_empty() {
        return format!("unknown field '{field}'");
    }

    format!(
        "unknown field '{field}'; known fields here are {}",
        known.join(", ")
    )
}

fn error(loc: String, msg: String) -> Issue {
    Issue {
        severity: Severity::Error,
        loc,
        msg,
        downgraded: false,
    }
}

fn issue_from(fault: Fault, containers: Containers) -> Issue {
    let msg = match fault.problem {
        Problem::Said(msg) => msg,
        Problem::UnknownField => unknown_field_msg(&fault.loc, containers),
    };

    error(fmt_loc(&fault.loc), msg)
}

/// Contract 01 §1 rules 3 and 4: one YAML document, a mapping at the top.
fn one_document<T>(
    text: &str,
    containers: Containers,
    read: impl FnOnce(Value<'_>, &mut Walk) -> Option<T>,
) -> (Option<T>, Vec<Issue>) {
    let refuse = |msg: String| (None, vec![error(DOCUMENT.to_owned(), msg)]);
    let documents = match yaml::load_all(text) {
        Ok(documents) => documents,
        // A text with no value has a report: the file is invalid.
        Err(LoadError::Syntax(message) | LoadError::Unreadable(message)) => {
            return refuse(format!("YAML will not parse: {message}"));
        }
    };
    if documents.count() > 1 {
        return refuse("one YAML document per file; found more than one".to_owned());
    }

    let body = documents.first();
    let found = body.map_or("NoneType", |body| shape::python_type(body.obj()));
    let Some(body) = body.filter(|_| found == "dict") else {
        return refuse(format!("the top level must be a mapping; found {found}"));
    };

    let mut walk = Walk::default();
    let raw = read(body, &mut walk);
    let issues: Vec<Issue> = walk
        .into_faults()
        .into_iter()
        .map(|fault| issue_from(fault, containers))
        .collect();

    closed(raw, issues)
}

/// The raw file of a read with no issue, or each issue of the read.
///
/// A read that gives no file and no issue is a defect of the shape reader.
/// The answer fails closed: it then holds one error for the whole file. A
/// file with no value thus never has a clean report.
fn closed<T>(raw: Option<T>, issues: Vec<Issue>) -> (Option<T>, Vec<Issue>) {
    if !issues.is_empty() {
        return (None, issues);
    }

    if raw.is_none() {
        return (None, vec![error(DOCUMENT.to_owned(), NO_REASON.to_owned())]);
    }

    (raw, issues)
}

/// The raw family file of a text in `markup`, or each issue of its shape.
#[must_use]
pub fn parse_family(text: &str, markup: Markup) -> (Option<RawFamily>, Vec<Issue>) {
    match markup {
        Markup::Yaml => one_document(text, FAMILY_CONTAINERS, shape::read_family),
        Markup::Toml => parse_family_toml(text.as_bytes()),
    }
}

/// The TOML path for the bytes of a file. The registry gives the bytes that
/// it read, so the TOML reader judges each byte of the file.
#[must_use]
pub(crate) fn parse_family_toml(bytes: &[u8]) -> (Option<RawFamily>, Vec<Issue>) {
    toml_issues(RawFamily::from_toml(bytes), FAMILY_CONTAINERS)
}

/// The raw file of a read of the TOML path, or each issue of the read.
fn toml_issues<T>(read: Result<T, TomlRefusal>, containers: Containers) -> (Option<T>, Vec<Issue>) {
    match read {
        Ok(raw) => (Some(raw), Vec::new()),
        Err(TomlRefusal::Unread(fault)) => {
            (None, vec![error(DOCUMENT.to_owned(), fault.to_string())])
        }
        Err(TomlRefusal::Shape(faults)) => {
            let issues = faults
                .iter()
                .map(|fault| shape_issue(fault, containers))
                .collect();

            closed(None, issues)
        }
    }
}

/// The part of a location of the report for one part of a fault of the
/// TOML path.
fn part_of(part: &LocPart) -> Part {
    match part {
        LocPart::Key(key) => Part::Key(key.clone()),
        LocPart::Index(index) => Part::Index(index.to_string()),
        LocPart::Arm(GrantArm::Names) => Part::Key(shape::LIST_ARM.to_owned()),
        LocPart::Arm(GrantArm::Text) => Part::Key(shape::STR_ARM.to_owned()),
    }
}

/// The error of pydantic in the strict mode for a value of a wrong kind.
fn wants_msg(wants: Wants) -> String {
    match wants {
        Wants::Text => lax::STRING_TYPE.to_owned(),
        Wants::Integer => lax::INT_TYPE.to_owned(),
        Wants::Boolean => lax::BOOL_TYPE.to_owned(),
        Wants::Number => lax::FLOAT_TYPE.to_owned(),
        Wants::List => shape::LIST_TYPE.to_owned(),
        Wants::Map => shape::DICT_TYPE.to_owned(),
        Wants::Block(block) => shape::model_type_msg(shape::class_of(block)),
    }
}

/// The issue of one fault of the TOML path, in a file whose containers are
/// `containers`.
fn shape_issue(fault: &ShapeFault, containers: Containers) -> Issue {
    let loc: Vec<Part> = fault.loc().iter().map(part_of).collect();
    let msg = match fault.problem() {
        ShapeProblem::Missing => shape::MISSING.to_owned(),
        ShapeProblem::Unknown => unknown_field_msg(&loc, containers),
        ShapeProblem::WrongType(wants) => wants_msg(wants),
    };

    error(fmt_loc(&loc), msg)
}

/// The raw server file of a text in `markup`, or each issue of its shape.
#[must_use]
pub fn parse_server(text: &str, markup: Markup) -> (Option<RawServer>, Vec<Issue>) {
    match markup {
        Markup::Yaml => one_document(text, SERVER_CONTAINERS, shape::read_server),
        Markup::Toml => parse_server_toml(text.as_bytes()),
    }
}

/// The TOML path for the bytes of a server file. The byte limit of the file
/// kind is the one size cap of this path.
#[must_use]
pub(crate) fn parse_server_toml(bytes: &[u8]) -> (Option<RawServer>, Vec<Issue>) {
    toml_issues(RawServer::from_toml(bytes), SERVER_CONTAINERS)
}

#[cfg(test)]
mod tests {
    use creche_contracts::family::Severity;

    use super::{
        DOCUMENT, Markup, NO_REASON, closed, error, parse_family, parse_family_toml, parse_server,
        parse_server_toml,
    };
    use crate::yaml::NESTING_MAX;

    /// The stack of the thread of the test below. A default thread has two
    /// times this stack.
    const SMALL_STACK: usize = 1 << 20;

    /// The mapping at the top of the file is the first level.
    #[test]
    fn a_file_at_the_nesting_limit_reads_on_a_small_stack() {
        let inner = NESTING_MAX - 1;
        let text = format!("egress: {}{}\n", "[".repeat(inner), "]".repeat(inner));
        let thread = std::thread::Builder::new()
            .stack_size(SMALL_STACK)
            .spawn(move || parse_family(&text, Markup::Yaml));
        let (raw, issues) = thread.unwrap().join().unwrap();

        assert_eq!(raw, None);
        assert!(issues.iter().any(|issue| issue.loc == "egress[0]"));
        assert!(issues.iter().all(|issue| issue.loc != DOCUMENT));
    }

    #[test]
    fn a_read_with_no_file_and_no_issue_is_one_error() {
        let (raw, issues) = closed::<u8>(None, Vec::new());

        assert_eq!(raw, None);
        assert_eq!(issues.len(), 1);
        assert_eq!(issues[0].severity, Severity::Error);
        assert_eq!(issues[0].loc, DOCUMENT);
        assert_eq!(issues[0].msg, NO_REASON);
    }

    #[test]
    fn a_read_with_an_issue_gives_no_file() {
        let issue = error("name".to_owned(), "field required".to_owned());

        assert_eq!(
            closed(Some(1_u8), vec![issue.clone()]),
            (None, vec![issue.clone()])
        );
        assert_eq!(closed::<u8>(None, vec![issue.clone()]), (None, vec![issue]));
        assert_eq!(closed(Some(1_u8), Vec::new()), (Some(1), Vec::new()));
    }

    #[test]
    fn the_toml_markup_reads_the_text_with_the_toml_reader() {
        let head = "kind = \"thin\"\ndescription = \"x\"\nmodel.router = \"fast\"\n";
        let valid = format!("name = \"oracle\"\n{head}model.budget_usd_per_day = 1\n");
        let (raw, issues) = parse_family(&valid, Markup::Toml);

        assert_eq!(
            (raw.clone(), issues.clone()),
            parse_family_toml(valid.as_bytes())
        );
        assert!(issues.is_empty(), "{issues:?}");
        assert_eq!(raw.map(|raw| raw.model.router), Some("fast".to_owned()));
        // The YAML reader reads no dotted key.
        assert_eq!(parse_family(&valid, Markup::Yaml).0, None);

        let wrong = format!("name = 1\n{head}model.budget_usd_per_day = true\n");
        let (raw, issues) = parse_family(&wrong, Markup::Toml);
        let read: Vec<(&str, &str)> = issues
            .iter()
            .map(|issue| (issue.loc.as_str(), issue.msg.as_str()))
            .collect();

        assert_eq!(raw, None);
        assert_eq!(
            read,
            [
                ("name", "Input should be a valid string"),
                ("model.budget_usd_per_day", "Input should be a valid number"),
            ]
        );
    }

    #[test]
    fn the_toml_markup_reads_a_server_file_with_the_toml_reader() {
        let valid = "name = \"example\"\nidentity = \"x\"\ninstall.source = \"agent-mcp\"\n\
                     run.entrypoint = \"example-mcp\"\n";
        let (raw, issues) = parse_server(valid, Markup::Toml);

        assert_eq!(
            (raw.clone(), issues.clone()),
            parse_server_toml(valid.as_bytes())
        );
        assert!(issues.is_empty(), "{issues:?}");
        assert_eq!(
            raw.map(|raw| raw.run.entrypoint),
            Some("example-mcp".to_owned())
        );
        // The YAML reader reads no dotted key.
        assert_eq!(parse_server(valid, Markup::Yaml).0, None);

        let wrong = "name = \"example\"\nidentity = \"x\"\ninstall = 1\nrun = { zz = 1 }\n";
        let (raw, issues) = parse_server(wrong, Markup::Toml);
        let read: Vec<(&str, &str)> = issues
            .iter()
            .map(|issue| (issue.loc.as_str(), issue.msg.as_str()))
            .collect();

        assert_eq!(raw, None);
        assert_eq!(
            read,
            [
                (
                    "install",
                    "Input should be a valid dictionary or instance of InstallBlock"
                ),
                ("run.entrypoint", "Field required"),
                (
                    "run.zz",
                    "unknown field 'zz'; known fields here are entrypoint, args, env, state_dir, \
                     state_dir_env"
                ),
            ]
        );
    }
}
