# C3 slot pool: the Hyperion check (CH51 spike 2)

SPIKE. This kit repeats the CI proof of concept of decision CH51 option 2 on Hyperion's own kernel (6.8.0-142, Ubuntu 24.04, AppArmor 4.0.1). The CI runner runs a 7.0 Azure kernel, so the CI result does not prove the host.

The kit tests two mechanisms side by side, with the same tests, so David can choose:

- **H, hard-link attach.** 64 hard links of CH, one per slot. Each profile attaches by the exec path of its link.
- **X, change_onexec.** One installed CH. The unconfined launch child writes `exec <slot profile>` to `/proc/self/attr/apparmor/exec`, sets no_new_privs, then execs CH. The slot profiles have no attachment path.

X exists because the release path cannot ship 64 hard links. `handover` treewalk.py:190 refuses `st_nlink > 1`, and executor/unpack.py:355 refuses tar hard-link members. An archive also holds at most 64 members (`ARCHIVE_MAX_MEMBERS = 64`). So H needs a handover change under D0.3. X needs no link.

The kit was written on the Mac and has not run on Linux yet. shellcheck is clean, and the Python files compile. The decision paths of `check.sh` and `cleanup.sh` ran on the Mac against stub `aa-exec` and `onexec` answers, for all 128 labels. The verdict helpers of `probes.sh` ran against sample probe lines for H and X. `xlaunch.py` ran on the Mac only: with no exec attr there, it exits 126 and execs nothing. Its prctl call has not run.

## The rules the kit keeps (CH36 G31 and G20)

1. Everything runs as david in one scratch dir, `/home/david/c3-spike`. `cleanup.sh` removes that dir at the end.
2. The kit writes nothing outside that dir. It starts no unit and touches no running service. It never uses `/opt/components` or `/run/user/<uid>/vmm`.
3. The scratch run root is `/home/david/c3-spike/run`. The scratch state store is `/home/david/c3-spike/state/store`.
4. CH v54.0, ch-remote and the guest kernel come from the pins of `bin/vm-smoke-setup.sh` (the job kvm), each checked by its sha256.
5. The 128 spike profiles are named `creche-c3spike-*`. The 64 H profiles `creche-c3spike-slot-<k>` each attach only to `/home/david/c3-spike/bin/ch-slot/<k>/cloud-hypervisor`. The 64 X profiles `creche-c3spike-xslot-<k>` attach to no path. Only an onexec write of the name enters one. So the profiles confine nothing else on the host.
6. David runs exactly two sudo steps: the load and the unload of one file. Agents never run sudo.

## The files

| File | What it does |
| --- | --- |
| `base.profile` | A copy of `rust/crates/vmm-confine/apparmor/cloud-hypervisor.profile` at `origin/vmm/c-kvm-vm` 14e247bf (blob be1c4f4c). |
| `gen-profiles.py` | The generator. It mirrors `slot_profile.rs` of the CI spike, with the scratch paths and the spike name prefixes. It makes the H set and the X set. |
| `c3spike-slots.profile` | The generated file that David loads: 128 profiles (64 H, then 64 X), one ABI line. |
| `lib.sh` | The shared names, the pins and the guard. |
| `setup.sh` | Fetches and checks the files, makes the H links, the run roots `run/` (H) and `runx/` (X) and the store, and compiles the profile file without loading it. |
| `check.sh` | Refuses if one of the 128 profiles is not loaded or not in enforce mode. `probes.sh` runs it first. |
| `probes.sh` | Runs T0 to T6 for H, then for X. Prints one PASS or FAIL line per test with its control. |
| `xlaunch.py` | The X launcher. It writes the exec attr, sets no_new_privs, then execs the program in place. |
| `sockprobe.py` | The connect probe, the idle CH probe, the static ELF check, and `onexec`, which gives the errno of an aa-exec failure. `--onexec <profile>` starts CH through `xlaunch.py`. |
| `cleanup.sh` | Kills leftover spike processes and removes the scratch dir. It refuses while a profile is loaded or the load state is unknown. |

### How the spike profiles differ from the CI profiles

The generator changes the same four kinds of line as `slot_profile.rs`: the header, the binary rule, the signal peer, and the five run dir rules. Three things differ:

1. The name prefix is `creche-c3spike-slot-` (H) or `creche-c3spike-xslot-` (X), not `creche-ch-slot-`.
2. The H link root is `/home/david/c3-spike/bin/ch-slot/`. The run dir rules name `/home/david/c3-spike/run/slot/<k>/` (H) or `/home/david/c3-spike/runx/slot/<k>/` (X) in place of `/run/user/[0-9]*/vmm/vm/*/`. So no H label reaches an X socket, and no X label reaches an H socket.
3. Each slot also has `/home/david/c3-spike/bin/busybox mr,`, the probe rule of `render_probe`. With it, `aa-exec` or `xlaunch.py` can run busybox under a slot label for T5 and T6. The rule adds no socket rule and no signal rule.

An X profile differs from an H profile in two lines:

1. The header has no attachment path: `profile creche-c3spike-xslot-<k> {`.
2. The binary rule names the one installed CH, `/home/david/c3-spike/bin/cloud-hypervisor mr,`. The kernel maps the binary after the label changes, so the new label needs `m` on it. The Rust form of this rule names `/opt/components/vmm/bin/cloud-hypervisor`, which the `Left` check of `slot_profile.rs` rejects today (CI report, point 4).

The kit applies no Landlock ruleset. It tests the AppArmor layer alone.

To make the profile file again on the Mac: `python3 -I gen-profiles.py --slots 64 > c3spike-slots.profile`. `setup.sh` checks that the shipped file equals the generator output.

### What the X run proves, and what it does not

`xlaunch.py` keeps the order of the vmm launch child: write the exec attr, set no_new_privs, exec CH. The pid stays the same, so `$!` and the probe pid are CH after the exec. If the write fails, `xlaunch.py` exits 126 and execs nothing, so CH never starts unconfined in place of a slot label. X-T1 tests that fail-closed path with the name `creche-c3spike-xslot-64`, which no profile has.

The run proves the kernel side of X only. In the vmm, the launch step execs `systemd-run --user --scope`, and `systemd-run` then execs CH. An onexec write in the launch step would put `systemd-run` under the slot label, not CH (CI report, points 1 to 3). X in production needs a launch chain change, which David decides.

X depends on `kernel.apparmor_restrict_unprivileged_unconfined = 0`. With 1, the kernel may refuse the onexec write of an unprivileged unconfined process. X-T1 prints the value. `setup.sh` prints it too.

## The tests

`probes.sh` runs the whole table twice: first for H (names `H-T0` to `H-T6`, run root `run/`), then for X (names `X-T0` to `X-T6`, run root `runx/`). It stops the H VMs before the X VMs boot. Under H, each CH runs at its slot link through `setpriv --no-new-privs`. Under X, each CH runs at `bin/cloud-hypervisor` through `xlaunch.py <label>`.

| Slot | Run dir symlink (H, and the same under `runx/` for X) | Use |
| --- | --- | --- |
| 0 | `run/vm/vm-a` | VM A, booted |
| 1 | `run/vm/vm-b` | VM B, booted, the signal target |
| 2 | `run/vm/vm-p` | the probe CH and its same-slot listeners |
| 3 | `run/vm/vm-q` | the cross-slot listeners |
| 4 | `run/vm/vm-c` | the wrong-slot bind of T2 |
| 63 | `run/vm/vm-z` | the far attach of T6 |

| Test | Proves | Control |
| --- | --- | --- |
| T0 | The probe CH cannot connect to a `vmm.sock` stand-in in the run root (CH20, CH24). | The T3 control. |
| H-T1 | Two hard links of one inode get two labels: VM A runs `creche-c3spike-slot-0 (enforce)`, VM B runs `creche-c3spike-slot-1 (enforce)`. | The base path of the same inode runs `unconfined`. |
| X-T1 | One path gets one label per onexec write: VM A and VM B both run `bin/cloud-hypervisor`, under `creche-c3spike-xslot-0 (enforce)` and `creche-c3spike-xslot-1 (enforce)`. | The same path with no write runs `unconfined`, so no X profile attaches to it. `xlaunch.py` to the unloaded `creche-c3spike-xslot-64` exits 126 with ENOENT and starts no CH. |
| T2 | A CH whose run dir is the `vm/<id>` symlink binds `api.sock` and `vsock.sock` in its slot, boots, and answers `ch-remote ping`. | A slot 0 CH that binds through `vm/vm-c` (slot 4) gets EACCES, and slot 4 stays empty. |
| T3 | A slot 2 CH cannot connect to `slot/3/vsock.sock_1100` (attack 1). | It connects to `slot/2/vsock.sock_1100`, under its own label, and logs no EACCES. |
| T4 | A slot 2 CH cannot connect to `api.sock` or `vsock.sock` of slot 3 (listeners) or of VM A (live, slot 0) (attacks 2 and 3). VM A still pings after. | It connects to `slot/2/api.sock` and `slot/2/vsock.sock` and logs no EACCES. The same probe under slot 0 aims at VM A's two sockets and logs no EACCES. |
| T5 | busybox under the slot 0 label cannot `kill -KILL` VM B (attack 4). H enters the label with `aa-exec`, because no profile attaches to busybox. X enters it with `xlaunch.py`. | busybox under the slot 1 label can, and VM A lives on. |
| T6 | All 128 labels are loaded in enforce mode, and slot 63 gets its label: by its link (H) or by `xlaunch.py` (X). It reports the time of the check and the unprivileged compile time from `setup.sh`. | Unconfined busybox reads `README.md`, which every slot label refuses. |

The connect probe is the one of `tests/kvm_apparmor.rs`: CH gets `--tpm socket=<path>` and connects there while it makes the VM. Every CH runs under no_new_privs (`setpriv` for H, `xlaunch.py` for X), so the label change under no_new_privs is proved too.

A refused case needs `connected=no` and `eacces=yes`. `eacces` is a search for "Permission denied" anywhere in the CH log. So every control also needs `eacces=no` and the label of its slot, and a stray EACCES cannot pass for a refusal. A live VM has no test listener, so `connected=no` proves nothing in the two live cases of T4. Their weight rests on the slot 0 control: the same probe at the same path logs no EACCES under the slot that owns VM A.

## The sequence

Each step names who runs it. "Agent" means an agent or David, as david, with no sudo.

### 1. Copy the kit to Hyperion (agent, on the Mac)

```sh
ssh hyperion 'test ! -e /home/david/c3-spike'
scp -r <this kit dir> hyperion:/home/david/c3-spike
```

Expected: the `ssh` exits 0 with no output. If it exits 1, a scratch dir is left from an earlier run: run step 6 (and step 5 first, if its profiles are loaded). `scp` prints nothing with `-q`, else one line per file.

### 2. Run setup.sh (agent, on Hyperion)

```sh
ssh hyperion /home/david/c3-spike/setup.sh
```

It takes about one minute, most of it the downloads. Expected output, with host values in angle brackets:

```
setup.sh: check the host
setup.sh: kernel 6.8.0-142-generic, Ubuntu 24.04.<n> LTS
setup.sh: apparmor_parser AppArmor parser version 4.0.1
setup.sh: kernel.apparmor_restrict_unprivileged_unconfined = 0
setup.sh: kernel.apparmor_restrict_unprivileged_userns = 1
setup.sh: fetch CH v54.0, ch-remote and the guest kernel
cloud-hypervisor: OK
ch-remote: OK
vmlinux: OK
setup.sh: fetch busybox-static and build the initramfs
Get:1 http://<mirror> noble-updates/universe amd64 busybox-static amd64 <version> [<size>]
setup.sh: busybox busybox-static_<version>_amd64.deb, sha256 <sha>
setup.sh: fill the scratch state store
setup.sh: make 64 hard links of CH
setup.sh: inode <n> has 65 names
setup.sh: make the scratch run roots of H and X
/home/david/c3-spike/run/vm/vm-a -> ../slot/0
... (six lines for run/, then six for runx/, each six in any order)
setup.sh: check the profile file and compile it without loading it
gen-profiles: /home/david/c3-spike/c3spike-slots.profile equals the output for 64 slots
setup.sh: T6 data: compiled 128 profiles in <ms> ms, no load
setup.sh: ready. David runs this now:

    sudo apparmor_parser -r /home/david/c3-spike/c3spike-slots.profile

setup.sh: then run /home/david/c3-spike/probes.sh
```

If `apparmor_restrict_unprivileged_unconfined` is 1, `aa-exec` and `xlaunch.py` may not enter the slot labels. `check.sh` then asks `sockprobe.py onexec` for the errno of each failure. ENOENT means the profile is not loaded. Any other errno is a refusal: `check.sh` prints `aa-exec=no` with the errno and goes on. H-T0 to H-T4 then run. H-T5, H-T6 and every X test FAIL with that cause. That result means X cannot work on that host.

If apparmor_parser refuses `-Q` without root, the T6 line says `compile_ms=none`. The run goes on.

### 3. Load the profiles (David, sudo, one-off G20)

```sh
time sudo apparmor_parser -r /home/david/c3-spike/c3spike-slots.profile
```

Expected: no output from apparmor_parser, exit 0. `time` gives the load time of the 128 profiles for T6. If sudo asks for a password, the time includes your typing. The `time` prefix is optional.

### 4. Run probes.sh (agent, on Hyperion)

```sh
ssh hyperion /home/david/c3-spike/probes.sh
```

It takes about two minutes: one per mechanism. The two live controls of T4 may each wait out the 30 s probe limit, so each mechanism may take up to one more minute. Expected output, with host values in angle brackets (a `...` stands for the same words as in the H line):

```
check.sh: policy dir: 128 creche-c3spike profiles, 128 in enforce mode
check.sh: aa-exec: all 128 labels run busybox true, and each refuses to read README.md
aa-exec=ok
check.sh: all 128 creche-c3spike profiles are loaded in enforce mode
probes.sh: host: kernel 6.8.0-142-generic, AppArmor parser version 4.0.1
probes.sh: mechanism H: hard links, labels creche-c3spike-slot-<k>, run root /home/david/c3-spike/run
probes.sh: H: boot VM A in slot 0 and VM B in slot 1
probes.sh: H: VM A booted=yes pid=<a>, VM B booted=yes pid=<b>
PASS H-T1: one inode <n>, slot 0 is 'creche-c3spike-slot-0 (enforce)', slot 1 is 'creche-c3spike-slot-1 (enforce)' | control: base path runs unconfined
PASS H-T2: VMs A and B boot, bind api.sock and vsock.sock in slot/0 and slot/1 through vm/<id>, and ping | control: slot 0 CH binding in slot/4 refused (bound=no eacces=yes)
PASS H-T3: slot 2 to slot/3/vsock.sock_1100 refused (connected=no exit=1 eacces=yes label=creche-c3spike-slot-2_(enforce)) | control: slot 2 to slot/2/vsock.sock_1100 connected (connected=yes exit=1 eacces=no label=creche-c3spike-slot-2_(enforce))
PASS H-T4: slot 2 refused (connected/eacces): vm-q/api.sock no/yes. vm-q/vsock.sock no/yes. vm-a/api.sock no/yes. vm-a/vsock.sock no/yes.  | control: (connected/eacces) slot 2 vm-p/api.sock yes/no. slot 2 vm-p/vsock.sock yes/no. slot 0 vm-a/api.sock no/no. slot 0 vm-a/vsock.sock no/no.
PASS H-T0: slot 2 to run/vmm.sock refused (connected=no exit=1 eacces=yes label=...) | control: T3 control connected with eacces=no
PASS H-T5: slot 0 kill -KILL of slot 1 CH refused (rc=1, B alive) | control: slot 1 kill -KILL of slot 1 CH worked (rc=0, B dead), VM A alive
PASS H-T6: 128 labels enforce, checked in <ms> ms, compile_ms=<ms> profiles=128, slot 63 attaches (bound=yes exit=running eacces=no label=creche-c3spike-slot-63_(enforce)) | control: unconfined busybox cat README.md: read
probes.sh: mechanism X: change_onexec by xlaunch.py, labels creche-c3spike-xslot-<k>, run root /home/david/c3-spike/runx
probes.sh: X: boot VM A in slot 0 and VM B in slot 1
probes.sh: X: VM A booted=yes pid=<a>, VM B booted=yes pid=<b>
PASS X-T1: one path bin/cloud-hypervisor, slot 0 is 'creche-c3spike-xslot-0 (enforce)', slot 1 is 'creche-c3spike-xslot-1 (enforce)', restrict_unprivileged_unconfined=0 | control: the same path with no write runs unconfined. xlaunch.py to unloaded creche-c3spike-xslot-64: rc=126 'xlaunch: write exec attr: ENOENT', no CH
PASS X-T2: ... (as H-T2)
PASS X-T3: ... label=creche-c3spike-xslot-2_(enforce) ... (as H-T3)
PASS X-T4: ... (as H-T4)
PASS X-T0: slot 2 to runx/vmm.sock refused (connected=no exit=1 eacces=yes label=...) | control: T3 control connected with eacces=no
PASS X-T5: ... (as H-T5)
PASS X-T6: 128 labels enforce, ..., slot 63 attaches (bound=yes exit=running eacces=no label=creche-c3spike-xslot-63_(enforce)) | control: unconfined busybox cat README.md: read
probes.sh: H: 7 PASS, 0 FAIL. X: 7 PASS, 0 FAIL. Logs: /home/david/c3-spike/results/logs
probes.sh: next: David runs: sudo apparmor_parser -R /home/david/c3-spike/c3spike-slots.profile
probes.sh: then run /home/david/c3-spike/cleanup.sh
```

The policy dir line may instead say "not readable without root". The check then rests on `aa-exec` alone. The `exit=` codes of CH may differ: the verdict reads only `connected`, `eacces`, `bound` and `label`.

Exit 0 when every test passes, else 1. Copy the output into the spike report before step 6, because cleanup removes `results/`. To keep the logs too: `scp -r hyperion:/home/david/c3-spike/results <local dir>`.

If `check.sh` refuses, the probes do not start. It refuses when one of the 128 profiles does not exist (ENOENT), and then the output ends with the load command of step 3. It refuses too when a profile is not in enforce mode, or when the policy dir shows fewer than 128 in enforce mode. A refusal of `aa-exec` with another errno does not stop the probes.

### 5. Unload the profiles (David, sudo, one-off G20)

```sh
sudo apparmor_parser -R /home/david/c3-spike/c3spike-slots.profile
```

Expected: no output, exit 0. Run it before step 6, because the command reads the profile file in the scratch dir.

### 6. Run cleanup.sh (agent, on Hyperion)

```sh
ssh hyperion /home/david/c3-spike/cleanup.sh
```

Expected output:

```
cleanup.sh: removed /home/david/c3-spike
cleanup.sh: no creche-c3spike profile is loaded
```

If a profile is still loaded, cleanup removes nothing. It prints the unload command of step 5 and exits 1. `cleanup.sh --force` removes the dir anyway and leaves the profiles loaded. The H profiles then attach to paths that no longer exist, and the X profiles attach to none. Only a reboot or a sudo removal by name drops them.

cleanup decides "not loaded" only when the kernel answers ENOENT for all 128 names, H and X. If the policy dir is not readable, `aa-exec` cannot enter H slot 0, and the kernel refuses with another errno, the load state is unknown. cleanup then removes nothing, prints the unload command and exits 1. After the unload, run `cleanup.sh --force`. Its last line then says `load state unknown`, not `no creche-c3spike profile is loaded`.

### 7. Check that nothing is left (agent, on the Mac)

```sh
ssh hyperion 'test ! -e /home/david/c3-spike && echo gone'
```

Expected: `gone`.
