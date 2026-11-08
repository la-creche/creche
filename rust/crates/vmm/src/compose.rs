//! The compose step of `vmm serve` (plan PR L20, decisions CH9, CH20).
//! It builds the vmm from its config and serves until SIGTERM:
//!
//! 1. `shutdown::sigterm` registers the handler of SIGTERM, inside the
//!    runtime and before any other step (caller duty 8 of `AGENTS.md`).
//! 2. In `spawn_blocking`: `leftovers::stop_all` stops each VM scope of an
//!    earlier vmm before any per-VM bind (decision CH19 (4), PR C3). Then
//!    `root_owned::check_program` checks the files of `VMM_CH_PROGRAM`,
//!    `VMM_CH_REMOTE_PROGRAM` and `VMM_GIT_PROGRAM` before any run of one
//!    (decisions CH67 rule 1, CH69 rule 17). Then `check_cloud_hypervisor` checks the CH version
//!    (caller duty 4, gap G10 option A). Then `image_root::read` reads
//!    the `digests.json` of the image root, and a missing, malformed or
//!    writable file refuses the start (decision CH57 rule 1). Each file that it names goes into
//!    the digest store (`Follow::start`). Then `guest_init::write_initramfs` packs the
//!    guest init program into the one initramfs of this vmm start, in the
//!    digest store, and each VM start names it (decisions CH62 rules 17
//!    and 18). Then the fault book starts, which clears every fault file
//!    (contract 05 §3.3.1 rule 8), the rows load, and the egress log
//!    opens (decision CH69 rule 10).
//! 3. The store, the confine parts and `ColdBoot` from the config. One
//!    `ColdBoot` is the `StartVm` hook, the `StopVm` hook, the `VmSet`
//!    and the `Sleeper` of the vmm.
//! 4. `nofile::raise` sets the soft `RLIMIT_NOFILE` to `fd_need`, and a
//!    hard limit below it refuses the start (decision CH67 rule 4, CH68
//!    rule 2, CH69 rule 14). It runs after the checks, so a bad program,
//!    image root or digest names its own fault on a host of any hard
//!    limit. It runs before the bind and the first VM, so each listener
//!    and each CH gets the raised limit.
//! 5. The handler set of `routes!` binds `vmm.sock` in the run root.
//!    `sleep::run`, the refresh of the fault book and the digests follow
//!    (`image_root::follow`, decision CH76 rule 8) run beside it (caller
//!    duty 5).
//! 6. At SIGTERM, the sleep loop gets its stop, and `shutdown::shutdown`
//!    runs with the end of `sleep::run` as its `sleep` future. `main.rs`
//!    then ends the runtime with `shutdown_timeout` (caller duty 8).
//!
//! A fault before the bind refuses the start, and `main.rs` exits 78
//! (owner item 86).

use std::fmt;
use std::future::{Future, poll_fn};
use std::io;
use std::path::PathBuf;
use std::pin::{Pin, pin};
use std::sync::{Arc, Mutex, PoisonError};
use std::task::Poll;
use std::time::{Duration, SystemTime};

use creche_vmm_confine::faults::{FaultBook, FaultDir, REFRESH};
use creche_vmm_confine::leftovers::{LeftoverError, stop_all};
use creche_vmm_confine::nofile::{self, NofileError};
use creche_vmm_confine::spawner::{SELF_EXE, Spawner};
use creche_vmm_confine::vmm_unit::VmmUnit;
use creche_vmm_store::ch_remote::ChRemote;
use creche_vmm_store::images::{ImageDigest, ImageStore};
use creche_vmm_store::key::{ChVersionError, CheckedCh, check_cloud_hypervisor};
use creche_vmm_store::rows::{LoadError, Rows};
use creche_vmm_store::session_disk::SessionDisks;
use creche_vmm_store::snapshot::Snapshotter;
use creche_wire::{
    AcpError, AcpRoute, CheckError, CheckReply, CheckRequest, CheckRoute, CreateError, CreateRoute,
    CreateSession, DropError, DropRoute, DropTarget, Dropped, ListError, ListRoute, ListSessions,
    OpenAcp, Serve, ServeUpgrade, SessionId, SessionPage, SessionRow,
};
use tokio::sync::{oneshot, watch};
use tokio::task::{JoinHandle, spawn_blocking};
use tokio::time::{MissedTickBehavior, interval};

use crate::ServeError;
use crate::config::{CH, CH_REMOTE, Config, EGRESS_LOG, GIT};
use crate::control::ControlState;
use crate::egress::log::Log;
use crate::egress::proxy;
use crate::guest_init::{GuestInitError, write_initramfs};
use crate::handlers::acp::{Acp, StartFault, StartVm};
use crate::handlers::check::Checks;
use crate::handlers::create::Creates;
use crate::handlers::drop::{Drops, StopVm, VmStopFailed};
use crate::handlers::list::Lists;
use crate::image_root::follow::Follow;
use crate::image_root::{self, DigestsError, ImageImportError};
use crate::mirror::{self, Mirror};
use crate::relay::{AcpLink, AcpRelay, Relays};
use crate::restore::ChTools;
use crate::root_owned::{ProgramError, check_program};
use crate::server::{Stop, run_routes};
use crate::shutdown::{self, Ended};
use crate::start::{ColdBoot, Digests, StartConfig};
use crate::{family_state, planes, sleep};

/// The bound of `cloud-hypervisor --version` at the start (decision CH44
/// rule 3: the caller sets each timeout).
// No row sets it. The check runs once per start, and the tests of the
// store use 10 s. `vmm verify` runs the same check with it (decision
// CH76 rule 2), so a change costs one constant.
pub(crate) const VERSION_WAIT: Duration = Duration::from_secs(10);

/// Why `vmm serve` did not start, or stopped before SIGTERM.
#[derive(Debug)]
pub enum StartError {
    /// The handler of SIGTERM did not register.
    Sigterm(io::Error),
    /// The leftover VM scopes did not clear (PR C3).
    Leftovers(LeftoverError),
    /// The file of `VMM_CH_PROGRAM`, `VMM_CH_REMOTE_PROGRAM` or
    /// `VMM_GIT_PROGRAM` is not trusted (decisions CH67 rule 1, CH69 rule
    /// 17).
    Program(ProgramError),
    /// The CH version check failed (gap G10 option A).
    ChVersion(ChVersionError),
    /// The `digests.json` of the image root is not trusted (decision CH57
    /// rule 1).
    Digests(DigestsError),
    /// A file of the image root did not go into the digest store.
    Import(ImageImportError),
    /// The guest init initramfs was not written (decision CH62 rule 17).
    GuestInit(GuestInitError),
    /// The fault dir or the fault book did not start.
    Faults(io::Error),
    /// The rows did not load.
    Rows(LoadError),
    /// The egress log of `VMM_EGRESS_LOG` did not open (decision CH69
    /// rule 10).
    EgressLog(io::Error),
    /// The spawner thread did not start.
    Spawner(io::Error),
    /// The soft `RLIMIT_NOFILE` did not rise to `fd_need` (decision CH67
    /// rule 4).
    Nofile(NofileError),
    /// A blocking step of the start panicked.
    Panicked,
    /// The server stopped before SIGTERM, such as a failed bind.
    Serve(ServeError),
}

impl fmt::Display for StartError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Sigterm(error) => write!(formatter, "cannot register SIGTERM: {error}"),
            Self::Leftovers(error) => write!(formatter, "{error}"),
            Self::Program(error) => write!(formatter, "{error}"),
            Self::ChVersion(error) => write!(formatter, "{error}"),
            Self::Digests(error) => write!(formatter, "{error}"),
            Self::Import(error) => write!(formatter, "{error}"),
            Self::GuestInit(error) => write!(formatter, "{error}"),
            Self::Faults(error) => write!(formatter, "cannot start the fault book: {error}"),
            Self::Rows(error) => write!(formatter, "cannot load the rows: {error:?}"),
            Self::EgressLog(error) => {
                write!(formatter, "{EGRESS_LOG}: cannot open the log: {error}")
            }
            Self::Spawner(error) => write!(formatter, "cannot start the spawner: {error}"),
            Self::Nofile(error) => write!(formatter, "{error}"),
            Self::Panicked => formatter.write_str("a start step panicked"),
            Self::Serve(error) => write!(formatter, "{error}"),
        }
    }
}

impl std::error::Error for StartError {}

/// The `ColdBoot` of the vmm.
type Hook = ColdBoot<Spawner, ChTools>;

/// One `ColdBoot`, shared by the acp and the drop handler, the sleep loop
/// and the shutdown.
#[derive(Debug, Clone)]
struct Shared(Arc<Hook>);

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

/// The handler set of the vmm: one handler for each route of the table.
#[derive(Debug)]
struct Handlers {
    creates: Creates,
    lists: Lists,
    acp: Acp<Shared>,
    drops: Drops<Shared>,
    checks: Checks,
}

impl Serve<CreateRoute> for Handlers {
    async fn serve(&self, request: CreateSession) -> Result<SessionRow, CreateError> {
        self.creates.serve(request).await
    }
}

impl Serve<ListRoute> for Handlers {
    async fn serve(&self, request: ListSessions) -> Result<SessionPage, ListError> {
        self.lists.serve(request).await
    }
}

impl Serve<DropRoute> for Handlers {
    async fn serve(&self, request: DropTarget) -> Result<Dropped, DropError> {
        self.drops.serve(request).await
    }
}

impl Serve<CheckRoute> for Handlers {
    async fn serve(&self, request: CheckRequest) -> Result<CheckReply, CheckError> {
        self.checks.serve(request).await
    }
}

impl ServeUpgrade<AcpRoute> for Handlers {
    type Relay = AcpRelay;

    async fn admit(&self, request: OpenAcp) -> Result<AcpRelay, AcpError> {
        self.acp.admit(request).await
    }
}

crate::routes!(Handlers);

/// The vmm after the start steps, before the bind.
struct Vmm {
    socket: PathBuf,
    handlers: Handlers,
    hook: Shared,
    faults: Arc<Mutex<FaultBook>>,
    follow: Follow,
}

/// Builds the vmm from `config`, then serves until SIGTERM and shuts
/// down. The caller runs it on the multi-thread runtime (decision CH36,
/// gap G21), then ends the runtime with `shutdown_timeout`.
///
/// # Errors
///
/// A `StartError` when a start step fails, or when the server stops
/// before SIGTERM. A shutdown that ends `Kept` or `Late` is no error: the
/// `ExecStopPost=` step of the vmm unit kills each VM that is left after
/// the vmm has exited (decision CH61 rule 1).
pub async fn serve(config: Config) -> Result<(), StartError> {
    let sigterm = shutdown::sigterm().map_err(StartError::Sigterm)?;
    let vmm = spawn_blocking(move || build(config))
        .await
        .map_err(|_| StartError::Panicked)??;
    nofile::raise(fd_need()).map_err(StartError::Nofile)?;
    vmm.run(sigterm).await
}

/// The fds of step 4: the bridges (decision CH67 rule 4), the 1100 links
/// (CH68 rule 2), and the 1103 and 1104 links (CH69 rule 14) of every VM.
/// The soft limit of systemd (1024) holds those of a few busy guests only.
fn fd_need() -> u64 {
    planes::fd_need()
        .saturating_add(family_state::fd_need())
        .saturating_add(mirror::fd_need())
        .saturating_add(proxy::fd_need())
}

/// Step 2 and 3: the blocking start steps, then the parts. Blocks.
fn build(config: Config) -> Result<Vmm, StartError> {
    let checked = check(&config)?;
    parts(config, checked)
}

/// What the checks of step 2 give to the parts.
struct Checked {
    ch_version: CheckedCh,
    images: ImageStore,
    digests: watch::Receiver<Digests>,
    follow: Follow,
    /// The guest init initramfs of this vmm start (CH62 rule 18).
    initramfs: ImageDigest,
}

/// The first checks of step 2: the leftover sweep, the CH, ch-remote and
/// git files, the CH version, the image root and the guest init initramfs.
/// Blocks.
fn check(config: &Config) -> Result<Checked, StartError> {
    for unit in stop_all(&config.systemctl).map_err(StartError::Leftovers)? {
        eprintln!("vmm: stopped the leftover unit {unit}");
    }
    // Decision CH67 rule 1: CH and ch-remote get the check of the guest
    // init program, before the vmm runs either.
    check_program(CH, config.ch.path()).map_err(StartError::Program)?;
    check_program(CH_REMOTE, config.ch_remote.path()).map_err(StartError::Program)?;
    // Decision CH69 rule 17: the git of each clone gets the same check.
    check_program(GIT, config.git.path()).map_err(StartError::Program)?;
    let ch_version =
        check_cloud_hypervisor(config.ch.path(), VERSION_WAIT).map_err(StartError::ChVersion)?;
    let images = ImageStore::new(&config.state, config.zstd.path().to_owned());
    // Decision CH57 rule 1 refuses the start for a bad `digests.json`, and
    // decision CH58 rule 5 for an entry that does not import: a missing
    // disk, a digest mismatch or a failed `zstd -d`. The follow of the
    // file then swaps the set of new starts (decision CH76 rule 8).
    let set = image_root::read(&config.image_root).map_err(StartError::Digests)?;
    let (follow, digests) = Follow::start(set, images.clone()).map_err(StartError::Import)?;
    // Decision CH62 rule 18: one initramfs per vmm start. A program that
    // root does not own, or that others may write, refuses the start
    // (CH62 rule 17).
    let initramfs = write_initramfs(config.guest_init.path(), &images)
        .map_err(StartError::GuestInit)?
        .digest();
    Ok(Checked {
        ch_version,
        images,
        digests,
        follow,
        initramfs,
    })
}

/// The rest of step 2 (the fault book, the rows, the spawner), then step
/// 3. Blocks.
fn parts(config: Config, checked: Checked) -> Result<Vmm, StartError> {
    let Checked {
        ch_version,
        images,
        digests,
        follow,
        initramfs,
    } = checked;
    let faults = FaultDir::open(&config.faults_root)
        .and_then(|dir| FaultBook::start(dir, SystemTime::now()))
        .map_err(StartError::Faults)?;
    let faults = Arc::new(Mutex::new(faults));
    let rows = Rows::open(config.state.clone()).map_err(StartError::Rows)?;
    let rows = Arc::new(Mutex::new(rows));
    let egress_log = Log::open(config.egress_log.clone()).map_err(StartError::EgressLog)?;
    let spawner = Spawner::start(PathBuf::from(SELF_EXE)).map_err(StartError::Spawner)?;

    let remote = || ChRemote::new(config.ch_remote.path().to_owned());
    let api = ChTools {
        remote: remote(),
        snapshotter: Snapshotter::new(remote(), config.fallocate.path().to_owned()),
    };
    // Decision CH60 rule 1: the unit comes from `/proc/self/cgroup`, with
    // no config key. A vmm in no service unit still serves, and each VM
    // launch refuses with the error.
    let vmm_unit = VmmUnit::of_self();
    if let Err(error) = &vmm_unit {
        eprintln!("vmm: {error}, so every VM launch refuses");
    }
    let start = StartConfig {
        images,
        disks: SessionDisks::new(&config.state, config.mke2fs.path().to_owned()),
        digests,
        initramfs,
        state: config.state,
        run: config.run.clone(),
        families: config.families.clone(),
        ch: config.ch,
        systemd_run: config.systemd_run,
        vmm_unit,
        limits: config.limits,
        budget: config.budget,
        ch_version,
        planes: config.planes,
        egress_log: Arc::new(egress_log),
        mirror: Mirror {
            root: config.mirror_root,
            git: config.git,
        },
    };
    let relays = Relays::default();
    let control = Arc::new(Mutex::new(ControlState::default()));
    let hook = Shared(Arc::new(ColdBoot::new(
        start,
        Arc::new(spawner),
        Arc::new(api),
        relays.clone(),
        Arc::clone(&rows),
        control,
        Arc::clone(&faults),
    )));
    let handlers = Handlers {
        creates: Creates::new(Arc::clone(&rows), config.families.clone()),
        lists: Lists::new(Arc::clone(&rows)),
        acp: Acp::new(Arc::clone(&rows), relays.clone(), hook.clone()),
        drops: Drops::new(rows, relays, hook.clone()),
        checks: Checks::new(config.families),
    };
    Ok(Vmm {
        socket: config.run.vmm_socket(),
        handlers,
        hook,
        faults,
        follow,
    })
}

impl Vmm {
    /// Step 5 and 6: serves until SIGTERM, then shuts down.
    async fn run(self, sigterm: impl Future<Output = ()>) -> Result<(), StartError> {
        let Self {
            socket,
            handlers,
            hook,
            faults,
            follow,
        } = self;
        let (stop, signal) = Stop::pair();
        let mut server = tokio::spawn(async move { run_routes(&socket, signal, handlers).await });
        let (sleep_stop, sleep_signal) = oneshot::channel::<()>();
        let sleep = tokio::spawn({
            let hook = hook.clone();
            async move {
                // An Err means the sender dropped, so the loop stops too.
                sleep::run(&*hook.0, async move { _ = sleep_signal.await }).await;
            }
        });
        let refresh = tokio::spawn(refresh_faults(faults));
        let follow = tokio::spawn(follow.run());

        let early = first_of(&mut server, sigterm).await;
        let _ = sleep_stop.send(());
        // The shutdown refuses each new start, so the set of new starts
        // has no reader now. An import that still runs in its blocking
        // thread ends with the process. Its temp file stays until the
        // next import of that digest truncates it.
        follow.abort();
        if let Some(ended) = early {
            refresh.abort();
            return Err(match ended {
                Ok(Err(error)) => StartError::Serve(error),
                Ok(Ok(())) | Err(_) => StartError::Panicked,
            });
        }

        let ended = shutdown::shutdown(&*hook.0, stop, async move { _ = sleep.await }).await;
        if ended == Ended::Kept {
            eprintln!("vmm: a VM did not stop, so the ExecStopPost= step of the vmm unit kills it");
        }
        refresh.abort();
        match server.await {
            Ok(Err(error)) => Err(StartError::Serve(error)),
            Ok(Ok(())) | Err(_) => Ok(()),
        }
    }
}

/// The end of `server` when it ends before `sigterm` fires, or `None` at
/// SIGTERM.
async fn first_of<T>(
    server: &mut JoinHandle<T>,
    sigterm: impl Future<Output = ()>,
) -> Option<Result<T, tokio::task::JoinError>> {
    let mut sigterm = pin!(sigterm);
    poll_fn(|context| {
        if let Poll::Ready(ended) = Pin::new(&mut *server).poll(context) {
            return Poll::Ready(Some(ended));
        }
        sigterm.as_mut().poll(context).map(|()| None)
    })
    .await
}

/// Rewrites each open fault file every `REFRESH`, so `written_at` never
/// passes the 90 s of contract 05 §3.3.1 rule 7 (`creche-vmm-confine`
/// AGENTS.md). Each write runs in `spawn_blocking` (decision CH36 G21).
async fn refresh_faults(faults: Arc<Mutex<FaultBook>>) {
    let mut tick = interval(REFRESH);
    tick.set_missed_tick_behavior(MissedTickBehavior::Delay);
    loop {
        tick.tick().await;
        let faults = Arc::clone(&faults);
        let written = spawn_blocking(move || {
            let book = faults.lock().unwrap_or_else(PoisonError::into_inner);
            book.refresh(SystemTime::now())
        })
        .await;
        match written {
            Ok(Ok(())) => {}
            Ok(Err(error)) => eprintln!("vmm: the fault refresh failed: {error}"),
            Err(_) => eprintln!("vmm: the fault refresh panicked"),
        }
    }
}

#[cfg(test)]
mod tests;
