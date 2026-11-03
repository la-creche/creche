# memory

This directory holds the package `creche-memory`. The package is the memory
service of the vault. The service hosts the memory store and answers the
`memory` upstream of the `mcp` repository over one Unix socket. The root
`AGENTS.md` applies here too.

## What the package holds

| File | What it holds |
|---|---|
| `src/creche_memory/cli.py` | The command `creche-memory`, and `creche-memory --check`. |
| `src/creche_memory/verify.py` | The verify hook `creche-memory-verify` (contract 06 §4). |
| `src/creche_memory/config.py` | What the service reads from its environment, and the host check. |
| `src/creche_memory/wire.py` | The wire types of the socket: each request, each reply, the refusal codes and the newtypes. |
| `src/creche_memory/vault.py` | `Vault`: it reads one note or lists one folder, inside the vault root. |
| `src/creche_memory/writes.py` | `Writer`: it creates inbox notes and rewrites whole files in the agent areas. |
| `src/creche_memory/store.py` | `StoreSettings` and `configure`: the settings of the memory store, as the env that Cognee reads. |
| `src/creche_memory/sync.py` | `Syncer`: the checks of the vault and the syncs into the store. |
| `src/creche_memory/search.py` | `Searcher` and `StoreLoop`: the search, and the one asyncio loop in which Cognee runs. |
| `src/creche_memory/cognee_search.py` | The two Cognee searches and the map from a document to its note. pyright checks it in basic mode. |
| `src/creche_memory/server.py` | `MemoryServer` and `Service`: the socket server. It checks the peer uid and answers one line per connection. |
| `vectors/wire.json` | The shared wire vectors. |
| `tests/test_memory_config.py` | The tests of the config and of the command. One test starts the command, reads a note over its socket and stops it. |
| `tests/test_memory_verify.py` | The tests of the verify hook. |
| `tests/test_memory_wire_vectors.py` | The test that runs the shared wire vectors through the models. |
| `tests/test_memory_vault.py` | The tests of the reads: the caps, the refusals and the symlink rules. |
| `tests/test_memory_writes.py` | The tests of the writes: the areas, the digest check, the modes and the symlink rules. |
| `tests/test_memory_store.py` | The tests of the settings. One test imports Cognee in a process of its own and reads its settings back. |
| `tests/test_memory_sync.py` | The tests of the sync: the folders, the stamp, the quiet time, a write and a failure. |
| `tests/test_memory_search.py` | The tests of the search: `not_ready`, the paths, the caps, a slow search and a failed one. |
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
3. The server answers each op through one `Service`. After a write that
   ends well, it asks the syncer for a sync.
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

## The memory store

1. Cognee reads its settings from the env at its first import. Call
   `configure` before any import of Cognee. It refuses to run after one.
2. Every folder of the store is under `store_root`: the databases, the
   files, the cache and the logs. Nothing goes to the home folder.
3. Telemetry is off. The tokenizer comes from a local cache, offline.
4. The model is the `fast` alias of LiteLLM, with reasoning off, at 40
   requests per minute. The model provider limits every family on one
   account.
5. The LiteLLM key is in no repr and on no command line.

## The component

`memory/component.yaml` is the manifest of the component `memory`
(contract 06 §1 row 11 and rule 13).

1. The build is `uv sync --frozen --no-editable --no-dev --project memory`.
   It syncs `memory/uv.lock` into `~/.local/components/memory`, with no dev
   package. The tree is about 1 GB.
2. The catalog row has `Lock.OWN`, so a change to the root `uv.lock` moves
   no `memory` tag.
3. The verify hook, `creche-memory-verify --json --env-file
   /srv/agents/state/creche/memory.env`, checks the key, the folders, the
   peer user and the socket. The socket must accept a connection, so the
   unit must run before a release can pass.

## The command

1. `creche-memory` starts the store loop, the syncer and the socket server.
   SIGTERM or SIGINT stops each one in turn, and the command exits 0.
2. `creche-memory --check` reads the config and checks the host. It opens
   no socket. The unit runs it as `ExecStartPre`.
3. A config or a host that stops the start exits 78. The unit holds
   `RestartPreventExitStatus=78`, so systemd does not restart it in a loop.
4. The environment:

   | Key | Default | What it is |
   |---|---|---|
   | `MEMORY_LITELLM_KEY` | none, required | The `memory` key of LiteLLM, from the unit's mode-0600 env file. |
   | `AGENT_LITELLM_BASE_URL` | port 4000 on `AGENT_LAN_ADDRESS` | LiteLLM, with no `/v1`. |
   | `TEI_URL` | port 8085 on `AGENT_LAN_ADDRESS` | The embedding server, with no `/v1`. |
   | `MEMORY_VAULT_ROOT` | `/srv/agents/vault` | The vault. |
   | `MEMORY_STORE_ROOT` | `/srv/agents/state/memory` | The store. The command makes it, mode 0700. |
   | `MEMORY_SOCKET` | `/var/lib/creche-memory/memory.sock` | The socket. Root makes its folder. |
   | `MEMORY_PEER_USER` | `mcp-memory` | The one user whose connections the server answers. |

5. The tokenizer cache is `<store>/hf/hub`. It must exist before the start.
   The operator copies the tokenizer of the embedding model into it once.

## The search

1. A search makes no LLM call (decision MM9). It asks Cognee for the graph
   facts near the query (`GRAPH_COMPLETION` with `only_context`) and for
   the matching passages (`CHUNKS`).
2. Each passage maps to its note through the external id of its document,
   `obsidian:<path>`. A passage with no note, or with a path that the wire
   types refuse, is left out.
3. A search before the first sync ends gets `not_ready`.
4. Every call into Cognee runs in the loop of `StoreLoop`. A search from a
   connection waits at most 15 seconds, then gets `internal`. A failed
   search names only the type of the error.
5. Cognee's search results carry no full types. `cognee_search.py` is the
   one module in pyright's basic mode, and it returns plain typed values.

## The sync

1. The loop checks the vault every 30 seconds. It syncs once the vault
   stays the same for 30 seconds. After a confirmed write that changes a
   folder it reads, it syncs at the next check.
2. The store reads every folder except `house/ha` and `house/scrum`. Cognee
   takes only the folders to read, so the loop lists them at each check:
   each top-level folder, and each folder beside a skipped one.
3. A note directly in `house/`, or at the vault root, is not read.
4. A stamp of each note's path, size and time tells the loop that the
   vault changed. A change in a skipped folder does not move it.
5. A failed sync prints only the type of the error, because a message can
   hold a URL. The next check tries again. `ready` is set after the first
   sync that ends well.
6. The sync calls `remember` with batches of one note and four chunks.

## Not a workspace member

The package is not a member of the uv workspace. It pins the memory store,
`cognee`, which has a large dependency tree. That tree must not go into the
root `uv.lock`, because a change there moves every venv component
(`CONTRIBUTING.md`).

1. The package has its own lock, `memory/uv.lock`.
2. The root checks do not see the package, except ruff and ruff format.
3. The `memory` job of the gate syncs the lock, then runs pyright and pytest
   in `memory/`. `memory/pyrightconfig.json` holds the pyright settings.
   The file must stay: without it, pyright in `memory/` loads the root
   `pyrightconfig.json`. The `[tool.pytest.ini_options]` section of
   `memory/pyproject.toml` holds the pytest settings.
4. Run the same checks before a push, in `memory/`: `uv sync --locked`, then
   `uv run pyright`, then `uv run pytest`.

## Known gaps

1. No script installs the unit yet. The operator installs it once, before
   the first release of the component.
