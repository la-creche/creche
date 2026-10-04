# agent-family

The Rust port of the Python package `agent_family`: the validator of
`family.yaml` and of `mcp/<name>/server.yaml`, the registry loader and the
`agent-family` program. `rust/AGENTS.md` and the root `AGENTS.md` apply here
too.

The Python package is the authority until a release of `caregiver` uses this
crate. This crate replaces nothing. Each function gives the answer of its
Python function: the same values, the same issues in the same order and the
same messages.

## Layout

| Path | What it holds |
|---|---|
| `src/yaml/` | A YAML reader that reads a text as PyYAML 6.0.3 reads it. |
| `src/lax.rs` | A value to a number, a boolean or a string, as pydantic reads it. |
| `src/shape.rs` | A YAML value to a raw file. It reports each wrong type and each unknown field. |
| `src/difflib.rs` | The nearest known name for an unknown field. |
| `src/parse.rs` | `parse_family` and `parse_server`: YAML text to a raw file, or to issues. |
| `src/crossref.rs` | `check_family` and `check_server`: the rules that need the registry or the host. |
| `src/registry.rs` | `load_registry` and `revision_of`. |
| `src/classify.rs` | `classify`: live or replacement, field by field. |
| `src/zones.rs` | Whether the host knows a time zone. |
| `src/sha256.rs` | SHA-256, for the revision. |
| `src/scratch.rs` | Test code only: a directory that one test fills. |
| `src/report.rs`, `src/json.rs`, `src/cli.rs`, `src/main.rs` | The report, and the program that prints it. |
| `tests/vectors.rs` | The differential test against the Python package. |

The types are in `creche-contracts`: `family` holds `RawFamily`, `Family` and
the report types, and `server` holds `RawServer` and `Server`.

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

## Rules

1. Change no rule here without a vector. `tests/vectors.rs` walks each
   vector of the five surfaces of this crate.
2. Write a difference from the Python code as a row of `DEVIATIONS` in
   `tests/vectors.rs`. The row names the surface, the vector and the
   contract section.
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

## Known gaps

- These `CONTRACT-QUESTION` comments are open:
  1. `yaml/scanner.rs` and `yaml/construct.rs`, contract 01 §1. The escape
     `\ud800` in a quoted text gives a lone surrogate in Python, and the
     Python validator accepts the file. A Rust `String` cannot hold that
     value. This crate refuses the file. The vector is
     `yaml-escape-surrogate`.
  2. `creche-contracts/src/family.rs`, `is_port`, contract 01 §3.7. The
     Python validator reads a decimal digit that is not ASCII in a port.
     This crate refuses such a digit, as rule 9 of `rust/AGENTS.md` says.
     The vector is `rule-egress-edges`.
  3. `yaml/construct.rs`, `py_int` and `py_float`. With an integer tag or
     a float tag on a quoted text, Python reads a decimal digit that is not
     ASCII. This crate refuses it. No vector holds such a text.
  4. `yaml/construct.rs`, `decode_base64`. Python 3.12 stops a `!!binary`
     value at its first complete padding. Python 3.13 and 3.14 read on.
     This crate reads as Python 3.13 reads. No vector holds such a value.
  5. `yaml/construct.rs`, `MERGE_DEPTH_MAX` and `MERGED_ENTRIES_MAX`.
     Contract 01 gives no limit for merge keys. This crate follows a chain
     of 400 merge keys and makes 100,000 entries from merge keys. It
     refuses a text past a limit. No test holds a text at a limit.
  6. `yaml/text.rs`, `NOT_PRINTED`. No contract gives the text of a YAML
     error. Python escapes a code point that its Unicode version does not
     assign. This crate does not, so a message that quotes such a code
     point differs.
  7. `yaml/compose.rs`, `NESTING_MAX`. Contract 01 gives no limit for the
     nesting of a file. This crate reads 128 levels and refuses a text with
     more. The Python validator reads more levels. A debug build reads a
     text of 128 levels on a stack of 1 MiB. The vectors are
     `nest-128-levels` and `nest-129-levels`.
  8. `creche-contracts/src/ids.rs`, `ToolName`, `EnvName` and
     `PackageVersion`, contract 01 §3.4 and contract 01b §3.1, §4.1 and §5.
     `rust/AGENTS.md` lists the three questions. The Python validator
     accepts a tool name and a variable name of more than 64 bytes. It also
     accepts a version with `+`, with `-` or of more than 64 bytes. This
     crate refuses them. Each case is a row of `DEVIATIONS` with the stance
     `Stricter`.
- A YAML integer of more than 4300 decimal digits has no value here, in
  each base. This crate refuses the text.
- The Python validator gives one message for each text that has no value:
  such an integer, a date that the calendar does not hold, a tag on a text
  that is no value of the tag. This crate gives the cause. Each such vector
  is a row of `DEVIATIONS`.
- A `!!set` gives its members in the order of the text. Python gives them in
  an order that changes from run to run.
- `zones::SystemZones` reads the four directories that Python reads by
  default. It does not read `PYTHONTZPATH` and has no `tzdata` package.
- `zones::SystemZones` reads the 44 bytes of the header of a zone file.
  Python also reads the body, and refuses a file with a wrong body.
- `registry::load_registry` reads a file name that is not UTF-8 with a
  replacement character. A directory with such a name gets a report under
  that changed name.
- The program writes the usage text and the help text of Python 3.13. The
  text of another Python version can differ. No vector holds that text.
- The program reads a command line as `argparse` of Python 3.13 reads it.
  Python 3.14 reads more texts as a negative number, for example `-5.`. For
  a number, the program reads only ASCII digits. Python also reads a decimal
  digit that is not ASCII.
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
  9. `state_dir_env` and `install.python` with no grammar check.
