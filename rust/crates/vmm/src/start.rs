//! The `StartVm` hook of the vmm: the cold boot of a session with no
//! usable snapshot (plan PR L16, decisions CH1, CH8-2). Each step keeps
//! its own bound, and the start has none (decision CH50 rule 1). The steps:
//!
//! 1. The family VM shape (PR C11) and the free bytes of the state root,
//!    in `spawn_blocking` (decision CH50 rule 9). Admission needs the
//!    guest RAM of the shape, so this read comes first.
//! 2. Admission (PR C9, decisions CH8-6, CH8-7). The RAM of the start
//!    counts from here on, so two starts never pass the cap together.
//! 3. In `spawn_blocking`: the stale sockets go (PR D1, decision CH19
//!    (4)), the kernel and the root disk pass the store check (PR D6,
//!    decision CH44 rule 2), the session disk exists (PR D7, decision
//!    CH21-4), the run dir `vm/<id>/` and `snapshots/` exist, and
//!    `serial.log` is a new file with mode 0600 (gap G23). Then the launch
//!    step starts CH with the argv of PR C7 and the sizes of PR C4 under
//!    the ruleset of PR C6 (PR C2, decisions CH8-3, CH8-5, CH8-8, CH30).
//! 4. The first `ControlReady` (plan PR L10). It clears the faults of the
//!    family (contract 05 §3.3.1 rule 8).
//! 5. The deny check (PR C8, decision CH6-3).
//! 6. The dial of guest port 1201 (decision CH50 rule 2).
//!
//! Each fault but `host_full` goes to the fault file of the family
//! (decisions CH21-1, CH6-3). A fault after the launch stops the VM.

use std::collections::HashMap;
use std::fs::{DirBuilder, OpenOptions, Permissions};
use std::io;
use std::os::unix::fs::{DirBuilderExt, OpenOptionsExt, PermissionsExt};
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex, MutexGuard, PoisonError};
use std::time::{Instant, SystemTime};

use creche_vmm_confine::admission::{Admission, Gate, Limits, Running, decide, free_bytes};
use creche_vmm_confine::command::{Boot, ChCommand, DiskLimit};
use creche_vmm_confine::deny::{check, cmdline_of};
use creche_vmm_confine::family::FamilyRoot;
use creche_vmm_confine::faults::{FaultBook, FaultCode};
use creche_vmm_confine::landlock::{HostPath, Start, VmFiles};
use creche_vmm_confine::launch::{Launch, Program};
use creche_vmm_confine::limits::VmLimits;
use creche_vmm_confine::spawner::{Launched, SpawnError, Spawner};
use creche_vmm_store::images::{ImageDigest, ImageStore};
use creche_vmm_store::layout::{RunRoot, StateRoot, VmRunDir};
use creche_vmm_store::rows::Rows;
use creche_vmm_store::session_disk::SessionDisks;
use creche_vmm_store::stale::remove_stale;
use creche_wire::{FamilyName, FamilyShape, SandboxFlavor, SessionId, SessionRow};
use tokio::task::spawn_blocking;

use crate::control::{Change, ControlLink, ControlState, LinkId, open, wall_time};
use crate::handlers::acp::{StartFault, StartVm};
use crate::relay::AcpLink;
use crate::stop::{VmProcess, stop};

/// The mode of the run dir and the snapshots dir of a VM.
const DIR_MODE: u32 = 0o700;

/// The mode of `serial.log` (gap G23).
const SERIAL_MODE: u32 = 0o600;

/// The store digests of the guest kernel and of the root disk of each
/// flavor (decisions CH7-3, CH8-8). Plan PR L20 reads them from the vmm
/// config.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Digests {
    /// The guest kernel. Every flavor boots it.
    pub kernel: ImageDigest,
    /// The root disk of the flavor `base`.
    pub base: ImageDigest,
    /// The root disk of the flavor `python`.
    pub python: ImageDigest,
}

impl Digests {
    /// The root disk of `flavor`.
    fn root(&self, flavor: SandboxFlavor) -> ImageDigest {
        match flavor {
            SandboxFlavor::Base => self.base,
            SandboxFlavor::Python => self.python,
        }
    }
}

/// What a cold boot reads. Plan PR L20 builds it from the vmm config.
#[derive(Debug)]
pub struct StartConfig {
    /// The rows, session disks and snapshots.
    pub state: StateRoot,
    /// The run dir of each VM.
    pub run: RunRoot,
    /// caregiver's families dir, for the VM shape.
    pub families: FamilyRoot,
    /// The digest store.
    pub images: ImageStore,
    /// The session disks.
    pub disks: SessionDisks,
    /// The kernel and the root disks.
    pub digests: Digests,
    /// The initramfs, optional until phase 3 adds guest init (PR C7).
    pub initramfs: Option<HostPath>,
    /// The Cloud Hypervisor program.
    pub ch: Program,
    /// The `systemd-run` program of the launch (PR C2).
    pub systemd_run: Program,
    /// The RAM cap and the free-space floor of admission.
    pub limits: Limits,
}

/// The start of a launch step. `Spawner` is the one of the vmm, and a
/// test gives a fake.
pub trait Launcher: Send + Sync + 'static {
    /// The handle of one launched VM.
    type Process: VmProcess + Send + 'static;

    /// Starts `launch`. The call blocks until the step is forked.
    ///
    /// # Errors
    ///
    /// The `SpawnError` of the spawner.
    fn launch(&self, launch: Launch) -> Result<Self::Process, SpawnError>;

    /// The command line file of `process`, for the deny check.
    fn cmdline(&self, process: &Self::Process) -> PathBuf;
}

impl Launcher for Spawner {
    type Process = Launched;

    fn launch(&self, launch: Launch) -> Result<Launched, SpawnError> {
        Self::launch(self, launch)
    }

    fn cmdline(&self, process: &Launched) -> PathBuf {
        cmdline_of(process.id())
    }
}

/// One VM that the vmm started.
#[derive(Debug)]
struct Vm<P> {
    /// The launched process.
    process: P,
    /// The run dir of the VM.
    run: VmRunDir,
    /// The configured guest RAM in bytes, for admission.
    guest_ram: u64,
    /// The control link after `ControlReady`. `None` after a failed stop.
    #[expect(
        dead_code,
        reason = "the StopVm hook of the vmm sends the graceful stop on it"
    )]
    link: Option<ControlLink>,
    /// The last use, for the eviction order (decision CH50 rule 5).
    last_use: Instant,
}

/// What the table holds for a session.
#[derive(Debug)]
enum Slot<P> {
    /// A start passed admission and holds this much guest RAM.
    Starting(u64),
    /// The VM runs, or its stop failed (decision CH50 rule 4).
    Running(Vm<P>),
}

/// The VMs of the vmm, one per session (decision CH1).
type Table<P> = Arc<Mutex<HashMap<SessionId, Slot<P>>>>;

/// The lock of `table`. Each change is one insert or one remove, so a
/// poisoned lock still holds a whole table.
fn lock<P>(table: &Table<P>) -> MutexGuard<'_, HashMap<SessionId, Slot<P>>> {
    table.lock().unwrap_or_else(PoisonError::into_inner)
}

/// Removes each VM whose process exited from `table`. Decision CH50 rule
/// 4 keeps the RAM of a VM that may still run, and an exited one runs no
/// more. A failed status check keeps the entry, so its RAM still counts.
fn reap<P: VmProcess>(table: &mut HashMap<SessionId, Slot<P>>) {
    table.retain(|_, slot| match slot {
        Slot::Running(vm) => !matches!(vm.process.exited(), Ok(true)),
        Slot::Starting(_) => true,
    });
}

/// A start between admission and its VM entry. Its drop removes the
/// reserved RAM. A process that it still holds gets SIGKILL and stays in
/// the table, so admission counts its RAM and a later start reaps it.
struct Pending<P: VmProcess> {
    table: Table<P>,
    id: SessionId,
    guest_ram: u64,
    run: VmRunDir,
    process: Option<P>,
    done: bool,
}

impl<P: VmProcess> Pending<P> {
    /// Makes the VM an entry of the table with its control link.
    fn commit(mut self, link: ControlLink) {
        if let Some(process) = self.process.take() {
            let vm = Vm {
                process,
                run: self.run.clone(),
                guest_ram: self.guest_ram,
                link: Some(link),
                last_use: Instant::now(),
            };
            lock(&self.table).insert(self.id.clone(), Slot::Running(vm));
            self.done = true;
        }
    }
}

impl<P: VmProcess> Drop for Pending<P> {
    fn drop(&mut self) {
        if self.done {
            return;
        }
        let mut table = lock(&self.table);
        match self.process.take() {
            Some(mut process) => {
                // The start was dropped, or its stop failed. A failed
                // SIGKILL leaves a process that the RAM count still holds.
                let _ = process.kill();
                let vm = Vm {
                    process,
                    run: self.run.clone(),
                    guest_ram: self.guest_ram,
                    link: None,
                    last_use: Instant::now(),
                };
                table.insert(self.id.clone(), Slot::Running(vm));
            }
            None => {
                table.remove(&self.id);
            }
        }
    }
}

/// The `StartVm` hook of the vmm.
#[derive(Debug)]
pub struct ColdBoot<L: Launcher> {
    config: Arc<StartConfig>,
    launcher: Arc<L>,
    table: Table<L::Process>,
    rows: Arc<Mutex<Rows>>,
    control: Arc<Mutex<ControlState>>,
    faults: Arc<Mutex<FaultBook>>,
}

impl<L: Launcher> ColdBoot<L> {
    /// The hook over `config` and `launcher`. It shares the rows, the
    /// control state and the fault book with the rest of the vmm.
    pub fn new(
        config: StartConfig,
        launcher: Arc<L>,
        rows: Arc<Mutex<Rows>>,
        control: Arc<Mutex<ControlState>>,
        faults: Arc<Mutex<FaultBook>>,
    ) -> Self {
        Self {
            config: Arc::new(config),
            launcher,
            table: Arc::default(),
            rows,
            control,
            faults,
        }
    }

    /// Whether the table holds a VM of `id`.
    #[must_use]
    pub fn running(&self, id: &SessionId) -> bool {
        matches!(lock(&self.table).get(id), Some(Slot::Running(_)))
    }

    /// The control state of every VM.
    fn control(&self) -> MutexGuard<'_, ControlState> {
        self.control.lock().unwrap_or_else(PoisonError::into_inner)
    }

    /// The vsock socket of a VM of `id` that still runs. A VM that exited
    /// leaves the table, so the start boots it again.
    fn find(&self, id: &SessionId) -> Result<Option<PathBuf>, String> {
        let mut table = lock(&self.table);
        let vm = match table.get_mut(id) {
            None => return Ok(None),
            Some(Slot::Starting(_)) => return Err("a start of the VM runs".to_owned()),
            Some(Slot::Running(vm)) => vm,
        };
        match vm.process.exited() {
            Ok(false) => Ok(Some(vm.run.vsock_socket())),
            Ok(true) => {
                table.remove(id);
                Ok(None)
            }
            Err(error) => Err(format!("checking the VM process: {error}")),
        }
    }

    /// Reserves `guest_ram` for `id` when admission admits it.
    fn admit(&self, id: &SessionId, guest_ram: u64, free: u64) -> Option<Pending<L::Process>> {
        let mut table = lock(&self.table);
        // A start that was dropped, a failed stop or a guest crash leaves
        // an entry whose process may have exited since.
        reap(&mut table);
        let now = Instant::now();
        // Plan PR L17 adds the sleep gate and runs the evictions. Until
        // then every VM counts as busy, so admission never names one.
        let running: Vec<Running> = table
            .iter()
            .map(|(session, slot)| {
                let (guest_ram, last_use) = match slot {
                    Slot::Starting(ram) => (*ram, now),
                    Slot::Running(vm) => (vm.guest_ram, vm.last_use),
                };
                let session = session.clone();
                let gate = Gate::Busy;
                Running {
                    session,
                    guest_ram,
                    last_use,
                    gate,
                }
            })
            .collect();
        match decide(guest_ram, &running, free, self.config.limits) {
            Admission::Admit => {}
            Admission::EvictFirst(_) | Admission::HostFull(_) => return None,
        }
        table.insert(id.clone(), Slot::Starting(guest_ram));
        Some(Pending {
            table: Arc::clone(&self.table),
            id: id.clone(),
            guest_ram,
            run: self.config.run.vm(id),
            process: None,
            done: false,
        })
    }

    /// Writes `code` with `message` to the fault file of `family` and
    /// returns the fault (decision CH21-1).
    async fn fault(&self, family: &FamilyName, code: FaultCode, message: String) -> StartFault {
        eprintln!("vmm: a VM of {family} did not start: {message}");
        self.book(family, move |book, family, now| {
            book.raise(family, code, message, now)
        })
        .await;
        match code {
            FaultCode::SandboxStartFailed => StartFault::SandboxStartFailed,
            FaultCode::EgressAssertFailed => StartFault::EgressAssertFailed,
        }
    }

    /// Runs `change` on the fault book for `family` in `spawn_blocking`.
    /// A failed write is logged: the book keeps the faults, and its next
    /// refresh writes them again.
    async fn book(
        &self,
        family: &FamilyName,
        change: impl FnOnce(&mut FaultBook, &FamilyName, SystemTime) -> io::Result<()> + Send + 'static,
    ) {
        let (faults, name) = (Arc::clone(&self.faults), family.clone());
        let written = spawn_blocking(move || {
            let mut book = faults.lock().unwrap_or_else(PoisonError::into_inner);
            change(&mut book, &name, SystemTime::now())
        })
        .await;
        if !matches!(written, Ok(Ok(()))) {
            eprintln!("vmm: the fault file of {family} was not written");
        }
    }

    /// Writes the fault, then stops the VM of `pending`. After
    /// `ControlReady` the start holds the control link `control`, and the
    /// stop sends the graceful stop on it first (decision CH19 (3): a kill
    /// is the fallback). Before `ControlReady` no link exists, so the stop
    /// skips that wait (decision CH50 rule 3). The link `link` stops
    /// changing the control state.
    async fn abort(
        &self,
        mut pending: Pending<L::Process>,
        link: LinkId,
        control: Option<&ControlLink>,
        family: &FamilyName,
        code: FaultCode,
        message: String,
    ) -> StartFault {
        self.control().closed(&pending.id, link);
        let fault = self.fault(family, code, message).await;
        if let Some(process) = pending.process.as_mut() {
            match stop(control, process, &pending.run).await {
                Ok(_) => pending.process = None,
                // Decision CH50 rule 4: the drop of `pending` keeps the VM
                // in the table, so admission counts its RAM.
                Err(error) => eprintln!("vmm: a faulted VM did not stop: {error}"),
            }
        }
        fault
    }

    /// Dials guest port 1201 of a VM that runs (decision CH50 rule 2).
    async fn dial(&self, family: &FamilyName, vsock: &Path) -> Result<AcpLink, StartFault> {
        match AcpLink::dial(vsock).await {
            Ok(link) => Ok(link),
            Err(error) => Err(self
                .fault(family, FaultCode::SandboxStartFailed, dial_text(error))
                .await),
        }
    }

    /// Writes the turn end of `change` to the row of `id` (decision CH32
    /// rule 4). A failed write is logged: the next `ControlReady` carries
    /// the end again.
    async fn turn_end(&self, id: &SessionId, change: Change) {
        let Change::Applied(Some(end)) = change else {
            return;
        };
        let (rows, id) = (Arc::clone(&self.rows), id.clone());
        let written = spawn_blocking(move || {
            let mut rows = rows.lock().map_err(|_| ())?;
            rows.turn_end(&id, &end).map(|_| ()).map_err(|_| ())
        })
        .await;
        if !matches!(written, Ok(Ok(()))) {
            eprintln!("vmm: the turn end of the first ready was not written");
        }
    }
}

/// The fault text of a failed dial of guest port 1201.
fn dial_text(error: crate::control::ControlError) -> String {
    format!("dialing guest port 1201: {error:?}")
}

/// Runs `step` in `spawn_blocking` (decision CH36 G21).
async fn blocking<T: Send + 'static>(
    step: impl FnOnce() -> Result<T, String> + Send + 'static,
) -> Result<T, String> {
    spawn_blocking(step)
        .await
        .unwrap_or_else(|_| Err("a blocking step panicked".to_owned()))
}

/// The family VM shape and the free bytes of the state root.
fn read_shape(config: &StartConfig, family: &FamilyName) -> Result<(FamilyShape, u64), String> {
    let shape = config
        .families
        .read_shape(family)
        .map_err(|error| format!("reading the VM shape: {error:?}"))?;
    let free = free_bytes(config.state.path())
        .map_err(|error| format!("reading the free bytes: {error}"))?;
    Ok((shape, free))
}

/// A host path of the absolute `path`.
fn host(path: &Path) -> Result<HostPath, String> {
    HostPath::new(path.to_path_buf()).map_err(|error| format!("{}: {error}", path.display()))
}

/// Makes `dir` and its parents with mode 0700.
fn make_dir(dir: &Path) -> Result<(), String> {
    DirBuilder::new()
        .recursive(true)
        .mode(DIR_MODE)
        .create(dir)
        .map_err(|error| format!("making {}: {error}", dir.display()))
}

/// Step 3: the files of the boot, then the launch. Returns the process
/// and the start that the ruleset and the deny check name.
fn launch<L: Launcher>(
    config: &StartConfig,
    launcher: &L,
    id: &SessionId,
    shape: FamilyShape,
) -> Result<(L::Process, Start), String> {
    let run = config.run.vm(id);
    remove_stale(&run).map_err(|error| format!("{error}: {}", error.source))?;
    let checked = |digest| {
        config
            .images
            .check(digest)
            .map_err(|error| error.to_string())
    };
    let kernel = checked(config.digests.kernel)?;
    let root = checked(config.digests.root(shape.flavor))?;
    let disk = config
        .disks
        .for_boot(id)
        .map_err(|error| error.to_string())?;
    let session = config.state.session(id);
    // The ruleset opens each path, and a missing one fails the launch.
    make_dir(run.path())?;
    make_dir(&session.snapshots())?;
    let serial = run.serial_log();
    OpenOptions::new()
        .write(true)
        .create(true)
        .truncate(true)
        .mode(SERIAL_MODE)
        .open(&serial)
        .and_then(|file| file.set_permissions(Permissions::from_mode(SERIAL_MODE)))
        .map_err(|error| format!("making {}: {error}", serial.display()))?;
    let limits = VmLimits::from(shape);
    let boot = Boot {
        kernel: &kernel,
        initramfs: config.initramfs.as_ref(),
        root: &root,
        session: &disk,
        disk: DiskLimit::SESSION,
        limits,
    };
    let command = ChCommand::boot(boot, &run).map_err(|error| error.to_string())?;
    let start = Start::Boot {
        kernel: host(kernel.path())?,
        initramfs: config.initramfs.clone(),
    };
    let files = VmFiles::new(start.clone(), host(root.path())?, &session, &run)
        .map_err(|error| error.to_string())?;
    let payload = command.payload(config.ch.clone());
    let scope = limits.scope(id.clone());
    let plan = Launch::new(config.systemd_run.clone(), scope, payload, files);
    let process = launcher.launch(plan).map_err(|error| error.to_string())?;
    Ok((process, start))
}

impl<L: Launcher> StartVm for ColdBoot<L> {
    async fn start(&self, row: &SessionRow) -> Result<AcpLink, StartFault> {
        let (id, family) = (&row.id, &row.family);
        let sandbox = FaultCode::SandboxStartFailed;
        match self.find(id) {
            Ok(Some(vsock)) => return self.dial(family, &vsock).await,
            Ok(None) => {}
            Err(message) => return Err(self.fault(family, sandbox, message).await),
        }
        let (config, name) = (Arc::clone(&self.config), family.clone());
        let (shape, free) = match blocking(move || read_shape(&config, &name)).await {
            Ok(read) => read,
            Err(message) => return Err(self.fault(family, sandbox, message).await),
        };
        let guest_ram = VmLimits::from(shape).guest_ram();
        // `host_full` is no fault of decision CH6-3, so it writes none.
        let mut pending = self
            .admit(id, guest_ram, free)
            .ok_or(StartFault::HostFull)?;

        let (config, launcher, session) = (
            Arc::clone(&self.config),
            Arc::clone(&self.launcher),
            id.clone(),
        );
        // The process goes into `pending` inside the closure. hyper drops
        // this future when the client hangs up before 101, and the blocking
        // step then ends with no one to take its output. The drop of that
        // output kills the VM and keeps it in the table, so admission
        // counts its RAM and no CH runs outside the table (decisions CH8-6,
        // CH50 rule 4).
        let launched = blocking(move || {
            let (process, start) = launch(&config, &*launcher, &session, shape)?;
            let cmdline = launcher.cmdline(&process);
            pending.process = Some(process);
            Ok((pending, start, cmdline))
        })
        .await;
        let (pending, start, cmdline) = match launched {
            Ok(launched) => launched,
            Err(message) => return Err(self.fault(family, sandbox, message).await),
        };

        let vsock = pending.run.vsock_socket();
        let link = self.control().replace(id);
        let (control, ready) = match open(&vsock, id, wall_time).await {
            Ok(opened) => opened,
            Err(error) => {
                let message = format!("waiting for ControlReady: {error:?}");
                return Err(self
                    .abort(pending, link, None, family, sandbox, message)
                    .await);
            }
        };
        let change = self.control().ready(id, link, ready);
        self.turn_end(id, change).await;
        self.book(family, FaultBook::clear).await;

        // Decision CH54 rule 2: the deny check runs after `ControlReady`
        // and before the dial of 1201 and the 101. Before CH execs, the
        // command line is the one of the launch step, and the check fails
        // closed (`deny::check`). The kernel fixes the command line at
        // exec, so a check after `ControlReady` proves the same thing.
        // Decision CH49 rule 3 binds 1104 from phase 7 on, so the bound
        // set is empty in phase 2.
        let (ch, run) = (self.config.ch.clone(), pending.run.clone());
        let denied = blocking(move || {
            check(&cmdline, &ch, &start, &run, &[]).map_err(|error| error.to_string())
        })
        .await;
        if let Err(message) = denied {
            let egress = FaultCode::EgressAssertFailed;
            let control = Some(&control);
            return Err(self
                .abort(pending, link, control, family, egress, message)
                .await);
        }
        let acp = match AcpLink::dial(&vsock).await {
            Ok(acp) => acp,
            Err(error) => {
                let message = dial_text(error);
                let control = Some(&control);
                return Err(self
                    .abort(pending, link, control, family, sandbox, message)
                    .await);
            }
        };
        pending.commit(control);
        Ok(acp)
    }
}
