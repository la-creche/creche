# guest-init

The crate `creche-guest-init`: a library and the program `guest-init`,
PID 1 of each Cloud Hypervisor VM (decisions CH9, CH7-5). It is the fourth
program of the vmm row. Only the root `AGENTS.md` applies here.

## Layer rule

`creche-guest-init` calls `creche-wire` only. It depends on no vmm crate,
and no vmm crate depends on it. It runs blocking std threads, not tokio
(CH62 rule 2).

## The program

`run()` returns exit status 78 on every OS. This skeleton holds no logic.
The PID 1 check and the power-off come in the next PR of the guest track.
`examples/standin_facade.rs` is a stub with the same body.

## Known gaps

None.
