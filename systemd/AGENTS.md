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
