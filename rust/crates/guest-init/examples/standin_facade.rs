//! The stand-in facade for the kvm tests (plan PR S1, gap Q54-12 option A).
//! This stub has no logic. A later PR of the guest track fills it.

use std::process::ExitCode;

fn main() -> ExitCode {
    creche_guest_init::run()
}
