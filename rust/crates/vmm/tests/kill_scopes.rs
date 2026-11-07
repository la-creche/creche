//! The program `vmm kill-scopes`, the `ExecStopPost=` step of the vmm
//! unit (decisions CH61 rule 1, CH63), against a fake `systemctl`. Each
//! run clears the env and sets only `AGENT_SYSTEMCTL_PROGRAM`, so a run
//! that works also shows that the mode reads no other key. The fake
//! records each call and prints `list.<turn>` for each `list-units`. Its
//! `kill` of a unit exits 0. When the file `kill.<unit>` exists, it
//! prints the NoSuchUnit text of systemd 255 and exits 1.

use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::path::PathBuf;
use std::process::{Command, Output};
use std::sync::{PoisonError, RwLock};

use creche_vmm::config::SYSTEMCTL;

const FAKE: &str = r#"#!/bin/sh
dir=${0%/*}
printf '%s\n' "$*" >> "$dir/calls"
if [ "$2" = list-units ]; then
  turn=$(grep -c ' list-units ' "$dir/calls")
  cat "$dir/list.$turn" 2>/dev/null
  exit 0
fi
if [ "$2" = kill ]; then
  [ -e "$dir/kill.$5" ] || exit 0
  printf 'Failed to kill unit %s: Unit %s not loaded.\n' "$5" "$5" >&2
  exit 1
fi
exit 5
"#;

/// The exit status of a refused start.
const EX_CONFIG: i32 = 78;

const A: &str = "creche-vm-0123abcd-0000-4000-8000-00000000c3d3.scope";
const B: &str = "creche-vm-0123abcd-0000-4000-8000-00000000c3d4.scope";

/// A test writes its fake under the write lock and runs `vmm` under the
/// read lock. So no fork of another test holds the fake open for
/// writing, and its exec never fails with `ETXTBSY`.
static EXEC: RwLock<()> = RwLock::new(());

// Each helper with `cfg(test)` may unwrap (rust/clippy.toml).

/// A fake `systemctl` in a new directory in `/tmp`, removed on drop.
struct Fake {
    dir: PathBuf,
}

#[cfg(test)]
impl Fake {
    /// The fake with one listing per `list-units` turn. The kill of each
    /// unit of `gone` fails with NoSuchUnit.
    fn new(name: &str, listings: &[&str], gone: &[&str]) -> Self {
        let dir = PathBuf::from(format!(
            "/tmp/vmm-kill-scopes-{}-{name}",
            std::process::id()
        ));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        for (turn, listing) in (1..).zip(listings) {
            fs::write(dir.join(format!("list.{turn}")), listing).unwrap();
        }
        for unit in gone {
            fs::write(dir.join(format!("kill.{unit}")), "").unwrap();
        }
        let _write = EXEC.write().unwrap();
        let script = dir.join("systemctl");
        fs::write(&script, FAKE).unwrap();
        fs::set_permissions(&script, fs::Permissions::from_mode(0o755)).unwrap();
        Self { dir }
    }

    /// Runs `vmm kill-scopes` with only the systemctl key, set to the
    /// fake. Gives the output and its stderr.
    fn run(&self) -> (Output, String) {
        let systemctl = self.dir.join("systemctl");
        run(&[(SYSTEMCTL, systemctl.to_str().unwrap())])
    }

    /// The kills of the run, in order.
    fn kills(&self) -> Vec<String> {
        let calls = fs::read_to_string(self.dir.join("calls")).unwrap_or_default();
        calls
            .lines()
            .filter(|call| call.contains(" kill "))
            .map(str::to_owned)
            .collect()
    }
}

impl Drop for Fake {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.dir);
    }
}

/// Runs `vmm kill-scopes` with only the keys of `env`.
#[cfg(test)]
fn run(env: &[(&str, &str)]) -> (Output, String) {
    let _read = EXEC.read().unwrap_or_else(PoisonError::into_inner);
    let output = Command::new(env!("CARGO_BIN_EXE_vmm"))
        .arg("kill-scopes")
        .env_clear()
        .envs(env.iter().copied())
        .output()
        .unwrap();
    let stderr = String::from_utf8(output.stderr.clone()).unwrap();
    (output, stderr)
}

#[cfg(test)]
fn line(unit: &str) -> String {
    format!("{unit} loaded active running /opt/ch/cloud-hypervisor\n")
}

#[cfg(test)]
fn kill(unit: &str) -> String {
    format!("--user kill --signal=SIGKILL -- {unit}")
}

#[test]
fn no_key_exits_78_and_names_the_key() {
    let (output, stderr) = run(&[]);
    assert_eq!(output.status.code(), Some(EX_CONFIG));
    assert_eq!(stderr, format!("vmm: {SYSTEMCTL}: the key has no value\n"));
}

#[test]
fn a_relative_program_exits_78() {
    let (output, stderr) = run(&[(SYSTEMCTL, "systemctl")]);
    assert_eq!(output.status.code(), Some(EX_CONFIG));
    let expected = format!("vmm: {SYSTEMCTL}: the key is not an absolute path\n");
    assert_eq!(stderr, expected);
}

#[test]
fn each_vm_unit_gets_a_sigkill_and_the_mode_exits_0() {
    let fake = Fake::new("each", &[&(line(A) + &line(B))], &[]);
    let (output, stderr) = fake.run();
    assert!(output.status.success(), "{stderr}");
    assert_eq!(fake.kills(), [kill(A), kill(B)]);
    let expected =
        format!("vmm: SIGKILL sent to the VM units: {A}, {B}. Ended before the kill: none.\n");
    assert_eq!(stderr, expected);
}

/// The fake answers NoSuchUnit for the kill of B, and the fresh list no
/// longer holds B. So B has ended, and the mode exits 0 (decision CH63).
#[test]
fn a_unit_gone_between_the_list_and_the_kill_exits_0() {
    let first = line(A) + &line(B);
    let fake = Fake::new("gone", &[&first, &line(A)], &[B]);
    let (output, stderr) = fake.run();
    assert!(output.status.success(), "{stderr}");
    assert_eq!(fake.kills(), [kill(A), kill(B)]);
    assert!(
        stderr.ends_with(&format!("Ended before the kill: {B}.\n")),
        "{stderr}"
    );
}

/// The fake fails the kill of B, and the fresh list still holds B. That
/// is a real failure, so the mode exits 1 and names B.
#[test]
fn a_failed_kill_of_a_loaded_unit_exits_1() {
    let first = line(A) + &line(B);
    let fake = Fake::new("fail", &[&first, &first], &[B]);
    let (output, stderr) = fake.run();
    assert_eq!(output.status.code(), Some(1), "{stderr}");
    assert!(
        stderr.starts_with("vmm: the kill of loaded VM units failed:"),
        "{stderr}"
    );
    assert!(stderr.contains(B), "{stderr}");
    assert!(!stderr.contains(&format!("{A} (")), "{stderr}");
}
