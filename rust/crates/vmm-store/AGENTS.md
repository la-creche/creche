# vmm-store

The crate `creche-vmm-store`: what the vmm keeps per session on the host
disk. The root `AGENTS.md` and `rust/crates/vmm/AGENTS.md` apply here too.
The second file has the table of the three vmm crates.

## Rules

- The code is synchronous. It uses std, plus `creche-wire`, `serde` and
  `serde_json`. The PR D4 adds `sha2`. The crate uses no async runtime.
- The crate never calls `creche-vmm-confine` or `creche-vmm`.

## Known gaps

- G24, `pub` items across the three vmm crates. The gap is open.
  `rust/crates/vmm/AGENTS.md` has the reading.
