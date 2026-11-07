//! The edit follow of a running VM (plan PR E3, decisions CH6-3, CH49
//! rules 3 and 4, CH69 rules 11, 12, 13 and 28, CH72 rule 3, CH73 rule
//! 1). caregiver edits `egress.json` and `repos.json` of a family while
//! its VMs run. One task per VM follows both files:
//!
//! 1. Every `POLL` it reads both files with the readers of `FamilyRoot`,
//!    in `spawn_blocking` (decisions CH36 G21, CH69 rule 11). A reading
//!    equal to the last one changes nothing.
//! 2. A new reading binds or removes the listeners of host ports 1103 and
//!    1104 by the rule of the start (`Reach`): a list that holds an
//!    entry binds, and so does a present file that does not read. An
//!    absent file or an empty list binds none (decisions CH49 rule 3,
//!    CH69 rule 13, CH72 rule 3, CH73 rule 1). A new listener gets the
//!    server of the start. A removed listener loses its server, which
//!    closes its open links, and then its socket file.
//! 3. Then `deny::check` runs with the bound set of the new reading
//!    (decisions CH6-3, CH49 rule 3). A failed check ends the task, and
//!    the caller writes `egress_assert_failed` and stops the VM (decision
//!    CH69 rule 12).
//!
//! A removed host or repo does not wait for the poll: the proxy and the
//! mirror read their file at each link (decisions CH6-4, CH69 rule 16).
//! An added one waits one `POLL` at most. Cloud Hypervisor connects each
//! guest dial to `vsock.sock_<port>` at the dial (decision CH24), so it
//! finds a listener that the follow bound after a boot or a restore
//! (decision CH49 rule 4). `tests/kvm_reach.rs` proves both, so the
//! fallback of CH49 rule 4 (a bound set fixed per boot) is not taken.
//!
//! The drop of `Watch` ends the task and drops both servers. The VM entry
//! of the start table holds it, so the follow ends with the VM.

use std::io;
use std::path::PathBuf;
use std::sync::Arc;
use std::time::Duration;

use creche_vmm_confine::deny::check;
use creche_vmm_confine::family::FamilyRoot;
use creche_vmm_confine::landlock::Start;
use creche_vmm_confine::launch::Program;
use creche_vmm_store::layout::VmRunDir;
use creche_wire::{FamilyEgress, FamilyName, FamilyRepos, HostPort, SessionId};
use tokio::task::{JoinHandle, spawn_blocking};
use tokio::time::sleep;

use crate::egress::log::Log;
use crate::egress::proxy;
use crate::mirror::{self, Mirror};
use crate::planes::Upstreams;
use crate::start::blocking;

/// The pause between two reads of the reach files of one VM (decision
/// CH69 rule 11).
pub const POLL: Duration = Duration::from_secs(2);

/// What the two reach files of a family say. `None` is a file that is
/// present and does not read.
// The follow compares readings, not bytes (Q61-11 option A says bytes).
// The reader of `FamilyRoot` is the one checked open of each file, and
// only the reading changes a listener. caregiver writes one canonical
// form (decision CH69 rule 5), so a new list is new bytes and a new
// reading.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct Reach {
    repos: Option<FamilyRepos>,
    egress: Option<FamilyEgress>,
}

impl Reach {
    /// Reads `repos.json` and `egress.json` of `family` in `families`. It
    /// blocks.
    pub(crate) fn read(families: &FamilyRoot, family: &FamilyName) -> Self {
        Self {
            repos: families.read_repos(family).ok(),
            egress: families.read_egress(family).ok(),
        }
    }

    /// Whether the VM gets the mirror listener. A clone list that holds a
    /// repo binds 1103 (decision CH69 rule 13). A present `repos.json`
    /// that does not read binds it too, so each clone gets
    /// `mirror::DENIED` and one stderr line that names `not_valid`. A
    /// zero-byte file does not read (decision CH73 rule 1).
    pub(crate) fn mirror(&self) -> bool {
        self.repos
            .as_ref()
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

/// The served reach listeners of one VM: the mirror and the egress proxy,
/// each only while the reading binds it.
#[derive(Debug)]
pub(crate) struct Served {
    pub(crate) mirror: Option<mirror::Server>,
    pub(crate) egress: Option<proxy::Server>,
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
/// task drops the served reach listeners.
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

    /// Reads both files every `POLL`, and after a new reading moves the
    /// listeners and runs the deny check. It returns the text of the
    /// first fault, and its end drops `served`.
    async fn run(self, mut reach: Reach, mut served: Served) -> String {
        loop {
            sleep(POLL).await;
            let (families, family) = (self.families.clone(), self.family.clone());
            // A read that did not end, as at the end of the runtime, is
            // tried again at the next poll.
            let Ok(now) = spawn_blocking(move || Reach::read(&families, &family)).await else {
                continue;
            };
            if now == reach {
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

    /// Binds or removes the mirror and the egress listener to match `now`.
    // CONTRACT-QUESTION: CH69 rule 12 decides a failed deny check after
    // an edit. No row decides a failed bind or serve of an added
    // listener, or a failed removal of a socket file. Reading taken: each
    // ends the follow as a failed check, so the VM faults
    // `egress_assert_failed` and stops. A path that is there fails the
    // bind, and the deny check reads only names, so it would pass a
    // listener that the vmm did not bind. Cost of a change: one branch
    // here (Known gap 30 of AGENTS.md).
    // CONTRACT-QUESTION: fact 43 and the P1 draft of contract 01 section
    // 3.7 say that open connections finish after a removal. The plan PR
    // E3 entry drops the server when a list turns empty, and that drop
    // cuts each open tunnel and clone of the VM. Reading taken: the plan
    // entry, which fails closed. A removal that leaves an entry in the
    // list keeps each open link (the proxy and the mirror read the list
    // per link). Cost of a change: a close of the listener only in
    // `proxy::Server` and `mirror::Server`, which then wait for their
    // links (Known gap 31 of AGENTS.md).
    async fn apply(&self, now: &Reach, served: &mut Served) -> Result<(), String> {
        let run = self.run.clone();
        if now.mirror() && served.mirror.is_none() {
            let bound = blocking(move || {
                mirror::bind(&run).map_err(|error| format!("{error}: {}", error.source))
            })
            .await?;
            let (families, family, session) = self.names();
            let server = bound.serve(self.mirror.clone(), families, family, session);
            served.mirror = Some(server.map_err(|error| format!("serving 1103: {error}"))?);
        } else if !now.mirror()
            && let Some(server) = served.mirror.take()
        {
            drop(server);
            self.remove(HostPort::GitMirror).await?;
        }
        let run = self.run.clone();
        if now.egress() && served.egress.is_none() {
            let bound = blocking(move || {
                proxy::bind(&run).map_err(|error| format!("{error}: {}", error.source))
            })
            .await?;
            let (families, family, session) = self.names();
            let server = bound.serve(families, family, session, Arc::clone(&self.log));
            served.egress = Some(server.map_err(|error| format!("serving 1104: {error}"))?);
        } else if !now.egress()
            && let Some(server) = served.egress.take()
        {
            drop(server);
            self.remove(HostPort::Egress).await?;
        }
        Ok(())
    }

    /// The families dir, the family and the session, for a server.
    fn names(&self) -> (FamilyRoot, FamilyName, SessionId) {
        (
            self.families.clone(),
            self.family.clone(),
            self.session.clone(),
        )
    }

    /// Removes the socket file of the listener of `port`, whose server
    /// the caller dropped. A file that is gone already is no fault.
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
