# creche-runtime

The runtime that each Rust service of this repository shares.
`rust/AGENTS.md` and the root `AGENTS.md` apply here too.

Each Python package holds its own copy of the helper code of a service. This
crate holds each helper one time. A service crate calls a module of this
crate in place of its copy.

This crate replaces no Python code. No release uses it. The Python packages
are the authority until a release of a service uses a Rust binary.

## Layout

Each module is one file. One packet of the port owns each file.

| File | What it holds | Packet |
|---|---|---|
| `src/lib.rs` | The list of the modules. | `foundation-skeleton` |
| `src/log.rs` | One line on stderr for each event. The panic hook. The writers for stdout and stderr. | `foundation-skeleton` |
| `src/atomic.rs` | A file write through a temporary file, a sync and a rename. | `foundation-files` |
| `src/readfile.rs` | A file read with a size cap. The read gives each failure as a value and does not panic. | `foundation-files` |
| `src/layout.rs` | The paths under the state root that more than one service uses. | `foundation-files` |
| `src/clock.rs` | The `Clock` seam and the clock of the host. | `foundation-clock-entropy` |
| `src/entropy.rs` | The `Entropy` seam. The mint of a ULID and of a random token. | `foundation-clock-entropy` |
| `src/tasks.rs` | The stop signal, the tracked tasks and the lock helper. | `foundation-tasks-signals` |
| `src/signals.rs` | SIGTERM, SIGINT and SIGHUP. | `foundation-tasks-signals` |
| `src/token.rs` | A token file into a `Secret`. The bearer of a request. | `foundation-token-faults` |
| `src/faults.rs` | The fault file of contract 05 §3.3.1, onto the disk. | `foundation-token-faults` |
| `src/command.rs` | A child program: exact words, a time limit, one owner. | `foundation-command` |
| `src/http.rs` | The list of the three HTTP modules. | `foundation-skeleton` |
| `src/http/client.rs` | An HTTP/1.1 client over a Unix socket or TCP. | `foundation-http-client` |
| `src/http/server.rs` | The bind of a listener and the serve loop. | `foundation-http-server` |
| `src/http/layers.rs` | The edge layer of a router. | `foundation-http-server` |
| `src/service.rs` | The steps of the start of a program. | `foundation-service` |
| `src/args.rs` | A reader for the arguments of a program. | `foundation-service` |

The skeleton packet also added two items to `creche-contracts`:

| Item | What it holds | Packet |
|---|---|---|
| `creche_contracts::untrusted` | Readers for an answer of another service. A field of a wrong type reads as empty. | `foundation-untrusted` |
| `creche_contracts::config::python_strip` | The public name of the Python `str.strip` rule. | `foundation-skeleton` |

The module is in `creche-contracts` because this crate depends on that crate.
The answer types of contract 02 need the readers, and they are in
`creche_contracts::session`.

## What is complete

The skeleton packet wrote these parts in full. A later packet does not write
them again.

- `log`: each function and each macro.
- `readfile::ByteCap` and `readfile::FileFacts`.
- `clock::Monotonic` and `clock::SystemClock::new`.
- `entropy::OsEntropy::new`.
- `layout::FaultWriter`.
- The builders and the readers of `command::Command` and of
  `command::PipedCommand`.
- `CommandRunner` for `Arc<R>` and for `&R`.
- `http::client::Timeouts`.
- The five constants of `token::TokenRule`, and its four readers.
- Each `Display` and each `Error` of an error type.

## Rules for a change here

1. Edit only the files of your packet. `lib.rs`, `http.rs`, `Cargo.toml` and
   this file belong to the skeleton packet.
2. Change no signature that the skeleton fixed. A module packet writes a
   body. Ask the owner of the crate before you change a signature.
3. A stub is `todo!()` below this attribute:
   `#[expect(clippy::todo, reason = "skeleton: packet <key> writes this body")]`.
   A stub with a named parameter also names `unused_variables` there.
4. Remove the attribute when you write the body. The build fails on an
   attribute that stays.
5. Write the stub of a trait method that returns `impl Future` as `async fn`
   in the implementation.
6. Mark each doc example that calls a stub `no_run`.
7. An opaque type of the skeleton has one private field of the type `()`.
   Replace that field when you write the body of the type.
8. This crate has `creche-testkit` as a dev dependency, and `creche-testkit`
   depends on this crate. A test inside `src/` thus sees other types than the
   testkit sees.
   - Put a test that uses `FixedClock`, `PausedClock`, `CountingEntropy` or
     `FakeRunner` under `tests/`, inside a `#[cfg(test)]` module.
   - A test inside `src/` uses only `TempRoot`, `HttpStub`, `RawHttp`,
     `write_program` and the vectors reader.
9. Give each type with a private field its `compile_fail` doc test
   (`rust/AGENTS.md`, "Tests"). The packet that writes the body of the type
   writes that test.
10. Where a surface `runtime.*` of `vectors/data` covers a function, the test
    of that function walks each vector. Read the vectors through
    `creche_testkit::vectors`. Name each surface in a table of the test.
11. Some differences from the Python origin are in no vector. Describe such
    a difference in the doc comment of the Rust function. Pin it with one
    plain test. Packet `decisions-runtime-tables` removes the `DEVIATIONS`
    tables of this crate. Until then, add no row to one.
    - A new `Command` has an empty standard input and an output cap of 1 MiB.
      `subprocess.run` of Python gives the child the standard input of the
      process and has no cap.
    - A port of such a call can keep one of the two defaults. The doc
      comment of its function then names that default.
12. Name the Python origin of each function in its doc comment, with the file
    and the line.
13. Write no `println!` and no `eprintln!` in code that is not a test. Use
    `log::out_line`, `log::err_line` and the log macros.
14. An error holds the kind and the text of the operating system error. It
    never holds a secret, the content of a file or the body of a request.
15. Implement `Display` and `Error` for each error type yourself. The
    workspace takes neither `anyhow` nor `thiserror`.
16. Give each type that can hold a token a `Debug` that prints no byte and no
    count of the bytes. The size of a token file is such a count. Contract 02
    §3 rule 6 keeps the length of a token out of each log line.

## The stubs

This list is the state of the skeleton. A module packet does not edit this
file when it writes a body. The integration packet removes the list when no
stub is left.

A stub panics when code calls it. Start a packet only after each packet that
it needs merged.

The list holds 98 stubs: 88 in this crate and 10 in
`creche_contracts::untrusted`. Each stub is one `clippy::todo` attribute.

| File | Packet | Stubs | The stubs |
|---|---|---|---|
| `src/atomic.rs` | `foundation-files` | 4 | `write`, `write_new`, `replace_dir`, `ensure_dir` |
| `src/readfile.rs` | `foundation-files` | 3 | `read_capped`, `facts`, `os_text` |
| `src/layout.rs` | `foundation-files` | 15 | `StateRoot::new`, `families_dir`, `family_dir`, `status_file`, `validation_file`, `creds_dir`, `config_dir`, `control_dir`, `grant_file`, `fault_dir`, `fault_file`, `outcomes_dir`, `audit_dir`, `tokens_dir`, `webhook_token_file` |
| `src/clock.rs` | `foundation-clock-entropy` | 4 | `SystemClock::now`, `SystemClock::monotonic`, `unix_micros`, `unix_seconds` |
| `src/entropy.rs` | `foundation-clock-entropy` | 3 | `OsEntropy::fill`, `new_ulid`, `url_token` |
| `src/tasks.rs` | `foundation-tasks-signals` | 12 | `shutdown_pair`, `Shutdown::cancelled`, `Shutdown::is_cancelled`, `ShutdownTrigger::trigger`, `Tasks::new`, `Tasks::shutdown`, `Tasks::spawn_must_complete`, `Tasks::spawn_blocking`, `Tasks::spawn_loop`, `Tasks::drain`, `Completion::poll`, `locked` |
| `src/signals.rs` | `foundation-tasks-signals` | 2 | `install`, `Hangups::next` |
| `src/token.rs` | `foundation-token-faults` | 4 | `read`, `CachedToken::new`, `CachedToken::current`, `bearer_of` |
| `src/faults.rs` | `foundation-token-faults` | 1 | `publish` |
| `src/command.rs` | `foundation-command` | 9 | `Ended::python_returncode`, `TokioRunner::new`, `TokioRunner::run`, `python_text`, `spawn_piped`, `ChildGuard::terminate`, `ChildGuard::start_kill`, `ChildGuard::wait`, `ChildGuard::end` |
| `src/http/client.rs` | `foundation-http-client` | 12 | `Target::unix`, `Target::from` for `&BindAddress`, `Target::try_from` for `&HttpUrl`, `Target::try_from` for `&AttendanceTarget`, `PathAndQuery::from_segments`, `PathAndQuery::with_query`, `Client::new`, `Client::send`, `Client::open`, `ReplyStream::status`, `ReplyStream::headers`, `ReplyStream::chunk` |
| `src/http/server.rs` | `foundation-http-server` | 4 | `bind`, `Bound::describe`, `Bound::local_port`, `serve` |
| `src/http/layers.rs` | `foundation-http-server` | 4 | `edge`, `ClientGone::gone`, `read_body`, `StarletteBodies::answer` |
| `src/service.rs` | `foundation-service` | 7 | `run`, `ErrorLines::lines` for `ConfigErrors`, `ErrorLines::lines` for `SiteErrors`, `ErrorLines::lines` for `RosterErrors`, `load`, `refuse_start`, `ready` |
| `src/args.rs` | `foundation-service` | 4 | `Args::from_os`, `Args::value`, `Args::next`, `usage_exit` |
| `creche-contracts/src/untrusted.rs` | `foundation-untrusted` | 10 | `text`, `int`, `number`, `flag`, `object`, `block`, `list`, `list_first`, `parse_object`, `cut` |

One more attribute waits for a body. The field `start` of
`clock::SystemClock` has `#[expect(dead_code)]`. Packet
`foundation-clock-entropy` removes it when `monotonic` reads the field.

Three stubs of `src/service.rs` hold one statement before `todo!()`: `run`,
`load` and `refuse_start`. Each one calls `log::init` with the name of the
program. Rule 16 of "The rules for a service" in `rust/AGENTS.md` gives the
reason. The packet that writes the three bodies obeys these rules:

1. Keep that call as the first statement of the body.
2. Keep the test `service::tests::each_entry_sets_the_panic_hook`. It runs
   each of the three in a child process.
3. Do not call one of the three from a test in the test process. Such a call
   replaces the panic hook of the test program. A later test that fails then
   prints no message.
4. Put the rest of each body in a private function that sets no hook. Give
   a test that function.

## Known gaps

- This `CONTRACT-QUESTION` comment is open in `src/log.rs`: no contract says
  which characters a log line holds. The Python log writes each character as
  it is. This crate writes a control character, a line separator and a
  bidirectional control as an escape. Each other format character of Unicode
  stays as it is, for example U+200B. A change of the set costs one
  function, `is_escaped`.
- This `CONTRACT-QUESTION` comment is open in `src/log.rs`: no contract gives
  the form of a log line, and no program reads one. The Python services
  write five forms. Three stamp the local time, and two have no time. This
  crate writes one form with the time in UTC. A change of the form costs one
  function, `format_line`.
- This `CONTRACT-QUESTION` comment is open in `src/token.rs`:
  `TokenRule::DOOR` and `TokenRule::NOT_EMPTY` check no mode. Contract 02 §3
  rule 5 gives each token file a mode, and `attendance` checks it. The Python
  readers of a door, of the chaperone and of `caregiver` check none. The two
  rules keep the reading of those readers. The owner decides if a rule gets
  a mode check.
- This `CONTRACT-QUESTION` comment is open in `src/entropy.rs`: contract 02
  §2 gives the length and the alphabet of a ULID and no layout of its bits.
  It gives a mint no rule for two times of the clock. One is a time before
  1970. The other is a time past 48 bits of milliseconds. Each Python copy
  mints 26 characters for such a time. `new_ulid` refuses it. A change costs
  one more mint in `creche-contracts`.
- This `CONTRACT-QUESTION` comment is open in `src/atomic.rs`: contract 04
  §1.3 step 2 names the temporary file of a grant file `<family>.json.tmp`.
  The Python writer of the grant file uses another name. This crate names
  each temporary file `.<name>.<pid>.<count>.tmp`, as the Python `attendance`
  does. A change of the name costs one function, `temp_name`.
- This `CONTRACT-QUESTION` comment is open in `src/atomic.rs`: contract 01
  §6.1 gives the swap of a directory no rule for a `stat` that the system
  refuses. An example is a `stat` through a symlink into a directory that
  the process cannot enter. The Python copy raises there on Python 3.12 and
  on Python 3.13. On Python 3.14 it reads the refusal as "no entry".
  `replace_dir` is an error there, and no entry moves. A change costs one
  function, `says_no_entry`.
- This `CONTRACT-QUESTION` comment is open in `src/signals.rs`: contract 02
  §3 rule 8 gives SIGHUP its job and does not say how many reloads follow
  two signals. `Hangups` gives one item for all the signals that arrive
  while a reload runs. Each SIGHUP then has a reload that starts after it.
  The Python chaperone has the same rule. The Python `attendance` and the
  Python trigger door run one reload for each SIGHUP that their loop takes.
  A change costs one function, `Hangups::next`.
- These `CONTRACT-QUESTION` comments are open in `src/http/client.rs`:
  1. `Target::try_from` for an `HttpUrl`: no contract gives the base URL of
     a service a grammar. `creche_contracts::config::HttpUrl` checks only
     the scheme, the user part and that a host is there. `httpx` takes most
     of the URLs that pass that check. The function refuses a URL with a
     query or with a fragment. It refuses a port that is not 1 to 65535 in
     ASCII digits, and a host that is no `BindHost`. A laxer reading costs
     one check and two functions, `host_and_port` and `port_of`.
  2. `bearer_value`: contract 02 §3 rules 4 and 7 give a token a least count
     of bytes and no set of bytes. The function refuses a token with a
     control character that is not a tab, and a token with the byte 0x7F.
     `httpx` sends such a token when the character is not one of these:
     NUL, line feed, vertical tab, form feed and carriage return. The `http`
     crate takes no header value with such a byte. A laxer reading thus
     needs another header type.
- `http::client::ClientError` has no variant for a request that the client
  cannot write. A request target of more than 65,534 bytes gives
  `ClientError::Protocol`. `TargetError::NotAnAddress` also stands for a base
  URL with a query or with a fragment. A variant for each case changes a
  type that the skeleton fixed. The owner of the crate decides.
- `http::client::Target::unix` returns no error. A host text that a header
  cannot hold gives an empty `Host` header. Each caller gives a constant of
  its code as that text.
- The connect limit of `http::client` does not stop the lookup of a host
  name. `tokio` runs the lookup on a blocking thread. That thread continues
  until the resolver of the host answers. A target with an IP address or
  with a Unix socket has no lookup.
- The write limit of `http::client` ends when hyper flushes the socket.
  hyper 1.11.1 flushes one time for a request, after the last byte. A
  version that flushes each part of a request gives the limit to each part.
  No test shows that change.
- No test of `http::client` makes a real connect wait, because no listener
  holds a connect open on each operating system. The test of
  `Phase::Connect` uses a private target that completes no connect.
- Most bodies are stubs. "The stubs" lists them.
- `log::line` blocks its thread until stderr takes the line. The service
  waits when the journal does not read. A Python service waits in the same
  way.
