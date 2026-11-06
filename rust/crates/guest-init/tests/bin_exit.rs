//! The program `guest-init` returns the exit status of `run()`. A test
//! process is never PID 1, so the status stays 78 on every OS.

use std::process::Command;

#[test]
fn bin_exits_78() {
    let status = Command::new(env!("CARGO_BIN_EXE_guest-init"))
        .status()
        .expect("the guest-init program starts");
    assert_eq!(status.code(), Some(78));
}
