//! The ci-runner daemon: one throwaway Cloud Hypervisor VM per waiting
//! Forgejo Actions job, on the host (`docs/design/ci-runner.md`).
//!
//! This crate holds the Forgejo API client (build plan PR 4) and the
//! per-job disks (PR 5). The job loop, the VM start and the program come
//! in PRs 6 and 7.
//!
//! ```text
//!   names, secret        parsed values: no invalid name, no printed token
//!        |
//!   client               the three calls, in domain terms
//!     |       \
//!   transport  reply     one HTTP/1.1 exchange | status and body to a type
//!
//!   net                  the MAC, the address and the DNS server of a VM
//!        |
//!   seed                 the cloud-init files of one job
//!        |
//!   disks                the run folder: seed, data disk, root copy
//!        |
//!   vfat                 the VFAT image of the seed
//! ```

pub mod client;
pub mod disks;
pub mod error;
pub mod names;
pub mod net;
pub mod reply;
pub mod secret;
pub mod seed;
mod transport;
mod vfat;
