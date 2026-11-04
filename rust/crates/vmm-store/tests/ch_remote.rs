//! Each call runs `ch-remote --api-socket <socket> <command>`, reads its
//! reply and ends at its timeout (decisions CH7-5, CH26).
//!
//! `tests/fixtures/ch-remote` stands in for `ch-remote`. It records its
//! argv, and files in the dir of the socket set its stdout, stderr and
//! exit. The fixture is in git, so no test writes a program that another
//! test thread runs. C16 runs the real `ch-remote`.

use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::time::{Duration, Instant};

use creche_vmm_store::ch_remote::{
    ChCommand, ChRemote, ChRemoteError, DiskConfig, Present, VmInfo, VmState, VsockConfig,
};
use creche_vmm_store::layout::{RunRoot, VmRunDir};
use creche_wire::SessionId;

const ID: &str = "0f8e2a4c-1b3d-4e5f-8a9b-0c1d2e3f4a5b";
const TIMEOUT: Duration = Duration::from_secs(10);

/// A run dir of one VM in a new dir under `/tmp`, removed on drop. A short
/// root keeps each socket path under `SOCKET_PATH_MAX`.
struct Temp {
    root: PathBuf,
    vm: VmRunDir,
}

// `cfg(test)` lets the helpers use `expect` (rust/clippy.toml).
#[cfg(test)]
impl Temp {
    fn new(name: &str) -> Self {
        let root = PathBuf::from(format!("/tmp/vmm-chr-{}-{name}", std::process::id()));
        let _ = fs::remove_dir_all(&root);
        let id: SessionId = ID.parse().expect("ID is a session id");
        let vm = RunRoot::new(root.clone()).expect("a run root").vm(&id);
        fs::create_dir_all(vm.path()).expect("the run dir");
        Self { root, vm }
    }

    /// Sets what the stub does: `stdout`, `stderr`, `exit` or `hang`.
    fn set(&self, file: &str, text: &str) {
        fs::write(self.vm.path().join(file), text).expect("a stub file");
    }

    fn argv(&self) -> Vec<String> {
        fs::read_to_string(self.vm.path().join("argv"))
            .expect("the stub ran")
            .lines()
            .map(str::to_owned)
            .collect()
    }

    fn expected_argv(&self, rest: &[&str]) -> Vec<String> {
        let socket = self.vm.api_socket();
        let mut argv = vec!["--api-socket".to_owned(), path_text(&socket)];
        argv.extend(rest.iter().map(|&part| part.to_owned()));
        argv
    }
}

impl Drop for Temp {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.root);
    }
}

#[cfg(test)]
fn path_text(path: &Path) -> String {
    path.to_str().expect("a UTF-8 path").to_owned()
}

/// A call of `ChRemote` that returns no reply.
type Call = fn(&ChRemote, &VmRunDir) -> Result<(), ChRemoteError>;

fn stub() -> ChRemote {
    ChRemote::new(Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures/ch-remote"))
}

#[test]
fn each_command_names_the_api_socket() {
    let temp = Temp::new("argv");
    let calls: [(&str, Call); 3] = [
        ("pause", |ch, vm| ch.pause(vm, TIMEOUT)),
        ("resume", |ch, vm| ch.resume(vm, TIMEOUT)),
        ("shutdown-vmm", |ch, vm| ch.shutdown_vmm(vm, TIMEOUT)),
    ];
    for (name, call) in calls {
        call(&stub(), &temp.vm).expect("the call");
        assert_eq!(temp.argv(), temp.expected_argv(&[name]));
    }
}

#[test]
fn a_snapshot_names_a_file_url() {
    let temp = Temp::new("snapshot");
    let dir = temp.root.join("generation");
    stub().snapshot(&temp.vm, &dir, TIMEOUT).expect("the call");
    let url = format!("file://{}", path_text(&dir));
    assert_eq!(temp.argv(), temp.expected_argv(&["snapshot", &url]));
}

#[test]
fn a_relative_snapshot_dir_is_refused() {
    let temp = Temp::new("relative");
    let error = stub()
        .snapshot(&temp.vm, Path::new("generation"), TIMEOUT)
        .expect_err("a refusal");
    assert!(matches!(error, ChRemoteError::BadDestination(_)), "{error}");
    assert!(!temp.vm.path().join("argv").exists());
}

/// An `info` reply of a booted VM in the shape of Cloud Hypervisor v54
/// (`VmInfoResponse` in `vmm/src/api/mod.rs`, `VmConfig` in
/// `vmm/src/vm_config.rs`): the two disks, the vsock, the serial file and
/// no network card. The shape comes from the v54 source, not from a live
/// CH. C16 reads the reply of the real one.
const V54_REPLY: &str = r#"{
  "config": {
    "cpus": {"boot_vcpus": 2, "max_vcpus": 2, "kvm_hyperv": false,
      "max_phys_bits": 46, "nested": true, "core_scheduling": "Vm",
      "profile": "Host"},
    "memory": {"size": 2147483648, "mergeable": false,
      "hotplug_method": "Acpi", "shared": false, "hugepages": false,
      "prefault": false, "thp": true},
    "payload": {"kernel": "/s/images/vmlinux",
      "cmdline": "console=ttyS0 root=/dev/vda ro"},
    "disks": [
      {"path": "/s/images/root.img", "readonly": true, "direct": false,
        "num_queues": 1, "queue_size": 128,
        "vhost_user": false, "id": "_disk0", "pci_segment": 0,
        "sparse": true, "image_type": "Raw"},
      {"path": "/s/sessions/a/disk.img", "readonly": false, "direct": false,
        "num_queues": 1, "queue_size": 128, "vhost_user": false,
        "rate_limiter_config": {"bandwidth": {"size": 104857600,
          "refill_time": 1000}}, "id": "_disk1", "pci_segment": 0,
        "sparse": true, "image_type": "Raw"}
    ],
    "rng": {"src": "/dev/urandom"},
    "balloon": {"size": 0, "deflate_on_oom": true,
      "free_page_reporting": false, "id": "_balloon0"},
    "serial": {"file": "/r/a/serial.log", "mode": "File"},
    "console": {"mode": "Off"},
    "debug_console": {"mode": "Off", "iobase": 233},
    "vsock": {"cid": 3, "socket": "/r/a/vsock.sock", "id": "_vsock0",
      "pci_segment": 0},
    "pvpanic": false,
    "watchdog": false,
    "landlock_enable": true
  },
  "state": "Paused",
  "memory_actual_size": 2147483648,
  "device_tree": {"_disk0": {"id": "_disk0", "resources": [],
    "children": [], "pci_bdf": "0000:00:02.0"}}
}"#;

/// The reply of `info` when the stub writes `reply`.
fn info_of(name: &str, reply: &str) -> Result<VmInfo, ChRemoteError> {
    let temp = Temp::new(name);
    temp.set("stdout", reply);
    stub().info(&temp.vm, TIMEOUT)
}

fn disk(path: &str) -> DiskConfig {
    DiskConfig {
        path: Some(PathBuf::from(path)),
    }
}

#[test]
fn info_runs_the_info_command() {
    let temp = Temp::new("info");
    temp.set("stdout", r#"{"config":{},"state":"Running"}"#);
    let info = stub().info(&temp.vm, TIMEOUT).expect("the info");
    assert_eq!(info.state, VmState::Running);
    assert_eq!(temp.argv(), temp.expected_argv(&["info"]));
}

/// The v54 reply gives the state, the two disks in order and the vsock,
/// and no other device (decisions CH42 rule 2, CH50 rule 10).
#[test]
fn info_reads_the_device_set_of_a_v54_reply() {
    let info = info_of("v54", V54_REPLY).expect("the info");
    assert_eq!(info.state, VmState::Paused);
    let config = info.config;
    assert_eq!(
        config.disks,
        [disk("/s/images/root.img"), disk("/s/sessions/a/disk.img")]
    );
    assert_eq!(
        config.vsock,
        Some(VsockConfig {
            socket: PathBuf::from("/r/a/vsock.sock"),
        })
    );
    assert!(config.net.is_empty());
    assert!(config.generic_vhost_user.is_empty() && config.fs.is_empty());
    assert!(config.user_devices.is_empty() && config.vdpa.is_empty());
    assert!(config.devices.is_empty() && config.pmem.is_empty());
    assert_eq!(config.tpm, None);
}

#[test]
fn info_reads_a_network_card() {
    let reply = V54_REPLY.replacen(
        r#""pvpanic""#,
        r#""net": [{"tap": "tap0", "num_queues": 2, "queue_size": 256,
          "vhost_user": false, "vhost_mode": "Client", "id": "_net0"}],
        "pvpanic""#,
        1,
    );
    let info = info_of("net", &reply).expect("the info");
    assert_eq!(info.config.net, [Present {}]);
}

/// A list of `null` is no device. A device kind that `VmConfig` does not
/// name passes the read unseen, and the check of the caller never sees it
/// (plan PR D15).
#[test]
fn info_skips_null_lists_and_unknown_kinds() {
    let reply = V54_REPLY.replacen(
        r#""pvpanic""#,
        r#""fs": null, "pmem": null,
        "ivshmem": {"path": "/dev/shm/x", "size": 1048576},
        "pvpanic""#,
        1,
    );
    let info = info_of("unknown", &reply).expect("the info");
    assert!(info.config.fs.is_empty() && info.config.pmem.is_empty());
    assert_eq!(info.config.disks.len(), 2);
}

/// A device field of the wrong type fails the read, and so does a reply
/// with no state or no config.
#[test]
fn an_info_reply_of_the_wrong_shape_is_refused() {
    let replies = [
        r#"{"config":{}}"#,
        r#"{"state":"Paused"}"#,
        r#"{"config":{"vsock":{"cid":3}},"state":"Paused"}"#,
        r#"{"config":{"disks":{"path":"/a"}},"state":"Paused"}"#,
    ];
    for (n, reply) in replies.into_iter().enumerate() {
        let error = info_of(&format!("bad{n}"), reply).expect_err("a refusal");
        assert!(
            matches!(error, ChRemoteError::NotInfo(_)),
            "{reply}: {error}"
        );
    }
}

#[test]
fn a_failed_command_keeps_its_status_and_stderr() {
    let temp = Temp::new("failed");
    temp.set("stderr", "Error: VM is not running\n");
    temp.set("exit", "3");
    let error = stub().pause(&temp.vm, TIMEOUT).expect_err("a failure");
    let ChRemoteError::Failed {
        command,
        status,
        stderr,
    } = error
    else {
        panic!("not Failed: {error}");
    };
    assert_eq!(command, ChCommand::Pause);
    assert_eq!(status.code(), Some(3));
    assert_eq!(stderr, "Error: VM is not running\n");
}

/// The timeout kills `ch-remote` and reaps it. `kill -0` on the pid then
/// fails: a live or an unreaped process would pass it.
#[test]
fn a_hung_command_ends_at_its_timeout() {
    let temp = Temp::new("hang");
    temp.set("hang", "");
    let timeout = Duration::from_secs(1);
    let start = Instant::now();
    let error = stub().resume(&temp.vm, timeout).expect_err("a timeout");
    assert!(start.elapsed() < Duration::from_secs(5));
    assert!(
        matches!(
            error,
            ChRemoteError::Timeout {
                command: ChCommand::Resume,
                ..
            }
        ),
        "{error}"
    );
    let pid = fs::read_to_string(temp.vm.path().join("pid")).expect("the stub pid");
    let probe = Command::new("sh")
        .args(["-c", "kill -0 \"$1\" 2>/dev/null", "sh", pid.trim()])
        .status()
        .expect("the probe");
    assert!(!probe.success(), "pid {} still exists", pid.trim());
}

#[test]
fn a_reply_over_the_cap_is_refused() {
    let temp = Temp::new("large");
    temp.set("stdout", &" ".repeat((1 << 20) + 1));
    let error = stub().info(&temp.vm, TIMEOUT).expect_err("a refusal");
    let ChRemoteError::Io { source, .. } = error else {
        panic!("not Io: {error}");
    };
    assert_eq!(source.kind(), std::io::ErrorKind::FileTooLarge);
}
