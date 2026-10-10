# creche-testkit

Test helpers for each crate of the Rust workspace, and the program
`creche-probe`. `rust/AGENTS.md` and the root `AGENTS.md` apply here too.

A test of a service must not start a real program, wait for a real clock or
call a real service. Each helper module of this crate gives one stand-in.

`creche-probe` is no stand-in. It is one program on each module of
`creche-runtime`. The process-level suite under `integration/proc` judges it
as a process, before a service has its Rust binary.

No release holds this crate. Add it to a crate only under
`[dev-dependencies]`.

## Layout

| File | What it holds | Packet |
|---|---|---|
| `src/lib.rs` | The list of the modules. | `foundation-skeleton` |
| `src/root.rs` | `TempRoot`: a directory that one test fills. | `foundation-skeleton` |
| `src/clock.rs` | `FixedClock` and `PausedClock`: a clock that the test moves. | `foundation-testkit` |
| `src/entropy.rs` | `CountingEntropy`: random bytes that the test knows. | `foundation-testkit` |
| `src/runner.rs` | `FakeRunner`: a `CommandRunner` that starts no program. | `foundation-testkit` |
| `src/program.rs` | `write_program`: a script that stands in for a child program. | `foundation-testkit` |
| `src/stub.rs` | `HttpStub` and `RawHttp`: a server and a client that a test scripts. | `foundation-testkit` |
| `src/router.rs` | `call`: one request through a router, with no socket. | `foundation-testkit` |
| `src/vectors.rs` | Each item of the crate `creche-vectors`, under the path `creche_testkit::vectors`. That crate is the one reader of the vector files under `vectors/data`. | `decisions-vectors-leaf` |
| `src/probe.rs` | The program `creche-probe`: its entry function, its start steps and its three routes. | `foundation-integration` |
| `src/bin/creche-probe.rs` | The `main` of the program: three steps and no other code. | `foundation-integration` |
| `tests/process.rs` | The tests that start the built program as a process. | `foundation-integration` |

No stub is left: each function of this crate has its body.

## Rules for a change here

1. The lint gate holds for this crate. No code of the testkit panics. The
   one exception is the handler of the route `/probe/panic` in
   `src/probe.rs`. It panics on purpose, behind the panic boundary of the
   edge layer, and it holds the one `#[expect(clippy::panic)]` of the crate.
2. Return `Result` from each function that can fail. The test calls
   `unwrap`.
3. Edit only the files of your packet. `lib.rs`, `Cargo.toml` and this file
   belong to the skeleton packet.
4. Do not use the client or the server of `creche-runtime` in `stub.rs`. The
   tests of that client and of that server use the stub.
5. `HttpStub` binds a loopback port that the operating system selects, or a
   Unix socket under a `TempRoot`. Never bind `0.0.0.0`.
6. Give each type that records a request a `Debug` that prints no header
   value.
7. A helper reads no file outside `rust/` and `vectors/data`
   (`rust/AGENTS.md`, "Tests").
8. Keep the name of a `TempRoot` short. The path of a Unix socket has 104
   bytes at most on macOS.
9. A build of this crate turns on the feature `test-util` of `tokio`. A
   program of this crate must not pause the time of `tokio`.
10. Give each type with a private field its `compile_fail` doc test
    (`rust/AGENTS.md`, "Tests"). The packet that writes the body of the type
    writes that test.
11. Some differences of a helper from its Python origin are in no vector.
    Describe such a difference in the doc comment of the helper. Pin it
    with one plain test.
12. Name the Python origin of a helper in its doc comment, with the file and
    the line. Some helpers have no Python origin. Say so there.
13. Write no `println!` and no `eprintln!` in code that is not a test.

## The program `creche-probe`

The program starts with the steps of `creche_runtime::service`. It serves
three routes behind the edge layer: `/healthz`, a route whose handler panics
and a route whose handler writes a file after a wait. The module doc of
`src/probe.rs` has the command line, the routes and each exit status.

The config type of the program is the config type of the noticeboard. Only
`creche-contracts` can read an `Env`, so the program has no variable of its
own. The suite gives the program the variables of the noticeboard
(`rust/proc/probe.run`).

These rules apply to a change of the program:

1. Keep the three steps of `main` in `src/bin/creche-probe.rs`, and add no
   other code there (`rust/AGENTS.md`, "The panic rule", clause 2). Put each
   other change in `src/probe.rs`.
2. Do not call `probe::entry` from a test in the test process. The function
   sets the panic hook of the process. Test a private function of the
   module, or start the program in `tests/process.rs`.
3. A test that makes the program write a line of the log goes to
   `tests/process.rs`. A line of a test in the test process goes to the
   output of the test program.
4. A test of `tests/process.rs` gives the program a directory of its own, a
   port of its own and its whole environment. It reads only what crossed the
   process boundary.
5. A test of `tests/process.rs` waits for a file, for a line or for the end
   of the process. It does not wait for a fixed time.
6. The program is no service. Do not add a route or a rule of a service to
   it. The port of a service proves that service with its own crate and its
   own file under `rust/proc`.

The proof of a change has two parts. `cargo test -p creche-testkit` runs the
tests of the program. `bin/proc-rust.sh` runs the scenarios of
`rust/proc/probe.run` against the built program.

## Known gaps

- This `CONTRACT-QUESTION` comment is open in `src/probe.rs`: no contract
  names the exit status of a program whose listener does not bind. The
  program ends with status 3 there, as the Python noticeboard does.
  `crates/creche-runtime/AGENTS.md` has the same question for each service. A
  change costs one constant, `NO_LISTENER`.
- This `CONTRACT-QUESTION` comment is open in `src/probe.rs`: no contract
  names the exit status of `--check` when the reader of its stdout left. The
  program drops the report and ends with status 0. The Python noticeboard
  does not end with status 0 there. No unit and no scenario gives `--check`
  such a stdout. The port of the noticeboard decides what its own `--check`
  does.
- A test of `tests/process.rs` that needs a listener selects a free port,
  releases it and then starts the program. Another process of the host can
  take the port between the two steps. The program then ends with the
  status of a bind that failed. The test starts it again with a new port,
  five times at most. A test of a start that must bind nothing holds its
  port itself, so it has no such gap.
- One test of `tests/process.rs` leaves a request open and then sends the
  stop signal. The signal can come before the program read the request. The
  program then writes no line, and the test starts it again, five times at
  most.
- The drop of a test of `tests/process.rs` ends its program. A test program
  that SIGKILL ends runs no drop, for example at a command time limit. Each
  `creche-probe` of that run then serves on its port until a signal ends it.
  `pkill -TERM -f creche-probe` ends each one, and also the programs of a
  test run that still goes. The owner of the crate decides if the program
  gets a time limit of its own for a test.
- The SIGHUP test of `tests/process.rs` cannot judge a run that ignores
  SIGHUP, for example a run under `nohup`. A child takes that from the
  process that started it. The test fails at its first line in such a run,
  and the failure names the cause.
