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

## Layout

| Path | Holds |
|---|---|
| `generate.py` | the program that writes every file under `data/` |
| `core.py` | the file format: the normalizer and the renderer |
| `surfaces/` | one module per group of surfaces, with the written inputs |
| `data/index.json` | one row per surface: name, path, entry point, counts |
| `data/ids/` | the id grammars, one file per copy of a grammar |
| `data/ids/disagreements.json` | each input for which two copies of one grammar give different results |
| `data/family_file.json`, `data/family_file.host.json` | `family.yaml` to its validation report |
| `data/channel/` | the channel protocol: `parse`, `frame`, `build` |
| `data/chaperone/` | the grant file and its writer, the call body, the approval body, the two logs, the reasons, the verb catalog |
| `data/status/` | the status document, one file per reader |
| `tests/` | the test that holds `data/` equal to the generator, and the tests of the generator |

## Regenerate

Run every command from the repository root.

```bash
uv run python -m vectors.generate            # write vectors/data/
uv run python -m vectors.generate --check    # write nothing, exit 1 when a file differs
uv run python -m vectors.generate --counts   # print the vectors of each surface
```

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
| `issues`, `status` | on `family_file` | the validation report |
| `http_status` | on the two body surfaces and on `chaperone.reason` | the HTTP status of the answer. On a refused vector of a body surface it is inside `refusal`. |
| `output` | on a surface that writes bytes | the exact bytes that the Python code writes, as an input form |
| `file` | on the two log surfaces of `data/chaperone/` | the name of the file that takes the line |
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
| `{"$entries": [[key, value], ...]}` | a mapping with a key that is not a plain string |
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

A Rust test that differs from a vector on purpose names the vector id and
the decision. Do not delete the vector.

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

`vectors/AGENTS.md` holds the rules for an edit and the known gaps.
