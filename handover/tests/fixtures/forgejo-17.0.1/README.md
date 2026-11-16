# Recorded replies of Forgejo 17.0.1

These files are sanitized copies of replies that Forgejo 17.0.1 gave on a
scratch repository on 2026-11-15 and 2026-11-16. The tests of the reading
side of the Forgejo release path read them
(`docs/design/forgejo-release-path.md`, section 5). Do not change a file to
make a test pass. Record the reply again instead.

Each file holds the body of one reply. `index.tsv` holds one row for each
file: the file, the method, the path, the status and the token kind. The
tests take the status from `index.tsv`, never from a literal.

## The files

| File | Request | Status | What it shows |
|---|---|---|---|
| `p3-release-by-tag.json` | `GET .../releases/tags/rec-ann` | 200 | A Release read back. The author is the operator |
| `p3-job-log-markers.txt` | `GET .../actions/jobs/189/logs` | 200 | The marker lines of the job that made the Release. The line `M release_body=` holds the create reply, with the author `forgejo-actions` |
| `p2-git-refs-tag.json` | `GET .../git/refs/tags/rec-ann` | 200 | A list of one ref, to an annotated tag |
| `p2-git-refs-prefix.json` | `GET .../git/refs/tags/rec-` | 200 | A prefix match: a list of two refs |
| `p2-git-refs-prefix-measure.json` | `GET .../git/refs/tags/measure` | 200 | A prefix match: two annotated tags and two lightweight tags |
| `p2-git-refs-missing.json` | `GET .../git/refs/tags/no-such-tag` | 404 | A missing tag |
| `p5-compare-ancestor.json` | `GET .../compare/main...<ancestor>` | 200 | `total_commits` 0 |
| `p5-compare-main-itself.json` | `GET .../compare/main...<main>` | 200 | `total_commits` 0 |
| `p5-compare-ahead.json` | `GET .../compare/main...<ahead>` | 200 | `total_commits` 1 |
| `p5-compare-diverged.json` | `GET .../compare/main...<diverged>` | 200 | `total_commits` 1 |
| `p5-compare-missing.json` | `GET .../compare/main...<missing>` | 404 | A missing commit |
| `p4-commit-status-main.json` | `GET .../commits/<merge>/status` | 200 | Four push contexts, each `success` |
| `p4-commit-status.json` | `GET .../commits/<ahead>/status` | 200 | Seven push contexts: one `failure` and one `skipped` |

The tests also read four files in place from the fixtures of the signer
crate, `rust/crates/ci-signer/tests/fixtures/forgejo-17.0.1/`, with their
rows of the `index.tsv` there. They are `p1-commit-pull-merge-sha.json`,
`p1-commit-pull-pr-head.json`, `p2-git-tags-sha.json` and
`p2-tags-get.json`. This directory holds no copy of them, so the two
copies cannot drift.

No recording holds a status for the head of the recorded pull request, and
no recording holds a `pull_request` context. A test that needs one changes
a recorded reply in test code and says so.

## The substitutions

A scratch script made these changes to the bytes of each file and of
`index.tsv`:

1. The name of the organization became `example-owner`.
2. Each occurrence of the operator's name became `operator`: the login,
   the user name, the commit author and committer names and the path of
   each profile URL (`html_url`).
3. Each UTC offset of a local time became `+00:00`. The clock time did not
   change.

The address of the instance is `192.0.2.10`, from the documentation range
TEST-NET-1. The emails were already `user@example.invalid`. The git object
ids and the avatar fields did not change.

## The test that holds the substitutions

`test_handover_frp_fixtures.py` reads each file. It works by allowlist. It
refuses these values:

1. An address outside `192.0.2.0/24`.
2. A login, a user name, an author name or a committer name other than
   `example-owner`, `operator` and `forgejo-actions`.
3. A repository whose owner is not `example-owner`, in a path, in a URL, in
   `full_name` or in an error text.
4. An email other than `user@example.invalid`.
5. An offset other than `Z` and `+00:00`.
