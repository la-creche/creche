//! The program `guest-init`. It calls `creche_guest_init::run` and returns
//! its exit status.

use std::process::ExitCode;

fn main() -> ExitCode {
    creche_guest_init::run()
}
