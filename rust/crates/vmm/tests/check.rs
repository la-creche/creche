//! The body checks over raw bytes on a temp socket (decisions CH26, CH33
//! rule 13, CH34 rule 3). A handler runs `BodyCheck` as a route will, and
//! answers 200 with the body that passed.

use std::io::{self, ErrorKind, Read, Write};
use std::net::Shutdown;
use std::os::unix::net::UnixStream;
use std::path::Path;
use std::sync::Arc;
use std::thread::sleep;
use std::time::{Duration, Instant};

use creche_vmm::server::{BodyCheck, Stop, run_with};
use creche_wire::{
    AcpError, CreateError, DropError, HOST_API_BODY_CAP, HOST_API_HEADER, HOST_API_PROTOCOL,
    ListError, RouteError,
};
use http_body_util::Full;
use hyper::Response;
use tokio::runtime::Builder;

const JSON: &str = "content-type: application/json\r\n";

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

/// The status and the body of the answer to one request. `framing` is the
/// `Content-Length` header, or `None` for a body in one chunk.
fn answer(
    path: &Path,
    headers: &str,
    body: &[u8],
    framing: Option<usize>,
) -> io::Result<(u16, String)> {
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
    exchange(path, bytes, false)
}

/// The status and the body of the answer to the raw `bytes`. With
/// `half_close`, the client shuts its write half after the bytes.
fn exchange(path: &Path, bytes: Vec<u8>, half_close: bool) -> io::Result<(u16, String)> {
    let mut stream = connect(path)?;
    // A bug fails the test, never hangs it.
    stream.set_read_timeout(Some(Duration::from_secs(10)))?;
    // The server may answer before it reads the body, and close. A write
    // fault after that is no fault of the check.
    let mut writer = stream.try_clone()?;
    let sent = std::thread::spawn(move || {
        writer.write_all(&bytes)?;
        if half_close {
            writer.shutdown(Shutdown::Write)?;
        }
        Ok::<_, io::Error>(())
    });
    let mut text = String::new();
    stream.read_to_string(&mut text)?;
    let _ = sent.join();
    let (head, body) = text.split_once("\r\n\r\n").ok_or(ErrorKind::InvalidData)?;
    let status = head.get(9..12).and_then(|code| code.parse().ok());
    Ok((status.ok_or(ErrorKind::InvalidData)?, body.to_owned()))
}

#[test]
fn each_check_gives_its_code_in_order() {
    let dir = std::env::temp_dir().join(format!("creche-vmm-{}-check", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();
    let path = dir.join("vmm.sock");
    let runtime = Builder::new_multi_thread()
        .enable_io()
        .enable_time()
        .build()
        .unwrap();
    let check = Arc::new(BodyCheck::new().unwrap());
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
    // One request gets the reply of `code`, with the status that each
    // route error gives it (decision CH33 rule 13).
    let refused = |headers: &str, body: &[u8], framing: Option<usize>, code: &str| {
        let (status, reply) = answer(&path, headers, body, framing).unwrap();
        assert_eq!(
            reply,
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
        let (status, reply) = exchange(&path, bytes, half_close).unwrap();
        assert_eq!(
            (status, reply.as_str()),
            (400, r#"{"code":"not_json"}"#),
            "{rest:?}"
        );
    }

    // A body of exactly the cap passes, with either framing.
    let at_cap = format!("\"{}\"", "a".repeat(HOST_API_BODY_CAP - 2));
    for framing in [Some(at_cap.len()), None] {
        let (status, body) = answer(&path, JSON, at_cap.as_bytes(), framing).unwrap();
        assert_eq!((status, body.len()), (200, HOST_API_BODY_CAP));
    }

    stop.stop();
    assert_eq!(runtime.block_on(server).unwrap(), Ok(()));
    std::fs::remove_dir_all(dir).unwrap();
}
