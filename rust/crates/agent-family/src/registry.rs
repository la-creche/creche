//! The registry loader: `families/`, `mcp/` and `skills/` under one root.
//!
//! The loader reads each file one time, checks each file against the rest
//! and gives one report for each family and for each server. A bad file
//! never stops the others (contract 01 §7 rule 5).

use std::collections::{BTreeMap, BTreeSet};
use std::fs;
use std::path::{Path, PathBuf};

use creche_contracts::family::{Family, Issue, RawFamily, Severity};
use creche_contracts::server::{RawServer, Server};
use creche_contracts::tomlfile::{self, FileKind};
use creche_util::{hex, pytext, sha256};

use crate::crossref::{HostFacts, Index, check_family, check_server};
use crate::parse::{DOCUMENT, Markup, parse_family, parse_family_toml, parse_server};
use crate::report::Report;
use crate::zones::ZoneFacts;

const SERVER_FILE: &str = "server.yaml";
const SKILL_FILE: &str = "SKILL.md";
const INSTRUCTIONS_FILE: &str = "instructions.md";
const FAMILIES_DIR: &str = "families";
const MCP_DIR: &str = "mcp";
const SKILLS_DIR: &str = "skills";

/// The start of a revision, for example `reg-9f21c4` (contract 05).
const REVISION_PREFIX: &str = "reg-";

/// The count of hexadecimal characters of the digest in a revision.
const REVISION_CHARS: usize = 6;

/// One read of one registry root.
#[derive(Debug, Clone, PartialEq)]
pub struct Registry {
    root: PathBuf,
    revision: String,
    parsed_families: BTreeMap<String, RawFamily>,
    families: BTreeMap<String, Family>,
    parsed_servers: BTreeMap<String, RawServer>,
    servers: BTreeMap<String, Server>,
    skills: BTreeSet<String>,
    reports: BTreeMap<String, Report>,
    server_reports: BTreeMap<String, Report>,
}

impl Registry {
    #[must_use]
    pub fn root(&self) -> &Path {
        &self.root
    }

    /// The revision: a digest of each file of the registry with its path.
    #[must_use]
    pub fn revision(&self) -> &str {
        &self.revision
    }

    /// Each family whose report holds no error, by the name of its
    /// directory.
    #[must_use]
    pub fn families(&self) -> &BTreeMap<String, Family> {
        &self.families
    }

    /// Each family file that has the right shape, by the name of its
    /// directory. Such a file can break a rule. The Python loader calls this
    /// map `families`.
    #[must_use]
    pub fn parsed_families(&self) -> &BTreeMap<String, RawFamily> {
        &self.parsed_families
    }

    /// Each server whose report holds no error, by the name of its
    /// directory.
    #[must_use]
    pub fn servers(&self) -> &BTreeMap<String, Server> {
        &self.servers
    }

    /// Each server file that has the right shape, by the name of its
    /// directory. The Python loader calls this map `servers`.
    #[must_use]
    pub fn parsed_servers(&self) -> &BTreeMap<String, RawServer> {
        &self.parsed_servers
    }

    /// The name of each skill directory that holds a `SKILL.md`.
    #[must_use]
    pub fn skills(&self) -> &BTreeSet<String> {
        &self.skills
    }

    /// The report of each directory under `families/`, by its name.
    #[must_use]
    pub fn reports(&self) -> &BTreeMap<String, Report> {
        &self.reports
    }

    /// The report of each directory under `mcp/`, by its name.
    #[must_use]
    pub fn server_reports(&self) -> &BTreeMap<String, Report> {
        &self.server_reports
    }

    /// Whether no report holds an error.
    #[must_use]
    pub fn ok(&self) -> bool {
        self.all_reports().iter().all(|report| report.ok())
    }

    /// Each report, in the order of the names. For a family and a server
    /// with one name, the report is the report of the family.
    #[must_use]
    pub fn all_reports(&self) -> Vec<&Report> {
        let mut by_name: BTreeMap<&str, &Report> = BTreeMap::new();
        for (name, report) in self.server_reports.iter().chain(&self.reports) {
            by_name.insert(name, report);
        }

        by_name.into_values().collect()
    }

    /// The instructions of a family. The layout gives this path, and the
    /// family file never names it (contract 01 §1 rule 2).
    #[must_use]
    pub fn instructions(&self, family: &str) -> PathBuf {
        self.root
            .join(FAMILIES_DIR)
            .join(family)
            .join(INSTRUCTIONS_FILE)
    }

    /// The file of one skill.
    #[must_use]
    pub fn skill_file(&self, skill: &str) -> PathBuf {
        self.root.join(SKILLS_DIR).join(skill).join(SKILL_FILE)
    }
}

fn error(msg: String) -> Issue {
    Issue {
        severity: Severity::Error,
        loc: DOCUMENT.to_owned(),
        msg,
        downgraded: false,
    }
}

fn warning(loc: &str, msg: String) -> Issue {
    Issue {
        severity: Severity::Warning,
        loc: loc.to_owned(),
        msg,
        downgraded: false,
    }
}

fn name_of(path: &Path) -> String {
    path.file_name()
        .map(|name| name.to_string_lossy().into_owned())
        .unwrap_or_default()
}

/// The reason that the loader reports for a file that it cannot read.
fn cannot_read(name: &str, error: &std::io::Error) -> String {
    let text = error.to_string();
    // The text of an operating system error ends with its number.
    let reason = text.split(" (os error ").next().unwrap_or(&text);

    format!("cannot read {name}: {reason}")
}

/// The text of a file, as Python reads it: UTF-8, with each line end as one
/// `\n`. The error is the reason that the loader reports.
fn read(path: &Path) -> Result<String, String> {
    let name = name_of(path);
    let bytes = fs::read(path).map_err(|error| cannot_read(&name, &error))?;
    let text = String::from_utf8(bytes)
        .map_err(|_| format!("cannot read {name}: it is not UTF-8 text"))?;

    Ok(pytext::universal_newlines(&text).into_owned())
}

/// The bytes of a TOML family file. The read stops one byte past the limit
/// of the file kind, so the TOML reader refuses a larger file with its own
/// text. The error is the reason that the loader reports.
fn read_toml(path: &Path) -> Result<Vec<u8>, String> {
    let name = name_of(path);
    let file = fs::File::open(path).map_err(|error| cannot_read(&name, &error))?;

    tomlfile::take_bytes(file, FileKind::Family).map_err(|error| cannot_read(&name, &error))
}

/// Each directory under `base`, in the order of the names.
fn subdirectories(base: &Path) -> Vec<PathBuf> {
    let Ok(entries) = fs::read_dir(base) else {
        return Vec::new();
    };

    let mut directories: Vec<PathBuf> = entries
        .filter_map(Result::ok)
        .map(|entry| entry.path())
        .filter(|path| path.is_dir())
        .collect();
    directories.sort();

    directories
}

/// Each file below `base`. The search does not go through a symbolic link
/// to a directory.
fn files_below(base: &Path, found: &mut Vec<PathBuf>) {
    let Ok(entries) = fs::read_dir(base) else {
        return;
    };

    for entry in entries.filter_map(Result::ok) {
        let path = entry.path();
        if path.is_file() {
            found.push(path);
        } else if entry.file_type().is_ok_and(|kind| kind.is_dir()) {
            files_below(&path, found);
        }
    }
}

/// The parts of the path of a file, from the root of the registry.
fn parts_from(root: &Path, path: &Path) -> Vec<String> {
    path.strip_prefix(root)
        .unwrap_or(path)
        .components()
        .map(|part| part.as_os_str().to_string_lossy().into_owned())
        .collect()
}

/// The revision of a registry: a digest of each file with its path. A file
/// with a new name gives a new revision.
#[must_use]
pub fn revision_of(root: &Path) -> String {
    let mut files = Vec::new();
    for directory in [FAMILIES_DIR, MCP_DIR, SKILLS_DIR] {
        files_below(&root.join(directory), &mut files);
    }

    // Python sorts the paths part by part, not as text.
    let mut files: Vec<(Vec<String>, PathBuf)> = files
        .into_iter()
        .map(|path| (parts_from(root, &path), path))
        .collect();
    files.sort();
    let mut data = Vec::new();
    for (parts, path) in files {
        data.extend_from_slice(parts.join("/").as_bytes());
        data.push(0);
        data.extend_from_slice(read(&path).unwrap_or_default().as_bytes());
        data.push(0);
    }

    let digest = hex::lower(&sha256::digest(&data));
    let short: String = digest.chars().take(REVISION_CHARS).collect();

    format!("{REVISION_PREFIX}{short}")
}

/// What one directory under `families/` or `mcp/` holds.
enum Found {
    /// The text of the file.
    Text(String),
    /// The issue for a directory with no file, or with a file that the
    /// loader cannot read.
    Issue(Issue),
}

fn find(directory: &Path, file: &str) -> Found {
    let path = directory.join(file);
    if !path.is_file() {
        return Found::Issue(warning(
            DOCUMENT,
            format!("no {file}; this directory is ignored"),
        ));
    }

    match read(&path) {
        Ok(text) => Found::Text(text),
        Err(problem) => Found::Issue(error(problem)),
    }
}

struct Servers {
    parsed: BTreeMap<String, RawServer>,
    valid: BTreeMap<String, Server>,
    reports: BTreeMap<String, Report>,
}

fn read_servers(root: &Path) -> Servers {
    let mut servers = Servers {
        parsed: BTreeMap::new(),
        valid: BTreeMap::new(),
        reports: BTreeMap::new(),
    };
    for directory in subdirectories(&root.join(MCP_DIR)) {
        let name = name_of(&directory);
        let file = format!("{MCP_DIR}/{name}/{SERVER_FILE}");
        let issues = match find(&directory, SERVER_FILE) {
            Found::Issue(issue) => vec![issue],
            Found::Text(text) => match parse_server(&text) {
                (Some(raw), _) => {
                    servers.parsed.insert(name.clone(), raw.clone());
                    let (server, issues) = check_server(raw, &name);
                    if let Some(server) = server {
                        servers.valid.insert(name.clone(), server);
                    }

                    issues
                }
                (None, issues) => issues,
            },
        };
        servers
            .reports
            .insert(name.clone(), Report::new(&name, &file, issues));
    }

    servers
}

/// One family file, read and parsed. A file that the loader cannot read
/// gives one error.
fn read_family(path: &Path, markup: Markup) -> (Option<RawFamily>, Vec<Issue>) {
    let read = match markup {
        Markup::Toml => read_toml(path).map(|bytes| parse_family_toml(&bytes)),
        Markup::Yaml => read(path).map(|text| parse_family(&text, Markup::Yaml)),
    };

    read.unwrap_or_else(|problem| (None, vec![error(problem)]))
}

/// The name of the family file in `markup`. The loader reads the TOML file
/// first and the YAML file second, and a directory with both is an error.
const fn family_file(markup: Markup) -> &'static str {
    match markup {
        Markup::Toml => "family.toml",
        Markup::Yaml => "family.yaml",
    }
}

/// The family file of the directory `name`, as its report names it.
fn report_file(name: &str, markup: Markup) -> String {
    format!("{FAMILIES_DIR}/{name}/{}", family_file(markup))
}

/// What the directories under `families/` give the loader.
struct Families {
    /// Each family file with the right shape, and the markup of that file.
    parsed: BTreeMap<String, (Markup, RawFamily)>,
    /// The report of each other directory.
    reports: BTreeMap<String, Report>,
}

fn read_families(root: &Path) -> Families {
    let mut families = Families {
        parsed: BTreeMap::new(),
        reports: BTreeMap::new(),
    };
    for directory in subdirectories(&root.join(FAMILIES_DIR)) {
        let name = name_of(&directory);
        let has_toml = directory.join(family_file(Markup::Toml)).is_file();
        let has_yaml = directory.join(family_file(Markup::Yaml)).is_file();
        // The report names the TOML file when it is there, else the YAML
        // file.
        let markup = if has_toml { Markup::Toml } else { Markup::Yaml };
        let issues = if has_toml && has_yaml {
            // CONTRACT-QUESTION: contract 01 §1 names one file,
            // `family.toml`. It does not say what a reader does with a
            // directory that also holds `family.yaml`. The loader takes the
            // strict reading, as the Python loader does: one error, and it
            // parses neither file. The report names `family.toml`. The other
            // reading names `family.yaml`. A change costs the line that
            // selects `markup` and one vector.
            vec![error(format!(
                "two family files: {} and {}; keep one",
                family_file(Markup::Toml),
                family_file(Markup::Yaml)
            ))]
        } else if !has_toml && !has_yaml {
            // Contract 01 §5.6 rule 3: a warning, and `caregiver` ignores
            // the directory.
            let file = family_file(Markup::Yaml);
            vec![warning(
                DOCUMENT,
                format!("no {file}; this directory is ignored"),
            )]
        } else {
            match read_family(&directory.join(family_file(markup)), markup) {
                (Some(raw), _) => {
                    families.parsed.insert(name, (markup, raw));
                    continue;
                }
                (None, issues) => issues,
            }
        };

        let report = Report::new(&name, &report_file(&name, markup), issues);
        families.reports.insert(name, report);
    }

    families
}

fn read_skills(root: &Path) -> BTreeSet<String> {
    subdirectories(&root.join(SKILLS_DIR))
        .iter()
        .filter(|directory| directory.join(SKILL_FILE).is_file())
        .map(|directory| name_of(directory))
        .collect()
}

fn add_issue(reports: &mut BTreeMap<String, Report>, name: &str, issue: Issue) {
    if let Some(report) = reports.get_mut(name) {
        *report = report.with_issue(issue);
    }
}

/// Contract 01 §5.6 rule 1: one family claims a webhook name. Each file that
/// claims the name gets the error, because each author can fix it.
fn check_webhooks(families: &BTreeMap<String, RawFamily>, reports: &mut BTreeMap<String, Report>) {
    let mut claims: Vec<(&str, Vec<&str>)> = Vec::new();
    for (name, family) in families {
        let webhooks = family
            .triggers
            .iter()
            .flatten()
            .filter_map(|trigger| trigger.webhook.as_deref());
        for webhook in webhooks {
            match claims.iter_mut().find(|(claimed, _)| *claimed == webhook) {
                Some((_, owners)) => owners.push(name),
                None => claims.push((webhook, vec![name])),
            }
        }
    }

    for (webhook, owners) in claims {
        if owners.len() < 2 {
            continue;
        }

        for owner in &owners {
            let others: BTreeSet<&str> = owners
                .iter()
                .copied()
                .filter(|other| other != owner)
                .collect();
            let others: Vec<&str> = others.into_iter().collect();
            let mut issue = error(format!(
                "webhook '{webhook}' is also claimed by {}",
                others.join(", ")
            ));
            "triggers".clone_into(&mut issue.loc);
            add_issue(reports, owner, issue);
        }
    }
}

/// Contract 01 §3.13 rule 6: an `enqueue` trigger that no family targets
/// can never fire. The issue is a warning: the grant of the caller and the
/// trigger of the target are in two files, and each one can come first.
fn check_dispatch(families: &BTreeMap<String, RawFamily>, reports: &mut BTreeMap<String, Report>) {
    let targeted: BTreeSet<&str> = families
        .values()
        .filter_map(|family| family.verbs.enqueue.as_ref())
        .flat_map(|fence| fence.targets.iter().map(String::as_str))
        .collect();
    for (name, family) in families {
        let dispatches = family
            .triggers
            .iter()
            .flatten()
            .any(|trigger| trigger.enqueue == Some(true));
        if !dispatches || targeted.contains(name.as_str()) {
            continue;
        }

        let issue = warning(
            "triggers",
            format!("no family enqueues '{name}', so this trigger can never fire"),
        );
        add_issue(reports, name, issue);
    }
}

/// Reads one registry, parses each file and checks each file against the
/// rest. The function never fails on the content of a file.
///
/// At this process edge the failure action is a report: a file that the
/// loader cannot read or cannot parse has a report with an error, and the
/// family keeps its last good state.
#[must_use]
pub fn load_registry(root: &Path, host: Option<&dyn HostFacts>, zones: &dyn ZoneFacts) -> Registry {
    let servers = read_servers(root);
    let Families {
        parsed: read,
        mut reports,
    } = read_families(root);
    let skills = read_skills(root);
    let index = Index {
        kinds: read
            .iter()
            .map(|(name, (_, family))| (name.clone(), family.kind.clone()))
            .collect(),
        servers: servers
            .parsed
            .iter()
            .map(|(name, server)| {
                let tools = server.tool_names().map(str::to_owned).collect();

                (name.clone(), tools)
            })
            .collect(),
        skills: skills.clone(),
    };
    let mut parsed = BTreeMap::new();
    let mut families = BTreeMap::new();
    for (name, (markup, raw)) in read {
        let (family, issues) = check_family(raw.clone(), &name, &index, host, zones);
        if let Some(family) = family {
            families.insert(name.clone(), family);
        }

        let report = Report::new(&name, &report_file(&name, markup), issues);
        reports.insert(name.clone(), report);
        parsed.insert(name, raw);
    }

    check_webhooks(&parsed, &mut reports);
    check_dispatch(&parsed, &mut reports);
    // A family with an error from a check of the whole registry is not
    // valid.
    families.retain(|name, _| reports.get(name).is_some_and(Report::ok));

    Registry {
        root: root.to_path_buf(),
        revision: revision_of(root),
        parsed_families: parsed,
        families,
        parsed_servers: servers.parsed,
        servers: servers.valid,
        skills,
        reports,
        server_reports: servers.reports,
    }
}

#[cfg(test)]
mod tests {
    use super::{load_registry, revision_of};
    use crate::scratch::Scratch;
    use crate::zones::SystemZones;

    /// What the Python function `revision_of` answers for the registry of
    /// these tests, and for the same registry with one file more.
    const PYTHON_REVISION: &str = "reg-9eda66";
    const PYTHON_REVISION_ONE_MORE: &str = "reg-259ec9";

    #[cfg(unix)]
    #[test]
    fn the_revision_reads_no_directory_through_a_symbolic_link() {
        use std::os::unix::fs::symlink;

        let scratch = Scratch::new();
        scratch.write("registry/families/one/family.yaml", b"name: one\n");
        scratch.write("elsewhere/inside.txt", b"text\n");
        let root = scratch.path("registry");
        assert_eq!(revision_of(&root), PYTHON_REVISION);

        symlink(scratch.path("elsewhere"), root.join("families/linked")).unwrap();
        assert_eq!(revision_of(&root), PYTHON_REVISION);

        // A link to a file is a file of the registry.
        symlink(
            scratch.path("elsewhere/inside.txt"),
            root.join("families/one/linked.txt"),
        )
        .unwrap();
        assert_eq!(revision_of(&root), PYTHON_REVISION_ONE_MORE);
    }

    #[test]
    fn the_revision_reads_each_line_end_as_one_line_feed() {
        let scratch = Scratch::new();
        scratch.write("unix/families/one/family.yaml", b"name: one\n");
        scratch.write("dos/families/one/family.yaml", b"name: one\r\n");
        scratch.write("old/families/one/family.yaml", b"name: one\r");

        assert_eq!(revision_of(&scratch.path("unix")), PYTHON_REVISION);
        assert_eq!(revision_of(&scratch.path("dos")), PYTHON_REVISION);
        assert_eq!(revision_of(&scratch.path("old")), PYTHON_REVISION);
    }

    #[test]
    fn each_report_names_the_family_file_of_its_directory() {
        let scratch = Scratch::new();
        let toml = "kind = \"thin\"\ndescription = \"x\"\n\
                    model = { router = \"fast\", budget_usd_per_day = 1 }\n";
        let yaml = "kind: thin\ndescription: x\nmodel: { router: fast, budget_usd_per_day: 1 }\n";
        let files = [
            ("both/family.toml", "name = \"both\"\n".to_owned()),
            ("both/family.yaml", "name: both\n".to_owned()),
            ("none/notes.txt", "No family file.\n".to_owned()),
            ("toml/family.toml", format!("name = \"toml\"\n{toml}")),
            ("toml-bad/family.toml", "name = 1\n".to_owned()),
            ("yaml/family.yaml", format!("name: yaml\n{yaml}")),
            ("yaml-bad/family.yaml", "name: [1]\n".to_owned()),
        ];
        for (path, text) in &files {
            scratch.write(&format!("registry/families/{path}"), text.as_bytes());
        }

        let loaded = load_registry(&scratch.path("registry"), None, &SystemZones::host());
        let named: Vec<(&str, &str)> = loaded
            .reports()
            .iter()
            .map(|(name, report)| (name.as_str(), report.file()))
            .collect();

        assert_eq!(
            named,
            [
                ("both", "families/both/family.toml"),
                ("none", "families/none/family.yaml"),
                ("toml", "families/toml/family.toml"),
                ("toml-bad", "families/toml-bad/family.toml"),
                ("yaml", "families/yaml/family.yaml"),
                ("yaml-bad", "families/yaml-bad/family.yaml"),
            ]
        );
        let parsed: Vec<&String> = loaded.parsed_families().keys().collect();
        assert_eq!(parsed, ["toml", "yaml"]);
    }
}
