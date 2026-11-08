//! The start hook over fakes (plan PRs L16 and L17), and the idle sleep
//! and the sweep over the same fakes (plan PR L18). A fake launcher
//! records each launch, writes a fake command line file and binds a fake
//! vsock socket that plays CH and guest init. A fake CH API answers
//! `info` and `resume` of a restore, and its snapshot ends the fake VM. Every other step is the real one on
//! a scratch dir in `/tmp`: the shape read, admission, the store check,
//! the session disk (with `/usr/bin/true` as `mke2fs`), the key check,
//! the claim, the deny check and the fault file. The edit follow of plan
//! PR E3 runs on paused tokio time, one `POLL` at a time.

use std::collections::HashSet;
use std::io::{BufRead, BufReader, Read, Write};
use std::net::{IpAddr, Ipv4Addr, Shutdown, SocketAddr, TcpListener};
use std::os::unix::fs::PermissionsExt;
use std::os::unix::net::{UnixListener, UnixStream};
use std::path::PathBuf;
use std::process::Command;
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex, PoisonError};
use std::thread::spawn;
use std::time::{Duration, Instant, SystemTime};
use std::{fs, io};

use creche_vmm::control::{ControlState, SYNC_WAIT};
use creche_vmm::egress::log::Log;
use creche_vmm::egress::watch::POLL;
use creche_vmm::handlers::acp::{Acp, StartFault, StartVm};
use creche_vmm::handlers::drop::{Drops, StopVm, VmStopFailed};
use creche_vmm::mirror::{DENIED, Mirror};
use creche_vmm::planes::Upstreams;
use creche_vmm::relay::{AcpLink, Door, Relays};
use creche_vmm::restore::ChApi;
use creche_vmm::server::Stop;
use creche_vmm::shutdown::{Ended, VmSet, shutdown};
use creche_vmm::start::{ColdBoot, Digests, Launcher, StartConfig};
use creche_vmm::stop::VmProcess;
use creche_vmm_confine::admission::{GIB, Limits};
use creche_vmm_confine::family::FamilyRoot;
use creche_vmm_confine::faults::{FaultBook, FaultDir};
use creche_vmm_confine::landlock::{HostPath, Start};
use creche_vmm_confine::launch::{Launch, Program};
use creche_vmm_confine::mirror::MirrorRoot;
use creche_vmm_confine::spawner::SpawnError;
use creche_vmm_confine::vmm_unit::{UnitError, VmmUnit};
use creche_vmm_store::ch_remote::{ChRemoteError, VmInfo};
use creche_vmm_store::images::{ImageDigest, ImageStore};
use creche_vmm_store::key::{CheckedCh, SnapshotKey, check_cloud_hypervisor, write_key};
use creche_vmm_store::layout::{RunRoot, SessionDir, SnapshotGeneration, StateRoot, VmRunDir};
use creche_vmm_store::retention::{BUDGET, Reason};
use creche_vmm_store::rows::Rows;
use creche_vmm_store::session_disk::SessionDisks;
use creche_vmm_store::sleep_gate::IDLE_TIMEOUT;
use creche_wire::{
    DropError, DropRoute, DropSession, DropTarget, Dropped, FacadeState, FamilyName, OpenAcp,
    PerFlavor, SandboxFlavor, Serve, ServeUpgrade as _, SessionId, SessionRow,
};
use tokio::runtime::{Builder, Runtime};
use tokio::sync::watch;

const A: &str = "3f2b8c1e-9d4a-4e6b-8a7c-0b1d2e3f4a5b";
const B: &str = "1a2b3c4d-5e6f-4a1b-8c2d-3e4f5a6b7c8d";
const C: &str = "5c6d7e8f-9a0b-4c1d-8e2f-3a4b5c6d7e8f";
const CH: &str = "/opt/ch/cloud-hypervisor";
const SHAPE: &str = r#"{"cpus":1,"flavor":"base","memory_mib":512}"#;
const READY: &str =
    r#"{"facade":{"running":{"last_turn_end":null,"pid":42,"starts":1,"turn_running":false}}}"#;
const TURN: &str =
    r#"{"facade":{"running":{"last_turn_end":null,"pid":42,"starts":1,"turn_running":true}}}"#;
const STARTING: &str = r#"{"facade":{"starting":{"last_turn_end":null,"pid":42,"starts":1}}}"#;
/// As `READY`, with the end of a turn that the row has not seen.
const ENDED: &str = r#"{"facade":{"running":{"last_turn_end":{"title":"First","updated_at":1793577700000},"pid":42,"starts":1,"turn_running":false}}}"#;
/// The listeners at the launch of a family with no egress and no clone
/// list: 1100, 1101 and 1102, and no 1103 or 1104.
const NO_EGRESS: [bool; 5] = [true, true, true, false, false];
/// The listeners at the launch of a family with egress and no clone list.
const EGRESS_ONLY: [bool; 5] = [true, true, true, false, true];
/// The listeners at the launch of a family with a clone list and no
/// egress.
const REPOS_ONLY: [bool; 5] = [true, true, true, true, false];
/// The allowlist of a family with egress (decision CH49 rule 3).
const EGRESS: &str = r#"{"hosts":[{"host":"github.com","port":443}]}"#;
/// The clone list of a family with repos (decision CH69 rule 13).
const REPOS: &str = r#"{"repos":["creche"]}"#;
/// The plane of each bridge of a test that dials none: port 9 of
/// 127.0.0.1, where nothing listens on a test host.
const NO_PLANE: SocketAddr = SocketAddr::new(IpAddr::V4(Ipv4Addr::LOCALHOST), 9);
/// The SHA-256 of no bytes: the fake kernel, initramfs and root disk are
/// empty.
const EMPTY: &str = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855";
/// The SHA-256 of `abc`: the root disk of each flavor but `python` in
/// the flavor test.
const ABC: &str = "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad";
/// The SHA-256 of `hello world`: the python root disk in the flavor test.
const HELLO: &str = "b94d27b9934d3e08a52e52d7da7dabfac484efe37a5380ee9088f7ace2efcde9";

/// How the fake guest answers.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Guest {
    /// `ControlReady`, then `OK` on 1201.
    Ready,
    /// As `Ready`, with a turn running, so admission may not evict it.
    Turn,
    /// As `Ready`, with the facade `starting`.
    Starting,
    /// As `Ready`, with a turn end in the ready.
    Ended,
    /// A close after the hello, before `ControlReady`.
    Closes,
    /// `ControlReady`, then a refused `CONNECT 1201`.
    RefusesAcp,
    /// As `Ready`, and 20 ms after a `stop` line the status check of the
    /// VM fails, in place of the power-off.
    Wedges,
    /// As `Ready`, and guest init never answers `sync` with `synced`.
    NoSync,
    /// As `Ready`, and a `stop` line does not power the VM off, so a stop
    /// waits `DROP_STOP_WAIT` and then sends SIGTERM.
    Ignores,
}

/// A launched fake VM. A signal ends it at once.
#[derive(Debug)]
struct FakeVm {
    number: usize,
    exited: Arc<AtomicBool>,
    /// Fails each status check, so each stop of the VM fails.
    broken: Arc<AtomicBool>,
    /// Fails the next status check only.
    flaky: Arc<AtomicBool>,
}

impl VmProcess for FakeVm {
    fn pid(&self) -> u32 {
        4242
    }

    fn exited(&mut self) -> io::Result<bool> {
        if self.broken.load(Ordering::SeqCst) || self.flaky.swap(false, Ordering::SeqCst) {
            return Err(io::Error::other("the status check failed"));
        }
        Ok(self.exited.load(Ordering::SeqCst))
    }

    fn terminate(&mut self) -> io::Result<()> {
        self.exited.store(true, Ordering::SeqCst);
        Ok(())
    }

    fn kill(&mut self) -> io::Result<()> {
        self.terminate()
    }
}

/// The fake launcher and what it saw.
#[derive(Debug)]
struct Fake {
    dir: PathBuf,
    guest: Mutex<Guest>,
    /// Adds `--net` to each command line file.
    net: AtomicBool,
    /// Adds a `vsock.sock_<port>` listener of this port to each run dir,
    /// a port that the vmm does not bind.
    extra: Mutex<Option<u32>>,
    /// For each launch: whether a dial of 1100, 1101, 1102, 1103 and 1104
    /// connected at the launch, before CH would start.
    listening: Mutex<Vec<[bool; 5]>>,
    /// Fails each launch.
    fail: AtomicBool,
    /// Holds each launch until the test clears it.
    hold: AtomicBool,
    /// The `stop` lines that guest init read on each control link.
    stop_lines: Arc<AtomicUsize>,
    /// The `sync` lines that guest init read on each control link.
    sync_lines: Arc<AtomicUsize>,
    /// The guest end of each control link, for the turn events.
    links: Arc<Mutex<Vec<UnixStream>>>,
    launches: Mutex<Vec<Launch>>,
    vms: Mutex<Vec<Arc<AtomicBool>>>,
    broken: Mutex<Vec<Arc<AtomicBool>>>,
    flaky: Mutex<Vec<Arc<AtomicBool>>>,
}

// `cfg(test)` lets the helpers unwrap (rust/clippy.toml).
#[cfg(test)]
impl Launcher for Fake {
    type Process = FakeVm;

    fn launch(&self, launch: Launch) -> Result<FakeVm, SpawnError> {
        if self.fail.load(Ordering::SeqCst) {
            return Err(SpawnError::Stopped);
        }
        while self.hold.load(Ordering::SeqCst) {
            std::thread::sleep(Duration::from_millis(1));
        }
        Ok(self.record(launch))
    }

    fn cmdline(&self, process: &FakeVm) -> PathBuf {
        self.dir.join(format!("cmdline-{}", process.number))
    }
}

#[cfg(test)]
impl Fake {
    /// Records `launch`, writes its command line file and starts the
    /// fake guest on its vsock socket.
    fn record(&self, launch: Launch) -> FakeVm {
        let mut args = vec![CH.to_owned()];
        args.extend(launch.payload().args().iter().cloned());
        if self.net.load(Ordering::SeqCst) {
            args.push("--net".to_owned());
        }
        let run = launch.files().run_dir().path();
        let listening = [1100, 1101, 1102, 1103, 1104].map(|port| {
            let listener = run.join(format!("vsock.sock_{port}"));
            UnixStream::connect(listener).is_ok()
        });
        self.listening.lock().unwrap().push(listening);
        if let Some(port) = *self.extra.lock().unwrap() {
            UnixListener::bind(run.join(format!("vsock.sock_{port}"))).unwrap();
        }
        let listener = UnixListener::bind(run.join("vsock.sock")).unwrap();
        let guest = *self.guest.lock().unwrap();
        let exited = Arc::new(AtomicBool::new(false));
        let broken = Arc::new(AtomicBool::new(false));
        let vm = Vm {
            exited: Arc::clone(&exited),
            broken: Arc::clone(&broken),
            stop_lines: Arc::clone(&self.stop_lines),
            sync_lines: Arc::clone(&self.sync_lines),
            links: Arc::clone(&self.links),
        };
        spawn(move || play(&listener, guest, &vm));
        let mut launches = self.launches.lock().unwrap();
        let number = launches.len();
        fs::write(self.dir.join(format!("cmdline-{number}")), args.join("\0")).unwrap();
        launches.push(launch);
        self.vms.lock().unwrap().push(Arc::clone(&exited));
        self.broken.lock().unwrap().push(Arc::clone(&broken));
        let flaky = Arc::new(AtomicBool::new(false));
        self.flaky.lock().unwrap().push(Arc::clone(&flaky));
        FakeVm {
            number,
            exited,
            broken,
            flaky,
        }
    }
}

/// The fake CH API and what it saw.
#[derive(Debug)]
struct FakeApi {
    /// The launcher, whose VM a snapshot ends.
    fake: Arc<Fake>,
    /// The run dir of each snapshot.
    snapshots: Mutex<Vec<PathBuf>>,
    /// The `info` reply. `{state}` stands for the state.
    reply: Mutex<String>,
    /// The first this many `info` calls fail, as before CH serves its API.
    fails: AtomicUsize,
    infos: AtomicUsize,
    resumes: AtomicUsize,
    /// Fails each snapshot, and the VM runs on.
    fail_take: AtomicBool,
    /// Holds each snapshot until the test clears it.
    hold: AtomicBool,
    /// A snapshot began.
    entered: AtomicBool,
    /// The `sync` lines that guest init had read at the start of each
    /// snapshot.
    synced_at_take: Mutex<Vec<usize>>,
    /// Writes generation 1 of the session with the key, as a real take
    /// does.
    keep: AtomicBool,
}

#[cfg(test)]
impl ChApi for FakeApi {
    /// Reads the reply as `ChRemote::info` reads the stdout of CH.
    fn info(&self, _vm: &VmRunDir) -> Result<VmInfo, ChRemoteError> {
        self.infos.fetch_add(1, Ordering::SeqCst);
        let fails = self.fails.load(Ordering::SeqCst);
        if fails > 0 {
            self.fails.store(fails - 1, Ordering::SeqCst);
            let source = io::ErrorKind::NotFound.into();
            let program = PathBuf::from("ch-remote");
            return Err(ChRemoteError::Io { program, source });
        }
        let reply = self.reply.lock().unwrap_or_else(PoisonError::into_inner);
        serde_json::from_str(&reply).map_err(ChRemoteError::NotInfo)
    }

    fn resume(&self, _vm: &VmRunDir) -> Result<(), String> {
        self.resumes.fetch_add(1, Ordering::SeqCst);
        Ok(())
    }

    /// Ends the newest fake VM of `vm`, as `shutdown-vmm` ends CH.
    fn snapshot(
        &self,
        vm: &VmRunDir,
        session: &SessionDir,
        key: &SnapshotKey,
    ) -> Result<SnapshotGeneration, String> {
        self.entered.store(true, Ordering::SeqCst);
        let synced = self.fake.sync_lines.load(Ordering::SeqCst);
        self.synced_at_take
            .lock()
            .map_err(|_| "poisoned")?
            .push(synced);
        while self.hold.load(Ordering::SeqCst) {
            std::thread::sleep(Duration::from_millis(1));
        }
        if self.fail_take.load(Ordering::SeqCst) {
            return Err("the take failed".to_owned());
        }
        let launches = self.fake.launches.lock().map_err(|_| "poisoned")?;
        let index = launches
            .iter()
            .rposition(|launch| launch.files().run_dir().path() == vm.path())
            .ok_or("no such VM")?;
        let vms = self.fake.vms.lock().map_err(|_| "poisoned")?;
        vms.get(index)
            .ok_or("no such VM")?
            .store(true, Ordering::SeqCst);
        if self.keep.load(Ordering::SeqCst) {
            let generation = SnapshotGeneration(1);
            fs::create_dir_all(session.snapshot(generation)).map_err(|error| error.to_string())?;
            write_key(&session.snapshot(generation), key).map_err(|error| format!("{error:?}"))?;
        }
        let mut snapshots = self.snapshots.lock().map_err(|_| "poisoned")?;
        snapshots.push(vm.path().to_owned());
        Ok(SnapshotGeneration(1))
    }
}

/// What guest init changes in its fake VM.
#[derive(Debug, Clone)]
struct Vm {
    exited: Arc<AtomicBool>,
    broken: Arc<AtomicBool>,
    stop_lines: Arc<AtomicUsize>,
    sync_lines: Arc<AtomicUsize>,
    links: Arc<Mutex<Vec<UnixStream>>>,
}

/// Plays CH and guest init on each link: `CONNECT`, then the control
/// link or the ACP link. Each link stays open until the test ends. A
/// `stop` line on the control link powers the VM off (decision CH19 (3)).
/// A `sync` line gets `synced`, but not from `Guest::NoSync` (decision
/// CH62 rule 23).
#[cfg(test)]
fn play(listener: &UnixListener, guest: Guest, vm: &Vm) {
    let mut held = Vec::new();
    for stream in listener.incoming() {
        let mut reader = BufReader::new(stream.unwrap());
        let mut line = String::new();
        reader.read_line(&mut line).unwrap();
        let mut stream: UnixStream = reader.get_ref().try_clone().unwrap();
        match (line.as_str(), guest) {
            ("CONNECT 1200\n", _) => {
                stream.write_all(b"OK 1\n").unwrap();
                reader.read_line(&mut line).unwrap();
                if guest == Guest::Closes {
                    continue;
                }
                let ready = match guest {
                    Guest::Turn => TURN,
                    Guest::Starting => STARTING,
                    Guest::Ended => ENDED,
                    _ => READY,
                };
                stream.write_all(format!("{ready}\n").as_bytes()).unwrap();
                vm.links.lock().unwrap().push(stream.try_clone().unwrap());
                let vm = vm.clone();
                let mut answer = stream.try_clone().unwrap();
                spawn(move || {
                    for line in reader.lines() {
                        if line.as_deref().is_ok_and(|line| line == r#"{"sync":{}}"#) {
                            vm.sync_lines.fetch_add(1, Ordering::SeqCst);
                            if guest != Guest::NoSync {
                                answer.write_all(b"{\"synced\":{}}\n").unwrap();
                            }
                        } else if line.is_ok_and(|line| line.contains("stop")) {
                            vm.stop_lines.fetch_add(1, Ordering::SeqCst);
                            if guest == Guest::Wedges {
                                std::thread::sleep(Duration::from_millis(20));
                                vm.broken.store(true, Ordering::SeqCst);
                            } else if guest != Guest::Ignores {
                                vm.exited.store(true, Ordering::SeqCst);
                            }
                        }
                    }
                });
            }
            ("CONNECT 1201\n", Guest::RefusesAcp) => stream.write_all(b"NO\n").unwrap(),
            _ => stream.write_all(b"OK 2\n").unwrap(),
        }
        held.push(stream);
    }
}

/// The hook on a scratch dir, with two rows of `chat`.
#[derive(Debug)]
struct Test {
    dir: PathBuf,
    hook: Arc<ColdBoot<Fake, FakeApi>>,
    fake: Arc<Fake>,
    api: Arc<FakeApi>,
    relays: Relays,
    ch: CheckedCh,
    state: StateRoot,
    control: Arc<Mutex<ControlState>>,
    store: Arc<Mutex<Rows>>,
    faults: PathBuf,
    rows: Vec<SessionRow>,
    runtime: Runtime,
}

#[cfg(test)]
impl Test {
    /// The hook, with `edit` applied to its config.
    fn new(name: &str, edit: impl FnOnce(&mut StartConfig)) -> Self {
        let dir = PathBuf::from(format!("/tmp/vmm-start-{}-{name}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        let config_dir = dir.join("families/chat/config");
        fs::create_dir_all(&config_dir).unwrap();
        fs::write(config_dir.join("shape.json"), SHAPE).unwrap();
        fs::create_dir_all(dir.join("faults")).unwrap();
        fs::write(dir.join("image"), b"").unwrap();

        let state = StateRoot::new(dir.join("state")).unwrap();
        let mut store = Rows::open(state.clone()).unwrap();
        let rows: Vec<SessionRow> = [A, B, C]
            .iter()
            .map(|id| {
                let create = format!(
                    r#"{{"created_at":1793577600000,"cwd":"/home/david","family":"chat","id":"{id}","origin":{{"door":{{}}}}}}"#
                );
                store.create(serde_json::from_str(&create).unwrap()).unwrap()
            })
            .collect();
        let stub =
            PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures/cloud-hypervisor");
        let ch = check_cloud_hypervisor(&stub, Duration::from_secs(10)).unwrap();
        let images = ImageStore::new(&state, PathBuf::from("/usr/bin/zstd"));
        let empty: ImageDigest = EMPTY.parse().unwrap();
        images.import(&dir.join("image"), empty).unwrap();
        let mut config = StartConfig {
            state: state.clone(),
            run: RunRoot::new(dir.join("run")).unwrap(),
            families: FamilyRoot::new(dir.join("families")).unwrap(),
            images,
            disks: SessionDisks::new(&state, PathBuf::from("/usr/bin/true")),
            digests: watch::channel(Digests {
                kernel: empty,
                roots: PerFlavor::every(|_| empty),
            })
            .1,
            initramfs: empty,
            ch: Program::new(PathBuf::from(CH)).unwrap(),
            systemd_run: Program::new(PathBuf::from("/usr/bin/systemd-run")).unwrap(),
            vmm_unit: Ok(VmmUnit::new("creche-vmm.service".to_owned()).unwrap()),
            limits: Limits {
                ram_cap: 80 * GIB,
                free_floor: 0,
            },
            budget: BUDGET,
            ch_version: ch,
            planes: Upstreams {
                litellm: NO_PLANE,
                chaperone: NO_PLANE,
            },
            egress_log: Arc::new(Log::open(dir.join("egress.jsonl")).unwrap()),
            mirror: Mirror {
                root: MirrorRoot::new(dir.join("mirror")).unwrap(),
                git: Program::new(PathBuf::from("/usr/bin/git")).unwrap(),
            },
        };
        edit(&mut config);

        let faults = FaultDir::open(&dir.join("faults")).unwrap();
        let path = faults.file(&"chat".parse::<FamilyName>().unwrap());
        let book = FaultBook::start(faults, SystemTime::now()).unwrap();
        let fake = Arc::new(Fake {
            dir: dir.clone(),
            guest: Mutex::new(Guest::Ready),
            net: AtomicBool::new(false),
            extra: Mutex::new(None),
            listening: Mutex::default(),
            fail: AtomicBool::new(false),
            hold: AtomicBool::new(false),
            stop_lines: Arc::default(),
            sync_lines: Arc::default(),
            links: Arc::default(),
            launches: Mutex::default(),
            vms: Mutex::default(),
            broken: Mutex::default(),
            flaky: Mutex::default(),
        });
        let control = Arc::new(Mutex::new(ControlState::default()));
        let store = Arc::new(Mutex::new(store));
        let api = Arc::new(FakeApi {
            fake: Arc::clone(&fake),
            snapshots: Mutex::default(),
            reply: Mutex::default(),
            fails: AtomicUsize::new(0),
            infos: AtomicUsize::new(0),
            resumes: AtomicUsize::new(0),
            fail_take: AtomicBool::new(false),
            hold: AtomicBool::new(false),
            entered: AtomicBool::new(false),
            synced_at_take: Mutex::default(),
            keep: AtomicBool::new(false),
        });
        let relays = Relays::default();
        let hook = Arc::new(ColdBoot::new(
            config,
            Arc::clone(&fake),
            Arc::clone(&api),
            relays.clone(),
            Arc::clone(&store),
            Arc::clone(&control),
            Arc::new(Mutex::new(book)),
        ));
        let runtime = Builder::new_current_thread().enable_all().build().unwrap();
        Self {
            dir,
            hook,
            fake,
            api,
            relays,
            ch,
            state,
            control,
            store,
            faults: path,
            rows,
            runtime,
        }
    }

    /// Starts the VM of row `index`.
    fn start(&self, index: usize) -> Result<(), StartFault> {
        let row = &self.rows[index];
        self.runtime.block_on(self.hook.start(row)).map(|_link| ())
    }

    /// The codes in the fault file of `chat`.
    fn fault_codes(&self) -> Vec<String> {
        let Ok(text) = fs::read(&self.faults) else {
            return Vec::new();
        };
        let body: serde_json::Value = serde_json::from_slice(&text).unwrap();
        let faults = body["faults"].as_array().unwrap();
        faults
            .iter()
            .map(|fault| fault["code"].as_str().unwrap().to_owned())
            .collect()
    }

    fn launches(&self) -> usize {
        self.fake.launches.lock().unwrap().len()
    }

    /// Whether launch `index` was stopped.
    fn stopped(&self, index: usize) -> bool {
        self.fake.vms.lock().unwrap()[index].load(Ordering::SeqCst)
    }

    /// Whether launch `index` was a restore.
    fn restored(&self, index: usize) -> bool {
        let launches = self.fake.launches.lock().unwrap();
        let args = launches[index].payload().args();
        args.contains(&"--restore".to_owned()) && !args.contains(&"--kernel".to_owned())
    }

    /// The key of a VM of `chat`: the fake kernel, initramfs and root
    /// disk.
    fn key(&self) -> SnapshotKey {
        let images = ImageStore::new(&self.state, PathBuf::from("/usr/bin/zstd"));
        let empty = images.check(EMPTY.parse().unwrap()).unwrap();
        SnapshotKey::current(self.ch, &empty, &empty, &empty)
    }

    /// Makes generation 1 of row `index` with `key`, or with a key of
    /// another CH rev when `key` is `None`.
    fn snapshot(&self, index: usize, key: Option<&SnapshotKey>) -> PathBuf {
        let session = self.state.session(&self.rows[index].id);
        let dir = session.snapshot(SnapshotGeneration(1));
        fs::create_dir_all(&dir).unwrap();
        match key {
            Some(key) => write_key(&dir, key).unwrap(),
            None => {
                let other = format!(
                    r#"{{"ch_rev":"0","kernel":"{EMPTY}","root_disk":"{EMPTY}","guest_init_protocol":1}}"#
                );
                fs::write(dir.join("key.json"), other).unwrap();
            }
        }
        dir
    }

    /// The two disk entries of row `index` in the shape of the CH v54
    /// `info` reply: the root disk, then the session disk.
    fn disks(&self, index: usize) -> [serde_json::Value; 2] {
        let id = &self.rows[index].id;
        let root = self.dir.join(format!("state/store/{EMPTY}"));
        let disk = self.dir.join(format!("state/sessions/{id}/disk.ext4"));
        [(root, true, "_disk0"), (disk, false, "_disk1")].map(|(path, readonly, name)| {
            serde_json::json!({"path": path, "readonly": readonly, "direct": false,
                "num_queues": 1, "queue_size": 128, "vhost_user": false,
                "id": name, "pci_segment": 0, "sparse": true, "image_type": "Raw"})
        })
    }

    /// Sets the `info` reply of a restore of row `index` in the shape of
    /// CH v54: `state`, the two disks and the vsock socket of the VM, then
    /// `extra` fields.
    fn reply(&self, index: usize, state: &str, extra: &[(&str, serde_json::Value)]) {
        let id = &self.rows[index].id;
        let vsock = self.dir.join(format!("run/vm/{id}/vsock.sock"));
        let mut config = serde_json::json!({
            "cpus": {"boot_vcpus": 1, "max_vcpus": 1},
            "memory": {"size": 536_870_912, "shared": false},
            "disks": self.disks(index),
            "serial": {"file": self.dir.join(format!("state/sessions/{id}/serial.log")), "mode": "File"},
            "vsock": {"cid": 3, "socket": vsock, "id": "_vsock0", "pci_segment": 0},
        });
        for (field, value) in extra {
            config[*field] = value.clone();
        }
        let reply = serde_json::json!({"state": state, "config": config, "memory_actual_size": 0});
        *self.api.reply.lock().unwrap() = reply.to_string();
    }

    /// How many `stop` lines guest init read.
    fn stop_lines(&self) -> usize {
        self.fake.stop_lines.load(Ordering::SeqCst)
    }
}

/// Waits up to 5 s for `done`.
#[cfg(test)]
fn eventually(done: impl Fn() -> bool) {
    let deadline = Instant::now() + Duration::from_secs(5);
    while !done() {
        assert!(
            Instant::now() < deadline,
            "the condition did not hold in 5 s"
        );
        std::thread::sleep(Duration::from_millis(1));
    }
}

impl Drop for Test {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.dir);
    }
}

/// A start with no shape faults and launches nothing. A later start
/// boots the VM with the checked files and clears the fault (contract 05
/// §3.3.1 rule 8).
#[test]
fn a_cold_boot_launches_the_checked_files_and_clears_the_fault() {
    let test = Test::new("boot", |_| {});
    let shape = test.dir.join("families/chat/config/shape.json");
    fs::remove_file(&shape).unwrap();
    assert_eq!(test.start(0), Err(StartFault::SandboxStartFailed));
    assert_eq!(test.fault_codes(), ["sandbox_start_failed"]);
    assert_eq!(test.launches(), 0);

    fs::write(&shape, SHAPE).unwrap();
    assert_eq!(test.start(0), Ok(()));
    assert!(test.fault_codes().is_empty());
    assert!(test.hook.running(&test.rows[0].id));
    let id = &test.rows[0].id;
    assert!(test.control.lock().unwrap().facade(id).is_some());

    let launches = test.fake.launches.lock().unwrap();
    let launch = &launches[0];
    assert_eq!(launch.scope().unit(), format!("creche-vm-{A}"));
    let args = launch.payload().args();
    let store = test.dir.join(format!("state/store/{EMPTY}"));
    let kernel = args.iter().position(|arg| arg == "--kernel").unwrap();
    assert_eq!(args[kernel + 1], store.to_str().unwrap());
    assert!(args.contains(&"size=512M".to_owned()));
    let session = test.dir.join(format!("state/sessions/{A}"));
    assert!(session.join("disk.ext4").is_file());
    assert!(session.join("snapshots").is_dir());
    let serial = session.join("serial.log");
    let mode = fs::metadata(&serial).unwrap().permissions().mode();
    assert_eq!(mode & 0o777, 0o600);
    let at = args.iter().position(|arg| arg == "--serial").unwrap();
    assert_eq!(args[at + 1], format!("file={}", serial.display()));
}

/// The serial log of a session is in the state root, so a restart of the
/// vmm keeps it until the next start of the session (gap G23, decision
/// CH59 rule 2). The test stops the VM, then removes the run root, as
/// each stop of the vmm unit does, a crash included. The log still holds
/// the guest output. The next start of the session makes it new, with
/// mode 0600, and a restore does the same.
#[test]
fn a_vmm_restart_keeps_the_serial_log_until_the_next_start() {
    let test = Test::new("serial", |_| {});
    let id = test.rows[0].id.clone();
    let serial = test.dir.join(format!("state/sessions/{A}/serial.log"));
    for restore in [false, true] {
        assert_eq!(test.start(0), Ok(()));
        fs::write(&serial, b"the last guest\r\n").unwrap();
        fs::set_permissions(&serial, fs::Permissions::from_mode(0o644)).unwrap();
        assert!(test.runtime.block_on(test.hook.stop(&id)).is_ok());
        fs::remove_dir_all(test.dir.join("run")).unwrap();
        assert_eq!(fs::read(&serial).unwrap(), b"the last guest\r\n");

        if restore {
            test.snapshot(0, Some(&test.key()));
            test.reply(0, "Paused", &[]);
        }
        assert_eq!(test.start(0), Ok(()));
        assert_eq!(test.restored(test.launches() - 1), restore);
        assert_eq!(fs::read(&serial).unwrap(), b"");
        let mode = fs::metadata(&serial).unwrap().permissions().mode();
        assert_eq!(mode & 0o777, 0o600);
        assert!(test.runtime.block_on(test.hook.stop(&id)).is_ok());
    }
}

/// A vmm in no service unit refuses every launch and starts nothing
/// (decision CH60 rule 1). The start faults, makes no `vm/<id>` link, and
/// the fake launcher sees no launch. Each launch of a vmm with a unit
/// binds its scope to that unit.
#[test]
fn a_vmm_in_no_service_unit_launches_nothing() {
    let path = "/user.slice/user-1000.slice/session-3.scope".to_owned();
    let test = Test::new("no-unit", |config| {
        config.vmm_unit = Err(UnitError::NoUserManager(path));
    });
    assert_eq!(test.start(0), Err(StartFault::SandboxStartFailed));
    assert_eq!(test.fault_codes(), ["sandbox_start_failed"]);
    assert_eq!(test.launches(), 0);
    let link = test.dir.join(format!("run/vm/{A}"));
    assert!(fs::symlink_metadata(link).is_err());

    let test = Test::new("unit", |_| {});
    assert_eq!(test.start(0), Ok(()));
    let launches = test.fake.launches.lock().unwrap();
    assert_eq!(launches[0].unit().name(), "creche-vmm.service");
}

/// Each VM boots the root disk of the flavor of its family shape
/// (decisions CH8-8, CH58 rule 4). The python disk and the other disks
/// hold other bytes, so a launch that takes the disk of another flavor
/// fails the check of the `--disk` path.
#[test]
fn each_flavor_boots_its_own_root_disk() {
    let python: ImageDigest = HELLO.parse().unwrap();
    let other: ImageDigest = ABC.parse().unwrap();
    let test = Test::new("flavor", |config| {
        let dir = config.state.path().parent().unwrap().to_owned();
        for (digest, bytes) in [(python, "hello world"), (other, "abc")] {
            let file = dir.join(format!("{digest}.disk"));
            fs::write(&file, bytes).unwrap();
            config.images.import(&file, digest).unwrap();
        }
        let kernel = config.digests.borrow().kernel;
        let roots = PerFlavor::every(|flavor| {
            if flavor == SandboxFlavor::Python {
                python
            } else {
                other
            }
        });
        config.digests = watch::channel(Digests { kernel, roots }).1;
    });
    let shape = test.dir.join("families/chat/config/shape.json");
    for (index, flavor, digest) in [(0, "python", python), (1, "base", other)] {
        let json = format!(r#"{{"cpus":1,"flavor":"{flavor}","memory_mib":512}}"#);
        fs::write(&shape, json).unwrap();
        assert_eq!(test.start(index), Ok(()), "{flavor}");
        let launches = test.fake.launches.lock().unwrap();
        let args = launches[index].payload().args();
        let disk = args.iter().position(|arg| arg == "--disk").unwrap();
        let root = test.dir.join(format!("state/store/{digest}"));
        assert_eq!(
            args[disk + 1],
            format!("path={},readonly=on,image_type=raw", root.display()),
            "{flavor}"
        );
    }
}

/// A second start of a VM that runs dials it and launches nothing. A
/// start after the VM exited boots it again.
#[test]
fn a_running_vm_is_dialed_and_an_exited_one_boots_again() {
    let test = Test::new("again", |_| {});
    assert_eq!(test.start(0), Ok(()));
    assert_eq!(test.start(0), Ok(()));
    assert_eq!(test.launches(), 1);

    test.fake.vms.lock().unwrap()[0].store(true, Ordering::SeqCst);
    assert_eq!(test.start(0), Ok(()));
    assert_eq!(test.launches(), 2);
}

/// A VM that does not fit gets `host_full` with no fault and no launch
/// when no VM may be evicted: a turn runs (decisions CH8-6, CH21-3). The
/// cap holds one VM of 512 MiB.
#[test]
fn a_vm_past_the_cap_is_host_full() {
    let test = Test::new("full", |config| config.limits.ram_cap = 512 << 20);
    *test.fake.guest.lock().unwrap() = Guest::Turn;
    assert_eq!(test.start(0), Ok(()));
    assert_eq!(test.start(1), Err(StartFault::HostFull));
    assert_eq!(test.launches(), 1);
    assert!(test.fault_codes().is_empty());
    assert!(!test.hook.running(&test.rows[1].id));
    assert!(test.api.snapshots.lock().unwrap().is_empty());
}

/// A failed launch frees the RAM that admission reserved, so the next
/// start of the full host still fits.
#[test]
fn a_failed_launch_faults_and_frees_its_ram() {
    let test = Test::new("launch", |config| config.limits.ram_cap = 512 << 20);
    test.fake.fail.store(true, Ordering::SeqCst);
    assert_eq!(test.start(0), Err(StartFault::SandboxStartFailed));
    assert_eq!(test.fault_codes(), ["sandbox_start_failed"]);
    test.fake.fail.store(false, Ordering::SeqCst);
    assert_eq!(test.start(1), Ok(()));
}

/// A store copy with no identity fails the start, and so does a failed
/// `mke2fs` (decision CH44 rule 2, PR D7). Neither launches.
#[test]
fn a_bad_image_or_disk_faults_before_the_launch() {
    let test = Test::new("image", |config| {
        let empty: ImageDigest = EMPTY.parse().unwrap();
        fs::remove_file(config.images.identity_path(&empty)).unwrap();
    });
    assert_eq!(test.start(0), Err(StartFault::SandboxStartFailed));
    assert_eq!(test.launches(), 0);

    let test = Test::new("disk", |config| {
        config.disks = SessionDisks::new(&config.state, PathBuf::from("/usr/bin/false"));
    });
    assert_eq!(test.start(0), Err(StartFault::SandboxStartFailed));
    assert_eq!(test.fault_codes(), ["sandbox_start_failed"]);
    assert_eq!(test.launches(), 0);
}

/// A guest that closes before `ControlReady`, and a refused dial of
/// 1201, each fault and stop the VM (decisions CH33 rule 6, CH50 rule 2).
/// After `ControlReady` the stop is the graceful one on the control link
/// (decision CH19 (3)). Before it no link exists.
#[test]
fn a_guest_fault_stops_the_vm() {
    for (name, guest, graceful) in [("closes", Guest::Closes, 0), ("acp", Guest::RefusesAcp, 1)] {
        let test = Test::new(name, |_| {});
        *test.fake.guest.lock().unwrap() = guest;
        assert_eq!(test.start(0), Err(StartFault::SandboxStartFailed));
        assert_eq!(test.fault_codes(), ["sandbox_start_failed"]);
        assert!(test.stopped(0));
        assert_eq!(test.stop_lines(), graceful, "{name}");
        assert!(!test.hook.running(&test.rows[0].id));
    }
}

/// A command line with `--net` fails the deny check: the fault is
/// `egress_assert_failed`, and the VM stops (decision CH6-3).
#[test]
fn a_network_card_fails_the_deny_check() {
    let test = Test::new("net", |_| {});
    test.fake.net.store(true, Ordering::SeqCst);
    assert_eq!(test.start(0), Err(StartFault::EgressAssertFailed));
    assert_eq!(test.fault_codes(), ["egress_assert_failed"]);
    assert!(test.stopped(0));
    assert_eq!(test.stop_lines(), 1);
    assert!(!test.hook.running(&test.rows[0].id));
}

/// A snapshot with the current key restores paused: `info` is read until
/// CH answers, the device check passes, then `resume` (decisions CH2,
/// CH50 rule 10). The claim marks the snapshot consumed, and the
/// generation is in use while the VM runs and no longer after it exited
/// (decisions CH19 (5), CH50 rule 8).
#[test]
fn a_keyed_snapshot_restores_paused_then_resumes() {
    let test = Test::new("restore", |_| {});
    let dir = test.snapshot(0, Some(&test.key()));
    test.reply(0, "Paused", &[]);
    test.api.fails.store(2, Ordering::SeqCst);
    assert_eq!(test.start(0), Ok(()));
    assert!(test.restored(0));
    assert_eq!(test.api.infos.load(Ordering::SeqCst), 3);
    assert_eq!(test.api.resumes.load(Ordering::SeqCst), 1);
    assert!(dir.join("consumed").is_file());
    let id = test.rows[0].id.clone();
    assert!(test.hook.in_use().contains(&(id, SnapshotGeneration(1))));
    assert!(test.fault_codes().is_empty());

    // The CH of the restore exits, and a sweep drops the consumed
    // generation. The next start finds the VM gone, which releases the
    // generation (decision CH50 rule 8), and cold-boots.
    test.fake.vms.lock().unwrap()[0].store(true, Ordering::SeqCst);
    fs::remove_dir_all(&dir).unwrap();
    assert_eq!(test.start(0), Ok(()));
    assert!(!test.restored(1));
    assert!(test.hook.in_use().is_empty());
}

/// Every start names the initramfs of the config: a boot names it and its
/// ruleset reads it, and the key holds its digest. A restore with that key
/// carries the same `store/<sha256>` file, so its ruleset reads it too,
/// and a key of another initramfs drops the snapshot (decisions CH60 rule
/// 2, CH61 rule 3, CH62 rule 18).
#[test]
fn every_start_names_the_initramfs() {
    let initramfs: ImageDigest = ABC.parse().unwrap();
    let with = |config: &mut StartConfig| {
        let file = config.state.path().parent().unwrap().join("initramfs");
        fs::write(&file, "abc").unwrap();
        config.images.import(&file, initramfs).unwrap();
        config.initramfs = initramfs;
    };
    let store = |test: &Test| test.dir.join(format!("state/store/{initramfs}"));

    let test = Test::new("initramfs-boot", with);
    assert_eq!(test.start(0), Ok(()));
    let launches = test.fake.launches.lock().unwrap();
    let args = launches[0].payload().args();
    let at = args.iter().position(|arg| arg == "--initramfs").unwrap();
    assert_eq!(args[at + 1], store(&test).to_str().unwrap());
    let Start::Boot {
        initramfs: read, ..
    } = launches[0].files().start()
    else {
        panic!("not a boot: {:?}", launches[0].files());
    };
    assert_eq!(read.path(), store(&test));
    drop(launches);

    let test = Test::new("initramfs-restore", with);
    let images = ImageStore::new(&test.state, PathBuf::from("/usr/bin/zstd"));
    let empty = images.check(EMPTY.parse().unwrap()).unwrap();
    let checked = images.check(initramfs).unwrap();
    let key = SnapshotKey::current(test.ch, &empty, &checked, &empty);
    test.snapshot(0, Some(&key));
    test.reply(0, "Paused", &[]);
    assert_eq!(test.start(0), Ok(()));
    assert!(test.restored(0));
    let launches = test.fake.launches.lock().unwrap();
    let path = HostPath::new(store(&test)).unwrap();
    let restore = Start::Restore { initramfs: path };
    assert_eq!(launches[0].files().start(), &restore);
    drop(launches);

    let test = Test::new("initramfs-mismatch", with);
    let dir = test.snapshot(0, Some(&test.key()));
    assert_eq!(test.start(0), Ok(()));
    assert!(!test.restored(0));
    assert!(!dir.exists());
}

/// Fields that a test adds to the config of an `info` reply.
type Extra<'a> = Vec<(&'a str, serde_json::Value)>;

/// A restored VM that is not paused, or that has a device past the fixed
/// set, never resumes. The fault is `egress_assert_failed`, the VM stops,
/// and its generation is no longer in use (decisions CH42 rule 2, CH50
/// rule 10). Each entry has the shape of CH v54.
#[test]
fn a_refused_device_set_stops_the_restore() {
    let other = serde_json::json!({"cid": 3, "socket": "/tmp/other.sock", "id": "_vsock0"});
    let net = serde_json::json!([{"tap": "tap0", "num_queues": 2, "queue_size": 256,
        "vhost_user": false, "vhost_mode": "Client", "id": "_net0", "pci_segment": 0}]);
    let user = serde_json::json!([{"socket": "/tmp/u.sock", "id": "_user0", "pci_segment": 0}]);
    let tpm = serde_json::json!({"socket": "/tmp/tpm.sock"});
    let cases: [(&str, &str, Extra); 8] = [
        ("running", "Running", vec![]),
        ("net", "Paused", vec![("net", net)]),
        ("user", "Paused", vec![("user_devices", user)]),
        ("tpm", "Paused", vec![("tpm", tpm)]),
        // The two cases below get their disks from the test dir.
        ("disks", "Paused", vec![]),
        ("vhost", "Paused", vec![]),
        ("vsock", "Paused", vec![("vsock", other)]),
        // A vsock with no socket is not a VM info.
        (
            "shape",
            "Paused",
            vec![("vsock", serde_json::json!({"cid": 3}))],
        ),
    ];
    for (name, state, mut extra) in cases {
        let test = Test::new(&format!("refuse-{name}"), |_| {});
        let [root, session] = test.disks(0);
        if name == "disks" {
            let mut third = session.clone();
            third["path"] = serde_json::json!("/tmp/third.img");
            extra.push(("disks", serde_json::json!([root, session, third])));
        } else if name == "vhost" {
            // CH v54 opens the socket and ignores the path (PR D15).
            let mut vhost = root;
            vhost["vhost_user"] = serde_json::json!(true);
            vhost["vhost_socket"] = serde_json::json!("/tmp/vhost.sock");
            extra.push(("disks", serde_json::json!([vhost, session])));
        }
        test.snapshot(0, Some(&test.key()));
        test.reply(0, state, &extra);
        assert_eq!(test.start(0), Err(StartFault::EgressAssertFailed), "{name}");
        assert_eq!(test.api.infos.load(Ordering::SeqCst), 1, "{name}");
        assert_eq!(test.api.resumes.load(Ordering::SeqCst), 0, "{name}");
        assert_eq!(test.fault_codes(), ["egress_assert_failed"], "{name}");
        assert!(test.stopped(0), "{name}");
        assert!(test.hook.in_use().is_empty(), "{name}");
    }
}

/// Every restore runs the device check, since the one CH API reads the
/// device set (PR D15, decision CH50 rule 10). The fake reports a changed
/// device set, so the restore faults and never resumes. The claim
/// consumed the generation, so the next start cold-boots and reads no
/// `info` (decisions CH19 (5), CH62 rule 23).
#[test]
fn a_claim_always_runs_the_device_check() {
    let test = Test::new("device-check", |_| {});
    let dir = test.snapshot(0, Some(&test.key()));
    let net = serde_json::json!([{"tap": "tap0", "num_queues": 2, "queue_size": 256,
        "vhost_user": false, "vhost_mode": "Client", "id": "_net0", "pci_segment": 0}]);
    test.reply(0, "Paused", &[("net", net)]);
    assert_eq!(test.start(0), Err(StartFault::EgressAssertFailed));
    assert!(test.restored(0));
    assert_eq!(test.api.infos.load(Ordering::SeqCst), 1);
    assert_eq!(test.api.resumes.load(Ordering::SeqCst), 0);
    assert!(dir.join("consumed").is_file());
    assert!(test.hook.in_use().is_empty());

    assert_eq!(test.start(0), Ok(()));
    assert_eq!(test.launches(), 2);
    assert!(!test.restored(1));
    assert_eq!(test.api.infos.load(Ordering::SeqCst), 1);
    assert!(test.fault_codes().is_empty());
}

/// A key of another release drops the snapshot, and a consumed one is
/// not restored again. Each start cold-boots (decisions CH7-6, CH19 (5),
/// CH44 rule 5).
#[test]
fn a_key_mismatch_or_a_consumed_snapshot_cold_boots() {
    let test = Test::new("mismatch", |_| {});
    let dir = test.snapshot(0, None);
    assert_eq!(test.start(0), Ok(()));
    assert!(!test.restored(0));
    assert!(!dir.exists());

    let test = Test::new("consumed", |_| {});
    let dir = test.snapshot(0, Some(&test.key()));
    fs::write(dir.join("consumed"), b"").unwrap();
    assert_eq!(test.start(0), Ok(()));
    assert!(!test.restored(0));
    assert_eq!(test.api.infos.load(Ordering::SeqCst), 0);
    assert!(test.hook.in_use().is_empty());
}

#[cfg(test)]
impl Test {
    /// The session ids of the snapshots, in order.
    fn snapshot_ids(&self) -> Vec<String> {
        let snapshots = self.api.snapshots.lock().unwrap();
        snapshots
            .iter()
            .map(|dir| dir.file_name().unwrap().to_str().unwrap().to_owned())
            .collect()
    }
}

/// Past the cap, admission evicts the least recently used idle VM: its
/// snapshot ends it, and the new VM starts. A VM with an open door counts
/// as used now (decisions CH8-6, CH50 rule 6).
#[test]
fn an_idle_vm_is_evicted_least_recently_used_first() {
    let test = Test::new("evict", |config| config.limits.ram_cap = 1 << 30);
    assert_eq!(test.start(0), Ok(()));
    assert_eq!(test.start(1), Ok(()));
    assert_eq!(test.start(2), Ok(()));
    assert_eq!(test.snapshot_ids(), [A]);
    assert!(!test.hook.running(&test.rows[0].id));
    assert!(test.hook.running(&test.rows[1].id));

    // B is older than C, but its door is open.
    let _door = test.relays.claim(&test.rows[1].id).unwrap();
    assert_eq!(test.start(0), Ok(()));
    assert_eq!(test.snapshot_ids(), [A, C]);
    assert!(test.hook.running(&test.rows[1].id));
    assert!(test.fault_codes().is_empty());
}

/// An eviction closes the open door link of the VM first, then takes
/// the snapshot (decision CH19 (6)).
#[test]
fn an_eviction_closes_the_door_link_first() {
    let test = Test::new("door", |config| config.limits.ram_cap = 512 << 20);
    assert_eq!(test.start(0), Ok(()));
    let mut door = test.relays.claim(&test.rows[0].id).unwrap();
    // The relay of a door ends when the stop fires, and so does its claim.
    let relay = spawn(move || {
        while !door.stopped() {
            std::thread::sleep(Duration::from_millis(1));
        }
    });
    assert_eq!(test.start(1), Ok(()));
    relay.join().unwrap();
    assert_eq!(test.snapshot_ids(), [A]);
    assert!(test.hook.running(&test.rows[1].id));
}

/// A turn that starts before the door link closes keeps the VM: it goes
/// back with no snapshot, and the start gets `host_full` (decision CH19
/// (6)).
#[test]
fn a_turn_before_the_close_keeps_the_vm() {
    let test = Test::new("turn", |config| config.limits.ram_cap = 512 << 20);
    assert_eq!(test.start(0), Ok(()));
    let id = test.rows[0].id.clone();
    let mut door = test.relays.claim(&id).unwrap();
    let control = Arc::clone(&test.control);
    let relay = spawn(move || {
        while !door.stopped() {
            std::thread::sleep(Duration::from_millis(1));
        }
        // The prompt that the door carried started a turn.
        let mut control = control.lock().unwrap();
        let link = control.link(&id).unwrap();
        let start = serde_json::from_str(r#"{"start":{}}"#).unwrap();
        let _ = control.turn(&id, link, &start);
    });
    assert_eq!(test.start(1), Err(StartFault::HostFull));
    relay.join().unwrap();
    assert!(test.api.snapshots.lock().unwrap().is_empty());
    assert!(test.hook.running(&test.rows[0].id));
    assert!(!test.hook.running(&test.rows[1].id));
    assert_eq!(test.launches(), 1);
}

#[cfg(test)]
impl Test {
    /// Runs the idle check `after` from now.
    fn sleep_after(&self, after: Duration) -> Vec<SessionId> {
        let now = Instant::now() + after;
        self.runtime.block_on(self.hook.sleep_idle(now))
    }

    /// Applies the turn event `line` to the VM of row `index`, as the
    /// reader of the control link does.
    fn turn(&self, index: usize, line: &str) {
        let id = &self.rows[index].id;
        let mut control = self.control.lock().unwrap();
        let link = control.link(id).unwrap();
        let _ = control.turn(id, link, &serde_json::from_str(line).unwrap());
    }
}

/// Just past the idle timeout.
const PAST_IDLE: Duration = IDLE_TIMEOUT.saturating_add(Duration::from_secs(1));

/// A VM idle past `IDLE_TIMEOUT` sleeps: its snapshot ends it, and it
/// leaves the table. Before the timeout it stays (decisions CH2, CH36
/// G12).
#[test]
fn an_idle_vm_sleeps_after_the_timeout() {
    let test = Test::new("sleep", |_| {});
    assert_eq!(test.start(0), Ok(()));
    assert!(test.sleep_after(Duration::ZERO).is_empty());
    assert!(test.api.snapshots.lock().unwrap().is_empty());

    assert_eq!(test.sleep_after(PAST_IDLE), [test.rows[0].id.clone()]);
    assert_eq!(test.snapshot_ids(), [A]);
    assert!(test.stopped(0));
    assert!(!test.hook.running(&test.rows[0].id));
}

/// A turn that runs or an open door link keeps the VM awake (decision
/// CH19 (6)).
#[test]
fn a_turn_or_an_open_door_keeps_the_vm_awake() {
    let test = Test::new("awake-turn", |_| {});
    *test.fake.guest.lock().unwrap() = Guest::Turn;
    assert_eq!(test.start(0), Ok(()));
    assert!(test.sleep_after(PAST_IDLE).is_empty());

    let test = Test::new("awake-door", |_| {});
    assert_eq!(test.start(0), Ok(()));
    let door = test.relays.claim(&test.rows[0].id).unwrap();
    assert!(test.sleep_after(PAST_IDLE).is_empty());
    assert!(test.api.snapshots.lock().unwrap().is_empty());
    assert!(test.hook.running(&test.rows[0].id));

    // A refused Upgrade records no door close, so after it the VM sleeps
    // at once.
    drop(door);
    assert_eq!(test.sleep_after(PAST_IDLE).len(), 1);
}

/// A VM whose ready said `starting` is up only after a `facade` line
/// says `running`, and a turn event alone does not make it up (decisions
/// CH32 rule 6, CH62 rule 11). Each turn event sets its last use
/// (decision CH50 rule 5).
#[test]
fn the_gate_opens_on_a_running_facade_line() {
    let test = Test::new("seen", |_| {});
    *test.fake.guest.lock().unwrap() = Guest::Starting;
    assert_eq!(test.start(0), Ok(()));
    let id = test.rows[0].id.clone();
    test.turn(0, r#"{"start":{}}"#);
    test.turn(0, r#"{"end":{"title":"First","updated_at":2000}}"#);
    assert!(test.sleep_after(PAST_IDLE).is_empty());

    // A `facade` line has the bytes of the `ControlReady` of its state.
    test.send(READY);
    test.settle(|test| {
        let control = test.control.lock().unwrap();
        matches!(control.facade(&id), Some(FacadeState::Running(_)))
    });
    std::thread::sleep(Duration::from_millis(5));
    let before = Instant::now();
    test.turn(0, r#"{"start":{}}"#);
    test.turn(0, r#"{"end":{"title":"First","updated_at":2000}}"#);
    let now = before + IDLE_TIMEOUT;
    assert!(test.runtime.block_on(test.hook.sleep_idle(now)).is_empty());
    assert_eq!(test.sleep_after(PAST_IDLE).len(), 1);
}

/// The sweep keeps the consumed generation of a VM that restored from
/// it. After that VM sleeps, the next sweep drops it (decision CH50 rule
/// 8).
#[test]
fn the_sweep_skips_a_snapshot_in_use() {
    let test = Test::new("sweep", |_| {});
    let dir = test.snapshot(0, Some(&test.key()));
    test.reply(0, "Paused", &[]);
    assert_eq!(test.start(0), Ok(()));
    assert!(test.restored(0));

    let swept = test.runtime.block_on(test.hook.sweep()).unwrap();
    assert!(swept.dropped.is_empty());
    assert!(dir.is_dir());

    assert_eq!(test.sleep_after(PAST_IDLE).len(), 1);
    assert!(test.hook.in_use().is_empty());
    let swept = test.runtime.block_on(test.hook.sweep()).unwrap();
    let reasons: Vec<Reason> = swept.dropped.iter().map(|each| each.reason).collect();
    assert_eq!(reasons, [Reason::Consumed]);
    assert!(!dir.exists());
}

/// The sweep reads the budget of `StartConfig`, the key
/// `VMM_SNAPSHOT_BUDGET_GIB` (gap G14). A budget of 1 byte drops a keyed
/// generation that `BUDGET` keeps.
#[test]
fn the_sweep_drops_past_the_config_budget() {
    let test = Test::new("budget-design", |_| {});
    let dir = test.snapshot(0, Some(&test.key()));
    let swept = test.runtime.block_on(test.hook.sweep()).unwrap();
    assert!(swept.dropped.is_empty());
    assert!(dir.is_dir());

    let test = Test::new("budget-small", |config| config.budget = 1);
    let dir = test.snapshot(0, Some(&test.key()));
    let swept = test.runtime.block_on(test.hook.sweep()).unwrap();
    let reasons: Vec<Reason> = swept.dropped.iter().map(|each| each.reason).collect();
    assert_eq!(reasons, [Reason::Budget]);
    assert!(!dir.exists());
}

/// A start that is dropped during its launch leaves no CH outside the
/// table. The launch output holds the process, and its drop kills the VM
/// and keeps it in the table (decisions CH8-6, CH50 rule 4). The next
/// admission reaps the exited VM, so a start of another session fits the
/// cap of one VM.
#[test]
fn a_start_dropped_during_its_launch_kills_and_keeps_the_vm() {
    let test = Test::new("cancel", |config| config.limits.ram_cap = 512 << 20);
    test.fake.hold.store(true, Ordering::SeqCst);
    let start = test.hook.start(&test.rows[0]);
    let cut = test
        .runtime
        .block_on(async { tokio::time::timeout(Duration::from_millis(50), start).await });
    assert!(cut.is_err());
    test.fake.hold.store(false, Ordering::SeqCst);
    eventually(|| test.hook.running(&test.rows[0].id));
    assert!(test.stopped(0));
    assert_eq!(test.start(1), Ok(()));
    assert!(!test.hook.running(&test.rows[0].id));
}

/// The turn end of the first `ControlReady` goes to the row: its
/// `updated_at` and its first title (decision CH32 rule 4).
#[test]
fn the_first_ready_writes_its_turn_end_to_the_row() {
    let test = Test::new("ended", |_| {});
    *test.fake.guest.lock().unwrap() = Guest::Ended;
    assert_eq!(test.start(0), Ok(()));
    let store = test.store.lock().unwrap();
    let row = store.get(&test.rows[0].id).unwrap();
    assert_eq!(
        row.title.as_ref().map(ToString::to_string).as_deref(),
        Some("First")
    );
    assert_ne!(row.updated_at, test.rows[0].updated_at);
}

/// Runs `first` and `second` at once and returns both outputs. The tokio
/// feature `macros` is off, so the tests have no `join!`.
#[cfg(test)]
async fn both<F: Future, G: Future>(first: F, second: G) -> (F::Output, G::Output) {
    let (mut first, mut second) = (Box::pin(first), Box::pin(second));
    let (mut one, mut two) = (None, None);
    std::future::poll_fn(|context| {
        if one.is_none()
            && let std::task::Poll::Ready(output) = first.as_mut().poll(context)
        {
            one = Some(output);
        }
        if two.is_none()
            && let std::task::Poll::Ready(output) = second.as_mut().poll(context)
        {
            two = Some(output);
        }
        if one.is_some() && two.is_some() {
            std::task::Poll::Ready(())
        } else {
            std::task::Poll::Pending
        }
    })
    .await;
    (one.unwrap(), two.unwrap())
}

/// A failed take is logged, and the eviction still stops the VM with the
/// graceful stop, so the start fits (decision CH44 rule 4).
#[test]
fn a_failed_take_still_stops_the_evicted_vm() {
    let test = Test::new("take", |config| config.limits.ram_cap = 512 << 20);
    assert_eq!(test.start(0), Ok(()));
    test.api.fail_take.store(true, Ordering::SeqCst);
    assert_eq!(test.start(1), Ok(()));
    assert!(test.api.snapshots.lock().unwrap().is_empty());
    assert!(test.stopped(0));
    assert_eq!(test.stop_lines(), 1);
    assert!(!test.hook.running(&test.rows[0].id));
    assert!(test.hook.running(&test.rows[1].id));
}

/// An evicted VM whose stop fails goes back and keeps its RAM, and the
/// VMs left in the list go back with no snapshot. The start gets
/// `host_full`, and so does a retry (decision CH50 rule 4). A shape of
/// 1 GiB needs both VMs of 512 MiB evicted.
#[test]
fn a_failed_eviction_stop_keeps_the_vm_and_its_ram() {
    let test = Test::new("hung", |config| config.limits.ram_cap = 1 << 30);
    assert_eq!(test.start(0), Ok(()));
    assert_eq!(test.start(1), Ok(()));
    let shape = test.dir.join("families/chat/config/shape.json");
    fs::write(shape, r#"{"cpus":1,"flavor":"base","memory_mib":1024}"#).unwrap();
    test.fake.broken.lock().unwrap()[0].store(true, Ordering::SeqCst);
    assert_eq!(test.start(2), Err(StartFault::HostFull));
    assert_eq!(test.snapshot_ids(), [A]);
    assert!(test.hook.running(&test.rows[0].id));
    assert!(test.hook.running(&test.rows[1].id));
    assert_eq!(test.start(2), Err(StartFault::HostFull));
    assert_eq!(test.snapshot_ids(), [A]);
    assert_eq!(test.launches(), 2);
}

/// A start of a session whose VM is under eviction gets `host_full` at
/// once and launches nothing (decisions CH19 (6), CH21-3). The cap is
/// 1792 MiB. C (1 GiB) evicts A. A second start of A (256 MiB) would fit
/// by evicting B, so only the `Evicting` slot refuses it.
#[test]
fn a_start_under_eviction_is_host_full() {
    let test = Test::new("evicting", |config| config.limits.ram_cap = 1792 << 20);
    let shape = |mib: u32| {
        let path = test.dir.join("families/chat/config/shape.json");
        let json = format!(r#"{{"cpus":1,"flavor":"base","memory_mib":{mib}}}"#);
        fs::write(path, json).unwrap();
    };
    assert_eq!(test.start(0), Ok(()));
    assert_eq!(test.start(1), Ok(()));
    shape(1024);
    test.api.hold.store(true, Ordering::SeqCst);
    let (evicts, again) = test
        .runtime
        .block_on(both(test.hook.start(&test.rows[2]), async {
            while !test.api.entered.load(Ordering::SeqCst) {
                tokio::time::sleep(Duration::from_millis(1)).await;
            }
            shape(256);
            let again = test.hook.start(&test.rows[0]);
            let again = tokio::time::timeout(Duration::from_secs(2), again).await;
            test.api.hold.store(false, Ordering::SeqCst);
            again.map(|started| started.map(|_link| ()))
        }));
    assert!(evicts.is_ok());
    assert_eq!(again, Ok(Err(StartFault::HostFull)));
    assert_eq!(test.snapshot_ids(), [A]);
    assert!(test.hook.running(&test.rows[1].id));
    assert_eq!(test.launches(), 3);
}

/// An eviction and an idle sleep each sync the guest before the take
/// (item 5.7 of the design, decision CH62 rule 23). Guest init read one
/// more `sync` line before each snapshot began. The take ends CH, so no
/// `stop` line follows.
#[test]
fn retire_syncs_before_the_take() {
    let test = Test::new("sync-take", |config| config.limits.ram_cap = 512 << 20);
    assert_eq!(test.start(0), Ok(()));
    assert_eq!(test.start(1), Ok(()));
    assert_eq!(test.sleep_after(PAST_IDLE), [test.rows[1].id.clone()]);
    assert_eq!(test.snapshot_ids(), [A, B]);
    assert_eq!(*test.api.synced_at_take.lock().unwrap(), [1, 2]);
    assert_eq!(test.stop_lines(), 0);
}

#[cfg(test)]
impl Test {
    /// Runs the idle check past the idle timeout on paused time, from
    /// now on, and gives the paused time that it took. A guest that never
    /// answers `sync` then costs no real `SYNC_WAIT`.
    fn sleep_paused(&self) -> (Vec<SessionId>, Duration) {
        let now = Instant::now() + PAST_IDLE;
        self.runtime.block_on(async {
            tokio::time::pause();
            let start = tokio::time::Instant::now();
            let slept = self.hook.sleep_idle(now).await;
            (slept, start.elapsed())
        })
    }
}

/// A guest that sends no `synced` within `SYNC_WAIT` gets no snapshot,
/// and the graceful stop runs (decision CH62 rule 23). A take would write
/// generation 1, and none exists. The VM leaves the table.
#[test]
fn retire_takes_no_snapshot_without_synced() {
    let test = Test::new("no-synced", |_| {});
    *test.fake.guest.lock().unwrap() = Guest::NoSync;
    test.api.keep.store(true, Ordering::SeqCst);
    assert_eq!(test.start(0), Ok(()));
    let (slept, took) = test.sleep_paused();
    assert_eq!(slept, [test.rows[0].id.clone()]);
    assert!(took >= SYNC_WAIT, "{took:?}");
    assert!(!test.api.entered.load(Ordering::SeqCst));
    let session = test.state.session(&test.rows[0].id);
    assert!(!session.snapshot(SnapshotGeneration(1)).exists());
    assert_eq!(test.fake.sync_lines.load(Ordering::SeqCst), 1);
    eventually(|| test.stop_lines() == 1);
    assert!(test.stopped(0));
    assert!(!test.hook.running(&test.rows[0].id));
}

#[cfg(test)]
impl Test {
    /// Shuts the hook down, with no sleep loop (plan PR L19).
    fn shut_down(&self) -> Ended {
        let (stop, _signal) = Stop::pair();
        self.runtime.block_on(shutdown(&*self.hook, stop, async {}))
    }
}

/// A shutdown closes the open door link first. Then the idle VM takes its
/// snapshot, and the VM with a turn running gets the graceful stop (gap
/// G16, decision CH19 (6)).
#[test]
fn a_shutdown_snapshots_an_idle_vm_and_stops_a_busy_one() {
    let test = Test::new("shutdown", |_| {});
    assert_eq!(test.start(0), Ok(()));
    *test.fake.guest.lock().unwrap() = Guest::Turn;
    assert_eq!(test.start(1), Ok(()));
    let mut door = test.relays.claim(&test.rows[0].id).unwrap();
    let relay = spawn(move || {
        while !door.stopped() {
            std::thread::sleep(Duration::from_millis(1));
        }
    });
    assert_eq!(test.shut_down(), Ended::Clean);
    relay.join().unwrap();
    assert_eq!(test.snapshot_ids(), [A]);
    assert_eq!(test.stop_lines(), 1);
    for index in [0, 1] {
        assert!(test.stopped(index));
        assert!(!test.hook.running(&test.rows[index].id));
    }
}

/// A retire whose stop fails after the snapshot puts the VM back
/// (`Kept`). The shutdown then stops it, and it leaves the set (decision
/// CH50 rule 4).
#[test]
fn a_shutdown_stops_a_vm_that_its_retire_kept() {
    let test = Test::new("shutdown-kept", |_| {});
    assert_eq!(test.start(0), Ok(()));
    let flaky = Arc::clone(&test.fake.flaky.lock().unwrap()[0]);
    flaky.store(true, Ordering::SeqCst);
    assert_eq!(test.shut_down(), Ended::Clean);
    assert!(!flaky.load(Ordering::SeqCst));
    assert_eq!(test.snapshot_ids(), [A]);
    assert!(test.stopped(0));
    assert!(!test.hook.running(&test.rows[0].id));
}

/// A VM whose facade is not up fails the gate of admission, so a
/// shutdown takes no snapshot of it. It gets the graceful stop (decision
/// CH19 (6), PR D12).
#[test]
fn a_shutdown_takes_no_snapshot_of_a_vm_that_is_not_up() {
    let test = Test::new("shutdown-starting", |_| {});
    *test.fake.guest.lock().unwrap() = Guest::Starting;
    assert_eq!(test.start(0), Ok(()));
    assert_eq!(test.shut_down(), Ended::Clean);
    assert!(!test.api.entered.load(Ordering::SeqCst));
    assert_eq!(test.stop_lines(), 1);
    assert!(test.stopped(0));
    assert!(!test.hook.running(&test.rows[0].id));
}

/// From the refusal of a shutdown on, a start gets `host_full`: a start
/// of a running VM, and a start that would boot one, which launches
/// nothing. The shutdown then stops the VM (plan PR L19, decision CH54
/// rule 4).
#[test]
fn a_start_during_a_shutdown_is_host_full() {
    let test = Test::new("shutdown-refuse", |_| {});
    assert_eq!(test.start(0), Ok(()));
    test.hook.refuse_starts();
    assert_eq!(test.start(0), Err(StartFault::HostFull));
    assert_eq!(test.start(1), Err(StartFault::HostFull));
    assert_eq!(test.launches(), 1);
    assert_eq!(test.shut_down(), Ended::Clean);
    assert!(!test.hook.running(&test.rows[0].id));
    assert_eq!(test.start(1), Err(StartFault::HostFull));
    assert_eq!(test.launches(), 1);
}

/// The shutdown take syncs the guest first, as the take of a retire does
/// (caller duty 7, decision CH62 rule 23). The take ends CH, so no `stop`
/// line follows.
#[test]
fn the_shutdown_take_syncs_first() {
    let test = Test::new("shutdown-sync", |_| {});
    assert_eq!(test.start(0), Ok(()));
    assert_eq!(test.shut_down(), Ended::Clean);
    assert_eq!(test.snapshot_ids(), [A]);
    assert_eq!(*test.api.synced_at_take.lock().unwrap(), [1]);
    assert_eq!(test.stop_lines(), 0);
    assert!(!test.hook.running(&test.rows[0].id));
}

/// A guest that sends no `synced` within `SYNC_WAIT` gets no shutdown
/// snapshot, and the graceful stop runs (decision CH62 rule 23). The
/// shutdown runs on paused time, so the wait costs no real 10 s.
#[test]
fn a_shutdown_without_synced_stops_gracefully() {
    let test = Test::new("shutdown-no-synced", |_| {});
    *test.fake.guest.lock().unwrap() = Guest::NoSync;
    assert_eq!(test.start(0), Ok(()));
    test.runtime.block_on(async { tokio::time::pause() });
    assert_eq!(test.shut_down(), Ended::Clean);
    assert!(!test.api.entered.load(Ordering::SeqCst));
    assert_eq!(test.fake.sync_lines.load(Ordering::SeqCst), 1);
    eventually(|| test.stop_lines() == 1);
    assert!(test.stopped(0));
    assert!(!test.hook.running(&test.rows[0].id));
}

/// The hook behind an `Arc`, for the acp handler, which owns its hook.
#[derive(Debug)]
struct Shared(Arc<ColdBoot<Fake, FakeApi>>);

impl StartVm for Shared {
    async fn start(&self, row: &SessionRow) -> Result<AcpLink, StartFault> {
        self.0.start(row).await
    }
}

impl StopVm for Shared {
    async fn stop(&self, id: &SessionId) -> Result<(), VmStopFailed> {
        self.0.stop(id).await
    }
}

/// A door close counts as a use (decision CH50 rule 5). An Upgrade
/// through the acp handler opens the door, and its end records the close.
/// The VM sleeps only `IDLE_TIMEOUT` after the close, not after its start.
#[test]
fn a_door_close_counts_as_a_use() {
    let test = Test::new("closed", |_| {});
    let hook = Shared(Arc::clone(&test.hook));
    let acp = Acp::new(Arc::clone(&test.store), test.relays.clone(), hook);
    let open: OpenAcp =
        serde_json::from_str(&format!(r#"{{"family":"chat","id":"{A}"}}"#)).unwrap();
    let relay = test.runtime.block_on(acp.admit(open)).unwrap();
    std::thread::sleep(Duration::from_millis(100));
    drop(relay);
    let Door::ClosedAt(closed) = test.relays.door(&test.rows[0].id) else {
        panic!("the end of the Upgrade recorded no close");
    };
    let closed = closed.into_std();
    let before = closed + IDLE_TIMEOUT - Duration::from_millis(50);
    assert!(
        test.runtime
            .block_on(test.hook.sleep_idle(before))
            .is_empty()
    );
    let after = closed + PAST_IDLE;
    assert_eq!(test.runtime.block_on(test.hook.sleep_idle(after)).len(), 1);
}

/// A start that hyper drops during an eviction leaves the eviction to
/// finish in its own task. The VM stays `Evicting` with no kill, its
/// snapshot ends, and it leaves the table. A later start of it boots.
#[test]
fn a_dropped_start_lets_its_eviction_finish() {
    let test = Test::new("evict-cut", |config| config.limits.ram_cap = 512 << 20);
    assert_eq!(test.start(0), Ok(()));
    test.api.hold.store(true, Ordering::SeqCst);
    let start = test.hook.start(&test.rows[1]);
    let cut = test
        .runtime
        .block_on(async { tokio::time::timeout(Duration::from_millis(100), start).await });
    // The release comes first, so a failed assert below leaves no
    // blocking step that holds the runtime drop for ever. The eviction
    // task waits until a block_on drives the runtime again.
    test.api.hold.store(false, Ordering::SeqCst);
    assert!(cut.is_err());
    assert!(test.api.entered.load(Ordering::SeqCst));
    assert!(!test.hook.running(&test.rows[0].id));
    assert!(!test.stopped(0));
    // The sleep drives the runtime, so the eviction task runs. A start
    // gets `host_full` until the eviction ends.
    eventually(|| {
        let pause = async { tokio::time::sleep(Duration::from_millis(1)).await };
        test.runtime.block_on(pause);
        test.start(0) == Ok(())
    });
    assert_eq!(test.snapshot_ids(), [A]);
    assert_eq!(test.launches(), 2);
}

/// The idle check retires one VM at a time and reads the table again
/// before the next. A VM that waits its turn still runs: a start of it
/// dials it, and its open door keeps it up after the first sleep.
#[test]
fn the_idle_check_takes_out_one_vm_at_a_time() {
    let test = Test::new("one-by-one", |_| {});
    assert_eq!(test.start(0), Ok(()));
    assert_eq!(test.start(1), Ok(()));
    test.api.hold.store(true, Ordering::SeqCst);
    let now = Instant::now() + PAST_IDLE;
    let (slept, other) = test
        .runtime
        .block_on(both(test.hook.sleep_idle(now), async {
            while !test.api.entered.load(Ordering::SeqCst) {
                tokio::time::sleep(Duration::from_millis(1)).await;
            }
            // The VM that still runs waits its turn.
            let other = usize::from(test.hook.running(&test.rows[1].id));
            let door = test.relays.claim(&test.rows[other].id).unwrap();
            let started = test.hook.start(&test.rows[other]).await.map(|_link| ());
            test.api.hold.store(false, Ordering::SeqCst);
            (other, door, started)
        }));
    let (other, _door, started) = other;
    assert_eq!(started, Ok(()));
    assert_eq!(slept, [test.rows[1 - other].id.clone()]);
    assert!(test.hook.running(&test.rows[other].id));
    assert_eq!(test.launches(), 2);
}

#[cfg(test)]
impl Test {
    /// Drops the session of row `index` through the drop handler over the
    /// hook (plan PR L18b).
    fn drop_session(&self, index: usize) -> Result<Dropped, DropError> {
        let hook = Shared(Arc::clone(&self.hook));
        let drops = Drops::new(Arc::clone(&self.store), self.relays.clone(), hook);
        let row = &self.rows[index];
        let target = DropTarget::Session(DropSession {
            family: row.family.clone(),
            id: row.id.clone(),
        });
        self.runtime
            .block_on(Serve::<DropRoute>::serve(&drops, target))
    }
}

/// A drop through the hook of the vmm sends the graceful stop, removes
/// the row and frees the RAM of the VM (decisions CH19 (3), CH33 rule
/// 11). The cap holds one VM, and the next start evicts nothing.
#[test]
fn a_drop_stops_the_vm_through_the_hook() {
    let test = Test::new("drop", |config| config.limits.ram_cap = 512 << 20);
    assert_eq!(test.start(0), Ok(()));
    assert_eq!(test.drop_session(0), Ok(Dropped {}));
    assert_eq!(test.stop_lines(), 1);
    assert!(test.stopped(0));
    assert!(!test.hook.running(&test.rows[0].id));
    assert!(test.store.lock().unwrap().get(&test.rows[0].id).is_none());
    assert_eq!(test.start(1), Ok(()));
    assert!(test.api.snapshots.lock().unwrap().is_empty());
}

/// A VM whose stop fails stays in the table with no link, so admission
/// counts its RAM and evicts nothing. The drop answers `internal`, and
/// the row stays (decision CH50 rule 4).
#[test]
fn a_failed_stop_keeps_the_vm_and_its_row() {
    let test = Test::new("drop-hung", |config| config.limits.ram_cap = 512 << 20);
    assert_eq!(test.start(0), Ok(()));
    test.fake.broken.lock().unwrap()[0].store(true, Ordering::SeqCst);
    assert_eq!(test.drop_session(0), Err(DropError::Internal {}));
    assert!(test.hook.running(&test.rows[0].id));
    assert!(test.store.lock().unwrap().get(&test.rows[0].id).is_some());
    assert_eq!(test.start(1), Err(StartFault::HostFull));
    assert!(test.api.snapshots.lock().unwrap().is_empty());
}

/// Two stops of one VM overlap. The second waits for the first and
/// returns its outcome: one `stop` line, one error (caller duty 6).
#[test]
fn a_second_stop_returns_the_outcome_of_the_first() {
    let test = Test::new("drop-twice", |_| {});
    *test.fake.guest.lock().unwrap() = Guest::Wedges;
    assert_eq!(test.start(0), Ok(()));
    let id = &test.rows[0].id;
    let (first, second) = test
        .runtime
        .block_on(both(test.hook.stop(id), test.hook.stop(id)));
    let (first, second) = (first.unwrap_err(), second.unwrap_err());
    assert!(Arc::ptr_eq(&first.error, &second.error));
    assert_eq!(first.pid, 4242);
    assert_eq!(test.stop_lines(), 1);
    assert!(test.hook.running(id));
}

/// A stop that finds a start in its launch step waits for it. The start
/// future was dropped, so the launch output kills the VM, and the stop
/// then finds it gone and removes it from the table.
#[test]
fn a_stop_waits_for_a_launch_that_outlives_its_start() {
    let test = Test::new("drop-launch", |_| {});
    test.fake.hold.store(true, Ordering::SeqCst);
    let start = test.hook.start(&test.rows[0]);
    let cut = test
        .runtime
        .block_on(async { tokio::time::timeout(Duration::from_millis(50), start).await });
    assert!(cut.is_err());
    let id = &test.rows[0].id;
    let ((stopped, killed), ()) = test.runtime.block_on(both(
        async {
            let stopped = test.hook.stop(id).await;
            // No index: a panic here would leave the launch held for ever.
            let vms = test.fake.vms.lock().unwrap();
            let killed = vms.first().is_some_and(|vm| vm.load(Ordering::SeqCst));
            (stopped, killed)
        },
        async {
            tokio::time::sleep(Duration::from_millis(50)).await;
            test.fake.hold.store(false, Ordering::SeqCst);
        },
    ));
    assert!(stopped.is_ok());
    assert!(killed);
    assert!(!test.hook.running(id));
}

/// A VM under a stop keeps its RAM, so a start of another session that
/// would need it gets `host_full` (decision CH50 rule 4).
#[test]
fn a_vm_under_a_stop_keeps_its_ram() {
    let test = Test::new("drop-ram", |config| config.limits.ram_cap = 512 << 20);
    *test.fake.guest.lock().unwrap() = Guest::Wedges;
    assert_eq!(test.start(0), Ok(()));
    let (stopped, started) = test
        .runtime
        .block_on(both(test.hook.stop(&test.rows[0].id), async {
            tokio::time::sleep(Duration::from_millis(5)).await;
            test.hook.start(&test.rows[1]).await.map(|_link| ())
        }));
    assert!(stopped.is_err());
    assert_eq!(started, Err(StartFault::HostFull));
    assert_eq!(test.launches(), 1);
}

#[cfg(test)]
impl Test {
    /// Writes `line` on the guest end of the newest control link, as guest
    /// init sends a guest event (decisions CH21, CH62 rule 11).
    fn send(&self, line: &str) {
        let links = self.fake.links.lock().unwrap();
        let mut link = links.last().unwrap();
        link.write_all(format!("{line}\n").as_bytes()).unwrap();
    }

    /// Drives the runtime, so the reader of the control link runs, until
    /// `done` holds. Fails after 5 s.
    fn settle(&self, done: impl Fn(&Self) -> bool) {
        eventually(|| {
            let pause = async { tokio::time::sleep(Duration::from_millis(1)).await };
            self.runtime.block_on(pause);
            done(self)
        });
    }

    /// The title of the row of `index`, and whether its `updated_at` moved.
    fn title(&self, index: usize) -> (Option<String>, bool) {
        let store = self.store.lock().unwrap();
        let row = store.get(&self.rows[index].id).unwrap();
        let title = row.title.as_ref().map(ToString::to_string);
        (title, row.updated_at != self.rows[index].updated_at)
    }
}

/// The reader of the control link applies each turn line (plan PR
/// L18b, decision CH62 rule 11). A start keeps the VM awake. A `synced`
/// line with no sync waiting is dropped, and the link stays open
/// (decision CH62 rule 23). An end clears the turn, writes the row
/// (decision CH32 rule 4) and counts as a use (decision CH50 rule 5).
#[test]
fn the_reader_applies_each_turn_event() {
    let test = Test::new("reader", |_| {});
    assert_eq!(test.start(0), Ok(()));
    let id = test.rows[0].id.clone();
    test.send(r#"{"turn":{"start":{}}}"#);
    test.settle(|test| test.control.lock().unwrap().turn_running(&id));
    assert!(test.sleep_after(PAST_IDLE).is_empty());
    // The end below arrives only when the reader reads on past it.
    test.send(r#"{"synced":{}}"#);

    std::thread::sleep(Duration::from_millis(5));
    let before = Instant::now();
    test.send(r#"{"turn":{"end":{"title":"First","updated_at":1793577700000}}}"#);
    test.settle(|test| test.title(0).0.is_some());
    assert!(!test.control.lock().unwrap().turn_running(&id));
    assert_eq!(test.title(0), (Some("First".to_owned()), true));
    let now = before + IDLE_TIMEOUT;
    assert!(test.runtime.block_on(test.hook.sleep_idle(now)).is_empty());
    assert_eq!(test.sleep_after(PAST_IDLE), [id]);
}

/// A refused line closes the link (decision CH43 rule 2) and stops the
/// VM at once (decision CH54 rule 6). The link is gone, so the stop sends
/// no `stop` line (decision CH50 rule 3). The cap holds one VM, so the
/// start of row 1 proves the RAM is free. The next start of row 0
/// launches a new CH and cold-boots (decision CH19 (5)).
#[test]
fn a_refused_line_stops_the_vm() {
    let test = Test::new("refused", |config| config.limits.ram_cap = 512 << 20);
    assert_eq!(test.start(0), Ok(()));
    let id = test.rows[0].id.clone();
    test.send("not a turn event");
    // The stop runs in the reader task, so the start of row 1 gets
    // `host_full` until the stop frees the RAM.
    test.settle(|test| test.start(1) == Ok(()));
    assert!(!test.hook.running(&id));
    assert!(test.control.lock().unwrap().link(&id).is_none());
    assert!(test.stopped(0));
    assert_eq!(test.stop_lines(), 0);
    assert!(test.api.snapshots.lock().unwrap().is_empty());
    let other = &test.rows[1].id;
    assert!(test.runtime.block_on(test.hook.stop(other)).is_ok());
    assert_eq!(test.start(0), Ok(()));
    assert_eq!(test.launches(), 3);
    assert!(!test.restored(2));
    assert!(test.hook.running(&id));
}

/// A guest that closes the control link after `ControlReady` ends the
/// reader, and the VM runs on (decision CH43 rule 2). With no control
/// link it never sleeps, and a drop skips the graceful stop (decision CH50
/// rule 3).
#[test]
fn a_closed_control_link_keeps_the_vm_awake() {
    let test = Test::new("link-closed", |_| {});
    assert_eq!(test.start(0), Ok(()));
    let id = test.rows[0].id.clone();
    let links = test.fake.links.lock().unwrap();
    links.last().unwrap().shutdown(Shutdown::Both).unwrap();
    drop(links);
    test.settle(|test| test.control.lock().unwrap().link(&id).is_none());
    assert!(test.hook.running(&id));
    assert!(test.sleep_after(PAST_IDLE).is_empty());
    assert_eq!(test.drop_session(0), Ok(Dropped {}));
    assert_eq!(test.stop_lines(), 0);
    assert!(test.stopped(0));
}

/// A start of the session under a stop finds its `Stopping` slot and gets
/// `host_full` at once. It launches no second CH on the session disk
/// (decision CH54 rule 4).
#[test]
fn a_start_of_the_stopped_session_is_host_full() {
    let test = Test::new("drop-same", |_| {});
    *test.fake.guest.lock().unwrap() = Guest::Wedges;
    assert_eq!(test.start(0), Ok(()));
    let (stopped, started) = test
        .runtime
        .block_on(both(test.hook.stop(&test.rows[0].id), async {
            tokio::time::sleep(Duration::from_millis(5)).await;
            test.hook.start(&test.rows[0]).await.map(|_link| ())
        }));
    assert!(stopped.is_err());
    assert_eq!(started, Err(StartFault::HostFull));
    assert_eq!(test.launches(), 1);
}

/// A stop that finds the VM under an idle sleep waits for the retire. The
/// take fails and the retire stop fails, so the VM goes back. The stop
/// then fails too, and the VM still runs (decision CH50 rule 4).
#[test]
fn a_stop_during_a_sleep_waits_for_the_retire() {
    let test = Test::new("drop-sleep", |_| {});
    *test.fake.guest.lock().unwrap() = Guest::Wedges;
    assert_eq!(test.start(0), Ok(()));
    test.api.fail_take.store(true, Ordering::SeqCst);
    let id = &test.rows[0].id;
    let now = Instant::now() + PAST_IDLE;
    let (slept, stopped) = test
        .runtime
        .block_on(both(test.hook.sleep_idle(now), async {
            tokio::time::sleep(Duration::from_millis(5)).await;
            test.hook.stop(id).await
        }));
    assert!(slept.is_empty());
    assert!(stopped.is_err());
    assert_eq!(test.stop_lines(), 1);
    assert!(test.hook.running(id));
}

/// A `facade` line after `ControlReady` writes its turn end to the row
/// through the reader too (decision CH32 rule 4).
#[test]
fn a_facade_line_writes_its_turn_end_to_the_row() {
    let test = Test::new("facade-ended", |_| {});
    assert_eq!(test.start(0), Ok(()));
    test.send(ENDED);
    test.settle(|test| test.title(0).0.is_some());
    assert_eq!(test.title(0), (Some("First".to_owned()), true));
}

/// A valid line on a replaced link ends the old reader, and the line
/// changes nothing (decisions CH54 rule 6, CH62 rule 23). Row 0 stops,
/// and its next start replaces the link. The old link then gets a
/// `synced`, a `facade` line, or a `facade` line with a turn end. The new
/// VM of row 0 runs on, and its row keeps no title.
#[test]
fn a_line_on_a_replaced_link_ends_its_reader() {
    for (index, line) in [r#"{"synced":{}}"#, READY, ENDED].into_iter().enumerate() {
        let test = Test::new(&format!("stale-line-{index}"), |_| {});
        assert_eq!(test.start(0), Ok(()));
        let id = test.rows[0].id.clone();
        assert!(test.runtime.block_on(test.hook.stop(&id)).is_ok());
        assert_eq!(test.start(0), Ok(()));
        let old = test.fake.links.lock().unwrap()[0].try_clone().unwrap();
        (&old).write_all(format!("{line}\n").as_bytes()).unwrap();
        old.set_read_timeout(Some(Duration::from_millis(1)))
            .unwrap();
        // The end of the old reader closes the vmm end of the old link.
        test.settle(|_| matches!((&old).read(&mut [0_u8; 64]), Ok(0)));
        assert!(test.hook.running(&id), "{line}");
        assert_eq!(test.title(0).0, None, "{line}");
    }
}

/// A reader whose link is no longer current stops nothing (decision CH54
/// rule 6). Row 0 stops, and its next start replaces the link. The old
/// link then ends on a partial line, and the old reader refuses it. The
/// new VM of row 0 runs on.
#[test]
fn a_stale_reader_stops_nothing() {
    let test = Test::new("refused-stale", |_| {});
    assert_eq!(test.start(0), Ok(()));
    let id = test.rows[0].id.clone();
    assert!(test.runtime.block_on(test.hook.stop(&id)).is_ok());
    assert_eq!(test.start(0), Ok(()));
    let old = test.fake.links.lock().unwrap()[0].try_clone().unwrap();
    (&old).write_all(br#"{"start""#).unwrap();
    old.shutdown(Shutdown::Write).unwrap();
    old.set_read_timeout(Some(Duration::from_millis(1)))
        .unwrap();
    // The end of the old reader closes the vmm end of the old link.
    test.settle(|_| matches!((&old).read(&mut [0_u8; 64]), Ok(0)));
    let pause = async { tokio::time::sleep(Duration::from_millis(100)).await };
    test.runtime.block_on(pause);
    assert!(test.hook.running(&id));
    assert!(!test.stopped(1));
    assert_eq!(test.launches(), 2);
}

/// The reader of a refused line stops only its own VM (decision CH54
/// rule 6). The line arrives while an idle sleep holds the VM. The retire
/// frees the slot, and a new start of row 0 takes it while its launch is
/// held. The old reader then finds the new VM, and that VM runs on.
#[test]
fn a_refused_line_stops_only_its_own_vm() {
    let test = Test::new("refused-own", |_| {});
    assert_eq!(test.start(0), Ok(()));
    let id = test.rows[0].id.clone();
    test.api.hold.store(true, Ordering::SeqCst);
    let now = Instant::now() + PAST_IDLE;
    let pause = || tokio::time::sleep(Duration::from_millis(1));
    let (_, started) = test
        .runtime
        .block_on(both(test.hook.sleep_idle(now), async {
            while !test.api.entered.load(Ordering::SeqCst) {
                pause().await;
            }
            test.send("not a turn event");
            while test.control.lock().unwrap().link(&id).is_some() {
                pause().await;
            }
            test.fake.hold.store(true, Ordering::SeqCst);
            test.api.hold.store(false, Ordering::SeqCst);
            let fake = Arc::clone(&test.fake);
            let release = spawn(move || {
                std::thread::sleep(Duration::from_millis(100));
                fake.hold.store(false, Ordering::SeqCst);
            });
            // The start gets `host_full` until the retire frees the slot.
            let started = loop {
                match test.hook.start(&test.rows[0]).await {
                    Err(StartFault::HostFull) => tokio::task::yield_now().await,
                    started => break started.map(|_link| ()),
                }
            };
            release.join().unwrap();
            started
        }));
    assert_eq!(started, Ok(()));
    let pause = async { tokio::time::sleep(Duration::from_millis(100)).await };
    test.runtime.block_on(pause);
    assert!(test.hook.running(&id));
    assert!(!test.stopped(1));
    assert_eq!(test.launches(), 2);
}

/// A refused line during the take of an idle sleep keeps no snapshot of
/// the faulted guest (decision CH54 rule 6). The VM stops, and the sleep
/// does not count it as slept. The take is consumed, so the next start
/// cold-boots (decision CH19 (5)).
#[test]
fn a_refused_line_during_a_take_keeps_no_snapshot() {
    let test = Test::new("refused-take", |_| {});
    assert_eq!(test.start(0), Ok(()));
    let id = test.rows[0].id.clone();
    test.api.keep.store(true, Ordering::SeqCst);
    test.api.hold.store(true, Ordering::SeqCst);
    let now = Instant::now() + PAST_IDLE;
    let pause = || tokio::time::sleep(Duration::from_millis(1));
    let (slept, ()) = test
        .runtime
        .block_on(both(test.hook.sleep_idle(now), async {
            while !test.api.entered.load(Ordering::SeqCst) {
                pause().await;
            }
            test.send("not a turn event");
            while test.control.lock().unwrap().link(&id).is_some() {
                pause().await;
            }
            test.api.hold.store(false, Ordering::SeqCst);
        }));
    assert!(slept.is_empty());
    assert!(test.stopped(0));
    assert!(!test.hook.running(&id));
    let generation = test.state.session(&id).snapshot(SnapshotGeneration(1));
    assert!(generation.join("consumed").is_file());
    test.reply(0, "Paused", &[]);
    assert_eq!(test.start(0), Ok(()));
    assert!(!test.restored(1));
    assert_eq!(test.launches(), 2);
}

#[cfg(test)]
impl Test {
    /// Adds rows of `chat` until the test has `count` rows.
    fn rows_up_to(&mut self, count: usize) {
        let mut store = self.store.lock().unwrap();
        for number in self.rows.len()..count {
            let create = format!(
                r#"{{"created_at":1793577600000,"cwd":"/home/david","family":"chat","id":"00000000-0000-4000-8000-{number:012x}","origin":{{"door":{{}}}}}}"#
            );
            let row = store
                .create(serde_json::from_str(&create).unwrap())
                .unwrap();
            self.rows.push(row);
        }
    }

    /// The slot number of launch `index` (plan PR L16b).
    fn slot(&self, index: usize) -> u16 {
        let launches = self.fake.launches.lock().unwrap();
        launches[index].slot().unwrap().number()
    }

    /// The target of the `vm/<id>` link of row `index`, or `None` when
    /// the row has no link (PR D16).
    fn link(&self, index: usize) -> Option<PathBuf> {
        let id = &self.rows[index].id;
        fs::read_link(self.dir.join(format!("run/vm/{id}"))).ok()
    }
}

/// The link target of slot `number`.
#[cfg(test)]
fn slot_dir(number: u16) -> PathBuf {
    PathBuf::from(format!("../slot/{number}"))
}

/// Each VM takes its own slot, and its `vm/<id>` links to it (plan PR
/// L16b, decisions CH51, CH55). A stop frees the slot and removes the
/// link, and the next start takes the freed slot. The link empties the
/// slot first, so a leftover listener of the old VM is gone (decision
/// CH56 rule 1).
#[test]
fn two_vms_get_two_slots_and_a_freed_slot_is_reused() {
    let test = Test::new("slots", |_| {});
    assert_eq!(test.start(0), Ok(()));
    assert_eq!(test.start(1), Ok(()));
    assert_eq!((test.slot(0), test.slot(1)), (0, 1));
    assert_eq!(test.link(0), Some(slot_dir(0)));
    assert_eq!(test.link(1), Some(slot_dir(1)));
    let leftover = test.dir.join("run/slot/0/vsock.sock_1104");
    fs::write(&leftover, b"the old guest").unwrap();

    let id = test.rows[0].id.clone();
    assert!(test.runtime.block_on(test.hook.stop(&id)).is_ok());
    assert_eq!(test.link(0), None);
    assert_eq!(test.start(2), Ok(()));
    assert_eq!(test.slot(2), 0);
    assert_eq!(test.link(2), Some(slot_dir(0)));
    assert!(!leftover.exists());
}

/// With each of the 64 slots held by a busy VM, the next start gets
/// `host_full` with no fault and no launch, though its RAM fits (plan PR
/// L16b, decisions CH21-3, CH58 rule 2). A stop frees one slot, and the
/// start then takes it.
#[test]
fn a_full_pool_makes_the_next_start_host_full() {
    let mut test = Test::new("pool", |_| {});
    *test.fake.guest.lock().unwrap() = Guest::Turn;
    test.rows_up_to(65);
    for index in 0..64 {
        assert_eq!(test.start(index), Ok(()), "start {index}");
    }
    let slots: HashSet<u16> = (0..64).map(|index| test.slot(index)).collect();
    assert_eq!(slots.len(), 64);
    assert_eq!(test.start(64), Err(StartFault::HostFull));
    assert_eq!(test.launches(), 64);
    assert!(test.fault_codes().is_empty());

    let id = test.rows[10].id.clone();
    assert!(test.runtime.block_on(test.hook.stop(&id)).is_ok());
    assert_eq!(test.start(64), Ok(()));
    assert_eq!(test.slot(64), test.slot(10));
}

/// A full pool counts like a full RAM cap (decision CH58 rule 2). 64 VMs
/// hold each slot, and one of them is idle. The 65th start evicts the
/// idle VM, which takes its snapshot and stops, and the start takes its
/// slot.
#[test]
fn a_full_pool_evicts_the_idle_vm_and_takes_its_slot() {
    let mut test = Test::new("pool-evict", |_| {});
    test.rows_up_to(65);
    for index in 0..64 {
        let guest = if index == 7 {
            Guest::Ready
        } else {
            Guest::Turn
        };
        *test.fake.guest.lock().unwrap() = guest;
        assert_eq!(test.start(index), Ok(()), "start {index}");
    }
    let idle = test.slot(7);
    *test.fake.guest.lock().unwrap() = Guest::Ready;
    assert_eq!(test.start(64), Ok(()));
    assert_eq!(test.launches(), 65);
    assert!(test.stopped(7));
    assert_eq!(test.snapshot_ids(), [test.rows[7].id.to_string()]);
    assert_eq!(test.slot(64), idle);
    assert_eq!(test.link(7), None);
    assert_eq!(test.link(64), Some(slot_dir(idle)));
    assert!(test.fault_codes().is_empty());
}

/// A guest fault at the start, a refused line and a VM that exited each
/// free the slot and remove the link (plan PR L16b, decision CH54 rule
/// 6). The next start takes the slot.
#[test]
fn a_fault_or_an_exit_frees_the_slot() {
    let test = Test::new("slot-fault", |_| {});
    *test.fake.guest.lock().unwrap() = Guest::Closes;
    assert_eq!(test.start(0), Err(StartFault::SandboxStartFailed));
    assert_eq!(test.link(0), None);

    *test.fake.guest.lock().unwrap() = Guest::Ready;
    assert_eq!(test.start(1), Ok(()));
    assert_eq!(test.slot(1), 0);
    test.send("not a turn event");
    test.settle(|test| test.link(1).is_none());
    assert_eq!(test.start(2), Ok(()));
    assert_eq!(test.slot(2), 0);

    // An exited VM holds its slot until admission reaps it.
    test.fake.vms.lock().unwrap()[2].store(true, Ordering::SeqCst);
    assert_eq!(test.link(2), Some(slot_dir(0)));
    assert_eq!(test.start(0), Ok(()));
    assert_eq!(test.link(2), None);
    assert_eq!(test.slot(3), 0);
}

/// A drop, an eviction and an idle sleep each free the slot and remove
/// the link (plan PR L16b). An evicted VM holds its slot until its CH
/// ends. So a start that evicts it for RAM while a slot is free takes
/// the free slot.
#[test]
fn a_drop_an_eviction_or_a_sleep_frees_the_slot() {
    let test = Test::new("slot-drop", |config| config.limits.ram_cap = 512 << 20);
    assert_eq!(test.start(0), Ok(()));
    assert_eq!(test.drop_session(0), Ok(Dropped {}));
    assert_eq!(test.link(0), None);

    assert_eq!(test.start(1), Ok(()));
    assert_eq!(test.slot(1), 0);
    assert_eq!(test.start(2), Ok(()));
    assert_eq!(test.snapshot_ids(), [B]);
    assert_eq!(test.slot(2), 1);
    assert_eq!(test.link(1), None);

    assert_eq!(test.sleep_after(PAST_IDLE).len(), 1);
    assert_eq!(test.link(2), None);
    assert_eq!(test.start(1), Ok(()));
    assert_eq!(test.slot(3), 0);
}

/// A shutdown frees the slot of each VM: the take of an idle one and the
/// stop of a busy one (plan PRs L16b, L19).
#[test]
fn a_shutdown_frees_each_slot() {
    let test = Test::new("slot-shutdown", |_| {});
    assert_eq!(test.start(0), Ok(()));
    *test.fake.guest.lock().unwrap() = Guest::Turn;
    assert_eq!(test.start(1), Ok(()));
    assert_eq!(test.shut_down(), Ended::Clean);
    assert_eq!(test.snapshot_ids(), [A]);
    assert_eq!((test.link(0), test.link(1)), (None, None));
}

/// A restore links the `vm/<id>` of its snapshot to the slot of the
/// start, which is not the slot of the VM that took the snapshot. The
/// device check passes, so the vsock path of the snapshot holds (PR D16,
/// plan PR L16b).
#[test]
fn a_restore_links_the_snapshot_to_its_new_slot() {
    let test = Test::new("slot-restore", |_| {});
    *test.fake.guest.lock().unwrap() = Guest::Turn;
    assert_eq!(test.start(1), Ok(()));
    *test.fake.guest.lock().unwrap() = Guest::Ready;
    assert_eq!(test.start(0), Ok(()));
    assert_eq!(test.slot(1), 1);
    test.api.keep.store(true, Ordering::SeqCst);
    assert_eq!(test.sleep_after(PAST_IDLE), [test.rows[0].id.clone()]);
    assert_eq!(test.link(0), None);

    *test.fake.guest.lock().unwrap() = Guest::Turn;
    assert_eq!(test.start(2), Ok(()));
    assert_eq!(test.slot(2), 1);
    test.reply(0, "Paused", &[]);
    assert_eq!(test.start(0), Ok(()));
    assert!(test.restored(3));
    assert_eq!(test.slot(3), 2);
    assert_eq!(test.link(0), Some(slot_dir(2)));
    assert_eq!(test.api.resumes.load(Ordering::SeqCst), 1);
    assert!(test.fault_codes().is_empty());
}

/// Two starts overlap in their launch steps. The `Starting` entry of the
/// first holds its slot, so the second takes another one, and its link
/// empties no slot of the first (plan PR L16b, PR D16 caller duty (c),
/// decision CH56 rule 1).
#[test]
fn two_overlapping_starts_take_two_slots() {
    let test = Test::new("slot-overlap", |_| {});
    test.fake.hold.store(true, Ordering::SeqCst);
    for index in 0..2 {
        let start = test.hook.start(&test.rows[index]);
        let cut = test
            .runtime
            .block_on(async { tokio::time::timeout(Duration::from_millis(50), start).await });
        assert!(cut.is_err(), "start {index}");
    }
    test.fake.hold.store(false, Ordering::SeqCst);
    eventually(|| test.launches() == 2);
    assert_ne!(test.slot(0), test.slot(1));
}

/// A start runs while a stop of another VM runs. The `Stopping` entry
/// holds the slot of the live CH, so the start takes another slot and
/// leaves the sockets of that CH alone (plan PR L16b, PR D16 caller duty
/// (c), decision CH56 rule 1).
#[test]
fn a_start_during_a_stop_takes_another_slot() {
    let test = Test::new("slot-stopping", |_| {});
    *test.fake.guest.lock().unwrap() = Guest::Wedges;
    assert_eq!(test.start(0), Ok(()));
    assert_eq!(test.slot(0), 0);
    *test.fake.guest.lock().unwrap() = Guest::Ready;
    let (stopped, started) = test
        .runtime
        .block_on(both(test.hook.stop(&test.rows[0].id), async {
            tokio::time::sleep(Duration::from_millis(5)).await;
            test.hook.start(&test.rows[1]).await.map(|_link| ())
        }));
    assert!(stopped.is_err());
    assert_eq!(started, Ok(()));
    assert_eq!(test.slot(1), 1);
    assert_eq!(test.link(0), Some(slot_dir(0)));
}

/// A stale `vm/<id>` link names the slot of another VM, as after a failed
/// unlink or a vmm restart with no reboot. The start links `vm/<id>` to
/// its own slot before `remove_stale` runs, so the stale sweep removes no
/// file of the other VM (PR D16 caller duty (a)).
#[test]
fn a_stale_link_to_a_held_slot_removes_nothing_of_its_vm() {
    let test = Test::new("slot-stale", |_| {});
    assert_eq!(test.start(1), Ok(()));
    assert_eq!(test.slot(0), 0);
    let vsock = test.dir.join("run/slot/0/vsock.sock");
    assert!(vsock.exists());
    let id = &test.rows[0].id;
    std::os::unix::fs::symlink(slot_dir(0), test.dir.join(format!("run/vm/{id}"))).unwrap();
    assert_eq!(test.start(0), Ok(()));
    assert_eq!(test.slot(1), 1);
    assert_eq!(test.link(0), Some(slot_dir(1)));
    assert!(vsock.exists());
}

/// Plan PRs V6 and V8: the listeners of host ports 1100, 1101 and 1102
/// take a dial at the launch, before CH starts (decisions CH6-2, CH56
/// rule 1). A family with no `egress.json` gets no 1104 (decision CH69
/// rule 3).
#[test]
fn listeners_exist_before_the_boot() {
    let test = Test::new("planes-boot", |_| {});
    assert_eq!(test.start(0), Ok(()));
    assert!(!test.restored(0));
    assert_eq!(*test.fake.listening.lock().unwrap(), [NO_EGRESS]);
}

/// A restore empties the slot and binds each listener again before CH
/// starts, so a restored guest that dials again finds them (decisions
/// CH6-2, CH62 rule 14).
#[test]
fn a_restore_binds_the_listeners_again() {
    let test = Test::new("planes-restore", |_| {});
    assert_eq!(test.start(0), Ok(()));
    let id = test.rows[0].id.clone();
    assert!(test.runtime.block_on(test.hook.stop(&id)).is_ok());
    test.snapshot(0, Some(&test.key()));
    test.reply(0, "Paused", &[]);
    assert_eq!(test.start(0), Ok(()));
    assert!(test.restored(1));
    assert_eq!(*test.fake.listening.lock().unwrap(), [NO_EGRESS; 2]);
}

/// A dial of 1101 reaches the plane of the config while the VM runs. The
/// stop of the VM closes the listener (plan PR V6).
#[test]
fn the_listeners_go_with_the_vm() {
    let plane = TcpListener::bind("127.0.0.1:0").unwrap();
    let addr = plane.local_addr().unwrap();
    spawn(move || {
        for stream in plane.incoming() {
            let mut stream = stream.unwrap();
            spawn(move || {
                let mut buffer = [0; 64];
                while let Ok(read @ 1..) = stream.read(&mut buffer) {
                    stream.write_all(&buffer[..read]).unwrap();
                }
            });
        }
    });
    let test = Test::new("planes-stop", |config| config.planes.litellm = addr);
    assert_eq!(test.start(0), Ok(()));
    let listener = test.dir.join("run/slot/0/vsock.sock_1101");
    let path = listener.clone();
    // The current-thread runtime runs the bridge only inside `block_on`,
    // so the guest side runs in `spawn_blocking`.
    let echoed = test.runtime.block_on(test.runtime.spawn_blocking(move || {
        let mut guest = UnixStream::connect(path).unwrap();
        guest
            .set_read_timeout(Some(Duration::from_secs(5)))
            .unwrap();
        guest.write_all(b"ping").unwrap();
        let mut back = [0; 4];
        guest.read_exact(&mut back).unwrap();
        back
    }));
    assert_eq!(&echoed.unwrap(), b"ping");

    let id = test.rows[0].id.clone();
    assert!(test.runtime.block_on(test.hook.stop(&id)).is_ok());
    // The file stays until the next link of the slot empties it. The end
    // of the bridge task closes its socket.
    let deadline = Instant::now() + Duration::from_secs(5);
    loop {
        let pause = async { tokio::time::sleep(Duration::from_millis(1)).await };
        test.runtime.block_on(pause);
        match UnixStream::connect(&listener) {
            Err(error) if error.kind() == io::ErrorKind::ConnectionRefused => break,
            _ => assert!(Instant::now() < deadline, "1101 still listens"),
        }
    }
}

/// The names of the host listeners in the run dir of row `index`.
#[cfg(test)]
fn listeners(test: &Test, index: usize) -> Vec<String> {
    let id = &test.rows[index].id;
    let mut listeners: Vec<String> = fs::read_dir(test.dir.join(format!("run/vm/{id}")))
        .unwrap()
        .map(|entry| entry.unwrap().file_name().into_string().unwrap())
        .filter(|name| name.starts_with("vsock.sock_"))
        .collect();
    listeners.sort();
    listeners
}

/// The deny check of a family with no egress names 1100, 1101 and 1102
/// (decision CH6-3), and an unbound listener fails it (PR C8).
#[test]
fn the_egress_check_names_the_planes_for_a_family_with_no_egress() {
    let test = Test::new("planes-deny", |_| {});
    assert_eq!(test.start(0), Ok(()));
    assert_eq!(
        listeners(&test, 0),
        ["vsock.sock_1100", "vsock.sock_1101", "vsock.sock_1102"]
    );

    *test.fake.extra.lock().unwrap() = Some(1103);
    assert_eq!(test.start(1), Err(StartFault::EgressAssertFailed));
    assert_eq!(test.fault_codes(), ["egress_assert_failed"]);
    assert!(test.stopped(1));
}

/// A family whose `egress.json` lists a host gets 1104 at the launch, and
/// the deny check names it (plan PR E2, decision CH49 rule 3).
#[test]
fn the_egress_check_names_1104_for_a_family_with_egress() {
    let test = Test::new("egress-deny", |_| {});
    fs::write(test.dir.join("families/chat/egress.json"), EGRESS).unwrap();
    assert_eq!(test.start(0), Ok(()));
    assert_eq!(*test.fake.listening.lock().unwrap(), [EGRESS_ONLY]);
    assert_eq!(
        listeners(&test, 0),
        [
            "vsock.sock_1100",
            "vsock.sock_1101",
            "vsock.sock_1102",
            "vsock.sock_1104"
        ]
    );
    assert!(test.fault_codes().is_empty());
}

/// An absent file and an empty list bind no 1104 (decision CH49 rule 3).
#[test]
fn an_absent_or_empty_list_binds_no_1104() {
    let test = Test::new("egress-none", |_| {});
    assert_eq!(test.start(0), Ok(()));
    let file = test.dir.join("families/chat/egress.json");
    fs::write(&file, r#"{"hosts":[]}"#).unwrap();
    assert_eq!(test.start(1), Ok(()));
    assert_eq!(*test.fake.listening.lock().unwrap(), [NO_EGRESS; 2]);
    assert!(test.fault_codes().is_empty());
}

/// A present `egress.json` that does not read binds 1104, and the deny
/// check names it (decision CH72 rule 3).
#[test]
fn a_bad_list_binds_1104() {
    let test = Test::new("egress-bad", |_| {});
    fs::write(test.dir.join("families/chat/egress.json"), "{").unwrap();
    assert_eq!(test.start(0), Ok(()));
    assert_eq!(*test.fake.listening.lock().unwrap(), [EGRESS_ONLY]);
    assert!(listeners(&test, 0).contains(&"vsock.sock_1104".to_owned()));
    assert!(test.fault_codes().is_empty());
}

/// A CONNECT on the 1104 of a bad list gets 403 and one log line with the
/// reason `not_valid` (decisions CH70 rule 2, CH72 rule 3).
#[test]
fn a_connect_on_a_bad_list_gets_403_and_one_not_valid_line() {
    let test = Test::new("egress-bad-connect", |_| {});
    fs::write(test.dir.join("families/chat/egress.json"), "{").unwrap();
    assert_eq!(test.start(0), Ok(()));
    let path = test.dir.join("run/slot/0/vsock.sock_1104");
    // The current-thread runtime runs the proxy only inside `block_on`, so
    // the guest side runs in `spawn_blocking`.
    let reply = test.runtime.block_on(test.runtime.spawn_blocking(move || {
        let mut guest = UnixStream::connect(path).unwrap();
        guest
            .set_read_timeout(Some(Duration::from_secs(5)))
            .unwrap();
        let head = "CONNECT github.com:443 HTTP/1.1\r\nHost: github.com:443\r\n\r\n";
        guest.write_all(head.as_bytes()).unwrap();
        let mut reply = String::new();
        guest.read_to_string(&mut reply).unwrap();
        reply
    }));
    assert!(reply.unwrap().starts_with("HTTP/1.1 403 "));
    // The fake launch probes 1104 with a dial that sends no head, so the
    // log holds its `bad_head` line beside the line of the CONNECT.
    let log = test.dir.join("egress.jsonl");
    let deadline = Instant::now() + Duration::from_secs(5);
    let (text, mut reasons) = loop {
        let text = fs::read_to_string(&log).unwrap();
        // A read during an append can see part of it, so only a line that
        // ends in its LF counts.
        let reasons: Vec<String> = text
            .split_inclusive('\n')
            .filter_map(|line| line.strip_suffix('\n'))
            .map(|line| serde_json::from_str::<serde_json::Value>(line).unwrap())
            .inspect(|line| assert_eq!(line["verdict"], "refused", "{line}"))
            .map(|line| line["reason"].as_str().unwrap().to_owned())
            .collect();
        if reasons.len() >= 2 {
            break (text, reasons);
        }
        assert!(Instant::now() < deadline, "{text}");
        let pause = async { tokio::time::sleep(Duration::from_millis(1)).await };
        test.runtime.block_on(pause);
    };
    reasons.sort();
    assert_eq!(reasons, ["bad_head", "not_valid"], "{text}");
}

/// A restore binds 1104 again before CH starts, as each plane listener
/// (decisions CH49 rule 3, CH62 rule 14).
#[test]
fn a_restore_binds_1104_again() {
    let test = Test::new("egress-restore", |_| {});
    fs::write(test.dir.join("families/chat/egress.json"), EGRESS).unwrap();
    assert_eq!(test.start(0), Ok(()));
    let id = test.rows[0].id.clone();
    assert!(test.runtime.block_on(test.hook.stop(&id)).is_ok());
    test.snapshot(0, Some(&test.key()));
    test.reply(0, "Paused", &[]);
    assert_eq!(test.start(0), Ok(()));
    assert!(test.restored(1));
    assert_eq!(*test.fake.listening.lock().unwrap(), [EGRESS_ONLY; 2]);
}

/// A 1104 listener in the run dir of a family with no egress is a port
/// that the vmm did not bind, so the deny check fails and the VM stops
/// (decisions CH6-3, CH49 rule 3).
#[test]
fn an_extra_1104_listener_for_a_family_with_no_egress_fails() {
    let test = Test::new("egress-extra", |_| {});
    *test.fake.extra.lock().unwrap() = Some(1104);
    assert_eq!(test.start(0), Err(StartFault::EgressAssertFailed));
    assert_eq!(test.fault_codes(), ["egress_assert_failed"]);
    assert!(test.stopped(0));
}

/// A family whose `repos.json` lists a repo gets 1103 at the launch, and
/// the deny check names it (plan PR M1, decision CH69 rule 13).
#[test]
fn the_egress_check_names_1103_for_a_family_with_repos() {
    let test = Test::new("repos-deny", |_| {});
    fs::write(test.dir.join("families/chat/repos.json"), REPOS).unwrap();
    assert_eq!(test.start(0), Ok(()));
    assert_eq!(*test.fake.listening.lock().unwrap(), [REPOS_ONLY]);
    assert_eq!(
        listeners(&test, 0),
        [
            "vsock.sock_1100",
            "vsock.sock_1101",
            "vsock.sock_1102",
            "vsock.sock_1103"
        ]
    );
    assert!(test.fault_codes().is_empty());
}

/// A family with a clone list and egress gets both 1103 and 1104, and the
/// deny check names both.
#[test]
fn a_family_with_repos_and_egress_binds_1103_and_1104() {
    let test = Test::new("repos-egress", |_| {});
    fs::write(test.dir.join("families/chat/repos.json"), REPOS).unwrap();
    fs::write(test.dir.join("families/chat/egress.json"), EGRESS).unwrap();
    assert_eq!(test.start(0), Ok(()));
    assert_eq!(*test.fake.listening.lock().unwrap(), [[true; 5]]);
    assert!(test.fault_codes().is_empty());
}

/// An absent file and an empty list bind no 1103 (decisions CH69 rule 13,
/// CH73 rule 1).
#[test]
fn an_absent_or_empty_clone_list_binds_no_1103() {
    let test = Test::new("repos-none", |_| {});
    assert_eq!(test.start(0), Ok(()));
    let file = test.dir.join("families/chat/repos.json");
    fs::write(&file, r#"{"repos":[]}"#).unwrap();
    assert_eq!(test.start(1), Ok(()));
    assert_eq!(*test.fake.listening.lock().unwrap(), [NO_EGRESS; 2]);
    assert!(test.fault_codes().is_empty());
}

/// A present `repos.json` that does not read binds 1103, and the deny
/// check names it. A zero-byte file is present and does not read, so it
/// binds 1103 too (decision CH73 rule 1).
#[test]
fn a_bad_clone_list_binds_1103() {
    let test = Test::new("repos-bad", |_| {});
    let file = test.dir.join("families/chat/repos.json");
    fs::write(&file, "{").unwrap();
    assert_eq!(test.start(0), Ok(()));
    assert!(listeners(&test, 0).contains(&"vsock.sock_1103".to_owned()));
    fs::write(&file, "").unwrap();
    assert_eq!(test.start(1), Ok(()));
    assert!(listeners(&test, 1).contains(&"vsock.sock_1103".to_owned()));
    assert_eq!(*test.fake.listening.lock().unwrap(), [REPOS_ONLY; 2]);
    assert!(test.fault_codes().is_empty());
}

/// The child of `a_clone_on_a_bad_clone_list_gets_denied_and_one_line`,
/// run in a process of its own, so its stderr holds only the lines of the
/// vmm. It starts a VM whose `repos.json` does not read, then sends the
/// first pkt-line of a clone on its 1103.
#[test]
#[ignore = "the child of a_clone_on_a_bad_clone_list_gets_denied_and_one_line"]
fn clone_on_a_bad_clone_list_for_the_log_test() {
    let test = Test::new("repos-bad-clone", |_| {});
    fs::write(test.dir.join("families/chat/repos.json"), "{").unwrap();
    assert_eq!(test.start(0), Ok(()));
    let path = test.dir.join("run/slot/0/vsock.sock_1103");
    // The current-thread runtime runs the mirror only inside `block_on`,
    // so the guest side runs in `spawn_blocking`.
    let answer = test.runtime.block_on(test.runtime.spawn_blocking(move || {
        let mut guest = UnixStream::connect(path).unwrap();
        guest
            .set_read_timeout(Some(Duration::from_secs(5)))
            .unwrap();
        let payload = "git-upload-pack /creche\0host=127.0.0.1:9418\0";
        let line = format!("{:04x}{payload}", payload.len() + 4);
        guest.write_all(line.as_bytes()).unwrap();
        guest.shutdown(Shutdown::Write).unwrap();
        let mut answer = Vec::new();
        guest.read_to_end(&mut answer).unwrap();
        answer
    }));
    assert_eq!(answer.unwrap(), DENIED);
}

/// A clone on the 1103 of a `repos.json` that does not read gets the one
/// error and one stderr line that names `not_valid` (decisions CH69 rule
/// 24, CH73 rule 1). The test runs its own binary as the child
/// `clone_on_a_bad_clone_list_for_the_log_test` and reads the real stderr
/// of the vmm code. The fake launch probes 1103 with a dial that sends no
/// line, so stderr may also hold its `bad_request` line.
#[test]
fn a_clone_on_a_bad_clone_list_gets_denied_and_one_line() {
    let output = Command::new(std::env::current_exe().unwrap())
        .args(["--exact", "clone_on_a_bad_clone_list_for_the_log_test"])
        .args(["--ignored", "--nocapture", "--test-threads=1"])
        .output()
        .unwrap();
    let stderr = String::from_utf8(output.stderr).unwrap();
    let stdout = String::from_utf8(output.stdout).unwrap();
    assert!(output.status.success(), "{stdout}{stderr}");
    assert!(stdout.contains("1 passed"), "{stdout}");
    let prefix = format!("vmm: a clone of the family chat, session {A}, ");
    // Only a line that ends in its LF counts.
    let reasons: Vec<&str> = stderr
        .split_inclusive('\n')
        .filter_map(|line| line.strip_suffix('\n'))
        .filter_map(|line| line.strip_prefix(prefix.as_str()))
        .collect();
    let named: Vec<&str> = reasons
        .iter()
        .copied()
        .filter(|reason| reason.contains("not_valid"))
        .collect();
    assert_eq!(named, ["refused: repos_not_valid"], "{stderr}");
    let probe = reasons
        .iter()
        .filter(|reason| **reason == "refused: bad_request");
    assert_eq!(probe.count() + named.len(), reasons.len(), "{stderr}");
}

/// A restore binds 1103 again before CH starts, as each plane listener
/// (decisions CH62 rule 14, CH69 rule 13).
#[test]
fn a_restore_binds_1103_again() {
    let test = Test::new("repos-restore", |_| {});
    fs::write(test.dir.join("families/chat/repos.json"), REPOS).unwrap();
    assert_eq!(test.start(0), Ok(()));
    let id = test.rows[0].id.clone();
    assert!(test.runtime.block_on(test.hook.stop(&id)).is_ok());
    test.snapshot(0, Some(&test.key()));
    test.reply(0, "Paused", &[]);
    assert_eq!(test.start(0), Ok(()));
    assert!(test.restored(1));
    assert_eq!(*test.fake.listening.lock().unwrap(), [REPOS_ONLY; 2]);
}

/// The listeners of a VM of a family with no egress and no clone list.
const PLANES: [&str; 3] = ["vsock.sock_1100", "vsock.sock_1101", "vsock.sock_1102"];

/// A CONNECT to an IP literal, which the proxy refuses with 403 before
/// any lookup (decision CH6-4), so no test reaches the network.
const IP_CONNECT: &str = "CONNECT 192.0.2.1:443 HTTP/1.1\r\nHost: 192.0.2.1:443\r\n\r\n";

#[cfg(test)]
impl Test {
    /// Pauses the clock of the runtime, so a `POLL` of the edit follow
    /// costs no real time.
    fn pause(&self) {
        self.runtime.block_on(async { tokio::time::pause() });
    }

    /// Runs the paused runtime one `POLL` at a time until `done` holds,
    /// for at most 5 s of real time. Then it runs two more, so the poll
    /// that met `done` ends its deny check (decision CH69 rule 11).
    fn follow(&self, done: impl Fn() -> bool) {
        let deadline = Instant::now() + Duration::from_secs(5);
        while !done() {
            assert!(Instant::now() < deadline, "the follow did not act in 5 s");
            self.runtime
                .block_on(async { tokio::time::sleep(POLL).await });
        }
        for _ in 0..2 {
            self.runtime
                .block_on(async { tokio::time::sleep(POLL).await });
        }
    }

    /// The path of the listener of `port` of row `index`.
    fn listener(&self, index: usize, port: u16) -> PathBuf {
        let id = &self.rows[index].id;
        self.dir.join(format!("run/vm/{id}/vsock.sock_{port}"))
    }

    /// Writes `request` on the listener of `port` of row `index`, then
    /// reads to the close. A reset counts as the close: Linux resets a
    /// Unix socket that closes with input unread. The current-thread
    /// runtime runs each server only inside `block_on`, so the guest side
    /// runs in `spawn_blocking`.
    fn ask(&self, index: usize, port: u16, request: &[u8]) -> Vec<u8> {
        let (path, request) = (self.listener(index, port), request.to_vec());
        let answer = self.runtime.block_on(self.runtime.spawn_blocking(move || {
            let mut guest = UnixStream::connect(path).unwrap();
            guest
                .set_read_timeout(Some(Duration::from_secs(5)))
                .unwrap();
            guest.write_all(&request).unwrap();
            guest.shutdown(Shutdown::Write).unwrap();
            let mut answer = Vec::new();
            match guest.read_to_end(&mut answer) {
                Err(error) if error.kind() != io::ErrorKind::ConnectionReset => panic!("{error}"),
                _ => answer,
            }
        }));
        answer.unwrap()
    }

    /// Whether the VM of row `index` runs, with no fault.
    fn runs_clean(&self, index: usize) -> bool {
        self.hook.running(&self.rows[index].id) && self.fault_codes().is_empty()
    }
}

/// An edit that lists a host on a running VM binds 1104 at the next poll,
/// and the proxy serves it: a CONNECT to an IP literal gets 403. The deny
/// check names 1104, so the VM runs on with no fault (plan PR E3,
/// decisions CH49 rule 3, CH69 rule 11).
#[test]
fn an_egress_edit_binds_1104_on_a_running_vm() {
    let test = Test::new("follow-egress", |_| {});
    assert_eq!(test.start(0), Ok(()));
    assert_eq!(listeners(&test, 0), PLANES);
    test.pause();
    fs::write(test.dir.join("families/chat/egress.json"), EGRESS).unwrap();
    test.follow(|| test.listener(0, 1104).exists());
    let reply = test.ask(0, 1104, IP_CONNECT.as_bytes());
    assert!(reply.starts_with(b"HTTP/1.1 403 "), "{reply:?}");
    assert!(test.runs_clean(0));
    assert!(!test.stopped(0));
}

/// An edit that empties the list removes the 1104 listener and its
/// socket file, and the deny check without 1104 passes. A later edit that
/// lists a host binds it again (decisions CH49 rule 3, CH69 rule 12).
#[test]
fn an_emptied_list_removes_1104_and_the_check_passes() {
    let test = Test::new("follow-empty", |_| {});
    let file = test.dir.join("families/chat/egress.json");
    fs::write(&file, EGRESS).unwrap();
    assert_eq!(test.start(0), Ok(()));
    test.pause();
    fs::write(&file, r#"{"hosts":[]}"#).unwrap();
    test.follow(|| !test.listener(0, 1104).exists());
    assert_eq!(listeners(&test, 0), PLANES);
    assert!(UnixStream::connect(test.listener(0, 1104)).is_err());
    assert!(test.runs_clean(0));

    fs::write(&file, EGRESS).unwrap();
    test.follow(|| test.listener(0, 1104).exists());
    let reply = test.ask(0, 1104, IP_CONNECT.as_bytes());
    assert!(reply.starts_with(b"HTTP/1.1 403 "), "{reply:?}");
    assert!(test.runs_clean(0));
}

/// An edit that lists a repo on a running VM binds 1103 at the next poll,
/// and the mirror serves it: the mirror root of the test is missing, so a
/// clone gets the one error (decisions CH69 rules 11, 13 and 16).
#[test]
fn a_repos_edit_binds_1103_on_a_running_vm() {
    let test = Test::new("follow-repos", |_| {});
    assert_eq!(test.start(0), Ok(()));
    test.pause();
    fs::write(test.dir.join("families/chat/repos.json"), REPOS).unwrap();
    test.follow(|| test.listener(0, 1103).exists());
    assert!(listeners(&test, 0).contains(&"vsock.sock_1103".to_owned()));
    let payload = "git-upload-pack /creche\0host=127.0.0.1:9418\0";
    let line = format!("{:04x}{payload}", payload.len() + 4);
    assert_eq!(test.ask(0, 1103, line.as_bytes()), DENIED);
    assert!(test.runs_clean(0));
}

/// A deny check that fails after an edit writes `egress_assert_failed` to
/// the fault file and stops the VM, with the graceful stop first: a
/// listener that the vmm did not bind sits in the run dir (decisions
/// CH6-3, CH19 (3), CH69 rule 12).
#[test]
fn a_failed_check_after_an_edit_writes_the_fault_and_stops_the_vm() {
    let test = Test::new("follow-fault", |_| {});
    assert_eq!(test.start(0), Ok(()));
    let _stray = UnixListener::bind(test.listener(0, 1105)).unwrap();
    test.pause();
    fs::write(test.dir.join("families/chat/egress.json"), EGRESS).unwrap();
    test.follow(|| test.stopped(0) && !test.hook.running(&test.rows[0].id));
    assert_eq!(test.fault_codes(), ["egress_assert_failed"]);
    eventually(|| test.stop_lines() == 1);
}

/// A file that does not change binds nothing and runs no deny check: the
/// same bytes written again keep the 1104 listener of the start, the same
/// socket file. The check runs after each edit only (decisions CH6-3,
/// CH69 rule 11), so a stray listener that would fail it stays unseen,
/// and the VM runs with no fault.
#[test]
fn an_unchanged_file_binds_nothing() {
    use std::os::unix::fs::MetadataExt as _;

    let test = Test::new("follow-same", |_| {});
    let file = test.dir.join("families/chat/egress.json");
    fs::write(&file, EGRESS).unwrap();
    assert_eq!(test.start(0), Ok(()));
    let inode = || fs::metadata(test.listener(0, 1104)).unwrap().ino();
    let before = inode();
    let _stray = UnixListener::bind(test.listener(0, 1105)).unwrap();
    test.pause();
    fs::write(&file, EGRESS).unwrap();
    test.follow(|| true);
    test.follow(|| true);
    assert_eq!(inode(), before);
    assert!(test.runs_clean(0));
    assert!(!test.stopped(0));
}

/// The follow compares bytes (decision CH69 rule 11). New bytes that read
/// as the same list are an edit, so the deny check runs again (decision
/// CH6-3). A stray listener fails it, and the VM faults and stops
/// (decision CH69 rule 12).
#[test]
fn new_bytes_of_the_same_list_rerun_the_deny_check() {
    let test = Test::new("follow-same-list", |_| {});
    let file = test.dir.join("families/chat/egress.json");
    fs::write(&file, EGRESS).unwrap();
    assert_eq!(test.start(0), Ok(()));
    let _stray = UnixListener::bind(test.listener(0, 1105)).unwrap();
    test.pause();
    fs::write(&file, format!("{EGRESS}\n")).unwrap();
    test.follow(|| test.stopped(0) && !test.hook.running(&test.rows[0].id));
    assert_eq!(test.fault_codes(), ["egress_assert_failed"]);
}

/// A file that does not read and changes to other bytes that do not read
/// is an edit too (decision CH69 rule 11). Both bind 1104 (decision CH72
/// rule 3), and the deny check runs again and fails on a stray listener.
#[test]
fn a_bad_file_that_changes_to_other_bad_bytes_reruns_the_deny_check() {
    let test = Test::new("follow-bad-bytes", |_| {});
    let file = test.dir.join("families/chat/egress.json");
    fs::write(&file, "not json").unwrap();
    assert_eq!(test.start(0), Ok(()));
    let _stray = UnixListener::bind(test.listener(0, 1105)).unwrap();
    test.pause();
    fs::write(&file, "still not json").unwrap();
    test.follow(|| test.stopped(0) && !test.hook.running(&test.rows[0].id));
    assert_eq!(test.fault_codes(), ["egress_assert_failed"]);
}

/// A read that takes no bytes, here of a symlink, has nothing to compare,
/// so each poll is a new reading and runs the deny check. It fails on a
/// stray listener with no edit of the file.
#[test]
fn a_file_with_no_bytes_to_compare_reruns_the_deny_check() {
    let test = Test::new("follow-no-bytes", |_| {});
    let file = test.dir.join("families/chat/egress.json");
    fs::write(test.dir.join("egress.json"), EGRESS).unwrap();
    std::os::unix::fs::symlink(test.dir.join("egress.json"), &file).unwrap();
    assert_eq!(test.start(0), Ok(()));
    let _stray = UnixListener::bind(test.listener(0, 1105)).unwrap();
    test.pause();
    test.follow(|| test.stopped(0) && !test.hook.running(&test.rows[0].id));
    assert_eq!(test.fault_codes(), ["egress_assert_failed"]);
}

/// An edit that empties the clone list removes the 1103 listener and its
/// socket file, and the deny check without 1103 passes (decisions CH49
/// rule 3, CH69 rules 12 and 13).
#[test]
fn an_emptied_repos_list_removes_1103() {
    let test = Test::new("follow-repos-empty", |_| {});
    let file = test.dir.join("families/chat/repos.json");
    fs::write(&file, REPOS).unwrap();
    assert_eq!(test.start(0), Ok(()));
    test.pause();
    fs::write(&file, r#"{"repos":[]}"#).unwrap();
    test.follow(|| !test.listener(0, 1103).exists() || test.stopped(0));
    assert_eq!(listeners(&test, 0), PLANES);
    assert!(test.runs_clean(0));
}

/// A bind that fails after an edit writes `egress_assert_failed` and
/// stops the VM: a path at `vsock.sock_1104` that the vmm did not bind
/// fails the bind (decisions CH69 rule 12, CH74 rule 1).
#[test]
fn a_failed_bind_after_an_edit_writes_the_fault_and_stops_the_vm() {
    let test = Test::new("follow-bind", |_| {});
    assert_eq!(test.start(0), Ok(()));
    let _stray = UnixListener::bind(test.listener(0, 1104)).unwrap();
    test.pause();
    fs::write(test.dir.join("families/chat/egress.json"), EGRESS).unwrap();
    test.follow(|| test.stopped(0) && !test.hook.running(&test.rows[0].id));
    assert_eq!(test.fault_codes(), ["egress_assert_failed"]);
}

/// An edit that empties the list closes the 1104 listener and removes its
/// socket file, so a new dial fails. A link that was open runs to its own
/// end (decision CH74 rule 2, fact 43). A refill binds a fresh listener
/// at the same path while that link drains, and a new link there gets
/// its 403. No test reaches the network, so the link waits in
/// its head read, not in a tunnel: the guest sends the rest of its head
/// after both edits and gets the 403 of its CONNECT. The polls take about
/// 14 s of paused time, inside `HEAD_WAIT` (30 s). A drop of the server
/// at the edit cuts the link, and the test fails.
#[test]
fn an_emptied_list_lets_an_open_link_end() {
    let test = Test::new("follow-drain", |_| {});
    let file = test.dir.join("families/chat/egress.json");
    fs::write(&file, EGRESS).unwrap();
    assert_eq!(test.start(0), Ok(()));
    let mut guest = UnixStream::connect(test.listener(0, 1104)).unwrap();
    guest
        .set_read_timeout(Some(Duration::from_secs(5)))
        .unwrap();
    guest
        .write_all(b"CONNECT 192.0.2.1:443 HTTP/1.1\r\n")
        .unwrap();
    test.pause();
    fs::write(&file, r#"{"hosts":[]}"#).unwrap();
    test.follow(|| !test.listener(0, 1104).exists());
    assert_eq!(listeners(&test, 0), PLANES);
    assert!(UnixStream::connect(test.listener(0, 1104)).is_err());

    fs::write(&file, EGRESS).unwrap();
    test.follow(|| test.listener(0, 1104).exists());
    let reply = test.ask(0, 1104, IP_CONNECT.as_bytes());
    assert!(reply.starts_with(b"HTTP/1.1 403 "), "{reply:?}");

    let ended = test.runtime.spawn_blocking(move || {
        guest.write_all(b"Host: 192.0.2.1:443\r\n\r\n").unwrap();
        let mut answer = Vec::new();
        guest.read_to_end(&mut answer).unwrap();
        answer
    });
    let answer = test.runtime.block_on(ended).unwrap();
    assert!(answer.starts_with(b"HTTP/1.1 403 "), "{answer:?}");
    assert!(test.runs_clean(0));
}

/// An edit that empties the clone list closes the 1103 listener and
/// removes its socket file, so a new dial fails. A clone that was open
/// runs to its own end (decision CH74 rule 2, fact 43): `upload-pack`
/// sent its refs before the edit, and after it the guest sends its want
/// and gets the pack. A drop of the server at the edit kills the clone,
/// and the test fails.
#[test]
fn an_emptied_clone_list_lets_an_open_clone_end() {
    let test = Test::new("follow-clone", |_| {});
    let repo = test.dir.join("mirror/creche");
    git(&test.dir, |git| git.args(["init", "--quiet"]).arg(&repo));
    git(&test.dir, |git| {
        git.arg("-C").arg(&repo);
        git.args(["-c", "user.name=t", "-c", "user.email=t@example.com"]);
        git.args(["commit", "--quiet", "--allow-empty", "-m", "one"])
    });
    let file = test.dir.join("families/chat/repos.json");
    fs::write(&file, REPOS).unwrap();
    assert_eq!(test.start(0), Ok(()));
    let path = test.listener(0, 1103);
    // The current-thread runtime runs the mirror only inside `block_on`,
    // so the guest side runs in `spawn_blocking`.
    let opened = test.runtime.spawn_blocking(move || {
        let mut guest = UnixStream::connect(path).unwrap();
        guest
            .set_read_timeout(Some(Duration::from_secs(5)))
            .unwrap();
        let payload = "git-upload-pack /creche\0host=127.0.0.1:9418\0";
        let line = format!("{:04x}{payload}", payload.len() + 4);
        guest.write_all(line.as_bytes()).unwrap();
        let head = read_pkt(&mut guest).unwrap();
        while read_pkt(&mut guest).is_some() {}
        (guest, String::from_utf8(head[..40].to_vec()).unwrap())
    });
    let (mut guest, head) = test.runtime.block_on(opened).unwrap();
    test.pause();
    fs::write(&file, r#"{"repos":[]}"#).unwrap();
    test.follow(|| !test.listener(0, 1103).exists());
    assert_eq!(listeners(&test, 0), PLANES);
    assert!(UnixStream::connect(test.listener(0, 1103)).is_err());

    let fetched = test.runtime.spawn_blocking(move || {
        let want = format!("want {head}\n");
        let lines = format!("{:04x}{want}00000009done\n", want.len() + 4);
        guest.write_all(lines.as_bytes()).unwrap();
        let mut answer = Vec::new();
        guest.read_to_end(&mut answer).unwrap();
        answer
    });
    let answer = test.runtime.block_on(fetched).unwrap();
    assert!(answer.starts_with(b"0008NAK\nPACK"), "{answer:?}");
    assert!(test.runs_clean(0));
}

/// A link that drains after an emptied list ends with the VM: the drop of
/// the VM entry drops the follow, and with it each link that drains
/// (decision CH74 rule 2). The link is open after the edit and reads its
/// end after the drop, and the drop writes no fault.
#[test]
fn a_draining_link_ends_with_the_vm() {
    let test = Test::new("follow-drain-end", |_| {});
    let file = test.dir.join("families/chat/egress.json");
    fs::write(&file, EGRESS).unwrap();
    assert_eq!(test.start(0), Ok(()));
    let mut guest = UnixStream::connect(test.listener(0, 1104)).unwrap();
    // macOS fails `set_read_timeout` with EINVAL once the peer closed, so
    // the timeout goes on before the cut.
    guest
        .set_read_timeout(Some(Duration::from_secs(1)))
        .unwrap();
    guest
        .write_all(b"CONNECT 192.0.2.1:443 HTTP/1.1\r\n")
        .unwrap();
    test.pause();
    fs::write(&file, r#"{"hosts":[]}"#).unwrap();
    test.follow(|| !test.listener(0, 1104).exists());
    guest.set_nonblocking(true).unwrap();
    let open = guest.read(&mut [0; 1]).map_err(|error| error.kind());
    assert_eq!(open, Err(io::ErrorKind::WouldBlock));
    guest.set_nonblocking(false).unwrap();

    assert!(test.drop_session(0).is_ok());
    test.follow(|| true);
    let mut answer = Vec::new();
    match guest.read_to_end(&mut answer) {
        Ok(_) => assert!(answer.is_empty(), "{answer:?}"),
        Err(error) => assert_eq!(error.kind(), io::ErrorKind::ConnectionReset, "{error}"),
    }
    assert!(test.fault_codes().is_empty());
}

/// The follow ends with the VM: the drop of the VM entry drops the
/// `Watch`, and with it the 1104 server of the start. A link that the
/// guest opened before the drop reads its end, and the drop writes no
/// fault (plan PR E3).
#[test]
fn the_follow_ends_with_the_vm() {
    let test = Test::new("follow-drop", |_| {});
    fs::write(test.dir.join("families/chat/egress.json"), EGRESS).unwrap();
    assert_eq!(test.start(0), Ok(()));
    let mut guest = UnixStream::connect(test.listener(0, 1104)).unwrap();
    guest
        .set_read_timeout(Some(Duration::from_secs(1)))
        .unwrap();
    guest
        .write_all(b"CONNECT 192.0.2.1:443 HTTP/1.1\r\n")
        .unwrap();
    assert!(test.drop_session(0).is_ok());
    test.pause();
    test.follow(|| true);
    test.follow(|| true);
    let mut answer = Vec::new();
    match guest.read_to_end(&mut answer) {
        Ok(_) => {}
        Err(error) => assert_eq!(error.kind(), io::ErrorKind::ConnectionReset, "{error}"),
    }
    assert!(test.fault_codes().is_empty());
    assert!(!test.listener(0, 1104).exists());
}

/// A check that fails while a stop owns the VM writes no fault: the VM
/// entry, and so the follow, lives on until the stop ends its CH. The
/// guest ignores the graceful stop, so the stop waits `DROP_STOP_WAIT`,
/// and the follow polls during that wait. A stray listener fails each
/// check (decisions CH21-1, CH69 rule 12).
#[test]
fn a_failed_check_during_a_stop_writes_no_fault() {
    let test = Test::new("follow-stopping", |_| {});
    *test.fake.guest.lock().unwrap() = Guest::Ignores;
    assert_eq!(test.start(0), Ok(()));
    let _stray = UnixListener::bind(test.listener(0, 1105)).unwrap();
    fs::write(test.dir.join("families/chat/egress.json"), EGRESS).unwrap();
    test.pause();
    assert!(test.drop_session(0).is_ok());
    assert!(test.stopped(0));
    assert!(!test.hook.running(&test.rows[0].id));
    assert!(test.fault_codes().is_empty());
}

/// A check that fails after the CH of the VM exited on its own writes no
/// fault, and the stop of the follow removes the VM (decision CH69 rule
/// 12).
#[test]
fn a_failed_check_after_ch_exited_writes_no_fault() {
    let test = Test::new("follow-exited", |_| {});
    assert_eq!(test.start(0), Ok(()));
    let _stray = UnixListener::bind(test.listener(0, 1105)).unwrap();
    test.fake.vms.lock().unwrap()[0].store(true, Ordering::SeqCst);
    test.pause();
    fs::write(test.dir.join("families/chat/egress.json"), EGRESS).unwrap();
    test.follow(|| !test.hook.running(&test.rows[0].id));
    assert!(test.fault_codes().is_empty());
}

/// Runs the git of the host with no config of the host or the user, as
/// `tests/mirror.rs` does, and fails the test unless it succeeds.
#[cfg(test)]
fn git(home: &std::path::Path, args: impl FnOnce(&mut Command) -> &mut Command) {
    let mut command = Command::new("/usr/bin/git");
    command
        .env_clear()
        .env("GIT_CONFIG_NOSYSTEM", "1")
        .env("GIT_CONFIG_GLOBAL", "/dev/null")
        .env("HOME", home)
        .env("PATH", "/usr/bin:/bin");
    let output = args(&mut command).output().unwrap();
    assert!(output.status.success(), "{output:?}");
}

/// Reads one pkt-line of `guest`: its payload, or `None` for a flush.
#[cfg(test)]
fn read_pkt(guest: &mut UnixStream) -> Option<Vec<u8>> {
    let mut length = [0; 4];
    guest.read_exact(&mut length).unwrap();
    let length = usize::from_str_radix(std::str::from_utf8(&length).unwrap(), 16).unwrap();
    if length == 0 {
        return None;
    }
    let mut payload = vec![0; length - 4];
    guest.read_exact(&mut payload).unwrap();
    Some(payload)
}

/// `creds.json` of the family `chat`, as caregiver writes it.
const CREDS: &str = r#"{"epoch":3,"litellm_key":"sk-start-key","pep_token":"abababababababababababababababababababababababababababababababab","written_at":"2026-11-06T12:00:00Z"}"#;

/// A pull on host port 1100 does not set `last_use` (decision CH62 rule
/// 22). The VM pulls 300 ms after its start, and the idle check 100 ms
/// past the timeout from the start still takes it. The reply is the
/// `state` of the family of the session in the family root of the config,
/// so the start hands the server the right family and root.
#[test]
fn a_pull_does_not_set_last_use() {
    let test = Test::new("pull-use", |_| {});
    let family = test.dir.join("families/chat");
    fs::create_dir_all(family.join("creds")).unwrap();
    fs::write(family.join("creds/creds.json"), CREDS).unwrap();
    fs::write(
        family.join("config/runtime.json"),
        r#"{"shell":true,"sandbox_tools":["read"],"model_alias":"litellm/test"}"#,
    )
    .unwrap();
    assert_eq!(test.start(0), Ok(()));
    let started = Instant::now();
    std::thread::sleep(Duration::from_millis(300));
    let path = test.dir.join("run/slot/0/vsock.sock_1100");
    // The current-thread runtime runs the listener only inside
    // `block_on`, so the guest side runs in `spawn_blocking`.
    let reply = test.runtime.block_on(test.runtime.spawn_blocking(move || {
        let mut guest = UnixStream::connect(path).unwrap();
        guest
            .set_read_timeout(Some(Duration::from_secs(5)))
            .unwrap();
        guest.write_all(b"{}\n").unwrap();
        let mut reply = String::new();
        BufReader::new(guest).read_line(&mut reply).unwrap();
        reply
    }));
    let reply: serde_json::Value = serde_json::from_str(&reply.unwrap()).unwrap();
    assert_eq!(reply["state"]["creds"]["litellm_key"], "sk-start-key");
    let at = started + IDLE_TIMEOUT + Duration::from_millis(100);
    let slept = test.runtime.block_on(test.hook.sleep_idle(at));
    assert_eq!(slept, [test.rows[0].id.clone()]);
}
