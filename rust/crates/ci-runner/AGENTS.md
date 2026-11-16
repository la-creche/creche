# ci-runner

The crate `creche-ci-runner`: the daemon that starts one throwaway Cloud
Hypervisor VM per waiting Forgejo Actions job (`docs/design/ci-runner.md`).
Only the root `AGENTS.md` applies here.

The crate holds the Forgejo API client (build plan PR 4) and the disks of
a job (PR 5). The VM start, the job loop, the program and the component
row come in PRs 6 and 7.

## Layer rule

- `creche-ci-runner` calls no other crate of this workspace. No crate of
  this workspace calls it.
- The client uses the HTTP/1.1 stack of the vmm: `hyper`, `hyper-util`,
  `http-body-util` and `tokio`. It adds no crate to `rust/Cargo.lock`.
- The disks use `rustix` of the vmm for `FICLONE`, `SEEK_DATA` and
  `SEEK_HOLE`, and `fatfs` =0.3.6 for the VFAT seed. `fatfs` adds four
  crates to `rust/Cargo.lock`: `fatfs`, `bitflags` 1.3.2, `byteorder` and
  `log`.
- `src/transport.rs` holds the socket and the hyper mechanics.
  `src/client.rs` speaks in calls. `src/reply.rs` reads each reply.
- `src/vfat.rs` holds the calls of `fatfs`. `src/disks.rs` speaks in
  disks. `src/seed.rs` writes the cloud-init files. `src/net.rs` holds
  the network values of a VM.

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

## The disks of a job

`JobDisks::write` makes the run folder `<runs>/<job handle>` with the mode
0700, and writes three files into it, each with the mode 0600 (design
section 4.4):

| File | What it is | CH `--disk` |
|---|---|---|
| `seed.img` | The seed: VFAT, 1 MiB, label `CIDATA` | `image_type=raw,readonly=on` |
| `data.raw` | A sparse raw file of a `DataDiskSize`, 1 to 1024 GiB, with no file system | `image_type=raw,serial=ci-data` |
| `root.qcow2` | A whole copy of the released root disk | `image_type=qcow2` |

1. Only `JobSeed::new` makes a seed, and it takes an `EphemeralRunner`
   (the origin, the runner name, the labels and the `Registration`), a
   `JobHandle` and a `GuestNetwork`.
2. `GuestNetwork` holds a `GuestMac`, a `GuestAddress` and a DNS server.
   A `GuestMac` is unicast and locally administered. Only
   `BridgeNet::guest` makes a `GuestAddress`: a host of the /24 of the
   gateway, and not the gateway.
3. The seed holds `meta-data`, `network-config` and `user-data`. Each is
   JSON, which cloud-init reads as YAML. `user-data` starts with
   `#cloud-config` and writes `/etc/forgejo-runner/runner.json` (owner
   `runner`, mode 0600) and `/etc/forgejo-runner/job.env` (owner root,
   mode 0644).
4. `runner.json` has the fields of the `Registration` type of
   `forgejo-runner` v13: `id`, `uuid`, `name`, `token`, `address` and
   `labels`. Each label gets the scheme `host`, for example
   `ubuntu-24.04:host`. A label with no scheme gets `docker`.
5. The runner token goes into `user-data` only. No path, no log line and
   no error holds it. A path names the job handle.
6. The root copy checks that the released disk is a regular file with the
   qcow2 magic. It tries a reflink (`FICLONE`) first. Where the file
   system refuses one, it copies each data range of `SEEK_DATA` and
   `SEEK_HOLE`. The host has no `qemu-img`, so no overlay is possible.
7. `JobDisks` owns the folder. Its drop removes the folder, and
   `JobDisks::remove` removes it and reports a fault. A step that fails
   removes the folder. A folder that exists already is a fault.

## Tests

- `tests/fixtures/forgejo-17.0.1/` holds the replies recorded from
  Forgejo 17.0.1 on 2026-11-15. Two edits keep the rule of section 6 of
  `docs/writing-standard.md`. The Forgejo address in `html_url` and `url`
  is `192.0.2.10:3000`. The organization in `html_url` is `example-org`.
- `src/reply.rs` reads each recorded reply with no socket.
- `tests/forgejo_client.rs` serves each recorded reply from a stand-in
  Forgejo on 127.0.0.1. Each test checks the request that the stand-in
  got and the typed result.
- `src/seed.rs`, `src/vfat.rs` and `src/disks.rs` write the disks of a
  fake registration in the temp dir and read them back with `fatfs`.
- `examples/job-disks.rs` writes the disks of a job for the proof of the
  image. The job `ci-runner-image` of the gate boots the image with them
  (`ci-runner-image/AGENTS.md`, "The proof").

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
- The VFAT seed is not checked on the host. The proof boots it on a
  GitHub runner. The reflink path is not checked in CI, because the disk
  of a GitHub runner is ext4.
- `fatfs` 0.3.6 is the last release, from 2023-01-17. Its repository
  has later commits, but no later release.
