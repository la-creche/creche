//! The Cloud Hypervisor command line of a boot and a restore. Each input
//! is a checked store type, so each test imports three images, makes a
//! session disk and a new serial log and, for a restore, claims a keyed
//! snapshot, as at a VM start.

use std::fs;
use std::path::PathBuf;
use std::time::Duration;

use creche_vmm_confine::command::{
    Boot, CMDLINE, ChCommand, CommandError, DiskLimit, REFILL_MIN_MS, RefillMs, RefillTooShort,
};
use creche_vmm_confine::launch::Program;
use creche_vmm_confine::limits::VmLimits;
use creche_vmm_store::claim::{ClaimedSnapshot, claim};
use creche_vmm_store::images::{CheckedImage, ImageStore};
use creche_vmm_store::key::{
    CH_VERSION, KeyCheck, SnapshotKey, check_cloud_hypervisor, check_newest, write_key,
};
use creche_vmm_store::layout::{RunRoot, SnapshotGeneration, StateRoot, VmRunDir};
use creche_vmm_store::serial_log::SerialLog;
use creche_vmm_store::session_disk::{SessionDisk, SessionDisks};
use creche_wire::{FamilyShape, SessionId};

const ID: &str = "0123abcd-0000-4000-8000-00000000c7c7";

/// The SHA-256 of the bytes `kernel`, `initramfs` and `disk`, the three
/// image sources.
const KERNEL_DIGEST: &str = "6923dd1bc0460082c5d55a831908c24a282860b7f1cd6c2b79cf1bc8857c639c";
const INITRAMFS_DIGEST: &str = "9752c38a9065f7646ffaac3621d1fa2f7dbe726c7e12e511eac7fdb14d4e2a24";
const DISK_DIGEST: &str = "1044dec7206e8d7c9fbb4ae8f766668406d2567fc7fc1a160a9d4700fcf8f8e9";

/// The `mke2fs` of the test: it formats nothing.
const MKE2FS: &str = "/usr/bin/true";

/// A boot of a 2 vCPU, 2 GiB VM under the temp root `<root>`, in this
/// order. `<root>` stands for the root.
const BOOT: &[&str] = &[
    "--api-socket",
    "path=<root>/run/vm/0123abcd-0000-4000-8000-00000000c7c7/api.sock",
    "--kernel",
    "<root>/state/store/6923dd1bc0460082c5d55a831908c24a282860b7f1cd6c2b79cf1bc8857c639c",
    "--initramfs",
    "<root>/state/store/9752c38a9065f7646ffaac3621d1fa2f7dbe726c7e12e511eac7fdb14d4e2a24",
    "--cmdline",
    "console=ttyS0 rdinit=/init",
    "--cpus",
    "boot=2",
    "--memory",
    "size=2048M",
    "--balloon",
    "size=0,deflate_on_oom=on,free_page_reporting=on",
    "--disk",
    "path=<root>/state/store/1044dec7206e8d7c9fbb4ae8f766668406d2567fc7fc1a160a9d4700fcf8f8e9,readonly=on,image_type=raw",
    "path=<root>/state/sessions/0123abcd-0000-4000-8000-00000000c7c7/disk.ext4,image_type=raw,bw_size=209715200,bw_refill_time=1000,ops_size=10000,ops_refill_time=1000",
    "--vsock",
    "cid=3,socket=<root>/run/vm/0123abcd-0000-4000-8000-00000000c7c7/vsock.sock",
    "--serial",
    "file=<root>/state/sessions/0123abcd-0000-4000-8000-00000000c7c7/serial.log",
    "--console",
    "off",
];

/// A restore of generation 1 under the temp root.
const RESTORE: &[&str] = &[
    "--api-socket",
    "path=<root>/run/vm/0123abcd-0000-4000-8000-00000000c7c7/api.sock",
    "--restore",
    "source_url=file://<root>/state/sessions/0123abcd-0000-4000-8000-00000000c7c7/snapshots/1,memory_restore_mode=copyonwrite,resume=false",
];

/// A state root with a kernel, an initramfs, a root disk, a session disk
/// and a serial log, and a run root, in a new temp dir, removed on drop.
struct Temp {
    root: PathBuf,
    state: StateRoot,
    kernel: CheckedImage,
    initramfs: CheckedImage,
    root_disk: CheckedImage,
    session: SessionDisk,
    serial: SerialLog,
    run: VmRunDir,
}

// Each helper with `cfg(test)` may unwrap (rust/clippy.toml).
#[cfg(test)]
impl Temp {
    fn new(name: &str) -> Self {
        Self::with_state(name, "state")
    }

    /// The temp root `name` with its state root in the dir `state`.
    fn with_state(name: &str, state: &str) -> Self {
        let root = PathBuf::from(format!("/tmp/vmm-command-{}-{name}", std::process::id()));
        let _ = fs::remove_dir_all(&root);
        fs::create_dir_all(&root).unwrap();
        let state = StateRoot::new(root.join(state)).unwrap();
        let store = ImageStore::new(&state, PathBuf::from("zstd"));
        let images = [
            ("kernel", KERNEL_DIGEST),
            ("initramfs", INITRAMFS_DIGEST),
            ("disk", DISK_DIGEST),
        ];
        let [kernel, initramfs, root_disk] = images.map(|(text, digest)| {
            let source = root.join(text);
            fs::write(&source, text).unwrap();
            let digest = digest.parse().unwrap();
            store.import(&source, digest).unwrap();
            store.check(digest).unwrap()
        });
        let id = id();
        let session = SessionDisks::new(&state, PathBuf::from(MKE2FS))
            .for_boot(&id)
            .unwrap();
        let serial = SerialLog::fresh(&state.session(&id)).unwrap();
        let run = RunRoot::new(root.join("run")).unwrap().vm(&id);
        Self {
            root,
            state,
            kernel,
            initramfs,
            root_disk,
            session,
            serial,
            run,
        }
    }

    /// A boot of the 2 vCPU, 2 GiB shape.
    fn boot(&self) -> Boot<'_> {
        Boot {
            kernel: &self.kernel,
            initramfs: &self.initramfs,
            root: &self.root_disk,
            session: &self.session,
            serial: &self.serial,
            disk: DiskLimit::SESSION,
            limits: limits(),
        }
    }

    /// Snapshot generation 1 with the current key, claimed as at a VM
    /// start. A CH stub that reports `CH_VERSION` makes the key. The stub
    /// is the committed vmm-store fixture, run through a link whose name
    /// sets its reply. A stub written at run time can fail its exec with
    /// ETXTBSY, because a parallel test's fork may still hold it open for
    /// writing (seen in CI on #466).
    fn claimed(&self) -> ClaimedSnapshot {
        let ch = self.root.join(format!("bare-{CH_VERSION}"));
        let stub = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("../vmm-store/tests/fixtures/cloud-hypervisor");
        std::os::unix::fs::symlink(stub, &ch).unwrap();
        let checked = check_cloud_hypervisor(&ch, Duration::from_secs(10)).unwrap();
        let key = SnapshotKey::current(checked, &self.kernel, &self.initramfs, &self.root_disk);

        let session = self.state.session(&id());
        let generation = SnapshotGeneration(1);
        fs::create_dir_all(session.snapshot(generation)).unwrap();
        fs::write(session.snapshot(generation).join("memory-ranges"), "ram").unwrap();
        write_key(&session, generation, &key).unwrap();
        let KeyCheck::Keyed(keyed) = check_newest(&session, &key).unwrap() else {
            panic!("the snapshot has the current key");
        };
        claim(keyed).unwrap().unwrap()
    }

    /// `expected` with `<root>` replaced by the temp root.
    fn argv(&self, expected: &[&str]) -> Vec<String> {
        let root = self.root.to_str().unwrap();
        expected
            .iter()
            .map(|arg| arg.replace("<root>", root))
            .collect()
    }
}

impl Drop for Temp {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.root);
    }
}

#[cfg(test)]
fn id() -> SessionId {
    ID.parse().unwrap()
}

#[cfg(test)]
fn limits() -> VmLimits {
    let text = r#"{"cpus":2,"flavor":"base","memory_mib":2048}"#;
    VmLimits::from(serde_json::from_str::<FamilyShape>(text).unwrap())
}

/// Checks the rules that every argv keeps: no network card, seccomp on,
/// and every path absolute (decisions CH6-3, CH8-4).
#[cfg(test)]
fn assert_safe(args: &[String]) {
    assert!(!args.iter().any(|arg| arg.starts_with("--net")), "{args:?}");
    assert!(!args.iter().any(|arg| arg.contains("seccomp")), "{args:?}");
    for (index, arg) in args.iter().enumerate() {
        if index > 0 && args[index - 1] == "--cmdline" {
            continue;
        }
        for part in arg.split(',') {
            let value = part.split_once('=').map_or(part, |(_, value)| value);
            let value = value.strip_prefix("file://").unwrap_or(value);
            if value.contains('/') {
                assert!(value.starts_with('/'), "{value} is not absolute");
            }
        }
    }
}

/// Every boot names the initramfs after the kernel, and the cmdline has
/// its final value (decisions CH62 rules 18 and 19).
#[test]
fn boot_passes_the_initramfs_and_the_final_cmdline() {
    let temp = Temp::new("boot");
    let command = ChCommand::boot(temp.boot(), &temp.run).unwrap();
    assert_eq!(command.args(), temp.argv(BOOT));
    assert_eq!(
        temp.initramfs.path(),
        temp.root.join(format!("state/store/{INITRAMFS_DIGEST}"))
    );
    assert_eq!(CMDLINE, "console=ttyS0 rdinit=/init");
    assert_safe(command.args());
}

#[test]
fn a_restore_renders_only_the_api_socket_and_the_source() {
    let temp = Temp::new("restore");
    let command = ChCommand::restore(temp.claimed(), &temp.run).unwrap();
    assert_eq!(command.args(), temp.argv(RESTORE));
    assert_safe(command.args());
}

#[test]
fn the_payload_keeps_the_program_and_the_argv() {
    let temp = Temp::new("payload");
    let command = ChCommand::boot(temp.boot(), &temp.run).unwrap();
    let ch = Program::new(PathBuf::from("/opt/components/vmm/bin/cloud-hypervisor")).unwrap();
    let payload = command.clone().payload(ch.clone());
    assert_eq!(payload.program(), &ch);
    assert_eq!(payload.args(), command.args());
}

#[test]
fn a_path_with_option_syntax_is_refused() {
    let temp = Temp::new("syntax");
    for name in ["a,b", "a[b", "a]b", "a\"b"] {
        let run = RunRoot::new(temp.root.join(name)).unwrap().vm(&id());
        let error = ChCommand::boot(temp.boot(), &run).unwrap_err();
        assert_eq!(error, CommandError::OptionSyntax(run.api_socket()));
    }
}

#[test]
fn a_state_path_with_option_syntax_is_refused() {
    // The run root is clean, so the check of each state path decides.
    let temp = Temp::with_state("syntax-state", "st,ate");
    let error = ChCommand::boot(temp.boot(), &temp.run).unwrap_err();
    assert_eq!(
        error,
        CommandError::OptionSyntax(temp.kernel.path().to_owned())
    );
    let claimed = temp.claimed();
    let dir = claimed.dir().to_owned();
    let error = ChCommand::restore(claimed, &temp.run).unwrap_err();
    assert_eq!(error, CommandError::OptionSyntax(dir));
}

#[test]
fn a_session_disk_path_with_option_syntax_is_refused() {
    // The kernel and the root disk are in a clean store.
    let temp = Temp::new("syntax-disk");
    let state = StateRoot::new(temp.root.join("st,ate")).unwrap();
    let session = SessionDisks::new(&state, PathBuf::from(MKE2FS))
        .for_boot(&id())
        .unwrap();
    let boot = Boot {
        session: &session,
        ..temp.boot()
    };
    let error = ChCommand::boot(boot, &temp.run).unwrap_err();
    assert_eq!(error, CommandError::OptionSyntax(session.path().to_owned()));
}

#[test]
fn a_serial_log_path_with_option_syntax_is_refused() {
    // The images and the session disk are in a clean state root.
    let temp = Temp::new("syntax-serial");
    let session = StateRoot::new(temp.root.join("st,ate"))
        .unwrap()
        .session(&id());
    fs::create_dir_all(session.path()).unwrap();
    let serial = SerialLog::fresh(&session).unwrap();
    let boot = Boot {
        serial: &serial,
        ..temp.boot()
    };
    let error = ChCommand::boot(boot, &temp.run).unwrap_err();
    assert_eq!(error, CommandError::OptionSyntax(serial.path().to_owned()));
}

#[test]
fn a_refill_below_one_second_is_refused() {
    assert_eq!(REFILL_MIN_MS, 1000);
    assert_eq!(RefillMs::new(999), Err(RefillTooShort(999)));
    assert_eq!(RefillMs::new(0), Err(RefillTooShort(0)));
    assert_eq!(RefillMs::new(1000).unwrap(), RefillMs::SECOND);
    assert_eq!(RefillMs::new(5000).unwrap().get(), 5000);
}
