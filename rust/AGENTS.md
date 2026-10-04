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
| `agent-family` | The validator of the family file and of the server file, the registry loader and the `agent-family` program. `crates/agent-family/AGENTS.md` holds its rules. |

| Module of `creche-contracts` | What it holds |
|---|---|
| `ids` | Each id grammar that `vectors/data/ids` covers. One type for each grammar. |
| `secret` | `Secret`, the type of a token or a key. |
| `family` | The family file: contract 01. |
| `server` | The MCP server file: contract 01b. |
| `session` | The session API: contract 02. `session.rs` declares the files under `session/`. |
| `channel` | The channel protocol: contract 03. "The channel module" below has its parts. |
| `grants` | The grant file, the call body, the approval body, the audit record and the words of a decision: contract 04. |
| `status` | The status document, the fault files and one view for each reader: contract 05. |
| `manifest` | The component manifest and the release request: contract 06. |
| `config` | The config of each process: the site file, the environment of each daemon, the roster and the mount files. "The config of a process" below holds its rules. |
| `vectors` | Test code only. It reads the vector files under `vectors/data/`. |

`src/manifest.rs` holds the closed sets, the catalog and the differential
test of `manifest`. Its other files are in `src/manifest/`. Three of them are
private readers and writers. Each one gives what a Python library of the
release tool gives:

| File of `manifest` | What it holds |
|---|---|
| `component.rs` | `component.yaml`: `ComponentManifest` and the operator account. |
| `request.rs` | The release request: one writer, one parser and the mint of an id. |
| `state.rs` | The live-state document. |
| `resolved.rs` | The resolved manifest, its hash, the gate id and the approval summary. |
| `yaml.rs` | Private. A port of `yaml.safe_load` of PyYAML: YAML 1.1, with each tag and each anchor. |
| `json.rs` | Private. A reader that takes what Python `json.loads` takes, and writers for the text of `json.dumps`. |
| `sha256.rs` | Private. SHA-256, because the crate has no dependency that gives a hash. |

No type of `manifest` implements a `serde` trait. The module is one of the
four exceptions that rule 1 names. The raw type of `manifest` is the private
value tree of its `yaml` reader or of its `json` reader. `Draft` is the raw
type of a request that a requester plans. The reason is the reason of
`channel`: no `serde` reader reads what `yaml.safe_load` and `json.loads`
read.

## Where a new type goes

1. Put the types of one contract in the module of that contract. The table
   above names each module.
2. Change only the file of your module. `lib.rs` declares each module. A
   module with more than one file keeps the other files in a directory with
   its name. `grants.rs` and `grants/` are the pattern.
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
   Exception: the modules `channel`, `grants`, `manifest` and `status` use
   no raw `serde` type. Each one reads a document with a reader of its own.
   "Layout", "The channel module", "The JSON reader of `status`" and "Known
   gaps" give the reasons. The owner decides if the exception of `grants`
   stays.
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

The `session` module has three more exceptions. The owner did not decide
them yet. Each one is a row of `DEVIATIONS` in `session/python.rs`, and
"Known gaps" lists them.

- A JSON text that is not strict JSON in UTF-8, for example a text with a
  byte order mark, with `NaN` or with a lone surrogate.
- A JSON text that nests deeper than 128 levels.
- A sequence number that does not fit 64 bits.

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

Rule 1 names a raw `serde` type. The `channel` module is one of four
exceptions.
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

## A reader that accepts more than the contract

A Python reader can accept a document that the contract does not permit. The
module `status` shows what to do:

1. The raw type reads each document. It checks no field.
2. The valid type refuses each field that the contract does not permit. A
   writer uses the valid type.
3. One view for each Python reader takes from the raw type what that reader
   takes. The port of a reader uses its view, so the port keeps the behavior
   of the reader.
4. The differential test holds each view equal to its reader. Where two
   readers differ, a table in the test names the document, the contract
   section and what each reader takes.

Reason: a port that refuses a document that the Python reader accepts changes
what runs on the host. The owner decides each such change. A view keeps the
change out of the port.

## The JSON reader of `status`

`status` does not read a file with `serde_json`. `status::json` holds a
reader and a writer of its own, and the raw type of `status` has no `serde`
derive.

Reason: `vectors/data/status` holds four forms of a JSON text that each
Python reader accepts and `serde_json` refuses.

1. The words `NaN`, `Infinity` and `-Infinity`.
2. An integer of more than 64 bits.
3. A key that an object holds two times.
4. A nesting of more than 128 levels.

The writer gives the bytes of `json.dumps` of Python. `serde_json` writes a
float and a character that is not ASCII in another form.

Rule 1 holds in each other part. The reader makes the raw type first, and a
conversion that can fail makes the valid type. Rule 7 holds too. Only the
detail of a fault holds a `status::json::Json`, and contract 05 §3.3 makes
that value opaque.

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
- `serde_json` has the feature `float_roundtrip`. It then reads each JSON
  float as the nearest float, as Python does. Without the feature, a float of
  16 digits or more can differ from the Python value in its last bit. Do not
  remove the feature.

## Known gaps

- CI does not run `cargo deny`. No check reads the advisories or the
  licenses of the locked crates.
- No release uses Rust code.
- `family` and `server` use the id types of `ids`. They refuse three texts
  that the Python package `agent_family` accepts. Each vector with such a
  text is a row of `DEVIATIONS` in `crates/agent-family/tests/vectors.rs`.
  1. A tool name of more than 64 bytes.
  2. The name of an environment variable of more than 64 bytes.
  3. A package version with `+`, with `-` or of more than 64 bytes.
- `crates/agent-family/AGENTS.md` lists the `CONTRACT-QUESTION` comments
  and the known gaps of the family file and of the server file.
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
  5. `OwuiChatId::session_id`, contract 02 §2. The Python door makes a
     session id of 133 bytes from a chat id of 128 bytes. The function
     refuses to make that session id.
  6. `Version`, `ContractVersion` and `Tag`, contract 06 §2 and §3. The
     contract gives no cap on the digits of a number. Python reads a text of
     4300 digits at most as an integer. The types have that cap.
- These `CONTRACT-QUESTION` comments are open in
  `crates/creche-contracts/src/grants/`:
  1. `json.rs`, contract 04. The contract gives no cap on the nesting of a
     grant file or of a request body. The Python reader stops at a limit of
     its interpreter. The Rust reader stops at 256 levels. The Python
     chaperone accepts a call whose arguments nest deeper, and the Rust
     reader refuses it. Four rows of `DEVIATIONS` hold the vectors.
  2. `json.rs`, contract 04. The contract does not say which characters a
     string holds. The Python reader keeps a lone surrogate. The Rust
     reader refuses the document.
  3. `BodyError`, contract 04 §5. The contract has no row for a request body
     that the chaperone cannot read. The type gives the three statuses of the
     Python chaperone: 413, 400 and 422.
  4. `AuditOutcome::Pending`, contract 04 §6.4. The contract names no reason
     for the first record of a gated call. The type writes
     `approval_required`, as the Python chaperone does.
  5. `UnidentifiedRecord`, contract 04 §6. The contract does not describe the
     log of a request that names no family. The type writes what the Python
     chaperone writes.
  6. `SandboxEvidence::Claimed`, contract 04 §3.2 and §6.2. The contract moves
     a sandbox id with no proof into `claimed`, and `claimed` has no key for
     it. The variant writes the id into `sandbox_id` with
     `sandbox_id_trusted: false`, as the writer of the Python chaperone can.
     The Python chaperone itself writes `null` in each record.
- The module `grants` has its own JSON reader and writer, and `serde_json`
  does not read a grant file or a request body. The Python code takes JSON
  that is not strict, and it reports each issue of a document. `grants::Value`
  is the document. It has no `Deserialize`.
- The crate has four JSON readers that do what `json.loads` of Python does:
  `channel::json`, `grants::json`, `status::json` and `manifest::json`. They
  differ in two decisions. The reader of `channel` stops at 9000 levels, and
  the readers of `grants` and of `status` stop at 256 levels. The reader of
  `manifest` has no nesting limit and uses no recursion. The reader of
  `channel` keeps a lone surrogate in a text, and the readers of `grants` and
  of `status` refuse the document. The reader of `manifest` writes U+FFFD.
  The owner of the crate decides if one reader replaces the four.
- The types of `grants` accept what the Python code accepts, also where a
  stricter reading of contract 04 is possible. The owner decides each case.
  Four examples:
  1. A limit of a grant file as `true`, as a float or as a text.
  2. A fence key that its verb does not read.
  3. `NaN` and an integer past 64 bits in the arguments of a call.
  4. A request body in UTF-16 or in UTF-32.
- `grants::AuditRecord` has public fields and holds no rule between two
  fields. Contract 04 §6.4 has one: the two records of a gated call name the
  same gate. The writer of the port holds that rule. The vectors of
  `chaperone.audit_line` hold records that break the rule, because the Python
  writer checks no field.
- `GrantFile::to_bytes` refuses a file of more than 256 KiB (contract 04
  §1.2). The Python caregiver writes such a file, and the Python chaperone
  then refuses it. No vector holds such a file.
- `GrantFile::rotated` and `GrantFile::same_grants` have no differential
  test. They stand for `rewrite_digests` and `grant_file_matches` of the
  Python caregiver, and no vector records those two functions. The Python
  functions read the JSON of a file and check no field. So `same_grants`
  differs: a file with no `limits` block is equal to a file with the three
  defaults.
- `grants::Allowed` and `grants::Held` are a sketch. No code builds a value.
  The port of the chaperone adds the decision function and the function that
  approves a held call. No other code builds a value.
- The crate has no public SHA-256. The chaperone gives `grants::ArgsDigest`
  the digest of `Arguments::digest_input`. The test has a SHA-256 of its own.
- No vector covers a request body with a content type that is not JSON. The
  HTTP layer of the port holds that rule.
- These `CONTRACT-QUESTION` comments are open in
  `crates/creche-contracts/src/manifest.rs` and in the files of
  `crates/creche-contracts/src/manifest/`:
  1. `Kind`, contract 06 §8. The contract lists four kinds. The Python code
     has `binary` as the fifth, and the type has the five kinds.
  2. The YAML reader, contract 06 §8 and §10. The contract gives no limit
     for the nesting. The reader refuses a text past 128 levels. PyYAML
     has no such limit.
  3. The YAML reader, `!!binary`. Two supported Python versions differ on
     base64 data after a pad. The reader takes the rule of Python 3.13. No
     field of a manifest takes bytes, so only the detail of a refusal
     changes.
  4. `Argv` and `Install`, contract 06 §4 and §8. The Python reader accepts
     a word and an install path with a lone surrogate. The Rust reader
     refuses them.
  5. `Request`, `stage7-releases.md` §2.3 and contract 06 §9. The first
     calls the id a lower-case ULID and the second calls it upper case. The
     Python code takes upper case, and the type is `ids::Ulid`.
  6. `Requester`, `stage7-releases.md` §2.3 and contract 06 §9. The two
     texts list different words. The Python code checks only the grammar of
     a name, and the type does the same.
  7. `ResolvedAt`, contract 06 §9. The Python builder takes each float. The
     type refuses NaN and an infinity, which JSON cannot hold.
  8. The YAML reader, contract 06 §8 and §10. The contract gives no limit
     for a merge key. The reader refuses a document whose merge keys copy
     more than 65,536 pairs.
  9. The JSON reader, `stage7-releases.md` §3.2 and contract 06 §11. Python
     keeps a lone surrogate escape as one code point. The reader writes
     U+FFFD. The detail of a refusal can then differ from the Python
     detail. The result is a refusal in both.
  10. The YAML reader, contract 06 §8 and §10. The contract gives no limit
      for a chain of merge keys. An alias makes such a chain with no
      nesting. The reader refuses a chain past 128 levels.
  11. The YAML reader, contract 06 §8. Python reads a decimal digit and a
      space that are not ASCII in a number with a tag: `!!int "\u0664"` is 4.
      The reader refuses such a scalar, as rule 9 says for an id.
  12. `mint_ulid`, contract 02 §2. For a time past 48 bits of milliseconds,
      the Python requester mints 26 characters that hold more than 48 bits
      of time. The function refuses that time. `ids::Ulid` accepts the text
      of the Python requester.
  13. `ComponentManifest::parse`, contract 06 §4 and §8. The Python reader
      reads the account name and the home of the operator apart, each when
      a manifest needs it. `Operator` holds both values or none. With a
      site file that gives one value, the Rust reader refuses a manifest
      that needs only that value. The Python reader accepts it. No vector
      holds that case.
- The types of `manifest` accept what the Python code accepts, also where
  a stricter reading of a contract is possible. The owner decides each
  case. The pull request of the module lists them.
- `manifest::Operator` holds two values of the site file. `config::site`
  holds the site file and has the types `OperatorUser` and `OperatorHome`.
  `ComponentManifest::parse` does not use them yet. A change to those two
  types also answers question 13 above.
- No other module uses the private `yaml` and `json` readers of `manifest`.
  The `family` module needs the same YAML reader. The owner of the crate
  moves that reader when a second module uses it.
- `manifest` has its own SHA-256, which is private. A crate for the hash
  replaces it when the workspace takes one.
- These `CONTRACT-QUESTION` comments are open in
  `crates/creche-contracts/src/session/`:
  1. The JSON reader, contract 02 §3 rule 3. The contract gives a body no
     nesting limit. Python reads a text until the recursion limit of the
     interpreter. The reader stops at 128 levels.
  2. `Timestamp`, contract 02 §13.2 rule 4 and §13.4.2. The contract says
     RFC 3339. Python reads more forms: a week date, a date with no time,
     one character between the time and the offset, and digits after
     `HHMMSS`. The type reads each form that each supported Python version
     reads in the same way. It refuses a form that two versions read in
     different ways.
  3. `SteerMessage`, contract 02 §5.6. The contract gives no cap. The Python
     parser has a cap of 4096 bytes. The type has that cap.
  4. `StopReason`, contract 02 §5.7. The contract gives no cap and no
     grammar. The Python parser has a cap of 200 characters and takes each
     text. The type does the same.
  5. `Labels`, contract 02 §4.2. The contract gives a key no cap and no
     grammar. The Python parser takes each key. The type does the same.
  6. `Holder`, contract 02 §7.1. The contract lists four holders. The Python
     code has a fifth, `dispatch`. The type has the five.
  7. `TurnRef`, contract 02 §5.5 and §8. The contract says that a `turn` is
     a ULID. The Python reader of a journal line and the events route take
     each text. The type takes each text and gives a text that is not a ULID
     no id.
  8. `ErrorDetail`, contract 02 §14. The contract gives no closed set of
     forms. The type has the holder block of §7.2 and keeps each other
     object.
  9. `OwuiRefs`, contract 02 §5.4 and §10. The contract gives the four ids
     no grammar and no cap. The type takes each text of one character or
     more.
  10. `Trigger`, contract 02 §13.2. The contract gives the name no grammar.
      The type takes each text.
  11. `DispatchRequest`, contract 02 §13.4.1. The contract says that `chain`
      is required. The Python parser takes a body with no chain. The type
      does the same.
  12. `SwitchRequest`, contract 05 §5.1. The Python parser checks no grammar
      for `family`, `to` and `from`. The type keeps each one as text.
  13. `EventsQuery`, contract 02 §5.5. The contract says that `follow` is a
      bool. The Python route reads each text but `0`, `false` and `no` as
      true. The type does the same.
  14. `TurnView`, contract 02 §4.4. The Python code writes the empty text
      as the sandbox of a turn that no sandbox served. The type does the
      same.
- The `session` module differs from the Python code on purpose in four
  ways. Each one is a row of `DEVIATIONS` in `session/python.rs`.
  1. A JSON text is UTF-8 with no byte order mark. It holds no `NaN` and
     no `Infinity`. It holds no lone surrogate in a key or in a text that a
     parser reads, and no bytes of a surrogate. The Python reader accepts a
     lone surrogate in each place.
  2. A JSON text nests 128 levels at most.
  3. A number of a query has the digits 0 to 9 only.
  4. A sequence number fits 64 bits. The body of a `pi_event` holds an
     integer past 64 bits as a float.

  Three more rows are vectors of the journal reader on which the two sides
  accept the same line. The Rust reader keeps `journal_seq: true` as the
  number 1, and it keeps a body as its JSON text.
- The `session` module reads a JSON text with `serde_json`. The readers of
  `channel`, `grants` and `status` do what `json.loads` of Python does, and
  the reader of `session` is strict JSON. Deviation 1 and deviation 2 are
  the result. The owner of the crate decides if one reader replaces them.
- The writer of the `session` module sorts the keys of an object of free
  form. The Python code keeps the order of its input. No vector shows the
  difference. These objects have free form:
  1. The labels.
  2. The body of a `pi_event`.
  3. A note that `attendance` does not write.
  4. An object inside an error detail.
- The writer keeps the order of the members of an error detail, as the
  Python code does. `session::ErrorDetail::from_members` takes the members
  in order. The vectors of `session.error_body` hold each detail of
  `attendance` whose keys are not in sorted order.
- `session::StoredLine` keeps the body of a line as the text of the file. A
  body that the Python code did not write goes out with that text: its
  white space, its number forms and each duplicate key. The Python code
  parses the body and writes it again.
- A typed body of free form reads the integer `-0` as the float `-0.0` and
  writes `-0.0`. The Python code writes `0`. The Python code never writes
  `-0` into a journal.
- `session::JournalBody::read` has no Python counterpart. No Python code
  reads the body of a journal line against its kind. The function refuses a
  body that lacks a field of its kind.
- The `session` module has no type for these parts of contract 02, because
  no vector covers them:
  1. The bodies of `wait=accepted` and `wait=settled`.
  2. The answer of a session list.
  3. The answers of `/dispatch` and `/dispatch/jobs`.
  4. The header `X-Door-Instance`.
  5. The state files of `attendance`.
- A request type of the `session` module has no writer. A door needs one to
  send a body. The three Python doors write their bodies by hand, and no
  vector covers them.
- `session::Turn` has no constructor from a stored turn record.
- `session::JournalLine::new` does not check the turn against the kind of
  the body. A `turn_queued` line with no turn is a value of the type. The
  Python writer has no such check.
- The body types of a journal line, for example `session::TurnStarted`, and
  `session::ServiceNote` have public fields. Some fields are a plain
  `String`: the contract gives them no grammar. Code can build such a body
  with each text.
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
- These `CONTRACT-QUESTION` comments are open under
  `crates/creche-contracts/src/status/`:
  1. `json::DEPTH_MAX`, contract 05 §2. The contract gives no cap on the
     nesting of a file. The reader stops at 256 levels. The Python reader
     stops at a depth that depends on the interpreter. A value of 256 levels
     needs less than 384 KiB of stack.
  2. `Json::parse_bytes`, contract 05 §2. The contract does not name the
     encoding of a file. The reader takes UTF-8. The Python reader of the
     noticeboard also takes UTF-16 and UTF-32.
  3. `JsonError::LoneSurrogate`, contract 05 §2. The Python reader keeps an
     escape of one half of a surrogate pair. The reader refuses the file.
  4. `time::Timestamp`, contract 05 §2.1. The contract names RFC 3339. The
     Python readers take each text that `datetime.fromisoformat` takes, and
     three of them read a time with no offset as UTC. The type takes RFC 3339
     with an offset.
  5. `document::HostPath`, contract 05 §3.2, §4.1.1 and §6.4. The contract
     gives no grammar for a host path. The type takes an absolute path with
     no control character.
  6. `Sandbox::supervisor_env`, contract 05 §4.1.1. `caregiver` writes an
     empty path for a row of its ledger that holds no path, in each
     lifecycle state. The type takes it in each lifecycle state.
  7. `Credentials::epoch`, contract 05 §6.1. The contract gives no range.
     The type refuses 0. The Python reader of `attendance` takes 0.
  8. `Fault::new`, contract 05 §3.3. `caregiver` writes `blocks_turns: false`
     for `sandbox_start_failed` when another sandbox serves. The type takes
     that one difference from the table.
  9. `DocumentParts::kind`, contract 05 §2.1. `caregiver` writes an empty
     kind for a family with no valid revision. The type takes it.
  10. `DocumentParts::faults`, contract 05 §2.1. `caregiver` writes a fault
      for a family in the state `invalid`. The type takes a fault in each
      state.
  11. `DocumentParts::credentials`, contract 05 §2.1. `caregiver` writes
      `null` for a family with no credentials. The type takes it.
  12. `fault_file::FAULT_FILE_CAP_BYTES`, contract 05 §3.3.1. The contract
      gives no size cap. The reader has a cap of 1 MiB.
  13. `ReconcileStep::WriteTimers`, contract 05 §3.4. The contract names
      eight steps. `caregiver` also writes the step `write_timers`. The type
      takes it.
- The valid status document takes these forms. Contract 05 excludes each
  one. The owner decides each case:
  1. An empty `supervisor_env` for a sandbox in the state `ready` (§4.1.1
     rule 4).
  2. An empty kind with `never_valid: false`, or with a sandbox in
     `sandboxes` (§2.1 and §3.1).
  3. A fault in the state `in_sync` or `reconciling` (§2.1).
  4. The state `degraded` with no fault (§3).
- Contract 05 gives no grammar for these texts of the status document. Each
  one is a `String`: `registry_rev`, `applied_rev`, `config_rev`,
  `validation.rev`, `key_id`, `token_id`, `image`, `spec_hash`, `memory` and
  the URL of the `pep` block.
- The valid status document keeps no key that contract 05 does not name. A
  document from a newer writer loses such a key when a program writes it
  again.
- Each view of `status::views` takes what its Python reader takes, and the
  five Python readers do not agree. The table `DISAGREEMENTS` in
  `status/python.rs` names each class of such documents, with one or two
  documents of the class. The owner decides which reading each port keeps.
- A view of `status` does not refuse a document for these three values. It
  reads a time with no UTC offset as no time. It reads a time outside the
  years 1 to 9999 as no time. It reads a number above the range of a float as
  no number. The document is then stale, or it shows no spend. A unit test
  covers each value, and no vector holds one.
- `status::outcome` holds the view of the noticeboard and no valid type.
  Contract 02 §13.1 owns the outcome record and its writer.
- `status` holds no code for `rescope_by_fleet` and `drop_superseded` of
  `caregiver.faults`. They are rules of the reconciler, not of a file.
- No vector covers the size cap of a reader of contract 05. A unit test
  covers each cap.
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
