//! The family-state reply on host port 1100 (plan PR V8, decisions CH5a,
//! CH14, CH23). The facade pulls the family state before each prompt.
//! Guest init splices each connection of `/run/creche/family-state.sock`
//! to a fresh vsock dial of host port 1100 (decision CH62 rule 13), and
//! Cloud Hypervisor connects each dial to `vsock.sock_1100` in the run dir
//! of the VM (decision CH24). The vmm listens there.
//!
//! 1. `bind` binds the listener beside the plane listeners, with their
//!    lifecycle: after `RunRoot::link_vm` emptied the slot and after
//!    `remove_stale`, before CH starts, for a cold boot and for each
//!    restore (plan PR V6, decision CH56 rule 1).
//! 2. `Bound::serve` gives the listener a task. Each link reads pull lines
//!    with the framing of "The vsock JSON links" in `wire/AGENTS.md`: a
//!    line of at most `FAMILY_STATE_CAP` bytes, read through
//!    `creche_wire::strict`, then as `FamilyStatePull` (decision CH65
//!    rule 2). The facade may send many pulls on one link (decision CH43
//!    rule 4). Each gets one `FamilyStateReply` line.
//! 3. Each pull reads the state with `FamilyRoot::read_state` in
//!    `spawn_blocking` (decision CH50 rule 9). A fault is its typed reply,
//!    and the link stays open (decision CH23 rule 2). Nothing keeps a last
//!    good state (decision CH53 rule 2).
//! 4. A `state` reply waits until the host time passes `model_live_at`,
//!    for at most `HOLD_CAP` (decision CH31 rule 1).
//! 5. A refused line closes the link with no reply, and the vmm logs why
//!    (decision CH43 rule 2). An EOF at a line end closes it with no log.
//!    After a restore the old link ends so, and the next pull dials fresh
//!    (fact 74).
//! 6. A pull does not set `last_use` (decision CH62 rule 22). This module
//!    holds no handle on the VM table.
//! 7. A good pull writes no log line. Each refused pull and each faulted
//!    pull writes one stderr line (decision CH68 rule 3). A guest that
//!    pulls in a loop cannot flood the journal with good pulls.
//! 8. The listener holds at most `LINK_CAP` open links, and a link past
//!    the cap is accepted and closed at once (decision CH68 rule 1). A
//!    root guest can dial 1100 with no guest init.
//! 9. The drop of `Server` closes the listener and every open link. The
//!    VM entry of the start table holds it, so the listener goes with the
//!    VM.

use std::io;
use std::os::unix::net::UnixListener as StdListener;
use std::sync::Arc;
use std::time::{Duration, Instant, SystemTime};

use creche_vmm_confine::family::{FamilyRoot, StateFault};
use creche_vmm_store::layout::{SLOTS, VmRunDir};
use creche_wire::{FAMILY_STATE_CAP, FamilyName, FamilyStatePull, HostPort, SessionId, strict};
use tokio::net::{UnixListener, UnixStream};
use tokio::task::{JoinHandle, JoinSet, spawn_blocking};
use tokio::time::sleep;

use crate::control::{ControlError, ControlLink};
use crate::planes::{BindError, SPLICE_CAP};
use crate::server::ACCEPT_PAUSE;

/// The longest hold of a `state` reply (decision CH31 rule 1).
pub const HOLD_CAP: Duration = Duration::from_secs(10);

/// The most open links of the listener of one VM.
// Decision CH68 rule 1: 64 links per VM listener, as the guest row cap
// (decision CH62 rule 14). A link past the cap is accepted and closed at
// once, as decision CH67 rule 3 does for a splice.
pub const LINK_CAP: usize = SPLICE_CAP;

/// The most fds that one `read_state` holds at once: the family dir,
/// `config/`, `skills/`, the copy that `Dir::read_from` opens, one skill
/// dir and one file.
const READ_FDS: u64 = 6;

/// The fds that the listeners of all `SLOTS` need when each one holds
/// `LINK_CAP` links and each link reads: per VM the listener, and per link
/// its stream and `READ_FDS`. `vmm serve` raises its soft `RLIMIT_NOFILE`
/// to this plus `planes::fd_need`.
// Decision CH68 rule 2: this need (28736) adds to the raise of decision
// CH67 rule 4 (20608), so the raise is 49344.
#[must_use]
pub fn fd_need() -> u64 {
    let links = u64::try_from(LINK_CAP).unwrap_or(u64::MAX);
    links
        .saturating_mul(READ_FDS.saturating_add(1))
        .saturating_add(1)
        .saturating_mul(u64::from(SLOTS))
}

/// The listener of one VM, bound and not yet served. A pull that the
/// guest sends before `serve` waits in the backlog. The drop closes it.
#[derive(Debug)]
pub struct Bound(StdListener);

/// Binds `vsock.sock_1100` in `run` (decision CH24). It blocks, so the
/// start runs it in `spawn_blocking`. The slot is empty, so a path that is
/// there fails the bind.
///
/// # Errors
///
/// A `BindError` when the listener did not bind.
pub fn bind(run: &VmRunDir) -> Result<Bound, BindError> {
    let path = run.host_listener(HostPort::FamilyState);
    StdListener::bind(&path)
        .and_then(|listener| listener.set_nonblocking(true).map(|()| listener))
        .map(Bound)
        .map_err(|source| BindError { path, source })
}

/// The family of one VM, which each pull reads, and its session, which
/// each log line names.
#[derive(Debug)]
struct Puller {
    families: FamilyRoot,
    family: FamilyName,
    session: SessionId,
}

impl Bound {
    /// Serves the listener in a task of its own. Each pull reads the state
    /// of `family` in `families`. Log lines name `session`.
    ///
    /// # Errors
    ///
    /// The error of a listener that the runtime did not take.
    pub fn serve(
        self,
        families: FamilyRoot,
        family: FamilyName,
        session: SessionId,
    ) -> io::Result<Server> {
        let listener = UnixListener::from_std(self.0)?;
        let puller = Arc::new(Puller {
            families,
            family,
            session,
        });
        Ok(Server(tokio::spawn(accept(listener, puller))))
    }
}

/// The served listener of one VM. Its drop ends the listener task, and the
/// end of the task drops its listener and its open links.
#[derive(Debug)]
pub struct Server(JoinHandle<()>);

impl Drop for Server {
    fn drop(&mut self) {
        self.0.abort();
    }
}

/// Accepts on `listener` until its task ends, and serves each link.
// Decision CH40 rule 1, as on vmm.sock: a failed accept, such as EMFILE,
// is logged by kind, and the loop pauses ACCEPT_PAUSE and goes on.
async fn accept(listener: UnixListener, puller: Arc<Puller>) {
    let mut links = JoinSet::new();
    loop {
        let accepted = listener.accept().await;
        while links.try_join_next().is_some() {}
        match accepted {
            Ok((guest, _)) if links.len() < LINK_CAP => {
                links.spawn(serve_link(guest, Arc::clone(&puller)));
            }
            Ok((guest, _)) => drop(guest),
            Err(error) => {
                eprintln!("vmm: a family-state accept failed: {}", error.kind());
                sleep(ACCEPT_PAUSE).await;
            }
        }
    }
}

/// Answers each pull line of `guest` until an EOF, a refused line or a
/// failed write ends the link.
async fn serve_link(guest: UnixStream, puller: Arc<Puller>) {
    let session = &puller.session;
    let mut link = ControlLink::accepted(guest);
    loop {
        let why = match link.line(FAMILY_STATE_CAP).await {
            Ok(line) if pull(&line) => None,
            Ok(_) => Some("a line that is not a pull"),
            Err(ControlError::Closed) => return,
            Err(ControlError::Line) => Some("a line past the cap or cut by an EOF"),
            Err(_) => Some("a failed read"),
        };
        if let Some(why) = why {
            eprintln!("{}", refused_log(session, why));
            return;
        }
        let pulled = Instant::now();
        let Some((kind, mut line, held)) = answer(&puller).await else {
            return;
        };
        line.push(b'\n');
        if link.write_raw(&line).await.is_err() {
            return;
        }
        if let Some(log) = reply_log(session, kind, pulled.elapsed(), held) {
            eprintln!("{log}");
        }
    }
}

/// The one log line of a refused pull (decision CH68 rule 3).
fn refused_log(session: &SessionId, why: &str) -> String {
    format!("vmm: session {session} family-state link closed: {why}")
}

/// The log line of a reply, or `None` for a good pull, which writes no
/// line (decision CH68 rule 3). A faulted pull writes one line with its
/// kind and its time.
fn reply_log(session: &SessionId, kind: &str, took: Duration, held: Duration) -> Option<String> {
    (kind != KIND_STATE).then(|| {
        let took = took.as_micros();
        let held = held.as_millis();
        format!("vmm: session {session} family-state {kind} in {took} us, held {held} ms")
    })
}

/// The kind of a good pull: the `state` reply.
const KIND_STATE: &str = "state";

/// Whether `line` is one `FamilyStatePull`, read through the strict check
/// (decision CH65 rule 2).
fn pull(line: &[u8]) -> bool {
    strict(line) && serde_json::from_slice::<FamilyStatePull>(line).is_ok()
}

/// The reply to one pull: its kind for the log, its line with no LF and
/// its hold. `None` when the blocking read did not end, as at the end of
/// the runtime.
async fn answer(puller: &Arc<Puller>) -> Option<(&'static str, Vec<u8>, Duration)> {
    let reader = Arc::clone(puller);
    let read = spawn_blocking(move || reader.families.read_state(&reader.family))
        .await
        .ok()?;
    match read {
        Ok(state) => {
            let held = hold(state.model_live_at(), SystemTime::now());
            sleep(held).await;
            Some((KIND_STATE, state.line().to_vec(), held))
        }
        Err(fault) => {
            let (kind, line) = fault_reply(fault);
            Some((kind, line.to_vec(), Duration::ZERO))
        }
    }
}

/// How long a `state` reply waits at `now`: until `live_at`, at most
/// `HOLD_CAP`. A time that passed, or none, holds nothing (decision CH31
/// rule 1).
fn hold(live_at: Option<SystemTime>, now: SystemTime) -> Duration {
    live_at
        .and_then(|at| at.duration_since(now).ok())
        .map_or(Duration::ZERO, |wait| wait.min(HOLD_CAP))
}

/// The kind and the line of the typed reply of `fault` (decisions CH23
/// rule 2, CH31 rules 3 and 4). Each line is the compact serde_json text
/// of its `FamilyStateReply`, and a test holds them equal.
fn fault_reply(fault: StateFault) -> (&'static str, &'static [u8]) {
    match fault {
        StateFault::CredsMissing => ("creds_missing", br#"{"creds_missing":{}}"#),
        StateFault::ConfigMissing => ("config_missing", br#"{"config_missing":{}}"#),
        StateFault::NotValid => ("not_valid", br#"{"not_valid":{}}"#),
        StateFault::TooLarge => ("too_large", br#"{"too_large":{}}"#),
    }
}

#[cfg(test)]
mod tests {
    use std::time::{Duration, SystemTime};

    use creche_vmm_confine::family::StateFault;
    use creche_wire::{
        ConfigMissing, CredsMissing, FamilyStateReply, SessionId, StateNotValid, StateTooLarge,
    };

    use super::{HOLD_CAP, KIND_STATE, fault_reply, hold, refused_log, reply_log};

    fn session() -> SessionId {
        "3f2b8c1e-9d4a-4e6b-8a7c-0b1d2e3f4a5b".parse().unwrap()
    }

    /// Each fault line is the serde_json text of its reply, and its kind
    /// is the key of that text.
    #[test]
    fn each_fault_line_is_its_typed_reply() {
        let faults = [
            (
                StateFault::CredsMissing,
                FamilyStateReply::CredsMissing(CredsMissing {}),
            ),
            (
                StateFault::ConfigMissing,
                FamilyStateReply::ConfigMissing(ConfigMissing {}),
            ),
            (
                StateFault::NotValid,
                FamilyStateReply::NotValid(StateNotValid {}),
            ),
            (
                StateFault::TooLarge,
                FamilyStateReply::TooLarge(StateTooLarge {}),
            ),
        ];
        for (fault, reply) in faults {
            let (kind, line) = fault_reply(fault);
            assert_eq!(line, serde_json::to_vec(&reply).unwrap());
            assert_eq!(line, format!(r#"{{"{kind}":{{}}}}"#).as_bytes());
        }
    }

    /// A good pull writes no log line, however long its hold (decision
    /// CH68 rule 3).
    #[test]
    fn a_good_pull_logs_nothing() {
        let session = session();
        let took = Duration::from_micros(40);
        assert_eq!(reply_log(&session, KIND_STATE, took, Duration::ZERO), None);
        assert_eq!(reply_log(&session, KIND_STATE, took, HOLD_CAP), None);
    }

    /// Each faulted pull writes one line, and a refused pull writes one
    /// line (decision CH68 rule 3).
    #[test]
    fn a_faulted_or_refused_pull_logs_one_line() {
        let session = session();
        for fault in [
            StateFault::CredsMissing,
            StateFault::ConfigMissing,
            StateFault::NotValid,
            StateFault::TooLarge,
        ] {
            let (kind, _) = fault_reply(fault);
            let log = reply_log(&session, kind, Duration::from_micros(7), Duration::ZERO)
                .expect("a faulted pull writes a line");
            assert!(log.contains(kind));
            assert!(!log.contains('\n'));
        }
        let log = refused_log(&session, "a failed read");
        assert!(log.ends_with("a failed read"));
        assert!(!log.contains('\n'));
    }

    /// A time ahead holds until it, at most `HOLD_CAP`. A time that passed,
    /// and no time, hold nothing.
    #[test]
    fn the_hold_runs_to_the_live_time_within_the_cap() {
        let now = SystemTime::UNIX_EPOCH + Duration::from_secs(1_800_000_000);
        let ahead = Duration::from_secs(3);
        assert_eq!(hold(Some(now + ahead), now), ahead);
        assert_eq!(hold(Some(now + Duration::from_secs(3600)), now), HOLD_CAP);
        assert_eq!(hold(Some(now - ahead), now), Duration::ZERO);
        assert_eq!(hold(None, now), Duration::ZERO);
    }
}
