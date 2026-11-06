//! The launch chain without systemd (decision CH19 (4), fact 76). The
//! example `launch_chain` plays the launch step, a fake `systemd-run` and
//! the launch stage. The stage is `stage::run`, the one entry, which
//! writes the real exec attribute (decision CH58 rule 1). The rust job
//! loads no slot profile, so each launch here fails closed at the label
//! step and starts no payload. The kvm job loads the slot profiles and
//! proves the full chain: the label, no_new_privs, Landlock, CH and the
//! kill chain of fact 80 (the C15 rework, #443).
#![cfg(target_os = "linux")]

mod common;

use std::io::{self, Write};
use std::num::{NonZeroU32, NonZeroU64};
use std::path::PathBuf;
use std::process::{Command, Stdio};
use std::sync::{Arc, mpsc};
use std::time::Duration;

use creche_vmm_confine::launch::{Launch, Payload, Program};
use creche_vmm_confine::output::Sink;
use creche_vmm_confine::scope::Scope;
use creche_vmm_confine::spawner::{SpawnError, Spawner};
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

/// A launch through the real stage fails closed when no slot profile is
/// loaded (decision CH58 rule 1). The stage refuses the label with
/// `StageError::Enter`, so the payload `say hello` never runs. The error
/// reaches the sink tagged with the session id, and no line of the
/// payload does.
#[test]
fn a_launch_with_no_slot_profile_fails_closed_and_starts_no_payload() {
    let vm = TempVm::new("closed", ID);
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
    assert_eq!(launched.wait().unwrap().code(), Some(78));
    let got: Vec<String> =
        std::iter::from_fn(|| lines.recv_timeout(Duration::from_millis(500)).ok()).collect();
    let prefix = format!("ch {ID}: launch_chain: entering creche-ch-slot-{SLOT}: ");
    assert!(
        got.first().is_some_and(|line| line.starts_with(&prefix)),
        "{got:?}"
    );
    assert!(got.iter().all(|line| !line.contains("hello")), "{got:?}");
}

#[test]
fn a_plan_larger_than_the_pipe_fails_at_once() {
    let vm = TempVm::new("large", ID);
    let spawner = Spawner::start(example()).unwrap();
    let launch_large = launch_with(&vm, vec!["x".repeat(70_000)]);
    let result = spawner.launch(launch_large);
    assert!(matches!(result, Err(SpawnError::TooLarge(_))), "{result:?}");
    // The spawner thread still works after the refusal: the next launch
    // runs the chain, which fails closed at the label step.
    let mut launched = spawner.launch(launch(&vm)).unwrap();
    assert_eq!(launched.wait().unwrap().code(), Some(78));
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

#[test]
fn a_stage_with_no_death_signal_starts_nothing() {
    // A child of this test has death signal 0, so the check of fact 80
    // refuses it before the label, Landlock and the exec.
    let vm = TempVm::new("nosignal", ID);
    let launch = launch_with(&vm, vec!["0".to_owned()]);
    let plan = serde_json::json!({
        "slot": SLOT,
        "payload": launch.payload(),
        "files": launch.files(),
    });
    let output = Command::new(example())
        .args(["launch-stage".to_owned(), plan.to_string()])
        .stderr(Stdio::piped())
        .output()
        .unwrap();
    assert_eq!(output.status.code(), Some(78));
    let stderr = String::from_utf8(output.stderr).unwrap();
    assert!(
        stderr.contains("the death signal is not SIGKILL"),
        "{stderr}"
    );
    assert!(!stderr.contains("entering"), "{stderr}");
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
