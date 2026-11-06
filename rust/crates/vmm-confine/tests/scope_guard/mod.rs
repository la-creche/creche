//! The user scope of one VM, killed on drop. The CH of the `hold`
//! stand-in is not a child of the test. The drop of the stand-in stops
//! its unit, and its `ExecStopPost=` step kills the scope (decision CH61
//! rule 1). The guard does not rely on that step: it kills every
//! process of the scope, then waits until systemd drops the unit. So a
//! failed test leaves no CH and no scope.
#![cfg(test)]

use std::process::{Command, Stdio};
use std::thread::sleep;
use std::time::{Duration, Instant};

use creche_vmm_store::layout::scope_unit;
use creche_wire::SessionId;

/// The `systemctl` of the runner.
const SYSTEMCTL: &str = "/usr/bin/systemctl";

/// The longest wait for the unit to go after the kill.
const GONE: Duration = Duration::from_secs(30);

/// The scope unit of one session. Declare it after the `Rig` or the
/// `Setup` of the test, so that it drops first.
pub struct ScopeGuard(String);

impl ScopeGuard {
    /// The guard of the scope of `id`.
    pub fn new(id: &SessionId) -> Self {
        Self(format!("{}.scope", scope_unit(id)))
    }

    /// Runs `systemctl --user <args> -- <unit>` and gives its stdout, or
    /// `None` when it did not run or failed.
    fn systemctl(&self, args: &[&str]) -> Option<String> {
        let output = Command::new(SYSTEMCTL)
            .arg("--user")
            .args(args)
            .arg("--")
            .arg(&self.0)
            .stdin(Stdio::null())
            .stderr(Stdio::null())
            .output()
            .ok()?;
        output
            .status
            .success()
            .then(|| String::from_utf8_lossy(&output.stdout).into_owned())
    }
}

impl Drop for ScopeGuard {
    /// Sends SIGKILL to every process of the scope and waits until the
    /// unit is gone. A scope that ends failed blocks its name, so each
    /// round also resets it. A drop never panics.
    fn drop(&mut self) {
        let _ = self.systemctl(&["kill", "--signal=SIGKILL"]);
        let deadline = Instant::now() + GONE;
        while Instant::now() < deadline {
            let _ = self.systemctl(&["reset-failed"]);
            let list = [
                "list-units",
                "--all",
                "--plain",
                "--no-legend",
                "--no-pager",
            ];
            if self
                .systemctl(&list)
                .is_some_and(|units| units.trim().is_empty())
            {
                return;
            }
            sleep(Duration::from_millis(50));
        }
    }
}
