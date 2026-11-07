//! The confinement of each Cloud Hypervisor process of the vmm: the launch
//! step in its own user scope (decision CH8-2), bound to the vmm unit
//! (decision CH61 rule 2), the limits and the access rules.
//!
//! The code is synchronous and uses no async runtime. It reads the types
//! of `creche-vmm-store` and never calls `creche-vmm`.

pub mod admission;
pub mod command;
pub mod deny;
pub mod family;
pub mod faults;
pub mod landlock;
pub mod launch;
pub mod leftovers;
pub mod limits;
pub mod mirror;
pub mod nofile;
pub mod output;
pub mod rfc3339;
pub mod scope;
pub mod slot_profile;
pub mod spawner;
pub mod stage;
pub mod vmm_unit;
