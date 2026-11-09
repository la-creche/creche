//! The router over raw bytes on a temp socket (decisions CH25, CH26 rules
//! 3 and 4, CH33 rules 2 and 13, CH34 rule 7). A fake handler set answers
//! each route, or refuses it with the code that the test sets. The bodies
//! are vectors of `rust/crates/wire/vectors/wire.json`.

use std::future::poll_fn;
use std::io::{ErrorKind, Read, Write};
use std::os::unix::net::UnixStream;
use std::path::PathBuf;
use std::pin::Pin;
use std::sync::{Arc, Mutex};
use std::thread::sleep;
use std::time::{Duration, Instant};

use creche_vmm::server::{Relay, Stop, run_routes};
use creche_wire::{
    AcpRoute, CheckError, CheckReply, CheckRequest, CheckRoute, CreateError, CreateRoute,
    CreateSession, DropError, DropRoute, DropTarget, Dropped, HOST_API_HEADER, HOST_API_PROTOCOL,
    JsonRoute, ListError, ListRoute, ListSessions, OpenAcp, RouteError, Serve, ServeUpgrade,
    SessionPage, SessionRow, UpgradeRoute,
};
use hyper::upgrade::Upgraded;
use hyper_util::rt::TokioIo;
use tokio::io::AsyncWrite;
use tokio::runtime::{Builder, Runtime};
use tokio::task::JoinHandle;

const CREATE: &str = r#"{"created_at":1793577600000,"cwd":"/home/david/code/lacreche","family":"chat","id":"3f2b8c1e-9d4a-4e6b-8a7c-0b1d2e3f4a5b","origin":{"door":{}}}"#;
const ROW: &str = r#"{"created_at":1793577600000,"cwd":"/home/david/code/lacreche","family":"chat","id":"3f2b8c1e-9d4a-4e6b-8a7c-0b1d2e3f4a5b","origin":{"door":{}},"title":"Fix the boiler alert","updated_at":1793577660000}"#;
const LIST: &str = r#"{"cursor":null,"cwd":null,"family":"chat"}"#;
const DROP: &str = r#"{"session":{"family":"chat","id":"3f2b8c1e-9d4a-4e6b-8a7c-0b1d2e3f4a5b"}}"#;
const ACP: &str = r#"{"family":"chat","id":"3f2b8c1e-9d4a-4e6b-8a7c-0b1d2e3f4a5b"}"#;
const CHECK: &str = r#"{"family":"code","target":"github.com:443"}"#;
const VERDICT: &str = r#"{"refused":{"reason":"not_listed"}}"#;

const JSON: &str = "content-type: application/json\r\n";
const CLOSE: &str = "connection: close\r\n";
const TOKEN: &str = "upgrade: creche-acp\r\n";

/// The codes and statuses of a route error.
type Statuses = &'static [(&'static str, u16)];

/// The fake handler set and the test server. It is test code, so it may
/// unwrap and index (`rust/clippy.toml`).
#[cfg(test)]
mod fake {
    use super::*;

    /// What the fake handlers do. The test changes it between requests.
    #[derive(Debug, Default)]
    pub(super) struct State {
        /// The code of every refusal, or `None` to answer.
        pub(super) refuse: Option<String>,
        /// The rows of each list page.
        pub(super) rows: usize,
    }

    #[derive(Debug, Clone, Default)]
    pub(super) struct Fake(pub(super) Arc<Mutex<State>>);

    impl Fake {
        /// The error of the code in `State::refuse`, when it is set. A code
        /// that `E` does not have answers, and the test then fails.
        fn refusal<E: RouteError>(&self) -> Result<(), E> {
            let code = self.0.lock().map(|state| state.refuse.clone());
            let code = code
                .unwrap_or_default()
                .map(|code| serde_json::json!({ "code": code }));
            match code.map(serde_json::from_value) {
                Some(Ok(error)) => Err(error),
                _ => Ok(()),
            }
        }
    }

    impl Serve<CreateRoute> for Fake {
        async fn serve(&self, _: CreateSession) -> Result<SessionRow, CreateError> {
            self.refusal()?;
            Ok(serde_json::from_str(ROW).unwrap())
        }
    }

    impl Serve<ListRoute> for Fake {
        async fn serve(&self, _: ListSessions) -> Result<SessionPage, ListError> {
            self.refusal()?;
            let rows = self.0.lock().unwrap().rows;
            // The longest cwd, so few rows pass the cap.
            let cwd = format!("/{}", "a".repeat(4095));
            let row = ROW.replace("/home/david/code/lacreche", &cwd);
            let row = row.strip_suffix('}').unwrap().to_owned() + r#","vm":"running"}"#;
            let page = format!(r#"{{"next":null,"rows":[{}]}}"#, vec![row; rows].join(","));
            Ok(serde_json::from_str(&page).unwrap())
        }
    }

    impl Serve<DropRoute> for Fake {
        async fn serve(&self, _: DropTarget) -> Result<Dropped, DropError> {
            self.refusal()?;
            Ok(Dropped {})
        }
    }

    impl Serve<CheckRoute> for Fake {
        async fn serve(&self, _: CheckRequest) -> Result<CheckReply, CheckError> {
            self.refusal()?;
            Ok(serde_json::from_str(VERDICT).unwrap())
        }
    }

    impl ServeUpgrade<AcpRoute> for Fake {
        type Relay = Hello;

        async fn admit(&self, _: OpenAcp) -> Result<Hello, <AcpRoute as UpgradeRoute>::Error> {
            self.refusal()?;
            Ok(Hello)
        }
    }

    creche_vmm::routes!(Fake);

    /// A relay that writes one line on the stream after 101, then closes it.
    #[derive(Debug)]
    pub(super) struct Hello;

    impl Relay for Hello {
        async fn run(self, mut stream: TokioIo<Upgraded>) {
            let mut line: &[u8] = b"relay\n";
            while !line.is_empty() {
                match poll_fn(|context| Pin::new(&mut stream).poll_write(context, line)).await {
                    Ok(written) if written > 0 => line = &line[written..],
                    _ => return,
                }
            }
            let _ = poll_fn(|context| Pin::new(&mut stream).poll_flush(context)).await;
        }
    }

    /// A server of `Fake` on a temp socket.
    pub(super) struct Server {
        pub(super) fake: Fake,
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
            std::fs::create_dir_all(&dir).unwrap();
            let path = dir.join("vmm.sock");
            let runtime = Builder::new_multi_thread()
                .enable_io()
                .enable_time()
                .build()
                .unwrap();
            let fake = Fake::default();
            let (stop, signal) = Stop::pair();
            let task = runtime.spawn({
                let (path, fake) = (path.clone(), fake.clone());
                async move { run_routes(&path, signal, fake).await }
            });
            Self {
                fake,
                dir,
                path,
                runtime,
                stop,
                task,
            }
        }

        /// The status, the head and the body of the answer to the request
        /// line `line`, with `headers` and `body`. The server closes the
        /// stream after the answer.
        pub(super) fn ask(&self, line: &str, headers: &str, body: &str) -> (u16, String, String) {
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
                "{line} HTTP/1.1\r\nhost: vmm\r\n{HOST_API_HEADER}: {HOST_API_PROTOCOL}\r\n\
                 {headers}content-length: {}\r\n\r\n{body}",
                body.len()
            )
            .unwrap();
            let mut text = String::new();
            if let Err(error) = stream.read_to_string(&mut text) {
                assert_eq!(error.kind(), ErrorKind::ConnectionReset, "{line}");
            }
            let (head, body) = text.split_once("\r\n\r\n").unwrap();
            (
                head.get(9..12).unwrap().parse().unwrap(),
                head.to_owned(),
                body.to_owned(),
            )
        }

        /// Asserts that the request gets the reply of `code` with `status`.
        pub(super) fn refused(
            &self,
            line: &str,
            headers: &str,
            body: &str,
            (code, status): (&str, u16),
        ) {
            let (got, head, reply) = self.ask(line, headers, body);
            assert_eq!(
                (got, reply),
                (status, format!(r#"{{"code":"{code}"}}"#)),
                "{line} {headers:?}: {head}"
            );
        }

        pub(super) fn end(self) {
            self.stop.stop();
            assert_eq!(self.runtime.block_on(self.task).unwrap(), Ok(()));
            std::fs::remove_dir_all(self.dir).unwrap();
        }
    }
}

use fake::Server;

#[test]
fn the_checks_come_in_order() {
    let server = Server::start("router-order");
    let no_route = ("no_route", 404);
    let plain = format!("{CLOSE}content-type: text/plain\r\n");

    // The route and the method come first, so a bad Content-Type on an
    // unknown path gets no_route (decision CH33 rule 13).
    for line in [
        "POST /nothing",
        "GET /nothing",
        "POST /",
        "POST /create/",
        "POST /create?x=1",
    ] {
        server.refused(line, &plain, "", no_route);
    }
    server.refused("GET /create", &plain, "", ("bad_method", 405));
    server.refused("PUT /acp", &plain, "", ("bad_method", 405));

    // An Upgrade route checks the Upgrade header before the body
    // (decisions CH26 rule 3, CH34 rule 7), and names the token. Only the
    // exact token, given once, names it (decision CH40 rule 10).
    for upgrade in [
        "",
        "upgrade: other\r\n",
        "upgrade: creche-acp, other\r\n",
        "upgrade: Creche-ACP\r\n",
        "upgrade: creche-acp\r\nupgrade: creche-acp\r\n",
    ] {
        let headers = format!("{plain}{upgrade}");
        server.refused("POST /acp", &headers, "{}", ("upgrade_required", 426));
        let (_, head, _) = server.ask("POST /acp", &headers, "{}");
        assert!(head.contains("\r\nupgrade: creche-acp"), "{head}");
        assert!(head.contains("\r\nconnection: upgrade"), "{head}");
    }
    server.refused(
        "POST /acp",
        &format!("{plain}{TOKEN}"),
        ACP,
        ("bad_content_type", 415),
    );
    server.refused("POST /create", &plain, CREATE, ("bad_content_type", 415));

    // A strict JSON body of another type gets not_valid.
    let json = format!("{CLOSE}{JSON}");
    for (line, body) in [
        ("POST /create", "{}"),
        ("POST /list", ACP),
        ("POST /drop", "[]"),
    ] {
        server.refused(line, &json, body, ("not_valid", 400));
    }
    server.refused(
        "POST /acp",
        &format!("{json}{TOKEN}"),
        LIST,
        ("not_valid", 400),
    );
    server.end();
}

#[test]
fn each_route_answers_or_refuses_each_code() {
    let server = Server::start("router-codes");
    let json = format!("{CLOSE}{JSON}");
    let upgrade = format!("{json}{TOKEN}");

    // The one table names each route, its error and its body.
    let mut routes: Vec<(&str, Statuses, &str)> = Vec::new();
    macro_rules! each_route {
        (json $route:path) => {
            routes.push((
                <$route as JsonRoute>::NAME,
                <<$route as JsonRoute>::Error as RouteError>::STATUSES,
                &json,
            ));
        };
        (upgrade $route:path) => {
            routes.push((
                <$route as UpgradeRoute>::NAME,
                <<$route as UpgradeRoute>::Error as RouteError>::STATUSES,
                &upgrade,
            ));
        };
    }
    creche_wire::route_table!(each_route);
    let bodies = [
        ("create", CREATE),
        ("list", LIST),
        ("drop", DROP),
        ("acp", ACP),
        ("check", CHECK),
    ];
    assert_eq!(routes.len(), bodies.len());

    for (name, statuses, headers) in routes {
        let body = bodies.iter().find(|(route, _)| *route == name).unwrap().1;
        let line = format!("POST /{name}");
        for &(code, status) in statuses {
            server.fake.0.lock().unwrap().refuse = Some(code.to_owned());
            server.refused(&line, headers, body, (code, status));
        }
        server.fake.0.lock().unwrap().refuse = None;
        if name == "acp" {
            let headers = format!("{JSON}connection: upgrade\r\n{TOKEN}");
            let (status, head, stream) = server.ask(&line, &headers, body);
            assert_eq!((status, stream.as_str()), (101, "relay\n"), "{head}");
            assert!(head.contains("\r\nupgrade: creche-acp"), "{head}");
            // RFC 9110 section 7.8: a 101 that carries Upgrade also says
            // Connection: upgrade.
            assert!(head.contains("\r\nconnection: upgrade"), "{head}");
            continue;
        }
        let reply = match name {
            "create" => ROW,
            "list" => r#"{"next":null,"rows":[]}"#,
            "check" => VERDICT,
            _ => "{}",
        };
        let (status, head, got) = server.ask(&line, headers, body);
        assert_eq!((status, got.as_str()), (200, reply), "{head}");
        assert!(head.contains("content-type: application/json"), "{head}");
    }
    server.end();
}

#[test]
fn a_reply_over_the_cap_gets_internal() {
    let server = Server::start("router-cap");
    let json = format!("{CLOSE}{JSON}");
    // 256 rows of a 4096-byte cwd pass 1 MiB. One row does not.
    server.fake.0.lock().unwrap().rows = 256;
    server.refused("POST /list", &json, LIST, ("internal", 500));
    server.fake.0.lock().unwrap().rows = 1;
    let (status, head, _) = server.ask("POST /list", &json, LIST);
    assert_eq!(status, 200, "{head}");
    server.end();
}
