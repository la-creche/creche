# playpen

The image that each root disk of the `disks` row is built from, and its
scripts. The root `AGENTS.md` applies here too.

## The image

`Dockerfile` builds the image of every family's microVM. It holds Node 24,
ripgrep and the ACP facade (`facade/AGENTS.md`) with the agent module it
imports (`agent/AGENTS.md`). One shim puts the facade's one bin on `PATH`:

| Program | Runs | The build proves |
|---|---|---|
| `creche-facade` | `node /opt/creche-facade/dist/src/bin/creche-facade.js` | exit 17 and one stderr line with no `CRECHE_SESSION_ID` |

The build stage installs `facade/` and `agent/` from the facade's frozen
lockfile, builds both and runs `pnpm deploy --prod` to `/opt/creche-facade`.
The Flue pin is the one in `facade/pnpm-lock.yaml`. The image fixes no
host address: the facade reaches the two planes on guest loopback, which
guest init splices to the host. The `python` flavor adds pandas, numpy and matplotlib.

The `python` flavor also adds a second venv, `/opt/test`. It has CPython
3.14, `pytest`, and the locked test dependencies of the Python workspace.
The workspace needs 3.14 because its `requires-python` names the Python of
the host.
On `PATH`, `pytest` runs the venv. `python3` stays the 3.11 interpreter of
the analysis stack.

The `coding` flavor of `code` and `creche` is the python flavor plus uv,
pnpm through corepack, and cargo with Rust 1.92.0, rustfmt and clippy
(CH7-1, for now). Agents install a repo's locked dependencies through the
egress proxy and run its checks. Its `SandboxFlavor` member joined before
the cut (CH79 rule 1). It also holds `creche-git-spool`, the guest end of
the git platform spool (see "The git platform spool helper").

The Python flavor ships no Python package installer. `pip`, `uv` and
`ensurepip` exist in build steps only. The test venv excludes `nodeenv`.
A dependency change for a venv is a lockfile change plus a disks release.

Python 3.11 also rides in with the node parent image. It is an accident
of the parent, not a tool of this image: it has no `pip` and it cannot
build a venv.

Each flavor's last step writes `/etc/creche/env`: one `NAME=value` line
per variable of the image config, sorted, with each `PATH` folder once.
Guest init reads it, because a root disk carries no image config (CH7-7,
CH49). The step lists the names. `bin/playpen-env-check` compares the file
with the built config in CI, so a new `ENV` name fails until it is listed.
Then node in the image places each name of the file in one list of the
agent (`agent/src/tool-env.ts`, CH49 rule 1), so a new name, the parent
image's too, fails until it sits in a list.

The base stage makes `/var/lib/creche/session`, owner root, mode 0755. Guest
init mounts the session disk there and makes no dir (CH62 rule 5). Only
this stage makes it (CH81 rule 1). `disk/Dockerfile` builds the disk
tools, and the dir reaches the disk in the flavor tar. The
other mount points (`/dev /proc /sys /tmp /run /root /var/cache /var/tmp`)
come with the parent image. The same stage fails the build on a parent
that drops a tmpfs dir. The build mounts `/dev`, `/proc` and `/sys` in each
`RUN` step, so only the disk can show them missing. `bin/playpen-disk`
reads all nine back from the disk with `debugfs`, and
`bin/tests/test_playpen_image.py` holds both lists equal to the boot plan
of guest init.

The kvm job builds the base disk from the tree of each run
(`bin/vm-smoke-setup.sh`), and `rust/crates/vmm/tests/kvm_facade.rs` boots
it under guest init. So a change to the facade, the agent or this image
gets one real facade proof on its own build (decisions CH62 rule 12,
CH80).

The image holds no pi, no fd, no playpen bundle, no PEP bridge, no
`agent-pi-launch`. Only the `coding` flavor holds a package installer.

CI builds the image from the repository root, with `playpen/Dockerfile`.
Do not build it on a development machine. CI builds each flavor on each
code change (`bin/playpen-ci`).

## The git platform spool helper

`guest/creche_git_spool.py` is `/usr/local/bin/creche-git-spool` in the
coding flavor (CH3, phase 12 PR3). A VM has no route to GitHub and holds
no token, so the `git-platform` tools of chaperone push and fetch for it.
A git bundle carries each branch between the VM and the tools. One MCP
result holds at most 1 MiB, so the bundle goes through the spool of the
host instead.

A push, end to end:

1. The agent runs `creche-git-spool push <branch>` in its clone. The
   helper runs `git bundle create` of `refs/heads/<branch>` over the
   remote-tracking refs, so the commits that GitHub already holds stay
   out and become prerequisites. A head that the remote already holds
   gets a bundle of that one commit over its parents.
2. The helper dials guest 127.0.0.1:1105 and sends `put <size>` and the
   bundle. Guest init splices the link to host port 1105.
3. The vmm checks the `push` grant, writes the bundle in `push/` of the
   spool and answers `ok <id>`. The helper prints the id on stdout.
4. The agent calls the `push` tool of `git-platform` with the repo, the
   branch and the id. The upstream reads the bundle, fetches its
   prerequisites, runs `git bundle verify` and pushes its head to
   `refs/heads/<branch>`. It refuses `main`, `master`, a tag and a force.

A fetch, end to end:

1. The agent calls the `fetch` tool of `git-platform` with the repo and
   the branch. The upstream writes a bundle of the whole branch in
   `fetch/` and answers its id.
2. The agent runs `creche-git-spool fetch <id>`. The helper sends `get
   <id>`, and the vmm checks the `fetch` grant and the family of the id
   and sends `ok <size>` and the bytes.
3. The helper runs `git bundle verify` and fetches the one branch of the
   bundle into a local branch of the same name, or into `--branch`. The
   refspec has no `+`, so a branch that does not fast-forward stays. git
   refuses to fetch into the checked-out branch. For that branch the
   helper fetches only the objects and runs `git merge --ff-only`, so the
   branch and its work tree move together.

A family with no grant has no host listener on 1105. Guest init then
closes each link with no reply, and the helper says so. The helper checks
only the size cap and the line cap before it dials. The vmm and the
upstream check every rule again. It runs on `python3.14 -I`, so no module
of the repo under work loads. `rust/crates/wire/src/spool.rs` holds the
protocol.

`bin/tests/test_playpen_git_spool.py` runs the helper as a program on
real git repos, against a listener on 127.0.0.1:1105 that speaks the
protocol of the vmm over two scratch folders. It holds the port, the caps
and the reason words equal to `creche_wire`. The kvm test
`kvm_guest_init_bridges_the_spool_port` proves the guest init row.

## The release

This tree has no release of its own. The `disks` row ships what it builds
(`disks/AGENTS.md`), and its catalog row bundles `playpen`, `agent`,
`facade`, `pyproject.toml` and `uv.lock`. So a change here moves the
`disks` tag.

1. `bin/playpen-ci` builds each flavor and its root disk twice, and checks
   that the two disks are equal. It runs in the `playpen-image` job of CI.
2. `disks/bin/playpen-release-disks` runs `bin/playpen-rootfs`,
   `bin/playpen-disk` and `bin/playpen-kernel` in the `disks` job of
   `release.yml`. CI signs each file. Root installs each signed file and
   runs no build.
3. Keep the flavor lists equal: the targets of `Dockerfile` are the
   `bin/playpen-ci` lists, the flavors then the services. `FLAVORS` equals
   `FLAVORS` of `disks/bin/playpen-release-disks` and
   `catalog.DISK_FLAVORS`. `SERVICES` equals `SERVICES` of that script and
   `catalog.SERVICE_IMAGES`.
   `bin/tests/test_playpen_image.py` reads the Dockerfile as text, and
   `bin/tests/test_playpen_release_disks.py` holds the other lists equal.
   No test builds an image.

## Layout

| File | Owns |
|---|---|
| `Dockerfile` | the image: the build stage and the flavors |
| `bin/creche-env-write`, `bin/playpen-env-check` | `/etc/creche/env` in the last step of each flavor and service image, and the CI check that it equals the image config env. For a flavor, the check also places each name in an agent list. For a service image (kind `service`), it does not, because the image has no agent |
| `bin/playpen-ci` | the steps of the `playpen-image` CI job: build each flavor and each service image, then check it |
| `bin/playpen-rootfs` | the tar export of one flavor, the input of its root disk |
| `bin/playpen-disk`, `disk/Dockerfile` | the root disk of one flavor from its tar, in the pinned trixie stage |
| `bin/playpen-kernel` | the pinned CH guest kernel, compressed to ship like the disks |
| `guest/creche_git_spool.py` | `creche-git-spool` of the coding flavor, the guest end of the git platform spool |
| `guest/creche_browser_facade.py`, `guest/creche-chrome` | the facade and the Chrome wrapper of the browser service image |

## The browser service image

The target `service-browser` is the root disk `browser` of the disks row.
It is not a family flavor: a family file cannot name it (contract 01
§3.9). Its wire name is `ServiceImage::Browser`. The vmm boots it for the
`browser` MCP upstream, so that Chrome runs with its own sandbox and on no
host kernel. The private design note of the browser has the decisions.

| Program | What it is |
|---|---|
| `/usr/local/bin/creche-facade` | `guest/creche_browser_facade.py`, at the facade path of guest init |
| `/usr/local/bin/creche-chrome` | `guest/creche-chrome`: Google's Chrome with the egress proxy and no background traffic |
| `/usr/local/bin/agent-browser` | the agent-browser release asset, at its GitHub digest |

1. The base is `PYTHON_IMAGE`. Chrome is `google-chrome-stable` from
   Google's signed apt repo, at build time. The build checks the
   fingerprint of the signing key. A new Chrome stable reaches the host as
   a new disks release.
2. The setuid helper of Chrome loses its setuid bit. Chrome starts its
   sandbox from user namespaces, which the guest kernel allows.
3. Guest init starts the facade as root, as it starts the facade of a
   family VM. The facade opens the local link at once and sends no line on
   it. The stop line, or the end of the link, ends the facade, its
   children and every process of the user `browser`.
4. The facade listens on `RELAY_SOCKET`, where guest init joins each link
   of guest port 1201. The first line of a link names it: `mcp` or `view`,
   then LF, at most 16 bytes, within 10 s. Any other line closes the link.
   At most 8 links of each kind are open.
5. An `mcp` link runs `agent-browser mcp --tools core` as the user
   `browser`, with the link as its stdin and stdout, and an env of its
   own. Every link shares one browser, the default session of
   agent-browser.
6. A `view` link joins the link to the stream server of agent-browser on
   guest `127.0.0.1:9223`, both ways, and reads no byte. The vmm filters
   the view: the facade is no guard.
7. The home of `browser` is a new directory on the guest tmpfs at each
   start of the facade. No profile or cookie stays on any disk.

The build runs Chrome and agent-browser once, and the facade outside
guest init, which must exit with status 17. `bin/tests/
test_playpen_browser_facade.py` holds the line rules, the paths that guest
init shares and the flags of the wrapper.

## Known gaps

1. CONTRACT-QUESTION: CH49 rule 1 places each name of a flavor's
   `/etc/creche/env` in one list of the agent. It says nothing about a
   service image, which has no agent. The reading taken: the CH7-7
   equality check applies to the browser image, and the agent list check
   does not (`bin/playpen-env-check`, kind `service`). A rule that also
   bound service images would need an env list of the browser facade.
