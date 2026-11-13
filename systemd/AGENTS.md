# systemd

Unit sources. A script installs them. Editing a file here changes nothing
until the unit is reinstalled and `daemon-reload` runs. Nothing is loaded
from this directory at run time. The root `AGENTS.md` applies here too.

| Unit | Scope | Installed by |
|---|---|---|
| `creche-handover.{service,path}` | system, root | by hand only, never by a release |
| `creche-attendance.service`, `creche-caregiver.service`, `creche-door-trigger.service`, `creche-noticeboard.service` | user | the cutover script, in the private repository |
| `creche-door-acp.service` | user | no script yet. The door ships in `attendance` |
| `creche-memory.service` | user | no script yet. The memory component ships it |
| `creche-door-trigger@.service` | user, templated | the cutover script, only when the host has none |
| `creche-vmm.service` | user | no script yet. The operator installs it by hand once (design section 6.2 step 3). No agent installs it |
The host's own units are not here: the watchdog, the registry sync, the
corpus sync, the drift check, `creche-library@` and follow. They are files
of creche-release (`units/user/`), and a `host` release links each one into
the operator's unit directory.

Installing a unit stays an installer's job. A release refreshes an already
installed unit file in place and never runs an installer.

## Rules

- **Every service of a component points its `ExecStart` into that
  component's tree**, never into `/opt/creche/.venv`. A release swaps the tree
  and restarts the unit. A unit that ran from the deploy's shared venv would
  relaunch the old code, and the ledger would record a success. The three
  door units run from the `attendance` tree. The two long-running doors
  carry `PartOf=creche-attendance.service`.
- **Every user unit carries both lines:**
  ```
  Environment=HOME=%h
  Environment=XDG_CONFIG_HOME=%h/.config
  ```
  `sbx` panics without them.
- **Every user unit reads the site file, except `creche-vmm.service`.**
  `EnvironmentFile=/etc/creche/site.env` sets `AGENT_LAN_ADDRESS` and the
  other site values. No unit names an address. `creche-vmm.service` reads
  `vmm.env` alone (Decided rule 1).
- **Per-family timer units are generated, not stored here.**
  `caregiver/src/caregiver/timers.py` writes `creche-door-trigger-<family>-t<N>.timer`
  into the user's unit directory. Each carries a "do not hand-edit" header
  and points at `creche-door-trigger@.service` with `Unit=`.
- **Mount-namespace hardening needs a system unit.** On a host with
  `apparmor_restrict_unprivileged_userns=1`, a user unit cannot use
  `ProtectSystem`, `ProtectHome`, `PrivateTmp` or `ProtectProc`. The unit
  of the chaperone is a system unit for this reason. That unit is in the
  chaperone repository, not here.
- **A user unit still gets the hardening that needs no namespace:**
  `NoNewPrivileges`, `UMask`, `SystemCallArchitectures`, `RestrictRealtime`,
  `LockPersonality`. `SystemCallFilter` and `MemoryDenyWriteExecute` are not
  set on the user units. They shell out to `sbx`, and neither option has been
  tested against that.
- **The release executor's service has no mount-namespace sandboxing.** Its
  `code` step is the deploy.

## Timers

The host's timers and their cadences are in creche-release. The per-family
timers are generated, as the rules say.

Timers hold no authority, so `caregiver` may create and retire them. A family
file's removal is always a human verb.

## Adding a unit

1. Write the source here with a header that says system or user, who installs
   it, and why it exists.
2. Install it from a script, not by hand.
3. A user unit gets the two `Environment=` lines.
4. A unit of the host itself, one that no component ships, goes in
   creche-release, not here.
5. Name it in the root `README.md`, section 3.

## Decided

1. The env file of `creche-vmm.service` is decisions CH59 rule 1 and
   CH77 rule 1. The unit reads `/srv/agents/state/creche/vmm.env` alone,
   never the site file. `bin/fleet-up up` writes `vmm.env` with
   `creche_env_get`: each key of `rust/crates/vmm/src/config.rs` except
   `RUNTIME_DIRECTORY`, resolved against the site file. The verify hooks
   of the vmm read the same file, so a hook checks the values that the
   unit runs. A site edit reaches the vmm only after `fleet-up up`.
2. The lifetime of `serial.log` is decision CH59 rule 2. The unit keeps
   `RuntimeDirectoryPreserve=no`, so systemd removes the run root at each
   stop and each restart. So each `serial.log` is in
   `sessions/<id>/serial.log` in the state root, kept until the next start
   of that session (gap G23, plan PR D17).
3. The vmm stand-in of the job `kvm` copies the decided lines of
   `creche-vmm.service` that touch the launch chain and its stop:
   `RuntimeDirectory=vmm`, `RuntimeDirectoryMode=0700`,
   `NoNewPrivileges=yes`, `KillMode=mixed`, `ExecStopPost=` and each
   `Restart` line (`rust/crates/vmm-confine/tests/stand_in`, decisions
   CH60 rule 1, CH61 rule 1). A test in the job `rust` fails when a line
   differs from this unit. The stand-in runs its `ExecStopPost=` line
   with the `vmm` that cargo built in place of `/opt/components/vmm/bin/vmm`
   (decision CH63). So no_new_privs is set before the launch
   stage writes the AppArmor exec attribute, as in production. The stage
   sets it only after that write (decision CH55). The kvm tests check
   no_new_privs of the stand-in before each launch, then prove the slot
   label of CH. A refused write fails the launch closed (decision CH55),
   so a wrong order starts no VM and lets no VM run unconfined. systemd
   removes the run root at each stop of a stand-in, so the kvm tests
   prove that a restart and a crash remove the run root and keep each
   `serial.log` (item 2). Each stop removes the files of every test, so
   the kvm tests that write in the run root run one at a time.
4. The end of each VM is decision CH61, and its step is decision CH63.
   Each `creche-vm-*` scope has `BindsTo=` and `Before=`
   `creche-vmm.service` (CH61 rule 2), so the shutdown of the vmm runs
   before any VM stops. The `ExecStopPost=` step of the unit then sends
   SIGKILL to every `creche-vm-*` scope that is left, after a stop, a
   restart and a crash (CH61 rule 1). It runs `vmm kill-scopes` (CH63),
   which lists the scopes with the code of the leftover sweep (CH19 (4))
   and runs `systemctl --user kill` on each one by name. A kill queues no
   job. A `systemctl stop` of a scope there would wait for a stop job
   that `Before=` orders after the stop of the vmm unit, which waits for
   the step: a deadlock. A scope that goes between the list and its kill
   gives NoSuchUnit and counts as ended. Any other failure fails the
   step. A crash queues no stop job, so `Before=` orders nothing there,
   and the `BindsTo=` stop of a scope may end its CH before the step
   does. Either way the VM ends. The kvm tests prove the stop, the
   restart and the crash, each
   with the control that CH runs while the stand-in runs
   (`rust/crates/vmm-confine/tests/kvm_slot.rs`). Each stand-in runs the
   `vmm kill-scopes` of the `vmm` that cargo built for the test run.
5. The vmm unit uses `Type=notify`. The vmm sends `READY=1` after it binds
   `vmm.sock`. Thus, a unit ordered after it starts after the socket bind.
   `NotifyAccess=main` lets only the main process send this notice.
