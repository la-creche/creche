# disks

The disks row: the signed files that the vmm boots. The root `AGENTS.md`
applies here too.

## The row

`component.toml` is the manifest of the `disks` row, `kind: disk-image`
(decisions CH7-2, CH35 (4), CH76 rule 5). One row holds every file:

| File | What it is |
|---|---|
| `base.ext4.zst`, `python.ext4.zst` | The root disk of each flavor of `catalog.DISK_FLAVORS`, an ext4 file compressed with zstd. |
| `vmlinux-x86_64.zst` | The pinned guest kernel, compressed with zstd (CH7-3). |
| `digests.json` | The SHA-256 of each other file after decompression (CH41 rule 1). |

1. CI builds each file and signs it as its own asset. Root checks each
   signature, then installs each file unchanged at
   `/opt/components/disks`, root-owned and 0644. Root runs no build and
   decompresses nothing (`handover/AGENTS.md`, Disk images).
2. The vmm reads `digests.json` from `VMM_IMAGE_ROOT`, which is this tree
   by default (CH57 rule 1). It trusts the file because root owns it.
   It decompresses each file into its own digest store (fact 56).
3. The row has no unit and runs as no account. Its verify hook is
   `vmm check-images` of the vmm tree, so `depends_on` names `vmm` (CH76
   rule 6). Its `--env-file` is the one of `creche-vmm.service`.
4. The flavors are the values of the wire enum `SandboxFlavor`, today base
   and python. Coding joins at the cut, with the PR that adds it to the
   enum (CH76 rule 7).
5. The catalog row bundles `playpen` and each path that `playpen`
   bundles: a change that moves the playpen tag moves the disks tag too.
   A change under `disks/` moves the disks tag alone.

## The build

`bin/playpen-release-disks` is the one `build` argv of the manifest. Only
the job `disks` of `release.yml` runs it, from the repository root, with
`DISK_OUT` set, when the plan tags `disks`.

1. For each flavor it runs `playpen/bin/playpen-rootfs` and
   `playpen/bin/playpen-disk`. Then it runs `playpen/bin/playpen-kernel`.
2. It writes `digests.json` from the digest line of each of those
   scripts: `kernel` and one key for each flavor, each `{file, sha256}`,
   keys sorted, one line (CH58 rule 4).
3. It writes `SHA256SUMS`, the digest of each of the four files as it
   ships. The job hands it to the `sign` job.
4. A step that fails before the last one leaves `DISK_OUT` empty.

`rust/crates/wire/vectors/digests.json` holds one good `digests.json`. The
vmm parses it (`rust/crates/vmm/src/image_root/tests.rs`), and
`bin/tests/test_playpen_release_disks.py` holds the output of the script
to its keys, its file names and its byte form. That test runs the script
against stand-ins for `docker`, `curl` and `sha256sum`. The
`playpen-image` job builds each disk for real on each PR.

## Known gaps

1. CONTRACT-QUESTION: no row says whether CI refuses a disk file past its
   limit before the `sign` job signs it, as `bin/build-artifacts.sh` does
   for an archive. The reading taken: the `disks` job checks no limit. The
   tag script refuses such a file before it makes any tag, so no Release
   holds one, but the file still gets its public Sigstore record. Each file
   is 415 to 506 MiB today (fact 51), and the limit is 2 GiB - 1.

## Not checked

1. The time of `vmm check-images` on the host: it decompresses about
   2.5 GB, and the hook has 300 s.
2. The download rate from the host to github.com. The follower must fetch
   each file of 415 to 506 MiB in one run of 270 s.
