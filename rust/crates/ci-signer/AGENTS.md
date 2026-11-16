# ci-signer

The crate `creche-ci-signer`: the core of the release signer `ci-signer`
(`docs/design/forgejo-release-path.md`, section 4). It is a library with no
I/O. The root `AGENTS.md` applies here too.

The crate holds the decisions and the data formats. A later PR adds the
program: the socket listener, the Forgejo client, the cosign call, the tag
creation, the unit and the catalog row.

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

## Crate calls

| Crate | Calls |
|---|---|
| `creche-ci-signer` | `creche-wire` (only `creche_wire::strict`), `serde`, `serde_json`, `sha2` |

The crate calls no other crate of this workspace. No crate of this workspace
calls it yet.

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
   is not `Clone`. The program of the next PR must hold exactly one
   `IssuedRuns`. It must read its journal with `IssuedRuns::from_journal`
   once at each start. Only then does the rule of one statement for each
   run and for each commit hold across a restart (section 4.7 step 1).
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
- `bin/tests/test_signing_pairs.py` holds each row of
  `catalog.RELEASE_WORKFLOW` in `signing-pairs.tsv`.

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
    cosign signs. This crate has no I/O and holds no path. The program of
    the next PR holds the path as its constant. `IssuedLine` gives the
    line its text form.

## Known gaps

Each item is a `CONTRACT-QUESTION:` in the code.

1. `forgejo.rs`: the status words of an unfinished run (see "Not
   measured").
2. `names.rs`, `CommitSha`: a repository with the sha256 object format
   fails closed.
