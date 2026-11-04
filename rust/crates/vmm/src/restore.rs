//! The check of a restored VM before its vCPUs run (decisions CH42 rule
//! 2, CH50 rule 10). A restore runs with `resume=false` (PR C7). Then the
//! vmm reads `ch-remote info`, requires `state` `Paused`, checks the
//! device set, and only then runs `ch-remote resume`. The wait for
//! `ControlReady` follows.
//!
//! The command line of a restore shows no device, so `deny::check` cannot
//! see a device that the snapshot holds. The device check refuses each
//! device kind that reaches the host: `net`, `generic_vhost_user`, `fs`,
//! `user_devices`, `vdpa`, `devices`, `pmem` and `tpm`. It refuses any
//! disk beyond the fixed two (the root disk, then the session disk), and
//! a vsock path other than the VM's own.

use std::fmt;
use std::path::{Path, PathBuf};
use std::time::Duration;

use creche_vmm_store::ch_remote::{ChRemote, VmState};
use creche_vmm_store::layout::VmRunDir;
use serde::Deserialize;
use serde::de::IgnoredAny;

/// The timeout of one `ch-remote info` call (decision CH44 rule 3). An
/// `info` reply is a few KiB, and Cloud Hypervisor answers it at once.
pub const INFO_TIMEOUT: Duration = Duration::from_secs(5);

/// The timeout of `ch-remote resume` (decision CH44 rule 3). A resume
/// takes about as long as a pause, about 4 ms (fact 72).
pub const RESUME_TIMEOUT: Duration = Duration::from_secs(5);

/// The Cloud Hypervisor API of a restored VM. `ChRemote` of
/// `creche-vmm-store` is the one of the vmm, and a test gives a fake.
/// Each call blocks, so the start runs it in `spawn_blocking`.
pub trait ChApi: Send + Sync + 'static {
    /// True when `info` gives the whole reply, so the device check can
    /// run. A restore with no device check never resumes, so the start
    /// cold-boots instead (decision CH50 rule 10).
    const READS_DEVICES: bool;

    /// The stdout of `ch-remote info`.
    ///
    /// # Errors
    ///
    /// Why the call failed. Before Cloud Hypervisor serves its API, the
    /// call fails, and the start tries again inside `READY_WAIT`.
    fn info(&self, vm: &VmRunDir) -> Result<Vec<u8>, String>;

    /// `ch-remote resume`.
    ///
    /// # Errors
    ///
    /// Why the call failed.
    fn resume(&self, vm: &VmRunDir) -> Result<(), String>;
}

// CONTRACT-QUESTION (decision CH50 rule 10, plan PR L17 "through ch-remote
// info (D8)"): `ChRemote::info` of PR D8 returns `VmInfo`, which holds
// only `state` and drops every other field. So the vmm cannot read the
// device set through it, and this crate may not edit the store crate.
// The narrowest reading: no restore resumes without the device check, so
// `READS_DEVICES` is false and every start cold-boots (the claim still
// consumes the snapshot, decision CH19 (5)). A store PR that gives the
// whole `info` reply (or the device fields) costs about 10 lines there
// and 3 lines here.
impl ChApi for ChRemote {
    const READS_DEVICES: bool = false;

    fn info(&self, _vm: &VmRunDir) -> Result<Vec<u8>, String> {
        Err("ChRemote::info gives no device set".to_owned())
    }

    fn resume(&self, vm: &VmRunDir) -> Result<(), String> {
        Self::resume(self, vm, RESUME_TIMEOUT).map_err(|error| error.to_string())
    }
}

/// The part of the `info` reply that the check reads. Every other field
/// is skipped. A field of the wrong type fails the read.
#[derive(Debug, Deserialize)]
struct Info {
    state: VmState,
    config: Config,
}

/// The devices of the VM config, as Cloud Hypervisor v54 names them. A
/// missing field and `null` are both no device.
#[derive(Debug, Deserialize)]
struct Config {
    disks: Option<Vec<Disk>>,
    vsock: Option<Vsock>,
    net: Option<Vec<IgnoredAny>>,
    generic_vhost_user: Option<Vec<IgnoredAny>>,
    fs: Option<Vec<IgnoredAny>>,
    user_devices: Option<Vec<IgnoredAny>>,
    vdpa: Option<Vec<IgnoredAny>>,
    devices: Option<Vec<IgnoredAny>>,
    pmem: Option<Vec<IgnoredAny>>,
    tpm: Option<IgnoredAny>,
}

/// One disk of the config.
#[derive(Debug, Deserialize)]
struct Disk {
    path: Option<PathBuf>,
}

/// The vsock device of the config.
#[derive(Debug, Deserialize)]
struct Vsock {
    socket: PathBuf,
}

/// Why a restored VM may not resume.
#[derive(Debug)]
pub enum DeviceError {
    /// The reply is not a VM info.
    NotInfo(serde_json::Error),
    /// The VM is not paused, so a vCPU may have run before the check.
    NotPaused(VmState),
    /// The VM has a device of this kind.
    Device(&'static str),
    /// The disks are not the root disk, then the session disk.
    Disks,
    /// The vsock socket is not the one of the VM.
    Vsock(PathBuf),
}

impl fmt::Display for DeviceError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::NotInfo(error) => write!(formatter, "the info reply is not a VM info: {error}"),
            Self::NotPaused(state) => write!(formatter, "the restored VM is {state:?}, not Paused"),
            Self::Device(kind) => write!(formatter, "the restored VM has a {kind} device"),
            Self::Disks => formatter.write_str("the restored VM has other disks than the two"),
            Self::Vsock(path) => write!(formatter, "the restored vsock is {}", path.display()),
        }
    }
}

impl std::error::Error for DeviceError {}

/// The files that a restored VM may name: the root disk, the session disk
/// and the vsock socket of its run dir.
#[derive(Debug, Clone)]
pub struct Expected {
    /// The checked store copy of the root disk.
    pub root: PathBuf,
    /// The session disk.
    pub session: PathBuf,
    /// The vsock socket of the VM run dir.
    pub vsock: PathBuf,
}

/// Checks the `info` reply of a restored VM against `expected` (decisions
/// CH42 rule 2, CH50 rule 10).
///
/// # Errors
///
/// The first `DeviceError` that the check finds.
pub fn check_devices(reply: &[u8], expected: &Expected) -> Result<(), DeviceError> {
    let info: Info = serde_json::from_slice(reply).map_err(DeviceError::NotInfo)?;
    if info.state != VmState::Paused {
        return Err(DeviceError::NotPaused(info.state));
    }
    let config = info.config;
    let lists = [
        ("net", config.net),
        ("generic_vhost_user", config.generic_vhost_user),
        ("fs", config.fs),
        ("user_devices", config.user_devices),
        ("vdpa", config.vdpa),
        ("devices", config.devices),
        ("pmem", config.pmem),
    ];
    if let Some((kind, _)) = lists
        .iter()
        .find(|(_, list)| list.as_ref().is_some_and(|list| !list.is_empty()))
    {
        return Err(DeviceError::Device(kind));
    }
    if config.tpm.is_some() {
        return Err(DeviceError::Device("tpm"));
    }
    let disks: Vec<Option<&Path>> = config
        .disks
        .iter()
        .flatten()
        .map(|disk| disk.path.as_deref())
        .collect();
    if disks
        != [
            Some(expected.root.as_path()),
            Some(expected.session.as_path()),
        ]
    {
        return Err(DeviceError::Disks);
    }
    match config.vsock {
        Some(vsock) if vsock.socket != expected.vsock => Err(DeviceError::Vsock(vsock.socket)),
        _ => Ok(()),
    }
}

#[cfg(test)]
mod tests {
    use std::path::PathBuf;

    use serde_json::{Value, json};

    use super::{DeviceError, Expected, check_devices};

    fn expected() -> Expected {
        Expected {
            root: PathBuf::from("/s/root"),
            session: PathBuf::from("/s/disk"),
            vsock: PathBuf::from("/r/vsock.sock"),
        }
    }

    fn reply(field: &str, value: Value) -> Vec<u8> {
        let mut config = json!({
            "disks": [{"path": "/s/root"}, {"path": "/s/disk"}],
            "vsock": {"cid": 3, "socket": "/r/vsock.sock"},
        });
        config[field] = value;
        json!({"state": "Paused", "config": config})
            .to_string()
            .into_bytes()
    }

    /// Each host-reaching list kind is refused, and an empty list or
    /// `null` is no device (decision CH50 rule 10).
    #[test]
    fn each_listed_kind_is_refused() {
        let kinds = [
            "net",
            "generic_vhost_user",
            "fs",
            "user_devices",
            "vdpa",
            "devices",
            "pmem",
        ];
        for kind in kinds {
            let found = check_devices(&reply(kind, json!([{}])), &expected());
            assert!(
                matches!(found, Err(DeviceError::Device(k)) if k == kind),
                "{kind}"
            );
            assert!(check_devices(&reply(kind, json!([])), &expected()).is_ok());
            assert!(check_devices(&reply(kind, Value::Null), &expected()).is_ok());
        }
    }

    /// A reply with no config, or a disk list in the wrong order, fails.
    #[test]
    fn a_bad_reply_or_disk_order_fails() {
        let found = check_devices(br#"{"state":"Paused"}"#, &expected());
        assert!(matches!(found, Err(DeviceError::NotInfo(_))));
        let swapped = json!([{"path": "/s/disk"}, {"path": "/s/root"}]);
        let found = check_devices(&reply("disks", swapped), &expected());
        assert!(matches!(found, Err(DeviceError::Disks)));
    }
}
