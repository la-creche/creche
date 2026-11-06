//! The store of the vmm: everything that the vmm keeps per session on the
//! host disk. The session rows, the session disks, the root images and the
//! snapshots live here.
//!
//! The code is synchronous. It uses std, plus `creche-wire`, `serde`,
//! `serde_json` and `sha2`. It uses no async runtime. It never
//! calls `creche-vmm-confine` or `creche-vmm`.

pub mod ch_remote;
pub mod claim;
pub mod images;
pub mod key;
pub mod layout;
pub mod remove;
pub mod retention;
pub mod rows;
pub mod serial_log;
pub mod session_disk;
pub mod sleep_gate;
pub mod snapshot;
pub mod stale;
