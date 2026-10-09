# creche-vectors

The reader of the vector files under `vectors/data`, for each differential
test of the Rust workspace. `rust/AGENTS.md` and the root `AGENTS.md` apply
here too.

A vector is one input and what the Python implementation did with it.
`vectors/README.md` holds the file format. `rust/AGENTS.md`, "The
differential test", says how a test uses the reader.

No release holds this crate. Add it to a crate only under
`[dev-dependencies]`. `creche-testkit` gives each item of this crate to its
users as `creche_testkit::vectors`.

## Layout

| Path | What it holds |
|---|---|
| `src/lib.rs` | `index` and `surface`: the rows of the index, and the vector file of one surface. The input forms, the markers and the error type. |

The doc comment of the crate lists each case in which the reader refuses a
file.

## Rules for a change here

Each rule has its reason.

1. **The crate depends on `serde` and `serde_json` only.** It can also take
   `creche-util`, when a helper of that crate replaces code here. It names
   no other crate of the workspace. `bin/tests/test_rust_workspace.py` fails
   for each other dependency.
   Reason: each crate of the workspace can take this crate for its tests,
   `creche-contracts` too. A dependency on such a crate makes a circle. cargo
   then builds that crate two times for one test run. A type of one build is
   not the type of the other build.
2. **Read each file through a raw `serde` type.** A conversion that can fail
   then makes the public type (`rust/AGENTS.md`, rule 1).
   Reason: one conversion holds each check of the file.
3. **Give each raw struct a closed set of keys, with `deny_unknown_fields`.
   Read each raw struct through `ObjectOnly`.** `object_of` reads the text of
   a file. `objects` reads a field that is a list of objects.
   Reason: the format of each file names an object and its keys. The derive
   of `serde` also reads a struct from an array that holds the values in
   order.
4. **Return `Result` from each function that can fail.** The lint gate holds
   for this crate, so no code here panics. The test calls `unwrap`.
   Reason: the caller then sees each failure in the signature.
5. **Give no struct a public field.** Give each public struct its two doc
   tests (`rust/AGENTS.md`, rule 12 and "Tests"). The public-field check of
   `bin/rust-gate.sh` reads this crate.
   Reason: a test then cannot build a row or a vector that no file holds.
6. **Write no table of differences here, and do not write the name of
   one.** `bin/tests/test_rust_workspace.py` reads each source file of this
   crate.
   Reason: `rust/AGENTS.md`, "The differential test", permits no such table.
7. **Change a rule of the index in the generator and here together.**
   `vectors/generate.py` refuses each index that this reader refuses. Two
   tables of `vectors/tests/test_vectors_frozen.py` have a twin in two tests
   of `src/lib.rs`: `FROZEN_PATHS` and `NO_FROZEN_PATHS`.
   Reason: the generator writes the index, and each Rust test reads it. With
   two rules, a file of one side stops the other side.
8. **Hold no count of a committed file in a test.** The walk of a committed
   file compares its counts with the index only.
   Reason: a change to a product package can add a vector with no change
   under `rust/`.
9. **Read no file outside `vectors/data`.** The path of that directory comes
   from `CARGO_MANIFEST_DIR`, and the crate is three levels below the
   repository root.
   Reason: the gate runs cargo only for a change under `rust/` or
   `vectors/` (`rust/AGENTS.md`, "Tests").

The reader has no Python origin. `vectors/core.py` and `vectors/generate.py`
write the files that it reads. The doc comment of each reader function names
its writer.

## Tests

- The unit tests give the reader a text that a test writes. One table for
  the index and one for a vector file hold each refusal with its reason.
- One test walks the committed files. It reads each surface of the index, so
  each count of each file is the count of its index row.
- The doc test of a public struct reads the committed files too.

## Known gaps

- The workspace holds three readers of the vector files: this crate, the
  private reader of `creche-contracts` and the reader in the test of
  `agent-family`. Packet `decisions-vectors-crate` moves the two others to
  this crate. The owner still has to confirm that one reader replaces the
  three.
- The crate reads the index and the vector files only. `vectors/data` holds
  two more files: `ids/disagreements.json` and
  `family_file.registries.json`. The private reader of `creche-contracts`
  reads the first one, and the test of `agent-family` reads the second one.
  This crate needs a reader for each of the two files before those crates
  can move to it.
- This `CONTRACT-QUESTION` comment is open in `src/lib.rs`: `rust/AGENTS.md`
  gives each value one source, in rule 13. `creche-contracts` holds the
  strict JSON reader and `ids::Sha256Hex`, and rule 1 above keeps that crate
  out of this one. The rules do not say what this crate does then. The
  reader takes this reading:
  1. The raw types of the index refuse each text that is not strict JSON.
     The doc comment of the crate says how.
  2. `is_digest` holds the form of a digest of the index. It is a second
     copy of the grammar of `ids::Sha256Hex`. A test holds `is_digest` equal
     to each vector of the surfaces `id.sha256_hex.*`. The test of `ids`
     holds that type equal to the same vectors.
  3. `ObjectOnly` does what `slot::MapOnly` of `creche-contracts` does. That
     type is private to its crate.

  A change costs one helper in `creche-util` for the digest, and a `serde`
  dependency there for the adapter. Rule 4 of `crates/creche-util/AGENTS.md`
  permits no dependency today.
- The reader holds a base64 decoder of its own. The workspace has more than
  one. Packet `decisions-util-runtime` moves them to `creche-util`.
- In three places of a vector file, the reader does not refuse a key that
  an object holds two times. `serde_json` keeps the last value of such a
  key there. The reader refuses such a key in each other object of each
  file. The generator writes no such object, and `vectors/tests` holds each
  committed file equal to the generator or to its digest. The three places
  are:
  1. The `context` of a file, and each object below it.
  2. The named arguments of an `args` input, and each object below them.
  3. Each key of a vector that is not `id`, `input` or `result`, and each
     object below such a key.
- The reader compares no digest of a frozen file. `vectors/AGENTS.md`,
  "Known gaps", has the entry.
