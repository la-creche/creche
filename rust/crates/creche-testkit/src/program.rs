//! A script that stands in for a child program.
//!
//! The body here is a stub. `AGENTS.md` of this crate lists the stubs and the
//! packet that writes them.

use std::io;
use std::path::PathBuf;

use crate::root::TempRoot;

/// Writes the shell script `script` as the file `name` under `root`, with
/// mode `0700`, and returns its path.
///
/// A test of a command runner starts the script in place of a real program.
///
/// # Errors
///
/// The error of the operating system when it cannot write the file.
#[expect(
    clippy::todo,
    unused_variables,
    reason = "skeleton: packet foundation-testkit writes this body"
)]
pub fn write_program(root: &TempRoot, name: &str, script: &str) -> io::Result<PathBuf> {
    todo!()
}
