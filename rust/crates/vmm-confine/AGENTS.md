# vmm-confine

The crate `creche-vmm-confine`: the launch step, the limits and the access
rules of each Cloud Hypervisor process of the vmm. The root `AGENTS.md`
and `rust/crates/vmm/AGENTS.md` apply here too. The second file has the
table of the three vmm crates.

## Rules

- The code is synchronous. The crate uses no async runtime.
- The crate reads the types of `creche-vmm-store`. It never calls
  `creche-vmm`.
- `launch::run` is the mode `vmm launch`. `rust/crates/vmm/src/main.rs`
  calls it, so a change here never edits that file.

## Known gaps

- CONTRACT-QUESTION (landlock on macOS): the landlock crate builds only on
  Linux, and no row decides whether the workspace must build on macOS.
  The narrowest reading: `landlock` is a Linux-only dependency in
  `Cargo.toml`, and the code that calls it sits behind
  `cfg(target_os = "linux")`. CI runs on Linux only. Open until David
  decides.
