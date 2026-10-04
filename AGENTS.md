# AGENTS.md

Rules for every contributor and every agent that edits this repository. A
package's own `AGENTS.md` adds rules for that package. Read both before you
edit.

## Documentation standard

**Every Markdown document in this repository is written in ASD-STE100
Simplified Technical English.** Read `docs/writing-standard.md` before you
write or change one. A pull request that breaks the standard is not merged
until the prose is fixed. This is the most important rule in this file.

## Setup

1. Run `git config core.hooksPath githooks` once per clone.
2. Run `uv sync` once.
3. For `playpen/`, run `pnpm install` in that directory.
4. For `rust/`, install rustup. It installs the toolchain that
   `rust/rust-toolchain.toml` names.

## Every change

- Every commit passes `bin/quality-gate.sh`: ruff, ruff format, pyright.
  The pre-commit hook runs it. Do not use `--no-verify`.
- A commit that changes a path under `rust/` also passes `cargo fmt` and
  `cargo clippy`. It also passes `cargo deny` where `cargo-deny` is on
  `PATH`. A change with no path under `rust/` needs no `cargo`.
- A push runs the tests of the packages it touches. CI runs the full suite
  and is the merge gate. Read the one check named `gate`.
- Fix a bug in this order: write the test, watch it fail, write the fix,
  watch it pass.
- Edit only the lines the change needs. Do not add a comment to code you did
  not change.
- `main` takes pull requests only, through the merge queue.

## Commit messages

1. Separate the subject from the body with one blank line.
2. Limit the subject to 50 characters. 72 is the hard limit.
3. Capitalize the subject. Do not end it with a period.
4. Write the subject in the imperative: "Add", not "Added" or "Adds".
5. Wrap the body at 72 characters.
6. Use the body for what changed and why. The code says how.
7. End the message with `Co-Authored-By` and nothing else. Do not add a
   `Claude-Session` line.

## Security rules that hold everywhere

- No secret on argv, in a URL, in a log line or on a page.
- Bind the LAN address or a Unix socket. Never bind `0.0.0.0`.
- Fail closed. A missing token file, a short key or a bad config refuses to
  start. Do not add a path that starts with a warning instead.
- Every byte from a sandbox, from another process or from a request body is
  untrusted. Validate shape and size before use.
- Keep every pattern anchored with `\Z` or `re.fullmatch`. A `$` also
  matches before a final newline.

## Sanitization

Name no host, no user, no domain, no address and no person of any
deployment. Write "the operator" and "the host". That information belongs in
the private repository. `docs/writing-standard.md` section 6 has the full
rule.

## Code style

- Extract a repeated or meaningful value into a named constant. Keep a
  one-off value inline.
- Return early. Avoid deep nesting.
- Keep function names under 30 characters.
- Use an enum, not a boolean, for a function parameter.
- Put a blank line between logical blocks.
- Always use `{}` on a one-line `if` in TypeScript.
- Keep a field or function private unless the design needs it public. Ask
  before you widen visibility.
- A layer talks only to the layer directly below it. Do not call through a
  layer.

## Where the rules for a directory live

| Directory | Read |
|---|---|
| `chaperone/` | `chaperone/AGENTS.md` |
| `family/` | `family/AGENTS.md` |
| `attendance/` | `attendance/AGENTS.md` |
| `caregiver/` | `caregiver/AGENTS.md` |
| `door-owui/`, `door-tui/`, `door-trigger/` | the door's own `AGENTS.md` |
| `noticeboard/` | `noticeboard/AGENTS.md` |
| `playpen/`, `toybox/` | `playpen/AGENTS.md`, `toybox/AGENTS.md` |
| `handover/` | `handover/AGENTS.md` |
| `library/` | `library/AGENTS.md` |
| `integration/` | `integration/AGENTS.md` |
| `bin/` | `bin/AGENTS.md` |
| `rust/` | `rust/AGENTS.md` |
| `vectors/` | `vectors/AGENTS.md` |
| `systemd/` | `systemd/AGENTS.md` |
| branches, hooks, CI, tags | `CONTRIBUTING.md` |

## When a contract is silent

Take the most conservative reading. Mark the spot with a `CONTRACT-QUESTION:`
comment that names the section, the reading you took and what a change would
cost. Say the same in the pull request. List it under "Known gaps" in the
package's `AGENTS.md` while it is open. Do not deviate quietly.
