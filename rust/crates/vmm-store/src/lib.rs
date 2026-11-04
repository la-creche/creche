//! The store of the vmm: everything that the vmm keeps per session on the
//! host disk. The session rows, the session disks, the root images and the
//! snapshots live here.
//!
//! The code is synchronous. It uses std, plus `creche-wire`, `serde` and
//! `serde_json`. The PR D4 adds `sha2`. It uses no async runtime. It never
//! calls `creche-vmm-confine` or `creche-vmm`.

pub mod layout;
pub mod rows;
pub mod stale;
