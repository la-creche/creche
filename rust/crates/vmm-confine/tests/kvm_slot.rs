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
//!    execs `/usr/bin/systemd-run --user --scope` with the scope limits
//!    and `BindsTo=` and `Before=` the vmm unit (decision CH61 rule 2).
//! 3. `systemd-run` execs the launch stage `stage::run`, which writes the
//!    exec attribute of the slot label, sets no_new_privs and applies the
//!    Landlock ruleset of the VM.
//! 4. The stage execs CH.
//!
//! The example `launch_chain` is the vmm binary of steps 1 and 3, as in
//! `tests/launch.rs`. Each test has its own vmm unit, a transient user
//! service of `tests/stand_in` (decisions CH60 rule 1, CH61). Most tests
//! run the spawner in the test and the service `/bin/sleep infinity` as
//! the unit that the scopes bind to. The hold stop, kill and restart
//! tests run the example in its role `hold` as the service, so the
//! spawner runs in the unit, as in the vmm. `Launch::in_slot` gives the slot, and
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
//! Each test uses its own slots and session ids. The tests run at the
//! same time, but one stand-in runs at a time (`tests/stand_in`), because
//! its `ExecStopPost=` step kills every VM scope. A test stops at a slot dir or a session dir that it did not
//! make, and a drop removes only what it made.
#![cfg(target_os = "linux")]

mod kvm;
mod plain_ch;
mod scope_guard;
mod stand_in;

use std::fs::{self, OpenOptions};
use std::io::{ErrorKind, Write};
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
use creche_vmm_confine::vmm_unit::VmmUnit;
use creche_vmm_store::layout::{RunRoot, SessionDir, Slot, StateRoot, VmRunDir, scope_unit};
use creche_wire::{HostPort, SessionId};

use kvm::{BOOT, booted, files};
use plain_ch::Vm;
use scope_guard::ScopeGuard;
use stand_in::{StandIn, kill_scopes, state_of, until_state};

/// The host programs of the ubuntu-24.04 runner. The vmm config passes
/// each one (plan PR L20).
const SYSTEMD_RUN: &str = "/usr/bin/systemd-run";
const SYSTEMCTL: &str = "/usr/bin/systemctl";

/// The busybox guest runs its `/init` from the initramfs.
const CMDLINE: &str = "console=ttyS0 rdinit=/init";

/// The size of each disk file. The guest mounts no disk.
const DISK_BYTES: u64 = 1 << 20;

/// The `MemoryMax` of each scope: 1 GiB, above the 256 MiB guest.
const MEMORY_MAX: u64 = 1 << 30;

/// The text of EACCES in the output of CH.
const DENIED: &str = "Permission denied";

/// The text of an EACCES error of Rust's `io::Error`, as CH logs it.
const EACCES: &str = "Permission denied (os error 13)";

/// The log line of CH v54 for any failed migration. It holds the whole
/// error chain (`check_migration` in `vmm/src/lib.rs`).
const MIGRATION_FAILED: &str = "Migration failed";

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

/// The vmm side of the chain: a spawner on the example, its sink, and
/// the vmm unit of the rig that each scope binds to. The spawner drops
/// first. The `ExecStopPost=` step of the stop of the unit then kills
/// each VM scope (decision CH61 rule 1).
struct Vmm {
    spawner: Spawner,
    lines: Arc<Mutex<Vec<String>>>,
    unit: StandIn,
}

#[cfg(test)]
impl Vmm {
    fn new(rig: &Rig) -> Self {
        Self::start(rig, None)
    }

    /// The vmm side whose unit stop waits for its gate, as the shutdown of
    /// the vmm runs before the vmm exits.
    fn gated(rig: &Rig) -> Self {
        Self::start(rig, Some(&example()))
    }

    /// The `/bin/sleep infinity` stand-in, gated by `gated_by`.
    fn start(rig: &Rig, gated_by: Option<&Path>) -> Self {
        let unit = StandIn::start(&rig.unit, gated_by, &["/bin/sleep", "infinity"]);
        let lines = Arc::new(Mutex::new(Vec::new()));
        let kept = Arc::clone(&lines);
        let sink: Sink = Arc::new(move |line| kept.lock().unwrap().push(line.to_owned()));
        let spawner = Spawner::with_sink(example(), sink).unwrap();
        Self {
            spawner,
            lines,
            unit,
        }
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
        self.lines_since(0, id)
    }

    /// The lines of the sink with the tag of `id`, from line `start` on.
    /// A seat that runs two probes gets the lines of each probe apart.
    fn lines_since(&self, start: usize, id: &SessionId) -> Vec<String> {
        let tag = format!("ch {id}");
        let lines = self.all_lines();
        lines
            .into_iter()
            .skip(start)
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

/// The paths of the devices of one boot: the API socket, the root disk,
/// the session disk, the vsock socket and the serial file.
struct BootFiles {
    api: PathBuf,
    root: PathBuf,
    disk: PathBuf,
    vsock: PathBuf,
    serial: PathBuf,
}

/// The files of one test under the two roots, and the vmm unit of the
/// test. A drop removes the files.
struct Rig {
    unit: VmmUnit,
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
            unit: stand_in::unit(&format!("slot-{name}")),
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
        let payload = Payload::new(ch, args);
        Launch::new(systemd_run, scope, payload, files, self.unit.clone()).in_slot(seat.slot)
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
        let run = &seat.run;
        let files = BootFiles {
            api: run.api_socket(),
            root: root.to_owned(),
            disk: seat.session.disk(),
            vsock: run.vsock_socket(),
            serial: run.serial_log(),
        };
        self.boot_args_at(&files)
    }

    /// The boot of the busybox guest with the devices of `files`.
    fn boot_args_at(&self, files: &BootFiles) -> Vec<String> {
        let mut args = vec![
            "--api-socket".to_owned(),
            format!("path={}", files.api.display()),
        ];
        args.extend(self.guest_args());
        args.extend([
            "--disk".to_owned(),
            format!("path={},readonly=on,image_type=raw", files.root.display()),
            "--disk".to_owned(),
            format!("path={},image_type=raw", files.disk.display()),
            "--vsock".to_owned(),
            format!("cid=3,socket={}", files.vsock.display()),
            "--serial".to_owned(),
            format!("file={}", files.serial.display()),
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
    let before = vmm.all_lines().len();
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
        lines: vmm.lines_since(before, &id),
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
    let vmm = Vmm::new(&rig);
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
    let scope = format!("{}.scope", scope_unit(&seat.id));
    assert!(
        cgroup.trim_end().ends_with(&format!("/{scope}")),
        "{cgroup}"
    );
    // The scope binds to the vmm unit and is ordered before it (decision
    // CH61 rule 2).
    for property in ["BindsTo", "Before"] {
        let units = unit_property(&scope, property);
        assert!(
            units.contains(&rig.unit.name().to_owned()),
            "{property}: {units:?}"
        );
    }
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
    // with the core limit of the test, because no slot profile has an
    // attachment path. The runner starts each job step with no_new_privs
    // set already, so no control here can show the set of the stage. The
    // stage reads the flag back and fails closed (`StageError::NoNewPrivs`).
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
    let vmm = Vmm::new(&rig);

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
/// The refusal must be the socket error of that migration: a `Migration
/// failed` line of CH since the call, or the answer of `ch-remote`, with
/// the EACCES text. The control sends the same call to a CH with the same
/// devices that runs with no chain, and its connect arrives. So only the
/// chain differs.
#[test]
#[ignore = "needs /dev/kvm and bin/vm-smoke-setup.sh: the job kvm runs it"]
fn kvm_ch_under_the_chain_makes_no_tcp_connect() {
    let mut rig = Rig::new("tcp");
    let seat = rig.seat("0123abcd-0000-4000-8000-0000000c1520", 5);
    let vmm = Vmm::new(&rig);
    let ch = vmm.launch(rig.boot(&seat));
    wait_booted(&vmm, &ch, &seat);

    let before = vmm.all_lines().len();
    let refused = migrate(&seat.run.api_socket(), NO_CONNECT);
    assert!(!refused.0, "CH under the chain made a TCP connect");
    let lines = vmm.lines_since(before, &seat.id);
    let socket_error = |text: &str| text.contains(EACCES);
    let migration_error = |line: &String| line.contains(MIGRATION_FAILED) && socket_error(line);
    assert!(
        socket_error(&refused.1) || lines.iter().any(migration_error),
        "no migration socket error {EACCES}: ch-remote {}, sink {lines:?}",
        refused.1
    );

    // The control: the same binary and the same devices with no chain, so
    // with no label and no ruleset.
    let work = files().join(format!("kvm-slot-tcp-{}", std::process::id()));
    let _ = fs::remove_dir_all(&work);
    fs::create_dir(&work).unwrap();
    let plain_files = BootFiles {
        api: work.join("api.sock"),
        root: work.join("root"),
        disk: work.join("disk"),
        vsock: work.join("vsock.sock"),
        serial: work.join("serial.log"),
    };
    zeros(&plain_files.root);
    zeros(&plain_files.disk);
    let plain = Vm(Command::new(CH_BINARY)
        .args(rig.boot_args_at(&plain_files))
        .stdin(Stdio::null())
        .spawn()
        .unwrap());
    until("boot marker of the plain CH", || {
        booted(&plain_files.serial)
    });
    let control = migrate(&plain_files.api, BOOT);
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
/// two different errors: a refused connect to the user bus, and a TPM
/// listener that closes the connect. The EACCES lines come with the id of
/// the first CH only, and the error chain of the second CH comes with its
/// own id.
#[test]
#[ignore = "needs /dev/kvm and bin/vm-smoke-setup.sh: the job kvm runs it"]
fn kvm_ch_output_reaches_the_sink_with_its_id() {
    let mut rig = Rig::new("output");
    let one = rig.seat("0123abcd-0000-4000-8000-0000000c1530", 7);
    let two = rig.seat("0123abcd-0000-4000-8000-0000000c1531", 8);
    let vmm = Vmm::new(&rig);
    let uid = rustix::process::getuid().as_raw();
    let bus = PathBuf::from(format!("/run/user/{uid}/bus"));
    let path = two.run.host_listener(HostPort::Chaperone);
    let listener = listen(&path);
    let first = probe(&vmm, rig.tpm(&one, &bus), None);
    let second = probe(&vmm, rig.tpm(&two, &path), Some(&listener));

    assert!(first.denied() && !second.denied(), "{first:?} {second:?}");
    let exited = "Cloud Hypervisor exited";
    assert!(
        second.lines.iter().any(|line| line.contains(exited)),
        "{second:?}"
    );
    let tag = format!("ch {}: ", one.id);
    let lines = vmm.all_lines();
    let denied: Vec<&String> = lines.iter().filter(|line| line.contains(DENIED)).collect();
    assert!(!denied.is_empty(), "{lines:?}");
    assert!(
        denied.iter().all(|line| line.starts_with(&tag)),
        "{lines:?}"
    );
}

/// The units in the dependency `property` of the user unit `name`.
#[cfg(test)]
fn unit_property(name: &str, property: &str) -> Vec<String> {
    let output = Command::new(SYSTEMCTL)
        .args(["--user", "show", "--value"])
        .arg(format!("--property={property}"))
        .arg("--")
        .arg(name)
        .output()
        .unwrap();
    assert!(output.status.success(), "systemctl show: {output:?}");
    let text = String::from_utf8(output.stdout).unwrap();
    text.split_whitespace().map(str::to_owned).collect()
}

/// Polls until the user unit `name` is `state` and has no job left, so
/// no queued job of systemd can still start or stop it.
#[cfg(test)]
fn until_settled(name: &str, state: &str) {
    until(&format!("{name} {state} with no job"), || {
        state_of(name) == state && unit_property(name, "Job").is_empty()
    });
}

/// The cgroup dir of the scope of `pid`.
#[cfg(test)]
fn scope_dir(pid: u32) -> PathBuf {
    let cgroup = fs::read_to_string(format!("/proc/{pid}/cgroup")).unwrap();
    let path = cgroup.trim_end().strip_prefix("0::").unwrap();
    let dir = PathBuf::from(format!("/sys/fs/cgroup{path}"));
    assert!(dir.exists(), "no scope dir {}", dir.display());
    dir
}

/// Starts the example in its role `hold` as the vmm unit of `rig`, with
/// the boot of `seat`. Waits for the boot marker, then checks the label
/// and the control: CH still runs 1 s later while the stand-in runs.
/// Returns the stand-in and the pid of CH.
#[cfg(test)]
fn hold_booted(rig: &Rig, seat: &Seat, gated: bool) -> (StandIn, u32) {
    let (vmm, pid) = StandIn::hold(&example(), &rig.boot(seat), gated);
    until("boot marker", || {
        assert!(running(pid), "CH exited before the marker");
        booted(&seat.run.serial_log())
    });
    assert_eq!(label_of(pid), Some(enforced(seat.slot)));
    sleep(Duration::from_secs(1));
    assert!(running(pid), "CH exited while the vmm lived");
    assert_eq!(vmm.state(), "active");
    (vmm, pid)
}

/// A stop of the vmm unit ends the VM scope and its CH, after the vmm
/// has exited (decision CH61 rules 1 and 2). The stand-in is gated: its
/// stop waits in `ExecStop=` until the test opens the gate, as a vmm
/// shutdown runs before the vmm exits. The controls: CH runs while the
/// stand-in runs, and CH and its scope still run 1 s into the stop of
/// the unit, because the scope is ordered `Before=` the unit. A drop of
/// the spawner does not end CH, because AppArmor clears the death signal
/// at the exec into the slot label. So this test proves the stop of the
/// unit in place of the drop of the spawner.
///
/// This test proves the outcome of CH61 rule 1 on a stop, not the
/// `ExecStopPost=` step. `BindsTo=` also stops the scope here, and CH is
/// a child of `hold`, so the test cannot read how CH died. The tests
/// that read the signal 9 of the step are
/// `kvm_a_vmm_unit_stop_kills_each_vm`,
/// `kvm_a_vmm_unit_restart_ends_each_vm` and
/// `kvm_a_vmm_crash_frees_the_scope_of_each_vm`.
#[test]
#[ignore = "needs /dev/kvm and bin/vm-smoke-setup.sh: the job kvm runs it"]
fn kvm_ch_stops_after_its_vmm_unit_stops() {
    let mut rig = Rig::new("unit-stop");
    let seat = rig.seat("0123abcd-0000-4000-8000-0000000c1540", 9);
    // CH is not a child of the test. The guard kills the scope when an
    // assert fails.
    let _scope = ScopeGuard::new(&seat.id);
    let (vmm, pid) = hold_booted(&rig, &seat, true);
    let scope = format!("{}.scope", scope_unit(&seat.id));
    let dir = scope_dir(pid);
    assert_eq!(state_of(&scope), "active");

    assert!(vmm.systemctl(&["stop", "--no-block"]), "no stop job");
    until_state(rig.unit.name(), "deactivating");
    sleep(Duration::from_secs(1));
    assert!(running(pid), "CH stopped before the vmm unit");
    assert_eq!(state_of(&scope), "active");

    fs::write(&vmm.gate, b"").unwrap();
    until("CH death after the vmm unit stop", || !running(pid));
    until("scope removal", || !dir.exists());
    until_state(&scope, "inactive");
    until_state(rig.unit.name(), "inactive");
}

/// A SIGKILL of the vmm ends the VM scope and its CH before the vmm unit
/// starts again (decision CH61 rule 1). The stand-in's main process gets
/// the SIGKILL, as a crash ends the vmm. The `ExecStopPost=` step of the
/// unit or `BindsTo=` ends each VM scope, and the unit passes through
/// `failed` into `auto-restart` (`Restart=always`). `RestartSec` is 5 s,
/// so CH and its scope are gone while the unit still waits in
/// `auto-restart`. The restarted `hold` reads EOF and exits 78, so the
/// unit then stays down. The control: CH runs while the stand-in runs.
///
/// This test proves the outcome of CH61 rule 1 on a crash, not the
/// `ExecStopPost=` step. `BindsTo=` alone stops the scope when the unit
/// fails, and CH is a child of `hold`, so the test cannot read how CH
/// died. The signal 9 of `kvm_a_vmm_crash_frees_the_scope_of_each_vm`
/// proves the step on a crash, and the signal 9 of
/// `kvm_a_vmm_unit_restart_ends_each_vm` proves it on a restart.
#[test]
#[ignore = "needs /dev/kvm and bin/vm-smoke-setup.sh: the job kvm runs it"]
fn kvm_ch_dies_with_a_killed_vmm() {
    let mut rig = Rig::new("vmm-kill");
    let seat = rig.seat("0123abcd-0000-4000-8000-0000000c1550", 10);
    let _scope = ScopeGuard::new(&seat.id);
    let (vmm, pid) = hold_booted(&rig, &seat, false);
    let scope = format!("{}.scope", scope_unit(&seat.id));
    let dir = scope_dir(pid);
    assert_eq!(state_of(&scope), "active");

    vmm.kill();
    until("CH death after the vmm kill", || !running(pid));
    until("scope removal", || !dir.exists());
    until_state(&scope, "inactive");
    assert_eq!(vmm.sub_state(), "auto-restart");
    until_state(rig.unit.name(), "inactive");
}

/// A restart of the `hold` stand-in, whose spawner runs in the unit as in
/// the vmm, ends the VM scope and its CH (decision CH61 rule 1). The
/// `ExecStopPost=` step of the stop part of the restart kills the scope,
/// or `BindsTo=` stops it when the restarted `hold` fails: it reads EOF
/// and exits 78, so the unit then ends `failed` and stays down. The
/// control: CH runs while the stand-in runs.
///
/// This test proves the outcome of CH61 rule 1 on a failed restart, not
/// the `ExecStopPost=` step. `BindsTo=` alone also stops the scope here,
/// and CH is a child of `hold`, so the test cannot read how CH died. The
/// signal 9 of `kvm_a_vmm_unit_restart_ends_each_vm` proves the step on
/// a restart, and the signal 9 of
/// `kvm_a_vmm_crash_frees_the_scope_of_each_vm` proves it on a crash.
#[test]
#[ignore = "needs /dev/kvm and bin/vm-smoke-setup.sh: the job kvm runs it"]
fn kvm_ch_stops_when_the_restarted_vmm_fails() {
    let mut rig = Rig::new("unit-restart");
    let seat = rig.seat("0123abcd-0000-4000-8000-0000000c15b0", 20);
    let _scope = ScopeGuard::new(&seat.id);
    let (vmm, pid) = hold_booted(&rig, &seat, false);
    let scope = format!("{}.scope", scope_unit(&seat.id));
    let dir = scope_dir(pid);
    assert_eq!(state_of(&scope), "active");

    assert!(vmm.systemctl(&["restart"]), "the restart failed");
    until("CH death after the failed start", || !running(pid));
    until("scope removal", || !dir.exists());
    until_settled(&scope, "inactive");
    until_state(rig.unit.name(), "inactive");
}

/// A stop of the vmm unit ends each VM scope and its CH by the SIGKILL of
/// the `ExecStopPost=` step (decision CH61 rule 1). The stand-in is a
/// gated `/bin/sleep infinity`, and the spawner runs in the test, so the
/// test reads how CH died. Without the step, `BindsTo=` stops the scope
/// after the stop of the unit (`Before=`, CH61 rule 2), and CH answers
/// the SIGTERM of that scope stop with an exit code. So the signal 9
/// proves that the step ran. The controls: CH runs while the stand-in
/// runs, and CH and its scope still run 1 s into the gated stop.
#[test]
#[ignore = "needs /dev/kvm and bin/vm-smoke-setup.sh: the job kvm runs it"]
fn kvm_a_vmm_unit_stop_kills_each_vm() {
    let mut rig = Rig::new("stop-kill");
    let seat = rig.seat("0123abcd-0000-4000-8000-0000000c15e0", 23);
    let vmm = Vmm::gated(&rig);
    let mut ch = vmm.launch(rig.boot(&seat));
    wait_booted(&vmm, &ch, &seat);
    let pid = ch.0.id();
    let scope = format!("{}.scope", scope_unit(&seat.id));
    let dir = scope_dir(pid);
    sleep(Duration::from_secs(1));
    assert!(running(pid), "CH exited while the vmm lived");
    assert_eq!(state_of(&scope), "active");

    assert!(vmm.unit.systemctl(&["stop", "--no-block"]), "no stop job");
    until_state(rig.unit.name(), "deactivating");
    sleep(Duration::from_secs(1));
    assert!(running(pid), "CH stopped before the vmm unit");
    assert_eq!(state_of(&scope), "active");

    fs::write(&vmm.unit.gate, b"").unwrap();
    until("CH death after the stop", || !running(pid));
    let status = ch.0.wait().unwrap();
    assert_eq!(status.signal(), Some(9), "{status}");
    until("scope removal", || !dir.exists());
    until_settled(&scope, "inactive");
    until_state(rig.unit.name(), "inactive");
}

/// A restart of the vmm unit ends each VM scope and its CH at the stop
/// part of the restart (decision CH61 rule 1). systemd passes no stop of
/// a restart on to a bound scope, so only the `ExecStopPost=` step of the
/// unit, the built `vmm kill-scopes` (decision CH63), ends CH. The stand-in is a gated `/bin/sleep infinity`, which
/// comes back, and the spawner runs in the test. CH dies of the SIGKILL
/// of that step. The controls: CH runs while the stand-in runs, CH and
/// its scope still run 1 s into the gated stop, as `Before=` promises
/// (CH61 rule 2), and with no VM the step exits 0. After the restart the
/// unit is active, and a new launch of the same session boots.
#[test]
#[ignore = "needs /dev/kvm and bin/vm-smoke-setup.sh: the job kvm runs it"]
fn kvm_a_vmm_unit_restart_ends_each_vm() {
    let mut rig = Rig::new("restart");
    let seat = rig.seat("0123abcd-0000-4000-8000-0000000c15c0", 21);
    let vmm = Vmm::gated(&rig);
    // One stand-in runs at a time, so no test launches a VM now. A scope
    // of an earlier test may still wait for its unload. The step kills it,
    // or finds it gone and counts it as ended (decision CH63), so the
    // step exits 0 either way.
    let status = kill_scopes().status().unwrap();
    assert!(status.success(), "the step with no VM: {status}");

    let mut ch = vmm.launch(rig.boot(&seat));
    wait_booted(&vmm, &ch, &seat);
    let pid = ch.0.id();
    let scope = format!("{}.scope", scope_unit(&seat.id));
    let dir = scope_dir(pid);
    sleep(Duration::from_secs(1));
    assert!(running(pid), "CH exited while the vmm lived");
    assert_eq!(state_of(&scope), "active");

    let restart = ["restart", "--no-block"];
    assert!(vmm.unit.systemctl(&restart), "no restart job");
    until_state(rig.unit.name(), "deactivating");
    sleep(Duration::from_secs(1));
    assert!(running(pid), "CH stopped before the vmm unit");
    assert_eq!(state_of(&scope), "active");

    fs::write(&vmm.unit.gate, b"").unwrap();
    until("CH death after the restart", || !running(pid));
    let status = ch.0.wait().unwrap();
    assert_eq!(status.signal(), Some(9), "{status}");
    until("scope removal", || !dir.exists());
    until_settled(&scope, "inactive");
    until_settled(rig.unit.name(), "active");
    relaunch(&rig, &vmm, &seat);
}

/// After a crash of the vmm, the session of a stopped VM starts again
/// under the same scope name (decision CH61 rule 1). The stand-in is
/// `/bin/sleep infinity`, and the spawner runs in the test. A SIGKILL of
/// its main process runs the `ExecStopPost=` step, the built `vmm
/// kill-scopes` (decision CH63), and CH dies of its SIGKILL. The unit then waits in `auto-restart`. CH and its scope are
/// gone before the unit starts again, 5 s later, and systemd starts no
/// scope again. The control: CH runs before the kill, and a new launch of
/// the same session boots.
#[test]
#[ignore = "needs /dev/kvm and bin/vm-smoke-setup.sh: the job kvm runs it"]
fn kvm_a_vmm_crash_frees_the_scope_of_each_vm() {
    let mut rig = Rig::new("crash-free");
    let seat = rig.seat("0123abcd-0000-4000-8000-0000000c15d0", 22);
    let vmm = Vmm::new(&rig);
    let mut ch = vmm.launch(rig.boot(&seat));
    wait_booted(&vmm, &ch, &seat);
    let pid = ch.0.id();
    let scope = format!("{}.scope", scope_unit(&seat.id));
    assert_eq!(state_of(&scope), "active");

    vmm.unit.kill();
    until("CH death after the vmm kill", || !running(pid));
    let status = ch.0.wait().unwrap();
    assert_eq!(status.signal(), Some(9), "{status}");
    until_state(&scope, "inactive");
    assert_eq!(vmm.unit.sub_state(), "auto-restart");
    until_state(rig.unit.name(), "active");
    until_settled(&scope, "inactive");
    relaunch(&rig, &vmm, &seat);
}

/// A new launch of the session of `seat` in its slot, after its CH has
/// stopped. `RunRoot::link_vm` empties the slot first, as lifecycle does
/// (decision CH56 rule 1). The launch boots, so the scope name is free.
#[cfg(test)]
fn relaunch(rig: &Rig, vmm: &Vmm, seat: &Seat) {
    rig.run.link_vm(&seat.id, seat.slot).unwrap();
    let ch = vmm.launch(rig.boot(seat));
    wait_booted(vmm, &ch, seat);
    assert_eq!(label_of(ch.0.id()), Some(enforced(seat.slot)));
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
    let vmm = Vmm::new(&rig);
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
    let vmm = Vmm::new(&rig);

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
    let vmm = Vmm::new(&rig);
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
    let vmm = Vmm::new(&rig);

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
    let vmm = Vmm::new(&rig);
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
