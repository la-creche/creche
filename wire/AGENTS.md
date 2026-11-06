# wire

This directory holds the package `creche-wire`. The package holds the
pydantic models of the wire types of the VM owner (decision CH9-1). The root
`AGENTS.md` applies here too.

## One definition per type

The Rust crate `rust/crates/wire` defines each wire type. Its test writes
`rust/crates/wire/schema/wire.json`, the JSON Schema of every wire type.
`datamodel-code-generator` turns that schema into
`src/creche_wire/models.py`. No wire type is written by hand in Python.
The same test writes `agent/src/wire.ts`, a TypeScript copy of the schema,
from which TypeBox gives the static types and the runtime checks
(decision CH22). No wire type is written by hand in TypeScript.

| File | What it holds |
|---|---|
| `src/creche_wire/models.py` | The generated models. Never edit this file. |
| `src/creche_wire/__init__.py` | The names a caller imports. |
| `src/creche_wire/reader.py` | `read`, the one read of a wire text. |
| `src/creche_wire/route.py` | `JsonRoute`, the type of a route constant. |
| `src/creche_wire/routes.py` | The generated route constants. Never edit this file. |
| `src/creche_wire/_route_typing.py` | The wrong uses of a route that pyright must refuse. |
| `src/creche_wire/strictjson.py` | A copy of the strict JSON reader. Never edit one copy alone. |
| `tests/test_wire_vectors.py` | The test that runs the shared wire vectors through `read`. |
| `tests/test_wire_read.py` | The test that shows `read` is strict. |
| `tests/test_wire_grammar.py` | The test that holds each name pattern equal to `family/grammar.py`. |
| `tests/test_wire_routes.py` | The test that holds each route constant equal to `x-routes`. |

`family/grammar.py` stays the authority for a name of the family file
(decision CH23 rule 5). A wire type of such a name copies its pattern, and
`tests/test_wire_grammar.py` names the pair. The same test holds attendance's
family pattern and its chain cap equal to the wire types. The facade's
`FAMILY` and `MAX_CHAIN` are TypeScript, so no test here reaches them.

A `dispatch` trigger needs a chain of 1 to 8 names. attendance today stores
a dispatch with no chain when `POST /dispatch` carries none. Before
attendance writes the `auto` origin, a PR of its own makes attendance refuse
a missing or empty chain, at the request and in the ledger reader.

## Read a wire text

Read a wire text only with `creche_wire.read(Model, data, cap=...)`.

1. `strictjson.loads` checks the text first. It refuses two equal keys, a
   lone surrogate, `NaN`, an integer out of range, the integer `-0` and a
   text over the cap. The error is `NotStrictJson`.
2. The model then reads the text in strict mode. An int refuses `"1"`, `1.0`
   and `true`. The error is `pydantic.ValidationError`.
3. The caller gives the cap. It is the cap of the caller's surface, so the
   package has no default. A client of the vmm host API gives
   `HOST_API_BODY_CAP` of `creche_wire.routes` (see "The body cap").

`strict-types` in `[tool.datamodel-codegen]` makes each scalar of a model
strict too. A direct `model_validate_json` then still refuses `"1"` for an
int. It does not check the rules of step 1.

`read` refuses some texts that the Rust crate reads with
`serde_json::from_str`. No wire type meets these texts today. A wire type
that does needs a decision of the owner before its vector cases are added:

1. A nesting depth of 65 to 128. The Rust limit is 128.
2. Two equal keys in an object read as a map. serde_json keeps the last one.
   `FamilySkills` reads its map itself and refuses a key given twice, so it
   does not meet this text.
3. An integer past 64 bits for a float. serde_json reads it as an `f64`.
4. A text over the cap. The Rust crate has no cap.
5. The integer token `-0` for a float. serde_json reads it as the `f64`
   value -0.0.

`bin/tests/test_strictjson_copies.py` fails when `strictjson.py` differs from
the copy in another package. Change each copy in one commit.

TypeScript reads a wire text with `read` of `agent/src/wire-read.ts`, and
`agent/test/wire-vectors.test.ts` runs the shared wire vectors through it
(`agent/AGENTS.md`).

## Secrets

`LitellmKey` and `PepToken` hold secrets. The `Debug` of the Rust types
writes no value. The generated Python models do not keep this rule:

1. The `repr` of `LitellmKey`, `PepToken`, `FamilyCreds`, `FamilyState`,
   `StateReply` and `FamilyStateReply` shows the secret.
2. The text of a `ValidationError` for a refused secret shows most of it.

A Python caller never logs one of these values or such an error. No Python
caller reads them today. datamodel-codegen writes `SecretStr` for
`format: password`, but it then drops the pattern, so the models keep a
plain string.

## Integers and times

1. Every integer of a wire type has a `minimum` and a `maximum`, at most
   2^53 - 1 from zero (decision CH22 rule 3). JavaScript reads no larger
   integer exactly. datamodel-codegen drops the `format` that schemars
   writes, so the bounds are the only width check of the generated model.
2. schemars bounds an integer of 8 or 16 bits itself. `bound_integers` in
   `rust/crates/wire/src/schema.rs` gives an integer of 32 bits the bounds
   of its width. A `u64`, `i64`, `usize` or `isize` needs bounds of its own
   type, or the schema test fails. A nullable integer, with the type
   `["integer", "null"]`, follows the same rules.
3. A time on the wire is `UnixMillis`: integer milliseconds since
   1970-01-01T00:00:00Z, UTC (decision CH22-1). No wire type holds a
   `date-time` string. This rule replaces the time rule of the root
   `AGENTS.md` for wire types only.
4. `test_each_integer_and_date_of_the_schema_is_checked` holds the committed
   schema to rules 1 and 3.

`read` refuses the integer `-0` with the rule `negative_zero` (decision
CH22-3). serde_json reads `-0` as a float, so the Rust type refuses it too.
`parseStrict` of `agent/src/strict-json.ts` refuses it with the same word.
`-0.0` is a float. The Rust type and the model of an integer refuse it.
`parseStrict` refuses it at the read, as it refuses `1.0`. The `UnixMillis`
block of the shared vectors holds a case for `-0` and for `-0.0`.

## The shared wire vectors

`rust/crates/wire/vectors/wire.json` holds, for each wire type, JSON texts
and what the Rust type does with each one. `written` is the JSON text the
type writes after it reads `json`, or `null` when the type refuses `json`.
The Rust crate (`src/vectors.rs`) and `tests/test_wire_vectors.py` read the
same file.

1. The Rust crate is the authority. A case says what the Rust type does.
   The repository's `vectors/` directory is a different oracle, whose
   authority is the Python code. These cases are not part of it.
2. A case is written by hand. Add one for each edge you find, in either
   language.
3. Never change a case to make a Python test pass. A Python failure is a
   defect of the generation.
4. Each wire type has at least one accepted case and one refused case.
5. Each wire type has one block. serde_json in the Rust test keeps only the
   last block of a type, and the strict reader of the Python test refuses
   two equal keys.

## The guest paths

`rust/crates/wire/src/guest.rs` holds the fixed paths, ports and env name
inside the guest (decisions CH5e, CH62 rules 5, 6, 10 and 13). They are
plain Rust constants with no serde and no schema entry (decision CH14), so
no generated copy changes. `rust/crates/wire/vectors/guest.json` lists the
same values, and `guest::tests::the_vector_file_matches_the_constants`
holds the two equal. The file also holds `family_state_cap`, the value of
`FAMILY_STATE_CAP` in `lib.rs` (decision CH23 rule 4). A phase 6 agent
test reads the file to hold its own copies equal, `STATE_REPLY_CAP` of
`agent/src/family-state.ts` among them. Change a value in the constant
and in the file together. A changed value needs a new
`GUEST_INIT_PROTOCOL` once guest init is deployed (decision CH14).

## A struct on the wire

A derived `Deserialize` of a struct also reads a JSON array, its fields in
order. `deny_unknown_fields` does not stop it. The schema and the generated
model take only an object.

1. Derive `Serialize` and `Deserialize` with `#[serde(remote = "Self")]`,
   and add the line `object_only!(Name);` in `rust/crates/wire/src/lib.rs`.
   The macro reads the struct only from a JSON object.
2. Add a refused case with the array form: `[]`, or the fields in order.
   Add the same case where an enum variant holds the struct.
3. Make the payload of an enum variant a struct type of its own. The class
   of an inline struct variant gets the name of its key, and a later type
   with the same name changes that name.
4. Give a field that may be `null` the type `Option` and
   `#[serde(deserialize_with = "Option::deserialize")]`, and give its struct
   `schemars(transform = schema::require_each_key)`. The key is then
   required in both languages, and `null` is the one form of no value.

## An enum on the wire

Decision CH22 rule 3 sets rules 1 and 2. Decision CH31 limits rule 2 to an
enum that carries data, and makes `system_prompt` a plain string enum, as
rule 3 allows.

1. An internally tagged enum writes a unit variant as `V {}`.
2. An externally tagged enum that carries data has no unit variant. The
   variant name is the one key of an object, and its value is the payload.
   A variant that carries nothing holds an empty struct of its own.
3. An enum that carries no data may be a plain string enum. Each variant
   is a JSON string, in `snake_case`. `SystemPrompt` is one.

## A route on the wire

The vmm host API has one route table (decision CH25). It is
`rust/crates/wire/src/routes.rs`. Each route is `POST /<name>` with a JSON
body. The table pairs the route with its request type, its reply type and
its error type.

1. A route is a marker type, an empty enum named `<Name>Route`, with an
   impl of `JsonRoute` or `UpgradeRoute`. `NAME` is in `snake_case`. An
   upgrade route also has a `TOKEN` and no reply type.
2. The macro `route_table!` is the one list of the routes. It calls a
   macro of the caller once per route, with the kind `json` or `upgrade`.
   The schema test expands it, and `creche_vmm::routes!` expands it for
   the vmm router. Do not write a second list.
3. The traits carry serde bounds only. The `JsonSchema` bounds stay on the
   schema test, so a crate that depends on `creche-wire` needs no schemars.
4. The schema test writes the table into the schema, under the root key
   `x-routes`. Each type is the name of a `$defs` entry. A route type needs
   no `subschema_for` line of its own. A `json` entry names a `reply`. An
   `upgrade` entry names a `token` and no `reply`.
5. datamodel-codegen and TypeBox ignore the key `x-routes` itself. A route
   that names a new type adds that type to `$defs`. Run
   `uv run datamodel-codegen` after each change of a route.
6. A client sends `HOST_API_PROTOCOL` in the header `HOST_API_HEADER`. The
   vmm checks it before it reads the body, and the match is exact. A
   request that fails the check gets the mismatch reply (see "The protocol
   mismatch reply"). A change of the schema subset that the routes reach
   needs a new number (see "The protocol number").
7. The schema test also writes `src/creche_wire/routes.py`. It holds the
   protocol, its header, `HOST_API_BODY_CAP`, `MISMATCH_STATUS` and one
   `JsonRoute` or `UpgradeRoute` constant per route, named as the route in
   capitals: `ACP`, `CREATE`, `DROP`, `LIST`. pyright then refuses a wrong
   request, reply or error model on a Python client. A route with
   `JsonRoute::CLIENT_TIMEOUT` also gets `<NAME>_CLIENT_TIMEOUT_S`, from
   `client_timeout_s` of its entry in `x-routes`.
8. `encode` of each route takes only the request model of its route. It also
   keeps the request type invariant, so do not remove it.
   `src/creche_wire/_route_typing.py` holds the proof. Each wrong use there
   carries an ignore of its pyright error, and the file fails pyright when
   an ignore is unnecessary. A wider `JsonRoute` then fails the gate.
9. A caller imports a route from `creche_wire.routes`. The route constants
   and `JsonRoute` are not in `__all__`, because they are no wire type.
10. The vmm handler of a route implements `Serve<R>` or `ServeUpgrade<R>`
    (decision CH26 rule 4). A handler set that misses a route does not
    build. `ServeUpgrade` admits or refuses before 101 and returns a relay
    that the vmm runs. `UpgradeRoute::UPGRADE_REQUIRED` is the error of a
    request whose `Upgrade` header does not name `TOKEN`.

To add a route, add its marker and its line in `route_table!`. Then follow
the steps below.

### The body cap

`HOST_API_BODY_CAP` is 1 MiB, 1048576 bytes (decisions CH26, CH33 rule 2).
`x-routes` holds it under `body_cap`.

1. It caps each request body and each reply body of each route. A body of
   exactly the cap is under it. It binds each JSON body of the host API (a
   request, a reply, an error), and no ACP byte after 101 (decision CH34
   rule 3).
2. The vmm refuses a larger request with `too_large`. A client reads each
   reply with `read(..., cap=HOST_API_BODY_CAP)`.
3. A page of the list route ends early when one more row would take the
   reply past the cap. `next` then points on, so the client loses no row.
   The largest row is far under the cap, so such a page holds at least one
   row. A Rust test holds that.

### The list route

`list` reads one page of the host session list of one family (decisions
CH21-5, CH21-7). The family key comes from the list entry of the route
table plan, and it is required (decision CH34 rule 4). The request is
`ListSessions`, the reply is `SessionPage`, and the error is `ListError`.

1. `cursor` is `null` for the first page. A later page sends the `next` of
   the page before. `ListCursor` is opaque, an integer of 1 to 2^53 - 1.
2. `cwd` is `null` for every row, or a cwd that each row of the page has.
3. A family with no rows, or one the vmm does not know, gives an empty
   page. Any cursor reads. So `ListError` holds the shared codes only.

### The drop route

`drop` drops one session or every session of one family (decisions
CH21-2, CH21-10, CH27). The request is `DropTarget`, the reply is
`Dropped`, and the error is `DropError`. A drop cannot be undone.

1. `DropTarget` has one key: `session` (class `SessionDrop`) with
   `{family, id}`, or `family` (class `FamilyDrop`) with `{family}`.
2. A session drop names the family that owns the session (decision CH33
   rule 5). The vmm answers `not_found` 404 when no row has the id, and
   when the row belongs to another family (decision CH33 rule 4).
3. A family drop of a family with no rows answers `Dropped`. It never
   answers `not_found`.
4. The vmm stops each open ACP relay of the target first (decision CH26
   rule 5). It then stops every VM of the target at once (decision CH33
   rule 11). Each VM gets the graceful stop of decision CH19 (3) for up
   to 30 s (`DROP_STOP_WAIT`), then SIGTERM, then SIGKILL after 5 s
   (`DROP_SIGTERM_GRACE`, decision CH34 rule 5). A kill is no error. Last the vmm removes each
   snapshot, session disk and row, and answers `Dropped`.
5. A client waits 60 s for the reply (`DROP_CLIENT_TIMEOUT`, decision
   CH34 rule 5). It retries
   a timeout as a transient. A retry after the session is gone gets
   `not_found`, and the client counts that as done. `x-routes` holds the
   client timeout as `client_timeout_s` of the entry `drop`, and
   `routes.py` holds it as `DROP_CLIENT_TIMEOUT_S`. A Python or TypeScript
   client reads it there and never writes 60 itself.
6. `DROP_STOP_WAIT` and `DROP_SIGTERM_GRACE` are Rust constants only. The
   vmm alone applies them, so no client reads them.

### The acp route

`acp` opens the ACP stream of one session (decisions CH20, CH21-7). It is
an upgrade route. The request is `OpenAcp`, and the error is `AcpError`.
After 101 the connection carries raw ACP bytes, which the vmm relays to
guest port 1201 (decision CH13).

1. The client sends `POST /acp` with the protocol header, `Content-Type:
   application/json`, `Connection: Upgrade` and `Upgrade: creche-acp`
   (`AcpRoute::TOKEN`, `ACP.token`). The body is `OpenAcp`, `{family, id}`
   (decision CH33 rule 5). Two tests prove that this body crosses: hyper
   reads it before it answers 101 (`rust/crates/vmm/tests/upgrade_body.rs`),
   and httpx 0.28.1 over `uds=` sends it, then reads and writes raw bytes
   after 101 (`integration/proc/test_proc_vmm_upgrade.py`).
2. Without the token in `Upgrade`, the vmm answers `upgrade_required` 426
   (decisions CH26 rule 3, CH34 rule 6).
3. The vmm answers `not_found` 404 when no row has the id and when the row
   belongs to another family (decisions CH21-9, CH33 rule 5, CH34 rule 6).
   It also answers `not_found` while a drop of the session runs (decision
   CH34 rule 6).
4. A second Upgrade for a session with an open stream gets `busy` 409
   (decision CH33 rule 7).
5. With no room on the host, the vmm answers `host_full` 503 at once
   (decisions CH8-6, CH21-3, CH34 rule 6). The client retries.
6. The vmm answers 101 only after `ControlReady` (decision CH33 rule 6).
   The 101 follows the first `ControlReady` of the start, whatever
   `FacadeState` it reports (decision CH34 rule 7). When the VM does not
   run, the vmm boots or restores it first. A failed start answers
   `sandbox_start_failed` or `egress_assert_failed` 502 (decision CH34
   rule 6). The vmm also writes the fault file (decision CH21-1).
7. After 101 each failure is the end of the stream. The client learns the
   cause from the error of its next Upgrade.
8. The vmm checks a request in this order (decision CH34 rule 7): the
   protocol header, the route and the method, the `Upgrade` header, the
   body (`Content-Type`, cap, JSON, type), the session (`not_found`,
   `busy`), admission (`host_full`), the start of the VM.

## A route error on the wire

Each route has a typed error (decision CH26 rule 4). A caller matches it on
its `code` (decision CH25 rule 3).

1. Declare the error only with `route_error!` in `routes.rs`, as
   `Name = "code" => status` per variant. The wire form of a variant is
   `{"code":"<code>"}`. A variant carries nothing, and the macro writes it
   `V {}`.
2. Each code has one HTTP status, 4xx or 5xx (decision CH33). A status
   outside 400 to 599 fails to compile. `RouteError::STATUSES` lists each
   code with its status, and `status` gives the status of a value. The
   entry of each route in `x-routes` maps each code to its status under
   `statuses`.
3. No variant has a schema title. datamodel-codegen names each class by
   its position: `CreateError1`, `CreateError2`. A new code shifts these
   names. Export only the error type, such as `CreateError`, from
   `src/creche_wire/__init__.py`.
4. A Python caller matches `error.root.code`. Never name a class such as
   `CreateError1`.
5. Add an accepted case for each code. Add a refused case with the array
   form, an unknown code and an extra key.
6. No error body holds free text (decision CH33 rule 13). A variant carries
   nothing, so a body is only its code. The vmm logs the details.

### The shared codes

Each route error holds the codes that every route shares (decision CH33
rule 13). `route_error!` appends them after the codes of the route, so no
route can miss one. List only the codes of the route in `route_error!`.
`SHARED_CODES` lists them, in the order in which the vmm checks a request:

| Code | Status | The vmm refuses a request when |
|---|---|---|
| `no_route` | 404 | no route has its path. |
| `bad_method` | 405 | its method is not `POST`. |
| `bad_content_type` | 415 | its `Content-Type` is not `application/json`. |
| `too_large` | 413 | its body is over the cap. |
| `not_json` | 400 | its body is not strict JSON. |
| `not_valid` | 400 | its body is not the request type of the route. |
| `internal` | 500 | the vmm fails after it takes the request. |

1. Decision CH26 orders the checks after the protocol header: the
   `Content-Type`, then the body cap, then the JSON. A request must first
   reach a route, so `no_route` and `bad_method` come before them.
   Decision CH33 rule 13 lists the codes in this order.
2. A code of a route never repeats a shared code. A Rust test fails when
   one does, and when an error does not end with `SHARED_CODES`.
3. The protocol mismatch reply is in no route error (decision CH25 rule 1).
   See "The protocol mismatch reply".
4. The shared vectors hold an accepted case `{"code":"<code>"}` for each
   code in each route error: the codes of the route and the shared codes.
   They also hold a refused case of a shared code with an extra key. The
   Rust test `each_code_of_a_route_error_has_a_case` fails when one is
   missing.

## The protocol mismatch reply

The vmm checks the header `HOST_API_HEADER` first, before the route and the
body (decisions CH25 rule 1, CH26). A request without the header, or with
another protocol number, gets one reply (decision CH33 rule 1). So does a
value that is not the exact decimal text of `HOST_API_PROTOCOL`, such as
`01`, `1 2` or `x`, and a header given twice (decision CH34 rule 1).
HTTP/1.1 strips the spaces around a value first, so ` 1 ` reads as `1`
(decision CH40 rule 3):

| Status | Body |
|---|---|
| 412, `MISMATCH_STATUS` | `{"code":"protocol_mismatch","protocol":<n>}` |

`<n>` is the `HOST_API_PROTOCOL` of the vmm. The type is `ProtocolMismatch`.

1. The reply is frozen for all protocol numbers. A new protocol number
   never changes its body or its status, because a client of an older
   protocol must still read it. The Rust test `the_mismatch_reply_is_frozen`
   pins the bytes and the status. Never change that test or the
   `ProtocolMismatch` cases to make a change pass.
2. The reply is in no route error. No code of a route error is
   `protocol_mismatch`, and no code has the status 412. A Rust test fails
   when one does.
3. `x-routes` names the reply and its status under `mismatch`.
   `src/creche_wire/routes.py` holds `MISMATCH_STATUS`.

### The client read order

A client reads each answer of the vmm in this order:

1. The status is `MISMATCH_STATUS`: read the body as `ProtocolMismatch`.
   Then follow the retry rule below.
2. The status is 2xx: read the body as the reply of the route. For an
   Upgrade route, the status 101 starts the stream, with no JSON body.
3. Any other status: read the body as the error of the route, and match
   its `code`.

The retry rule (decision CH33 rule 10): after a mismatch, attendance tries
3 times, 2 s apart. The 3 tries are 3 requests in all, the first one
included, so the fault comes 4 s after the first mismatch (decision CH34
rule 2). When each try gets the mismatch, attendance records the fault
`vmm_protocol_mismatch`. caregiver keeps decision CH25 rule 1 (retry a few
times, then report a named fault), with no fixed numbers. The rule lives in
the client. This package holds no code for it.

## The protocol number

A change of the schema subset that the routes reach needs a new
`HOST_API_PROTOCOL` (decision CH33 rule 8). Any change counts, an added
key or an added route too, because the match of the number is exact.

1. `rust/crates/wire/schema/host-api-<N>.json` holds a frozen copy of that
   subset for the number N. The subset is the whole `x-routes` and each
   `$defs` entry that a route type reaches through `$ref`,
   `ProtocolMismatch` too (decision CH34 rule 8). A `title` stays, because
   it names a generated class. A `description` goes. So a new route or
   status also needs a new number.
2. The schema test fails when the subset differs from the file of the
   current number. The `rust` job of CI runs that test.
3. Write mode never writes over the file of the current number. It writes
   a file only for a number that has none.
4. To change a reached type, raise `HOST_API_PROTOCOL` by one. Then run
   write mode, and commit the new file. Keep each older file. The test
   `each_frozen_host_api_file_is_kept` fails when a file of a number up to
   the current one is missing.
5. Never edit a `host-api-<N>.json` file by hand. The step `frozen host
   api` of the CI `rust` job diffs the files against the commit the change
   starts from. It fails when a change edits or deletes one, so a file
   deleted and written again fails too.
6. A change of a type that no route reaches needs no new number:
   `FamilyStateReply`, `TurnEvent`, `ControlHello`, `ControlCommand`,
   `GuestEvent`, `FamilyShape` and the types that only they reach. A change of a
   `description` needs none either.

## The vsock JSON links

Two vsock links and one Unix socket in the guest carry wire types as JSON
(decisions CH36 G11, CH62 rule 10). Each one carries one JSON value per
line.

| Link | The host end sends | The guest end sends | Line cap |
|---|---|---|---|
| Control, guest port 1200 | The vmm sends `ControlHello`, then `ControlCommand` | Guest init sends `ControlReady`, then `GuestEvent` | `CONTROL_LINE_CAP`, 64 KiB (decision CH43 rule 1) |
| Family state, host port 1100 | The vmm sends one `FamilyStateReply` per pull | The facade sends `FamilyStatePull` (decisions CH5a, CH43 rule 4) | `FAMILY_STATE_CAP`, 256 KiB (decision CH23 default 4) |
| Local link, `/run/creche/guest-init.sock` in the guest (decision CH62 rule 10) | Guest init, the listener, sends `ControlCommand`, only `stop` | The facade sends `TurnEvent` | `CONTROL_LINE_CAP` |

1. A writer writes each value in the compact form of serde_json, then one
   LF. The compact form holds no LF, because JSON escapes an LF in a
   string. A writer writes no CR and no blank line.
2. A reader reads one line up to its LF. The cap counts the bytes of the
   line, the LF not counted. A reader that gets more bytes than the cap
   and no LF refuses the line. It does not read the rest.
3. A reader reads each line with the strict reader of its language: the
   check `strict` of `rust/crates/vmm/src/server/check.rs`,
   `creche_wire.read` in Python and `read` of `agent/src/wire-read.ts` in
   TypeScript. It then reads the line as the type that the link expects
   at that point.
4. On the control link the vmm sends `ControlHello` first, on each new
   link (decisions CH19 rule 2, CH28). Guest init answers with one
   `ControlReady`. After that the vmm sends `ControlCommand` and guest
   init sends `GuestEvent`, each at any time. Guest init sends nothing
   before `ControlReady` (decision CH62 rule 11). `GuestEvent` holds a
   `facade` line at each change of the facade state, a `synced` line
   after each `sync`, and each turn event of the local link nested in a
   `turn` line (decisions CH21, CH62 rules 11 and 23). A `facade` line
   has the same bytes as the `ControlReady` of the same state, so a
   reader tells the two apart by their place on the link.
5. `stop` of `ControlCommand` starts the graceful stop (decision CH19
   rule 3). `DROP_STOP_WAIT` and `DROP_SIGTERM_GRACE` bound it (see "The
   drop route").
6. A refused line ends the link: the reader closes it and logs why
   (decision CH43 rule 2). No link carries an error reply for a line.
7. The framing is part of the guest init protocol. Once guest init is
   deployed, a change of the framing or of a cap needs a new
   `GUEST_INIT_PROTOCOL` (decision CH14), so that an old snapshot
   cold-boots. No route reaches these types, so `HOST_API_PROTOCOL` stays.
8. On host port 1100 the facade sends `FamilyStatePull`, the empty
   object, before each prompt, and the vmm answers each pull with one
   `FamilyStateReply` (decisions CH5a, CH23, CH43 rule 4). One link
   carries many pulls. The facade connects to
   `/run/creche/family-state.sock` in the guest, and guest init splices
   each connection to a new vsock dial of host port 1100, for the facade
   pid only (decision CH62 rule 13). A `state` reply over
   `FAMILY_STATE_CAP` goes out as `too_large` (decision CH31 rule 3).
9. On the local link the facade sends `TurnEvent` and guest init sends
   `stop` (decision CH62 rules 9 and 10). Guest init accepts one link at
   a time, and only from the pid of the live facade (`SO_PEERCRED`). It
   reads each turn event strictly. A refused line closes the link, and
   guest init then kills the facade (decisions CH32 rule 7 and CH62 rule
   11). An event that comes while no control link is open is dropped,
   with no buffer. No guest init is deployed yet, so these new lines keep
   `GUEST_INIT_PROTOCOL` at 1 (rule 7).

## Change a wire type

1. Change the type in `rust/crates/wire/src/`.
2. In `rust/`, run `CRECHE_WIRE_SCHEMA=write cargo test -p creche-wire`. The
   test writes `schema/wire.json`, `agent/src/wire.ts` and
   `src/creche_wire/routes.py`. When the routes reach the type, it fails
   until you raise `HOST_API_PROTOCOL` (see "The protocol number").
3. At the root of the repository, run `uv run datamodel-codegen`. It writes
   `src/creche_wire/models.py`. The `[tool.datamodel-codegen]` section of the
   root `pyproject.toml` holds its settings.
4. Export a new type from `src/creche_wire/__init__.py`.
5. Add its cases to `rust/crates/wire/vectors/wire.json`, and its line to
   `read_and_write` in `rust/crates/wire/src/vectors.rs`.
6. Commit the files together.

`bin/quality-gate.sh` runs `uv run datamodel-codegen --check`. The check
fails when `models.py` is not what the schema generates. The Rust test fails
when `wire.json`, `wire.ts` or `routes.py` is not what the Rust types give.

## Who uses it

No program imports the package yet. caregiver and attendance become its
callers when they talk to the VM owner. Each one then names `creche-wire` as
a dependency.

## Known gaps

1. Decision CH43 rule 3 names the family VM shape file (`FamilyShape`)
   `config/shape.json` (`FAMILY_SHAPE_FILE`). Contract 01 §6.1 lists the
   entries of `config/` and needs an amendment for this file, as decision
   CH23-1 amended it for `model_live_at`. That amendment is not made yet.
