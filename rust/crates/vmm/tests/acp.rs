//! The `acp` handler over raw bytes on a temp socket (decisions CH21-9,
//! CH26 rule 5, CH33 rules 6 and 7, CH34 rules 6 and 7). A fake hook
//! starts each VM, counts the starts, and fails with the fault that the
//! test sets.

use std::io::{ErrorKind, Read, Write};
use std::os::unix::net::UnixStream;
use std::path::PathBuf;
use std::sync::{Arc, Mutex};
use std::thread::sleep;
use std::time::{Duration, Instant};

use creche_vmm::handlers::acp::{Acp, StartFault, StartVm};
use creche_vmm::relay::{AcpRelay, DropMark, Relays};
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
    }

    #[derive(Debug, Clone, Default)]
    pub(super) struct Hook(pub(super) Arc<Mutex<State>>);

    impl StartVm for Hook {
        async fn start(&self, row: &SessionRow) -> Result<(), StartFault> {
            let mut state = self.0.lock().unwrap();
            state.starts += 1;
            if let Some(relays) = state.drop_during.clone() {
                state.marks.push(relays.mark_drop(&row.id));
            }
            state.fault.map_or(Ok(()), Err)
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

    /// The server of `Set` on a temp socket, with one row: `A` of `chat`.
    pub(super) struct Server {
        pub(super) hook: Hook,
        pub(super) relays: Relays,
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

use fake::Server;

/// Asserts that the relay closed `stream`.
fn closed(mut stream: UnixStream) {
    match stream.read(&mut [0_u8; 1]) {
        Ok(read) => assert_eq!(read, 0),
        Err(error) => assert_eq!(error.kind(), ErrorKind::ConnectionReset),
    }
}

#[test]
fn an_unknown_id_gets_not_found_with_no_start() {
    let server = Server::start("acp-unknown");
    server.refused("chat", B, AcpError::NotFound {});
    // Another family does not own the id (decision CH33 rule 5).
    server.refused("code", A, AcpError::NotFound {});
    assert_eq!(server.starts(), 0);
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
