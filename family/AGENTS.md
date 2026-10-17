# family

The one place the family file and the server file are defined. A family
file is `family.toml` or `family.yaml`. A server file is
`mcp/<name>/server.toml` or `mcp/<name>/server.yaml`. The package is
`agent_family`. `caregiver` is its only caller. The root `AGENTS.md` applies
here too.

Two products come out of it:

- Validated objects: `FamilyFile` and `McpServerFile`.
- A `Report`: a list of `Issue` rows with severity, location and message.

Bad input never raises. A bad definition yields a report, not a crash.

## Layout

| Module | Owns |
|---|---|
| `grammar.py` | names, ranges and small parsers. One pattern per identifier. The copies of the grammar of root for a server file. |
| `model.py` | the family file shape only: fields, types, `extra="forbid"` |
| `server.py` | the server file shape, same rule |
| `parse.py` | YAML or TOML text to a model, or to issues. Never an exception. |
| `tomlfile.py` | the TOML reader: the byte limit of each file kind, the level limit, the TOML 1.0 check and the four texts of a refusal |
| `report.py` | `Issue`, `Report`, `FamilyState` |
| `validate.py` | the family file value and cross-reference rules |
| `serverrules.py` | the server file value rules. A TOML file also gets the rules of root. |
| `registry.py` | reads `families/`, `mcp/`, `skills/`. One report per family. |
| `classify.py` | pure: two parsed families in, a live-or-replace diff out |
| `cli.py` | `agent-family validate` |

Shape lives in `model.py` and `server.py`. Values live in `validate.py` and
`serverrules.py`. The split is what lets one parse report every violation
instead of the first.

## Rules

- Never raise on bad input. `parse_family`, `parse_server`, `check_family`
  and `check_server` return issues.
- Every schema model inherits `Strict`: `extra="forbid", frozen=True`. An
  unknown field is refused, named, with the closest known field suggested.
- A field that needs a regex, a range or another file's content is a value
  rule. It is never a pydantic field validator.
- One pattern per identifier, in `grammar.py`.
- `ALLOWED_ROOTS` and `PLATFORM_ALLOWLIST` are constants in code. A fence the
  fenced family could edit is not a fence.
- The two host-dependent checks downgrade, never skip. With no `HostFacts`
  they report a warning with `downgraded=True`.
- `classify.py` stays pure. No clock, no disk, no import of `validate.py`.
- An `approval` entry only narrows. Adding one removes reach. Do not treat
  `approval` like `tools` or `egress`.
- `all` resolves against the server file at read time, in
  `Index.granted_tools` only.

## The two names of a file

`registry.py` holds the two names of each file kind:

| Kind | TOML name | YAML name |
|---|---|---|
| family file | `FAMILY_FILE`, `family.toml` | `FAMILY_FILE_YAML`, `family.yaml` |
| server file | `SERVER_FILE`, `server.toml` | `SERVER_FILE_YAML`, `server.yaml` |

The names have no other source. A package that imports `agent_family` takes
each name from `registry.py`. "Known gaps" lists the copies that are left.
The process-level suite imports no package of a service
(`integration/proc/AGENTS.md`, rule 2), so it keeps its own text of each
name.

For each kind, the registry reads a directory in this way:

1. The registry reads the TOML name first and the YAML name second.
2. A directory with both names gets one error at `<document>`, for example
   `two server files: server.toml and server.yaml; keep one`. The registry
   does not parse either file.
3. A directory with neither name keeps the warning of today, for example
   `no server.yaml; this directory is ignored`.
4. The report names the file that the registry read. With both names, it
   names the TOML name. The contracts do not decide this case ("Known
   gaps").

A registry can hold files of both markups. A family in `family.toml` finds
a server in `server.yaml`, and a family in `family.yaml` finds a server in
`server.toml`.

## The TOML path

`parse_family(text, Markup.TOML)` and `parse_server(text, Markup.TOML)` read
a TOML text. The default markup is `Markup.YAML`, so a caller that gives one
argument reads YAML as before.

- `tomlfile.loads` reads the bytes of the text with `FileKind.FAMILY` or
  `FileKind.SERVER`. The byte limit of the kind is the one size cap of the
  TOML path. A refusal is one error at `<document>`. Its message is one of
  the four texts of the reader. A text holds no line and no character of
  the file.
- The doc string of `tomlfile.py` lists the seven checks of `loads` in
  their sequence. Check 6 refuses an integer outside 64 bits. It also
  refuses a float text with a digit and a value that is not finite, for
  example `1e999`.
- The registry reads a TOML file as bytes, and it reads the byte limit of
  its kind plus one byte at most. It gives the bytes to `parse_family_toml`
  or to `parse_server_toml`, so the reader judges each byte of the file.
- The model validates in the strict mode of pydantic. A field takes only
  the type of the model. A float field also takes an integer. The model
  refuses `"2"` and `2.0` for an integer, `"yes"` for a boolean and a date
  for a text. The YAML path keeps the lax mode.
- `loads` gives the keys of each table in code point order. The Rust TOML
  reader gives sorted keys too, so a port reports the issues of one text
  in the same order.

`tomlfile.py` imports only from the standard library. Its doc string names
no package of this repository and no path in it. Another package can thus
hold a byte copy of the file. It
copies the four byte limits, the level limit and the four texts from
`rust/crates/creche-contracts/src/tomlfile.rs`, which is their source.
`bin/tests/test_tomlfile_numbers.py` holds each copy equal to the Rust file.

### The check of the six forms of TOML 1.1

The `tomllib` of Python 3.12 to 3.14 reads TOML 1.0. The `tomllib` of Python
3.15 reads TOML 1.1. `_only_toml10` refuses the six forms that TOML 1.1
added, under each Python version. `loads` calls it after `tomllib` read the
text.

1. A line end in an inline table. A line end in a string, or in an array
   that the inline table holds, does not count.
2. A comma as the last sign before the `}` of an inline table.
3. The escape `\e` in a basic string.
4. The escape `\x` in a basic string.
5. A time with no seconds, for example `07:32`.
6. A date-time with no seconds, for example `1979-05-27T07:32Z`.

Python 3.12 to 3.14 refuse each form before the check runs. The tests thus
call `_only_toml10` directly, and one test gives `loads` a stand-in parser
that reads TOML 1.1.

## The grammar of root for a TOML server file

Root reads each server file again at a release, with
`handover.mcpserver`. Under contract 01b §1, root accepts each TOML server
file that the validator accepts. `check_server` takes the markup of the
file. A TOML file gets each rule of the YAML path and each rule of root that
the YAML path does not hold:

1. An identity and a description count each character. The YAML path
   counts after the collapse of white space.
2. The grammar of `package`, `repo`, `asset`, `python`, `entrypoint` and
   `state_dir_env`.
3. A value of `run.env`, a word of `run.args` and a value of a fence have
   256 characters at most and no NUL character.
4. The key after `secret:` matches `[a-z][a-z0-9_]{1,62}`.
5. A fence names one tool at least, or `all`.
6. Each count has a cap:
   - 200 tools.
   - 32 words of `run.args`.
   - 32 variables of `run.env`.
   - 64 entries of `arg_allows` and 64 entries of `arg_denies`.
   - 256 values of a fence.
   - 8 shared secrets.

The TOML path also keeps two rules of the YAML path that root does not
hold:

1. An identity has 300 characters at most. Root takes 1000.
2. A description of white space only is an error. Root takes it.

`grammar.py` holds a copy of each pattern and each cap of root that the
YAML path does not hold. Each copy has the name of its source in
`handover.mcpserver`, and a copy of a pattern adds `^` and `\Z`.
`serverrules.py` holds two more copies, which both paths apply:
`_LOCK_PATH` is `LOCK_PATH_RE`, and `_ARG_NAME` is `ARG_NAME_RE`. The
argument of a fence has two parts at most in both readers.
`bin/tests/test_server_grammar_copies.py` holds each copy equal to its
source. The package `family` does not import `handover`, so the test is in
`bin/tests`.

`bin/tests/test_yaml_to_toml_models.py` holds the two readers equal on each
server file of the fixture registry. Root accepts the YAML text, and the
validator accepts its TOML text. The test also gives root the YAML text of
each rule of the list above. Root refuses each one, the YAML path accepts
it, and the TOML path refuses its TOML text.

## What it enforces

| Axis | Rule |
|---|---|
| Unknown fields | refused on every model |
| `name` | `[a-z][a-z0-9-]{1,30}`, equal to the directory name |
| Mounts | absolute, no glob, under an allowed root. The platform root is fenced to one family. |
| `tools` | the server is declared, and each tool is in its catalog |
| Tool name | `[A-Za-z][A-Za-z0-9_-]{0,63}`, in a family file and in a server file |
| `run.env` name | `[A-Z][A-Z0-9_]{0,63}` |
| `install.version` | `[0-9][0-9A-Za-z.]{0,63}`: no `+`, no `-` and no range sign |
| `tools: all` on `autonomous` | refused |
| `delegates` | each target exists and is `kind: thin` |
| `max_inflight_delegations` | 1 to 8, default 2 |
| `egress` | no IP literal, no wildcard, port 1 to 65535 |
| `triggers` cron | five fields of ASCII digits and `*`, `,`, `-`, `/`, or one of the three short forms |
| `approval` | every entry names something the file grants. No two overlap. |

Absence is denial. No mount, no read. No tool grant, no call.

## Use

```bash
uv run agent-family validate <registry path>
uv run agent-family validate <registry path> --family chat --json
```

Exit codes: 0 every report is clean, 1 at least one error, 2 a usage
mistake.

## Tests

```bash
uv run pytest family/tests
```

- `family_helpers.py` is not a `conftest.py`. Every test directory is on
  `sys.path`, so a second `conftest` module would shadow another package's.
- Helpers take the fixture name as `fixture`, not `name`, because a test may
  override the family's own `name` field.
- A fence test edits one fixture family and checks it against the unedited
  rest of the fixture registry.

## Known gaps

- The schema has no per-job budget and no per-job turn cap (`model.py`).
- No rule refuses `embed` without an index mount (`validate.py`).
- Four files still hold the text `family.yaml` and do not take it from
  `registry.py`:
  1. `caregiver/src/caregiver/paths.py`, the name of the applied copy.
  2. `caregiver/src/caregiver/reconcile.py`, the path of the registry file.
  3. `noticeboard/src/noticeboard/registrywrite.py`.
  4. `vectors/surfaces/family_file.py`.

  The TOML packet of each package changes its file:
  `toml-caregiver-applied`, `toml-noticeboard-write` and
  `toml-family-read-rust`.
- Three files still hold the text `server.yaml` and do not take it from
  `registry.py`:
  1. `validate.py`, in three messages of the family file rules. Packet
     `toml-drop-yaml-validator-python` changes them.
  2. `vectors/surfaces/server_file.py`, the constant `SERVER_FILE`. Packet
     `toml-server-read-rust` changes it.
  3. `caregiver/src/caregiver/mcp_wire.py`, in one message. No packet
     changes it yet.
- These `CONTRACT-QUESTION` comments are open:
  1. `validate.py`, `_check_cron`, contract 01 §3.13. The contract gives no
     grammar for a cron field. The check takes ASCII digits and `*`, `,`,
     `-`, `/`. It refuses a month name and a day name.
  2. `model.py`, `Strict`, contract 01 §2 and contract 01b. The contracts do
     not say how strict the read of a type is. The YAML path reads `"2"` as
     the integer 2, `"yes"` as true and `true` as the number 1. The TOML
     path uses the strict mode.
  3. `serverrules.py`, seven comments, contract 01b §3 to §7. These
     differences apply to a YAML server file only. A TOML server file gets
     the grammar of root ("The grammar of root for a TOML server file").
     `handover.mcpserver` reads the same file as root and refuses more YAML
     files. A YAML file can thus pass here, and root can refuse it at a
     release. The contract does not decide these differences:
     - The count of an identity and of a description. The YAML path counts
       after the collapse of whitespace. Root counts each character.
     - The grammar of `package`, `repo`, `asset`, `python`, `entrypoint`
       and `state_dir_env`.
     - The grammar of a value and of a secret key.
     - A fence with an empty list of tools.
     - Each other cap on a length and on a count.

     The two readers have one form for a tool name, for the name of a
     variable and for a version. `grammar.py` holds the three forms of this
     validator. `handover.mcpserver` holds the three patterns of root.
  4. `parse.py`, `_MERGE_DEPTH_MAX` and `_MERGED_ENTRIES_MAX`, contract 01
     §1 and contract 01b §1. The contracts give no limit for merge keys. The
     reader follows a chain of 400 merge keys at most. It makes 100,000
     entries from merge keys at most. A merged value with no entry counts
     as one entry. The reader refuses a text past a limit. The Rust reader
     of a family file and of a server file has the same two limits.

     The reader uses two calls for each merge key of a chain. With the
     default recursion limit of Python, the chain limit holds for a caller
     that is less than 180 calls deep. With a deeper caller, the reader can
     refuse a shorter chain. It then gives the report for a text that nests
     too deep. The Rust reader reads that chain.
  5. `registry.py`, `_read_directory`, contract 01 §1 and contract 01b §1.
     Each contract names one file, `family.toml` and `server.toml`.
     `spec.md` §12.3 row 5 makes a directory with both names a refusal. It
     does not say which name the report names. The registry gives one
     error and parses neither file. The report names the TOML name. The
     other reading names the YAML name. The Rust reader of a registry must
     give the same answer.
