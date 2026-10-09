# rust

Rules for all Rust code in this repository. The root `AGENTS.md` applies here
too.

The Rust code uses type-driven design. A type permits only valid values. Code
that holds a value does not check the value again. The compiler then refuses a
defect that a test finds late.

## Layout

| Path | What it holds |
|---|---|
| `Cargo.toml` | The workspace, the dependency versions, the lint gate and the two build profiles. |
| `Cargo.lock` | The locked versions. Commit it. Each gate command uses `--locked`. |
| `rust-toolchain.toml` | The one toolchain version. rustup reads it for each cargo command under `rust/`. |
| `rustfmt.toml` | The line width: 100, the same as ruff. |
| `clippy.toml` | The lints that a test can break. |
| `deny.toml` | The policy for the locked crates: the licenses, the sources, the bans and the advisories. `cargo deny` reads it. |
| `crates/<name>/` | One crate. Each directory there is a workspace member. |

| Crate | What it holds |
|---|---|
| `creche-util` | Each shared helper: SHA-256, the hex text of bytes and the white space rules of Python `str`. It has no dependency. `crates/creche-util/AGENTS.md` holds its rules. |
| `creche-contracts` | The wire types and the config types of the contracts. `ids::FamilyName` is the pattern for each new type. |
| `agent-family` | The validator of the family file and of the server file, the registry loader and the `agent-family` program. `crates/agent-family/AGENTS.md` holds its rules. |
| `creche-runtime` | The runtime that each Rust service shares: file writes, token files, the log, tasks, signals, child programs and HTTP. `crates/creche-runtime/AGENTS.md` holds its rules. |
| `creche-testkit` | Test helpers for each crate. No release holds it. `crates/creche-testkit/AGENTS.md` holds its rules. |

| Module of `creche-contracts` | What it holds |
|---|---|
| `ids` | Each id grammar that `vectors/data/ids` covers. One type for each grammar. |
| `secret` | `Secret`, the type of a token or a key. |
| `slot` | `Slot` is the one lenient field type for a raw type: a value of a wrong kind does not fail the read. `MapOnly` wraps a nested table in a raw type whose read can fail: it refuses a value that is not a table. |
| `time` | `Timestamp`, the one type of a time in a file or in a wire message. "Time" below holds its rules. |
| `family` | The family file: contract 01. |
| `server` | The MCP server file: contract 01b. |
| `session` | The session API: contract 02. `session.rs` declares the files under `session/`. |
| `channel` | The channel protocol: contract 03. "The channel module" below has its parts. |
| `grants` | The grant file, the call body, the approval body, the audit record and the words of a decision: contract 04. |
| `status` | The status document, the fault files and one view for each reader: contract 05. |
| `manifest` | The component manifest and the release request: contract 06. |
| `config` | The config of each process: the site file, the environment of each daemon, the roster and the mount files. "The config of a process" below holds its rules. |
| `untrusted` | Readers for an answer of another service. A field of a wrong type reads as empty. The raw type of an answer uses them. |
| `vectors` | Test code only. It reads the vector files under `vectors/data/`. |

`src/manifest.rs` holds the closed sets, the catalog and the differential
test of `manifest`. Its other files are in `src/manifest/`. Two of them are
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

No type of `manifest` implements a `serde` trait. The module does not hold
rule 1 yet ("Known gaps"). The raw type of `manifest` is the private
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

## Where a helper goes

A shared helper is a pure function with two users. The users are two crates,
or two modules that hold two contracts. `creche-util` holds each shared
helper. Today it holds SHA-256, the hex text of bytes and the white space
rules of Python `str`.

1. Put a shared helper in `creche-util`.
2. Write no second copy of a helper that `creche-util` holds. Call the
   function of that crate.
3. Keep a helper with one user in the module of that user.
4. Keep a step that adds a prefix to a result, or cuts it, in the caller.
5. Read `crates/creche-util/AGENTS.md` before you add a helper. It holds the
   six rules for what the crate takes.

Reason: two copies of a helper drift, as two copies of a value do (rule 13).
The revision of a registry and the id of an approval gate each come from a
digest. A copy that drifts gives another revision and another gate id.

## Checks

You need rustup. It installs the toolchain at the first cargo command under
`rust/`.

1. Run each cargo command from `rust/`. rustup finds the toolchain file only
   from there.
2. Run `bin/rust-gate.sh --tests` before you push. CI runs the same script.

`bin/rust-gate.sh` runs these steps in this order:

1. The `[lints]` check. Each crate must take the lint gate.
2. Three text checks. Each one reads the source text and runs no cargo
   command.
   - The include check. No Rust source file includes a Markdown file.
   - The panic check. The word `catch_unwind` is only in the three
     places of clause 8 of "The panic rule".
   - The public-field check. No field of a struct has `pub`. Rule 12
     names the two forms that pass.
3. `cargo fmt --all --check`.
4. `cargo clippy --workspace --all-targets --locked -- -D warnings`.
5. `cargo deny --locked check`, where `cargo-deny` is on `PATH`.
6. `cargo test --workspace --locked`, with `--tests` only.

The panic check and the public-field check read each `.rs` file under
`crates/`. Both checks use one definition of test code:

- A file below `crates/<name>/tests/` is test code. cargo builds the files
  of that directory as test targets.
- A directory `tests` in another place does not count. `crates/tests` is a
  crate, and `crates/<name>/src/tests/` is a module of its crate.
- In each other file, test code is each module with a body that has the
  line `#[cfg(test)]` directly above its first line.
- Such a module ends at the first line that starts with its `}`, at the
  indent of its first line. Only a comment can follow the `}` on that
  line. `cargo fmt` writes a module in that form. Each check reads the
  code after that line again.
- Each check fails for a file that ends inside a test module. The check
  found no last line of that module, so it read no code below the first
  line.
- A `#[cfg(test)]` line above another item starts no test code, for
  example above `mod python;`. Each check reads that item and the code
  after it. Each check also reads the file `python.rs` of that module,
  unless the file is below `crates/<name>/tests/`.
- A string of more than one line can hold a line that has the form of the
  last line of its test module. Each check then reads the rest of that
  module as code that is not test code. Give such a line an indent in the
  string.
- Each check reads a line that ends with CR LF as a line that ends with
  LF.

More rules of the panic check:

- The check prints one line for each file with the word in a fourth
  place, and fails.
- The word in a comment counts too.
- Place 2 of clause 8 is `src/entry.rs` in a crate with no dependency on
  `creche-runtime`. For the check, a crate has that dependency when its
  `Cargo.toml` holds the name. A comment that holds the name counts too.

More rules of the public-field check:

- The check reads the field list of each struct. A tuple struct has one
  too. The check prints one line for each field with `pub`, and fails.
- Only `pub(crate)` and `pub(super)` pass. The check refuses each other
  form of `pub` on a field, for example `pub(in crate::wire)`.
- The check reads no test code.
- The check also fails when the scan cannot read a file, and when it does
  not find the end of a struct.
- The script has a list of the crates that the check does not read yet.
  "Known gaps" has the names.

Step 5 needs the program `cargo-deny`. rustup does not install it.

- Without `cargo-deny` on `PATH`, the script prints one line and runs each
  other step. CI runs step 5 for the same change.
- In CI, the script fails without `cargo-deny`. The `rust` job installs it
  before the script runs. `.github/workflows/gate.yml` names the version.
- To run step 5 on your machine, install that version of `cargo-deny`.
- Step 5 reads the advisory database from the network.

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
   This rule has no exception for a module. "JSON" below has the rules for
   a JSON text. Such a text takes three steps:
   1. The strict reader in the `json` module of `creche-contracts` reads
      the text.
   2. The reader fills a raw `serde` type whose fields are lenient. A
      lenient field takes a JSON value of each kind. A wrong kind is then
      an issue for step 3 and not a failed read.
   3. A single conversion builds the valid type.
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
12. **Give no struct a public field.** Code outside the crate gets a value
    through an accessor method. A field with `pub(crate)` or `pub(super)` is
    not a public field.
    Reason: other code can write a public field directly, so no constructor
    checks that value.
    - For a record or a view, write an accessor for each field. Write `new`
      with a parameter for each required field. Write a `with_<field>`
      method for each optional field.
    - When only a reader function builds a view, write the accessors and no
      public constructor.
    - A raw form for `serde` gets its values from `Deserialize` alone. Write
      no public constructor for it.
    - For an error struct, write the accessors and no public constructor.
    - Two fields can state one fact together. Set such a pair through a
      single method.
    - The rule does not apply to the fields of an enum variant.
13. **Keep one source for each value.** Define a constant, a limit, a name
    or a grammar in one module only. Code in other places imports it from
    that module. When the host already has a value, do not ask for it in
    the family file.
    Reason: two copies of a value drift. An edit changes one copy, and the
    other copy stays.

## JSON

A JSON text of a contract must be strict JSON. The reader refuses a text
that does not meet each line of this list:

- The encoding is UTF-8. The text starts with no byte order mark.
- `NaN`, `Infinity` and `-Infinity` are not valid tokens.
- A string has no lone surrogate. This applies to a channel line too.
- An object has each key one time only.
- The nesting depth is 64 levels or less.
- Each integer token is in the range of 64 bits.
- Each float token is a finite number.

Reason: for a text outside these rules, two readers can return two values.

`creche-contracts` must have one JSON reader and one JSON writer, in its
`json` module. Packets `decisions-json-check` and `decisions-json-reader`
add that module. Write no JSON parser, no JSON value tree and no code that
formats a float in another module.

When the reader refuses a text, apply the failure action that rule 8
demands. The module error keeps the name of the broken rule. The service
uses that name to record a notice for the operator. Packet
`decisions-notice` adds the notice.

## Time

A time in a file or in a wire message is a `date-time` of RFC 3339,
section 5.6. A writer writes each time in UTC, with `Z` as the offset. A
text with no UTC offset is not a time. The root `AGENTS.md` holds the same
rule for each language.

`creche_contracts::time::Timestamp` is the one type for such a time. It
holds one instant in UTC, to the microsecond, in the years 0001 to 9999.

- Read a time text only with the `FromStr` of `Timestamp`.
- Write a time only with a writer of `Timestamp`. `to_rfc3339` writes whole
  seconds, and `to_rfc3339_millis` writes milliseconds.
- Define no second type for a time text.
- Write no date arithmetic in another module. The `time` module holds the
  copy that stays.
- The caller of the reader decides what a refused text means. For example,
  a view reads the file as stale, and a request gets a refusal.

Reason: with two grammars, one text is a time for one reader and no time
for another reader. Two copies of the date arithmetic drift.

The reader takes these parts, in this sequence. It refuses each other
text.

1. The date, `YYYY-MM-DD`.
2. `T` or `t`.
3. The time of the day, `HH:MM:SS`.
4. An optional fraction: `.` and 1 to 9 digits. The type keeps the first
   six digits.
5. The offset: `Z`, `z`, `+HH:MM` or `-HH:MM`.

Thus the reader refuses a space in place of the `T`. It also refuses second
60 and the year 0000. "Known gaps" has the question about the limits of the
reader.

`to_rfc3339_millis_plus_00_00` is a third writer. It writes milliseconds
and the offset `+00:00`. The chaperone keeps a log of each request that
names no family, and the lines of that log have this offset today. Use the
writer for that log only. Delete the writer when that log writes `Z`.

`manifest::Timestamp` is a count of seconds and not a time text. This
section does not apply to it.

Six older parts of the workspace still hold a time type or date arithmetic
of their own. Add no user of them. One packet moves each part to
`Timestamp` or deletes the part. The pull request of that packet deletes
the row of the part. The pull request that deletes the last row also
deletes this paragraph and the table.

| Part | Packet |
|---|---|
| `status::time` | `decisions-time` |
| `AuditTime` of `grants` | `decisions-time` |
| `session::Timestamp` | `strict-attendance-time` |
| The stamp of a log line in `creche-runtime` | `decisions-runtime-time` |
| The `!!timestamp` value of the YAML reader of `agent-family` | `toml-drop-yaml-validator` |
| The `!!timestamp` check of `manifest::yaml` | `toml-drop-yaml-manifest` |

## When two Python copies of a grammar disagree

The Python code holds more than one copy of most id grammars.
`vectors/data/ids/disagreements.json` lists each input on which two copies of
one grammar give different results.

1. The Rust type takes the strictest copy. It refuses each input that one
   copy refuses.
2. Mark the type with a `CONTRACT-QUESTION` comment. The comment names each
   copy and what the copy does.
3. List the question under "Known gaps".
4. A surface in the test table of the type has the stance `equal`: the type
   and the copy give the same result for each vector. `stricter` is an old
   second stance. Do not use it for a new surface. Packet `decisions-ids`
   deletes it from `ids`.

Reason: a value passes more than one copy before the platform uses it. The
strictest copy is thus the grammar that holds on the host. A type that takes
a laxer copy accepts a value that a Python component refuses later.

When each Python copy accepts an input, the Rust type accepts it too. This
rule also applies when a stricter reading of the contract is possible. Name
such a case in the pull request. The owner decides it.

Rule 9 makes each type refuse a digit outside ASCII. Add no table row for
that refusal. When a Python copy accepts such a digit, follow "When the two
results differ".

## When two Python versions differ

The Python workspace runs under more than one Python version. For some
inputs, the result depends on the version.

1. Follow Python 3.13 in the Rust code.
2. Cover such an input with a plain Rust test. It cannot be a vector: rule 7
   of `vectors/AGENTS.md` demands the same output under each version.

Reason: a port can match one behavior only. A fixed version gives each
packet the same target.

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

Rule 1 names a raw `serde` type. The `channel` module does not hold rule 1
yet ("Known gaps").
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
| `endpoints` | The names of some variables that hold the address of another service. A daemon module reads such a name from this module. `PlaneUrl` is the URL of a plane. A plane is a service of the host that each sandbox calls: the chaperone and LiteLLM. |
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
- The `systemd-proof` job of CI proves both facts about the line on the
  systemd of a Linux runner. After the main process exits with 78, systemd
  does not start a unit that holds the line again. After a process of
  `ExecStartPre=` exits with 78, systemd starts the unit again.
- `bin/systemd-proof.sh` is that proof. It uses transient units of its own
  and starts no daemon. `bin/AGENTS.md` has its three cases.
- The proof runs for each code change that touches `systemd/`. It thus runs
  for the pull request that adds the line to a unit. It also gives each unit
  file to `systemd-analyze verify`.
- `AtReload::KeepLastGood` never exits. A reload that fails keeps the last
  good value.
- `config::reload` takes only a type that says `AtReload::KeepLastGood`. A
  call with a type that says `AtReload::NotRead` does not build.
  `cargo build` and `cargo test` report that error, and `cargo check` does
  not.
- `config::start` and `config::reload` take the error type of each parse.
  The roster and the site file have an error type of their own.
- A cutover release moves a component from its Python package to its Rust
  binary. In that release, the verify hook parses the env file and the site
  file on the host with the Rust config types. A failed hook makes the
  release restore the Python tree. The report of the hook lists each
  refused variable by name and holds no value. The check needs no manual
  command on the host.

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
  `SURFACES` names each `config.` surface. A later packet deletes its second
  table, `DEVIATIONS`. Do not add a row to that table.

## The rules for a service

A service crate makes a daemon or a command of the platform. It uses
`creche-runtime`. These rules apply to each service crate. Each rule has its
reason. `crates/creche-runtime/AGENTS.md` holds the rules for a change to the
runtime itself.

The compiler does not check what a dropped future leaves behind. In `tokio`,
a future that its caller drops stops at its next `await`. No code after that
`await` runs. A Python handler runs to its end after its client left. Rules 1
to 5 give a Rust service the Python behavior on purpose.

1. **One owner task holds each child program.** No other task reads the
   pipes of the child, writes to it, kills it or waits for it. Each other
   task asks the owner through a message.
   Reason: a child with two holders has no task that must stop it. A caller
   that goes away then leaves a child that no task waits for.
2. **Give each write that must complete to `Tasks::spawn_must_complete` or
   to `Tasks::spawn_blocking`.** Then await the `Completion`.
   Reason: the task runs to its end when its caller goes away. A dropped
   caller then cuts no line and no file in two.
3. **In each loop and in each stream, select on the `Shutdown` token.**
   Reason: the token is the one stop signal of a process. A loop that does
   not read it holds the process until systemd kills it.
4. **Start a child only through `command::spawn_piped` or a
   `CommandRunner`.** A service crate does not use
   `tokio::process::Command` directly.
   Reason: the two set `kill_on_drop` in one place. Without it, a child
   continues to run after its owner stopped.
5. **Do not write cleanup code after an `await` when a caller can drop the
   future.** Write the cleanup in the owner task.
   Reason: the code after such an `await` does not run when the caller goes
   away.
6. **Give each `Router` to `http::layers::edge`. Run each daemon loop
   through `Tasks::spawn_loop`.** "The panic rule" below has the full rule
   for a panic.
   Reason: a panic then ends one request or one pass, and the service
   continues. A router with no `edge` also loses its handler when a client
   leaves. No compiler check finds such a router.
7. **Do not block a thread of the runtime.** Run `fsync`, a call of
   `std::fs` and a call of `git` in `Tasks::spawn_blocking` or through a
   `CommandRunner`.
   Reason: a blocking call holds a thread of the runtime. Each task of that
   thread waits.
   Exception: the write of one line to stdout or to stderr, and the read of
   the random device.
8. **Do not turn `overflow-checks` off. For arithmetic on a number that
   another process gave, use a checked or a saturating call.**
   Reason: without the checks, a release build wraps the number and
   continues with a wrong value. With the checks, an overflow is a panic. A
   checked call makes it an error that the code handles.
9. **Use only a channel with a bound.** In its doc comment, say what a
   sender gets from a full channel.
   Reason: a channel with no bound grows without limit when its reader is
   slow.
10. **Set a total time limit on each call to another service.** Only a
    stream with `StreamLimit::UntilShutdown` has none.
    Reason: the limit of one phase does not end a call to a peer that sends
    one byte in each interval.
11. **Let `service::run` build the runtime. `main` returns the `ExitCode`
    of that call.** A service does not use `#[tokio::main]`.
    Reason: the code of `#[tokio::main]` calls `expect`. `service::run`
    drains the tracked tasks and gives the runtime a time limit at the stop.
12. **Call `Env::from_os` one time, at the start.** That call is the only
    read of the environment.
    Reason: the parse of the config is the one check of each variable. A
    later read gets a value that no parse checked.
13. **Lock a std `Mutex` only through `tasks::locked`. Between two
    statements, each value under a lock must be valid.**
    Reason: a panic poisons a lock. `lock().unwrap()` then stops each later
    request. `locked` takes the value, so the value must be valid after each
    statement.
14. **For a write to stdout or to stderr, use only `log::out_line`,
    `log::err_line` and the log macros.**
    Reason: a closed stream makes `println!` and `eprintln!` panic.
15. **State the `AtShutdown` of each `Command`. Select `Finish` when the
    step of the child must end whole.**
    Reason: `subprocess.run` of Python runs to its end at a stop of the
    service. A child that a stop kills in the middle of a step can leave
    state that the next start cannot use.
16. **Call `log::init` first in `main`.**
    Reason: the standard panic hook of Rust writes the message of a panic
    to stderr. That message can hold a part of a request or of a file. The
    hook of `log::init` writes only the place of the panic. `service::run`,
    `service::load` and `service::refuse_start` set the same hook. Without
    the call in `main`, the code before the first of the three runs with
    the standard hook.
17. **A daemon that refuses its start exits with `EX_CONFIG`, status 78.**
    This applies to each cause of the list below. A command that runs to
    its end can keep another status for a usage error, when no unit
    restarts it.
    Reason: a restart repairs none of these causes. Only status 78 keeps a
    unit with `RestartPreventExitStatus=78` stopped.
    - An invalid config.
    - A bad argument on the command line.
    - A token file or a key file that the daemon cannot use.
    - A target of a client that the daemon cannot use, for example a URL.
18. **Take each address of another service from the environment.** Here an
    address is also a URL, a host or a port. Write no default for one in
    the code. A fixed path, a count, an interval and a duration stay in the
    code. Make each one a named constant with one home.
    Reason: where another service listens is a fact of the host. A default
    in the code is a second copy of that fact. With a wrong copy, a service
    calls the wrong peer and no config error shows it.

The lint gate checks rule 2 in part: `Completion` is `must_use`, so the
build fails for a `Completion` that the code does not use. It checks rule 13
in part: `await_holding_lock` refuses a guard that the code holds across an
`await`. `service::run` holds rule 16 in part: it sets the hook before the
runtime starts. No check holds the other rules. The reviewer checks them.

## The panic rule

Each binary crate follows this rule. The lint gate reads source text only,
so a program that passes the gate can still panic. The clauses limit what
one panic can stop and what it can damage.

1. **Each profile keeps `panic = "unwind"` and keeps the overflow checks
   on.** The release profile sets `overflow-checks = true`. The dev profile
   has the checks by default. `bin/tests/test_rust_workspace.py` pins both
   profile tables.
   Reason: under `abort`, the first panic stops the program, and a boundary
   cannot catch it.
2. **`main` has three steps and no other code.** A service on
   `creche-runtime` gets the steps from rules 11, 12 and 16 of "The rules
   for a service".
   Reason: no boundary is around the code of `main`, and no library test
   runs it.
   1. Set the panic hook.
   2. Build `Env`, one time.
   3. Call the entry function of the library, and return its `ExitCode`.
3. **The panic hook logs a single `ERROR` line.** That line names the
   program and the place of the panic: file, line and column. The log
   never holds the panic message.
   Reason: a panic message can carry bytes of a request or of a file.
4. **Put a boundary that catches a panic around each unit of work.**
   Reason: one panic then ends one unit of work, and the program
   continues.
   - A request: the boundary is `http::layers::edge`.
   - A pass of a loop: the boundary is `Tasks::spawn_loop`.
   - A tracked task: the boundary is `Tasks::spawn_must_complete` or
     `Tasks::spawn_blocking`.
   - A program without the runtime: a unit of work is a step, a pass or a
     connection.
5. **After a panic, a boundary ends that unit of work and starts the next
   unit.** A boundary answers with a constant text. Do not continue a unit
   of work after its panic.
   Reason: a panic can leave the values of that unit of work in a wrong
   state.
6. **A caught panic must not leave a partial file or a lock that blocks the
   next unit of work.** Write a file with the atomic writer or in a
   must-complete task. Lock a std `Mutex` with `tasks::locked` only.
   Reason: the program continues after the catch, and later work uses the
   same files and locks.
7. **A panic outside the boundaries of clause 4 ends the program.** The
   entry function catches that panic and logs one `ERROR` line. The exit
   status is then 1. The exit status of a panic is never 78.
   Reason: status 78 means a config fault, and a panic is not one.
8. **Only three places can call `catch_unwind`.**
   Reason: the reviewer then knows where each boundary is.
   1. Code of the crate `creche-runtime`.
   2. `src/entry.rs` in a crate with no dependency on `creche-runtime`.
   3. Test code.
9. **A stack overflow and an abort are not in the scope of this rule.** No
   boundary can catch either one, and the program ends.

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
`clippy.toml`. It also pins the two `[profile]` tables of `Cargo.toml`. To
change an entry, change the pin in the same commit. Give the reason in the
commit message.

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
- Give each public struct that has a field a `compile_fail` doc test. Rule
  12 permits no public field. The test shows that code outside the module
  cannot build the struct from a raw value. Put a doc test that compiles
  beside it, with the same `use` line. A wrong path then cannot make the
  `compile_fail` test pass.
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
- The test passes only when the Rust result equals the result of each
  vector of each surface. Do not record a difference in a table. Give a
  surface no stance other than `equal`. Do not add a second, laxer type.
  "When the two results differ" below has the procedure.
- Do not compare against a count of vectors that the test holds. A change to
  a product package can add a vector with no change under `rust/`.
- `ids::tests::python` is the pattern.

#### When the two results differ

For one input, the Rust result can differ from the Python result. The
packet then stops at that input and resolves the difference. Four
resolutions exist, and each one has a letter as its name. Check them in this
order: (d), (b), (a), (c). Use the first one that fits.

- **Resolution (d): change the Rust code.** Use it when the contract shows
  that the Rust result is wrong. Use it also when a Rust change by itself
  makes the two results equal.
- **Resolution (b): align the Python copies.** Use it when the Python code
  has two copies of a grammar and the copies disagree. Change each copy to
  the strictest one, in a pull request of the Python package. Regenerate the
  vectors in that pull request.
- **Resolution (a): make the Python reader strict.** Change the Python
  reader in its own package, and regenerate the vectors. Put the Rust change
  into that same pull request. Use this resolution in each of these cases:
  1. The owner decided that the rule is strict.
  2. Python code of the platform writes the value.
  3. One file has two readers, and they must agree.
  4. A caller of the platform sends the value today.

**Resolution (c): remove the input from the surface.** It is the last
resolution, and it fits two cases only. Case 1: nothing in a contract or on
the platform can show the difference. No contract decides the input, no
platform writer makes it and no platform caller sends it. Case 2: a daemon
calls the Python reader at its start. The generator then no longer writes
the input on that surface, and a plain Rust test pins the Rust result with
the input inline. The pull request explains why (d), (b) and (a) do not fit.

**Do not add a refusal to a Python reader that a daemon calls at its
start.** Such a refusal can stop a service on a value that it accepted at
its previous start. So (b) and (a) are not for this reader. The Rust type
still refuses the value. The verify hook of the cutover release parses the
actual files on the host with the Rust types, so it finds such a value. A
failed hook makes the release restore the Python tree ("The config of a
process").

Some differences from the Python origin are in no vector. Describe such a
difference in the doc comment of the Rust function. Pin it with one plain
test.

## Dependencies

- Write each dependency version one time, in `[workspace.dependencies]`. A
  crate refers to it with `workspace = true`.
- Add a third-party crate only when the code needs it. Each crate adds code
  that no person here reviews.
- A crate has no `version` key. No number lives in a file. A version is a
  tag that CI allocates.
- Commit `Cargo.lock` with each change to a dependency.
- Turn the default features of a third-party crate off in
  `[workspace.dependencies]`. Name each feature that the code needs. A
  feature that no code needs adds crates to the lock file.
- `serde` and `serde_json` keep their default features. The default of each
  one is the feature `std` only, and it adds no crate.
- A service crate that needs one more feature names it in its own
  `Cargo.toml`, for example `json` of `axum`.
- The workspace has no TLS crate. The client of `creche-runtime` refuses an
  `https` URL.
- Build the program of a service with `cargo build -p <crate>`. A build of
  the whole workspace also builds `creche-testkit`. That build turns on the
  feature `test-util` of `tokio` for each crate.
- `serde_json` has the feature `float_roundtrip`. It then reads each JSON
  float as the nearest float, as Python does. Without the feature, a float of
  16 digits or more can differ from the Python value in its last bit. Do not
  remove the feature.

### The check of the locked crates

`deny.toml` is the policy for the crates of `Cargo.lock`.
`cargo deny --locked check` makes four checks against it:

| Check | What fails |
|---|---|
| `licenses` | A crate that needs a license outside this list: `MIT`, `Apache-2.0`, `BSD-3-Clause`, `Unicode-3.0`. |
| `sources` | A crate from a registry that is not crates.io. A crate from a git repository. |
| `bans` | Two versions of one crate. A dependency with the version `*`. |
| `advisories` | A crate with a vulnerability advisory or with an `unmaintained` advisory. A direct dependency with an `unsound` advisory. A version that its author removed from crates.io. |

- `bin/tests/test_rust_workspace.py` pins each table of `deny.toml`, entry
  for entry.
- To add a license or a source, change that pin in the same commit. Give
  the reason in the commit message.
- The same rule applies to each other entry, for example an advisory that
  the check ignores.
- The checks read each crate that a build for one of four targets can use:
  Linux with glibc and macOS, each on x86-64 and on arm64. `deny.toml` lists
  the targets.
- A crate that only a test uses is in the `licenses` check and in the count
  of versions. Two keys of `deny.toml` do this: `include-dev` and
  `multiple-versions-include-dev`. Without its key, each of the two checks
  skips such a crate.
- A crate of this workspace names another one by its path, with no version.
  `deny.toml` permits that only for a crate with `publish = false`.
- The `advisories` check reads the advisory database from the network at
  each run. For a version that its author removed, it reads only the copy
  of the crates.io index that cargo keeps on the machine. "Known gaps" has
  the limits of that copy.
- The check does not read the code of a crate. A crate that passes is not a
  crate that a person here reviewed.

## Known gaps

- Rules that the code does not hold yet. A line names each packet that
  changes the code for its rule. Delete a line in the pull request that
  makes the code hold the rule, and not before. Some lines name no packet
  for a part of the work. No packet has that part yet.
  - Rule 1. Four modules have a JSON reader of their own and no raw `serde`
    type: `channel`, `grants`, `manifest` and `status`. One packet moves
    each module to the shared reader: `decisions-raw-serde-channel`,
    `decisions-raw-serde-grants`, `decisions-raw-serde-manifest` and
    `decisions-raw-serde-status`.
  - "JSON". Other modules read a JSON text with `serde_json` directly, for
    example `session`, `untrusted` and `config::mounts`. `agent-family`
    has a JSON writer of its own. Packet `decisions-raw-serde-others` moves
    them to the `json` module.
  - Rule 12. A count at the time of this line found 101 structs with a
    public field. The packets `decisions-private-*` and
    `decisions-runtime-private` make the fields private. The public-field
    check of `bin/rust-gate.sh` does not read the crates of those structs
    yet. Packet `decisions-private-fields-gate` extends the check to each
    crate.
  - Rule 13. Some values have two sources today. One example is the field
    `zone` of `quiet.daily` in the family file: the host has a time zone.
    A second example is the name of each directory below the state root,
    for example `families`. `creche_contracts::config` holds such a name,
    and `creche_runtime::layout` holds a copy. No packet has that change
    yet.
  - "The panic rule", clauses 2, 3 and 7. `agent-family` is the only
    program of the workspace today. Its `main` sets no panic hook and
    parses the command line itself. Its library has no entry function that
    catches a panic. No packet has this change yet.
  - The exit status of a refused start. `service::refuse_start` has a
    parameter for a second exit status. Packet `decisions-runtime-exit`
    deletes the parameter.
  - The address of another service. `config::chaperone` and
    `config::caregiver` define the port of another service as a constant.
    `config::endpoints` holds the names of the variables. The packet that
    makes a service read a variable deletes the constant of that service.
  - Rule 10. `time::Timestamp` has no differential test. No Python reader
    has its grammar today. Packet `strict-noticeboard-time` adds that
    reader, its vectors and the test.
  - Epoch. The crate needs a single epoch type with the range 1 to
    2^53 - 1. Packet `decisions-epoch` adds it.
  - Shared helpers. Base64 has more than one copy. Packet
    `decisions-util-runtime` moves it to `creche-util`. "Known gaps" of
    `crates/creche-util/AGENTS.md` names each other function that is
    still open. No packet has that part yet.
  - Vector reader. `vectors/data` needs a single reader. The owner still
    has to confirm this. Three readers exist today. Packet
    `decisions-vectors-crate` reduces them to one.
  - Coverage. A Rust file that ports a decision module of the chaperone
    needs a coverage rule (`chaperone/AGENTS.md`, rule 4). Nothing measures
    Rust coverage today. Packet `decisions-ci-coverage-gate` adds the rule
    and the job.
  - Tables of differences. Some tests still have one. Add no table and no
    row. The packets `decisions-tables-*`, `decisions-ids` and
    `decisions-runtime-tables` delete them.
  - "The differential test". Two parts of the test of `token` in
    `creche-runtime` do not hold the rule. The test walks no vector of
    `runtime.bearer.chaperone`, and the constant `NO_PORT_HERE` names that
    surface. The test gives the header of the vector `byte-1c-at-the-end`
    to a private function, because no request holds that header. Packet
    `attendance-one-bearer` gives each Python copy one rule for the
    bearer. It deletes the constant and the private path.
- Two checks do not read four crates yet: `agent-family`,
  `creche-contracts`, `creche-runtime` and `creche-testkit`. Each check has
  a list of its own with the four names. No list names a new crate, so both
  checks read a new crate from its first commit. Add no name to a list.
  - The public-field check of `bin/rust-gate.sh`. Packet
    `decisions-runtime-private` deletes `creche-runtime` and
    `creche-testkit` from the list of the script. Packet
    `decisions-private-fields-gate` deletes that list.
  - The table test of `bin/tests/test_rust_workspace.py`. In each `.rs`
    file, it looks for the name `DEVIATIONS` and for a struct whose name
    starts with `Deviation`. Packet `decisions-runtime-tables` deletes
    `creche-runtime` from the list of the test. Packet
    `decisions-tables-guard` deletes that list.
  - The public-field check reads the source text and expands no macro. It
    does not find a field that a macro adds to a struct. It finds a struct
    only at a line whose first word, after a visibility, is `struct`.
    `cargo fmt` writes each struct in that form.
  - The panic check and the public-field check read the form that
    `cargo fmt` writes. `cargo fmt` does not format an item below
    `#[rustfmt::skip]` and does not format the text of a macro call. Text
    in another form can hide code from both checks. These are two
    examples:
    - The last line of a test module has another indent than its first
      line. Both checks then read no code up to the next line with `}` at
      the indent of the first line.
    - A struct starts after another word on its line, for example after
      an attribute. The public-field check does not find that struct.
  - This `CONTRACT-QUESTION` comment is open in `bin/rust-gate.sh`: rule
    12 does not name `pub(self)` and `pub(in <path>)`. The public-field
    check refuses both forms. The owner did not confirm that reading. A
    change costs one condition in the scan. The same condition holds the
    two forms that pass, so a change to the third sentence of rule 12
    also changes it.
- Two lines of "JSON" wait for a confirmation of the owner: the duplicate
  key line and the 64-bit integer line. The Python readers accept both kinds
  of text today. If the owner says no, change those two lines.
- Two texts of "When the two results differ" wait for a confirmation of the
  owner. One is resolution (c). The other is the paragraph on a Python
  reader that a daemon calls at its start. Rule 10 of `vectors/AGENTS.md`
  depends on resolution (c). If the owner says no, change those texts.
- Rule 12 permits a field with `pub(crate)` or `pub(super)`. The owner did
  not confirm that reading yet. Some structs have such a field today, for
  example the raw forms of `status`. The other reading makes each such
  field private too. If the owner selects it, change the third sentence of
  rule 12.
- The owner did not decide if the advisory check blocks a merge. Today it
  does: step 5 of `bin/rust-gate.sh` makes the four checks. A change with no
  new dependency can thus fail on a new advisory. The other choice is an
  advisory check on a schedule.
- `release.yml` runs the same step after a merge that touches `rust/` or
  `vectors/`. A new advisory there fails the `rust` job, and that push gets
  no tag. The next push that passes gets the tags of both.
- The check of the locked crates reads four targets. A crate that only a
  build for another target uses gets no check, for example a build for Linux
  with musl or for Windows. `Cargo.lock` holds such crates.
- `bin/rust-gate.sh` does not check the version of the `cargo-deny` on
  `PATH`. Another version can read `deny.toml` in another way.
- The `advisories` check does not read crates.io for a version that its
  author removed. It reads the copy of the crates.io index that cargo keeps
  on the machine. With a complete `Cargo.lock`, cargo does not read
  crates.io again for a crate that the copy holds. A version that its
  author removes after cargo wrote the copy thus passes the check.
- The `rust` job keeps that copy in its cache. The key of the cache holds a
  hash of the toolchain file and of the lock file. A run can thus read the
  copy that an earlier run saved, until one of the two files changes.
  `bin/tests/test_gate_workflow.py` pins the path of the copy and the key.
  The other choice is a `rust` job that keeps no copy of the index in its
  cache. cargo then reads crates.io at each run.
- `cargo-deny` prints the warning `index-failure` for a crate when it cannot
  read the index entry of that crate. The check then cannot find a removed
  version of that crate. The warning does not fail step 5. An advisory for
  that crate still fails the check. The flag `-D index-failure` of
  `cargo deny check` makes the warning an error. Step 5 does not have the
  flag.
- No release uses Rust code.
- Most bodies of `creche-runtime` and of `creche-testkit` are stubs. A stub
  panics when code calls it. `crates/creche-runtime/AGENTS.md` and
  `crates/creche-testkit/AGENTS.md` list each stub and the packet that
  writes its body. The list of the first file still names the module
  `untrusted`. The bodies of that module are complete.
- This `CONTRACT-QUESTION` comment is open in
  `crates/creche-runtime/src/log.rs`: no contract says which characters a
  log line holds. The Python log writes each character as it is. The
  runtime writes a control character, a line separator and a bidirectional
  control as an escape.
- This `CONTRACT-QUESTION` comment is open in
  `crates/creche-runtime/src/log.rs`: no contract gives the form of a log
  line. The Python services write five forms. Three stamp the local time,
  and two have no time. The runtime writes one form, with the time in UTC.
- These `CONTRACT-QUESTION` comments are open in
  `crates/creche-runtime/src/token.rs`:
  1. `TokenRule::DOOR` and `TokenRule::NOT_EMPTY`, contract 02 §3 rule 5.
     The contract gives each token file a mode. The Python readers behind
     the two rules check none, and the rules do the same.
  2. `FILE_CAP`, contract 02 §3 rule 7. The contract gives a token a least
     count of bytes and no largest count. Each Python reader reads a token
     file of each size. `token::read` refuses a file of more than 1 MiB.
  3. `BEARER`, contract 02 §3 rule 4. The contract does not say if a service
     takes the scheme `Bearer` in another case of letters. Three Python
     copies take only `Bearer`. The chaperone takes each case.
     `token::bearer_of` takes only `Bearer`, so `BearerTrim` has no value
     for the rule of the chaperone. The test of `token` thus walks no vector
     of `runtime.bearer.chaperone`.
  4. `same_content`, contract 04 §7.3. The contract names three facts that
     the reader of the delegate token file compares: the time of the last
     change, the size and the inode. `token::CachedToken` also compares the
     device.
- This `CONTRACT-QUESTION` comment is open in
  `crates/creche-runtime/src/entropy.rs`: contract 02 §2 gives a mint of a
  ULID no rule for two times of the clock. One is a time before 1970. The
  other is a time past 48 bits of milliseconds. Each Python copy mints 26
  characters for such a time. `new_ulid` refuses it.
- This `CONTRACT-QUESTION` comment is open in
  `crates/creche-runtime/src/atomic.rs`: contract 04 §1.3 step 2 names the
  temporary file of a grant file `<family>.json.tmp`. The Python writer of
  the grant file uses another name. The runtime names each temporary file
  `.<name>.<pid>.<count>.tmp`, as the Python `attendance` does. A change of
  the name costs one function, `temp_name`.
- This `CONTRACT-QUESTION` comment is open in
  `crates/creche-runtime/src/atomic.rs`: contract 01 §6.1 gives the swap of
  a directory no rule for a `stat` that the system refuses. The Python copy
  raises there on Python 3.12 and on Python 3.13. On Python 3.14 it reads
  the refusal as "no entry". `replace_dir` is an error there, and no entry
  moves. A change costs one function, `says_no_entry`.
- This `CONTRACT-QUESTION` comment is open in
  `crates/creche-runtime/src/signals.rs`: contract 02 §3 rule 8 does not
  say how many reloads follow two SIGHUP signals. `Hangups` gives one item
  for all the signals that arrive while a reload runs. Two Python services
  run one reload for each SIGHUP that their loop takes. A change costs one
  function, `Hangups::next`.
- No check holds the rules of "The rules for a service", except a part of
  rule 2 and a part of rule 13. A service crate that breaks one of the
  other rules builds and passes the lint gate.
- `family` and `server` use the id types of `ids`. They refuse three texts
  that the Python package `agent_family` accepts. Each vector with such a
  text is a row of `DEVIATIONS` in `crates/agent-family/tests/vectors.rs`.
  1. A tool name of more than 64 bytes.
  2. The name of an environment variable of more than 64 bytes.
  3. A package version with `+`, with `-` or of more than 64 bytes.
- `crates/agent-family/AGENTS.md` lists the `CONTRACT-QUESTION` comments
  and the known gaps of the family file and of the server file.
- This `CONTRACT-QUESTION` comment is open in
  `crates/creche-contracts/src/time.rs`: the contracts name RFC 3339 for a
  time and say no more about its grammar. `time::Timestamp` refuses three
  texts that section 5.6 of RFC 3339 permits. A change costs one check of
  the reader and the rows of that text in the two test tables.
  1. A fraction of more than 9 digits.
  2. Second 60, the form of a leap second.
  3. The year 0000. One day of that year can name an instant of the year
     0001 through its offset, for example `0000-12-31T23:30:00-01:00`. No
     Python reader of the platform takes such a text.
- These `CONTRACT-QUESTION` comments are open in
  `crates/creche-contracts/src/ids.rs`:
  1. `Ulid`, contract 02 §2. The contract writes the pattern with `$`. In
     Python, `$` also matches before a final newline. Each Python copy
     refuses a final newline, and the type refuses it.
  2. `ToolName`, contract 01 §3.4 and contract 01b §5. The contracts give no
     cap. The three Python copies have no cap, a cap of 128 and a cap of 64.
     The type has the cap of 64.
  3. `EnvName`, contract 01b §4.1. The contract gives no grammar. One Python
     copy has no cap, and one has a cap of 64. The type has the cap of 64.
  4. `PackageVersion`, contract 01b §3.1. The contract gives no grammar. One
     Python copy permits `+` and `-` and has no cap. The type takes the other
     copy: no `+`, no `-` and a cap of 64.
  5. `OwuiChatId`, contract 02 §2. The contract gives no cap for a chat id.
     The Python door refuses a chat id of more than 123 bytes, because the
     session id `owui-<chat id>` has 128 bytes or less. The type has that
     cap. `OwuiChatId::session_id` thus gives no error.
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
  15. `ServiceNote::IllegalTransition`, contract 02 §4.3 and §8.1. No
      contract names the note for a refused move of a turn. The Python
      `attendance` writes that note only for a move that its state table
      refuses. The type takes each pair of turn states.
  16. `StreamRecord`, contract 02 §8.1. The contract says that the set of
      kinds is closed. It does not say what a reader of the stream does
      with a record of another kind. The type has no variant for such a
      record, so a reader that makes the type refuses it. The Python
      readers of the stream give no output for such a line. The crate has
      no reader of a stream record yet.
- The `session` module differs from the Python code on purpose in four
  ways. Each one is a row of `DEVIATIONS` in `session/python.rs`.
  1. A JSON text is UTF-8 with no byte order mark. It holds no `NaN` and
     no `Infinity`. It holds no lone surrogate in a key or in a text that a
     parser reads, and no bytes of a surrogate. The Python parsers refuse a
     lone surrogate in a text that a parser reads, and in the key of a label.
     They take one in four texts that reach a file only: the reason of a
     stop, the reason of a switch, the name of a trigger and the key of a
     dispatch. The Python reader accepts one in each other key, and it
     accepts the bytes of a surrogate in a member that no parser reads.
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
- Some fields of the body types of a journal line and of
  `session::ServiceNote` are a plain `String`: the contract gives them no
  grammar. Code can build such a body with each text.
- Only `serde` makes a `session::Lease`, a `session::SessionView` and a
  `session::TurnView`, through the raw type of each one. No constructor
  takes typed parts.
- These `CONTRACT-QUESTION` comments are open in
  `crates/creche-contracts/src/untrusted.rs`:
  1. `parse_object`, contract 02 §3 rule 3. The contract says that a body is
     JSON. It does not say if a reader takes what `json.loads` of Python
     takes past strict JSON in UTF-8. Each Python client takes a part of it.
     Difference 1 and difference 6 below say which part. The reader is
     `serde_json`, which refuses each part.
  2. `DEPTH_MAX`, contract 02 §3 rule 3. The contract gives a body no nesting
     limit. `parse_object` reads 127 levels, the limit of `serde_json`. The
     tree of the module stops at 128 levels, for a deserializer with no
     limit.
  3. `int`, contracts 02, 04 and 05. No contract gives a count a range. The
     Python readers keep an integer of each size. The reader gives an `i64`,
     and it reads a larger integer as 0.
- The module `untrusted` differs from the Python helpers on purpose in six
  ways. Each one is a row of `DEVIATIONS` in the test of the module.
  1. An answer is strict JSON in UTF-8, with no byte order mark. It holds no
     `NaN`, no `Infinity`, no number outside the range of a float and no half
     of a surrogate pair. Each Python client reads `NaN`, `Infinity`, such a
     number and the escape of such a half. The noticeboard, the delegate
     client of the chaperone and `caregiver` give `json.loads` the bytes.
     They also read a byte order mark, UTF-16, UTF-32 and the bytes of such
     a half. The three doors give `json.loads` the text of `httpx`, and they
     refuse the first three.
  2. An answer nests 127 levels at most.
  3. `int` reads an integer that no `i64` holds as 0.
  4. `number` reads the integer `-0` as `-0.0`. Python reads it as `0.0`.
  5. `text` reads a field that is no text as the empty text. `_text` of the
     delegate client of the chaperone reads it as `None`.
  6. An answer holds no byte that is not UTF-8. The three doors read such a
     byte as U+FFFD, because `httpx` makes a text of the body first. The
     three other clients refuse the answer, and `parse_object` refuses it
     too. The port of a door gives `parse_object` the lossy text of the body,
     or it names the difference in its pull request.

  One more row is a vector on which the two sides accept the same document.
  The raw type of the test keeps an integer past 64 bits as a float.
- `untrusted::parse_object` refuses the whole answer for difference 1, for
  difference 2 and for difference 6. The Python services write the JSON body
  of an answer with the JSON response class of their web framework. That
  class writes no `NaN`, no `Infinity` and no half of a surrogate pair. It
  writes an integer of each size. The owner of the crate decides if the
  module gets a JSON reader of its own, as `channel`, `grants`, `status` and
  `manifest` have.
- The module `untrusted` has no reader that tells a value that is no list
  from an empty list. `untrusted::list` reads both as the empty list.
  `is_list` of each door gives `False` for the first only, and `as_array` of
  `attendance` gives `None` for the first only.
- `untrusted::parse_object` gives `NotAnObject::NotObject` for an object that
  the raw type refuses. `NotAnObject` has no variant for that case. A raw
  type refuses no object when each of its fields names a reader of the
  module and has `default`.
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
  8. `claim::Event`, contract 03 §13 rule 5. The contract names no event that
     the host cannot record. The Python host keeps only the type of an event
     with a lone surrogate. The reader does the same.
- The host side of `channel` accepts what the Python host accepts, also
  where a stricter reading of contract 03 is possible. The owner decides each
  case. Four examples:
  1. A session id and a turn id of a line can be each text.
     `TurnAddress` is the check that follows.
  2. `turn_seq` and each count can be an integer past 64 bits.
  3. A text outside an event can hold a lone surrogate.
  4. `turn_failed` with no session, no turn and the `turn_seq` 0 is
     `malformed`. Contract 03 §5.1 permits that line.
- No vector covers the side of the playpen: `HostMessage::parse` and
  `PlaypenMessage`. The tests read each line of one side with the parser of
  the other side. `HostMessage::parse` is stricter than the TypeScript
  playpen in nine places:
  1. A required number with a fraction or an exponent is a fault: `600.0`.
     The same applies to `grace_ms`. The playpen reads `600.0` as 600.
  2. A sandbox number has 9 digits at most.
  3. A text with a lone surrogate is not a text.
  4. A protocol version is two numbers. The playpen takes each text with a
     `.`.
  5. A workspace kind is `code-sandbox`. The playpen reads each text.
  6. A cap counts bytes. The playpen counts UTF-16 code units.
  7. A `model` has 200 bytes at most. The playpen keeps each text.
  8. An `env_epoch` is less than 2^64. The playpen reads a larger one as a
     float.
  9. A line with an integer of more than 4300 digits is not JSON, in each
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
     `--write` without the key. The Python service refuses that start too.
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
  14. `endpoints::PlaneUrl`, contract 01 §3.7 rule 4 and contract 03 §7. The
      first section names the two plane endpoints as `host:port`. The second
      section gives the two URLs as fixed values of the form
      `http://<host>:<port>`. No contract gives the URL of a plane a
      grammar. The type takes `http://`, a host and a port, and no other
      byte. The port has no sign and no zero at its start. A host name
      keeps its letter case. No Python reader holds this grammar, so no
      vector covers the type.
- No type reads the text of a roster file, and no type writes it. PyYAML
  reads YAML 1.1, and no Rust YAML reader is in the workspace. The owner of
  the crate selects one. `roster::RawRoster` then takes its tree.
- The Python reader of a roster refuses a text past one of three limits. A
  Rust reader of that text must hold the same limits. No vector holds such a
  text. The first two limits are the limits of `manifest/yaml.rs`. No Rust
  reader has the third limit.
  1. The merge keys copy more than 65,536 pairs.
  2. The merge keys make a chain of more than 128 levels.
  3. The aliases stand for more than 262,144 nodes. An alias stands for the
     node of its anchor and for each node that this node holds.
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
- The `systemd-proof` job proves the rule about `RestartPreventExitStatus`
  and `ExecStartPre=` on the systemd of a CI runner. It uses transient units
  of its own. No test starts a daemon unit of this repository under systemd.
  The host can have another version of systemd, and no run on the host
  proves the rule there.
