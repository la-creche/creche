# vectors

This directory records what the Python implementation accepts, refuses and
writes. The record is data. A test in another language reads the data and
compares its own result.

A vector is one input and what the Python code did with that input. A surface
is one entry point of the Python code. One surface has one vector file.

## The rule of authority

The Python implementation is the authority for a surface until a Rust
release replaces that surface.

1. A vector records what the Python code does. It does not record what a
   contract says.
2. A vector with the result `raised` records a defect of the Python code.
   The Rust code must not raise there. Refuse the input. No committed file
   holds such a vector. Rule 5 of `vectors/AGENTS.md` states why.
3. When a Rust release replaces a surface, the Rust code becomes the
   authority. Change the generator in the same pull request.
4. When the Python origin of a surface leaves this repository, the file of
   that surface becomes a frozen file. "A frozen file" below has the steps.

## Layout

| Path | Holds |
|---|---|
| `generate.py` | the program that writes every file under `data/` |
| `core.py` | the file format: the normalizer and the renderer |
| `surfaces/` | one module per group of surfaces, with the written inputs |
| `data/index.json` | one row per surface: name, path, entry point, counts. One line per frozen file: path and digest |
| `data/ids/` | the id grammars, one file per copy of a grammar |
| `data/ids/disagreements.json` | each input for which two copies of one grammar give different results |
| `data/family_file.json`, `data/family_file.host.json` | `family.yaml` to its validation report |
| `data/server_file.json` | `server.yaml` to its validation report |
| `data/family_file.classify.json` | two family files to the diff between them |
| `data/family_file.cli.json` | a command line of `agent-family` to its output |
| `data/family_file.registries.json` | each file of each registry that a vector names |
| `data/channel/` | the channel protocol: `parse`, `frame`, `build` |
| `data/chaperone/` | the grant file and its writer, the call body, the approval body, the two logs, the reasons, the verb catalog |
| `data/status/` | the status document, one file per reader. The writer of the status document, the fault files and the outcome record |
| `data/config/` | the configs: the site file, the roster, `runtime.json`, `creds.json`, the env file of the playpen and three env readers |
| `data/manifest/` | the component manifest, the release request, the live-state document and the resolved manifest |
| `data/session/` | the session API: the request bodies, the queries, the error body, the journal and the event stream |
| `data/runtime/` | the helper code that each service copies: the lenient field readers, the token files, the bearer of a request and the answers of the web framework |
| `tests/` | the test that holds `data/` equal to the generator, and the tests of the generator |

## Regenerate

Run every command from the repository root.

```bash
uv run python -m vectors.generate                    # write vectors/data/
uv run python -m vectors.generate --check            # write nothing, exit 1 when a file differs
uv run python -m vectors.generate --counts           # print the vectors of each surface
uv run python -m vectors.generate --freeze <path>    # freeze a file that no group writes
```

The generator takes one of the three flags, or no flag.

The generator reads and writes only `*.json` files under `data/`. `--check`
reports each other file there as `left over`. Remove that file. The generator
does not write through a symbolic link.

`vectors/tests/test_vectors_current.py` runs the generator in memory. It fails
when a committed file differs. The full suite runs it, and CI runs the full
suite.

The pre-push hook runs this test for a change under a product package and
for a change under `vectors/`.

The Rust tests read `data/`. CI runs them for each code change under
`vectors/`. The pre-push hook runs them for such a change when `cargo` is on
`PATH`. Without `cargo`, the hook prints one line and passes.

When a change to a product package moves behavior:

1. Run `uv run python -m vectors.generate`.
2. Read the diff of `vectors/data/`. Each changed line is a change in what
   the platform accepts.
3. Commit the changed vectors in the commit that changes the behavior.

## A frozen file

A frozen file is a file under `data/` whose Python origin left this
repository. No group of the generator writes it again. The index holds the
SHA-256 of its bytes in the map `frozen`. "The index file" below has the
form of that map.

- The generator does not write a frozen file and does not remove one.
- The generator makes the index row of a frozen surface from its file.
- `--check` compares the bytes of each frozen file with its digest. It
  prints the line `frozen: <path>` for each one.
- `--check` reports a frozen file with other bytes as `differs`. It reports
  an absent frozen file as `missing`. Restore that file with git. Until
  then, the generator writes no file.
- `vectors/tests/test_vectors_current.py` fails for each of the two
  problems.

A pull request that removes a Python package freezes each data file of that
package. Do these steps in this order:

1. Remove each surface of the package from the generator: its module, or
   its part of a shared module. Change no file under `data/`.
2. Run `uv run python -m vectors.generate --check`. It reports each file
   that no group writes as `left over`.
3. Run `uv run python -m vectors.generate --freeze <path> <path>`. Name
   each of those files by its path from `vectors/data/`.
4. Run `--check` again. It exits with status 0. It prints one `frozen:`
   line for each file.
5. Commit the index. The map `frozen` holds one new line for each file.
   The diff holds no frozen file.

Between step 1 and step 3, do not run the generator with no flag. That run
removes each JSON file that no group writes and that is not frozen. It
prints the line `removed: <path>` for each one. If it removed a file of the
package, restore the file with `git checkout -- vectors/data`.

In step 3, name each file that step 2 reports. `--freeze` writes nothing
while a JSON file that no group writes has no name in the call. It reports
that file as `left over` and exits with status 1. It removes no file.

`--freeze` writes each other file of the generator too. Read the diff of
`vectors/data/`. A file that a group still writes can change in that run,
for example `ids/disagreements.json`.

`--freeze` refuses these files:

- A file that a group writes.
- A file that is frozen already. A frozen file thus keeps its first digest.
- A file that is absent.
- A file that is not ASCII, and a file that is no JSON object.
- A file with no `kind` that is not a vector file of format 1.

The index is the one home of the map. The generator reads the map from the
committed index and writes it again. It stops when `index.json` is absent
or does not give the map. A directory with no JSON file has no frozen file.
When `index.json` has a merge conflict:

1. Take the `index.json` of the base branch.
2. Run the generator. If your branch froze a file, give each such file to
   `--freeze` in this run. A run with no flag removes those files.
3. Run `--check`. It prints one `frozen:` line for each frozen file of the
   two branches.

An index that lacks a line of the map makes that file a `left over` file.
Do not run the generator with no flag then. Do the three steps above.

## The index file

The index is one JSON object. It is ASCII. Its keys are in sorted order.

| Key | Meaning |
|---|---|
| `format` | the version of this format. It is `1`. |
| `frozen` | one line for each frozen file. The key is the path of the file from `data/`. The value is the SHA-256 of its bytes: 64 hexadecimal digits in lower case. The paths are in sorted order. The map is empty when no file is frozen. |
| `kind` | `index` |
| `surfaces` | one row for each surface. The built surfaces come first, in build order. The frozen surfaces follow, in the order of their paths. |

One row has seven keys:

| Key | Meaning |
|---|---|
| `surface` | the name of the surface |
| `path` | the vector file, as a path from `data/` |
| `entry` | the Python entry point that the generator called |
| `vectors` | the count of the vectors in the file |
| `accepted`, `refused`, `raised` | the count of the vectors with each result |

A path of the map `frozen` has one form:

- The parts of the path have `/` between them.
- No part is empty, `.` or `..`.
- The name of the file ends with `.json`.
- The path is not `index.json`.

A reader refuses an index with no `frozen` key. It also refuses a path or a
digest in another form. The generator and the Rust readers of `creche-testkit`
and of `creche-contracts` hold these rules.

## The vector file

A vector file is one JSON object. It is ASCII. Its keys are in sorted order.
It ends with one newline. Each vector is on one line.

| Key | Meaning |
|---|---|
| `format` | the version of this format. It is `1`. Refuse a file with another value. |
| `surface` | the name of the surface, for example `id.family_name.attendance` |
| `entry` | the Python entry point that the generator calls |
| `contract` | the design contract that the surface belongs to |
| `notes` | what a reader must know to replay a vector of this file |
| `context` | facts that every vector of the file shares |
| `vectors` | the vectors, in a fixed order |

### A vector

| Key | Present | Meaning |
|---|---|---|
| `id` | always | a name that is unique in the file |
| `input` | always | the input, in one of the five forms below |
| `params` | on some surfaces | other arguments of the entry point. The `notes` of the file name each one. |
| `result` | always | `accepted`, `refused` or `raised` |
| `value` | when the Python code parsed the input into a value | the normalized value |
| `refusal` | when the Python code gives a reason | the refusal code, or an object that holds the reason |
| `issues`, `status` | on `family_file` and on `server_file` | the validation report |
| `http_status` | on the two body surfaces, on `chaperone.reason` and on a surface of `data/session/` that writes an answer | the HTTP status of the answer. On a refused vector of a body surface or of `data/session/` it is inside `refusal`. |
| `output` | on a surface that writes bytes | the exact bytes that the Python code writes, as an input form |
| `file` | on the two log surfaces of `data/chaperone/` | the name of the file that takes the line |
| `ts_bits` | on the two request surfaces of `data/manifest/` | the `ts` of the value as the 16 hexadecimal digits of its IEEE 754 bits |
| `exception` | when `result` is `raised` | the name of the exception type |

### The three results

- `accepted`: the Python code took the input.
- `refused`: the Python code refused the input in the way its contract
  states.
- `raised`: the Python code raised an exception that its contract does not
  state. The vector keeps the exception type and no message.

### The five input forms

An `input` object has exactly one key.

| Key | The input is |
|---|---|
| `text` | this text. For an entry point that takes bytes, the input is the UTF-8 bytes of the text. |
| `base64` | these bytes. The generator uses this form only for bytes that are not UTF-8. |
| `repeat` | a long text. Each item is `[text, count]`. Repeat each text `count` times. Join the results in order. |
| `args` | the named arguments of a builder. A value can be a marker object. |
| `chunks` | the chunks of one byte stream, in order. Each chunk is a `text` or a `base64` object. |

### The normalized value

The generator makes one JSON projection of each Python value:

- An enum is its value. A dataclass and a model are objects of their fields.
- A tuple is an array. A set is a sorted array.
- A time is its ISO 8601 text, with the offset that the Python value holds.
- Every default is present.

Six values have no JSON form that every strict reader accepts. Each one is
an object with exactly one key:

| Marker | Stands for |
|---|---|
| `{"$int": "<digits>"}` | an integer below -2^63 or above 2^64 - 1 |
| `{"$float": "NaN"}`, `"Infinity"`, `"-Infinity"` | a float that is not finite |
| `{"$utf16": [<code units>]}` | a string that holds a lone surrogate |
| `{"$base64": "<bytes>"}` | bytes |
| `{"$entries": [[key, value], ...]}` | a mapping with a key that is not a plain string. On `session.error_body`, also a mapping whose keys are not in sorted order. The pairs are in the order of the mapping. |
| `{"$json": "<text>"}` | a field that nests deeper than 96 levels. The text is the JSON of the field. |

The generator writes the `$json` marker for a whole field of a vector, for
example `value` or `refusal`. The text can hold the other markers. Parse the
text with a reader that has no nesting limit.

No file nests deeper than 100 levels. No string in a file holds a lone
surrogate. A reader that stops at 128 levels reads every file. `serde_json`
with its default settings is such a reader.

## How a Rust test reads a vector

1. Read `vectors/data/index.json`. Find the row of the surface.
2. Read the file that the `path` of the row names. Refuse a `format` that is
   not `1`.
3. Read the `notes` of the file once. They state how to replay the surface.
4. For each vector, build the input from its form and its `params`.
5. Call the Rust code.
6. Compare the result with `result`.
7. For `accepted`, compare the parsed value with `value` as parsed JSON, not
   as text.
8. For `refused`, compare the refusal code. Compare a message only where the
   Rust code must write the same message.
9. For `raised`, make sure that the Rust code refuses the input.

A Rust test must not disagree with a vector on purpose. When a Rust result
and a vector disagree, follow "The differential test" in `rust/AGENTS.md`.

## Rules for a new surface

1. Call a public entry point of the product package. Do not change product
   code for a vector.
2. Give every copy of one grammar the same inputs.
3. Write no timestamp, no absolute path and no value of the machine into a
   vector.
4. Name no deployment in an input. Use `192.0.2.10` for an address.
5. Keep the text of an interpreter error out of a vector when the text
   differs between two Python versions. Rule 6 finds such a text.
6. Run `--check` under each Python version that the workspace supports. The
   files must be the same.

## The registry file

A vector of `family_file`, of `server_file` and of `family_file.cli` names a
registry of this repository in `params.registry`. A Rust test reads no file
outside `rust/` and `vectors/data`. `data/family_file.registries.json` thus
holds each file of each such registry.

| Key | Meaning |
|---|---|
| `format` | the version of this format. It is `1`. |
| `kind` | `registries` |
| `files` | one row for each file, in a fixed order |

One row has three keys:

| Key | Meaning |
|---|---|
| `registry` | the path of the registry from the repository root |
| `path` | the path of the file from the root of that registry |
| `text` or `base64` | the bytes of the file, as an input form |

The generator reads no file and no directory whose name starts with `.`.
git does not track such a file in a registry of this repository. The file
of a file browser thus does not change a vector.

To replay a vector:

1. Make an empty directory.
2. Write each file of the registry there.
3. Write each file of `params.files`.
4. Write the input.
5. Call the entry point.

`vectors/AGENTS.md` holds the rules for an edit and the known gaps.
