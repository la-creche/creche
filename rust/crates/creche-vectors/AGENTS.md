# creche-vectors

The one reader of the files under `vectors/data`, for each differential test
of the Rust workspace. `rust/AGENTS.md` and the root `AGENTS.md` apply here
too.

A vector is one input and what the Python implementation did with it.
`vectors/README.md` holds the file format. `rust/AGENTS.md`, "The
differential test", says how a test uses the reader.

No release holds this crate. A crate that a release holds takes this crate
only under `[dev-dependencies]`. `creche-contracts` and `agent-family` take
it in that way. `creche-testkit` takes it under `[dependencies]`, because it
gives each item of this crate to its users as `creche_testkit::vectors`.

## Layout

| Path | What it holds |
|---|---|
| `src/lib.rs` | `index` and `surface`: the rows of the index, and the vector file of one surface. The input forms, the markers and the error type. |
| `src/disagreements.rs` | `disagreements`: the rows of `ids/disagreements.json`. |
| `src/registries.rs` | `registries`: the rows of `family_file.registries.json`. |

The doc comment of each file lists each case in which its reader refuses a
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
   a file. `object` and `objects` read a field.
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
10. **Give each raw type of the index fields of four kinds only.** The
    kinds are a text, an integer with no sign, the map type `RawFrozen` and
    a list of closed raw structs. Before you add a field of another kind,
    add rows to the refusal table of the index. Add one row for each rule of
    a strict text that the new field can break. Four such rules are a key
    two times, a lone surrogate, the nesting depth and the integer range.
    Reason: this crate calls no strict reader, so the raw types hold the
    rules of `rust/AGENTS.md`, "JSON", for the index. A field of another
    kind can take a text that is not strict. For example, a `Value` and a
    plain map keep the last value of a key that an object holds two times.
11. **Put the reader of each file under `vectors/data` in this crate.** When
    a test of another crate needs a file of another kind, add its reader
    here. Give that reader a raw type, a public type and a table of
    refusals.
    Reason: no other crate reads that directory (`rust/AGENTS.md`, "The
    differential test"). A second reader in a test holds none of the rules
    above.

The reader has no Python origin. `vectors/core.py`, `vectors/generate.py` and
two modules under `vectors/surfaces/` write the files that it reads. The doc
comment of each reader function names its writer.

## Tests

- The unit tests give each reader a text that a test writes. One table for
  each kind of file holds each refusal with its reason.
- One test for each kind of file walks the committed file. The walk of the
  index reads each surface, so each count of each file is the count of its
  index row.
- The doc test of a public struct reads the committed files too.

## Known gaps

- The owner still has to confirm that this crate is the one reader of
  `vectors/data`. "Known gaps" of `rust/AGENTS.md` has the entry, and what
  to do if the owner says no.
- This `CONTRACT-QUESTION` comment is open in `src/lib.rs`: `rust/AGENTS.md`
  gives each value one source, in rule 13. `creche-contracts` holds the
  strict JSON reader and `ids::Sha256Hex`, and rule 1 above keeps that crate
  out of this one. The rules do not say what this crate does then. The
  reader takes this reading:
  1. The raw types of the index refuse each text that is not strict JSON.
     The doc comment of the crate says how. Rule 10 above keeps them so.
  2. `is_digest` holds the form of a digest of the index. It is a second
     copy of the grammar of `ids::Sha256Hex`, so it breaks rule 13 of
     `rust/AGENTS.md`. A test holds `is_digest` equal to each vector of the
     surfaces `id.sha256_hex.*`. The test of `ids` holds that type equal to
     the same vectors.
  3. `ObjectOnly` does what `slot::MapOnly` of `creche-contracts` does. That
     type is private to its crate.

  One change removes the copy of point 2. `creche-util` gets a reader of the
  hex text that its `hex::lower` writes for a digest of `sha256`. `ids` and
  this crate then call that reader. `rust/AGENTS.md`, "Where a new type
  goes", asks for the owner of `creche-contracts` before a change to `ids`.
  No packet has this change yet. The pull request of that change deletes
  `is_digest`, its test and point 2. The adapter of point 3 needs `serde`,
  and rule 4 of `crates/creche-util/AGENTS.md` permits no dependency there.
- This `CONTRACT-QUESTION` comment is open in `src/lib.rs`:
  `vectors/README.md`, "The vector file", gives the list `vectors` no least
  count. The generator refuses no surface that has no case, so it can write
  a file with no vector. `surface` refuses such a file, because a test that
  walks it compares nothing. When a committed file holds no vector, the walk
  of the committed files fails in the `rust` job. `vectors/README.md` does
  not name the rule yet. No packet has that part yet. A change costs one
  check and one test.
- The reader holds a base64 decoder of its own. The workspace has more than
  one. Packet `decisions-util-runtime` moves them to `creche-util`.
- In three places of a vector file, the reader does not refuse a key that
  an object holds two times. `serde_json` keeps the last value of such a
  key there. The reader refuses such a key in each other object of each
  file. The generator writes no such object, and `vectors/tests` holds each
  committed file equal to the generator or to its digest. No packet makes
  the reader refuse such a key in the three places yet. The three places
  are:
  1. The `context` of a file, and each object below it.
  2. The named arguments of an `args` input, and each object below them.
  3. Each key of a vector that is not `id`, `input` or `result`, and each
     object below such a key.
- The reader compares no digest of a frozen file. `vectors/AGENTS.md`,
  "Known gaps", has the entry.
- `registries` does not refuse two paths of one registry that differ only
  in letter case. A file system that ignores case keeps one file for the
  two rows. No registry of the committed file holds such a pair. No packet
  has that part yet.
