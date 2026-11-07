//! The read of a family's `config/`, its VM shape file and its family
//! state (decisions CH5b, CH23 (3), CH36 G6, CH43 rule 3).
//!
//! caregiver is the only writer of `config/` (decision CH5b). It builds a
//! whole revision in `config.tmp`, renames `config` to `config.old`,
//! renames `config.tmp` to `config`, then removes `config.old`
//! (`atomic.py` `atomic_replace_dir`). So a reader can find no `config/`
//! for a short window, or find files gone from a handle it already holds.
//! Each read here goes through one directory handle:
//!
//! 1. Open the family dir and `openat` its `config/` with `O_NOFOLLOW`.
//! 2. Read through the `config/` handle.
//! 3. Compare the device and inode of the handle with
//!    `fstatat(family_dir, "config")`. A mismatch is a swap, so the read
//!    starts again.
//! 4. An absent `config/` is the rename window, so the read starts again.
//!    After `READ_TRIES` tries it is `FamilyError::Unknown`, which create
//!    answers with `unknown_family` (decision CH33 rule 3).
//!
//! `FamilyRoot` has no default path. Lifecycle passes it from the vmm
//! config (decision CH36 G5). caregiver writes `shape.json` only from the
//! caregiver PR of the cut (decision CH45 rule 2), so on a live host before
//! the cut each shape read gives `NotValid(Absent)`. A proof uses a scratch
//! family root with a hand-written `shape.json` (decision CH47 rule 2).
//!
//! `read_state` reads the family state of one pull on host port 1100
//! (decisions CH5a, CH23): `creds/creds.json`, then `config/` through the
//! one handle above. The two are two reads with no cross-check, so a
//! caregiver write between them gives a mixed reply that the next pull
//! fixes (decision CH62 rule 21). caregiver replaces `creds.json` with one
//! `rename`, so that read needs no retry.
//!
//! `read_egress` reads the egress allowlist `egress.json` of a family
//! (decisions CH6-4, CH69 rules 3 and 5). The file sits in the family dir
//! beside `config/`, and caregiver replaces it with one `rename`, so the
//! read needs no retry. An absent family dir or file is the empty list:
//! no egress, which fails closed.
//!
//! Decision CH50 rule 9 sets the number of tries, the pause between two
//! tries and the size cap of `shape.json`: `READ_TRIES` (5),
//! `RETRY_PAUSE` (10 ms) and `SHAPE_CAP` (4 KiB). A read blocks its thread
//! in `thread::sleep`, so a caller in tokio runs it in `spawn_blocking`.

use std::fmt;
use std::fs::File;
use std::io::{self, Read};
use std::os::fd::{AsFd, BorrowedFd, OwnedFd};
use std::path::{Component, Path, PathBuf};
use std::thread;
use std::time::{Duration, SystemTime};

use creche_vmm_store::layout::RootError;
use creche_wire::{
    FAMILY_EGRESS_FILE, FAMILY_SHAPE_FILE, FAMILY_STATE_CAP, FamilyConfig, FamilyCreds,
    FamilyEgress, FamilyName, FamilyShape, FamilySkills, FamilyState, FamilyStateReply, LitellmKey,
    ModelAlias, PepToken, SandboxGrant, SystemPrompt, strict,
};
use rustix::fs::{AtFlags, Dir, FileType, Mode, OFlags, Stat};
use rustix::io::Errno;
use rustix::path::Arg;
use serde::de::value::{Error as ValueError, MapDeserializer};
use serde::de::{DeserializeOwned, IgnoredAny};
use serde::{Deserialize, Deserializer};

use crate::rfc3339;

/// The name of the config dir in each family dir (contract 01 §6.1).
pub const CONFIG_DIR: &str = "config";

/// The creds dir in each family dir and the one file in it (contract 03
/// §12, `paths.py` `creds_path`), then the files and dirs of `config/`
/// that the family state reads (`config_mount.py`).
const CREDS_DIR: &str = "creds";
const CREDS_FILE: &str = "creds.json";
const RUNTIME_FILE: &str = "runtime.json";
const INSTRUCTIONS_FILE: &str = "instructions.md";
const SKILLS_DIR: &str = "skills";
const SKILL_FILE: &str = "SKILL.md";

/// The most reads of one request before it gives up (decision CH50
/// rule 9).
pub const READ_TRIES: u32 = 5;

/// The pause before each try after the first (decision CH50 rule 9).
pub const RETRY_PAUSE: Duration = Duration::from_millis(10);

/// The most bytes of `shape.json`. The file holds three short fields
/// (decision CH50 rule 9).
pub const SHAPE_CAP: u64 = 4096;

/// The most bytes of `egress.json`.
// CONTRACT-QUESTION: no row sets this cap. It is the 64 KiB of plan PR
// E1: room for about a thousand entries, far past any family file today.
// A larger file is `EgressFault::TooLarge`, so the vmm allows nothing.
pub const EGRESS_CAP: u64 = 64 * 1024;

/// The flags of each directory open. `O_NOFOLLOW` refuses a symlink as the
/// last part of the path (decision CH23 (3)).
const DIR_FLAGS: OFlags = OFlags::RDONLY
    .union(OFlags::DIRECTORY)
    .union(OFlags::NOFOLLOW)
    .union(OFlags::CLOEXEC);

/// Why `shape.json` is not a family VM shape.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ShapeFault {
    /// `config/` has no `shape.json`.
    Absent,
    /// `shape.json` is a symlink or not a regular file.
    NotFile,
    /// `shape.json` has more than `SHAPE_CAP` bytes.
    TooLarge,
    /// The bytes are not one `FamilyShape`.
    NotShape,
}

/// Why a read of a family's `config/` failed.
#[derive(Debug)]
pub enum FamilyError {
    /// The family has no dir or no `config/` dir (decision CH33 rule 3).
    Unknown,
    /// The family dir or its `config/` is a symlink or not a dir.
    NotDir,
    /// `config/` holds no valid `shape.json`.
    NotValid(ShapeFault),
    /// caregiver swapped `config/` during each of the `READ_TRIES` reads.
    Swapped,
    /// Any other error of the file system.
    Io(io::Error),
}

impl From<Errno> for FamilyError {
    fn from(errno: Errno) -> Self {
        Self::Io(errno.into())
    }
}

/// Why `egress.json` of a family does not read. Each fault allows no
/// egress.
#[derive(Debug)]
pub enum EgressFault {
    /// The family dir is a symlink or not a dir.
    NotDir,
    /// `egress.json` is a symlink or not a regular file.
    NotFile,
    /// `egress.json` has more than `EGRESS_CAP` bytes.
    TooLarge,
    /// The bytes are not one `FamilyEgress` of strict JSON.
    NotValid,
    /// Any other error of the file system.
    Io(io::Error),
}

/// Why a pull gets no `state` reply (decisions CH23 rule 2, CH31 rules 3
/// and 4). Each variant is one typed error reply on host port 1100.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum StateFault {
    /// `creds.json` is absent: no family dir, no `creds/` or no file. It
    /// wins over every fault of `config/`, so it wins when both are
    /// absent (decision CH31 rule 4).
    CredsMissing,
    /// `config/` is absent through every try.
    ConfigMissing,
    /// A file is present and does not read as its part of the state, or
    /// `config/` is a symlink, a file, swapped during every try, or an
    /// error of the file system.
    NotValid,
    /// The `state` reply is over `FAMILY_STATE_CAP` (decision CH31
    /// rule 3).
    TooLarge,
}

/// The family state of one pull: the text of its `state` reply and the
/// `model_live_at` of `config/runtime.json`.
///
/// The one way to make it is `FamilyRoot::read_state`, so its line is
/// always one `FamilyStateReply::State` of at most `FAMILY_STATE_CAP`
/// bytes, with no LF (decision CH23 rule 4). Its `Debug` writes no byte of
/// the line, which holds the key and the token (invariant 13).
pub struct StateRead {
    /// The JSON text of the reply.
    line: Vec<u8>,
    /// When the model of the reply is live, if caregiver set a time.
    model_live_at: Option<SystemTime>,
}

impl StateRead {
    /// The JSON text of the `state` reply, with no LF.
    #[must_use]
    pub fn line(&self) -> &[u8] {
        &self.line
    }

    /// When the model alias of the reply is live everywhere (decision
    /// CH23-1). The vmm holds the reply until the host time passes it, for
    /// at most 10 s (decision CH31 rule 1). caregiver leaves it out until
    /// a model change sets it (decision CH45 rule 1), so `None` holds
    /// nothing.
    #[must_use]
    pub fn model_live_at(&self) -> Option<SystemTime> {
        self.model_live_at
    }
}

impl fmt::Debug for StateRead {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("StateRead")
            .field("line", &format_args!("{} bytes", self.line.len()))
            .field("model_live_at", &self.model_live_at)
            .finish()
    }
}

/// `creds.json` as caregiver writes it (`credentials.py` `as_json`). The
/// reply takes the key and the token. The other four fields stay on the
/// host (decision CH23 rule 1): `IgnoredAny` reads past each value and
/// keeps none, so the old token never reaches the reply. A key that is
/// none of the six refuses the file. caregiver reads a file with no
/// `previous_*` fields, so each of the four may be absent.
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct CredsFile {
    litellm_key: LitellmKey,
    pep_token: PepToken,
    #[serde(default, rename = "epoch")]
    _epoch: IgnoredAny,
    #[serde(default, rename = "written_at")]
    _written_at: IgnoredAny,
    #[serde(default, rename = "previous_pep_token")]
    _previous_pep_token: IgnoredAny,
    #[serde(default, rename = "previous_expires_at")]
    _previous_expires_at: IgnoredAny,
}

/// `config/runtime.json` as caregiver writes it (`config_mount.py`
/// `RuntimeConfig.as_json`). `system_prompt` is absent for `append`, and
/// `model_live_at` until a model change sets it (decision CH45 rule 1).
/// `shell` must be a bool, and the reply does not carry it (`FamilyConfig`).
// Decision CH67 rule 6: this reader refuses an unknown key and a missing
// `shell` or `sandbox_tools`, as it refuses an unknown key of
// `creds.json`. The facade's file backend ignores or defaults each.
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct RuntimeFile {
    #[serde(rename = "shell")]
    _shell: bool,
    sandbox_tools: Vec<SandboxTool>,
    model_alias: ModelAlias,
    #[serde(default)]
    system_prompt: Option<SystemPrompt>,
    #[serde(default, deserialize_with = "live_at")]
    model_live_at: Option<SystemTime>,
}

/// A name of `sandbox_tools` in `runtime.json`: `SandboxTool` of
/// `family/grammar.py`, one key of `SandboxGrant` each.
#[derive(Deserialize, Clone, Copy, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
enum SandboxTool {
    Bash,
    Edit,
    Glob,
    Grep,
    Read,
    Write,
}

/// `model_live_at` in the form of `rfc3339::format`. A `null` or any other
/// text refuses the file.
fn live_at<'de, D: Deserializer<'de>>(deserializer: D) -> Result<Option<SystemTime>, D::Error> {
    let text = String::deserialize(deserializer)?;
    rfc3339::parse(&text)
        .map(Some)
        .ok_or_else(|| serde::de::Error::custom("model_live_at is not YYYY-MM-DDTHH:MM:SSZ"))
}

/// The grant of a tool list. The validator refuses a tool listed twice
/// (`SandboxGrant`), so a list with one is not a grant.
fn grant(tools: &[SandboxTool]) -> Option<SandboxGrant> {
    let mut grant = SandboxGrant {
        bash: false,
        edit: false,
        glob: false,
        grep: false,
        read: false,
        write: false,
    };
    for (index, tool) in tools.iter().enumerate() {
        if tools.iter().skip(index + 1).any(|other| other == tool) {
            return None;
        }
        let granted = match tool {
            SandboxTool::Bash => &mut grant.bash,
            SandboxTool::Edit => &mut grant.edit,
            SandboxTool::Glob => &mut grant.glob,
            SandboxTool::Grep => &mut grant.grep,
            SandboxTool::Read => &mut grant.read,
            SandboxTool::Write => &mut grant.write,
        };
        *granted = true;
    }
    Some(grant)
}

/// `bytes` read as `T` through the strict check of the Rust readers
/// (decision CH65 rule 2), so a key given twice refuses the file.
fn strict_json<T: DeserializeOwned>(bytes: &[u8]) -> Option<T> {
    strict(bytes)
        .then(|| serde_json::from_slice(bytes).ok())
        .flatten()
}

/// caregiver's families dir: one dir per family, each with its `config/`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FamilyRoot(PathBuf);

impl FamilyRoot {
    /// Checks `path` and makes the family root.
    ///
    /// # Errors
    ///
    /// `NotAbsolute` or `ParentComponent` when the path breaks that rule.
    pub fn new(path: PathBuf) -> Result<Self, RootError> {
        if !path.is_absolute() {
            return Err(RootError::NotAbsolute);
        }
        if path.components().any(|part| part == Component::ParentDir) {
            return Err(RootError::ParentComponent);
        }
        Ok(Self(path))
    }

    /// The root itself.
    #[must_use]
    pub fn path(&self) -> &Path {
        &self.0
    }

    /// Whether `family` has a `config/` dir. Create calls it (decision
    /// CH33 rule 3).
    ///
    /// # Errors
    ///
    /// `Unknown` when it has none, else the error of `with_config`.
    pub fn exists(&self, family: &FamilyName) -> Result<(), FamilyError> {
        self.with_config(family, |_| Ok(()))
    }

    /// The VM shape of `family`, from `config/shape.json` (decisions CH36
    /// G6, CH43 rule 3, CH8-5).
    ///
    /// # Errors
    ///
    /// `NotValid` when the file is absent or bad, else the error of
    /// `with_config`.
    pub fn read_shape(&self, family: &FamilyName) -> Result<FamilyShape, FamilyError> {
        let bytes = self.with_config(family, read_shape_file)?;
        serde_json::from_slice(&bytes).map_err(|_| FamilyError::NotValid(ShapeFault::NotShape))
    }

    /// The egress allowlist of `family`, from `egress.json` in its family
    /// dir (decisions CH6-4, CH69 rules 3 and 5). One `openat` of the file
    /// with `O_NOFOLLOW` and `O_NONBLOCK`, a cap of `EGRESS_CAP` and the
    /// strict reader (decision CH65 rule 2). An absent family dir or file
    /// is the empty list.
    ///
    /// # Errors
    ///
    /// An `EgressFault` when the family dir or the file is present and
    /// does not read.
    pub fn read_egress(&self, family: &FamilyName) -> Result<FamilyEgress, EgressFault> {
        // `ELOOP` is the refusal of `O_NOFOLLOW`, `ENOTDIR` the refusal of
        // `O_DIRECTORY`, as in `open_dir`.
        let path = self.0.join(family.as_str());
        let family_dir = match rustix::fs::open(path, DIR_FLAGS, Mode::empty()) {
            Ok(dir) => dir,
            Err(Errno::NOENT) => return Ok(FamilyEgress { hosts: Vec::new() }),
            Err(Errno::LOOP | Errno::NOTDIR) => return Err(EgressFault::NotDir),
            Err(errno) => return Err(EgressFault::Io(errno.into())),
        };
        match read_file(family_dir.as_fd(), FAMILY_EGRESS_FILE, EGRESS_CAP) {
            Ok(FileRead::Bytes(bytes)) => strict_json(&bytes).ok_or(EgressFault::NotValid),
            Ok(FileRead::Absent) => Ok(FamilyEgress { hosts: Vec::new() }),
            Ok(FileRead::NotFile) => Err(EgressFault::NotFile),
            Ok(FileRead::OverCap) => Err(EgressFault::TooLarge),
            Err(error) => Err(EgressFault::Io(error)),
        }
    }

    /// The family state of `family` for one pull on host port 1100
    /// (decisions CH5a, CH23): `creds/creds.json`, then `config/` through
    /// `with_config`, as two reads with no cross-check (decision CH62
    /// rule 21). It keeps no last good state, so each fault is the reply
    /// of that pull (decision CH53 rule 2).
    ///
    /// # Errors
    ///
    /// `CredsMissing` first, whatever `config/` holds (decision CH31
    /// rule 4). Then `ConfigMissing`, `NotValid`, or `TooLarge` when the
    /// reply passes `FAMILY_STATE_CAP`.
    pub fn read_state(&self, family: &FamilyName) -> Result<StateRead, StateFault> {
        let creds = self.read_creds(family)?;
        let (config, model_live_at) = match self.with_config(family, read_config) {
            Ok(read) => read?,
            Err(FamilyError::Unknown) => return Err(StateFault::ConfigMissing),
            Err(
                FamilyError::NotDir
                | FamilyError::NotValid(_)
                | FamilyError::Swapped
                | FamilyError::Io(_),
            ) => return Err(StateFault::NotValid),
        };
        // serde_json fails only on a map key that is not a string, or on
        // a writer error, and a Vec has none.
        let line = serde_json::to_vec(&FamilyStateReply::State(FamilyState { config, creds }))
            .map_err(|_| StateFault::NotValid)?;
        if line.len() > FAMILY_STATE_CAP {
            return Err(StateFault::TooLarge);
        }
        Ok(StateRead {
            line,
            model_live_at,
        })
    }

    /// The key and the token of `creds/creds.json` of `family`, one capped
    /// `O_NOFOLLOW` read (decision CH62 rule 21).
    fn read_creds(&self, family: &FamilyName) -> Result<FamilyCreds, StateFault> {
        let opened = |dir: Result<OwnedFd, FamilyError>| match dir {
            Ok(dir) => Ok(dir),
            Err(FamilyError::Unknown) => Err(StateFault::CredsMissing),
            Err(_) => Err(StateFault::NotValid),
        };
        let path = self.0.join(family.as_str());
        let family_dir = opened(open_dir(rustix::fs::open(path, DIR_FLAGS, Mode::empty())))?;
        let creds_dir = opened(open_dir(rustix::fs::openat(
            &family_dir,
            CREDS_DIR,
            DIR_FLAGS,
            Mode::empty(),
        )))?;
        // Decision CH67 rule 5: creds.json and runtime.json each take
        // FAMILY_STATE_CAP, and a larger file is not_valid, not
        // too_large, because its excess may sit in fields that the reply
        // drops.
        let read = read_file(creds_dir.as_fd(), CREDS_FILE, cap_u64(FAMILY_STATE_CAP));
        let file: CredsFile = match read {
            Ok(FileRead::Bytes(bytes)) => strict_json(&bytes),
            Ok(FileRead::Absent) => return Err(StateFault::CredsMissing),
            Ok(FileRead::NotFile | FileRead::OverCap) | Err(_) => None,
        }
        .ok_or(StateFault::NotValid)?;
        Ok(FamilyCreds {
            litellm_key: file.litellm_key,
            pep_token: file.pep_token,
        })
    }

    /// Runs `read` on one handle of the `config/` of `family`, and returns
    /// its result only when `config/` is still the dir of that handle
    /// afterwards. A swap or an absent `config/` starts the read again, at
    /// most `READ_TRIES` times (decisions CH5b, CH23 (3)).
    ///
    /// # Errors
    ///
    /// `Unknown` when the family dir or its `config/` is absent, `NotDir`
    /// for a symlink or a file, `Swapped` when every try saw a swap, `Io`
    /// for any other error, else the error of `read`.
    pub fn with_config<T>(
        &self,
        family: &FamilyName,
        mut read: impl FnMut(BorrowedFd<'_>) -> Result<T, FamilyError>,
    ) -> Result<T, FamilyError> {
        let dir = open_dir(rustix::fs::open(
            self.0.join(family.as_str()),
            DIR_FLAGS,
            Mode::empty(),
        ))?;
        let mut last = FamilyError::Unknown;
        for attempt in 0..READ_TRIES {
            if attempt > 0 {
                thread::sleep(RETRY_PAUSE);
            }
            let config = match open_dir(rustix::fs::openat(
                &dir,
                CONFIG_DIR,
                DIR_FLAGS,
                Mode::empty(),
            )) {
                Err(FamilyError::Unknown) => {
                    last = FamilyError::Unknown;
                    continue;
                }
                other => other?,
            };
            let held = rustix::fs::fstat(&config)?;
            let result = read(config.as_fd());
            match rustix::fs::statat(&dir, CONFIG_DIR, AtFlags::SYMLINK_NOFOLLOW) {
                Ok(now) if same_file(&held, &now) => return result,
                Ok(_) | Err(Errno::NOENT) => last = FamilyError::Swapped,
                Err(errno) => return Err(errno.into()),
            }
        }
        Err(last)
    }
}

/// A byte count as the `u64` cap of `read_file`.
fn cap_u64(cap: usize) -> u64 {
    u64::try_from(cap).unwrap_or(u64::MAX)
}

/// The `FamilyConfig` and `model_live_at` of the dir `config`. The texts
/// of `instructions.md` and the skills share one budget of
/// `FAMILY_STATE_CAP` bytes, because the reply holds each of their bytes.
/// So a read past it is `TooLarge`, and no read holds more than the cap.
/// The outer `Result` is the one of `with_config`, which retries on a swap
/// whatever the inner one holds.
fn read_config(
    config: BorrowedFd<'_>,
) -> Result<Result<(FamilyConfig, Option<SystemTime>), StateFault>, FamilyError> {
    let not_valid = || Ok(Err(StateFault::NotValid));
    let runtime: RuntimeFile = match read_file(config, RUNTIME_FILE, cap_u64(FAMILY_STATE_CAP)) {
        Ok(FileRead::Bytes(bytes)) => match strict_json(&bytes) {
            Some(runtime) => runtime,
            None => return not_valid(),
        },
        Ok(_) | Err(_) => return not_valid(),
    };
    let Some(sandbox_tools) = grant(&runtime.sandbox_tools) else {
        return not_valid();
    };
    let mut budget = FAMILY_STATE_CAP;
    // caregiver always writes instructions.md. An absent one gives no
    // instructions, as the facade's file reader reads it.
    let instructions = match read_text(config, INSTRUCTIONS_FILE, &mut budget) {
        Ok(Some(text)) => text,
        Ok(None) => String::new(),
        Err(fault) => return Ok(Err(fault.into())),
    };
    let skills = match read_skills(config, &mut budget) {
        Ok(skills) => skills,
        Err(fault) => return Ok(Err(fault.into())),
    };
    let family = FamilyConfig {
        instructions,
        model_alias: runtime.model_alias,
        sandbox_tools,
        skills,
        system_prompt: runtime.system_prompt.unwrap_or(SystemPrompt::Append),
    };
    Ok(Ok((family, runtime.model_live_at)))
}

/// Why a text of `config/` did not read.
enum TextFault {
    /// The text is not there as caregiver writes it.
    NotValid,
    /// The text passes the budget.
    OverBudget,
}

impl From<TextFault> for StateFault {
    fn from(fault: TextFault) -> Self {
        match fault {
            TextFault::NotValid => Self::NotValid,
            TextFault::OverBudget => Self::TooLarge,
        }
    }
}

/// The UTF-8 text of the file `name` of `dir`, or `None` when it is
/// absent. Its bytes come off `budget`.
fn read_text(
    dir: BorrowedFd<'_>,
    name: impl Arg,
    budget: &mut usize,
) -> Result<Option<String>, TextFault> {
    match read_file(dir, name, cap_u64(*budget)) {
        Ok(FileRead::Bytes(bytes)) => {
            *budget = budget.saturating_sub(bytes.len());
            String::from_utf8(bytes)
                .map(Some)
                .map_err(|_| TextFault::NotValid)
        }
        Ok(FileRead::Absent) => Ok(None),
        Ok(FileRead::OverCap) => Err(TextFault::OverBudget),
        Ok(FileRead::NotFile) | Err(_) => Err(TextFault::NotValid),
    }
}

/// The skills of `config/skills/`: each `<name>/SKILL.md`, in ascending
/// name order (`FamilySkills`). An absent `skills/` grants none. caregiver
/// writes one dir per skill and one file in it (`config_mount.py`), so any
/// other entry, a symlink, a name that is not a `SkillName` or a dir with
/// no `SKILL.md` refuses the config. Each name and text comes off
/// `budget`, because the reply holds both.
fn read_skills(config: BorrowedFd<'_>, budget: &mut usize) -> Result<FamilySkills, TextFault> {
    let skills = match open_dir(rustix::fs::openat(
        config,
        SKILLS_DIR,
        DIR_FLAGS,
        Mode::empty(),
    )) {
        Ok(skills) => Some(skills),
        Err(FamilyError::Unknown) => None,
        Err(_) => return Err(TextFault::NotValid),
    };
    let mut texts: Vec<(String, String)> = Vec::new();
    if let Some(skills) = skills {
        for entry in Dir::read_from(&skills).map_err(|_| TextFault::NotValid)? {
            let entry = entry.map_err(|_| TextFault::NotValid)?;
            let name = entry.file_name();
            if matches!(name.to_bytes(), b"." | b"..") {
                continue;
            }
            let name = name.to_str().map_err(|_| TextFault::NotValid)?.to_owned();
            *budget = budget
                .checked_sub(name.len())
                .ok_or(TextFault::OverBudget)?;
            let skill = open_dir(rustix::fs::openat(
                &skills,
                name.as_str(),
                DIR_FLAGS,
                Mode::empty(),
            ))
            .map_err(|_| TextFault::NotValid)?;
            let text = read_text(skill.as_fd(), SKILL_FILE, budget)?.ok_or(TextFault::NotValid)?;
            texts.push((name, text));
        }
    }
    texts.sort_by(|one, other| one.0.cmp(&other.0));
    // The wire type checks each name and refuses a name given twice.
    FamilySkills::deserialize(MapDeserializer::<_, ValueError>::new(texts.into_iter()))
        .map_err(|_| TextFault::NotValid)
}

/// The dir of an open, or why it is not one. `ELOOP` is the refusal of
/// `O_NOFOLLOW` and `ENOTDIR` the refusal of `O_DIRECTORY`.
fn open_dir(opened: Result<OwnedFd, Errno>) -> Result<OwnedFd, FamilyError> {
    match opened {
        Ok(dir) => Ok(dir),
        Err(Errno::NOENT) => Err(FamilyError::Unknown),
        Err(Errno::LOOP | Errno::NOTDIR) => Err(FamilyError::NotDir),
        Err(errno) => Err(errno.into()),
    }
}

/// Whether two stats name one file: the same device and inode.
fn same_file(one: &Stat, other: &Stat) -> bool {
    one.st_dev == other.st_dev && one.st_ino == other.st_ino
}

/// The bytes of `shape.json` in the dir `config`, at most `SHAPE_CAP`.
fn read_shape_file(config: BorrowedFd<'_>) -> Result<Vec<u8>, FamilyError> {
    match read_file(config, FAMILY_SHAPE_FILE, SHAPE_CAP).map_err(FamilyError::Io)? {
        FileRead::Bytes(bytes) => Ok(bytes),
        FileRead::Absent => Err(FamilyError::NotValid(ShapeFault::Absent)),
        FileRead::NotFile => Err(FamilyError::NotValid(ShapeFault::NotFile)),
        FileRead::OverCap => Err(FamilyError::NotValid(ShapeFault::TooLarge)),
    }
}

/// What a capped read of one file of a dir found.
enum FileRead {
    /// The file is a regular file of at most the cap.
    Bytes(Vec<u8>),
    /// The dir has no entry of that name.
    Absent,
    /// The entry is a symlink or not a regular file.
    NotFile,
    /// The file has more bytes than the cap.
    OverCap,
}

/// Reads the file `name` of the dir `dir`, at most `cap` bytes. The open
/// takes `O_NOFOLLOW`, and `O_NONBLOCK` so a FIFO cannot hold the read.
fn read_file(dir: BorrowedFd<'_>, name: impl Arg, cap: u64) -> io::Result<FileRead> {
    let flags = OFlags::RDONLY | OFlags::NOFOLLOW | OFlags::CLOEXEC | OFlags::NONBLOCK;
    let fd = match rustix::fs::openat(dir, name, flags, Mode::empty()) {
        Ok(fd) => fd,
        Err(Errno::NOENT) => return Ok(FileRead::Absent),
        Err(Errno::LOOP) => return Ok(FileRead::NotFile),
        Err(errno) => return Err(errno.into()),
    };
    let stat = rustix::fs::fstat(&fd)?;
    if FileType::from_raw_mode(stat.st_mode) != FileType::RegularFile {
        return Ok(FileRead::NotFile);
    }
    let mut bytes = Vec::new();
    File::from(fd)
        .take(cap.saturating_add(1))
        .read_to_end(&mut bytes)?;
    if u64::try_from(bytes.len()).map_or(true, |len| len > cap) {
        return Ok(FileRead::OverCap);
    }
    Ok(FileRead::Bytes(bytes))
}
