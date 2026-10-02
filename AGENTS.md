# AGENTS.md

- Set the hooks once per clone: `git config core.hooksPath githooks`. Never
  use `--no-verify`.
- Every commit passes `bin/quality-gate.sh`. CI's check named `gate` is the
  merge gate, and `main` takes pull requests only, through the merge queue.
- No number lives in a file. A `bump:minor` or `bump:major` label on the
  pull request raises the next tag of each component it changes.
- A commit message ends with `Co-Authored-By` and nothing else. Never add a
  `Claude-Session` line.
- Name no host, user, domain, address or person of any deployment here.
  That belongs in the private repository.
