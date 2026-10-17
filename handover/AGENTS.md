# handover

The release tool. Six jobs, and two of them touch nothing. The root
`AGENTS.md` applies here too.

1. **The resolver.** Every component declares itself in a manifest:
   `component.toml` or `component.yaml`. The resolver reads them, builds the
   deploy order from `depends_on`, checks that a set is compatible, and
   produces the resolved manifest and its hash. The operator's phone
   approval binds to that hash.
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
6. **The follower** (`follow/`). A timer runs `handover follow` as the
   operator. It fetches the corpus and reads which followed components have
   a newer tag. It files one request for that set. It approves nothing.

## The rule that contains every other one

**Nothing outside `executor/`, `intake/`, `requester/`, `corpus/` and
`follow/` touches a host.** The top level reads files the caller names and
writes nothing. The one exception is `notices.py`: it writes the notice file
of a program that names one, and only the follower names one. No
`subprocess`, no network, no environment variable that changes a decision.

1. The pure half never imports `executor/`.
2. Inside `executor/`, one module starts children: `host.py`. A `Command`
   carries an argv list. No code path takes a string.

`requester/` writes exactly one file, into `requests/`, and nothing else. It
imports the executor's pure modules only: `request`, `approval`, the path
constants of `spool` and `live_state`, and the `live_state` builder. It
prints no gate id, because root alone computes `manifest_sha256`.

`corpus/` runs `git fetch` in the corpus root, as the operator, before a
request is filed. `corpus/artifacts.py` runs `curl` as the operator. It
fetches the two assets of the Release of a binary component into the store
below the home of the operator. One fetch at a time writes in the directory
of a tag: the fetch holds a lock on that directory. The package refuses to
run as root. Nothing in it raises. `chaperone/` never imports it, and neither
does `requester/`.

`follow/` reads one ledger entry and the marker file, and it writes the
marker file. The marker also holds the wait of each binary member. When the
ledger entry or the marker is not strict JSON, the follower also writes one
row in its notice file. It starts no child and opens no socket. The fetch
is `corpus/`'s and the request is `requester/`'s. `cli.py` calls all three.
`chaperone/` never imports it.

`bin/allocate-tags.sh` runs in GitHub Actions. It never runs on a host. Its
arithmetic lives in `allocate.py`, which is pure.

Prose moves no tag. `allocate.cut_paths` drops each `.md` path outside
`tests/` and `fixtures/` before the cut, because the cut loses the file
name. `bin/lib/docsrule.sh` holds the same rule for the hook and CI. A test
holds the two copies equal.

The console script is `handover`. The verify hook and the operator's
`request` command run it by that name.

## Trust rules

1. Every input is hostile. A manifest can come from a branch an agent
   wrote. A byte cap before the parse, `tomlfile` for a TOML text, the safe
   loader of PyYAML only for a YAML text, a closed key set, a strict pattern
   per scalar, and a refusal that names the field.
2. Every pattern is `re.fullmatch`. A pattern writes a digit as `[0-9]`.
   On text, `\d` also matches a digit that is not ASCII.
3. `errors.safe_token` is the one door untrusted text goes through. Anything
   else prints as `<unprintable>`.
4. A refusal code comes from the closed list in `RefusalCode`.
5. A command is a list of argv lists, never a string.
6. A parser answers a refusal for each error of its reader library. PyYAML
   raises more than `YAMLError`: a scalar that it cannot build raises
   `ValueError`, and a text that nests too deep raises `RecursionError`.
7. Each JSON reader calls `strictjson.loads` with the byte cap of its file
   or answer, and never `json.loads`. Each JSON writer calls
   `strictjson.dumps`, and never `json.dumps`. The command line is the one
   exception: it prints its own reports with `json.dumps`, and no reader
   reads them. A whole number that becomes a time limit has a range.
8. Each YAML reader calls `boundedyaml.load`, and never `yaml.safe_load`.
   PyYAML gives a merge key no limit, so a short text can make it use time
   and memory with no bound. No `except` clause stops that. `boundedyaml`
   counts the pairs that the merge keys copy, and the levels of a chain of
   merge keys. The reader of a server file counts one for a merged mapping
   with no pair. Past a limit, the text does not parse.
9. Each TOML reader calls `tomlfile.loads` with the kind of its file, and
   never `tomllib.loads`. `tomlfile` holds the byte limit and the level
   limit of each kind. It refuses each form that TOML 1.1 added, with each
   version of Python. A refusal has one of four fixed texts, and no text
   holds a byte of the file.
10. `tomlfile.py` is a copy of `family/src/agent_family/tomlfile.py`, byte
    for byte. Reason: `handover` imports no module of `agent_family`.
    `bin/tests/test_tomlfile_numbers.py` holds the two files equal. Change
    both files in one commit.
11. A reader gives a text that is not strict JSON the answer that it gives
    a text that is not JSON. It also calls `notices.record` with the
    surface word of the reader and the rule word. No text of the refused
    file goes into the notice. A writer refuses a value with no strict
    text, and it writes no part of a file and sends no body.
12. `strictjson.py` and `notices.py` are copies of the files of the
    noticeboard, byte for byte. Both need the standard library only, so
    `executor/phone.py` and `executor/quiet.py` can import them.
    `bin/tests/test_strictjson_copies.py` holds each copy equal. Change each
    copy in one commit.
13. Each program calls `notices.configure` one time at its start. Only the
    follower has a notice file: `notices/follow.json` below the state root
    of the operator (`executor/layout.py`). A timer starts it, and the
    operator does not watch its log. `--notices` names another file. A run
    with `--dry-run` writes no notice file, and a run as root writes none.
    The executor, the intake and each other subcommand write the log line
    only. The executor and the intake run as root. Root writes no file in a
    directory that the operator owns.

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
6. The version of the manifest contract has one source: the `provides` row
   for `component-manifest` in `handover/component.yaml`. `catalog.py` holds
   the same two numbers as constants that a person types:
   `MANIFEST_CONTRACT_MAJOR` and `MANIFEST_CONTRACT_MINOR`. They are the
   newest version that the reader takes.
   `tests/test_handover_contract_version.py` fails when the constants and
   the row differ. Only the Rust code takes the two numbers from the row
   with a program: a build script (`rust/AGENTS.md`). Change the row and
   the constants in one commit.

## The component manifest (`manifest.py`, `discovery.py`)

The manifest of a component has two names during the change from YAML to
TOML. `MANIFEST_FILENAME` is `component.toml`, and `MANIFEST_FILENAME_YAML`
is `component.yaml`. Write no other copy of a name.

1. A directory holds one of the two names. The walk of `discover` and
   `read_one` read `component.toml` when the directory holds that name.
   Else they read `component.yaml`.
2. The tool refuses a directory that holds both names, with the code
   `manifest`. It then reads neither file. Each entry of a name counts: a
   regular file, a link, a link to no file and a directory. Before that
   rule, `read_one` refuses a component directory that is a link, with the
   code `catalog`.
3. The cap of `MAX_MANIFEST_FILES` counts the manifests of both names.
4. The caller of `parse_manifest` names the markup of the text. `Markup`
   holds the two markups. No default markup exists.
5. A TOML text goes to `tomlfile.loads` with the kind `MANIFEST`. The byte
   limit of that kind is the one size limit of the text. A refusal of the
   reader gives one refusal with the text of the reader.
6. The tool reads a TOML file as bytes, up to one byte past that limit.
   The reader thus refuses a larger file with its own text.
7. A YAML text keeps `MAX_MANIFEST_BYTES` and the merge limits of
   `boundedyaml`. Packet `toml-drop-yaml-manifest` removes the YAML form.
8. Each check after the read is the same for the two forms, with two rules
   of TOML. `unit` is optional, and an absent key means no unit. `release`
   is `true` or `false`, and the tool refuses a text. A YAML text keeps
   `unit: null` and the words `yes` and `no`.
9. `silent.py` builds a TOML text. It has no `unit` key, and `release` is
   `false`.

The switch stamps the manifest into the tree that it installs. The stamp
has the name of the markup of the manifest: `.component.toml` or
`.component.yaml` (`live_state.MANIFEST_STAMP` and `MANIFEST_STAMP_YAML`).

1. `installed_manifest` reads `.component.toml` when the tree holds an
   entry of that name. Else it reads `.component.yaml`. It gives the text
   with its markup.
2. The read of a TOML stamp stops one byte past the byte limit of the kind
   `MANIFEST`. A YAML stamp keeps the cap of 64 KiB.
3. A stamp that does not parse reads as absent, and never as a refusal.

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
9. A step that raises an error it does not name is a failed step. The
   ledger gets the type of the error, and never its text.
10. A unit file longer than 64 KiB stops the stage, for a `venv` component
    and for a `binary` component. A line after the cap can start a program
    that no reader saw.
11. A staged venv tree is a directory, as a staged binary tree is. A link in
    its place is a fault: step 9 renames the link, and not the directory.

## Rules `follow/` adds

1. It follows a component only while the tree of that component carries a
   release stamp. The operator files a first release by hand.
2. It files a set on the second run that reads the same set. CI makes a tag
   before the Release of that tag, and one merge can tag many components.
3. The marker names the set and the request. A run that finds its own set
   in the marker files nothing.
4. A denial, a refusal and a failed release hold the set. Only a newer tag
   asks again.
5. A gate that closed with no tap is not an answer. The follower asks again
   after one hour, and four times in all. It asks again in the same way
   after a refusal of check P6 with the reason
   `the verifier was not available`.
6. The marker is written before the request. A marker that cannot be
   written costs no request.
7. A followed name is a releasable component of the catalog. It carries no
   version: the version is the newest tag.
8. A member that ships an archive is ready when the store holds its two
   files. While a member is not ready and its 600 seconds are not over, the
   run files nothing. The 600 seconds start at the first run that read the
   tag of the member. After that time, the rest of the set goes to rules 2
   to 6, and the run prints one line. The marker keeps the version and the
   time of that first run, under the key `waits`. When the files arrive
   later, the member is part of a new set.

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
    cannot read. The rule has two cases:
    1. The `view-ro` token of the quiet window (`quiet.py`).
    2. The archive and the bundle of a binary component in the store
       (`executor/artifact.py`). Root also opens each directory below the
       operator home with no link followed, and the operator must own each
       one. A file has one name only, and root reads it in blocks up to its
       limit. Root reads the store one time in a release, at step 2. Each
       later read is a read of root's own copy.
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

## Binary builds

1. A `kind: binary` component is a tree of compiled programs. Each program
   is at `<install.to>/bin/<name>`, the layout of a venv.
2. Root builds it on the host from the manifest's own `build` argv, as it
   builds a venv. The user, the environment and the time limit are the
   same. Every `build` argv carries `--locked`, and `cargo install` carries
   `--no-track`. The first word of each `build` argv is the word `cargo`,
   with no path (`catalog.BINARY_BUILD_PROGRAM`).
3. The executor sets `CARGO_INSTALL_ROOT` to the staged tree. No venv step
   runs: no `uv venv --relocatable` and no walk of `site-packages`.
4. The unit rules of a venv apply. The unit must start a program inside
   `install.to`, and a sibling unit travels with the component's own unit.
5. Every program that a unit or the verify command starts is a regular,
   executable file of the staged tree.
6. The staged tree is a directory. A link in its place is a fault. No link
   in the staged tree has an absolute target, leaves the tree or is in a
   loop. No file holds the path of the fetched work tree or has a second
   name.
7. `catalog.BINARY_BUILD_FILES` move every binary component and no other
   kind. The input digest of a binary component covers those files and not
   `uv.lock`.
8. Change a component's kind in its catalog row and in its manifest in one
   commit. Discovery refuses a manifest whose kind is not the kind of its
   row, with the code `catalog`.
9. Release `handover` before the first manifest says `kind: binary`. An
   older executor or requester refuses that manifest.
10. Release `handover` alone before each release of a component that
    changes its kind. Root compares a manifest with the catalog of the
    installed code. Until the installed catalog holds the new row:
    - Root refuses the release of that component, with the code `catalog`.
    - Root refuses a set that holds that component and `handover`. Root
      reads each manifest of a set before it swaps a tree. The follower
      then holds that set (`follow/` rule 4).
    - The installed `handover request` refuses each request. It reads each
      manifest of its roots.
11. File the request for that release from a checkout that holds the new
    catalog, with `uv run handover request handover`. The `release` verb
    of the chaperone reads no manifest, so it can file the request too.

## The server file (`mcpserver.py`)

Root reads each server file again at an `mcp-servers` release. It trusts no
other reader of the file.

1. A server directory holds `server.toml` or `server.yaml`. Root reads
   `server.toml` when the directory holds that name. Else it reads
   `server.yaml`.
2. A directory that holds both names is a refusal, and root reads neither
   file. A link, a link to no file and a directory each count as a name.
3. Root reads no link, for each of the two names.
4. Root gives a TOML text to `tomlfile.loads` with the kind `SERVER`. The
   byte limit of that kind is the one size limit of the text. A refusal of
   the reader gives one refusal: `server.toml: <the text of the reader>`.
5. A YAML text keeps `MAX_SERVER_BYTES` and the merge limits of
   `boundedyaml`.
6. Each check after the read is the same for the two forms. A refusal names
   the field, and never the value.
7. `SERVER_FILE_NAME` and `SERVER_FILE_NAME_YAML` hold the two names. Write
   no other copy of a name.

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
uv run handover follow attendance chaperone --dry-run   # what the timer would do
```

`--partial` is what this repository needs: two of the catalog's components
live in other repositories. Exit codes: 0 pass, 1 refusal, 2 a usage mistake.

The follower runs as the operator from `creche-follow.timer`, out of the
`handover` component tree. The marker is `~/.local/state/creche/follow.json`.
The store of artifacts is `~/.local/state/creche/artifacts`.
The notice file is `notices/follow.json` below the state root.

The executor runs as root from `creche-handover.path`, through the wrapper
`bin/creche-handover`, which decrypts the site's sops file and execs
`handover-exec`. The spool is `/var/lib/creche-handover/releases/`.

## Layout

| Module | Owns |
|---|---|
| `errors.py`, `catalog.py`, `site.py` | the closed refusal list, the component list, the site file |
| `strictjson.py`, `notices.py` | the strict reader and the strict writer of a JSON text, and the notice of a refused text. Each file is a copy |
| `boundedyaml.py`, `tomlfile.py` | the YAML loader of each reader, with its two merge limits and its count of an empty merged mapping, and the TOML reader of each file kind |
| `manifest.py`, `discovery.py`, `order.py`, `contracts.py` | the manifest as hostile input, the walk, the order, rules C1 to C5 |
| `state.py`, `resolve.py`, `allocate.py`, `mcpserver.py`, `silent.py` | the live-state shape, the decision and hash, the tag plan, the server file as hostile bytes, the uninstalled component |
| `artifacts.py`, `treewalk.py` | the names, the limits and the signer of the archive of a binary component, the walk of a tree of compiled programs |
| `cli.py` | `check`, `resolve`, `allocate-tags`, `request`, `follow`, `artifacts`, `check-tree` |
| `requester/`, `corpus/` | one request file, one `git fetch`, and the fetch of the two assets of a binary component into the store (`corpus/artifacts.py`) |
| `follow/` | the set a newer tag moves, the marker, the decision |
| `executor/spool.py`, `request.py`, `live_state.py`, `provenance.py` | the five directories, the request, the built document, P1 to P5 |
| `executor/approval.py`, `phone.py`, `notice.py`, `quiesce.py`, `quiet.py` | the gate, the transport, the outcome push, step 7, the quiet minutes |
| `executor/host.py`, `install.py`, `mcpbuild.py`, `roster.py`, `self_switch.py` | the one spawn, steps 8 to 10, a server tree, the roster, the self-release |
| `executor/unpack.py`, `artifact.py` | the unpack of a tar archive and the member list of a component archive, root's copy of the two assets of a binary component and the digest of its archive |
| `executor/ledger.py`, `steps.py`, `drain.py` | the entry, the ten steps, the unit's entry point |
| `intake/token.py`, `store.py`, `service.py`, `gaps.py`, `notify.py`, `run.py` | the capability, the kernel rule, the two routes, the gap directory, the one push, the entry point |
| `bin/allocate-tags.sh` | the workflow's body: gather, plan, tag, release |

## Tests

```bash
uv run pytest handover/tests
```

No test needs a host. A real `git`, `uv`, `cargo` or `sops` runs against
`tmp_path`. The `cargo` test skips on a machine that has no `cargo`. A real
`curl` fetches from a loopback https server, and that test skips on a
machine that has no `curl` or no `openssl`.
TLS runs on loopback. Every test file's basename starts with
`test_handover_`. `handover_fixtures.py` builds valid manifests only. A test
that wants a refusal changes one field.

## Known gaps

- The checker runs C2, C3, C4, then C1 (`contracts.py`).
- A component named at its live version is `unchanged`, and step 9 restarts
  nothing (`resolve.py`).
- Every build uses `UV_PROJECT_ENVIRONMENT`, or `CARGO_INSTALL_ROOT` for a
  binary component. Every unit file is `systemd/<unit>`. No manifest can
  name another variable or another place (`manifest.py`,
  `executor/install.py`).
- Every component with no tag gets `0.1.0` (`allocate.py`).
- The per-requester cap counts one drain pass and keys on `requested_by`,
  which the requester writes (`executor/drain.py`).
- When the network connection of the host cannot fetch an archive inside
  one run of the follower, the follower never fetches it. One run has 270
  seconds, and the fetch resumes no partial file. `handover request` fetches
  with 600 seconds (`cli.py`).
- The follower does not ask again for a request that root never ledgered.
  A newer tag or a request by hand recovers (`follow/`).
- The follower reads tags out of the corpus. Root refuses a tag that has no
  Release, and that refusal holds the set (`follow/`).
- The follower and `caregiver` can each file a request for `mcp-servers`.
  Root refuses the second one, because it moves no component (`follow/`).
- The requester's wake-up is not built. Root writes the ledger entry and
  pushes the phone only (`executor/notice.py`).
- A switch keeps the unit it replaces as `<unit>.prev` (`executor/install.py`).
- A crash between a server swap and the verify is not repaired
  (`executor/spool.py`, `executor/steps.py`).
- `stage7-releases.md` §2.4 names no end for an error between the switch
  note and the end of the swap. It names none for an error inside the
  moves of the restore. In each case the run ends as a crash does: it
  writes no entry and it removes no staged tree. The next run repairs the
  component (`executor/steps.py`).
- `stage7-releases.md` §2.6 says a `reason` is a fixed string of a closed
  list. For an error that no step names, the reason also holds the type of
  the error and the symbol of its number. Both come from code
  (`executor/steps.py`).
- Contract 06 §5.2 says that the executor stops when the hook of a restored
  component fails. §5.1 says that a release goes back whole. The restore
  puts each other component back first, and the step then fails. A fault
  that is no failed hook still stops the restore at its component
  (`executor/steps.py`).
- `arg_allows` is applied by nothing (`executor/roster.py`).
- PyYAML reads a digit that is not ASCII in a number with the tag `!!int`.
  A whole number of a manifest can thus hold one. Contract 06 §8 names no
  YAML form for a number (`manifest.py`).
- Contract 06 §8 and §10 and contract 01b give no limit for a merge key. A
  manifest takes a chain of 128 merge keys and 65,536 copied pairs. A YAML
  server file takes a chain of 400 and 100,000 copied pairs. Each pair of
  limits is that of the Rust reader of the same file, so the two readers
  refuse the same text (`manifest.py`, `mcpserver.py`).
- Under contract 06 §10.2, the tool refuses a directory with the two
  manifest names. The contract does not say which kind of entry holds a
  name. Each entry counts, also a link, a link to no file and a directory
  (`discovery.py`).
- Contract 06 §10.2 puts the TOML stamp before the YAML stamp. It does not
  say what the tool reads when the TOML stamp is there and the tool cannot
  read it. The tool reads no stamp then, and the component takes row 4 of
  §10.2 (`executor/live_state.py`).
- Contract 01b §1 names one server file in each directory. It does not say
  which kind of entry holds a name. Root counts each entry: a regular file,
  a link, a link to no file and a directory. A stray entry of the other
  name thus refuses the directory. The family reader of
  `agent_family.registry` counts only a regular file or a link to one
  (`mcpserver.py`).
- No contract names the sops file. Its reader takes the two merge limits of
  a manifest (`intake/store.py`).
- A manifest whose chain of merge keys passes the limit gets the refusal
  text `nests deeper than 128 levels`. The Rust reader gives such a chain
  that text. The Python reader still reads plain nesting deeper than 128
  levels (`manifest.py`).
- The intake reads `Content-Length` with `int`. That reader takes a sign,
  an underscore and a digit that is not ASCII (`intake/service.py`).
- The secret-name pattern is copied into five modules, and the copies agree
  only by hand.
- The requester refuses once `requests/` holds eight entries of any owner
  (`requester/file.py`).
- The `chaperone` venv carries `handover`'s console scripts
  (`chaperone/pyproject.toml`).
- Contract 06 §8 does not list `kind: binary`. Build on the host or verify
  an artifact that CI built and attested: the operator decides. Root builds
  on the host today, as it builds a venv. A cargo build script runs
  arbitrary code, so the user that runs the build matters (`catalog.py`).
- Contract 06 §1 rule 10 names no file of a Cargo workspace. Three files
  move a binary component: `rust/Cargo.lock`, `rust/Cargo.toml` and
  `rust/rust-toolchain.toml`. A `rust/.cargo/config.toml` moves none
  (`catalog.py`).
- Only a test of this repository holds a binary build to `--locked`. The
  executor does not check the flag (`tests/test_handover_bin_manifest.py`).
- Contract 06 §3.4 names three checks of a file in the store of artifacts:
  a regular file, the operator as its owner, and a size at most the cap.
  Root also refuses a file with a bit for group or other, a file with a
  second name and an empty file. It refuses a directory of the store path
  that is a link or that the operator does not own (`executor/artifact.py`).
- Contract 06 §8 says that the unpack of a component archive counts
  members and bytes. It names no bound for the bytes of a tar header or for
  the bytes after the end of the tar. The unpack refuses a tar stream that
  holds more than the bytes of its members and `FRAME_BYTES`. The size of a
  sparse member is not the count of its bytes in the stream. The unpack
  thus refuses a sparse member as a member that is not a plain regular file
  (`executor/unpack.py`).
- Contract 06 §8.2 names the code `editable` for a venv tree. A binary tree
  that is not self-contained gets the same code
  (`executor/selfcontained.py`).
- Contract 06 §8.2 names no rule for a `.pth` line that does not resolve.
  The walk reports such a line as a path outside the tree, under each
  Python version (`executor/selfcontained.py`).
- One manifest whose kind is not the kind of its row refuses the whole walk
  of `discover`. The installed `handover request` then files no request,
  also for another component, until a `handover` release holds the new row
  (`discovery.py`, `cli.py`).
- For a component with a version stamp and no stamped manifest, root reads
  the manifest at the live tag of that component. After the catalog row of
  that component changes its kind, that manifest states the old kind. Root
  then refuses each release that does not deploy that component
  (`discovery.py`, `executor/steps.py`).
- The executor refuses a binary tree when a file holds the path of the
  fetched work tree. The walk cannot tell a path that a program opens from
  a path that it only prints. Code that a build script generates can carry
  its own path into a panic message. Such a build must remap the path. That
  path holds the request id, so a fixed cargo configuration cannot name it
  (`treewalk.py`).
- `handover artifacts --repo` reads the manifests only when the catalog of
  that repository holds a binary row. It then reads each manifest of its
  roots, also the manifest of a venv component. Such a read needs the site
  file for a `~/` path, and a CI runner has no site file. No catalog row
  says `binary` today (`cli.py`).
- A binary build runs in the root of the fetched tree. A `rustup` proxy
  reads a toolchain file from the working directory and its parents, so it
  does not read `rust/rust-toolchain.toml`. It also downloads a toolchain
  that the host does not have. The operator decides how the host gets its
  toolchain (`executor/install.py`).
- A run that ends in the way of a crash keeps `work/<id>`. No later run
  removes it. A binary build leaves its `target` directory there
  (`executor/steps.py`).
- `stage7-releases.md` §2.4 row 8 names no rule for the set-user-ID bit, the
  set-group-ID bit and the sticky bit of a staged file. The mode pass drops
  the three bits (`executor/install.py`).
- Contract 06 §1 rule 8 names no size for a unit file. A unit file longer
  than 64 KiB stops the stage, for a `venv` component and for a `binary`
  component (`executor/install.py`).
- Contract 06 §8.2 names no rule for a staged venv tree that is a link. The
  walk reports the link as a fault with the code `editable`
  (`executor/selfcontained.py`).
- Contract 06 §8.2 and PEP 610 name no size for a `direct_url.json` record.
  Root reads 64 KiB of such a record at most. A larger record is a record
  that root cannot read, and the walk reports it as a fault
  (`executor/selfcontained.py`).
