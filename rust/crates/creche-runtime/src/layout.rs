//! The paths under the state root that more than one service uses.
//!
//! One service writes a file and another service reads it. The two must give
//! the path one name. [`StateRoot`] holds each such path one time. A path
//! that only one service uses stays in the crate of that service.
//!
//! Each path takes typed parts: a `FamilyName`, a `SandboxName`, a
//! [`FaultWriter`]. No function here takes a raw text, so no caller can make
//! a path that leaves the state root.
//!
//! The Python services hold two copies of these paths:
//! `attendance/src/attendance/paths.py` and
//! `caregiver/src/caregiver/paths.py`. The two give one text for each path
//! that both hold. The table of the test in this module holds each text and
//! the Python lines of each copy.

use std::path::PathBuf;

use creche_contracts::config::DirPath;
use creche_contracts::ids::{FamilyName, SandboxName, WebhookName};
use creche_contracts::status::fault_file::FaultFile;
use creche_contracts::status::words::FaultSource;

/// The names under the state root. The Python names are the constants of
/// `attendance/src/attendance/paths.py:20-34` and of
/// `caregiver/src/caregiver/paths.py:26-47`.
const FAMILIES: &str = "families";
const GRANTS: &str = "grants";
const FAULTS: &str = "faults";
const OUTCOMES: &str = "outcomes";
const AUDIT: &str = "audit";
const TOKENS: &str = "tokens";
const TRIGGERS: &str = "triggers";
const WEBHOOKS: &str = "webhooks";

/// The names under the directory of one family.
const STATUS_FILE: &str = "status.json";
const VALIDATION_FILE: &str = "validation.json";
const CREDS: &str = "creds";
const CONFIG: &str = "config";
const CONTROL: &str = "control";

/// The end of the name of a file for one family, and of a token file.
const JSON_SUFFIX: &str = ".json";
const TOKEN_SUFFIX: &str = ".token";

/// A service that writes a fault file (contract 05 §3.3.1).
///
/// `attendance` and the chaperone each write one fault file for a family.
/// `caregiver` reads the files and writes none. `FaultSource` has `caregiver`
/// as its third member, so that type cannot name a fault directory. This type
/// holds only the two that can.
///
/// ```
/// use creche_contracts::status::words::FaultSource;
/// use creche_runtime::layout::FaultWriter;
///
/// assert_eq!(FaultSource::from(FaultWriter::Sessiond), FaultSource::Sessiond);
/// assert_eq!(FaultSource::from(FaultWriter::Pep), FaultSource::Pep);
/// ```
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum FaultWriter {
    /// `attendance`. Its files are in `faults/sessiond/`.
    Sessiond,
    /// The chaperone. Its files are in `faults/pep/`.
    Pep,
}

impl FaultWriter {
    /// The writer of `file`. `None` for a file whose source is `caregiver`.
    ///
    /// `FaultFile::new` refuses that source, so `None` does not occur for a
    /// file that the code built. The function still gives it as a value: a
    /// later change to `FaultFile` must not become a panic here.
    #[must_use]
    pub fn of(file: &FaultFile) -> Option<Self> {
        match file.source() {
            FaultSource::Sessiond => Some(Self::Sessiond),
            FaultSource::Pep => Some(Self::Pep),
            FaultSource::Managerd => None,
        }
    }
}

impl From<FaultWriter> for FaultSource {
    fn from(writer: FaultWriter) -> Self {
        match writer {
            FaultWriter::Sessiond => Self::Sessiond,
            FaultWriter::Pep => Self::Pep,
        }
    }
}

/// The state root of the platform: the directory that holds the state of
/// each family.
///
/// Each function returns one path under the root. No function reads the
/// disk.
///
/// ```
/// use std::path::Path;
///
/// use creche_contracts::config::DirPath;
/// use creche_contracts::ids::{FamilyName, SandboxName};
/// use creche_runtime::layout::StateRoot;
///
/// fn layout(dir: DirPath) -> StateRoot {
///     StateRoot::new(dir)
/// }
///
/// let root = layout("/srv/agents/state/rework".parse()?);
/// let family: FamilyName = "chat".parse()?;
/// let sandbox: SandboxName = "chat-s12".parse()?;
///
/// assert_eq!(
///     root.status_file(&family),
///     Path::new("/srv/agents/state/rework/families/chat/status.json")
/// );
/// assert_eq!(
///     root.control_dir(&sandbox),
///     Path::new("/srv/agents/state/rework/families/chat/control/chat-s12")
/// );
/// # Ok::<(), Box<dyn std::error::Error>>(())
/// ```
///
/// Code outside this module cannot reach the field. [`StateRoot::new`] is
/// the one constructor, and it takes a `DirPath`, so each root is an
/// absolute path:
///
/// ```compile_fail,E0423
/// use creche_contracts::config::DirPath;
/// use creche_runtime::layout::StateRoot;
///
/// fn layout(dir: DirPath) -> StateRoot {
///     StateRoot(dir)
/// }
/// ```
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct StateRoot(DirPath);

impl StateRoot {
    /// The layout under the directory `root`.
    ///
    /// The Python origin is the `state_root` argument of each function of
    /// `attendance/src/attendance/paths.py:91-177`, and the `root` argument
    /// of each function of `caregiver/src/caregiver/paths.py:54-193`.
    #[must_use]
    pub fn new(root: DirPath) -> Self {
        Self(root)
    }

    /// The path of one entry of the root. `name` is a constant of this
    /// module, so it is one plain part.
    fn entry(&self, name: &str) -> PathBuf {
        self.0.as_path().join(name)
    }

    /// The directory that holds one directory for each family.
    ///
    /// The Python origins are `attendance/src/attendance/paths.py:127-129`
    /// and `caregiver/src/caregiver/paths.py:26`.
    #[must_use]
    pub fn families_dir(&self) -> PathBuf {
        self.entry(FAMILIES)
    }

    /// The directory of one family.
    ///
    /// The Python origins are `attendance/src/attendance/paths.py:91-92` and
    /// `caregiver/src/caregiver/paths.py:54-58`.
    #[must_use]
    pub fn family_dir(&self, family: &FamilyName) -> PathBuf {
        self.families_dir().join(family.as_str())
    }

    /// The status document of one family (contract 05 §2).
    ///
    /// The Python origins are `attendance/src/attendance/paths.py:95-97` and
    /// `caregiver/src/caregiver/paths.py:61-63`.
    #[must_use]
    pub fn status_file(&self, family: &FamilyName) -> PathBuf {
        self.family_dir(family).join(STATUS_FILE)
    }

    /// The validation report of one family.
    ///
    /// The Python origin is `caregiver/src/caregiver/paths.py:66-68`.
    #[must_use]
    pub fn validation_file(&self, family: &FamilyName) -> PathBuf {
        self.family_dir(family).join(VALIDATION_FILE)
    }

    /// The directory that holds the credentials mount of one family.
    ///
    /// The Python origins are `attendance/src/attendance/paths.py:117-119`
    /// and `caregiver/src/caregiver/paths.py:71-74`.
    #[must_use]
    pub fn creds_dir(&self, family: &FamilyName) -> PathBuf {
        self.family_dir(family).join(CREDS)
    }

    /// The directory that holds the config mount of one family.
    ///
    /// The Python origins are `attendance/src/attendance/paths.py:122-124`
    /// and `caregiver/src/caregiver/paths.py:81-85`.
    #[must_use]
    pub fn config_dir(&self, family: &FamilyName) -> PathBuf {
        self.family_dir(family).join(CONFIG)
    }

    /// The control directory of one sandbox. The function takes the family
    /// from the sandbox name, so the two cannot differ.
    ///
    /// The Python origins are `attendance/src/attendance/paths.py:100-105`
    /// and `caregiver/src/caregiver/paths.py:95-100`. Each one takes the
    /// family and the sandbox as two texts, so its caller can give the
    /// sandbox of another family. This function takes one `SandboxName` and
    /// reads the family from it.
    #[must_use]
    pub fn control_dir(&self, sandbox: &SandboxName) -> PathBuf {
        self.family_dir(sandbox.family())
            .join(CONTROL)
            .join(sandbox.as_str())
    }

    /// The grant file of one family (contract 04 §1).
    ///
    /// The Python origin is `caregiver/src/caregiver/paths.py:163-165`.
    #[must_use]
    pub fn grant_file(&self, family: &FamilyName) -> PathBuf {
        self.entry(GRANTS).join(json_name(family))
    }

    /// The directory that holds each fault file of one writer.
    ///
    /// The Python origins are `attendance/src/attendance/paths.py:137-138`
    /// and the directory of `caregiver/src/caregiver/paths.py:189-193`. The
    /// copy of `caregiver` takes each text as the source of a fault file,
    /// also the name of `caregiver`, which writes no fault file. This
    /// function takes a [`FaultWriter`], which has the two writers only.
    #[must_use]
    pub fn fault_dir(&self, writer: FaultWriter) -> PathBuf {
        self.entry(FAULTS).join(FaultSource::from(writer).as_str())
    }

    /// The fault file of one writer for one family (contract 05 §3.3.1).
    ///
    /// The Python origins are `attendance/src/attendance/paths.py:132-134`
    /// and `caregiver/src/caregiver/paths.py:189-193`. The copy of
    /// `caregiver` takes each text as the source. This function takes a
    /// [`FaultWriter`], as [`StateRoot::fault_dir`] does.
    #[must_use]
    pub fn fault_file(&self, writer: FaultWriter, family: &FamilyName) -> PathBuf {
        self.fault_dir(writer).join(json_name(family))
    }

    /// The directory that holds the outcome records of one family.
    ///
    /// The Python origin is the directory of
    /// `attendance/src/attendance/paths.py:151-153`.
    #[must_use]
    pub fn outcomes_dir(&self, family: &FamilyName) -> PathBuf {
        self.entry(OUTCOMES).join(family.as_str())
    }

    /// The directory that holds the audit log of the chaperone.
    ///
    /// The Python origins are the directory of
    /// `attendance/src/attendance/paths.py:171-177` and
    /// `caregiver/src/caregiver/paths.py:29`.
    #[must_use]
    pub fn audit_dir(&self) -> PathBuf {
        self.entry(AUDIT)
    }

    /// The directory that holds one token file for each principal of
    /// `attendance`.
    ///
    /// The Python origins are the directory of
    /// `attendance/src/attendance/paths.py:146-148` and of
    /// `caregiver/src/caregiver/paths.py:168-172`.
    #[must_use]
    pub fn tokens_dir(&self) -> PathBuf {
        self.entry(TOKENS)
    }

    /// The token file of one webhook of one family.
    ///
    /// The Python origin is `caregiver/src/caregiver/paths.py:175-186`.
    #[must_use]
    pub fn webhook_token_file(&self, family: &FamilyName, name: &WebhookName) -> PathBuf {
        self.entry(TRIGGERS)
            .join(WEBHOOKS)
            .join(family.as_str())
            .join(format!("{}{TOKEN_SUFFIX}", name.as_str()))
    }
}

/// The name of the file of one family in a directory that holds one file for
/// each family: `<family>.json`.
fn json_name(family: &FamilyName) -> String {
    format!("{}{JSON_SUFFIX}", family.as_str())
}

#[cfg(test)]
mod tests {
    use std::path::Path;

    use creche_contracts::status::time::Timestamp;

    use super::*;

    /// The state root of the examples in the contracts.
    const ROOT: &str = "/srv/agents/state/rework";

    fn root_at(text: &str) -> StateRoot {
        StateRoot::new(text.parse().unwrap())
    }

    fn family(text: &str) -> FamilyName {
        text.parse().unwrap()
    }

    fn sandbox(text: &str) -> SandboxName {
        text.parse().unwrap()
    }

    /// One path of the layout: the name of the function, the path that it
    /// gives, the text that the Python copies give, and the lines of each
    /// Python copy.
    type Row = (&'static str, PathBuf, &'static str, &'static [&'static str]);

    /// Each path of [`StateRoot`] for the family `chat`, the sandbox
    /// `chat-s12` and the webhook `door-bell`.
    ///
    /// No vector covers a path. Each text is what the Python functions of
    /// the last column give for the same root and the same names. A row
    /// with two Python copies is a path that the two give with one text.
    fn table(root: &StateRoot) -> Vec<Row> {
        let chat = family("chat");
        let chat_s12 = sandbox("chat-s12");
        let door_bell: WebhookName = "door-bell".parse().unwrap();

        vec![
            (
                "families_dir",
                root.families_dir(),
                "/srv/agents/state/rework/families",
                &[
                    "attendance/src/attendance/paths.py:127-129",
                    "caregiver/src/caregiver/paths.py:26",
                ],
            ),
            (
                "family_dir",
                root.family_dir(&chat),
                "/srv/agents/state/rework/families/chat",
                &[
                    "attendance/src/attendance/paths.py:91-92",
                    "caregiver/src/caregiver/paths.py:54-58",
                ],
            ),
            (
                "status_file",
                root.status_file(&chat),
                "/srv/agents/state/rework/families/chat/status.json",
                &[
                    "attendance/src/attendance/paths.py:95-97",
                    "caregiver/src/caregiver/paths.py:61-63",
                ],
            ),
            (
                "validation_file",
                root.validation_file(&chat),
                "/srv/agents/state/rework/families/chat/validation.json",
                &["caregiver/src/caregiver/paths.py:66-68"],
            ),
            (
                "creds_dir",
                root.creds_dir(&chat),
                "/srv/agents/state/rework/families/chat/creds",
                &[
                    "attendance/src/attendance/paths.py:117-119",
                    "caregiver/src/caregiver/paths.py:71-74",
                ],
            ),
            (
                "config_dir",
                root.config_dir(&chat),
                "/srv/agents/state/rework/families/chat/config",
                &[
                    "attendance/src/attendance/paths.py:122-124",
                    "caregiver/src/caregiver/paths.py:81-85",
                ],
            ),
            (
                "control_dir",
                root.control_dir(&chat_s12),
                "/srv/agents/state/rework/families/chat/control/chat-s12",
                &[
                    "attendance/src/attendance/paths.py:100-105",
                    "caregiver/src/caregiver/paths.py:95-100",
                ],
            ),
            (
                "grant_file",
                root.grant_file(&chat),
                "/srv/agents/state/rework/grants/chat.json",
                &["caregiver/src/caregiver/paths.py:163-165"],
            ),
            (
                "fault_dir of attendance",
                root.fault_dir(FaultWriter::Sessiond),
                "/srv/agents/state/rework/faults/sessiond",
                &[
                    "attendance/src/attendance/paths.py:137-138",
                    "caregiver/src/caregiver/paths.py:189-193",
                ],
            ),
            (
                "fault_dir of the chaperone",
                root.fault_dir(FaultWriter::Pep),
                "/srv/agents/state/rework/faults/pep",
                &["caregiver/src/caregiver/paths.py:189-193"],
            ),
            (
                "fault_file of attendance",
                root.fault_file(FaultWriter::Sessiond, &chat),
                "/srv/agents/state/rework/faults/sessiond/chat.json",
                &[
                    "attendance/src/attendance/paths.py:132-134",
                    "caregiver/src/caregiver/paths.py:189-193",
                ],
            ),
            (
                "fault_file of the chaperone",
                root.fault_file(FaultWriter::Pep, &chat),
                "/srv/agents/state/rework/faults/pep/chat.json",
                &["caregiver/src/caregiver/paths.py:189-193"],
            ),
            (
                "outcomes_dir",
                root.outcomes_dir(&chat),
                "/srv/agents/state/rework/outcomes/chat",
                &["attendance/src/attendance/paths.py:151-153"],
            ),
            (
                "audit_dir",
                root.audit_dir(),
                "/srv/agents/state/rework/audit",
                &[
                    "attendance/src/attendance/paths.py:171-177",
                    "caregiver/src/caregiver/paths.py:29",
                ],
            ),
            (
                "tokens_dir",
                root.tokens_dir(),
                "/srv/agents/state/rework/tokens",
                &[
                    "attendance/src/attendance/paths.py:146-148",
                    "caregiver/src/caregiver/paths.py:168-172",
                ],
            ),
            (
                "webhook_token_file",
                root.webhook_token_file(&chat, &door_bell),
                "/srv/agents/state/rework/triggers/webhooks/chat/door-bell.token",
                &["caregiver/src/caregiver/paths.py:175-186"],
            ),
        ]
    }

    #[test]
    fn each_path_has_the_text_of_the_python_copies() {
        let root = root_at(ROOT);

        for (name, path, text, python) in table(&root) {
            // The compare is on the text. A compare of two paths takes a
            // doubled slash, a `.` part and a final slash as equal.
            assert_eq!(path.to_str(), Some(text), "{name}");
            assert!(!python.is_empty(), "{name}");

            for origin in python {
                let (file, lines) = origin.rsplit_once(':').unwrap();

                assert!(file.ends_with("/paths.py"), "{name}: {origin}");
                assert!(
                    lines
                        .bytes()
                        .all(|byte| byte.is_ascii_digit() || byte == b'-'),
                    "{name}: {origin}"
                );
            }
        }
    }

    #[test]
    fn each_path_is_under_the_root_and_two_rows_never_share_a_path() {
        let root = root_at(ROOT);
        let rows = table(&root);

        for (name, path, _text, _python) in &rows {
            assert!(path.starts_with(ROOT), "{name}");
            assert_ne!(path, Path::new(ROOT), "{name}");
        }

        let mut paths: Vec<&PathBuf> = rows.iter().map(|(_, path, _, _)| path).collect();
        paths.sort();
        paths.dedup();

        assert_eq!(paths.len(), rows.len());
    }

    #[test]
    fn the_root_is_in_the_normal_form_of_its_type() {
        // `DirPath` drops a final slash, as `pathlib` of Python does.
        assert_eq!(root_at("/srv/agents/state/rework/"), root_at(ROOT));
        assert_eq!(
            root_at("/srv/agents/state/rework/").tokens_dir().to_str(),
            Some("/srv/agents/state/rework/tokens")
        );
        assert_eq!(root_at("/").families_dir().to_str(), Some("/families"));
        assert_eq!(
            root_at("/").status_file(&family("chat")).to_str(),
            Some("/families/chat/status.json")
        );
    }

    #[test]
    fn the_files_of_one_family_are_in_the_directory_of_that_family() {
        let root = root_at(ROOT);
        let chat = family("chat");
        let dir = root.family_dir(&chat);

        assert_eq!(dir.parent(), Some(root.families_dir().as_path()));

        for path in [
            root.status_file(&chat),
            root.validation_file(&chat),
            root.creds_dir(&chat),
            root.config_dir(&chat),
        ] {
            assert_eq!(path.parent(), Some(dir.as_path()), "{}", path.display());
        }

        assert_eq!(
            root.fault_file(FaultWriter::Pep, &chat).parent(),
            Some(root.fault_dir(FaultWriter::Pep).as_path())
        );
    }

    #[test]
    fn two_families_and_two_writers_never_share_a_path() {
        let root = root_at(ROOT);
        let chat = family("chat");
        let worker = family("worker");

        assert_ne!(root.family_dir(&chat), root.family_dir(&worker));
        assert_ne!(root.grant_file(&chat), root.grant_file(&worker));
        assert_ne!(root.outcomes_dir(&chat), root.outcomes_dir(&worker));
        assert_ne!(
            root.fault_file(FaultWriter::Sessiond, &chat),
            root.fault_file(FaultWriter::Pep, &chat)
        );
        assert_ne!(
            root.fault_file(FaultWriter::Pep, &chat),
            root.fault_file(FaultWriter::Pep, &worker)
        );
    }

    /// [`StateRoot::control_dir`] takes one sandbox name. Each Python copy
    /// takes the family and the sandbox as two texts.
    #[test]
    fn the_control_dir_is_in_the_family_of_the_sandbox() {
        let root = root_at(ROOT);

        // The family name of the second sandbox holds `-s1` itself.
        for (name, its_family) in [("chat-s12", "chat"), ("chat-s1-s2", "chat-s1")] {
            let name = sandbox(name);
            let dir = root.control_dir(&name);

            assert_eq!(name.family().as_str(), its_family);
            assert_eq!(
                dir,
                root.family_dir(&family(its_family))
                    .join("control")
                    .join(name.as_str())
            );
        }
    }

    /// [`StateRoot::fault_dir`] takes a [`FaultWriter`]. The Python copy of
    /// `caregiver` takes each text as the source of a fault file.
    #[test]
    fn only_two_sources_have_a_fault_dir() {
        let root = root_at(ROOT);
        let dirs: Vec<PathBuf> = [FaultWriter::Sessiond, FaultWriter::Pep]
            .into_iter()
            .map(|writer| root.fault_dir(writer))
            .collect();

        assert_eq!(
            dirs,
            [
                Path::new("/srv/agents/state/rework/faults/sessiond"),
                Path::new("/srv/agents/state/rework/faults/pep"),
            ]
        );
        assert!(
            !dirs
                .iter()
                .any(|dir| dir.ends_with(FaultSource::Managerd.as_str()))
        );
    }

    fn fault_file(source: FaultSource) -> FaultFile {
        let family: FamilyName = "chat".parse().unwrap();
        let written_at: Timestamp = "2031-04-18T06:43:10Z".parse().unwrap();

        FaultFile::new(family, source, written_at, Vec::new()).unwrap()
    }

    #[test]
    fn each_writer_is_its_fault_source() {
        assert_eq!(
            FaultSource::from(FaultWriter::Sessiond),
            FaultSource::Sessiond
        );
        assert_eq!(FaultSource::from(FaultWriter::Pep), FaultSource::Pep);
    }

    #[test]
    fn the_writer_of_a_file_is_the_source_of_the_file() {
        for writer in [FaultWriter::Sessiond, FaultWriter::Pep] {
            let file = fault_file(writer.into());

            assert_eq!(FaultWriter::of(&file), Some(writer));
        }
    }

    #[test]
    fn caregiver_is_no_writer_and_no_file_has_it_as_source() {
        // `FaultFile::new` refuses the one source that `FaultWriter::of`
        // gives `None` for, so no test can build that file.
        let family: FamilyName = "chat".parse().unwrap();
        let written_at: Timestamp = "2031-04-18T06:43:10Z".parse().unwrap();

        assert!(FaultFile::new(family, FaultSource::Managerd, written_at, Vec::new()).is_err());
        assert!(
            FaultSource::ALL
                .iter()
                .filter(|source| **source != FaultSource::Managerd)
                .all(|source| [FaultWriter::Sessiond, FaultWriter::Pep]
                    .iter()
                    .any(|writer| FaultSource::from(*writer) == *source))
        );
    }
}
