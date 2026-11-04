# vmm

The crate `creche-vmm`: a library and the program `vmm` (decisions CH9,
CH7-5). One `vmm` process owns every Cloud Hypervisor VM of the playpen.
The root `AGENTS.md` applies here too.

## The three crates

| Crate | Directory | Calls |
|---|---|---|
| `creche-vmm` | `rust/crates/vmm` | `creche-vmm-confine`, `creche-vmm-store`, `creche-wire` |
| `creche-vmm-confine` | `rust/crates/vmm-confine` | `creche-vmm-store`, `creche-wire` |
| `creche-vmm-store` | `rust/crates/vmm-store` | `creche-wire` |

- `creche-vmm` composes two libraries: `creche-vmm-confine` and
  `creche-vmm-store`. `creche-vmm-confine` reads the types of
  `creche-vmm-store`.
- A crate calls only the crates in its row. For these three crates, this
  table replaces the layer rule of the root `AGENTS.md`.
- Each `Cargo.toml` declares every path edge of the table. A change adds
  no path edge.

## The program

`src/main.rs` documents the modes of `vmm` and the exit status 78.

## Rules

- Each new dependency is its own pull request, with its lock delta
  (decision CH9-1).
- A library returns a typed error. Only `src/main.rs` turns an error into
  an exit status.
- A crate exposes `pub` items only for the crates that list it in their
  row. This rule is the reading of the open gap G24 (Known gaps).

## Known gaps

- G24, `pub` items across the three vmm crates. The root `AGENTS.md` says
  to ask before a change widens visibility. The owner has not answered
  yet. This reading: `creche-vmm-store` and `creche-vmm-confine` expose
  `pub` items, and `creche-vmm` is a library with `pub` items. The other
  reading is one crate with private items, so the tracks share files. A
  change costs a merge of the three crates (`src/lib.rs`). Remove this
  entry when the owner answers.
- G21, the flavor of the tokio runtime. Decision CH26 names only the
  builder. The owner has not answered yet. This reading: `vmm serve` runs
  on the multi-thread runtime, and blocking work goes to `spawn_blocking`.
  The other reading is the current-thread runtime plus dedicated threads.
  A change costs the builder in `src/main.rs`, the tokio feature list in
  `rust/Cargo.toml` and a thread for each blocking job. Remove this entry
  when the owner answers.
