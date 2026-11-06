//! The launch chain of the vmm under the slot profiles, on the KVM of the
//! runner (decisions CH8-1, CH8-3, CH51, CH55, CH58 rule 1, plan PR C15).
//! `bin/vm-smoke-setup.sh` installs CH at `slot_profile::CH_BINARY`, loads
//! `apparmor/cloud-hypervisor-slots.profile` and fails unless each
//! `creche-ch-slot-<k>` label is in enforce mode.
//!
//! Each CH here starts through the real chain of the vmm, with one pid
//! from end to end (fact 80):
//!
//! 1. `Spawner::launch` forks the launch step `launch::run`.
//! 2. The launch step sets the death signal and the core limit 0, then
//!    execs `/usr/bin/systemd-run --user --scope` with the scope limits.
//! 3. `systemd-run` execs the launch stage `stage::run`, which writes the
//!    exec attribute of the slot label, sets no_new_privs and applies the
//!    Landlock ruleset of the VM.
//! 4. The stage execs CH.
//!
//! The example `launch_chain` is the vmm binary of steps 1 and 3, as in
//! `tests/launch.rs`. `Launch::in_slot` gives the slot, and
//! `RunRoot::link_vm` makes `vm/<id>` a symlink to `slot/<k>` (PR D16).
//! The run root is the real `/run/user/<uid>/vmm` (decision CH20). The
//! state root is the default `~/.local/state/creche-vmm` (decision CH50
//! rule 11). The slot profiles name both. The user manager runs by linger,
//! and the setup script exports its `XDG_RUNTIME_DIR` (the C0 spike, fact
//! 79).
//!
//! The probes take the method of the C3 spike (branch spike/c3-slot-pool,
//! `spike/c3/probes.sh` and `sockprobe.py`). `--tpm socket=<path>` makes
//! CH connect to a path while it makes the VM, with no guest program. A
//! refused connect logs `Permission denied` in the output of CH. Each
//! control runs the same probe at a path that the label allows, and needs
//! the label of its slot and no `Permission denied`. The slot profiles map
//! no program but CH, so the signal and inet socket probes run on a test
//! thread that enters a slot label with `changeprofile`. That is the
//! spike's `aa-exec` with no exec.
//!
//! Each test uses its own slots and session ids, so the tests run at the
//! same time. A test stops at a slot dir or a session dir that it did not
//! make, and a drop removes only what it made.
#![cfg(target_os = "linux")]

mod kvm;

use std::fs::{self, OpenOptions};
use std::io::{BufRead, BufReader, ErrorKind, Write};
use std::net::{Ipv4Addr, TcpListener, UdpSocket};
use std::num::{NonZeroU32, NonZeroU64};
use std::os::unix::net::{UnixListener, UnixStream};
use std::os::unix::process::ExitStatusExt;
use std::path::{Path, PathBuf};
use std::process::{Command, ExitStatus, Stdio};
use std::sync::{Arc, Mutex, mpsc};
use std::thread::{self, sleep};
use std::time::{Duration, Instant};

use creche_vmm_confine::landlock::{HostPath, Start, VmFiles};
use creche_vmm_confine::launch::{Launch, Payload, Program};
use creche_vmm_confine::output::Sink;
use creche_vmm_confine::scope::Scope;
use creche_vmm_confine::slot_profile::{CH_BINARY, label};
use creche_vmm_confine::spawner::{Launched, Spawner};
use creche_vmm_store::layout::{RunRoot, SessionDir, Slot, StateRoot, VmRunDir, scope_unit};
use creche_wire::{HostPort, SessionId};

use kvm::{BOOT, Vm, booted, files};

/// The host programs of the ubuntu-24.04 runner. The vmm config passes
/// each one (plan PR L20).
const SYSTEMD_RUN: &str = "/usr/bin/systemd-run";

/// The busybox guest runs its `/init` from the initramfs.
const CMDLINE: &str = "console=ttyS0 rdinit=/init";

/// The size of each disk file. The guest mounts no disk.
const DISK_BYTES: u64 = 1 << 20;

/// The `MemoryMax` of each scope: 1 GiB, above the 256 MiB guest.
const MEMORY_MAX: u64 = 1 << 30;

/// The text of EACCES in the output of CH.
const DENIED: &str = "Permission denied";

/// The longest wait for a probe CH to exit. A CH that connected to a live
/// socket may wait for an answer that never comes, and is then killed.
const PROBE: Duration = Duration::from_secs(10);

/// The wait for the last lines of a CH after its exit.
const QUIET: Duration = Duration::from_millis(500);

/// The longest wait for a TCP connect that must not come.
const NO_CONNECT: Duration = Duration::from_secs(5);

/// The exec attribute of a thread, in the order the stage tries the exec
/// attribute (decision CH55).
const CURRENT: [&str; 2] = [
    "/proc/thread-self/attr/apparmor/current",
    "/proc/thread-self/attr/current",
];

// Each helper with `cfg(test)` may unwrap (rust/clippy.toml).

/// The example binary, as in `tests/common`. Cargo builds it next to the
/// test's `deps` dir.
#[cfg(test)]
fn example() -> PathBuf {
    let exe = std::env::current_exe().unwrap();
    let debug = exe.parent().and_then(|deps| deps.parent()).unwrap();
    debug.join("examples").join("launch_chain")
}

/// Fails if `path` exists. A test removes only the paths it made.
#[cfg(test)]
fn absent(path: &Path) {
    assert!(
        fs::symlink_metadata(path).is_err(),
        "{} exists: a vmm may be live, so the test stops",
        path.display()
    );
}

/// Polls `check` until it holds. Fails after `BOOT`.
#[cfg(test)]
fn until(what: &str, mut check: impl FnMut() -> bool) {
    let deadline = Instant::now() + BOOT;
    while !check() {
        assert!(Instant::now() < deadline, "no {what} in {BOOT:?}");
        sleep(Duration::from_millis(5));
    }
}

/// The state letter of `pid` in `/proc/<pid>/stat`, or `None` when the
/// pid is gone.
fn state(pid: u32) -> Option<char> {
    let stat = fs::read_to_string(format!("/proc/{pid}/stat")).ok()?;
    let (_, rest) = stat.rsplit_once(") ")?;
    rest.chars().next()
}

/// Whether `pid` runs and is not a zombie.
fn running(pid: u32) -> bool {
    state(pid).is_some_and(|letter| letter != 'Z')
}

/// The AppArmor label of `pid`, such as `creche-ch-slot-3 (enforce)`. A
/// zombie keeps its label until it is reaped.
fn label_of(pid: u32) -> Option<String> {
    let text = fs::read_to_string(format!("/proc/{pid}/attr/apparmor/current")).ok()?;
    Some(text.trim_end().to_owned())
}

/// The label of `slot` in enforce mode, as the kernel shows it.
fn enforced(slot: Slot) -> String {
    format!("{} (enforce)", label(slot))
}

/// The value of `field` in `/proc/<pid>/status`.
#[cfg(test)]
fn status_field(pid: &str, field: &str) -> String {
    let status = fs::read_to_string(format!("/proc/{pid}/status")).unwrap();
    let line = status
        .lines()
        .find_map(|line| line.strip_prefix(&format!("{field}:")))
        .unwrap();
    line.trim().to_owned()
}

/// The soft and the hard core limit of `pid`.
#[cfg(test)]
fn core_limits(pid: &str) -> (String, String) {
    let limits = fs::read_to_string(format!("/proc/{pid}/limits")).unwrap();
    let line = limits
        .lines()
        .find_map(|line| line.strip_prefix("Max core file size"))
        .unwrap();
    let mut words = line.split_whitespace();
    let soft = words.next().unwrap().to_owned();
    let hard = words.next().unwrap().to_owned();
    (soft, hard)
}

/// Makes a file of `DISK_BYTES` zeros at `path`.
#[cfg(test)]
fn zeros(path: &Path) {
    absent(path);
    fs::File::create(path).unwrap().set_len(DISK_BYTES).unwrap();
}

/// A launched CH, killed and reaped on drop, so a failed test leaves no
/// VM.
struct Ch(Launched);

impl Drop for Ch {
    fn drop(&mut self) {
        let _ = self.0.kill();
        let _ = self.0.wait();
    }
}

/// The vmm side of the chain: a spawner on the example, and its sink.
struct Vmm {
    spawner: Spawner,
    lines: Arc<Mutex<Vec<String>>>,
}

#[cfg(test)]
impl Vmm {
    fn new() -> Self {
        let lines = Arc::new(Mutex::new(Vec::new()));
        let kept = Arc::clone(&lines);
        let sink: Sink = Arc::new(move |line| kept.lock().unwrap().push(line.to_owned()));
        let spawner = Spawner::with_sink(example(), sink).unwrap();
        Self { spawner, lines }
    }

    fn launch(&self, launch: Launch) -> Ch {
        Ch(self.spawner.launch(launch).unwrap())
    }

    /// Every line of the sink so far.
    fn all_lines(&self) -> Vec<String> {
        self.lines.lock().unwrap().clone()
    }

    /// The lines of the sink with the tag of `id`.
    fn lines_of(&self, id: &SessionId) -> Vec<String> {
        let tag = format!("ch {id}");
        let lines = self.all_lines();
        lines
            .into_iter()
            .filter(|line| line.starts_with(&tag))
            .collect()
    }
}

/// One VM seat: a session id, its slot, its session dir and its run dir.
#[derive(Clone)]
struct Seat {
    id: SessionId,
    slot: Slot,
    session: SessionDir,
    run: VmRunDir,
}

/// The files of one test under the two roots. A drop removes them.
struct Rig {
    run: RunRoot,
    state: StateRoot,
    kernel: PathBuf,
    initramfs: PathBuf,
    root: PathBuf,
    links: Vec<SessionId>,
    dirs: Vec<PathBuf>,
    files: Vec<PathBuf>,
}

#[cfg(test)]
impl Rig {
    /// The store files of the test `name`: the kernel, the initramfs and a
    /// root disk. The profiles match `store/*`, so a name of the test
    /// stands in for the digest name of the store (fact 56).
    fn new(name: &str) -> Self {
        let uid = rustix::process::getuid().as_raw();
        let runtime = format!("/run/user/{uid}");
        assert_eq!(
            std::env::var("XDG_RUNTIME_DIR").ok().as_deref(),
            Some(runtime.as_str()),
            "systemd-run --user needs XDG_RUNTIME_DIR: bin/vm-smoke-setup.sh exports it"
        );
        let home = PathBuf::from(std::env::var_os("HOME").unwrap());
        let state = StateRoot::new(home.join(".local/state/creche-vmm")).unwrap();
        let run = RunRoot::new(PathBuf::from(format!("{runtime}/vmm"))).unwrap();
        let store = state.store_dir();
        fs::create_dir_all(&store).unwrap();
        fs::create_dir_all(state.sessions_dir()).unwrap();

        let files = files();
        let kernel = store.join(format!("kvm-slot-{name}-vmlinux"));
        let initramfs = store.join(format!("kvm-slot-{name}-initramfs"));
        let root = store.join(format!("kvm-slot-{name}-root"));
        for (from, to) in [("vmlinux", &kernel), ("initramfs.cpio", &initramfs)] {
            absent(to);
            fs::copy(files.join(from), to).unwrap();
        }
        zeros(&root);
        let made = vec![kernel.clone(), initramfs.clone(), root.clone()];
        Self {
            run,
            state,
            kernel,
            initramfs,
            root,
            links: Vec::new(),
            dirs: Vec::new(),
            files: made,
        }
    }

    /// A session dir with its disk and its snapshot dir, which the ruleset
    /// opens.
    fn session(&mut self, id: &SessionId) -> SessionDir {
        let session = self.state.session(id);
        absent(session.path());
        fs::create_dir_all(session.snapshots()).unwrap();
        self.dirs.push(session.path().to_owned());
        zeros(&session.disk());
        session
    }

    /// A seat of `id` in `slot`: its session and the link `vm/<id>` to
    /// `slot/<k>`, made by `RunRoot::link_vm` as lifecycle does.
    fn seat(&mut self, id: &str, slot: u16) -> Seat {
        let id: SessionId = id.parse().unwrap();
        let slot = Slot::new(slot).unwrap();
        let session = self.session(&id);
        absent(&self.run.slot(slot));
        absent(self.run.vm(&id).path());
        self.dirs.push(self.run.slot(slot));
        self.links.push(id.clone());
        let run = self.run.link_vm(&id, slot).unwrap();
        Seat {
            id,
            slot,
            session,
            run,
        }
    }

    /// A seat of `id` in the slot of `host`, with its own session and the
    /// run dir of `host`. It makes no link. A probe in it runs under the
    /// label of `host` and makes no file in the run dir.
    fn guest(&mut self, id: &str, host: &Seat) -> Seat {
        let id: SessionId = id.parse().unwrap();
        let session = self.session(&id);
        Seat {
            id,
            slot: host.slot,
            session,
            run: host.run.clone(),
        }
    }

    /// The launch of CH with `args` in `seat`, whose ruleset names `root`
    /// as the root disk.
    fn launch(&self, seat: &Seat, root: &Path, args: Vec<String>) -> Launch {
        let host = |path: &Path| HostPath::new(path.to_owned()).unwrap();
        let start = Start::Boot {
            kernel: host(&self.kernel),
            initramfs: Some(host(&self.initramfs)),
        };
        let files = VmFiles::new(start, host(root), &seat.session, &seat.run).unwrap();
        let scope = Scope::new(
            seat.id.clone(),
            NonZeroU64::new(MEMORY_MAX).unwrap(),
            NonZeroU32::new(100).unwrap(),
        );
        let ch = Program::new(PathBuf::from(CH_BINARY)).unwrap();
        let systemd_run = Program::new(PathBuf::from(SYSTEMD_RUN)).unwrap();
        Launch::new(systemd_run, scope, Payload::new(ch, args), files).in_slot(seat.slot)
    }

    /// The kernel and the guest of a CH with no disk and no socket.
    fn guest_args(&self) -> Vec<String> {
        let mut args = vec![
            "--kernel".to_owned(),
            self.kernel.display().to_string(),
            "--initramfs".to_owned(),
            self.initramfs.display().to_string(),
        ];
        args.extend(
            [
                "--cmdline",
                CMDLINE,
                "--cpus",
                "boot=1",
                "--memory",
                "size=256M",
            ]
            .map(str::to_owned),
        );
        args
    }

    /// The boot of the busybox guest in `seat`, as PR C7 renders it: the
    /// API socket, the root disk `readonly=on`, the session disk, the
    /// vsock and the serial file.
    fn boot_args(&self, seat: &Seat, root: &Path) -> Vec<String> {
        let mut args = vec![
            "--api-socket".to_owned(),
            format!("path={}", seat.run.api_socket().display()),
        ];
        args.extend(self.guest_args());
        args.extend([
            "--disk".to_owned(),
            format!("path={},readonly=on,image_type=raw", root.display()),
            "--disk".to_owned(),
            format!("path={},image_type=raw", seat.session.disk().display()),
            "--vsock".to_owned(),
            format!("cid=3,socket={}", seat.run.vsock_socket().display()),
            "--serial".to_owned(),
            format!("file={}", seat.run.serial_log().display()),
            "--console".to_owned(),
            "off".to_owned(),
        ]);
        args
    }

    /// The boot of `seat` with the root disk of the rig.
    fn boot(&self, seat: &Seat) -> Launch {
        self.launch(seat, &self.root, self.boot_args(seat, &self.root))
    }

    /// The connect probe of the C3 spike: CH connects to `target` while
    /// it makes the VM. It writes no file in its run dir.
    fn tpm(&self, seat: &Seat, target: &Path) -> Launch {
        let mut args = self.guest_args();
        args.extend([
            "--serial".to_owned(),
            "off".to_owned(),
            "--console".to_owned(),
            "off".to_owned(),
            "--tpm".to_owned(),
            format!("socket={}", target.display()),
        ]);
        self.launch(seat, &self.root, args)
    }
}

impl Drop for Rig {
    fn drop(&mut self) {
        for id in &self.links {
            let _ = self.run.unlink_vm(id);
        }
        for dir in &self.dirs {
            let _ = fs::remove_dir_all(dir);
        }
        for file in &self.files {
            let _ = fs::remove_file(file);
        }
    }
}

/// Waits for the marker of the guest in `seat`.
#[cfg(test)]
fn wait_booted(vmm: &Vmm, ch: &Ch, seat: &Seat) {
    let pid = ch.0.id();
    until("boot marker", || {
        assert!(
            running(pid),
            "CH exited before the marker: {:?}",
            vmm.lines_of(&seat.id)
        );
        booted(&seat.run.serial_log())
    });
}

/// The outcome of one connect probe.
#[derive(Debug)]
struct Probe {
    label: Option<String>,
    connected: bool,
    status: ExitStatus,
    lines: Vec<String>,
}

impl Probe {
    /// Whether CH logged EACCES.
    fn denied(&self) -> bool {
        self.lines.iter().any(|line| line.contains(DENIED))
    }
}

/// A listener at a new path, which does not block on accept. A drop
/// removes the path.
struct Listener(UnixListener, PathBuf);

impl Drop for Listener {
    fn drop(&mut self) {
        let _ = fs::remove_file(&self.1);
    }
}

#[cfg(test)]
fn listen(path: &Path) -> Listener {
    absent(path);
    let listener = UnixListener::bind(path).unwrap();
    listener.set_nonblocking(true).unwrap();
    Listener(listener, path.to_owned())
}

/// Whether a connect waits on `listener`. Each one is accepted and
/// closed, so the TPM client of CH fails and CH exits.
#[cfg(test)]
fn accepted(listener: Option<&Listener>) -> bool {
    listener.is_some_and(|listener| match listener.0.accept() {
        Ok(_) => true,
        Err(error) if error.kind() == ErrorKind::WouldBlock => false,
        Err(error) => panic!("accept: {error}"),
    })
}

/// Runs `launch` and waits for CH to exit, or kills it after `PROBE`.
/// The label comes from the zombie, which keeps it until the reap.
#[cfg(test)]
fn probe(vmm: &Vmm, launch: Launch, listener: Option<&Listener>) -> Probe {
    let id = launch.scope().id().clone();
    let mut ch = vmm.launch(launch);
    let pid = ch.0.id();
    let deadline = Instant::now() + PROBE;
    let mut connected = false;
    let mut seen = None;
    while running(pid) && Instant::now() < deadline {
        seen = label_of(pid).filter(|label| label != "unconfined").or(seen);
        connected |= accepted(listener);
        sleep(Duration::from_millis(1));
    }
    if running(pid) {
        ch.0.kill().unwrap();
        until("probe exit after the kill", || !running(pid));
    }
    let label = label_of(pid).or(seen);
    // A connect that CH made just before it exited waits in the backlog.
    connected |= accepted(listener);
    let status = ch.0.wait().unwrap();
    sleep(QUIET);
    Probe {
        label,
        connected,
        status,
        lines: vmm.lines_of(&id),
    }
}

/// Asserts that `probe` ran CH under the label of `slot` and was refused.
#[cfg(test)]
fn assert_refused(probe: &Probe, slot: Slot, what: &str) {
    assert_eq!(
        probe.label.as_deref(),
        Some(enforced(slot).as_str()),
        "{what}: {probe:?}"
    );
    assert!(!probe.connected, "{what}: CH connected: {probe:?}");
    assert!(!probe.status.success(), "{what}: {probe:?}");
    assert!(probe.denied(), "{what}: no {DENIED}: {probe:?}");
}

/// Asserts that `probe` ran CH under the label of `slot` with no EACCES.
#[cfg(test)]
fn assert_allowed(probe: &Probe, slot: Slot, what: &str) {
    assert_eq!(
        probe.label.as_deref(),
        Some(enforced(slot).as_str()),
        "{what}: {probe:?}"
    );
    assert!(!probe.denied(), "{what}: {probe:?}");
}

/// Runs `probe` on a new thread under the label of `slot`. The thread
/// writes `changeprofile <label>` to its own attribute, so only it changes
/// label. The test reads the label of the thread before the probe runs.
#[cfg(test)]
fn as_label<T: Send + 'static>(slot: Slot, probe: impl FnOnce() -> T + Send + 'static) -> T {
    let (entered, entry) = mpsc::channel();
    let (go, start) = mpsc::channel::<()>();
    let worker = thread::spawn(move || {
        let tid = rustix::thread::gettid().as_raw_nonzero().get();
        let result = enter(slot);
        let ok = result.is_ok();
        let _ = entered.send((tid, result));
        if !ok {
            return None;
        }
        start.recv().ok()?;
        Some(probe())
    });
    let (tid, result) = entry.recv().unwrap();
    result.unwrap();
    let attr = format!("/proc/self/task/{tid}/attr/apparmor/current");
    assert_eq!(fs::read_to_string(attr).unwrap().trim_end(), enforced(slot));
    go.send(()).unwrap();
    worker.join().unwrap().unwrap()
}

/// Writes `changeprofile <label>` to the attribute of this thread.
fn enter(slot: Slot) -> std::io::Result<()> {
    let value = format!("changeprofile {}", label(slot));
    for path in CURRENT {
        match OpenOptions::new().write(true).open(path) {
            Ok(mut file) => return file.write_all(value.as_bytes()),
            Err(error) if error.kind() == ErrorKind::NotFound => {}
            Err(error) => return Err(error),
        }
    }
    Err(ErrorKind::NotFound.into())
}

/// The kinds of the three inet sockets and of one Unix stream socket that
/// the calling thread opens.
fn socket_kinds() -> [Result<(), ErrorKind>; 4] {
    let kind = |result: std::io::Result<()>| result.map_err(|error| error.kind());
    [
        kind(TcpListener::bind((Ipv4Addr::LOCALHOST, 0)).map(drop)),
        kind(UdpSocket::bind((Ipv4Addr::LOCALHOST, 0)).map(drop)),
        kind(UdpSocket::bind("[::1]:0").map(drop)),
        kind(UnixStream::pair().map(drop)),
    ]
}

#[test]
#[ignore = "needs /dev/kvm and bin/vm-smoke-setup.sh: the job kvm runs it"]
fn kvm_ch_boots_under_its_slot_label() {
    let mut rig = Rig::new("boot");
    // The last slot has the longest name (spike T6, the far attach).
    let seat = rig.seat("0123abcd-0000-4000-8000-0000000c1500", Slot::LAST.number());
    let vmm = Vmm::new();
    let ch = vmm.launch(rig.boot(&seat));
    wait_booted(&vmm, &ch, &seat);

    // CH booted with the root disk read only and the session disk read and
    // write, so the ruleset grants both (decisions CH8-3, CH8-8).
    let pid = ch.0.id().to_string();
    assert_eq!(
        fs::read_link(format!("/proc/{pid}/exe")).unwrap(),
        Path::new(CH_BINARY)
    );
    assert_eq!(label_of(ch.0.id()), Some(enforced(seat.slot)));
    assert_eq!(status_field(&pid, "NoNewPrivs"), "1");
    assert_eq!(core_limits(&pid), ("0".to_owned(), "0".to_owned()));
    let cgroup = fs::read_to_string(format!("/proc/{pid}/cgroup")).unwrap();
    let scope = format!("/{}.scope", scope_unit(&seat.id));
    assert!(cgroup.trim_end().ends_with(&scope), "{cgroup}");
    let slot_dir = rig.run.slot(seat.slot);
    for socket in ["api.sock", "vsock.sock"] {
        assert!(slot_dir.join(socket).exists(), "no {socket} in slot dir");
    }
    let ping = Command::new(files().join("ch-remote"))
        .arg("--api-socket")
        .arg(seat.run.api_socket())
        .arg("ping")
        .output()
        .unwrap();
    assert!(ping.status.success(), "ping: {ping:?}");

    // The control: the same binary started with no chain runs unconfined,
    // with no_new_privs clear and the core limit of the test, because no
    // slot profile has an attachment path.
    let work = files().join(format!("kvm-slot-control-{}", std::process::id()));
    let _ = fs::remove_dir_all(&work);
    fs::create_dir(&work).unwrap();
    let api = work.join("api.sock");
    let plain = Vm(Command::new(CH_BINARY)
        .arg("--api-socket")
        .arg(format!("path={}", api.display()))
        .stdin(Stdio::null())
        .spawn()
        .unwrap());
    until("API socket of the plain CH", || api.exists());
    let plain_pid = plain.0.id().to_string();
    assert_eq!(label_of(plain.0.id()).as_deref(), Some("unconfined"));
    assert_eq!(status_field(&plain_pid, "NoNewPrivs"), "0");
    let (_, hard) = core_limits(&plain_pid);
    assert_ne!(
        hard, "0",
        "the test itself has core limit 0, so the check shows nothing"
    );
    drop(plain);
    fs::remove_dir_all(&work).unwrap();
}

/// The Landlock ruleset is in force under the label (decisions CH8-3,
/// CH8-8, CH30). Each refused path is one that the slot profile allows,
/// `sessions/*/disk.ext4`, so the refusal comes from the ruleset. The
/// first case puts the root disk of the VM at such a path, so only the
/// read-only rule of the ruleset stops a write.
#[test]
#[ignore = "needs /dev/kvm and bin/vm-smoke-setup.sh: the job kvm runs it"]
fn kvm_the_ruleset_holds_ch_to_its_vm_files() {
    let mut rig = Rig::new("ruleset");
    let other = "0123abcd-0000-4000-8000-0000000c1510".parse().unwrap();
    let root = rig.session(&other).disk();
    let outside = rig
        .session(&"0123abcd-0000-4000-8000-0000000c1511".parse().unwrap())
        .disk();
    let vmm = Vmm::new();

    // A write open of the root disk.
    let seat = rig.seat("0123abcd-0000-4000-8000-0000000c1512", 2);
    let mut args = rig.boot_args(&seat, &root);
    let read_only = format!("path={},readonly=on,image_type=raw", root.display());
    let position = args.iter().position(|arg| *arg == read_only).unwrap();
    args[position] = format!("path={},image_type=raw", root.display());
    let write_root = probe(&vmm, rig.launch(&seat, &root, args), None);
    assert_refused(&write_root, seat.slot, "a write of the root disk");

    // A read of a disk outside the VM files.
    let seat = rig.seat("0123abcd-0000-4000-8000-0000000c1513", 3);
    let mut args = rig.boot_args(&seat, &root);
    args.extend([
        "--disk".to_owned(),
        format!("path={},readonly=on,image_type=raw", outside.display()),
    ]);
    let read_outside = probe(&vmm, rig.launch(&seat, &root, args), None);
    assert_refused(&read_outside, seat.slot, "a read outside the VM files");

    // The control: the same root disk read only, and the session disk.
    let seat = rig.seat("0123abcd-0000-4000-8000-0000000c1514", 4);
    let ch = vmm.launch(rig.launch(&seat, &root, rig.boot_args(&seat, &root)));
    wait_booted(&vmm, &ch, &seat);
    assert_eq!(label_of(ch.0.id()), Some(enforced(seat.slot)));
}

/// A TCP connect of CH under the chain is refused (decisions CH8-1,
/// CH8-3). `ch-remote send-migration` makes CH connect to a TCP address.
/// The control sends the same call to a CH that runs with no chain, and
/// its connect arrives.
#[test]
#[ignore = "needs /dev/kvm and bin/vm-smoke-setup.sh: the job kvm runs it"]
fn kvm_ch_under_the_chain_makes_no_tcp_connect() {
    let mut rig = Rig::new("tcp");
    let seat = rig.seat("0123abcd-0000-4000-8000-0000000c1520", 5);
    let vmm = Vmm::new();
    let ch = vmm.launch(rig.boot(&seat));
    wait_booted(&vmm, &ch, &seat);

    let refused = migrate(&seat.run.api_socket(), NO_CONNECT);
    assert!(!refused.0, "CH under the chain made a TCP connect");
    let lines = vmm.lines_of(&seat.id);
    let denied = |text: &str| text.contains(DENIED);
    assert!(
        denied(&refused.1) || lines.iter().any(|line| denied(line)),
        "no {DENIED}: ch-remote {}, sink {lines:?}",
        refused.1
    );

    // The control: the same binary with no chain, so with no label and no
    // ruleset.
    let work = files().join(format!("kvm-slot-tcp-{}", std::process::id()));
    let _ = fs::remove_dir_all(&work);
    fs::create_dir(&work).unwrap();
    let serial = work.join("serial.log");
    let mut args = vec![
        "--api-socket".to_owned(),
        format!("path={}", work.join("api.sock").display()),
    ];
    args.extend(rig.guest_args());
    args.extend([
        "--serial".to_owned(),
        format!("file={}", serial.display()),
        "--console".to_owned(),
        "off".to_owned(),
    ]);
    let plain = Vm(Command::new(CH_BINARY)
        .args(args)
        .stdin(Stdio::null())
        .spawn()
        .unwrap());
    until("boot marker of the plain CH", || booted(&serial));
    let control = migrate(&work.join("api.sock"), BOOT);
    assert!(control.0, "the plain CH made no TCP connect: {}", control.1);
    drop(plain);
    fs::remove_dir_all(&work).unwrap();
}

/// Starts `ch-remote send-migration` to a new TCP listener and waits up
/// to `wait` for a connect. It gives whether one came and the output of
/// `ch-remote`. The wait does not end when `ch-remote` exits, because CH
/// may run the migration after its answer. Each connect is accepted and
/// closed, so the migration fails.
#[cfg(test)]
fn migrate(api: &Path, wait: Duration) -> (bool, String) {
    let listener = TcpListener::bind((Ipv4Addr::LOCALHOST, 0)).unwrap();
    listener.set_nonblocking(true).unwrap();
    let port = listener.local_addr().unwrap().port();
    let mut remote = Command::new(files().join("ch-remote"))
        .arg("--api-socket")
        .arg(api)
        .arg("send-migration")
        .arg(format!("destination_url=tcp:127.0.0.1:{port}"))
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    let deadline = Instant::now() + wait;
    let mut connected = false;
    while Instant::now() < deadline && !connected {
        match listener.accept() {
            Ok(_) => connected = true,
            Err(error) if error.kind() == ErrorKind::WouldBlock => {}
            Err(error) => panic!("accept: {error}"),
        }
        sleep(Duration::from_millis(5));
    }
    let _ = remote.kill();
    let output = remote.wait_with_output().unwrap();
    let mut text = String::from_utf8_lossy(&output.stdout).into_owned();
    text.push_str(&String::from_utf8_lossy(&output.stderr));
    (connected, text)
}

/// The slot profile denies every inet socket, TCP and UDP, which Landlock
/// ABI 4 does not cover for UDP (fact 77, decision CH8-1). A Unix stream
/// socket still opens under the label. The control opens all four on a
/// thread with no label.
#[test]
#[ignore = "needs /dev/kvm and bin/vm-smoke-setup.sh: the job kvm runs it"]
fn kvm_a_slot_label_opens_no_inet_socket() {
    let slot = Slot::new(6).unwrap();
    let denied = Err(ErrorKind::PermissionDenied);
    assert_eq!(
        as_label(slot, socket_kinds),
        [denied, denied, denied, Ok(())]
    );
    let control = thread::spawn(socket_kinds).join().unwrap();
    assert_eq!(control, [Ok(()); 4]);
}

/// The output of each CH reaches the sink of the vmm, tagged with its own
/// session id (decision CH50 rule 12). Two CHs on one spawner fail with
/// two different errors. Each error comes with its own id only.
#[test]
#[ignore = "needs /dev/kvm and bin/vm-smoke-setup.sh: the job kvm runs it"]
fn kvm_ch_output_reaches_the_sink_with_its_id() {
    let mut rig = Rig::new("output");
    let one = rig.seat("0123abcd-0000-4000-8000-0000000c1530", 7);
    let two = rig.seat("0123abcd-0000-4000-8000-0000000c1531", 8);
    let vmm = Vmm::new();
    let uid = rustix::process::getuid().as_raw();
    let bus = PathBuf::from(format!("/run/user/{uid}/bus"));
    let missing = two.run.path().join("no-such-socket");
    let first = probe(&vmm, rig.tpm(&one, &bus), None);
    let second = probe(&vmm, rig.tpm(&two, &missing), None);

    assert!(first.denied() && !second.denied(), "{first:?} {second:?}");

    // Each error text of CH comes only with the tag of its own CH.
    let gone = "No such file or directory";
    let lines = vmm.all_lines();
    for (text, seat) in [(DENIED, &one), (gone, &two)] {
        let tag = format!("ch {}: ", seat.id);
        let with: Vec<&String> = lines.iter().filter(|line| line.contains(text)).collect();
        assert!(!with.is_empty(), "no {text}: {lines:?}");
        assert!(with.iter().all(|line| line.starts_with(&tag)), "{lines:?}");
    }
}

/// A drop of the spawner ends its thread, and the kernel kills CH by its
/// death signal (decision CH19 (4), facts 76 and 80). The control: CH
/// still runs after its boot while the spawner lives.
#[test]
#[ignore = "needs /dev/kvm and bin/vm-smoke-setup.sh: the job kvm runs it"]
fn kvm_ch_dies_with_the_spawner_thread() {
    let mut rig = Rig::new("spawner");
    let seat = rig.seat("0123abcd-0000-4000-8000-0000000c1540", 9);
    let vmm = Vmm::new();
    let mut ch = vmm.launch(rig.boot(&seat));
    wait_booted(&vmm, &ch, &seat);
    assert_eq!(label_of(ch.0.id()), Some(enforced(seat.slot)));
    sleep(Duration::from_secs(1));
    assert!(running(ch.0.id()), "CH exited while the spawner lived");

    drop(vmm);
    let status = ch.0.wait().unwrap();
    assert_eq!(status.signal(), Some(9), "{status}");
}

/// A SIGKILL of the vmm kills CH by its death signal, and the scope goes
/// with it (decision CH19 (4), fact 80). The example in its role `hold`
/// is the vmm. The control: CH still runs after its boot while the vmm
/// lives.
#[test]
#[ignore = "needs /dev/kvm and bin/vm-smoke-setup.sh: the job kvm runs it"]
fn kvm_ch_dies_with_a_killed_vmm() {
    let mut rig = Rig::new("vmm-kill");
    let seat = rig.seat("0123abcd-0000-4000-8000-0000000c1550", 10);
    let mut vmm = Vm(Command::new(example())
        .arg("hold")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .spawn()
        .unwrap());
    let plan = serde_json::to_vec(&rig.boot(&seat)).unwrap();
    vmm.0.stdin.take().unwrap().write_all(&plan).unwrap();
    // The chain writes to the output pipe of the spawner, so the one line
    // on the stdout of the vmm is the pid.
    let mut line = String::new();
    BufReader::new(vmm.0.stdout.take().unwrap())
        .read_line(&mut line)
        .unwrap();
    let pid: u32 = line.trim().parse().unwrap();
    until("boot marker", || {
        assert!(running(pid), "CH exited before the marker");
        booted(&seat.run.serial_log())
    });
    assert_eq!(label_of(pid), Some(enforced(seat.slot)));
    sleep(Duration::from_secs(1));
    assert!(running(pid), "CH exited while the vmm lived");
    let cgroup = format!("/proc/{pid}/cgroup");
    let scope = fs::read_to_string(&cgroup).unwrap();
    let scope_dir = PathBuf::from(format!(
        "/sys/fs/cgroup{}",
        scope.trim_end().trim_start_matches("0::")
    ));
    assert!(scope_dir.exists(), "no scope dir {}", scope_dir.display());

    vmm.0.kill().unwrap();
    vmm.0.wait().unwrap();
    until("CH death after the vmm kill", || !running(pid));
    until("scope removal", || !scope_dir.exists());
}

/// `Launched::terminate` sends SIGTERM, which the label lets the vmm send
/// (`signal (receive) peer=unconfined`). CH handles it and exits with a
/// code (decision CH19 (3)). The control: `Launched::kill` ends a second
/// CH by SIGKILL, which no handler sees.
#[test]
#[ignore = "needs /dev/kvm and bin/vm-smoke-setup.sh: the job kvm runs it"]
fn kvm_terminate_sends_sigterm_to_ch() {
    let mut rig = Rig::new("terminate");
    let term = rig.seat("0123abcd-0000-4000-8000-0000000c1560", 11);
    let kill = rig.seat("0123abcd-0000-4000-8000-0000000c1561", 12);
    let vmm = Vmm::new();
    let mut termed = vmm.launch(rig.boot(&term));
    let mut killed = vmm.launch(rig.boot(&kill));
    wait_booted(&vmm, &termed, &term);
    wait_booted(&vmm, &killed, &kill);

    termed.0.terminate().unwrap();
    let status = termed.0.wait().unwrap();
    assert!(
        status.code().is_some() && status.signal().is_none(),
        "{status}"
    );
    // A second call finds CH reaped and sends no signal.
    termed.0.terminate().unwrap();

    killed.0.kill().unwrap();
    let status = killed.0.wait().unwrap();
    assert_eq!(status.signal(), Some(9), "{status}");
}

/// A CH in one slot cannot connect to a host listener `vsock.sock_<port>`
/// of another slot (decisions CH24, CH51, spike T3). The control: the same
/// probe connects to the same listener name in its own slot.
#[test]
#[ignore = "needs /dev/kvm and bin/vm-smoke-setup.sh: the job kvm runs it"]
fn kvm_ch_connects_only_to_listeners_of_its_slot() {
    let mut rig = Rig::new("listener");
    let own = rig.seat("0123abcd-0000-4000-8000-0000000c1570", 13);
    let other = rig.seat("0123abcd-0000-4000-8000-0000000c1571", 14);
    let vmm = Vmm::new();

    let path = other.run.host_listener(HostPort::Chaperone);
    let listener = listen(&path);
    let cross = probe(&vmm, rig.tpm(&own, &path), Some(&listener));
    drop(listener);
    assert_refused(&cross, own.slot, "a listener of another slot");

    let path = own.run.host_listener(HostPort::Chaperone);
    let listener = listen(&path);
    let control = probe(&vmm, rig.tpm(&own, &path), Some(&listener));
    assert_allowed(&control, own.slot, "a listener of its own slot");
    assert!(control.connected, "no connect: {control:?}");
}

/// A CH in one slot cannot connect to the `api.sock` or the `vsock.sock`
/// of a live CH in another slot (decision CH51, spike T4), and the live VM
/// still answers after. The control: the same probe under the label of
/// the live VM's slot gets no EACCES at the same paths.
#[test]
#[ignore = "needs /dev/kvm and bin/vm-smoke-setup.sh: the job kvm runs it"]
fn kvm_ch_cannot_reach_a_live_ch_in_another_slot() {
    let mut rig = Rig::new("live");
    let prober = rig.seat("0123abcd-0000-4000-8000-0000000c1580", 15);
    let live = rig.seat("0123abcd-0000-4000-8000-0000000c1581", 16);
    let vmm = Vmm::new();
    let ch = vmm.launch(rig.boot(&live));
    wait_booted(&vmm, &ch, &live);

    let targets = [live.run.api_socket(), live.run.vsock_socket()];
    for target in &targets {
        let cross = probe(&vmm, rig.tpm(&prober, target), None);
        assert_refused(&cross, prober.slot, &target.display().to_string());
    }
    let ping = Command::new(files().join("ch-remote"))
        .arg("--api-socket")
        .arg(live.run.api_socket())
        .arg("ping")
        .output()
        .unwrap();
    assert!(
        ping.status.success(),
        "the live VM no longer answers: {ping:?}"
    );

    let ids = [
        "0123abcd-0000-4000-8000-0000000c1582",
        "0123abcd-0000-4000-8000-0000000c1583",
    ];
    for (target, id) in targets.iter().zip(ids) {
        let guest = rig.guest(id, &live);
        let control = probe(&vmm, rig.tpm(&guest, target), None);
        assert_allowed(&control, live.slot, &target.display().to_string());
    }
}

/// A CH in a slot cannot connect to `vmm.sock` in the run root (decisions
/// CH20, CH24, spike T0) or to the user bus (decision CH8-1). The control:
/// the same probe connects to a host listener of its own slot.
#[test]
#[ignore = "needs /dev/kvm and bin/vm-smoke-setup.sh: the job kvm runs it"]
fn kvm_ch_cannot_connect_to_vmm_sock_or_the_user_bus() {
    let mut rig = Rig::new("host-sockets");
    let seat = rig.seat("0123abcd-0000-4000-8000-0000000c1590", 17);
    let vmm = Vmm::new();

    let path = rig.run.vmm_socket();
    let listener = listen(&path);
    let to_vmm = probe(&vmm, rig.tpm(&seat, &path), Some(&listener));
    drop(listener);
    assert_refused(&to_vmm, seat.slot, "vmm.sock");

    let uid = rustix::process::getuid().as_raw();
    let bus = PathBuf::from(format!("/run/user/{uid}/bus"));
    let to_bus = probe(&vmm, rig.tpm(&seat, &bus), None);
    assert_refused(&to_bus, seat.slot, "the user bus");

    let path = seat.run.host_listener(HostPort::Chaperone);
    let listener = listen(&path);
    let control = probe(&vmm, rig.tpm(&seat, &path), Some(&listener));
    assert_allowed(&control, seat.slot, "a listener of its own slot");
    assert!(control.connected, "no connect: {control:?}");
}

/// A process under the label of one slot cannot signal the CH of another
/// slot (decision CH51, spike T5). The control: under the label of the
/// CH's own slot the same SIGKILL goes through, and CH dies of it.
#[test]
#[ignore = "needs /dev/kvm and bin/vm-smoke-setup.sh: the job kvm runs it"]
fn kvm_a_slot_cannot_signal_the_ch_of_another_slot() {
    let mut rig = Rig::new("signal");
    let seat = rig.seat("0123abcd-0000-4000-8000-0000000c15a0", 19);
    let vmm = Vmm::new();
    let mut ch = vmm.launch(rig.boot(&seat));
    wait_booted(&vmm, &ch, &seat);
    let target = rustix::process::Pid::from_raw(i32::try_from(ch.0.id()).unwrap()).unwrap();
    let kill = move || {
        rustix::process::kill_process(target, rustix::process::Signal::KILL)
            .map_err(|errno| std::io::Error::from(errno).kind())
    };

    let other = Slot::new(18).unwrap();
    assert_eq!(as_label(other, kill), Err(ErrorKind::PermissionDenied));
    sleep(Duration::from_millis(200));
    assert!(running(ch.0.id()), "CH died of a signal from another slot");

    assert_eq!(as_label(seat.slot, kill), Ok(()));
    let status = ch.0.wait().unwrap();
    assert_eq!(status.signal(), Some(9), "{status}");
}
