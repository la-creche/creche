# ci-runner

The crate `creche-ci-runner`: the daemon that starts one throwaway Cloud
Hypervisor VM per waiting Forgejo Actions job (`docs/design/ci-runner.md`).
Only the root `AGENTS.md` applies here.

The crate holds the Forgejo API client (build plan PR 4). The per-job
disks, the VM start, the job loop, the program and the component row come
in PRs 5 to 7.

## Layer rule

- `creche-ci-runner` calls no other crate of this workspace. No crate of
  this workspace calls it.
- The client uses the HTTP/1.1 stack of the vmm: `hyper`, `hyper-util`,
  `http-body-util` and `tokio`. It adds no crate to `rust/Cargo.lock`.
- `src/transport.rs` holds the socket and the hyper mechanics.
  `src/client.rs` speaks in calls. `src/reply.rs` reads each reply.

## Rules

1. The client makes three calls on the runners of one organization
   (design section 4.2):

   | Call | Request | Reply |
   |---|---|---|
   | `waiting_jobs` | `GET /api/v1/orgs/{org}/actions/runners/jobs?labels=a,b` | 200 |
   | `register` | `POST /api/v1/orgs/{org}/actions/runners` | 201 |
   | `remove_runner` | `DELETE /api/v1/orgs/{org}/actions/runners/{id}` | 204, or 404 |

2. Each request carries `Authorization: token <api token>`. The client
   reads the API token from the file that its caller names. The file
   holds 40 ASCII letters or digits and at most one newline. A file that
   gives others a mode bit, or the group the write bit, is refused.
3. The client reads each reply as untrusted input. The body cap is 1 MiB.
   A reply type reads only the fields that the daemon uses. It skips
   every other field, as `creche-vmm-store` reads the `info` reply of
   Cloud Hypervisor. A field that the type reads must have its type.
4. Forgejo writes an empty jobs list as `null`. The client reads `null`
   and `[]` as an empty list. A job in the list must have the status
   `waiting` and the task id 0, or the list fails.
5. A 404 of a removal is `Removal::Absent`, not an error. Forgejo removes
   an ephemeral runner after its one job.
6. An error names the call and the status. A 401 keeps no message,
   because Forgejo puts the token that it got into that message. Each
   other message has each copy of the API token replaced by `<token>`.
7. `ApiToken` and `RunnerToken` have no `Display`. Their `Debug` prints
   only the type name.
8. The caller gives the bound of each call. The crate names no value.

## Tests

- `tests/fixtures/forgejo-17.0.1/` holds the replies recorded from
  Forgejo 17.0.1 on 2026-11-15. Two edits keep the rule of section 6 of
  `docs/writing-standard.md`. The Forgejo address in `html_url` and `url`
  is `192.0.2.10:3000`. The organization in `html_url` is `example-org`.
- `src/reply.rs` reads each recorded reply with no socket.
- `tests/forgejo_client.rs` serves each recorded reply from a stand-in
  Forgejo on 127.0.0.1. Each test checks the request that the stand-in
  got and the typed result.

## Known gaps

- CONTRACT-QUESTION: design section 4.1 labels the link to Forgejo
  "HTTPS". Section 4.5 lets each VM reach only the HTTP port of the
  Forgejo host, and the replies were recorded over plain HTTP. The
  reading taken is plain HTTP to an IP address on the LAN.
  `ForgejoOrigin` takes only `http://<address>:<port>`. A change to HTTPS
  adds a TLS crate and a CA file, and changes `client.rs` and
  `transport.rs` only.
- The token rule takes 40 ASCII letters or digits. Forgejo writes hex,
  but the recording replaced the runner token with zeros, so the
  alphabet of a real token is not checked.
