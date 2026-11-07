//! The kvm_ tests of the guest that the vmm composes (decisions CH7-6,
//! CH62 rules 17, 18 and 19). `guest_init::write_initramfs` packs the
//! static guest init of `bin/vm-smoke-setup.sh` into the digest store, and
//! Cloud Hypervisor boots that `store/<sha256>` file with the final
//! `CMDLINE` and a root disk that the test builds (decision CH62 rule
//! 12). The first test runs CH directly: no launch step, no scope and no
//! Landlock ruleset. `creche-vmm-confine/tests/kvm_vm.rs` proves those.
//!
//! `kvm_phase3_pulls_sleeps_restores_and_stops` is the phase 3 proof (plan
//! PR V10, section 5 of the design): `examples/phase3_vm.rs` plays the vmm
//! as a transient user service (decisions CH60 rule 1, CH62 rule 29), with
//! the stand-in facade on the root disk (CH62 rule 12) and Rust stub
//! planes in the test (CH62 rule 28). Its numbered steps name what each
//! one proves.
//!
//! `#[ignore]`: only the job `kvm` runs them, after the setup script, with
//! `cargo test --locked --no-fail-fast -- --ignored kvm_`. With no setup
//! or no `/dev/kvm` they fail. The runner's sudo makes the copies of
//! guest init and ch-remote root-owned, as the vmm row installs them
//! (decision CH36 G20).
#![cfg(target_os = "linux")]

use std::collections::BTreeSet;
use std::ffi::{OsStr, OsString};
use std::fs::{self, File};
use std::io::{self, BufRead as _, BufReader, Write as _};
use std::net::{Ipv4Addr, SocketAddr, TcpListener};
use std::os::unix::fs::PermissionsExt as _;
use std::os::unix::net::UnixStream;
use std::path::{Path, PathBuf};
use std::process::{Child, ChildStdin, ChildStdout, Command, Stdio};
use std::sync::mpsc::{self, Receiver};
use std::thread::{self, sleep};
use std::time::{Duration, Instant};

use creche_vmm::config;
use creche_vmm::guest_init::write_initramfs;
use creche_vmm_confine::command::CMDLINE;
use creche_vmm_confine::slot_profile::CH_BINARY;
use creche_vmm_store::claim::CONSUMED;
use creche_vmm_store::images::ImageStore;
use creche_vmm_store::key::{CH_VERSION, ChVersionError, check_cloud_hypervisor};
use creche_vmm_store::layout::{RunRoot, StateRoot};
use creche_wire::guest::{
    CHAPERONE_PORT, FACADE_PROGRAM, FACADE_STOP_WAIT, LITELLM_PORT, SESSION_DISK_MOUNT,
};
use creche_wire::{FacadeState, GuestPort, SessionId};

/// The variable that names the files of the setup script.
const DIR: &str = "CRECHE_KVM_DIR";

/// The host `mke2fs` of the vmm (`VMM_MKE2FS_PROGRAM`).
const MKE2FS: &str = "/usr/sbin/mke2fs";

/// The size of each test disk.
const DISK_BYTES: u64 = 16 << 20;

/// The image env of the test root disk (decision CH7-7).
const ENV: &[u8] = b"LANG=C.UTF-8\nPATH=/usr/bin:/bin\n";

/// The dirs of the test root disk: each mount point of the guest init
/// boot plan.
const ROOT_DIRS: [&str; 8] = [
    "dev",
    "proc",
    "sys",
    "tmp",
    "run",
    "root",
    "var/cache",
    "var/tmp",
];

/// The line that guest init writes once it is up.
const UP: &str = "guest-init: up";

/// A boot takes about 0.6 s on the runner (fact 79).
const BOOT: Duration = Duration::from_secs(30);

// Each helper with `cfg(test)` may unwrap (rust/clippy.toml).

/// A CH process, killed and reaped on drop, so a failed test leaves no VM.
struct Vm(Child);

impl Drop for Vm {
    fn drop(&mut self) {
        let _ = self.0.kill();
        let _ = self.0.wait();
    }
}

/// The directory of the setup script.
#[cfg(test)]
fn files() -> PathBuf {
    let dir = std::env::var_os(DIR)
        .unwrap_or_else(|| panic!("{DIR} is not set: run bin/vm-smoke-setup.sh first"));
    PathBuf::from(dir)
}

/// Runs `command` on `path` with the sudo of the runner. Decision CH36
/// G20 allows sudo on GitHub's throwaway runners only, never on the host.
#[cfg(test)]
fn sudo(command: &[&str], path: &Path) {
    assert_eq!(
        std::env::var("GITHUB_ACTIONS").as_deref(),
        Ok("true"),
        "sudo runs only on a GitHub runner (decision CH36 G20)"
    );
    let status = Command::new("sudo")
        .arg("-n")
        .args(command)
        .arg(path)
        .status()
        .unwrap();
    assert!(status.success(), "sudo {command:?}: {status}");
}

/// Writes an ext4 disk at `disk`. With `tree`, the disk holds a copy of
/// it. The root dir is owned by root, as on the session disk of the vmm.
#[cfg(test)]
fn ext4(disk: &Path, tree: Option<&Path>) {
    File::create(disk).unwrap().set_len(DISK_BYTES).unwrap();
    let mut command = Command::new(MKE2FS);
    command.args(["-q", "-F", "-t", "ext4", "-E", "root_owner=0:0"]);
    if let Some(tree) = tree {
        command.arg("-d").arg(tree);
    }
    let output = command.arg("--").arg(disk).output().unwrap();
    assert!(
        output.status.success(),
        "mke2fs failed: {}",
        String::from_utf8_lossy(&output.stderr)
    );
}

/// The serial lines, with the CR of each CRLF line end cut.
fn lines(serial: &Path) -> Vec<String> {
    fs::read_to_string(serial)
        .unwrap_or_default()
        .lines()
        .map(|line| line.trim_end().to_owned())
        .collect()
}

/// The vmm packs a root-owned guest init into `store/<sha256>`, and CH
/// boots it with `CMDLINE` to `guest-init: up`. A wrong header, a wrong
/// `/init` mode or a wrong cmdline keeps the line from coming.
#[test]
#[ignore = "needs /dev/kvm and bin/vm-smoke-setup.sh: the job kvm runs it"]
fn kvm_guest_boots_the_vmm_initramfs() {
    let files = files();
    let work = files.join(format!("kvm-guest-{}", std::process::id()));
    let _ = fs::remove_dir_all(&work);
    fs::create_dir(&work).unwrap();

    let program = work.join("guest-init");
    fs::copy(files.join("guest-init"), &program).unwrap();
    sudo(&["chown", "0:0"], &program);
    let state = StateRoot::new(work.join("state")).unwrap();
    let store = ImageStore::new(&state, PathBuf::from("/usr/bin/zstd"));
    let initramfs = write_initramfs(&program, &store).unwrap();
    assert!(initramfs.path().starts_with(work.join("state/store")));

    let tree = work.join("tree");
    let session_dir = SESSION_DISK_MOUNT.trim_start_matches('/');
    for dir in ROOT_DIRS.into_iter().chain([session_dir, "etc/creche"]) {
        fs::create_dir_all(tree.join(dir)).unwrap();
    }
    // guest init reads `/etc/creche/env` before `up` and powers off when
    // it is missing (decision CH62 rule 7). Two sorted lines in the
    // grammar of `creche-env-write`.
    fs::write(tree.join("etc/creche/env"), ENV).unwrap();
    let root = work.join("root.img");
    let session = work.join("session.img");
    ext4(&root, Some(&tree));
    ext4(&session, None);

    let serial = work.join("serial.log");
    // The root disk first, read-only, then the session disk (decisions
    // CH8-7, CH8-8, CH50 rule 10).
    let mut vm = Vm(Command::new(files.join("cloud-hypervisor"))
        .arg("--kernel")
        .arg(files.join("vmlinux"))
        .arg("--initramfs")
        .arg(initramfs.path())
        .args(["--cmdline", CMDLINE])
        .args(["--cpus", "boot=1", "--memory", "size=256M"])
        .arg("--disk")
        .arg(format!(
            "path={},readonly=on,image_type=raw",
            root.display()
        ))
        .arg("--disk")
        .arg(format!("path={},image_type=raw", session.display()))
        .arg("--serial")
        .arg(format!("file={}", serial.display()))
        .args(["--console", "off"])
        .stdin(Stdio::null())
        .spawn()
        .unwrap());

    let deadline = Instant::now() + BOOT;
    while !lines(&serial).iter().any(|line| line == UP) {
        let log = fs::read_to_string(&serial).unwrap_or_default();
        if let Some(status) = vm.0.try_wait().unwrap() {
            panic!("CH exited with {status} before {UP}. Serial:\n{log}");
        }
        assert!(
            Instant::now() < deadline,
            "no {UP} in {BOOT:?}. Serial:\n{log}"
        );
        sleep(Duration::from_millis(20));
    }
    assert!(vm.0.try_wait().unwrap().is_none(), "CH exited after up");

    drop(vm);
    fs::remove_dir_all(&work).unwrap();
}

/// The session and the family of the phase 3 proof.
const SESSION: &str = "0123abcd-0000-4000-8000-0000000010a0";
const FAMILY: &str = "proof";

/// The host programs of the ubuntu-24.04 runner, as `kvm_vm.rs` names
/// them. The vmm config passes each one (plan PR L20).
const SYSTEMD_RUN: &str = "/usr/bin/systemd-run";
const SYSTEMCTL: &str = "/usr/bin/systemctl";
const ZSTD: &str = "/usr/bin/zstd";
const FALLOCATE: &str = "/usr/bin/fallocate";
const SHA256SUM: &str = "/usr/bin/sha256sum";
const E2FSCK: &str = "/usr/sbin/e2fsck";
const DUMPE2FS: &str = "/usr/sbin/dumpe2fs";

/// The smallest family VM shape: 1 vCPU and 256 MiB.
const SHAPE: &str = r#"{"cpus":1,"flavor":"base","memory_mib":256}"#;

/// `creds.json` as caregiver writes it, as in `tests/family_state.rs`.
const CREDS: &str = r#"{"epoch":3,"litellm_key":"sk-test-key","pep_token":"abababababababababababababababababababababababababababababababab","written_at":"2026-11-06T12:00:00Z"}"#;

/// `runtime.json` with a past `model_live_at`, so the `state` reply
/// waits for no hold (decisions CH23-1, CH31 rule 1).
const RUNTIME: &str = r#"{"shell":true,"sandbox_tools":["read"],"model_alias":"litellm/test","model_live_at":"2026-01-01T00:00:00Z"}"#;

/// The example `phase3_vm`. Cargo builds it next to the `deps` dir of
/// the test binary.
#[cfg(test)]
fn example() -> PathBuf {
    let exe = std::env::current_exe().unwrap();
    let debug = exe.parent().and_then(|deps| deps.parent()).unwrap();
    debug.join("examples").join("phase3_vm")
}

/// The program of the version check. The upstream CH of fact 71 may
/// print its git tag, not the `CH_VERSION` of a `--rev` build (fact 78).
/// Then a script that prints `CH_VERSION` stands in, as in `kvm_vm.rs`.
/// Phase 4 brings the CI-built CH (decision CH36 G19).
#[cfg(test)]
fn version_program(work: &Path) -> PathBuf {
    let ch = PathBuf::from(CH_BINARY);
    match check_cloud_hypervisor(&ch, Duration::from_secs(10)) {
        Ok(_) => ch,
        Err(ChVersionError::Mismatch(text)) => {
            assert!(text.contains("v54.0"), "not CH v54.0: {text}");
            let stand_in = work.join("ch-version");
            fs::write(&stand_in, format!("#!/bin/sh\necho {CH_VERSION}\n")).unwrap();
            fs::set_permissions(&stand_in, fs::Permissions::from_mode(0o755)).unwrap();
            stand_in
        }
        Err(error) => panic!("cloud-hypervisor --version: {error:?}"),
    }
}

/// The SHA-256 of `path` in hex.
#[cfg(test)]
fn sha256(path: &Path) -> String {
    let output = Command::new(SHA256SUM)
        .arg("--")
        .arg(path)
        .output()
        .unwrap();
    assert!(output.status.success(), "sha256sum: {output:?}");
    let text = String::from_utf8(output.stdout).unwrap();
    text.split_whitespace().next().unwrap().to_owned()
}

/// The image root (decision CH57 rule 1): the guest kernel and a root
/// disk with the stand-in facade in mode `linked-relay` at
/// `FACADE_PROGRAM` (decision CH62 rule 12). `/etc/creche/env` comes
/// from `playpen/bin/creche-env-write` (decision CH7-7). Root owns
/// `digests.json`, which names the one disk for each flavor.
#[cfg(test)]
fn image_root(work: &Path) -> PathBuf {
    let files = files();
    let tree = work.join("tree");
    let session_dir = SESSION_DISK_MOUNT.trim_start_matches('/');
    for dir in ROOT_DIRS
        .into_iter()
        .chain([session_dir, "etc/creche", "etc/creche-standin"])
    {
        fs::create_dir_all(tree.join(dir)).unwrap();
    }
    let facade = tree.join(FACADE_PROGRAM.trim_start_matches('/'));
    fs::create_dir_all(facade.parent().unwrap()).unwrap();
    fs::copy(files.join("standin_facade"), &facade).unwrap();
    fs::write(tree.join("etc/creche-standin/mode"), "linked-relay\n").unwrap();
    let script =
        Path::new(env!("CARGO_MANIFEST_DIR")).join("../../../playpen/bin/creche-env-write");
    let env = Command::new("/bin/sh")
        .arg(script)
        .args(["LANG", "PATH"])
        .env_clear()
        .envs([("LANG", "C.UTF-8"), ("PATH", "/usr/bin:/bin")])
        .output()
        .unwrap();
    assert!(env.status.success(), "creche-env-write: {env:?}");
    fs::write(tree.join("etc/creche/env"), env.stdout).unwrap();

    let root = work.join("image");
    fs::create_dir(&root).unwrap();
    ext4(&root.join("root.img"), Some(&tree));
    fs::copy(files.join("vmlinux"), root.join("vmlinux")).unwrap();
    let entry = |file: &str| {
        format!(
            r#"{{"file":"{file}","sha256":"{}"}}"#,
            sha256(&root.join(file))
        )
    };
    let disk = entry("root.img");
    let digests = root.join("digests.json");
    let text = format!(
        r#"{{"kernel":{},"base":{disk},"python":{disk}}}"#,
        entry("vmlinux")
    );
    fs::write(&digests, text).unwrap();
    sudo(&["chown", "0:0"], &digests);
    root
}

/// The family `FAMILY` with creds, a config and its VM shape (decisions
/// CH5a, CH45 rule 2, CH47 rule 2).
#[cfg(test)]
fn family_root(work: &Path) -> PathBuf {
    let root = work.join("families");
    let family = root.join(FAMILY);
    fs::create_dir_all(family.join("creds")).unwrap();
    fs::create_dir_all(family.join("config")).unwrap();
    fs::write(family.join("creds/creds.json"), CREDS).unwrap();
    fs::write(family.join("config/runtime.json"), RUNTIME).unwrap();
    fs::write(family.join("config/instructions.md"), "Be brief.").unwrap();
    fs::write(family.join("config/shape.json"), SHAPE).unwrap();
    root
}

/// One stub plane (decision CH62 rule 28, gap Q54-28 option A): a
/// listener on 127.0.0.1 with a port from the kernel. Its thread answers
/// each request with `200` and no body, and sends each request head on
/// the channel.
#[cfg(test)]
fn stub_plane() -> (SocketAddr, Receiver<String>) {
    let listener = TcpListener::bind((Ipv4Addr::LOCALHOST, 0)).unwrap();
    let address = listener.local_addr().unwrap();
    let (heads, received) = mpsc::channel();
    thread::spawn(move || {
        for stream in listener.incoming() {
            let stream = stream.unwrap();
            let mut reader = BufReader::new(&stream);
            let mut head = String::new();
            while !head.ends_with("\r\n\r\n") && reader.read_line(&mut head).unwrap() > 0 {}
            let ok = b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\nConnection: close\r\n\r\n";
            (&stream).write_all(ok).unwrap();
            let _ = heads.send(head);
        }
    });
    (address, received)
}

/// The stub `plane` got one request with the `Host` header of guest
/// `port`, so the splice kept the bytes (decision CH6-2, fact 46).
#[cfg(test)]
fn hit(plane: &Receiver<String>, port: u16) {
    let head = plane.recv_timeout(BOOT).unwrap();
    let host = format!("\r\nHost: 127.0.0.1:{port}\r\n");
    assert!(head.contains(&host), "no {host:?} in {head:?}");
}

/// What the proof writes in the state root of the runner, the default
/// `~/.local/state/creche-vmm` that the slot profiles name (decision
/// CH50 rule 11). A drop removes the session dir, the row, each store
/// file that was not there before, and the work dir.
struct Written {
    state: StateRoot,
    id: SessionId,
    store: BTreeSet<OsString>,
    work: PathBuf,
}

impl Drop for Written {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(self.state.session(&self.id).path());
        let _ = fs::remove_file(self.state.row_file(&self.id));
        for name in store_files(&self.state) {
            if !self.store.contains(&name) {
                let _ = fs::remove_file(self.state.store_dir().join(name));
            }
        }
        let _ = fs::remove_dir_all(&self.work);
    }
}

/// The names in the digest store of `state`.
fn store_files(state: &StateRoot) -> BTreeSet<OsString> {
    fs::read_dir(state.store_dir())
        .map(|dir| dir.flatten().map(|entry| entry.file_name()).collect())
        .unwrap_or_default()
}

/// The vmm of `examples/phase3_vm.rs` as a transient user service
/// (decisions CH60 rule 1, CH62 rule 29). With `--pipe` it reads the
/// stdin and writes the stdout of `systemd-run`, and its stderr, the vmm
/// log, reaches the job log. The unit has the lines of
/// `systemd/creche-vmm.service` that a VM start reads: the run root
/// `RuntimeDirectory=vmm` (gap G5) and `NoNewPrivileges=yes`, so the
/// launch stage writes the exec attribute under no_new_privs (decision
/// CH55). A drop stops the unit, so `BindsTo=` stops each VM scope
/// (decision CH61 rule 1), and reaps `systemd-run`.
struct Vmm {
    unit: String,
    child: Child,
    input: ChildStdin,
    output: BufReader<ChildStdout>,
}

#[cfg(test)]
impl Vmm {
    /// Starts the unit with `env` and waits for `ready`.
    fn start(env: &[(&str, &OsStr)], version: &Path) -> Self {
        let unit = format!("kvm-vmm-phase3-{}.service", std::process::id());
        let mut run = Command::new(SYSTEMD_RUN);
        run.args(["--user", "--pipe", "--quiet", "--collect"])
            .arg(format!("--unit={unit}"));
        for line in [
            "RuntimeDirectory=vmm",
            "RuntimeDirectoryMode=0700",
            "NoNewPrivileges=yes",
        ] {
            run.args(["-p", line]);
        }
        for (key, value) in env {
            let mut arg = OsString::from(format!("--setenv={key}="));
            arg.push(value);
            run.arg(arg);
        }
        let mut child = run
            .arg("--")
            .arg(example())
            .arg("serve")
            .arg(version)
            .args([SESSION, FAMILY])
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .spawn()
            .unwrap();
        let input = child.stdin.take().unwrap();
        let output = BufReader::new(child.stdout.take().unwrap());
        let mut vmm = Self {
            unit,
            child,
            input,
            output,
        };
        assert_eq!(vmm.line(), "ready");
        vmm
    }

    /// Sends the step `step` and reads its answer.
    fn ask(&mut self, step: &str) -> String {
        writeln!(self.input, "{step}").unwrap();
        self.line()
    }

    /// One answer line.
    fn line(&mut self) -> String {
        let mut line = String::new();
        self.output.read_line(&mut line).unwrap();
        assert!(line.ends_with('\n'), "the vmm stand-in ended: {line:?}");
        line.trim_end().to_owned()
    }

    /// The start time and the facade state of the ready of a `start`.
    /// A failed start fails the proof `step`.
    fn start_vm(&mut self, step: &str) -> (u64, FacadeState) {
        let answer = self.ask("start");
        let rest = answer
            .strip_prefix("started ")
            .unwrap_or_else(|| panic!("{step}: {answer}"));
        let (ms, ready) = rest.split_once(' ').unwrap();
        (ms.parse().unwrap(), serde_json::from_str(ready).unwrap())
    }
}

impl Drop for Vmm {
    fn drop(&mut self) {
        let _ = Command::new(SYSTEMCTL)
            .args(["--user", "stop", "--"])
            .arg(&self.unit)
            .status();
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

/// Sends `pull` on a new link to guest port 1201: `CONNECT 1201` on the
/// vsock socket of CH, as `AcpLink::dial` dials (decision CH13). The
/// stand-in pulls 1100 and GETs both planes (decision CH62 rule 13). It
/// gives the words after `pulled`: the facade pid, the reply key, the
/// status of :4000 and of :8300, and the pull time in ms. A fault fails
/// the proof `step`.
#[cfg(test)]
fn pull(vsock: &Path, step: &str) -> Vec<String> {
    let stream = UnixStream::connect(vsock).unwrap();
    stream.set_read_timeout(Some(BOOT)).unwrap();
    let mut reader = BufReader::new(&stream);
    writeln!(&stream, "CONNECT {}", GuestPort::Acp.number()).unwrap();
    let mut line = String::new();
    reader.read_line(&mut line).unwrap();
    assert!(line.starts_with("OK "), "{step}: CONNECT gave {line:?}");
    writeln!(&stream, "pull").unwrap();
    line.clear();
    reader.read_line(&mut line).unwrap();
    let words: Vec<String> = line.split_whitespace().map(str::to_owned).collect();
    assert_eq!(words.len(), 6, "{step}: {line:?}");
    assert_eq!(words[0], "pulled", "{step}: {line:?}");
    words[1..].to_vec()
}

/// The phase 3 proof (plan PR V10, section 5 of the design): a facade in
/// a VM gets the family-state reply and reaches the stub planes after a
/// restore, and after the restore the vmm opens 1200 and 1201 again at
/// once, and the same facade answers (decision CH19 rules 1, 2 and 6).
/// The plan names three proofs, and each is one group of steps here, so
/// one VM serves all of them (plan risk 10). Each step names the plan PR
/// whose idea it needs. The job log holds both pull times (decision
/// CH23).
#[test]
#[ignore = "needs /dev/kvm and bin/vm-smoke-setup.sh: the job kvm runs it"]
fn kvm_phase3_pulls_sleeps_restores_and_stops() {
    let files = files();
    // The run root of `RuntimeDirectory=vmm`, which the slot profiles
    // name (decision CH20, gap G5).
    let runtime = std::env::var_os("XDG_RUNTIME_DIR")
        .expect("systemd-run --user needs XDG_RUNTIME_DIR: bin/vm-smoke-setup.sh exports it");
    let run = RunRoot::new(PathBuf::from(runtime).join("vmm")).unwrap();
    let home = PathBuf::from(std::env::var_os("HOME").unwrap());
    let state = StateRoot::new(home.join(".local/state/creche-vmm")).unwrap();
    let id: SessionId = SESSION.parse().unwrap();
    let session = state.session(&id);
    // A live vmm may own a path of the session, so the test stops at one
    // that it did not make.
    for path in [run.vm(&id).path(), session.path()] {
        assert!(
            fs::symlink_metadata(path).is_err(),
            "{} exists",
            path.display()
        );
    }
    let work = files.join(format!("kvm-phase3-{}", std::process::id()));
    let _ = fs::remove_dir_all(&work);
    fs::create_dir(&work).unwrap();
    let written = Written {
        store: store_files(&state),
        state: state.clone(),
        id: id.clone(),
        work: work.clone(),
    };

    // The owner check of the vmm start refuses a program that root does
    // not own (decisions CH62 rule 17, CH67 rule 1). The setup script
    // installs CH with `sudo install`, so only these two need a chown.
    let guest_init = work.join("guest-init");
    fs::copy(files.join("guest-init"), &guest_init).unwrap();
    sudo(&["chown", "0:0"], &guest_init);
    let ch_remote = work.join("ch-remote");
    fs::copy(files.join("ch-remote"), &ch_remote).unwrap();
    sudo(&["chown", "0:0"], &ch_remote);
    // `FaultDir::open` creates `<faults root>/vmm`, not the root itself.
    let faults = work.join("faults");
    fs::create_dir(&faults).unwrap();
    let (litellm, litellm_heads) = stub_plane();
    let (chaperone, chaperone_heads) = stub_plane();
    let (litellm, chaperone) = (litellm.to_string(), chaperone.to_string());
    let paths = [
        (config::STATE_ROOT, state.path().to_owned()),
        (config::FAMILY_ROOT, family_root(&work)),
        (config::FAULTS_ROOT, faults),
        (config::IMAGE_ROOT, image_root(&work)),
        (config::CH, PathBuf::from(CH_BINARY)),
        (config::CH_REMOTE, ch_remote),
        (config::GUEST_INIT, guest_init),
        (config::EGRESS_LOG, work.join("egress.jsonl")),
    ];
    let mut env: Vec<(&str, &OsStr)> = paths
        .iter()
        .map(|(key, path)| (*key, path.as_os_str()))
        .collect();
    env.extend(
        [
            (config::SYSTEMD_RUN, SYSTEMD_RUN),
            (config::SYSTEMCTL, SYSTEMCTL),
            (config::ZSTD, ZSTD),
            (config::MKE2FS, MKE2FS),
            (config::FALLOCATE, FALLOCATE),
            (config::RAM_CAP, "1"),
            (config::FREE_FLOOR, "0"),
            (config::BUDGET, "1"),
            (config::LITELLM, litellm.as_str()),
            (config::CHAPERONE, chaperone.as_str()),
        ]
        .map(|(key, value)| (key, OsStr::new(value))),
    );
    let mut vmm = Vmm::start(&env, &version_program(&work));
    let vsock = run.vm(&id).vsock_socket();

    // 1. Cold boot: the start ends after the first ControlReady (G3). Its
    // facade is `starting` with start 1 (CH21-8, CH25 rule 2), or already
    // `running` when the facade line of step 2 came first.
    let (_, ready) = vmm.start_vm("step 1 cold boot (G3)");
    let (pid, starts) = match &ready {
        FacadeState::Starting(first) => (first.pid, first.starts.get()),
        FacadeState::Running(first) => (first.pid, first.starts.get()),
        other => panic!("step 1 cold boot (G3): the facade is {other:?}"),
    };
    assert_eq!(starts, 1, "step 1 cold boot (G3)");

    // 2. The facade opens its local link, and a facade line says
    // `running` with the same pid (G5, CH62 rule 11).
    let deadline = Instant::now() + BOOT;
    loop {
        let answer = vmm.ask("facade");
        let facade = answer.strip_prefix("facade ").unwrap();
        if let Ok(FacadeState::Running(running)) = serde_json::from_str::<FacadeState>(facade) {
            assert_eq!(running.pid, pid, "step 2 running (G4): another pid");
            break;
        }
        assert!(Instant::now() < deadline, "step 2 running (G5): {answer}");
        sleep(Duration::from_millis(100));
    }

    // 3. A pull on 1201 reaches the same facade (G6), which gets a
    // `state` reply on 1100 (G7, V8) and a 200 from each stub plane (V6).
    let first_pull = pull(&vsock, "step 3 first pull (G6)");
    assert_eq!(
        first_pull[..4],
        [pid.to_string(), "state".into(), "200".into(), "200".into()],
        "step 3 first pull (G6, G7, V6, V8)"
    );
    hit(&litellm_heads, LITELLM_PORT);
    hit(&chaperone_heads, CHAPERONE_PORT);

    // 4. Sleep: the guest syncs, then the VM snapshots into one
    // generation (G8, V9, CH62 rule 23).
    assert_eq!(vmm.ask("sleep"), "slept 1", "step 4 sleep (G8, V9)");
    let generations: Vec<PathBuf> = fs::read_dir(session.snapshots())
        .unwrap()
        .map(|entry| entry.unwrap().path())
        .collect();
    assert_eq!(generations.len(), 1, "step 4 sleep (V9): {generations:?}");

    // 5. Restore: the start claims that generation, and control::open and
    // AcpLink::dial succeed at once. The ready says `running` with the same
    // pid and start 1, so the same facade answers (CH19 rules 1 and 2). The
    // new serial log has no `up` line, which a cold boot writes.
    let (restore_ms, ready) = vmm.start_vm("step 5 restore (G8, V9)");
    assert!(
        generations[0].join(CONSUMED).is_file(),
        "step 5 restore (V9): no claim"
    );
    assert!(
        !lines(&session.serial_log()).iter().any(|line| line == UP),
        "step 5 restore (V9): the VM booted"
    );
    let FacadeState::Running(restored) = ready else {
        panic!("step 5 restore (G4, CH19 rule 2): the ready is {ready:?}");
    };
    assert_eq!(
        (restored.pid, restored.starts.get()),
        (pid, 1),
        "step 5 restore (G4)"
    );

    // 6. A second pull after the restore: the same pid, a state reply and
    // both planes, through fresh dials (CH6-2, CH62 rule 14, fact 74).
    let second_pull = pull(&vsock, "step 6 second pull (G6)");
    assert_eq!(
        second_pull[..4],
        first_pull[..4],
        "step 6 second pull (G6, G7, V6, V8)"
    );
    hit(&litellm_heads, LITELLM_PORT);
    hit(&chaperone_heads, CHAPERONE_PORT);
    // Decision CH23: the proof measures the pull. A direct write skips
    // the capture of the test harness, so the job log holds the line.
    let times = format!(
        "phase 3 proof: pull {} ms cold, {} ms after a restore, restore start {restore_ms} ms\n",
        first_pull[4], second_pull[4]
    );
    io::stderr().write_all(times.as_bytes()).unwrap();

    // 7. Stop: the graceful stop ends CH, and guest init synced and
    // remounted the session disk, so e2fsck finds it clean (CH19 rule 3,
    // CH62 rule 9). The facade got the stop line and exited by itself:
    // guest init sent it no signal. Guest init signals the facade only
    // after `FACADE_STOP_WAIT`, so the stop also ends before that.
    let answer = vmm.ask("stop");
    let ms: u64 = answer
        .strip_prefix("stopped ")
        .unwrap_or_else(|| panic!("step 7 stop: {answer}"))
        .parse()
        .unwrap();
    assert!(
        Duration::from_millis(ms) < FACADE_STOP_WAIT,
        "step 7 stop (CH19 rule 3): {ms} ms"
    );
    let serial = lines(&session.serial_log());
    for line in [
        "standin: stop line",
        "guest-init: session remounted ro",
        "guest-init: power off",
    ] {
        assert!(
            serial.iter().any(|each| each == line),
            "step 7 stop: no {line}: {serial:?}"
        );
    }
    for signal in ["SIGTERM", "SIGKILL"] {
        let line = format!("guest-init: facade pid {pid} sent {signal}");
        assert!(!serial.contains(&line), "step 7 stop (CH19 rule 3): {line}");
    }
    let header = Command::new(DUMPE2FS)
        .arg("-h")
        .arg(session.disk())
        .output()
        .unwrap();
    let header = String::from_utf8_lossy(&header.stdout);
    assert!(
        !header.contains("needs_recovery"),
        "step 7 stop: the journal waits for a replay"
    );
    let check = Command::new(E2FSCK)
        .arg("-fn")
        .arg(session.disk())
        .output()
        .unwrap();
    assert!(check.status.success(), "step 7 stop: e2fsck -fn: {check:?}");

    drop(vmm);
    drop(written);
}
