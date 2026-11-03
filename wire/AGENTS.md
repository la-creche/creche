# wire

This directory holds the package `creche-wire`. The package holds the
pydantic models of the wire types of the VM owner (decision CH9-1). The root
`AGENTS.md` applies here too.

## One definition per type

The Rust crate `rust/crates/wire` defines each wire type. Its test writes
`rust/crates/wire/schema/wire.json`, the JSON Schema of every wire type.
`datamodel-code-generator` turns that schema into
`src/creche_wire/models.py`. No wire type is written by hand in Python.
The same test writes `agent/src/wire.ts`, a TypeScript copy of the schema,
from which TypeBox gives the static types and the runtime checks
(decision CH22). No wire type is written by hand in TypeScript.

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
   lone surrogate, `NaN`, an integer out of range, the integer `-0` and a
   text over the cap. The error is `NotStrictJson`.
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
   `FamilySkills` reads its map itself and refuses a key given twice, so it
   does not meet this text.
3. An integer past 64 bits for a float. serde_json reads it as an `f64`.
4. A text over the cap. The Rust crate has no cap.
5. The integer token `-0` for a float. serde_json reads it as the `f64`
   value -0.0.

`bin/tests/test_strictjson_copies.py` fails when `strictjson.py` differs from
the copy in another package. Change each copy in one commit.

TypeScript reads a wire text with `read` of `agent/src/wire-read.ts`, and
`agent/test/wire-vectors.test.ts` runs the shared wire vectors through it
(`agent/AGENTS.md`).

## Secrets

`LitellmKey` and `PepToken` hold secrets. The `Debug` of the Rust types
writes no value. The generated Python models do not keep this rule:

1. The `repr` of `LitellmKey`, `PepToken`, `FamilyCreds`, `FamilyState`,
   `StateReply` and `FamilyStateReply` shows the secret.
2. The text of a `ValidationError` for a refused secret shows most of it.

A Python caller never logs one of these values or such an error. No Python
caller reads them today. datamodel-codegen writes `SecretStr` for
`format: password`, but it then drops the pattern, so the models keep a
plain string.

## Integers and times

1. Every integer of a wire type has a `minimum` and a `maximum`, at most
   2^53 - 1 from zero (decision CH22 rule 3). JavaScript reads no larger
   integer exactly. datamodel-codegen drops the `format` that schemars
   writes, so the bounds are the only width check of the generated model.
2. schemars bounds an integer of 8 or 16 bits itself. `bound_integers` in
   `rust/crates/wire/src/schema.rs` gives an integer of 32 bits the bounds
   of its width. A `u64`, `i64`, `usize` or `isize` needs bounds of its own
   type, or the schema test fails. A nullable integer, with the type
   `["integer", "null"]`, follows the same rules.
3. A time on the wire is `UnixMillis`: integer milliseconds since
   1970-01-01T00:00:00Z, UTC (decision CH22-1). No wire type holds a
   `date-time` string. This rule replaces the time rule of the root
   `AGENTS.md` for wire types only.
4. `test_each_integer_and_date_of_the_schema_is_checked` holds the committed
   schema to rules 1 and 3.

`read` refuses the integer `-0` with the rule `negative_zero` (decision
CH22-3). serde_json reads `-0` as a float, so the Rust type refuses it too.
`parseStrict` of `agent/src/strict-json.ts` refuses it with the same word.
`-0.0` is a float. The Rust type and the model of an integer refuse it.
`parseStrict` refuses it at the read, as it refuses `1.0`. The `UnixMillis`
block of the shared vectors holds a case for `-0` and for `-0.0`.

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
4. Give a field that may be `null` the type `Option` and
   `#[serde(deserialize_with = "Option::deserialize")]`, and give its struct
   `schemars(transform = schema::require_each_key)`. The key is then
   required in both languages, and `null` is the one form of no value.

## An enum on the wire

Decision CH22 rule 3 sets rules 1 and 2. Decision CH31 limits rule 2 to an
enum that carries data, and makes `system_prompt` a plain string enum, as
rule 3 allows.

1. An internally tagged enum writes a unit variant as `V {}`.
2. An externally tagged enum that carries data has no unit variant. The
   variant name is the one key of an object, and its value is the payload.
   A variant that carries nothing holds an empty struct of its own.
3. An enum that carries no data may be a plain string enum. Each variant
   is a JSON string, in `snake_case`. `SystemPrompt` is one.

## A route on the wire

The vmm host API has one route table (decision CH25). It is
`rust/crates/wire/src/routes.rs`. Each route is `POST /<name>` with a JSON
body. The table pairs the route with its request type, its reply type and
its error type.

1. A route is a marker type, an empty enum named `<Name>Route`, with an
   impl of `JsonRoute`. `NAME` is in `snake_case`.
2. The macro `route_table!` is the one list of the routes. It calls a
   macro of the caller once per route. The schema test expands it, and the
   vmm router expands it later. Do not write a second list.
3. The traits carry serde bounds only. The `JsonSchema` bounds stay on the
   schema test, so a crate that depends on `creche-wire` needs no schemars.
4. The schema test writes the table into the schema, under the root key
   `x-routes`. Each type is the name of a `$defs` entry. A route type needs
   no `subschema_for` line of its own.
5. datamodel-codegen and TypeBox ignore the key `x-routes` itself. A route
   that names a new type adds that type to `$defs`. Run
   `uv run datamodel-codegen` after each change of a route.
6. A client sends `HOST_API_PROTOCOL` in the header `HOST_API_HEADER`. The
   vmm checks it before it reads the body, and the match is exact. A change
   of the schema subset that the routes reach needs a new number (decision
   CH33 rule 8). The repo holds a frozen copy of that subset per number,
   and CI fails on a change that keeps the number. That check is not built
   yet (see "Known gaps").

To add a route, add its marker and its line in `route_table!`. Then follow
the steps below.

## A route error on the wire

Each route has a typed error (decision CH26 rule 4). A caller matches it on
its `code` (decision CH25 rule 3).

1. Declare the error only with `route_error!` in `routes.rs`, as
   `Name = "code" => status` per variant. The wire form of a variant is
   `{"code":"<code>"}`. A variant carries nothing, and the macro writes it
   `V {}`.
2. Each code has one HTTP status, 4xx or 5xx (decision CH33). A status
   outside 400 to 599 fails to compile. `RouteError::STATUSES` lists each
   code with its status, and `status` gives the status of a value. The
   entry of each route in `x-routes` maps each code to its status under
   `statuses`.
3. No variant has a schema title. datamodel-codegen names each class by
   its position: `CreateError1`, `CreateError2`. A new code shifts these
   names. Export only the error type, such as `CreateError`, from
   `src/creche_wire/__init__.py`.
4. A Python caller matches `error.root.code`. Never name a class such as
   `CreateError1`.
5. Add a refused case with the array form, an unknown code and an extra
   key.

Decision CH33 rule 13 names the codes that each route shares. No error
type holds them yet (see "Known gaps").

## Change a wire type

1. Change the type in `rust/crates/wire/src/`.
2. In `rust/`, run `CRECHE_WIRE_SCHEMA=write cargo test -p creche-wire`. The
   test writes `schema/wire.json` and `agent/src/wire.ts`.
3. At the root of the repository, run `uv run datamodel-codegen`. It writes
   `src/creche_wire/models.py`. The `[tool.datamodel-codegen]` section of the
   root `pyproject.toml` holds its settings.
4. Export a new type from `src/creche_wire/__init__.py`.
5. Add its cases to `rust/crates/wire/vectors/wire.json`, and its line to
   `read_and_write` in `rust/crates/wire/src/vectors.rs`.
6. Commit the files together.

`bin/quality-gate.sh` runs `uv run datamodel-codegen --check`. The check
fails when `models.py` is not what the schema generates. The Rust test fails
when `wire.json` or `wire.ts` is not the schema of the Rust types.

## Who uses it

No program imports the package yet. caregiver and attendance become its
callers when they talk to the VM owner. Each one then names `creche-wire` as
a dependency.

## Known gaps

- `HOST_API_PROTOCOL` in `rust/crates/wire/src/routes.rs`: decided
  (decision CH33 rule 8), not built yet. A change of the schema subset
  that the routes reach needs a new number. The frozen copy per number and
  its CI check come in a later PR (PR 10 of the route table plan). Until
  then no check stops a change that keeps the number 1.
- Decision CH33 rule 13 names the codes that each route shares, in the
  CH26 check order, each with its status. No error type holds them yet.
  A later change adds them to each error type.
