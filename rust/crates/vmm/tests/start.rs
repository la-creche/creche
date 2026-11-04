//! The cold boot hook over fakes (plan PR L16). A fake launcher records
//! each launch, writes a fake command line file and binds a fake vsock
//! socket that plays CH and guest init. Every other step is the real one
//! on a scratch dir in `/tmp`: the shape read, admission, the store
//! check, the session disk (with `/usr/bin/true` as `mke2fs`), the deny
//! check and the fault file.

use std::io::{BufRead, BufReader, Write};
use std::os::unix::fs::PermissionsExt;
use std::os::unix::net::{UnixListener, UnixStream};
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::thread::spawn;
use std::time::SystemTime;
use std::{fs, io};

use creche_vmm::control::ControlState;
use creche_vmm::handlers::acp::{StartFault, StartVm};
use creche_vmm::start::{ColdBoot, Digests, Launcher, StartConfig};
use creche_vmm::stop::VmProcess;
use creche_vmm_confine::admission::{GIB, Limits};
use creche_vmm_confine::family::FamilyRoot;
use creche_vmm_confine::faults::{FaultBook, FaultDir};
use creche_vmm_confine::launch::{Launch, Program};
use creche_vmm_confine::spawner::SpawnError;
use creche_vmm_store::images::{ImageDigest, ImageStore};
use creche_vmm_store::layout::{RunRoot, StateRoot};
use creche_vmm_store::rows::Rows;
use creche_vmm_store::session_disk::SessionDisks;
use creche_wire::{FamilyName, SessionRow};
use tokio::runtime::{Builder, Runtime};

const A: &str = "3f2b8c1e-9d4a-4e6b-8a7c-0b1d2e3f4a5b";
const B: &str = "1a2b3c4d-5e6f-4a1b-8c2d-3e4f5a6b7c8d";
const CH: &str = "/opt/ch/cloud-hypervisor";
const SHAPE: &str = r#"{"cpus":1,"flavor":"base","memory_mib":512}"#;
const READY: &str =
    r#"{"facade":{"running":{"last_turn_end":null,"pid":42,"starts":1,"turn_running":false}}}"#;
/// The SHA-256 of no bytes: the fake kernel and root disk are empty.
const EMPTY: &str = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855";

/// How the fake guest answers.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Guest {
    /// `ControlReady`, then `OK` on 1201.
    Ready,
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
                stream.write_all(format!("{READY}\n").as_bytes()).unwrap();
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
    hook: ColdBoot<Fake>,
    fake: Arc<Fake>,
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
        let rows: Vec<SessionRow> = [A, B]
            .iter()
            .map(|id| {
                let create = format!(
                    r#"{{"created_at":1793577600000,"cwd":"/home/david","family":"chat","id":"{id}","origin":{{"door":{{}}}}}}"#
                );
                store.create(serde_json::from_str(&create).unwrap()).unwrap()
            })
            .collect();
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
        let hook = ColdBoot::new(
            config,
            Arc::clone(&fake),
            Arc::new(Mutex::new(store)),
            Arc::clone(&control),
            Arc::new(Mutex::new(book)),
        );
        let runtime = Builder::new_current_thread().enable_all().build().unwrap();
        Self {
            dir,
            hook,
            fake,
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
/// (decisions CH8-6, CH21-3). The cap holds one VM of 512 MiB.
#[test]
fn a_vm_past_the_cap_is_host_full() {
    let test = Test::new("full", |config| config.limits.ram_cap = 512 << 20);
    assert_eq!(test.start(0), Ok(()));
    assert_eq!(test.start(1), Err(StartFault::HostFull));
    assert_eq!(test.launches(), 1);
    assert!(test.fault_codes().is_empty());
    assert!(!test.hook.running(&test.rows[1].id));
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
