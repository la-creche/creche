//! The config of the vmm (plan PR L20). `Config::read` parses the env of
//! the process once, at the start of `vmm serve`. The owner env rule makes
//! each path and each host value a key with a default (decision CH36 G5).
//! `env/src/creche_env/default.env` holds each default, and the unit gives
//! the vmm the keys. So the vmm compiles in no default: a missing or a bad
//! key refuses the start, and `src/main.rs` exits 78 (owner item 86).
//!
//! The run root is `RUNTIME_DIRECTORY`, which systemd sets from
//! `RuntimeDirectory=` (gap G5 option A). It is no catalog key.

use std::ffi::OsString;
use std::fmt;
use std::path::{Component, PathBuf};

use creche_vmm_confine::admission::{GIB, Limits};
use creche_vmm_confine::family::FamilyRoot;
use creche_vmm_confine::launch::Program;
use creche_vmm_store::layout::{RootError, RunRoot, StateRoot};

/// The rows, the session disks, the snapshots and the digest store
/// (decision CH39 rule 3). Its default is the `StateDirectory=` of the
/// user unit, `~/.local/state/creche-vmm` (decision CH50 rule 11).
pub const STATE_ROOT: &str = "VMM_STATE_ROOT";
/// The runtime dir of the unit: `vmm.sock` and the VM run dirs.
pub const RUN_ROOT: &str = "RUNTIME_DIRECTORY";
/// caregiver's families dir, for `config/` of each family (decision CH36
/// G6).
pub const FAMILY_ROOT: &str = "VMM_FAMILY_ROOT";
/// The faults dir of the creche state root (contract 05 §3.3.1).
pub const FAULTS_ROOT: &str = "VMM_FAULTS_ROOT";
/// The shipped `cloud-hypervisor` (decision CH7-5).
pub const CH: &str = "VMM_CH_PROGRAM";
/// The shipped `ch-remote` (decision CH7-5).
pub const CH_REMOTE: &str = "VMM_CH_REMOTE_PROGRAM";
/// `systemd-run` of the launch (PR C2). The key exists already.
pub const SYSTEMD_RUN: &str = "AGENT_SYSTEMD_RUN_PROGRAM";
/// `systemctl` of the leftover sweep (PR C3). The key exists already.
pub const SYSTEMCTL: &str = "AGENT_SYSTEMCTL_PROGRAM";
/// The host `zstd` of the store import (decision CH35 rule 4, gap G9).
pub const ZSTD: &str = "VMM_ZSTD_PROGRAM";
/// The host `mke2fs` of a new session disk (gap G7).
pub const MKE2FS: &str = "VMM_MKE2FS_PROGRAM";
/// The host `fallocate` of a snapshot (decision CH29).
pub const FALLOCATE: &str = "VMM_FALLOCATE_PROGRAM";
/// The admission cap on guest RAM, in GiB (decision CH8-6, gap G14).
pub const RAM_CAP: &str = "VMM_RAM_CAP_GIB";
/// The free-space floor of the state root, in GiB (CH8-7, gap G14).
pub const FREE_FLOOR: &str = "VMM_FREE_FLOOR_GIB";
/// The snapshot budget, in GiB (decision CH8-7, gap G14).
pub const BUDGET: &str = "VMM_SNAPSHOT_BUDGET_GIB";

/// What the vmm reads from its env.
///
/// CONTRACT-QUESTION: no row names the image source (the dir of the
/// `.zst` files that root installs, decision CH35 rule 4) or the source of
/// the digests of `start::Digests`. A digest changes with each release, so
/// it is no catalog default (`env/AGENTS.md` rule 5). The config reads
/// neither until a row decides. The compose step of plan PR L20 needs both.
#[derive(Debug)]
pub struct Config {
    /// `VMM_STATE_ROOT`.
    pub state: StateRoot,
    /// `RUNTIME_DIRECTORY`.
    pub run: RunRoot,
    /// `VMM_FAMILY_ROOT`.
    pub families: FamilyRoot,
    /// `VMM_FAULTS_ROOT`. `faults::FaultDir::open` makes its `vmm` dir.
    pub faults_root: PathBuf,
    /// `VMM_CH_PROGRAM`.
    pub ch: Program,
    /// `VMM_CH_REMOTE_PROGRAM`.
    pub ch_remote: Program,
    /// `AGENT_SYSTEMD_RUN_PROGRAM`.
    pub systemd_run: Program,
    /// `AGENT_SYSTEMCTL_PROGRAM`.
    pub systemctl: Program,
    /// `VMM_ZSTD_PROGRAM`.
    pub zstd: Program,
    /// `VMM_MKE2FS_PROGRAM`.
    pub mke2fs: Program,
    /// `VMM_FALLOCATE_PROGRAM`.
    pub fallocate: Program,
    /// `VMM_RAM_CAP_GIB` and `VMM_FREE_FLOOR_GIB`, in bytes.
    pub limits: Limits,
    /// `VMM_SNAPSHOT_BUDGET_GIB`, in bytes.
    pub budget: u64,
}

/// What is wrong with one key. A fault never holds the value, as in
/// `env/AGENTS.md`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Fault {
    /// The env has no value, or an empty one.
    Missing,
    /// A path is not absolute.
    NotAbsolute,
    /// A path has a `..` component.
    ParentComponent,
    /// A socket path under the run root has this many bytes, more than
    /// `SOCKET_PATH_MAX`.
    SocketPathTooLong(usize),
    /// A size is not a whole number of GiB in decimal digits.
    NotGib,
    /// A size that must hold something is 0.
    Zero,
    /// A size has more bytes than a `u64` holds.
    TooLarge,
}

/// The first key that refused the start.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ConfigError {
    /// The key.
    pub key: &'static str,
    /// What is wrong with it.
    pub fault: Fault,
}

impl fmt::Display for ConfigError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        let fault = match self.fault {
            Fault::Missing => "has no value",
            Fault::NotAbsolute => "is not an absolute path",
            Fault::ParentComponent => "has a `..` component",
            Fault::SocketPathTooLong(bytes) => {
                return write!(
                    formatter,
                    "{}: a socket path under it has {bytes} bytes, too many",
                    self.key
                );
            }
            Fault::NotGib => "is not a whole number of GiB",
            Fault::Zero => "is 0",
            Fault::TooLarge => "is too large",
        };
        write!(formatter, "{}: the key {fault}", self.key)
    }
}

impl std::error::Error for ConfigError {}

impl Config {
    /// Reads the env of the process.
    ///
    /// # Errors
    ///
    /// The first key that is missing or bad, in the order of `Config`.
    pub fn read() -> Result<Self, ConfigError> {
        Self::from_lookup(|key| std::env::var_os(key))
    }

    /// Reads each key through `lookup`. A test passes a map.
    ///
    /// # Errors
    ///
    /// The first key that is missing or bad, in the order of `Config`.
    pub fn from_lookup(lookup: impl Fn(&str) -> Option<OsString>) -> Result<Self, ConfigError> {
        let path_of = |key| path(&lookup, key);
        let program_of =
            |key| Program::new(path_of(key)?).map_err(|_| error(key, Fault::NotAbsolute));
        let gib_of = |key, zero| gib(&lookup, key, zero);
        Ok(Self {
            state: StateRoot::new(path_of(STATE_ROOT)?).map_err(|e| root(STATE_ROOT, e))?,
            run: RunRoot::new(path_of(RUN_ROOT)?).map_err(|e| root(RUN_ROOT, e))?,
            families: FamilyRoot::new(path_of(FAMILY_ROOT)?).map_err(|e| root(FAMILY_ROOT, e))?,
            faults_root: path_of(FAULTS_ROOT)?,
            ch: program_of(CH)?,
            ch_remote: program_of(CH_REMOTE)?,
            systemd_run: program_of(SYSTEMD_RUN)?,
            systemctl: program_of(SYSTEMCTL)?,
            zstd: program_of(ZSTD)?,
            mke2fs: program_of(MKE2FS)?,
            fallocate: program_of(FALLOCATE)?,
            limits: Limits {
                ram_cap: gib_of(RAM_CAP, Zero::Refused)?,
                free_floor: gib_of(FREE_FLOOR, Zero::Allowed)?,
            },
            budget: gib_of(BUDGET, Zero::Refused)?,
        })
    }
}

/// Whether a size of 0 GiB is a good value. CONTRACT-QUESTION: no row
/// says. A cap of 0 admits no VM, and a budget of 0 drops every snapshot
/// at the first sweep, so both refuse 0. A floor of 0 only turns the
/// free-space check off.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Zero {
    Allowed,
    Refused,
}

fn error(key: &'static str, fault: Fault) -> ConfigError {
    ConfigError { key, fault }
}

/// The value of `key`. An empty value counts as missing, as in a `.env`.
fn value(
    lookup: &impl Fn(&str) -> Option<OsString>,
    key: &'static str,
) -> Result<OsString, ConfigError> {
    lookup(key)
        .filter(|value| !value.is_empty())
        .ok_or(error(key, Fault::Missing))
}

/// The path of `key`: absolute, with no `..` component. A `..` is refused
/// for a program too, so each path names one place.
fn path(
    lookup: &impl Fn(&str) -> Option<OsString>,
    key: &'static str,
) -> Result<PathBuf, ConfigError> {
    let path = PathBuf::from(value(lookup, key)?);
    if !path.is_absolute() {
        return Err(error(key, Fault::NotAbsolute));
    }
    if path.components().any(|part| part == Component::ParentDir) {
        return Err(error(key, Fault::ParentComponent));
    }
    Ok(path)
}

/// The fault of a root check. `path` ran the two shared checks first.
fn root(key: &'static str, root_error: RootError) -> ConfigError {
    let fault = match root_error {
        RootError::NotAbsolute => Fault::NotAbsolute,
        RootError::ParentComponent => Fault::ParentComponent,
        RootError::SocketPathTooLong(bytes) => Fault::SocketPathTooLong(bytes),
    };
    error(key, fault)
}

/// The size of `key` in bytes: decimal digits only, a count of GiB.
fn gib(
    lookup: &impl Fn(&str) -> Option<OsString>,
    key: &'static str,
    zero: Zero,
) -> Result<u64, ConfigError> {
    let value = value(lookup, key)?;
    let text = value.to_str().ok_or(error(key, Fault::NotGib))?;
    if !text.bytes().all(|byte| byte.is_ascii_digit()) {
        return Err(error(key, Fault::NotGib));
    }
    let count: u64 = text.parse().map_err(|_| error(key, Fault::TooLarge))?;
    if count == 0 && zero == Zero::Refused {
        return Err(error(key, Fault::Zero));
    }
    count.checked_mul(GIB).ok_or(error(key, Fault::TooLarge))
}
