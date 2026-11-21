# ci-signer

The package `creche-ci-signer`: the release signer `ci-signer`
(`docs/design/forgejo-release-path.md`, section 4). The root `AGENTS.md`
applies here too.

1. The library is the core: the decisions and the data formats, with no
   I/O.
2. The program `ci-signer` (`src/bin/ci-signer/`) is a service on the
   attach interface of the CI runner (the runner design at commit
   `131ce43` of `infra/ci-runner`, section 6). It holds the I/O: the
   socket, the peer check, the Forgejo client, the journal, the cosign
   call and the tag creation.

```text
  ci-runner daemon ──open, conn + fd, close──► /run/ci-signer/attach.sock
                                                 │ session.rs
                 check_daemon, check(fd, slot) ──┤
                                                 ▼
  Forgejo ◄── GET runs, GET pull, POST tags ── signer.rs ──► cosign sign-blob
                                                 │
  job VM ◄──── one request line, one answer ── guest.rs
```

## Modules

| Module | What it holds | Note section |
|---|---|---|
| `names.rs` | `Owner`, `RepoName`, `RepoFullName`, `WorkflowFile`, `ReleaseTag`, `AssetName`, `Sha256Hex`, `CommitSha`, `RunId`. A value of each type is always valid. | 4.4, 4.5 step 4 |
| `limits.rs` | The caps of section 4.5, the workflow cap and the measured Forgejo limit. A compile-time assertion holds the worst tag message below the cap. | 4.5 |
| `signed_at.rs` | `SignedAt`: whole seconds of UTC, written `YYYY-MM-DDTHH:MM:SSZ`. | 4.5 |
| `statement.rs` | `Statement`, its canonical writer and `from_canonical`, its one reader. A private wire type is the serde form and the source of the schema. | FR7, 4.5, 5 |
| `base64.rs` | Base64 of RFC 4648 section 4, with a strict decode. | 4.5 |
| `message.rs` | `TagMessage` (the signer writes it, only from a `Signable`), `UnverifiedTagMessage` (a reader decodes it), `Bundle` and `MessageSource`: the three lines of a tag message. | FR8, 4.5, M3 |
| `forgejo.rs` | `Reply` and the readers of the run reply and of the commit pull reply. The one module that knows Forgejo field names. | 4.4, M5, M17 |
| `pairs.rs` | `SigningPairs`, from `signing-pairs.tsv`. | 4.3 item 6 |
| `eligibility.rs` | `decide_run` and `decide_merge`: the typestate `RunKey`, `RunChecked`, `Eligible`. | 4.4 |
| `exchange.rs` | `Request` and `Answer`: the one-line exchange with the job VM. | 4.5 steps 3, 4 and 6 |
| `issue.rs` | `IssuedRuns`, `IssuedLine`, `Signable` and `issue`: one statement for each run, and the one place that makes the bytes to sign. | 4.3 item 7, 4.5 step 6, 4.6 |
| `schema.rs` | A test only: the committed JSON Schema `schema/release-statement.json`. | 5 |

## The program

Each file is a module of the binary target `ci-signer`.

| File | What it holds | Note section |
|---|---|---|
| `main.rs` | The argv: `serve --config <path>` and `verify --json --config <path>`. The paths of the unit. The exit status 0, 1, 2 and 78. | 4.3 |
| `config.rs` | The host config `/etc/creche/ci-signer/signer.conf` and its parse: `owner`, `forgejo` and `cosign`, each once. A fault names the line or the key, never a value. | 4.3 item 6 |
| `credentials.rs` | The key path and the two tokens from `CREDENTIALS_DIRECTORY`. | 4.3 items 3 and 4 |
| `forge.rs` | `ForgejoOrigin`, `Token` and the three calls: the run, the PR of a commit, the tag creation. The one module that holds a socket to Forgejo. | 4.4, 4.5 step 7 |
| `ledger.rs` | `Ledger`: the one `IssuedRuns` and its journal `/var/lib/ci-signer/issued.tsv`. `issue` syncs the line before it gives the bytes to sign. | 4.3 item 7, FR21, FR22 |
| `cosign.rs` | `Cosign`: the empty signing config, the empty home and one `sign-blob` child for each statement. | 4.5 step 5, M7 |
| `signer.rs` | `Signer`: `decide` (the reads and the checks of section 4.4) and `sign` (the journal, cosign and each tag). | 4.4, 4.5 |
| `line.rs` | Pure: the control lines of version 2 of the exchange and the two answers. | runner 6.2 |
| `session.rs` | Linux only. One control connection: `check_daemon`, the `open` line, the answer, and `check` of each passed fd before a byte is read. | 4.3 item 5, runner 6.3 |
| `guest.rs` | One guest connection: one request line, one answer line. | 4.5 steps 3, 4 and 6 |
| `serve.rs` | Linux only. The listening socket of stdin and the accept loop. | runner 6.4 |
| `verify.rs` | The verify hook: the config, cosign, the user, the group, the key and the two tokens of the host. | 7 |
| `tests.rs`, `tests/linux.rs` | The stand-in Forgejo, the stand-in cosign and the stand-in daemon. | 8, PR 2 |

### The bounds and the caps

Each wait has a bound. Each value is a first value, a constant of its
module.

| Wait or cap | Value | Constant |
|---|---|---|
| From the accept of a control connection to the end of the answer | 9 s, inside the 10 s of the daemon | `session::ANSWER_BOUND` |
| Each read of section 4.4 | 4 s | `signer::READ_WAIT` |
| The TCP connect to Forgejo | 3 s | `forge::CONNECT_WAIT` |
| A session after `accept` | 4 h, the wall clock of 180 minutes and a margin | `session::SESSION_BOUND` |
| The poll of the pidfd of the first accepted peer | each 1 s | `session::PEER_POLL` |
| The fds of one session | 8, the cap of the daemon for one port | `session::CONN_MAX` |
| The sessions at once | 16 | `serve::SESSIONS_MAX` |
| The request line, from the conn line | 30 s | `guest::REQUEST_WAIT` |
| One whole guest exchange | 10 min | `guest::EXCHANGE_WAIT` |
| The write of the answer | 10 s | `guest::ANSWER_WAIT` |
| One cosign sign | 30 s, then the child is killed | `cosign::SIGN_WAIT` |
| Each tag creation | 10 s | `signer::TAG_WAIT` |
| A control line | 4 KiB with its LF | `line::LINE_MAX` |
| A request line | `exchange::REQUEST_MAX_BYTES` and its LF | the core |
| A reply body of Forgejo | `forgejo::REPLY_MAX_BYTES` | the core |
| The journal | 16 MiB, else the start refuses | `ledger::JOURNAL_MAX_BYTES` |

### The rules of the program

1. The session checks the peer of each control connection with
   `ci_runner_peer::check_daemon` before it reads a byte, and each passed
   fd with `ci_runner_peer::check` and the slot of the `open` line before
   it reads a byte (the README of `ci-runner-peer`). A refused fd ends the
   session. A later fd must have the cgroup id of the first one.
2. A session is keyed by its control connection. The `session` of the
   line only matches the later lines. A `conn` or `close` line of another
   session or port ends the session (section 8, PR 2).
3. The signer takes only the repository and the run id from the facts of
   the `open` line (section 4.4). It trusts no other fact.
4. The signer answers `accept` only for a run that passes the five checks
   of section 4.4 and that the ledger admits. The session then holds the
   `Eligible` run, and the first guest exchange takes it. A later one
   answers `already_issued`.
5. The guest exchange reads the whole request line before it answers,
   also for a refusal. A close with unread bytes resets the read of the
   guest on Linux, and the answer is lost.
6. `sign` runs in this order: the journal line, synced (FR21), then
   cosign, then one `POST /tags` for each tag, then the answer. A failure
   after the journal line leaves the run and the commit issued: no second
   statement, also after a restart.
7. The end of a session aborts its guest tasks and drops each fd that it
   holds: the passed fds, the queue of fds, and the pidfd.
8. No log line holds a byte of a guest, of a reply or of a secret. A
   token prints as `Token(..)`.
9. The program holds no test seam. The tests reach the layers below the
   two kernel checks of `ci-runner-peer`, which pass only on a runner host
   for a real slot unit and for the user `ci-runner`. `session::serve` is
   the session after `check_daemon`, and `guest::serve` is the exchange
   after `check`.

### The units and the host

`systemd/creche-ci-signer.socket` and `systemd/creche-ci-signer.service`
are the units. `bin/tests/test_ci_signer_units.py` holds each decided line
and each path that the program names. The operator makes the user, the
key, the tokens and the config, and installs the two units once (section
7 of the note, before the window, step 3).

### The release row

The catalog row is `CatalogRow("ci-signer", Repo.CRECHE,
"rust/crates/ci-signer", Kind.BINARY, Releases.YES, ("rust/crates/wire",
"rust/vendor/ci-runner"))`. It is in `catalog.ARRIVING`: the row lands
first, and the manifest in the next PR (`handover/AGENTS.md`, "Binary
builds", rule 11a). That PR adds this text as `component.toml` of this
folder, takes the name out of `ARRIVING`, and teaches
`test_each_component_bundles_what_its_build_installs` the Cargo path
dependencies of a binary build:

```toml
manifest_version = "0.27"
name = "ci-signer"
repo = "creche"
path = "rust/crates/ci-signer"
kind = "binary"
unit = "creche-ci-signer.service"
runs_as = "root"
build = [
  ["cargo", "install", "--locked", "--no-track", "--path", "rust/crates/ci-signer", "--bin", "ci-signer"],
]
install.to = "/opt/components/ci-signer"
install.prev = "/opt/components/ci-signer.prev"
verify.command = ["/opt/components/ci-signer/bin/ci-signer", "verify", "--json", "--config", "/etc/creche/ci-signer/signer.conf"]
verify.user = "root"
verify.timeout_s = 60
restore.mode = "automatic"
restore.keep = 3
release = true
```

Each path of the row is also under `rust`, the path of `vmm`. So each
change of this package moves the `vmm` tag too.

## Crate calls

| Target | Calls |
|---|---|
| the library | `creche-wire` (only `creche_wire::strict`), `serde`, `serde_json`, `sha2` |
| the program | the library, `ci-runner-peer` (Linux only, vendored at `rust/vendor/ci-runner/`), `rustix` (Linux only), `tokio`, `hyper`, `hyper-util`, `http-body-util`, `serde`, `serde_json` |

The library calls no I/O crate. The program is the one caller of the
library. No other crate of this workspace calls the package.

## Rules

1. Only `IssuedRuns::issue` makes a `Signable`. Its field is private to
   `issue.rs`. `issue` takes an `Eligible` value, and only `decide_merge`
   makes one. Only a `Signable` makes a `TagMessage`, and only a
   `TagMessage` makes `Answer::Signed`. `Statement` has no serde reader and
   no way to a `Signable`. Do not widen any of these. The compile-fail
   doctests of `lib.rs` hold them.
2. The statement takes the repository, the workflow, the commit and the run
   from `Eligible`. The request gives only the tags and the digests (FR7).
3. `IssuedRuns` has no empty constructor and is not `Clone`, and `Eligible`
   is not `Clone`. The program holds exactly one `IssuedRuns`, in its one
   `Ledger`. It reads its journal with `IssuedRuns::from_journal` once at
   each start. Only then does the rule of one statement for each run and
   for each commit hold across a restart (section 4.7 step 1).
4. `UnverifiedTagMessage::decode` does not parse the statement. Root gives
   the bytes to cosign first, then to its parser (section 5). Root's
   verifier is the Python reader of PR 4. The Rust reader
   `Statement::from_canonical` serves the vectors and the tests. A Rust
   verifier must first add a type that only the cosign check makes, and
   take it in place of the bytes.
5. Each reader of a Forgejo reply checks the status, the byte cap and
   `creche_wire::strict` before it reads a field.
6. Each refusal has a stable code. No text of a reply gets into a code.

## Tests

- Unit tests in each module test the grammars, the limits, the time, base64
  and the worst message.
- `tests/signer/` tests the public interface on the recordings in
  `tests/fixtures/forgejo-17.0.1/`. Each reply takes its status from the
  `index.tsv` of that directory. Its `README.md` lists each file.
- `fixtures_are_sanitized` refuses a fixture that names a deployment.
- `bin/tests/test_signing_pairs.py` holds `signing-pairs.tsv` equal to
  `catalog.RELEASE_WORKFLOW`.
- The program tests (`src/bin/ci-signer/tests.rs`) run the eligible run
  48 of the recordings to its statement, its journal line and its two tag
  creations, with a stand-in Forgejo of the recorded replies and a
  stand-in cosign. Each failed check of section 4.4 on the recordings
  gives no statement, no POST and no journal line. A failed sign keeps
  the run issued. A `POST /tags` reply that is not 201 with the name, the
  commit and the message of the request gives `fault` after one POST. A
  bad request gets its refusal. A guest that sends
  nothing is refused at `REQUEST_WAIT`, in paused time.
- The session tests (`src/bin/ci-signer/tests/linux.rs`, Linux only) put
  a stand-in daemon on a socketpair: open, accept, a conn line with an fd
  whose peer is the test process, which the peer check refuses before a
  byte is read. A conn or close line of another session or port, a
  refused open, a close, a conn with no fd, a line past 4 KiB and a
  daemon that sends nothing each end the session.
- `the_bundle_of_the_real_cosign_verifies` signs with the real cosign
  when the machine has one, and `cosign verify-blob --key
  --insecure-ignore-tlog` passes the bundle. With no cosign it prints
  `not run`.

## Vectors

`vectors/statement.json`, `vectors/tag-message.json` and
`vectors/exchange.json` hold texts and what this crate does with each one.
This crate is the authority. Root's Python reader (PR 4 of section 8) and
the guest helper (PR 5) must run the same files.

Each refused case names its reason in the field `reason`, one word of a
closed list: `too_large`, `suffix`, `lines`, `header`, `base64`,
`statement_size`, `bundle_size`, `not_json`, `field` and `not_canonical`.
The word names the first check that fails. So a reader must run its checks
in the order of `UnverifiedTagMessage::decode` and of
`Statement::from_canonical`. `tests/signer/vectors.rs` maps each error
variant to its word, with no wildcard. The two bundle errors share one
word. A new variant needs a word, and its readers change in the same PR.

The word is shared for every vector, with one known exception. The integer
`-0` and an integer past 64 bits give `field` in this crate and `not_json`
in the Python reader. Decision CH40 rule 7 is the cause:
`creche_wire::strict` passes both forms, and `strictjson.py` refuses them.
Both readers refuse these texts. The vectors therefore hold no such text.

A large text of `vectors/tag-message.json` is a recipe: a list of pairs
`[piece, count]`. The key `texts` of that file says how to expand it. So the
file holds the messages near the cap of 120000 bytes in a few lines. No
message that decode accepts reaches the cap. The caps of the statement and
of the bundle hold the largest one at 113449 bytes, and a vector holds that
message.

Do not change a vector to pass a test. A changed vector is a changed
format. Change the format on purpose, then change the vector and its
readers in the same PR.

The schema `schema/release-statement.json` comes from the types. To write
it after a change of a statement type, run
`CRECHE_CI_SIGNER_SCHEMA=write cargo test -p creche-ci-signer` in `rust/`.
The schema cannot say the canonical byte rules or the sort order. The
vectors say them. The schema of `Tags` does not express the order, the
uniqueness or the rule of one tag for each component, so a reader must
apply the canonical check (re-encode and compare) and pass the vectors.

## Not measured

Each case fails closed. The rehearsal of section 7 records the real value.

1. The `status` of a run that waits or runs (check 5). The crate passes only
   `waiting` and `running`, refuses `blocked` and each finished state, and
   treats each other text as a fault.
2. That a Forgejo rerun keeps the run id, and that it gives the run a
   status that is not finished. The journal refuses a second statement for
   the same run id and for the same commit. So a rerun with a new run id
   gets no second statement either.
3. The sha256 object format of git. The crate refuses a commit id that is
   not 40 hex digits.

## Measured: cosign v3.1.4

The records of 2026-11-16 measured cosign v3.1.4 with a P-256 key and no
log entry (section 3 M7). `vectors/cosign-3.1.4/` holds a public key, a
statement and its bundle that verify together. The private key is
deleted.

1. The bundle is 387 bytes for each signature and each statement size.
   The cap of 4096 bytes leaves 3709 bytes.
2. The bundle has the media type
   `application/vnd.dev.sigstore.bundle.v0.3+json`, a key hint, the
   SHA-256 of the statement and a signature. It has no log entry and no
   final LF.
3. Two signatures of one statement give two different bundles (ECDSA is
   randomized). Both verify.
4. The largest tag message with a real bundle is 108501 bytes, below the
   cap of 120000 bytes.

`tests/signer/cosign_vector.rs` checks these files with no cosign: the
statement is canonical, the bundle fits `Bundle`, the bundle names the
SHA-256 of the statement and of the key, and a `tag-message.json` case
holds the two as one message. The crate still keeps the bundle opaque.
cosign at root is the check.

## Decided rules

The operator decided these rules on 2026-11-16. Each was a
`CONTRACT-QUESTION:` before.

1. `limits.rs`: a tag name and an asset name hold at most 64 bytes, a
   bundle at most 4096 bytes and a tag message at most 120000 bytes
   (section 4.5).
2. `limits.rs`, `MAX_WORKFLOW_BYTES`: a workflow file name holds at most
   64 bytes.
3. `exchange.rs`: the job VM and the signer exchange one strict JSON line
   each, with the version field `v` 1.
4. `statement.rs`, `Branch`: the statement spells the ref `main`.
5. `statement.rs`, `Tags` and `Assets`: the signer sorts the tags and the
   assets by name. It refuses a duplicate, an empty tag list and two tags
   of one component.
6. `statement.rs`, `Tags`: root checks every tag of a statement against
   the catalog. The signer does not know component kinds, so it checks
   neither the rule of section 4.5 step 3 nor that a component belongs to
   the repository.
7. `names.rs`: a version number has no leading zero and at most 9 digits.
   The wiring PR (4b) gives `_TAG_RE` of `handover/executor/source.py` the
   same rule.
8. `eligibility.rs`: the run id is the API id, not the `index_in_repo` of
   the URL. A reply with another id or repository is refused.
9. `issue.rs`: one statement for each run and for each commit.
10. `issue.rs`: the journal is the file `/var/lib/ci-signer/issued.tsv`,
    owned by the signer user. The signer writes and syncs the line before
    cosign signs. The library has no I/O and holds no path. The program
    holds the path as `ledger::JOURNAL_PATH`. `IssuedLine` gives the line
    its text form.

## Known gaps

Each item is a `CONTRACT-QUESTION:` in the code.

1. `forgejo.rs`: the status words of an unfinished run (see "Not
   measured").
2. `names.rs`, `CommitSha`: a repository with the sha256 object format
   fails closed.
3. `src/bin/ci-signer/forge.rs`, `ForgejoOrigin`: the program speaks plain
   HTTP to an IP address on the LAN, as the runner does (the runner
   design, section 14, question 1). Section 4.1 of the note draws HTTPS,
   and HTTPS on the Forgejo host (question 4) comes after the install of
   the signer. A change to HTTPS adds a TLS crate and a CA file, and
   changes `ForgejoOrigin` and `exchange` only.

## Not measured in the program

Each case fails closed. The rehearsal of section 7 records it.

1. An accepted peer check. It needs a real slot unit of a runner host.
   The tests show only refusals: the test process runs in no slot unit,
   and a CI container shows `NotCgroupRoot`.
2. cosign v3.1.4 under `ProcSubset=pid` and the rest of the sandbox of
   the unit. The test of the real cosign runs it with no sandbox.
3. `systemd-analyze verify` of the two units. The job `systemd-proof` of
   the gate runs it.
4. The reply of `POST /tags` for a tag that exists, and for the tag
   protection of `*-v*` with the bot `ci-signer` on its allowlist. The
   signer requires 201 with the name, the commit and the message of its
   request, and refuses each other reply.
