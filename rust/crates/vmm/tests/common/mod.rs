//! The helpers of the kvm_ tests of the vmm (plan PRs V10 and E3): the
//! files of `bin/vm-smoke-setup.sh`, the root-owned copies of the runner,
//! the ext4 disks, an image root with the stand-in facade in one mode
//! (decision CH62 rule 12), a family root, the stub planes (decision CH62
//! rule 28), and the vmm of `examples/phase3_vm.rs` as a transient user
//! service (decisions CH60 rule 1, CH62 rule 29). Plan PR E3 moved them
//! here from `tests/kvm_guest.rs`, so `tests/kvm_reach.rs` boots the same
//! vmm.
#![cfg(test)]

use std::collections::BTreeSet;
use std::ffi::{OsStr, OsString};
use std::fs::{self, File};
use std::io::{BufRead as _, BufReader, Write as _};
use std::net::{Ipv4Addr, SocketAddr, TcpListener};
use std::path::{Path, PathBuf};
use std::process::{Child, ChildStdin, ChildStdout, Command, Stdio};
use std::sync::mpsc::{self, Receiver};
use std::sync::{Mutex, MutexGuard, PoisonError};
use std::thread;
use std::time::Duration;

use creche_vmm::config;
use creche_vmm_confine::slot_profile::CH_BINARY;
use creche_vmm_store::layout::{RunRoot, SessionDir, StateRoot};
use creche_wire::guest::{FACADE_PROGRAM, SESSION_DISK_MOUNT};
use creche_wire::{FacadeState, SessionId};

/// The variable that names the files of the setup script.
const DIR: &str = "CRECHE_KVM_DIR";

/// The host `mke2fs` of the vmm (`VMM_MKE2FS_PROGRAM`).
const MKE2FS: &str = "/usr/sbin/mke2fs";

/// The size of each test disk.
const DISK_BYTES: u64 = 16 << 20;

/// The dirs of the test root disk: each mount point of the guest init
/// boot plan.
pub const ROOT_DIRS: [&str; 8] = [
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
pub const UP: &str = "guest-init: up";

/// A boot takes about 0.6 s on the runner (fact 79).
pub const BOOT: Duration = Duration::from_secs(30);

/// The session and the family of each proof that boots the vmm.
pub const SESSION: &str = "0123abcd-0000-4000-8000-0000000010a0";
pub const FAMILY: &str = "proof";

/// The host programs of the ubuntu-24.04 runner, as `kvm_vm.rs` names
/// them. The vmm config passes each one (plan PR L20).
const SYSTEMD_RUN: &str = "/usr/bin/systemd-run";
pub const SYSTEMCTL: &str = "/usr/bin/systemctl";
const ZSTD: &str = "/usr/bin/zstd";
/// The git of the runner, which root owns, so the owner check of
/// `VMM_GIT_PROGRAM` passes (decision CH69 rule 17).
pub const HOST_GIT: &str = "/usr/bin/git";
const FALLOCATE: &str = "/usr/bin/fallocate";
const SHA256SUM: &str = "/usr/bin/sha256sum";

/// The smallest family VM shape: 1 vCPU and 256 MiB.
const SHAPE: &str = r#"{"cpus":1,"flavor":"base","memory_mib":256}"#;

/// `creds.json` as caregiver writes it, as in `tests/family_state.rs`.
const CREDS: &str = r#"{"epoch":3,"litellm_key":"sk-test-key","pep_token":"abababababababababababababababababababababababababababababababab","written_at":"2026-11-06T12:00:00Z"}"#;

/// `runtime.json` with a past `model_live_at`, so the `state` reply
/// waits for no hold (decisions CH23-1, CH31 rule 1).
const RUNTIME: &str = r#"{"shell":true,"sandbox_tools":["read"],"model_alias":"litellm/test","model_live_at":"2026-01-01T00:00:00Z"}"#;

/// One vmm at a time: each proof uses the session `SESSION`, the run root
/// of `RuntimeDirectory=vmm` and the state root of the runner, and the
/// tests of one binary run in parallel.
static ONE_VMM: Mutex<()> = Mutex::new(());

/// The directory of the setup script.
pub fn files() -> PathBuf {
    let dir = std::env::var_os(DIR)
        .unwrap_or_else(|| panic!("{DIR} is not set: run bin/vm-smoke-setup.sh first"));
    PathBuf::from(dir)
}

/// Runs `command` on `path` with the sudo of the runner. Decision CH36
/// G20 allows sudo on GitHub's throwaway runners only, never on the host.
pub fn sudo(command: &[&str], path: &Path) {
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
pub fn ext4(disk: &Path, tree: Option<&Path>) {
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
pub fn lines(serial: &Path) -> Vec<String> {
    fs::read_to_string(serial)
        .unwrap_or_default()
        .lines()
        .map(|line| line.trim_end().to_owned())
        .collect()
}

/// The example `phase3_vm`. Cargo builds it next to the `deps` dir of
/// the test binary.
fn example() -> PathBuf {
    let exe = std::env::current_exe().unwrap();
    let debug = exe.parent().and_then(|deps| deps.parent()).unwrap();
    debug.join("examples").join("phase3_vm")
}

/// The SHA-256 of `path` in hex.
pub fn sha256(path: &Path) -> String {
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
/// disk with the stand-in facade in mode `mode` at `FACADE_PROGRAM`
/// (decision CH62 rule 12). `/etc/creche/env` comes from
/// `playpen/bin/creche-env-write` (decision CH7-7). Root owns
/// `digests.json`, which names the one disk for each flavor.
fn image_root(work: &Path, mode: &str) -> PathBuf {
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
    fs::write(tree.join("etc/creche-standin/mode"), format!("{mode}\n")).unwrap();
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

/// The family root of a proof in `work`: the family `FAMILY` with creds,
/// a config and its VM shape (decisions CH5a, CH45 rule 2, CH47 rule 2).
pub fn family_root(work: &Path) -> PathBuf {
    work.join("families")
}

/// Writes the family `FAMILY` in `family_root(work)`.
fn write_family(work: &Path) -> PathBuf {
    let root = family_root(work);
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
pub fn stub_plane() -> (SocketAddr, Receiver<String>) {
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

/// What a proof writes in the state root of the runner, the default
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
pub struct Vmm {
    unit: String,
    child: Child,
    input: ChildStdin,
    output: BufReader<ChildStdout>,
}

impl Vmm {
    /// Starts the unit of the proof `name` with `env` and waits for
    /// `ready`.
    fn start(name: &str, env: &[(&str, &OsStr)]) -> Self {
        let unit = format!("kvm-vmm-{name}-{}.service", std::process::id());
        let mut child = transient(&unit, env)
            .arg(example())
            .arg("serve")
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
    pub fn ask(&mut self, step: &str) -> String {
        writeln!(self.input, "{step}").unwrap();
        self.line()
    }

    /// One answer line. Only a line that ends in its LF counts.
    fn line(&mut self) -> String {
        let mut line = String::new();
        self.output.read_line(&mut line).unwrap();
        assert!(line.ends_with('\n'), "the vmm stand-in ended: {line:?}");
        line.trim_end().to_owned()
    }

    /// The start time and the facade state of the ready of a `start`.
    /// A failed start fails the proof `step`.
    pub fn start_vm(&mut self, step: &str) -> (u64, FacadeState) {
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

/// The work dir of the proof `name` in the files of the setup script.
pub fn work(name: &str) -> PathBuf {
    files().join(format!("kvm-{name}-{}", std::process::id()))
}

/// A vmm that `boot` started, and what its proof reads. The fields drop
/// in their order: the vmm stops first, then the written files go, then
/// the next proof may start a vmm.
pub struct Booted<V = Vmm> {
    /// The vmm of the proof.
    pub vmm: V,
    /// The vsock socket of the VM of the session.
    pub vsock: PathBuf,
    /// The session dir in the state root.
    pub session: SessionDir,
    _written: Written,
    _one: MutexGuard<'static, ()>,
}

/// Starts the vmm of the proof `name` with the env of each key of the
/// vmm config (decisions CH59 rule 1, CH62 rule 29): the state root of
/// the runner, a scratch family root, root-owned copies of guest init and
/// ch-remote (decision CH36 G20), an image root with the stand-in facade
/// in mode `mode`, and the planes `litellm` and `chaperone`. The family of
/// the proof has no `egress.json` and no `repos.json`.
pub fn boot(name: &str, mode: &str, planes: (SocketAddr, SocketAddr)) -> Booted {
    boot_with(name, mode, planes, |name, env, _| Vmm::start(name, env))
}

/// `boot` with the vmm that `start` starts from the name of the proof,
/// the env and the run root (plan PR E4).
pub fn boot_with<V>(
    name: &str,
    mode: &str,
    (litellm, chaperone): (SocketAddr, SocketAddr),
    start: impl FnOnce(&str, &[(&str, &OsStr)], &RunRoot) -> V,
) -> Booted<V> {
    let one = ONE_VMM.lock().unwrap_or_else(PoisonError::into_inner);
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
    let work = work(name);
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
    let (litellm, chaperone) = (litellm.to_string(), chaperone.to_string());
    let paths = [
        (config::STATE_ROOT, state.path().to_owned()),
        (config::FAMILY_ROOT, write_family(&work)),
        (config::FAULTS_ROOT, faults),
        (config::IMAGE_ROOT, image_root(&work, mode)),
        (config::CH, PathBuf::from(CH_BINARY)),
        (config::CH_REMOTE, ch_remote),
        (config::GUEST_INIT, guest_init),
        (config::EGRESS_LOG, work.join("egress.jsonl")),
        // A start opens the mirror root only for a family with a
        // `repos.json` (decision CH69 rule 13).
        (config::MIRROR_ROOT, work.join("mirror")),
        (config::GIT, PathBuf::from(HOST_GIT)),
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
    let vmm = start(name, &env, &run);
    Booted {
        vmm,
        vsock: run.vm(&id).vsock_socket(),
        session,
        _written: written,
        _one: one,
    }
}

/// The `systemd-run` of a transient user service `unit` with `env`, up to
/// the `--` before its program. With `--pipe` the unit reads the stdin and
/// writes the stdout and stderr of `systemd-run`. The unit has the lines
/// of `systemd/creche-vmm.service` that a VM start reads: the run root
/// `RuntimeDirectory=vmm` (gap G5) and `NoNewPrivileges=yes`, so the
/// launch stage writes the exec attribute under no_new_privs (decision
/// CH55). It has no `LimitNOFILE=` line, as that unit has none (decision
/// CH67 rule 4), so the fd limits of the user manager apply (decision
/// CH69 rule 31).
pub fn transient(unit: &str, env: &[(&str, &OsStr)]) -> Command {
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
    run.arg("--");
    run
}
