//! The launch chain without systemd (decision CH19 (4), fact 76). The
//! example `launch_chain` plays the vmm, the launch step, a fake
//! `systemd-run` and the launch stage, so each payload runs after four
//! execs: the launch step, the fake `systemd-run`, the stage and
//! `/bin/sleep` (decision CH55). The Landlock ruleset of the VM is in
//! force from the last exec on.
#![cfg(target_os = "linux")]

mod common;

use std::io::{self, BufRead, BufReader, Write};
use std::num::{NonZeroU32, NonZeroU64};
use std::os::unix::process::ExitStatusExt;
use std::path::PathBuf;
use std::process::{Command, Stdio};
use std::sync::{Arc, mpsc};
use std::thread::sleep;
use std::time::{Duration, Instant};

use creche_vmm_confine::launch::{Launch, Payload, Program};
use creche_vmm_confine::output::Sink;
use creche_vmm_confine::scope::Scope;
use creche_vmm_confine::spawner::{Launched, SpawnError, Spawner};
use creche_vmm_store::layout::Slot;

use common::{TempVm, example};

const ID: &str = "0123abcd-0000-4000-8000-00000000c2c2";
const SLEEP: &str = "/bin/sleep";

/// The slot of each launch.
const SLOT: u16 = 3;

// Each helper with `cfg(test)` may unwrap (rust/clippy.toml).

/// A launch of `sleep 60` through the fake `systemd-run`.
#[cfg(test)]
fn launch(vm: &TempVm) -> Launch {
    launch_with(vm, vec!["60".to_owned()])
}

/// A launch of `sleep` with `args` through the fake `systemd-run`.
#[cfg(test)]
fn launch_with(vm: &TempVm, args: Vec<String>) -> Launch {
    let scope = Scope::new(
        ID.parse().unwrap(),
        NonZeroU64::new(1 << 30).unwrap(),
        NonZeroU32::new(100).unwrap(),
    );
    let payload = Payload::new(Program::new(PathBuf::from(SLEEP)).unwrap(), args);
    let systemd_run = Program::new(example()).unwrap();
    Launch::new(systemd_run, scope, payload, vm.files.clone()).in_slot(slot())
}

#[cfg(test)]
fn slot() -> Slot {
    Slot::new(SLOT).unwrap()
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
fn the_payload_has_no_core_limit() {
    let vm = TempVm::new("core", ID);
    let spawner = Spawner::start(example()).unwrap();
    let launched = launched(&spawner, &vm);
    let limits = std::fs::read_to_string(format!("/proc/{}/limits", launched.id())).unwrap();
    let core = limits
        .lines()
        .find(|line| line.starts_with("Max core file size"))
        .unwrap();
    let values: Vec<&str> = core.split_whitespace().skip(4).take(2).collect();
    assert_eq!(values, ["0", "0"], "{core}");
}

#[test]
fn a_plan_larger_than_the_pipe_fails_at_once() {
    let vm = TempVm::new("large", ID);
    let spawner = Spawner::start(example()).unwrap();
    let launch = launch_with(&vm, vec!["x".repeat(70_000)]);
    let result = spawner.launch(launch);
    assert!(matches!(result, Err(SpawnError::TooLarge(_))), "{result:?}");
    // The spawner thread still works after the refusal.
    let mut payload = launched(&spawner, &vm);
    payload.kill().unwrap();
    payload.wait().unwrap();
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

#[test]
fn the_payload_output_reaches_the_sink_tagged_with_the_session_id() {
    let vm = TempVm::new("output", ID);
    let (sender, lines) = mpsc::channel();
    let sink: Sink = Arc::new(move |line| sender.send(line.to_owned()).unwrap());
    let spawner = Spawner::with_sink(example(), sink).unwrap();
    let scope = Scope::new(
        ID.parse().unwrap(),
        NonZeroU64::new(1 << 30).unwrap(),
        NonZeroU32::new(100).unwrap(),
    );
    let payload = Payload::new(
        Program::new(example()).unwrap(),
        vec!["say".to_owned(), "hello".to_owned()],
    );
    let systemd_run = Program::new(example()).unwrap();
    let launch = Launch::new(systemd_run, scope, payload, vm.files.clone()).in_slot(slot());
    let mut launched = spawner.launch(launch).unwrap();
    assert!(launched.wait().unwrap().success());
    let mut got: Vec<String> = (0..3)
        .map(|_| lines.recv_timeout(Duration::from_secs(5)).unwrap())
        .collect();
    got.sort();
    // The stage of the example writes its exec attribute to stderr.
    assert_eq!(
        got,
        [
            format!("ch {ID}: apparmor exec creche-ch-slot-{SLOT}"),
            format!("ch {ID}: hello err"),
            format!("ch {ID}: hello out"),
        ]
    );
}

#[test]
fn a_launch_with_no_slot_starts_nothing() {
    let vm = TempVm::new("noslot", ID);
    // The test is the parent of the step, so only the slot is wrong.
    let mut plan = serde_json::json!({ "spawner": std::process::id(), "launch": launch(&vm) });
    plan["launch"]["slot"] = serde_json::Value::Null;
    let (reader, mut writer) = io::pipe().unwrap();
    writer.write_all(plan.to_string().as_bytes()).unwrap();
    drop(writer);
    let (code, stderr) = launch_step(reader.into());
    assert_eq!(code, Some(78));
    assert!(stderr.contains("the launch has no slot"), "{stderr}");
}

#[test]
fn a_stage_plan_that_is_not_json_is_refused() {
    let output = Command::new(example())
        .args(["launch-stage", "{"])
        .stderr(Stdio::piped())
        .output()
        .unwrap();
    assert_eq!(output.status.code(), Some(78));
    let stderr = String::from_utf8(output.stderr).unwrap();
    assert!(stderr.contains("the stage plan is not valid"), "{stderr}");
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
fn a_plan_from_another_spawner_is_refused() {
    // Pid 1 is never the parent of the launch step in this test.
    let vm = TempVm::new("parent", ID);
    let plan = serde_json::json!({ "spawner": 1, "launch": launch(&vm) });
    let (reader, mut writer) = io::pipe().unwrap();
    writer.write_all(plan.to_string().as_bytes()).unwrap();
    drop(writer);
    let (code, stderr) = launch_step(reader.into());
    assert_eq!(code, Some(78));
    assert!(
        stderr.contains("the vmm exited before the launch"),
        "{stderr}"
    );
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
