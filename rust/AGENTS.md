# rust

Rules for all Rust code in this repository. The root `AGENTS.md` applies here
too.

The Rust code uses type-driven design. A type permits only valid values. Code
that holds a value does not check the value again. The compiler then refuses a
defect that a test finds late.

## Layout

| Path | What it holds |
|---|---|
| `Cargo.toml` | The workspace, the dependency versions and the lint gate. |
| `Cargo.lock` | The locked versions. Commit it. Each gate command uses `--locked`. |
| `rust-toolchain.toml` | The one toolchain version. rustup reads it for each cargo command under `rust/`. |
| `rustfmt.toml` | The line width: 100, the same as ruff. |
| `clippy.toml` | The lints that a test can break. |
| `crates/<name>/` | One crate. Each directory there is a workspace member. |

| Crate | What it holds |
|---|---|
| `creche-contracts` | The wire types and the config types of the contracts. `ids::FamilyName` is the pattern for each new type. |

| Module of `creche-contracts` | What it holds |
|---|---|
| `ids` | Each id grammar that `vectors/data/ids` covers. One type for each grammar. |
| `secret` | `Secret`, the type of a token or a key. |
| `family` | The family file: contract 01. |
| `server` | The MCP server file: contract 01b. |
| `session` | The session API: contract 02. |
| `channel` | The channel protocol: contract 03. "The channel module" below has its parts. |
| `grants` | The grant file, the call body, the approval body and the audit record: contract 04. |
| `status` | The status document: contract 05. |
| `manifest` | The component manifest and the release request: contract 06. |
| `config` | The config of each process. |
| `vectors` | Test code only. It reads the vector files under `vectors/data/`. |

## Where a new type goes

1. Put the types of one contract in the module of that contract. The table
   above names each module.
2. Change only the file of your module. `lib.rs` declares each module.
3. Use the id types of `ids`. Do not write a second check for a grammar that
   `ids` holds.
4. If `ids` lacks an id type that your module needs, define the type in your
   module. Say so in the pull request. The owner of the crate moves the type
   to `ids` when a second contract needs it. Give the vectors of that type a
   surface name that starts with the name of your module, not with `id.`. A
   test of `ids` fails on an `id.` surface that no table of `ids` names.
5. Ask the owner of the crate before you change `ids`, `secret` or
   `vectors`. A change there reaches each module.
6. In `ids`, for an id that is one run of ASCII bytes, write a `Run`
   constant. Then call `run_id!`. The macro makes the type and its error
   type in the form of `FamilyName`. `Run` and `run_id!` are private to
   `ids`. In another module, write the check by hand over ASCII bytes.
7. For an id with parts, write a struct with a private field for the text
   and for each part. `ids::Tag` is the pattern.
8. Give each type its own doc comment and its own error type. The doc
   comment names the contract section.

## Checks

You need rustup. It installs the toolchain at the first cargo command under
`rust/`.

1. Run each cargo command from `rust/`. rustup finds the toolchain file only
   from there.
2. Run `bin/rust-gate.sh --tests` before you push. CI runs the same script.

`bin/rust-gate.sh` runs these steps in this order:

1. The `[lints]` check. Each crate must take the lint gate.
2. The include check. No Rust source file includes a Markdown file.
3. `cargo fmt --all --check`.
4. `cargo clippy --workspace --all-targets --locked -- -D warnings`.
5. `cargo test --workspace --locked`, with `--tests` only.

`bin/quality-gate.sh` starts `bin/rust-gate.sh` only for a change that
touches `rust/`. `bin/AGENTS.md` has the table. Every path under `rust/`
counts, a Markdown file too. The exception is a push of Markdown files only.
That push runs the tests marked `docs` and no cargo step.

A push that changes a path under `vectors/` is the one other case. It starts
`bin/rust-gate.sh` when `cargo` is on `PATH`. In CI, a code change under
`vectors/` runs the `rust` job.

## The rules

Each rule has its reason. Do not break a rule without a change to this file.

1. **Parse at the edge.** Untyped data becomes a raw `serde` type first. A
   conversion that can fail then makes the valid type. Code after the
   conversion never sees an invalid value.
   Reason: one conversion holds every check, so no code path can skip a
   check.
2. **Give each id, name, path, size, duration and token its own type.** The
   type has a private field and a parsing constructor. Do not implement
   `Default`. Do not derive `Deserialize` directly. Use
   `#[serde(try_from = "...")]`, so `serde` reads through the constructor.
   Reason: a public field, a `Default` and a direct `Deserialize` are each a
   constructor that skips the check.
3. **Make a closed set an enum.** An enum can cross a process boundary. For
   each such enum, the doc comment of the type says what a reader does with
   an unknown value. The reader refuses the value, or accepts it as an
   `Unknown` variant.
   Reason: components release separately, so a reader can get a value from a
   newer writer.
4. **Return `Result` with a typed error enum from a function that can fail.**
   Do not use `anyhow` in a library crate.
   Reason: the signature shows each failure, and the compiler makes the
   caller handle each one. An error with no type hides them.
5. **Do not use `unwrap`, `expect`, `panic!` or a `[]` index outside a test.**
   The lint gate refuses them. Write an exception as
   `#[expect(clippy::<lint>, reason = "...")]` on one item. Never write
   `#[allow]`. The lint gate refuses it. It also refuses an `#[expect]` that
   gives no reason.
   Reason: each one stops the process on a value that the code did not
   expect. `#[expect]` fails the build when the exception is not necessary,
   and `#[allow]` stays.
6. **Give a secret its own type.** The type has a `Debug` that prints no
   secret. It has no `Display` and no `Serialize`. `secret::Secret` is that
   type. Compare a secret only with `Secret::matches`. Read its bytes only
   with `Secret::expose_secret`.
   Reason: the secret then cannot go to a log line, to a page or to a wire by
   accident. A search for `expose_secret` finds each place where a secret
   leaves the type.
7. **Do not put `serde_json::Value` in a contract type.** The exception is a
   field that the contract calls opaque.
   Reason: a `Value` holds any shape, so the type checks nothing.
8. **State the failure action at each process edge.** A process edge is a
   place where a process parses a file, a config or a message. The code
   there says what occurs when the parse fails. It keeps the last good value
   and publishes a fault, or it refuses, or it exits with `EX_CONFIG`, which
   is exit status 78. A daemon must not restart in a loop on an invalid
   value.
   Reason: the types say nothing about a parse that fails. A daemon that
   exits on an invalid file and starts again each 5 seconds is an outage.
9. **Do not use a regex for an id grammar.** Write the check by hand over
   ASCII bytes.
   Reason: in a Rust pattern, `\d` and `\w` also match characters that are
   not ASCII. A check over bytes has one meaning and needs no crate.
10. **Give each contract type a differential test against the Python
    implementation.** The vector files under `vectors/data/` hold what the
    Python code does. "The differential test" below says how a test reads
    them.
    Reason: the Python code is the behavior that runs on the host. A port
    that passes only its own tests can differ from that behavior.
11. **Keep Rust code under `rust/` until a release of its component uses
    the Rust code.** Do not put a Cargo file at the repository root or in a
    package directory.
    Reason: the tag allocator (`handover/src/handover/allocate.py`) mints a
    tag for a component when a path under that component changes. A path
    under `rust/` is under no component, so a change here mints no tag and
    starts no release.

## When two Python copies of a grammar disagree

The Python code holds more than one copy of most id grammars.
`vectors/data/ids/disagreements.json` lists each input on which two copies of
one grammar give different results.

1. The Rust type takes the strictest copy. It refuses each input that one
   copy refuses.
2. Mark the type with a `CONTRACT-QUESTION` comment. The comment names each
   copy and what the copy does.
3. List the question under "Known gaps".
4. In the test table of the type, give each surface one stance. `equal` means
   that the type and the copy agree on each vector. `stricter` means that the
   copy accepts an input of `disagreements.json` and the type refuses it.

Reason: a value passes more than one copy before the platform uses it. The
strictest copy is thus the grammar that holds on the host. A type that takes
a laxer copy accepts a value that a Python component refuses later.

When each Python copy accepts an input, the Rust type accepts it too. This
rule also applies when a stricter reading of the contract is possible. Name
such a case in the pull request. The owner decides it.

The rule has two exceptions:

- A digit that is not ASCII. Rule 9 refuses it. Each such difference is a
  row of the `DEVIATIONS` table in the test.
- A number of more than 4300 digits in a version. Python reads no longer
  text as an integer, so the types refuse it. No vector holds such a number.

## The channel module

`crates/creche-contracts/src/channel.rs` declares the parts. Each part is a
file under `src/channel/`.

| Part | What it holds |
|---|---|
| `frame` | `LineSplitter`: one record for each line. `Refusal`: why the host drops a line. |
| `host` | `HostMessage`: what the host writes and the playpen reads. |
| `claim` | `PlaypenLine`: what the host reads from a line of the playpen. `parse` reads one record. |
| `playpen` | `PlaypenMessage`: what the playpen writes. |
| `vocabulary` | Each closed set of names of a line, as an enum. |
| `json`, `text`, `number` | The JSON reader and the values of a line from the sandbox. |

The direction from the playpen to the host has two types. The host reads
each field as a claim and keeps what the Python host keeps. The playpen
writes only what the contract permits.

Rule 1 names a raw `serde` type. The `channel` module is the one exception.
Its raw type is `channel::json::Json`, from a reader of its own. The Python
host reads a line with `json.loads`, and `serde_json` does not read what
`json.loads` reads:

1. `json.loads` reads `NaN`, `Infinity` and `-Infinity`. `serde_json`
   refuses them.
2. `json.loads` keeps an integer of 4300 digits. `serde_json` reads an
   integer past 64 bits as a float.
3. `json.loads` reads a surrogate that has no partner, for example
   `\ud800`. `serde_json` refuses it.
4. Python 3.12 reads a line that nests 9997 levels. `serde_json` stops at
   128 levels.

The reader has these properties:

- It uses no recursion. The depth of a line cannot exhaust the stack.
- It stops at 9000 levels. Each supported Python version reads that depth.
- The `parse` of `claim` gives `malformed` for a line of 9001 levels or
  more. The Python host gives `malformed` when `json.loads` raises
  `RecursionError`. The deepest line that it accepts has 9997 levels on
  Python 3.12, 9998 on 3.13 and about 116,000 on 3.14. The reader thus
  refuses a line that each supported Python version accepts, up to the limit
  of that version. One vector holds such a line, and it is a row of the
  `DEVIATIONS` table of `claim`.
- The `parse` of `claim` gives `malformed` for an integer of more than 4300
  digits. Python raises `ValueError` there, and the Python host gives
  `malformed`.
- `Json` drops, copies, compares and prints with no recursion.

Rule 7 holds. One field holds a `Json`: the `event` of a line. Contract 03
§5.1 makes that value opaque.

The writer of `json` makes the bytes of
`json.dumps(value, separators=(",", ":"), ensure_ascii=False)`. The host
counts those bytes against the size limit of an event. A float has the text
that `repr` of Python gives.

## Code style

The code style rules of the root `AGENTS.md` apply. In Rust they read:

- Extract a repeated or meaningful value into a named `const`. Keep a one-off
  value inline.
- Return early. Use `?` and `let ... else`. Avoid deep nesting.
- Keep function names under 30 characters. The name of a test is a
  sentence and can be longer.
- Use an enum, not a `bool`, for a function parameter.
- Put a blank line between logical blocks.
- Keep an item private unless the design needs it public. Use `pub(crate)`
  before `pub`. Ask before you widen visibility.
- A layer talks only to the layer directly below it.
- `cargo fmt` sets the format. Do not format by hand.

## The lint gate

`[workspace.lints]` in `Cargo.toml` is the lint gate. It forbids `unsafe`
code. It denies a `Result` that the code drops without a check. It denies
the lints of rule 5 and these: `todo`, `unimplemented`, `unreachable`,
`string_slice`, `as_conversions`, `unwrap_in_result`, `panic_in_result_fn`,
`await_holding_lock`, `dbg_macro`, `exit` and `mem_forget`. It warns about a
public type with no `Debug`. clippy runs with `-D warnings`, so a warning
also fails the gate.

Two more lints hold the exception rule of rule 5. `allow_attributes` denies
each `#[allow]`. `allow_attributes_without_reason` denies an `#[expect]` that
gives no reason. Without them, one attribute lifts the lint gate for an item
or for a crate.

`bin/tests/test_rust_workspace.py` pins each entry of the lint gate and of
`clippy.toml`. To change an entry, change the pin in the same commit. Give
the reason in the commit message.

- Each crate has these two lines in its `Cargo.toml`: `[lints]`, then
  `workspace = true`. Write them in that form. `bin/rust-gate.sh` reads no
  other spelling and refuses a crate without them.
- A crate without the two lines builds with no lint of the workspace. cargo
  does not tell you.
- `clippy.toml` lets a test use `unwrap`, `expect`, `panic!` and a `[]`
  index.
- A test function returns `()`. The lint gate refuses an assertion or an
  `unwrap` in a function that returns `Result`, in a test too.
- Put a test helper in a `#[cfg(test)]` module. clippy does not count a
  helper outside such a module as test code, in a file under `tests/` too.
- The lint gate reads the text of the code. Code that passes it can panic,
  for example on a division by zero.

## Tests

- Put the unit tests of a module in a `#[cfg(test)] mod tests` in the same
  file.
- Give each parsing constructor one table of accepted texts and one table of
  refused texts. For a text grammar, refuse a final newline and a digit that
  is not ASCII.
- Give each type with a private field a `compile_fail` doc test. It shows
  that code outside the module cannot build the type from a raw value. Put a
  doc test that compiles beside it, with the same `use` line. A wrong path
  then cannot make the `compile_fail` test pass.
- An error code on a `compile_fail` test, for example `E0423`, is a note for
  the reader. The toolchain of this workspace does not check the code. The
  test passes on each compile error.
- Make the private field the only compile error of a `compile_fail` test. A
  struct literal that omits a field fails for the absent field, also when
  each field is public. Name each field, or take the other fields from a
  valid value: `Ready { caps: Vec::new(), ..line }`.
- To prove a `compile_fail` test, make each field of the type public for one
  local run. The test must then fail.
- A Rust test reads no file outside `rust/` and `vectors/data`. The gate
  runs cargo only for a change under `rust/` or `vectors/`, so a change to a
  file elsewhere does not run the test. If a later change needs such a file,
  add its directory to `bin/lib/rustrule.sh` first.
- A Rust source file includes no Markdown file. A push of Markdown files
  only runs no cargo step. `bin/rust-gate.sh` refuses a line that holds
  `include_str!`, `include_bytes!` or `include!` and a name that ends in
  `.md`.
- A push that changes only `rust/` runs no pytest suite. A Python test that
  reads a file under `rust/` then runs first in CI.
  `bin/tests/test_rust_workspace.py` is such a test.

### The differential test

`crates/creche-contracts/src/vectors.rs` reads the vector files under
`vectors/data/`. It is test code. `vectors/README.md` holds the file format.

1. Call `vectors::surface` with the name of a surface. The function finds
   the file in `vectors/data/index.json`. It stops the test on a format that
   is not 1 and on a count that differs from the index.
2. Read the input of each vector with `Input::text`, `Input::bytes` or
   `Input::args`.
3. Call the Rust code with the input.
4. Compare the result with the `result` of the vector. Compare the value with
   `Vector::value` and the refusal with `Vector::refusal`, as parsed JSON.
5. For the result `raised`, make sure that the Rust code refuses the input.

A value, a refusal and an argument of a vector can hold a marker object.
`vectors::Marker::of` reads one. `vectors/README.md` lists the six markers.

Rules for the test:

- Write one test for each type. The test walks each vector of each surface
  that the type implements.
- Put a table in the test that names each surface. Make the test fail when
  the index holds a surface of your module that no table names.
- Write each difference on purpose as a row of a `DEVIATIONS` table in the
  test. The row names the surface, the vector and the contract section. Make
  the test fail for a row that names no difference.
- Do not compare against a count of vectors that the test holds. A change to
  a product package can add a vector with no change under `rust/`.
- `ids::tests::python` is the pattern.

## Dependencies

- Write each dependency version one time, in `[workspace.dependencies]`. A
  crate refers to it with `workspace = true`.
- Add a third-party crate only when the code needs it. Each crate adds code
  that no person here reviews.
- A crate has no `version` key. No number lives in a file. A version is a
  tag that CI allocates.
- Commit `Cargo.lock` with each change to a dependency.

## Known gaps

- CI does not run `cargo deny`. No check reads the advisories or the
  licenses of the locked crates.
- No release uses Rust code. The component manifest has no kind for a
  compiled binary.
- Seven modules of `creche-contracts` hold a doc comment and no type:
  `family`, `server`, `session`, `grants`, `status`, `manifest` and
  `config`.
- These `CONTRACT-QUESTION` comments are open in
  `crates/creche-contracts/src/ids.rs`:
  1. `Ulid`, contract 02 §2. One Python copy of seven accepts a final
     newline. The type refuses it.
  2. `ToolName`, contract 01 §3.4 and contract 01b §5. The contracts give no
     cap. The three Python copies have no cap, a cap of 128 and a cap of 64.
     The type has the cap of 64.
  3. `EnvName`, contract 01b §4.1. The contract gives no grammar. One Python
     copy has no cap, and one has a cap of 64. The type has the cap of 64.
  4. `PackageVersion`, contract 01b §3.1. The contract gives no grammar. One
     Python copy permits `+` and `-` and has no cap. The type takes the other
     copy: no `+`, no `-` and a cap of 64.
  5. `Version`, `ContractVersion` and `Tag`, contract 06 §2 and §3. Each
     Python copy accepts a decimal digit that is not ASCII. The types refuse
     it.
  6. `OwuiChatId::session_id`, contract 02 §2. The Python door makes a
     session id of 133 bytes from a chat id of 128 bytes. The function
     refuses to make that session id.
  7. `Version`, `ContractVersion` and `Tag`, contract 06 §2 and §3. The
     contract gives no cap on the digits of a number. Python reads a text of
     4300 digits at most as an integer. The types have that cap.
- `Secret` does not erase its bytes when the value drops. A sure erase needs
  `unsafe` code, and the lint gate forbids `unsafe` code.
- `Secret::matches` has no branch on a byte of the secret. The compiler gives
  no proof that its time is constant.
- The id types accept what each Python copy accepts, also where a stricter
  reading of a contract is possible. The owner decides each case. Three
  examples:
  1. A version number of 4300 digits. The number does not fit `u64`.
  2. A version number with a zero at its start.
  3. A sandbox number with a zero at its start.
- These `CONTRACT-QUESTION` comments are open in
  `crates/creche-contracts/src/channel/`:
  1. `json::MAX_LINE_DEPTH`, contract 03 §13 rule 1. The contract gives no
     nesting limit for a line. The limit of Python changes with its version:
     9997 levels on 3.12, 9998 on 3.13 and about 116,000 on 3.14. The reader
     stops at 9000 levels. It refuses a line of 9001 levels or more, and each
     supported Python version accepts such a line up to its own limit.
  2. `claim::MAX_EVENT_DEPTH`, contract 03 §13 rule 6. The contract gives no
     nesting limit for an event. The Python host keeps only the type of an
     event that nests more than 64 levels. The reader does the same.
  3. `claim::MAX_LOG_CHARS`, contract 03 §8. The contract caps a message at
     4 KiB. The Python host cuts at 4096 code points. The reader does the
     same.
  4. `host::ConfigRev` and `host::EntryId`, contract 03 §4.1. The contract
     gives no grammar. The types take the rule of the playpen: 1 to 200
     bytes.
  5. `host::PromptText`, contract 03 §4.3. The contract gives no cap for the
     message of `steer`. The type takes the cap of a prompt, as the playpen
     does.
  6. `host::Model`, contract 03 §4.1. The contract gives no grammar. The type
     takes 1 to 200 bytes.
  7. `host::ProtocolVersion`, contract 03 §3. The contract gives no grammar
     for a number. The type takes two numbers of 1 to 9 ASCII digits.
- The host side of `channel` accepts what the Python host accepts, also
  where a stricter reading of contract 03 is possible. The owner decides each
  case. Five examples:
  1. A session id and a turn id of a line can be each text.
     `TurnAddress` is the check that follows.
  2. `turn_seq` and each count can be an integer past 64 bits.
  3. `cost_usd` can be `NaN` or `Infinity`.
  4. A text outside an event can hold a lone surrogate.
  5. `turn_failed` with no session, no turn and the `turn_seq` 0 is
     `malformed`. Contract 03 §5.1 permits that line.
- No vector covers the side of the playpen: `HostMessage::parse` and
  `PlaypenMessage`. The tests read each line of one side with the parser of
  the other side. `HostMessage::parse` is stricter than the TypeScript
  playpen in ten places:
  1. A required number with a fraction or an exponent is a fault: `600.0`.
     The same applies to `grace_ms`. The playpen reads `600.0` as 600.
  2. A session id has 128 bytes at most. The playpen permits 200.
  3. A sandbox number has 9 digits at most.
  4. A text with a lone surrogate is not a text.
  5. A protocol version is two numbers. The playpen takes each text with a
     `.`.
  6. A workspace kind is `code-sandbox`. The playpen reads each text.
  7. A cap counts bytes. The playpen counts UTF-16 code units.
  8. A `model` has 200 bytes at most. The playpen keeps each text.
  9. An `env_epoch` is less than 2^64. The playpen reads a larger one as a
     float.
  10. A line with an integer of more than 4300 digits is not JSON, in each
      field. The playpen reads that integer as a float.
- `HostMessage::parse` and the playpen accept two lines with different
  values:
  1. An empty `model` is no model. The playpen keeps the empty text.
  2. An optional number of `hello` is absent when it has a fraction or an
     exponent, and when it is 2^64 or more. The playpen keeps `900.0` as 900
     and keeps the large number as a float.
- Only `HostMessage::parse` holds the range of `deadline_s`, the range of
  `grace_ms` and the count of attachment names. `Seconds`, `Millis` and the
  list of names have no bound, as the Python builders have none. A host can
  write a line that the playpen refuses for one of the three.
- `channel::claim::Event::from_json` refuses an event over a limit. The
  playpen truncates such an event (contract 03 §8). No Rust code does that.
- `Event::from_json` and `playpen::EventMessage::new` refuse an event with a
  number that is not finite, for example `1e999`. The playpen writes `null`
  for that number. The host side keeps such a number, as the Python host
  does.
- `channel` has no function that maps a `FailReason` to a turn reason of
  contract 02 §14. The `session` module holds no turn reason yet.
