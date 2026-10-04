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
Code corpora are dedicated clones. `bin/provision-library.sh` namespaces a
code index as `code-<repo>`.

## Profiles

| Profile | Indexes | Skips |
|---|---|---|
| `vault` | `.md .txt .pdf` | `.index`, `review-inbox`, dot-dirs |
| `code` | `.py .ts .tsx .js .jsx .rs .go .sh .toml .yaml .yml .sql .md .txt` | `.index`, `node_modules`, `target`, `dist`, `build`, `__pycache__`, dot-dirs |

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
index-scope <corpus-path> <index-dir> [vault|code]     # TEI_URL env
./bin/provision-library.sh <name> [vault|code|test]    # image, sandbox, egress, timer
```

Live, it runs from `index@<scope>.timer` and `index-code@<repo>.timer`, every
15 minutes and every hour, inside a sandbox that mounts the corpus read-only.
A TEI preflight embeds a canary first. An unreachable embedder leaves the
corpus untouched.

The image build needs `--build-arg AGENT_LAN_ADDRESS=<address>`. A sandbox
has no site file, so the build fixes the embedder's address.

## Layout

| File | Owns |
|---|---|
| `src/library/library.py` | walk, hash, chunk, embed, write, atomic publish |
| `src/library/__main__.py` | the `index-scope` CLI, the TEI client and its preflight |
| `Dockerfile` | the sandbox image |

## Tests

```bash
uv run pytest library/tests
```

A fake embedder. No TEI, no sandbox.
