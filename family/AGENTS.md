# family

The one place `family.yaml` and `mcp/<name>/server.yaml` are defined. The
package is `agent_family`. `caregiver` is its only caller. The root
`AGENTS.md` applies here too.

Two products come out of it:

- Validated objects: `FamilyFile` and `McpServerFile`.
- A `Report`: a list of `Issue` rows with severity, location and message.

Bad input never raises. A bad definition yields a report, not a crash.

## Layout

| Module | Owns |
|---|---|
| `grammar.py` | names, ranges and small parsers. One pattern per identifier. |
| `model.py` | `family.yaml` shape only: fields, types, `extra="forbid"` |
| `server.py` | `server.yaml` shape, same rule |
| `parse.py` | YAML text to a model, or to issues. Never an exception. |
| `report.py` | `Issue`, `Report`, `FamilyState` |
| `validate.py` | `family.yaml` value and cross-reference rules |
| `serverrules.py` | `server.yaml` value rules |
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

## What it enforces

| Axis | Rule |
|---|---|
| Unknown fields | refused on every model |
| `name` | `[a-z][a-z0-9-]{1,30}`, equal to the directory name |
| Mounts | absolute, no glob, under an allowed root. The platform root is fenced to one family. |
| `tools` | the server is declared, and each tool is in its catalog |
| `tools: all` on `autonomous` | refused |
| `delegates` | each target exists and is `kind: thin` |
| `max_inflight_delegations` | 1 to 8, default 2 |
| `egress` | no IP literal, no wildcard, port 1 to 65535 |
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
