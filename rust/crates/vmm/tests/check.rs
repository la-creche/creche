//! The body checks over raw bytes on a temp socket (decisions CH26, CH33
//! rule 13, CH34 rule 3). A handler runs `BodyCheck` as a route will, and
//! answers 200 with the body that passed.

use std::error::Error;
use std::io::{self, ErrorKind, Read, Write};
use std::net::Shutdown;
use std::os::unix::net::UnixStream;
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::thread::sleep;
use std::time::{Duration, Instant};

use creche_vmm::ServeError;
use creche_vmm::server::{BodyCheck, Stop, run_with};
use creche_wire::{
    AcpError, CreateError, DropError, HOST_API_BODY_CAP, HOST_API_HEADER, HOST_API_PROTOCOL,
    ListError, RouteError,
};
use http_body_util::Full;
use hyper::Response;
use tokio::runtime::{Builder, Runtime};
use tokio::task::JoinHandle;

const JSON: &str = "content-type: application/json\r\n";

/// A server on a temp socket whose handler runs `BodyCheck` as a route
/// will, and answers 200 with the body that passed.
struct Served {
    runtime: Runtime,
    dir: PathBuf,
    path: PathBuf,
    stop: Stop,
    server: JoinHandle<Result<(), ServeError>>,
}

impl Served {
    /// Starts a server. `name` keeps the socket of each test apart.
    fn start(name: &str) -> Result<Self, Box<dyn Error>> {
        let dir = std::env::temp_dir().join(format!("creche-vmm-{}-{name}", std::process::id()));
        std::fs::create_dir_all(&dir)?;
        let path = dir.join("vmm.sock");
        let runtime = Builder::new_multi_thread()
            .enable_io()
            .enable_time()
            .build()?;
        let check = Arc::new(BodyCheck::new().map_err(|error| error.to_string())?);
        let handler = move |request| {
            let check = Arc::clone(&check);
            async move {
                match check.read(request).await {
                    Ok(body) => Response::new(Full::new(body)),
                    Err(reply) => reply,
                }
            }
        };
        let (stop, signal) = Stop::pair();
        let server = runtime.spawn({
            let path = path.clone();
            async move { run_with(&path, signal, handler).await }
        });
        Ok(Self {
            runtime,
            dir,
            path,
            stop,
            server,
        })
    }

    /// Stops the server and removes its socket. The result is the end of
    /// the server.
    fn end(self) -> Result<Result<(), ServeError>, Box<dyn Error>> {
        self.stop.stop();
        let ended = self.runtime.block_on(self.server)?;
        std::fs::remove_dir_all(self.dir)?;
        Ok(ended)
    }
}

/// Connects once the server has bound the socket.
fn connect(path: &Path) -> io::Result<UnixStream> {
    let deadline = Instant::now() + Duration::from_secs(10);
    loop {
        match UnixStream::connect(path) {
            Err(_) if Instant::now() < deadline => sleep(Duration::from_millis(10)),
            connected => return connected,
        }
    }
}

/// What a client got for one request.
#[derive(Debug)]
struct Answer {
    /// Whether the whole request went out with no write error.
    sent: bool,
    status: u16,
    body: String,
}

/// The answer to one request. `framing` is the `Content-Length` header,
/// or `None` for a body in one chunk.
fn answer(path: &Path, headers: &str, body: &[u8], framing: Option<usize>) -> io::Result<Answer> {
    let mut request = format!(
        "POST /create HTTP/1.1\r\nhost: vmm\r\nconnection: close\r\n\
         {HOST_API_HEADER}: {HOST_API_PROTOCOL}\r\n{headers}"
    );
    let mut bytes = match framing {
        Some(length) => format!("{request}content-length: {length}\r\n\r\n").into_bytes(),
        None => {
            request.push_str("transfer-encoding: chunked\r\n\r\n");
            format!("{request}{:x}\r\n", body.len()).into_bytes()
        }
    };
    bytes.extend_from_slice(body);
    if framing.is_none() {
        bytes.extend_from_slice(b"\r\n0\r\n\r\n");
    }
    exchange(path, &bytes, false)
}

/// The answer to the raw `bytes`. With `half_close`, the client shuts its
/// write half after the bytes.
///
/// The client acts as httpx does: it writes the whole request, then reads
/// the reply. The server may refuse before it reads the body. A write
/// error is kept in `sent`, and the read still runs. A read error, such as
/// the reset of a server that closed with request bytes unread, fails the
/// exchange (decision CH26: each refusal is a typed reply).
fn exchange(path: &Path, bytes: &[u8], half_close: bool) -> io::Result<Answer> {
    let mut stream = connect(path)?;
    // A bug fails the test, never hangs it.
    stream.set_read_timeout(Some(Duration::from_secs(10)))?;
    stream.set_write_timeout(Some(Duration::from_secs(10)))?;
    let sent = stream
        .write_all(bytes)
        .and_then(|()| {
            if half_close {
                stream.shutdown(Shutdown::Write)
            } else {
                Ok(())
            }
        })
        .is_ok();
    let mut text = String::new();
    stream.read_to_string(&mut text)?;
    let (head, body) = text.split_once("\r\n\r\n").ok_or(ErrorKind::InvalidData)?;
    let status = head.get(9..12).and_then(|code| code.parse().ok());
    Ok(Answer {
        sent,
        status: status.ok_or(ErrorKind::InvalidData)?,
        body: body.to_owned(),
    })
}

#[test]
fn each_check_gives_its_code_in_order() {
    let served = Served::start("check").unwrap();
    let path = &served.path;
    // One request gets the reply of `code`, with the status that each
    // route error gives it (decision CH33 rule 13). The whole request goes
    // out first, even when the server refuses it before its body.
    let refused = |headers: &str, body: &[u8], framing: Option<usize>, code: &str| {
        let Answer { sent, status, body } = answer(path, headers, body, framing).unwrap();
        assert!(sent, "{headers:?} {framing:?}: the server closed early");
        assert_eq!(
            body,
            format!(r#"{{"code":"{code}"}}"#),
            "{headers:?} {framing:?}"
        );
        let routes = [
            CreateError::STATUSES,
            ListError::STATUSES,
            DropError::STATUSES,
            AcpError::STATUSES,
        ];
        assert!(
            routes
                .iter()
                .all(|statuses| statuses.contains(&(code, status))),
            "{code}"
        );
    };

    let twice = format!("{JSON}{JSON}");
    let types = [
        "content-type: text/plain\r\n",
        "content-type: application/json; charset=utf-8\r\n",
    ];
    for headers in ["", types[0], types[1], &twice] {
        refused(headers, b"{}", Some(2), "bad_content_type");
    }

    // The Content-Type comes before the cap, and the cap before the JSON.
    let over = "a".repeat(HOST_API_BODY_CAP + 1);
    refused("", over.as_bytes(), Some(over.len()), "bad_content_type");
    refused(JSON, b"{", Some(HOST_API_BODY_CAP + 1), "too_large");
    for framing in [Some(over.len()), None] {
        refused(JSON, over.as_bytes(), framing, "too_large");
    }
    for body in [&b""[..], br#"{"a":1,"a":2}"#, b"{} x"] {
        refused(JSON, body, Some(body.len()), "not_json");
    }

    // A body that fails to arrive whole gets not_json: a bad chunk size,
    // and a chunked body that ends before its last chunk.
    let head = format!(
        "POST /create HTTP/1.1\r\nhost: vmm\r\n{HOST_API_HEADER}: {HOST_API_PROTOCOL}\r\n\
         {JSON}transfer-encoding: chunked\r\n\r\n"
    );
    for (rest, half_close) in [("zz\r\n{}\r\n0\r\n\r\n", false), ("2\r\n{}\r\n", true)] {
        let bytes = format!("{head}{rest}").into_bytes();
        let reply = exchange(path, &bytes, half_close).unwrap();
        assert_eq!(
            (reply.sent, reply.status, reply.body.as_str()),
            (true, 400, r#"{"code":"not_json"}"#),
            "{rest:?}"
        );
    }

    // A body of exactly the cap passes, with either framing.
    let at_cap = format!("\"{}\"", "a".repeat(HOST_API_BODY_CAP - 2));
    for framing in [Some(at_cap.len()), None] {
        let reply = answer(path, JSON, at_cap.as_bytes(), framing).unwrap();
        assert_eq!(
            (reply.sent, reply.status, reply.body.len()),
            (true, 200, HOST_API_BODY_CAP)
        );
    }

    assert_eq!(served.end().unwrap(), Ok(()));
}

/// A client that sends a body over the cap with its `Content-Length`
/// writes the whole request, then reads `too_large`. The server refuses
/// it before it reads a byte of the body, so it closes with the body
/// unread. Without a lingering close, Linux then resets the connection:
/// the client write fails, and the read ends in ECONNRESET. The race
/// shows in some runs only, so the request runs many times.
#[test]
fn a_body_over_the_cap_gets_too_large_each_time() {
    let served = Served::start("over-cap").unwrap();
    let over = "a".repeat(HOST_API_BODY_CAP + 1);
    for run in 0..200 {
        let reply = answer(&served.path, JSON, over.as_bytes(), Some(over.len()))
            .unwrap_or_else(|error| panic!("run {run}: {error}"));
        assert_eq!(
            (reply.sent, reply.status, reply.body.as_str()),
            (true, 413, r#"{"code":"too_large"}"#),
            "run {run}"
        );
    }
    assert_eq!(served.end().unwrap(), Ok(()));
}
