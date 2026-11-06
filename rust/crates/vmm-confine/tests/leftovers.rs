//! The stop of leftover VM scopes and the kill of `vmm kill-scopes`
//! against a fake `systemctl` (decisions CH19 (4), CH63, fact 76). The
//! fake records each call and prints the listing of its turn: `list.1`
//! for the first `list-units`, `list.2` for the second, and so on. It
//! exits with `list.status`, or with `list.status.<turn>` when that file
//! exists. Its `stop` and `reset-failed` exit 5, so a test also shows
//! that their status is not read. Its `kill` of a unit exits 0. When the
//! file `kill.<unit>` exists, it prints the NoSuchUnit text of systemd
//! 255 (`Unit <unit> not loaded.`) and exits 1.

use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::path::PathBuf;
use std::sync::{PoisonError, RwLock};

use creche_vmm_confine::launch::Program;
use creche_vmm_confine::leftovers::{KillError, Kills, LeftoverError, kill_all, stop_all};

const FAKE: &str = r#"#!/bin/sh
dir=${0%/*}
printf '%s\n' "$*" >> "$dir/calls"
if [ "$2" = list-units ]; then
  turn=$(grep -c ' list-units ' "$dir/calls")
  cat "$dir/list.$turn" 2>/dev/null
  if [ -e "$dir/list.status.$turn" ]; then
    exit "$(cat "$dir/list.status.$turn")"
  fi
  exit "$(cat "$dir/list.status")"
fi
if [ "$2" = kill ]; then
  [ -e "$dir/kill.$5" ] || exit 0
  printf 'Failed to kill unit %s: Unit %s not loaded.\n' "$5" "$5" >&2
  exit 1
fi
exit 5
"#;

const LIST: &str = "--user list-units --all --plain --no-legend --no-pager -- creche-vm-*";
const A: &str = "creche-vm-0123abcd-0000-4000-8000-00000000c3c3.scope";
const B: &str = "creche-vm-0123abcd-0000-4000-8000-00000000c3c4.scope";

/// A test writes its fake under the write lock and runs it under the read
/// lock. So no fork of another test holds the fake open for writing, and
/// its exec never fails with `ETXTBSY`.
static EXEC: RwLock<()> = RwLock::new(());

// Each helper with `cfg(test)` may unwrap (rust/clippy.toml).

/// A fake `systemctl` in a new directory in `/tmp`, removed on drop.
struct Fake {
    dir: PathBuf,
    systemctl: Program,
}

#[cfg(test)]
impl Fake {
    /// The fake with one listing per `list-units` turn. `list-units` exits
    /// with `list_status`.
    fn new(name: &str, listings: &[&str], list_status: u8) -> Self {
        let dir = PathBuf::from(format!("/tmp/vmm-leftovers-{}-{name}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        for (turn, listing) in (1..).zip(listings) {
            fs::write(dir.join(format!("list.{turn}")), listing).unwrap();
        }
        fs::write(dir.join("list.status"), list_status.to_string()).unwrap();
        let script = dir.join("systemctl");
        let _write = EXEC.write().unwrap();
        fs::write(&script, FAKE).unwrap();
        fs::set_permissions(&script, fs::Permissions::from_mode(0o755)).unwrap();
        let systemctl = Program::new(script).unwrap();
        Self { dir, systemctl }
    }

    fn stop_all(&self) -> Result<Vec<String>, LeftoverError> {
        let _read = EXEC.read().unwrap_or_else(PoisonError::into_inner);
        stop_all(&self.systemctl)
    }

    fn kill_all(&self) -> Result<Kills, KillError> {
        let _read = EXEC.read().unwrap_or_else(PoisonError::into_inner);
        kill_all(&self.systemctl)
    }

    /// The kill of `unit` fails as for a unit that is not loaded.
    fn fail_kill(&self, unit: &str) {
        fs::write(self.dir.join(format!("kill.{unit}")), "").unwrap();
    }

    /// The arguments of each call, in order.
    fn calls(&self) -> Vec<String> {
        let calls = fs::read_to_string(self.dir.join("calls")).unwrap_or_default();
        calls.lines().map(str::to_owned).collect()
    }
}

impl Drop for Fake {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.dir);
    }
}

#[cfg(test)]
fn line(unit: &str, state: &str) -> String {
    format!("{unit} loaded {state} /opt/ch/cloud-hypervisor --api-socket x\n")
}

#[test]
fn no_leftover_runs_no_stop() {
    let fake = Fake::new("none", &[""], 0);
    assert!(fake.stop_all().unwrap().is_empty());
    assert_eq!(fake.calls(), [LIST]);
}

#[test]
fn every_leftover_is_stopped_in_one_call() {
    let first = line(A, "active running") + &line(B, "active running");
    let fake = Fake::new("stop", &[&first, ""], 0);
    assert_eq!(fake.stop_all().unwrap(), [A, B]);
    let stop = format!("--user stop -- {A} {B}");
    assert_eq!(fake.calls(), [LIST, stop.as_str(), LIST]);
}

#[test]
fn a_failed_leftover_is_reset() {
    let first = line(A, "active running");
    let second = line(A, "failed failed");
    let fake = Fake::new("reset", &[&first, &second, ""], 0);
    assert_eq!(fake.stop_all().unwrap(), [A]);
    let stop = format!("--user stop -- {A}");
    let reset = format!("--user reset-failed -- {A}");
    let expected = [LIST, stop.as_str(), LIST, reset.as_str(), LIST];
    assert_eq!(fake.calls(), expected);
}

#[test]
fn every_failed_leftover_is_reset_in_one_call() {
    let first = line(A, "active running") + &line(B, "active running");
    let second = line(A, "failed failed") + &line(B, "failed failed");
    let fake = Fake::new("reset-two", &[&first, &second, ""], 0);
    assert_eq!(fake.stop_all().unwrap(), [A, B]);
    let stop = format!("--user stop -- {A} {B}");
    let reset = format!("--user reset-failed -- {A} {B}");
    let expected = [LIST, stop.as_str(), LIST, reset.as_str(), LIST];
    assert_eq!(fake.calls(), expected);
}

#[test]
fn a_unit_still_loaded_at_the_end_is_an_error() {
    let first = line(A, "active running") + &line(B, "active running");
    let stuck = line(B, "failed failed");
    let fake = Fake::new("stuck", &[&first, &stuck, &stuck], 0);
    match fake.stop_all() {
        Err(LeftoverError::Remaining(units)) => assert_eq!(units, [B]),
        other => panic!("expected Remaining, got {other:?}"),
    }
    let stop = format!("--user stop -- {A} {B}");
    let reset = format!("--user reset-failed -- {B}");
    let expected = [LIST, stop.as_str(), LIST, reset.as_str(), LIST];
    assert_eq!(fake.calls(), expected);
}

#[test]
fn a_failed_listing_is_an_error_and_stops_nothing() {
    let fake = Fake::new("list", &[&line(A, "active running")], 1);
    assert!(matches!(fake.stop_all(), Err(LeftoverError::List(_))));
    assert_eq!(fake.calls(), [LIST]);
}

#[test]
fn a_missing_systemctl_is_an_error() {
    let missing = Program::new(PathBuf::from("/nonexistent/systemctl")).unwrap();
    let _read = EXEC.read().unwrap();
    assert!(matches!(stop_all(&missing), Err(LeftoverError::Run(_))));
    assert!(matches!(kill_all(&missing), Err(KillError::Run(_))));
}

#[cfg(test)]
fn kill(unit: &str) -> String {
    format!("--user kill --signal=SIGKILL -- {unit}")
}

#[test]
fn no_vm_unit_runs_no_kill() {
    let fake = Fake::new("kill-none", &[""], 0);
    assert_eq!(fake.kill_all().unwrap(), Kills::default());
    assert_eq!(fake.calls(), [LIST]);
}

/// One `kill` per unit, by name, and no `stop`: a stop job would wait on
/// `Before=` (decision CH61 rule 2).
#[test]
fn each_vm_unit_gets_its_own_sigkill_and_no_stop() {
    let first = line(A, "active running") + &line(B, "failed failed");
    let fake = Fake::new("kill-each", &[&first], 0);
    let kills = fake.kill_all().unwrap();
    assert_eq!(kills.killed, [A, B]);
    assert!(kills.gone.is_empty());
    let (kill_a, kill_b) = (kill(A), kill(B));
    assert_eq!(fake.calls(), [LIST, kill_a.as_str(), kill_b.as_str()]);
}

/// A unit that the manager unloads between the list and its kill gives
/// NoSuchUnit. The fresh list no longer holds it, so it counts as ended
/// (decision CH63).
#[test]
fn a_unit_gone_between_the_list_and_the_kill_has_ended() {
    let first = line(A, "active running") + &line(B, "failed failed");
    let fresh = line(A, "deactivating stop-sigterm");
    let fake = Fake::new("kill-gone", &[&first, &fresh], 0);
    fake.fail_kill(B);
    let kills = fake.kill_all().unwrap();
    assert_eq!(kills.killed, [A]);
    assert_eq!(kills.gone, [B]);
    let (kill_a, kill_b) = (kill(A), kill(B));
    let expected = [LIST, kill_a.as_str(), kill_b.as_str(), LIST];
    assert_eq!(fake.calls(), expected);
}

/// A failed kill of a unit that the fresh list still holds is an error,
/// and the error carries the stderr of `systemctl`. The fresh list
/// decides, not the text of that stderr.
#[test]
fn a_failed_kill_of_a_loaded_unit_is_an_error() {
    let first = line(A, "active running") + &line(B, "active running");
    let fake = Fake::new("kill-fail", &[&first, &first], 0);
    fake.fail_kill(B);
    match fake.kill_all() {
        Err(KillError::Unkilled(units)) => {
            assert_eq!(units.len(), 1);
            assert_eq!(units[0].unit, B);
            assert_eq!(units[0].status.code(), Some(1));
            let stderr = format!("Failed to kill unit {B}: Unit {B} not loaded.");
            assert_eq!(units[0].stderr, stderr);
        }
        other => panic!("expected Unkilled, got {other:?}"),
    }
    let (kill_a, kill_b) = (kill(A), kill(B));
    let expected = [LIST, kill_a.as_str(), kill_b.as_str(), LIST];
    assert_eq!(fake.calls(), expected);
}

#[test]
fn a_failed_listing_is_an_error_and_kills_nothing() {
    let fake = Fake::new("kill-list", &[&line(A, "active running")], 1);
    assert!(matches!(fake.kill_all(), Err(KillError::List(_))));
    assert_eq!(fake.calls(), [LIST]);
}

#[test]
fn a_failed_fresh_listing_after_a_failed_kill_is_an_error() {
    let first = line(A, "active running");
    let fake = Fake::new("kill-relist", &[&first], 0);
    fake.fail_kill(A);
    fs::write(fake.dir.join("list.status.2"), "1").unwrap();
    assert!(matches!(fake.kill_all(), Err(KillError::List(_))));
    let kill_a = kill(A);
    assert_eq!(fake.calls(), [LIST, kill_a.as_str(), LIST]);
}
