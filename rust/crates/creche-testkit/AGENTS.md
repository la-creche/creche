# creche-testkit

Test helpers for each crate of the Rust workspace. `rust/AGENTS.md` and the
root `AGENTS.md` apply here too.

A test of a service must not start a real program, wait for a real clock or
call a real service. Each module of this crate gives one stand-in.

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
| `src/vectors.rs` | A public reader of the vector files under `vectors/data`. | `foundation-testkit` |

`TempRoot` is complete. Each other body is a stub.

## Rules for a change here

1. The lint gate holds for this crate. No code of the testkit panics.
2. Return `Result` from each function that can fail. The test calls
   `unwrap`.
3. Edit only the files of your packet. `lib.rs`, `Cargo.toml` and this file
   belong to the skeleton packet.
4. A stub has the form that `crates/creche-runtime/AGENTS.md` gives, in
   "Rules for a change here". Remove its attribute when you write the body.
5. An opaque type of the skeleton has one private field of the type `()`.
   Replace that field when you write the body of the type.
6. Do not use the client or the server of `creche-runtime` in `stub.rs`. The
   tests of that client and of that server use the stub.
7. `HttpStub` binds a loopback port that the operating system selects, or a
   Unix socket under a `TempRoot`. Never bind `0.0.0.0`.
8. Give each type that records a request a `Debug` that prints no header
   value.
9. A helper reads no file outside `rust/` and `vectors/data`
   (`rust/AGENTS.md`, "Tests").
10. Keep the name of a `TempRoot` short. The path of a Unix socket has 104
    bytes at most on macOS.
11. A build of this crate turns on the feature `test-util` of `tokio`. A
    program of this crate must not pause the time of `tokio`.
12. Give each type with a private field its `compile_fail` doc test
    (`rust/AGENTS.md`, "Tests"). The packet that writes the body of the type
    writes that test.
13. Write each difference from a Python test helper as a row of a
    `DEVIATIONS` table in the test of the module. The row names the Python
    file and the line.
14. Name the Python origin of a helper in its doc comment, with the file and
    the line. Some helpers have no Python origin. Say so there.
15. Write no `println!` and no `eprintln!` in code that is not a test.

## The stubs

This list is the state of the skeleton. The packet `foundation-testkit` does
not edit this file when it writes a body. The integration packet removes the
list when no stub is left.

A stub panics when a test calls it. A packet that needs a helper of this list
starts only after `foundation-testkit` merged.

The list holds 22 stubs. Each stub is one `clippy::todo` attribute.

| File | Stubs | The stubs |
|---|---|---|
| `src/clock.rs` | 8 | `FixedClock::new`, `FixedClock::set`, `FixedClock::advance`, `FixedClock::now`, `FixedClock::monotonic`, `PausedClock::new`, `PausedClock::now`, `PausedClock::monotonic` |
| `src/entropy.rs` | 2 | `CountingEntropy::new`, `CountingEntropy::fill` |
| `src/runner.rs` | 4 | `FakeRunner::new`, `FakeRunner::script`, `FakeRunner::calls`, `FakeRunner::run` |
| `src/program.rs` | 1 | `write_program` |
| `src/stub.rs` | 4 | `HttpStub::loopback`, `HttpStub::unix`, `RawHttp::tcp`, `RawHttp::unix` |
| `src/router.rs` | 1 | `call` |
| `src/vectors.rs` | 2 | `index`, `surface` |

The skeleton fixed only the names of the types and the signatures above. The
packet `foundation-testkit` adds what a helper also needs:

- The type of a scripted answer and the type of a recorded request of
  `HttpStub`, and the functions that use them.
- The fields of `IndexRow`, `Surface`, `Vector`, `Input` and `Marker` in
  `vectors.rs`. `crates/creche-contracts/src/vectors.rs` holds the same
  forms as private test code.

## Known gaps

- Most bodies are stubs. "The stubs" lists them.
- The workspace holds two readers of the vector files: this one and the
  private one of `creche-contracts`. `agent-family` holds a third in its
  test. The owner of the crates decides if the two others move to this one.
