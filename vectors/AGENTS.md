# vectors

This directory holds the oracle for the Rust port. The oracle is what the
Python implementation accepts, as data. `README.md` holds the file format
and the commands. The root `AGENTS.md` applies here too.

This directory is not a component. A change here mints no tag. It is not a
workspace package, so it does not change `uv.lock`.

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
| `core.py` | `normalize`, the input forms, `Vector`, `Surface`, `render` |
| `generate.py` | the list of groups, the index, `--check`, `--counts` |
| `surfaces/ids.py` | the id grammars and `ids/disagreements.json` |
| `surfaces/family_cases.py` | the written `family.yaml` inputs |
| `surfaces/family_file.py` | `family_file` and `family_file.host` |
| `surfaces/channel.py` | `channel.parse`, `channel.frame`, `channel.build` |
| `surfaces/grants.py` | `grants.parse`, `chaperone.call_body`, `chaperone.approval_body` |
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
- `channel.parse` gives no vector for `unknown_address` or `sequence_gap`.
  Those refusals need the state of a channel.
- No vector covers the size cap of a grant file or the body cap of the
  chaperone. Each cap is checked before the entry point of its surface.
- Four patterns have no public entry point: `_ENV_NAME_RE` in the four
  `verify.py` modules, `_LOCK_PATH` and `_ARG_NAME` in
  `agent_family.serverrules`, and `_REPO_NAME` in `handover.site`.
- An id surface whose entry point is a pattern records the pattern alone. A
  caller of that pattern can add a length cap.
- `family_file` uses two time zone names that every tz database holds. A
  name that depends on the file system of the host has no vector.
- The vectors with a 400,000-deep nesting assume the default stack size. A
  larger stack can let Python 3.14 read that input.
- The generator runs on macOS and on Linux. No run on another system is
  recorded.
