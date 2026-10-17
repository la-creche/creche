# agent-family

The Rust port of the Python package `agent_family`: the validator of the
family file and of the server file, the registry loader and the
`agent-family` program. A family file is `families/<name>/family.toml` or
`family.yaml`. A server file is `mcp/<name>/server.toml` or `server.yaml`.
`rust/AGENTS.md` and the root `AGENTS.md` apply here too.

The Python package is the authority until a release of `caregiver` uses this
crate. This crate replaces nothing. Each function gives the answer of its
Python function: the same values, the same issues in the same order and the
same messages.

## Layout

| Path | What it holds |
|---|---|
| `src/yaml/` | A YAML reader that reads a text as PyYAML 6.0.3 reads it. |
| `src/lax.rs` | A value to a number, a boolean or a string, as pydantic reads it. It also holds the texts of a wrong type in the strict mode, which the TOML path writes. |
| `src/shape.rs` | A YAML value to a raw file. It reports each wrong type and each unknown field. It also holds the Python class name of each nested model and the texts of the shape, for both paths. |
| `src/difflib.rs` | The nearest known name for an unknown field. |
| `src/parse.rs` | `parse_family` and `parse_server`: YAML or TOML text to a raw file, or to issues. |
| `src/crossref.rs` | `check_family` and `check_server`: the rules that need the registry or the host. |
| `src/registry.rs` | `load_registry` and `revision_of`. |
| `src/classify.rs` | `classify`: live or replacement, field by field. |
| `src/zones.rs` | Whether the host knows a time zone. |
| `src/scratch.rs` | Test code only: a directory that one test fills. |
| `src/report.rs`, `src/json.rs` | The report, and its JSON text. |
| `src/cli.rs` | The command line of the program. `run` prints the report. `entry` is the entry function of the program. |
| `src/main.rs` | The `main` of the program: three steps and no other code. |
| `tests/vectors.rs` | The differential test against the Python package. |
| `tests/process.rs` | The tests that start the program as a process. |

The types are in `creche-contracts`: `family` holds `RawFamily`, `Family` and
the report types, and `server` holds `RawServer` and `Server`.
`RawFamily::from_toml` of `family` reads a TOML family file, and
`RawServer::from_toml` of `server` reads a TOML server file.

## How one file becomes a report

1. `yaml::load_all` reads the text. The result is each document, as PyYAML
   gives it to Python.
2. `shape` reads the first document into a raw file. A raw file holds each
   field as the file wrote it. One read reports each fault of the shape.
3. `Family::try_from` checks each rule that one file decides. It gives a
   `Family`, or each issue.
4. `crossref` checks each rule that needs another file or the host.
5. Each issue has a `Slot`. A stable sort on the slot puts the issues of
   steps 3 and 4 into the one order of the Python report.

A `Family` exists only through step 3. `Registry::families` holds a family
only when steps 3 and 4 give no error.

A TOML family file and a TOML server file take another path for steps 1
and 2:

1. `RawFamily::from_toml` or `RawServer::from_toml` of `creche-contracts`
   reads the bytes with the TOML reader of that crate. It gives a raw file, a
   refusal of the reader or each fault of the shape. A fault holds no
   message.
2. `parse.rs` writes the message of each fault with the texts of the YAML
   path. The TOML path reads each field in the strict mode of pydantic: a
   field takes the kind of its type, and a float field also takes an
   integer.
3. Steps 3 to 5 are the same for both formats, with one difference for a
   server file. "The grammar of root" below has it.

The loader reads `family.toml` first and `family.yaml` second, and
`server.toml` first and `server.yaml` second. A directory with both names of
one kind gets one error, and the loader parses neither file. The report
names the TOML name.

## The grammar of root

Root reads each server file again at a release. Contract 01b §1 makes root
accept each TOML file that the validator accepts. `Server::from_raw` of
`creche-contracts` thus gets the markup of the file. A TOML server file gets
each rule of root that the YAML path does not hold:

1. The grammar of `install.package`, `install.repo`, `install.asset`,
   `install.python`, `run.entrypoint` and `run.state_dir_env`.
2. A cap and no NUL character for a word of `run.args`, a value of
   `run.env` and a value of a fence.
3. The grammar of `ids::SecretName` for the key after `secret:`.
4. A count of each character of an identity and of a description. A text
   of white space only gets a message of its own.
5. One tool at least in the list of a fence.
6. A cap on each of these counts:
   - The tools.
   - The words of `run.args`.
   - The variables of `run.env`.
   - The fences of each kind.
   - The values of one fence.
   - The shared secrets.

A YAML server file keeps the rules of the Python YAML path. The vectors of
`server_file.toml` hold each rule at its limit and one past it.

`TryFrom<RawServer>` and the `Deserialize` of `Server` name no markup. Each
one applies the grammar of a TOML file. Only `Server::from_raw` with
`Markup::Yaml` applies the YAML rules.

## Rules

1. Change no rule here without a vector. `tests/vectors.rs` walks each
   vector of the seven surfaces of this crate.
2. For a difference from the Python code, do what "The differential test"
   of `rust/AGENTS.md` says.
3. Keep the message of an issue equal to the Python message. The operator
   reads one text from both validators.
4. `src/yaml/` is a port. Each function has the name of its PyYAML function
   and does the same steps in the same order. Do not improve it. A YAML
   reader with other rules accepts another set of family files.
5. Add no YAML crate. Current Rust readers are YAML 1.2 readers. PyYAML is a
   YAML 1.1 reader: `yes` is a boolean, `010` is eight and `1:30` is ninety.
6. Name no input that makes the Python code raise, in code, in a test or in
   a document. `vectors/AGENTS.md` rule 5 gives the reason. Refuse such an
   input, and report it as `SECURITY.md` says.

## The YAML reader

`src/yaml/mod.rs` holds the license notice of PyYAML. Keep it.

The reader gives the same value as PyYAML for a text, and the same message
for a text that PyYAML refuses. A fuzz run compared more than 100,000
changed copies of the fixture files with PyYAML. Each value and each
message was equal.

`LoadError::Syntax` holds a message of PyYAML. `LoadError::Unreadable` is for
a text with a value that this reader cannot make. Its message is the message
of this reader.

## The program

```bash
cd rust && cargo run -p agent-family -- validate <registry path>
cd rust && cargo run -p agent-family -- validate <registry path> --family chat --json
```

The exit status is 0 when each report is clean, 1 when a report holds an
error and 2 for a usage mistake. The output is the output of the Python
program, byte for byte. The surface `family_file.cli` holds the Python
output.

A file that the loader cannot read or cannot parse gets a report with an
error. The program does not stop on such a file.

`main` calls `entry`, and the body of `entry` is one call of
`service::enter` of `creche-runtime`. A panic thus ends the program with
exit status 1. The log then holds the place of the panic and never its
message.

Only `main` reads the environment, one time. `entry` gets the result and
uses no variable of it today.

Rules for the program:

1. Keep the three steps of `main`: `log::init`, `Env::from_os` and the call
   of `entry`. Add no other code there (`rust/AGENTS.md`, "The panic rule",
   clause 2).
2. Do not call `entry` in the process of a test. The function sets the
   panic hook of the process. A later test that fails then prints no
   message.
3. A test that starts the program goes to `tests/process.rs`. Such a test
   reads the exit status and the two output streams of the built program.
4. `run` writes each text to one of its two writers, with `write_all` or
   with `writeln!`. In the program, the two writers are the lock of stdout
   and the lock of stderr. Rule 14 of "The rules for a service" in
   `rust/AGENTS.md` has the text for such a command. When a write to stdout
   fails, `run` returns exit status 1. `run` drops the error of a write to
   stderr. Use no `println!` and no `eprintln!` in this crate.

## Known gaps

- These `CONTRACT-QUESTION` comments are open:
  1. `yaml/scanner.rs` and `yaml/construct.rs`, contract 01 §1. The escape
     `\ud800` in a quoted text gives a lone surrogate in Python, and the
     Python validator accepts the file. A Rust `String` cannot hold that
     value. This crate refuses the file. The vector is
     `yaml-escape-surrogate`.
  2. `yaml/construct.rs`, `py_int` and `py_float`. With an integer tag or
     a float tag on a quoted text, Python reads a decimal digit that is not
     ASCII. This crate refuses it. No vector holds such a text.
  3. `yaml/construct.rs`, `decode_base64`. Python 3.12 stops a `!!binary`
     value at its first complete padding. Python 3.13 and 3.14 read on.
     This crate reads as Python 3.13 reads. No vector holds such a value.
  4. `yaml/construct.rs`, `MERGE_DEPTH_MAX` and `MERGED_ENTRIES_MAX`.
     Contract 01 gives no limit for merge keys. This crate follows a chain
     of 400 merge keys and makes 100,000 entries from merge keys. A merged
     value with no entry counts as one entry. This crate refuses a text
     past a limit. The Python validator has the same two limits.

     Ten vectors hold a text at a limit or one past it: six of
     `family_file` and four of `server_file`. Their names start with
     `yaml-merge-chain`, `yaml-merge-entries` and `yaml-merge-empty`.
  5. `yaml/text.rs`, `NOT_PRINTED`. No contract gives the text of a YAML
     error. Python escapes a code point that its Unicode version does not
     assign. This crate does not, so a message that quotes such a code
     point differs.
  6. `yaml/compose.rs`, `NESTING_MAX`. Contract 01 gives no limit for the
     nesting of a file. This crate reads 128 levels and refuses a text with
     more. The Python validator reads more levels. A debug build reads a
     text of 128 levels on a stack of 1 MiB. The vectors are
     `nest-128-levels` and `nest-129-levels`.
  7. `registry.rs`, `read_one`, contract 01 §1 and contract 01b §1. Row 5
     of `spec.md` §12.3 refuses a directory that holds the TOML name and
     the YAML name. It does not say which name the report shows. The loader
     gives one error and parses neither file, as the Python loader does. The
     report shows the TOML name. The vectors are `toml-two-names` of
     `family_file.toml` and of `server_file.toml`.
  8. `creche-contracts/src/ids.rs`, `ToolName`, `EnvName` and
     `PackageVersion`, contract 01 §3.4 and contract 01b §3.1, §4.1 and §5.
     `rust/AGENTS.md` lists the three questions. The Python validator and
     this crate have one form for each of the three texts. A tool name and
     a variable name have 64 bytes at most. A version has 64 bytes at most
     and holds no `+` and no `-`.
  9. `creche-contracts/src/server.rs`, the grammar of root, contract 01b §3
     to §7. The contract gives no grammar for six fields of a server file,
     for a value and for the key after `secret:`. It gives no cap for a
     count, and it does not say how to count an identity and a description.
     The TOML path takes the rules of root ("The grammar of root"). The YAML
     path takes none of them. A change costs one constant or one grammar in
     `server.rs`, its twin in the Python TOML path and the vectors of that
     rule.
- A YAML integer of more than 4300 decimal digits has no value here, in
  each base. This crate refuses the text.
- The Python validator gives the same message for each text that has no
  value. Three examples are such an integer, a date that the calendar does
  not hold, and a wrong value for a tag. This crate gives the cause. Each
  such vector is a row of `DEVIATIONS`.
- A `!!set` gives its members in the order of the text. Python gives them in
  an order that changes from run to run.
- `zones::SystemZones` reads the four directories that Python reads by
  default. It does not read `PYTHONTZPATH` and has no `tzdata` package.
- `zones::SystemZones` reads the 44 bytes of the header of a zone file.
  Python also reads the body, and refuses a file with a wrong body.
- `registry::load_registry` reads a file name that is not UTF-8 with a
  replacement character. A directory with such a name gets a report under
  that changed name. `registry::revision_of` reads the name in the same
  way. The Python function uses the bytes of the name, so the two
  revisions differ for a registry with such a name. No vector holds such a
  name.
- The program writes the usage text and the help text of Python 3.13. The
  text of another Python version can differ. No vector holds that text.
- The program reads a command line as `argparse` of Python 3.13 reads it.
  Python 3.14 reads more texts as a negative number, for example `-5.`. For
  a number, the program reads only ASCII digits. Python also reads a decimal
  digit that is not ASCII.
- `Server` holds `install.package`, `install.asset`, `install.python` and
  `run.state_dir_env` as text, and `install.repo` and `run.entrypoint` in
  types with the grammar of the YAML path. The YAML path takes each such
  text, and one `Server` type holds the files of both paths. The TOML path
  checks the grammar of root for each one before it makes the `Server`. The
  packet that removes the YAML path of a server file can give each one a
  type with the grammar of root.
- `report::FamilyState` is here. The module `status` of `creche-contracts`
  holds no type yet. Move the type there when contract 05 has its types.
- The Python validator accepts each value of the list below. This crate
  accepts it too. The owner decides whether a later version refuses it:
  1. A YAML 1.1 plain scalar in a typed field: `yes` and `on` for a
     boolean, `010` for the integer 8, `1:30` for the integer 90.
  2. A text for a number or a boolean: `"2"`, `" 2 "`, `"2.0"`, `"0-1"` and
     `"1_0"` for an integer, `"on"` and `"t"` for a boolean.
  3. A boolean for a number: `true` for 1. A float with no fraction for an
     integer: `2.0` for 2.
  4. `!!binary` for a text, and `!!set` for a list.
  5. A key that the file writes twice. The last value stays.
  6. A merge key `<<`, an anchor and an alias.
  7. A delegate and an enqueue target with no grammar check. The registry
     check finds the family by the name of its directory.
  8. A lock path with a `.` segment, and a mount path with a `/` at its
     end.
  9. `state_dir_env` and `install.python` of a YAML server file with no
     grammar check. A TOML server file gets the grammar of root.
