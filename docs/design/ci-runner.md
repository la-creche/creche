# CI runner VMs on the host (`ci-runner`): design

> Status as of 2026-11-15: Decided. PR 2 of section 8 builds the guest image, and its proof runs in the gate. The operator decided R1 to R6 on 2026-11-15, all as recommended. The spike on the host is complete, and every step passed on a tap network. Questions 1 and 2 of section 7 take their defaults. `ci-runner-image/AGENTS.md` has the build. Next: PR 3 of section 8.
> Host names, addresses and accounts are placeholders. The operator's private notes hold the real values.

Source: the operator's private plan `reframe/forgejo-runner-vm-plan.md` (sections 2, 4 and 7), and the spike scripts `reframe/forgejo-runner/net-up.sh` and `net-down.sh`. Section 7 of the plan (the spike results) replaces section 3 of the plan (the first design) where the two differ. Research date: 2026-11-15. File evidence was checked against creche `origin/main` at `6c1009d5`.

## 1. Goal and non-goals

The la-creche repositories move from GitHub to a self-hosted Forgejo 17.0.1. Forgejo Actions needs runners. This note designs them.

Goal: one throwaway Cloud Hypervisor (CH) VM per Forgejo Actions job, on the host. Inside the VM a job gets:

1. Docker. The Panda `container-release` job and the creche base disk build need it.
2. systemd as PID 1 and sudo with no password. `bin/systemd-proof.sh` and `bin/vm-smoke-setup.sh` need them.
3. `/dev/kvm`, on jobs with the `kvm` label only. The creche `kvm` jobs need it.
4. Network to the Forgejo host and to the public internet.

The VM is the security boundary. Nothing of a job survives its VM. This meets CH36 G20 (`cloud-hypervisor-playpen.md`, line 114): a job uses sudo only inside a throwaway runner. No release and no step that recurs needs sudo on the host.

Non-goals:

1. Windows and macOS jobs (Panda `desktop.yml`). They stay on GitHub.
2. A change to the `vmm` component or to the playpen confinement. The playpen stays as it is.
3. A warm pool with snapshot restore. The first version boots each VM cold (section 7).

## 2. Why `vmm` is not reused

The `vmm` daemon owns the playpen VMs. Each of its fences exists to keep an agent away from the network. A runner VM needs each of the things that the fences refuse.

| Need | vmm today | Evidence (creche at `6c1009d5`) |
|---|---|---|
| A network card | None. The deny check fails a VM when any CH argument starts with `--net` | `rust/crates/vmm-confine/src/deny.rs:42` (the `--net` prefix), `:57-58` (the `Net` fault), `:124-125` (the check). Decision CH8-1 |
| TCP from the CH process | Landlock denies TCP bind and connect | `rust/crates/vmm-confine/src/landlock.rs:9-11` |
| A tap device | The AppArmor slot profile allows `/dev/kvm`, not `/dev/net/tun` | `rust/crates/vmm-confine/apparmor/cloud-hypervisor-slots.profile:27-32` (slot 0. Each slot repeats the rules) |
| Reach the Forgejo host | The egress proxy refuses IP literals and private addresses | `rust/crates/vmm/src/egress/rule.rs:5-6` (IP literal), `:158-164` (the private ranges) |
| Docker and KVM in the guest | The guest kernel is a downloaded CH build. No kernel config is in the repo | `playpen/bin/playpen-kernel:33-34` |
| systemd as PID 1 | PID 1 is `guest-init`. It runs one program, the facade | `rust/crates/guest-init/AGENTS.md:4` (PID 1), `:17-24` (the boot plan), `:60-66` (the one program) |
| Per-job data | None. The kernel command line is fixed, and the env is baked into the root disk | `rust/crates/vmm-confine/src/command.rs:52` |
| Run a job and get its status | No route. The host API serves ACP sessions per family | `rust/crates/wire/src/routes.rs:601-608` |

Each row would need an exception in the vmm. Eight exceptions to the playpen fences cost more than a second, small component.

The `ci-runner` component reuses three things:

1. The pinned `cloud-hypervisor` build: the same git revision and the same build argv as the `vmm` row of `rust/component.toml`.
2. The release path: a `component.toml` row, the handover install into `/opt/components/<name>`, and a unit with group `kvm` and no sudo.
3. The disk build pattern of `disks/`: CI builds the image, and root installs the signed file unchanged.

## 3. Decisions

Each row records the decision, the operator's words and the date. The plan file numbered the rows R1 to R7. R7 approved the spike and is done, so this note does not repeat it.

| # | Topic | Decision | The operator's words | Date |
| - | ----- | -------- | -------------------- | ---- |
| R1 | Component | `ci-runner` is a new component, apart from `vmm`. The playpen confinement does not change. Every vmm fence of section 2 would need an exception otherwise. | "as recommended" | 2026-11-15 |
| R2 | Network | The rule as decided: `passt` (option N2) if spike step S3 passes, else a bridge and tap devices (option N1). S3 failed for `passt` on the host (section 5, fact 8). So the network is N1: root makes one bridge and one tap device per slot at install. The nftables rules on the bridge are the fence. | "as recommended" | 2026-11-15 |
| R3 | Guest image | The guest boots an Ubuntu 24.04 cloud image with its stock kernel, not a playpen flavor. The stock kernel has KVM, overlayfs and nftables. | "as recommended" | 2026-11-15 |
| R4 | Nested KVM | Only a slot of the `kvm` label starts CH with `--cpus nested=on`. A plain slot starts CH with `nested=off`. A plain job never lands on a `kvm` slot. Nested KVM widens the attack surface of the host kernel. | "as recommended" | 2026-11-15 |
| R5 | Registration credential | A bot user `runner-bot` owns the organization. Its token has the `organization` scope only. The token stays on the host and never enters a VM. | "as recommended" | 2026-11-15 |
| R6 | Capacity | 4 plain slots of 4 vCPU and 8 GB. 2 `kvm` slots of 8 vCPU and 16 GB. Measure, then adjust. | "as recommended" | 2026-11-15 |

R5 note: only an organization owner can register an organization runner. An owner token with the `repository` scope could change branch protection. So the token gets the `organization` scope only. The residual risk: an owner can change teams.

## 4. Design

### 4.1 The shape

```
Forgejo host                         the host
+-----------------+   HTTPS   +--------------------------------------------+
| Forgejo 17.0.1  |<----------| ci-runner daemon (user ci-runner)          |
|  jobs list      |           |   1. list waiting jobs                     |
|  runner registry|           |   2. register one ephemeral runner per job |
+-----------------+           |   3. write per-job disks, start CH         |
        ^                     +------------------+-------------------------+
        |                                        | one CH process per job
        |                      +-----------------v-------------------------+
        |   tap + bridge       | VM: Ubuntu 24.04, systemd PID 1           |
        +----------------------|  forgejo-runner one-job --handle <handle> |
          (fence: nftables)    |  Docker, Node, sudo, /dev/kvm on kvm only |
                               +-------------------------------------------+
```

### 4.2 The daemon and the job loop

Two facts from spike step S5 set the shape:

1. `forgejo-runner daemon` refuses an ephemeral runner. Its error: "requires an ephemeral runner, which is not supported in daemon-mode". The command for an ephemeral runner is `forgejo-runner one-job`.
2. `one-job` does not wait for work. It asks Forgejo once. With no waiting job it exits at once with "no task received".

So the host daemon must see a waiting job first, and then start a VM for it. The first design kept N warm slots. This design starts one VM per waiting job, up to the capacity of R6. No idle VM holds a token.

The loop of the daemon:

1. The daemon lists the waiting jobs of the organization: `GET /orgs/{org}/actions/runners/jobs`. Each job has a `handle`.
2. The daemon matches each job to a slot kind by its labels. `ubuntu-24.04` and `ubuntu-latest` go to a plain slot. `kvm` goes to a `kvm` slot. The daemon ignores a job with no matching label, and a job whose handle it already started.
3. For each matched job with a free slot of its kind, the daemon registers one ephemeral runner: `POST /orgs/{org}/actions/runners` with `ephemeral: true` and the labels of the slot kind. Forgejo returns a runner uuid and a runner token.
4. The daemon writes the per-job disks (section 4.4) and starts CH (section 4.6).
5. In the guest, a systemd unit runs `forgejo-runner one-job --handle <handle>`. `--handle` asks for that one job (Forgejo 15 or later). The unit powers the guest off when the runner exits.
6. When the CH process exits, the daemon deletes the run folder of the job. Forgejo removes the ephemeral runner after its one job, so the runner token is dead.

Rules of the loop:

1. A VM that runs past a wall-clock cap gets SIGKILL from the daemon. The first value of the cap is open (PR 6 of section 8).
2. A job can go to another runner before its VM asks for it. Then `one-job` exits with "no task received", and the VM powers off. The daemon then removes the runner that it registered, if Forgejo still lists it. The delete endpoint is not checked on 17.0.1.
3. The daemon reads every reply of the Forgejo API as untrusted input. It validates the shape and the size before use (root `AGENTS.md`, security rules).
4. The poll interval is a config value. The first value is open. A job starts within 1 s of `one-job` (section 5), so the poll interval and the 17 s boot set the wait of a job.

### 4.3 The guest image

The image is the Ubuntu 24.04 cloud image (`noble-server-cloudimg-amd64`), checked against its published `SHA256SUMS`. The image build adds:

1. Docker Engine (`docker.io`) and containerd. The spike measured Docker 29.1.3 with the overlayfs storage driver and cgroup v2.
2. git.
3. Node, for JavaScript actions such as `actions/checkout`. Host mode gives a job no Node of its own.
4. `forgejo-runner` v13.2.1.
5. A job user with sudo and no password.
6. The systemd unit that runs `one-job` and then powers off.
7. Mounts that put `/var/lib/docker` and `/var/lib/containerd` on the data disk.

Rule 7 comes from the spike. The first Panda build failed with "no space left on device" under `/var/lib/containerd` on the 3.5 GB root.

CH v54 boots the image by direct kernel boot. The kernel is the image's own `vmlinuz` (6.8.0-147) with its initrd. The command line is `root=LABEL=cloudimg-rootfs ro console=ttyS0`. The root disk is qcow2. CH v54 requires `image_type` on each `--disk`.

### 4.4 The per-job disks

Each job gets three disks in its own run folder:

1. **The root disk.** A copy of the qcow2 image, 1.4 GB in the spike. The host has no `qemu-img`, so it cannot make a qcow2 overlay. The spike copied the whole image.
2. **The data disk.** A sparse raw file of 40 GB. It holds `/var/lib/docker`, `/var/lib/containerd` and the workspace. The spike used 1.7 GB of it for the Panda build. The spike record does not say which side formatted it. This design formats it in the guest at first boot, with `x-systemd.makefs` in `/etc/fstab` of the image. The host gives the disk the virtio serial `ci-data`. Docker and containerd then start only after the mounts, because the mounts are part of `local-fs.target`. cloud-init `fs_setup` runs later, in `cloud-init.service`, and containerd has no order after that unit.
3. **The config disk.** A cloud-init NoCloud seed with the label `cidata`. It holds the runner uuid and token, the job handle, the labels, the Forgejo URL, and a `network-config` that matches the MAC of the slot and sets the static address of the slot.

The spike built its seed as an ISO on another machine, because the host has no ISO tool. NoCloud also reads a VFAT filesystem with the label `cidata`. The daemon writes the seed as a small VFAT image itself, so the host needs no new tool. This is a design choice. The spike did not test a VFAT seed.

### 4.5 The network

Root sets up the network once at install. The rules follow `net-up.sh` of the spike, which is the first draft of the fence.

1. One bridge, `cirun0`, with the gateway address of a private /24 (`<bridge-net>`).
2. One tap device per slot, `cirun-tap<n>`, owned by the user `ci-runner`. Six taps for R6. CH opens a tap that the user owns with no root.
3. Each tap is an isolated bridge port (`bridge link set dev cirun-tap<n> isolated on`). Two VMs then cannot reach each other over the bridge. The spike had one tap and did not test this rule.
4. An nftables table on the forward and input hooks:

   | Chain | From the bridge | Verdict |
   |---|---|---|
   | forward | TCP to the Forgejo host's HTTP port (`<forgejo-addr>:<forgejo-port>`) | accept |
   | forward | TCP and UDP port 53 to the LAN DNS server (`<dns-addr>`) | accept |
   | forward | to 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16, 100.64.0.0/10, 169.254.0.0/16 and 127.0.0.0/8 | drop |
   | forward | any IPv6 | drop |
   | input | established and related replies | accept |
   | input | anything else | drop |
   | postrouting (nat) | source in `<bridge-net>`, out of any other interface | masquerade |

5. Docker on the host sets the iptables FORWARD policy to drop. An nftables accept in one base chain does not stop a drop in another. So root also adds two rules to the `DOCKER-USER` chain: accept from `cirun0`, and accept established and related traffic to `cirun0`. A restart of Docker on the host may remove these rules. This is not checked.

The input chain is what keeps a VM off every service of the host. The forward drops keep a VM off the LAN, the Docker bridges and the shared address range (100.64.0.0/10).

A guest could send a packet with a source address outside `<bridge-net>`. The masquerade rule does not match it. This design adds one forward rule: drop from the bridge when the source is not in `<bridge-net>`. The spike did not test this rule.

`passt` (option N2) is not used. Section 5, fact 8 has the failure.

### 4.6 The VM start

The daemon starts each CH process with:

1. `--cpus boot=4,nested=off` and `--memory size=8G` on a plain slot. `--cpus boot=8,nested=on` and `--memory size=16G` on a `kvm` slot (R4, R6).
2. `--kernel` and `--initramfs` from the image tree, and the command line of section 4.3.
3. Three `--disk` arguments, each with `image_type`.
4. `--net tap=cirun-tap<n>,mac=<slot MAC>`.
5. `--serial file=<run folder>/serial.log` and `--console off`.

The `cloud-hypervisor` program comes from the `ci-runner` tree, built from the same pinned revision as the `vmm` row. The AppArmor slot profiles of the vmm live in `/etc/apparmor.d/opt.components.vmm.bin.cloud-hypervisor`. The spike ran its own copy of CH, so that it never ran under them. The `ci-runner` tree gives the same separation.

### 4.7 The host user

The system user `ci-runner` exists on the host, in the groups `ci-runner` and `kvm`, with its home in `/var/lib/ci-runner`. The operator made it with one `sudo useradd`, as for the `mcp-<name>` users. The daemon and every CH process run as `ci-runner`. Group `kvm` gives the CH processes `/dev/kvm`.

### 4.8 The systemd unit and the limits

`creche-ci-runner.service` runs the daemon as `ci-runner`. Each CH process is a child of the daemon, in the cgroup of the unit. So one set of limits bounds every job at once, and jobs cannot starve the playpens.

First values, from R6:

1. `MemoryMax=`: the guest RAM of all slots, 4 × 8 GB + 2 × 16 GB = 64 GB, plus a margin for CH. The spike measured 4.0 GB resident for an 8 GB VM after a Docker build, so 64 GB is a cap, not the normal use. The host has 123 GB of RAM.
2. `CPUQuota=`: the vCPUs of all slots, 4 × 4 + 2 × 8 = 32, so 3200%. The host has 72 threads.
3. `LimitCORE=0`, `NoNewPrivileges=yes`, `UMask=0077`, as `creche-vmm.service` has.
4. `KillMode=mixed` and an `ExecStopPost=` step that kills each CH process left over, as `creche-vmm.service` has. A stop of the unit ends every job.

The run folders sit under the state dir of the unit. The registration token is a file that only `ci-runner` reads, outside each run folder.

### 4.9 What needs root, and what does not (rule G20)

Root does these steps once, by hand, at install. A change to one of them is a one-off sudo step, as a change to the AppArmor slot profile is.

1. The user `ci-runner` (done).
2. A system oneshot unit that makes the bridge, the taps and the nftables table, and adds the `DOCKER-USER` rules. It runs at each boot, because none of it survives a reboot.
3. The install of `creche-ci-runner.service`.
4. The token file of `runner-bot`.

A release needs no sudo. Handover installs the `ci-runner` tree and the image tree, refreshes the unit file and restarts the unit.

Each job needs no root on the host. The daemon, as `ci-runner`:

1. Calls the Forgejo API.
2. Writes the three disks into the run folder.
3. Starts CH on a tap that `ci-runner` owns.
4. Deletes the run folder.

Inside the VM the job has root through sudo. The VM is thrown away after the job.

## 5. Measured numbers

Each number comes from the spike of 2026-11-15 on the host, as the operator's user, with no sudo but the network step. Section 7 of the plan holds the record.

| # | Fact | Value | Source |
|---|---|---|---|
| 1 | Boot to the first cloud-init command, with a network card, first boot of a fresh image copy | 17.1 s | Plan section 7, S1, console `serial6.log` |
| 2 | The same, with no network card and `systemd-networkd-wait-online` masked | 10.4 s in one run, 44.4 s in the next. The spread is not explained. Host load was 5 to 60 | Plan section 7, S1, `serial2.log` to `serial4.log` |
| 3 | `one-job` with a waiting job: time to take the job | within 1 s. It exited 0 after the job | Plan section 7, S5 |
| 4 | Panda `docker build` (`ci.yml` job `container-release`) in a 4 vCPU, 8 GB VM | 81 s | Plan section 7, S4 |
| 5 | Cost of one VM (4 vCPU, 8 GB, nested on) after the Docker build | 4.0 GB resident on the host, 1.4 GB root copy, 1.7 GB used of the 40 GB data disk | Plan section 7, S6, `ps` of the CH process and `du` of the run folder |
| 6 | Power-off to the exit of the CH process | 3 s | The spike status file. Plan section 7, S6 points to it |
| 7 | Nested KVM with `--cpus nested=on` | Pass. The guest has `/dev/kvm` and the `vmx` CPU flag. In the guest, the same CH binary booted the same kernel. The nested kernel reached "Run /init as init process" at 1.56 s | Plan section 7, S2 |
| 8 | `passt` on the host (option N2) | Fail. It binds its socket, then logs "Failed to add fd to epoll: Bad file descriptor" on each client and never answers. CH hangs at "Creating virtio-net device". The cause is not found. It needs root to trace | Plan section 7, S3 |
| 9 | The fence of `net-up.sh` | Every private target tried is refused: a LAN service, the Forgejo SSH port, the router, the host's SSH port, the bridge gateway, the Docker bridge gateway, an address in 100.64.0.0/10, and all IPv6. The Forgejo HTTP port, DNS and public HTTPS answer | Plan section 7, S3 |
| 10 | Docker in the guest | `docker run hello-world` works in 2 s. `apt-get install docker.io git` takes 37 s, which the image build removes | Plan section 7, S4 |

## 6. Security

**The VM is the boundary.** A job runs as root in its guest, with its own kernel. To reach the host, an attacker needs a CH escape or a KVM bug. The VM, its disks and its runner token end with the job.

**The fence.** The nftables table of section 4.5 refuses each packet from the bridge to a private range, to IPv6 and to any service of the host. It allows the Forgejo HTTP port, DNS on the LAN DNS server and the public internet. The rules match on the bridge interface, so they cover every process of every guest. The isolated bridge ports refuse traffic between two VMs.

**No shared Docker daemon.** Each job has its own Docker daemon inside its own VM. Access to a Docker daemon is root on the machine of that daemon. A shared daemon would let one job read, change or replace the containers, images and build cache of another job. With one daemon per VM, a poisoned image cache dies with the VM.

**Nested KVM on the `kvm` label only.** A guest with nested virtualization drives the nested code of the host's KVM module. That code is a large attack surface in the host kernel. Plain jobs, which are most jobs, never get it. Any repository of the organization can ask for the `kvm` label. Section 7, question 6 asks whether the `kvm` runners register on the creche repository alone.

**The registration credential.** The `runner-bot` token has the `organization` scope only. It sits on the host in a file that only `ci-runner` reads. It never enters a VM, a seed or a log line. Each VM gets only its own runner token. That token is valid for one job, and Forgejo removes its runner after the job.

**What a compromised job can reach:**

1. The public internet, inside the CPU and memory caps of the unit.
2. The Forgejo HTTP port, with the job token of its own run and the secrets that its workflow gives it.
3. The LAN DNS server, on port 53.
4. Its own guest, as root.

**What a compromised job cannot reach:**

1. Any service of the host. The input chain drops it.
2. Any other LAN address, the Docker bridges, 100.64.0.0/10 and IPv6.
3. Another job's VM, disks or Docker daemon.
4. The `runner-bot` token.
5. Anything after its VM stops.

An attacker with code execution as `ci-runner` on the host gets the `runner-bot` token. With it the attacker can register runners and change organization teams. The attacker cannot change branch protection or repository content with that token.

## 7. Open questions for the operator

Before PR 2:

1. **The daemon's language and layout.** Default: a Rust crate in creche, `rust/crates/ci-runner`, released like `vmm`.
2. **The guest image build and release.** Default: a CI job builds the image from the pinned cloud image, and a `disk-image` row like `disks` releases it. Each job copies the root disk whole, because the host has no `qemu-img`.
3. **The release path for a component user.** The `vmm` row has `runs_as = "operator"` and a user unit. `ci-runner` runs as its own user. Does contract 06 allow a row that runs as a component user, with a system unit and `User=ci-runner`? Default: yes, the conservative reading, checked in PR 7.

Later:

4. **IPv6.** The fence drops all IPv6. Default: IPv4 only until a job needs IPv6.
5. **A warm pool.** Default: a cold boot per job (17 s). A restored snapshot would need a fresh runner token and handle per job.
6. **The scope of the `kvm` runners.** Default: register them on the creche repository only, so that no other repository gets nested KVM.
7. **The retirement of the existing Docker-in-Docker runner on the Forgejo host.** Default: remove it after the repos move their `runs-on` labels (PR 9).

## 8. Build plan

Each PR holds one idea. Each PR states what proves it.

1. **This note.** Proof: the operator merges it.
2. **The guest image build.** A CI job builds the image of section 4.3. Proof: the `kvm` CI job boots it under CH and runs `systemctl is-system-running`, `sudo -n true` and `docker run hello-world`.
3. **The image release row.** A `disk-image` row in its own `component.toml`, a signed asset and its digest. Proof: the release plan lists the row, and its verify hook checks the digest.
4. **The daemon crate and the Forgejo API client.** Typed requests and replies for the jobs list, the runner registration and the runner delete, parsed at the boundary. Proof: unit tests against replies recorded from Forgejo 17.0.1.
5. **The per-job disks.** The VFAT `cidata` seed writer, the data disk and the root copy. Proof: the `kvm` CI job boots the image with a written seed, and the guest reads its network config and its runner config.
6. **The VM start and the slot accounting.** The CH argv of section 4.6, the capacity of R6, the wall-clock cap and the cleanup. Proof: the `kvm` CI job runs one fake job to its end, and the run folder is gone after.
7. **The component row, the daemon unit and the network unit.** `component.toml` for `ci-runner`, `systemd/creche-ci-runner.service`, and the root-owned network unit and nftables file. Proof: the unit tests pin each decided line, as `bin/tests/test_vmm_unit.py` does for the vmm.
8. **The host step (the operator, sudo, once).** Install the network unit and the daemon unit, and write the token file. Not a PR. Proof: from a VM, every target of section 5, fact 9 is refused again, two VMs cannot reach each other, and a workflow of la-creche/runner-test passes on `ubuntu-24.04` and on `kvm`.
9. **The move of the workflows.** Each repository points `runs-on` at the new labels. Proof: Panda `container-release` and the creche `kvm` jobs pass on Forgejo. Then the Docker-in-Docker runner retires (question 7).
