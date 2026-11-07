//! The helpers of the kvm_ tests of guest init: the files of
//! `bin/vm-smoke-setup.sh`, the ext4 disks that each test builds with
//! `mke2fs -d` and the stand-in facade on them (decision CH62 rule 12), a
//! CH process killed on drop and a link to a guest port through the
//! hybrid vsock socket of CH.
#![cfg(test)]

use std::fs::{self, File};
use std::io::{self, BufRead as _, BufReader, Write as _};
use std::num::NonZeroU32;
use std::os::unix::net::{UnixListener, UnixStream};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, ExitStatus, Stdio};
use std::thread::sleep;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use creche_wire::guest::{FACADE_PROGRAM, SESSION_DISK_MOUNT};
use creche_wire::{
    ControlHello, ControlReady, FacadeExited, FacadeRunning, FacadeState, GuestEvent, GuestPort,
    HostPort, StartCount, TurnEnd, UnixMillis,
};
use serde::Serialize;

/// The variable that names the files of the setup script.
const DIR: &str = "CRECHE_KVM_DIR";

/// The host `mke2fs`, as the vmm kvm tests name it.
const MKE2FS: &str = "/usr/sbin/mke2fs";

/// The size of each test disk.
const DISK_BYTES: u64 = 16 << 20;

/// The dirs of the test root disk: each mount point of the boot plan.
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

/// A boot takes about 0.6 s on the runner (fact 79).
pub const BOOT: Duration = Duration::from_secs(30);

/// The guest CID of each test VM. The vmm gives CID 3 too.
const CID: u32 = 3;

/// The pause between two dials, as the vmm pauses (decision CH43 rule 5).
const DIAL_PAUSE: Duration = Duration::from_millis(10);

/// The directory of the setup script.
pub fn files() -> PathBuf {
    let dir = std::env::var_os(DIR)
        .unwrap_or_else(|| panic!("{DIR} is not set: run bin/vm-smoke-setup.sh first"));
    PathBuf::from(dir)
}

/// A new empty work directory for one VM.
pub fn work(name: &str) -> PathBuf {
    let work = files().join(format!("{name}-{}", std::process::id()));
    let _ = fs::remove_dir_all(&work);
    fs::create_dir(&work).unwrap();
    work
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

/// The CH command of one VM: the guest kernel, the guest-init initramfs
/// of the setup script, the cmdline of decision CH62 rule 19 and the
/// serial port in `serial`. The caller adds the disks.
pub fn ch(serial: &Path) -> Command {
    let files = files();
    let mut command = Command::new(files.join("cloud-hypervisor"));
    command
        .arg("--kernel")
        .arg(files.join("vmlinux"))
        .arg("--initramfs")
        .arg(files.join("guest-init.cpio"))
        .args(["--cmdline", "console=ttyS0 rdinit=/init"])
        .args(["--cpus", "boot=1", "--memory", "size=256M"])
        .arg("--serial")
        .arg(format!("file={}", serial.display()))
        .args(["--console", "off"])
        .stdin(Stdio::null());
    command
}

/// A CH process of the test, killed and reaped on drop, so a failed test
/// leaves no VM.
pub struct Vm(pub Child);

impl Drop for Vm {
    fn drop(&mut self) {
        let _ = self.0.kill();
        let _ = self.0.wait();
    }
}

/// The serial lines, with the CR of each CRLF line end cut.
pub fn lines(serial: &Path) -> Vec<String> {
    fs::read_to_string(serial)
        .unwrap_or_default()
        .lines()
        .map(|line| line.trim_end().to_owned())
        .collect()
}

/// Waits until `done` holds for the serial lines. It panics with the
/// serial text when CH exits first or `BOOT` passes.
pub fn wait_for(vm: &mut Vm, serial: &Path, what: &str, done: impl Fn(&[String]) -> bool) {
    let deadline = Instant::now() + BOOT;
    while !done(&lines(serial)) {
        if let Some(status) = vm.0.try_wait().unwrap() {
            let log = fs::read_to_string(serial).unwrap_or_default();
            panic!("CH exited with {status} before {what}. Serial:\n{log}");
        }
        let log = fs::read_to_string(serial).unwrap_or_default();
        assert!(
            Instant::now() < deadline,
            "no {what} in {BOOT:?}. Serial:\n{log}"
        );
        sleep(Duration::from_millis(20));
    }
}

/// Waits until the serial file holds `line`. The guest writes a console
/// line after the link write that it names, and the tty can buffer it, so
/// a test reads the serial file this way after a link read. A one-time
/// `logged` is right only after `wait_for_exit`.
pub fn wait_for_line(vm: &mut Vm, serial: &Path, line: &str) {
    wait_for(vm, serial, line, |lines| {
        lines.iter().any(|each| each == line)
    });
}

/// Waits until CH exits, as it does when the guest powers off. It panics
/// with the serial text when `BOOT` passes first.
pub fn wait_for_exit(vm: &mut Vm, serial: &Path) -> ExitStatus {
    let deadline = Instant::now() + BOOT;
    loop {
        if let Some(status) = vm.0.try_wait().unwrap() {
            return status;
        }
        let log = fs::read_to_string(serial).unwrap_or_default();
        assert!(
            Instant::now() < deadline,
            "CH runs after {BOOT:?}. Serial:\n{log}"
        );
        sleep(Duration::from_millis(20));
    }
}

/// The hybrid vsock socket of the CH of `work`.
pub fn vsock(work: &Path) -> PathBuf {
    work.join("vsock.sock")
}

/// The session disk of `vm_with_env`.
pub fn session_disk(work: &Path) -> PathBuf {
    work.join("session.img")
}

/// The mode of the stand-in facade on a test root disk
/// (`examples/standin_facade.rs`).
#[derive(Debug, Clone, Copy)]
pub enum Standin {
    /// No mode file: the stand-in stays up.
    StaysUp,
    /// Mode `exit-once`: the first start leaves a child in its process
    /// group and exits with status 3. The next start writes whether that
    /// child is gone, and each later start stays up.
    ExitsOnce,
    /// Mode `holder`: the stand-in starts a child outside its process
    /// group that holds a file of the session disk open for writing, and
    /// stays up.
    Holder,
    /// Mode `turn`: the stand-in opens the local link, sends one turn,
    /// starts a stranger that opens the link too, and exits on the stop
    /// line.
    Turn,
    /// Mode `stream`: the stand-in opens the local link and writes turns
    /// on it with no pause. It never reads the stop line.
    Stream,
    /// Mode `close-once`: the first start opens the local link, closes it
    /// and stays up. Each later start stays up.
    CloseOnce,
    /// Mode `relay`: after 2 s the stand-in listens on the relay socket
    /// and answers each line `ping` with `pong <pid>` and each line
    /// `pull` with `pulled <pid> <reply key> <status 4000> <status 8300>
    /// <pull ms>`, and stays up. Before each pull of its own, a child of
    /// it sends a pull and writes `standin: family stranger pid <pid>
    /// <answer>`.
    Relay,
    /// Mode `markers`: the stand-in writes `STANDIN_MARKERS` on the
    /// session disk, each holding its name and a LF, and stays up.
    Markers,
}

/// The files of mode `markers`, at the root of the session disk, as
/// `examples/standin_facade.rs` names them.
pub const STANDIN_MARKERS: [&str; 3] = ["standin-marker-1", "standin-marker-2", "standin-marker-3"];

/// The mode file of the stand-in on the root disk.
const STANDIN_MODE: &str = "etc/creche-standin/mode";

/// `vm_with` with a stand-in that stays up.
pub fn vm_with_env(work: &Path, env: &[u8]) -> Command {
    vm_with(work, env, Standin::StaysUp)
}

/// Builds a test root disk whose env file holds `env`, with the
/// stand-in facade of the setup script at `FACADE_PROGRAM` in `standin`
/// mode, and an empty session disk. It returns the CH command with both
/// and a vsock device at `vsock(work)`. The root disk goes first,
/// read-only, then the session disk (decisions CH8-7, CH8-8, CH50 rule
/// 10).
pub fn vm_with(work: &Path, env: &[u8], standin: Standin) -> Command {
    let tree = work.join("tree");
    let session_dir = SESSION_DISK_MOUNT.trim_start_matches('/');
    let facade = tree.join(FACADE_PROGRAM.trim_start_matches('/'));
    let facade_dir = facade.parent().unwrap().to_owned();
    for dir in ROOT_DIRS.into_iter().chain([session_dir, "etc/creche"]) {
        fs::create_dir_all(tree.join(dir)).unwrap();
    }
    fs::write(tree.join("etc/creche/env"), env).unwrap();
    fs::create_dir_all(facade_dir).unwrap();
    fs::copy(files().join("standin_facade"), &facade).unwrap();
    let mode = match standin {
        Standin::StaysUp => None,
        Standin::ExitsOnce => Some("exit-once\n"),
        Standin::Holder => Some("holder\n"),
        Standin::Turn => Some("turn\n"),
        Standin::Stream => Some("stream\n"),
        Standin::CloseOnce => Some("close-once\n"),
        Standin::Relay => Some("relay\n"),
        Standin::Markers => Some("markers\n"),
    };
    if let Some(mode) = mode {
        let file = tree.join(STANDIN_MODE);
        fs::create_dir_all(file.parent().unwrap()).unwrap();
        fs::write(file, mode).unwrap();
    }
    let root = work.join("root.img");
    let session = session_disk(work);
    ext4(&root, Some(&tree));
    ext4(&session, None);

    let mut command = ch(&work.join("serial.log"));
    command
        .arg("--disk")
        .arg(format!(
            "path={},readonly=on,image_type=raw",
            root.display()
        ))
        .arg("--disk")
        .arg(format!("path={},image_type=raw", session.display()))
        .arg("--vsock")
        .arg(format!("cid={CID},socket={}", vsock(work).display()));
    command
}

/// Listens on host `port` of the VM of `work`: CH joins each guest dial
/// of vsock CID 2 at `port` to the Unix socket `<vsock socket>_<port>`
/// (decision CH14).
pub fn listen_host(work: &Path, port: HostPort) -> UnixListener {
    let mut path = vsock(work).into_os_string();
    path.push(format!("_{}", port.number()));
    UnixListener::bind(path).unwrap()
}

/// One link to a guest port: the test writes `CONNECT <port>` on the
/// hybrid vsock socket and CH answers `OK <host port>` (decision CH13).
/// Then each line is one compact JSON value (decision CH36 G11).
pub struct Link {
    reader: BufReader<UnixStream>,
    writer: UnixStream,
}

/// Dials `port` of the VM of `work`, as the vmm dials: it retries while
/// the socket is missing or refused, and while CH closes the link because
/// no guest listener is there yet. It panics after `BOOT`.
pub fn dial(work: &Path, port: GuestPort) -> Link {
    let deadline = Instant::now() + BOOT;
    let log = || fs::read_to_string(work.join("serial.log")).unwrap_or_default();
    loop {
        assert!(
            Instant::now() < deadline,
            "no link to {port:?} in {BOOT:?}. Serial:\n{}",
            log()
        );
        let mut writer = match UnixStream::connect(vsock(work)) {
            Ok(stream) => stream,
            Err(error)
                if matches!(
                    error.kind(),
                    io::ErrorKind::NotFound | io::ErrorKind::ConnectionRefused
                ) =>
            {
                sleep(DIAL_PAUSE);
                continue;
            }
            Err(error) => panic!("the vsock socket refused: {error}"),
        };
        writer.set_read_timeout(Some(BOOT)).unwrap();
        writeln!(writer, "CONNECT {}", port.number()).unwrap();
        let mut link = Link {
            reader: BufReader::new(writer.try_clone().unwrap()),
            writer,
        };
        match link.line() {
            Some(reply) if reply.starts_with("OK ") => return link,
            Some(reply) => panic!("CH answered CONNECT with {reply:?}"),
            None => sleep(DIAL_PAUSE),
        }
    }
}

impl Link {
    /// Writes `value` as one compact JSON line.
    pub fn send<T: Serialize>(&mut self, value: &T) {
        let mut line = serde_json::to_vec(value).unwrap();
        line.push(b'\n');
        self.writer.write_all(&line).unwrap();
    }

    /// Writes `bytes` as they are, for a link that carries no JSON.
    pub fn write(&mut self, bytes: &[u8]) {
        self.writer.write_all(bytes).unwrap();
    }

    /// Reads one line, its LF cut, or `None` at the end of the link. It
    /// panics after `BOOT` with no line.
    pub fn line(&mut self) -> Option<String> {
        let mut line = String::new();
        match self.reader.read_line(&mut line) {
            Ok(0) => None,
            Ok(_) => {
                assert_eq!(line.pop(), Some('\n'), "a line with no LF: {line:?}");
                Some(line)
            }
            Err(error) if error.kind() == io::ErrorKind::ConnectionReset => None,
            Err(error) => panic!("the read failed: {error}"),
        }
    }

    /// Reads one `GuestEvent` line. It panics at the end of the link.
    pub fn event(&mut self) -> GuestEvent {
        let line = self.line().expect("no event: the link ended");
        serde_json::from_str(&line).unwrap_or_else(|error| panic!("{line:?}: {error}"))
    }

    /// Reads each line until the end of the link and returns the last
    /// one. A read that waits `BOOT` for a line panics.
    pub fn last_line(mut self) -> Option<String> {
        let mut last = None;
        while let Some(line) = self.line() {
            last = Some(line);
        }
        last
    }

    /// Whether no byte comes within `wait`.
    pub fn quiet_for(&mut self, wait: Duration) -> bool {
        self.writer.set_read_timeout(Some(wait)).unwrap();
        let mut line = String::new();
        let quiet = match self.reader.read_line(&mut line) {
            Err(error) => matches!(
                error.kind(),
                io::ErrorKind::WouldBlock | io::ErrorKind::TimedOut
            ),
            Ok(_) => false,
        };
        self.writer.set_read_timeout(Some(BOOT)).unwrap();
        quiet
    }
}

/// A valid env file, as `playpen/bin/creche-env-write` writes it.
pub const GOOD_ENV: &[u8] = b"LANG=C.UTF-8\nPATH=/usr/bin:/bin\n";

/// The session of each hello.
pub const SESSION: &str = "0123abcd-0000-4000-8000-00000000c7c7";

/// 2030-01-01T00:00:00Z, a time the guest clock cannot hold at boot.
pub const IN_2030: u64 = 1_893_456_000_000;

/// The host tools that read a session disk after the VM, as `MKE2FS`.
pub const E2FSCK: &str = "/usr/sbin/e2fsck";
pub const DUMPE2FS: &str = "/usr/sbin/dumpe2fs";
pub const DEBUGFS: &str = "/usr/sbin/debugfs";

/// `booted_with` with a stand-in that stays up.
pub fn booted(name: &str) -> (PathBuf, Vm) {
    booted_with(name, Standin::StaysUp)
}

/// A VM with a good env file, the stand-in in `standin` mode and a
/// session disk, booted to `up`.
pub fn booted_with(name: &str, standin: Standin) -> (PathBuf, Vm) {
    let work = work(name);
    let serial = work.join("serial.log");
    let mut vm = Vm(vm_with(&work, GOOD_ENV, standin).spawn().unwrap());
    wait_for(&mut vm, &serial, "guest-init: up", |lines| {
        lines.iter().any(|line| line == "guest-init: up")
    });
    (work, vm)
}

/// The wall time of the host.
pub fn now() -> u64 {
    let since = SystemTime::now().duration_since(UNIX_EPOCH).unwrap();
    u64::try_from(since.as_millis()).unwrap()
}

/// Sends a hello at `sent_at` and returns the ready.
pub fn hello(link: &mut Link, sent_at: u64) -> ControlReady {
    link.send(&ControlHello {
        sent_at: UnixMillis::try_from(sent_at).unwrap(),
        session: SESSION.parse().unwrap(),
    });
    let line = link.line().expect("no ready: the link ended");
    serde_json::from_str(&line).unwrap_or_else(|error| panic!("{line:?}: {error}"))
}

/// The pid of the facade of a ready that the first start answers: the
/// state is `starting`, with `starts` 1 and no turn end (decision CH32
/// rules 6 and 7).
pub fn starting(ready: &ControlReady) -> NonZeroU32 {
    match &ready.facade {
        FacadeState::Starting(starting)
            if starting.starts.get() == 1 && starting.last_turn_end.is_none() =>
        {
            starting.pid
        }
        other => panic!("the ready is not a first start: {other:?}"),
    }
}

/// Whether `serial` holds `line`.
pub fn logged(serial: &Path, line: &str) -> bool {
    lines(serial).iter().any(|each| each == line)
}

/// The facade state `exited` after `starts` starts, as a link event.
pub fn exited(starts: u64) -> GuestEvent {
    exited_after(starts, None)
}

/// The facade state `exited` after `starts` starts and `last_turn_end`,
/// as a link event.
pub fn exited_after(starts: u64, last_turn_end: Option<TurnEnd>) -> GuestEvent {
    GuestEvent::Facade(FacadeState::Exited(FacadeExited {
        last_turn_end,
        starts: StartCount::try_from(starts).unwrap(),
    }))
}

/// The state `running` of the first start of the facade `pid`.
pub fn running(pid: NonZeroU32, turn_running: bool, last_turn_end: Option<TurnEnd>) -> FacadeState {
    FacadeState::Running(FacadeRunning {
        last_turn_end,
        pid,
        starts: StartCount::try_from(1).unwrap(),
        turn_running,
    })
}

/// The turn end that the stand-in sends in mode `turn`, as
/// `examples/standin_facade.rs` makes it.
pub fn standin_turn_end() -> TurnEnd {
    TurnEnd {
        title: "standin turn".parse().unwrap(),
        updated_at: UnixMillis::try_from(IN_2030).unwrap(),
    }
}

/// The session disk of `work` is clean after the VM: guest init synced
/// it and remounted it read-only, so no journal waits for a replay, and
/// `e2fsck -fn` finds no fault.
pub fn assert_clean(work: &Path) {
    let disk = session_disk(work);
    let header = Command::new(DUMPE2FS)
        .arg("-h")
        .arg(&disk)
        .output()
        .unwrap();
    let header = String::from_utf8_lossy(&header.stdout);
    let field = |name: &str| {
        header
            .lines()
            .find_map(|line| line.strip_prefix(name))
            .map(str::trim)
            .unwrap_or_else(|| panic!("no {name} in:\n{header}"))
            .to_owned()
    };
    assert_eq!(field("Filesystem state:"), "clean");
    assert!(
        !field("Filesystem features:").contains("needs_recovery"),
        "the journal waits for a replay"
    );
    let check = Command::new(E2FSCK).arg("-fn").arg(&disk).output().unwrap();
    assert!(
        check.status.success(),
        "e2fsck -fn: {}{}",
        String::from_utf8_lossy(&check.stdout),
        String::from_utf8_lossy(&check.stderr)
    );
}

/// Accepts one link on the host side `listener`, reads its HTTP request
/// head and answers `200` with no body. It returns the head.
pub fn answer_get(listener: &UnixListener) -> String {
    let (stream, _) = listener.accept().unwrap();
    stream.set_read_timeout(Some(BOOT)).unwrap();
    let mut reader = BufReader::new(&stream);
    let mut head = String::new();
    loop {
        let mut line = String::new();
        reader.read_line(&mut line).unwrap();
        head.push_str(&line);
        if line == "\r\n" || line.is_empty() {
            break;
        }
    }
    (&stream)
        .write_all(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
        .unwrap();
    head
}

/// The words of the stand-in answer to a line `pull` on 1201, after the
/// check of `pulled <pid>`: the reply key, the two plane statuses and the
/// pull time.
pub fn pulled(work: &Path, pid: NonZeroU32) -> Vec<String> {
    let mut acp = dial(work, GuestPort::Acp);
    acp.write(b"pull\n");
    let answer = acp.line().expect("no answer: the 1201 link ended");
    let words: Vec<String> = answer.split(' ').map(str::to_owned).collect();
    assert_eq!(
        words.get(..2),
        Some(["pulled".to_owned(), pid.to_string()].as_slice()),
        "{answer:?}"
    );
    assert_eq!(words.len(), 6, "{answer:?}");
    words[2..].to_vec()
}
