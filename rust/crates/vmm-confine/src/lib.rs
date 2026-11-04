//! The confinement of each Cloud Hypervisor process of the vmm: the launch
//! step in its own user scope (decision CH8-2), the limits and the access
//! rules.
//!
//! The code is synchronous and uses no async runtime. It reads the types
//! of `creche-vmm-store` and never calls `creche-vmm`.

pub mod admission;
pub mod deny;
pub mod faults;
pub mod landlock;
pub mod launch;
pub mod leftovers;
pub mod scope;
pub mod spawner;
