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

use std::fmt;
use std::io;
use std::process::{Command, ExitStatus, Stdio};

use creche_vmm_store::layout::SCOPE_PREFIX;

use crate::launch::Program;

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

/// The loaded user units with the VM prefix, in any state (fact 76).
fn list(systemctl: &Program) -> Result<Vec<String>, LeftoverError> {
    let pattern = format!("{SCOPE_PREFIX}*");
    let output = Command::new(systemctl.path())
        .args(["--user", "list-units", "--all", "--plain", "--no-legend"])
        .args(["--no-pager", "--", pattern.as_str()])
        .stdin(Stdio::null())
        .stderr(Stdio::inherit())
        .output()
        .map_err(LeftoverError::Run)?;
    if !output.status.success() {
        return Err(LeftoverError::List(output.status));
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
