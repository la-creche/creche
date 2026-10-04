# noticeboard

The one view. One page shows every family, sandbox and session, and that
view is the truth. It is also where the operator edits a family. The root
`AGENTS.md` applies here too.

Server-rendered HTML from FastAPI and Jinja2. No JavaScript build, no CDN, no
script tag on any page. Every page works with scripts off.

## The sentence that bounds this package

**Read everything, write one thing: a validated git commit in the registry.**

There is no systemd call, no daemon verb and no `sbx` in this package. Adding
one is a design change. `caregiver` watches the registry and converges. A
view that also applied would be a second writer to the same state.

| It reads | From |
|---|---|
| family state, sandboxes, faults, spend | `caregiver`'s status documents |
| why a family is invalid | the validation report |
| what an autonomous run did | the outcome records |
| every tool call and its decision | the chaperone's audit files |
| sessions, turns, the event stream | `attendance`, as the principal `view-ro` |
| the family file behind the form | the registry checkout |

## Perimeter rules

1. Never add a "key not configured, skip the check" branch. An empty key is
   legal only on a loopback bind. `config.from_env` raises otherwise.
2. Never bind a wildcard.
3. Never read the request body in middleware. The CSRF hidden field is read in
   the route.
4. Keep the `Origin` and `Referer` check even when the token matches.
   `SameSite=Strict` classifies by registrable domain, so a sibling
   subdomain is still same-site.
5. No secret on a page, in a log line or in a URL. `--check` says whether a
   key is set, never what it is.
6. `/static` stays styling only. Nothing that holds a fact may live there.

A route parameter is an id. `app.py` checks a family name against
`registrywrite.NAME_RE` and a session id with `sessions.is_session` before
any use. A value that fails answers 404.

## Reading another process's files

7. Nothing that reads a file may raise. `jsonfiles.py` answers a problem
   string, and the page renders it as a report.
8. Every list from another process is capped. A cap that bites is reported.
9. Split NDJSON on LF alone. U+2028 and U+2029 are legal inside a JSON
   string.
10. A status document older than 90 seconds reads `unknown`, never its last
    state.
11. Spend is one number per family. `SandboxRow` has no spend field. A count
    comes from the service that can count it, or the page shows none.
12. Key "settled" on the `turn_settled` line, never on the wrapped
    `agent_settled` event.

## The form

13. Never hand-maintain a list of field names. `familyform.py` reads
    `FamilyFile.model_fields` and asks `check_family` for its locks.
14. A locked field is disabled and dropped from the saved document. A locked
    required field keeps its value.
15. Recompute the locks on the server.
16. A field a rule forbids renders disabled with the rule as its label, never
    hidden.

## Writing the registry

17. Snapshot the family's whole directory, not two known paths.
18. Restore from a `finally`. `_git` turns a timeout and an `OSError` into a
    failed call.
19. Pin the commit identity with `-c user.name`.
20. Scope the commit to one pathspec.
21. Build git's environment from an allowlist, not by filtering.
22. Hold the lock of the registry for the whole save. The lock is an
    exclusive `flock` on the registry directory, and one save runs at a time.
    The service refuses a save that waits more than 10 seconds for the lock.
    Git's own `index.lock` makes a commit fail while another program writes
    the index, and a failed commit restores.
23. Validate the whole registry with the new text before you write. The
    validator reads a copy of `families`, `mcp` and `skills`. `caregiver`
    then never reads a text that the validator refuses. Write the family
    file to a temporary file in its own directory. Rename it into place.
24. The noticeboard never parses YAML itself. `agent_family` owns the reader.
    `yamlkeep.py` is the one module that touches a YAML library, and it reads
    no meaning. `app.py` gives the patched text back to `agent_family`. When
    the text does not read as the model of the form, the save writes the
    document of `yamlout.py`.

## Run it

```bash
uv run noticeboard --check                      # validate the environment, print, exit
VIEW_BIND=127.0.0.1 VIEW_STATE_ROOT=/tmp/rework uv run noticeboard
uv run noticeboard-verify --json                # the component's verify hook
```

| Variable | Meaning |
|---|---|
| `VIEW_BIND`, `VIEW_PORT` | the listen address and port 8370. Never a wildcard. |
| `VIEW_ACCESS_KEY`, `VIEW_ACCESS_KEY_FILE` | the `X-View-Key` value the proxy injects. The file wins. |
| `VIEW_STATE_ROOT` | families, audit, outcomes, tokens |
| `VIEW_REGISTRY_DIR` | the git checkout the form commits in |
| `VIEW_SESSIOND_SOCKET`, `VIEW_SESSIOND_URL` | `attendance`, over a socket or TCP |
| `VIEW_PAGE_SIZE` | audit lines and sessions per page |
| `VIEW_COOKIE_SECURE` | `Secure` on the CSRF cookie. Set `0` only for a plain-HTTP dev run. |

A LAN bind with no key refuses to start, exit 2. The reverse proxy injects
`X-View-Key` inside its `location /` block and forwards `Cookie`, `Origin`
and `Referer` unchanged. `/healthz` and `/static/*` stay keyless.

## Layout

| Module | What |
|---|---|
| `config.py`, `security.py` | the environment and the two starts it refuses, the key and CSRF checks |
| `jsonfiles.py`, `statusdocs.py`, `auditfiles.py` | reading another process's files without raising |
| `sessions.py`, `attendancehttp.py`, `transcript.py` | `attendance` as `view-ro`, the wire, journal lines folded for a reader |
| `familyform.py`, `yamlout.py`, `yamlkeep.py`, `registrywrite.py` | the form, the whole file, the patched file, the commit |
| `pages.py`, `app.py` | what each page needs, and the routes |
| `verify.py` | `noticeboard-verify` |
| `templates/`, `static/` | Jinja2 and one stylesheet |

## Tests

```bash
uv run pytest noticeboard/tests
```

Every source is a fixture: a state root on disk, a git registry in a tmp dir,
a fake `attendance` behind `sessions.Transport`. A new test file's basename is
prefixed `test_noticeboard_`.

## Known gaps

- The session list derives the door from the session id prefix, then from
  `writer.holder` (`sessions.py`).
- No contract says how a view running as another user reads `view-ro.token`
  (`config.py`).
- The base URL over the Unix socket is `http://sessiond`, never resolved
  (`config.py`).
- A save marks its commit with a `Via: noticeboard` trailer
  (`registrywrite.py`).
- The family page reads whatever `report_path` the status document names
  (`pages.py`).
- Contract 05 §2 names no encoding for a file. `jsonfiles.py` takes UTF-8,
  UTF-16 and UTF-32, as `json.loads` does.
- No contract gives a range for a count. `jsonfiles.integer` takes an integer
  of any size.
- `spec.md` §8.1 names no answer for a route parameter that is not an id.
  The route answers the 404 of a path that has no route (`app.py`).
- `spec.md` §8.3 names no answer for an exception that no reader predicted.
  The service answers 500 with the refusal body and the word `internal`
  (`app.py`).
- A save still rewrites two shapes that it did not edit (`yamlkeep.py`). The
  first is a flow mapping inside a flow mapping. The second is a flow mapping
  on a line past column 100.
- A save that removes the last key of a block also removes the comment lines
  and the blank lines after that key (`yamlkeep.py`).
- The copy that a save validates keeps each link as a link
  (`registrywrite.py`). A relative link that leaves `families`, `mcp` and
  `skills` does not resolve there. When the validator needs its target, the
  save is refused, and the error can name a file that the checkout holds.
  A `families` directory that is a link refuses each save.
- `spec.md` §8.2 names no rule for two saves at one time. One save runs at
  a time for each registry, and a save that waits more than 10 seconds for
  its turn writes nothing (`registrywrite.py`).
- The lock of a save holds only the saves of this service. Another program
  that writes the checkout does not take it (`registrywrite.py`). When the
  commit of a save then fails, the save restores the bytes that it found.
- The form holds no revision of the file that it shows. A save from a form
  that is older than the newest commit puts each field back to the value
  that the form holds (`app.py`). The preview shows each such field.
- A user unit started before its user joined the `agents` group cannot read
  the chaperone's 0640 audit files until the host reboots. The audit page
  shows a banner and renders the rest.
