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

`vmm` takes exactly one argument, the mode:

1. `vmm serve` runs the host API server (decision CH20).
2. `vmm launch` is the launch step of one Cloud Hypervisor process
   (decision CH8-2). Only the vmm runs it.

Any other argument list exits with status 78. A mode with no code also
exits with status 78. An early install thus fails closed (owner item 86).
`src/main.rs` holds the one definition of the status. A library returns a
typed error, and `src/main.rs` turns each error into the status.

## Rules

- Each new dependency is its own pull request, with its lock delta
  (decision CH9-1).
- A crate exposes `pub` items only for the crates that list it in their
  row.

## Known gaps

- None.
