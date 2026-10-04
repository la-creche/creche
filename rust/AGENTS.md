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
| `channel` | The channel protocol: contract 03. |
| `grants` | The grant file, the call body, the approval body and the audit record: contract 04. |
| `status` | The status document: contract 05. |
| `manifest` | The component manifest and the release request: contract 06. |
| `config` | The config of each process: the site file, the environment of each daemon, the roster and the mount files. "The config of a process" below holds its rules. |
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

## The config of a process

`creche_contracts::config` holds one type for the config of each daemon, and
one type for each config file. A type holds an address, a path or a duration,
and never a raw text.

| Module of `config` | What it holds |
|---|---|
| `site` | The site file: `SiteFile` is the raw form, and `Site` is the valid form. |
| `attendance`, `caregiver`, `chaperone`, `door_owui`, `door_trigger`, `noticeboard`, `intake` | The config of one daemon. |
| `roster` | The roster of the chaperone. `RawRoster` takes its tree through `serde`. |
| `mounts` | `runtime.json`, `creds.json` and the env file of the playpen. |

A binary crate loads its config in this sequence:

1. Build the map of the variables one time, with
   `Env::from_os(std::env::vars_os())`. Do not call `std::env::vars`. It
   stops the process on a value that is not UTF-8.
2. Read each file that the config needs, for example the site file or a key
   file. Give the text to the constructor. No config type reads the
   environment or a file.
3. Call the constructor of the config type, for example
   `AttendanceConfig::from_env`. It returns each error of the parse, not only
   the first one.
4. Give the result to `config::start`. It applies the failure action of the
   type.
5. For `Start::Exit`, write each error to the log. Then return the status
   from `main` as an `ExitCode`. Do not call `std::process::exit`. The lint
   gate refuses it.
6. For `Start::RefuseEachCall`, start the listener, refuse each call and
   publish the errors as a fault.
7. At a reload, give the last good value and the new result to
   `config::reload`. For `Reload::Kept`, publish the fault and continue.

The rule against a crash loop:

- A daemon must not start again in a loop on a config that is not valid. No
  restart corrects such a config.
- Each config type states its failure action: it implements `Checked`.
  `config::start` takes only a type that does.
- `AtStart::ExitConfig` exits with status 78, `EX_CONFIG`. The unit file of
  that daemon must hold `RestartPreventExitStatus=78`.
- Today each of the seven daemon units holds `Restart=always` and
  `StartLimitIntervalSec=0`, and none holds that line. systemd thus starts a
  daemon again after each exit status, with no limit. The delay is 5 seconds
  at first and 120 seconds at most.
- Add the line in the pull request that moves a unit to a Rust binary.
  `bin/tests/test_rust_config_units.py` pins what the units hold today.
  Change the pin in the same pull request.
- systemd reads `RestartPreventExitStatus` only for the exit status of the
  main process. It does not read the line for a process of `ExecStartPre=`.
- Three units hold `ExecStartPre=<service> --check` today:
  `creche-attendance`, `creche-door-owui` and `creche-noticeboard`. With a
  config that is not valid, the check fails before the main process starts.
  systemd then starts the unit again, also when the unit file holds the line.
- In the pull request that moves such a unit to a Rust binary, remove its
  `ExecStartPre=` line. The main process does the same parse.
- Then prove on a Linux host that the unit stays stopped after exit status
  78. No test in this repository runs systemd.
- `AtReload::KeepLastGood` never exits. A reload that fails keeps the last
  good value.
- `config::reload` takes only a type that says `AtReload::KeepLastGood`. A
  call with a type that says `AtReload::NotRead` does not build.
  `cargo build` and `cargo test` report that error, and `cargo check` does
  not.
- `config::start` and `config::reload` take the error type of each parse.
  The roster and the site file have an error type of their own.

More rules for a config type:

- An error names the variable and the reason. It never holds the value,
  because a value can be a secret.
- Give each default of the code as text to the parser of the type. A default
  that is not valid is then an error and not a panic.
- A config holds the path of a key file and never the key. The binary reads
  the file and calls `config::key_of_file`.
- Give each variable of a unit a constant in the module of its daemon.
  `bin/tests/test_rust_config_units.py` fails for a variable of a unit that
  has no constant.
- `config/python.rs` holds the differential test of the module. Its table
  `SURFACES` names each `config.` surface, and its table `DEVIATIONS` names
  each difference on purpose.

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
- `serde_json` has the feature `float_roundtrip`. It then reads each JSON
  float as the nearest float, as Python does. Without the feature, a float of
  16 digits or more can differ from the Python value in its last bit. Do not
  remove the feature.

## Known gaps

- CI does not run `cargo deny`. No check reads the advisories or the
  licenses of the locked crates.
- No release uses Rust code. The component manifest has no kind for a
  compiled binary.
- Seven modules of `creche-contracts` hold a doc comment and no type:
  `family`, `server`, `session`, `channel`, `grants`, `status` and
  `manifest`.
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
  `crates/creche-contracts/src/config/`:
  1. `LanAddress`. No contract gives the LAN address of the site file a
     grammar. One Python copy of six takes labels with dots, and five take
     each text. The type takes the strictest copy. It also refuses `0.0.0.0`
     and a text that ends in a number and is not one IPv4 address.
  2. `BindHost`, contract 02 §3 rule 2. The contract gives no grammar. The
     type takes an IP address or a host name, and refuses each spelling of
     each interface.
  3. `SocketPath`, `DirPath`, `TokenFilePath` and `FilePath`. No contract
     gives a config path a grammar. The types refuse a relative path and a
     NUL byte. `SocketPath` has the cap of 107 bytes.
  4. `Seconds`, contract 03 §11.4 rule 4. The contract does not say which
     numbers are permitted. The type refuses a value that is not finite and
     a value of less than 1 nanosecond.
  5. `HttpUrl`. No contract gives a config URL a grammar. The type demands
     `http://` or `https://` and a host, and refuses a user part.
  6. `attendance::ChannelCommand`, contract 03 §1. The contract gives one
     command and no grammar for another one. The type refuses a text that
     does not split into words.
  7. `caregiver::ImageRef`, contract 01 §3.9. The contract gives an image
     reference no grammar. The type demands a reference with a digest.
  8. `roster`, `stage7-releases.md` §4.4. The contract names no YAML
     version. The module holds no YAML reader.
  9. `caregiver::CaregiverConfig`, `spec.md` §5.4. The spec does not say
     what the caregiver does with no master key of LiteLLM. The type refuses
     `--write` without the key. The Python service starts.
  10. `chaperone::ChaperoneConfig`, contract 04 §10 rule 7. A generated
      roster with no base roster fails the verify hook. The contract does not
      say what the service does at start. The type reads no roster then, as
      the Python service does.
  11. `chaperone::ChaperoneConfig`. No contract gives the action at start for
      a config that is not valid. The type exits with `EX_CONFIG`. The other
      choice is `AtStart::RefuseEachCall`.
  12. `mounts::Credentials`, contract 03 §12 rule 3. The contract gives the
      epoch no range. The type holds 64 bits with a sign. It takes zero and a
      negative epoch.
  13. `mounts::Credentials`, contract 03 §12. The contract does not say what
      a reader does with a secret that is not a JSON string. The type refuses
      it, and an empty secret. The Python reader makes text of each value.
- No type reads the text of a roster file, and no type writes it. PyYAML
  reads YAML 1.1, and no Rust YAML reader is in the workspace. The owner of
  the crate selects one. `roster::RawRoster` then takes its tree.
- `config::mounts` defines `ModelAlias`, `SandboxTool` and `SystemPrompt`.
  The family file uses the same three. The owner of the crate moves them
  when the `family` module has its types.
- The config types follow the Python readers where a reader is lax against
  a contract. The owner decides each case. Three examples:
  1. `creds.json` with an `epoch` that is `true`, `7.9` or `"7"`.
  2. A roster row with an empty `command`, or with a name that is no server
     name.
  3. A `VIEW_COOKIE_SECURE` of `off`, which leaves the switch on.
- Sixteen types of `config` have a private field and no `compile_fail` doc
  test. The rule in "Tests" asks for one. The types are in four groups:
  1. A part of a daemon config: `attendance::OwuiCopy`, `caregiver::Images`,
     `chaperone::RosterFiles`, `chaperone::Doors` and `intake::PushHook`.
     Only the parse of that config makes one.
  2. A raw form, which checks nothing: `roster::RawRoster`,
     `roster::RawUpstream`, `roster::RawArgDeny` and
     `mounts::RawRuntimeConfig`.
  3. An error type: `roster::RosterIssue`, `roster::RosterErrors`,
     `site::SiteError`, `site::SiteErrors`, `mounts::RuntimeConfigErrors` and
     `mounts::PlaypenEnvErrors`.
  4. `FailureAction`. Its constructor is public and takes each pair.
- No vector covers five configs: the chaperone without its `site` readers,
  the caregiver, the two doors and the intake. No Python entry point takes
  their variables as a map. The tests of those types use a copy of the
  variables of each unit file. No test holds a copy equal to its unit file,
  except for the names of the variables.
- No vector covers a reader of the playpen. `mounts::RuntimeView` follows
  `playpen/src/runtime-config.ts`, and its tests are a copy of that file.
- The three mount files state their failure action only in a doc comment:
  `mounts::RuntimeView`, `mounts::Credentials` and `mounts::PlaypenEnv` do
  not implement `Checked`. `AtStart` and `AtReload` have no variant for their
  actions: a safe default for each field, a retry and then
  `stale_credentials`, and the fatal `mount_dir_unset`. The port of the
  playpen adds the variants.
- No unit file holds `RestartPreventExitStatus=78`, and no service exits
  with 78 for each config error. The failure action of each config type
  states what the port of its service must do.
- No test runs systemd. The rule about `RestartPreventExitStatus` and
  `ExecStartPre=` comes from the manual page `systemd.service(5)`. No run on
  a host proves it.
