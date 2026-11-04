//! The launch chain without systemd (decision CH19 (4), fact 76). The
//! example `launch_chain` plays the vmm, the launch step and a fake
//! `systemd-run`, so each payload runs after three execs: the launch step,
//! the fake `systemd-run` and `/bin/sleep`. The Landlock ruleset of the VM
//! is in force from the second exec on.
#![cfg(target_os = "linux")]

mod common;

use std::io::{self, BufRead, BufReader, Write};
use std::num::{NonZeroU32, NonZeroU64};
use std::os::unix::process::ExitStatusExt;
use std::path::PathBuf;
use std::process::{Command, Stdio};
use std::thread::sleep;
use std::time::{Duration, Instant};

use creche_vmm_confine::launch::{Launch, Payload, Program};
use creche_vmm_confine::scope::Scope;
use creche_vmm_confine::spawner::{Launched, Spawner};

use common::{TempVm, example};

const ID: &str = "0123abcd-0000-4000-8000-00000000c2c2";
const SLEEP: &str = "/bin/sleep";

// Each helper with `cfg(test)` may unwrap (rust/clippy.toml).

/// A launch of `sleep 60` through the fake `systemd-run`.
#[cfg(test)]
fn launch(vm: &TempVm) -> Launch {
    let scope = Scope::new(
        ID.parse().unwrap(),
        NonZeroU64::new(1 << 30).unwrap(),
        NonZeroU32::new(100).unwrap(),
    );
    let payload = Payload::new(
        Program::new(PathBuf::from(SLEEP)).unwrap(),
        vec!["60".to_owned()],
    );
    let systemd_run = Program::new(example()).unwrap();
    Launch::new(systemd_run, scope, payload, vm.files.clone())
}

/// Whether `pid` runs and is not a zombie.
fn running(pid: u32) -> bool {
    std::fs::read_to_string(format!("/proc/{pid}/stat")).is_ok_and(|stat| {
        stat.rsplit_once(") ")
            .is_some_and(|(_, rest)| !rest.starts_with('Z'))
    })
}

/// Waits up to 5 s for `check`.
fn eventually(check: impl Fn() -> bool) -> bool {
    let deadline = Instant::now() + Duration::from_secs(5);
    while Instant::now() < deadline {
        if check() {
            return true;
        }
        sleep(Duration::from_millis(5));
    }
    false
}

/// Waits until the pid runs `sleep`, so all three execs are done.
fn wait_for_payload(pid: u32) {
    let comm = || std::fs::read_to_string(format!("/proc/{pid}/comm")).unwrap_or_default();
    assert!(eventually(|| comm() == "sleep\n"), "the payload never ran");
}

#[cfg(test)]
fn launched(spawner: &Spawner, vm: &TempVm) -> Launched {
    let launched = spawner.launch(launch(vm)).unwrap();
    wait_for_payload(launched.id());
    launched
}

#[test]
fn the_payload_dies_with_the_spawner_thread() {
    let vm = TempVm::new("thread", ID);
    let spawner = Spawner::start(example()).unwrap();
    let mut payload = launched(&spawner, &vm);
    drop(spawner);
    assert_eq!(payload.wait().unwrap().signal(), Some(9));
}

#[test]
fn the_payload_dies_with_a_killed_vmm() {
    let vm = TempVm::new("killed", ID);
    let mut vmm = Command::new(example())
        .args([
            "spawn".as_ref(),
            example().as_os_str(),
            vm.path().as_os_str(),
        ])
        .args([ID, SLEEP, "60"])
        .stdout(Stdio::piped())
        .spawn()
        .unwrap();
    let mut line = String::new();
    BufReader::new(vmm.stdout.take().unwrap())
        .read_line(&mut line)
        .unwrap();
    let pid: u32 = line.trim().parse().unwrap();
    wait_for_payload(pid);

    vmm.kill().unwrap();
    vmm.wait().unwrap();
    assert!(eventually(|| !running(pid)), "the payload outlived the vmm");
}

#[test]
fn terminate_sends_sigterm() {
    let vm = TempVm::new("sigterm", ID);
    let spawner = Spawner::start(example()).unwrap();
    let mut payload = launched(&spawner, &vm);
    payload.terminate().unwrap();
    assert_eq!(payload.wait().unwrap().signal(), Some(15));
    payload.terminate().unwrap();
}

/// The exit code and stderr of the launch step with `stdin`.
#[cfg(test)]
fn launch_step(stdin: Stdio) -> (Option<i32>, String) {
    let output = Command::new(example())
        .arg("launch")
        .stdin(stdin)
        .stderr(Stdio::piped())
        .output()
        .unwrap();
    (
        output.status.code(),
        String::from_utf8(output.stderr).unwrap(),
    )
}

#[test]
fn a_plan_that_is_not_json_is_refused() {
    let (reader, mut writer) = io::pipe().unwrap();
    writer.write_all(b"{").unwrap();
    drop(writer);
    let (code, stderr) = launch_step(reader.into());
    assert_eq!(code, Some(78));
    assert!(stderr.contains("the launch plan is not valid"), "{stderr}");
}

#[test]
fn an_empty_open_pipe_is_refused_at_once() {
    // The write end stays open, so a read would block.
    let (reader, _writer) = io::pipe().unwrap();
    let (code, stderr) = launch_step(reader.into());
    assert_eq!(code, Some(78));
    assert!(stderr.contains("holds no launch plan"), "{stderr}");
}

#[test]
fn a_plan_not_on_a_pipe_is_refused() {
    let (code, stderr) = launch_step(Stdio::null());
    assert_eq!(code, Some(78));
    assert!(stderr.contains("reads its plan from a pipe"), "{stderr}");
}
