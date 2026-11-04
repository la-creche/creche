//! The Cloud Hypervisor command line of one VM, for a boot or a restore.
//!
//! The inputs are the checked store types, so only a checked kernel and
//! root disk (PR D6), a made session disk (PR D7) and a claimed snapshot
//! (PR D11) reach the argv. No input names a network card, so the argv
//! has no `--net` (decisions CH6-3, CH8-1, fact 44). The argv never names
//! `--seccomp`, so the upstream default `true` holds (decision CH8-4).
//!
//! A boot renders, in this order:
//!
//! 1. `--api-socket path=<run>/api.sock`.
//! 2. `--kernel`, `--initramfs` when there is one, and `--cmdline`
//!    `CMDLINE` (decisions CH7-3, CH7-6, CH36 G23).
//! 3. `--cpus`, `--memory` and `--balloon` from `VmLimits` (decision
//!    CH8-5).
//! 4. `--disk` with the root disk `readonly=on` first, so it is
//!    `/dev/vda` (decision CH8-8), then the session disk with its rate
//!    limiter (decision CH8-7, CH36 G15).
//! 5. `--vsock cid=3,socket=<run>/vsock.sock` (decisions CH13, CH14).
//! 6. `--serial file=<run>/serial.log` and `--console off` (decisions
//!    CH36 G23, CH41 rule 2).
//!
//! A restore renders only `--api-socket` and `--restore`. CH v54 takes
//! the sizes and every device from the snapshot and refuses a device
//! argument beside `--restore` (the spike of `deny.rs`). The restore maps
//! `memory-ranges` copy-on-write, so the snapshot files stay unchanged
//! while the VM runs (decisions CH2, CH29, facts 55, 79). The restore
//! passes `resume=false`, so no vCPU runs before the vmm checks the
//! device set of the restored VM (decision CH50 rule 10).

use std::fmt;
use std::num::NonZeroU64;
use std::path::{Path, PathBuf};

use creche_vmm_store::claim::ClaimedSnapshot;
use creche_vmm_store::images::CheckedImage;
use creche_vmm_store::layout::VmRunDir;
use creche_vmm_store::session_disk::SessionDisk;

use crate::landlock::HostPath;
use crate::launch::{Payload, Program};
use crate::limits::{MIB, VmLimits};

/// The guest kernel command line (decision CH36 G23). The root disk is
/// the first `--disk`, so it is `/dev/vda`, mounted read only. The kernel
/// writes to the serial port. Phase 3 sets the final value with guest
/// init. With an initramfs the kernel runs its `/init` and ignores
/// `root=`.
pub const CMDLINE: &str = "console=ttyS0 root=/dev/vda ro";

/// The guest vsock CID. CH gives each VM its own vsock device behind its
/// own socket, so every VM may use the same CID.
pub const GUEST_CID: u32 = 3;

/// The shortest refill time of a rate limiter, in ms (decision CH8-7).
pub const REFILL_MIN_MS: u64 = 1000;

/// The session disk bandwidth: 200 MiB/s (decision CH36 G15). A proposal,
/// not a measure. Phase 8 measures it.
pub const SESSION_BYTES_PER_SECOND: u64 = 200 * MIB;

/// The session disk operations per second: 10,000 (decision CH36 G15). A
/// proposal, not a measure. Phase 8 measures it.
pub const SESSION_OPS_PER_SECOND: u64 = 10_000;

/// The chars that the option parser of CH reads as syntax: a comma ends
/// a value, and brackets and quotes group one. A path in an option value
/// holds none of them.
const OPTION_SYNTAX: &[char] = &[',', '[', ']', '"'];

/// A rate limiter refill time of at least `REFILL_MIN_MS`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct RefillMs(u64);

/// Why a refill time is refused: it is below `REFILL_MIN_MS`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct RefillTooShort(pub u64);

impl fmt::Display for RefillTooShort {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            formatter,
            "a refill time of {} ms is below {REFILL_MIN_MS} ms",
            self.0
        )
    }
}

impl std::error::Error for RefillTooShort {}

impl RefillMs {
    /// One second, the refill of the session disk (decision CH36 G15).
    pub const SECOND: Self = Self(REFILL_MIN_MS);

    /// Checks `ms` and makes the refill time.
    ///
    /// # Errors
    ///
    /// `RefillTooShort` below `REFILL_MIN_MS` (decision CH8-7).
    pub fn new(ms: u64) -> Result<Self, RefillTooShort> {
        if ms < REFILL_MIN_MS {
            return Err(RefillTooShort(ms));
        }
        Ok(Self(ms))
    }

    /// The time in ms.
    #[must_use]
    pub fn get(self) -> u64 {
        self.0
    }
}

/// The rate limiter of the session disk: the bytes and the operations
/// that each refill gives back (decision CH8-7).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct DiskLimit {
    bytes: NonZeroU64,
    ops: NonZeroU64,
    refill: RefillMs,
}

impl DiskLimit {
    /// `bytes` and `ops` each `refill`.
    #[must_use]
    pub fn new(bytes: NonZeroU64, ops: NonZeroU64, refill: RefillMs) -> Self {
        Self { bytes, ops, refill }
    }

    /// The limit of decision CH36 G15: `SESSION_BYTES_PER_SECOND` and
    /// `SESSION_OPS_PER_SECOND`, refilled each second. MIN (1) plus the
    /// value minus 1 is the value, with no `None` branch.
    pub const SESSION: Self = Self {
        bytes: NonZeroU64::MIN.saturating_add(SESSION_BYTES_PER_SECOND - 1),
        ops: NonZeroU64::MIN.saturating_add(SESSION_OPS_PER_SECOND - 1),
        refill: RefillMs::SECOND,
    };

    /// The options of the limiter in a CH `--disk` value.
    fn options(self) -> String {
        let refill = self.refill.get();
        format!(
            "bw_size={},bw_refill_time={refill},ops_size={},ops_refill_time={refill}",
            self.bytes, self.ops
        )
    }
}

/// The files and sizes of a cold boot.
#[derive(Debug, Clone, Copy)]
pub struct Boot<'a> {
    /// The guest kernel (decision CH7-3).
    pub kernel: &'a CheckedImage,
    /// The initramfs that the vmm writes at VM start (decision CH7-6). It
    /// is optional until phase 3 adds guest init.
    pub initramfs: Option<&'a HostPath>,
    /// The shared root disk, read only (decision CH8-8).
    pub root: &'a CheckedImage,
    /// The session disk (decision CH8-7).
    pub session: &'a SessionDisk,
    /// The limiter of the session disk.
    pub disk: DiskLimit,
    /// The sizes from the family VM shape (decision CH8-5).
    pub limits: VmLimits,
}

/// Why a path cannot go into the argv.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum CommandError {
    /// The path is not UTF-8, and an argument is a `String`.
    NotUtf8(PathBuf),
    /// The path holds a char of `OPTION_SYNTAX`.
    OptionSyntax(PathBuf),
}

impl fmt::Display for CommandError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::NotUtf8(path) => write!(formatter, "{} is not UTF-8", path.display()),
            Self::OptionSyntax(path) => write!(
                formatter,
                "{} holds a comma, a bracket or a quote",
                path.display()
            ),
        }
    }
}

impl std::error::Error for CommandError {}

/// The text of `path` for the argv. The check refuses every path, also a
/// path that is not in an option value, so one rule holds for all.
fn text(path: &Path) -> Result<&str, CommandError> {
    let text = path
        .to_str()
        .ok_or_else(|| CommandError::NotUtf8(path.to_owned()))?;
    if text.contains(OPTION_SYNTAX) {
        return Err(CommandError::OptionSyntax(path.to_owned()));
    }
    Ok(text)
}

/// The Cloud Hypervisor command line of one VM, after the program.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ChCommand(Vec<String>);

impl ChCommand {
    /// The argv of a cold boot of the VM whose sockets are in `run`.
    ///
    /// # Errors
    ///
    /// `NotUtf8` or `OptionSyntax` for a path.
    pub fn boot(boot: Boot<'_>, run: &VmRunDir) -> Result<Self, CommandError> {
        let mut args = api_socket(run)?;
        args.extend(["--kernel".to_owned(), text(boot.kernel.path())?.to_owned()]);
        if let Some(initramfs) = boot.initramfs {
            args.extend(["--initramfs".to_owned(), text(initramfs.path())?.to_owned()]);
        }
        args.extend(["--cmdline".to_owned(), CMDLINE.to_owned()]);
        args.extend(boot.limits.ch_args());
        args.extend([
            "--disk".to_owned(),
            format!("path={},readonly=on", text(boot.root.path())?),
            format!(
                "path={},{}",
                text(boot.session.path())?,
                boot.disk.options()
            ),
            "--vsock".to_owned(),
            format!("cid={GUEST_CID},socket={}", text(&run.vsock_socket())?),
            "--serial".to_owned(),
            format!("file={}", text(&run.serial_log())?),
            "--console".to_owned(),
            "off".to_owned(),
        ]);
        Ok(Self(args))
    }

    /// The argv of a restore of `snapshot` into a VM whose sockets are in
    /// `run`. The restore takes the claim, so one claim restores once
    /// (decision CH19 (5)).
    ///
    /// The restore passes `resume=false`, so the VM stays paused after
    /// the restore (decision CH50 rule 10). The caller then reads
    /// `ch-remote info`, requires `state` `Paused`, checks the device
    /// set, and only then runs `ch-remote resume`. AGENTS.md names the
    /// steps.
    ///
    /// # Errors
    ///
    /// `NotUtf8` or `OptionSyntax` for a path.
    pub fn restore(snapshot: ClaimedSnapshot, run: &VmRunDir) -> Result<Self, CommandError> {
        let mut args = api_socket(run)?;
        args.extend([
            "--restore".to_owned(),
            format!(
                "source_url=file://{},memory_restore_mode=copyonwrite,resume=false",
                text(snapshot.dir())?
            ),
        ]);
        Ok(Self(args))
    }

    /// The arguments after the program.
    #[must_use]
    pub fn args(&self) -> &[String] {
        &self.0
    }

    /// The payload of the launch step: `ch` with this argv.
    #[must_use]
    pub fn payload(self, ch: Program) -> Payload {
        Payload::new(ch, self.0)
    }
}

/// `--api-socket path=<run>/api.sock`, the first two arguments.
fn api_socket(run: &VmRunDir) -> Result<Vec<String>, CommandError> {
    Ok(vec![
        "--api-socket".to_owned(),
        format!("path={}", text(&run.api_socket())?),
    ])
}
