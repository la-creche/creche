//! The mount files of a sandbox: `runtime.json`, `creds.json` and the env
//! file of the playpen.
//!
//! The caregiver writes the three files. The playpen reads them in the
//! sandbox, through two read-only mounts and through `sbx exec --env-file`.
//!
//! | File | Type | Contract |
//! |---|---|---|
//! | `<config dir>/runtime.json` | [`RuntimeConfig`], [`RuntimeView`] | 01 §6.1, 03 §7.1 |
//! | `<creds dir>/creds.json` | [`Credentials`] | 03 §12 |
//! | `supervisor-<sandbox>.env` | [`PlaypenEnv`] | 03 §7.1, 05 §4.1.1 |
//!
//! Each writer here gives the bytes that the Python caregiver writes. The
//! binary writes the bytes to a temporary file in the same directory and
//! renames that file, so a reader never sees half a file.

use std::collections::BTreeMap;
use std::error::Error;
use std::fmt;
use std::fmt::Write as _;
use std::str::FromStr;

use serde::Deserialize;
use serde_json::Value;

use super::pytext;
use super::shape::MapOnly;
use super::values::{DirPath, PathError};
use crate::ids::{SandboxName, SandboxNameError};
use crate::secret::Secret;

// --- JSON as Python writes it ---

/// The JSON text of one string as Python's `json.dumps` writes it: each
/// character outside the range from space to `~` is an escape.
fn json_string(text: &str) -> String {
    let mut out = String::with_capacity(text.len() + 2);
    out.push('"');
    for character in text.chars() {
        match character {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{08}' => out.push_str("\\b"),
            '\u{0c}' => out.push_str("\\f"),
            ' '..='~' => out.push(character),
            _ => {
                let mut units = [0; 2];
                for unit in character.encode_utf16(&mut units) {
                    // A write to a `String` does not fail.
                    let _ = write!(out, "\\u{unit:04x}");
                }
            }
        }
    }
    out.push('"');

    out
}

// --- runtime.json ---

/// The name of the file in the family config mount.
pub const RUNTIME_FILE: &str = "runtime.json";

/// A built-in tool of pi that a family file can grant (contract 01 §3.8).
///
/// The set is closed. `bash` is never a member: `shell` is the one way to a
/// shell.
///
/// A reader in a sandbox can get a value from a newer caregiver.
/// [`RuntimeView::read`] drops an unknown value: deny by default (invariant
/// 11). The strict conversion to [`RuntimeConfig`] refuses it.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub enum SandboxTool {
    /// `read`
    Read,
    /// `write`
    Write,
    /// `edit`
    Edit,
    /// `grep`
    Grep,
    /// `find`
    Find,
    /// `ls`
    Ls,
    /// `codemode`, the built-in extension of pi.
    Codemode,
}

impl SandboxTool {
    /// Each tool.
    pub const ALL: [Self; 7] = [
        Self::Read,
        Self::Write,
        Self::Edit,
        Self::Grep,
        Self::Find,
        Self::Ls,
        Self::Codemode,
    ];

    /// The tools of a family whose file names none (contract 01 §3.8).
    pub const DEFAULT: [Self; 4] = [Self::Read, Self::Grep, Self::Find, Self::Ls];

    /// The tool as the file writes it.
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Read => "read",
            Self::Write => "write",
            Self::Edit => "edit",
            Self::Grep => "grep",
            Self::Find => "find",
            Self::Ls => "ls",
            Self::Codemode => "codemode",
        }
    }

    /// The tool of a name. `None` for a name that is not in the set.
    fn of(name: &str) -> Option<Self> {
        Self::ALL.into_iter().find(|tool| tool.as_str() == name)
    }
}

/// How `instructions.md` meets the system prompt of pi (contract 01 §3.8).
///
/// The set is closed. A reader in a sandbox can get a value from a newer
/// caregiver. [`RuntimeView::read`] reads an unknown value as `Append`: that
/// reading keeps the prompt of pi. The strict conversion to
/// [`RuntimeConfig`] refuses it.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum SystemPrompt {
    /// After the prompt of pi. The file holds no key for this value.
    Append,
    /// In place of the prompt of pi.
    Replace,
}

impl SystemPrompt {
    /// The value as the file writes it.
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Append => "append",
            Self::Replace => "replace",
        }
    }
}

/// A LiteLLM alias: `[a-z0-9][a-z0-9._/-]*` (contract 01 §3.2).
///
/// The grammar is the one of `agent_family.grammar.MODEL_ALIAS`. It has no
/// cap, as the contract has none.
///
/// The alias is bare. The playpen puts `litellm/` before it for pi, and that
/// form never leaves the sandbox (contract 01 §6.1 rule 3).
///
/// ```
/// use creche_contracts::config::mounts::ModelAlias;
///
/// let alias: ModelAlias = "agent-router".parse()?;
/// assert_eq!(alias.as_str(), "agent-router");
/// # Ok::<(), creche_contracts::config::mounts::ModelAliasError>(())
/// ```
///
/// Code outside this module cannot build a value from a raw string:
///
/// ```compile_fail,E0423
/// use creche_contracts::config::mounts::ModelAlias;
///
/// let alias = ModelAlias(String::from("*"));
/// ```
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct ModelAlias(String);

impl ModelAlias {
    /// The alias as text.
    #[must_use]
    pub fn as_str(&self) -> &str {
        &self.0
    }
}

impl FromStr for ModelAlias {
    type Err = ModelAliasError;

    fn from_str(text: &str) -> Result<Self, Self::Err> {
        let bytes = text.as_bytes();
        if bytes.is_empty() {
            return Err(ModelAliasError::Empty);
        }

        let plain = |byte: &u8| byte.is_ascii_lowercase() || byte.is_ascii_digit();
        let first_holds = bytes.first().is_some_and(plain);
        let tail_holds = bytes
            .iter()
            .all(|byte| plain(byte) || matches!(byte, b'.' | b'_' | b'/' | b'-'));
        if !first_holds || !tail_holds {
            return Err(ModelAliasError::BadByte);
        }

        Ok(Self(text.to_owned()))
    }
}

/// Why a text is not a model alias.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ModelAliasError {
    /// The text has no byte.
    Empty,
    /// The first byte is not `a` to `z` or a digit, or a later byte is not
    /// `a` to `z`, a digit, `.`, `_`, `/` or `-`.
    BadByte,
}

impl fmt::Display for ModelAliasError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Empty => f.write_str("a model alias has 1 byte or more"),
            Self::BadByte => f.write_str(
                "a model alias starts with a to z or 0 to 9 and holds a to z, 0 to 9, ., _, / and -",
            ),
        }
    }
}

impl Error for ModelAliasError {}

/// Whether a family has the shell tool of pi.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum Shell {
    /// The family has no shell.
    Off,
    /// The family has the shell.
    On,
}

/// The keys of a `runtime.json`.
#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct RuntimeFields {
    shell: bool,
    sandbox_tools: Vec<String>,
    model_alias: String,
    #[serde(default)]
    system_prompt: Option<String>,
}

/// The raw form of `runtime.json`: one mapping with the keys of contract 01
/// §6.1 rule 2 and `system_prompt`, and no other key.
#[derive(Debug, Deserialize)]
#[serde(transparent)]
pub struct RawRuntimeConfig(MapOnly<RuntimeFields>);

/// `runtime.json` as the caregiver writes it (contract 01 §6.1).
///
/// The file holds no credential and no grant. No field is part of the
/// perimeter (contract 01 §3.8 rule 3).
///
/// This is the strict form. The conversion from [`RawRuntimeConfig`] refuses
/// each value that the contract does not name, and it collects each issue.
/// The playpen does not use this form to read the file: [`RuntimeView`] is
/// its reading.
///
/// ```
/// use creche_contracts::config::mounts::{RuntimeConfig, SandboxTool, Shell, SystemPrompt};
///
/// let config = RuntimeConfig::new(
///     Shell::Off,
///     vec![SandboxTool::Read, SandboxTool::Grep],
///     "agent-router".parse()?,
///     SystemPrompt::Append,
/// )?;
/// let text = config.to_json();
/// assert!(text.starts_with("{\n  \"shell\": false,\n"));
/// assert_eq!(RuntimeConfig::parse(text.as_bytes())?, config);
/// # Ok::<(), Box<dyn std::error::Error>>(())
/// ```
///
/// Code outside this module cannot build a value from raw parts:
///
/// ```compile_fail,E0451
/// use creche_contracts::config::mounts::{RuntimeConfig, Shell, SystemPrompt};
///
/// let config = RuntimeConfig {
///     shell: Shell::On,
///     sandbox_tools: Vec::new(),
///     model_alias: "agent-router".parse().unwrap(),
///     system_prompt: SystemPrompt::Append,
/// };
/// ```
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RuntimeConfig {
    shell: Shell,
    sandbox_tools: Vec<SandboxTool>,
    model_alias: ModelAlias,
    system_prompt: SystemPrompt,
}

impl RuntimeConfig {
    /// The config of the four values.
    ///
    /// # Errors
    ///
    /// [`RuntimeIssue::DuplicateTool`] for a tool that the list holds two
    /// times.
    pub fn new(
        shell: Shell,
        sandbox_tools: Vec<SandboxTool>,
        model_alias: ModelAlias,
        system_prompt: SystemPrompt,
    ) -> Result<Self, RuntimeIssue> {
        if let Some(at) = first_duplicate(&sandbox_tools) {
            return Err(RuntimeIssue::DuplicateTool { at });
        }

        Ok(Self {
            shell,
            sandbox_tools,
            model_alias,
            system_prompt,
        })
    }

    /// Parses the bytes of a `runtime.json`, in the strict form.
    ///
    /// # Errors
    ///
    /// [`RuntimeConfigErrors`] holds each issue of the file.
    pub fn parse(bytes: &[u8]) -> Result<Self, RuntimeConfigErrors> {
        let raw: RawRuntimeConfig = serde_json::from_slice(bytes)
            .map_err(|_| RuntimeConfigErrors(vec![RuntimeIssue::BadShape]))?;

        Self::try_from(raw)
    }

    /// Whether the family has the shell tool of pi.
    #[must_use]
    pub fn shell(&self) -> Shell {
        self.shell
    }

    /// The built-in tools of pi that the family has, in the order of the
    /// family file.
    #[must_use]
    pub fn sandbox_tools(&self) -> &[SandboxTool] {
        &self.sandbox_tools
    }

    /// The model that the playpen gives to pi when a turn names none.
    #[must_use]
    pub fn model_alias(&self) -> &ModelAlias {
        &self.model_alias
    }

    /// How `instructions.md` meets the system prompt of pi.
    #[must_use]
    pub fn system_prompt(&self) -> SystemPrompt {
        self.system_prompt
    }

    /// The bytes of the file, as the Python caregiver writes them:
    /// `json.dumps(body, indent=2)` and one newline.
    ///
    /// The file holds `system_prompt` only for `replace`. The caregiver
    /// compares the bytes of the mount with the bytes that it would write,
    /// so one more key would write the mount of each family again.
    #[must_use]
    pub fn to_json(&self) -> String {
        let shell = self.shell == Shell::On;
        let mut text = format!("{{\n  \"shell\": {shell},\n  \"sandbox_tools\": [");
        for (at, tool) in self.sandbox_tools.iter().enumerate() {
            let separator = if at == 0 { "" } else { "," };
            text.push_str(separator);
            text.push_str("\n    ");
            text.push_str(&json_string(tool.as_str()));
        }

        if !self.sandbox_tools.is_empty() {
            text.push_str("\n  ");
        }

        text.push_str("],\n  \"model_alias\": ");
        text.push_str(&json_string(self.model_alias.as_str()));
        if self.system_prompt != SystemPrompt::Append {
            text.push_str(",\n  \"system_prompt\": ");
            text.push_str(&json_string(self.system_prompt.as_str()));
        }

        text.push_str("\n}\n");

        text
    }
}

/// The position of the first tool that the list holds a second time.
fn first_duplicate(tools: &[SandboxTool]) -> Option<usize> {
    tools
        .iter()
        .enumerate()
        .position(|(at, tool)| tools.iter().take(at).any(|earlier| earlier == tool))
}

impl TryFrom<RawRuntimeConfig> for RuntimeConfig {
    type Error = RuntimeConfigErrors;

    fn try_from(raw: RawRuntimeConfig) -> Result<Self, Self::Error> {
        let RawRuntimeConfig(MapOnly(raw)) = raw;
        let mut issues = Vec::new();
        let mut tools = Vec::with_capacity(raw.sandbox_tools.len());
        for (at, name) in raw.sandbox_tools.iter().enumerate() {
            match SandboxTool::of(name) {
                Some(tool) if tools.contains(&tool) => {
                    issues.push(RuntimeIssue::DuplicateTool { at });
                }
                Some(tool) => tools.push(tool),
                None => issues.push(RuntimeIssue::UnknownTool { at }),
            }
        }

        let alias = raw.model_alias.parse::<ModelAlias>();
        if let Err(error) = &alias {
            issues.push(RuntimeIssue::ModelAlias(*error));
        }

        let system_prompt = match raw.system_prompt.as_deref() {
            None | Some("append") => Some(SystemPrompt::Append),
            Some("replace") => Some(SystemPrompt::Replace),
            Some(_) => {
                issues.push(RuntimeIssue::UnknownSystemPrompt);
                None
            }
        };

        match (alias, system_prompt) {
            (Ok(model_alias), Some(system_prompt)) if issues.is_empty() => Ok(Self {
                shell: if raw.shell { Shell::On } else { Shell::Off },
                sandbox_tools: tools,
                model_alias,
                system_prompt,
            }),
            _ => Err(RuntimeConfigErrors(issues)),
        }
    }
}

/// One reason why a `runtime.json` is not valid in the strict form.
///
/// No variant holds a value of the file.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RuntimeIssue {
    /// The file is not JSON, is not an object, lacks a key, holds a key that
    /// the contract does not name, or holds a value of the wrong JSON type.
    BadShape,
    /// The tool at this position, from 0, is not in the set.
    UnknownTool {
        /// The position of the tool, from 0.
        at: usize,
    },
    /// The tool at this position, from 0, is in the list a second time.
    DuplicateTool {
        /// The position of the second tool, from 0.
        at: usize,
    },
    /// `model_alias` is not a model alias.
    ModelAlias(ModelAliasError),
    /// `system_prompt` is not `append` and not `replace`.
    UnknownSystemPrompt,
}

impl fmt::Display for RuntimeIssue {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::BadShape => f.write_str("the file is not the JSON object of contract 01 §6.1"),
            Self::UnknownTool { at } => write!(f, "sandbox_tools[{at}] is not a sandbox tool"),
            Self::DuplicateTool { at } => write!(f, "sandbox_tools[{at}] is in the list two times"),
            Self::ModelAlias(error) => write!(f, "model_alias: {error}"),
            Self::UnknownSystemPrompt => f.write_str("system_prompt is not append or replace"),
        }
    }
}

impl Error for RuntimeIssue {}

/// Each issue of one `runtime.json`: one issue or more.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RuntimeConfigErrors(Vec<RuntimeIssue>);

impl RuntimeConfigErrors {
    /// The issues, in the order of the file. The slice is never empty.
    #[must_use]
    pub fn as_slice(&self) -> &[RuntimeIssue] {
        &self.0
    }
}

impl fmt::Display for RuntimeConfigErrors {
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

impl Error for RuntimeConfigErrors {}

/// `runtime.json` as the playpen reads it when it starts a pi process.
///
/// FAILURE ACTION. The read never fails. The family config mount is outside
/// the playpen, and a bad revision must not stop a session (invariant 19).
/// A file that is missing or malformed gives the safe default of each
/// field: no shell, the four default tools, no model alias and `append`. A
/// field with a bad value gives the safe default of that field. The playpen
/// publishes no fault for this file.
///
/// This type follows `playpen/src/runtime-config.ts`. No vector covers that
/// reader, because it has no Python entry point.
///
/// ```
/// use creche_contracts::config::mounts::{RuntimeView, SandboxTool, Shell};
///
/// let view = RuntimeView::read(Some(br#"{"shell": true, "sandbox_tools": ["read", "bash"]}"#));
/// assert_eq!(view.shell(), Shell::On);
/// assert_eq!(view.sandbox_tools(), [SandboxTool::Read]);
/// assert_eq!(RuntimeView::read(None).shell(), Shell::Off);
/// ```
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RuntimeView {
    shell: Shell,
    sandbox_tools: Vec<SandboxTool>,
    model_alias: Option<ModelAlias>,
    system_prompt: SystemPrompt,
}

impl RuntimeView {
    /// The view of a mount with no readable `runtime.json`.
    fn safe_default() -> Self {
        Self {
            shell: Shell::Off,
            sandbox_tools: SandboxTool::DEFAULT.to_vec(),
            model_alias: None,
            system_prompt: SystemPrompt::Append,
        }
    }

    /// Reads the bytes of the file. `None` stands for a file that is
    /// missing or that the playpen cannot read.
    #[must_use]
    pub fn read(bytes: Option<&[u8]>) -> Self {
        let parsed = bytes.and_then(|bytes| serde_json::from_slice(bytes).ok());
        let Some(Value::Object(raw)) = parsed else {
            return Self::safe_default();
        };

        let sandbox_tools = match raw.get("sandbox_tools") {
            // Deny by default: the view drops a name that is not in the set.
            Some(Value::Array(names)) => names
                .iter()
                .filter_map(|name| SandboxTool::of(name.as_str()?))
                .collect(),
            _ => SandboxTool::DEFAULT.to_vec(),
        };
        let model_alias = raw
            .get("model_alias")
            .and_then(Value::as_str)
            .and_then(|alias| alias.parse().ok());
        let replaces = raw.get("system_prompt").and_then(Value::as_str)
            == Some(SystemPrompt::Replace.as_str());

        Self {
            shell: if raw.get("shell") == Some(&Value::Bool(true)) {
                Shell::On
            } else {
                Shell::Off
            },
            sandbox_tools,
            model_alias,
            system_prompt: if replaces {
                SystemPrompt::Replace
            } else {
                SystemPrompt::Append
            },
        }
    }

    /// Whether the family has the shell tool of pi.
    #[must_use]
    pub fn shell(&self) -> Shell {
        self.shell
    }

    /// The built-in tools of pi that the family has.
    #[must_use]
    pub fn sandbox_tools(&self) -> &[SandboxTool] {
        &self.sandbox_tools
    }

    /// The model that the playpen gives to pi when a turn names none.
    /// `None` when the file gives no valid alias.
    #[must_use]
    pub fn model_alias(&self) -> Option<&ModelAlias> {
        self.model_alias.as_ref()
    }

    /// How `instructions.md` meets the system prompt of pi.
    #[must_use]
    pub fn system_prompt(&self) -> SystemPrompt {
        self.system_prompt
    }
}

impl From<&RuntimeConfig> for RuntimeView {
    fn from(config: &RuntimeConfig) -> Self {
        Self {
            shell: config.shell,
            sandbox_tools: config.sandbox_tools.clone(),
            model_alias: Some(config.model_alias.clone()),
            system_prompt: config.system_prompt,
        }
    }
}

// --- creds.json ---

/// The name of the file in the credentials directory.
pub const CREDS_FILE: &str = "creds.json";

/// The mode of `creds.json` (contract 03 §12 rule 2).
pub const CREDS_FILE_MODE: u32 = 0o600;

/// The LiteLLM key and the PEP token of one family: `creds.json` (contract
/// 03 §12, contract 05 §6.2).
///
/// `epoch` increases with each write. The two secrets are [`Secret`] values,
/// so `Debug` prints no byte of them.
///
/// The parse follows `caregiver.credentials.read_creds`, which is lax in
/// four places. The type matches it there, and the owner decides each case
/// later:
///
/// 1. `epoch` can be a JSON true or false, a JSON number with a fraction,
///    or a JSON string of decimal digits.
/// 2. `epoch` can be less than 1.
/// 3. `written_at` and `previous_expires_at` can be each text.
/// 4. A `previous_pep_token` or a `previous_expires_at` that is not a string
///    reads as absent.
///
/// The type is stricter than the Python reader in three places, because a
/// secret is never empty and is never the text of another JSON value:
/// `litellm_key`, `pep_token` and `written_at` must be JSON strings, the two
/// secrets must not be empty, and `epoch` must fit 64 bits.
///
/// FAILURE ACTION. No reader stops on this file. The caregiver reads a
/// file that it cannot parse as no credentials, and it reports a fault. The
/// playpen reads the file again for 250 ms, then answers `turn_failed` with
/// `stale_credentials` (contract 03 §12 rule 5).
///
/// ```
/// use creche_contracts::config::mounts::Credentials;
///
/// let text = br#"{"epoch": 7, "litellm_key": "sk-test", "pep_token": "TOKEN",
///                 "written_at": "2030-01-02T03:04:05Z"}"#;
/// let creds = Credentials::parse(text)?;
/// assert_eq!(creds.epoch(), 7);
/// assert!(creds.pep_token().matches(b"TOKEN"));
/// assert!(!format!("{creds:?}").contains("TOKEN"));
/// # Ok::<(), creche_contracts::config::mounts::CredentialsError>(())
/// ```
///
/// Code outside this module cannot build a value from raw parts:
///
/// ```compile_fail,E0451
/// use creche_contracts::config::mounts::Credentials;
/// use creche_contracts::secret::Secret;
///
/// let creds = Credentials {
///     epoch: 0,
///     litellm_key: Secret::try_from(String::from("k")).unwrap(),
///     pep_token: Secret::try_from(String::from("t")).unwrap(),
///     written_at: String::new(),
///     previous_pep_token: None,
///     previous_expires_at: None,
/// };
/// ```
#[derive(Debug)]
pub struct Credentials {
    epoch: i64,
    litellm_key: Secret,
    pep_token: Secret,
    written_at: String,
    previous_pep_token: Option<Secret>,
    previous_expires_at: Option<String>,
}

/// One field of `creds.json`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CredsField {
    /// `epoch`
    Epoch,
    /// `litellm_key`
    LitellmKey,
    /// `pep_token`
    PepToken,
    /// `written_at`
    WrittenAt,
    /// `previous_pep_token`
    PreviousPepToken,
}

impl CredsField {
    /// The field as the file writes it.
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Epoch => "epoch",
            Self::LitellmKey => "litellm_key",
            Self::PepToken => "pep_token",
            Self::WrittenAt => "written_at",
            Self::PreviousPepToken => "previous_pep_token",
        }
    }
}

/// Why bytes are not a `creds.json`, or why a value cannot go into one.
///
/// No variant holds a byte of the file: the file holds two secrets.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CredentialsError {
    /// The bytes are not JSON, or the JSON is not an object.
    NotAnObject,
    /// The object does not hold the field.
    Missing(CredsField),
    /// The field has a value that the type does not take.
    BadValue(CredsField),
    /// The secret of the field is not UTF-8, so it has no JSON form.
    NotText(CredsField),
}

impl fmt::Display for CredentialsError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::NotAnObject => f.write_str("creds.json is one JSON object"),
            Self::Missing(field) => write!(f, "creds.json holds no {}", field.as_str()),
            Self::BadValue(field) => {
                write!(
                    f,
                    "{} of creds.json has a value that is not valid",
                    field.as_str()
                )
            }
            Self::NotText(field) => write!(f, "{} is not UTF-8", field.as_str()),
        }
    }
}

impl Error for CredentialsError {}

/// The epoch of a JSON value, as Python's `int` reads the value.
fn epoch_of(value: &Value) -> Option<i64> {
    match value {
        Value::Bool(flag) => Some(i64::from(*flag)),
        Value::Number(number) => number.as_i64().or_else(|| {
            // Python cuts the fraction. The text form of the whole part
            // converts with no cast, and it fails for a number that does
            // not fit 64 bits.
            let whole = number.as_f64().filter(|float| float.is_finite())?.trunc();

            format!("{whole:.0}").parse().ok()
        }),
        Value::String(text) => match pytext::integer(text)? {
            pytext::Integer::Fits(epoch) => Some(epoch),
            pytext::Integer::TooLarge => None,
        },
        Value::Null | Value::Array(_) | Value::Object(_) => None,
    }
}

/// A secret of one field. The value must be a JSON string that is not
/// empty.
fn secret_of(
    raw: &serde_json::Map<String, Value>,
    field: CredsField,
) -> Result<Secret, CredentialsError> {
    let text = match raw.get(field.as_str()) {
        Some(Value::String(text)) => text,
        Some(_) => return Err(CredentialsError::BadValue(field)),
        None => return Err(CredentialsError::Missing(field)),
    };

    Secret::try_from(text.clone()).map_err(|_| CredentialsError::BadValue(field))
}

/// A field that an older epoch did not write. A value that is not a string,
/// and an empty string, read as absent.
fn optional_text<'a>(raw: &'a serde_json::Map<String, Value>, key: &str) -> Option<&'a str> {
    raw.get(key)
        .and_then(Value::as_str)
        .filter(|text| !text.is_empty())
}

impl Credentials {
    /// The credentials of one write, with no previous token.
    #[must_use]
    pub fn new(epoch: i64, litellm_key: Secret, pep_token: Secret, written_at: String) -> Self {
        Self {
            epoch,
            litellm_key,
            pep_token,
            written_at,
            previous_pep_token: None,
            previous_expires_at: None,
        }
    }

    /// The same credentials with the token of the previous epoch and the
    /// time at which the chaperone stops to accept it (contract 05 §6.3).
    ///
    /// The file holds the two fields apart, and the Python reader takes one
    /// without the other. The chaperone accepts the previous token only
    /// when the file holds the two.
    #[must_use]
    pub fn with_previous(mut self, token: Option<Secret>, expires_at: Option<String>) -> Self {
        self.previous_pep_token = token;
        self.previous_expires_at = expires_at;

        self
    }

    /// Parses the bytes of a `creds.json`.
    ///
    /// # Errors
    ///
    /// [`CredentialsError`] names the first field that the type does not
    /// take.
    pub fn parse(bytes: &[u8]) -> Result<Self, CredentialsError> {
        let parsed = serde_json::from_slice(bytes).map_err(|_| CredentialsError::NotAnObject)?;
        let Value::Object(raw) = parsed else {
            return Err(CredentialsError::NotAnObject);
        };

        let epoch = raw
            .get(CredsField::Epoch.as_str())
            .ok_or(CredentialsError::Missing(CredsField::Epoch))?;
        let epoch = epoch_of(epoch).ok_or(CredentialsError::BadValue(CredsField::Epoch))?;
        let litellm_key = secret_of(&raw, CredsField::LitellmKey)?;
        let pep_token = secret_of(&raw, CredsField::PepToken)?;
        let written_at = match raw.get(CredsField::WrittenAt.as_str()) {
            Some(Value::String(text)) => text.clone(),
            Some(_) => return Err(CredentialsError::BadValue(CredsField::WrittenAt)),
            None => return Err(CredentialsError::Missing(CredsField::WrittenAt)),
        };
        let previous_pep_token = optional_text(&raw, CredsField::PreviousPepToken.as_str())
            .and_then(|token| Secret::try_from(token.to_owned()).ok());
        let previous_expires_at = optional_text(&raw, "previous_expires_at").map(str::to_owned);

        Ok(Self {
            epoch,
            litellm_key,
            pep_token,
            written_at,
            previous_pep_token,
            previous_expires_at,
        })
    }

    /// The count of writes of the file.
    #[must_use]
    pub fn epoch(&self) -> i64 {
        self.epoch
    }

    /// The LiteLLM key of the family.
    #[must_use]
    pub fn litellm_key(&self) -> &Secret {
        &self.litellm_key
    }

    /// The PEP token of the family.
    #[must_use]
    pub fn pep_token(&self) -> &Secret {
        &self.pep_token
    }

    /// The time of the write, as the file holds it.
    #[must_use]
    pub fn written_at(&self) -> &str {
        &self.written_at
    }

    /// The token of the previous epoch, while the grant file still holds
    /// its digest.
    #[must_use]
    pub fn previous_pep_token(&self) -> Option<&Secret> {
        self.previous_pep_token.as_ref()
    }

    /// The time at which the chaperone stops to accept the previous token.
    #[must_use]
    pub fn previous_expires_at(&self) -> Option<&str> {
        self.previous_expires_at.as_deref()
    }

    /// The bytes of the file, as the Python caregiver writes them:
    /// `json.dumps(body, indent=2)` and one newline. The binary writes them
    /// with the mode [`CREDS_FILE_MODE`].
    ///
    /// This function is the one place where the two secrets leave the type
    /// as text.
    ///
    /// # Errors
    ///
    /// [`CredentialsError::NotText`] for a secret that is not UTF-8.
    pub fn to_json(&self) -> Result<String, CredentialsError> {
        let text = |secret: &Secret, field| {
            std::str::from_utf8(secret.expose_secret())
                .map(json_string)
                .map_err(|_| CredentialsError::NotText(field))
        };
        let previous_pep_token = match &self.previous_pep_token {
            Some(token) => text(token, CredsField::PreviousPepToken)?,
            None => String::from("null"),
        };
        let previous_expires_at = self
            .previous_expires_at
            .as_deref()
            .map_or_else(|| String::from("null"), json_string);

        Ok(format!(
            "{{\n  \"epoch\": {},\n  \"litellm_key\": {},\n  \"pep_token\": {},\n  \
             \"written_at\": {},\n  \"previous_pep_token\": {},\n  \
             \"previous_expires_at\": {}\n}}\n",
            self.epoch,
            text(&self.litellm_key, CredsField::LitellmKey)?,
            text(&self.pep_token, CredsField::PepToken)?,
            json_string(&self.written_at),
            previous_pep_token,
            previous_expires_at,
        ))
    }
}

// --- the env file of the playpen ---

/// The mode of the env file of the playpen (contract 03 §7.1 rule 2).
pub const PLAYPEN_ENV_MODE: u32 = 0o640;

/// The variable that names the credentials directory.
pub const CRED_DIR_VAR: &str = "AGENT_CRED_DIR";

/// The variable that names the family config directory.
pub const CONFIG_DIR_VAR: &str = "AGENT_FAMILY_CONFIG_DIR";

/// The variable that names the control directory.
pub const CONTROL_DIR_VAR: &str = "AGENT_CONTROL_DIR";

/// The variable that names the sandbox.
pub const SANDBOX_VAR: &str = "AGENT_SANDBOX";

/// What separates a name from its value in the env file.
const ASSIGN: char = '=';

/// The directory of each family under the state root.
const FAMILIES_DIR: &str = "families";

/// The credentials directory of a family.
const CREDS_DIR: &str = "creds";

/// The family config directory of a family.
const CONFIG_DIR: &str = "config";

/// The parent of the control directory of each sandbox of a family.
const CONTROL_DIR: &str = "control";

/// The lines of an env file of the playpen, as a map: the raw form.
///
/// The parse follows `caregiver.playpen_env.read_playpen_env`. A line with
/// no `=` is ignored. The name is the text before the first `=`. Nothing is
/// removed from the name or the value. The last line of a name wins.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RawPlaypenEnv(BTreeMap<String, String>);

impl RawPlaypenEnv {
    /// Parses the text of an env file. The parse never fails.
    #[must_use]
    pub fn parse(text: &str) -> Self {
        let pairs = pytext::lines(text)
            .filter_map(|line| line.split_once(ASSIGN))
            .map(|(name, value)| (name.to_owned(), value.to_owned()));

        Self(pairs.collect())
    }

    /// The value of one name. `None` for a name that the file does not set.
    #[must_use]
    pub fn get(&self, name: &str) -> Option<&str> {
        self.0.get(name).map(String::as_str)
    }

    /// Each name and its value, in the order of the names.
    pub fn iter(&self) -> impl Iterator<Item = (&str, &str)> {
        self.0
            .iter()
            .map(|(name, value)| (name.as_str(), value.as_str()))
    }
}

/// The env file of the playpen of one sandbox:
/// `supervisor-<family>-s<N>.env` (contract 03 §7.1, contract 05 §4.1.1).
///
/// `sbx` mounts a host directory at the same path in the sandbox, and
/// `sbx exec` forwards no host environment. The file tells the playpen
/// where its three directories are and which sandbox it runs in. It holds
/// no secret.
///
/// FAILURE ACTION. A variable that the file does not set is `fatal` with
/// `mount_dir_unset` for the playpen, and never a default (contract 03 §7.1
/// rule 5). The conversion from [`RawPlaypenEnv`] collects each such
/// variable.
///
/// ```
/// use creche_contracts::config::mounts::PlaypenEnv;
///
/// let env = PlaypenEnv::for_sandbox(&"/srv/agents/state/rework".parse()?, &"chat-s3".parse()?);
/// assert_eq!(
///     env.control_dir().as_str(),
///     "/srv/agents/state/rework/families/chat/control/chat-s3"
/// );
/// assert!(env.to_text().ends_with("AGENT_SANDBOX=chat-s3\n"));
/// # Ok::<(), Box<dyn std::error::Error>>(())
/// ```
///
/// Code outside this module cannot build a value from raw parts:
///
/// ```compile_fail,E0451
/// use creche_contracts::config::mounts::PlaypenEnv;
///
/// let env = PlaypenEnv {
///     cred_dir: "/a".parse().unwrap(),
///     config_dir: "/b".parse().unwrap(),
///     control_dir: "/c".parse().unwrap(),
///     sandbox: "chat-s3".parse().unwrap(),
/// };
/// ```
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PlaypenEnv {
    cred_dir: DirPath,
    config_dir: DirPath,
    control_dir: DirPath,
    sandbox: SandboxName,
}

impl PlaypenEnv {
    /// The env file of one sandbox under one state root. The three paths
    /// are the paths of `caregiver.paths`.
    #[must_use]
    pub fn for_sandbox(state_root: &DirPath, sandbox: &SandboxName) -> Self {
        let family = state_root
            .child(FAMILIES_DIR)
            .child(sandbox.family().as_str());

        Self {
            cred_dir: family.child(CREDS_DIR),
            config_dir: family.child(CONFIG_DIR),
            control_dir: family.child(CONTROL_DIR).child(sandbox.as_str()),
            sandbox: sandbox.clone(),
        }
    }

    /// The credentials directory: the place of `creds.json`.
    #[must_use]
    pub fn cred_dir(&self) -> &DirPath {
        &self.cred_dir
    }

    /// The family config directory: the place of `runtime.json`.
    #[must_use]
    pub fn config_dir(&self) -> &DirPath {
        &self.config_dir
    }

    /// The control directory of the sandbox.
    #[must_use]
    pub fn control_dir(&self) -> &DirPath {
        &self.control_dir
    }

    /// The sandbox.
    #[must_use]
    pub fn sandbox(&self) -> &SandboxName {
        &self.sandbox
    }

    /// The bytes of the file, as the Python caregiver writes them: four
    /// `NAME=VALUE` lines. The binary writes them with the mode
    /// [`PLAYPEN_ENV_MODE`].
    #[must_use]
    pub fn to_text(&self) -> String {
        format!(
            "{CRED_DIR_VAR}{ASSIGN}{}\n{CONFIG_DIR_VAR}{ASSIGN}{}\n{CONTROL_DIR_VAR}{ASSIGN}{}\n\
             {SANDBOX_VAR}{ASSIGN}{}\n",
            self.cred_dir,
            self.config_dir,
            self.control_dir,
            self.sandbox.as_str(),
        )
    }
}

/// One reason why an env file of the playpen is not valid.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PlaypenEnvError {
    /// The file does not set the variable, or sets it to the empty text.
    /// The playpen answers `fatal` with `mount_dir_unset`.
    Unset {
        /// The name of the variable.
        variable: &'static str,
    },
    /// The value of the variable is not an absolute path.
    Path {
        /// The name of the variable.
        variable: &'static str,
        /// The rule that the value breaks.
        error: PathError,
    },
    /// The value of `AGENT_SANDBOX` is not a sandbox name.
    Sandbox(SandboxNameError),
}

impl fmt::Display for PlaypenEnvError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Unset { variable } => write!(f, "{variable}: no value"),
            Self::Path { variable, error } => write!(f, "{variable}: {error}"),
            Self::Sandbox(error) => write!(f, "{SANDBOX_VAR}: {error}"),
        }
    }
}

impl Error for PlaypenEnvError {}

/// Each error of one env file of the playpen: one error or more.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PlaypenEnvErrors(Vec<PlaypenEnvError>);

impl PlaypenEnvErrors {
    /// The errors, in the order of the file that the caregiver writes. The
    /// slice is never empty.
    #[must_use]
    pub fn as_slice(&self) -> &[PlaypenEnvError] {
        &self.0
    }
}

impl fmt::Display for PlaypenEnvErrors {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        for (at, error) in self.0.iter().enumerate() {
            if at > 0 {
                f.write_str("; ")?;
            }

            write!(f, "{error}")?;
        }

        Ok(())
    }
}

impl Error for PlaypenEnvErrors {}

/// The directory that one variable names.
fn dir_of(raw: &RawPlaypenEnv, variable: &'static str) -> Result<DirPath, PlaypenEnvError> {
    let text = raw
        .get(variable)
        .filter(|text| !text.is_empty())
        .ok_or(PlaypenEnvError::Unset { variable })?;

    text.parse()
        .map_err(|error| PlaypenEnvError::Path { variable, error })
}

impl TryFrom<&RawPlaypenEnv> for PlaypenEnv {
    type Error = PlaypenEnvErrors;

    fn try_from(raw: &RawPlaypenEnv) -> Result<Self, Self::Error> {
        let sandbox = raw
            .get(SANDBOX_VAR)
            .filter(|text| !text.is_empty())
            .ok_or(PlaypenEnvError::Unset {
                variable: SANDBOX_VAR,
            })
            .and_then(|text| text.parse().map_err(PlaypenEnvError::Sandbox));

        match (
            dir_of(raw, CRED_DIR_VAR),
            dir_of(raw, CONFIG_DIR_VAR),
            dir_of(raw, CONTROL_DIR_VAR),
            sandbox,
        ) {
            (Ok(cred_dir), Ok(config_dir), Ok(control_dir), Ok(sandbox)) => Ok(Self {
                cred_dir,
                config_dir,
                control_dir,
                sandbox,
            }),
            (cred_dir, config_dir, control_dir, sandbox) => {
                let errors = [
                    cred_dir.err(),
                    config_dir.err(),
                    control_dir.err(),
                    sandbox.err(),
                ];

                Err(PlaypenEnvErrors(errors.into_iter().flatten().collect()))
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn secret(text: &str) -> Secret {
        Secret::try_from(text.to_owned()).unwrap()
    }

    fn alias() -> ModelAlias {
        "agent-router".parse().unwrap()
    }

    #[test]
    fn a_json_string_is_ascii_as_python_writes_it() {
        for (text, json) in [
            ("", r#""""#),
            ("plain", r#""plain""#),
            ("a\"b\\c", r#""a\"b\\c""#),
            ("\n\r\t\u{08}\u{0c}", r#""\n\r\t\b\f""#),
            ("\u{00}\u{1f}", r#""\u0000\u001f""#),
            ("\u{7f}", r#""\u007f""#),
            ("\u{e9}", r#""\u00e9""#),
            ("\u{2028}", r#""\u2028""#),
            ("\u{1f600}", r#""\ud83d\ude00""#),
            ("a/b", r#""a/b""#),
        ] {
            assert_eq!(json_string(text), json, "{text:?}");
        }
    }

    #[test]
    fn a_model_alias_is_a_litellm_alias() {
        for text in ["agent-router", "a", "0", "a.b_c/d-e", &"a".repeat(300)] {
            assert_eq!(text.parse::<ModelAlias>().unwrap().as_str(), text);
        }

        for (text, error) in [
            ("", ModelAliasError::Empty),
            ("*", ModelAliasError::BadByte),
            ("Agent", ModelAliasError::BadByte),
            ("-a", ModelAliasError::BadByte),
            ("litellm/*", ModelAliasError::BadByte),
            ("a b", ModelAliasError::BadByte),
            ("a\n", ModelAliasError::BadByte),
            ("é", ModelAliasError::BadByte),
        ] {
            assert_eq!(text.parse::<ModelAlias>().unwrap_err(), error, "{text:?}");
        }
    }

    #[test]
    fn the_writer_gives_the_file_of_the_contract() {
        let config = RuntimeConfig::new(
            Shell::Off,
            vec![
                SandboxTool::Read,
                SandboxTool::Grep,
                SandboxTool::Find,
                SandboxTool::Ls,
            ],
            alias(),
            SystemPrompt::Append,
        )
        .unwrap();

        assert_eq!(
            config.to_json(),
            "{\n  \"shell\": false,\n  \"sandbox_tools\": [\n    \"read\",\n    \"grep\",\n    \
             \"find\",\n    \"ls\"\n  ],\n  \"model_alias\": \"agent-router\"\n}\n"
        );
    }

    #[test]
    fn the_writer_holds_the_system_prompt_only_for_replace() {
        let config =
            RuntimeConfig::new(Shell::On, Vec::new(), alias(), SystemPrompt::Replace).unwrap();

        assert_eq!(
            config.to_json(),
            "{\n  \"shell\": true,\n  \"sandbox_tools\": [],\n  \"model_alias\": \
             \"agent-router\",\n  \"system_prompt\": \"replace\"\n}\n"
        );
    }

    #[test]
    fn the_strict_parse_reads_back_what_the_writer_gives() {
        for system_prompt in [SystemPrompt::Append, SystemPrompt::Replace] {
            for shell in [Shell::Off, Shell::On] {
                let config =
                    RuntimeConfig::new(shell, SandboxTool::ALL.to_vec(), alias(), system_prompt)
                        .unwrap();
                let parsed = RuntimeConfig::parse(config.to_json().as_bytes()).unwrap();

                assert_eq!(parsed, config);
                assert_eq!(parsed.shell(), shell);
                assert_eq!(parsed.sandbox_tools(), SandboxTool::ALL);
                assert_eq!(parsed.model_alias(), &alias());
                assert_eq!(parsed.system_prompt(), system_prompt);
                assert_eq!(
                    RuntimeView::read(Some(config.to_json().as_bytes())),
                    (&config).into()
                );
            }
        }
    }

    #[test]
    fn a_tool_two_times_is_refused() {
        let tools = vec![SandboxTool::Read, SandboxTool::Ls, SandboxTool::Read];

        assert_eq!(
            RuntimeConfig::new(Shell::Off, tools, alias(), SystemPrompt::Append),
            Err(RuntimeIssue::DuplicateTool { at: 2 })
        );
    }

    #[test]
    fn the_strict_parse_collects_each_issue() {
        let text = br#"{"shell": false, "sandbox_tools": ["read", "bash", "read", "nope"],
            "model_alias": "*", "system_prompt": "prepend"}"#;
        let errors = RuntimeConfig::parse(text).unwrap_err();

        assert_eq!(
            errors.as_slice(),
            [
                RuntimeIssue::UnknownTool { at: 1 },
                RuntimeIssue::DuplicateTool { at: 2 },
                RuntimeIssue::UnknownTool { at: 3 },
                RuntimeIssue::ModelAlias(ModelAliasError::BadByte),
                RuntimeIssue::UnknownSystemPrompt,
            ]
        );
        assert_eq!(
            errors.to_string(),
            "sandbox_tools[1] is not a sandbox tool; sandbox_tools[2] is in the list two times; \
             sandbox_tools[3] is not a sandbox tool; model_alias: a model alias starts with a to \
             z or 0 to 9 and holds a to z, 0 to 9, ., _, / and -; system_prompt is not append or \
             replace"
        );
    }

    #[test]
    fn the_strict_parse_refuses_a_file_of_another_shape() {
        for text in [
            "",
            "not json",
            "[]",
            "null",
            r#"[false, [], "agent-router"]"#,
            r#"{"shell": false, "sandbox_tools": []}"#,
            r#"{"shell": "no", "sandbox_tools": [], "model_alias": "a"}"#,
            r#"{"shell": false, "sandbox_tools": "read", "model_alias": "a"}"#,
            r#"{"shell": false, "sandbox_tools": [1], "model_alias": "a"}"#,
            r#"{"shell": false, "sandbox_tools": [], "model_alias": null}"#,
            r#"{"shell": false, "sandbox_tools": [], "model_alias": "a", "pi_idle_ttl_s": 5}"#,
            r#"{"shell": false, "sandbox_tools": [], "model_alias": "a", "pep_token": "t"}"#,
        ] {
            assert_eq!(
                RuntimeConfig::parse(text.as_bytes())
                    .unwrap_err()
                    .as_slice(),
                [RuntimeIssue::BadShape],
                "{text}"
            );
        }
    }

    #[test]
    fn the_view_of_a_bad_file_is_the_safe_default() {
        let safe = RuntimeView::safe_default();

        assert_eq!(safe.shell(), Shell::Off);
        assert_eq!(safe.sandbox_tools(), SandboxTool::DEFAULT);
        assert_eq!(safe.model_alias(), None);
        assert_eq!(safe.system_prompt(), SystemPrompt::Append);
        assert_eq!(RuntimeView::read(None), safe);
        for text in [
            "",
            "not json",
            "[]",
            "null",
            "7",
            "\"text\"",
            "{",
            "\u{feff}{}",
        ] {
            assert_eq!(RuntimeView::read(Some(text.as_bytes())), safe, "{text}");
        }

        assert_eq!(RuntimeView::read(Some(b"{}")), safe);
        assert_eq!(RuntimeView::read(Some(b"\xff")), safe);
    }

    #[test]
    fn the_view_takes_the_safe_default_of_each_bad_field() {
        // Each row follows `readRuntimeConfig` of playpen/src/runtime-config.ts.
        let default = SandboxTool::DEFAULT.to_vec();
        for (text, shell, tools, alias, prompt) in [
            (
                r#"{"shell": true}"#,
                Shell::On,
                default.clone(),
                None,
                SystemPrompt::Append,
            ),
            (
                r#"{"shell": 1}"#,
                Shell::Off,
                default.clone(),
                None,
                SystemPrompt::Append,
            ),
            (
                r#"{"shell": "true"}"#,
                Shell::Off,
                default.clone(),
                None,
                SystemPrompt::Append,
            ),
            (
                r#"{"sandbox_tools": []}"#,
                Shell::Off,
                Vec::new(),
                None,
                SystemPrompt::Append,
            ),
            (
                r#"{"sandbox_tools": "read"}"#,
                Shell::Off,
                default.clone(),
                None,
                SystemPrompt::Append,
            ),
            (
                r#"{"sandbox_tools": ["write", "bash", 7, null, "powershell", "codemode", "write"]}"#,
                Shell::Off,
                vec![
                    SandboxTool::Write,
                    SandboxTool::Codemode,
                    SandboxTool::Write,
                ],
                None,
                SystemPrompt::Append,
            ),
            (
                r#"{"model_alias": "agent-router"}"#,
                Shell::Off,
                default.clone(),
                Some("agent-router"),
                SystemPrompt::Append,
            ),
            (
                r#"{"model_alias": ""}"#,
                Shell::Off,
                default.clone(),
                None,
                SystemPrompt::Append,
            ),
            (
                r#"{"model_alias": 7}"#,
                Shell::Off,
                default.clone(),
                None,
                SystemPrompt::Append,
            ),
            (
                r#"{"system_prompt": "replace"}"#,
                Shell::Off,
                default.clone(),
                None,
                SystemPrompt::Replace,
            ),
            (
                r#"{"system_prompt": "REPLACE"}"#,
                Shell::Off,
                default.clone(),
                None,
                SystemPrompt::Append,
            ),
            (
                r#"{"shell": false, "shell": true, "pi_idle_ttl_s": 5, "max_resident_processes": 2}"#,
                Shell::On,
                default.clone(),
                None,
                SystemPrompt::Append,
            ),
        ] {
            let view = RuntimeView::read(Some(text.as_bytes()));

            assert_eq!(view.shell(), shell, "{text}");
            assert_eq!(view.sandbox_tools(), tools, "{text}");
            assert_eq!(view.model_alias().map(ModelAlias::as_str), alias, "{text}");
            assert_eq!(view.system_prompt(), prompt, "{text}");
        }
    }

    #[test]
    fn the_view_drops_a_model_alias_that_is_not_valid() {
        // The TypeScript reader keeps each text that is not empty. The view
        // drops a text that is not a model alias: no model is the default.
        let view = RuntimeView::read(Some(br#"{"model_alias": "litellm/*"}"#));

        assert_eq!(view.model_alias(), None);
    }

    #[test]
    fn credentials_read_back_what_the_writer_gives() {
        let creds = Credentials::new(
            7,
            secret("sk-test"),
            secret("TOKEN"),
            String::from("2030-01-02T03:04:05Z"),
        );
        let text = creds.to_json().unwrap();

        assert_eq!(
            text,
            "{\n  \"epoch\": 7,\n  \"litellm_key\": \"sk-test\",\n  \"pep_token\": \"TOKEN\",\n  \
             \"written_at\": \"2030-01-02T03:04:05Z\",\n  \"previous_pep_token\": null,\n  \
             \"previous_expires_at\": null\n}\n"
        );

        let parsed = Credentials::parse(text.as_bytes()).unwrap();

        assert_eq!(parsed.epoch(), 7);
        assert!(parsed.litellm_key().matches(b"sk-test"));
        assert!(parsed.pep_token().matches(b"TOKEN"));
        assert_eq!(parsed.written_at(), "2030-01-02T03:04:05Z");
        assert!(parsed.previous_pep_token().is_none());
        assert_eq!(parsed.previous_expires_at(), None);
    }

    #[test]
    fn credentials_keep_the_previous_token_for_its_overlap() {
        let creds = Credentials::new(8, secret("k"), secret("new"), String::from("now"))
            .with_previous(Some(secret("old")), Some(String::from("later")));
        let parsed = Credentials::parse(creds.to_json().unwrap().as_bytes()).unwrap();

        assert!(parsed.previous_pep_token().unwrap().matches(b"old"));
        assert_eq!(parsed.previous_expires_at(), Some("later"));
    }

    #[test]
    fn debug_prints_no_secret_of_the_credentials() {
        let creds = Credentials::new(7, secret("sk-test"), secret("TOKEN"), String::new())
            .with_previous(Some(secret("OLDER")), Some(String::new()));
        let text = format!("{creds:?}");

        for word in ["sk-test", "TOKEN", "OLDER"] {
            assert!(!text.contains(word), "{text}");
        }
    }

    #[test]
    fn a_secret_that_is_not_utf8_has_no_json_form() {
        let key = Secret::try_from(vec![0xff]).unwrap();
        let creds = Credentials::new(1, key, secret("t"), String::new());

        assert_eq!(
            creds.to_json(),
            Err(CredentialsError::NotText(CredsField::LitellmKey))
        );
    }

    #[test]
    fn an_epoch_is_what_python_int_reads() {
        for (json, epoch) in [
            ("7", 7),
            ("0", 0),
            ("-3", -3),
            ("true", 1),
            ("false", 0),
            ("7.9", 7),
            ("-7.9", -7),
            ("7.0", 7),
            ("1e2", 100),
            ("\"7\"", 7),
            ("\" 7 \"", 7),
            ("\"1_0\"", 10),
            ("9223372036854775807", i64::MAX),
        ] {
            let text = format!(
                r#"{{"epoch": {json}, "litellm_key": "k", "pep_token": "t", "written_at": "w"}}"#
            );

            assert_eq!(
                Credentials::parse(text.as_bytes()).unwrap().epoch(),
                epoch,
                "{json}"
            );
        }
    }

    #[test]
    fn a_file_that_is_no_creds_file_is_refused() {
        let field = |field, value: &str| {
            let mut body = BTreeMap::from([
                ("epoch", "7"),
                ("litellm_key", "\"k\""),
                ("pep_token", "\"t\""),
                ("written_at", "\"w\""),
            ]);
            if value.is_empty() {
                body.remove(field);
            } else {
                body.insert(field, value);
            }
            let pairs: Vec<String> = body
                .iter()
                .map(|(key, value)| format!("\"{key}\": {value}"))
                .collect();

            format!("{{{}}}", pairs.join(", "))
        };
        for (text, error) in [
            (String::new(), CredentialsError::NotAnObject),
            (String::from("not json"), CredentialsError::NotAnObject),
            (String::from("[]"), CredentialsError::NotAnObject),
            (String::from("null"), CredentialsError::NotAnObject),
            (String::from("\u{feff}{}"), CredentialsError::NotAnObject),
            (
                String::from("{}"),
                CredentialsError::Missing(CredsField::Epoch),
            ),
            (
                field("epoch", ""),
                CredentialsError::Missing(CredsField::Epoch),
            ),
            (
                field("epoch", "null"),
                CredentialsError::BadValue(CredsField::Epoch),
            ),
            (
                field("epoch", "[7]"),
                CredentialsError::BadValue(CredsField::Epoch),
            ),
            (
                field("epoch", "\"seven\""),
                CredentialsError::BadValue(CredsField::Epoch),
            ),
            (
                field("epoch", "\"7.0\""),
                CredentialsError::BadValue(CredsField::Epoch),
            ),
            (
                field("epoch", "1e30"),
                CredentialsError::BadValue(CredsField::Epoch),
            ),
            (
                field("epoch", "9223372036854775808"),
                CredentialsError::BadValue(CredsField::Epoch),
            ),
            (
                field("litellm_key", ""),
                CredentialsError::Missing(CredsField::LitellmKey),
            ),
            (
                field("litellm_key", "null"),
                CredentialsError::BadValue(CredsField::LitellmKey),
            ),
            (
                field("litellm_key", "\"\""),
                CredentialsError::BadValue(CredsField::LitellmKey),
            ),
            (
                field("pep_token", ""),
                CredentialsError::Missing(CredsField::PepToken),
            ),
            (
                field("pep_token", "7"),
                CredentialsError::BadValue(CredsField::PepToken),
            ),
            (
                field("written_at", ""),
                CredentialsError::Missing(CredsField::WrittenAt),
            ),
            (
                field("written_at", "7"),
                CredentialsError::BadValue(CredsField::WrittenAt),
            ),
        ] {
            assert_eq!(
                Credentials::parse(text.as_bytes()).unwrap_err(),
                error,
                "{text}"
            );
        }

        assert_eq!(
            Credentials::parse(b"\xff").unwrap_err(),
            CredentialsError::NotAnObject
        );
        assert_eq!(
            CredentialsError::Missing(CredsField::PepToken).to_string(),
            "creds.json holds no pep_token"
        );
    }

    #[test]
    fn a_previous_field_that_is_not_a_text_reads_as_absent() {
        for value in ["null", "7", "\"\"", "[\"old\"]", "true"] {
            let text = format!(
                r#"{{"epoch": 7, "litellm_key": "k", "pep_token": "t", "written_at": "",
                    "previous_pep_token": {value}, "previous_expires_at": {value}}}"#
            );
            let creds = Credentials::parse(text.as_bytes()).unwrap();

            assert!(creds.previous_pep_token().is_none(), "{value}");
            assert_eq!(creds.previous_expires_at(), None, "{value}");
            assert_eq!(creds.written_at(), "");
        }
    }

    #[test]
    fn the_env_file_holds_the_paths_of_one_sandbox() {
        let root: DirPath = "/srv/agents/state/rework".parse().unwrap();
        let env = PlaypenEnv::for_sandbox(&root, &"chat-s3".parse().unwrap());
        let family = "/srv/agents/state/rework/families/chat";

        assert_eq!(env.cred_dir().as_str(), format!("{family}/creds"));
        assert_eq!(env.config_dir().as_str(), format!("{family}/config"));
        assert_eq!(
            env.control_dir().as_str(),
            format!("{family}/control/chat-s3")
        );
        assert_eq!(env.sandbox().as_str(), "chat-s3");
        assert_eq!(
            env.to_text(),
            format!(
                "AGENT_CRED_DIR={family}/creds\nAGENT_FAMILY_CONFIG_DIR={family}/config\n\
                 AGENT_CONTROL_DIR={family}/control/chat-s3\nAGENT_SANDBOX=chat-s3\n"
            )
        );
    }

    #[test]
    fn the_env_file_reads_back_as_the_same_value() {
        for root in ["/srv/agents/state/rework", "/", "//net/state"] {
            let env =
                PlaypenEnv::for_sandbox(&root.parse().unwrap(), &"dev-team-s12".parse().unwrap());
            let raw = RawPlaypenEnv::parse(&env.to_text());

            assert_eq!(PlaypenEnv::try_from(&raw), Ok(env), "{root}");
        }
    }

    #[test]
    fn the_raw_env_file_is_each_line_with_an_assignment() {
        let raw = RawPlaypenEnv::parse("A=1\nno assignment\n B = 2 \nC=a=b\n=x\nA=3\r\nD=\n");
        let pairs: Vec<(&str, &str)> = raw.iter().collect();

        assert_eq!(
            pairs,
            [
                ("", "x"),
                (" B ", " 2 "),
                ("A", "3"),
                ("C", "a=b"),
                ("D", "")
            ]
        );
        assert_eq!(raw.get("A"), Some("3"));
        assert_eq!(raw.get("B"), None);
    }

    #[test]
    fn a_variable_that_the_env_file_does_not_set_is_an_error() {
        let raw = RawPlaypenEnv::parse(
            "AGENT_CRED_DIR=\nAGENT_FAMILY_CONFIG_DIR=config\nAGENT_SANDBOX=chat\n",
        );
        let errors = PlaypenEnv::try_from(&raw).unwrap_err();

        assert_eq!(
            errors.as_slice(),
            [
                PlaypenEnvError::Unset {
                    variable: CRED_DIR_VAR
                },
                PlaypenEnvError::Path {
                    variable: CONFIG_DIR_VAR,
                    error: PathError::NotAbsolute
                },
                PlaypenEnvError::Unset {
                    variable: CONTROL_DIR_VAR
                },
                PlaypenEnvError::Sandbox(SandboxNameError::NoNumber),
            ]
        );
        assert_eq!(
            errors.to_string(),
            "AGENT_CRED_DIR: no value; AGENT_FAMILY_CONFIG_DIR: a path starts with /; \
             AGENT_CONTROL_DIR: no value; AGENT_SANDBOX: a sandbox name ends with -s and a number"
        );
        assert_eq!(
            PlaypenEnv::try_from(&RawPlaypenEnv::parse(""))
                .unwrap_err()
                .as_slice()
                .len(),
            4
        );
    }

    #[test]
    fn the_modes_of_the_two_files_are_the_modes_of_the_contract() {
        assert_eq!(CREDS_FILE_MODE, 0o600);
        assert_eq!(PLAYPEN_ENV_MODE, 0o640);
        assert_eq!(RUNTIME_FILE, "runtime.json");
        assert_eq!(CREDS_FILE, "creds.json");
    }
}
