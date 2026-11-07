//! The plane bridges on host ports 1101 and 1102 (plan PR V6, decisions
//! CH6-2, CH14). Guest init splices guest 127.0.0.1:4000 and :8300 to a
//! vsock dial of host port 1101 and 1102. Cloud Hypervisor turns each such
//! dial into a connect to `vsock.sock_<port>` in the run dir of the VM
//! (decision CH24). The vmm listens there and splices each connection to
//! a new TCP connect to the plane: LiteLLM for 1101, the chaperone for
//! 1102 (decision CH62 rule 20). It reads no byte, looks up no allowlist
//! and rewrites nothing, so the Host header stays valid (fact 46).
//!
//! 1. `bind` binds both listeners in the run dir of a VM. The start runs
//!    it after `RunRoot::link_vm` emptied the slot (decision CH56 rule 1)
//!    and after `remove_stale`, before CH starts, for a cold boot and for
//!    each restore. A restored guest dials again (decision CH62 rule 14),
//!    so the bridge needs no state of the old VM (fact 45).
//! 2. `Bound::serve` gives each listener a task of its own. Each accepted
//!    connection gets a fresh connect to its plane within `CONNECT_WAIT`.
//!    A connect that fails or that takes longer closes the guest side, and
//!    nothing retries (decision CH62 rule 14). Guest clients treat the
//!    close as transient (decision CH20).
//! 3. Each listener holds at most `SPLICE_CAP` open splices (decision CH62
//!    rule 14). A connection past the cap closes at once.
//! 4. The drop of `Planes` closes both listeners and every open splice.
//!    The VM entry of the start table holds it, so the bridges go with the
//!    VM.
//!
//! Host port 1100 is `family_state` (plan PR V8), 1103 is `mirror` (plan
//! PR M1, decision CH69 rule 13), and 1104 is `egress::proxy` (plan PR
//! E2, decision CH49 rule 3).

use std::fmt;
use std::future::poll_fn;
use std::io;
use std::net::SocketAddr;
use std::os::unix::net::UnixListener as StdListener;
use std::path::PathBuf;
use std::time::Duration;

use creche_vmm_store::layout::{SLOTS, VmRunDir};
use creche_wire::HostPort;
use tokio::net::{TcpStream, UnixListener, UnixStream};
use tokio::task::{JoinHandle, JoinSet};
use tokio::time::{sleep, timeout};

use crate::relay::Splice;
use crate::server::ACCEPT_PAUSE;

/// The most open splices of one bridge row of one VM (decision CH62 rule
/// 14).
pub const SPLICE_CAP: usize = 64;

/// The bound of the connect to a plane (decision CH62 rule 14).
pub const CONNECT_WAIT: Duration = Duration::from_secs(5);

/// The bridge rows of each VM: 1101 and 1102.
const ROWS: usize = 2;

/// The fds that the vmm holds beside the plane bridges: `vmm.sock` and
/// its links, the links and run files of each VM, the store and the CH
/// children (decision CH67 rule 4).
const FD_HEADROOM: u64 = 4096;

/// The fds that the vmm needs when each slot holds a VM whose bridge rows
/// are full: per row its listener, and a Unix and a TCP stream per splice,
/// plus `FD_HEADROOM`. `vmm serve` raises its soft `RLIMIT_NOFILE` to it
/// (`creche_vmm_confine::nofile`), so a busy guest cannot use the fds of
/// every other VM.
#[must_use]
pub fn fd_need() -> u64 {
    let streams = u64::try_from(SPLICE_CAP).map_or(u64::MAX, |cap| cap.saturating_mul(2));
    let rows = u64::try_from(ROWS).unwrap_or(u64::MAX);
    streams
        .saturating_add(1)
        .saturating_mul(rows)
        .saturating_mul(u64::from(SLOTS))
        .saturating_add(FD_HEADROOM)
}

/// The address of each plane (decision CH62 rule 20): the keys
/// `VMM_LITELLM_ADDR` and `VMM_CHAPERONE_ADDR`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Upstreams {
    /// LiteLLM, for host port 1101.
    pub litellm: SocketAddr,
    /// The chaperone, for host port 1102.
    pub chaperone: SocketAddr,
}

impl Upstreams {
    /// Each bridge row: the host port and the address of its plane
    /// (decisions CH6-2, CH14). Its ports are the bound set of the deny
    /// check (decision CH6-3).
    #[must_use]
    pub fn rows(self) -> [(HostPort, SocketAddr); ROWS] {
        [
            (HostPort::Litellm, self.litellm),
            (HostPort::Chaperone, self.chaperone),
        ]
    }
}

/// Why `bind` failed.
#[derive(Debug)]
pub struct BindError {
    /// The listener path.
    pub path: PathBuf,
    /// The error of the bind.
    pub source: io::Error,
}

impl fmt::Display for BindError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(formatter, "binding {}", self.path.display())
    }
}

impl std::error::Error for BindError {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        Some(&self.source)
    }
}

/// The listeners of one VM, bound and not yet served. A connection that
/// the guest makes before `serve` waits in the backlog. The drop closes
/// both listeners.
#[derive(Debug)]
pub struct Bound(Vec<(StdListener, SocketAddr)>);

/// Binds the listener `vsock.sock_<port>` of each row of `upstreams` in
/// `run` (decision CH24). It blocks, so the start runs it in
/// `spawn_blocking`. The slot is empty, so a path that is there fails the
/// bind.
///
/// # Errors
///
/// A `BindError` for the first listener that did not bind.
pub fn bind(run: &VmRunDir, upstreams: Upstreams) -> Result<Bound, BindError> {
    let mut bound = Vec::new();
    for (port, upstream) in upstreams.rows() {
        let path = run.host_listener(port);
        let listener = StdListener::bind(&path)
            .and_then(|listener| listener.set_nonblocking(true).map(|()| listener))
            .map_err(|source| BindError { path, source })?;
        bound.push((listener, upstream));
    }
    Ok(Bound(bound))
}

impl Bound {
    /// Serves each listener in a task of its own.
    ///
    /// # Errors
    ///
    /// The error of a listener that the runtime did not take. The tasks
    /// that started end with the drop of the partial `Planes`.
    pub fn serve(self) -> io::Result<Planes> {
        let mut planes = Planes(Vec::new());
        for (listener, upstream) in self.0 {
            let listener = UnixListener::from_std(listener)?;
            planes.0.push(tokio::spawn(bridge(listener, upstream)));
        }
        Ok(planes)
    }
}

/// The served bridges of one VM. Its drop ends each listener task, and the
/// end of a task drops its listener and its open splices.
#[derive(Debug)]
pub struct Planes(Vec<JoinHandle<()>>);

impl Drop for Planes {
    fn drop(&mut self) {
        for task in &self.0 {
            task.abort();
        }
    }
}

/// Accepts on `listener` until its task ends, and splices each connection
/// to `upstream`.
// Decision CH40 rule 1, as on vmm.sock: a failed accept, such as EMFILE,
// is logged by kind, and the loop pauses ACCEPT_PAUSE and goes on.
async fn bridge(listener: UnixListener, upstream: SocketAddr) {
    let mut splices = JoinSet::new();
    loop {
        let accepted = listener.accept().await;
        while splices.try_join_next().is_some() {}
        match accepted {
            Ok((guest, _)) if splices.len() < SPLICE_CAP => {
                splices.spawn(splice(guest, upstream));
            }
            // Decision CH67 rule 3: a connection past the cap of CH62
            // rule 14 closes at once, and the client retries.
            Ok((guest, _)) => drop(guest),
            Err(error) => {
                eprintln!("vmm: a plane accept failed: {}", error.kind());
                sleep(ACCEPT_PAUSE).await;
            }
        }
    }
}

/// Connects to `upstream` and copies bytes both ways until both sides
/// close or one fails. A connect that fails or passes `CONNECT_WAIT` drops
/// `guest`, which closes the guest side (decision CH62 rule 14).
async fn splice(mut guest: UnixStream, upstream: SocketAddr) {
    let Ok(Ok(mut plane)) = timeout(CONNECT_WAIT, TcpStream::connect(upstream)).await else {
        return;
    };
    let mut splice = Splice::new(Vec::new());
    poll_fn(|context| splice.carry(context, &mut guest, &mut plane)).await;
}
