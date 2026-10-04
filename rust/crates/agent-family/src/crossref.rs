//! The rules of a family file that need another file of the registry or the
//! host (contract 01 §5).
//!
//! `Family::try_from` checks each rule that one file decides. This module
//! checks the rest: a delegate exists and is thin, a server declares a tool,
//! the router serves a model alias, a mount resolves to a path under an
//! allowed root. Each issue has the slot of its place in the Python report,
//! so the issues of the two passes sort into one order.

use std::collections::{BTreeMap, BTreeSet};

use creche_contracts::family::{
    CALL_SEPARATOR, Family, Issue, Kind, MountPath, MountPathError, PLATFORM_SERVER,
    PLATFORM_WITHHELD, Placed, RawFamily, RawToolGrant, SURVEY_TOOL, Section, Severity, Slot,
    mount_root_issue,
};
use creche_contracts::ids::{SkillName, ToolName};
use creche_contracts::server::{RawServer, Server};

use crate::zones::ZoneFacts;

/// What only the host knows. `caregiver` answers from the model router and
/// from the file system.
pub trait HostFacts {
    /// Whether the model router serves `alias` now.
    fn serves_alias(&self, alias: &str) -> bool;

    /// `path` with each symbolic link resolved.
    fn real_path(&self, path: &str) -> String;
}

/// The rest of the registry, as the cross-reference checks need it.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Index {
    /// The kind of each family file that has the right shape, as the file
    /// wrote it, by the name of its directory.
    pub kinds: BTreeMap<String, String>,
    /// The tool names of each server file that has the right shape, in the
    /// order of the file, by the name of its directory.
    pub servers: BTreeMap<String, Vec<String>>,
    /// The name of each skill directory that holds a `SKILL.md`.
    pub skills: BTreeSet<String>,
}

impl Index {
    fn declares(&self, server: &str, tool: &str) -> bool {
        self.servers
            .get(server)
            .is_some_and(|tools| tools.iter().any(|name| name == tool))
    }
}

fn downgraded(slot: Slot, loc: &str, msg: String) -> Placed {
    let mut placed = Placed::warning(slot, loc, msg);
    placed.issue.downgraded = true;

    placed
}

fn check_model(raw: &RawFamily, host: Option<&dyn HostFacts>, out: &mut Vec<Placed>) {
    let at = Slot::of(Section::Model).step(2).registry();
    let router = &raw.model.router;
    let Some(host) = host else {
        out.push(downgraded(
            at,
            "model.router",
            format!("no LiteLLM context: '{router}' was not checked"),
        ));

        return;
    };

    if !host.serves_alias(router) {
        out.push(Placed::error(
            at,
            "model.router",
            format!("'{router}' is not an alias LiteLLM serves"),
        ));
    }
}

fn check_files(raw: &RawFamily, host: Option<&dyn HostFacts>, out: &mut Vec<Placed>) {
    let section = Slot::of(Section::Files);
    let Some(host) = host else {
        if !raw.files.is_empty() {
            out.push(downgraded(
                section.item(usize::MAX).registry(),
                "files",
                "no host context: no mount's symlinks were resolved".to_owned(),
            ));
        }

        return;
    };

    for (index, mount) in raw.files.iter().enumerate() {
        let has_form = match mount.path.parse::<MountPath>() {
            Ok(_) => true,
            Err(error) => MountPathError::has_path_form(error),
        };
        if !has_form {
            continue;
        }

        let resolved = host.real_path(&mount.path);
        if resolved == mount.path {
            continue;
        }

        if let Some(msg) = mount_root_issue(raw, &resolved) {
            out.push(Placed::error(
                section.item(index).registry(),
                format!("files[{index}].path"),
                msg,
            ));
        }
    }
}

fn check_tools(raw: &RawFamily, index: &Index, out: &mut Vec<Placed>) {
    for (rank, (server, grant)) in raw.tools.iter().enumerate() {
        let at = Slot::of(Section::Tools).item(rank);
        let loc = format!("tools.{server}");
        let declared = index.servers.get(server);
        if declared.is_none() {
            out.push(Placed::error(
                at.step(1).registry(),
                &loc,
                format!("'{server}' has no mcp/{server}/server.yaml"),
            ));
        }

        if server == PLATFORM_SERVER && raw.grants_all(server) {
            let withheld = declared
                .into_iter()
                .flatten()
                .enumerate()
                .filter(|(_, tool)| PLATFORM_WITHHELD.contains(&tool.as_str()));
            for (position, tool) in withheld {
                out.push(Placed::error(
                    at.step(2).place(position).registry(),
                    &loc,
                    format!(
                        "'all' grants '{tool}', which is never granted: a platform change lands \
                         only when the operator merges it (§5.5 rule 7); drop it from \
                         mcp/{server}/server.yaml or name each tool"
                    ),
                ));
            }
        }

        let (RawToolGrant::Named(tools), Some(declared)) = (grant, declared) else {
            continue;
        };

        for (position, tool) in tools.iter().enumerate() {
            if tool.parse::<ToolName>().is_ok() && !declared.contains(tool) {
                out.push(Placed::error(
                    at.step(4).place(position).registry(),
                    format!("{loc}[{position}]"),
                    format!("'{tool}' is not declared in mcp/{server}/server.yaml"),
                ));
            }
        }
    }
}

fn check_enqueue(raw: &RawFamily, index: &Index, out: &mut Vec<Placed>) {
    let targets = raw.verbs.enqueue.iter().flat_map(|fence| &fence.targets);
    for (position, target) in targets.enumerate() {
        let msg = match index.kinds.get(target).map(String::as_str) {
            None => format!("'{target}' is not a family in this registry"),
            Some(kind) if kind != Kind::Autonomous.as_str() => {
                format!("'{target}' is '{kind}'; an enqueue target must be autonomous")
            }
            Some(_) => continue,
        };
        out.push(Placed::error(
            Slot::of(Section::Verbs).item(1).place(position).registry(),
            format!("verbs.enqueue.targets[{position}]"),
            msg,
        ));
    }
}

fn check_delegates(raw: &RawFamily, index: &Index, out: &mut Vec<Placed>) {
    for (position, target) in raw.delegates.iter().enumerate() {
        if *target == raw.name {
            continue;
        }

        let msg = match index.kinds.get(target).map(String::as_str) {
            None => format!("'{target}' is not a family in this registry"),
            Some(kind) if kind != Kind::Thin.as_str() => {
                format!("'{target}' exists but its kind is '{kind}'; a delegate must be thin")
            }
            Some(_) => continue,
        };
        out.push(Placed::error(
            Slot::of(Section::Delegates).item(position + 1).registry(),
            format!("delegates[{position}]"),
            msg,
        ));
    }
}

fn check_skills(raw: &RawFamily, index: &Index, out: &mut Vec<Placed>) {
    for (position, skill) in raw.skills.iter().enumerate() {
        if skill.parse::<SkillName>().is_ok() && !index.skills.contains(skill) {
            out.push(Placed::error(
                Slot::of(Section::Skills).item(position).registry(),
                format!("skills[{position}]"),
                format!("'{skill}' has no skills/{skill}/SKILL.md"),
            ));
        }
    }
}

fn check_approval(raw: &RawFamily, index: &Index, out: &mut Vec<Placed>) {
    let mut seen = BTreeSet::new();
    for (position, entry) in raw.approval.iter().enumerate() {
        if !seen.insert(entry.as_str()) {
            continue;
        }

        let Some((server, tool)) = entry.split_once(CALL_SEPARATOR) else {
            continue;
        };

        if tool != "*" && raw.grants_all(server) && !index.declares(server, tool) {
            out.push(Placed::error(
                Slot::of(Section::Approval).item(position).registry(),
                format!("approval[{position}]"),
                format!("'{entry}' is not a tool this file grants from '{server}'"),
            ));
        }
    }
}

fn check_quiet(raw: &RawFamily, index: &Index, zones: &dyn ZoneFacts, out: &mut Vec<Placed>) {
    let at = Slot::of(Section::Quiet);
    let Some(quiet) = &raw.quiet else {
        return;
    };

    if Kind::parse(&raw.kind) != Some(Kind::Autonomous) {
        return;
    }

    if let Some(server) = &quiet.board
        && raw.grants_all(server)
        && !index.declares(server, SURVEY_TOOL)
    {
        out.push(Placed::error(
            at.step(3).registry(),
            "quiet.board",
            format!("'{server}' does not grant '{SURVEY_TOOL}', which 'quiet.board' reads"),
        ));
    }

    let Some(daily) = &quiet.daily else {
        return;
    };

    if let Some((server, tool)) = daily.call.split_once(CALL_SEPARATOR)
        && raw.grants_all(server)
        && !index.declares(server, tool)
    {
        out.push(Placed::error(
            at.step(4).registry(),
            "quiet.daily.call",
            format!(
                "'{}' is not a verb or a '<server>{CALL_SEPARATOR}<tool>' this file grants",
                daily.call
            ),
        ));
    }

    if !zones.knows(&daily.zone) {
        out.push(Placed::error(
            at.step(6).registry(),
            "quiet.daily.zone",
            format!("'{}' is not an IANA time zone", daily.zone),
        ));
    }
}

/// Each issue of the rules that need the registry or the host.
fn check_links(
    raw: &RawFamily,
    directory: &str,
    index: &Index,
    host: Option<&dyn HostFacts>,
    zones: &dyn ZoneFacts,
) -> Vec<Placed> {
    let mut out = Vec::new();
    if raw.name != directory {
        out.push(Placed::error(
            Slot::of(Section::Identity).step(2).registry(),
            "name",
            format!(
                "'{}' does not match its directory 'families/{directory}'",
                raw.name
            ),
        ));
    }

    check_model(raw, host, &mut out);
    check_files(raw, host, &mut out);
    check_tools(raw, index, &mut out);
    check_enqueue(raw, index, &mut out);
    check_delegates(raw, index, &mut out);
    check_skills(raw, index, &mut out);
    check_approval(raw, index, &mut out);
    check_quiet(raw, index, zones, &mut out);

    out
}

fn in_report_order(mut placed: Vec<Placed>) -> Vec<Issue> {
    placed.sort_by_key(|placed| placed.slot);

    placed.into_iter().map(|placed| placed.issue).collect()
}

fn has_error(issues: &[Issue]) -> bool {
    issues.iter().any(|issue| issue.severity == Severity::Error)
}

/// Each rule that contract 01 states for one family file, in the order of
/// the contract: the answer of the Python `check_family`.
///
/// The family is `Some` only when no rule gives an error. A caller that
/// holds one does not read the issues to trust it.
#[must_use]
pub fn check_family(
    raw: RawFamily,
    directory: &str,
    index: &Index,
    host: Option<&dyn HostFacts>,
    zones: &dyn ZoneFacts,
) -> (Option<Family>, Vec<Issue>) {
    let mut placed = check_links(&raw, directory, index, host, zones);
    let family = match Family::try_from(raw) {
        Ok(family) => {
            placed.extend(family.warnings().iter().cloned());
            Some(family)
        }
        Err(refused) => {
            placed.extend(refused.into_issues());
            None
        }
    };
    let issues = in_report_order(placed);
    let family = family.filter(|_| !has_error(&issues));

    (family, issues)
}

/// Each rule that contract 01b states for one server file: the answer of
/// the Python `check_server`.
///
/// The server is `Some` only when no rule gives an error.
#[must_use]
pub fn check_server(raw: RawServer, directory: &str) -> (Option<Server>, Vec<Issue>) {
    let mut placed = Vec::new();
    if raw.name != directory {
        placed.push(Placed::error(
            Slot::of(Section::ServerName).step(1).registry(),
            "name",
            format!(
                "'{}' does not match its directory 'mcp/{directory}'",
                raw.name
            ),
        ));
    }

    let server = match Server::try_from(raw) {
        Ok(server) => Some(server),
        Err(refused) => {
            placed.extend(refused.into_issues());
            None
        }
    };
    let issues = in_report_order(placed);
    let server = server.filter(|_| !has_error(&issues));

    (server, issues)
}

#[cfg(test)]
mod tests {
    use creche_contracts::family::{RawFamily, RawModel, Severity};

    use super::{Index, check_family};
    use crate::zones::ZoneFacts;

    struct NoZones;

    impl ZoneFacts for NoZones {
        fn knows(&self, _key: &str) -> bool {
            false
        }
    }

    fn thin(name: &str) -> RawFamily {
        RawFamily::with_defaults(
            name.to_owned(),
            "thin".to_owned(),
            "One written input.".to_owned(),
            RawModel {
                router: "fast".to_owned(),
                budget_usd_per_day: 1.0,
            },
        )
    }

    #[test]
    fn a_family_with_no_error_is_valid() {
        let (family, issues) = check_family(thin("one"), "one", &Index::default(), None, &NoZones);

        assert!(family.is_some());
        assert!(
            issues
                .iter()
                .all(|issue| issue.severity == Severity::Warning)
        );
    }

    /// The file obeys each rule that one file decides. The one error is a
    /// rule of the registry: the name is not the name of the directory.
    #[test]
    fn a_family_with_only_a_registry_error_is_not_valid() {
        let (family, issues) =
            check_family(thin("one"), "other", &Index::default(), None, &NoZones);
        let errors: Vec<&str> = issues
            .iter()
            .filter(|issue| issue.severity == Severity::Error)
            .map(|issue| issue.msg.as_str())
            .collect();

        assert!(family.is_none());
        assert_eq!(
            errors,
            ["'one' does not match its directory 'families/other'"]
        );
    }
}
