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
| `limits.rs` | The caps of section 4.5, the three proposals, the workflow cap of this code and the measured Forgejo limit. A compile-time assertion holds the worst tag message below the cap. | 4.5 |
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
2. The content and the size of a cosign v3 bundle with a key and no log
   entry (M7). The crate keeps the bundle opaque and caps it at 4096 bytes.
3. That a Forgejo rerun keeps the run id, and that it gives the run a
   status that is not finished. The journal refuses a second statement for
   the same run id and for the same commit. So a rerun with a new run id
   gets no second statement either.
4. The sha256 object format of git. The crate refuses a commit id that is
   not 40 hex digits.

## Known gaps

Each item is a `CONTRACT-QUESTION:` in the code.

1. `exchange.rs`: section 4.5 gives only the content of the request and of
   the answer. The reading is one strict JSON line each, with `v` 1.
2. `forgejo.rs`: the status words of an unfinished run (see "Not measured").
3. `statement.rs`, `Branch`: the statement spells the ref `main`, as the
   example of section 4.5 and the P1 row of section 5 do.
4. `statement.rs`, `Tags`: section 4.5 gives no order and says nothing of an
   empty list or of two tags of one component. The reading sorts by name,
   refuses an empty list and refuses two tags of one component.
5. `names.rs`: a version number has at most 9 digits and no leading zero.
   So this grammar is a strict subset of `_TAG_RE` of
   `handover/executor/source.py`. PR 4 can tighten `_TAG_RE` to the same
   grammar.
6. `statement.rs`, `Tags`: section 4.5 step 3 says that a `venv` or `units`
   tag has no asset. The signer has no catalog, so it checks neither that
   nor that a component belongs to the repository. Root's P1 refuses both.
7. `names.rs`, `CommitSha`: a repository with the sha256 object format
   fails closed.
8. `message.rs`, `Bundle`: opaque bytes, 1 to 4096 (see "Not measured").
9. `eligibility.rs`: the open line carries the API id of the run, not the
   `index_in_repo`. A reply with another id or repository is refused.
10. `issue.rs`: section 4.3 item 7 does not say where the journal lives.
    The reading gives the line a strict text form. The program must append
    and sync the line before it calls cosign.
11. `issue.rs`: section 4.6 names one statement for each run. The reading
    also refuses a second statement for the same commit, because a rerun
    can get a new run id (see "Not measured").
12. The three proposals of section 4.5 (64-byte tag and asset names, a
    4096-byte bundle, a 120000-byte message) are constants in `limits.rs`.
    The operator has not accepted them yet.
13. `limits.rs`, `MAX_WORKFLOW_BYTES`: the note sets no cap on a workflow
    file name. The code caps it at 64 bytes, its own reading and not part
    of proposal 1.
