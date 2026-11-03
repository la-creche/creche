# wire

This directory holds the package `creche-wire`. The package holds the
pydantic models of the wire types of the VM owner (decision CH9-1). The root
`AGENTS.md` applies here too.

## One definition per type

The Rust crate `rust/crates/wire` defines each wire type. Its test writes
`rust/crates/wire/schema/wire.json`, the JSON Schema of every wire type.
`datamodel-code-generator` turns that schema into
`src/creche_wire/models.py`. No wire type is written by hand in Python.

| File | What it holds |
|---|---|
| `src/creche_wire/models.py` | The generated models. Never edit this file. |
| `src/creche_wire/__init__.py` | The names a caller imports. |
| `src/creche_wire/reader.py` | `read`, the one read of a wire text. |
| `src/creche_wire/strictjson.py` | A copy of the strict JSON reader. Never edit one copy alone. |
| `tests/test_wire_vectors.py` | The test that runs the shared wire vectors through `read`. |
| `tests/test_wire_read.py` | The test that shows `read` is strict. |
| `tests/test_wire_grammar.py` | The test that holds each name pattern equal to `family/grammar.py`. |

`family/grammar.py` stays the authority for a name of the family file
(decision CH23 rule 5). A wire type of such a name copies its pattern, and
`tests/test_wire_grammar.py` names the pair. The same test holds attendance's
family pattern and its chain cap equal to the wire types. The facade's
`FAMILY` and `MAX_CHAIN` are TypeScript, so no test here reaches them.

A `dispatch` trigger needs a chain of 1 to 8 names. attendance today stores
a dispatch with no chain when `POST /dispatch` carries none. Before
attendance writes the `auto` origin, a PR of its own makes attendance refuse
a missing or empty chain, at the request and in the ledger reader.

## Read a wire text

Read a wire text only with `creche_wire.read(Model, data, cap=...)`.

1. `strictjson.loads` checks the text first. It refuses two equal keys, a
   lone surrogate, `NaN`, an integer out of range and a text over the cap.
   The error is `NotStrictJson`.
2. The model then reads the text in strict mode. An int refuses `"1"`, `1.0`
   and `true`. The error is `pydantic.ValidationError`.
3. The caller gives the cap. It is the cap of the caller's surface, so the
   package has no default.

`strict-types` in `[tool.datamodel-codegen]` makes each scalar of a model
strict too. A direct `model_validate_json` then still refuses `"1"` for an
int. It does not check the rules of step 1.

`read` refuses some texts that the Rust crate reads with
`serde_json::from_str`. No wire type meets these texts today. A wire type
that does needs a decision of the owner before its vector cases are added:

1. A nesting depth of 65 to 128. The Rust limit is 128.
2. Two equal keys in an object read as a map. serde_json keeps the last one.
3. An integer past 64 bits for a float. serde_json reads it as an `f64`.
4. A text over the cap. The Rust crate has no cap.

The generated model is wider than the Rust type in two places. The test
`test_each_integer_and_date_of_the_schema_is_checked` fails when the schema
holds one of them:

1. An integer without `minimum` and `maximum`. datamodel-codegen drops the
   `format` that schemars writes, so a `u8` field would take 300.
2. A `date-time` without a `pattern`. pydantic takes dates that RFC 3339
   refuses, for example `"2026-11-02 00:00:00Z"`. It also keeps only 6
   digits of a fraction, so a date with nanoseconds cannot round-trip.

`bin/tests/test_strictjson_copies.py` fails when `strictjson.py` differs from
the copy in another package. Change each copy in one commit.

## The shared wire vectors

`rust/crates/wire/vectors/wire.json` holds, for each wire type, JSON texts
and what the Rust type does with each one. `written` is the JSON text the
type writes after it reads `json`, or `null` when the type refuses `json`.
The Rust crate (`src/vectors.rs`) and `tests/test_wire_vectors.py` read the
same file.

1. The Rust crate is the authority. A case says what the Rust type does.
   The repository's `vectors/` directory is a different oracle, whose
   authority is the Python code. These cases are not part of it.
2. A case is written by hand. Add one for each edge you find, in either
   language.
3. Never change a case to make a Python test pass. A Python failure is a
   defect of the generation.
4. Each wire type has at least one accepted case and one refused case.
5. Each wire type has one block. serde_json in the Rust test keeps only the
   last block of a type, and the strict reader of the Python test refuses
   two equal keys.

## A struct on the wire

A derived `Deserialize` of a struct also reads a JSON array, its fields in
order. `deny_unknown_fields` does not stop it. The schema and the generated
model take only an object.

1. Derive `Serialize` and `Deserialize` with `#[serde(remote = "Self")]`,
   and add the line `object_only!(Name);` in `rust/crates/wire/src/lib.rs`.
   The macro reads the struct only from a JSON object.
2. Add a refused case with the array form: `[]`, or the fields in order.
   Add the same case where an enum variant holds the struct.
3. Make the payload of an enum variant a struct type of its own. The class
   of an inline struct variant gets the name of its key, and a later type
   with the same name changes that name.

## Change a wire type

1. Change the type in `rust/crates/wire/src/`.
2. In `rust/`, run `CRECHE_WIRE_SCHEMA=write cargo test -p creche-wire`. The
   test writes `schema/wire.json`.
3. At the root of the repository, run `uv run datamodel-codegen`. It writes
   `src/creche_wire/models.py`. The `[tool.datamodel-codegen]` section of the
   root `pyproject.toml` holds its settings.
4. Export a new type from `src/creche_wire/__init__.py`.
5. Add its cases to `rust/crates/wire/vectors/wire.json`, and its line to
   `read_and_write` in `rust/crates/wire/src/vectors.rs`.
6. Commit the files together.

`bin/quality-gate.sh` runs `uv run datamodel-codegen --check`. The check
fails when `models.py` is not what the schema generates. The Rust test fails
when `wire.json` is not the schema of the Rust types.

## Who uses it

No program imports the package yet. caregiver and attendance become its
callers when they talk to the VM owner. Each one then names `creche-wire` as
a dependency.
