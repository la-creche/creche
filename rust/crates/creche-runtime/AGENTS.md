# creche-runtime

The runtime that each Rust service of this repository shares.
`rust/AGENTS.md` and the root `AGENTS.md` apply here too.

Each Python package holds its own copy of the helper code of a service. This
crate holds each helper one time. A service crate calls a module of this
crate in place of its copy.

This crate replaces no Python code. No release uses it. The Python packages
are the authority until a release of a service uses a Rust binary.

## Layout

Each module is one file. One packet of the port owns each file. No stub is
left: each function of this crate has its body.

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

## Rules for a change here

1. Edit only the files of your packet. `lib.rs`, `http.rs`, `Cargo.toml` and
   this file belong to the skeleton packet.
2. Change no signature that the skeleton fixed. A module packet writes a
   body. Ask the owner of the crate before you change a signature.
3. This crate has `creche-testkit` as a dev dependency, and `creche-testkit`
   depends on this crate. A test inside `src/` thus sees other types than the
   testkit sees.
   - Put a test that uses `FixedClock`, `PausedClock`, `CountingEntropy` or
     `FakeRunner` under `tests/`, inside a `#[cfg(test)]` module.
   - A test inside `src/` uses only `TempRoot`, `HttpStub`, `RawHttp`,
     `write_program` and the vectors reader.
4. Give each type with a private field its `compile_fail` doc test
   (`rust/AGENTS.md`, "Tests"). The packet that writes the body of the type
   writes that test.
5. Where a surface `runtime.*` of `vectors/data` covers a function, the test
   of that function walks each vector. Read the vectors through
   `creche_testkit::vectors`. Name each surface in a table of the test.
6. Some differences from the Python origin are in no vector. Describe such
   a difference in the doc comment of the Rust function. Pin it with one
   plain test. Do not record a difference in a table. A test of
   `bin/tests/test_rust_workspace.py` fails for such a table in this crate.
   - A new `Command` has an empty standard input and an output cap of 1 MiB.
     `subprocess.run` of Python gives the child the standard input of the
     process and has no cap.
   - A port of such a call can keep one of the two defaults. The doc
     comment of its function then names that default.
7. Name the Python origin of each function in its doc comment, with the file
   and the line.
8. Write no `println!` and no `eprintln!` in code that is not a test. Use
   `log::out_line`, `log::err_line` and the log macros.
9. An error holds the kind and the text of the operating system error. It
   never holds a secret, the content of a file or the body of a request.
10. Implement `Display` and `Error` for each error type yourself. The
    workspace takes neither `anyhow` nor `thiserror`.
11. Give each type that can hold a token a `Debug` that prints no byte and no
    count of the bytes. The size of a token file is such a count. Contract 02
    §3 rule 6 keeps the length of a token out of each log line.
12. Four functions of `src/service.rs` set the panic hook of the process:
    `enter`, `run`, `load` and `refuse_start`. Each one calls `log::init`
    with the name of the program. Rule 16 of "The rules for a service" in
    `rust/AGENTS.md` gives the reason.
    - Keep that call as the first statement of each body.
    - Keep the test `service::tests::each_entry_sets_the_panic_hook`. It
      runs each of the four in a child process.
    - Do not call one of the four from a test in the test process. Such a
      call replaces the panic hook of the test program. A later test that
      fails then prints no message.
    - Keep the rest of each body in a private function that sets no hook.
      Give a test that function.

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
- These `CONTRACT-QUESTION` comments are open in `src/token.rs`:
  1. `TokenRule::DOOR` and `TokenRule::NOT_EMPTY` check no mode. Contract 02
     §3 rule 5 gives each token file a mode, and `attendance` checks it. The
     Python readers of a door, of the chaperone and of `caregiver` check
     none. The two rules keep the reading of those readers. The owner
     decides if a rule gets a mode check.
  2. `FILE_CAP`: contract 02 §3 rule 7 gives a token a least count of bytes
     and no largest count. Each Python reader reads a token file of each
     size. `read` refuses a file of more than 1 MiB. A larger cap costs one
     constant.
  3. `BEARER`: contract 02 §3 rule 4 does not say if a service takes the
     scheme `Bearer` in another case of letters. `attendance`, the Open
     WebUI door and the trigger listener take only `Bearer`. The chaperone
     takes each case. `bearer_of` takes only `Bearer`. `BearerTrim` thus has
     no value for the rule of the chaperone. The port of the chaperone needs
     the answer first.
  4. `same_content`: contract 04 §7.3 names three facts that the reader of
     the delegate token file compares. They are the time of the last change,
     the size and the inode. `CachedToken` also compares the device, because
     an inode number is an id only on one device. The Python cache compares
     the three facts. A change costs one line.
- The test of `token` walks no vector of `runtime.bearer.chaperone`. The
  Python chaperone accepts the scheme in each case of letters, and
  `bearer_of` gives no bearer for the vectors `scheme-lower-case` and
  `scheme-upper-case`. The constant `NO_PORT_HERE` names the surface. This
  difference on an HTTP surface is open. No Rust service reads a bearer
  yet.
- The test `no_trim_gives_each_result_of_the_surface_with_no_walk` holds the
  reason for `NO_PORT_HERE`. It fails when one value of `BearerTrim` gives
  each result of the surface. Remove the constant then. Name the surface in
  the table `COPIES`.
- The vector `byte-1c-at-the-end` of the `runtime.bearer` surfaces holds a
  header that ends with the byte `0x1c`. Three Python services accept that
  request. A `HeaderValue` holds no such byte, so no request gives that
  header to `bearer_of`. The test gives the bytes to the private function
  `bearer_in`: the vector proves that function and no service. This
  difference on an HTTP surface is open. A Rust service answers such a
  request with status 400, and no handler runs. `hyper` refuses a header
  value with that byte. A test of `src/http/server.rs` holds that answer.
- `token::CachedToken` reads the file again only when one of four facts of
  the file moved. The facts are the device, the inode, the size and the time
  of the last change. It does not see a new token that has each fact of the
  old one. The Python cache has the same limit. A writer that replaces the
  file with a rename gives it a new inode.
- `token::CachedToken` checks the mode of its rule only when it reads the
  file. A wider mode alone starts no read, so the token stays in use. The
  Python cache checks no mode, as `TokenRule::DOOR` checks none. The owner
  of the crate decides this case before a cached token gets a rule with a
  mode check.
- `token::CachedToken::current` blocks for one `stat`, and for one read
  after a change. It gives a borrow of the token. A caller thus cannot run
  it in `Tasks::spawn_blocking` and keep the token after the call. Rule 7 of
  "The rules for a service" in `rust/AGENTS.md` has no exception for that
  `stat`. The owner of the crate decides: an exception, or one more reader
  of the type. The port of the chaperone needs the answer.
- `token::bearer_of` gives the bearer of a request as a `Vec<u8>`. Those
  bytes can be a token, and the `Debug` of a `Vec<u8>` prints each byte.
  Rule 11 of "Rules for a change here" asks for a `Debug` that prints no
  byte. A service gives the bytes to `Secret::matches`. It writes them to no
  log line. The owner of the crate decides if the result gets a type of its
  own.
- `command` sets no limit on the size of a file that a child writes. The
  Python `handover` sets one in the child before the program starts
  (`handover/src/handover/executor/host.py:359-376` and `:407`). In Rust
  that step needs `unsafe` code, and the lint gate forbids it. The port of
  `handover` needs another design.
- An owner task of `command` kills only the child. A program that the child
  started continues to run, and it can hold an output stream of the child
  open. `subprocess.run` of Python has the same limit. With
  `TimeLimit::None`, a run that captures such a stream waits until that
  program closes the stream.
- `command::ChildGuard::wait` gives the exit status 255 when the wait call
  of the operating system fails. The skeleton fixed the signature of the
  function, and that signature has no error. The owner of the crate decides
  if the function gets one.
- `command` does not refuse each program file that CPython refuses. Such a
  file has no `#!` line and is no binary program, and CPython gives the
  error "Exec format error" for it. For a program name with no `/`, the
  runner gives the file to `/bin/sh` when the command clears the environment
  or sets `PATH`. For a path with a `/`, Linux refuses the file and macOS
  gives it to `/bin/sh`. A service that names each program by its absolute
  path gets the refusal on Linux.
- A child of `command` gets each descriptor of the process that has no
  close-on-exec flag. CPython closes each descriptor past 2 in the child.
  Open each descriptor of a service with the flag. `rustix` sets the flag
  only when the call asks for it. Give `OFlags::CLOEXEC` to each open call
  of `rustix`.
- `tokio` starts a program first and gives its pipes to the I/O driver after
  that. When the driver refuses a pipe, `command` gives
  `RunError::NotStarted`, and the program runs with no owner. This process
  then closes its ends of the pipes of that program.
- After a wait call that failed, the drop of the child sends SIGKILL to the
  id of the child, because `tokio` holds that flag. The operating system can
  give that id to another process before the drop. This applies to a run
  that gave `RunError::OwnerLost` for a failed wait call. It also applies to
  the drop of a `ChildGuard` after such a call.
- No test gives `faults::publish` a fault file whose source is `caregiver`.
  `FaultFile::new` refuses that source, so no code can build such a file. A
  test gives the private function `publish_as` no writer in its place.
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
- This `CONTRACT-QUESTION` comment is open in `src/http/layers.rs`: no
  contract and no Python framework gives an answer for three failures of the
  edge. The failures are a handler that the runtime stopped, a body past a
  cap and a body that stops early. `StarletteBodies` answers the first as
  the framework answers an exception. It answers the two others with the
  status only: 413 and 400. A change costs one arm of `answer`.
- No vector holds the answer of `StarletteBodies` for a panic. Each Python
  service has a handler of its own for an exception, so no surface
  `runtime.edge.*` holds the answer of the framework. The constant is the
  text of `starlette/middleware/errors.py:259`. A plain test holds it.
- No route behind `http::layers::edge` answers `HEAD`. The edge gives each
  `HEAD` request another method before the router gets it, so a route that
  names `HEAD` gets no request. The Python noticeboard answers `HEAD` on a
  file of its static mount with status 200 and no body. A router behind
  `edge` answers status 405 for that route. The port of the noticeboard
  needs an answer first: a change to `edge`, or a route outside it.
- A layer that a router has before `edge` gets only the answer of a handler.
  The Python noticeboard adds a cookie to an answer of its framework too,
  for example to an answer with status 404. A layer on the result of `edge`
  gets each answer. It runs outside the task of the request and outside the
  panic boundary. The port of the noticeboard needs that answer too.
- `http::server` and `http::layers` differ from the server and from the
  framework of the Python services in more ways. The doc comments of `bind`,
  `serve`, `edge` and `read_body` name each one, and a plain test holds each
  one. Two examples: a listener sets no time limit on a connection, and a
  router matches the path as the client sent it.
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
- This `CONTRACT-QUESTION` comment is open in `src/service.rs`. No contract
  and no rule names the exit status of a program that gets no runtime or no
  signal handler from the operating system. `rust/AGENTS.md`, "The rules for
  a service", rule 17 does not list that cause. `service::run` returns
  status 71, `EX_OSERR` of `sysexits.h`. A restart can repair the cause, so
  systemd must start the unit again. A change costs one constant.
- The same question is open for a listener that does not bind. A Python
  service ends with status 3 there, which is the status of `uvicorn` for a
  start that failed. The first example of `src/service.rs` returns the
  failure status of the standard library. Each service states that status
  in its own `main`. The owner of the crate decides if the runtime gets one
  constant for it.
- `service::run` does not stop a `main` that continues after the stop
  signal. The drain limit of the program starts when `main` returns. The
  time that `main` uses after the signal plus the drain limit must thus be
  less than `TimeoutStopSec=` of the unit. No check holds that sum. A `main`
  that never returns holds the process until systemd kills it.
- `service` differs from the `main` of each Python service, and `args`
  differs from `argparse` of Python. The doc comment of each of the two
  modules names each difference, and a plain test holds each one. Two
  examples: a program takes a flag only with its full name, and
  `service::load` writes one line for each error of a config.
- No vector covers `args`. A command line is a contract surface where a unit
  file, a component manifest, a hook or a script of this repository writes
  it. None of them writes a command line on which `args` and `argparse`
  differ. One other file does: `library/Dockerfile` gives its program the
  flag `--help`, and `argparse` answers that flag itself. The port of that
  program matches the flag. A program with vectors for its command line
  holds its own parser equal to those vectors.
- A program on `args` accepts one kind of command line that the `argparse`
  parser of its Python origin refuses. That kind has a `--` at a place where
  the parser has no positional word left to take. `args` gives a program no
  word for `--`, so the program cannot refuse it. No unit file, no component
  manifest, no hook and no script of this repository writes such a command
  line.
- `service::Loaded` has the variant `RefuseEachCall` for each config type. A
  program whose config type says `AtStart::ExitConfig` thus writes an arm
  that never runs. The skeleton fixed the type. The owner of the crate
  decides if `service::load` gets one form for each failure action.
- `log::line` blocks its thread until stderr takes the line. The service
  waits when the journal does not read. A Python service waits in the same
  way.
