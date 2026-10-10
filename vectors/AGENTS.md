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
   A second build doubles the cost of the suite. The one exception is
   `tests/test_vectors_runtime.py`. It builds the group `runtime` alone, in
   about one second.
9. `pyright` checks this directory in strict mode.
10. Do not remove an input because the Rust result differs from the Python
    result. The one exception is resolution (c) in `rust/AGENTS.md`, section
    "The differential test". The pull request then explains why resolutions
    (d), (b) and (a) do not fit. A plain Rust test holds the removed input.
11. Do not change a frozen file. A frozen file is a data file whose Python
    origin left this repository. `README.md`, "A frozen file", has the
    steps and the checks.
    - The index holds the SHA-256 of each frozen file. A test fails when
      the bytes of a file do not have that digest. Restore the file with
      git.
    - Freeze a file only with `--freeze`. Do not write a line of the map
      `frozen` by hand. Do not change a digest.
    - The Rust test of a frozen surface is the one reader of its file.
    - Add no vector to a frozen file. Write each new case as a plain Rust
      test, with the input in the test.

## Module map

| Module | Owns |
|---|---|
| `core.py` | `normalize`, the markers, the input forms, `attempt`, `Vector`, `Surface`, `render` |
| `generate.py` | the list of groups, the index, the frozen files, `write`, `--check`, `--counts`, `--freeze` |
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
| `surfaces/runtime.py` | the helper code that each service copies: `runtime.untrusted.<copy>.<helper>`, `runtime.parse_object.noticeboard`, `runtime.token.<reader>`, `runtime.bearer.<copy>`, `runtime.edge.<service>` |
| `surfaces/noticeboard_cases.py` | the written inputs of the noticeboard surfaces |
| `surfaces/noticeboard.py` | the noticeboard: `noticeboard.security.*`, `noticeboard.urlform`, `noticeboard.app.query`, `noticeboard.route.*`, `noticeboard.sessions.*`, `noticeboard.transcript.fold`, `noticeboard.audit.page`, `noticeboard.statusdocs.report`, `noticeboard.verify.envfile`, `noticeboard.cli.check`, `noticeboard.static.css` |
| `surfaces/library.py` | the index builder: `library.chunk_text`, `library.file_hash`, `library.read_document`, `library.report`, `library.tei_url`, `library.embedding`, `library.schema`, `library.index_scope` |

## Known gaps

- An input that makes the Python code raise has no vector until its fix
  merges. Rule 5 states why.
- Rule 10 depends on resolution (c) of `rust/AGENTS.md`. That text waits
  for a confirmation of the owner. "Known gaps" of `rust/AGENTS.md` has the
  open point.
- No file is frozen yet. The pull request that removes the first Python
  package freezes the first files.
- No Rust code compares the bytes of a frozen file with its digest. The
  Rust reader, the crate `creche-vectors`, checks only the form of the map
  `frozen`. `vectors/tests` holds each digest.
- The generator does not read the value of `surfaces` in the committed
  index. The Rust reader refuses an index with a wrong row.
- The generator reads a byte of the index that is not UTF-8 as U+FFFD. The
  Rust reader refuses an index with such a byte.
- The committed index is the one home of the map `frozen`. A merge can
  take an index that lacks a line of the map. `--check` then reports that
  file as `left over`. A run of the generator with no flag removes the file
  and prints its path. After that run, no test under `vectors/tests` fails.
  The Rust test of that surface fails.
- No command removes a file from the map `frozen`.
- `ids/disagreements.json` holds only the copies of a grammar that a group
  builds. When the file of a copy becomes a frozen file, the results of that
  copy leave `ids/disagreements.json`.
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
- `noticeboard.sessions.is_session` is a copy of the session id grammar.
  `noticeboard.route.session` covers it. The copy has no `id.` surface: a
  new `id.` surface needs a row in the `ids` module of the Rust crate. Rule
  5 of "Where a new type goes" in `rust/AGENTS.md` applies to that change.
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
- The vector `yaml-merge-chain-at-limit` of `family_file` and of
  `server_file` has a chain of 400 merge keys. The Python reader uses two
  calls for each merge key of a chain. The vector assumes the default
  recursion limit of Python and a caller that is less than 180 calls deep.
  With a deeper caller, the reader refuses that input.
- A vector with an integer of more than 4,300 digits assumes the default
  digit limit of Python `int`. With `PYTHONINTMAXSTRDIGITS=0`, Python reads
  such an integer, and the vector can move. `integer-4301-digits` of
  `runtime.parse_object.noticeboard` then moves from `refused` to
  `accepted`. `offset-4301-digits` of `noticeboard.app.query` moves too: the
  page then shows no record.
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
  alias, a tab or a plain `yes`. No vector covers the three limits that the
  chaperone holds on the merge keys and on the aliases of that text. No Rust
  type reads the text of a roster.
- No vector covers the roster writer, `handover.executor.roster`.
- No vector covers `handover.mcpserver.parse_server` or the reader of the
  sops file in `handover.intake.store`. Each one has merge limits of its
  own. Only the tests of `handover` hold those limits.
- No vector covers the roster reader of the caregiver,
  `caregiver.mcp_release.served_servers`. It gives one answer for a roster
  that it refuses and for a roster with no row, so a vector cannot hold a
  refusal. `caregiver/tests` holds its two limits for merge keys.
- Four configs have no entry point that takes a map of variables, so no
  vector covers them: `chaperone.__main__`, `caregiver.cli`,
  `agent_door_owui.config` and `agent_door_trigger.config`. The two doors
  read a key file while they parse. `config.chaperone.site` covers four
  readers of `chaperone.site`. No vector covers the fifth reader,
  `listener`. It gives the host and the port of the bind that `bind` gives
  as text.
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
  reads that file. `noticeboard.cli.check` holds the vectors of a key file.
- The group `runtime` covers the lenient readers, the token files, the
  bearer of a request and the answers of the web framework. No vector
  covers an atomic write, a read with a size cap or a path under the state
  root. No vector covers the mint of a random token or a signal.
- No vector covers the text that a service decodes from the output of a
  child program. The locale gives the encoding of that decode, and the
  locale is a value of the machine.
- `_text` of `chaperone.delegate` has no public entry point and no vector.
  The reply reader of the delegate client calls it, and no surface covers
  that reader.
- The group `runtime` has no surface for `read_object`, `moment`, `age_s` and
  `missing` of `noticeboard.jsonfiles`, or for `read_json` of
  `attendance.atomic`.
- A `runtime.untrusted` surface of the noticeboard uses the default of each
  optional argument: the limit of `text` and the fallback of `integer`.
- A refused vector of `runtime.parse_object.noticeboard` holds no reason. The
  problem text of the entry point holds a message of the interpreter.
- Two token readers are private functions: `_read_token` of
  `attendance.auth` and `_read_key` of `agent_door_owui.config`. A
  `runtime.token` surface calls the public caller of each one:
  `TokenBook.load` and `from_env`.
- No vector covers `_read_token` of `agent_door_tui.config` or of
  `agent_door_trigger.config`. Each one is a copy of the reader that
  `runtime.token.door` covers.
- No surface covers `read_api` of `attendance.owui_copy`, which reads the
  key of Open WebUI. It reads UTF-8 text, removes the whitespace of Python
  `str.strip` from the two ends and has no least count of bytes.
- `_bearer` of `noticeboard.sessions.SessionReader` reads the token that the
  noticeboard sends to `attendance`. It has no public entry point.
  `noticeboard.sessions.token` covers it through one call of the reader. It
  has the two rules of `read_api`. That surface holds files of mode 0600.
  A token there has 43 ASCII characters, with nothing before it and with
  one line feed or nothing after it. No vector covers another mode, a
  shorter token, a token between spaces or a token with a character
  outside ASCII.
- `runtime.token.attendance` and `runtime.token.attendance_pep_read` hold no
  token. `TokenBook.load` returns nothing, and the tokens that it keeps are
  private.
- A `runtime.token` surface holds one path with no file, the vector
  `absent`. Each other vector is a file that the generator can read. No
  vector covers a directory at the path, a file that the generator cannot
  read or a file of more than 1 MiB.
- No vector covers a symbolic link at the path of a token file. Each of the
  six readers follows the link. A reader with a mode rule reads the mode of
  the file that the link names.
- Each bearer reader is a private function: `_bearer_value` of
  `attendance.auth` and of `agent_door_trigger.webhooks`, `_authenticate` of
  `agent_door_owui.app`, `_bearer` and `_bearer_matches` of `chaperone.app`.
  A `runtime.bearer` surface calls its reader through one route of the app.
- `runtime.bearer.chaperone` covers the bearer of the approval callback. No
  vector covers the bearer of a family on `GET /manifest` and on
  `POST /call`. The chaperone looks up that bearer in the grant files.
- A `runtime.bearer` surface gives the app the bytes of a header with no
  server. A server removes each space and each tab at the two ends of a
  header value. No network client can thus send the header of five vectors:
  `scheme-and-space`, `scheme-and-spaces`, `space-before-the-scheme`,
  `space-at-the-end` and `tab-at-the-end`.
- No vector covers a request with two `Authorization` headers. Each of the
  four copies reads the first one.
- The answers of a `runtime.edge` surface come from the versions of
  Starlette and of FastAPI that `uv.lock` pins. A vector can move when
  `uv.lock` takes a newer version.
- No `runtime.edge` vector holds the answer of the web framework to a
  handler that raises. Each of the five services has its own handler for an
  exception. The vector `handler-raises` holds the answer of that handler.
- `runtime.edge.door_trigger` has no vector for HEAD on a GET route. The
  listener has no GET route.
- A `runtime.edge` vector holds the name of each cookie of an answer. No
  vector holds the value or an attribute of a cookie.
- No `runtime.edge` vector holds the `Content-Length` header. The answer to
  HEAD has no body, and its `Content-Length` is 31. That is the length of
  the body for a wrong method.
- No `runtime.edge` vector covers a final slash with a wrong method or with
  a query. The web framework answers the first with status 307, before it
  checks the method. It keeps the query in the `Location` header of the
  second. A vector holds only the path of that header.
- Five helpers of `noticeboard.app` are private. A noticeboard surface
  covers each one through another entry point:
  1. `_form_of` reads a form body. `noticeboard.urlform` calls
     `urllib.parse.parse_qsl` with the two steps of that helper. A change to
     the helper does not move a vector.
  2. `_one` and `_offset_of` read the query of the audit page.
     `noticeboard.app.query` covers them through that page.
  3. `_require_family` checks a route parameter. `noticeboard.route.family`
     covers it through three routes.
  4. `_require_session` calls `sessions.is_session`.
     `noticeboard.route.session` covers that function. No vector covers the
     session route with a text that is no session id.
- `_parse_env_file` of `noticeboard.verify` is private.
  `noticeboard.verify.envfile` covers it through `main`. A vector shows a
  variable of the file only through the bind and the port of the config.
- `_refusal` and `_stream` of `noticeboard.sessions` are private.
  `noticeboard.sessions.refusal` and `noticeboard.sessions.events` cover them
  through the calls of `SessionReader`.
- `noticeboard.security.key` gives `check_key` a text that the generator
  makes from the bytes of a header. In the service, the web framework makes
  that text. No vector covers the key check or the check of a form post
  through a request. No vector covers a form body of more than 1 MiB.
- A noticeboard surface that needs a request gives the app a scope that the
  generator makes. The scope holds the raw path, and the path with each
  percent escape decoded as UTF-8. The server of the noticeboard makes that
  scope in a deployment. A query and a path segment of such a surface are
  ASCII.
- No noticeboard surface holds a JSON text that `json.loads` takes and a
  strict reader refuses. The Python readers of the noticeboard take each
  of these texts:
  1. A text with `NaN` or `Infinity`.
  2. A text with an integer outside 64 bits.
  3. A text with a key two times in one object.
  4. A text with a byte order mark, or a text in UTF-16.
  5. A text with more than 64 levels.
  6. A text with one half of a surrogate pair.

  `runtime.parse_object.noticeboard` holds such texts for the reader of one
  file. `vectors/tests/test_vectors_noticeboard.py` holds this rule for the
  written inputs.
- A token that the service keeps in a vector of `noticeboard.security.cookie`
  has 1 to 512 bytes. Each byte of it is a `cookie-octet` of RFC 6265.
  No vector covers a longer token. No vector covers a token with a space, a
  comma, a semicolon, a backslash, a double quote or a byte above 127. The
  Python service keeps such a token.
  `vectors/tests/test_vectors_noticeboard.py` holds this rule for the
  committed vectors.
- `noticeboard.security.cookie` covers the cookie of the edit page. No
  vector covers the cookie of another answer. `runtime.edge.noticeboard`
  holds the names of the cookies of five answers.
- No vector covers a request with two `Cookie` headers. The service reads
  each one, and the last value of the name stays.
- Four noticeboard surfaces go through the app: `noticeboard.security.cookie`,
  `noticeboard.app.query`, `noticeboard.route.family` and
  `noticeboard.static.css`. Their vectors come from the versions of
  Starlette and of FastAPI that `uv.lock` pins. A vector can move when
  `uv.lock` takes a newer version.
- `noticeboard.app.query` holds no offset with a decimal digit outside
  ASCII. Python `int` reads such a digit.
- No answer of `attendance`, no line of an event stream and no audit record
  of a noticeboard surface holds a count below zero. The Python readers of
  the noticeboard take an integer of each sign.
- `noticeboard.statusdocs.report` gives the entry point the absolute path of
  a file that the generator makes. No vector covers a relative path or a
  path with a NUL character.
- `noticeboard.route.family` holds no segment with an escaped slash that
  makes the path of another route, for example `chat%2Fedit`. The web
  framework decodes the path before it finds the route. The segment of the
  example thus gets the edit page.
- `noticeboard.cli.check` and `noticeboard.verify.envfile` hold no relative
  path and no URL with no scheme. `config.noticeboard.env` holds those
  inputs.
- Each vector of `noticeboard.cli.check` names the state root, the registry
  and the socket or the URL of `attendance`. The program says if a path
  exists, and a default path can exist on a machine. No vector thus covers
  the lines of a default path.
- A refused vector of `noticeboard.cli.check` holds the exit status of the
  Python program today. The vector moves when that status changes.
- The generator replaces `logging.basicConfig` while
  `noticeboard.__main__.main` runs, and `httpx.get` while
  `noticeboard.verify.main` runs. The first call changes the log setup of
  the process. The second call opens a socket.
- `noticeboard.verify.envfile` holds the first check of the hook only. No
  vector covers the exit status or another check: each one reads a path or
  a socket of the machine.
- A problem of the noticeboard can end with a message of the JSON reader of
  Python. A noticeboard vector holds the class of such a problem and the
  start of its sentence, and no message.
- The sentence for a token file or an audit directory that does not exist
  ends with the text that the system gives for the error. The vectors hold
  the text `No such file or directory`, which macOS and Linux give.
- No vector covers a day file of more than 32 MiB. The audit reader reads
  the end of such a file. No vector covers a day file or a report that the
  reader cannot open.
- No vector covers the edit form, the text that a save writes or the save
  itself: `noticeboard.familyform`, `noticeboard.yamlout`,
  `noticeboard.yamlkeep` and `noticeboard.registrywrite`. No vector covers
  the markup of a page.
- Three surfaces of the index builder go through `index_scope`:
  `library.embedding`, `library.schema` and `library.index_scope`. The
  function that packs a vector, `_float32`, and the function that walks a
  corpus, `_walk`, are private. No vector calls one of the two directly.
- Each vector of `library.chunk_text` has an overlap that is less than its
  size. No vector covers an overlap that is equal to the size or larger.
- No vector holds text from a PDF file. `library.read_document` holds text
  files only. No corpus of `library.index_scope` holds a file that the PDF
  reader reads.
- A corpus of `library.index_scope` holds no two names of one directory
  that differ only in letter case. It holds no name with a character that
  is not ASCII and no name that ends in a dot. A file system can change such
  a name, and the output then differs between two machines (rule 7).
- No vector of the index builder holds a time: `mtime` and `indexed_at` of
  a row of `files`, and `updated_at` of `meta`.
- `library.schema` holds no statement of `chunks_vec`, and none of a table
  that a virtual table makes for itself. `library.index_scope` holds no row
  of `chunks_vec`. No reader outside the index builder reads `chunks_vec`.
- `library.index_scope` holds the `rowid` of each row of `chunks_fts`. It
  holds no text of such a row, and no vector holds the answer to a search.
- `library.tei_url` holds only a text that the Rust index builder will
  take at its start. The entry point returns each text that is not empty.
  Four examples of a text with no vector:
  1. A URL with the scheme `https`. The HTTP client of the Rust runtime has
     no TLS.
  2. A URL with no scheme.
  3. A URL with a user part.
  4. The LAN address `0.0.0.0`.

  The strict config types of the Rust crate refuse examples 2, 3 and 4.

  A text with no vector stays out under resolution (c) of `rust/AGENTS.md`:
  the program reads the value at its start. That document states the case
  for a daemon. The index builder is a command that a timer starts. The
  reading here is that the case holds for such a command too. The Rust
  config type of the index builder does not exist yet. The pull request of
  that type adds a plain Rust test for each such text.
- `library.embedding` holds no value that is not finite, no boolean and no
  vector with a wrong count of values.
- No vector of `library.index_scope` holds a file that `index_scope`
  reports as an error. `library.embedding` holds such a file. It records
  only that the report names the file.
- A vector of `library.index_scope` holds the counts of each report. It
  does not hold the paths that a report lists.
- No vector covers the cut of an error text at 200 code points.
  `index_scope` makes the cut. An error text there comes from the
  interpreter or from a library.
- No vector covers a symbolic link in a corpus, a scope behind a symbolic
  link or a directory that the program cannot read.
- No vector covers a store with no table `chunks_emb`, or a store file that
  SQLite cannot read.
- No vector covers `main` of `library.__main__` or its TEI client: the
  command line, the exit status, the preflight and the calls of the
  embedder. The embedder of each vector is a stub of the generator.
