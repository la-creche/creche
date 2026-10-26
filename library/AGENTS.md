# library

Builds one corpus's hybrid retrieval index: SQLite FTS5 for words, sqlite-vec
for vectors, in one file per corpus. The root `AGENTS.md` applies here too.

**Not an agent.** No LLM, no chaperone, no tools. A deterministic batch job in
a throwaway sandbox whose only egress hole is the embedder (TEI).

## Six rules

1. The content hash decides re-indexing, never mtime. Sync tools preserve
   mtimes.
2. Embedding batches stay under `EMBED_MAX_BATCH`.
3. `review-inbox/` and dotted directories are never indexed.
4. One bad file never kills the corpus. Per-file errors collect into the
   report.
5. Publication is atomic. The store is updated on `store.work.db` and
   published with `os.replace`.
6. Every vector is written twice: to `chunks_vec` and to `chunks_emb` as
   plain little-endian float32. The sandbox reads `chunks_emb` only.

An index whose recorded `embed_model` differs from the live one is rebuilt
from scratch. The reader, `playpen/bridge/search.ts`, refuses the vector half
of such an index. FTS keeps serving.

## Where the index lives

The index is derived state. It lives outside the corpus it describes, under
the state root, never inside a vault scope. Never index a working checkout.

## Profiles

| Profile | Indexes | Skips |
|---|---|---|
| `vault` | `.md .txt .pdf` | `.index`, `review-inbox`, dot-dirs |

## Store schema

```
meta(key, value)                     embed_model, dims, updated_at
files(path, hash, mtime, indexed_at)
chunks(id, path, ord, text)
chunks_fts  = fts5(text)             rowid = chunk id
chunks_vec  = vec0(embedding[768])   rowid = chunk id
chunks_emb(id, embedding)            768 float32, little-endian
```

This schema is the contract for every reader. `playpen/bridge/index-store.ts`
reads it from TypeScript. Change one side, change both. `DIMS = 768` matches
the pinned embedding model. A change of model forces a full rebuild of every
corpus.

## Run it

```bash
index-scope <corpus-path> <index-dir> [vault]          # TEI_URL env
./bin/provision-library.sh <name> [vault|test]         # image, sandbox, egress, timer
```

Live, it runs from `index@<scope>.timer` every 15 minutes, inside a sandbox
that mounts the corpus read-only. A TEI preflight embeds a canary first. An
unreachable embedder leaves the corpus untouched.

The image build needs `--build-arg AGENT_LAN_ADDRESS=<address>`. A sandbox
has no site file, so the build fixes the embedder's address.

`index-scope` sets three statuses:

| Status | Cause |
|---|---|
| 0 | The run published the store. |
| 2 | The program refuses its command line: a wrong count of words, an unknown profile, or a corpus path that is no directory. A preflight that fails has this status too. |
| 78 | The environment names no address of TEI. 78 is `EX_CONFIG`, the status of a config that is not valid. |

The program reads its command line before its environment. A run that ends
in another way has a status that is not 0. Two examples are a signal and a
store file that the program cannot read. The index unit is a `oneshot` unit
of a timer, so no unit starts the program again because of a status.

## The answers of TEI

`index-scope` reads each answer of TEI with `strictjson.loads`. The
docstring of `src/library/strictjson.py` holds the rules of a strict text.

- An answer has `ANSWER_MAX_BYTES` bytes at most. That is 16,777,216 bytes.
- For an answer that is not strict, the program writes one log line of
  level `ERROR`: `json_not_strict embedding.answer <rule>`. `<rule>` is the
  word of the rule that the answer breaks.
- The line holds no byte of the answer.
- An answer that is not strict in the preflight ends the run with status 2.
  That run publishes no store.
- An answer that is not strict for one file is an error of that file
  (rule 4). The report lists the file, and the run publishes each other
  file.

## Layout

| File | Owns |
|---|---|
| `src/library/library.py` | walk, hash, chunk, embed, write, atomic publish |
| `src/library/__main__.py` | the `index-scope` CLI, the TEI client and its preflight |
| `src/library/strictjson.py` | the strict reader and the strict writer of a JSON text. The file is a copy, and `bin/tests/test_strictjson_copies.py` holds each copy to the same bytes |
| `Dockerfile` | the sandbox image |

## Tests

```bash
uv run pytest library/tests
```

A fake embedder. No TEI, no sandbox. The tests of the TEI client give
`httpx` a mock transport, so no test opens a socket.

## Known gaps

- `strictjson.py` names the rule of the Rust check for a refused text of
  1 MiB or less. An answer of TEI can have up to `ANSWER_MAX_BYTES` bytes.
  For a longer refused answer, `strictjson.py` names the fault that the
  standard decoder finds, and the two words can differ.
- A service records a notice for the operator when the strict reader
  refuses a text. `noticeboard/src/noticeboard/notices.py` writes such a
  notice, and `bin/AGENTS.md`, "The watchdog", sends it. No rule says what
  a program in a sandbox does. `index-scope` writes the log line of "The
  answers of TEI" and no notice. Its sandbox has no state root and no hook.
