//! The `acp` handler over raw bytes on a temp socket (decisions CH21-9,
//! CH26 rule 5, CH33 rules 6 and 7, CH34 rules 6 and 7). A fake hook
//! starts each VM, counts the starts, and fails with the fault that the
//! test sets. A fake vsock socket plays Cloud Hypervisor and guest port
//! 1201: it answers `CONNECT`, sends `hello`, then echoes (decisions
//! CH13, CH20, CH34 rule 3).

use std::io::{ErrorKind, Read, Write};
use std::net::Shutdown;
use std::os::unix::net::{UnixListener, UnixStream};
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
use std::thread::{sleep, spawn};
use std::time::{Duration, Instant};

use creche_vmm::control::READY_WAIT;
use creche_vmm::handlers::acp::{Acp, StartFault, StartVm};
use creche_vmm::relay::{AcpRelay, Door, DropMark, Relays};
use creche_vmm::server::{Stop, run_routes};
use creche_vmm_store::layout::StateRoot;
use creche_vmm_store::rows::Rows;
use creche_wire::{
    AcpError, AcpRoute, CreateError, CreateRoute, CreateSession, DropError, DropRoute, DropTarget,
    Dropped, HOST_API_HEADER, HOST_API_PROTOCOL, ListError, ListRoute, ListSessions, OpenAcp,
    RouteError, Serve, ServeUpgrade, SessionId, SessionPage, SessionRow,
};
use tokio::runtime::{Builder, Runtime};
use tokio::task::JoinHandle;

const A: &str = "3f2b8c1e-9d4a-4e6b-8a7c-0b1d2e3f4a5b";
const B: &str = "1a2b3c4d-5e6f-4a1b-8c2d-3e4f5a6b7c8d";

/// The fake hook, its state and the test server. It is test code, so it
/// may unwrap and index (`rust/clippy.toml`).
#[cfg(test)]
mod fake {
    use super::*;

    #[derive(Debug, Default)]
    pub(super) struct State {
        /// How many starts the hook ran.
        pub(super) starts: usize,
        /// The fault of each start, or `None` for a start that works.
        pub(super) fault: Option<StartFault>,
        /// When set, each start marks a drop of its session and keeps the
        /// mark here, as a drop that begins during the start.
        pub(super) drop_during: Option<Relays>,
        pub(super) marks: Vec<DropMark>,
        /// The fake vsock socket that each start returns.
        pub(super) vsock: PathBuf,
    }

    #[derive(Debug, Clone, Default)]
    pub(super) struct Hook(pub(super) Arc<Mutex<State>>);

    impl StartVm for Hook {
        async fn start(&self, row: &SessionRow) -> Result<PathBuf, StartFault> {
            let mut state = self.0.lock().unwrap();
            state.starts += 1;
            if let Some(relays) = state.drop_during.clone() {
                state.marks.push(relays.mark_drop(&row.id));
            }
            state.fault.map_or_else(|| Ok(state.vsock.clone()), Err)
        }
    }

    /// A handler set with the real `acp` handler. The JSON routes are not
    /// under test.
    #[derive(Debug)]
    pub(super) struct Set(Acp<Hook>);

    impl Serve<CreateRoute> for Set {
        async fn serve(&self, _: CreateSession) -> Result<SessionRow, CreateError> {
            Err(CreateError::Internal {})
        }
    }

    impl Serve<ListRoute> for Set {
        async fn serve(&self, _: ListSessions) -> Result<SessionPage, ListError> {
            Err(ListError::Internal {})
        }
    }

    impl Serve<DropRoute> for Set {
        async fn serve(&self, _: DropTarget) -> Result<Dropped, DropError> {
            Err(DropError::Internal {})
        }
    }

    impl ServeUpgrade<AcpRoute> for Set {
        type Relay = AcpRelay;

        async fn admit(&self, request: OpenAcp) -> Result<AcpRelay, AcpError> {
            self.0.admit(request).await
        }
    }

    creche_vmm::routes!(Set);

    /// How the fake guest answers a `CONNECT`.
    #[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
    pub(super) enum Answer {
        /// `OK` and `hello` in one write, then an echo.
        #[default]
        Echo,
        /// A line that is not `OK <port>`, so the dial fails at once.
        Refuse,
        /// No answer at all, so the dial waits.
        Silent,
    }

    /// What the fake guest saw: each `CONNECT` line, and how many links
    /// the vmm closed. `answer` sets the answer to the next `CONNECT`.
    #[derive(Debug, Default)]
    pub(super) struct Guest {
        pub(super) connects: Vec<String>,
        pub(super) closes: usize,
        pub(super) answer: Answer,
    }

    /// Reads the `hello` that the fake guest sends right after `OK`.
    pub(super) fn hello(stream: &mut UnixStream) {
        let mut hello = [0_u8; 6];
        stream.read_exact(&mut hello).unwrap();
        assert_eq!(&hello, b"hello\n");
    }

    /// Plays Cloud Hypervisor and guest port 1201 on `path`. Each link
    /// gets the answer that `Guest::answer` sets. Then the guest echoes
    /// until the vmm closes the link.
    fn guest(path: &Path, seen: Arc<Mutex<Guest>>) {
        let listener = UnixListener::bind(path).unwrap();
        spawn(move || {
            for stream in listener.incoming() {
                let (mut stream, seen) = (stream.unwrap(), seen.clone());
                spawn(move || {
                    let mut line = Vec::new();
                    let mut byte = [0_u8];
                    while line.last() != Some(&b'\n') && stream.read(&mut byte).unwrap() == 1 {
                        line.push(byte[0]);
                    }
                    let line = String::from_utf8(line).unwrap();
                    let answer = {
                        let mut seen = seen.lock().unwrap();
                        seen.connects.push(line);
                        seen.answer
                    };
                    match answer {
                        Answer::Echo => stream.write_all(b"OK 1073741824\nhello\n").unwrap(),
                        // The vmm may close the link before it reads this.
                        Answer::Refuse => drop(stream.write_all(b"ERR\n")),
                        Answer::Silent => {}
                    }
                    let mut buffer = [0_u8; 65536];
                    while let Ok(read @ 1..) = stream.read(&mut buffer) {
                        if stream.write_all(&buffer[..read]).is_err() {
                            break;
                        }
                    }
                    seen.lock().unwrap().closes += 1;
                });
            }
        });
    }

    /// The server of `Set` on a temp socket, with one row: `A` of `chat`.
    pub(super) struct Server {
        pub(super) hook: Hook,
        pub(super) relays: Relays,
        pub(super) guest: Arc<Mutex<Guest>>,
        dir: PathBuf,
        path: PathBuf,
        runtime: Runtime,
        stop: Stop,
        task: JoinHandle<Result<(), creche_vmm::ServeError>>,
    }

    impl Server {
        pub(super) fn start(test: &str) -> Self {
            let dir =
                std::env::temp_dir().join(format!("creche-vmm-{}-{test}", std::process::id()));
            let _ = std::fs::remove_dir_all(&dir);
            std::fs::create_dir_all(&dir).unwrap();
            let mut rows = Rows::open(StateRoot::new(dir.join("state")).unwrap()).unwrap();
            let create = format!(
                r#"{{"created_at":1793577600000,"cwd":"/home/david","family":"chat","id":"{A}","origin":{{"door":{{}}}}}}"#
            );
            rows.create(serde_json::from_str(&create).unwrap()).unwrap();

            let (hook, relays) = (Hook::default(), Relays::default());
            let seen = Arc::new(Mutex::new(Guest::default()));
            hook.0.lock().unwrap().vsock = dir.join("vsock.sock");
            guest(&dir.join("vsock.sock"), seen.clone());
            let set = Set(Acp::new(
                Arc::new(Mutex::new(rows)),
                relays.clone(),
                hook.clone(),
            ));
            let path = dir.join("vmm.sock");
            let runtime = Builder::new_multi_thread()
                .enable_io()
                .enable_time()
                .build()
                .unwrap();
            let (stop, signal) = Stop::pair();
            let task = runtime.spawn({
                let path = path.clone();
                async move { run_routes(&path, signal, set).await }
            });
            Self {
                hook,
                relays,
                guest: seen,
                dir,
                path,
                runtime,
                stop,
                task,
            }
        }

        pub(super) fn starts(&self) -> usize {
            self.hook.0.lock().unwrap().starts
        }

        /// The status and the body of the answer to an Upgrade of `id` of
        /// `family`, and the stream. After 101 the stream is the relay.
        pub(super) fn upgrade(&self, family: &str, id: &str) -> (u16, String, UnixStream) {
            let deadline = Instant::now() + Duration::from_secs(10);
            let mut stream = loop {
                match UnixStream::connect(&self.path) {
                    Err(_) if Instant::now() < deadline => sleep(Duration::from_millis(10)),
                    connected => break connected.unwrap(),
                }
            };
            stream
                .set_read_timeout(Some(Duration::from_secs(10)))
                .unwrap();
            let body = format!(r#"{{"family":"{family}","id":"{id}"}}"#);
            write!(
                stream,
                "POST /acp HTTP/1.1\r\nhost: vmm\r\n{HOST_API_HEADER}: {HOST_API_PROTOCOL}\r\n\
                 content-type: application/json\r\nconnection: upgrade\r\n\
                 upgrade: creche-acp\r\ncontent-length: {}\r\n\r\n{body}",
                body.len()
            )
            .unwrap();
            let mut head = Vec::new();
            while !head.ends_with(b"\r\n\r\n") {
                let mut byte = [0_u8];
                stream.read_exact(&mut byte).unwrap();
                head.push(byte[0]);
            }
            let head = String::from_utf8(head).unwrap().to_lowercase();
            let status = head.get(9..12).unwrap().parse().unwrap();
            let length = head
                .lines()
                .find_map(|line| line.strip_prefix("content-length: "))
                .map_or(0, |length| length.parse().unwrap());
            let mut body = vec![0_u8; length];
            stream.read_exact(&mut body).unwrap();
            (status, String::from_utf8(body).unwrap(), stream)
        }

        /// Asserts that an Upgrade of `id` of `family` gets `error`.
        pub(super) fn refused(&self, family: &str, id: &str, error: AcpError) {
            let (status, body, _) = self.upgrade(family, id);
            let want = (error.status(), serde_json::to_string(&error).unwrap());
            assert_eq!((status, body), want, "{family} {id}");
        }

        pub(super) fn end(self) {
            self.stop.stop();
            assert_eq!(self.runtime.block_on(self.task).unwrap(), Ok(()));
            std::fs::remove_dir_all(self.dir).unwrap();
        }
    }
}

use fake::{Answer, Server, hello};

/// Asserts that the relay closed `stream`. The `hello` of the guest may
/// come first.
fn closed(mut stream: UnixStream) {
    let mut bytes = [0_u8; 64];
    loop {
        match stream.read(&mut bytes) {
            Ok(0) => return,
            Ok(_) => {}
            Err(error) => return assert_eq!(error.kind(), ErrorKind::ConnectionReset),
        }
    }
}

/// Waits up to 10 s for `done`.
fn eventually(done: impl Fn() -> bool) {
    let deadline = Instant::now() + Duration::from_secs(10);
    while !done() {
        assert!(Instant::now() < deadline, "no change within 10 s");
        sleep(Duration::from_millis(10));
    }
}

/// Bytes cross both ways with no cap, so 2 MiB pass the 1 MiB body cap
/// (decision CH34 rule 3). The door stays open until the client closes.
#[test]
fn the_relay_carries_bytes_both_ways() {
    let server = Server::start("acp-relay");
    let (status, _, mut stream) = server.upgrade("chat", A);
    assert_eq!(status, 101);
    hello(&mut stream);
    let sent: Vec<u8> = (0..2 << 20)
        .map(|byte: u32| byte.to_le_bytes()[0])
        .collect();
    let mut writer = stream.try_clone().unwrap();
    let write = spawn({
        let sent = sent.clone();
        move || writer.write_all(&sent).unwrap()
    });
    let mut echoed = vec![0_u8; sent.len()];
    stream.read_exact(&mut echoed).unwrap();
    write.join().unwrap();
    assert!(echoed == sent);
    assert_eq!(server.guest.lock().unwrap().connects, ["CONNECT 1201\n"]);

    let id: SessionId = A.parse().unwrap();
    assert_eq!(server.relays.door(&id), Door::Open);
    drop(stream);
    eventually(|| matches!(server.relays.door(&id), Door::ClosedAt(_)));
    eventually(|| server.guest.lock().unwrap().closes == 1);
    server.end();
}

/// A client that shuts down its write half after its last request still
/// gets the echo of the guest, then an EOF. The guest sees the EOF and
/// closes, and then the relay ends.
#[test]
fn a_half_close_keeps_the_reply_of_the_guest() {
    let server = Server::start("acp-half");
    let (status, _, mut stream) = server.upgrade("chat", A);
    assert_eq!(status, 101);
    hello(&mut stream);
    stream.write_all(b"ping\n").unwrap();
    stream.shutdown(Shutdown::Write).unwrap();
    let mut reply = Vec::new();
    stream.read_to_end(&mut reply).unwrap();
    assert_eq!(reply, b"ping\n");
    eventually(|| server.guest.lock().unwrap().closes == 1);
    let id: SessionId = A.parse().unwrap();
    eventually(|| matches!(server.relays.door(&id), Door::ClosedAt(_)));
    server.end();
}

/// A dial of guest port 1201 that fails after 101 closes the client
/// stream and frees the session. The door records the close.
// The narrow reading of the CONTRACT-QUESTION in `AcpRelay::run`.
#[test]
fn a_failed_dial_closes_the_client_stream() {
    let server = Server::start("acp-dial-refused");
    server.guest.lock().unwrap().answer = Answer::Refuse;
    let (status, _, stream) = server.upgrade("chat", A);
    assert_eq!(status, 101);
    closed(stream);
    let id: SessionId = A.parse().unwrap();
    eventually(|| matches!(server.relays.door(&id), Door::ClosedAt(_)));
    server.guest.lock().unwrap().answer = Answer::Echo;
    let (status, _, mut stream) = server.upgrade("chat", A);
    assert_eq!(status, 101);
    hello(&mut stream);
    assert_eq!(server.guest.lock().unwrap().connects.len(), 2);
    server.end();
}

/// A drop during the dial stops it at once, well before `READY_WAIT`
/// (decision CH26 rule 5).
#[test]
fn a_drop_during_the_dial_closes_the_client_stream() {
    let server = Server::start("acp-dial-drop");
    server.guest.lock().unwrap().answer = Answer::Silent;
    let (status, _, stream) = server.upgrade("chat", A);
    assert_eq!(status, 101);
    eventually(|| server.guest.lock().unwrap().connects.len() == 1);
    let start = Instant::now();
    let mark = server.relays.mark_drop(&A.parse().unwrap());
    closed(stream);
    assert!(start.elapsed() < READY_WAIT / 2, "{:?}", start.elapsed());
    eventually(|| server.guest.lock().unwrap().closes == 1);
    drop(mark);
    server.end();
}

/// A guest that never answers the dial gets `READY_WAIT`, then the relay
/// closes the client stream (decision CH43 rule 5). The test takes 10 s.
#[test]
fn a_dial_with_no_answer_ends_after_ready_wait() {
    let server = Server::start("acp-dial-silent");
    server.guest.lock().unwrap().answer = Answer::Silent;
    let (status, _, stream) = server.upgrade("chat", A);
    assert_eq!(status, 101);
    let start = Instant::now();
    stream.set_read_timeout(Some(READY_WAIT * 2)).unwrap();
    closed(stream);
    let waited = start.elapsed();
    assert!(
        waited > READY_WAIT / 2 && waited < READY_WAIT * 2,
        "{waited:?}"
    );
    eventually(|| server.guest.lock().unwrap().closes == 1);
    server.end();
}

/// The stop signal of a drop closes the client end and the guest end
/// (decision CH26 rule 5).
#[test]
fn a_drop_closes_both_ends() {
    let server = Server::start("acp-relay-drop");
    let (status, _, mut stream) = server.upgrade("chat", A);
    assert_eq!(status, 101);
    hello(&mut stream);
    let mark = server.relays.mark_drop(&A.parse().unwrap());
    closed(stream);
    eventually(|| server.guest.lock().unwrap().closes == 1);
    drop(mark);
    server.end();
}

/// A refused Upgrade records no door close (decision CH19 (6)).
#[test]
fn an_unknown_id_gets_not_found_with_no_start() {
    let server = Server::start("acp-unknown");
    server.refused("chat", B, AcpError::NotFound {});
    // Another family does not own the id (decision CH33 rule 5).
    server.refused("code", A, AcpError::NotFound {});
    assert_eq!(server.starts(), 0);
    assert_eq!(server.relays.door(&B.parse().unwrap()), Door::NotOpened);
    assert_eq!(server.relays.door(&A.parse().unwrap()), Door::NotOpened);
    server.end();
}

#[test]
fn a_second_upgrade_gets_busy_until_the_first_ends() {
    let server = Server::start("acp-busy");
    let (status, _, first) = server.upgrade("chat", A);
    assert_eq!(status, 101);
    server.refused("chat", A, AcpError::Busy {});
    assert_eq!(server.starts(), 1);

    // The relay frees the session when the client closes the stream.
    drop(first);
    let deadline = Instant::now() + Duration::from_secs(10);
    loop {
        match server.upgrade("chat", A) {
            (101, _, _) => break,
            (409, _, _) if Instant::now() < deadline => sleep(Duration::from_millis(10)),
            (status, body, _) => panic!("{status} {body}"),
        }
    }
    server.end();
}

/// Another family gets `not_found` while the owner holds the session, so
/// it learns nothing of the session (decisions CH33 rule 5, CH34 rule 7).
/// Its Upgrade starts no VM and leaves the claim of the owner in place.
#[test]
fn another_family_gets_not_found_while_the_owner_holds_the_session() {
    let server = Server::start("acp-foreign");
    let (status, _, _first) = server.upgrade("chat", A);
    assert_eq!(status, 101);
    server.refused("code", A, AcpError::NotFound {});
    server.refused("chat", A, AcpError::Busy {});
    assert_eq!(server.starts(), 1);
    assert_eq!(server.relays.door(&A.parse().unwrap()), Door::Open);
    server.end();
}

#[test]
fn a_start_fault_answers_before_101() {
    let server = Server::start("acp-fault");
    // Each code is written out, so a wrong map in `StartFault::error`
    // fails the body check (decision CH34 rule 6).
    let faults = [
        (StartFault::HostFull, AcpError::HostFull {}, 503),
        (
            StartFault::SandboxStartFailed,
            AcpError::SandboxStartFailed {},
            502,
        ),
        (
            StartFault::EgressAssertFailed,
            AcpError::EgressAssertFailed {},
            502,
        ),
    ];
    for (fault, error, status) in faults {
        server.hook.0.lock().unwrap().fault = Some(fault);
        assert_eq!(error.status(), status);
        server.refused("chat", A, error);
    }
    assert_eq!(server.starts(), 3);
    // A start fault opened no door link, so it records no close (decision
    // CH19 (6)).
    assert_eq!(server.relays.door(&A.parse().unwrap()), Door::NotOpened);
    // A refusal frees the session at once.
    server.hook.0.lock().unwrap().fault = None;
    assert_eq!(server.upgrade("chat", A).0, 101);
    server.end();
}

#[test]
fn a_drop_stops_the_relay_and_refuses_upgrades() {
    let server = Server::start("acp-drop");
    let (status, _, stream) = server.upgrade("chat", A);
    assert_eq!(status, 101);
    let id: SessionId = A.parse().unwrap();
    let mark = server.relays.mark_drop(&id);
    closed(stream);
    // An Upgrade under the mark gets not_found with no start (decision
    // CH34 rule 6). A second mark keeps it after the first ends.
    let second = server.relays.mark_drop(&id);
    drop(mark);
    server.refused("chat", A, AcpError::NotFound {});
    assert_eq!(server.starts(), 1);
    drop(second);
    assert_eq!(server.upgrade("chat", A).0, 101);
    server.end();
}

#[test]
fn a_drop_during_the_start_gets_not_found() {
    let server = Server::start("acp-drop-start");
    server.hook.0.lock().unwrap().drop_during = Some(server.relays.clone());
    server.refused("chat", A, AcpError::NotFound {});
    assert_eq!(server.starts(), 1);
    server.end();
}
