//! Removes the socket files that a killed Cloud Hypervisor leaves, before
//! the next boot or restore of the VM (decision CH19 (4), fact 75).
//!
//! A boot or restore with a stale `vsock.sock` exits with `Address in
//! use`. Under Landlock Cloud Hypervisor cannot remove `api.sock.lock`.
//! CH v54 also removes a stale `api.sock` itself before it binds the API
//! socket (`vmm/src/locked_unix_listener.rs`). The Landlock ruleset grants
//! no right to remove a file, so that removal fails with EACCES and CH
//! exits (`CreateApiServerSocket`, seen on a restore in the kvm job). So
//! the vmm removes a stale `api.sock` too.

use std::fmt;
use std::io;
use std::path::PathBuf;

use crate::layout::VmRunDir;

/// Why a stale file stayed.
#[derive(Debug)]
pub struct StaleError {
    /// The file that stayed.
    pub path: PathBuf,
    /// Why the removal failed.
    pub source: io::Error,
}

impl fmt::Display for StaleError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(formatter, "cannot remove {}", self.path.display())
    }
}

impl std::error::Error for StaleError {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        Some(&self.source)
    }
}

/// Removes a stale `vsock.sock`, `api.sock` and `api.sock.lock` from `vm`.
/// A file that is not there is no error. Every other file stays.
///
/// Decision CH19 (4): call this only after the leftover scopes are
/// stopped, so that no live Cloud Hypervisor holds the files.
///
/// Decisions CH51 and CH55 (PR D16): `vm` is the `vm/<id>` symlink to the
/// slot dir, and each removal goes through it into `slot/<k>/`. So call
/// this only while the VM holds its slot. Through a link to a slot that
/// another VM took, it removes the files of that VM. After
/// `RunRoot::unlink_vm` every file is missing, and this is a no-op.
///
/// # Errors
///
/// A `StaleError` for the first file that a removal fails on.
pub fn remove_stale(vm: &VmRunDir) -> Result<(), StaleError> {
    for path in [vm.vsock_socket(), vm.api_socket(), vm.api_socket_lock()] {
        match std::fs::remove_file(&path) {
            Ok(()) => {}
            Err(source) if source.kind() == io::ErrorKind::NotFound => {}
            Err(source) => return Err(StaleError { path, source }),
        }
    }
    Ok(())
}
