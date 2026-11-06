//! The serial log of a session lives in the state root, so a vmm restart
//! keeps it until the next start of the session (gap G23, decision CH59
//! rule 2). Systemd removes the run root at each stop of the vmm unit, a
//! crash included. The tests remove it as that stop does.

use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::path::PathBuf;

use creche_vmm_store::layout::{RunRoot, SessionDir, Slot, StateRoot};
use creche_vmm_store::serial_log::{SERIAL_MODE, SerialLog};
use creche_wire::SessionId;

const ID: &str = "0f8e2a4c-1b3d-4e5f-8a9b-0c1d2e3f4a5b";

/// A state root and a run root in a new directory under `/tmp`, with the
/// session dir of `ID`, removed on drop. The temp dir of some hosts is too
/// long for the socket limit of `RunRoot`.
struct Temp {
    dir: PathBuf,
    run: RunRoot,
    session: SessionDir,
    id: SessionId,
}

// `cfg(test)` lets the helper use `expect` (rust/clippy.toml).
#[cfg(test)]
impl Temp {
    fn new(name: &str) -> Self {
        let dir = PathBuf::from(format!("/tmp/vmm-serial-{}-{name}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        let state = StateRoot::new(dir.join("state")).expect("a state root");
        let run = RunRoot::new(dir.join("run")).expect("a run root");
        let id: SessionId = ID.parse().expect("ID is a session id");
        let session = state.session(&id);
        fs::create_dir_all(session.path()).expect("the session dir");
        Self {
            dir,
            run,
            session,
            id,
        }
    }

    /// The mode bits of the serial log.
    fn mode(&self) -> u32 {
        let metadata = fs::metadata(self.session.serial_log()).expect("the log");
        metadata.permissions().mode() & 0o777
    }
}

impl Drop for Temp {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.dir);
    }
}

#[test]
fn a_fresh_log_is_empty_with_mode_0600() {
    let temp = Temp::new("fresh");
    let log = SerialLog::fresh(&temp.session).expect("a new log");

    assert_eq!(log.path(), temp.session.serial_log());
    assert_eq!(fs::read(log.path()).expect("the log"), b"");
    assert_eq!(SERIAL_MODE, 0o600);
    assert_eq!(temp.mode(), 0o600);
}

/// A start makes the log of the last start new, and it sets the mode on
/// a file that has another one.
#[test]
fn a_new_start_empties_the_last_log() {
    let temp = Temp::new("again");
    fs::write(temp.session.serial_log(), b"the last guest").expect("an old log");
    let wide = fs::Permissions::from_mode(0o644);
    fs::set_permissions(temp.session.serial_log(), wide).expect("a wide mode");

    SerialLog::fresh(&temp.session).expect("a new log");

    assert_eq!(fs::read(temp.session.serial_log()).expect("the log"), b"");
    assert_eq!(temp.mode(), 0o600);
}

/// The guest of a start writes its log. A stop of the vmm unit removes
/// the run root, and the start of the restarted vmm makes it again and
/// links the session to a slot. The log still holds the lines of the
/// last guest. Only the next start of the session makes it new.
#[test]
fn a_vmm_restart_keeps_the_log_until_the_next_start() {
    let temp = Temp::new("restart");
    let slot = Slot::new(0).expect("slot 0");
    temp.run.link_vm(&temp.id, slot).expect("the link");
    let log = SerialLog::fresh(&temp.session).expect("a new log");
    fs::write(log.path(), b"the last guest\r\n").expect("the guest output");

    fs::remove_dir_all(temp.run.path()).expect("the stop removes the run root");
    fs::create_dir(temp.run.path()).expect("the restart makes the run root");
    let read = || fs::read(temp.session.serial_log()).expect("the log");
    assert_eq!(read(), b"the last guest\r\n");

    temp.run
        .link_vm(&temp.id, slot)
        .expect("the link of the next start");
    assert_eq!(read(), b"the last guest\r\n");
    SerialLog::fresh(&temp.session).expect("the log of the next start");
    assert_eq!(read(), b"");
}

#[test]
fn a_missing_session_dir_names_the_log() {
    let temp = Temp::new("missing");
    fs::remove_dir(temp.session.path()).expect("the session dir goes");

    let error = SerialLog::fresh(&temp.session).expect_err("a log with no dir");

    assert_eq!(error.path, temp.session.serial_log());
    assert_eq!(error.source.kind(), std::io::ErrorKind::NotFound);
}
