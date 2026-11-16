# ci-runner-image

The guest image of the CI runner VMs: one Ubuntu 24.04 VM for each Forgejo
Actions job (`docs/design/ci-runner.md`, section 4.3). The root
`AGENTS.md` applies here too.

This directory holds the build and its proof. No release row ships the
image yet (design section 8, PR 3), and no daemon boots it yet (PR 4 to
PR 6).

## The files

| File | What it is |
|---|---|
| `bin/ci-runner-image-build` | The build of the image. It runs in CI only, with the sudo of the runner. |
| `bin/ci-runner-image-proof` | The proof. It runs the build, builds CH and boots the image. The job `ci-runner-image` of `gate.yml` runs it. |
| `proof/guest-proof` | The guest side of the proof. The proof adds it to the seed as `vendor-data`. It is no file of the image. |
| `root/` | The files that the build installs into the image, at the same path. |
| `fstab` | The lines that the build appends to `/etc/fstab` of the image. |

## The build

1. The input is the Ubuntu 24.04 cloud image of a dated release, never
   `current`. The script pins its URL and its SHA-256 from the file
   `SHA256SUMS` of that release. It pins `forgejo-runner` v13.2.1 and Node
   by URL and SHA-256 too. A download with another SHA-256 stops the
   build.
2. The script converts the image to a raw file. It grows the file by
   2 GiB, and grows the root partition and its file system into that
   space. The root of the cloud image has too little free space for the
   packages. It attaches the file to a loop device. It mounts the root and `/boot` of the image and installs
   the packages in a chroot. It then converts the raw file back to qcow2.
   No VM boots during the build, so the build runs beside the CH build.
3. apt reads the Ubuntu snapshot `APT_SNAPSHOT`. A rebuild then gets the
   same package versions. To take newer packages, change the snapshot.
4. The build adds Docker Engine (`docker.io`), containerd, the buildx
   plugin, git, Node in `/usr/local` and `forgejo-runner` in
   `/usr/local/bin`.
5. The build purges snapd. It masks `apt-daily.timer` and
   `apt-daily-upgrade.timer`. On a fresh image both timers start at the
   first boot, and an apt run then holds the lock that a job needs.
6. The job user is `runner`, in the groups `docker` and `kvm`. It uses
   sudo with no password. The VM is the boundary.
7. The output is `root.qcow2`, `vmlinuz`, `initrd.img` and `SHA256SUMS`.
   The kernel and the initrd come from `/boot` of the image, so they match
   its modules. The image must hold exactly one kernel.

## The data disk

The data disk holds the storage of Docker and containerd and the job
workspaces. The root of the image is 5.5 GB, and a Docker build fills it
(design section 4.3, rule 7).

1. The host gives the data disk the virtio serial `ci-data`. The guest
   finds it as `/dev/disk/by-id/virtio-ci-data`, in any disk order.
2. systemd formats the disk as ext4 at the first boot
   (`x-systemd.makefs`), and mounts it at `/var/lib/ci-data`.
3. `ci-data-dirs.service` makes `docker`, `containerd` and `work` there.
   Two bind mounts then put `/var/lib/docker` and `/var/lib/containerd` on
   the disk.
4. A drop-in gives `docker.service` and `containerd.service` a
   `RequiresMountsFor=` line. With no data disk, neither daemon starts.

## The job unit

`forgejo-runner-job.service` runs `forgejo-runner one-job --handle` and
then powers the VM off, after a success or a failure. The seed of each job
writes two files:

1. `/etc/forgejo-runner/runner.json`: the registration of the ephemeral
   runner. Owner `runner`, mode 0600.
2. `/etc/forgejo-runner/job.env`: `JOB_HANDLE=<handle>`.

With either file absent, the conditions of the unit keep it stopped. The
crate `creche-ci-runner` writes the seed (`rust/crates/ci-runner/AGENTS.md`,
"The disks of a job"). PR 6 of the design runs a job through the unit.

## The proof

`bin/ci-runner-image-proof` boots the image as a job VM boots on the
host. It uses the CH of the vmm row (`bin/lib/chbuild.sh`) and the three
disks of a job. The example `job-disks` of the crate `creche-ci-runner`
writes them: a whole copy of the root disk, a sparse data disk and a VFAT
seed with the label CIDATA. The VM gets a tap device with NAT on the
runner. The guest prints its results as `CI-PROOF` lines on the serial
console, and then powers off.

1. The registration of the seed is fake: a random runner uuid and a
   random token. No Forgejo listens at its address.
2. The proof adds `proof/guest-proof` to the seed as the `runcmd` of a
   cloud-init `vendor-data` file. The files that the crate wrote do not
   change.
3. The kernel command line of the proof adds
   `systemd.mask=forgejo-runner-job.service`. The fake registration then
   runs no job.

The proof passes only when each of these holds:

1. `systemctl is-system-running` prints `running`. `degraded` fails the
   proof, and the guest prints each failed unit first.
2. `sudo -n true` as `runner` exits 0.
3. `docker run hello-world` as `runner` prints its greeting.
4. `forgejo-runner --version` prints v13.2.1.
5. `/var/lib/docker`, `/var/lib/containerd` and the work directory sit
   on the data disk.
6. The NIC with the MAC of the seed has the address of the seed, and the
   default route goes through the gateway of the seed. The instance id is
   the one of the job.
7. `/etc/forgejo-runner/runner.json` has the owner `runner`, the mode
   0600, and the keys `address`, `id`, `labels`, `name`, `token` and
   `uuid`. Each value but the token is the value of the seed. The guest
   prints only whether the token has the shape of a token.
8. `/etc/forgejo-runner/job.env` holds `JOB_HANDLE=<handle of the job>`.
9. The job unit is masked, its two conditions pass, and
   `systemd-analyze verify` prints no line for it.
10. CH exits 0 after the power-off, within 300 s.
11. No line of the serial log and no output of `job-disks` holds the
    token.
12. `job-disks` removes the run folder when the proof closes its stdin.

## When the job runs

The scope answer `ci_runner_image` is `false` only when a change touches
no file under `ci-runner-image/` or `rust/crates/ci-runner/`, and not
`bin/lib/chbuild.sh`. The job then skips its step and passes. `bin/tests/test_gate_workflow.py` holds
the job and the answer.

## Not checked

1. The image on the host. The proof boots it on a GitHub runner, with a
   tap device and NAT, not with the bridge and the fence of design section
   4.5.
2. A nested `kvm` slot. The proof starts CH with no `nested=on`.
3. A real job through `forgejo-runner-job.service` (design PR 6). The
   proof masks the unit.
4. The reflink of the root copy. The disk of a GitHub runner is ext4, so
   the copy there is a sparse copy.
