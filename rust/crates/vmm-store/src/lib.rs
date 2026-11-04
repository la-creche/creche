//! The store of the vmm: everything that the vmm keeps per session on the
//! host disk. The session rows, the session disks, the root images and the
//! snapshots live here.
//!
//! The code is synchronous and uses std only. It never calls the crates
//! above it: `creche-vmm-confine` and `creche-vmm`.
