//! The roster of MCP servers that the chaperone starts: `upstreams.yaml`.
//!
//! The chaperone reads two roster files and merges them
//! (`stage7-releases.md` §4.4):
//!
//! 1. The base roster of the deployed tree. The unit names it as
//!    `PEP_UPSTREAMS`. It holds no row.
//! 2. The generated roster. Root writes it at the end of a verified
//!    `mcp-servers` release. `PEP_UPSTREAMS_GENERATED` names it. A name in
//!    the two files takes the generated row.
//!
//! [`RawRoster`] is the raw form: the tree of one file. [`Roster`] is the
//! valid form. The Python reader is `chaperone.mcp_client.load_upstreams`.
//!
//! CONTRACT-QUESTION: `stage7-releases.md` §4.4 says that the file is YAML
//! and names no YAML version. The Python reader is PyYAML, which reads YAML
//! 1.1. This module holds no YAML reader. [`RawRoster`] takes its tree
//! through `serde`, from the reader that the owner of the crate selects. A
//! YAML 1.2 reader gives `on` and `yes` as text where PyYAML gives a
//! boolean, so the set of accepted files can change with that decision.

use std::collections::{BTreeMap, BTreeSet};
use std::error::Error;
use std::fmt;

use serde::{Deserialize, Serialize};

use super::shape::MapOnly;
use super::{AtReload, AtStart, Checked, FailureAction};

/// What starts an env value that refers to a secret of the chaperone.
const SECRET_PREFIX: &str = "secret:";

// --- the raw form ---

/// The tree of one roster file: a mapping from the name of an upstream to
/// its row. A file with no content is a roster with no row.
#[derive(Debug, Clone, PartialEq, Eq, Deserialize, Serialize)]
#[serde(transparent)]
pub struct RawRoster(Option<BTreeMap<String, MapOnly<RawUpstream>>>);

/// The row of one upstream, as the file holds it.
#[derive(Debug, Clone, PartialEq, Eq, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct RawUpstream {
    command: String,
    #[serde(default)]
    args: Vec<String>,
    #[serde(default)]
    env: BTreeMap<String, String>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    tools: Vec<String>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    arg_denies: Vec<MapOnly<RawArgDeny>>,
}

/// One argument fence of an upstream, as the file holds it. Each of the
/// three keys is necessary.
#[derive(Debug, Clone, PartialEq, Eq, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct RawArgDeny {
    tools: Vec<String>,
    arg: String,
    values: Vec<String>,
}

// --- the valid form ---

/// The value of one variable of an upstream.
///
/// The roster file writes a secret as `secret:<key>`. The chaperone reads
/// the secret from its own store when it starts the upstream. The roster
/// holds no secret.
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub enum EnvValue {
    /// The value itself.
    Literal(String),
    /// The key of a secret in the store of the chaperone.
    Secret(String),
}

impl EnvValue {
    /// The value of the text of a roster file.
    fn of(text: String) -> Self {
        match text.strip_prefix(SECRET_PREFIX) {
            Some(key) => Self::Secret(key.to_owned()),
            None => Self::Literal(text),
        }
    }

    /// The text that a roster file holds for the value.
    #[must_use]
    pub fn to_text(&self) -> String {
        match self {
            Self::Literal(text) => text.clone(),
            Self::Secret(key) => format!("{SECRET_PREFIX}{key}"),
        }
    }
}

/// One argument fence of an upstream: the chaperone refuses a call to one
/// of `tools` whose argument `arg` is one of `values`.
///
/// Each of the two sets holds one member or more, and `arg` is not empty.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ArgDeny {
    tools: BTreeSet<String>,
    arg: String,
    values: BTreeSet<String>,
}

impl ArgDeny {
    /// The tools that the fence covers.
    #[must_use]
    pub fn tools(&self) -> &BTreeSet<String> {
        &self.tools
    }

    /// The name of the argument.
    #[must_use]
    pub fn arg(&self) -> &str {
        &self.arg
    }

    /// The values that the fence refuses.
    #[must_use]
    pub fn values(&self) -> &BTreeSet<String> {
        &self.values
    }
}

/// One upstream of the roster: the command of one MCP server.
///
/// The type matches the Python reader, which is lax against contract 01b in
/// four places. The owner decides each case later:
///
/// 1. `command` can be each text, the empty text too.
/// 2. The name of the upstream can be each text. Contract 01b §1 gives a
///    server name the grammar of `ids::ServerName`.
/// 3. The name of a variable can be each text. Contract 01b §4.1 gives it
///    the grammar of `ids::EnvName`.
/// 4. The name of a tool can be each text. Contract 01b §5 gives it the
///    grammar of `ids::ToolName`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Upstream {
    command: String,
    args: Vec<String>,
    env: BTreeMap<String, EnvValue>,
    arg_denies: Vec<ArgDeny>,
    tools: Vec<String>,
}

impl Upstream {
    /// The program that the chaperone starts.
    #[must_use]
    pub fn command(&self) -> &str {
        &self.command
    }

    /// The arguments of the program.
    #[must_use]
    pub fn args(&self) -> &[String] {
        &self.args
    }

    /// The variables of the program.
    #[must_use]
    pub fn env(&self) -> &BTreeMap<String, EnvValue> {
        &self.env
    }

    /// The argument fences of the upstream.
    #[must_use]
    pub fn arg_denies(&self) -> &[ArgDeny] {
        &self.arg_denies
    }

    /// The tools that the server file declares. An empty list means that
    /// the file declares none, and the probe of the chaperone decides.
    #[must_use]
    pub fn tools(&self) -> &[String] {
        &self.tools
    }
}

/// The roster of the chaperone: each upstream by its name.
///
/// FAILURE ACTION. A roster that the chaperone cannot read never stops the
/// chaperone (`stage7-releases.md` §4.4, contract 04 §10 rule 3). At start,
/// the chaperone serves no MCP server: it refuses each call to one with
/// `not_implemented`, and `/healthz` publishes `roster.state: unreadable`.
/// At a reload, the set that served before keeps serving, and `/healthz`
/// publishes the same fault.
///
/// ```
/// use creche_contracts::config::roster::{RawRoster, Roster};
///
/// let raw: RawRoster = serde_json::from_str(
///     r#"{"web-search": {"command": "/opt/mcp/web-search/bin/web-search",
///         "env": {"SEARCH_API_KEY": "secret:search_api_key"}}}"#,
/// )?;
/// let roster = Roster::try_from(raw)?;
/// assert_eq!(roster.names().collect::<Vec<_>>(), ["web-search"]);
/// # Ok::<(), Box<dyn std::error::Error>>(())
/// ```
///
/// Code outside this module cannot build a value from a raw map:
///
/// ```compile_fail,E0423
/// use creche_contracts::config::roster::Roster;
///
/// let roster = Roster(std::collections::BTreeMap::new());
/// ```
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Roster(BTreeMap<String, Upstream>);

impl Roster {
    /// The roster with no upstream.
    #[must_use]
    pub fn empty() -> Self {
        Self(BTreeMap::new())
    }

    /// The upstream of one name.
    #[must_use]
    pub fn get(&self, name: &str) -> Option<&Upstream> {
        self.0.get(name)
    }

    /// The name of each upstream, in sorted order.
    pub fn names(&self) -> impl Iterator<Item = &str> {
        self.0.keys().map(String::as_str)
    }

    /// Each upstream with its name, in the sorted order of the names.
    pub fn iter(&self) -> impl Iterator<Item = (&str, &Upstream)> {
        self.0
            .iter()
            .map(|(name, upstream)| (name.as_str(), upstream))
    }

    /// The merge of this base roster and the generated roster. A name in
    /// the two takes the generated row. The second value holds each such
    /// name: the chaperone writes one log line for each.
    #[must_use]
    pub fn superseded_by(mut self, generated: Self) -> (Self, Vec<String>) {
        let mut superseded = Vec::new();
        for (name, upstream) in generated.0 {
            if self.0.insert(name.clone(), upstream).is_some() {
                superseded.push(name);
            }
        }

        (self, superseded)
    }

    /// The tree of a roster file that holds this roster.
    #[must_use]
    pub fn to_raw(&self) -> RawRoster {
        let rows = self.0.iter().map(|(name, upstream)| {
            let env = upstream
                .env
                .iter()
                .map(|(variable, value)| (variable.clone(), value.to_text()));
            let arg_denies = upstream.arg_denies.iter().map(|deny| {
                MapOnly(RawArgDeny {
                    tools: deny.tools.iter().cloned().collect(),
                    arg: deny.arg.clone(),
                    values: deny.values.iter().cloned().collect(),
                })
            });
            let row = RawUpstream {
                command: upstream.command.clone(),
                args: upstream.args.clone(),
                env: env.collect(),
                tools: upstream.tools.clone(),
                arg_denies: arg_denies.collect(),
            };

            (name.clone(), MapOnly(row))
        });

        RawRoster(Some(rows.collect()))
    }
}

impl Checked for Roster {
    const FAILURE: FailureAction =
        FailureAction::new(AtStart::RefuseEachCall, AtReload::KeepLastGood);
}

/// The fence of one raw entry, or the rule that the entry breaks.
fn arg_deny(raw: RawArgDeny) -> Result<ArgDeny, RosterFault> {
    if raw.tools.is_empty() {
        return Err(RosterFault::NoTool);
    }

    if raw.values.is_empty() {
        return Err(RosterFault::NoValue);
    }

    if raw.arg.is_empty() {
        return Err(RosterFault::NoArg);
    }

    Ok(ArgDeny {
        tools: raw.tools.into_iter().collect(),
        arg: raw.arg,
        values: raw.values.into_iter().collect(),
    })
}

impl TryFrom<RawRoster> for Roster {
    type Error = RosterErrors;

    fn try_from(raw: RawRoster) -> Result<Self, Self::Error> {
        let mut upstreams = BTreeMap::new();
        let mut issues = Vec::new();
        for (name, MapOnly(row)) in raw.0.unwrap_or_default() {
            let mut arg_denies = Vec::with_capacity(row.arg_denies.len());
            for (at, MapOnly(entry)) in row.arg_denies.into_iter().enumerate() {
                match arg_deny(entry) {
                    Ok(deny) => arg_denies.push(deny),
                    Err(fault) => issues.push(RosterIssue {
                        upstream: name.clone(),
                        at,
                        fault,
                    }),
                }
            }

            let env = row
                .env
                .into_iter()
                .map(|(variable, value)| (variable, EnvValue::of(value)));
            let upstream = Upstream {
                command: row.command,
                args: row.args,
                env: env.collect(),
                arg_denies,
                tools: row.tools,
            };
            upstreams.insert(name, upstream);
        }

        if !issues.is_empty() {
            return Err(RosterErrors(issues));
        }

        Ok(Self(upstreams))
    }
}

/// The rule that one `arg_denies` entry breaks.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RosterFault {
    /// `tools` holds no name.
    NoTool,
    /// `values` holds no value.
    NoValue,
    /// `arg` is the empty text.
    NoArg,
}

/// One reason why the tree of a roster file is not a roster: the upstream,
/// the position of the `arg_denies` entry and the rule.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RosterIssue {
    upstream: String,
    at: usize,
    fault: RosterFault,
}

impl RosterIssue {
    /// The name of the upstream. Root writes the roster, and the name is no
    /// secret.
    #[must_use]
    pub fn upstream(&self) -> &str {
        &self.upstream
    }

    /// The position of the `arg_denies` entry, from 0.
    #[must_use]
    pub fn at(&self) -> usize {
        self.at
    }

    /// The rule that the entry breaks.
    #[must_use]
    pub fn fault(&self) -> RosterFault {
        self.fault
    }
}

impl fmt::Display for RosterIssue {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        let rule = match self.fault {
            RosterFault::NoTool => "tools holds one name or more",
            RosterFault::NoValue => "values holds one value or more",
            RosterFault::NoArg => "arg is not empty",
        };

        write!(
            f,
            "upstream {:?}, arg_denies[{}]: {rule}",
            self.upstream, self.at
        )
    }
}

impl Error for RosterIssue {}

/// Each issue of one roster tree: one issue or more.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RosterErrors(Vec<RosterIssue>);

impl RosterErrors {
    /// The issues, in the sorted order of the upstream names. The slice is
    /// never empty.
    #[must_use]
    pub fn as_slice(&self) -> &[RosterIssue] {
        &self.0
    }
}

impl fmt::Display for RosterErrors {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        for (at, issue) in self.0.iter().enumerate() {
            if at > 0 {
                f.write_str("; ")?;
            }

            write!(f, "{issue}")?;
        }

        Ok(())
    }
}

impl Error for RosterErrors {}

#[cfg(test)]
mod tests {
    use serde_json::json;

    use super::*;

    fn raw(tree: serde_json::Value) -> RawRoster {
        serde_json::from_value(tree).unwrap()
    }

    fn roster(tree: serde_json::Value) -> Roster {
        Roster::try_from(raw(tree)).unwrap()
    }

    #[test]
    fn a_row_holds_a_command_and_four_optional_keys() {
        let roster = roster(json!({
            "web-search": {
                "command": "/opt/mcp/web-search/bin/web-search",
                "args": ["--stdio"],
                "env": {"SEARCH_API_KEY": "secret:search_api_key", "LOG_LEVEL": "info"},
                "tools": ["search", "fetch"],
                "arg_denies": [{"tools": ["fetch", "search"], "arg": "site", "values": ["a", "a"]}],
            },
            "plain": {"command": "run"},
        }));
        let upstream = roster.get("web-search").unwrap();
        let deny = &upstream.arg_denies()[0];

        assert_eq!(roster.names().collect::<Vec<_>>(), ["plain", "web-search"]);
        assert_eq!(upstream.command(), "/opt/mcp/web-search/bin/web-search");
        assert_eq!(upstream.args(), ["--stdio"]);
        assert_eq!(
            upstream.env().get("SEARCH_API_KEY"),
            Some(&EnvValue::Secret(String::from("search_api_key")))
        );
        assert_eq!(
            upstream.env().get("LOG_LEVEL"),
            Some(&EnvValue::Literal(String::from("info")))
        );
        assert_eq!(upstream.tools(), ["search", "fetch"]);
        assert_eq!(deny.tools().iter().collect::<Vec<_>>(), ["fetch", "search"]);
        assert_eq!(deny.arg(), "site");
        assert_eq!(deny.values().iter().collect::<Vec<_>>(), ["a"]);

        let plain = roster.get("plain").unwrap();

        assert_eq!(plain.command(), "run");
        assert!(plain.args().is_empty() && plain.env().is_empty());
        assert!(plain.tools().is_empty() && plain.arg_denies().is_empty());
        assert_eq!(roster.iter().count(), 2);
    }

    #[test]
    fn a_file_with_no_content_is_a_roster_with_no_row() {
        assert_eq!(roster(json!(null)), Roster::empty());
        assert_eq!(roster(json!({})), Roster::empty());
    }

    #[test]
    fn a_tree_of_another_shape_has_no_raw_form() {
        for tree in [
            json!([]),
            json!("text"),
            json!(7),
            json!({"a": null}),
            json!({"a": "run"}),
            json!({"a": ["run"]}),
            json!({"a": {}}),
            json!({"a": {"command": null}}),
            json!({"a": {"command": 7}}),
            json!({"a": {"command": true}}),
            json!({"a": {"command": ["run"]}}),
            json!({"a": {"command": "run", "args": null}}),
            json!({"a": {"command": "run", "args": "x"}}),
            json!({"a": {"command": "run", "args": [7]}}),
            json!({"a": {"command": "run", "env": null}}),
            json!({"a": {"command": "run", "env": []}}),
            json!({"a": {"command": "run", "env": {"A": 7}}}),
            json!({"a": {"command": "run", "tools": null}}),
            json!({"a": {"command": "run", "tools": [true]}}),
            json!({"a": {"command": "run", "arg_denies": null}}),
            json!({"a": {"command": "run", "arg_denies": {}}}),
            json!({"a": {"command": "run", "arg_denies": ["x"]}}),
            json!({"a": {"command": "run", "arg_denies": [["t"], "a", ["v"]]}}),
            json!({"a": {"command": "run", "arg_denies": [{"tools": ["t"], "arg": "a"}]}}),
            json!({"a": {"command": "run", "arg_denies": [
                {"tools": ["t"], "arg": "a", "values": ["v"], "more": 1}
            ]}}),
            json!({"a": {"command": "run", "arg_denies": [
                {"tools": "t", "arg": "a", "values": ["v"]}
            ]}}),
            json!({"a": {"command": "run", "arg_denies": [
                {"tools": ["t"], "arg": 7, "values": ["v"]}
            ]}}),
            json!({"a": {"command": "run", "user": "root"}}),
        ] {
            assert!(
                serde_json::from_value::<RawRoster>(tree.clone()).is_err(),
                "{tree}"
            );
        }
    }

    #[test]
    fn the_conversion_collects_each_bad_fence() {
        let errors = Roster::try_from(raw(json!({
            "b": {"command": "run", "arg_denies": [
                {"tools": [], "arg": "a", "values": ["v"]},
                {"tools": ["t"], "arg": "a", "values": ["v"]},
                {"tools": ["t"], "arg": "a", "values": []},
            ]},
            "a": {"command": "run", "arg_denies": [{"tools": ["t"], "arg": "", "values": ["v"]}]},
        })))
        .unwrap_err();
        let found: Vec<(&str, usize, RosterFault)> = errors
            .as_slice()
            .iter()
            .map(|issue| (issue.upstream(), issue.at(), issue.fault()))
            .collect();

        assert_eq!(
            found,
            [
                ("a", 0, RosterFault::NoArg),
                ("b", 0, RosterFault::NoTool),
                ("b", 2, RosterFault::NoValue),
            ]
        );
        assert_eq!(
            errors.to_string(),
            "upstream \"a\", arg_denies[0]: arg is not empty; upstream \"b\", arg_denies[0]: \
             tools holds one name or more; upstream \"b\", arg_denies[2]: values holds one value \
             or more"
        );
    }

    #[test]
    fn the_python_reader_is_lax_and_the_type_matches_it() {
        let roster = roster(json!({
            "": {"command": ""},
            "Not A Server/Name": {
                "command": "relative",
                "env": {"lower case": "secret:", "": ""},
                "tools": ["", "a tool"],
            },
        }));
        let upstream = roster.get("Not A Server/Name").unwrap();

        assert_eq!(roster.get("").unwrap().command(), "");
        assert_eq!(
            upstream.env().get("lower case"),
            Some(&EnvValue::Secret(String::new()))
        );
        assert_eq!(upstream.tools(), ["", "a tool"]);
    }

    #[test]
    fn the_generated_roster_wins_a_name() {
        let base = roster(json!({"a": {"command": "base-a"}, "b": {"command": "base-b"}}));
        let generated = roster(json!({"b": {"command": "new-b"}, "c": {"command": "new-c"}}));
        let (merged, superseded) = base.superseded_by(generated);

        assert_eq!(merged.names().collect::<Vec<_>>(), ["a", "b", "c"]);
        assert_eq!(merged.get("a").unwrap().command(), "base-a");
        assert_eq!(merged.get("b").unwrap().command(), "new-b");
        assert_eq!(superseded, ["b"]);
    }

    #[test]
    fn the_tree_of_a_roster_reads_back_as_the_same_roster() {
        let tree = json!({
            "web-search": {
                "command": "/opt/mcp/web-search/bin/web-search",
                "args": ["--stdio"],
                "env": {"SEARCH_API_KEY": "secret:search_api_key", "LOG_LEVEL": "info"},
                "tools": ["search"],
                "arg_denies": [{"tools": ["search"], "arg": "site", "values": ["a", "b"]}],
            },
            "plain": {"command": "run", "args": [], "env": {}},
        });
        let first = roster(tree.clone());
        let written = serde_json::to_value(first.to_raw()).unwrap();

        assert_eq!(written, tree);
        assert_eq!(Roster::try_from(raw(written)), Ok(first));
        assert_eq!(
            serde_json::to_value(Roster::empty().to_raw()).unwrap(),
            json!({})
        );
    }

    #[test]
    fn a_roster_that_cannot_be_read_never_stops_the_chaperone() {
        assert_eq!(Roster::FAILURE.at_start(), AtStart::RefuseEachCall);
        assert_eq!(Roster::FAILURE.at_reload(), AtReload::KeepLastGood);
    }
}
