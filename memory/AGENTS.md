# memory

This directory holds the package `creche-memory`. The package is the memory
service of the vault. The service hosts the memory store and answers the
`memory` upstream of the `mcp` repository over one Unix socket. The root
`AGENTS.md` applies here too.

## What the package holds

| File | What it holds |
|---|---|
| `src/creche_memory/wire.py` | The wire types of the socket: each request, each reply, the refusal codes and the newtypes. |
| `vectors/wire.json` | The shared wire vectors. |
| `tests/test_memory_wire_vectors.py` | The test that runs the shared wire vectors through the models. |

## One definition per type

1. `wire.py` is the one definition of each wire type of the socket.
2. The upstream in the `mcp` repository uses only the standard library. It
   writes each request shape by hand and reads only `ok` and `refused` of a
   reply. The parser of this package is the judge.
3. The socket carries one JSON object per line, in UTF-8. A connection
   carries one request and one reply.

## The shared wire vectors

`vectors/wire.json` holds, for each exported type, JSON texts and what the
type does with each one. `written` is the JSON text that the type writes
after it reads `json`, or `null` when the type refuses `json`.

1. The models of `wire.py` are the authority.
2. Write each case by hand. Add one for each edge you find.
3. Do not change a case to make a test pass. A case that changes is a change
   in what the socket accepts. Say why in the commit body.
4. Each exported type has at least one accepted case and one refused case.
5. The tests of the upstream in the `mcp` repository hold the same request
   texts as their accepted cases.

## Not a workspace member

The package is not a member of the uv workspace. The service will need the
memory store, which has a large dependency tree. That tree must not go into
the root `uv.lock`, because a change there moves every venv component
(`CONTRIBUTING.md`). The root `pyproject.toml` names `memory/src` and
`memory/tests` for pytest, and `pyrightconfig.json` names `memory/src`. The
root venv supplies `pydantic`.

## Known gaps

1. The package has no socket server yet.
2. The package has no lock of its own yet. The change that adds the memory
   store adds the lock.
