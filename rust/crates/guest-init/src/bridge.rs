//! The bridge rows: each joins a listener in the guest to a fresh dial
//! per connection, through `splice` (decisions CH13, CH6-2, CH62 rules
//! 13, 14 and 16).
//!
//! The ACP row joins guest port 1201 to the relay socket of the facade
//! (decision CH13). The host rows join the guest to the vmm: TCP on
//! guest 127.0.0.1:4000 to host port 1101 and :8300 to 1102 (decisions
//! CH6-2, CH14), and the family-state socket to host port 1100 (decisions
//! CH5a, CH62 rule 13).
//!
//! 1. Guest init listens on every row before `up`, and each row has an
//!    accept thread of its own. 1201 accepts in every facade state,
//!    before the first hello too (decision CH50 rule 2).
//! 2. Each accepted link dials fresh, and nothing retries a link
//!    (decision CH62 rule 14). A 1201 link waits for the relay socket:
//!    while it does not listen, the dial tries again every `RELAY_PAUSE`
//!    for up to `RELAY_WAIT` after the accept (decision CH62 rule 16). A
//!    host row dials vsock CID 2 once. A failed dial closes the guest
//!    side at once.
//! 3. The splice reads and rewrites no byte, so the `Host` header of a
//!    plane request stays valid (decision CH6-2, fact 46).
//! 4. The family-state socket keeps only links of the live facade, by
//!    the `SO_PEERCRED` pid that the local link reads too (decision CH62
//!    rule 13). The facade speaks the pull lines itself: guest init reads
//!    none of them. The plane rows take any guest process (decision CH6-2:
//!    no allowlist).
//! 5. A row holds at most `ROW_CAP` open links, from the accept to the
//!    close of both halves. It closes a link past the cap at once
//!    (decision CH62 rule 14). No idle timeout closes a link.
//! 6. Each link writes `<row> link <n> open`, then `relay connected` or
//!    `host connected` or the dial fault, then `closed` on the console
//!    (decision CH62 rule 26). A failed accept writes its error and powers
//!    off, as on 1200.
//!
//! `Slots`, `dial`, `join` and the row table are the pure core, so the
//! unit tests drive them on any OS. The Linux shell runs the listeners,
//! the vsock dial and the threads.

// Decision CH62 rule 15: before the first deploy, a new row needs no
// GUEST_INIT_PROTOCOL bump. After it, each change of the row table bumps
// it, because a restored old guest lacks the new listener. Phase 7 adds
// 3128 to 1104 and 9418 to 1103 as loopback rows (decision CH46 rules 1
// and 4).

use std::io;
use std::os::fd::OwnedFd;
use std::os::unix::net::UnixStream;
use std::path::Path;
use std::sync::Arc;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::thread::sleep;
use std::time::{Duration, Instant};

use creche_wire::HostPort;
use creche_wire::guest::{CHAPERONE_PORT, LITELLM_PORT};

use crate::splice::splice;

/// The open links of one row at most (decision CH62 rule 14).
pub const ROW_CAP: usize = 64;

/// How long a 1201 link waits for the relay socket after its accept
/// (decision CH62 rule 16): the `READY_WAIT` of the vmm.
pub const RELAY_WAIT: Duration = Duration::from_secs(10);

/// The pause between two dials of the relay socket (decision CH62 rule
/// 16).
pub const RELAY_PAUSE: Duration = Duration::from_millis(100);

/// One loopback row: TCP on guest 127.0.0.1 at `guest`, each link to a
/// fresh vsock dial of `host` (decisions CH6-2, CH14).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct LoopbackRow {
    /// The row on the console.
    pub name: &'static str,
    /// The guest loopback port.
    pub guest: u16,
    /// The host port that each link dials.
    pub host: HostPort,
}

/// The loopback rows: the two planes (decision CH6-2).
pub const LOOPBACK_ROWS: [LoopbackRow; 2] = [
    LoopbackRow {
        name: "litellm",
        guest: LITELLM_PORT,
        host: HostPort::Litellm,
    },
    LoopbackRow {
        name: "chaperone",
        guest: CHAPERONE_PORT,
        host: HostPort::Chaperone,
    },
];

/// The host port of each link of the family-state socket (decisions
/// CH5a, CH62 rule 13).
pub const FAMILY_STATE_HOST: HostPort = HostPort::FamilyState;

/// The open-link count of one row, capped at `ROW_CAP` (decision CH62
/// rule 14).
#[derive(Debug, Clone, Default)]
pub struct Slots(Arc<AtomicUsize>);

/// One open link of a row. Its drop frees the slot.
#[derive(Debug)]
pub struct Slot(Arc<AtomicUsize>);

impl Slots {
    /// A slot for one more link, or `None` when `ROW_CAP` links are open.
    pub fn take(&self) -> Option<Slot> {
        self.0
            .fetch_update(Ordering::AcqRel, Ordering::Acquire, |open| {
                (open < ROW_CAP).then_some(open + 1)
            })
            .ok()
            .map(|_| Slot(Arc::clone(&self.0)))
    }
}

impl Drop for Slot {
    fn drop(&mut self) {
        self.0.fetch_sub(1, Ordering::AcqRel);
    }
}

/// Dials the Unix socket `path`. While nothing listens there (no socket
/// file, or a socket file with no listener), it tries again every
/// `RELAY_PAUSE` until `until` (decision CH62 rule 16). Each other error
/// ends the dial at once (decision CH62 rule 14).
///
/// # Errors
///
/// The error of the last try.
pub fn dial(path: &Path, until: Instant) -> io::Result<UnixStream> {
    loop {
        match UnixStream::connect(path) {
            Ok(stream) => return Ok(stream),
            Err(error)
                if matches!(
                    error.kind(),
                    io::ErrorKind::NotFound | io::ErrorKind::ConnectionRefused
                ) && Instant::now() + RELAY_PAUSE <= until =>
            {
                sleep(RELAY_PAUSE);
            }
            Err(error) => return Err(error),
        }
    }
}

/// How one link of a row ended, for its console line.
#[derive(Debug)]
pub enum End {
    /// The fresh dial failed, and the link closed at once.
    DialFailed(io::Error),
    /// The splice ran until an EOF or a fault in either direction.
    Closed,
    /// The splice did not start its second copy thread.
    SpliceFailed(io::Error),
}

impl End {
    /// The console line of the end of link `name`, whose dial is `what`
    /// (decision CH62 rule 26).
    pub fn line(&self, name: &str, what: &str) -> String {
        match self {
            Self::DialFailed(error) => format!("{name} {what} dial failed: {error}"),
            Self::Closed => format!("{name} closed"),
            Self::SpliceFailed(error) => format!("{name} splice failed: {error}"),
        }
    }
}

/// Splices `link` to `dialed`, the fresh dial of its row. A failed dial
/// closes `link` at once, and nothing dials again (decision CH62 rule
/// 14).
pub fn join(link: OwnedFd, dialed: io::Result<OwnedFd>) -> End {
    match dialed {
        Err(error) => {
            drop(link);
            End::DialFailed(error)
        }
        Ok(other) => match splice(link, other) {
            Ok(()) => End::Closed,
            Err(error) => End::SpliceFailed(error),
        },
    }
}

#[cfg(target_os = "linux")]
pub use shell::{listen_vsock, serve_acp, serve_family, serve_loopback};

/// The listeners, the vsock dial and the threads of the rows (decisions
/// CH62 rules 1 and 2). The kvm tests prove it.
#[cfg(target_os = "linux")]
mod shell {
    use std::io;
    use std::net::{Ipv4Addr, TcpListener};
    use std::num::NonZeroU32;
    use std::os::fd::{AsRawFd as _, OwnedFd};
    use std::os::unix::net::UnixListener;
    use std::path::Path;
    use std::thread;
    use std::time::Instant;

    use creche_wire::guest::RELAY_SOCKET;
    use creche_wire::{GuestPort, HostPort};
    use nix::sys::socket::{AddressFamily, SockFlag, SockType, VsockAddr, bind, connect, socket};
    use rustix::io::Errno;
    use rustix::net::{SocketFlags, accept_with};

    use super::{FAMILY_STATE_HOST, LOOPBACK_ROWS, RELAY_WAIT, ROW_CAP, Slots, dial, join};
    use crate::local_link;

    /// A vsock socket of guest init.
    fn vsock() -> io::Result<OwnedFd> {
        Ok(socket(
            AddressFamily::Vsock,
            SockType::Stream,
            SockFlag::SOCK_CLOEXEC,
            None,
        )?)
    }

    /// Listens on vsock `port` of any CID with `backlog` (decisions CH13,
    /// CH14, CH62 rule 1).
    ///
    /// # Errors
    ///
    /// The error of `socket`, `bind` or `listen`.
    pub fn listen_vsock(port: GuestPort, backlog: i32) -> io::Result<OwnedFd> {
        let listener = vsock()?;
        let address = VsockAddr::new(nix::libc::VMADDR_CID_ANY, port.number());
        bind(listener.as_raw_fd(), &address)?;
        rustix::net::listen(&listener, backlog)?;
        Ok(listener)
    }

    /// Dials host `port`: vsock CID 2, which CH joins to its socket
    /// `<vsock socket>_<port>` on the host (decisions CH14, CH8-1).
    fn dial_host(port: HostPort) -> io::Result<OwnedFd> {
        let stream = vsock()?;
        let address = VsockAddr::new(nix::libc::VMADDR_CID_HOST, port.number());
        connect(stream.as_raw_fd(), &address)?;
        Ok(stream)
    }

    /// Listens on guest port 1201 and starts its accept thread (decisions
    /// CH13, CH50 rule 2). The backlog is `ROW_CAP`: the row never holds
    /// more links.
    ///
    /// # Errors
    ///
    /// The error of the listen or of the thread spawn.
    pub fn serve_acp() -> io::Result<()> {
        let backlog = i32::try_from(ROW_CAP).map_err(io::Error::other)?;
        let listener = listen_vsock(GuestPort::Acp, backlog)?;
        serve(
            "acp",
            move || match accept_with(&listener, SocketFlags::CLOEXEC) {
                Ok(link) => Ok(Some(link)),
                Err(Errno::INTR | Errno::CONNABORTED) => Ok(None),
                Err(error) => Err(error.into()),
            },
            relay,
        )
    }

    /// Listens on guest 127.0.0.1 at the port of each loopback row and
    /// starts its accept thread (decision CH6-2).
    ///
    /// # Errors
    ///
    /// The error of a listen or of a thread spawn.
    pub fn serve_loopback() -> io::Result<()> {
        for row in LOOPBACK_ROWS {
            let listener = TcpListener::bind((Ipv4Addr::LOCALHOST, row.guest))?;
            serve(
                row.name,
                move || match listener.accept() {
                    Ok((link, _)) => Ok(Some(link.into())),
                    Err(error) if retried(&error) => Ok(None),
                    Err(error) => Err(error),
                },
                move |name, link| to_host(name, link, row.host),
            )?;
        }
        Ok(())
    }

    /// Starts the accept thread of the family-state socket `listener`. It
    /// keeps a link only when `is_facade` holds for its peer pid
    /// (decision CH62 rule 13), and closes each other link at once.
    ///
    /// # Errors
    ///
    /// The error of the thread spawn.
    pub fn serve_family(
        listener: UnixListener,
        is_facade: impl Fn(NonZeroU32) -> bool + Send + 'static,
    ) -> io::Result<()> {
        serve(
            "family-state",
            move || {
                let link = match listener.accept() {
                    Ok((link, _)) => link,
                    Err(error) if retried(&error) => return Ok(None),
                    Err(error) => return Err(error),
                };
                match local_link::peer(&link) {
                    Ok(peer) if is_facade(peer) => Ok(Some(link.into())),
                    Ok(peer) => {
                        crate::say(&format!(
                            "family-state peer pid {peer} refused: not the facade"
                        ));
                        Ok(None)
                    }
                    Err(error) => {
                        crate::say(&format!("family-state peer read failed: {error}"));
                        Ok(None)
                    }
                }
            },
            |name, link| to_host(name, link, FAMILY_STATE_HOST),
        )
    }

    /// An accept error that the loop retries, as on 1200.
    fn retried(error: &io::Error) -> bool {
        matches!(
            error.kind(),
            io::ErrorKind::Interrupted | io::ErrorKind::ConnectionAborted
        )
    }

    /// Starts the accept thread of the row `row`. A failed accept writes
    /// its error and powers off, as a failed accept of 1200 does.
    fn serve(
        row: &'static str,
        accept: impl FnMut() -> io::Result<Option<OwnedFd>> + Send + 'static,
        bridge: impl Fn(&str, OwnedFd) + Copy + Send + 'static,
    ) -> io::Result<()> {
        thread::Builder::new()
            .name(format!("{row}-accept"))
            .spawn(move || {
                let error = accept_loop(row, accept, bridge);
                crate::say(&format!("{row} accept failed: {error}"));
                let _ = crate::power::off();
            })?;
        Ok(())
    }

    /// Takes each link that `accept` returns and starts its thread, which
    /// runs `bridge` (decision CH62 rule 14). `accept` returns `None` for
    /// a link that the row does not keep. It returns each other error.
    fn accept_loop(
        row: &str,
        mut accept: impl FnMut() -> io::Result<Option<OwnedFd>>,
        bridge: impl Fn(&str, OwnedFd) + Copy + Send + 'static,
    ) -> io::Error {
        let slots = Slots::default();
        let mut count: u64 = 0;
        loop {
            let link = match accept() {
                Ok(Some(link)) => link,
                Ok(None) => continue,
                Err(error) => return error,
            };
            count += 1;
            let name = format!("{row} link {count}");
            let Some(slot) = slots.take() else {
                crate::say(&format!("{name} refused: {ROW_CAP} links open"));
                continue;
            };
            crate::say(&format!("{name} open"));
            let spawned = thread::Builder::new().name(name.clone()).spawn(move || {
                bridge(&name, link);
                drop(slot);
            });
            if let Err(error) = spawned {
                crate::say(&format!("{row} link {count} thread failed: {error}"));
            }
        }
    }

    /// Dials the relay socket for `link` and splices the two until either
    /// closes (decisions CH13, CH62 rules 14 and 16).
    fn relay(name: &str, link: OwnedFd) {
        let until = Instant::now() + RELAY_WAIT;
        let dialed = dial(Path::new(RELAY_SOCKET), until).map(OwnedFd::from);
        report(name, "relay", link, dialed);
    }

    /// Dials host `port` for `link` and splices the two until either
    /// closes (decisions CH6-2, CH62 rules 13 and 14).
    fn to_host(name: &str, link: OwnedFd, port: HostPort) {
        report(name, "host", link, dial_host(port));
    }

    /// Joins `link` to `dialed` and writes each step on the console, the
    /// dial named `what`.
    fn report(name: &str, what: &str, link: OwnedFd, dialed: io::Result<OwnedFd>) {
        if dialed.is_ok() {
            crate::say(&format!("{name} {what} connected"));
        }
        crate::say(&join(link, dialed).line(name, what));
    }
}

#[cfg(test)]
mod tests {
    use std::io::{self, Read as _, Write as _};
    use std::num::NonZeroU32;
    use std::os::fd::OwnedFd;
    use std::os::unix::net::{UnixListener, UnixStream};
    use std::path::PathBuf;
    use std::thread;
    use std::time::{Duration, Instant};

    use creche_wire::{GuestPort, HostPort};
    use serde_json::json;

    use super::{
        End, FAMILY_STATE_HOST, LOOPBACK_ROWS, RELAY_PAUSE, RELAY_WAIT, ROW_CAP, Slots, dial, join,
    };
    use crate::control::{Action, Control};

    /// A socket path of this test process. The `sun_path` cap is 104
    /// bytes on macOS, so the name stays short.
    fn path(name: &str) -> PathBuf {
        let path = std::env::temp_dir().join(format!("gi-{}-{name}.sock", std::process::id()));
        let _ = std::fs::remove_file(&path);
        path
    }

    #[test]
    fn the_bounds_are_the_decided_ones() {
        assert_eq!(ROW_CAP, 64);
        assert_eq!(RELAY_WAIT, Duration::from_secs(10));
        assert_eq!(RELAY_PAUSE, Duration::from_millis(100));
    }

    /// A dial before the relay socket listens tries again until it
    /// listens. With no listener until the bound, the dial ends at the
    /// bound (decision CH62 rule 16).
    #[test]
    fn the_1201_row_waits_for_the_relay_up_to_the_bound() {
        // No socket file at first, then a listener.
        let late = path("late");
        let dialer = {
            let late = late.clone();
            thread::spawn(move || dial(&late, Instant::now() + Duration::from_secs(5)))
        };
        thread::sleep(Duration::from_millis(350));
        let listener = UnixListener::bind(&late).unwrap();
        let mut stream = dialer.join().unwrap().unwrap();
        let (mut accepted, _) = listener.accept().unwrap();
        stream.write_all(b"ping\n").unwrap();
        let mut bytes = [0_u8; 5];
        accepted.read_exact(&mut bytes).unwrap();
        assert_eq!(&bytes, b"ping\n");
        std::fs::remove_file(&late).unwrap();

        // A socket file with no listener: ECONNREFUSED until the bound.
        let stale = path("stale");
        drop(UnixListener::bind(&stale).unwrap());
        let bound = Duration::from_millis(500);
        let started = Instant::now();
        let error = dial(&stale, started + bound).unwrap_err();
        let waited = started.elapsed();
        assert_eq!(error.kind(), std::io::ErrorKind::ConnectionRefused);
        assert!(waited + RELAY_PAUSE >= bound, "it gave up after {waited:?}");
        assert!(waited < bound + RELAY_PAUSE, "it waited {waited:?}");
        std::fs::remove_file(&stale).unwrap();
    }

    /// A row holds `ROW_CAP` open links. One more is refused, and a close
    /// frees its slot (decision CH62 rule 14).
    #[test]
    fn refuses_past_the_row_cap() {
        let slots = Slots::default();
        let mut open: Vec<_> = (0..ROW_CAP).map(|_| slots.take().unwrap()).collect();
        assert!(slots.take().is_none());
        open.pop();
        let again = slots.take();
        assert!(again.is_some());
        assert!(slots.take().is_none());
        drop(open);
        drop(again);
        let all: Vec<_> = (0..ROW_CAP).filter_map(|_| slots.take()).collect();
        assert_eq!(all.len(), ROW_CAP);
    }

    /// The rows join the ports of decisions CH6-2 and CH14: guest
    /// 127.0.0.1:4000 to host port 1101, :8300 to 1102, the family-state
    /// socket to 1100 and guest port 1201 to the relay socket. The git
    /// mirror and the egress proxy wait for phase 7 (decision CH46 rules 1
    /// and 4).
    #[test]
    fn the_table_holds_the_ch6_2_and_ch14_ports() {
        let rows: Vec<_> = LOOPBACK_ROWS
            .iter()
            .map(|row| (row.name, row.guest, row.host.number()))
            .collect();
        assert_eq!(rows, [("litellm", 4000, 1101), ("chaperone", 8300, 1102)]);
        assert_eq!(FAMILY_STATE_HOST.number(), 1100);
        assert_eq!(GuestPort::Acp.number(), 1201);
        assert!(
            !LOOPBACK_ROWS
                .iter()
                .any(|row| matches!(row.host, HostPort::GitMirror | HostPort::Egress))
        );
    }

    /// A failed dial closes the guest side at once: its peer reads an
    /// EOF, and nothing dials again (decision CH62 rule 14).
    #[test]
    fn a_refused_dial_closes_the_guest_side() {
        let (mut guest, link) = UnixStream::pair().unwrap();
        let refused = io::Error::from(io::ErrorKind::ConnectionRefused);
        let end = join(OwnedFd::from(link), Err(refused));
        assert!(matches!(end, End::DialFailed(_)), "{end:?}");
        assert_eq!(
            end.line("litellm link 1", "host"),
            "litellm link 1 host dial failed: connection refused"
        );
        let mut byte = [0_u8; 1];
        assert_eq!(guest.read(&mut byte).unwrap(), 0);
    }

    /// Only the live facade may use the family-state socket: no pid
    /// before the first start, no other pid, and no pid after the reap
    /// (decision CH62 rule 13). Unlike the local link, the check holds
    /// for each link of the facade, after its local link opened too.
    #[test]
    fn family_state_refuses_a_peer_that_is_not_the_facade() {
        let facade = NonZeroU32::new(7).unwrap();
        let stranger = NonZeroU32::new(8).unwrap();
        let mut control = Control::default();
        assert!(!control.is_facade(facade));

        let (link, _) = control.accept();
        let hello = json!({"sent_at": 1_893_456_000_000_u64, "session": "0123abcd-0000-4000-8000-00000000c7c7"});
        control.line(link, &serde_json::to_vec(&hello).unwrap());
        let start = control.clock_set(link, Ok(()));
        assert!(matches!(start, Some(Action::Start(_))), "{start:?}");
        control.started(facade);
        assert!(control.is_facade(facade));
        assert!(!control.is_facade(stranger));

        // The local link of the facade changes nothing here.
        control.local_accept(facade);
        assert!(control.is_facade(facade));

        control.reaped(facade);
        assert!(!control.is_facade(facade));
    }
}
