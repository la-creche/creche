//! The ci-runner daemon: one throwaway Cloud Hypervisor VM per waiting
//! Forgejo Actions job, on the host (`docs/design/ci-runner.md`).
//!
//! This crate holds the Forgejo API client (build plan PR 4). The job
//! loop, the per-job disks, the VM start and the program come in PRs 5
//! to 7.
//!
//! ```text
//!   names, secret        parsed values: no invalid name, no printed token
//!        |
//!   client               the three calls, in domain terms
//!     |       \
//!   transport  reply     one HTTP/1.1 exchange | status and body to a type
//! ```

pub mod client;
pub mod error;
pub mod names;
pub mod reply;
pub mod secret;
mod transport;
