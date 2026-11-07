//! The `check` handler over raw bytes on a temp socket (decisions CH6-3,
//! CH69 rules 1 to 3, CH70 rule 2). A scratch family root holds the
//! family `code` with an `egress.json`. Each verdict of the route is the
//! verdict of `egress::rule` on the same list and target. No test reaches
//! the network: a listed name resolves through `/etc/hosts` (`localhost`).

use std::io::{ErrorKind, Read, Write};
use std::os::unix::net::UnixStream;
use std::path::PathBuf;
use std::thread::sleep;
use std::time::{Duration, Instant};

use creche_vmm::egress::rule::{Target, decide};
use creche_vmm::handlers::check::Checks;
use creche_vmm::relay::AcpRelay;
use creche_vmm::server::{Stop, run_routes};
use creche_vmm_confine::family::FamilyRoot;
use creche_wire::{
    AcpError, AcpRoute, CheckError, CheckReply, CheckRequest, CheckRoute, CreateError, CreateRoute,
    CreateSession, DropError, DropRoute, DropTarget, Dropped, FamilyEgress, HOST_API_HEADER,
    HOST_API_PROTOCOL, ListError, ListRoute, ListSessions, OpenAcp, Serve, ServeUpgrade,
    SessionPage, SessionRow,
};
use serde_json::json;
use tokio::runtime::{Builder, Runtime};
use tokio::task::JoinHandle;

/// The allowlist of the family `code`.
const EGRESS: &str =
    r#"{"hosts":[{"host":"github.com","port":443},{"host":"localhost","port":443}]}"#;

/// The targets of `tests/egress_rule.rs`, one per kind of verdict.
const TARGETS: [&str; 17] = [
    "140.82.112.3:443",
    "[2606:50c0:8000::153]:443",
    "::ffff:127.0.0.1:443",
    "2606:50c0:8000:0:0:0:0:153:443",
    "fe80:0:0:0:0:0:0:1:443",
    "[::ffff:127.0.0.1]:443",
    "2130706433:443",
    "github.com",
    "github.com:0",
    "github.com.:443",
    "*.github.com:443",
    "pypi.org:443",
    "github.com:8443",
    "localhost:443",
    "LOCALHOST:443",
    "localhost:80",
    "",
];

/// The handler set and the test server. It is test code, so it may unwrap
/// and index (`rust/clippy.toml`).
#[cfg(test)]
mod fake {
    use super::*;

    /// The handler set: the real `check`, and a refusal for each other
    /// route.
    #[derive(Debug)]
    pub(super) struct Set(Checks);

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

        async fn admit(&self, _: OpenAcp) -> Result<AcpRelay, AcpError> {
            Err(AcpError::Internal {})
        }
    }

    impl Serve<CheckRoute> for Set {
        async fn serve(&self, request: CheckRequest) -> Result<CheckReply, CheckError> {
            self.0.serve(request).await
        }
    }

    creche_vmm::routes!(Set);

    /// The server of `Set` on a temp socket, over a scratch family root.
    pub(super) struct Server {
        pub(super) dir: PathBuf,
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
            std::fs::create_dir_all(dir.join("families/code/config")).unwrap();
            let families = FamilyRoot::new(dir.join("families")).unwrap();
            let set = Set(Checks::new(families));
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
                dir,
                path,
                runtime,
                stop,
                task,
            }
        }

        /// Writes `egress.json` of the family `code`.
        pub(super) fn egress(&self, text: &str) {
            std::fs::write(self.dir.join("families/code/egress.json"), text).unwrap();
        }

        /// The status and the body of the answer to `POST /check` with
        /// `body` and the protocol number `protocol`.
        pub(super) fn ask(&self, protocol: u32, body: &str) -> (u16, String) {
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
            write!(
                stream,
                "POST /check HTTP/1.1\r\nhost: vmm\r\n{HOST_API_HEADER}: {protocol}\r\n\
                 content-type: application/json\r\nconnection: close\r\n\
                 content-length: {}\r\n\r\n{body}",
                body.len()
            )
            .unwrap();
            let mut text = String::new();
            if let Err(error) = stream.read_to_string(&mut text) {
                assert_eq!(error.kind(), ErrorKind::ConnectionReset);
            }
            let (head, body) = text.split_once("\r\n\r\n").unwrap();
            (head.get(9..12).unwrap().parse().unwrap(), body.to_owned())
        }

        /// The verdict of the route on `target` for `family`.
        pub(super) fn check(&self, family: &str, target: &str) -> CheckReply {
            let request = json!({"family": family, "target": target});
            let (status, body) = self.ask(HOST_API_PROTOCOL, &request.to_string());
            assert_eq!(status, 200, "{target}: {body}");
            serde_json::from_str(&body).unwrap()
        }

        /// The verdict of `egress::rule` on `target` for the list `list`.
        pub(super) fn rule(&self, list: &FamilyEgress, target: &str) -> CheckReply {
            let reason = match Target::parse(target) {
                Ok(target) => return self.runtime.block_on(decide(list, &target)),
                Err(reason) => reason,
            };
            serde_json::from_value(json!({"refused": {"reason": reason}})).unwrap()
        }

        pub(super) fn end(self) {
            self.stop.stop();
            assert_eq!(self.runtime.block_on(self.task).unwrap(), Ok(()));
            std::fs::remove_dir_all(self.dir).unwrap();
        }
    }
}

use fake::Server;

/// The refusal of `reason` as the route writes it.
#[cfg(test)]
fn refused(reason: &str) -> CheckReply {
    serde_json::from_value(json!({"refused": {"reason": reason}})).unwrap()
}

#[test]
fn check_refuses_an_ip_literal() {
    let server = Server::start("check-literal");
    server.egress(EGRESS);
    for target in ["140.82.112.3:443", "[::1]:443", "2606:50c0:8000::153"] {
        assert_eq!(
            server.check("code", target),
            refused("ip_literal"),
            "{target}"
        );
    }
    server.end();
}

/// An absent `egress.json`, and a family the vmm does not know, allow no
/// egress (decision CH69 rule 3).
#[test]
fn check_reads_an_absent_file_as_no_egress() {
    let server = Server::start("check-absent");
    assert_eq!(
        server.check("code", "github.com:443"),
        refused("not_listed")
    );
    assert_eq!(
        server.check("chat", "github.com:443"),
        refused("not_listed")
    );
    server.end();
}

/// Each target gets the same verdict through the route and through
/// `decide` on the same list (decision CH6-3: the same function).
#[test]
fn check_answers_the_rule_verdict_for_each_case() {
    let server = Server::start("check-cases");
    server.egress(EGRESS);
    let list: FamilyEgress = serde_json::from_str(EGRESS).unwrap();
    for target in TARGETS {
        assert_eq!(
            server.check("code", target),
            server.rule(&list, target),
            "{target}"
        );
    }
    assert_eq!(server.check("code", "localhost:443"), refused("not_public"));
    assert_eq!(server.check("code", "pypi.org:443"), refused("not_listed"));
    server.end();
}

/// An `egress.json` that is present and does not read allows nothing: the
/// refusal `not_valid` with status 200, never `internal` 500 (decision
/// CH70 rule 2). The test reads the raw status and body, so a handler that
/// answers 500 again fails it.
#[test]
fn a_bad_egress_file_gets_not_valid() {
    let server = Server::start("check-bad");
    let request = json!({"family": "code", "target": "github.com:443"}).to_string();
    let not_valid = r#"{"refused":{"reason":"not_valid"}}"#.to_owned();
    // A valid list padded past the cap of 64 KiB (decision CH70 rule 1).
    let past_cap = format!(r#"{{"hosts":[]}}{}"#, " ".repeat(64 * 1024));
    for text in [
        r#"{"hosts":["github.com"]}"#,
        "{",
        r#"{"hosts":[],"repos":[]}"#,
        &past_cap,
    ] {
        server.egress(text);
        let answer = server.ask(HOST_API_PROTOCOL, &request);
        assert_eq!(
            answer,
            (200, not_valid.clone()),
            "{}",
            text.get(..40).unwrap_or(text)
        );
    }
    std::fs::remove_file(server.dir.join("families/code/egress.json")).unwrap();
    std::fs::create_dir(server.dir.join("families/code/egress.json")).unwrap();
    let answer = server.ask(HOST_API_PROTOCOL, &request);
    assert_eq!(answer, (200, not_valid), "a dir as egress.json");
    server.end();
}

/// A client of protocol 1, from before `check`, gets the mismatch reply
/// of this protocol (decisions CH33 rule 1, CH69 rule 1).
#[test]
fn an_old_protocol_number_gets_a_mismatch() {
    let server = Server::start("check-old");
    server.egress(EGRESS);
    let request = json!({"family": "code", "target": "github.com:443"}).to_string();
    let reply = format!(r#"{{"code":"protocol_mismatch","protocol":{HOST_API_PROTOCOL}}}"#);
    assert_eq!(server.ask(HOST_API_PROTOCOL - 1, &request), (412, reply));
    server.end();
}
