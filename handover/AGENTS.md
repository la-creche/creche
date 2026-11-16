# handover

The release tool. Five jobs, and two of them touch nothing. The root
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
   file into `requests/`. The operator reaches it as `handover request`.
5. **The follower** (`follow/`). A timer runs `handover follow` as the
   operator. It fetches the corpus and reads which followed components have
   a newer tag. It files one request for that set. It approves nothing.

## The rule that contains every other one

**Nothing outside `executor/`, `requester/`, `corpus/` and `follow/`
touches a host.** The top level reads files the caller names and
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
fetches the two assets of the Release of a binary component, and each file
and bundle of a disk-image component, into the store below the home of the
operator. One fetch at a time writes in the directory
of a tag: the fetch holds a lock on that directory. The package refuses to
run as root. Nothing in it raises. `requester/` never imports it.

`follow/` reads one ledger entry and the marker file, and it writes the
marker file. The marker also holds the wait of each binary member. When the
ledger entry or the marker is not strict JSON, the follower also writes one
row in its notice file. It starts no child and opens no socket. The fetch
is `corpus/`'s and the request is `requester/`'s. `cli.py` calls all three.

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
    The executor and each other subcommand write the log line only. The
    executor runs as root. Root writes no file in a directory that the
    operator owns.

## Rules the design depends on

1. The catalog is code, not a file. `CATALOG` and `CONTRACT_OWNER` live in
   `catalog.py`.
2. Every list in the resolved document is ordered here, so two runs over one
   set produce one hash whatever order the arguments arrived in.
3. `handover` releases alone. The executor that runs a set is the
   installed `handover`. Only a newer one can know how to stage a newer
   component, for example a new disk flavor.
   - `resolve.check_alone` refuses a set that deploys `handover` and
     another component, with the code `request`. `resolve` calls it, so
     step 2 of the executor refuses before the push, and the requester
     refuses before the file.
   - Only a deploy row counts. A set with `handover` at its live version
     is not a mixed set. A redeploy is a deploy row.
   - `order.py` still puts `handover` last, but a set with `handover`
     holds nothing else. Nothing may depend on it.
   - The follower files `handover` alone first (`follow/` rule 10).
4. `FIRST_VERSION` and the bump arithmetic live in one place.
5. The site file holds everything that differs between deployments. `site.py`
   reads it. A required value that is missing refuses with the `site` code.
   It never falls back to a default. An optional key has its default beside
   its read in `site.py`, and the default names no deployment. A value that
   does not have its shape refuses with the `site` code.
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
7. A followed name is a releasable component of the catalog. A name on the
   command line carries no version: the version is the newest tag.
8. A member that ships signed assets is ready when the store holds each of
   them: the two files of a binary member, each file and bundle of a
   disk-image member. While a member is not ready and its 600 seconds are not over, the
   run files nothing. The 600 seconds start at the first run that read the
   tag of the member. After that time, the rest of the set goes to rules 2
   to 6, and the run prints one line. The marker keeps the version and the
   time of that first run, under the key `waits`. When the files arrive
   later, the member is part of a new set.
9. With no names on the command line, the follower reads `host.toml` in
   the live `host` tree (`follow/policy.py`). It never reads a clone, so a
   change to what is followed waits for a tapped `host` release. A version
   in `[follow]` is a ceiling: the follower moves the component up to it
   and never above it, and it prints one note for each newer tag the
   ceiling holds back. A file that breaks one of the five rules of
   `policy.py` stops the run before the fetch, with exit 2.
10. `handover` releases alone, and first ("Rules the design depends on",
    rule 3). When the newer tags move `handover` and another component,
    the run takes `handover` alone, at its newest tag under the ceiling,
    into rules 2 to 8. It holds the rest and prints one line:
    `split: handover 0.1.94 goes alone first. Held until it is installed:
    disks 0.1.15, vmm 0.1.22`. When the stamp of `handover` reads that
    version, the rest is a new set for rules 2 to 8. While `handover` still
    moves, each run splits again. So a denial of the set of `handover`
    holds the rest too (rule 4), and a newer tag of `handover` is a new set
    of one. The registry watch reads the whole set, so a held
    `mcp-servers` tag still stops a redeploy.

## Rules `mcpbuild.py` adds

1. No third-party package build runs as root. The venv is built as
   `mcp-<name>`, and root takes the tree back afterwards.
2. A hash is checked before an artifact is opened.
3. A path under the MCP root is built from a validated name, never from a
   file's field.
4. The entrypoint check reads the first 2 KiB, not the first line.
5. `stage_all` swaps nothing. Every server is built before any is live.
6. Root looks for the entrypoint of an unpacked asset in four places, in
   this order, and walks no tree:
   1. `<entrypoint>` at the top of the asset.
   2. `bin/<entrypoint>`.
   3. `<folder>/<entrypoint>`.
   4. `<folder>/bin/<entrypoint>`.

   `<folder>` is the asset name without its archive suffix: `.tar.gz`,
   `.tgz`, `.tar.xz`, `.tar.bz2`, `.tar.zst` or `.tar` (`ARCHIVE_SUFFIXES`). An asset
   name with another suffix has no folder, and only places 1 and 2 count.
   The server file pins the asset name, so the file names the folder, and
   the archive does not.

## Sets, the phone and the quiet window

1. A set of one is not a special case.
2. Everything builds before anything swaps. The restore runs in reverse.
3. The phone transport is the chaperone's, not a second one. `phone.py`
   polls the hook for the verdict, because root owns no HTTP server.
3a. The operator decided the text of the two pushes. Each arrow is the
    ASCII `->`.
    1. The approval push has the title `Release Approval` and a message of
       two lines. `Components:` lists each deploy row in name order, as
       `name from -> to`, with `absent` for a first install. `Unsafe:`
       lists in name order each component with no automatic rollback: its
       restore mode is `manual`, or root could not verify its manifest. An
       empty list is `none`.
    2. The outcome push has the title `Release Result`. Its message is
       `All succeeded`, or `Release failed at <where>.` and a second line
       `Rolled back components: <names or none>`. `<where>` is
       `<component> release` when one component caused the end, and
       `<step> step` when none did. The executor records the component and
       each component that the restore put back (`Entry.failed_component`,
       `Entry.rolled_back`). No code reads the text of an error for them.
    3. Each push carries `summary`, the message cut to 200 characters.
       Contract 04 §8.4 rule 1 needs it, and the live flow shows it until
       the operator imports the flow that reads `title` and `message`. The
       message has 900 characters at most, and the title 60.
    4. The approval push carries no `fields`. The outcome push carries
       `fields` with the request `id` only, and the flow builds the tag of
       the card from it.
    5. The log of the ledger entry keeps §2.5's seven fields, one line for
       each field. The requester's preview prints the two lines of the
       phone, then the seven fields.
4. `phone.py` and `quiet.py` are stdlib only. A release re-syncs `handover`'s
   own venv under the running executor.
5. A signal root cannot read is never quiet, and it restarts the window.
5a. Root reads an operator-owned file with `O_NOFOLLOW` and checks the open
    descriptor: regular file, the operator's uid, mode 0600, bounded size.
    Root must not act as the operator's deputy against a file the operator
    cannot read. The rule has three cases:
    1. The `noticeboard` token of the quiet window (`quiet.py`).
    2. The archive and the bundle of a binary component in the store
       (`executor/artifact.py`). Root also opens each directory below the
       operator home with no link followed, and the operator must own each
       one. A file has one name only, and root reads it in blocks up to its
       limit. Root reads the store one time in a release, at step 2. Each
       later read is a read of root's own copy.
    3. The env file of a unit, at the env fill (`executor/envfill.py`).
       The size limit is the 64 KiB of `creche-env`.
6. Root believes a manifest at a SHA it verifies, or out of a tree it owns.
   There is no third source.

## The env fill

A unit can read one env file alone. `creche-vmm.service` reads
`/srv/agents/state/creche/vmm.env` (CH59 rule 1, CH77 rule 1).
`bin/fleet-up up` writes that file from the catalog of `creche-env` and the
site file. fleet-up and the catalog reach the host through `creche-deploy`,
and never through a release. So a release adds each key that the program
needs and the file does not hold (`executor/envfill.py`).

1. The manifest names the file and the keys in the optional table `env`.
   `file` is an absolute path, or `~/` for the operator home, as
   `install.to` is. `keys` holds 1 to 64 distinct keys of the form
   `[A-Z][A-Z0-9_]{0,63}`.
2. The reader refuses an `env` table in a component whose `repo` is not
   `creche`, because the catalog is in that repository only. Each
   `--env-file` of the verify hook must name `env.file`.
3. A key is absent when the file has no line for it. A line with an empty
   value refuses the release: the fill changes no line, and a second line
   of one key makes the reader of the vmm refuse the file.
4. Root reads the value of each absent key from the catalog at the release
   commit: `env/src/creche_env/default.env` and `example.env` in root's own
   clone of the commit that step 3 verified. No link is followed. Root
   never reads the installed tree, so a key that lands in the same set as
   the program resolves. `creche-env` resolves the value against the site
   file, as fleet-up does, so the two writers give one value.
5. Step 4 reads the file by rule 5a and plans the fill. The step writes one
   log line for each component: `env vmm: adds <keys> to <file>`, or `env
   vmm: nothing to add to <file>`. Step 4 runs after step 3, so root reads
   the catalog at a verified commit, and it runs before the push of step
   5.
6. Each fault refuses the release at step 4 with the code `env`, before
   the push. The detail names the component, the file and the key. A
   missing file names the fix `/opt/creche/bin/fleet-up up`. A file that
   fails rule 5a, a file that does not parse and a key that the catalog and
   the site file give no value are the other faults.
7. Step 8 runs the fill after the build of each component and before any
   swap. Root opens the file again with `O_NOFOLLOW`, read and write, and
   each write at the end. It checks the descriptor by rule 5a, reads the
   file, and finds each key that is still absent. Just before the write, it
   checks the descriptor again: the same file, the same size. Then it
   appends one `KEY=VALUE` line for each absent key, with a line end before
   the first one when the last line has none. The new text must parse. The
   step writes one log line: `env vmm: wrote <keys> to <file>`.
8. A restore keeps each added key. The previous program reads only the keys
   that it names, so a key that it does not name costs nothing.
9. No value of the fill is a secret. The catalog and the site file hold
   none (`env/AGENTS.md`). A log line names each key and no value.
10. Release `handover` before the first manifest has an `env` table. An
    older executor or requester refuses that manifest.

## Venv builds and the action id

1. A `kind: venv` component is built with `uv venv --relocatable` first, so
   the tree survives its own rename.
2. The action id root pushes is a 26-character lower-case ULID.
3. A venv builds on an interpreter that the `requires-python` of its
   project allows. The floor thus names the Python of the host: 3.14 in
   each `pyproject.toml` of this repository. When the host has two
   interpreters that the floor allows, uv can build on either one.

## Binary builds

1. A `kind: binary` component is a tree of compiled programs. Each program
   is at `<install.to>/bin/<name>`, the layout of a venv.
2. CI builds it from the manifest's own `build` argv. CI packs the programs
   into one archive and signs the archive. Every `build` argv carries
   `--locked`, and `cargo install` carries `--no-track`. The first word of
   each `build` argv is the word `cargo`, with no path
   (`catalog.BINARY_BUILD_PROGRAM`). CI sets `CARGO_INSTALL_ROOT`
   (`catalog.BINARY_INSTALL_ENV`).
3. Root runs no command of the `build` argv. Check P6 verifies root's copy
   of the archive at step 3, and step 8 unpacks that copy into
   `<install.to>.new` (`executor/unpack.py`). No venv step runs: no
   `uv venv --relocatable` and no walk of `site-packages`.
4. The unit rules of a venv apply. The unit must start a program inside
   `install.to`, and a sibling unit travels with the component's own unit.
5. Every program that a unit or the verify command starts is a regular,
   executable file of the staged tree.
6. The staged tree is a directory. A link in its place is a fault. No link
   in the staged tree has an absolute target, leaves the tree or is in a
   loop. No file holds the path of the fetched work tree or has a second
   name.
7. `catalog.BINARY_BUILD_FILES` holds the Cargo workspace files of each
   repository. They move every binary component of that repository and no
   other kind. The input digest of a binary component covers those files
   and not `uv.lock`.
8. Change a component's kind in its catalog row and in its manifest in one
   commit. Discovery refuses a manifest whose kind is not the kind of its
   row, with the code `catalog`.
9. Release `handover` before the first manifest says `kind: binary`. An
   older executor or requester refuses that manifest.
10. Release `handover` alone before each release of a component that
    changes its kind. Root compares a manifest with the catalog of the
    installed code. Until the installed catalog holds the new row:
    - Root refuses the release of that component, with the code `catalog`.
    - The installed `handover request` refuses each request. It reads each
      manifest of its roots.
11. File the request for that release from a checkout that holds the new
    catalog, with `uv run handover request handover`. The `release` verb
    of the chaperone reads no manifest, so it can file the request too.
11a. A new row lands in two PRs, so rule 10 never refuses. The first PR
     adds the row and puts its name in `catalog.ARRIVING`. Discovery then
     reads a checkout with no manifest for that name as complete, and the
     allocator tags no such row. `handover` releases with the new row. The
     second PR adds the manifest and takes the name out of `ARRIVING`.
     `vmm` came this way: the row first, then `rust/component.toml`
     (CH7-5, CH76 rule 1). `disks` came the same way, then
     `disks/component.toml`.
11b. A row leaves in two PRs, the opposite of rule 11a. The first PR puts
     its name in `catalog.RETIRING`. Discovery then reads a checkout with
     or without the manifest of that name, and the allocator tags no such
     row. `handover` releases with the new set. The second PR deletes the
     manifest of that name, after a host installs that `handover`. It
     deletes the directory only when nothing else reads it. The row goes
     once no installed requester expects the manifest. `releasable_names()`
     keeps a retiring name, so a `host.toml` that still follows it reads
     (`follow/` rule 7). `playpen` left this way (CH86, Q78-2 A): its
     manifest and its row went in one PR, after hosts ran the requester
     of the first PR and dropped `playpen` from `host.toml`. Its
     `Dockerfile` and its disk scripts in `playpen/bin` stay, because the
     `disks` build reads them.
12. Check P6 (`executor/provenance.py`) starts the verifier, `cosign`, in
    two children. Each child runs in a transient unit of systemd, as a
    dynamic user with no privilege (`host.As.VERIFIER`). Child 1 loads the
    trust root. Child 2 checks the signature with no network. Root reads
    the exit status of each child and no text. After a pass, root reads
    the member list of its copy and writes no file. A pass gives a
    `VerifiedArtifact`. Only the check makes one, and step 8 unpacks only
    such a value.
12a. The dynamic user of the unit reads root's copy. Before the first
     child, root gives the work root (`layout.WORK_ROOT`) the mode 0755.
     Step 2 gives the same mode to each directory from `work/<id>` down to
     the archive.
13. Root starts a child of the verifier only when root trusts the verifier
    and `systemd-run` (`host.distrusted`). Each program is a regular file
    of root, and group and other cannot write it. Each directory above it,
    up to `/`, has the same owner rule. No link is followed. The check runs
    before each child. The site file names the two programs with the keys
    `AGENT_COSIGN_PROGRAM` and `AGENT_SYSTEMD_RUN_PROGRAM`, and each key has
    a default.
13a. For each program that fails the check, step 3 writes one line to the
     log of the ledger entry. The line names the path that failed and the
     reason. The refusal of P6 names no path.
14. Root's own time limit of a verifier child ends before the time limit
    of its unit. A child that does not end in its time is thus no answer,
    and the follower asks again.

## The chaperone, a binary component of its own repository

The catalog row of `chaperone` is `CatalogRow("chaperone", Repo.CHAPERONE,
".", Kind.BINARY, Releases.YES)`. It replaced the row of the Python
`chaperone`. This repository no longer holds the Python `chaperone`. The
`live-stack` branch keeps the old catalog.

1. The manifest is `component.toml` at the root of the chaperone
   repository. This repository holds no manifest of `chaperone`. Root reads
   the manifest from a clone of the chaperone repository, at the commit of
   the tag.
2. `tests/handover_chaperone_component.toml` holds a copy of the manifest.
   A test holds the copy equal to the text below.
3. The `release.yml` workflow of the chaperone repository builds the seven
   programs and packs them into one archive. It signs the archive and makes
   the tag `chaperone-v<version>`. `bin/allocate-tags.sh` does not run in
   that repository.
4. The archive is `chaperone-x86_64-unknown-linux-musl.tar.gz`. It holds
   seven members: `chaperone/bin/chaperone`, `chaperone/bin/chaperone-as`,
   `chaperone/bin/chaperone-attendance`, `chaperone/bin/chaperone-deny-all`,
   `chaperone/bin/chaperone-ha-approval`, `chaperone/bin/chaperone-ha-call`,
   and `chaperone/bin/chaperone-release`. The bundle is the name of the
   archive with `.sigstore.json` at the end.
5. Check P6 takes a signature only from `release.yml` of the chaperone
   repository, on `main` (`catalog.RELEASE_WORKFLOW`).
6. The Cargo workspace of the chaperone repository is at its root:
   `Cargo.lock`, `Cargo.toml` and `rust-toolchain.toml`. The row has the
   path `.`, so each path of that repository moves `chaperone`.
7. Root refuses a version below the installed version, with the code
   `monotonic`. The first tag of the Rust `chaperone` must be above the
   version of the Python `chaperone` that the host runs.
8. `chaperone verify` reads the front door address from the site file,
   under the key `CHAPERONE_LISTEN`.

The text of `component.toml` at the root of the chaperone repository:

```toml
manifest_version = "0.19"
name = "chaperone"
repo = "chaperone"
path = "."
kind = "binary"
unit = "creche-chaperone.service"
runs_as = "root"
build = [
  ["cargo", "install", "--locked", "--no-track", "--path", "crates/chaperone", "--bin", "chaperone"],
  ["cargo", "install", "--locked", "--no-track", "--path", "crates/run-as", "--bin", "chaperone-as"],
  ["cargo", "install", "--locked", "--no-track", "--path", "plugins/effect/included/attendance", "--bin", "chaperone-attendance"],
  ["cargo", "install", "--locked", "--no-track", "--path", "plugins/default/deny-all", "--bin", "chaperone-deny-all"],
  ["cargo", "install", "--locked", "--no-track", "--path", "plugins/effect/included/ha-approval", "--bin", "chaperone-ha-approval"],
  ["cargo", "install", "--locked", "--no-track", "--path", "plugins/effect/included/ha-call", "--bin", "chaperone-ha-call"],
  ["cargo", "install", "--locked", "--no-track", "--path", "plugins/effect/included/release", "--bin", "chaperone-release"],
]
install.to = "/opt/components/chaperone"
install.prev = "/opt/components/chaperone.prev"
verify.command = ["/opt/components/chaperone/bin/chaperone", "verify", "--json", "--env-file", "/etc/creche/site.env", "--listen-key", "CHAPERONE_LISTEN"]
verify.user = "root"
verify.timeout_s = 60
restore.mode = "automatic"
restore.keep = 3
release = true
```

Before the first release of the Rust `chaperone`, the operator does these
steps:

1. Add the chaperone repository to `/srv/agents/state/code-repos.txt`, with
   the directory name `chaperone`.
2. Start `creche-code-corpus-sync.service`. Then make sure that
   `/srv/agents/code/chaperone` exists.
3. Give the token of root read access to the contents and the Actions of
   the chaperone repository. Checks P1 to P5 read them.
4. Set `CHAPERONE_LISTEN` in the site file to the front door address.
5. Set `AGENT_GITHUB_REPO_CHAPERONE` in the site file only when the
   repository has another name on GitHub.
6. Release `handover` alone ("Binary builds", rule 10).

## The vmm, a binary component of this repository

The catalog row of `vmm` is `CatalogRow("vmm", Repo.CRECHE, "rust",
Kind.BINARY, Releases.YES)` (CH7-5, CH9, item 12 (1)). Its manifest is
`rust/component.toml`.

1. The archive is `vmm-x86_64-unknown-linux-musl.tar.gz`. It holds four
   members: `vmm/bin/vmm`, `vmm/bin/guest-init`,
   `vmm/bin/cloud-hypervisor` and `vmm/bin/ch-remote`. guest-init has no
   row of its own, so the vmm and its guest init come from one release
   (CH7-6, CH62 rules 17 and 18).
2. The manifest has three build argv lists: the vmm, guest-init, and the
   13 words of `CH_BUILD` of `bin/lib/chbuild.sh` (CH35 (3)). The kvm job
   builds CH with the same words (CH75).
   `bin/tests/test_vmm_manifest.py` holds the third argv equal to
   `CH_BUILD`.
3. `runs_as` is `operator`, and `install.to` is `/opt/components/vmm`.
   Root owns the tree, so the operator runs each program and changes none
   (CH7-5). The unit is the user unit `creche-vmm.service` (CH36 G17).
   The first release lists it under `manual`. The operator installs it once
   (design section 6.2 step 3).
4. `vmm verify --json --env-file /srv/agents/state/creche/vmm.env` checks
   the four programs, the version that CH prints and a static guest-init.
   It opens no socket and reads no disk (CH76 rule 2). `bin/fleet-up up`
   writes `vmm.env` (CH59 rule 1). The file must exist before the first
   vmm release. The `env` table of the vmm manifest lists each key of
   `vmm.env`, so a release also adds each absent key to the file ("The env
   fill"). `bin/tests/test_vmm_manifest.py` holds the table equal to
   `rust/crates/vmm/src/config.rs` and to the `VMM_KEYS` of
   `bin/fleet-up`.
5. The manifest requires `family-file` 0.19. Minor 16 is the first with
   the reach files that the vmm reads (CH76 rule 3), minor 17 adds the
   flavor `coding` (CH79 rule 1), minor 18 drops `files` (CH10-10), and
   minor 19 drops `sandbox.max_resident_processes` (CH8-6).
   Each floor is the current minor of its provider
   (`tests/test_handover_repo_manifests.py`).
6. The AppArmor slot profile does not ship in the archive. A change to
   `rust/crates/vmm-confine/apparmor/cloud-hypervisor-slots.profile` is a
   one-off sudo step on the host, which the PR names.
   `bin/tests/test_vmm_manifest.py` pins its SHA-256, so no change is
   silent (CH76 rule 9). A release never runs sudo (CH36 G20).
7. The gate job `vmm-archive` builds the archive unsigned on each PR that
   changes a file of the build (CH76 rule 4). A broken build then fails
   the PR, not the release run.
8. `handover artifacts --repo` reads only the manifest of each binary row
   (`discovery.read_named`). A CI runner has no site file, and each other
   manifest of this repository names a `~/` path, which needs one.

## Disk images

The catalog row of `disks` is `CatalogRow("disks", Repo.CRECHE, "disks",
Kind.DISK_IMAGE, Releases.YES, ("playpen", "agent", "facade",
"pyproject.toml", "uv.lock"))` (contract 06 §1 rule 15, CH7-2, CH35 (4),
CH76 rules 1 and 5). Its manifest is `disks/component.toml`, and
`disks/AGENTS.md` says how CI builds its files.

1. A `kind: disk-image` tree is a set of signed files at the root of
   `install.to`. CI builds and signs each file. Root checks each signature
   and installs each file unchanged. Root runs no build and unpacks
   nothing (contract 06 §8.4).
2. The files are `digests.json`, `vmlinux-x86_64.zst`, one
   `<flavor>.ext4.zst` for each flavor of `catalog.DISK_FLAVORS` and one
   `<image>.ext4.zst` for each service image of `catalog.SERVICE_IMAGES`.
   Each file has its own bundle `<file>.sigstore.json`. `artifacts.asset_limits`
   holds the list and the limits: `DISK_FILE_MAX_BYTES` (2 GiB - 1) for a
   disk and the kernel, `DIGESTS_MAX_BYTES` (64 KiB, the `DIGESTS_CAP` of
   the vmm) for `digests.json`, and `BUNDLE_MAX_BYTES` for each bundle.
3. `DISK_FLAVORS` is `base`, `python` and `coding`, the values of the wire
   enum `SandboxFlavor`. `tests/test_handover_disk_flavors.py` holds the
   two equal (CH58 rule 4, CH76 rule 7). `coding` joined before the cut
   (CH79 rule 1). `SERVICE_IMAGES` is `browser`, the values of the wire
   enum `ServiceImage` (browser design 10.5.2). A service image is not a
   flavor, and no family file can choose one. The same test holds
   `SERVICE_IMAGES` equal to the keys of
   `rust/crates/wire/vectors/digests.json` that are not `kernel` and not
   a flavor. The vmm parses that vector, so the keys are the values of
   the enum.
4. A disk-image manifest names no unit, and its `runs_as` is `none`. Its
   `build` argv starts with an absolute path, and only CI runs it, with
   `DISK_OUT` set (`catalog.DISK_OUT_ENV`).
5. Its verify program sits in the tree of a component that its
   `depends_on` names, and each `--env-file` of its hook is one of the hook
   of that component (contract 06 §4 rules 6 and 7, CH76 rule 6).
   `manifest.check_disk_image_hooks` holds both rules, and `resolve` calls
   it on each set. Rule 7 names the `EnvironmentFile=` of the unit of that
   component. The check reads no unit file. It compares each `--env-file`
   with the `--env-file` of the verify hook of that component, and
   `tests/test_handover_r7r_verify_env.py` holds that one equal to the
   `EnvironmentFile=` of its unit (CH78 rule 2).
6. `handover disk-assets --repo <repo>` prints each disk-image row: the
   component, `out-env DISK_OUT`, one `asset <name> <bytes>` line for each
   file and bundle, then each argv, then `end` (`artifacts.DiskRow`).
   `handover artifacts` prints binary rows only, so `read_rows` of
   `bin/lib/artifactrows.sh` never meets a disk line.
7. `artifacts.assets_expected` is true for `binary` and `disk-image`. So
   root's check P6 runs for a disk-image release, and P6 refuses with `no
   artifact in the store` when step 2 made no copy.
8. Release `handover` before the first manifest says `kind: disk-image`.
   An older executor or requester refuses that manifest.
9. The operator side fetches each asset of the row with its own
   `--max-filesize` (`corpus/artifacts.py`). The follower reads the member
   as ready only when the store holds each file and each bundle
   (`follow/` rule 7).
10. Root copies each file and its bundle by rule 5a, with the limit of
    that file, one read of the store in a release (`executor/artifact.py`).
    Each bundle is `<file>.sigstore.json`. The approval digest is
    `artifacts.sum_lines_digest`: the SHA-256 of the sorted `sha256sum`
    lines of root's copies of the files (contract 06 §9, CH76 rule 5).
11. Check P6 runs child 2 of the verifier once for each file, with the
    bundle of that file and the signer policy of a binary archive
    (`provenance.check_files`). A pass gives one `VerifiedFiles`, and only
    the check makes one.
12. Step 8 copies each verified file into `<install.to>.new` under its
    name, root's and 0644, and hashes what it wrote. Sum lines that differ
    from the approval digest fail the stage. Root runs no build, unpacks
    nothing and decompresses nothing. The walk of contract 06 §8.2 does not
    run (`Installer.stage_files`, `install.py` rule 11).
13. The verify program sits in the tree of the `depends_on` component
    that `manifest.hook_holder` names. The program must be a regular file
    inside the checked tree, owned by root, and no group or other account
    may write it. A disk-image row names no unit, so step 9 restarts
    nothing, and a failed hook puts the previous tree back (contract 06
    §8.4 rules 5 and 6).
14. One set may hold the `vmm` and `disks` (CH78 rule 1). Step 9 switches
    and verifies the `vmm` first, so the disks hook runs from the vmm tree
    that the same set installs. Step 8 checks the program in the
    `<install.to>.new` of the `vmm`, and a set of `disks` alone checks the
    live vmm tree. Step 8 stages in `plan.order`, and step 9 switches in
    `steps._switch_order`. A holder that does not come before the
    disk-image component there fails step 8 before any swap
    (`Release._hook_holder_tree`). A restore runs in reverse order, so
    `disks` goes back while the new `vmm` is still live.
15. Child 2 of check P6 for one disk file runs with `MemoryMax=` 2.5 GiB
    (`provenance.DISK_CHECK_MEMORY_BYTES`): cosign `verify-blob` reads the
    whole file into memory. Fact 84 (CH78 rule 4) measured cosign v3.1.4
    on the host: a peak of the file size plus about 60 MiB, so a file at
    the 2 GiB - 1 cap needs about 2.06 GiB.
16. `bin/allocate-tags.sh` takes every asset of the `--assets` lines, in
    pairs of a file and its bundle. It runs the verifier once for each
    pair, and the Release of a disk-image component holds the twelve assets
    (CH35 (2), CH76 rule 5). `bin/build-artifacts.sh` skips a planned
    disk-image row, and the `sign` job of `release.yml` signs each of the
    six files.
17. The job `disks` of `release.yml` runs the build of the row,
    `disks/bin/playpen-release-disks`, when the plan tags `disks`. Then
    `bin/check-disk-files.sh` refuses a file past its limit of
    `artifacts.asset_limits` before the `sign` job signs it (CH78 rule 3).
    The job hands its files and their `SHA256SUMS` to the `sign` job beside
    the archives of the `artifacts` job. `tests/test_handover_repo_manifests.py` holds
    the bundles of the row: what `playpen/Dockerfile` copies, and
    `playpen`.

## The host component of creche-release

The catalog row of `host` is `CatalogRow("host", Repo.CRECHE_RELEASE, ".",
Kind.UNITS, Releases.YES)`. It is the whole creche-release repository: the
units that belong to no component, and the scripts that they run (contract
06 §1 rule 12).

1. `host` is the one `kind: units` component. The reader refuses a `units`
   manifest that names a `unit` or that has a `build` (contract 06 §8.3).
2. `tests/test_handover_host_component.py` holds a copy of the manifest of
   creche-release. Change the copy when that manifest changes.
3. A host must hold a clone of creche-release at
   `/srv/agents/code/creche-release`. Root reads the source of a `host`
   release there. `catalog.ARRIVING_REPOS` is empty, so a typed `request`,
   `check` or `resolve` needs a `--root` for creche-release as well.
4. Step 8 of a `host` release copies the fetched tree into `host.new`
   (`install.py` rule 10). It leaves out the clone's `.git`. A link or any
   other entry that is not a directory or a regular file fails the stage.
5. Step 8 then reads the unit files of the staged tree
   (`executor/units.py`). It refuses with code `unit` any file under
   `units/` that is not `units/user/<unit>` or
   `units/system/<unit>.d/<name>.conf`, a handover unit, or a user unit that
   another manifest names as its `unit`. creche-release's `bin/check-tree`
   runs the same layout rules in its gate. Change both together.
6. Step 8 then checks the place of each unit file on the host
   (`Installer.check_adopt`). The place may hold nothing, the link to the
   file in the live tree, or a regular file with the same bytes. Anything
   else is refused with code `unit`, so root never overwrites a unit that
   nobody put in creche-release.
7. Step 9 links the unit files (contract 06 §8.3 items 2 to 6). Before the
   swap, root compares the live tree with the staged tree
   (`units.units_diff`). It stops and disables each user unit whose file
   goes, when the place of that file is root's own link. After the swap,
   root removes those links, checks each place again, and links each file
   to `<install.to>/units/...`. The operator writes in their own unit
   directory, and root writes the drop-ins. Then root reloads both
   managers, enables and starts each new timer, and restarts each changed
   timer. A template unit is linked, and never started or stopped.
   Last, root restarts each installed unit whose drop-in was added, changed
   or removed (contract 06 §8.3 item 6).
8. A restore runs the same steps again, from the failed tree to the
   previous tree. With no previous tree, the restore stops and no unit
   moves.
9. Step 5 names each unit whose drop-in the release changes in §2.5's
   `restarts` row, as `host: <unit>, ...`, from the live tree and the
   fetched tree. Step 9 compares its own list with that row before it moves
   anything of `host`. A different list fails the switch. The requester's
   preview says that root names these units.

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
8. `install.source` has four values: `pypi`, `github-release`,
   `forge-release` and `agent-mcp`. A `forge-release` pin has the fields
   of a `github-release` pin and one more, `host`. The build of the two
   sources is one path (`ASSET_SOURCES`). Only the host of the URL
   differs: `CODE_HOST` for `github-release`, and `host` for
   `forge-release`.
9. `host` is a DNS name in lower case, with two labels at least and 253
   characters at most. Its last label starts with a letter. Root refuses
   a scheme, a port, a path, a user, an IP address and `github.com`
   (`HOST_RE`, rule 17 of `mcpserver.py`).
10. Release `handover` before the first server file says
    `forge-release`. An older executor refuses that file.

## An `mcp-servers` release

1. The release builds and swaps the server trees under `/opt/mcp`. It does
   nothing more for the chaperone.
2. Root writes no roster. The Rust chaperone reads no roster.
3. Root sends no signal to `creche-chaperone.service`. The Rust chaperone
   has no handler for `SIGHUP`, so the signal ends its process.
4. A restore puts the server trees back. It writes no roster and sends no
   signal.
5. The operator adds a server to the chaperone by hand: one plugin
   manifest, one entry in `/etc/chaperone/install.toml`, and a restart of
   `creche-chaperone.service`. `bin/chaperone-install-draft.py` prints a
   draft of both files.

## The Forgejo release path, reading side

`docs/design/forgejo-release-path.md` section 5 gives checks P1 to P6 for a
release from the Forgejo instance. Three modules of `executor/` hold them.
Nothing calls them yet. A later PR connects them to step 3, with the HTTP
client, the site keys and the key file of FR10. The GitHub path of
`provenance.py` does not change.

```text
forge_api.py           the paths and the replies of the Forgejo API
release_statement.py   the tag message, the cosign check, the statement
forge_provenance.py    the rules P1 to P6 and the chain
```

1. `forge_api.py` is the one module that knows a Forgejo path or a field
   name of a Forgejo reply. Only its builders make a `ForgePath`, from a
   checked `ForgeRepo`, `TagName` and `ObjectId`.
2. Each reader of a reply checks the status, the byte cap
   `API_MAX_BYTES` and `strictjson.loads`, in that order. Then it reads
   each field and its type. A `bool` is no integer. A reply that is not
   strict records one notice with the surface `code_host.answer`.
3. Each refusal of a reader or of a rule has the code of its check and one
   fixed reason of `ForgeReason`, `StatementReason` or `ProvenanceReason`.
   No text of a reply reaches a refusal.
4. P2 takes only the ref that is exactly `refs/tags/<tag>`, because the
   refs call is a prefix match (M3). The ref names an annotated tag. A
   lightweight tag refuses.
5. P5 reads `total_count` and the `context` and the `status` of each entry.
   It never reads `state`. Each required context of `REQUIRED_CONTEXTS` is
   `success`. Each other context is `success` or `skipped`.
6. The tag message has the format of the signer crate
   `rust/crates/ci-signer`. That crate is the authority. Its vectors, its
   schema and its limits drive `tests/test_handover_frp_vectors.py`. Change
   a constant or a pattern of `release_statement.py` only with the same
   change in the crate.
7. Root reads the message only from the git tag object of P2, which adds
   one LF. `decode_message` does not parse the statement.
8. `check_signature` writes the statement and the bundle into
   `work/<id>/statement/<component>/`, with the modes of rule 12a. Then it
   starts one verifier child: `cosign verify-blob --key` with
   `/etc/creche/release-signing.pub`, no trust root and no network (FR1,
   FR12). Only this check makes a `SignedStatement`.
9. `read_statement` parses only a `SignedStatement`. It refuses a text that
   is not the canonical form byte for byte. Only it makes a
   `ReleaseStatement`, a `StatementTag` and a `StatementAsset`.
10. P1 checks each tag of the statement. Each tag names a component of the
    catalog in the same repository, with `Releases.YES`. A tag of a `venv`
    or `units` component names no asset.
11. Only `verify_forge_release` makes a `ForgeVerified`. P6
    (`check_digests`) takes the signed tag entry from that value, so no
    caller can give P6 an entry that no signature covers.
12. A refusal of P6 has one reason of `ArtifactRefusal`, as today.

## Use

```bash
uv run handover check --partial                     # this repository's manifests
uv run handover resolve chaperone=2.1.0 --root . --state live-state.json --id <ULID>
uv run handover request attendance chaperone@2.1.0 --root . --dry-run
uv run handover follow attendance chaperone --dry-run   # what the timer would do
uv run handover follow --dry-run                     # the same, from the live host.toml
```

`--partial` is what this repository needs: four of the catalog's components
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
| `artifacts.py`, `treewalk.py` | the names, the limits and the signer of the archive of a binary component and of the files of a disk-image component, the walk of a tree of compiled programs |
| `cli.py` | `check`, `resolve`, `allocate-tags`, `request`, `follow`, `artifacts`, `disk-assets`, `check-tree` |
| `requester/`, `corpus/` | one request file, one `git fetch`, and the fetch of the signed assets of a binary or disk-image component into the store (`corpus/artifacts.py`) |
| `follow/` | the set a newer tag moves, the marker, the decision |
| `executor/spool.py`, `request.py`, `live_state.py`, `provenance.py` | the five directories, the request, the built document, P1 to P6 |
| `executor/approval.py`, `phone.py`, `notice.py`, `quiet.py` | the gate and the text of the approval push, the transport, the outcome push, the busy check of step 7 and the quiet minutes |
| `executor/envfill.py` | the env fill: the plan of step 4 and the append of step 8 |
| `executor/forge_api.py` | the paths and the reply readers of the Forgejo API for P1 to P5 |
| `executor/release_statement.py` | the tag message, the cosign check of P1 and the release statement |
| `executor/forge_provenance.py` | the rules P1 to P6 on Forgejo, and the chain `verify_forge_release` |
| `executor/host.py`, `install.py`, `mcpbuild.py`, `self_switch.py` | the one spawn and the unit of the verifier, steps 8 to 10, a server tree, the self-release |
| `executor/unpack.py`, `artifact.py` | the unpack of a tar archive and the member list of a component archive, root's copy of the signed assets of a binary or disk-image component and its digest |
| `executor/ledger.py`, `steps.py`, `drain.py` | the entry, the ten steps, the unit's entry point |
| `bin/allocate-tags.sh` | the workflow's body: gather, plan, tag, release |

## Tests

```bash
uv run pytest handover/tests
```

No test needs a host. A real `git`, `uv`, `cargo` or `sops` runs against
`tmp_path`. A stand-in runs in the place of `cosign`, after the real check
of the two programs of the verifier. The `cargo` test skips on a machine
that has no `cargo`. A real
`curl` fetches from a loopback https server, and that test skips on a
machine that has no `curl` or no `openssl`.
TLS runs on loopback. Every test file's basename starts with
`test_handover_`. `handover_fixtures.py` builds valid manifests only. A test
that wants a refusal changes one field.

`tests/fixtures/forgejo-17.0.1/` holds sanitized replies of Forgejo 17.0.1.
Its `README.md` lists each file, and `index.tsv` gives the status of each
reply. Four more replies are read in place from the fixtures of the signer
crate. `test_handover_frp_fixtures.py` refuses a fixture that names a
deployment.

## Known gaps

- A `host.toml` that breaks a rule of `follow/policy.py` fails
  `creche-follow.service` with exit 2. The follower writes no row in its
  notice file for it, so the operator learns of it only from the failed
  unit.
- `follow` fetches four repositories in turn, each capped at 120 s.
  `creche-follow.service` stops a run at 300 s, so a run with three or more
  hung fetches ends with no request. The next firing fetches again.
- The checker runs C2, C3, C4, then C1 (`contracts.py`).
- A component named at its live version is `unchanged`, and step 9 restarts
  nothing (`resolve.py`).
- Every build that root runs uses `UV_PROJECT_ENVIRONMENT`. Every unit file
  is `systemd/<unit>`. No manifest can name another variable or another
  place (`manifest.py`, `executor/install.py`).
- Every component with no tag gets `0.1.0` (`allocate.py`).
- The per-requester cap counts one drain pass and keys on `requested_by`,
  which the requester writes (`executor/drain.py`).
- When the network connection of the host cannot fetch an archive inside
  one run of the follower, the follower never fetches it. One run has 270
  seconds, and the fetch resumes no partial file. `handover request` fetches
  with 600 seconds (`cli.py`). A disk file of 415 to 562 MiB needs about
  2.1 MiB/s in one run. Not checked: the rate from the host to the code
  host (plan risk 10).
- The follower does not ask again for a request that root never ledgered.
  A newer tag or a request by hand recovers (`follow/`).
- The follower reads tags out of the corpus. Root refuses a tag that has no
  Release, and that refusal holds the set (`follow/`).
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
- `arg_allows` is applied by nothing (`mcpserver.py`).
- Contract 01b §3.2 does not say where the program sits in an unpacked
  asset. Root reads the four places of "Rules `mcpbuild.py` adds" rule 6,
  and no other (`executor/mcpbuild.py`).
- Contract 01b §3 names no `forge-release` source and no `host` field.
  Root takes the strict reading: a DNS name in lower case, no name of one
  label, no end dot, and no list of allowed hosts. The file that names
  the host is the file that the operator reviews (`mcpserver.py`,
  `HOST_RE`).
- Contract 06 §3 names `chaperone` as the owner of `pep-grant`. The
  manifest of the Rust `chaperone` provides no contract. Each floor on
  `pep-grant` is then a line `not verified`, and not a refusal
  (`contracts.py`).
- The manifest of the Rust `chaperone` names `creche-chaperone.service`.
  The clone of the chaperone repository holds
  `systemd/creche-chaperone.service`. Step 8 stages that file and step 9
  installs it (`executor/steps.py`).
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
- A manifest whose chain of merge keys passes the limit gets the refusal
  text `nests deeper than 128 levels`. The Rust reader gives such a chain
  that text. The Python reader still reads plain nesting deeper than 128
  levels (`manifest.py`).
- The secret-name pattern is copied into more than one module, and the
  copies agree only by hand.
- The requester refuses once `requests/` holds eight entries of any owner
  (`requester/file.py`).
- `stage7-releases.md` §2.4 step 3 says that root reads only the exit
  status of the verifier, and §2.6 lists the four reasons of P6. Neither
  section gives a reason to an exit status. Through `systemd-run --wait`,
  a child that does not start ends with the status 1. A child past the
  time limit of its unit ends with the same status, as a refused signature
  does. Root checks the two program files before each child, and its own
  time limit ends first. Each other status of child 2 that is not 0 is a
  refused signature (`executor/provenance.py`).
- Contract 06 §1 rule 10 names no file of a Cargo workspace. In this
  repository, three files move a binary component: `rust/Cargo.lock`,
  `rust/Cargo.toml` and `rust/rust-toolchain.toml`. A
  `rust/.cargo/config.toml` moves none. In the chaperone repository, each
  path moves `chaperone` (`catalog.py`).
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
  a path that it only prints. CI builds with a remap of the paths of the
  runner, and it runs the walk with those paths before it signs
  (`treewalk.py`).
- A run that ends in the way of a crash keeps `work/<id>`. No later run
  removes it. It holds root's copy of each archive of the release
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
- Contract 06 §8.3 names no rule for a link in a `units` tree. Step 8
  refuses each link, also one that stays inside the tree. It leaves out the
  clone's top-level `.git` and nothing else (`executor/install.py`).
- Contract 06 §1 rule 12 says "the reader refuses" a `units` tree that holds
  a wrong file, and names no step. Step 8 refuses it, on the staged tree.
  The check knows only the units that the manifests of the release name. A
  sibling unit, and the unit of a component with no install tree, are not
  in that set (`executor/units.py`).
- Contract 06 §8.3 item 3 says step 8 refuses a unit file "before it builds
  anything". The check runs after the copy of the `host` tree and before
  the swap. Another component of the set can be built by then. It refuses
  every link that does not name the file in the live tree, also a link to
  another file of the `host` tree (`executor/install.py`).
- A repair after a crash in the switch of a `host` tree puts the tree back
  and does not link the unit files again (`executor/steps.py`).
- `forgejo-release-path.md` section 3 M7 does not measure the cosign v3
  flags for `verify-blob --key` with a bundle and no log entry. Root passes
  `--insecure-ignore-tlog=true`. A wrong flag refuses P1
  (`executor/release_statement.py`, `VERIFY_SKIP_TLOG`).
- FR10 says that the executor refuses P1 without the key file. No check of
  the owner or the mode of `/etc/creche/release-signing.pub` runs yet. A
  missing key makes cosign end with the status 1, so P1 refuses with the
  reason of a refused signature, and the follower does not ask again
  (`executor/release_statement.py`).
- The page size of `git/refs/tags/<prefix>` is not measured. P2 reads one
  page. When the page drops the exact ref, P2 refuses
  (`executor/forge_api.py`, `read_tag_refs`).
- Section 5 names only `object.sha` and `message` of `git/tags/<sha>`. P2
  also requires `sha` and `tag` of the reply to be the tag object and the
  tag (`executor/forge_api.py`, `read_tag_object`).
- The page size of `commits/<sha>/status` is not measured. P5 refuses when
  `total_count` is not the count of the listed entries
  (`executor/forge_provenance.py`, `check_green`).
- No `pull_request` context of the five repositories is measured on
  Forgejo. `REQUIRED_CONTEXTS` holds the names of section 6. A missing
  context refuses P5 (`executor/forge_provenance.py`).
- Section 5 names only the statuses `success` and `skipped`. P5 refuses
  each other status, and it refuses a word that Forgejo does not use with
  its own reason (`executor/forge_provenance.py`, `KNOWN_STATUSES`).
- FR9 adds mcp and creche-release to `catalog.RELEASE_WORKFLOW`. Until a
  later PR adds them, `expected_origin` refuses a release of each one with
  the code `catalog` (`executor/forge_provenance.py`).
- Section 5 does not say whether P1 checks each tag of the statement. P1
  checks each one (`executor/forge_provenance.py`, `check_statement`).
- Section 5 names no check of `signed_at` against a clock and no check of
  the run id. P1 checks the grammar of both and nothing more
  (`executor/release_statement.py`).
- Contract 06 §8.2 and PEP 610 name no size for a `direct_url.json` record.
  Root reads 64 KiB of such a record at most. A larger record is a record
  that root cannot read, and the walk reports it as a fault
  (`executor/selfcontained.py`).
