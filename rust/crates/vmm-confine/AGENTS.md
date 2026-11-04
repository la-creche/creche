# vmm-confine

The crate `creche-vmm-confine`: the launch step, the limits and the access
rules of each Cloud Hypervisor process of the vmm. The root `AGENTS.md`
and `rust/crates/vmm/AGENTS.md` apply here too. The second file has the
layer table of the three vmm crates.

## Rules

- The code is synchronous.
- The crate reads the types of `creche-vmm-store`. It never calls
  `creche-vmm`.
- `launch::run` is the mode `vmm launch`. `rust/crates/vmm/src/main.rs`
  calls it, so a change here never edits that file.

## Known gaps

- None.
