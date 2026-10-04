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
| `surfaces/grants.py` | `grants.parse`, `chaperone.call_body`, `chaperone.approval_body` |
| `surfaces/status.py` | the five readers of `status.json`: `status.<reader>` |
| `surfaces/session_cases.py` | the written inputs of the session API surfaces |
| `surfaces/session.py` | the session API: `session.request.*`, `session.query.*`, `session.error_body`, `session.answer.*`, `session.journal.*`, `session.stream.*`, `session.turn.move`, `session.state.derive`, `session.outcome.*` |

## Known gaps

- An input that makes the Python code raise has no vector until its fix
  merges. Rule 5 states why.
- No vector covers the writer of the status document, or a fault file, or
  an outcome file of contract 05. The five readers of `status.json` have
  vectors.
- No vector covers contract 06, the component manifest and the release
  request file.
- No vector covers `server.yaml`, contract 01b.
- `channel.parse` gives no vector for `unknown_address` or `sequence_gap`.
  Those refusals need the state of a channel.
- No vector covers the size cap of a grant file or the body cap of the
  chaperone. The caller applies each cap before it calls the entry point.
  No vector covers the size cap of a status reader.
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
- A vector file sorts keys. `session.journal.write`, `session.stream.encode`
  and `session.error_body` thus give an object of free form its keys in
  sorted order. No vector shows that the Python code keeps another order.
- `session.outcome.write` and `session.answer.*` use no time before the
  year 1000. `attendance.clock.rfc3339` writes such a year with a width that
  depends on the system.
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
