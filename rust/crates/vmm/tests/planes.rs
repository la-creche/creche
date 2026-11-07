//! The plane bridges (plan PR V6, decisions CH6-2, CH14, CH62 rule 14)
//! on real Unix and TCP sockets in the test process. A test plays the
//! guest side, as Cloud Hypervisor connects to `vsock.sock_<port>`, and a
//! TCP listener on 127.0.0.1 plays each plane (decision CH62 rule 28).

use std::io::{ErrorKind, Read, Write};
use std::net::{Shutdown, SocketAddr, TcpListener, TcpStream};
use std::os::unix::net::UnixStream;
use std::path::PathBuf;
use std::sync::mpsc::{Receiver, channel};
use std::thread::spawn;
use std::time::{Duration, Instant};

use creche_vmm::planes::{Planes, SPLICE_CAP, Upstreams, bind, fd_need};
use creche_vmm_store::layout::{RunRoot, Slot, VmRunDir};
use creche_wire::{HostPort, SessionId};
use tokio::runtime::{Builder, Runtime};

/// The bound of each wait of a test.
const WAIT: Duration = Duration::from_secs(5);

/// The run dir of one VM in `/tmp` and the runtime of its bridges. Its
/// drop removes the dir, also after a failed assert.
struct Scratch {
    dir: PathBuf,
    run: VmRunDir,
    runtime: Runtime,
}

impl Drop for Scratch {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.dir);
    }
}

// `cfg(test)` lets the helpers unwrap (rust/clippy.toml).
#[cfg(test)]
impl Scratch {
    /// `/tmp`, not `temp_dir`: the run root bounds the socket paths under
    /// it, and the temp dir of macOS is too long.
    fn new(name: &str) -> Self {
        let dir = PathBuf::from(format!("/tmp/vmm-planes-{}-{name}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        let root = RunRoot::new(dir.join("run")).unwrap();
        let id: SessionId = "3f2b8c1e-9d4a-4e6b-8a7c-0b1d2e3f4a5b".parse().unwrap();
        let run = root.link_vm(&id, Slot::new(0).unwrap()).unwrap();
        let runtime = Builder::new_multi_thread()
            .worker_threads(2)
            .enable_all()
            .build()
            .unwrap();
        Self { dir, run, runtime }
    }

    /// Binds and serves the bridges of the VM to `upstreams`.
    fn serve(&self, upstreams: Upstreams) -> Planes {
        let bound = bind(&self.run, upstreams).unwrap();
        let _runtime = self.runtime.enter();
        bound.serve().unwrap()
    }

    /// A guest dial of `port`, as CH makes it.
    fn dial(&self, port: HostPort) -> UnixStream {
        let stream = UnixStream::connect(self.run.host_listener(port)).unwrap();
        stream.set_read_timeout(Some(WAIT)).unwrap();
        stream
    }
}

/// A plane on 127.0.0.1. Each connection goes to `each` in a thread of
/// its own.
#[cfg(test)]
fn plane(each: impl Fn(TcpStream) + Clone + Send + 'static) -> SocketAddr {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let addr = listener.local_addr().unwrap();
    spawn(move || {
        for stream in listener.incoming() {
            let each = each.clone();
            spawn(move || each(stream.unwrap()));
        }
    });
    addr
}

/// Sends each byte back, then closes after the EOF of the peer.
#[cfg(test)]
fn echo(mut stream: TcpStream) {
    let mut buffer = [0; 4096];
    loop {
        match stream.read(&mut buffer) {
            Ok(0) | Err(_) => break,
            Ok(read) => {
                if stream.write_all(&buffer[..read]).is_err() {
                    break;
                }
            }
        }
    }
    let _ = stream.shutdown(Shutdown::Write);
}

/// An address where nothing listens: a port that the test bound and
/// closed.
#[cfg(test)]
fn closed_port() -> SocketAddr {
    TcpListener::bind("127.0.0.1:0")
        .unwrap()
        .local_addr()
        .unwrap()
}

/// Reads until EOF. A read error fails the test.
#[cfg(test)]
fn read_all(stream: &mut UnixStream) -> Vec<u8> {
    let mut bytes = Vec::new();
    stream.read_to_end(&mut bytes).unwrap();
    bytes
}

/// Each row splices to its own plane: 1101 to LiteLLM, 1102 to the
/// chaperone (decisions CH14, CH62 rule 20).
#[test]
fn splices_a_guest_dial_to_the_plane() {
    let scratch = Scratch::new("rows");
    let answer = |name: &'static [u8]| {
        move |mut stream: TcpStream| {
            let _ = stream.write_all(name);
        }
    };
    let upstreams = Upstreams {
        litellm: plane(answer(b"litellm")),
        chaperone: plane(answer(b"chaperone")),
    };
    let _planes = scratch.serve(upstreams);
    let mut litellm = scratch.dial(HostPort::Litellm);
    assert_eq!(read_all(&mut litellm), b"litellm");
    let mut chaperone = scratch.dial(HostPort::Chaperone);
    assert_eq!(read_all(&mut chaperone), b"chaperone");
}

/// The bridge reads no byte and rewrites none, so a request with its Host
/// header and every byte value come back as they went (decision CH6-2,
/// fact 46). A half close of the guest still gets each reply.
#[test]
fn the_bytes_pass_unchanged() {
    let scratch = Scratch::new("bytes");
    let upstreams = Upstreams {
        litellm: plane(echo),
        chaperone: plane(echo),
    };
    let _planes = scratch.serve(upstreams);
    let mut sent = b"POST /v1/chat/completions HTTP/1.1\r\nHost: 192.0.2.10:4000\r\n\r\n".to_vec();
    // 1 MiB past the head, more than one buffer of the copy.
    sent.extend((0..1_u32 << 20).map(|index| {
        let [low, high, ..] = index.to_le_bytes();
        low ^ high
    }));
    for port in [HostPort::Litellm, HostPort::Chaperone] {
        let mut guest = scratch.dial(port);
        let mut writer = guest.try_clone().unwrap();
        let bytes = sent.clone();
        let wrote = spawn(move || {
            writer.write_all(&bytes).unwrap();
            writer.shutdown(Shutdown::Write).unwrap();
        });
        assert!(read_all(&mut guest) == sent, "port {}", port.number());
        wrote.join().unwrap();
    }
}

/// A plane that refuses the connect closes the guest side, and nothing
/// retries (decision CH62 rule 14).
#[test]
fn a_refused_upstream_closes_the_guest_side() {
    let scratch = Scratch::new("refused");
    let upstreams = Upstreams {
        litellm: closed_port(),
        chaperone: closed_port(),
    };
    let _planes = scratch.serve(upstreams);
    for port in [HostPort::Litellm, HostPort::Chaperone] {
        let started = Instant::now();
        let mut guest = scratch.dial(port);
        assert!(read_all(&mut guest).is_empty(), "port {}", port.number());
        assert!(started.elapsed() < WAIT);
    }
}

/// Holds each connection open until the peer closes it, and sends one unit
/// down `accepted` for each accept.
#[cfg(test)]
fn holding_plane() -> (SocketAddr, Receiver<()>) {
    let (accepted, seen) = channel();
    let addr = plane(move |mut stream: TcpStream| {
        let _ = accepted.send(());
        let mut buffer = [0; 64];
        while stream.read(&mut buffer).is_ok_and(|read| read > 0) {}
    });
    (addr, seen)
}

/// One row holds `SPLICE_CAP` open splices. The next dial closes at once
/// and reaches no plane. After one splice ends, a dial gets through again
/// (decision CH62 rule 14).
#[test]
fn refuses_past_the_connection_cap() {
    let scratch = Scratch::new("cap");
    let (litellm, seen) = holding_plane();
    let upstreams = Upstreams {
        litellm,
        chaperone: closed_port(),
    };
    let _planes = scratch.serve(upstreams);
    let mut held: Vec<UnixStream> = (0..SPLICE_CAP)
        .map(|_| {
            let guest = scratch.dial(HostPort::Litellm);
            seen.recv_timeout(WAIT).unwrap();
            guest
        })
        .collect();

    let mut refused = scratch.dial(HostPort::Litellm);
    assert!(read_all(&mut refused).is_empty());
    assert!(seen.recv_timeout(Duration::from_millis(200)).is_err());

    // The plane sees the EOF of the guest and closes, so the splice ends.
    // The next accept counts it gone.
    held.pop().unwrap().shutdown(Shutdown::Both).unwrap();
    let deadline = Instant::now() + WAIT;
    loop {
        assert!(Instant::now() < deadline, "no dial got through");
        let guest = scratch.dial(HostPort::Litellm);
        if seen.recv_timeout(Duration::from_millis(50)).is_ok() {
            held.push(guest);
            break;
        }
    }
    assert_eq!(held.len(), SPLICE_CAP);
}

/// The drop of the bridges closes each open splice and each listener, as
/// a VM leaves the start table.
#[test]
fn a_drop_closes_the_listeners_and_the_splices() {
    let scratch = Scratch::new("drop");
    let upstreams = Upstreams {
        litellm: plane(echo),
        chaperone: plane(echo),
    };
    let planes = scratch.serve(upstreams);
    let mut guest = scratch.dial(HostPort::Litellm);
    guest.write_all(b"x").unwrap();
    let mut byte = [0];
    guest.read_exact(&mut byte).unwrap();
    assert_eq!(&byte, b"x");

    drop(planes);
    assert!(read_all(&mut guest).is_empty());
    let deadline = Instant::now() + WAIT;
    for port in [HostPort::Litellm, HostPort::Chaperone] {
        let path = scratch.run.host_listener(port);
        while UnixStream::connect(&path).is_ok() {
            assert!(
                Instant::now() < deadline,
                "port {} still listens",
                port.number()
            );
            std::thread::sleep(Duration::from_millis(1));
        }
        let error = UnixStream::connect(&path).unwrap_err();
        assert_eq!(error.kind(), ErrorKind::ConnectionRefused);
    }
}

/// Each of the 64 slots with both rows full: per row the listener and two
/// streams per splice, 64 × 2 × 129, plus the headroom of 4096. The vmm
/// raises its soft fd limit to it at start.
#[test]
fn the_fd_need_holds_every_full_bridge() {
    assert_eq!(SPLICE_CAP, 64);
    assert_eq!(fd_need(), 64 * 2 * 129 + 4096);
}
