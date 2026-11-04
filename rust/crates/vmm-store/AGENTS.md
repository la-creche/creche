# vmm-store

The crate `creche-vmm-store`: what the vmm keeps per session on the host
disk. The root `AGENTS.md` and `rust/crates/vmm/AGENTS.md` apply here too.
The second file has the table of the three vmm crates.

## Rules

- The code is synchronous. It uses std, plus `creche-wire`, `serde`,
  `serde_json` and `sha2`. The crate uses no async runtime.
- The crate never calls `creche-vmm-confine` or `creche-vmm`.
- Decision CH44 rule 2 (PR D6, `images.rs`): a store copy that fails
  the identity check at VM start fails the start. `ImageStore::check`
  removes and repairs nothing. Item 42 Q42-2 adds that an operator step
  or a later re-import fixes the copy.
