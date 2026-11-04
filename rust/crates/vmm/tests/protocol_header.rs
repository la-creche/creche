//! The protocol header check over raw bytes on a temp socket (decisions
//! CH25 rule 1, CH33 rule 1, CH34 rule 1). Each mismatch form gets the
//! frozen reply. A request that passes gets `no_route` until the server
//! has routes.

use std::io::{self, ErrorKind, Read, Write};
use std::os::unix::net::UnixStream;
use std::path::Path;
use std::thread::sleep;
use std::time::{Duration, Instant};

use creche_vmm::ServeError;
use creche_vmm::server::{Stop, run};
use tokio::runtime::Builder;

/// The frozen mismatch reply. Never change it to make a change pass.
const MISMATCH: &str = r#"{"code":"protocol_mismatch","protocol":1}"#;

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

/// The head and the body of the answer to one request with `headers`.
fn answer(path: &Path, headers: &str) -> io::Result<(String, String)> {
    let mut stream = connect(path)?;
    stream.set_read_timeout(Some(Duration::from_secs(10)))?;
    let head = "POST /acp HTTP/1.1\r\nhost: vmm\r\nconnection: close\r\n";
    let json = "content-type: application/json\r\ncontent-length: 2\r\n";
    write!(stream, "{head}{json}{headers}\r\n{{}}")?;
    let mut text = String::new();
    stream.read_to_string(&mut text)?;
    let (head, body) = text.split_once("\r\n\r\n").ok_or(ErrorKind::InvalidData)?;
    Ok((head.to_owned(), body.to_owned()))
}

#[test]
fn each_header_form_gets_its_reply() {
    let dir = std::env::temp_dir().join(format!("creche-vmm-{}-header", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();
    let path = dir.join("vmm.sock");
    let runtime = Builder::new_multi_thread()
        .enable_io()
        .enable_time()
        .build()
        .unwrap();
    let (stop, signal) = Stop::pair();
    let server = runtime.spawn({
        let path = path.clone();
        async move { run(&path, signal).await }
    });

    let named = |value: &str| format!("creche-host-api-protocol: {value}\r\n");
    let twice = format!("{}{}", named("1"), named("1"));
    let forms = [
        String::new(),
        named("2"),
        named("01"),
        named("x"),
        named("1, 1"),
        twice,
    ];
    for form in forms {
        let (head, body) = answer(&path, &form).unwrap();
        assert!(head.starts_with("HTTP/1.1 412 "), "{form:?}: {head}");
        assert!(head.contains("content-type: application/json"), "{head}");
        assert_eq!(body, MISMATCH, "{form:?}");
    }

    // The exact text passes. So does the raw form "  1 ": hyper (httparse)
    // strips the spaces around a header value (RFC 9110 section 5.5), and
    // the check reads "1" (decision CH40 rule 3, which amends CH34 rule 1).
    for value in ["1", "  1 "] {
        let (head, body) = answer(&path, &named(value)).unwrap();
        assert!(head.starts_with("HTTP/1.1 404 "), "{value:?}: {head}");
        assert_eq!(body, r#"{"code":"no_route"}"#);
    }

    // A second server cannot take a bound path.
    let second = runtime.block_on(run(&path, Stop::pair().1));
    assert_eq!(second, Err(ServeError::Bind(ErrorKind::AddrInUse)));

    stop.stop();
    assert_eq!(runtime.block_on(server).unwrap(), Ok(()));
    std::fs::remove_dir_all(dir).unwrap();
}

/// Each connection runs on its own task, so an idle kept-alive client does
/// not hold up another one. The server closes an idle connection only
/// after 30 s, and `answer` waits 10 s at most.
#[test]
fn an_idle_connection_holds_up_no_other() {
    let dir = std::env::temp_dir().join(format!("creche-vmm-{}-idle", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();
    let path = dir.join("vmm.sock");
    let runtime = Builder::new_multi_thread()
        .enable_io()
        .enable_time()
        .build()
        .unwrap();
    let (stop, signal) = Stop::pair();
    let server = runtime.spawn({
        let path = path.clone();
        async move { run(&path, signal).await }
    });

    // One whole request with no `connection: close`, then nothing.
    let mut idle = connect(&path).unwrap();
    write!(
        idle,
        "POST /acp HTTP/1.1\r\nhost: vmm\r\ncontent-length: 0\r\n\r\n"
    )
    .unwrap();
    idle.set_read_timeout(Some(Duration::from_secs(10)))
        .unwrap();
    let mut reply = Vec::new();
    let mut chunk = [0; 512];
    while !reply.ends_with(MISMATCH.as_bytes()) {
        let read = idle.read(&mut chunk).unwrap();
        assert_ne!(read, 0, "the server closed a kept-alive connection");
        reply.extend_from_slice(&chunk[..read]);
    }

    let (head, body) = answer(&path, "creche-host-api-protocol: 1\r\n").unwrap();
    assert!(head.starts_with("HTTP/1.1 404 "), "{head}");
    assert_eq!(body, r#"{"code":"no_route"}"#);

    drop(idle);
    stop.stop();
    assert_eq!(runtime.block_on(server).unwrap(), Ok(()));
    std::fs::remove_dir_all(dir).unwrap();
}
