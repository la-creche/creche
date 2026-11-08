//! The edit follow of a running VM (plan PR E3, decisions CH6-3, CH49
//! rules 3 and 4, CH69 rules 11, 12, 13 and 28, CH72 rule 3, CH73 rule
//! 1, CH74 rules 1 and 2). caregiver edits `egress.json` and `repos.json`
//! of a family while its VMs run. One task per VM follows both files:
//!
//! 1. Every `POLL` it reads the bytes of both files with the byte readers
//!    of `FamilyRoot`, in `spawn_blocking` (decisions CH36 G21, CH69 rule
//!    11). Bytes equal to the last ones change nothing. New bytes are a
//!    new reading, also when they read as the same list, or when they do
//!    not read before or after. A read that takes no bytes (a symlink, a
//!    file that is not regular, a file past its cap, an error) has nothing
//!    to compare, so it is a new reading at each poll.
//! 2. A new reading binds or removes the listeners of host ports 1103 and
//!    1104 by the rule of the start (`Reach`): a list that holds an
//!    entry binds, and so does a present file that does not read. An
//!    absent file or an empty list binds none (decisions CH49 rule 3,
//!    CH69 rule 13, CH72 rule 3, CH73 rule 1). A new listener gets the
//!    server of the start. A removed listener closes, and then its socket
//!    file goes, so no new link starts. Each open tunnel or clone runs to
//!    its own end (decision CH74 rule 2, contract 01 section 3.7 rule 11,
//!    fact 43). A listener that binds again while old links drain gets
//!    those links, so they count against its link cap.
//! 3. Then `deny::check` runs with the bound set of the new reading
//!    (decisions CH6-3, CH49 rule 3). A failed check ends the task, and
//!    the caller writes `egress_assert_failed` and stops the VM (decision
//!    CH69 rule 12). A failed bind or serve of a listener that the reading
//!    adds, or a failed removal of a socket file, does the same (decision
//!    CH74 rule 1).
//!
//! A removed host or repo does not wait for the poll: the proxy and the
//! mirror read their file at each link (decisions CH6-4, CH69 rule 16).
//! An added one waits one `POLL` at most. The follow assumes that Cloud
//! Hypervisor connects each guest dial to `vsock.sock_<port>` at the dial
//! (decision CH24), so it finds a listener that the follow bound after a
//! boot or a restore (decision CH49 rule 4). The CH49 rule 4 spike of
//! decision CH69 rule 28 checks that assumption: the kvm tests
//! `kvm_reach_binds_1104_after_an_egress_edit` and
//! `kvm_reach_binds_1104_after_a_restore_and_an_edit` of
//! `tests/kvm_reach.rs`. No kvm run has passed them yet (plan risk 6). If
//! the restore test fails, plan PR E3 takes the fallback of CH49 rule 4:
//! a bound set fixed per boot and restore.
//!
//! The drop of `Watch` ends the task and drops both listeners, with each
//! link that still drains. The VM entry of the start table holds it, so
//! the follow ends with the VM, and no link outlives the VM.

use std::io;
use std::mem;
use std::path::PathBuf;
use std::sync::Arc;
use std::time::Duration;

use creche_vmm_confine::deny::check;
use creche_vmm_confine::family::{FamilyRoot, ReachBytes};
use creche_vmm_confine::landlock::Start;
use creche_vmm_confine::launch::Program;
use creche_vmm_store::layout::VmRunDir;
use creche_wire::{FamilyEgress, FamilyName, FamilyRepos, HostPort, SessionId};
use tokio::task::{JoinHandle, spawn_blocking};
use tokio::time::sleep;

use crate::egress::log::Log;
use crate::egress::proxy;
use crate::mirror::{self, Mirror};
use crate::planes::{BindError, Upstreams};
use crate::start::blocking;

/// The pause between two reads of the reach files of one VM (decision
/// CH69 rule 11).
pub const POLL: Duration = Duration::from_secs(2);

/// What the follow compares of the two reach files of a family: the bytes
/// of each (decision CH69 rule 11). `None` is a read that took no bytes.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct Reach {
    repos: Option<ReachBytes<FamilyRepos>>,
    egress: Option<ReachBytes<FamilyEgress>>,
}

impl Reach {
    /// Reads the bytes of `repos.json` and `egress.json` of `family` in
    /// `families`. It blocks.
    pub(crate) fn read(families: &FamilyRoot, family: &FamilyName) -> Self {
        Self {
            repos: families.read_repos_bytes(family).ok(),
            egress: families.read_egress_bytes(family).ok(),
        }
    }

    /// Whether this reading has the bytes of `last`. A read that took no
    /// bytes has nothing to compare, so it is never the same.
    fn same(&self, last: &Self) -> bool {
        self.repos.is_some() && self.egress.is_some() && self == last
    }

    /// Whether the VM gets the mirror listener. A clone list that holds a
    /// repo binds 1103 (decision CH69 rule 13). A present `repos.json`
    /// that does not read binds it too, so each clone gets
    /// `mirror::DENIED` and one stderr line that names `not_valid`. A
    /// zero-byte file does not read (decision CH73 rule 1).
    pub(crate) fn mirror(&self) -> bool {
        self.repos
            .as_ref()
            .and_then(|bytes| bytes.list().ok())
            .is_none_or(|list| !list.repos.names().is_empty())
    }

    /// Whether the VM gets the egress proxy listener. A list that holds a
    /// host binds 1104 (decision CH49 rule 3). A present `egress.json`
    /// that does not read binds it too, so each CONNECT gets 403 and a
    /// `not_valid` log line. A zero-byte file does not read (decision
    /// CH72 rule 3).
    pub(crate) fn egress(&self) -> bool {
        self.egress
            .as_ref()
            .and_then(|bytes| bytes.list().ok())
            .is_none_or(|list| !list.hosts.is_empty())
    }

    /// The bound set of the deny check: the family-state port 1100 (plan
    /// PR V8), the plane ports of `planes` (plan PR V6), and 1103 and 1104
    /// when this reading binds each (decisions CH6-3, CH49 rule 3, CH69
    /// rule 13).
    pub(crate) fn ports(&self, planes: Upstreams) -> Vec<HostPort> {
        let mut ports = vec![HostPort::FamilyState];
        ports.extend(planes.rows().map(|(port, _)| port));
        if self.mirror() {
            ports.push(HostPort::GitMirror);
        }
        if self.egress() {
            ports.push(HostPort::Egress);
        }
        ports
    }
}

/// One reach listener of a VM and its links.
#[derive(Debug)]
pub(crate) enum Listener<S, D> {
    /// No listener, and no link drains.
    Unbound,
    /// The served listener, while the reading binds it.
    Open(S),
    /// The links of a closed listener, each until its own end (decision
    /// CH74 rule 2).
    Draining(D),
}

impl<S, D> From<Option<S>> for Listener<S, D> {
    fn from(server: Option<S>) -> Self {
        server.map_or(Self::Unbound, Self::Open)
    }
}

/// The reach listeners of one VM: the mirror and the egress proxy.
#[derive(Debug)]
pub(crate) struct Served {
    pub(crate) mirror: Listener<mirror::Server, mirror::Draining>,
    pub(crate) egress: Listener<proxy::Server, proxy::Draining>,
}

/// What the follow of one VM reads, binds, serves and checks.
#[derive(Debug)]
pub(crate) struct Follow {
    /// caregiver's families dir.
    pub(crate) families: FamilyRoot,
    /// The family of the VM.
    pub(crate) family: FamilyName,
    /// The session of the VM, for the servers.
    pub(crate) session: SessionId,
    /// The run dir of the VM, which holds each listener.
    pub(crate) run: VmRunDir,
    /// The planes, for the bound set.
    pub(crate) planes: Upstreams,
    /// The mirror of each clone.
    pub(crate) mirror: Mirror,
    /// The egress log of each CONNECT.
    pub(crate) log: Arc<Log>,
    /// The command line file of the CH process, for the deny check.
    pub(crate) cmdline: PathBuf,
    /// The CH program, for the deny check.
    pub(crate) ch: Program,
    /// The boot or the restore of the VM, for the deny check.
    pub(crate) start: Start,
}

/// The follow task of one VM. Its drop ends the task, and the end of the
/// task drops the reach listeners and each link that drains.
#[derive(Debug)]
pub(crate) struct Watch(JoinHandle<()>);

impl Drop for Watch {
    fn drop(&mut self) {
        self.0.abort();
    }
}

impl Follow {
    /// Follows the reach files from `reach`, the reading of the launch,
    /// with the listeners `served` of that reading. A failed bind, serve
    /// or deny check ends the follow, and `failed` gets its text.
    pub(crate) fn spawn(
        self,
        reach: Reach,
        served: Served,
        failed: impl FnOnce(String) + Send + 'static,
    ) -> Watch {
        Watch(tokio::spawn(async move {
            let message = self.run(reach, served).await;
            failed(message);
        }))
    }

    /// Reads the bytes of both files every `POLL`, and after a new reading
    /// moves the listeners and runs the deny check. It returns the text of
    /// the first fault, and its end drops `served`.
    async fn run(self, mut reach: Reach, mut served: Served) -> String {
        loop {
            sleep(POLL).await;
            let (families, family) = (self.families.clone(), self.family.clone());
            // A read that did not end, as at the end of the runtime, is
            // tried again at the next poll.
            let Ok(now) = spawn_blocking(move || Reach::read(&families, &family)).await else {
                continue;
            };
            if now.same(&reach) {
                continue;
            }
            let moved = self.apply(&now, &mut served).await;
            reach = now;
            let checked = match moved {
                Ok(()) => self.check(&reach).await,
                Err(message) => Err(message),
            };
            if let Err(message) = checked {
                return message;
            }
        }
    }

    /// Binds or closes the mirror and the egress listener to match `now`.
    /// A failed bind, serve or removal is a failed deny check (decision
    /// CH74 rule 1): a path that is there fails the bind, and the deny
    /// check reads only names, so it would pass a listener that the vmm
    /// did not bind. A close keeps each open link to its own end and
    /// removes the socket file, so no new link starts (decision CH74 rule
    /// 2).
    async fn apply(&self, now: &Reach, served: &mut Served) -> Result<(), String> {
        let serving = |port: u16| move |error: io::Error| format!("serving {port}: {error}");
        served.mirror = match (
            now.mirror(),
            mem::replace(&mut served.mirror, Listener::Unbound),
        ) {
            (true, Listener::Unbound) => {
                let (families, family, session) = self.names();
                let bound = self.bind(mirror::bind).await?;
                let server = bound.serve(self.mirror.clone(), families, family, session);
                Listener::Open(server.map_err(serving(1103))?)
            }
            (true, Listener::Draining(links)) => {
                let bound = self.bind(mirror::bind).await?;
                Listener::Open(links.reopen(bound).map_err(serving(1103))?)
            }
            (false, Listener::Open(server)) => {
                let links = server.close().await;
                self.remove(HostPort::GitMirror).await?;
                Listener::Draining(links)
            }
            (_, kept) => kept,
        };
        served.egress = match (
            now.egress(),
            mem::replace(&mut served.egress, Listener::Unbound),
        ) {
            (true, Listener::Unbound) => {
                let (families, family, session) = self.names();
                let bound = self.bind(proxy::bind).await?;
                let server = bound.serve(families, family, session, Arc::clone(&self.log));
                Listener::Open(server.map_err(serving(1104))?)
            }
            (true, Listener::Draining(links)) => {
                let bound = self.bind(proxy::bind).await?;
                Listener::Open(links.reopen(bound).map_err(serving(1104))?)
            }
            (false, Listener::Open(server)) => {
                let links = server.close().await;
                self.remove(HostPort::Egress).await?;
                Listener::Draining(links)
            }
            (_, kept) => kept,
        };
        Ok(())
    }

    /// Binds a listener in the run dir with `bind`, in `spawn_blocking`.
    async fn bind<B: Send + 'static>(
        &self,
        bind: fn(&VmRunDir) -> Result<B, BindError>,
    ) -> Result<B, String> {
        let run = self.run.clone();
        blocking(move || bind(&run).map_err(|error| format!("{error}: {}", error.source))).await
    }

    /// The families dir, the family and the session, for a server.
    fn names(&self) -> (FamilyRoot, FamilyName, SessionId) {
        (
            self.families.clone(),
            self.family.clone(),
            self.session.clone(),
        )
    }

    /// Removes the socket file of the listener of `port`, which the caller
    /// closed. A file that is gone already is no fault.
    async fn remove(&self, port: HostPort) -> Result<(), String> {
        let path = self.run.host_listener(port);
        blocking(move || match std::fs::remove_file(&path) {
            Ok(()) => Ok(()),
            Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(()),
            Err(error) => Err(format!("removing {}: {error}", path.display())),
        })
        .await
    }

    /// The deny check with the bound set of `reach` (decisions CH6-3,
    /// CH49 rule 3). The start runs it once before the 101, and the follow
    /// after each new reading.
    pub(crate) async fn check(&self, reach: &Reach) -> Result<(), String> {
        let (cmdline, ch, start, run) = (
            self.cmdline.clone(),
            self.ch.clone(),
            self.start.clone(),
            self.run.clone(),
        );
        let ports = reach.ports(self.planes);
        blocking(move || {
            check(&cmdline, &ch, &start, &run, &ports).map_err(|error| error.to_string())
        })
        .await
    }
}
