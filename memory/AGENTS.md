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
| `src/creche_memory/sync.py` | `Syncer`: the checks of the vault and the syncs into the store. `failed_extractions`: the items of a partial sync. `_cognify` compacts the vector store during the run and after a partial run. |
| `src/creche_memory/logs.py` | `plain_console`: one plain journal line for each Cognee record at WARNING and above. |
| `src/creche_memory/graph.py` | The graph check: it tells a dead graph database from one that opens. The engine check: it tells a graph that another engine built. |
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
| `tests/test_memory_sync.py` | The tests of the sync: the folders, the stamp, the quiet time, a write, a failure, the wait after failures, a dead graph and a partial sync. Four syncs through Cognee against a fake model: one note that fits no schema, with the compaction after it, a run with compactions during it, an auth error and a budget error. |
| `tests/memory_model_helpers.py` | A fake model server for the chat and embedding routes, a tokenizer in the test's cache, and a sync through Cognee in a child process. |
| `tests/test_memory_logs.py` | The tests of the journal lines: one plain line for a Cognee record, and the file log that keeps the traceback. |
| `tests/test_memory_graph.py` | The tests of the graph check and the engine check, on real ladybug databases. The checkpoints of one open under a small cap. |
| `tests/test_memory_stop.py` | The tests of the stop: a SIGTERM in a checkpoint, a kill of every process in a checkpoint, a sync that ignores its cancel, the unit lines and the daemon thread. |
| `tests/memory_graph_helpers.py` | Real ladybug databases for the tests: a clean one, a dead one and one that recovers, each with the mark of the engine. A writer through Cognee that gets a SIGTERM or a kill in a checkpoint, and one that ignores its first cancel. A writer through Cognee that checkpoints many times in one open. |
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
6. `configure` adds a row for `openai/fast` to LiteLLM's model table: the
   model takes a JSON schema (`LLM_MODEL_INFO`). `fast` is an alias of the
   LiteLLM proxy, and the table of the library does not hold it. Without
   the row, Cognee asks for a plain JSON object and puts the schema in the
   prompt only. On 2026-11-15 one chunk of
   `docs/creche/docs/design/cloud-hypervisor-playpen.md` lost
   `relationship_name` on 2 edges in each of 3 tries, in every sync. That
   failed every sync, so search stayed `not_ready`. With the row, Cognee
   sends the schema as `response_format`, and the provider holds the model
   to it.

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
   starts the syncer and the socket server. SIGTERM or SIGINT stops the
   service as "The stop" says, and the command exits 0.
2. `creche-memory --check` reads the config and checks the host. It opens
   no socket. The unit runs it as `ExecStartPre`.
3. A config or a host that stops the start exits 78. A dead graph database
   exits 78 too, at the start or after a failed sync, and so does a graph
   that another engine built ("The engine check"). The unit holds
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
3. A search before the first good or partial sync gets `not_ready`.
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
5. A failed sync prints one line: the type of the error, because a message
   can hold a URL, and the count of failed syncs in a row. Then the loop
   checks the graph database. A dead graph ends the loop and stops the
   service with exit 78. An error of the check itself ends the loop too,
   with exit 75. `ready` is set after the first sync that ends well or in
   part ("The partial sync").
6. After any other failed sync, the loop waits before the next sync. The
   wait is 30 s after one failed sync, and it doubles with each failed
   sync in a row, up to 30 minutes. The line of the failed sync names the
   wait. A write in the wait syncs after the wait. The first sync that
   ends well names the count of failed syncs before it and resets it.
7. The sync adds the notes, then the PDFs, then calls `cognify` once, with
   batches of one note and four chunks.

## The partial sync

On 2026-11-15, the model's answer for one dense note never fitted the
extraction schema, in each of Cognee's 3 tries, in each sync. By default,
Cognee 1.6.6 then stops the run and raises `CognifyFailedError`. So every
sync failed, and search stayed `not_ready`.

1. `configure` sets `RAISE_INCREMENTAL_LOADING_ERRORS=false`. A failed item
   then does not stop the run. The other items go on, and the run records
   each failed item with its error.
2. The sync calls `cognify` with `raise_on_error=False`. Then a run with
   failed items returns them. An error of the run itself still raises, for
   example a failed connection test before the first sync of a process.
3. `failed_extractions` reads the run. A failed item counts when its error
   is a structured-output validation failure, by Cognee's own test
   (`is_structured_output_validation_error`): the model answered, and no
   answer fitted the schema. Any other failed item fails the sync with
   `CognifyFailedError`, as before. An auth, a budget, a quota or a
   content-policy error is one of these. A failed step after the run
   (`AfterRunHookError`) fails the sync too.
4. A sync whose failed items all count is a partial sync. It sets `ready`,
   and it prints one line:
   `memory: sync done in <s> s; <n> note(s) or PDF(s) failed graph extraction and sync again at the next change`.
   It resets the count of failed syncs, and no wait follows it.
5. A partial sync keeps the vault stamp. So its failed items sync again at
   the next change of the vault, not at each check.
6. After a partial run, the sync compacts the vector store once
   ("The compaction").

What Cognee 1.6.6 does with a partial run, measured with a fake model on
2026-11-15 (`test_a_sync_through_cognee_counts_the_note_that_fits_no_schema`):

1. The chunks of every item stay in the store, the failed item's too. So
   search finds the text of each note.
2. Cognee rolls back the graph of the whole run, not only the graph of the
   failed item. It clears the completion mark of each item of the run. So
   the next sync extracts each of those items again, and the graph holds
   only the items of the earlier runs that ended well.
3. So one `cognify` takes the notes and the PDFs together. With one
   `remember` for the notes and one for the PDFs, the second call extracted
   each item of the first call again.
4. A partial sync costs one extraction of each item that waits for one, at
   each change of the vault.
5. With rule 1, an auth or a budget error that starts in a sync fails each
   item that is left, one at a time. Before rule 1, the first failed item
   stopped the run. Cognee does not try an auth or a budget error again.
6. Without rule 6, a partial run leaves 3 fragments in a table of the
   test's store. With rule 6 it leaves 1.

## The compaction

Each upsert adds a fragment to a LanceDB table of the vector store, and
each later upsert reads all of them. Cognee 1.6.6 compacts the store only
after a run that completed (`after_run_completed`).

On 2026-11-16 no run had completed on the host, so LanceDB had never
compacted the store. `EdgeType_relationship_name` held 3223 fragments in
805 MB. In one sync, the graph write of one item grew from 15 s to 16
minutes. The service sat idle while the LanceDB worker read that table.

1. During each `cognify`, the sync compacts the store every
   `COMPACT_EVERY_S` (120 s) with Cognee's `compact_vector_store`.
2. After a partial run, the sync compacts the store once more. Cognee does
   that itself after a completed run.
3. A pass rewrites at most 4 groups of fragments
   (`DEFAULT_MAX_TASKS_PER_RUN`). So a large backlog drains over several
   passes.
4. The rewrite takes the lock of each upsert (`VECTOR_DB_LOCK`), so a pass
   and an upsert never write together. A pass that starts while another
   one runs returns at once.
5. A compaction that fails fails the sync, as it does after a completed
   run.

`test_a_sync_through_cognee_compacts_the_store_during_the_run` runs passes
every 0.2 s beside the upserts of a real Cognee run. The run completes, and
search finds each note.

## The journal

From 2026-11-04 to 2026-11-09, a dead graph failed every sync, and the
service of that time did not stop on it ("The dead graph"). A sync failed
every 8 to 9 minutes and printed about 20,000 journal lines. On 2026-11-08
the service wrote 3.5 million lines, and from 2026-11-08 to 2026-11-15 it
wrote 2.53 GB, 94% of the host's syslog. Most lines came from Cognee, not
from this package.

1. Cognee 1.6.6 sets up the root logger at its first import. Its console
   handler forces colour codes, writes an empty line before each record,
   and draws a rich traceback box for each record with an exception. Its
   orphan cleanup logged one box for each document of the vault, at each
   sync.
2. The command imports Cognee once, after `configure`, and swaps that
   handler (`logs.py`). The new handler writes one plain line for each
   record at WARNING and above: `cognee <level>: <event> (<error type>)
   [<logger>]`. It never writes colour, the message of an exception or a
   traceback. An event text over 300 characters is cut.
3. Cognee's file handler stays as it is. Every record at INFO and above,
   with its whole traceback, goes to the rotating files in `<store>/logs`.
4. The records of the import itself come before the swap: the lines of
   each start keep Cognee's form.
5. A Cognee whose import leaves other than one console handler stops the
   start with exit 78. `test_memory_logs.py` proves the swap on the pinned
   Cognee.
6. The wait after failed syncs ("The sync", rule 6) bounds the syncs that
   fail to about two each hour.

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
   2026-11-03). The sync prints how many PDFs it cut.
4. A PDF with no text, such as a scan that the OCR job has not processed, is
   not a document. A PDF that cannot be read keeps what the store holds. A
   removed PDF is forgotten.
5. The text of each PDF is kept while its size and time stay the same. The
   reads run in a worker thread before `remember`, so the store loop keeps
   answering searches.
6. The vault stamp counts PDFs too, so a new PDF starts a sync.

## The dead graph

Cognee keeps its knowledge graph in one ladybug database file,
`<store>/system/databases/cognee_graph_ladybug`. Ladybug 0.19.0 left the
file in a state that no open repairs after a kill in a checkpoint, and
after a checkpoint that followed a refusal of its size cap ("The size
cap"). Ladybug 0.22.3 did neither in the tests of 2026-11-17 ("The stop").
A file in that state still fails every sync and every search, so the check
stays. Cognee's own repair removes only the `.wal` file, so it cannot fix
this state.

1. The graph check asks Cognee's cached engine `RETURN 1`, at the start
   and after a failed sync. The first call into Cognee opens the database,
   and later calls use the same open. The check never closes the engine,
   and `RETURN 1` writes nothing. The first call builds Cognee's engine,
   and that build writes (gap 4). The checks after a failed sync use the
   cached engine and write nothing.
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
   is its default. So a graph dies in a sync or in a kill, and the next
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

## The engine check

Ladybug 0.21 changed how it hashes text keys, and it kept its storage
version. So ladybug 0.22.3 opens a file of ladybug 0.19.0, and a lookup of
a key with non-ASCII text can miss with no error
([nestweaver#452](https://github.com/Kehl-io/nestweaver/pull/452)). The
check refuses such a file before the first open.

1. The mark is the file `<db>.engine` beside the database. It holds the
   engine of the service, for example `ladybug 0.22.3`.
2. Before the first open, `check_engine` reads the mark. With no database
   file, the first open builds one, so the check writes the mark first.
3. A database with no mark, or with the mark of another engine, stops the
   start with exit 78 and one line: the engine that built it, the engine
   of the service, and the recovery of "The dead graph".
4. So each change of the ladybug version needs a rebuild of the store. A
   rebuild extracts each note and PDF again through the model.
5. The check writes no mark beside a file that it did not see built, and
   it never moves or removes a file.

## The stop

1. The unit sends SIGTERM to the main process only (`KillMode=mixed`). So
   no worker of Cognee dies before the main process, and Cognee starts no
   new worker during the stop.
2. The command sets its signal handler before the first call into Cognee.
   A SIGTERM in the start check waits for the end of the check.
3. The stop closes the socket, then cancels every task of the store loop
   and waits at most 10 s for them. A task that still runs then prints one
   line: `a task of the store loop still ran 10 s after the stop. The
   service exits without it.`
4. Then the stop ends the process at once (`os._exit`). The exit ends
   Cognee's workers, by `PR_SET_PDEATHSIG` on Linux and by Cognee's parent
   watchdog on macOS. A normal exit hangs on Linux: the end of the loop
   thread kills the graph worker, and Cognee starts a new worker that the
   exit waits on.
5. The stop writes nothing to the graph database. The next start replays
   its WAL.
6. The PDF reads run in a daemon thread, so the exit does not wait for them.
   systemd kills a `pdftext.py` child that still runs after the main process
   exits. The child only reads.
7. `TimeoutStopSec=30` bounds the stop.

Why the stop writes nothing: on 2026-11-17 a writer committed statements
of 200,000 rows and checkpointed after each one. A SIGKILL came 0 to 30 ms
into its first, second or third checkpoint. Then a new process opened the
file and counted the rows.

| ladybug | macOS | Linux (arm64, in a container) |
|---|---|---|
| 0.19.0 | 3 of 40 files did not open | 8 of 60 files did not open |
| 0.22.3 | 40 of 40 opened, with every committed row | 60 of 60 opened, with every committed row |

The tests of the stop run the same kill through Cognee's engine, with the
stop of the service and with a kill of every process of the group.

Until 2026-11-17, with ladybug 0.19.0, the stop waited for the statement
of the graph worker, checkpointed and closed the database. It wrote
nothing after a failed checkpoint, and the unit gave it 480 s. The kills
above made that stop necessary, and ladybug 0.22.3 removed the cause.
## The size cap

Ladybug 0.19.0 never gave back the buffer frames of the shadow file of a
checkpoint ([ladybug#924](https://github.com/LadybugDB/ladybug/issues/924)).
`KUZU_MAX_DB_SIZE` capped the sum of these frames over the checkpoints of
one open, not the size of the file. Cognee checkpoints after each
`add_nodes`. At the cap of 32 GiB, each statement failed: `Maximum
database size of 34359738368 bytes has been reached`. Then the next
checkpoint segfaulted
([ladybug#1386](https://github.com/LadybugDB/ladybug/issues/1386)), and the
WAL failed its checksum at each open after it.

1. On 2026-11-15 one open reached the cap after 2 h 7 min. On 2026-11-16
   the first sync of a rebuilt store reached it at 22:07 UTC, after 2 h 27
   min of one sync, with a graph file of 319 MB. The worker segfaulted 11 s
   later. The service between those days reopened the graph after each
   sync, and one long sync still reached the cap.
2. Ladybug 0.21.0 reuses the frames (ladybug#925), and ladybug 0.22.1 no
   longer segfaults after a refusal (ladybug#1389). Cognee 1.6.8 pins
   ladybug 0.22.3, and its default cap is 128 GiB. The cap is address
   space, not memory or disk.
3. `test_the_checkpoints_of_one_open_stay_under_a_small_cap` runs 30
   checkpoints of one open of a 2 MiB file under a cap of 64 MiB. Ladybug
   0.19.0 failed at about the seventh.
4. The buffer pool is memory. Cognee's default is 32 GiB, and Cognee does
   not size it to the unit. `configure` sets it to 2 GiB
   (`GRAPH_BUFFER_POOL_BYTES`). The largest graph file of the host was 448
   MB.
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
dead-graph check. Gap 5 and gap 7 belong to the exit 75 of the check.

2. Cognee's open removes `<db>.wal` when ladybug's open error names the
   WAL. Then it opens again. A WAL file can hold committed writes.
3. After a failed checkpoint, ladybug 0.22.3 can skip the tables that the
   failed one had finished at the next checkpoint. Their new keys, or their
   new rows, are then lost after a reopen
   ([ladybug#1406](https://github.com/LadybugDB/ladybug/issues/1406), with
   its fix in draft). Cognee checkpoints again after the next write. With
   the cap of 128 GiB, a full disk is the likely cause of a failed
   checkpoint on the host.
4. The first check of a process builds Cognee's engine. In subprocess
   mode, the default, that build runs three `CREATE ... IF NOT EXISTS`
   statements. Each one commits a write, and the commit can run ladybug's
   auto-checkpoint inside the start check. A SIGTERM waits for the end of
   the check.
5. An exit-75 loop never reaches the `failed` state, so the watchdog's
   `is-failed` check does not see it. The journal line names the cause.
6. The OOM killer at `MemoryMax=12G` picks the largest process of the
   unit. A kill ends the sync that runs. `MemoryHigh=8G` throttles first
   and kills nothing, and `MemorySwapMax=2G` keeps the unit's swap off the
   host's 8 GiB. The warning is the `high` count in the cgroup's
   `memory.events`. No watcher reads it yet. The operator reads it with
   `cat /sys/fs/cgroup/user.slice/user-1000.slice/user@1000.service/app.slice/creche-memory.service/memory.events`.
   The unit comment holds the measurements of 2026-11-15. On 2026-11-15 at
   17:00 UTC the vector worker held 6.5 GiB of RSS. The cause of that is
   not known. The buffer pool of 2 GiB is new on 2026-11-17, and the
   footprint with it is not measured yet.
7. Some dead files make ladybug raise `MemoryError('std::bad_alloc')`. One
   such file is a database whose bytes after the first 4096 are garbage.
   The check passes a `MemoryError` up, because host memory pressure
   raises it too. So the service exits 75 on every start, and gap 5 hides
   the loop from the watchdog. The journal line names only the type,
   `MemoryError`.
8. The exit of the stop ends the LanceDB worker of the vector store
   without a close. No test of this change stops that worker in a write.
