# handover

The release tool. Five jobs, and two of them touch nothing. The root
`AGENTS.md` applies here too.

1. **The resolver.** Every component declares itself in a `component.yaml`.
   The resolver reads them, builds the deploy order from `depends_on`, checks
   that a set is compatible, and produces the resolved manifest and its
   hash. The operator's phone approval binds to that hash.
2. **The tag allocator.** A merge allocates each touched component's next
   tag from its newest tag plus the bump level a label asked for.
3. **The executor** (`executor/`). The ten release steps, as root, on the
   host: intake, resolve, provenance, contracts, approve, lock, quiesce,
   stage, switch, restore or record. It is the only actor that installs or
   restarts anything.
4. **The requester** (`requester/`). It mints a ULID, builds the request's
   seven fields, validates them with the executor's own parser, and writes one
   file into `requests/`. The operator reaches it as `handover request`. The
   chaperone's `release` verb calls the same function.
5. **The secret intake** (`intake/`). It drains the gap directory, pushes
   the operator one link per missing secret, and seals what the operator
   pastes to a public recipient set. Root writes a secret it cannot read.

## The rule that contains every other one

**Nothing outside `executor/`, `intake/`, `requester/` and `corpus/` touches
a host.** The top level reads files the caller names and writes nothing. No
`subprocess`, no network, no environment variable that changes a decision.

1. The pure half never imports `executor/`.
2. Inside `executor/`, one module starts children: `host.py`. A `Command`
   carries an argv list. No code path takes a string.

`requester/` writes exactly one file, into `requests/`, and nothing else. It
imports the executor's pure modules only: `request`, `approval`, the path
constants of `spool` and `live_state`, and the `live_state` builder. It
prints no gate id, because root alone computes `manifest_sha256`.

`corpus/` runs `git fetch` in the corpus root, as the operator, before a
request is filed. It refuses to run as root. Nothing in it raises.
`chaperone/` never imports it, and neither does `requester/`.

`bin/allocate-tags.sh` runs in GitHub Actions. It never runs on a host. Its
arithmetic lives in `allocate.py`, which is pure.

The console script is `handover`. The verify hook and the operator's
`request` command run it by that name.

## Trust rules

1. Every input is hostile. A `component.yaml` can come from a branch an agent
   wrote. A byte cap before the parse, `yaml.safe_load` only, a closed key
   set, a strict pattern per scalar, and a refusal that names the field.
2. Every pattern is `re.fullmatch`.
3. `errors.safe_token` is the one door untrusted text goes through. Anything
   else prints as `<unprintable>`.
4. A refusal code comes from the closed list in `RefusalCode`.
5. A command is a list of argv lists, never a string.

## Rules the design depends on

1. The catalog is code, not a file. `CATALOG` and `CONTRACT_OWNER` live in
   `catalog.py`.
2. Every list in the resolved document is ordered here, so two runs over one
   set produce one hash whatever order the arguments arrived in.
3. `handover` is last in `order`, whatever `depends_on` says. Nothing may
   depend on it.
4. `FIRST_VERSION` and the bump arithmetic live in one place.
5. The site file holds everything that differs between deployments. `site.py`
   reads it. A missing value refuses with the `site` code. It never falls back
   to a default.

## Rules `executor/` adds

1. Root builds the live-state document. There is no file. The tag reader is
   GitHub for root and the corpus for the requester.
2. `present` is not `version is not None`. A tree with no stamp is `suspect`,
   not absent.
3. The switch note is written before the move. A note that outlives a run is
   read as a crash by the next run.
4. A release that does not succeed removes every tree it staged.
5. `requests/` is drained on every run, with one exception: a pass that
   swapped `handover`'s own tree ends after that request.
6. A refusal's `reason` is a module constant.
7. `runuser` is `/usr/sbin/runuser`, by absolute path.
8. The path unit and the root wrapper are outside every release. The thing
   that deploys code is not deployed by code.

## Rules `mcpbuild.py` adds

1. No third-party package build runs as root. The venv is built as
   `mcp-<name>`, and root takes the tree back afterwards.
2. A hash is checked before an artifact is opened.
3. A path under the MCP root is built from a validated name, never from a
   file's field.
4. The entrypoint check reads the first 2 KiB, not the first line.
5. `stage_all` swaps nothing. Every server is built before any is live.

## Sets, the phone and the quiet window

1. A set of one is not a special case.
2. Everything builds before anything swaps. The restore runs in reverse.
3. The phone transport is the chaperone's, not a second one. `phone.py`
   polls the hook for the verdict, because root owns no HTTP server.
4. `phone.py` and `quiet.py` are stdlib only. A release re-syncs `handover`'s
   own venv under the running executor.
5. A signal root cannot read is never quiet, and it restarts the window.
5a. Root reads an operator-owned file with `O_NOFOLLOW` and checks the open
    descriptor: regular file, the operator's uid, mode 0600, bounded size.
    Root must not act as the operator's deputy against a file the operator
    cannot read.
6. Root believes a manifest at a SHA it verifies, or out of a tree it owns.
   There is no third source.

## Venv builds, the action id and the intake

1. A `kind: venv` component is built with `uv venv --relocatable` first, so
   the tree survives its own rename.
2. The action id root pushes is a 26-character lower-case ULID.
3. `intake/` holds no private key, starts no child and has no read verb.
   `host.make_sealer` is the one door, and it hands the plaintext to a
   child's stdin.
4. The intake's gap rule lives in the kernel: `O_EXCL`.
5. The intake's mint never answers its caller. A token leaves on one phone
   push and by no other route.
6. The intake's page carries no script, and the policy says `script-src
   'none'`.
7. The listener is bound only while a token is pending.
8. The intake reads the gap directory the way `spool.py` reads `requests/`.
   One refused entry never refuses the directory.

## The chaperone's upstream roster (`executor/roster.py`)

1. Root writes it. `caregiver` must never write it.
2. `command` is built from the validated name and entrypoint, never copied.
3. It lives outside every component install tree.
4. A release that declares no server still writes it and still signals.
5. `tools: all` is expanded here.

## Use

```bash
uv run handover check --partial                     # this repository's manifests
uv run handover resolve chaperone=2.1.0 --root . --state live-state.json --id <ULID>
uv run handover request attendance chaperone@2.1.0 --root . --dry-run
```

`--partial` is what this repository needs: two of the catalog's components
live in other repositories. Exit codes: 0 pass, 1 refusal, 2 a usage mistake.

The executor runs as root from `creche-handover.path`, through the wrapper
`bin/creche-handover`, which decrypts the site's sops file and execs
`handover-exec`. The spool is `/var/lib/creche-handover/releases/`.

## Layout

| Module | Owns |
|---|---|
| `errors.py`, `catalog.py`, `site.py` | the closed refusal list, the component list, the site file |
| `manifest.py`, `discovery.py`, `order.py`, `contracts.py` | `component.yaml` as hostile input, the walk, the order, rules C1 to C5 |
| `state.py`, `resolve.py`, `allocate.py`, `mcpserver.py`, `silent.py` | the live-state shape, the decision and hash, the tag plan, `server.yaml` as hostile bytes, the uninstalled component |
| `cli.py` | `check`, `resolve`, `allocate-tags`, `request` |
| `requester/`, `corpus/` | one request file, one `git fetch` |
| `executor/spool.py`, `request.py`, `live_state.py`, `provenance.py` | the five directories, the request, the built document, P1 to P5 |
| `executor/approval.py`, `phone.py`, `notice.py`, `quiesce.py`, `quiet.py` | the gate, the transport, the outcome push, step 7, the quiet minutes |
| `executor/host.py`, `install.py`, `mcpbuild.py`, `roster.py`, `self_switch.py` | the one spawn, steps 8 to 10, a server tree, the roster, the self-release |
| `executor/ledger.py`, `steps.py`, `drain.py` | the entry, the ten steps, the unit's entry point |
| `intake/token.py`, `store.py`, `service.py`, `gaps.py`, `notify.py`, `run.py` | the capability, the kernel rule, the two routes, the gap directory, the one push, the entry point |
| `bin/allocate-tags.sh` | the workflow's body: gather, plan, tag, release |

## Tests

```bash
uv run pytest handover/tests
```

No test needs a host. A real `git`, `uv` or `sops` runs against `tmp_path`.
TLS runs on loopback. Every test file's basename starts with
`test_handover_`. `handover_fixtures.py` builds valid manifests only. A test
that wants a refusal changes one field.

## Known gaps

- The checker runs C2, C3, C4, then C1 (`contracts.py`).
- A component named at its live version is `unchanged`, and step 9 restarts
  nothing (`resolve.py`).
- Every build uses `UV_PROJECT_ENVIRONMENT` and every unit file is
  `systemd/<unit>` (`manifest.py`, `executor/install.py`).
- Every component with no tag gets `0.1.0` (`allocate.py`).
- The per-requester cap counts one drain pass and keys on `requested_by`,
  which the requester writes (`executor/drain.py`).
- The requester's wake-up is not built. Root writes the ledger entry and
  pushes the phone only (`executor/notice.py`).
- A switch keeps the unit it replaces as `<unit>.prev` (`executor/install.py`).
- A `playpen` release swaps a tree and builds no image
  (`playpen/component.yaml`).
- `playpen-verify` is declared and implemented nowhere.
- A crash between a server swap and the verify is not repaired
  (`executor/spool.py`, `executor/steps.py`).
- `arg_allows` is applied by nothing (`executor/roster.py`).
- The secret-name pattern is copied into five modules, and the copies agree
  only by hand.
- The requester refuses once `requests/` holds eight entries of any owner
  (`requester/file.py`).
- The `chaperone` venv carries `handover`'s console scripts
  (`chaperone/pyproject.toml`).
