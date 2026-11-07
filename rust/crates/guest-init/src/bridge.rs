//! The bridge rows: each joins a listener in the guest to a fresh dial
//! per connection, through `splice` (decisions CH13, CH62 rules 14 and
//! 16).
//!
//! This file holds one row, the ACP row: guest port 1201 to the relay
//! socket of the facade (decision CH13).
//!
//! 1. Guest init listens on AF_VSOCK port 1201 of any CID before `up`,
//!    and its accept thread accepts in every facade state, before the
//!    first hello too (decision CH50 rule 2).
//! 2. Each accepted link dials `creche_wire::guest::RELAY_SOCKET` fresh
//!    (decision CH62 rule 14). While the relay socket does not listen,
//!    the dial tries again every `RELAY_PAUSE`, for up to `RELAY_WAIT`
//!    after the accept. Then guest init closes the link (decision CH62
//!    rule 16). Any other dial error closes the link at once (decision
//!    CH62 rule 14: nothing else retries).
//! 3. A row holds at most `ROW_CAP` open links, from the accept to the
//!    close of both halves. It closes a link past the cap at once
//!    (decisions CH62 rule 14, CH67 rule 3). No idle timeout closes a
//!    link.
//! 4. Each link writes `acp link <n> open`, then `relay connected` or the
//!    dial fault, then `closed` on the console (decision CH62 rule 26).
//! 5. A failed accept ends the accept thread. The thread hands the error
//!    to the control loop, as the 1200 accept thread does. The loop then
//!    returns its end line, and the main thread powers off (decisions
//!    CH19 rule 3, CH67 rule 11, plan risk 9).
//!
//! `Slots`, `dial` and `accept_links` are the portable core, so the unit
//! tests drive them on any OS. The Linux shell runs the vsock listener
//! and its accept thread.

use std::io;
use std::os::fd::OwnedFd;
use std::os::unix::net::UnixStream;
use std::path::Path;
use std::sync::Arc;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::thread::{self, sleep};
use std::time::{Duration, Instant};

/// The open links of one row at most (decision CH62 rule 14).
pub const ROW_CAP: usize = 64;

/// How long a 1201 link waits for the relay socket after its accept
/// (decision CH62 rule 16): the `READY_WAIT` of the vmm.
pub const RELAY_WAIT: Duration = Duration::from_secs(10);

/// The pause between two dials of the relay socket (decision CH62 rule
/// 16).
pub const RELAY_PAUSE: Duration = Duration::from_millis(100);

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

/// Takes each link that `accept` returns and starts its thread, which
/// runs `bridge` and holds a slot of the row until `bridge` returns
/// (decision CH62 rule 14). `accept` returns `None` for an accept that
/// the loop tries again. A link past `ROW_CAP` closes at once. It returns
/// the accept error that ends the loop.
pub fn accept_links(
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
        // A refused link closes here, at the end of this pass.
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

#[cfg(target_os = "linux")]
pub use shell::{listen_acp, listen_vsock, serve_acp};

/// The vsock listener and the threads of the ACP row (decisions CH62
/// rules 1 and 2). The kvm tests prove it.
#[cfg(target_os = "linux")]
mod shell {
    use std::io;
    use std::os::fd::{AsRawFd as _, OwnedFd};
    use std::path::Path;
    use std::thread;
    use std::time::Instant;

    use creche_wire::GuestPort;
    use creche_wire::guest::RELAY_SOCKET;
    use nix::sys::socket::{AddressFamily, SockFlag, SockType, VsockAddr, bind, socket};
    use rustix::io::Errno;
    use rustix::net::{SocketFlags, accept_with};

    use super::{RELAY_WAIT, ROW_CAP, accept_links, dial};
    use crate::splice::splice;

    /// Listens on vsock `port` of any CID with `backlog` (decisions CH13,
    /// CH14, CH62 rule 1).
    ///
    /// # Errors
    ///
    /// The error of `socket`, `bind` or `listen`.
    pub fn listen_vsock(port: GuestPort, backlog: i32) -> io::Result<OwnedFd> {
        let listener = socket(
            AddressFamily::Vsock,
            SockType::Stream,
            SockFlag::SOCK_CLOEXEC,
            None,
        )?;
        let address = VsockAddr::new(nix::libc::VMADDR_CID_ANY, port.number());
        bind(listener.as_raw_fd(), &address)?;
        rustix::net::listen(&listener, backlog)?;
        Ok(listener)
    }

    /// Listens on guest port 1201 (decision CH13). The backlog is
    /// `ROW_CAP`: the row never holds more links. The listen comes before
    /// `up`, and a link that comes before `serve_acp` waits in the
    /// backlog.
    ///
    /// # Errors
    ///
    /// The error of the listen.
    pub fn listen_acp() -> io::Result<OwnedFd> {
        let backlog = i32::try_from(ROW_CAP).map_err(io::Error::other)?;
        listen_vsock(GuestPort::Acp, backlog)
    }

    /// Starts the accept thread of the 1201 `listener` (decision CH50
    /// rule 2). The thread ends only on an accept error, and it hands that
    /// error and the row name to `failed`. The caller then powers off on
    /// its main thread (decisions CH19 rule 3, CH67 rule 11).
    ///
    /// # Errors
    ///
    /// The error of the thread spawn.
    pub fn serve_acp(
        listener: OwnedFd,
        failed: impl FnOnce(&'static str, io::Error) + Send + 'static,
    ) -> io::Result<()> {
        thread::Builder::new()
            .name("acp-accept".to_owned())
            .spawn(move || {
                let error = accept_links(
                    "acp",
                    // EINTR and ECONNABORTED are tried again, as on 1200.
                    || match accept_with(&listener, SocketFlags::CLOEXEC) {
                        Ok(link) => Ok(Some(link)),
                        Err(Errno::INTR | Errno::CONNABORTED) => Ok(None),
                        Err(error) => Err(error.into()),
                    },
                    relay,
                );
                failed("acp", error);
            })?;
        Ok(())
    }

    /// Dials the relay socket for `link` and splices the two until either
    /// closes (decisions CH13, CH62 rules 14 and 16). The link thread
    /// starts at the accept, so the bound counts from the accept.
    fn relay(name: &str, link: OwnedFd) {
        let until = Instant::now() + RELAY_WAIT;
        let relay = match dial(Path::new(RELAY_SOCKET), until) {
            Ok(relay) => relay,
            Err(error) => {
                crate::say(&format!("{name} relay dial failed: {error}"));
                return;
            }
        };
        crate::say(&format!("{name} relay connected"));
        match splice(link, relay.into()) {
            Ok(()) => crate::say(&format!("{name} closed")),
            Err(error) => crate::say(&format!("{name} splice failed: {error}")),
        }
    }
}

#[cfg(test)]
mod tests {
    use std::fs::File;
    use std::io::{self, Read as _, Write as _};
    use std::os::fd::OwnedFd;
    use std::os::unix::net::{UnixListener, UnixStream};
    use std::path::PathBuf;
    use std::sync::mpsc::{Sender, channel};
    use std::thread;
    use std::time::{Duration, Instant};

    use super::{RELAY_PAUSE, RELAY_WAIT, ROW_CAP, Slots, accept_links, dial};

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

    /// A dial error other than a missing or refused listener ends the
    /// dial at once, with no retry, even with time left until the bound
    /// (decision CH62 rule 14). A socket path under a regular file gives
    /// ENOTDIR.
    #[test]
    fn the_relay_dial_ends_at_once_on_another_error() {
        let file = path("file");
        std::fs::write(&file, b"").unwrap();
        let started = Instant::now();
        let error = dial(&file.join("relay.sock"), started + Duration::from_secs(5)).unwrap_err();
        let waited = started.elapsed();
        assert!(
            !matches!(
                error.kind(),
                std::io::ErrorKind::NotFound | std::io::ErrorKind::ConnectionRefused
            ),
            "{error:?}"
        );
        assert_eq!(
            error.raw_os_error(),
            Some(rustix::io::Errno::NOTDIR.raw_os_error())
        );
        assert!(waited < RELAY_PAUSE / 2, "it waited {waited:?}");
        std::fs::remove_file(&file).unwrap();
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

    /// Hands one new link to the accept loop of `links` and returns its
    /// peer, which reads with a bound of `wait`.
    fn hand(links: &Sender<OwnedFd>, wait: Duration) -> UnixStream {
        let (peer, link) = UnixStream::pair().unwrap();
        peer.set_read_timeout(Some(wait)).unwrap();
        links.send(link.into()).unwrap();
        peer
    }

    /// Whether the accept loop kept the link of `peer`: the read of a kept
    /// link waits, and the read of a closed one gets an EOF.
    fn kept(peer: &mut UnixStream) -> bool {
        match peer.read(&mut [0_u8; 1]) {
            Ok(0) => false,
            Err(error)
                if matches!(
                    error.kind(),
                    io::ErrorKind::WouldBlock | io::ErrorKind::TimedOut
                ) =>
            {
                true
            }
            other => panic!("the link got {other:?}"),
        }
    }

    /// The accept loop holds `ROW_CAP` open links and closes the next one
    /// at once. After one of them closes, it keeps a new link again
    /// (decision CH62 rule 14).
    #[test]
    fn the_accept_loop_closes_a_link_past_the_cap() {
        let (links, accepted) = channel::<OwnedFd>();
        let row = thread::spawn(move || {
            accept_links(
                "test",
                move || {
                    accepted
                        .recv()
                        .map(Some)
                        .map_err(|_| io::Error::other("no more links"))
                },
                // A link stays open until its peer writes or closes.
                |_: &str, link: OwnedFd| {
                    let _ = File::from(link).read(&mut [0_u8; 1]);
                },
            )
        });
        let wait = Duration::from_millis(300);
        let mut open: Vec<_> = (0..ROW_CAP).map(|_| hand(&links, wait)).collect();
        // Link 65 finds every slot taken, so it reads an EOF at once. A
        // kept link would wait the full 5 s.
        assert!(!kept(&mut hand(&links, Duration::from_secs(5))));

        // The slot frees when the bridge thread of the closed link ends.
        // Until then, each new link reads an EOF.
        drop(open.remove(0));
        let deadline = Instant::now() + Duration::from_secs(5);
        let again = loop {
            let mut peer = hand(&links, wait);
            if kept(&mut peer) {
                break peer;
            }
            assert!(Instant::now() < deadline, "no slot freed after a close");
        };
        // The row is full again.
        assert!(!kept(&mut hand(&links, Duration::from_secs(5))));

        drop(links);
        assert_eq!(row.join().unwrap().to_string(), "no more links");
        drop(open);
        drop(again);
    }
}
