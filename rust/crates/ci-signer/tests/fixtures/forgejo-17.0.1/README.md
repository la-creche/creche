# Recorded replies of Forgejo 17.0.1

These files are sanitized copies of replies that Forgejo 17.0.1 gave on a
scratch repository on 2026-11-15 and 2026-11-16. The tests of this crate
read them. Do not change a file to make a test pass. Record the reply again
instead.

Each file holds the body of one reply. `index.tsv` holds one row for each
file: the file, the method, the path, the status and the token kind. The
tests take the status from `index.tsv`, never from a literal.

## The files

| File | Request | Status | What it shows |
|---|---|---|---|
| `p1-run-push-main.json` | `GET .../actions/runs/48` | 200 | A merge push to `main` (workflow `probe.yml`) |
| `p1-run-push-branch.json` | `GET .../actions/runs/56` | 200 | A push to another branch |
| `p1-run-push-tag.json` | `GET .../actions/runs/65` | 200 | A pushed tag at the merge commit of run 48 |
| `p1-run-push-tag-main.json` | `GET .../actions/runs/15` | 200 | A tag named `main` (workflow `test.yml`) |
| `p1-run-pull-request.json` | `GET .../actions/runs/70` | 200 | A `pull_request` run |
| `p11-run-prt.json` | `GET .../actions/runs/69` | 200 | A `pull_request_target` run (workflow `prt.yml`) |
| `p1-run-workflow-dispatch.json` | `GET .../actions/runs/104` | 200 | A `workflow_dispatch` run (workflow `rel.yml`) with an empty `event` |
| `p1-commit-pull-merge-sha.json` | `GET .../commits/<merge>/pull` | 200 | The PR of a merge commit |
| `p1-commit-pull-pr-head.json` | `GET .../commits/<head>/pull` | 404 | A commit that is no PR merge |
| `p2-tag-create.json` | `POST .../tags` | 201 | An annotated tag: the message with no final LF |
| `p2-tags-get.json` | `GET .../tags/rec-ann` | 200 | The same tag: the message with no final LF |
| `p2-git-tags-sha.json` | `GET .../git/tags/<sha>` | 200 | The same tag object: the message with one LF added |
| `p2-tag-size-b131071.json` | `POST .../tags` | 201 | A message of 131071 bytes |
| `p2-tag-size-b131072.json` | `POST .../tags` | 500 | A message of 131072 bytes: `argument list too long` |
| `version.json` | `GET /version` | 200 | The version of the instance |

Every run reply has the status `success`. A test that needs a run that waits
or runs changes the status in test code and says so.

## The substitutions

A scratch script made these changes to the bytes of each file and of
`index.tsv`, also inside `event_payload`:

1. The name of the organization became `example-owner`.
2. The login of the operator became `operator`.
3. Each UTC offset of a local time became `+00:00`. The clock time did not
   change.

The address of the instance is `192.0.2.10`, from the documentation range
TEST-NET-1. The emails were already `user@example.invalid`. The git object
ids and the avatar fields did not change.

The test `fixtures_are_sanitized` reads each file. It refuses an address
outside `192.0.2.0/24`, a login other than `example-owner`, `operator` and
`forgejo-actions`, a repository of another owner and an offset other than
`Z` and `+00:00`.
