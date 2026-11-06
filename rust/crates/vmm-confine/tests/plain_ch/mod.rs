//! A CH process that the test starts itself, with no chain, killed and
//! reaped on drop. `tests/kvm_vm.rs` starts each CH through a stand-in
//! service, so it has no such process and does not take this module.
#![cfg(test)]

use std::process::Child;

/// A child process of the test, killed and reaped on drop. For a CH that
/// the test starts itself, a failed test leaves no VM. The CH of a `hold`
/// stand-in is not a child, so its test takes a `ScopeGuard`.
pub struct Vm(pub Child);

impl Drop for Vm {
    fn drop(&mut self) {
        let _ = self.0.kill();
        let _ = self.0.wait();
    }
}
