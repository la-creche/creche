//! The `StartVm` hook of the vmm: the cold boot of a session with no
//! usable snapshot (plan PR L16, decisions CH1, CH8-2), or the restore of
//! its snapshot (plan PR L17, decision CH2). Each step keeps its own
//! bound, and the start has none (decision CH50 rule 1). The steps:
//!
//! 1. The family VM shape (PR C11) and the free bytes of the state root,
//!    in `spawn_blocking` (decision CH50 rule 9). Admission needs the
//!    guest RAM of the shape, so this read comes first.
//! 2. Admission (PR C9, decisions CH8-6, CH8-7). The RAM of the start
//!    counts from here on, so two starts never pass the cap together.
//!    Each VM that admission evicts loses its door link, then its
//!    snapshot is taken and it stops (plan PR L17, decision CH19 (6)).
//! 3. In `spawn_blocking`: the stale sockets go (PR D1, decision CH19
//!    (4)), the kernel and the root disk pass the store check (PR D6,
//!    decision CH44 rule 2), the session disk exists (PR D7, decision
//!    CH21-4), the run dir `vm/<id>/` and `snapshots/` exist, and
//!    `serial.log` is a new file with mode 0600 (gap G23). The key check
//!    (PR D10) and the claim (PR D11) pick a restore or a cold boot. Then
//!    the launch step starts CH with the argv of PR C7 and the sizes of PR
//!    C4 under the ruleset of PR C6 (PR C2, decisions CH8-3, CH8-5, CH8-8,
//!    CH30).
//! 4. A restore only: `ch-remote info` must say `Paused`, the device
//!    check passes, then `ch-remote resume` (decision CH50 rule 10).
//! 5. The first `ControlReady` (plan PR L10). It clears the faults of the
//!    family (contract 05 §3.3.1 rule 8).
//! 6. The deny check (PR C8, decision CH6-3).
//! 7. The dial of guest port 1201 (decision CH50 rule 2).
//!
//! Each fault but `host_full` goes to the fault file of the family
//! (decisions CH21-1, CH6-3). A fault after the launch stops the VM.
//!
//! The idle sleep and the snapshot sweep of plan PR L18 use the same
//! table (`ColdBoot::sleep_idle`, `ColdBoot::sweep`, `sleep::run`).

use std::collections::{HashMap, HashSet};
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
use creche_vmm_store::claim::{ClaimError, ClaimedSnapshot, claim};
use creche_vmm_store::images::{ImageDigest, ImageStore};
use creche_vmm_store::key::{CheckedCh, KeyCheck, SnapshotKey, check_newest};
use creche_vmm_store::layout::{RunRoot, SessionDir, StateRoot, VmRunDir};
use creche_vmm_store::retention::{BUDGET, InUse, Swept, sweep};
use creche_vmm_store::rows::Rows;
use creche_vmm_store::session_disk::SessionDisks;
use creche_vmm_store::sleep_gate::Activity;
use creche_vmm_store::stale::remove_stale;
use creche_wire::{FamilyName, FamilyShape, SandboxFlavor, SessionId, SessionRow};
use tokio::sync::RwLock;
use tokio::task::spawn_blocking;
use tokio::time::sleep;

use crate::control::{
    Change, ControlLink, ControlState, DIAL_PAUSE, LinkId, READY_WAIT, open, wall_time,
};
use crate::handlers::acp::{StartFault, StartVm};
use crate::relay::{AcpLink, Door, Relays};
use crate::restore::{ChApi, Expected, check_devices};
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
    /// The version check of `ch`, for the snapshot key (decision CH7-6,
    /// gap G10). The vmm runs it at its start.
    pub ch_version: CheckedCh,
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
    link: Option<ControlLink>,
    /// The snapshot key of the VM. `None` after a failed stop.
    key: Option<SnapshotKey>,
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
    /// Admission evicts the VM, which holds this much guest RAM.
    Evicting(u64),
}

/// What the table holds for the session of a start.
enum Found {
    /// No VM: the start boots or restores one.
    Free,
    /// A VM runs with this vsock socket.
    Runs(PathBuf),
    /// Admission evicts the VM of the session.
    Evicting,
}

/// The VMs of the vmm, one per session (decision CH1).
type Table<P> = Arc<Mutex<HashMap<SessionId, Slot<P>>>>;

/// The lock of `table`. Each change is one insert or one remove, so a
/// poisoned lock still holds a whole table.
fn lock<P>(table: &Table<P>) -> MutexGuard<'_, HashMap<SessionId, Slot<P>>> {
    table.lock().unwrap_or_else(PoisonError::into_inner)
}

/// The lock of the claims. It guards no data, so a poisoned lock is
/// whole.
fn lock_claims(claims: &Mutex<()>) -> MutexGuard<'_, ()> {
    claims.lock().unwrap_or_else(PoisonError::into_inner)
}

/// The lock of `in_use`. Each change is one insert or one remove, and no
/// holder keeps it across file work.
fn lock_in_use(in_use: &Mutex<InUse>) -> MutexGuard<'_, InUse> {
    in_use.lock().unwrap_or_else(PoisonError::into_inner)
}

/// Removes the generation of `id` from `in_use`: its CH ended (decision
/// CH50 rule 8).
fn release(in_use: &Mutex<InUse>, id: &SessionId) {
    lock_in_use(in_use).retain(|(session, _)| session != id);
}

/// Removes each VM whose process exited from `table`, and its generation
/// from `in_use`. Decision CH50 rule 4 keeps the RAM of a VM that may
/// still run, and an exited one runs no more. A failed status check keeps
/// the entry, so its RAM still counts.
fn reap<P: VmProcess>(table: &mut HashMap<SessionId, Slot<P>>, in_use: &Mutex<InUse>) {
    table.retain(|id, slot| {
        let Slot::Running(vm) = slot else {
            return true;
        };
        let exited = matches!(vm.process.exited(), Ok(true));
        if exited {
            release(in_use, id);
        }
        !exited
    });
}

/// A start between admission and its VM entry. Its drop removes the
/// reserved RAM. A process that it still holds gets SIGKILL and stays in
/// the table, so admission counts its RAM and a later start reaps it.
struct Pending<P: VmProcess> {
    table: Table<P>,
    in_use: Arc<Mutex<InUse>>,
    id: SessionId,
    guest_ram: u64,
    run: VmRunDir,
    process: Option<P>,
    key: Option<SnapshotKey>,
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
                key: self.key.take(),
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
                    key: None,
                    last_use: Instant::now(),
                };
                table.insert(self.id.clone(), Slot::Running(vm));
            }
            None => {
                table.remove(&self.id);
                release(&self.in_use, &self.id);
            }
        }
    }
}

/// A VM that admission evicts, out of the table while its slot is
/// `Evicting`. A drop before the eviction ends sends SIGKILL and puts the
/// VM back with no link, so admission counts its RAM (decision CH50 rule
/// 4).
struct Eviction<P: VmProcess> {
    table: Table<P>,
    id: SessionId,
    vm: Option<Vm<P>>,
}

impl<P: VmProcess> Eviction<P> {
    /// Puts the VM back in the table as it is.
    fn put_back(mut self) {
        if let Some(vm) = self.vm.take() {
            lock(&self.table).insert(self.id.clone(), Slot::Running(vm));
        }
    }

    /// Removes the stopped VM from the table and from `in_use`.
    fn done(mut self, in_use: &Mutex<InUse>) {
        self.vm = None;
        lock(&self.table).remove(&self.id);
        release(in_use, &self.id);
    }
}

impl<P: VmProcess> Drop for Eviction<P> {
    fn drop(&mut self) {
        if let Some(mut vm) = self.vm.take() {
            let _ = vm.process.kill();
            vm.link = None;
            lock(&self.table).insert(self.id.clone(), Slot::Running(vm));
        }
    }
}

/// An admitted start and the VMs that it evicts first, in order.
type Admitted<P> = (Pending<P>, Vec<Eviction<P>>);

/// Runs `job` in a task of its own and waits for it. hyper drops the
/// start future when the client hangs up before 101. The task runs on,
/// so each take and each stop ends as it began, and no drop of an
/// `Eviction` kills a VM in the middle of its snapshot. A task that
/// panicked dropped its evictions, which put their VMs back (decision
/// CH50 rule 4).
async fn detached(job: impl Future<Output = bool> + Send + 'static) -> bool {
    tokio::spawn(job).await.unwrap_or(false)
}

/// The owned parts of a retire, so it runs in a task of its own.
#[derive(Debug, Clone)]
struct Retirer<A> {
    api: Arc<A>,
    /// Each take holds it to read, and the sweep holds it to write.
    takes: Arc<RwLock<()>>,
    in_use: Arc<Mutex<InUse>>,
    relays: Relays,
    control: Arc<Mutex<ControlState>>,
    state: StateRoot,
}

impl<A: ChApi> Retirer<A> {
    /// The control state of every VM.
    fn control(&self) -> MutexGuard<'_, ControlState> {
        self.control.lock().unwrap_or_else(PoisonError::into_inner)
    }

    /// Evicts each VM of `evictions` in the order of admission: closes
    /// its door link, then retires it (decisions CH8-6, CH19 (6)). After
    /// one that fails, the rest go back. True when each one stopped.
    async fn evict_all<P: VmProcess>(self, evictions: Vec<Eviction<P>>) -> bool {
        let mut fits = true;
        for eviction in evictions {
            if fits {
                self.relays.close(&eviction.id).await;
                fits = self.retire(eviction).await;
            } else {
                eviction.put_back();
            }
        }
        fits
    }

    /// Retires the idle VM of `eviction` (decisions CH2, CH19 (6)).
    async fn sleep<P: VmProcess>(self, eviction: Eviction<P>) -> bool {
        self.retire(eviction).await
    }

    /// Snapshots the VM of `eviction`, then stops it (decision CH19 (6)).
    /// The take ends CH, so the stop finds it gone. With no device read
    /// (`ChApi::READS_DEVICES`) it takes no snapshot and sends the graceful
    /// stop. False when the VM still holds its RAM: a turn runs, or the
    /// stop failed (decision CH50 rule 4).
    async fn retire<P: VmProcess>(&self, mut eviction: Eviction<P>) -> bool {
        let id = eviction.id.clone();
        let key = eviction.vm.as_ref().and_then(|vm| vm.key.clone());
        let turn = self.control().turn_running(&id);
        let (Some(key), false) = (key, turn) else {
            eviction.put_back();
            return false;
        };
        let Some(vm) = eviction.vm.as_mut() else {
            return false;
        };
        let (api, run, session) = (
            Arc::clone(&self.api),
            vm.run.clone(),
            self.state.session(&id),
        );
        // Decision CH44 rule 4: the caller decides after a failed take.
        // The VM stops anyway, so its RAM frees, and the session
        // cold-boots from its disk later (decision CH19 (5)).
        // An API that reads no device set restores no snapshot, and the
        // next claim consumes it unused (Known gaps). A take ends CH with
        // no guest power-off, so that cold boot would find its disk as a
        // power cut left it. So with no device read the VM takes no
        // snapshot, and the stop below is the graceful one (decision CH19
        // (3)): guest init syncs and powers off.
        if A::READS_DEVICES {
            // The blocking step owns the read lock, so the lock lasts as
            // long as the take (`retention::sweep`).
            let take = Arc::clone(&self.takes).read_owned().await;
            let taken = blocking(move || {
                let _take = take;
                api.snapshot(&run, &session, &key)
            })
            .await;
            if let Err(message) = taken {
                eprintln!("vmm: the snapshot of {id} failed: {message}");
            }
        }
        if let Err(error) = stop(vm.link.as_ref(), &mut vm.process, &vm.run).await {
            eprintln!("vmm: the VM of {id} did not stop after its snapshot: {error}");
            vm.link = None;
            eviction.put_back();
            return false;
        }
        {
            let mut control = self.control();
            if let Some(link) = control.link(&id) {
                control.closed(&id, link);
            }
        }
        eviction.done(&self.in_use);
        true
    }
}

/// The `StartVm` hook of the vmm: a cold boot or a restore.
#[derive(Debug)]
pub struct ColdBoot<L: Launcher, A: ChApi> {
    config: Arc<StartConfig>,
    launcher: Arc<L>,
    api: Arc<A>,
    table: Table<L::Process>,
    in_use: Arc<Mutex<InUse>>,
    /// Each take holds it to read, and the sweep holds it to write, so no
    /// sweep runs while a take runs (`retention::sweep`).
    takes: Arc<RwLock<()>>,
    /// Each key check and claim holds it, and so does the sweep, so no
    /// generation joins `in_use` during a sweep (decision CH50 rule 8).
    claims: Arc<Mutex<()>>,
    relays: Relays,
    rows: Arc<Mutex<Rows>>,
    control: Arc<Mutex<ControlState>>,
    faults: Arc<Mutex<FaultBook>>,
}

/// The gates of one running VM (PR D12).
struct Gates {
    /// The last use, for the eviction order (decision CH50 rules 5 and 6).
    last_use: Instant,
    /// `Activity::may_evict`, with a control link and a key.
    evict: bool,
    /// `Activity::may_sleep`, with a control link and a key.
    sleep: bool,
}

impl<L: Launcher, A: ChApi> ColdBoot<L, A> {
    /// The hook over `config`, `launcher` and `api`. It shares the relay
    /// registry, the rows, the control state and the fault book with the
    /// rest of the vmm.
    pub fn new(
        config: StartConfig,
        launcher: Arc<L>,
        api: Arc<A>,
        relays: Relays,
        rows: Arc<Mutex<Rows>>,
        control: Arc<Mutex<ControlState>>,
        faults: Arc<Mutex<FaultBook>>,
    ) -> Self {
        Self {
            config: Arc::new(config),
            launcher,
            api,
            table: Arc::default(),
            in_use: Arc::default(),
            takes: Arc::default(),
            claims: Arc::default(),
            relays,
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

    /// The snapshot generations that a VM restores from, from the key
    /// check of its start until its CH ends (decision CH50 rule 8).
    /// `sweep` reads them.
    #[must_use]
    pub fn in_use(&self) -> InUse {
        lock_in_use(&self.in_use).clone()
    }

    /// The control state of every VM.
    fn control(&self) -> MutexGuard<'_, ControlState> {
        self.control.lock().unwrap_or_else(PoisonError::into_inner)
    }

    /// The VM of `id` in the table. A VM that exited leaves the table, so
    /// the start boots it again.
    fn find(&self, id: &SessionId) -> Result<Found, String> {
        let mut table = lock(&self.table);
        let vm = match table.get_mut(id) {
            None => return Ok(Found::Free),
            Some(Slot::Starting(_)) => return Err("a start of the VM runs".to_owned()),
            Some(Slot::Evicting(_)) => return Ok(Found::Evicting),
            Some(Slot::Running(vm)) => vm,
        };
        match vm.process.exited() {
            Ok(false) => Ok(Found::Runs(vm.run.vsock_socket())),
            Ok(true) => {
                table.remove(id);
                release(&self.in_use, id);
                Ok(Found::Free)
            }
            Err(error) => Err(format!("checking the VM process: {error}")),
        }
    }

    /// The gates of `vm` of `session` at `now` (decisions CH8-6, CH19
    /// (6)). Only a VM with a link and a key may be evicted or sleep.
    fn gates(&self, session: &SessionId, vm: &Vm<L::Process>, now: Instant) -> Gates {
        // Decision CH50 rules 5 and 6: the start, each turn event and each
        // door close set the last use, and an open door counts as used
        // now in the eviction order.
        let door = self.relays.door(session);
        let control = self.control();
        let mut last_use = vm.last_use;
        if let Door::ClosedAt(at) = door {
            last_use = last_use.max(at.into_std());
        }
        if let Some(at) = control.last_turn(session) {
            last_use = last_use.max(at);
        }
        let activity = Activity {
            facade: control.facade(session),
            turn_seen: control.turn_seen(session),
            turn_running: control.turn_running(session),
            door_open: door == Door::Open,
            last_use,
        };
        let ready = vm.link.is_some() && vm.key.is_some();
        Gates {
            last_use: if door == Door::Open { now } else { last_use },
            evict: ready && activity.may_evict(),
            sleep: ready && activity.may_sleep(now),
        }
    }

    /// The admission view of the VM of `session` in `slot` (decisions
    /// CH8-6, CH19 (6)).
    fn view(&self, session: &SessionId, slot: &Slot<L::Process>, now: Instant) -> Running {
        let (guest_ram, last_use, gate) = match slot {
            Slot::Starting(ram) | Slot::Evicting(ram) => (*ram, now, Gate::Busy),
            Slot::Running(vm) => {
                // PR D12: `may_evict`, not `may_sleep`, is the gate of
                // admission.
                let gates = self.gates(session, vm, now);
                let gate = if gates.evict { Gate::Idle } else { Gate::Busy };
                (vm.guest_ram, gates.last_use, gate)
            }
        };
        Running {
            session: session.clone(),
            guest_ram,
            last_use,
            gate,
        }
    }

    /// Takes the VM of `session` out of `table` and marks its slot
    /// `Evicting`, so its RAM still counts.
    fn take_out(
        &self,
        table: &mut HashMap<SessionId, Slot<L::Process>>,
        session: SessionId,
    ) -> Option<Eviction<L::Process>> {
        let Some(Slot::Running(vm)) = table.remove(&session) else {
            return None;
        };
        table.insert(session.clone(), Slot::Evicting(vm.guest_ram));
        Some(Eviction {
            table: Arc::clone(&self.table),
            id: session,
            vm: Some(vm),
        })
    }

    /// Reserves `guest_ram` for `id` when admission admits it, and takes
    /// out each VM that admission evicts first, in its order.
    fn admit(&self, id: &SessionId, guest_ram: u64, free: u64) -> Option<Admitted<L::Process>> {
        let mut table = lock(&self.table);
        // A start that was dropped, a failed stop or a guest crash leaves
        // an entry whose process may have exited since.
        reap(&mut table, &self.in_use);
        let now = Instant::now();
        let running: Vec<Running> = table
            .iter()
            .map(|(session, slot)| self.view(session, slot, now))
            .collect();
        let evict = match decide(guest_ram, &running, free, self.config.limits) {
            Admission::Admit => Vec::new(),
            Admission::EvictFirst(evict) => evict,
            Admission::HostFull(_) => return None,
        };
        let evictions = evict
            .into_iter()
            .filter_map(|session| self.take_out(&mut table, session))
            .collect();
        table.insert(id.clone(), Slot::Starting(guest_ram));
        let pending = Pending {
            table: Arc::clone(&self.table),
            in_use: Arc::clone(&self.in_use),
            id: id.clone(),
            guest_ram,
            run: self.config.run.vm(id),
            process: None,
            key: None,
            done: false,
        };
        Some((pending, evictions))
    }

    /// The owned parts of a retire, for a task of its own.
    fn retirer(&self) -> Retirer<A> {
        Retirer {
            api: Arc::clone(&self.api),
            takes: Arc::clone(&self.takes),
            in_use: Arc::clone(&self.in_use),
            relays: self.relays.clone(),
            control: Arc::clone(&self.control),
            state: self.config.state.clone(),
        }
    }

    /// Puts each VM to sleep that `Activity::may_sleep` passes at `now`:
    /// its facade is up, no turn runs, no door link is open, and it is
    /// idle past `IDLE_TIMEOUT` (decisions CH2, CH19 (6), CH36 G12). It
    /// takes out one VM, retires it, then reads the table again for the
    /// next. So a VM waits as `Evicting` only for its own retire, and a VM
    /// whose door opens meanwhile fails its gate and stays up. Returns
    /// the sessions whose VM slept.
    // Decision CH19 (6): the gate and the take out run under the table
    // lock. An Upgrade claims its door before its start reads the table,
    // so a door that opens after the gate finds the VM `Evicting` and gets
    // `host_full` (decision CH54 rule 4). The sleep closes no door.
    pub async fn sleep_idle(&self, now: Instant) -> Vec<SessionId> {
        let (mut tried, mut slept) = (HashSet::new(), Vec::new());
        loop {
            let next = {
                let mut table = lock(&self.table);
                let session = table.iter().find_map(|(session, slot)| match slot {
                    Slot::Running(vm)
                        if !tried.contains(session) && self.gates(session, vm, now).sleep =>
                    {
                        Some(session.clone())
                    }
                    _ => None,
                });
                session.and_then(|session| {
                    // A VM whose retire fails goes back and passes its gate
                    // again, so each VM gets one try per pass.
                    tried.insert(session.clone());
                    self.take_out(&mut table, session)
                })
            };
            let Some(eviction) = next else {
                return slept;
            };
            let id = eviction.id.clone();
            if detached(self.retirer().sleep(eviction)).await {
                slept.push(id);
            }
        }
    }

    /// Runs `retention::sweep` with `BUDGET` over the state root (PR D13,
    /// decisions CH2, CH8-7). It holds the lock of the claims, so no key
    /// check or claim runs, and it names each generation in a copy of the
    /// `InUse` set. A VM that ends meanwhile only shrinks the set, so the
    /// copy names each generation in use, and the sweep never removes the
    /// files of a restored VM (decision CH50 rule 8). It waits for each
    /// take to end, and no take starts until it ends. The blocking step
    /// owns both locks, so a dropped sweep future frees neither early.
    ///
    /// # Errors
    ///
    /// A failed read of `sessions/`, or a sweep that panicked.
    pub async fn sweep(&self) -> io::Result<Swept> {
        let takes = Arc::clone(&self.takes).write_owned().await;
        let (claims, in_use, state) = (
            Arc::clone(&self.claims),
            Arc::clone(&self.in_use),
            self.config.state.clone(),
        );
        spawn_blocking(move || {
            let _takes = takes;
            let _claims = lock_claims(&claims);
            let in_use = lock_in_use(&in_use).clone();
            sweep(&state, SystemTime::now(), BUDGET, &in_use)
        })
        .await
        .unwrap_or_else(|_| Err(io::Error::other("the sweep panicked")))
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

    /// Checks a restored VM, then resumes it (decision CH50 rule 10). CH
    /// serves its API only after it starts, so a failed `info` is tried
    /// again inside `READY_WAIT`.
    // Decision CH54 rule 3: a VM that is not `Paused`, or that has a
    // refused device, gives `egress_assert_failed`. A CH that does not
    // answer `info` or `resume` gives `sandbox_start_failed`.
    async fn resume_checked(
        &self,
        run: &VmRunDir,
        expected: Expected,
    ) -> Result<(), (FaultCode, String)> {
        let sandbox = FaultCode::SandboxStartFailed;
        let deadline = Instant::now() + READY_WAIT;
        let reply = loop {
            let (api, vm) = (Arc::clone(&self.api), run.clone());
            match blocking(move || api.info(&vm)).await {
                Ok(reply) => break reply,
                Err(error) if Instant::now() >= deadline => {
                    return Err((sandbox, format!("reading ch-remote info: {error}")));
                }
                Err(_) => sleep(DIAL_PAUSE).await,
            }
        };
        if let Err(error) = check_devices(&reply, &expected) {
            return Err((FaultCode::EgressAssertFailed, error.to_string()));
        }
        let (api, vm) = (Arc::clone(&self.api), run.clone());
        blocking(move || api.resume(&vm))
            .await
            .map_err(|error| (sandbox, format!("resuming the VM: {error}")))
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

/// The snapshot that the start restores, or `None` for a cold boot
/// (decisions CH7-6, CH19 (5), CH44 rules 5 and 6). When `A` reads no
/// device set (`ChApi::READS_DEVICES`), the claim still consumes the
/// snapshot, because the cold boot moves the session disk on.
///
/// The key check and the claim run under the lock of the claims, and
/// the generation stays in `in_use` until CH ends (decision CH50 rule 8).
/// So a sweep that holds the lock of the claims never removes a
/// generation that a start restores. The lock of `in_use` lasts one
/// insert or one remove (decision CH36 G21).
fn claim_newest<A: ChApi>(
    session: &SessionDir,
    key: &SnapshotKey,
    (claims, in_use): (&Mutex<()>, &Mutex<InUse>),
    id: &SessionId,
) -> Result<Option<ClaimedSnapshot>, String> {
    let _claims = lock_claims(claims);
    let keyed = match check_newest(session, key).map_err(|error| error.to_string())? {
        KeyCheck::Keyed(keyed) => keyed,
        KeyCheck::NoSnapshot | KeyCheck::Dropped { .. } => return Ok(None),
    };
    let entry = (id.clone(), keyed.generation());
    lock_in_use(in_use).insert(entry.clone());
    let claimed = match claim(keyed) {
        Ok(claimed) => claimed,
        Err(error @ ClaimError::Retired { .. }) => {
            eprintln!("vmm: {error}");
            None
        }
        Err(error @ ClaimError::Stuck { .. }) => {
            lock_in_use(in_use).remove(&entry);
            return Err(error.to_string());
        }
    };
    if claimed.is_none() || !A::READS_DEVICES {
        lock_in_use(in_use).remove(&entry);
        return Ok(None);
    }
    Ok(claimed)
}

/// What the launch step started.
struct Booted<P> {
    /// The CH process.
    process: P,
    /// The snapshot key of the VM.
    key: SnapshotKey,
    /// The start that the ruleset and the deny check name.
    start: Start,
    /// The files of a restore, for the device check. `None` for a boot.
    restore: Option<Expected>,
}

/// Step 3: the files of the boot, the restore or cold boot choice, then
/// the launch.
fn launch<L: Launcher, A: ChApi>(
    config: &StartConfig,
    launcher: &L,
    locks: (&Mutex<()>, &Mutex<InUse>),
    id: &SessionId,
    shape: FamilyShape,
) -> Result<Booted<L::Process>, String> {
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
    let key = SnapshotKey::current(config.ch_version, &kernel, &root);
    let (command, start, restore) = match claim_newest::<A>(&session, &key, locks, id)? {
        Some(snapshot) => {
            let command = ChCommand::restore(snapshot, &run).map_err(|error| error.to_string())?;
            let expected = Expected {
                root: root.path().to_owned(),
                session: disk.path().to_owned(),
                vsock: run.vsock_socket(),
            };
            (command, Start::Restore, Some(expected))
        }
        None => {
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
            (command, start, None)
        }
    };
    let files = VmFiles::new(start.clone(), host(root.path())?, &session, &run)
        .map_err(|error| error.to_string())?;
    let payload = command.payload(config.ch.clone());
    let scope = limits.scope(id.clone());
    let plan = Launch::new(config.systemd_run.clone(), scope, payload, files);
    let process = launcher.launch(plan).map_err(|error| error.to_string())?;
    Ok(Booted {
        process,
        key,
        start,
        restore,
    })
}

impl<L: Launcher, A: ChApi> StartVm for ColdBoot<L, A> {
    async fn start(&self, row: &SessionRow) -> Result<AcpLink, StartFault> {
        let (id, family) = (&row.id, &row.family);
        let sandbox = FaultCode::SandboxStartFailed;
        match self.find(id) {
            Ok(Found::Runs(vsock)) => return self.dial(family, &vsock).await,
            Ok(Found::Free) => {}
            // Decision CH54 rule 4: an Upgrade of a session whose VM is
            // being evicted or put to sleep gets `host_full`. The VM still
            // holds its RAM.
            Ok(Found::Evicting) => return Err(StartFault::HostFull),
            Err(message) => return Err(self.fault(family, sandbox, message).await),
        }
        let (config, name) = (Arc::clone(&self.config), family.clone());
        let (shape, free) = match blocking(move || read_shape(&config, &name)).await {
            Ok(read) => read,
            Err(message) => return Err(self.fault(family, sandbox, message).await),
        };
        let guest_ram = VmLimits::from(shape).guest_ram();
        // `host_full` is no fault of decision CH6-3, so it writes none.
        let (mut pending, evictions) = self
            .admit(id, guest_ram, free)
            .ok_or(StartFault::HostFull)?;
        // The evictions run in the order of admission, in a task of their
        // own. After one that fails, the rest go back, and the start does
        // not fit.
        let fits = evictions.is_empty() || detached(self.retirer().evict_all(evictions)).await;
        if !fits {
            return Err(StartFault::HostFull);
        }

        let (config, launcher, claims, in_use, session) = (
            Arc::clone(&self.config),
            Arc::clone(&self.launcher),
            Arc::clone(&self.claims),
            Arc::clone(&self.in_use),
            id.clone(),
        );
        // The process goes into `pending` inside the closure. hyper drops
        // this future when the client hangs up before 101, and the blocking
        // step then ends with no one to take its output. The drop of that
        // output kills the VM and keeps it in the table, so admission
        // counts its RAM and no CH runs outside the table (decisions CH8-6,
        // CH50 rule 4).
        let launched = blocking(move || {
            let locks = (&*claims, &*in_use);
            let booted = launch::<L, A>(&config, &*launcher, locks, &session, shape)?;
            let cmdline = launcher.cmdline(&booted.process);
            pending.process = Some(booted.process);
            pending.key = Some(booted.key);
            Ok((pending, booted.start, booted.restore, cmdline))
        })
        .await;
        let (pending, start, restore, cmdline) = match launched {
            Ok(launched) => launched,
            Err(message) => return Err(self.fault(family, sandbox, message).await),
        };

        let vsock = pending.run.vsock_socket();
        let link = self.control().replace(id);
        if let Some(expected) = restore
            && let Err((code, message)) = self.resume_checked(&pending.run, expected).await
        {
            return Err(self.abort(pending, link, None, family, code, message).await);
        }
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

#[cfg(test)]
mod tests {
    use std::fs;
    use std::path::PathBuf;
    use std::sync::Mutex;
    use std::time::Duration;

    use creche_vmm_store::images::ImageStore;
    use creche_vmm_store::key::{SnapshotKey, check_cloud_hypervisor, write_key};
    use creche_vmm_store::layout::{SnapshotGeneration, StateRoot};
    use creche_vmm_store::retention::InUse;
    use creche_wire::SessionId;

    use super::claim_newest;
    use crate::restore::ChTools;

    const EMPTY: &str = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855";

    /// With no device check (`ChTools`), the start cold-boots, and the
    /// claim still consumes the snapshot, because the cold boot moves the
    /// session disk on (decisions CH19 (5), CH50 rule 10).
    #[test]
    fn no_device_check_consumes_and_cold_boots() {
        let dir = PathBuf::from(format!("/tmp/vmm-claim-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        fs::write(dir.join("image"), b"").unwrap();
        let state = StateRoot::new(dir.join("state")).unwrap();
        let images = ImageStore::new(&state, PathBuf::from("/usr/bin/zstd"));
        let digest = EMPTY.parse().unwrap();
        images.import(&dir.join("image"), digest).unwrap();
        let empty = images.check(digest).unwrap();
        let stub = concat!(
            env!("CARGO_MANIFEST_DIR"),
            "/tests/fixtures/cloud-hypervisor"
        );
        let ch = check_cloud_hypervisor(stub.as_ref(), Duration::from_secs(10)).unwrap();
        let key = SnapshotKey::current(ch, &empty, &empty);
        let id: SessionId = "3f2b8c1e-9d4a-4e6b-8a7c-0b1d2e3f4a5b".parse().unwrap();
        let session = state.session(&id);
        let generation = SnapshotGeneration(1);
        fs::create_dir_all(session.snapshot(generation)).unwrap();
        write_key(&session, generation, &key).unwrap();

        let in_use = Mutex::new(InUse::new());
        let claimed =
            claim_newest::<ChTools>(&session, &key, (&Mutex::new(()), &in_use), &id).unwrap();
        assert!(claimed.is_none());
        assert!(session.snapshot(generation).join("consumed").is_file());
        assert!(in_use.lock().unwrap().is_empty());
        let _ = fs::remove_dir_all(&dir);
    }
}
