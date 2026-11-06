//! The leftover VM scopes of an earlier vmm. At start, the vmm stops every
//! user unit with the prefix `creche-vm-` and adopts none (decision CH19
//! (4), fact 76). PR L20 calls `stop_all` before it binds or unlinks any
//! per-VM socket path.
//!
//! A scope whose payload ignores SIGTERM ends `failed` after its stop, and
//! `systemd-run --unit` refuses that name until `reset-failed` (host
//! spike 2026-11-03, systemd 255.4). So a unit that is still listed after
//! the stop gets `reset-failed`, and a unit that is still listed after that
//! is an error.
//!
//! `kill_all` is the `ExecStopPost=` step of the vmm unit, `vmm
//! kill-scopes` (decisions CH61 rule 1, CH63). It lists the same units,
//! then sends SIGKILL to each one by name. It queues no stop job, because
//! each scope is ordered `Before=` the vmm unit (CH61 rule 2), so a wait
//! for its stop job there would deadlock. A scope that the manager
//! unloads between the list and its kill makes the kill fail with
//! NoSuchUnit. That scope has ended, so a fresh list decides: a unit that
//! it no longer holds counts as ended, and a unit that it still holds is
//! an error.

use std::fmt;
use std::io;
use std::process::{Command, ExitStatus, Output, Stdio};

use creche_vmm_store::layout::SCOPE_PREFIX;

use crate::launch::Program;

/// Why a listing of the VM units failed.
#[derive(Debug)]
enum ListError {
    /// `systemctl` did not run.
    Run(io::Error),
    /// `systemctl list-units` exited with this status.
    Status(ExitStatus),
}

/// Why the vmm did not clear the leftover scopes.
#[derive(Debug)]
pub enum LeftoverError {
    /// `systemctl` did not run.
    Run(io::Error),
    /// `systemctl list-units` exited with this status.
    List(ExitStatus),
    /// These units are still loaded after the stop and the `reset-failed`.
    /// A launch with one of these names would fail.
    Remaining(Vec<String>),
}

impl fmt::Display for LeftoverError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Run(error) => write!(formatter, "running systemctl: {error}"),
            Self::List(status) => write!(formatter, "systemctl list-units failed: {status}"),
            Self::Remaining(units) => write!(
                formatter,
                "leftover VM units are still loaded: {}",
                units.join(", ")
            ),
        }
    }
}

impl std::error::Error for LeftoverError {}

impl From<ListError> for LeftoverError {
    fn from(error: ListError) -> Self {
        match error {
            ListError::Run(error) => Self::Run(error),
            ListError::Status(status) => Self::List(status),
        }
    }
}

/// What `kill_all` did with each VM unit that it listed.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Kills {
    /// The units that took the SIGKILL, in the order of the list.
    pub killed: Vec<String>,
    /// The units whose kill failed and that the fresh list no longer
    /// holds. Each one had ended before its kill (NoSuchUnit).
    pub gone: Vec<String>,
}

impl fmt::Display for Kills {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        let names = |units: &[String]| match units {
            [] => "none".to_owned(),
            units => units.join(", "),
        };
        write!(
            formatter,
            "SIGKILL sent to the VM units: {}. Ended before the kill: {}.",
            names(&self.killed),
            names(&self.gone)
        )
    }
}

/// A unit whose kill failed while the fresh list still holds it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Unkilled {
    /// The unit name.
    pub unit: String,
    /// The exit status of its `systemctl kill`.
    pub status: ExitStatus,
    /// The stderr of that `systemctl kill`, trimmed.
    pub stderr: String,
}

/// Why `kill_all` did not end every VM unit.
#[derive(Debug)]
pub enum KillError {
    /// `systemctl` did not run.
    Run(io::Error),
    /// `systemctl list-units` exited with this status.
    List(ExitStatus),
    /// The kill of each of these units failed, and the fresh list still
    /// holds it. Such a unit may still run.
    Unkilled(Vec<Unkilled>),
}

impl fmt::Display for KillError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Run(error) => write!(formatter, "running systemctl: {error}"),
            Self::List(status) => write!(formatter, "systemctl list-units failed: {status}"),
            Self::Unkilled(units) => {
                write!(formatter, "the kill of loaded VM units failed:")?;
                for one in units {
                    write!(
                        formatter,
                        " {} ({}: {:?})",
                        one.unit, one.status, one.stderr
                    )?;
                }
                Ok(())
            }
        }
    }
}

impl std::error::Error for KillError {}

impl From<ListError> for KillError {
    fn from(error: ListError) -> Self {
        match error {
            ListError::Run(error) => Self::Run(error),
            ListError::Status(status) => Self::List(status),
        }
    }
}

/// Stops every user unit with the VM prefix and clears a failed state.
/// `systemctl` is the absolute path that the vmm config gives, like the
/// path of `systemd-run`. The result names each unit that was found.
///
/// # Errors
///
/// `Run` when `systemctl` does not run. `List` when a listing fails.
/// `Remaining` when a unit is still loaded at the end.
pub fn stop_all(systemctl: &Program) -> Result<Vec<String>, LeftoverError> {
    let found = list(systemctl)?;
    if found.is_empty() {
        return Ok(found);
    }
    // `systemctl stop` waits for each stop job: SIGTERM, then SIGKILL after
    // the stop timeout (decision CH19 (3)). Its status is not read, because
    // a unit that ends between the list and the stop makes it fail. The
    // next list is the check.
    run(systemctl, "stop", &found)?;
    let failed = list(systemctl)?;
    if failed.is_empty() {
        return Ok(found);
    }
    // `reset-failed` unloads a failed scope, so `systemd-run` can use the
    // name again. Its status is not read either: the last list decides.
    run(systemctl, "reset-failed", &failed)?;
    let remaining = list(systemctl)?;
    if remaining.is_empty() {
        Ok(found)
    } else {
        Err(LeftoverError::Remaining(remaining))
    }
}

/// Sends SIGKILL to every user unit with the VM prefix, one unit per
/// call: `systemctl --user kill --signal=SIGKILL -- <unit>` (decision
/// CH63). It queues no job. When a kill fails, one fresh list decides
/// each failed unit: a unit that the list no longer holds had ended, and
/// a unit that it still holds is an error. `systemctl` is the absolute
/// path that the config of `vmm kill-scopes` gives.
///
/// # Errors
///
/// `Run` when `systemctl` does not run. `List` when a listing fails.
/// `Unkilled` when a unit whose kill failed is still loaded.
pub fn kill_all(systemctl: &Program) -> Result<Kills, KillError> {
    let found = list(systemctl)?;
    let mut kills = Kills::default();
    let mut failed = Vec::new();
    for unit in found {
        let output = kill(systemctl, &unit).map_err(KillError::Run)?;
        if output.status.success() {
            kills.killed.push(unit);
        } else {
            let stderr = String::from_utf8_lossy(&output.stderr).trim().to_owned();
            let status = output.status;
            failed.push(Unkilled {
                unit,
                status,
                stderr,
            });
        }
    }
    if failed.is_empty() {
        return Ok(kills);
    }
    let loaded = list(systemctl)?;
    let (unkilled, gone): (Vec<Unkilled>, Vec<Unkilled>) = failed
        .into_iter()
        .partition(|one| loaded.contains(&one.unit));
    if unkilled.is_empty() {
        kills.gone = gone.into_iter().map(|one| one.unit).collect();
        Ok(kills)
    } else {
        Err(KillError::Unkilled(unkilled))
    }
}

/// Runs `systemctl --user kill --signal=SIGKILL -- <unit>` and gives its
/// status and its stderr.
fn kill(systemctl: &Program, unit: &str) -> io::Result<Output> {
    Command::new(systemctl.path())
        .args(["--user", "kill", "--signal=SIGKILL", "--", unit])
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .output()
}

/// The loaded user units with the VM prefix, in any state (fact 76).
fn list(systemctl: &Program) -> Result<Vec<String>, ListError> {
    let pattern = format!("{SCOPE_PREFIX}*");
    let output = Command::new(systemctl.path())
        .args(["--user", "list-units", "--all", "--plain", "--no-legend"])
        .args(["--no-pager", "--", pattern.as_str()])
        .stdin(Stdio::null())
        .stderr(Stdio::inherit())
        .output()
        .map_err(ListError::Run)?;
    if !output.status.success() {
        return Err(ListError::Status(output.status));
    }
    Ok(units(&String::from_utf8_lossy(&output.stdout)))
}

/// Runs `systemctl --user <verb> -- <units>` and waits for it.
fn run(systemctl: &Program, verb: &str, units: &[String]) -> Result<(), LeftoverError> {
    Command::new(systemctl.path())
        .args(["--user", verb, "--"])
        .args(units)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .status()
        .map(|_status| ())
        .map_err(LeftoverError::Run)
}

/// The unit names in the `--plain --no-legend` output of `list-units`: the
/// first word of each line. A name without the VM prefix is not ours, so
/// it is skipped.
fn units(listing: &str) -> Vec<String> {
    listing
        .lines()
        .filter_map(|line| line.split_whitespace().next())
        .filter(|unit| unit.starts_with(SCOPE_PREFIX))
        .map(str::to_owned)
        .collect()
}

#[cfg(test)]
mod tests {
    use super::units;

    const ID: &str = "0123abcd-0000-4000-8000-00000000c3c3";

    #[test]
    fn the_first_word_of_each_line_is_the_unit() {
        let listing = format!(
            "creche-vm-{ID}.scope loaded active running /opt/ch/cloud-hypervisor\n\
             creche-vm-b.scope loaded failed failed /opt/ch/cloud-hypervisor\n"
        );
        let expected = [
            format!("creche-vm-{ID}.scope"),
            "creche-vm-b.scope".to_owned(),
        ];
        assert_eq!(units(&listing), expected);
    }

    #[test]
    fn a_unit_without_the_prefix_and_a_blank_line_are_skipped() {
        let listing = "init.scope loaded active running System\n\n  \ncreche-vm-a.scope x\n";
        assert_eq!(units(listing), ["creche-vm-a.scope"]);
    }

    #[test]
    fn an_empty_listing_has_no_unit() {
        assert!(units("").is_empty());
    }
}
