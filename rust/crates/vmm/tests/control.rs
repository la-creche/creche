//! The control link against a fake vsock socket of Cloud Hypervisor
//! (decisions CH13, CH19 (2), CH19 (3), CH21, CH28, CH32 rules 3 and 4,
//! CH62 rule 11, fact 74). The fake answers the `CONNECT` line and plays guest init. The
//! live proof against guest init is phase 3.

use std::io::{self, ErrorKind, Read, Write};
use std::os::unix::net::{UnixListener, UnixStream};
use std::path::{Path, PathBuf};
use std::thread::{sleep, spawn};
use std::time::Duration;

use creche_vmm::control::{Change, ControlError, ControlLink, ControlState, open};
use creche_vmm_store::layout::StateRoot;
use creche_vmm_store::rows::Rows;
use creche_wire::{
    CONTROL_LINE_CAP, ControlReady, FacadeState, GuestEvent, SessionId, TurnEvent, UnixMillis,
};
use tokio::runtime::Builder;

const SESSION: &str = "3f2b8c1e-9d4a-4e6b-8a7c-0b1d2e3f4a5b";
const HELLO: &str = r#"{"sent_at":1793577600000,"session":"3f2b8c1e-9d4a-4e6b-8a7c-0b1d2e3f4a5b"}"#;
const READY: &str =
    r#"{"facade":{"running":{"last_turn_end":null,"pid":42,"starts":1,"turn_running":false}}}"#;
const EXITED: &str = r#"{"facade":{"exited":{"last_turn_end":null,"starts":2}}}"#;
const ENDED: &str = r#"{"facade":{"running":{"last_turn_end":{"title":"First","updated_at":2000},"pid":42,"starts":1,"turn_running":true}}}"#;

/// The `ControlReady` of `json`.
fn ready(json: &str) -> serde_json::Result<ControlReady> {
    serde_json::from_str(json)
}

// `cfg(test)` lets the helper unwrap (rust/clippy.toml).
/// The facade state of `json`.
#[cfg(test)]
fn facade(json: &str) -> FacadeState {
    serde_json::from_str(json).unwrap()
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

// `cfg(test)` lets the helpers below unwrap (rust/clippy.toml).

/// The rows of a new temp state root, with the row of `SESSION` made at
/// 1000. The root stays in the temp dir until the next run of `name`.
#[cfg(test)]
fn rows(name: &str) -> Rows {
    let path = std::env::temp_dir().join(format!("creche-rows-{}-{name}", std::process::id()));
    let _ = std::fs::remove_dir_all(&path);
    let mut rows = Rows::open(StateRoot::new(path).unwrap()).unwrap();
    let create = serde_json::json!({
        "created_at": 1000, "cwd": "/w", "family": "anna", "id": SESSION, "origin": {"door": {}},
    });
    rows.create(serde_json::from_value(create).unwrap())
        .unwrap();
    rows
}

/// The title and `updated_at` of the row of `vm`.
#[cfg(test)]
fn title_and_time(rows: &Rows, vm: &SessionId) -> (serde_json::Value, serde_json::Value) {
    let row = serde_json::to_value(rows.get(vm).unwrap()).unwrap();
    (row["title"].clone(), row["updated_at"].clone())
}

/// Writes the turn end of `change` to the row of `vm`, as the caller of
/// `ControlState` does. Returns whether the link was current.
#[cfg(test)]
fn apply(rows: &mut Rows, vm: &SessionId, change: Change) -> bool {
    match change {
        Change::Stale => false,
        Change::Applied(end) => {
            if let Some(end) = end {
                rows.turn_end(vm, &end).unwrap();
            }
            true
        }
    }
}

/// Opens the control link of `SESSION` on `path` and runs `then` on it in
/// the same runtime.
#[cfg(test)]
fn with_link<T>(path: &Path, then: impl AsyncFnOnce(ControlLink) -> T) -> T {
    let runtime = Builder::new_current_thread()
        .enable_io()
        .enable_time()
        .build()
        .unwrap();
    let clock = || UnixMillis::try_from(1_793_577_600_000).map_err(|_| ControlError::Clock);
    let session = SESSION.parse().unwrap();
    runtime.block_on(async {
        let (link, _ready) = open(path, &session, clock).await.unwrap();
        then(link).await
    })
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
    let mut rows = rows("rows-replace");
    let mut state = ControlState::default();
    let old = state.replace(&vm);
    assert!(apply(
        &mut rows,
        &vm,
        state.ready(&vm, old, ready(READY).unwrap())
    ));
    // A running facade with `turn_running` false runs no turn.
    assert!(!state.turn_running(&vm));

    // A restore: the vmm opens a new link, and the old one ends later
    // (fact 74). A ready of the old link writes nothing, not even its
    // turn end.
    let new = state.replace(&vm);
    assert_eq!(state.link(&vm), Some(new));
    let stale =
        r#"{"facade":{"exited":{"last_turn_end":{"title":"Stale","updated_at":9000},"starts":2}}}"#;
    assert!(!apply(
        &mut rows,
        &vm,
        state.ready(&vm, old, ready(stale).unwrap())
    ));
    let untitled = (serde_json::Value::Null, serde_json::json!(1000));
    assert_eq!(title_and_time(&rows, &vm), untitled);
    assert!(!state.closed(&vm, old));
    assert_eq!(state.link(&vm), Some(new));
    assert_eq!(state.facade(&vm), Some(&ready(READY).unwrap().facade));

    assert!(apply(
        &mut rows,
        &vm,
        state.ready(&vm, new, ready(EXITED).unwrap())
    ));
    assert!(state.closed(&vm, new));
    assert_eq!(state.link(&vm), None);
    // A link close never stops the facade (decision CH19 (1)).
    assert_eq!(state.facade(&vm), Some(&ready(EXITED).unwrap().facade));
}

#[test]
fn turn_events_keep_the_first_title_and_move_updated_at() {
    let path = socket("control-turns").unwrap();
    let listener = UnixListener::bind(&path).unwrap();
    let events = [
        r#"{"turn":{"start":{}}}"#,
        r#"{"turn":{"end":{"title":"First","updated_at":2000}}}"#,
        r#"{"turn":{"start":{}}}"#,
        r#"{"turn":{"end":{"title":"Second","updated_at":3000}}}"#,
    ];
    let lines = format!("{READY}\n{}\n", events.join("\n"));
    let guest = spawn(move || play(listener.accept()?.0, lines.as_bytes()));

    let vm: SessionId = SESSION.parse().unwrap();
    let mut rows = rows("rows-turns");
    let mut state = ControlState::default();
    let link = state.replace(&vm);
    assert!(apply(
        &mut rows,
        &vm,
        state.ready(&vm, link, ready(READY).unwrap())
    ));
    let mut seen = Vec::new();
    let end = with_link(&path, async |mut control| {
        loop {
            match control.event().await {
                Ok(GuestEvent::Turn(event)) => {
                    assert!(apply(&mut rows, &vm, state.turn(&vm, link, &event)));
                    seen.push((state.turn_running(&vm), title_and_time(&rows, &vm)));
                }
                Ok(other) => panic!("not a turn line: {other:?}"),
                Err(error) => break error,
            }
        }
    });

    // Guest init closed the link after the last event. A close after
    // ControlReady asks for no cold boot (decision CH32 rule 3).
    assert_eq!(end, ControlError::Ended);
    assert!(!end.needs_cold_boot());
    let first = (serde_json::json!("First"), serde_json::json!(2000));
    let later = (serde_json::json!("First"), serde_json::json!(3000));
    let untitled = (serde_json::Value::Null, serde_json::json!(1000));
    assert_eq!(
        seen,
        [
            (true, untitled),
            (false, first.clone()),
            (true, first),
            (false, later),
        ]
    );
    guest.join().unwrap().unwrap();
}

#[test]
fn a_ready_applies_its_last_turn_end_once() {
    let vm: SessionId = SESSION.parse().unwrap();
    let mut rows = rows("rows-ready-end");
    let mut state = ControlState::default();
    let first = (serde_json::json!("First"), serde_json::json!(2000));

    // A restore dropped the end event of the old link (fact 45). The ready
    // of the new link carries it (decision CH32 rule 4).
    let link = state.replace(&vm);
    assert!(apply(
        &mut rows,
        &vm,
        state.ready(&vm, link, ready(ENDED).unwrap())
    ));
    assert_eq!(title_and_time(&rows, &vm), first);
    assert!(state.turn_running(&vm));

    // The next restore repeats the seen end.
    let link = state.replace(&vm);
    assert!(apply(
        &mut rows,
        &vm,
        state.ready(&vm, link, ready(ENDED).unwrap())
    ));
    assert_eq!(title_and_time(&rows, &vm), first);

    // An end of the replaced link changes nothing.
    let late =
        TurnEvent::End(serde_json::from_str(r#"{"title":"Late","updated_at":9000}"#).unwrap());
    let old = state.replace(&vm);
    let new = state.replace(&vm);
    assert!(!apply(&mut rows, &vm, state.turn(&vm, old, &late)));
    assert_eq!(title_and_time(&rows, &vm), first);
    assert!(state.turn_running(&vm));
    assert!(apply(
        &mut rows,
        &vm,
        state.ready(&vm, new, ready(EXITED).unwrap())
    ));
    assert!(!state.turn_running(&vm));

    // An exited facade and a facade that starts again carry their last
    // turn end too (decision CH32 rule 5). The title stays the first one.
    let exited = r#"{"facade":{"exited":{"last_turn_end":{"title":"Exited","updated_at":4000},"starts":2}}}"#;
    let link = state.replace(&vm);
    assert!(apply(
        &mut rows,
        &vm,
        state.ready(&vm, link, ready(exited).unwrap())
    ));
    let moved = (serde_json::json!("First"), serde_json::json!(4000));
    assert_eq!(title_and_time(&rows, &vm), moved);
    let starting = r#"{"facade":{"starting":{"last_turn_end":{"title":"Starting","updated_at":5000},"pid":43,"starts":3}}}"#;
    let link = state.replace(&vm);
    assert!(apply(
        &mut rows,
        &vm,
        state.ready(&vm, link, ready(starting).unwrap())
    ));
    let moved = (serde_json::json!("First"), serde_json::json!(5000));
    assert_eq!(title_and_time(&rows, &vm), moved);
}

#[test]
fn stop_sends_one_stop_line() {
    let path = socket("control-stop").unwrap();
    let listener = UnixListener::bind(&path).unwrap();
    let guest = spawn(move || {
        let mut stream = listener.accept()?.0;
        play(stream.try_clone()?, format!("{READY}\n").as_bytes())?;
        line(&mut stream)
    });

    with_link(&path, async |control| control.stop().await).unwrap();
    assert_eq!(guest.join().unwrap().unwrap(), "{\"stop\":{}}\n");
}

/// After `ControlReady`, the reader reads each kind of guest line: a
/// facade state, a `synced` and a relayed turn event (decisions CH21,
/// CH62 rules 11 and 23). A bare turn event, the line of the local link,
/// is not a guest event, so the reader refuses it.
#[test]
fn reads_facade_synced_and_turn_lines() {
    let path = socket("control-guest-lines").unwrap();
    let listener = UnixListener::bind(&path).unwrap();
    let lines = [
        READY,
        EXITED,
        r#"{"synced":{}}"#,
        r#"{"turn":{"start":{}}}"#,
        r#"{"start":{}}"#,
    ];
    let lines = format!("{}\n", lines.join("\n"));
    let guest = spawn(move || play(listener.accept()?.0, lines.as_bytes()));

    let (read, end) = with_link(&path, async |mut control| {
        let mut read = Vec::new();
        loop {
            match control.event().await {
                Ok(event) => read.push(event),
                Err(error) => break (read, error),
            }
        }
    });

    assert!(
        matches!(
            &read[..],
            [
                GuestEvent::Facade(FacadeState::Exited(_)),
                GuestEvent::Synced(_),
                GuestEvent::Turn(TurnEvent::Start(_)),
            ]
        ),
        "{read:?}"
    );
    assert_eq!(read[0], GuestEvent::Facade(ready(EXITED).unwrap().facade));
    assert_eq!(end, ControlError::Line);
    guest.join().unwrap().unwrap();
}

/// A `facade` line of a replaced link changes nothing, not even the row
/// (decision CH28). The same line of the current link applies.
#[test]
fn a_facade_line_from_a_replaced_link_is_stale() {
    let vm: SessionId = SESSION.parse().unwrap();
    let mut rows = rows("rows-facade-stale");
    let mut state = ControlState::default();
    let old = state.replace(&vm);
    assert!(apply(
        &mut rows,
        &vm,
        state.ready(&vm, old, ready(READY).unwrap())
    ));
    let new = state.replace(&vm);
    assert!(apply(
        &mut rows,
        &vm,
        state.ready(&vm, new, ready(READY).unwrap())
    ));

    let exited = r#"{"exited":{"last_turn_end":{"title":"Late","updated_at":9000},"starts":1}}"#;
    assert!(!apply(
        &mut rows,
        &vm,
        state.report(&vm, old, facade(exited))
    ));
    let untitled = (serde_json::Value::Null, serde_json::json!(1000));
    assert_eq!(title_and_time(&rows, &vm), untitled);
    assert_eq!(state.facade(&vm), Some(&ready(READY).unwrap().facade));

    assert!(apply(
        &mut rows,
        &vm,
        state.report(&vm, new, facade(exited))
    ));
    let late = (serde_json::json!("Late"), serde_json::json!(9000));
    assert_eq!(title_and_time(&rows, &vm), late);
    assert_eq!(state.facade(&vm), Some(&facade(exited)));
}

/// A facade that leaves `running` clears `turn_running`, so a facade that
/// exits mid-turn blocks no snapshot (decision CH62 rule 11). A `running`
/// line sets the flag that it holds.
#[test]
fn leaving_running_clears_turn_running() {
    let vm: SessionId = SESSION.parse().unwrap();
    let mut state = ControlState::default();
    let link = state.replace(&vm);
    let _ = state.ready(&vm, link, ready(READY).unwrap());
    let start: TurnEvent = serde_json::from_str(r#"{"start":{}}"#).unwrap();
    let left = [
        r#"{"exited":{"last_turn_end":null,"starts":1}}"#,
        r#"{"starting":{"last_turn_end":null,"pid":43,"starts":2}}"#,
        r#"{"not_started":{}}"#,
    ];
    for json in left {
        assert_eq!(state.turn(&vm, link, &start), Change::Applied(None));
        assert!(state.turn_running(&vm));
        assert_eq!(state.report(&vm, link, facade(json)), Change::Applied(None));
        assert!(!state.turn_running(&vm), "{json}");
        assert_eq!(state.facade(&vm), Some(&facade(json)));
    }

    let busy = r#"{"running":{"last_turn_end":null,"pid":44,"starts":3,"turn_running":true}}"#;
    let _ = state.report(&vm, link, facade(busy));
    assert!(state.turn_running(&vm));
    let idle = r#"{"running":{"last_turn_end":null,"pid":44,"starts":3,"turn_running":false}}"#;
    let _ = state.report(&vm, link, facade(idle));
    assert!(!state.turn_running(&vm));
}
