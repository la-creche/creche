//! The guest init: PID 1 of each Cloud Hypervisor VM (decisions CH9,
//! CH7-5).
//!
//! As PID 1 it runs the boot plan of `boot` (decision CH62 rule 4): the
//! mounts, the switch root into the root disk and `lo` up. Then it writes
//! `guest-init: up` and waits. PID 1 never exits on its own: the guest
//! kernel has `PANIC_TIMEOUT=0`, so an exit of PID 1 hangs the VM (fact
//! 53). Each fault powers the VM off instead (decision CH19 rule 3).
//!
//! Off Linux, and on Linux in a process that is not PID 1, `run` starts
//! nothing and returns exit status 78 (decision CH38).

use std::process::ExitCode;

#[cfg(any(test, target_os = "linux"))]
mod boot;
#[cfg(any(test, target_os = "linux"))]
mod lo;
#[cfg(target_os = "linux")]
mod power;

/// The exit status of a refused start (owner item 86), the same value as
/// `EX_CONFIG` in `rust/crates/vmm/src/main.rs`.
const EX_CONFIG: u8 = 78;

/// Runs the guest init. As PID 1 on Linux it never returns while the VM
/// runs. Each other process gets exit status 78.
#[must_use]
pub fn run() -> ExitCode {
    #[cfg(target_os = "linux")]
    if rustix::process::getpid().is_init() {
        return as_pid_1();
    }
    ExitCode::from(EX_CONFIG)
}

/// The life of PID 1. A boot fault and a panic both end in `power::off`
/// (decision CH19 rule 3). The default panic hook writes the panic text
/// to the console first.
#[cfg(target_os = "linux")]
fn as_pid_1() -> ExitCode {
    match std::panic::catch_unwind(boot::boot) {
        Ok(Ok(())) => {
            say("up");
            // PID 1 must not exit (fact 53). `park` may return early, so
            // it runs in a loop.
            loop {
                std::thread::park();
            }
        }
        Ok(Err(fault)) => say(&fault.to_string()),
        Err(_) => say("boot panicked"),
    }
    power::off()
}

/// Writes one `guest-init: <text>` line to the console (decision CH62
/// rule 26). The kernel opens `/dev/console` as fd 0, 1 and 2 of PID 1,
/// and `console=ttyS0` sends it to the serial file of the VM. A failed
/// write has no other place to go, so `say` drops it.
#[cfg(target_os = "linux")]
fn say(text: &str) {
    use std::io::Write as _;
    let _ = writeln!(std::io::stderr(), "guest-init: {text}");
}

#[cfg(test)]
mod tests {
    use super::*;

    /// A test process is not PID 1, so `run` refuses on every OS. As PID 1,
    /// for example in a container, `run` would boot, so the test stops.
    #[test]
    fn run_refuses_when_not_pid_1() {
        #[cfg(target_os = "linux")]
        assert!(
            !rustix::process::getpid().is_init(),
            "the test runs as PID 1"
        );
        assert_eq!(run(), ExitCode::from(EX_CONFIG));
    }
}
