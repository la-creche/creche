//! Each call runs `ch-remote --api-socket <socket> <command>`, reads its
//! reply and ends at its timeout (decisions CH7-5, CH26).
//!
//! `tests/fixtures/ch-remote` stands in for `ch-remote`. It records its
//! argv, and files in the dir of the socket set its stdout, stderr and
//! exit. The fixture is in git, so no test writes a program that another
//! test thread runs. C16 runs the real `ch-remote`.

use std::fs;
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

use creche_vmm_store::ch_remote::{ChCommand, ChRemote, ChRemoteError, VmState};
use creche_vmm_store::layout::{RunRoot, VmRunDir};
use creche_wire::SessionId;

const ID: &str = "0f8e2a4c-1b3d-4e5f-8a9b-0c1d2e3f4a5b";
const TIMEOUT: Duration = Duration::from_secs(10);

/// A run dir of one VM in a new dir under `/tmp`, removed on drop. A short
/// root keeps each socket path under `SOCKET_PATH_MAX`.
struct Temp {
    root: PathBuf,
    vm: VmRunDir,
}

// `cfg(test)` lets the helpers use `expect` (rust/clippy.toml).
#[cfg(test)]
impl Temp {
    fn new(name: &str) -> Self {
        let root = PathBuf::from(format!("/tmp/vmm-chr-{}-{name}", std::process::id()));
        let _ = fs::remove_dir_all(&root);
        let id: SessionId = ID.parse().expect("ID is a session id");
        let vm = RunRoot::new(root.clone()).expect("a run root").vm(&id);
        fs::create_dir_all(vm.path()).expect("the run dir");
        Self { root, vm }
    }

    /// Sets what the stub does: `stdout`, `stderr`, `exit` or `hang`.
    fn set(&self, file: &str, text: &str) {
        fs::write(self.vm.path().join(file), text).expect("a stub file");
    }

    fn argv(&self) -> Vec<String> {
        fs::read_to_string(self.vm.path().join("argv"))
            .expect("the stub ran")
            .lines()
            .map(str::to_owned)
            .collect()
    }

    fn expected_argv(&self, rest: &[&str]) -> Vec<String> {
        let socket = self.vm.api_socket();
        let mut argv = vec!["--api-socket".to_owned(), path_text(&socket)];
        argv.extend(rest.iter().map(|&part| part.to_owned()));
        argv
    }
}

impl Drop for Temp {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.root);
    }
}

#[cfg(test)]
fn path_text(path: &Path) -> String {
    path.to_str().expect("a UTF-8 path").to_owned()
}

/// A call of `ChRemote` that returns no reply.
type Call = fn(&ChRemote, &VmRunDir) -> Result<(), ChRemoteError>;

fn stub() -> ChRemote {
    ChRemote::new(Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures/ch-remote"))
}

#[test]
fn each_command_names_the_api_socket() {
    let temp = Temp::new("argv");
    let calls: [(&str, Call); 3] = [
        ("pause", |ch, vm| ch.pause(vm, TIMEOUT)),
        ("resume", |ch, vm| ch.resume(vm, TIMEOUT)),
        ("shutdown-vmm", |ch, vm| ch.shutdown_vmm(vm, TIMEOUT)),
    ];
    for (name, call) in calls {
        call(&stub(), &temp.vm).expect("the call");
        assert_eq!(temp.argv(), temp.expected_argv(&[name]));
    }
}

#[test]
fn a_snapshot_names_a_file_url() {
    let temp = Temp::new("snapshot");
    let dir = temp.root.join("generation");
    stub().snapshot(&temp.vm, &dir, TIMEOUT).expect("the call");
    let url = format!("file://{}", path_text(&dir));
    assert_eq!(temp.argv(), temp.expected_argv(&["snapshot", &url]));
}

#[test]
fn a_relative_snapshot_dir_is_refused() {
    let temp = Temp::new("relative");
    let error = stub()
        .snapshot(&temp.vm, Path::new("generation"), TIMEOUT)
        .expect_err("a refusal");
    assert!(matches!(error, ChRemoteError::BadDestination(_)), "{error}");
    assert!(!temp.vm.path().join("argv").exists());
}

#[test]
fn info_reads_the_state() {
    let temp = Temp::new("info");
    temp.set(
        "stdout",
        r#"{"config":{},"state":"Paused","memory_actual_size":0}"#,
    );
    let info = stub().info(&temp.vm, TIMEOUT).expect("the info");
    assert_eq!(info.state, VmState::Paused);
    assert_eq!(temp.argv(), temp.expected_argv(&["info"]));
}

#[test]
fn an_info_reply_with_no_state_is_refused() {
    let temp = Temp::new("not-info");
    temp.set("stdout", r#"{"config":{}}"#);
    let error = stub().info(&temp.vm, TIMEOUT).expect_err("a refusal");
    assert!(matches!(error, ChRemoteError::NotInfo(_)), "{error}");
}

#[test]
fn a_failed_command_keeps_its_status_and_stderr() {
    let temp = Temp::new("failed");
    temp.set("stderr", "Error: VM is not running\n");
    temp.set("exit", "3");
    let error = stub().pause(&temp.vm, TIMEOUT).expect_err("a failure");
    let ChRemoteError::Failed {
        command,
        status,
        stderr,
    } = error
    else {
        panic!("not Failed: {error}");
    };
    assert_eq!(command, ChCommand::Pause);
    assert_eq!(status.code(), Some(3));
    assert_eq!(stderr, "Error: VM is not running\n");
}

#[test]
fn a_hung_command_ends_at_its_timeout() {
    let temp = Temp::new("hang");
    temp.set("hang", "");
    let timeout = Duration::from_millis(200);
    let start = Instant::now();
    let error = stub().resume(&temp.vm, timeout).expect_err("a timeout");
    assert!(start.elapsed() < Duration::from_secs(5));
    assert!(
        matches!(
            error,
            ChRemoteError::Timeout {
                command: ChCommand::Resume,
                ..
            }
        ),
        "{error}"
    );
}

#[test]
fn a_reply_over_the_cap_is_refused() {
    let temp = Temp::new("large");
    temp.set("stdout", &" ".repeat((1 << 20) + 1));
    let error = stub().info(&temp.vm, TIMEOUT).expect_err("a refusal");
    let ChRemoteError::Io { source, .. } = error else {
        panic!("not Io: {error}");
    };
    assert_eq!(source.kind(), std::io::ErrorKind::FileTooLarge);
}
