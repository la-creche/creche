# disks

The disks row: the signed files that the vmm boots. The root `AGENTS.md`
applies here too.

## The row

`component.toml` is the manifest of the `disks` row, `kind: disk-image`
(decisions CH7-2, CH35 (4), CH76 rule 5). One row holds every file:

| File | What it is |
|---|---|
| `base.ext4.zst`, `python.ext4.zst`, `coding.ext4.zst` | The root disk of each flavor of `catalog.DISK_FLAVORS`, an ext4 file compressed with zstd. |
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
   rule 6). Its `--env-file` is the one of `creche-vmm.service`. One set
   may hold `vmm` and `disks`: `vmm` switches first, so the hook runs from
   the vmm tree that the same set installs (CH78 rule 1).
4. The flavors are the values of the wire enum `SandboxFlavor`: base,
   python and coding. Coding joined with the PR that added it to the enum,
   before the cut (CH76 rule 7, CH79 rule 1). It has no sbx image (CH53
   rule 1).
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
3. It writes `SHA256SUMS`, the digest of each of the five files as it
   ships. The job hands it to the `sign` job.
4. A step that fails before the last one leaves `DISK_OUT` empty.

After the build, the job runs `bin/check-disk-files.sh`, before the `sign`
job signs any file (decision CH78 rule 3). The script reads the row from
`handover disk-assets`, so it holds no copy of a limit. It refuses a file
past its limit (`artifacts.asset_limits`): 2 GiB - 1 for a disk and the
kernel, 64 KiB for `digests.json`. It also refuses a file that is missing
or a link, and an entry of `DISK_OUT` that is no file of the row. A refusal
fails the job, so the `sign` job does not run and no such file gets a
public Sigstore record. Each file is 415 to 506 MiB today (fact 51), and the coding disk is
589,241,742 bytes (562 MiB, gate run 42493246555). The
`sign` job makes the bundles, so the tag script checks their limit.
`bin/tests/test_check_disk_files.py` holds the script.

`rust/crates/wire/vectors/digests.json` holds one good `digests.json`. The
vmm parses it (`rust/crates/vmm/src/image_root/tests.rs`), and
`bin/tests/test_playpen_release_disks.py` holds the output of the script
to its keys, its file names and its byte form. That test runs the script
against stand-ins for `docker`, `curl` and `sha256sum`. The
`playpen-image` job builds each disk for real on each PR.

The verify timeout of the row, 300 s, equals `follow::HOOK_TIMEOUT` of
the vmm (decision CH77 rule 2). The vmm imports new bytes of
`digests.json` only after they stay the same that long plus one poll, so
no import runs while `vmm check-images` runs.
`bin/tests/test_disks_manifest.py` holds the two equal.

## Not checked

1. The time of `vmm check-images` on the host: it decompresses about
   6 GB with the 3559 MiB coding disk, and the hook has 300 s (plan risk
   4 of phase 10).
2. The download rate from the host to github.com. The follower must fetch
   each file of 415 to 562 MiB in one run of 270 s.
