# systemd

Unit sources. A script installs them. Editing a file here changes nothing
until the unit is reinstalled and `daemon-reload` runs. Nothing is loaded
from this directory at run time. The root `AGENTS.md` applies here too.

| Unit | Scope | Installed by |
|---|---|---|
| `creche-chaperone.service` | system, `User=chaperone` | the root bootstrap script, in the private repository |
| `creche-handover.{service,path}`, `creche-handover-intake.service` | system, root | by hand only, never by a release |
| `creche-attendance.service`, `creche-door-owui.service`, `creche-caregiver.service`, `creche-trigger-webhooks.service`, `creche-noticeboard.service` | user | the cutover script, in the private repository |
| `creche-trigger@.service` | user, templated | the cutover script, only when the host has none |
| `creche-watchdog.{service,timer}`, `registry-sync.{service,timer}` | user | the cutover script |
| `index@.{service,timer}`, `index-code@.{service,timer}` | user, templated | `bin/provision-library.sh` |
| `code-corpus-sync.*`, `sbx-drift.*` | user | setup scripts, in the private repository |

Installing a unit stays an installer's job. A release refreshes an already
installed unit file in place and never runs an installer.

## Rules

- **Every service of a component points its `ExecStart` into that
  component's tree**, never into `/opt/creche/.venv`. A release swaps the tree
  and restarts the unit. A unit that ran from the deploy's shared venv would
  relaunch the old code, and the ledger would record a success. The three
  door units run from the `attendance` tree. The two long-running doors carry
  `PartOf=creche-attendance.service`.
- **A unit that runs a repository script points into `/opt/creche/bin`**, the
  root-owned deployed checkout. The watchdog and `registry-sync` do this.
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
  `caregiver/src/caregiver/timers.py` writes `creche-trigger-<family>-t<N>.timer`
  into the user's unit directory. Each carries a "do not hand-edit" header
  and points at `creche-trigger@.service` with `Unit=`.
- **Mount-namespace hardening needs a system unit.** On a host with
  `apparmor_restrict_unprivileged_userns=1`, a user unit cannot use
  `ProtectSystem`, `ProtectHome`, `PrivateTmp` or `ProtectProc`. The
  chaperone's unit carries them in full. Its `ReadWritePaths` list is the
  complete set of directories it may write. Its only capabilities are
  `CAP_SETUID` and `CAP_SETGID`, for `chaperone-as`. `InaccessiblePaths=`
  hides the vault, the registry and the work roots, with a `-` prefix.
- **The chaperone's `HOME` is a subdirectory of its state directory, never
  the parent.** Owning the parent would let a compromised child rename a
  root-owned tree out from under a deploy.
- **A user unit still gets the hardening that needs no namespace:**
  `NoNewPrivileges`, `UMask`, `SystemCallArchitectures`, `RestrictRealtime`,
  `LockPersonality`. `SystemCallFilter` and `MemoryDenyWriteExecute` are not
  set on the user units. They shell out to `sbx`, and neither option has been
  tested against that.
- **`sleep 8` in the index templates stays.** `sbx exec`'s attach races
  process exit. Keep `TimeoutStartSec` above the `timeout` value.
- **The two index timeouts differ on purpose.** The vault template allows
  1500 s, the code template 3000 s.
- **The release executor's service has no mount-namespace sandboxing.** Its
  `code` step is the deploy.

## Timer cadences

| Timer | Cadence | Why |
|---|---|---|
| `index@<scope>` | 15 min | bounds retrieval staleness |
| `index-code@<repo>` | 1 h | the corpus changes only on a push |
| `code-corpus-sync` | 1 h | pull only |
| `sbx-drift` | daily | the global policy must hold zero network allows |
| `creche-watchdog` | 1 min | the thresholds set how fast an outage is seen, not the cadence |
| `registry-sync` | 1 min | an unchanged run is one `git fetch` that transfers nothing |

A drop-in under the user's unit directory survives a reinstall, so these
cadences are defaults. `systemctl --user cat <unit>` shows what runs.

Timers hold no authority, so `caregiver` may create and retire them. A family
file's removal is always a human verb.

## Adding a unit

1. Write the source here with a header that says system or user, who installs
   it, and why it exists.
2. Install it from a script, not by hand.
3. A user unit gets the two `Environment=` lines.
4. A unit that runs a repository script uses `ExecStart=/opt/creche/bin/<name>`.
5. Name it in the root `README.md`, section 3.
