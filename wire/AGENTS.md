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
| `tests/test_wire_models.py` | The cases that hold the models to the Rust parser. |

## Change a wire type

1. Change the type in `rust/crates/wire/src/`.
2. In `rust/`, run `CRECHE_WIRE_SCHEMA=write cargo test -p creche-wire`. The
   test writes `schema/wire.json`.
3. At the root of the repository, run `uv run datamodel-codegen`. It writes
   `src/creche_wire/models.py`. The `[tool.datamodel-codegen]` section of the
   root `pyproject.toml` holds its settings.
4. Export a new type from `src/creche_wire/__init__.py`.
5. Commit the three files together.

`bin/quality-gate.sh` runs `uv run datamodel-codegen --check`. The check
fails when `models.py` is not what the schema generates. The Rust test fails
when `wire.json` is not the schema of the Rust types.

## Who uses it

No program imports the package yet. caregiver and attendance become its
callers when they talk to the VM owner. Each one then names `creche-wire` as
a dependency.
