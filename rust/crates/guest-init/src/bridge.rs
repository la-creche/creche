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
//!    (decision CH62 rule 14). No idle timeout closes a link.
//! 4. Each link writes `acp link <n> open`, then `relay connected` or the
//!    dial fault, then `closed` on the console (decision CH62 rule 26). A
//!    failed accept writes its error and powers off, as on 1200.
//!
//! `Slots` and `dial` are the pure core, so the unit tests drive them on
//! any OS. The Linux shell runs the vsock listener and the threads.

use std::io;
use std::os::unix::net::UnixStream;
use std::path::Path;
use std::sync::Arc;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::thread::sleep;
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

#[cfg(target_os = "linux")]
pub use shell::{listen_vsock, serve_acp};

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

    use super::{RELAY_WAIT, ROW_CAP, Slots, dial};
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
        thread::Builder::new()
            .name("acp-accept".to_owned())
            .spawn(move || {
                let error = accept_loop(&listener);
                crate::say(&format!("acp accept failed: {error}"));
                let _ = crate::power::off();
            })?;
        Ok(())
    }

    /// Accepts each 1201 link and starts its splice thread. EINTR and
    /// ECONNABORTED are retried, as on 1200. It returns each other error.
    fn accept_loop(listener: &OwnedFd) -> io::Error {
        let slots = Slots::default();
        let mut count: u64 = 0;
        loop {
            let link = match accept_with(listener, SocketFlags::CLOEXEC) {
                Ok(link) => link,
                Err(Errno::INTR | Errno::CONNABORTED) => continue,
                Err(error) => return error.into(),
            };
            count += 1;
            let name = format!("acp link {count}");
            let Some(slot) = slots.take() else {
                crate::say(&format!("{name} refused: {ROW_CAP} links open"));
                continue;
            };
            crate::say(&format!("{name} open"));
            let until = Instant::now() + RELAY_WAIT;
            let spawned = thread::Builder::new().name(name.clone()).spawn(move || {
                relay(&name, link, until);
                drop(slot);
            });
            if let Err(error) = spawned {
                crate::say(&format!("acp link {count} thread failed: {error}"));
            }
        }
    }

    /// Dials the relay socket for `link` and splices the two until either
    /// closes (decisions CH13, CH62 rules 14 and 16).
    fn relay(name: &str, link: OwnedFd, until: Instant) {
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
    use std::io::{Read as _, Write as _};
    use std::os::unix::net::UnixListener;
    use std::path::PathBuf;
    use std::thread;
    use std::time::{Duration, Instant};

    use super::{RELAY_PAUSE, RELAY_WAIT, ROW_CAP, Slots, dial};

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
}
