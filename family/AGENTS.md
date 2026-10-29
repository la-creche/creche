# family

The one place the family file and the server file are defined. A family
file is `family.toml`. A server file is `mcp/<name>/server.toml`. Each file
is TOML. The package is
`agent_family`. `caregiver` is its only caller. The root `AGENTS.md` applies
here too.

Two products come out of it:

- Validated objects: `FamilyFile` and `McpServerFile`.
- A `Report`: a list of `Issue` rows with severity, location and message.

Bad input never raises. A bad definition yields a report, not a crash.

## Layout

| Module | Owns |
|---|---|
| `grammar.py` | names, ranges and small parsers. One pattern per identifier. The copies of the grammar of root for a server file. The two file names. |
| `model.py` | the family file shape only: fields, types, `extra="forbid"` |
| `server.py` | the server file shape, same rule |
| `parse.py` | TOML text to a model, or to issues. Never an exception. |
| `tomlfile.py` | the TOML reader: the byte limit of each file kind, the level limit, the TOML 1.0 check and the four texts of a refusal |
| `report.py` | `Issue`, `Report`, `FamilyState` |
| `validate.py` | the family file value and cross-reference rules |
| `serverrules.py` | the server file value rules, and the rules of root |
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

## The name of a file

`grammar.py` holds the name of each file kind, and `registry.py` gives it on:

| Kind | Name |
|---|---|
| family file | `FAMILY_FILE`, `family.toml` |
| server file | `SERVER_FILE`, `server.toml` |

The names have no other source. A package that imports `agent_family` takes
each name from `registry.py`. `validate.py` takes the name of a server file
from `grammar.py`, because `registry.py` imports `validate.py`. The
process-level suite imports no package of a service
(`integration/proc/AGENTS.md`, rule 2), so it keeps its own text of each
name.

For each kind, the registry reads a directory in this way:

1. The registry reads the file of the name of the kind. It reads no other
   file of the directory. A `family.yaml` or a `server.yaml` is no file of
   the registry.
2. A directory with no such file gets a warning, for example
   `no server.toml; this directory is ignored`.
3. The report names the file of the name of the kind.

## The reader

`parse_family(text)` and `parse_server(text)` read a TOML text. `Markup` has
one member, `Markup.TOML`, and it is the default of the two functions.

- `tomlfile.loads` reads the bytes of the text with `FileKind.FAMILY` or
  `FileKind.SERVER`. The byte limit of the kind is the one size cap of the
  reader. A refusal is one error at `<document>`. Its message is one of
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
  for a text.
- `loads` gives the keys of each table in code point order. The Rust TOML
  reader gives sorted keys too, so a port reports the issues of one text
  in the same order.

`tomlfile.py` imports only from the standard library. Its doc string names
no package of this repository and no path in it. Another package can thus
hold a byte copy of the file. `handover/src/handover/tomlfile.py` is such a
copy. `bin/tests/test_tomlfile_numbers.py` holds the two files equal, byte
for byte.

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

## The grammar of root for a server file

Root reads each server file again at a release, with
`handover.mcpserver`. Under contract 01b §1, root accepts each server file
that the validator accepts. A server file gets each rule of contract 01b and
each rule of root:

1. An identity and a description count each character.
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

The validator also keeps two rules that root does not hold:

1. An identity has 300 characters at most. Root takes 1000.
2. A description of white space only is an error. Root takes it.

`grammar.py` holds a copy of each pattern and each cap of root that
contract 01b does not give. Each copy has the name of its source in
`handover.mcpserver`, and a copy of a pattern adds `^` and `\Z`.
`serverrules.py` holds two more copies: `_LOCK_PATH` is `LOCK_PATH_RE`, and
`_ARG_NAME` is `ARG_NAME_RE`. The argument of a fence has two parts at most
in both readers. `bin/tests/test_server_grammar_copies.py` holds each copy
equal to its source. The package `family` does not import `handover`, so
the test is in `bin/tests`.

`bin/tests/test_server_file_readers.py` holds the two readers equal on each
server file of the fixture registry. Root and the validator accept each
one. The test also gives root a text for each rule of the list above. Root
refuses each one, and so does the validator.

## What it enforces

| Axis | Rule |
|---|---|
| Unknown fields | refused on every model |
| `name` | `[a-z][a-z0-9-]{1,30}`, equal to the directory name |
| Mounts | absolute, no glob, under an allowed root. No root holds a working copy of the platform. |
| `tools` | the server is declared, and each tool is in its catalog. Only a family of `PLATFORM_ALLOWLIST` holds `github-platform` |
| Tool name | `[A-Za-z][A-Za-z0-9_-]{0,63}`, in a family file and in a server file |
| `run.env` name | `[A-Z][A-Z0-9_]{0,63}` |
| `install.version` | `[0-9][0-9A-Za-z.]{0,63}`: no `+`, no `-` and no range sign |
| `tools: all` on `autonomous` | refused |
| `delegates` | each target exists and is `kind: thin` |
| `max_inflight_delegations` | 1 to 8, default 2 |
| `egress` | no IP literal, no wildcard, port 1 to 65535 |
| `sandbox_tools` | Flue's six names only: `read`, `write`, `edit`, `bash`, `grep`, `glob`. No name two times. The default is empty. `codemode` is not a name: the agent mounts it for every family. |
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
- Contract 01 §3.8 names other sandbox tools: `find`, `ls`, `codemode`,
  with a default of `read`, `grep`, `find`, `ls`, and `shell` as the only
  way to get `bash`. The owner decided on Flue's six names and an empty
  default (`grammar.py`, `SandboxTool`). The owner updates the contract.
- These `CONTRACT-QUESTION` comments are open:
  1. `validate.py`, `_check_cron`, contract 01 §3.13. The contract gives no
     grammar for a cron field. The check takes ASCII digits and `*`, `,`,
     `-`, `/`. It refuses a month name and a day name.
  2. `model.py`, `Strict`, contract 01 §2 and contract 01b. The contracts do
     not say how strict the read of a type is. The reader uses the strict
     mode.
  3. `serverrules.py`, seven comments, contract 01b §3 to §7. The contract
     does not give these rules, and the validator takes the rules of root
     ("The grammar of root for a server file"):
     - The count of an identity and of a description.
     - The grammar of `package`, `repo`, `asset`, `python`, `entrypoint`
       and `state_dir_env`.
     - The grammar of a value and of a secret key.
     - A fence with an empty list of tools.
     - Each other cap on a length and on a count.

     The two readers have one form for a tool name, for the name of a
     variable and for a version. `grammar.py` holds the three forms of this
     validator. `handover.mcpserver` holds the three patterns of root.
