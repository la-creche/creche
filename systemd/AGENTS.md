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
- **Every unit reads the site file.** `EnvironmentFile=/etc/creche/site.env`
  sets `AGENT_LAN_ADDRESS` and the other site values. No unit names an
  address.
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

## Known gaps

1. CONTRACT-QUESTION: the env file of `creche-vmm.service`. Gap G5
   option A (decision CH36) says the keys of the vmm are "rendered into
   the unit from the site file", and no row names the renderer. The vmm
   compiles in no default, and the site file holds only overrides. The
   reading taken: the unit reads
   `/srv/agents/state/creche/vmm.env`, as the other user units read their
   own env file. The file holds each key of
   `rust/crates/vmm/src/config.rs` except `RUNTIME_DIRECTORY`, as
   `creche-env get` resolves it against the site file. No script writes
   the file yet, and no script installs the unit. A proposal: `fleet-up up`
   writes the file with `creche_env_get`, as it writes `noticeboard.env`,
   and installs the unit. Until then the start fails, and the vmm never
   runs with a guessed value.
2. Plan risk 5: the unit sets `NoNewPrivileges`, so no_new_privs is set
   when the launch stage writes the AppArmor exec attribute. The kvm job
   sets it after that write, as the stage does (decision CH55). No job
   proves the order under the unit. The pre-cut proof (decision CH47)
   runs the vmm by hand with the unit stopped, so the first start of the
   unit on the host is also its first run in this order. A refused write
   fails the launch closed (decision CH55), so a wrong order starts no
   VM and lets no VM run unconfined.
3. CONTRACT-QUESTION: the lifetime of `serial.log` under
   `creche-vmm.service`. Each VM's `serial.log` lives in the run root
   (decision CH41 Q39-2, `rust/crates/vmm-store/src/layout.rs`). Gap G23
   keeps it until the next start, and `layout.rs` keeps it until a VM next
   takes the slot. The unit keeps the default
   `RuntimeDirectoryPreserve=no`, so systemd removes the run root at each
   stop and each restart, a crash included. Each `serial.log` is then gone
   when it matters most. `RuntimeDirectoryPreserve=restart` is no fix: the
   bind refuses an existing `vmm.sock` (`rust/crates/vmm/src/server/accept.rs`),
   so the vmm exits 78 and the unit stays stopped. The alternative needs a
   decision: move `serial.log` out of the run root, for example into the
   state root.
