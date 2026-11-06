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
//!    Until the guest can sync (`ChApi::GUEST_SYNCS`), the VM takes no
//!    snapshot and gets the graceful stop.
//! 3. In `spawn_blocking`: a vmm in no service unit refuses the launch
//!    here (decision CH60 rule 1). Then the stale sockets go (PR D1,
//!    decision CH19 (4)), the kernel, the initramfs and the root disk
//!    pass the store check (PR D6, decision CH44 rule 2), the session disk exists (PR D7, decision
//!    CH21-4), the run dir `vm/<id>/` links to the slot of the start (PR
//!    D16), `snapshots/` exists, and `sessions/<id>/serial.log` in the
//!    state root is a new file with mode 0600 (gap G23, decision CH59 rule
//!    2, PR D17). The key check (PR D10) and the claim (PR D11) pick a
//!    restore or a cold boot. Then the launch step starts CH in its slot
//!    (PR C18) with the argv of PR C7 and the sizes of PR C4 under the
//!    ruleset of PR C6 (PR C2, decisions CH8-3, CH8-5, CH8-8, CH30, CH55).
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
//! Plan PR L16b: each entry of the table holds one slot of the pool
//! (decisions CH51, CH55). Admission takes the lowest free slot under
//! the table lock. With no free slot it evicts the least recently used
//! idle VM and takes its slot, else `host_full` (decisions CH21-3, CH58
//! rule 2).
//! The entry keeps its slot until its CH ended. Then the entry leaves the
//! table, and `release` removes the `vm/<id>` link first. So a free slot
//! holds no live CH (decision CH19 (4)), and `RunRoot::link_vm` may
//! empty it (decision CH56 rule 1).
//!
//! The idle sleep and the snapshot sweep of plan PR L18 use the same
//! table (`ColdBoot::sleep_idle`, `ColdBoot::sweep`, `sleep::run`). So
//! do the `StopVm` hook of the drop route (plan PR L18b) and the `VmSet`
//! of the shutdown (plan PR L19).

use std::collections::{HashMap, HashSet};
use std::fs::DirBuilder;
use std::io;
use std::os::unix::fs::DirBuilderExt;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex, MutexGuard, PoisonError};
use std::time::{Duration, Instant, SystemTime};

use creche_vmm_confine::admission::{Admission, Gate, Limits, Running, decide, free_bytes};
use creche_vmm_confine::command::{Boot, ChCommand, DiskLimit};
use creche_vmm_confine::deny::{check, cmdline_of};
use creche_vmm_confine::family::FamilyRoot;
use creche_vmm_confine::faults::{FaultBook, FaultCode};
use creche_vmm_confine::landlock::{HostPath, Start, VmFiles};
use creche_vmm_confine::launch::{Launch, Program};
use creche_vmm_confine::limits::VmLimits;
use creche_vmm_confine::spawner::{Launched, SpawnError, Spawner};
use creche_vmm_confine::vmm_unit::{UnitError, VmmUnit};
use creche_vmm_store::ch_remote::ChRemoteError;
use creche_vmm_store::claim::{ClaimError, ClaimedSnapshot, claim};
use creche_vmm_store::images::{ImageDigest, ImageStore};
use creche_vmm_store::key::{CheckedCh, KeyCheck, SnapshotKey, check_newest};
use creche_vmm_store::layout::{RunRoot, SessionDir, Slot as PoolSlot, StateRoot, VmRunDir};
use creche_vmm_store::retention::{InUse, Swept, sweep};
use creche_vmm_store::rows::Rows;
use creche_vmm_store::serial_log::SerialLog;
use creche_vmm_store::session_disk::SessionDisks;
use creche_vmm_store::sleep_gate::Activity;
use creche_vmm_store::stale::remove_stale;
use creche_wire::{FamilyName, FamilyShape, GuestEvent, PerFlavor, SessionId, SessionRow};
use tokio::net::UnixStream;
use tokio::sync::{RwLock, watch};
use tokio::task::spawn_blocking;
use tokio::time::sleep;

use crate::control::{
    Change, ControlError, ControlLink, ControlState, DIAL_PAUSE, EventEnd, LinkId, READY_WAIT,
    StopSender, open, wall_time,
};
use crate::handlers::acp::{StartFault, StartVm};
use crate::handlers::drop::{StopVm, VmStopFailed};
use crate::relay::{AcpLink, Door, Relays};
use crate::restore::{ChApi, Expected, check_devices};
use crate::shutdown::{Taken, VmSet};
use crate::stop::{VmProcess, stop};

/// The mode of the run dir and the snapshots dir of a VM.
const DIR_MODE: u32 = 0o700;

/// The pause between two reads of a slot that a start or a retire holds,
/// while a stop waits for it. No row sets it. Only the vmm reads it, so a
/// change costs one constant.
const SLOT_POLL: Duration = Duration::from_millis(10);

/// The store digests of the guest kernel and of the root disk of each
/// flavor (decisions CH7-3, CH8-8). The vmm keeps one root disk for each
/// value of the wire flavor enum (decision CH58 rule 4).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Digests {
    /// The guest kernel. Every flavor boots it.
    pub kernel: ImageDigest,
    /// The root disk of each flavor.
    pub roots: PerFlavor<ImageDigest>,
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
    /// The kernel and the root disks. The vmm reads them from the
    /// `digests.json` of its image root (decision CH57 rule 1).
    pub digests: Digests,
    /// The guest init initramfs in the digest store (decision CH60 rule
    /// 2). `None` until phase 3 adds guest init (PR C7): the compose step
    /// sets none, so each boot has none, its snapshot has none, and its
    /// restore carries none (decision CH61 rule 3).
    pub initramfs: Option<ImageDigest>,
    /// The Cloud Hypervisor program.
    pub ch: Program,
    /// The `systemd-run` program of the launch (PR C2).
    pub systemd_run: Program,
    /// The unit of the vmm, which each VM scope binds to (decision CH60
    /// rule 1). The vmm reads it at its start with `VmmUnit::of_self`. A
    /// vmm in no service unit keeps the error, and every launch then
    /// refuses with it and starts nothing.
    pub vmm_unit: Result<VmmUnit, UnitError>,
    /// The RAM cap and the free-space floor of admission.
    pub limits: Limits,
    /// The snapshot budget of the sweep, in bytes (decision CH8-7, gap
    /// G14). The vmm config reads it, and its catalog default equals
    /// `retention::BUDGET`.
    pub budget: u64,
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
    /// The slot of the VM until its CH ended (plan PR L16b).
    slot: PoolSlot,
    /// The control link after `ControlReady`. `None` after a failed stop.
    link: Option<StopSender>,
    /// The identity of that link, so its reader stops only this VM
    /// (decision CH54 rule 6). `None` for a VM whose start never
    /// committed it.
    link_id: Option<LinkId>,
    /// The snapshot key of the VM. `None` after a failed stop.
    key: Option<SnapshotKey>,
    /// The last use, for the eviction order (decision CH50 rule 5).
    last_use: Instant,
}

/// What the table holds for a session. Each entry holds its pool slot
/// (plan PR L16b).
#[derive(Debug)]
enum Slot<P> {
    /// A start passed admission and holds this much guest RAM.
    Starting(u64, PoolSlot),
    /// The VM runs, or its stop failed (decision CH50 rule 4).
    Running(Vm<P>),
    /// Admission evicts the VM, which holds this much guest RAM.
    Evicting(u64, PoolSlot),
    /// The `StopVm` hook stops the VM, which holds this much guest RAM.
    /// A second stop waits for the outcome.
    Stopping(u64, PoolSlot, Outcome),
}

impl<P> Slot<P> {
    /// The pool slot of the entry.
    fn pool(&self) -> PoolSlot {
        match self {
            Self::Starting(_, slot) | Self::Evicting(_, slot) | Self::Stopping(_, slot, _) => *slot,
            Self::Running(vm) => vm.slot,
        }
    }
}

/// The pool slots that no entry of `table` holds, lowest first
/// (decisions CH51, CH55).
fn free_slots<P>(table: &HashMap<SessionId, Slot<P>>) -> Vec<PoolSlot> {
    let held: HashSet<PoolSlot> = table.values().map(Slot::pool).collect();
    PoolSlot::all()
        .filter(|slot| !held.contains(slot))
        .collect()
}

/// The outcome of the stop of a `Stopping` slot, `None` until it ends.
type Outcome = watch::Receiver<Option<Result<(), VmStopFailed>>>;

/// What a stop finds in the slot of its session.
enum Held<P: VmProcess> {
    /// No VM: nothing to stop.
    Free,
    /// A start or a retire holds the slot. The stop reads it again later.
    Busy,
    /// Another stop runs. This one returns its outcome.
    Joins(Outcome),
    /// The VM, out of the table under a `Stopping` slot, and the sender of
    /// its outcome.
    Takes(
        Box<Eviction<P>>,
        watch::Sender<Option<Result<(), VmStopFailed>>>,
    ),
}

/// What the table holds for the session of a start.
enum Found {
    /// No VM: the start boots or restores one.
    Free,
    /// A VM runs with this vsock socket.
    Runs(PathBuf),
    /// Admission evicts the VM of the session.
    Evicting,
    /// The shutdown refuses each start (plan PR L19, decision CH56 rule
    /// 4).
    Refused,
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

/// Frees the slot of `id`: its CH ended. It removes the `vm/<id>` link
/// (caller duty (b) of PR D16) and the generation of `id` from `in_use`
/// (decision CH50 rule 8). The caller holds the table lock and removes
/// the entry after this call, so no start of `id` links it before the
/// unlink. A failed unlink is logged. The slot still frees: the next link
/// of `id` replaces the link, and the old VM has no step left.
// Decision CH58 rule 3: the unlink is one `unlink` on the tmpfs of the
// run root, so it runs under the table lock and not in `spawn_blocking`,
// an accepted exception to CH36 G21. The status check and the SIGKILL
// under the same lock are system calls of the same cost.
fn release(in_use: &Mutex<InUse>, run: &RunRoot, id: &SessionId) {
    if let Err(error) = run.unlink_vm(id) {
        eprintln!("vmm: {error}: {}", error.source);
    }
    lock_in_use(in_use).retain(|(session, _)| session != id);
}

/// Removes each VM whose process exited from `table`, and its generation
/// from `in_use`. Decision CH50 rule 4 keeps the RAM of a VM that may
/// still run, and an exited one runs no more. A failed status check keeps
/// the entry, so its RAM still counts.
fn reap<P: VmProcess>(
    table: &mut HashMap<SessionId, Slot<P>>,
    in_use: &Mutex<InUse>,
    run: &RunRoot,
) {
    table.retain(|id, slot| {
        let Slot::Running(vm) = slot else {
            return true;
        };
        let exited = matches!(vm.process.exited(), Ok(true));
        if exited {
            release(in_use, run, id);
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
    root: RunRoot,
    id: SessionId,
    guest_ram: u64,
    slot: PoolSlot,
    run: VmRunDir,
    process: Option<P>,
    key: Option<SnapshotKey>,
    done: bool,
}

impl<P: VmProcess> Pending<P> {
    /// Makes the VM an entry of the table with its control link `link`,
    /// whose identity is `link_id`.
    fn commit(mut self, link: StopSender, link_id: LinkId) {
        if let Some(process) = self.process.take() {
            let vm = Vm {
                process,
                run: self.run.clone(),
                guest_ram: self.guest_ram,
                slot: self.slot,
                link: Some(link),
                link_id: Some(link_id),
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
                    slot: self.slot,
                    link: None,
                    link_id: None,
                    key: None,
                    last_use: Instant::now(),
                };
                table.insert(self.id.clone(), Slot::Running(vm));
            }
            None => {
                release(&self.in_use, &self.root, &self.id);
                table.remove(&self.id);
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

    /// Removes the stopped VM from the table and from `in_use`, and frees
    /// its slot.
    fn done(mut self, in_use: &Mutex<InUse>, run: &RunRoot) {
        self.vm = None;
        let mut table = lock(&self.table);
        release(in_use, run, &self.id);
        table.remove(&self.id);
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

/// How a retire ended.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Retired {
    /// The VM still holds its RAM: a turn runs, or the stop failed
    /// (decision CH50 rule 4).
    Kept,
    /// The VM stopped after its snapshot or its graceful stop.
    Slept,
    /// The VM stopped and left no snapshot to restore: its control link
    /// closed, or it refused a line (decision CH54 rule 6).
    Stopped,
}

/// The owned parts of a retire, so it runs in a task of its own.
#[derive(Debug, Clone)]
struct Retirer<A> {
    api: Arc<A>,
    /// Each take holds it to read, and the sweep holds it to write.
    takes: Arc<RwLock<()>>,
    /// The lock of the claims, for the claim of a faulted take.
    claims: Arc<Mutex<()>>,
    in_use: Arc<Mutex<InUse>>,
    relays: Relays,
    control: Arc<Mutex<ControlState>>,
    state: StateRoot,
    run: RunRoot,
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
                fits = self.retire(eviction).await != Retired::Kept;
            } else {
                eviction.put_back();
            }
        }
        fits
    }

    /// Retires the idle VM of `eviction` (decisions CH2, CH19 (6)). True
    /// when it slept.
    async fn sleep<P: VmProcess>(self, eviction: Eviction<P>) -> bool {
        self.retire(eviction).await == Retired::Slept
    }

    /// Snapshots the VM of `eviction`, then stops it (decision CH19 (6)).
    /// The take ends CH, so the stop finds it gone. With no device read
    /// (`ChApi::READS_DEVICES`) or no guest sync (`ChApi::GUEST_SYNCS`) it
    /// takes no snapshot and sends the graceful stop. `Kept` when the VM
    /// still holds its RAM: a turn runs, or the stop failed (decision CH50
    /// rule 4). `Stopped` when the VM faulted (decision CH54 rule 6).
    async fn retire<P: VmProcess>(&self, mut eviction: Eviction<P>) -> Retired {
        let id = eviction.id.clone();
        let key = eviction.vm.as_ref().and_then(|vm| vm.key.clone());
        let turn = self.control().turn_running(&id);
        let (Some(key), false) = (key, turn) else {
            eviction.put_back();
            return Retired::Kept;
        };
        let Some(vm) = eviction.vm.as_mut() else {
            return Retired::Kept;
        };
        // Decision CH54 rule 6: a refused line is a guest fault, and the
        // next start cold-boots. A link that closed after the gate leaves
        // the VM with no control link, so it takes no snapshot either. The
        // stop below then sends SIGTERM, then SIGKILL.
        let link = vm.link_id;
        let refused =
            |retirer: &Self| link.is_some_and(|link| retirer.control().refused(&id, link));
        let open = vm.link.as_ref().is_some_and(StopSender::is_open) && !refused(self);
        let (api, run, session) = (
            Arc::clone(&self.api),
            vm.run.clone(),
            self.state.session(&id),
        );
        // Decision CH44 rule 4: the caller decides after a failed take.
        // The VM stops anyway, so its RAM frees, and the session
        // cold-boots from its disk later (decision CH19 (5)).
        // An API that reads no device set restores no snapshot, and the
        // next claim consumes it unused (`ChApi::READS_DEVICES`). A take
        // ends CH with no guest power-off, so that cold boot would find its
        // disk as a power cut left it. So with no device read the VM takes
        // no snapshot, and the stop below is the graceful one (decision
        // CH19 (3)): guest init syncs and powers off.
        // Item 5.7 of the design asks for a guest `sync` before each take,
        // and the item 42 note gives it to lifecycle. The control link has
        // no `sync` command until phase 3, so with no guest sync
        // (`ChApi::GUEST_SYNCS`) the VM takes no snapshot either. The
        // phase 3 PR sends `sync` here before the take (caller duty 7).
        let took = open && A::READS_DEVICES && A::GUEST_SYNCS;
        if took {
            // The blocking step owns the read lock, so the lock lasts as
            // long as the take (`retention::sweep`).
            let take = Arc::clone(&self.takes).read_owned().await;
            let key = key.clone();
            let taken = blocking(move || {
                let _take = take;
                api.snapshot(&run, &session, &key)
            })
            .await;
            if let Err(message) = taken {
                eprintln!("vmm: the snapshot of {id} failed: {message}");
            }
        }
        let stopped = stop(vm.link.as_ref(), &mut vm.process, &vm.run).await;
        // A refused line that arrived during the take or the stop makes
        // the take one of a faulted guest, so the claim consumes it. A
        // line that the reader reads after this check is not seen here.
        let faulted = !open || refused(self);
        if took && faulted {
            self.discard(&id, key).await;
        }
        if let Err(error) = stopped {
            eprintln!("vmm: the VM of {id} did not stop after its snapshot: {error}");
            vm.link = None;
            eviction.put_back();
            return Retired::Kept;
        }
        forget_link(&self.control, &id);
        eviction.done(&self.in_use, &self.run);
        if faulted {
            eprintln!("vmm: the VM of {id} faulted during its retire, so it keeps no snapshot");
            Retired::Stopped
        } else {
            Retired::Slept
        }
    }

    /// Consumes the newest snapshot of `id`, the take of a faulted guest,
    /// so the next start cold-boots (decisions CH19 (5), CH54 rule 6). It
    /// holds the lock of the claims, as a start does. A failed claim is
    /// logged.
    async fn discard(&self, id: &SessionId, key: SnapshotKey) {
        let (claims, session) = (Arc::clone(&self.claims), self.state.session(id));
        let claimed = blocking(move || {
            let _claims = lock_claims(&claims);
            match check_newest(&session, &key).map_err(|error| error.to_string())? {
                KeyCheck::Keyed(keyed) => claim(keyed)
                    .map(|_unused| ())
                    .map_err(|error| error.to_string()),
                KeyCheck::NoSnapshot | KeyCheck::Dropped { .. } => Ok(()),
            }
        })
        .await;
        if let Err(message) = claimed {
            eprintln!("vmm: the snapshot of the faulted VM of {id} was not consumed: {message}");
        }
    }
}

/// Takes the VM of `session` out of `slots`, the locked `table`, and
/// marks its slot with `mark` of its guest RAM and its pool slot, so its
/// RAM and its pool slot stay held.
fn take_out<P: VmProcess>(
    table: &Table<P>,
    slots: &mut HashMap<SessionId, Slot<P>>,
    session: SessionId,
    mark: impl FnOnce(u64, PoolSlot) -> Slot<P>,
) -> Option<Eviction<P>> {
    let Some(Slot::Running(vm)) = slots.remove(&session) else {
        return None;
    };
    slots.insert(session.clone(), mark(vm.guest_ram, vm.slot));
    Some(Eviction {
        table: Arc::clone(table),
        id: session,
        vm: Some(vm),
    })
}

/// Marks the current control link of `id` closed: its VM stopped.
fn forget_link(control: &Mutex<ControlState>, id: &SessionId) {
    let mut control = control.lock().unwrap_or_else(PoisonError::into_inner);
    if let Some(link) = control.link(id) {
        control.closed(id, link);
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
    /// The shutdown set it (`VmSet::refuse_starts`). It is set and read
    /// under the table lock, so no start adds a slot after a shutdown read
    /// the table.
    refusing: AtomicBool,
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
            refusing: AtomicBool::new(false),
        }
    }

    /// The config and the API of the hook, for the wiring test of the
    /// compose step.
    #[cfg(test)]
    pub(crate) fn parts(&self) -> (&StartConfig, &A) {
        (&self.config, &self.api)
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
        if self.refusing.load(Ordering::SeqCst) {
            return Ok(Found::Refused);
        }
        let vm = match table.get_mut(id) {
            None => return Ok(Found::Free),
            Some(Slot::Starting(..)) => return Err("a start of the VM runs".to_owned()),
            // Decision CH54 rule 4: a stop holds the RAM of the VM as an
            // eviction does, so the start gets `host_full`. Under the mark
            // of a drop no Upgrade reaches the hook.
            Some(Slot::Evicting(..) | Slot::Stopping(..)) => return Ok(Found::Evicting),
            Some(Slot::Running(vm)) => vm,
        };
        match vm.process.exited() {
            Ok(false) => Ok(Found::Runs(vm.run.vsock_socket())),
            Ok(true) => {
                release(&self.in_use, &self.config.run, id);
                table.remove(id);
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
            turn_running: control.turn_running(session),
            door_open: door == Door::Open,
            last_use,
        };
        // A link whose reader ended is closed (decision CH43 rule 2).
        let ready = vm.link.as_ref().is_some_and(StopSender::is_open) && vm.key.is_some();
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
            Slot::Starting(ram, _) | Slot::Evicting(ram, _) | Slot::Stopping(ram, ..) => {
                (*ram, now, Gate::Busy)
            }
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

    /// Reserves `guest_ram` and a pool slot for `id` when admission admits
    /// it, and takes out each VM that admission evicts first, in its
    /// order. With no free slot, the start takes the slot of its first
    /// eviction (plan PR L16b, decision CH58 rule 2).
    fn admit(&self, id: &SessionId, guest_ram: u64, free: u64) -> Option<Admitted<L::Process>> {
        let mut table = lock(&self.table);
        // A start that read the table before the refusal of a shutdown
        // stops here, so it adds no slot (plan PR L19).
        if self.refusing.load(Ordering::SeqCst) {
            return None;
        }
        // A start that was dropped, a failed stop or a guest crash leaves
        // an entry whose process may have exited since.
        reap(&mut table, &self.in_use, &self.config.run);
        let now = Instant::now();
        let running: Vec<Running> = table
            .iter()
            .map(|(session, slot)| self.view(session, slot, now))
            .collect();
        let free_slots = free_slots(&table);
        let limits = self.config.limits;
        let evict = match decide(guest_ram, &running, free, free_slots.len(), limits) {
            Admission::Admit => Vec::new(),
            Admission::EvictFirst(evict) => evict,
            Admission::HostFull(_) => return None,
        };
        // Decision CH58 rule 2: a full pool counts like a full RAM cap, so
        // admission evicts one idle VM at least, and the start takes the
        // slot of the first. That VM holds the slot as `Evicting` until
        // its CH ends. The start links the slot only after each eviction
        // ended, and a failed eviction fails the start, so no two live CH
        // share a slot.
        let slot = match free_slots.first() {
            Some(slot) => *slot,
            None => evict
                .first()
                .and_then(|session| table.get(session))
                .map(Slot::pool)?,
        };
        let evictions = evict
            .into_iter()
            .filter_map(|session| take_out(&self.table, &mut table, session, Slot::Evicting))
            .collect();
        table.insert(id.clone(), Slot::Starting(guest_ram, slot));
        let pending = Pending {
            table: Arc::clone(&self.table),
            in_use: Arc::clone(&self.in_use),
            root: self.config.run.clone(),
            id: id.clone(),
            guest_ram,
            slot,
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
            claims: Arc::clone(&self.claims),
            in_use: Arc::clone(&self.in_use),
            relays: self.relays.clone(),
            control: Arc::clone(&self.control),
            state: self.config.state.clone(),
            run: self.config.run.clone(),
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
                    take_out(&self.table, &mut table, session, Slot::Evicting)
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

    /// Runs `retention::sweep` with `StartConfig::budget` over the state
    /// root (PR D13, decisions CH2, CH8-7). It holds the lock of the
    /// claims, so no key check or claim runs, and it names each generation
    /// in a copy of the `InUse` set. A VM that ends meanwhile only shrinks
    /// the set, so the copy names each generation in use, and the sweep
    /// never removes the files of a restored VM (decision CH50 rule 8). It
    /// waits for each take to end, and no take starts until it ends. The
    /// blocking step owns both locks, so a dropped sweep future frees
    /// neither early.
    ///
    /// # Errors
    ///
    /// A failed read of `sessions/`, or a sweep that panicked.
    pub async fn sweep(&self) -> io::Result<Swept> {
        let takes = Arc::clone(&self.takes).write_owned().await;
        let (claims, in_use, state, budget) = (
            Arc::clone(&self.claims),
            Arc::clone(&self.in_use),
            self.config.state.clone(),
            self.config.budget,
        );
        spawn_blocking(move || {
            let _takes = takes;
            let _claims = lock_claims(&claims);
            let in_use = lock_in_use(&in_use).clone();
            sweep(&state, SystemTime::now(), budget, &in_use)
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
    // answer `info` or `resume` gives `sandbox_start_failed`. A reply that
    // is not a VM info is a device set that the check cannot read, so it
    // gives `egress_assert_failed` at once.
    async fn resume_checked(
        &self,
        run: &VmRunDir,
        expected: Expected,
    ) -> Result<(), (FaultCode, String)> {
        let (sandbox, egress) = (FaultCode::SandboxStartFailed, FaultCode::EgressAssertFailed);
        let deadline = Instant::now() + READY_WAIT;
        let info = loop {
            let (api, vm) = (Arc::clone(&self.api), run.clone());
            match blocking(move || Ok(api.info(&vm))).await {
                Ok(Ok(info)) => break info,
                Ok(Err(ChRemoteError::NotInfo(error))) => {
                    return Err((egress, format!("the info reply is not a VM info: {error}")));
                }
                Ok(Err(error)) if Instant::now() >= deadline => {
                    return Err((sandbox, format!("reading ch-remote info: {error}")));
                }
                Err(error) if Instant::now() >= deadline => {
                    return Err((sandbox, format!("reading ch-remote info: {error}")));
                }
                Ok(Err(_)) | Err(_) => sleep(DIAL_PAUSE).await,
            }
        };
        if let Err(error) = check_devices(&info, &expected) {
            return Err((egress, error.to_string()));
        }
        let (api, vm) = (Arc::clone(&self.api), run.clone());
        blocking(move || api.resume(&vm))
            .await
            .map_err(|error| (sandbox, format!("resuming the VM: {error}")))
    }
}

/// Writes the turn end of `change` to the row of `id` in `rows`
/// (decision CH32 rule 4): `updated_at`, and the title only while the row
/// has none. A failed write is logged: the next `ControlReady` carries the
/// end again.
async fn turn_end(rows: &Arc<Mutex<Rows>>, id: &SessionId, change: Change) {
    let Change::Applied(Some(end)) = change else {
        return;
    };
    let (rows, session) = (Arc::clone(rows), id.clone());
    let written = spawn_blocking(move || {
        let mut rows = rows.lock().map_err(|_| ())?;
        rows.turn_end(&session, &end).map(|_| ()).map_err(|_| ())
    })
    .await;
    if !matches!(written, Ok(Ok(()))) {
        eprintln!("vmm: the turn end of {id} was not written");
    }
}

/// Reads the guest events of `events`, the control link `link` of the VM
/// of `id`, until the link ends (plan PR L18b, decisions CH21, CH62 rule
/// 11). A `facade` line goes to `ControlState::report`, which keeps the
/// state for the sleep gate and clears `turn_running` when the facade
/// leaves `running`. A turn event goes to `ControlState::turn`, which sets
/// `turn_running` and the time of the last use (decision CH50 rule 5).
/// Each turn end of either goes to the row (decision CH32 rule 4). The
/// end of the reader drops the stream, so the link closes, and a stop
/// then skips the graceful step (decision CH50 rule 3). An event of a
/// replaced link ends the reader too.
///
/// No take sends `sync` yet, so a `synced` line has no waiter. The reader
/// logs it and drops it (decision CH62 rule 23). Plan PR V9 sends `sync`
/// and hands each `synced` to the take that waits for it.
///
/// A refused line ends the reader (decision CH43 rule 2). It is a guest
/// fault (decision CH54 rule 6): the reader logs the line with `id`, and
/// `stopper` stops the VM of `link` at once. The link is gone, so the
/// stop sends SIGTERM, then SIGKILL. The VM leaves the table, so the next
/// start of the session cold-boots (decision CH19 (5)). A later VM of the
/// session has another link, so the stop leaves it alone.
async fn read_guest<P: VmProcess>(
    mut events: ControlLink<Arc<UnixStream>>,
    (id, link): (SessionId, LinkId),
    stopper: Stopper<P>,
    rows: Arc<Mutex<Rows>>,
) {
    let control = Arc::clone(&stopper.control);
    let lock = || control.lock().unwrap_or_else(PoisonError::into_inner);
    let refused = loop {
        let event = match events.next_event().await {
            Ok(event) => event,
            Err(EventEnd::Error(ControlError::Ended)) => break false,
            Err(EventEnd::Error(error)) => {
                eprintln!("vmm: the control link of {id} closed: {error:?}");
                break false;
            }
            Err(EventEnd::Refused(line)) => {
                let line = String::from_utf8_lossy(&line);
                eprintln!("vmm: the guest of {id} sent a refused line: {line:?}");
                break true;
            }
        };
        let change = match event {
            GuestEvent::Facade(facade) => lock().report(&id, link, facade),
            GuestEvent::Turn(turn) => lock().turn(&id, link, &turn),
            GuestEvent::Synced(_) if lock().link(&id) == Some(link) => {
                eprintln!("vmm: the guest of {id} sent synced, and no sync waits");
                continue;
            }
            GuestEvent::Synced(_) => Change::Stale,
        };
        if change == Change::Stale {
            break false;
        }
        turn_end(&rows, &id, change).await;
    };
    drop(events);
    // A link that is no longer current belongs to a VM that stopped or
    // to a replaced link, so the reader stops nothing.
    let current = if refused {
        lock().refuse(&id, link)
    } else {
        lock().closed(&id, link)
    };
    if !(refused && current) {
        return;
    }
    if let Err(VmStopFailed { pid, error }) = stopper.stop(&id, Some(link)).await {
        eprintln!("vmm: the VM of {id} (pid {pid}) did not stop after a refused line: {error}");
    }
}

/// The owned parts of a stop, so the reader of a control link stops its
/// VM through the same path as the `StopVm` hook (decision CH54 rule 6).
#[derive(Debug)]
struct Stopper<P> {
    table: Table<P>,
    in_use: Arc<Mutex<InUse>>,
    control: Arc<Mutex<ControlState>>,
    run: RunRoot,
}

impl<P: VmProcess> Stopper<P> {
    /// What the slot of `id` holds for a stop. A VM that runs leaves the
    /// table under a `Stopping` slot, so its RAM still counts. With
    /// `Some(link)`, only the VM of the control link `link` counts.
    fn hold(&self, id: &SessionId, link: Option<LinkId>) -> Held<P> {
        let mut table = lock(&self.table);
        match table.get(id) {
            None => Held::Free,
            // Decision CH54 rule 6: the reader of a refused line stops only
            // its own VM. A start takes the slot only after that VM left
            // the table, and a VM of another link is not its own.
            Some(Slot::Starting(..)) if link.is_some() => Held::Free,
            Some(Slot::Running(vm)) if link.is_some_and(|link| vm.link_id != Some(link)) => {
                Held::Free
            }
            Some(Slot::Starting(..) | Slot::Evicting(..)) => Held::Busy,
            Some(Slot::Stopping(.., outcome)) => Held::Joins(outcome.clone()),
            Some(Slot::Running(_)) => {
                let (sender, outcome) = watch::channel(None);
                let mark = |ram, slot| Slot::Stopping(ram, slot, outcome);
                match take_out(&self.table, &mut table, id.clone(), mark) {
                    Some(eviction) => Held::Takes(Box::new(eviction), sender),
                    None => Held::Free,
                }
            }
        }
    }

    /// Stops the VM of `eviction`: the graceful stop on its control link
    /// first (decision CH19 (3)). A VM that did not stop goes back with no
    /// link, so admission counts its RAM (decision CH50 rule 4). A drop of
    /// this future puts the VM back too, after SIGKILL (`Eviction`).
    async fn stop_taken(&self, mut eviction: Eviction<P>) -> Result<(), VmStopFailed> {
        let Some(vm) = eviction.vm.as_mut() else {
            return Ok(());
        };
        match stop(vm.link.as_ref(), &mut vm.process, &vm.run).await {
            Ok(_) => {
                forget_link(&self.control, &eviction.id);
                eviction.done(&self.in_use, &self.run);
                Ok(())
            }
            Err(error) => {
                let pid = vm.process.pid();
                vm.link = None;
                eviction.put_back();
                let error = Arc::new(error);
                Err(VmStopFailed { pid, error })
            }
        }
    }

    /// Stops the VM of `id`. With `Some(link)`, it stops only the VM of
    /// the control link `link`, for the reader of that link (decision
    /// CH54 rule 6). A start or a retire that holds the slot ends first,
    /// so the stop finds the VM that the start committed or the retire put
    /// back. A stop that finds a stop of the same VM waits for it and
    /// returns its outcome (caller duty 6 of `AGENTS.md`).
    // A start under the mark of a drop ends before the stop (plan PR L15),
    // but its launch step can outlive a dropped start future. So the stop
    // polls a `Starting` slot until the launch output kills the VM and
    // puts it in the table.
    async fn stop(&self, id: &SessionId, link: Option<LinkId>) -> Result<(), VmStopFailed> {
        loop {
            match self.hold(id, link) {
                Held::Free => return Ok(()),
                Held::Busy => sleep(SLOT_POLL).await,
                Held::Joins(mut outcome) => {
                    // A stop whose future was dropped sends no outcome. Its
                    // `Eviction` put the VM back, so the slot is read again.
                    let done = outcome.wait_for(Option::is_some).await;
                    if let Some(result) = done.ok().and_then(|done| done.clone()) {
                        return result;
                    }
                }
                Held::Takes(eviction, sender) => {
                    let result = self.stop_taken(*eviction).await;
                    sender.send_replace(Some(result.clone()));
                    return result;
                }
            }
        }
    }
}

impl<L: Launcher, A: ChApi> ColdBoot<L, A> {
    /// The owned parts of a stop, for the reader of a control link.
    fn stopper(&self) -> Stopper<L::Process> {
        Stopper {
            table: Arc::clone(&self.table),
            in_use: Arc::clone(&self.in_use),
            control: Arc::clone(&self.control),
            run: self.config.run.clone(),
        }
    }
}

impl<L: Launcher, A: ChApi> StopVm for ColdBoot<L, A> {
    /// Stops the VM of `id` (plan PR L18b, decisions CH19 (3), CH33 rule
    /// 11). A start or a retire that holds the slot ends first, so the
    /// stop finds the VM that the start committed or the retire put back.
    /// A stop that finds a stop of the same VM waits for it and returns
    /// its outcome (caller duty 6 of `AGENTS.md`).
    async fn stop(&self, id: &SessionId) -> Result<(), VmStopFailed> {
        self.stopper().stop(id, None).await
    }
}

impl<L: Launcher, A: ChApi> VmSet for ColdBoot<L, A> {
    fn refuse_starts(&self) {
        let _table = lock(&self.table);
        self.refusing.store(true, Ordering::SeqCst);
    }

    fn sessions(&self) -> Vec<SessionId> {
        lock(&self.table).keys().cloned().collect()
    }

    async fn close_door(&self, id: &SessionId) {
        self.relays.close(id).await;
    }

    /// Retires the VM of `id` as an eviction does, when the gate of
    /// admission passes after its door closed (plan PR L19, decision CH19
    /// (6)). A VM that a start, a retire or a stop holds is skipped, and
    /// the stop of the shutdown waits for that slot.
    async fn take(&self, id: &SessionId) -> Taken {
        // Caller duty 7: with no device read or no guest sync no take
        // runs, so each VM gets the graceful stop of the `Stopper`.
        if !(A::READS_DEVICES && A::GUEST_SYNCS) {
            return Taken::Skipped;
        }
        let eviction = {
            let mut table = lock(&self.table);
            let now = Instant::now();
            let passes = matches!(
                table.get(id),
                Some(Slot::Running(vm)) if self.gates(id, vm, now).evict
            );
            if !passes {
                return Taken::Skipped;
            }
            take_out(&self.table, &mut table, id.clone(), Slot::Evicting)
        };
        let Some(eviction) = eviction else {
            return Taken::Skipped;
        };
        match self.retirer().retire(eviction).await {
            Retired::Slept => Taken::Slept,
            Retired::Stopped => Taken::Stopped,
            // A turn started, or the stop failed: the VM is back.
            Retired::Kept => Taken::Skipped,
        }
    }
}

/// The fault text of a failed dial of guest port 1201.
fn dial_text(error: ControlError) -> String {
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

/// Step 3: the link of `vm/<id>` to `slot`, the files of the boot, the
/// restore or cold boot choice, then the launch in `slot`.
fn launch<L: Launcher, A: ChApi>(
    config: &StartConfig,
    launcher: &L,
    locks: (&Mutex<()>, &Mutex<InUse>),
    (id, slot): (&SessionId, PoolSlot),
    shape: FamilyShape,
) -> Result<Booted<L::Process>, String> {
    // Decision CH60 rule 1: with no vmm unit to bind the scope to, the
    // launch stops here and touches no file.
    let unit = config.vmm_unit.clone().map_err(|error| error.to_string())?;
    // Caller duty (a) of PR D16: the link comes before `remove_stale`, so
    // an old link to a slot that another VM took removes nothing of that
    // VM. The slot is free (caller duty (c)), so the link may empty it
    // (decision CH56 rule 1). A restore links the `vm/<id>` of its
    // snapshot to the new slot, and each path in the snapshot holds.
    let run = config
        .run
        .link_vm(id, slot)
        .map_err(|error| format!("{error}: {}", error.source))?;
    remove_stale(&run).map_err(|error| format!("{error}: {}", error.source))?;
    let checked = |digest| {
        config
            .images
            .check(digest)
            .map_err(|error| error.to_string())
    };
    let kernel = checked(config.digests.kernel)?;
    let initramfs = config.initramfs.map(checked).transpose()?;
    // `PerFlavor` of the wire crate holds each flavor, so the vmm holds no
    // list of its own (decision CH58 rule 4).
    let root = checked(*config.digests.roots.get(shape.flavor))?;
    let disk = config
        .disks
        .for_boot(id)
        .map_err(|error| error.to_string())?;
    let session = config.state.session(id);
    // The ruleset opens each path, and a missing one fails the launch.
    // `link_vm` made the slot dir of `vm/<id>`, and `for_boot` made the
    // session dir. The serial log of the last start stays until here, also
    // across a vmm restart (decision CH59 rule 2). A restore opens the
    // same path from its snapshot config.
    make_dir(&session.snapshots())?;
    let serial =
        SerialLog::fresh(&session).map_err(|error| format!("{error}: {}", error.source))?;
    let limits = VmLimits::from(shape);
    let key = SnapshotKey::current(config.ch_version, &kernel, initramfs.as_ref(), &root);
    // The initramfs of the ruleset. A snapshot with the current key names
    // this same `store/<sha256>` file, so a restore reads it as a boot
    // does (decision CH61 rule 3).
    let initramfs_path = initramfs
        .as_ref()
        .map(|image| host(image.path()))
        .transpose()?;
    let (command, start, restore) = match claim_newest::<A>(&session, &key, locks, id)? {
        Some(snapshot) => {
            let command = ChCommand::restore(snapshot, &run).map_err(|error| error.to_string())?;
            let expected = Expected {
                root: root.path().to_owned(),
                session: disk.path().to_owned(),
                vsock: run.vsock_socket(),
            };
            let start = Start::Restore {
                initramfs: initramfs_path,
            };
            (command, start, Some(expected))
        }
        None => {
            let boot = Boot {
                kernel: &kernel,
                initramfs: initramfs.as_ref(),
                root: &root,
                session: &disk,
                serial: &serial,
                disk: DiskLimit::SESSION,
                limits,
            };
            let command = ChCommand::boot(boot, &run).map_err(|error| error.to_string())?;
            let start = Start::Boot {
                kernel: host(kernel.path())?,
                initramfs: initramfs_path,
            };
            (command, start, None)
        }
    };
    let files = VmFiles::new(start.clone(), host(root.path())?, &session, &run)
        .map_err(|error| error.to_string())?;
    let payload = command.payload(config.ch.clone());
    let scope = limits.scope(id.clone());
    let plan = Launch::new(config.systemd_run.clone(), scope, payload, files, unit).in_slot(slot);
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
            // Plan PR L19: a start during the shutdown gets it too.
            Ok(Found::Evicting | Found::Refused) => return Err(StartFault::HostFull),
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
        let slot = pending.slot;
        let launched = blocking(move || {
            let locks = (&*claims, &*in_use);
            let booted = launch::<L, A>(&config, &*launcher, locks, (&session, slot), shape)?;
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
        turn_end(&self.rows, id, change).await;
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
        // The reader owns the stream, and the table keeps the sender of the
        // graceful stop (decision CH19 (3)).
        let (sender, events) = control.split();
        let (stopper, rows) = (self.stopper(), Arc::clone(&self.rows));
        tokio::spawn(read_guest(events, (id.clone(), link), stopper, rows));
        pending.commit(sender, link);
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

    /// The check of a claim needs only `READS_DEVICES`. Each call fails.
    struct NoDevices;

    impl crate::restore::ChApi for NoDevices {
        const READS_DEVICES: bool = false;
        const GUEST_SYNCS: bool = false;

        fn info(
            &self,
            _vm: &creche_vmm_store::layout::VmRunDir,
        ) -> Result<creche_vmm_store::ch_remote::VmInfo, creche_vmm_store::ch_remote::ChRemoteError>
        {
            Err(creche_vmm_store::ch_remote::ChRemoteError::BadDestination(
                PathBuf::new(),
            ))
        }

        fn resume(&self, _vm: &creche_vmm_store::layout::VmRunDir) -> Result<(), String> {
            Err("no resume".to_owned())
        }

        fn snapshot(
            &self,
            _vm: &creche_vmm_store::layout::VmRunDir,
            _session: &creche_vmm_store::layout::SessionDir,
            _key: &SnapshotKey,
        ) -> Result<SnapshotGeneration, String> {
            Err("no snapshot".to_owned())
        }
    }

    const EMPTY: &str = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855";

    /// With no device check, the start cold-boots, and the claim still
    /// consumes the snapshot, because the cold boot moves the session disk
    /// on (decisions CH19 (5), CH50 rule 10). `ChTools` reads the device
    /// set (PR D15), so its claim gives the snapshot and keeps the
    /// generation in use (decision CH50 rule 8).
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
        let key = SnapshotKey::current(ch, &empty, None, &empty);
        let id: SessionId = "3f2b8c1e-9d4a-4e6b-8a7c-0b1d2e3f4a5b".parse().unwrap();
        let session = state.session(&id);
        let generation = SnapshotGeneration(1);
        fs::create_dir_all(session.snapshot(generation)).unwrap();
        write_key(&session, generation, &key).unwrap();

        let in_use = Mutex::new(InUse::new());
        let locks = (&Mutex::new(()), &in_use);
        let claimed = claim_newest::<ChTools>(&session, &key, locks, &id).unwrap();
        assert!(claimed.is_some());
        assert!(in_use.lock().unwrap().contains(&(id.clone(), generation)));

        let generation = SnapshotGeneration(2);
        fs::create_dir_all(session.snapshot(generation)).unwrap();
        write_key(&session, generation, &key).unwrap();
        let in_use = Mutex::new(InUse::new());
        let locks = (&Mutex::new(()), &in_use);
        let claimed = claim_newest::<NoDevices>(&session, &key, locks, &id).unwrap();
        assert!(claimed.is_none());
        assert!(session.snapshot(generation).join("consumed").is_file());
        assert!(in_use.lock().unwrap().is_empty());
        let _ = fs::remove_dir_all(&dir);
    }
}
