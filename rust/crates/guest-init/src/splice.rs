//! The splice: one byte copy in both directions between two connected
//! sockets (decisions CH13, CH62 rules 2 and 14, CH67 rule 9).
//!
//! Each bridge row of `bridge` joins one accepted connection to one fresh
//! dial. The rows join three socket kinds (vsock, TCP and Unix), so the
//! splice takes any two socket fds.
//!
//! 1. Two std threads copy the bytes, one per direction (decision CH62
//!    rule 2). The splice reads no byte as a protocol and caps none.
//! 2. An EOF in one direction shuts down the write half of the other
//!    socket, and the other direction runs on. A client that half-closes
//!    after its request thus still reads the answer. The splice closes
//!    both sockets when both directions have ended (decision CH67 rule
//!    9).
//! 3. A failed read or a failed write in either direction shuts down
//!    both sockets in both directions, so the other thread wakes and ends
//!    too. A reset link thus dies with both halves closed (decision CH62
//!    rule 14). A stale 1201 link after a restore closes only its own
//!    relay connection, and the facade keeps running (decision CH19 rule
//!    1, fact 74).
//! 4. No idle timeout ends a splice (decision CH62 rule 14).

use std::fs::File;
use std::io::{self, Read as _, Write as _};
use std::os::fd::OwnedFd;
use std::thread;

use rustix::net::{Shutdown, shutdown};

/// The bytes of one read.
const CHUNK: usize = 16 * 1024;

/// Copies the bytes of `one` to `other` and the bytes of `other` to
/// `one` until both directions have ended. It waits for both copies and
/// closes both fds.
///
/// # Errors
///
/// The error of the spawn of the second copy thread. The fds then close
/// at once.
pub fn splice(one: OwnedFd, other: OwnedFd) -> io::Result<()> {
    let one = File::from(one);
    let other = File::from(other);
    thread::scope(|scope| {
        thread::Builder::new()
            .name("splice".to_owned())
            .spawn_scoped(scope, || copy(&other, &one))?;
        copy(&one, &other);
        Ok(())
    })
}

/// How one direction of the splice ended.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Ended {
    /// The source reached EOF, and each byte reached the sink.
    Eof,
    /// A read or a write failed.
    Failed,
}

/// Copies the bytes of `from` to `to` until an EOF or a fault. At an EOF
/// it shuts down the write half of `to`. At a fault it shuts down both
/// sockets in both directions.
fn copy(from: &File, to: &File) {
    match carry(from, to) {
        // A failed shutdown means that `to` is no longer connected, so
        // its peer reads an EOF or a reset anyway.
        Ended::Eof => {
            let _ = shutdown(to, Shutdown::Write);
        }
        // ENOTCONN means that the other copy already shut the socket down.
        Ended::Failed => {
            let _ = shutdown(from, Shutdown::Both);
            let _ = shutdown(to, Shutdown::Both);
        }
    }
}

/// Moves the bytes of `from` to `to` until an EOF or a fault.
fn carry(from: &File, to: &File) -> Ended {
    let mut chunk = [0_u8; CHUNK];
    loop {
        let read = match (&*from).read(&mut chunk) {
            Ok(0) => return Ended::Eof,
            Ok(read) => read,
            Err(error) if error.kind() == io::ErrorKind::Interrupted => continue,
            Err(_) => return Ended::Failed,
        };
        if (&*to)
            .write_all(chunk.get(..read).unwrap_or_default())
            .is_err()
        {
            return Ended::Failed;
        }
    }
}

#[cfg(test)]
mod tests {
    use std::io::{Read as _, Write as _};
    use std::net::{Ipv4Addr, Shutdown, TcpListener, TcpStream};
    use std::os::fd::OwnedFd;
    use std::os::unix::net::UnixStream;
    use std::thread;
    use std::time::Duration;

    use rustix::net::sockopt::set_socket_linger;

    use super::splice;

    /// Two socket pairs and a splice of their inner ends on a thread. It
    /// returns the two outer ends and the thread.
    fn spliced() -> (UnixStream, UnixStream, thread::JoinHandle<()>) {
        let (one, one_inner) = UnixStream::pair().unwrap();
        let (other_inner, other) = UnixStream::pair().unwrap();
        let thread = thread::spawn(move || {
            splice(OwnedFd::from(one_inner), OwnedFd::from(other_inner)).unwrap();
        });
        (one, other, thread)
    }

    fn read_exact(stream: &mut UnixStream, length: usize) -> Vec<u8> {
        let mut bytes = vec![0_u8; length];
        stream.read_exact(&mut bytes).unwrap();
        bytes
    }

    #[test]
    fn copies_bytes_both_ways() {
        let (mut one, mut other, thread) = spliced();
        one.write_all(b"ping\n").unwrap();
        assert_eq!(read_exact(&mut other, 5), b"ping\n");
        other.write_all(b"pong 7\n").unwrap();
        assert_eq!(read_exact(&mut one, 7), b"pong 7\n");

        // More than one read of the splice, in one write.
        let big: Vec<u8> = (0..100_000_u32).map(|n| n.to_le_bytes()[0]).collect();
        let writer = {
            let mut one = one.try_clone().unwrap();
            let big = big.clone();
            thread::spawn(move || one.write_all(&big).unwrap())
        };
        assert_eq!(read_exact(&mut other, big.len()), big);
        writer.join().unwrap();

        drop(one);
        drop(other);
        thread.join().unwrap();
    }

    /// A client that half-closes after its request still reads the
    /// answer: the EOF shuts down only the write half of the other
    /// socket, and the splice ends when both directions have ended
    /// (decision CH67 rule 9).
    #[test]
    fn a_half_close_still_gets_the_answer() {
        let (mut one, mut other, thread) = spliced();
        one.write_all(b"request\n").unwrap();
        one.shutdown(Shutdown::Write).unwrap();
        assert_eq!(read_exact(&mut other, 8), b"request\n");
        let mut byte = [0_u8; 1];
        assert_eq!(other.read(&mut byte).unwrap(), 0);

        other.write_all(b"answer\n").unwrap();
        assert_eq!(read_exact(&mut one, 7), b"answer\n");
        drop(other);
        assert_eq!(one.read(&mut byte).unwrap(), 0);
        thread.join().unwrap();
    }

    /// A reset in one direction closes both halves: the far end of the
    /// other socket, which stays open, reads an EOF, and the splice ends
    /// (decision CH62 rule 14, fact 74).
    #[test]
    fn a_reset_closes_both_halves() {
        let listener = TcpListener::bind((Ipv4Addr::LOCALHOST, 0)).unwrap();
        let address = listener.local_addr().unwrap();
        let one = TcpStream::connect(address).unwrap();
        let (one_inner, _) = listener.accept().unwrap();
        let (other_inner, mut other) = UnixStream::pair().unwrap();
        let thread = thread::spawn(move || {
            splice(OwnedFd::from(one_inner), OwnedFd::from(other_inner)).unwrap();
        });

        // A zero linger makes the close send a reset.
        set_socket_linger(&one, Some(Duration::ZERO)).unwrap();
        drop(one);
        let mut byte = [0_u8; 1];
        assert_eq!(other.read(&mut byte).unwrap(), 0);
        thread.join().unwrap();
        assert!(other.write_all(b"late\n").is_err());
    }
}
