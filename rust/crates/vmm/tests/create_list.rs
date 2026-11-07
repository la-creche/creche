//! The `create` and `list` handlers over raw bytes on a temp socket
//! (decisions CH21-4, CH21-5, CH25 rule 2, CH33 rules 2, 3 and 12, CH34
//! rule 4). The request bodies are vectors of
//! `rust/crates/wire/vectors/wire.json`. A scratch family root holds the
//! family `chat` with its `config/`.

use std::io::{ErrorKind, Read, Write};
use std::os::unix::net::UnixStream;
use std::path::PathBuf;
use std::sync::{Arc, Mutex};
use std::thread::sleep;
use std::time::{Duration, Instant};

use creche_vmm::handlers::create::Creates;
use creche_vmm::handlers::list::Lists;
use creche_vmm::relay::AcpRelay;
use creche_vmm::server::{Stop, run_routes};
use creche_vmm_confine::family::FamilyRoot;
use creche_vmm_store::layout::StateRoot;
use creche_vmm_store::rows::{PAGE_ROWS, Rows};
use creche_wire::{
    AcpError, AcpRoute, CheckError, CheckReply, CheckRequest, CheckRoute, CreateError, CreateRoute,
    CreateSession, DropError, DropRoute, DropTarget, Dropped, HOST_API_BODY_CAP, HOST_API_HEADER,
    HOST_API_PROTOCOL, ListError, ListRoute, ListSessions, OpenAcp, Serve, ServeUpgrade,
    SessionPage, SessionRow,
};
use serde_json::{Value, json};
use tokio::runtime::{Builder, Runtime};
use tokio::task::JoinHandle;

const VECTORS: &str = include_str!("../../wire/vectors/wire.json");

/// The handler set and the test server. It is test code, so it may unwrap
/// and index (`rust/clippy.toml`).
#[cfg(test)]
mod fake {
    use super::*;

    /// A handler set with the real `create` and `list` handlers. The
    /// other routes are not under test.
    #[derive(Debug)]
    pub(super) struct Set(Creates, Lists);

    impl Serve<CreateRoute> for Set {
        async fn serve(&self, request: CreateSession) -> Result<SessionRow, CreateError> {
            self.0.serve(request).await
        }
    }

    impl Serve<ListRoute> for Set {
        async fn serve(&self, request: ListSessions) -> Result<SessionPage, ListError> {
            self.1.serve(request).await
        }
    }

    impl Serve<DropRoute> for Set {
        async fn serve(&self, _: DropTarget) -> Result<Dropped, DropError> {
            Err(DropError::Internal {})
        }
    }

    impl Serve<CheckRoute> for Set {
        async fn serve(&self, _: CheckRequest) -> Result<CheckReply, CheckError> {
            Err(CheckError::Internal {})
        }
    }

    impl ServeUpgrade<AcpRoute> for Set {
        type Relay = AcpRelay;

        async fn admit(&self, _: OpenAcp) -> Result<AcpRelay, AcpError> {
            Err(AcpError::Internal {})
        }
    }

    creche_vmm::routes!(Set);

    /// The `json` of the vector of `name` whose note is `note`.
    pub(super) fn vector(name: &str, note: &str) -> String {
        let vectors: Value = serde_json::from_str(VECTORS).unwrap();
        let found = vectors[name].as_array().unwrap().iter();
        let mut found = found.filter(|vector| vector["note"] == note);
        found.next().unwrap()["json"].as_str().unwrap().to_owned()
    }

    /// The row that `create` makes: `title` is `null` and `updated_at` is
    /// `created_at` (decision CH21-4).
    pub(super) fn row_of(create: &str) -> Value {
        let mut row: Value = serde_json::from_str(create).unwrap();
        row["title"] = Value::Null;
        row["updated_at"] = row["created_at"].clone();
        row
    }

    /// The server of `Set` on a temp socket, with no rows.
    pub(super) struct Server {
        pub(super) dir: PathBuf,
        pub(super) state: StateRoot,
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
            std::fs::create_dir_all(dir.join("families/chat/config")).unwrap();
            let state = StateRoot::new(dir.join("state")).unwrap();
            let rows = Arc::new(Mutex::new(Rows::open(state.clone()).unwrap()));
            let families = FamilyRoot::new(dir.join("families")).unwrap();
            let set = Set(Creates::new(Arc::clone(&rows), families), Lists::new(rows));
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
                state,
                path,
                runtime,
                stop,
                task,
            }
        }

        /// The status and the body of the answer to `POST /<route>` with
        /// `body`. The server closes the stream after the answer.
        pub(super) fn ask(&self, route: &str, body: &str) -> (u16, String) {
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
                "POST /{route} HTTP/1.1\r\nhost: vmm\r\n{HOST_API_HEADER}: {HOST_API_PROTOCOL}\r\n\
                 content-type: application/json\r\nconnection: close\r\n\
                 content-length: {}\r\n\r\n{body}",
                body.len()
            )
            .unwrap();
            let mut text = String::new();
            if let Err(error) = stream.read_to_string(&mut text) {
                assert_eq!(error.kind(), ErrorKind::ConnectionReset, "{route}");
            }
            let (head, body) = text.split_once("\r\n\r\n").unwrap();
            (head.get(9..12).unwrap().parse().unwrap(), body.to_owned())
        }

        /// The page of `family` after `cursor`.
        pub(super) fn page(&self, family: &str, cursor: Option<u64>) -> SessionPage {
            let request = json!({"cursor": cursor, "cwd": null, "family": family});
            let (status, body) = self.ask("list", &request.to_string());
            assert_eq!(status, 200, "{body}");
            serde_json::from_str(&body).unwrap()
        }

        pub(super) fn end(self) {
            self.stop.stop();
            assert_eq!(self.runtime.block_on(self.task).unwrap(), Ok(()));
            std::fs::remove_dir_all(self.dir).unwrap();
        }
    }
}

use fake::{Server, row_of, vector};

/// The same typed create twice gives the same row with the same status,
/// even with its keys in another order (decisions CH25 rule 2, CH33 rule
/// 12). The create makes no session dir and so no disk (decision CH21-4).
#[test]
fn the_same_create_twice_gives_the_same_row() {
    let server = Server::start("create-same");
    let create = vector("CreateSession", "a door session");

    let (status, first) = server.ask("create", &create);
    assert_eq!(status, 200, "{first}");
    let row: Value = serde_json::from_str(&first).unwrap();
    assert_eq!(row, row_of(&create));
    let again = server.ask("create", &vector("CreateSession", "its keys in any order"));
    assert_eq!(again, (200, first.clone()));

    let page = server.ask(
        "list",
        &vector("ListSessions", "the first page of every row of a family"),
    );
    assert_eq!(page, (200, format!(r#"{{"next":null,"rows":[{first}]}}"#)));
    assert!(!server.state.sessions_dir().exists());
    server.end();
}

/// A different create of the same id gets `conflict` 409, and the row
/// stays as the first create made it (decisions CH25 rule 2, CH33 rule
/// 12).
#[test]
fn a_different_create_of_the_id_gets_conflict() {
    let server = Server::start("create-conflict");
    let create = vector("CreateSession", "a door session");
    let (status, first) = server.ask("create", &create);
    assert_eq!(status, 200, "{first}");

    let other = vector("CreateSession", "a delegated job");
    assert_eq!(
        server.ask("create", &other),
        (409, r#"{"code":"conflict"}"#.to_owned())
    );
    let page = server.page("chat", None);
    assert_eq!(
        serde_json::to_value(&page.rows).unwrap(),
        json!([row_of(&create)])
    );
    server.end();
}

/// A family with no dir, a family dir with no `config/` and a `config`
/// that is a file each get `unknown_family` 404 and make no row (decision
/// CH33 rule 3).
#[test]
fn an_unknown_family_gets_unknown_family() {
    let server = Server::start("create-unknown");
    let unknown = (404, r#"{"code":"unknown_family"}"#.to_owned());
    let auto = vector("CreateSession", "an autonomous job of a dispatch");
    assert_eq!(server.ask("create", &auto), unknown);

    let family = server.dir.join("families/scrum-lead");
    std::fs::create_dir(&family).unwrap();
    assert_eq!(server.ask("create", &auto), unknown);
    std::fs::write(family.join("config"), b"").unwrap();
    assert_eq!(server.ask("create", &auto), unknown);

    assert_eq!(server.page("scrum-lead", None).rows, []);
    assert!(!server.state.sessions_dir().exists());
    server.end();
}

/// A repeat of a create whose family `config/` is gone gets
/// `unknown_family` 404, not the stored row (decision CH57 rule 4: CH33
/// rule 3 wins over CH25 rule 2).
#[test]
fn a_repeat_create_after_the_family_went_gets_unknown_family() {
    let server = Server::start("create-gone");
    let create = vector("CreateSession", "a door session");
    let (status, first) = server.ask("create", &create);
    assert_eq!(status, 200, "{first}");

    std::fs::remove_dir(server.dir.join("families/chat/config")).unwrap();
    assert_eq!(
        server.ask("create", &create),
        (404, r#"{"code":"unknown_family"}"#.to_owned())
    );
    server.end();
}

/// A family read or a row write that fails for another reason gets
/// `internal` 500 and makes no row. A family dir of mode 0o000 fails the
/// read, and a rows dir of mode 0o500 fails the write. A root user opens
/// any dir, so the test skips.
#[test]
fn a_failed_family_read_or_row_write_gets_internal() {
    use std::os::unix::fs::PermissionsExt;
    let mode = |path: &std::path::Path, mode: u32| {
        std::fs::set_permissions(path, std::fs::Permissions::from_mode(mode)).unwrap();
    };
    let server = Server::start("create-internal");
    let internal = (500, r#"{"code":"internal"}"#.to_owned());
    let create = vector("CreateSession", "a door session");

    let family = server.dir.join("families/chat");
    mode(&family, 0o000);
    if std::fs::read_dir(&family).is_ok() {
        mode(&family, 0o755);
        server.end();
        return;
    }
    let answer = server.ask("create", &create);
    mode(&family, 0o755);
    assert_eq!(answer, internal);

    let rows = server.state.rows_dir();
    mode(&rows, 0o500);
    let answer = server.ask("create", &create);
    mode(&rows, 0o755);
    assert_eq!(answer, internal);

    assert_eq!(server.page("chat", None).rows, []);
    server.end();
}

/// A walk of rows with 4096-character cwds: serde_json writes U+0001 in 6
/// bytes, so 50 rows pass the 1 MiB cap. The first page ends early with a
/// `next`, each page fits the cap, and the walk gives each row once,
/// newest first (decisions CH21-5, CH33 rule 2).
#[test]
fn a_page_of_4096_character_cwds_ends_early_with_next() {
    let server = Server::start("list-cap");
    let id = |index: usize| format!("0f8e2a4c-1b3d-4e5f-8a9b-{index:012x}");
    for index in 0..PAGE_ROWS {
        let create = json!({
            "created_at": 1_793_577_600_000_u64,
            "cwd": format!("/{}{index:03}", "\u{1}".repeat(4092)),
            "family": "chat",
            "id": id(index),
            "origin": {"door": {}},
        });
        let (status, body) = server.ask("create", &create.to_string());
        assert_eq!(status, 200, "{body}");
    }

    let first = server.page("chat", None);
    assert!(first.rows.len() < PAGE_ROWS, "{} rows", first.rows.len());
    let next = first.next.expect("a next page").get();
    let second = server.page("chat", Some(next));
    assert_eq!(second.next, None);
    for page in [&first, &second] {
        let size = serde_json::to_vec(page).unwrap().len();
        assert!(size <= HOST_API_BODY_CAP, "{size} bytes");
    }
    let walked: Vec<String> = [first, second]
        .iter()
        .flat_map(|page| page.rows.iter().map(|row| row.id.to_string()))
        .collect();
    let newest_first: Vec<String> = (0..PAGE_ROWS).rev().map(id).collect();
    assert_eq!(walked, newest_first);
    server.end();
}
