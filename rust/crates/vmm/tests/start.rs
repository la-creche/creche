//! The start hook over fakes (plan PRs L16 and L17), and the idle sleep
//! and the sweep over the same fakes (plan PR L18). A fake launcher
//! records each launch, writes a fake command line file and binds a fake
//! vsock socket that plays CH and guest init. A fake CH API answers
//! `info` and `resume` of a restore, and its snapshot ends the fake VM. Every other step is the real one on
//! a scratch dir in `/tmp`: the shape read, admission, the store check,
//! the session disk (with `/usr/bin/true` as `mke2fs`), the key check,
//! the claim, the deny check and the fault file.

use std::io::{BufRead, BufReader, Read, Write};
use std::net::Shutdown;
use std::os::unix::fs::PermissionsExt;
use std::os::unix::net::{UnixListener, UnixStream};
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex, PoisonError};
use std::thread::spawn;
use std::time::{Duration, Instant, SystemTime};
use std::{fs, io};

use creche_vmm::control::ControlState;
use creche_vmm::handlers::acp::{Acp, StartFault, StartVm};
use creche_vmm::handlers::drop::{Drops, StopVm, VmStopFailed};
use creche_vmm::relay::{AcpLink, Door, Relays};
use creche_vmm::restore::ChApi;
use creche_vmm::server::Stop;
use creche_vmm::shutdown::{Ended, VmSet, shutdown};
use creche_vmm::start::{ColdBoot, Digests, Launcher, StartConfig};
use creche_vmm::stop::VmProcess;
use creche_vmm_confine::admission::{GIB, Limits};
use creche_vmm_confine::family::FamilyRoot;
use creche_vmm_confine::faults::{FaultBook, FaultDir};
use creche_vmm_confine::launch::{Launch, Program};
use creche_vmm_confine::spawner::SpawnError;
use creche_vmm_store::ch_remote::{ChRemoteError, VmInfo};
use creche_vmm_store::images::{ImageDigest, ImageStore};
use creche_vmm_store::key::{CheckedCh, SnapshotKey, check_cloud_hypervisor, write_key};
use creche_vmm_store::layout::{RunRoot, SessionDir, SnapshotGeneration, StateRoot, VmRunDir};
use creche_vmm_store::retention::{BUDGET, Reason};
use creche_vmm_store::rows::Rows;
use creche_vmm_store::session_disk::SessionDisks;
use creche_vmm_store::sleep_gate::IDLE_TIMEOUT;
use creche_wire::{
    DropError, DropRoute, DropSession, DropTarget, Dropped, FamilyName, OpenAcp, Serve,
    ServeUpgrade as _, SessionId, SessionRow,
};
use tokio::runtime::{Builder, Runtime};

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
/// The SHA-256 of no bytes: the fake kernel and root disk are empty.
const EMPTY: &str = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855";

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
    /// Fails each launch.
    fail: AtomicBool,
    /// Holds each launch until the test clears it.
    hold: AtomicBool,
    /// The `stop` lines that guest init read on each control link.
    stop_lines: Arc<AtomicUsize>,
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
        let vsock = launch.files().run_dir().path().join("vsock.sock");
        let listener = UnixListener::bind(vsock).unwrap();
        let guest = *self.guest.lock().unwrap();
        let exited = Arc::new(AtomicBool::new(false));
        let broken = Arc::new(AtomicBool::new(false));
        let vm = Vm {
            exited: Arc::clone(&exited),
            broken: Arc::clone(&broken),
            stop_lines: Arc::clone(&self.stop_lines),
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

/// The fake CH API and what it saw. `DEVICES` is its `READS_DEVICES`,
/// and `SYNCS` is its `GUEST_SYNCS`. `SYNCS` is true by default, so the
/// take path of an eviction and a sleep stays under test before the
/// control link can sync the guest.
#[derive(Debug)]
struct FakeApi<const DEVICES: bool = true, const SYNCS: bool = true> {
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
    /// Writes generation 1 of the session with the key, as a real take
    /// does.
    keep: AtomicBool,
}

#[cfg(test)]
impl<const DEVICES: bool, const SYNCS: bool> ChApi for FakeApi<DEVICES, SYNCS> {
    const READS_DEVICES: bool = DEVICES;
    const GUEST_SYNCS: bool = SYNCS;

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
            write_key(session, generation, key).map_err(|error| format!("{error:?}"))?;
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
    links: Arc<Mutex<Vec<UnixStream>>>,
}

/// Plays CH and guest init on each link: `CONNECT`, then the control
/// link or the ACP link. Each link stays open until the test ends. A
/// `stop` line on the control link powers the VM off (decision CH19 (3)).
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
                spawn(move || {
                    for line in reader.lines() {
                        if line.is_ok_and(|line| line.contains("stop")) {
                            vm.stop_lines.fetch_add(1, Ordering::SeqCst);
                            if guest == Guest::Wedges {
                                std::thread::sleep(Duration::from_millis(20));
                                vm.broken.store(true, Ordering::SeqCst);
                            } else {
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
struct Test<const DEVICES: bool = true, const SYNCS: bool = true> {
    dir: PathBuf,
    hook: Arc<ColdBoot<Fake, FakeApi<DEVICES, SYNCS>>>,
    fake: Arc<Fake>,
    api: Arc<FakeApi<DEVICES, SYNCS>>,
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
        Self::build(name, edit)
    }
}

#[cfg(test)]
impl<const DEVICES: bool, const SYNCS: bool> Test<DEVICES, SYNCS> {
    /// The hook over a fake API whose `READS_DEVICES` is `DEVICES` and
    /// whose `GUEST_SYNCS` is `SYNCS`.
    fn build(name: &str, edit: impl FnOnce(&mut StartConfig)) -> Self {
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
            digests: Some(Digests {
                kernel: empty,
                base: empty,
                python: empty,
            }),
            initramfs: None,
            ch: Program::new(PathBuf::from(CH)).unwrap(),
            systemd_run: Program::new(PathBuf::from("/usr/bin/systemd-run")).unwrap(),
            limits: Limits {
                ram_cap: 80 * GIB,
                free_floor: 0,
            },
            budget: BUDGET,
            ch_version: ch,
        };
        edit(&mut config);

        let faults = FaultDir::open(&dir.join("faults")).unwrap();
        let path = faults.file(&"chat".parse::<FamilyName>().unwrap());
        let book = FaultBook::start(faults, SystemTime::now()).unwrap();
        let fake = Arc::new(Fake {
            dir: dir.clone(),
            guest: Mutex::new(Guest::Ready),
            net: AtomicBool::new(false),
            fail: AtomicBool::new(false),
            hold: AtomicBool::new(false),
            stop_lines: Arc::default(),
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

    /// The key of a VM of `chat`: the fake kernel and root disk.
    fn key(&self) -> SnapshotKey {
        let images = ImageStore::new(&self.state, PathBuf::from("/usr/bin/zstd"));
        let empty = images.check(EMPTY.parse().unwrap()).unwrap();
        SnapshotKey::current(self.ch, &empty, &empty)
    }

    /// Makes generation 1 of row `index` with `key`, or with a key of
    /// another CH rev when `key` is `None`.
    fn snapshot(&self, index: usize, key: Option<&SnapshotKey>) -> PathBuf {
        let session = self.state.session(&self.rows[index].id);
        let dir = session.snapshot(SnapshotGeneration(1));
        fs::create_dir_all(&dir).unwrap();
        match key {
            Some(key) => write_key(&session, SnapshotGeneration(1), key).unwrap(),
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
            "serial": {"file": self.dir.join(format!("run/vm/{id}/serial.log")), "mode": "File"},
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

impl<const DEVICES: bool, const SYNCS: bool> Drop for Test<DEVICES, SYNCS> {
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
    let serial = test.dir.join(format!("run/vm/{A}/serial.log"));
    let mode = fs::metadata(serial).unwrap().permissions().mode();
    assert_eq!(mode & 0o777, 0o600);
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
/// `mke2fs` (decision CH44 rule 2, PR D7). So does a config with no
/// digests: the vmm of plan PR L20 until it reads the `digests.json` of
/// decision CH57 rule 1. None launches.
#[test]
fn a_bad_image_or_disk_faults_before_the_launch() {
    let test = Test::new("digests", |config| config.digests = None);
    assert_eq!(test.start(0), Err(StartFault::SandboxStartFailed));
    assert_eq!(test.fault_codes(), ["sandbox_start_failed"]);
    assert_eq!(test.launches(), 0);

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
impl<const DEVICES: bool, const SYNCS: bool> Test<DEVICES, SYNCS> {
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
impl<const DEVICES: bool, const SYNCS: bool> Test<DEVICES, SYNCS> {
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

/// A VM whose ready said `starting` is up only after a turn event, and
/// each turn event sets its last use (decisions CH43 rule 8, CH50 rule
/// 5).
#[test]
fn a_turn_event_marks_the_facade_up_and_used() {
    let test = Test::new("seen", |_| {});
    *test.fake.guest.lock().unwrap() = Guest::Starting;
    assert_eq!(test.start(0), Ok(()));
    assert!(test.sleep_after(PAST_IDLE).is_empty());

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

/// With no device read, no snapshot would restore, so an eviction takes
/// none. It sends the graceful stop, and guest init powers off (decision
/// CH19 (3)).
#[test]
fn with_no_device_read_an_eviction_stops_gracefully() {
    let test = Test::<false>::build("plain", |config| config.limits.ram_cap = 512 << 20);
    assert_eq!(test.start(0), Ok(()));
    assert_eq!(test.start(1), Ok(()));
    assert!(test.api.snapshots.lock().unwrap().is_empty());
    assert!(!test.api.entered.load(Ordering::SeqCst));
    assert_eq!(test.stop_lines(), 1);
    assert!(test.stopped(0));
    assert!(test.hook.running(&test.rows[1].id));
}

/// With no guest sync, a retire takes no snapshot: the control link has no
/// `sync` command until phase 3 (item 5.7, the item 42 note of the
/// design). An eviction and a sleep each send the graceful stop, and
/// guest init syncs and powers off (decision CH19 (3)).
#[test]
fn with_no_guest_sync_a_retire_stops_gracefully() {
    let test = Test::<true, false>::build("nosync", |config| config.limits.ram_cap = 512 << 20);
    assert_eq!(test.start(0), Ok(()));
    assert_eq!(test.start(1), Ok(()));
    assert!(!test.api.entered.load(Ordering::SeqCst));
    assert_eq!(test.stop_lines(), 1);
    assert!(test.stopped(0));
    assert!(test.hook.running(&test.rows[1].id));

    assert_eq!(test.sleep_after(PAST_IDLE), [test.rows[1].id.clone()]);
    assert!(!test.api.entered.load(Ordering::SeqCst));
    assert!(test.api.snapshots.lock().unwrap().is_empty());
    assert_eq!(test.stop_lines(), 2);
    assert!(test.stopped(1));
    assert!(!test.hook.running(&test.rows[1].id));
}

#[cfg(test)]
impl<const DEVICES: bool, const SYNCS: bool> Test<DEVICES, SYNCS> {
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

/// With no guest sync, a shutdown takes no snapshot, and each VM gets the
/// graceful stop (caller duty 7, decision CH19 (3)).
#[test]
fn with_no_guest_sync_a_shutdown_stops_each_vm_gracefully() {
    let test = Test::<true, false>::build("shutdown-nosync", |_| {});
    assert_eq!(test.start(0), Ok(()));
    assert_eq!(test.start(1), Ok(()));
    assert_eq!(test.shut_down(), Ended::Clean);
    assert!(!test.api.entered.load(Ordering::SeqCst));
    assert_eq!(test.stop_lines(), 2);
    assert!(test.stopped(0) && test.stopped(1));
}

/// The hook behind an `Arc`, for the acp handler, which owns its hook.
#[derive(Debug)]
struct Shared<const DEVICES: bool, const SYNCS: bool>(Arc<ColdBoot<Fake, FakeApi<DEVICES, SYNCS>>>);

impl<const DEVICES: bool, const SYNCS: bool> StartVm for Shared<DEVICES, SYNCS> {
    async fn start(&self, row: &SessionRow) -> Result<AcpLink, StartFault> {
        self.0.start(row).await
    }
}

impl<const DEVICES: bool, const SYNCS: bool> StopVm for Shared<DEVICES, SYNCS> {
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

/// A new `ControlReady` clears `turn_seen`. A VM that saw a turn event,
/// slept, and booted again `starting` is not up, so it does not sleep
/// (decision CH43 rule 8).
#[test]
fn a_new_ready_clears_the_turn_seen() {
    let test = Test::new("reseen", |_| {});
    assert_eq!(test.start(0), Ok(()));
    test.turn(0, r#"{"start":{}}"#);
    test.turn(0, r#"{"end":{"title":"First","updated_at":2000}}"#);
    assert_eq!(test.sleep_after(PAST_IDLE).len(), 1);
    *test.fake.guest.lock().unwrap() = Guest::Starting;
    assert_eq!(test.start(0), Ok(()));
    assert!(test.sleep_after(PAST_IDLE).is_empty());
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
impl<const DEVICES: bool, const SYNCS: bool> Test<DEVICES, SYNCS> {
    /// Writes `line` on the guest end of the newest control link, as guest
    /// init relays a turn event (decision CH21).
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

/// The reader of the control link applies each turn event (plan PR
/// L18b). A VM whose ready said `starting` is up after its first event
/// (decision CH43 rule 8). A start keeps it awake. An end clears the turn,
/// writes the row (decision CH32 rule 4) and counts as a use (decision
/// CH50 rule 5).
#[test]
fn the_reader_applies_each_turn_event() {
    let test = Test::new("reader", |_| {});
    *test.fake.guest.lock().unwrap() = Guest::Starting;
    assert_eq!(test.start(0), Ok(()));
    let id = test.rows[0].id.clone();
    test.send(r#"{"start":{}}"#);
    test.settle(|test| test.control.lock().unwrap().turn_running(&id));
    assert!(test.control.lock().unwrap().turn_seen(&id));
    assert!(test.sleep_after(PAST_IDLE).is_empty());

    std::thread::sleep(Duration::from_millis(5));
    let before = Instant::now();
    test.send(r#"{"end":{"title":"First","updated_at":1793577700000}}"#);
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
