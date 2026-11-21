# The vendored crates of ci-runner

This folder holds two crates of the repository `ci-runner` in the
organization `infra` on the Forgejo instance. The files are copies, byte
for byte, of commit `131ce43acd5185cdc40440ead64357aa03300bdb` of its
branch `main` (2026-11-21). Each file keeps its path under `crates/`.

| Crate | What ci-signer uses |
|---|---|
| `ci-runner-peer` | `check` of a passed guest fd and `check_daemon` of a control connection (the runner design, section 6.3) |
| `ci-runner-sys` | The two system calls of the peer check that no safe wrapper offers, `SO_PEERPIDFD` and `PIDFD_GET_INFO`. `ci-runner-peer` depends on it |

The licence of both crates is MIT, the `license` of the workspace of
ci-runner. The licence of this repository is MIT too.

## Why a copy and not a git dependency

The README of `ci-runner-peer` says to pin a git `rev` of the forge. The
CI of this repository runs on GitHub, and the GitHub runner cannot reach
the forge on the LAN. So `cargo` on GitHub cannot fetch that dependency.
A copy builds on each runner with no network to the forge.

## Rules

1. Change no file of this folder by hand. A newer commit of ci-runner is
   a new copy of each file and a new commit id here, in one commit.
2. `bin/tests/test_vendored_ci_runner.py` holds the SHA-256 of each file.
   A new copy changes the list in that test in the same commit.
3. The `Cargo.toml` of each copy reads `edition`, `rustix`, `libc` and the
   lint gate from `rust/Cargo.toml`. The workspace entries `rustix` and
   `libc` pin the versions that ci-runner pins: `rustix =1.1.5` and
   `libc =0.2.191`.
4. `ci-runner-sys` holds `unsafe` code with its own lint table
   (`unsafe_code = "deny"`). It is the one crate of this workspace with
   `unsafe`. Each other crate keeps `unsafe_code = "forbid"`.
