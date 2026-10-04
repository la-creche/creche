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
| `surfaces/family_file.py` | `family_file`, `family_file.host` and `family_file.registries.json` |
| `surfaces/server_cases.py` | the written `server.yaml` inputs |
| `surfaces/server_file.py` | `server_file` |
| `surfaces/classify.py` | `family_file.classify` |
| `surfaces/family_cli.py` | `family_file.cli` |
| `surfaces/channel.py` | `channel.parse`, `channel.frame`, `channel.build` |
| `surfaces/grants.py` | `grants.parse`, `grants.write`, `chaperone.call_body`, `chaperone.approval_body`, `chaperone.verb` |
| `surfaces/audit.py` | `chaperone.audit_line`, `chaperone.unidentified_line`, `chaperone.reason` |
| `surfaces/status.py` | the five readers of `status.json`: `status.<reader>` |
| `surfaces/status_files.py` | the writer of `status.json`, the fault files and the outcome record: `status.write`, `status.fault_file.<package>`, `status.outcome.noticeboard` |
| `surfaces/config.py` | the site file, the roster, the mount files and three env readers: `config.<name>` |
| `surfaces/manifest_cases.py` | the written `component.yaml` inputs |
| `surfaces/manifest.py` | the eleven `manifest.<name>` surfaces of contract 06 |
| `surfaces/session_cases.py` | the written inputs of the session API surfaces |
| `surfaces/session.py` | the session API: `session.request.*`, `session.query.*`, `session.error_body`, `session.answer.*`, `session.journal.*`, `session.stream.*`, `session.turn.move`, `session.state.derive`, `session.outcome.*` |

## Known gaps

- An input that makes the Python code raise has no vector until its fix
  merges. Rule 5 states why.
- No vector covers a scalar of `component.yaml` that PyYAML cannot build,
  such as a word with the tag `!!int`. The Python code refuses it and names
  no line. The Rust reader refuses it and names a line. Such a vector first
  needs a row in the Rust table of details.
- `status.write` builds the `reconcile` block and the `spend` block by hand.
  `caregiver` builds them in two private functions of `caregiver.reconcile`.
  The generator copies the key order of those functions.
- `status.fault_file.attendance` gives `attendance.faults` a clock with
  `unittest.mock`. That writer has no parameter for a clock.
- No vector covers the writer of an outcome record, `attendance.outcomes`.
  `status.outcome.noticeboard` reads records that this writer made.
- No vector covers `rescope_by_fleet` and `drop_superseded` of
  `caregiver.faults`. They change a fault after `read_fault_file` reads it.
- No vector covers the resolver, the deploy order or rules C1 to C4 of
  contract 06 §3.2. `manifest.resolved` starts from a resolution.
- No vector covers the ledger entry or the spool. `manifest.operator`
  covers two values of the site file, and `config.site_file` covers its form.
- `manifest.component` reads the site file in two states: with both values
  of the operator, and with no file. A site file with one of the two
  values has no vector.
- `manifest.resolved` gives `resolved_at` as a float only. With an integer,
  Python writes no `.0`.
- `family_file.cli` holds no text that `argparse` writes: no usage line and
  no help text. That text differs between two Python versions. A vector for
  such a command line holds the exit status only.
- `server_file` and `family_file.cli` have no host. No vector covers the
  program with a model router or with a mount that is a symbolic link.
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
  reader, of the fault file reader or of the outcome reader.
- No vector covers a status document that is UTF-16. The Python noticeboard
  reads such a document. The Rust view of the noticeboard refuses it.
- No vector covers a `written_at` with no UTC offset. `attendance` and the
  noticeboard read such a time as UTC. The Rust views read it as no time.
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
- `noticeboard.sessions.is_session` is a copy of the session id grammar
  with no id surface. A new `id.` surface needs a row in the `ids` module of
  the Rust crate. Rule 5 of "Where a new type goes" in `rust/AGENTS.md`
  applies to that change.
- `host_reason` of `attendance.wire` has no vector. `cap_event` and
  `read_usage` have vectors only through `channel.parse`.
- No vector covers the playpen side of contract 03: what the playpen accepts
  from the host, and what it writes. The playpen is TypeScript, and the
  generator calls Python.
- `channel.build` has no vector for `prompt`. `attendance.wire` has no
  builder for that message.
- `status.door_trigger` lists only an autonomous family, and a refused
  vector holds no reason. Most status documents are attended, so that
  reader refuses them on `kind` alone. The surface pins little of how that
  reader reads another field.
- `family_file` uses two time zone names that every tz database holds. A
  name that depends on the file system of the host has no vector.
- The vectors with a 400,000-deep nesting assume the default stack size. A
  larger stack can let Python 3.14 read that input.
- The vector with a 9,100-deep nesting is under the limit of each supported
  Python version: 9,997 levels on 3.12 and 9,998 on 3.13. Python refuses that
  input when the caller is about 900 C calls deep. Python 3.14 refuses it on
  a small stack.
- The vector `yaml-deep-flow` of `family_file` has a 10,000-deep nesting. It
  assumes the default recursion limit of Python. With a larger limit, the
  YAML reader can read that input.
- The vector `yaml-merge-chain-at-limit` has a chain of 400 merge keys. The
  Python reader uses two calls for each merge key of a chain. The vector
  assumes the default recursion limit of Python and a caller that is less
  than 180 calls deep. With a deeper caller, the reader refuses that input.
- The session surfaces go through the routes of `attendance.api`. The
  service behind the routes is a stand-in. No vector covers a refusal that
  the real service makes after the parse: a token, a family kind, a lease.
- No vector covers the header `X-Door-Instance`, or a path parameter. The
  parsers of `attendance.requests` do not check a path parameter.
- No vector covers the bodies of `wait=accepted` and `wait=settled`. No
  vector covers the answers of `/dispatch` and `/dispatch/jobs`. No vector
  covers a file of `attendance.store`: `session.json`, a turn record, a
  lease file, a dispatch ledger entry. Each one has no public entry point
  without the real service.
- No vector covers the readers in the three doors: the error body, a
  session row, a settled turn, a line of the event stream.
- `session.journal.write` and `session.stream.encode` take each body. The
  entry point does not check a body against its kind. The service has no
  public function that makes the body of a kind. The bodies in
  `session_cases.py` copy the keys and their order from
  `attendance.service`. A change there does not move a vector.
- A vector file sorts keys. `session.journal.write` and
  `session.stream.encode` thus give an object of free form its keys in
  sorted order. No vector shows that the Python code keeps another order.
  `session.error_body` writes a detail whose keys are not in sorted order as
  an `$entries` marker. An object inside a detail has its keys in sorted
  order.
- A text of a time has a vector only when each supported Python version
  reads it in the same way. `datetime.fromisoformat` differs between
  versions on five forms:
  1. A fraction with no digit.
  2. A fraction after the hours or after the minutes.
  3. A fraction in an offset of zero seconds.
  4. Hour 24.
  5. A colon and digits after the seconds, as in `06:00:23:599999`.
- The generator runs on macOS, and CI runs it on Linux. No other system
  has a run.
- `config.roster` holds the tree of a roster file and no YAML text. No
  vector covers what PyYAML does with the text of a file: a comment, an
  alias, a tab or a plain `yes`.
- No vector covers the roster writer, `handover.executor.roster`.
- No vector covers the roster reader of the caregiver,
  `caregiver.mcp_release.served_servers`. It gives one answer for a roster
  that it refuses and for a roster with no row, so a vector cannot hold a
  refusal. `caregiver/tests` holds its two limits for merge keys.
- Four configs have no entry point that takes a map of variables, so no
  vector covers them: `chaperone.__main__`, `caregiver.cli`,
  `agent_door_owui.config` and `agent_door_trigger.config`. The two doors
  read a key file while they parse. `config.chaperone.site` covers the four
  readers of `chaperone.site`.
- No vector covers `handover.intake.run`. It reads the site file and the
  environment of the process.
- `config.playpen_env.read` takes a text. No vector covers an env file
  that is not UTF-8. `caregiver.playpen_env.read_playpen_env` reads such a
  file as a file with no variable.
- No vector covers a reader of the playpen: `runtime-config.ts`, `creds.ts`
  and `mounts.ts` are TypeScript and have no Python entry point.
- `config.site_file` has no vector for a file that another account owns. The
  generator cannot change the owner of a file.
- `config.noticeboard.env` names no `VIEW_ACCESS_KEY_FILE`. The entry point
  reads that file.
