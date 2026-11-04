//! The start hook over fakes (plan PRs L16 and L17), and the idle sleep
//! and the sweep over the same fakes (plan PR L18). A fake launcher
//! records each launch, writes a fake command line file and binds a fake
//! vsock socket that plays CH and guest init. A fake CH API answers
//! `info` and `resume` of a restore, and its snapshot ends the fake VM. Every other step is the real one on
//! a scratch dir in `/tmp`: the shape read, admission, the store check,
//! the session disk (with `/usr/bin/true` as `mke2fs`), the key check,
//! the claim, the deny check and the fault file.

use std::io::{BufRead, BufReader, Write};
use std::os::unix::fs::PermissionsExt;
use std::os::unix::net::{UnixListener, UnixStream};
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::thread::spawn;
use std::time::{Duration, Instant, SystemTime};
use std::{fs, io};

use creche_vmm::control::ControlState;
use creche_vmm::handlers::acp::{StartFault, StartVm};
use creche_vmm::relay::Relays;
use creche_vmm::restore::ChApi;
use creche_vmm::start::{ColdBoot, Digests, Launcher, StartConfig};
use creche_vmm::stop::VmProcess;
use creche_vmm_confine::admission::{GIB, Limits};
use creche_vmm_confine::family::FamilyRoot;
use creche_vmm_confine::faults::{FaultBook, FaultDir};
use creche_vmm_confine::launch::{Launch, Program};
use creche_vmm_confine::spawner::SpawnError;
use creche_vmm_store::images::{ImageDigest, ImageStore};
use creche_vmm_store::key::{CheckedCh, SnapshotKey, check_cloud_hypervisor, write_key};
use creche_vmm_store::layout::{RunRoot, SessionDir, SnapshotGeneration, StateRoot, VmRunDir};
use creche_vmm_store::retention::Reason;
use creche_vmm_store::rows::Rows;
use creche_vmm_store::session_disk::SessionDisks;
use creche_vmm_store::sleep_gate::IDLE_TIMEOUT;
use creche_wire::{FamilyName, SessionId, SessionRow};
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
    /// A close after the hello, before `ControlReady`.
    Closes,
    /// `ControlReady`, then a refused `CONNECT 1201`.
    RefusesAcp,
}

/// A launched fake VM. A signal ends it at once.
#[derive(Debug)]
struct FakeVm {
    number: usize,
    exited: Arc<AtomicBool>,
}

impl VmProcess for FakeVm {
    fn exited(&mut self) -> io::Result<bool> {
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
    launches: Mutex<Vec<Launch>>,
    vms: Mutex<Vec<Arc<AtomicBool>>>,
}

// `cfg(test)` lets the helpers unwrap (rust/clippy.toml).
#[cfg(test)]
impl Launcher for Fake {
    type Process = FakeVm;

    fn launch(&self, launch: Launch) -> Result<FakeVm, SpawnError> {
        if self.fail.load(Ordering::SeqCst) {
            return Err(SpawnError::Stopped);
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
        spawn(move || play(&listener, guest));
        let mut launches = self.launches.lock().unwrap();
        let number = launches.len();
        fs::write(self.dir.join(format!("cmdline-{number}")), args.join("\0")).unwrap();
        launches.push(launch);
        let exited = Arc::new(AtomicBool::new(false));
        self.vms.lock().unwrap().push(Arc::clone(&exited));
        FakeVm { number, exited }
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
}

#[cfg(test)]
impl ChApi for FakeApi {
    const READS_DEVICES: bool = true;

    fn info(&self, _vm: &VmRunDir) -> Result<Vec<u8>, String> {
        self.infos.fetch_add(1, Ordering::SeqCst);
        let fails = self.fails.load(Ordering::SeqCst);
        if fails > 0 {
            self.fails.store(fails - 1, Ordering::SeqCst);
            return Err("no api.sock yet".to_owned());
        }
        let reply = self.reply.lock().map_err(|_| "poisoned".to_owned())?;
        Ok(reply.clone().into_bytes())
    }

    fn resume(&self, _vm: &VmRunDir) -> Result<(), String> {
        self.resumes.fetch_add(1, Ordering::SeqCst);
        Ok(())
    }

    /// Ends the newest fake VM of `vm`, as `shutdown-vmm` ends CH.
    fn snapshot(
        &self,
        vm: &VmRunDir,
        _session: &SessionDir,
        _key: &SnapshotKey,
    ) -> Result<SnapshotGeneration, String> {
        let launches = self.fake.launches.lock().map_err(|_| "poisoned")?;
        let index = launches
            .iter()
            .rposition(|launch| launch.files().run_dir().path() == vm.path())
            .ok_or("no such VM")?;
        let vms = self.fake.vms.lock().map_err(|_| "poisoned")?;
        vms.get(index)
            .ok_or("no such VM")?
            .store(true, Ordering::SeqCst);
        let mut snapshots = self.snapshots.lock().map_err(|_| "poisoned")?;
        snapshots.push(vm.path().to_owned());
        Ok(SnapshotGeneration(1))
    }
}

/// Plays CH and guest init on each link: `CONNECT`, then the control
/// link or the ACP link. Each link stays open until the test ends.
#[cfg(test)]
fn play(listener: &UnixListener, guest: Guest) {
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
                    _ => READY,
                };
                stream.write_all(format!("{ready}\n").as_bytes()).unwrap();
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
    hook: ColdBoot<Fake, FakeApi>,
    fake: Arc<Fake>,
    api: Arc<FakeApi>,
    relays: Relays,
    ch: CheckedCh,
    state: StateRoot,
    control: Arc<Mutex<ControlState>>,
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
            digests: Digests {
                kernel: empty,
                base: empty,
                python: empty,
            },
            initramfs: None,
            ch: Program::new(PathBuf::from(CH)).unwrap(),
            systemd_run: Program::new(PathBuf::from("/usr/bin/systemd-run")).unwrap(),
            limits: Limits {
                ram_cap: 80 * GIB,
                free_floor: 0,
            },
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
            launches: Mutex::default(),
            vms: Mutex::default(),
        });
        let control = Arc::new(Mutex::new(ControlState::default()));
        let api = Arc::new(FakeApi {
            fake: Arc::clone(&fake),
            snapshots: Mutex::default(),
            reply: Mutex::default(),
            fails: AtomicUsize::new(0),
            infos: AtomicUsize::new(0),
            resumes: AtomicUsize::new(0),
        });
        let relays = Relays::default();
        let hook = ColdBoot::new(
            config,
            Arc::clone(&fake),
            Arc::clone(&api),
            relays.clone(),
            Arc::new(Mutex::new(store)),
            Arc::clone(&control),
            Arc::new(Mutex::new(book)),
        );
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

    /// Sets the `info` reply of a restore of row `index`: `state`, the
    /// two disks and the vsock socket of the VM, then `extra` fields.
    fn reply(&self, index: usize, state: &str, extra: &[(&str, serde_json::Value)]) {
        let id = &self.rows[index].id;
        let root = self.dir.join(format!("state/store/{EMPTY}"));
        let disk = self.dir.join(format!("state/sessions/{id}/disk.ext4"));
        let vsock = self.dir.join(format!("run/vm/{id}/vsock.sock"));
        let mut config = serde_json::json!({
            "cpus": {"boot_vcpus": 1},
            "disks": [{"path": root}, {"path": disk}],
            "net": null,
            "vsock": {"cid": 3, "socket": vsock},
        });
        for (field, value) in extra {
            config[*field] = value.clone();
        }
        let reply = serde_json::json!({"state": state, "config": config});
        *self.api.reply.lock().unwrap() = reply.to_string();
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
#[test]
fn a_guest_fault_stops_the_vm() {
    for (name, guest) in [("closes", Guest::Closes), ("acp", Guest::RefusesAcp)] {
        let test = Test::new(name, |_| {});
        *test.fake.guest.lock().unwrap() = guest;
        assert_eq!(test.start(0), Err(StartFault::SandboxStartFailed));
        assert_eq!(test.fault_codes(), ["sandbox_start_failed"]);
        assert!(test.stopped(0));
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
    assert!(!test.hook.running(&test.rows[0].id));
}

/// A snapshot with the current key restores paused: `info` is read until
/// CH answers, the device check passes, then `resume` (decisions CH2,
/// CH50 rule 10). The claim marks the snapshot consumed, and the
/// generation is in use while the VM runs (decisions CH19 (5), CH50 rule
/// 8).
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
}

/// Fields that a test adds to the config of an `info` reply.
type Extra<'a> = Vec<(&'a str, serde_json::Value)>;

/// A restored VM that is not paused, or that has a device past the fixed
/// set, never resumes. The fault is `egress_assert_failed`, the VM stops,
/// and its generation is no longer in use (decisions CH42 rule 2, CH50
/// rule 10).
#[test]
fn a_refused_device_set_stops_the_restore() {
    let other = serde_json::json!({"cid": 3, "socket": "/tmp/other.sock"});
    let extra_disk = serde_json::json!([{"path": "/a"}, {"path": "/b"}, {"path": "/c"}]);
    let cases: [(&str, &str, Extra); 6] = [
        ("running", "Running", vec![]),
        (
            "net",
            "Paused",
            vec![("net", serde_json::json!([{"tap": "t"}]))],
        ),
        (
            "user",
            "Paused",
            vec![("user_devices", serde_json::json!([{}]))],
        ),
        (
            "tpm",
            "Paused",
            vec![("tpm", serde_json::json!({"socket": "/t"}))],
        ),
        ("disks", "Paused", vec![("disks", extra_disk)]),
        ("vsock", "Paused", vec![("vsock", other)]),
    ];
    for (name, state, extra) in cases {
        let test = Test::new(&format!("refuse-{name}"), |_| {});
        test.snapshot(0, Some(&test.key()));
        test.reply(0, state, &extra);
        assert_eq!(test.start(0), Err(StartFault::EgressAssertFailed), "{name}");
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
