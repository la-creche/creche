//! The guest init: PID 1 of each Cloud Hypervisor VM (decisions CH9,
//! CH7-5). This skeleton holds no logic. The PID 1 check and the power-off
//! come in the next PR of the guest track.

use std::process::ExitCode;

/// The exit status of a refused start (owner item 86), the same value as
/// `EX_CONFIG` in `rust/crates/vmm/src/main.rs`.
const EX_CONFIG: u8 = 78;

/// Runs the guest init. It returns exit status 78 on every OS, because
/// this skeleton starts nothing (decision CH38).
#[must_use]
pub fn run() -> ExitCode {
    ExitCode::from(EX_CONFIG)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn run_returns_exit_code_78() {
        // `ExitCode` has no `PartialEq`, so the test compares the `Debug`
        // text.
        assert_eq!(format!("{:?}", run()), format!("{:?}", ExitCode::from(78)));
    }
}
