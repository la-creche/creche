# memory

This directory holds the package `creche-memory`. The package is the memory
service of the vault. The service hosts the memory store and answers the
`memory` upstream of the `mcp` repository over one Unix socket. The root
`AGENTS.md` applies here too.

## What the package holds

| File | What it holds |
|---|---|
| `src/creche_memory/wire.py` | The wire types of the socket: each request, each reply, the refusal codes and the newtypes. |
| `src/creche_memory/vault.py` | `Vault`: it reads one note or lists one folder, inside the vault root. |
| `src/creche_memory/writes.py` | `Writer`: it creates inbox notes and rewrites whole files in the agent areas. |
| `src/creche_memory/server.py` | `MemoryServer`: the socket server. It checks the peer uid and answers one line per connection. |
| `vectors/wire.json` | The shared wire vectors. |
| `tests/test_memory_wire_vectors.py` | The test that runs the shared wire vectors through the models. |
| `tests/test_memory_vault.py` | The tests of the reads: the caps, the refusals and the symlink rules. |
| `tests/test_memory_writes.py` | The tests of the writes: the areas, the digest check, the modes and the symlink rules. |
| `tests/test_memory_server.py` | The tests of the socket: the peer check, the line rules and the dispatch. |

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

## The socket

1. The server reads the uid of each peer (`SO_PEERCRED` on Linux). A peer
   with a uid other than the configured one gets no reply.
2. A peer sends one request line within 10 seconds. A line over 640 KiB,
   or a line with no newline, gets a refusal.
3. The server answers `read`, `list`, `create` and `rewrite`. It refuses
   `search` with `not_ready` until its change lands.
4. At start, the server removes the socket of an earlier run. Any other
   file at the path stops the start. The socket file gets mode 0660.

## Vault writes

1. `create` writes `inbox/<YYYY-MM-DD>-<slug>.md`, with the date in UTC.
   The note starts with frontmatter that names the family and the time,
   then a `# Title` line, then the body.
2. `rewrite` writes a whole file under `inbox/`, `house/ha/` or
   `house/scrum/` only. Any other path is refused.
3. A rewrite carries the SHA-256 of the file as the agent read it. A file
   that changed since then is refused. `new` writes a file that must not
   exist yet, and it makes a folder that is missing.
4. One lock holds each write. Each write goes to a temp file in the same
   folder first, then `fsync`. A new file is linked into place, so an
   existing file stops it. A rewrite renames the temp file over the old
   one.
5. Each file gets mode 0644, and each new folder 0755. The service user
   owns them.

## Vault reads

1. `Vault` opens the vault root, then each folder of the path relative to
   the folder before it, with `O_NOFOLLOW`. A symlink on the path fails the
   open. The wire types refuse a `.`, `..` or hidden part.
2. A read returns only a regular file of 512 KiB or less, in UTF-8.
3. A listing returns notes and folders only. It skips a symlink, a hidden
   entry, a file that is not a note and a name that the wire types refuse.
   A folder of more than 2,000 entries is refused.

## Not a workspace member

The package is not a member of the uv workspace. The service will need the
memory store, which has a large dependency tree. That tree must not go into
the root `uv.lock`, because a change there moves every venv component
(`CONTRIBUTING.md`). The root `pyproject.toml` names `memory/src` and
`memory/tests` for pytest, and `pyrightconfig.json` names `memory/src`. The
root venv supplies `pydantic`.

## Known gaps

1. The package has no command and no unit yet. Nothing starts the server.
2. The package has no lock of its own yet. The change that adds the memory
   store adds the lock.
