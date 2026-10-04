//! YAML text to a raw file, or to issues. Never a panic (invariant 19).
//!
//! A parse that fails is data. The caller gets `None` and each issue that
//! the YAML reader and the shape check found. The family keeps its last good
//! state.

use creche_contracts::family::{Issue, RawFamily, Severity};
use creche_contracts::server::RawServer;

use crate::difflib::closest_name;
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

/// The raw family file of a YAML text, or each issue of its shape.
#[must_use]
pub fn parse_family(text: &str) -> (Option<RawFamily>, Vec<Issue>) {
    one_document(text, FAMILY_CONTAINERS, shape::read_family)
}

/// The raw server file of a YAML text, or each issue of its shape.
#[must_use]
pub fn parse_server(text: &str) -> (Option<RawServer>, Vec<Issue>) {
    one_document(text, SERVER_CONTAINERS, shape::read_server)
}

#[cfg(test)]
mod tests {
    use creche_contracts::family::Severity;

    use super::{DOCUMENT, NO_REASON, closed, error, parse_family};
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
            .spawn(move || parse_family(&text));
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
}
