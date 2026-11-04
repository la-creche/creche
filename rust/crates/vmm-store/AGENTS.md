# vmm-store

The crate `creche-vmm-store`: what the vmm keeps per session on the host
disk. The root `AGENTS.md` and `rust/crates/vmm/AGENTS.md` apply here too.
The second file has the table of the three vmm crates.

## Rules

- The code is synchronous. It uses std, plus `creche-wire`, `serde`,
  `serde_json` and `sha2`. The crate uses no async runtime.
- The crate never calls `creche-vmm-confine` or `creche-vmm`.

## Known gaps

- CONTRACT-QUESTION (PR D6, `images.rs`): no row decides what the vmm
  does with a store copy that fails the check at VM start. `import`
  returns the recorded entry and does not copy again, so nothing repairs
  the copy. The narrowest reading: `ImageStore::check` removes and
  repairs nothing, and the start fails. The options are a re-import at
  the next start or an operator step.
- CONTRACT-QUESTION (PR D8, `ch_remote.rs`): no row decides the timeout
  of a `ch-remote` call. A pause takes about 4 ms (fact 72) and a 16 GiB
  snapshot about 12 s (gap G16). The narrowest reading: each call takes
  its timeout from its caller, and this crate names no value.
