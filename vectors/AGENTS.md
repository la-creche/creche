# vectors

This directory holds the oracle for the Rust port. The oracle is what the
Python implementation accepts, as data. `README.md` holds the file format
and the commands. The root `AGENTS.md` applies here too.

This directory is not a component. A change here mints no tag. This
directory is not a workspace package, so a change here does not change
`uv.lock`.

## Rules

1. Change no product code for a vector. Call a public entry point. If a
   surface has none, record that in "Known gaps".
2. Do not edit a file under `data/` by hand. Run the generator.
3. Do not change a vector to make a test pass. A vector that moves is a
   change in what the platform accepts. Say why in the commit body.
4. Keep a vector id when its input stays the same. A Rust test names the id.
5. Commit no `raised` vector. A `raised` vector is a defect of the Python
   code. This repository is public, and such a vector publishes an input
   that makes a service raise. Report the defect as `SECURITY.md` says. Fix
   the defect in its own package, test-first. Then add the input. Its
   vector is `refused`. A test fails on a committed `raised` vector.
6. Every input is public. Use only files that this repository already
   holds, or text that you write. Name no deployment.
7. The output is the same on every machine and under each supported Python
   version. `--check` under each version is the proof.
8. One test builds the vectors. The other tests read the committed files.
   A second build doubles the cost of the suite.
9. `pyright` checks this directory in strict mode.

## Module map

| Module | Owns |
|---|---|
| `core.py` | `normalize`, the markers, the input forms, `attempt`, `Vector`, `Surface`, `render` |
| `generate.py` | the list of groups, the index, `write`, `--check`, `--counts` |
| `surfaces/ids.py` | the id grammars and `ids/disagreements.json` |
| `surfaces/family_cases.py` | the written `family.yaml` inputs |
| `surfaces/family_file.py` | `family_file` and `family_file.host` |
| `surfaces/channel.py` | `channel.parse`, `channel.frame`, `channel.build` |
| `surfaces/grants.py` | `grants.parse`, `grants.write`, `chaperone.call_body`, `chaperone.approval_body`, `chaperone.verb` |
| `surfaces/audit.py` | `chaperone.audit_line`, `chaperone.unidentified_line`, `chaperone.reason` |
| `surfaces/status.py` | the five readers of `status.json`: `status.<reader>` |

## Known gaps

- An input that makes the Python code raise has no vector until its fix
  merges. Rule 5 states why.
- No vector covers the writer of the status document, or a fault file, or
  an outcome file of contract 05. The five readers of `status.json` have
  vectors.
- No vector covers contract 06, the component manifest and the release
  request file.
- No vector covers `server.yaml`, contract 01b.
- `grants.write` calls `write_grant_file`. No vector covers
  `build_grant_file`, which expands `all` and `<server>__*`.
- `grants.write` holds valid fields only. The writer does not validate a
  field, so an invalid field has no refusal to record. It holds no file of
  more than 256 KiB. The writer writes such a file, and the chaperone then
  refuses it.
- No vector covers `rewrite_digests` or `grant_file_matches` of the
  caregiver.
- The two log surfaces replace the clock of `chaperone.audit` and of
  `chaperone.family_audit` while the entry point runs. The two modules give
  no other way to set the time of a record.
- No vector covers the retention sweep of a log, or a log directory that
  the chaperone cannot write.
- `channel.parse` gives no vector for `unknown_address` or `sequence_gap`.
  Those refusals need the state of a channel.
- No vector covers the size cap of a grant file. No vector of the two body
  surfaces covers the body cap of the chaperone. The caller applies each cap
  before it calls the entry point. `chaperone.unidentified_line` holds two
  vectors of a body over the cap. No vector covers the size cap of a status
  reader.
- Five patterns have no public entry point: `_ENV_NAME_RE` in the four
  `verify.py` modules, `_LOCK_PATH` and `_ARG_NAME` in
  `agent_family.serverrules`, `_REPO_NAME` in `handover.site` and
  `_SAFE_TOKEN_RE` in `handover.errors`.
- An id surface whose entry point is a pattern records the pattern alone. A
  caller of that pattern can add a length cap.
- Eight grammars have one copy and no id surface: the model alias, the
  mount path, the Home Assistant identifiers, the GitHub repo name, the
  unit name, the path segment, the image reference and the age recipient.
  `family_file` covers the grammars that `agent_family` applies to a family
  file.
- `host_reason` of `attendance.wire` has no vector. `cap_event` and
  `read_usage` have vectors only through `channel.parse`.
- `status.door_trigger` lists only an autonomous family, and a refused
  vector holds no reason. Most status documents are attended, so that
  reader refuses them on `kind` alone. The surface pins little of how that
  reader reads another field.
- `family_file` uses two time zone names that every tz database holds. A
  name that depends on the file system of the host has no vector.
- The vectors with a 400,000-deep nesting assume the default stack size. A
  larger stack can let Python 3.14 read that input.
- The generator runs on macOS, and CI runs it on Linux. No other system
  has a run.
