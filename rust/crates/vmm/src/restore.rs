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
//! disk beyond the fixed two (the root disk, then the session disk), a
//! vhost-user disk, and a vsock path other than the VM's own. It reads
//! `VmInfo` of `creche-vmm-store` (PR D15).

use std::fmt;
use std::path::{Path, PathBuf};
use std::time::Duration;

use creche_vmm_store::ch_remote::{ChRemote, ChRemoteError, VmInfo, VmState};
use creche_vmm_store::key::SnapshotKey;
use creche_vmm_store::layout::{SessionDir, SnapshotGeneration, VmRunDir};
use creche_vmm_store::snapshot::Snapshotter;

use crate::sleep::{EVICT_SNAPSHOT, take_keyed};

/// The timeout of one `ch-remote info` call (decision CH44 rule 3). An
/// `info` reply is a few KiB, and Cloud Hypervisor answers it at once.
pub const INFO_TIMEOUT: Duration = Duration::from_secs(5);

/// The timeout of `ch-remote resume` (decision CH44 rule 3). A resume
/// takes about as long as a pause, about 4 ms (fact 72).
pub const RESUME_TIMEOUT: Duration = Duration::from_secs(5);

/// The Cloud Hypervisor API that a start uses: the check of a restore and
/// the snapshot of an eviction or a sleep. `ChTools` is the one of the vmm, and a
/// test gives a fake. Each call blocks, so the start runs it in
/// `spawn_blocking`.
pub trait ChApi: Send + Sync + 'static {
    /// True when `info` gives the device set, so the device check can
    /// run. A restore with no device check never resumes, so the start
    /// cold-boots instead (decision CH50 rule 10).
    const READS_DEVICES: bool;

    /// The reply of `ch-remote info`: the state and the device set.
    ///
    /// # Errors
    ///
    /// The `ChRemoteError` of the call. Before Cloud Hypervisor serves its
    /// API, the call fails, and the start tries again inside
    /// `READY_WAIT`. `NotInfo` is a reply that CH gave, so the start
    /// tries no more.
    fn info(&self, vm: &VmRunDir) -> Result<VmInfo, ChRemoteError>;

    /// `ch-remote resume`.
    ///
    /// # Errors
    ///
    /// Why the call failed.
    fn resume(&self, vm: &VmRunDir) -> Result<(), String>;

    /// Snapshots the VM of `vm` into a new generation of `session` with
    /// `key`, and ends its CH (`sleep::take_keyed`).
    ///
    /// # Errors
    ///
    /// Why the take failed (decision CH44 rule 4).
    fn snapshot(
        &self,
        vm: &VmRunDir,
        session: &SessionDir,
        key: &SnapshotKey,
    ) -> Result<SnapshotGeneration, String>;
}

/// The `ch-remote` and the snapshot step of the vmm (PRs D8, D9).
#[derive(Debug, Clone)]
pub struct ChTools {
    /// The shipped `ch-remote`.
    pub remote: ChRemote,
    /// The snapshot step over the same `ch-remote`.
    pub snapshotter: Snapshotter,
}

// PR D15 gives the device set in `VmInfo`, so the vmm restores (plan PR
// L18b). `INFO_TIMEOUT` is the timeout of the call. The retire syncs the
// guest on the control link before each take (decision CH62 rule 23,
// caller duty 7 of AGENTS.md).
impl ChApi for ChTools {
    const READS_DEVICES: bool = true;

    fn info(&self, vm: &VmRunDir) -> Result<VmInfo, ChRemoteError> {
        self.remote.info(vm, INFO_TIMEOUT)
    }

    fn resume(&self, vm: &VmRunDir) -> Result<(), String> {
        self.remote
            .resume(vm, RESUME_TIMEOUT)
            .map_err(|error| error.to_string())
    }

    fn snapshot(
        &self,
        vm: &VmRunDir,
        session: &SessionDir,
        key: &SnapshotKey,
    ) -> Result<SnapshotGeneration, String> {
        take_keyed(&self.snapshotter, vm, session, key, EVICT_SNAPSHOT)
    }
}

/// Why a restored VM may not resume.
#[derive(Debug)]
pub enum DeviceError {
    /// The VM is not paused, so a vCPU may have run before the check.
    NotPaused(VmState),
    /// The VM has a device of this kind.
    Device(&'static str),
    /// The disks are not the root disk, then the session disk.
    Disks,
    /// A disk is a vhost-user disk. CH v54 opens its socket and ignores
    /// its path (PR D15).
    VhostDisk,
    /// The vsock socket is not the one of the VM.
    Vsock(PathBuf),
}

impl fmt::Display for DeviceError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::NotPaused(state) => write!(formatter, "the restored VM is {state:?}, not Paused"),
            Self::Device(kind) => write!(formatter, "the restored VM has a {kind} device"),
            Self::Disks => formatter.write_str("the restored VM has other disks than the two"),
            Self::VhostDisk => formatter.write_str("the restored VM has a vhost-user disk"),
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
pub fn check_devices(info: &VmInfo, expected: &Expected) -> Result<(), DeviceError> {
    if info.state != VmState::Paused {
        return Err(DeviceError::NotPaused(info.state));
    }
    let config = &info.config;
    let lists = [
        ("net", config.net.len()),
        ("generic_vhost_user", config.generic_vhost_user.len()),
        ("fs", config.fs.len()),
        ("user_devices", config.user_devices.len()),
        ("vdpa", config.vdpa.len()),
        ("devices", config.devices.len()),
        ("pmem", config.pmem.len()),
        ("tpm", usize::from(config.tpm.is_some())),
    ];
    if let Some((kind, _)) = lists.iter().find(|(_, count)| *count > 0) {
        return Err(DeviceError::Device(kind));
    }
    // CH v54 opens the socket of a vhost-user disk and ignores its path
    // (PR D15), so a fixed path does not make such a disk safe.
    if config
        .disks
        .iter()
        .any(|disk| disk.vhost_user || disk.vhost_socket.is_some())
    {
        return Err(DeviceError::VhostDisk);
    }
    let disks: Vec<Option<&Path>> = config
        .disks
        .iter()
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
    match &config.vsock {
        Some(vsock) if vsock.socket != expected.vsock => {
            Err(DeviceError::Vsock(vsock.socket.clone()))
        }
        _ => Ok(()),
    }
}

#[cfg(test)]
mod tests {
    use std::path::PathBuf;

    use creche_vmm_store::ch_remote::VmInfo;
    use serde_json::{Value, json};

    use super::{DeviceError, Expected, check_devices};

    fn expected() -> Expected {
        Expected {
            root: PathBuf::from("/s/root"),
            session: PathBuf::from("/s/disk"),
            vsock: PathBuf::from("/r/vsock.sock"),
        }
    }

    /// A disk entry of `path` in the shape of the CH v54 `info` reply.
    fn disk(path: &str, id: &str) -> Value {
        json!({"path": path, "readonly": false, "direct": false,
            "num_queues": 1, "queue_size": 128, "vhost_user": false,
            "id": id, "pci_segment": 0, "sparse": true, "image_type": "Raw"})
    }

    /// The v54 `info` reply of a paused VM with the two disks and the
    /// vsock, with `value` at `field` of the config.
    fn reply(field: &str, value: Value) -> VmInfo {
        let mut config = json!({
            "cpus": {"boot_vcpus": 1, "max_vcpus": 1},
            "memory": {"size": 536_870_912, "shared": false},
            "disks": [disk("/s/root", "_disk0"), disk("/s/disk", "_disk1")],
            "serial": {"file": "/r/serial.log", "mode": "File"},
            "vsock": {"cid": 3, "socket": "/r/vsock.sock", "id": "_vsock0",
                "pci_segment": 0},
        });
        config[field] = value;
        let reply = json!({"state": "Paused", "config": config, "memory_actual_size": 0});
        serde_json::from_value(reply).unwrap()
    }

    /// One v54 entry of each refused kind (decision CH50 rule 10).
    fn refused() -> [(&'static str, Value); 8] {
        [
            (
                "net",
                json!([{"tap": "tap0", "num_queues": 2, "queue_size": 256,
                "vhost_user": false, "vhost_mode": "Client", "id": "_net0"}]),
            ),
            ("generic_vhost_user", json!([{"socket": "/g", "id": "_g0"}])),
            (
                "fs",
                json!([{"tag": "t", "socket": "/s", "num_queues": 1,
                "queue_size": 1024, "id": "_fs0"}]),
            ),
            ("user_devices", json!([{"socket": "/u", "id": "_user0"}])),
            (
                "vdpa",
                json!([{"path": "/dev/vhost-vdpa-0", "num_queues": 1,
                "id": "_vdpa0"}]),
            ),
            (
                "devices",
                json!([{"path": "/sys/bus/pci/devices/0000:01:00.0",
                "iommu": false, "id": "_vfio0"}]),
            ),
            (
                "pmem",
                json!([{"file": "/p", "discard_writes": false,
                "id": "_pmem0"}]),
            ),
            ("tpm", json!({"socket": "/t"})),
        ]
    }

    /// Each refused kind is refused, and an empty list or `null` is no
    /// device (decision CH50 rule 10).
    #[test]
    fn each_refused_kind_is_refused() {
        assert!(check_devices(&reply("net", Value::Null), &expected()).is_ok());
        for (kind, entry) in refused() {
            let found = check_devices(&reply(kind, entry), &expected());
            assert!(
                matches!(found, Err(DeviceError::Device(k)) if k == kind),
                "{kind}"
            );
            if kind != "tpm" {
                assert!(check_devices(&reply(kind, json!([])), &expected()).is_ok());
            }
            assert!(check_devices(&reply(kind, Value::Null), &expected()).is_ok());
        }
    }

    /// A disk list in the wrong order or with a third disk fails, and so
    /// does a vhost-user disk on the path of the root disk (PR D15).
    #[test]
    fn a_bad_disk_list_fails() {
        let swapped = json!([disk("/s/disk", "_disk0"), disk("/s/root", "_disk1")]);
        let found = check_devices(&reply("disks", swapped), &expected());
        assert!(matches!(found, Err(DeviceError::Disks)));
        let third = json!([
            disk("/s/root", "_disk0"),
            disk("/s/disk", "_disk1"),
            disk("/x", "_disk2")
        ]);
        let found = check_devices(&reply("disks", third), &expected());
        assert!(matches!(found, Err(DeviceError::Disks)));
        for (field, value) in [
            ("vhost_user", json!(true)),
            ("vhost_socket", json!("/x.sock")),
        ] {
            let mut root = disk("/s/root", "_disk0");
            root[field] = value;
            let disks = json!([root, disk("/s/disk", "_disk1")]);
            let found = check_devices(&reply("disks", disks), &expected());
            assert!(matches!(found, Err(DeviceError::VhostDisk)), "{field}");
        }
    }

    /// A vsock socket of another VM fails, and a VM that is not paused
    /// fails before the devices.
    #[test]
    fn another_vsock_or_a_running_vm_fails() {
        let other = json!({"cid": 3, "socket": "/r/other.sock", "id": "_vsock0"});
        let found = check_devices(&reply("vsock", other), &expected());
        assert!(matches!(found, Err(DeviceError::Vsock(_))));
        let mut info = reply("net", Value::Null);
        info.state = creche_vmm_store::ch_remote::VmState::Running;
        let found = check_devices(&info, &expected());
        assert!(matches!(found, Err(DeviceError::NotPaused(_))));
    }
}
