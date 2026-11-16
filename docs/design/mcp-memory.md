# mcp-memory: design

> Status as of 2026-11-14: Done. `mcp-memory` has been live since 2026-11-03 and every proof step of section 11 passed.
> Host names, addresses and accounts are placeholders. The operator's private notes hold the real values.

Status: LIVE on the host since 2026-11-03 20:00 UTC. Every proof step of section 11 passed. Approved by the operator on 2026-11-02: "Approve, start PR 1". Approval covers the D2.5 change (search by meaning, TEI stays), the operator's leave under D0.3 for the handover row PR (section 9, PR 7), and the grant to every family except vault-oracle.

Parent: [cloud-hypervisor-playpen-design.md](cloud-hypervisor-playpen-design.md), section 4 item 11 and Phase 9. The CH cut waits for `mcp-memory` (CH10-1).

Research (shared notes folder of this session group):
- `mcp-memory-oss-research.md`: 13 MCP memory servers against the four item 11 questions.
- `mcp-memory-code-map.md`: vault-oracle, the index builder, the chaperone upstream model, the memory skills.
- `mcp-memory-ingest-research.md` and `cognee-vs-mem0.md`: tools that ingest Markdown into a memory store.
- `cognee-spike-results.md`: the Cognee spike on the host.

## 1. Decisions

Each entry records the decision, the operator's words, and the date. CH4a, CH4d, CH4d-2 and CH4f in the CH note stay binding, except where an entry here says it replaces part of one.

| # | Topic | Decision | The operator's words | Date |
| - | ----- | -------- | ------------- | ---- |
| MM1 | Shape | The vault is the source that the operator keeps. A memory service ingests the vault with an LLM into its own store (SQLite or similar, outside the vault), and agents query that store. The service never writes the vault. Whether the service has a model is not a constraint: the goal is the best memory service. This replaces the CH4d sentence "`mcp-memory` has no model". | "in my mind, it should be trivial to feed markdown into a memory service and have it store the actual memories in markdown? ie, i maintain my set of markdown that i can read and make sure is updated, which <something> then intakes and creates the actual memories that an agent reads." Then: "forget this. we're past this decision, and making a new decision. whether or not mcp-memory has a model is tangential; i care about the best possible memory service" | 2026-11-02 |
| MM2 | Spike | Spike Cognee on the host as <user>, on a copy of the vault, with an existing LiteLLM key and DeepSeek V4 Flash (`fast`). Result: all five checks pass (section 3). | "Spike cognee. Is it possible to just spike it on the host where all this data and infra already exists?" Then: "Run the spike as above. You can reuse any key from LiteLLM that already exists, use deepseek v4 flash as the model" | 2026-11-02 |
| MM3 | Where agent-written memories go | A Markdown inbox in the vault. The CH4a spool and the <user> writer write each agent memory as a note. The service ingests it like any other note. The operator sees, edits and deletes agent memories in Obsidian. | "The markdown inbox" | 2026-11-02 |
| MM4 | Which memory service | Cognee, pinned to one version (1.6.6 at the spike). Agents reach it only through our own MCP tools, never through Cognee's MCP server, because that server cannot be made read-only. mem0 is dropped. | "Adopt Cognee" | 2026-11-02 |
| MM5 | What agents may write (replaces CH4b) | Agents create inbox notes, one note per memory (`inbox/<date>-<slug>.md`, with the family and date in frontmatter). Agents rewrite whole files only in agent-owned places: the inbox, `house/ha` and `house/scrum`. Every rewrite carries the hash of the file as the agent read it, and the <user> writer refuses it if the file changed since. Agents never edit the operator's notes. A correction to one of his notes becomes an inbox note that names the note and the change, and the operator makes the edit. | "Option 1" | 2026-11-02 |
| MM6 | When the store re-syncs (replaces CH4h) | The `mcp-memory` process syncs by itself. It checks vault file times every 30 s and syncs after 30 s with no further change. After a confirmed agent write it syncs at once. One process owns the Cognee store. No timer or unit is added. The operator's edits reach agents in about 1–2 minutes, and an agent's own write in about 30 s (estimates from the spike's 15–24 s syncs). | "Option 1" | 2026-11-02 |
| MM7 | Where it runs and ships | Two parts. (1) A thin `mcp-memory` chaperone upstream in the `mcp` repo: stdlib only (the repo keeps `dependencies = []`), runs as `mcp-memory`, takes the caller from the call's app ID (D1.2), and sends typed requests. (2) A `memory` service in creche, run as a <user> user unit with its own venv, restarts and memory limit. It hosts Cognee, owns the store, runs the MM6 sync, answers searches, and checks and applies every vault write: it is the CH4a <user> writer. The two talk over one Unix socket in a folder root creates once (owner <user>, group `mcp-memory`, mode 0750). This replaces the CH4a spool files. CH4a's two checks stay: the upstream checks the caller, and the <user> service checks the request again before any write. As <user>, the service reads the four 0700 `vault/docs` folders with no mode change. Cost: a creche release row (a D0.3 handover PR) and a user unit. | "Option 1" | 2026-11-02 |
| MM8 | Which folders the service ingests | The whole vault except `house/ha` and `house/scrum`. That covers the operator's notes, the `docs` mirror (all four 0700 folders, read as <user> per MM7), the rest of `house` and the inbox: about 375 KB of Markdown. The two skipped folders are generated and agent-owned. ha-review and scrum-lead still read and rewrite their files there by path (MM5), but search does not cover them. This replaces the CH4d-2 scope "the whole vault". | "Skip ha and scrum" | 2026-11-02 |
| MM9 | Search modes agents get | Raw context only. `search` returns graph facts and matching passages, each with its note path, with no LLM call (Cognee `GRAPH_COMPLETION` with `only_context=True`, plus `CHUNKS`). The calling agent reasons over the results itself. A `read` tool opens a note by path. There is no `ask` tool. One can be added later if agents struggle with raw context. So the service calls an LLM only to ingest. | First "Both modes", then: "actually i changed my mind - just do option 1, the raw context only" | 2026-11-02 |
| MM10 | LiteLLM key and model for ingest | A new LiteLLM key with the alias `memory`. It allows only the `fast` alias (DeepSeek V4.1 Flash, MiniMax M3.1 Flash from 2026-11-15, creche-release#36) and has a $5 daily budget (`budget_duration` `1d`, as caregiver sets family keys). Reasoning stays off (`reasoning_effort: none`). The `vault-oracle` key goes away with the vault-oracle family. | "New memory key" | 2026-11-02 |

## 2. Open decisions

None. Sections 4 to 12 are proposals. The operator approves them with this note.

## 3. Spike summary

See `cognee-spike-results.md`. In short:

1. An edit re-extracts only that note (2 LLM calls, 24 s).
2. A delete removes the note's own entities and keeps entities that other notes share.
3. A hit's `document_id` maps to `obsidian:<path>` with one lookup.
4. LiteLLM and TEI work with `reasoning_effort: none` and small batches at 40 requests per minute. Default batches caused Fireworks 429 errors on the shared account.
5. Raw search makes 0 LLM calls (about 4 s for chunks, 0.4–1.8 s for graph context).

Full ingest: 579 calls, 38 min, about $0.60. Store: 430 MB for 847 KB of Markdown.

An end-to-end spike on 2026-11-03 ran the real service and the real `memory-mcp` upstream over stdio on the vault copy. Each tool behaved as section 5 says. The first sync took about 30 minutes. The MM8 folders held. A write reached search after its sync, and a deleted note left search within 100 s. See `mcp-memory-e2e-spike.md` in the shared notes folder.

## 4. Architecture

### 4.1 The pieces

| Piece | Repo | Runs as | Job |
| ----- | ---- | ------- | --- |
| `memory` upstream | `mcp` (`src/agent_mcp/memory/`) | `mcp-memory`, spawned by chaperone through `chaperone-as` | Serves the five MCP tools (section 5). Takes the caller from `_meta["chaperone.app"]` (D1.2). Sends one typed request per call over the socket and returns the reply as the tool output. It never opens a vault file. |
| `memory` service | `creche` (`memory/`, its own uv project and lock) | `<user>`, user unit `creche-memory.service` | Hosts Cognee 1.6.6. Owns the store. Runs the MM6 sync. Answers search, read and list. Checks and applies every write (the CH4a <user> writer). |
| Socket | root creates its folder once | folder `<user>:mcp-memory 2750`, socket `0660` | One Unix socket, `/var/lib/creche-memory/memory.sock`. The setgid bit gives the socket the group `mcp-memory`. |
| Store | none | `<user>`, `/srv/agents/state/memory/` mode 0700 | Cognee's SQLite, LanceDB and Ladybug files. Outside the vault, so nothing in it reaches LiteSync. |
| Env file | none | `<user>`, `/srv/agents/state/creche/memory.env` mode 0600 | The `memory` LiteLLM key (MM10). The other Cognee settings are fixed in the service code (section 4.4). |

Why `/var/lib/creche-memory` and not `/srv/agents/state`: `/srv/agents/state` is `<user>:chaperone 0710` on the host (checked 2026-11-02). `chaperone-as` drops every supplementary group (`setgroups([])`), so `mcp-memory` cannot pass through it. `/var/lib/creche-handover` is the precedent for a root-made folder in `/var/lib`.

### 4.2 The life of a call

1. An agent calls `memory__search`. Chaperone checks the grant and passes the call to the upstream with the app ID in `_meta`.
2. The upstream checks its input against its schema and reads the caller. It connects to the socket and writes one request line.
3. The service reads the peer uid with `SO_PEERCRED` and refuses any uid other than `mcp-memory`. It parses the line into one wire type (section 6). Its parser is the judge, as the handover requester rule says.
4. The service answers on the same connection with one reply line, then closes it.
5. The upstream returns the reply text as the tool output. A refusal is ordinary output (`mcp` `AGENTS.md`), never `isError`.

A write goes the same way. After the service writes the file, it replies at once, then starts a sync if the file is in an ingested folder (MM6, MM8). The reply never waits for the sync.

### 4.3 Rules the upstream keeps

1. The upstream never exits because the socket fails. If the service is down, each call returns `{"refused": true, "reason": "service_down"}`. Reason: chaperone v1 restarts no upstream, and a dead upstream refuses every app that has its grant (chaperone `docs/design.md` §7.7).
2. The upstream holds no secret and needs no state folder.
3. The upstream adds a socket timeout of 20 s, under chaperone's 30 s call limit.

### 4.4 Service settings

The service sets these Cognee settings in code, from the spike (section 3). Only the key comes from the env file.

1. `TELEMETRY_DISABLED=1`, and every Cognee folder (logs, data, system, cache) under the store.
2. LLM: `LLM_PROVIDER=custom`, `LLM_MODEL=openai/fast`, endpoint `http://192.0.2.10:4000/v1`, `reasoning_effort: none`, 40 requests per minute, `data_per_batch=1`, `chunks_per_batch=4`.
3. Embeddings: TEI `BAAI/bge-base-en-v1.5` at `http://192.0.2.10:8085/v1`, 768 dims, 512 tokens, tokenizer from the TEI cache with `HF_HUB_OFFLINE=1`.
4. Ingest: Cognee's Obsidian connector, `write_disposition="merge"`, folders per MM8. The service builds the folder list from the vault's top-level folders at each sync, without `house/ha` and `house/scrum`. The spike did not test the connector's `folders` setting.
5. Unit: `Restart=on-failure`, `MemoryMax=2G`, `UMask=0077`. The service sets the socket to 0660 after it binds. The spike did not measure memory use, so the limit is a first guess to tune after the first ingest.

## 5. Tools

Each tool has a hand-written strict schema (`additionalProperties: false`, every key required). Exposed names are `memory__<tool>` (D1.3). Text from the vault is data, and the upstream marks it as such.

| Tool | Input | Output | Notes |
| ---- | ----- | ------ | ----- |
| `search` | `query` (2–512 chars), `k` (1–12) | Graph facts, then up to `k` passages. Each passage has its note path and a snippet of up to 500 chars. | MM9. No LLM call. Cognee `GRAPH_COMPLETION` with `only_context=True`, plus `CHUNKS` with references. Each `document_id` maps to its path through `obsidian:<path>`. |
| `read` | `path` | The file text, its `sha256`, its size and its modified time. | Any `.md` file in the vault, `house/ha` and `house/scrum` included. No dot folder. Cap 512 KiB. |
| `list` | `folder` | The `.md` files and subfolders of one folder, with size and modified time. | Replaces `glob` and `grep` over a mount. No dot folder. Cap 2,000 entries. |
| `create` | `slug`, `title`, `body` | The new path and its `sha256`. | MM3, MM5. Writes `inbox/<YYYY-MM-DD>-<slug>.md` with frontmatter `family` and `created` (UTC). Refuses if the file exists. `slug` is `[a-z0-9-]{1,64}`. Body cap 16 KiB. |
| `rewrite` | `path`, `text`, `if_match` | The path and its new `sha256`. | MM5. Only under `inbox/`, `house/ha/` and `house/scrum/`. `if_match` is the `sha256` from `read`, or `new` for a file that must not exist yet. Text cap 512 KiB. |

No tool deletes a file. The operator deletes notes in Obsidian. No `ask` tool (MM9).

## 6. Socket wire types

1. One definition per type: pydantic models in the creche `memory` package (`memory/src/creche_memory/wire.py`). This is the CH9-1 shape without the Rust step, because both ends are Python and no Rust program reads these types.
2. The mcp upstream cannot import pydantic (`dependencies = []`). It writes the five request shapes by hand and parses only the reply envelope (`ok` or `refused`). The service's parser is the judge.
3. Shared vectors: `memory/vectors/wire.json` in creche holds accepted and refused cases for each type. The mcp tests hold the same request texts as their accepted cases. A drift shows as a refusal in the proof (section 11).
4. Framing: one JSON object per line, UTF-8, one request per connection. A request line over 640 KiB is refused.

Types (newtypes and enums, exhaustive matches):

| Type | Shape |
| ---- | ----- |
| `Family` | Text, `[a-z][a-z0-9-]{0,62}`, the app ID. |
| `VaultPath` | Relative path, `/`-separated, ends in `.md`, no `.` or `..` part, no part that starts with `.`, at most 512 bytes. |
| `FolderPath` | The same rules without `.md`. Empty text means the vault root. |
| `Slug` | `[a-z0-9-]{1,64}`. |
| `Sha256` | 64 lowercase hex chars. |
| `IfMatch` | `Sha256` or the literal `new`. |
| `Request` | Union on `op`: `search {caller, query, k}`, `read {caller, path}`, `list {caller, folder}`, `create {caller, slug, title, body}`, `rewrite {caller, path, text, if_match}`. Every request carries `v: 1`. |
| `Refusal` | Enum: `bad_request`, `not_found`, `not_allowed_here`, `exists`, `changed`, `too_large`, `not_ready`, `internal`. `service_down` is the upstream's own, never on the wire. |
| `Reply` | `{ok: true, result}` with one result type per op, or `{ok: false, refused, reason}`. |

`not_ready` answers a search before the first ingest ends.

## 7. What the service checks before a write

CH4a's two checks stay. The upstream checks the caller (the `_meta` key is present and is a `Family`). The service checks the request again:

1. The peer uid is `mcp-memory`.
2. The request parses into one `Request` type.
3. The path is allowed for the op (section 5).
4. The service opens each folder from the vault root with `openat2` and `RESOLVE_BENEATH | RESOLVE_NO_SYMLINKS`, so no symlink leads out of the vault.
5. For `rewrite`, the `sha256` of the file on disk equals `if_match`. The service holds one write lock, so no two writes race.

Then it writes a temp file in the same folder, runs `fsync`, and renames it into place (the attendance `atomic.py` pattern). The file stays owned by <user> with mode 0644. No vault folder changes mode.

## 8. What this note replaces

| Where | What changes |
| ----- | ------------ |
| CH4a | The socket replaces the spool files. The two checks stay. Root still creates `mcp-memory` and one folder. |
| CH4b | Settled by MM3 and MM5: inbox notes, plus whole-file rewrites with `if_match` in agent-owned places. No history copy. |
| CH4d | "`mcp-memory` has no model" is replaced by MM1. The service calls an LLM to ingest, never to search (MM9). |
| CH4d-2 | "The whole vault" becomes MM8 for search. `read` and `list` still cover the whole vault. |
| CH4h | Replaced by MM6. No `.path` unit. |
| Item 4.3 (CH4c) | Settled by MM7: the service reads as <user>. No mode change and no root step for the 0700 `vault/docs` folders. |
| Item 4.10.1 | The `index-scope` builder, the `index-*` sandboxes and the `creche-library@` timers go away at the cut. Cognee replaces them. |
| Item 4.10.2 | Search is no longer words only. D2.5 says "Index search is words only". This note replaces that sentence for `mcp-memory`. No agent gets an `embed` verb. |
| Item 4.10.3 | Settled by the `read` tool. |
| Consolidation open item 2.4 | Its recommended option 1 removes TEI. Cognee needs TEI, so TEI stays. |
| Memory-writing skill | Its "append dated entries" rule and chat's "merge and rewrite" rule both give way to one note per memory through `memory__create`. |

## 9. PR list (Phase 9)

PRs are small and one idea each, opened one at a time per repo. The operator merges each one. Sizes are estimates in changed lines.

| # | Repo | PR | Size |
| - | ---- | -- | ---- |
| 1a | creche | `memory` package outside the uv workspace (a root `uv.lock` change moves every venv component): newtypes, refusal enum, shared vectors and their test. Its root `pyproject.toml` lines mint one `playpen` tag. | ~310 |
| 1b | creche | The request and reply types, with their vectors. | ~250 |
| 2 | creche | Socket server: `SO_PEERCRED` check, one request per connection, `read` and `list`. | ~200 |
| 3 | creche | Writes: `create` and `rewrite`, path rules, `openat2`, `if_match`, write lock, atomic write. | ~220 |
| 4 | creche | Cognee: pinned in the package's own lock, settings from section 4.4, the MM6 sync loop with MM8 folders. | ~230 |
| 5 | creche | `search` over Cognee: the two modes, the path lookup, the caps, `not_ready`. Tests a search during a sync. | ~150 |
| 6 | creche | The user unit `creche-memory.service`, and how it ships (it follows `creche-release-design.md`, `host` kind `units`). | ~100 |
| 7a | creche | Handover (D0.3): the root `uv.lock` moves only the venv rows that the root workspace builds. Today it moves every venv row, so each root lock change would release `memory` for nothing. The operator's leave, 2026-11-02: "Fix the rule first". | ~80 |
| 7b | creche | The handover release row for `memory` (D0.3). It raises `MAX_REQUEST_COMPONENTS` to 10, which CH7-5 had in the `vmm` PR (agreed with the CH session, 2026-11-02). | ~120 |
| 8 | mcp | The `memory` upstream: the five tools, the socket client, `service_down`, the verify list, stdio tests against a fake socket. | ~230 |
| 9 | registry | `mcp/memory/server.toml`, and the grant `tools.memory = "all"` for every family except vault-oracle. Merges after root installs the upstream (section 10.1, step 4). | ~60 |

At the cut (CH phase 10, registry PRs 2 and 4): the memory-writing skill and each family's instructions point at `memory__*`, and the vault mounts and vault-oracle go. After the cut (CH phase 11): remove `agent/src/index-search.ts` and its wiring, the `library/` package, the `creche-library@` units, `bin/provision-library.sh` and `/srv/agents/state/index` (D0.6).

The CH session also opens creche PRs. Before each creche PR: `gh pr list --repo la-creche/creche --author @me`. Only one Claude PR may be open per repo.

### 9.1 Status on 2026-11-03

Merged: creche #335, #336, #337, #338, #340, #341, #343, #344, #346, #348 (the service), #352 (the lock rule), #354 (the cap of 10), #355 (the `memory` row); mcp #94 (the upstream); private-docs #47 (contract 06 0.22.0) and #49. Also merged: creche #358 (the verify hook waits for the socket). Registry #58 (the server file) and #60 (the grants) are merged. #59 merged with no change (section 10 step 11). The operator ran the host steps of section 10 on 2026-11-03.

The split of PR 7 into 7a, 7b and 7c, and of PR 9 into a server-file PR and a grants PR, kept one idea per PR. The registry split keeps a grant from naming an upstream that the chaperone does not run.

## 10. Host steps for the operator

This replaces CH note section 6.1 step 3. Run the steps in this order. Each step says if it needs sudo. Paths were checked on the host on 2026-11-03: the code mirror holds the unit file and the draft script, and neither `mcp-memory` nor `/var/lib/creche-memory` exists yet.

### 10.1 The memory service

1. **Tap the pending handover releases.** Done: on 2026-11-03 the host runs handover `0.1.71` (the `memory` row, cap 10, contract minor 22) and `mcp-servers` `0.1.14` (with `memory-mcp`).
2. **Create the user (sudo).** The socket folder needs its group, and the `mcp-servers` build of `/opt/mcp/memory` runs as it.
   `sudo useradd --system --user-group --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin mcp-memory`
3. **Create the socket folder (sudo).** Its group is a group that <user> is not in, so only root can set it.
   `sudo install -d -o <user> -g mcp-memory -m 2750 /var/lib/creche-memory`
4. **Mint the `memory` key (no sudo, MM10).** It reads the master key from `caregiver.env` and never prints a key.
   ```
   umask 077; MK=$(grep '^LITELLM_MASTER_KEY=' /srv/agents/state/creche/caregiver.env | cut -d= -f2-); curl -s http://192.0.2.10:4000/key/generate -H "Authorization: Bearer $MK" -H 'Content-Type: application/json' -d '{"key_alias":"memory","models":["fast"],"max_budget":5,"budget_duration":"1d"}' | python3 -c 'import json,sys; print("MEMORY_LITELLM_KEY="+json.load(sys.stdin)["key"])' >> /srv/agents/state/creche/memory.env
   ```
5. **Copy the tokenizer into the store (no sudo).** The service reads it offline from `<store>/hf/hub`. The TEI cache is readable by others.
   `mkdir -p -m 0700 /srv/agents/state/memory/hf/hub && cp -rL /srv/agents/appdata/tei-cache/models--BAAI--bge-base-en-v1.5 /srv/agents/state/memory/hf/hub/`
6. **Install the unit (no sudo).** It fails to start until the tree exists, then the release restarts it. The verify hook needs it running.
   `install -m 0644 /srv/agents/code/creche/systemd/creche-memory.service ~/.config/systemd/user/ && systemctl --user daemon-reload && systemctl --user enable --now creche-memory.service`
7. **File the first `memory` release, then tap.** No follower files a first release.
   `cd /srv/agents/code && /opt/components/handover/bin/handover request memory --root /srv/agents/code/creche --root /srv/agents/code/mcp --root /srv/agents/code/registry --root /srv/agents/code/chaperone --root /srv/agents/code/creche-release --wait`
   With no `--root`, the command walks the working folder. From `~` it finds the old `~/code/creche` checkout and refuses. A dry run of this command passed on 2026-11-03.
   The first sync starts by itself. Estimate from the spike: about 250 calls, 16 minutes and $0.25.

### 10.2 The upstream and the grants

8. **Merge la-creche/registry#58** (the server file only), then tap the `mcp-servers` release that it starts. It builds `/opt/mcp/memory` as `mcp-memory`.
9. **Add the chaperone drop-in (sudo).** Chaperone `docs/design.md` §8 step 7 asks for one for the folder of a Unix socket. Linux may allow `connect()` on a read-only mount without it. That is not checked, so the step stays.
   `printf '[Service]\nReadWritePaths=/var/lib/creche-memory\n' | sudo tee /etc/systemd/system/creche-chaperone.service.d/memory.conf && sudo systemctl daemon-reload`
10. **Add the plugin manifest and the `install.toml` entry (sudo), then restart the chaperone (D3.2, D4.2).** The draft script printed both on 2026-11-03 from registry#58. The manifest matches the `headroom` one in form.
    ```
    sudo install -d -m 0755 /etc/chaperone/plugins/effect/added/memory
    printf 'name = "memory"\nversion = "1.0.0"\nkind = "effect"\nhooks = ["upstream"]\ncommand = [\n    "/usr/bin/env",\n    "MEMORY_SOCKET=/var/lib/creche-memory/memory.sock",\n    "/opt/mcp/memory/bin/memory-mcp",\n]\n' | sudo tee /etc/chaperone/plugins/effect/added/memory/plugin.toml
    printf '\n[plugins.memory]\nuser = "mcp-memory"\n' | sudo tee -a /etc/chaperone/install.toml
    sudo systemctl restart creche-chaperone.service
    ```
11. **Merge la-creche/registry#60** (the grants). It goes live with no tap. #59 merged with no change: its one commit was already in `main` through #58, whose last commit removed the grants.
    Merge it only after the chaperone log shows the `memory` upstream up. The chaperone refuses each app that has a grant of a down upstream (chaperone `docs/design.md` §7.7). It does not restart an upstream, and the `mcp-servers` release has no unit, so the chaperone restart comes after the tap of step 8.
12. **Remove the spike.** Done on 2026-11-03 under the operator's overnight leave: `~/spikes` and `~/.cognee` are gone, with the spike's copy of the `vault-oracle` key.

## 11. Proof

Result, 2026-11-03, a `chat` session through the chaperone: all five steps passed.
1. `list`, `read` and `create` answered. The note `inbox/2026-11-03-mcp-memory-proof.md` was written at 19:40:50 UTC, `<user>:<user>` 0644, with `family: chat` frontmatter.
2. After the first sync (about 19:25 to 19:50 UTC), a search returned the note as its first passage, with graph facts built from it.
3. A `rewrite` with 64 zeros as `if_match` returned `changed`.
4. With the service stopped, `list` returned `service_down: ConnectionRefusedError`. The chaperone kept the upstream, and the service came back at 20:00:47 UTC.
5. Gap found: the service logs no line when a sync starts or ends, so nobody can tell when search is ready. Fixed by creche#372 (merged 2026-11-03), released as `memory-v0.1.2`: each sync logs `sync started` and `sync done in <n> s`, and the first good sync adds `; search is ready`.

The steps as planned:

1. Through chaperone, `chat` calls `memory__search`, `memory__read` and `memory__create`.
2. The new note appears in `inbox/`, owned by <user> with mode 0644, and reaches the operator's phone.
3. Within about 1 minute, a search finds the new note.
4. A `rewrite` with a stale `if_match` returns `changed`.
5. With the service stopped, each tool returns `service_down`, and chat keeps working.

## 12. Risks and what is not tested

1. **A poisoned memory.** An agent can write an inbox note from text it read (a web page, an email). Cognee then serves it to every agent. Guards: the memory-writing skill rules, the `family` frontmatter, and the operator's review of `inbox/` in Obsidian.
2. **Concurrent use.** The spike did not test a search during a sync. PR 5 tests it.
3. **Cognee's size and pace.** The venv is 1.2 GB and Cognee releases often. The pin and the package's own lock hold it still. An upgrade is its own PR.
4. **Shared rate limit.** Fireworks limits all families together. Ingest stays at 40 requests per minute.
5. **Not tested:** the connector's `folders` setting, memory use, and running the upstream as `mcp-memory`.
