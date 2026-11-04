//! The `acp` request sends a JSON body with an Upgrade (decision CH20).
//! This test proves the Rust half of the gap of `wire/AGENTS.md`: hyper
//! reads that body before it answers 101, and no body byte reaches the
//! stream after 101. `integration/proc/test_proc_vmm_upgrade.py` proves
//! the httpx half.

use std::io::{self, Read, Write};
use std::os::unix::net::UnixStream;
use std::thread::sleep;
use std::time::{Duration, Instant};

use creche_vmm::server::{Stop, run_with};
use creche_wire::{AcpRoute, HOST_API_HEADER, HOST_API_PROTOCOL, UpgradeRoute};
use tokio::runtime::Builder;

#[path = "../examples/upgrade_echo/echo.rs"]
mod echo;

const BODY: &[u8] = br#"{"family":"chat","id":"0b9d6f4e-8a51-4c3e-9f57-3d2b1c0a9e88"}"#;

/// Reads up to the blank line that ends the head of the answer.
fn read_head(stream: &mut UnixStream) -> io::Result<Vec<u8>> {
    let mut head = Vec::new();
    while !head.ends_with(b"\r\n\r\n") {
        let mut byte = [0_u8];
        stream.read_exact(&mut byte)?;
        head.extend_from_slice(&byte);
    }
    Ok(head)
}

#[test]
fn hyper_reads_the_body_before_it_answers_101() {
    let dir = std::env::temp_dir().join(format!("creche-vmm-{}-upgrade", std::process::id()));
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
        async move { run_with(&path, signal, echo::answer).await }
    });

    let deadline = Instant::now() + Duration::from_secs(10);
    let mut stream = loop {
        match UnixStream::connect(&path) {
            Ok(stream) => break stream,
            Err(error) if Instant::now() > deadline => panic!("no server: {error}"),
            Err(_) => sleep(Duration::from_millis(10)),
        }
    };
    stream
        .set_read_timeout(Some(Duration::from_secs(10)))
        .unwrap();
    let token = AcpRoute::TOKEN;
    write!(
        stream,
        "POST /acp HTTP/1.1\r\nhost: vmm\r\n{HOST_API_HEADER}: {HOST_API_PROTOCOL}\r\n\
         content-type: application/json\r\nconnection: upgrade\r\nupgrade: {token}\r\n\
         content-length: {}\r\n\r\n",
        BODY.len()
    )
    .unwrap();
    stream.write_all(BODY).unwrap();

    let head = String::from_utf8(read_head(&mut stream).unwrap()).unwrap();
    assert!(head.starts_with("HTTP/1.1 101 "), "{head}");
    let mut echoed = vec![0_u8; BODY.len()];
    stream.read_exact(&mut echoed).unwrap();
    assert_eq!(
        echoed, BODY,
        "the handler did not get the whole body before 101"
    );

    stream.write_all(b"ping\n").unwrap();
    let mut ping = [0_u8; 5];
    stream.read_exact(&mut ping).unwrap();
    assert_eq!(&ping, b"ping\n");

    drop(stream);
    stop.stop();
    assert_eq!(runtime.block_on(server).unwrap(), Ok(()));
    std::fs::remove_dir_all(dir).unwrap();
}
