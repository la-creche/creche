//! The control link against a fake vsock socket of Cloud Hypervisor
//! (decisions CH13, CH19 (2), CH28, CH32 rule 3, fact 74). The fake
//! answers the `CONNECT` line and plays guest init. The live proof
//! against guest init is phase 3.

use std::io::{self, ErrorKind, Read, Write};
use std::os::unix::net::{UnixListener, UnixStream};
use std::path::{Path, PathBuf};
use std::thread::{sleep, spawn};
use std::time::Duration;

use creche_vmm::control::{ControlError, ControlState, open};
use creche_wire::{CONTROL_LINE_CAP, ControlReady, SessionId, UnixMillis};
use tokio::runtime::Builder;

const SESSION: &str = "3f2b8c1e-9d4a-4e6b-8a7c-0b1d2e3f4a5b";
const HELLO: &str = r#"{"sent_at":1793577600000,"session":"3f2b8c1e-9d4a-4e6b-8a7c-0b1d2e3f4a5b"}"#;
const READY: &str =
    r#"{"facade":{"running":{"last_turn_end":null,"pid":42,"starts":1,"turn_running":false}}}"#;
const EXITED: &str = r#"{"facade":{"exited":{"last_turn_end":null,"starts":2}}}"#;

/// The `ControlReady` of `json`.
fn ready(json: &str) -> serde_json::Result<ControlReady> {
    serde_json::from_str(json)
}

/// A fresh vsock socket path for the test `name`.
fn socket(name: &str) -> io::Result<PathBuf> {
    let dir = std::env::temp_dir().join(format!("creche-vmm-{}-{name}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir)?;
    Ok(dir.join("vsock.sock"))
}

/// Reads one line, its LF too.
fn line(stream: &mut UnixStream) -> io::Result<String> {
    let mut bytes = Vec::new();
    let mut byte = [0_u8];
    while stream.read(&mut byte)? == 1 {
        bytes.extend_from_slice(&byte);
        if byte == *b"\n" {
            break;
        }
    }
    String::from_utf8(bytes).map_err(|_| ErrorKind::InvalidData.into())
}

/// Plays Cloud Hypervisor and guest init on one stream: it reads the
/// `CONNECT` line, answers `OK`, reads the hello and writes `answer`.
/// Returns the `CONNECT` line and the hello line.
fn play(mut stream: UnixStream, answer: &[u8]) -> io::Result<(String, String)> {
    let connect = line(&mut stream)?;
    stream.write_all(b"OK 1073741824\n")?;
    let hello = line(&mut stream)?;
    // The vmm may close first when it refuses a long line.
    let _ = stream.write_all(answer);
    Ok((connect, hello))
}

/// Opens the control link of `session` on `path` with the wall time of
/// `HELLO`.
fn open_on(path: &Path, session: &SessionId) -> io::Result<Result<ControlReady, ControlError>> {
    let runtime = Builder::new_current_thread()
        .enable_io()
        .enable_time()
        .build()?;
    let clock = || UnixMillis::try_from(1_793_577_600_000).map_err(|_| ControlError::Clock);
    Ok(runtime
        .block_on(open(path, session, clock))
        .map(|(_link, ready)| ready))
}

#[test]
fn the_dial_waits_for_the_socket_and_sends_the_exact_hello() {
    let path = socket("control-enoent").unwrap();
    let guest = spawn({
        let path = path.clone();
        move || {
            // Before Cloud Hypervisor makes its socket, a dial gets ENOENT
            // (fact 74).
            sleep(Duration::from_millis(100));
            let listener = UnixListener::bind(&path)?;
            play(listener.accept()?.0, format!("{READY}\n").as_bytes())
        }
    });

    let session = SESSION.parse().unwrap();
    assert_eq!(open_on(&path, &session).unwrap(), Ok(ready(READY).unwrap()));
    let (connect, hello) = guest.join().unwrap().unwrap();
    assert_eq!(connect, "CONNECT 1200\n");
    assert_eq!(hello, format!("{HELLO}\n"));
}

#[test]
fn the_dial_retries_a_refused_connect() {
    let path = socket("control-refused-connect").unwrap();
    // A socket file with no listener behind it, as before Cloud Hypervisor
    // calls `listen`: a connect gets ECONNREFUSED.
    drop(UnixListener::bind(&path).unwrap());
    let guest = spawn({
        let path = path.clone();
        move || {
            sleep(Duration::from_millis(100));
            std::fs::remove_file(&path)?;
            let listener = UnixListener::bind(&path)?;
            play(listener.accept()?.0, format!("{READY}\n").as_bytes())
        }
    });

    let session = SESSION.parse().unwrap();
    assert_eq!(open_on(&path, &session).unwrap(), Ok(ready(READY).unwrap()));
    guest.join().unwrap().unwrap();
}

#[test]
fn the_dial_retries_after_an_empty_reply() {
    let path = socket("control-empty").unwrap();
    let listener = UnixListener::bind(&path).unwrap();
    let guest = spawn(move || {
        // Before the guest listener exists, Cloud Hypervisor can close the
        // stream with no reply (fact 74).
        let (mut first, _) = listener.accept()?;
        let connect = line(&mut first)?;
        drop(first);
        let (second, _) = listener.accept()?;
        Ok::<_, io::Error>((connect, play(second, format!("{READY}\n").as_bytes())?))
    });

    let session = SESSION.parse().unwrap();
    assert_eq!(open_on(&path, &session).unwrap(), Ok(ready(READY).unwrap()));
    let (connect, (_, hello)) = guest.join().unwrap().unwrap();
    assert_eq!(connect, "CONNECT 1200\n");
    assert_eq!(hello, format!("{HELLO}\n"));
}

#[test]
fn a_close_before_ready_asks_for_a_cold_boot() {
    let path = socket("control-closed").unwrap();
    let listener = UnixListener::bind(&path).unwrap();
    // Guest init closes the link on a hello for another session (decision
    // CH32 rule 3).
    let guest = spawn(move || play(listener.accept()?.0, b""));

    let other = "00000000-0000-4000-8000-000000000000".parse().unwrap();
    let error = open_on(&path, &other).unwrap().unwrap_err();
    assert_eq!(error, ControlError::Closed);
    assert!(error.needs_cold_boot());
    guest.join().unwrap().unwrap();
}

#[test]
fn a_bad_answer_ends_the_open() {
    let exited = r#"{"exited":{"last_turn_end":null,"starts":2}}"#;
    let twice = format!(r#"{{"facade":{exited},"facade":{exited}}}"#);
    let cases = [
        // A key given twice: the strict reader refuses it.
        ("control-twice", format!("{twice}\n").into_bytes()),
        // A turn event in place of the ready.
        ("control-turn", b"{\"start\":{}}\n".to_vec()),
        // A ready cut off before its LF.
        ("control-cut", READY.as_bytes().to_vec()),
    ];
    let session = SESSION.parse().unwrap();
    for (name, answer) in cases {
        let path = socket(name).unwrap();
        let listener = UnixListener::bind(&path).unwrap();
        let guest = spawn(move || play(listener.accept()?.0, &answer));
        let error = open_on(&path, &session).unwrap().unwrap_err();
        assert_eq!(error, ControlError::Line, "{name}");
        assert!(!error.needs_cold_boot(), "{name}");
        guest.join().unwrap().unwrap();
    }

    // Cloud Hypervisor refuses the CONNECT.
    let path = socket("control-refused").unwrap();
    let listener = UnixListener::bind(&path).unwrap();
    let guest = spawn(move || {
        let (mut stream, _) = listener.accept()?;
        line(&mut stream)?;
        stream.write_all(b"NO\n")
    });
    let error = open_on(&path, &session).unwrap().unwrap_err();
    assert_eq!(error, ControlError::Connect);
    guest.join().unwrap().unwrap();
}

#[test]
fn the_line_cap_holds_at_its_boundary() {
    let session = SESSION.parse().unwrap();

    // A line of exactly CONTROL_LINE_CAP bytes passes (rule 2 of "The
    // vsock JSON links" in wire/AGENTS.md).
    let path = socket("control-at-cap").unwrap();
    let listener = UnixListener::bind(&path).unwrap();
    let mut answer = READY.as_bytes().to_vec();
    answer.resize(CONTROL_LINE_CAP, b' ');
    answer.push(b'\n');
    let guest = spawn(move || play(listener.accept()?.0, &answer));
    assert_eq!(open_on(&path, &session).unwrap(), Ok(ready(READY).unwrap()));
    guest.join().unwrap().unwrap();

    // One byte more with no LF, on a stream that stays open: the vmm
    // refuses the line before any LF comes. The open must not wait for
    // READY_WAIT, so a 1 s bound ends the test early.
    let path = socket("control-past-cap").unwrap();
    let listener = UnixListener::bind(&path).unwrap();
    let guest = spawn(move || {
        let mut stream = listener.accept()?.0;
        play(stream.try_clone()?, &[b' '; CONTROL_LINE_CAP + 1])?;
        // Hold the stream open until the vmm closes it. A reset counts as
        // a close.
        while matches!(stream.read(&mut [0_u8]), Ok(1)) {}
        Ok::<_, io::Error>(())
    });
    let runtime = Builder::new_current_thread()
        .enable_io()
        .enable_time()
        .build()
        .unwrap();
    let clock = || UnixMillis::try_from(1_793_577_600_000).map_err(|_| ControlError::Clock);
    let open =
        async { tokio::time::timeout(Duration::from_secs(1), open(&path, &session, clock)).await };
    let error = runtime.block_on(open).unwrap().map(|_| ()).unwrap_err();
    assert_eq!(error, ControlError::Line);
    guest.join().unwrap().unwrap();
}

#[test]
fn a_new_link_replaces_the_old_one() {
    let vm: SessionId = SESSION.parse().unwrap();
    let mut state = ControlState::default();
    let old = state.replace(&vm);
    assert!(state.ready(&vm, old, ready(READY).unwrap()));

    // A restore: the vmm opens a new link, and the old one ends later
    // (fact 74).
    let new = state.replace(&vm);
    assert_eq!(state.link(&vm), Some(new));
    assert!(!state.ready(&vm, old, ready(EXITED).unwrap()));
    assert!(!state.closed(&vm, old));
    assert_eq!(state.link(&vm), Some(new));
    assert_eq!(state.facade(&vm), Some(&ready(READY).unwrap().facade));

    assert!(state.ready(&vm, new, ready(EXITED).unwrap()));
    assert!(state.closed(&vm, new));
    assert_eq!(state.link(&vm), None);
    // A link close never stops the facade (decision CH19 (1)).
    assert_eq!(state.facade(&vm), Some(&ready(EXITED).unwrap().facade));
}
