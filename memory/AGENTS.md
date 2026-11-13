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
| `src/creche_memory/graph.py` | The graph check: it tells a dead graph database from one that opens. |
| `src/creche_memory/pdf.py` | The PDFs of the vault as a second source of the store: the listing, the rows, the cache of texts. |
| `src/creche_memory/pdftext.py` | The child process that reads the text of one PDF, with its limits. |
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
| `tests/test_memory_sync.py` | The tests of the sync: the folders, the stamp, the quiet time, a write, a failure and a dead graph. |
| `tests/test_memory_graph.py` | The tests of the graph check, on real ladybug databases. |
| `tests/memory_graph_helpers.py` | Real ladybug databases for the tests: a clean one, a dead one and one that recovers. The check through Cognee in a child process, after a failed checkpoint. |
| `tests/test_memory_pdf.py` | The tests of the PDFs: the listing, the rows, the cap, the cache, the child and the real source. |
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

1. `creche-memory` starts the store loop, checks the graph database, then
   starts the syncer and the socket server. SIGTERM or SIGINT stops each one
   in turn, and the command exits 0.
2. `creche-memory --check` reads the config and checks the host. It opens
   no socket. The unit runs it as `ExecStartPre`.
3. A config or a host that stops the start exits 78. A dead graph database
   exits 78 too, at the start or after a failed sync. The unit holds
   `RestartPreventExitStatus=78`, so systemd does not restart it in a loop.
   An error of the graph check that is not a verdict, or any other error
   that ends the sync loop, exits 75. systemd restarts the unit, and the
   check at the next start decides.
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
2. Each passage maps to its note or PDF through the external id of its
   document, `obsidian:<path>`. A passage with no source, or with a path that
   the wire types refuse, is left out. A passage path is a `SourcePath`: a
   `.md` note or a `.pdf`. `read` and `rewrite` still take notes only.
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
   hold a URL. Then the loop checks the graph database. A dead graph ends
   the loop and stops the service with exit 78. An error of the check
   itself ends the loop too, with exit 75. After any other failed sync,
   the next check tries again. `ready` is set after the first sync
   that ends well.
6. The sync calls `remember` with batches of one note and four chunks.

## The PDFs

The Obsidian connector reads only `.md` notes. `pdf.py` gives the store the
PDFs of the same folders, as a second `remember` after the notes.

1. Each PDF is one document, with the id `obsidian:<path>` and its file name
   as the title. Its table and sync state are `vault_pdf`. Cognee scopes its
   cleanup by source and table, so neither source forgets the other's
   documents.
2. The service never parses a PDF. `pdftext.py` reads each one in a child
   process: `python -I`, an empty env, 120 seconds, and on Linux 512 MiB of
   address space and 60 CPU seconds. A PDF over 64 MiB is not read.
3. The store takes the first 200,000 characters of each PDF (David,
   2026-11-03). The sync prints how many PDFs it cut. `index_search` still
   finds the rest of a long PDF.
4. A PDF with no text, such as a scan that the OCR job has not processed, is
   not a document. A PDF that cannot be read keeps what the store holds. A
   removed PDF is forgotten.
5. The text of each PDF is kept while its size and time stay the same. The
   reads run in a worker thread before `remember`, so the store loop keeps
   answering searches.
6. The vault stamp counts PDFs too, so a new PDF starts a sync.

## The dead graph

Cognee keeps its knowledge graph in one ladybug database file,
`<store>/system/databases/cognee_graph_ladybug`. A worker process that dies
in a checkpoint can leave the file in a state that no open repairs. Then
every sync and every search fails. Cognee's own repair removes only the
`.wal` file, so it cannot fix this state.

1. The graph check asks Cognee's cached engine `RETURN 1`, at the start
   and after a failed sync. The first call into Cognee opens the database,
   and later calls use the same open. The check never closes the engine,
   and `RETURN 1` writes nothing. A checkpoint in the check can lose rows
   or leave a file that does not open. A close after a failed checkpoint
   can leave a file that does not open. The first call builds Cognee's
   engine, and that build writes (gap 6). The checks after a failed sync
   use the cached engine and write nothing.
2. Ladybug usually raises a `RuntimeError` or an `IndexError` for a file
   that does not open, and the check calls that a dead graph. Some dead
   files raise a `MemoryError` instead (gap 7). Three kinds of error
   are not a verdict: a `SubprocessTransportError` of Cognee's worker, a
   lock that another opener holds, and an error of the host. The host
   errors are a full disk, a quota, a denied permission, an operation that
   is not permitted, a read-only file system, too many open files and an
   I/O error of the device. These and any other error go up, and the
   service exits 75. For a lock or a host error, the line carries
   ladybug's text, which names the file.
3. The test is the open, not the files beside the database. A checkpoint
   that stops part way leaves files (`.shadow`, `.wal.checkpoint`, two
   `.checkpoint.*.lock` files) that the next open replays and removes.
   These files are a normal state.
4. The service checks the graph at the start, before the socket opens, and
   after each failed sync. A search does not check it. Cognee's search
   writes nothing to the graph while `ENABLE_LAST_ACCESSED` is off, which
   is its default. So a graph dies in a sync or in a stop, and the next
   check after it is the failed sync or the next start.
5. A dead graph prints one line: the database, the error, the files beside
   it before the open, and the recovery. The check lists the files before
   the open, because Cognee's open can remove `<db>.wal`. Then the service
   exits 78, and systemd holds the unit in the failed state.
6. The service code never moves or removes a file of the store. A WAL file
   can hold committed writes, so removing it can lose data that no sync
   restores.

The operator's recovery:

1. Move the whole `<store>/system/databases` folder aside. Do not move only
   the files beside the graph: the sync does not re-read work that the
   other stores already hold.
2. Start the unit. The first sync rebuilds the graph, the relational store
   and the vector store from the vault.

The time of a full rebuild is not measured. A search gets `not_ready` until
the first sync ends.

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

Gaps 2 to 4 and gap 6 come from ladybug and Cognee, not from the
dead-graph check. Gaps 5 and 7 belong to the exit 75 of the check.

2. Ladybug 0.19.0 replays a `.wal.checkpoint` only into memory, and it
   removes the file. A stop before the next successful checkpoint loses
   those rows. The fix belongs to the stop path, not to the check.
3. Cognee's open removes `<db>.wal` when ladybug's open error names the
   WAL. Then it opens again. A WAL file can hold committed writes.
4. A stop that kills the worker inside a checkpoint can leave a file that
   does not open. The unit gives a stop 20 s (`TimeoutStopSec=20`).
5. An exit-75 loop never reaches the `failed` state, so the watchdog's
   `is-failed` check does not see it. The journal line names the cause.
6. The first check of a process builds Cognee's engine. In subprocess
   mode, the default, that build runs three `CREATE ... IF NOT EXISTS`
   statements. Each one commits a write. The commit can write a `.wal`.
   After a large write that no checkpoint ended, the commit can run
   ladybug's auto-checkpoint inside the start check. A full disk or a kill
   inside that checkpoint can then lose rows or leave a file that does not
   open (gaps 2 and 4).
7. Some dead files make ladybug raise `MemoryError('std::bad_alloc')`. One
   such file is a database whose bytes after the first 4096 are garbage.
   The check passes a `MemoryError` up, because host memory pressure
   raises it too. So the service exits 75 on every start, and gap 5 hides
   the loop from the watchdog. The journal line names only the type,
   `MemoryError`.
