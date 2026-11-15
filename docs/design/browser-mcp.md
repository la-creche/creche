# Browser tool for principal (agent-browser) — design note

> Status as of 2026-11-14: Open. The decisions B1 to B6 are decided and the VM option of section 10 is open. Host steps are on hold. Check the operator's private notes before you act on a step.
> Host names, addresses and accounts are placeholders. The operator's private notes hold the real values.

Status: DECIDED 2026-11-13 (B1-B5 all as recommended), B6 2026-11-14 (fix A). REOPENED 2026-11-14 for the VM option, section 10. V1-V6 DECIDED 2026-11-14, all as recommended: the browser runs in a service VM, and B1 (in its host form), B3, B5 and B6 are superseded. The host steps of section 7 will not run. Next: the spike, 10.5 step 1.

**B5 (Chromium source), decided:** Chrome for Testing publishes no sha256, only md5 and crc32c, and contract 01b §3.2 forbids an author-computed pin. Chromium comes from Google's signed apt repo (`google-chrome-stable`) as a host step, with unattended-upgrades allowed for that origin. handover changes only to take a bare-binary release asset (`install.format = "binary"`). agent-browser's own sha256 is published by GitHub (asset digest `sha256:8e69c9fb...` for v0.43.0 linux-x64). The host lacked only `libasound.so.2` of Chrome's 32 needed libraries (checked 2026-11-13); apt pulls it with the package.

## 1. The ask

The operator wants the principal family to drive a real browser with [vercel-labs/agent-browser](https://github.com/vercel-labs/agent-browser) (Apache-2.0, v0.43.0, 2026-11-12). The tool can be a direct MCP upstream (like kagi or memory) or an agentic tool (a delegate family, like coding-sandbox), whichever fits better.

## 2. Facts that decide the shape

1. agent-browser is a Rust CLI. `agent-browser mcp` is an MCP server over stdio. The chaperone runs every upstream as a stdio effect plugin (DECISIONS Q29), so the server fits the chaperone with no bridge.
2. Its MCP tools take a `--tools` profile. The `core` profile has 37 tools: open, snapshot, read, click, fill, type, press, select, scroll, waits, get_text/url/title, tabs, back/forward/reload, eval, screenshot, close, flow_*, webmcp_*.
3. The chaperone refuses an image block in a result (`bad_result`, chaperone design.md §7.7). `agent_browser_screenshot` returns an image, so it cannot be granted. Snapshots are text, so the main loop works.
4. A family VM reaches only exact `host:port` entries (contract 01 §3.7, no wildcard). A browser needs the open web, so a browser inside a family VM (principal or a thin delegate) does not work without a contract change.
5. An upstream runs on the host as `mcp-<name>`, a child of `creche-chaperone.service`. That unit has no IP fence. In practice an upstream reaches the LAN: Home Assistant, Node-RED (192.0.2.11), the router, the host's own ports.
6. The chaperone unit sets `RestrictNamespaces=yes`. Chromium's sandbox needs user namespaces, so Chromium under the chaperone needs `--no-sandbox`.
7. Install: handover has three sources (`pypi`, `github-release`, `agent-mcp`). `github-release` requires a tar archive (`mcpbuild.py:563`). agent-browser ships bare binaries (`agent-browser-linux-x64`). Chromium is not in any source. `agent-browser install` downloads Chrome for Testing at run time, which contract 01b §9 forbids ("no runtime fetch").
8. The default call timeout is 30 s. A page load fits. A long multi-step task does not, but each step is its own call.
9. Useful knobs (env, read in `cli/src/flags.rs`): `AGENT_BROWSER_EXECUTABLE_PATH`, `AGENT_BROWSER_ARGS`, `AGENT_BROWSER_SOCKET_DIR`, `AGENT_BROWSER_ALLOWED_DOMAINS`, `AGENT_BROWSER_ACTION_POLICY`, `AGENT_BROWSER_CONTENT_BOUNDARIES`, `AGENT_BROWSER_MAX_OUTPUT`. The daemon exits after 1 h idle.

## 3. Recommendation: a direct MCP upstream, `browser`

Grant the tools to principal directly. Do not add a delegate family.

- A delegate adds a second model loop, a 120 s cap on the whole browsing task, and a second model's cost. The browsing agent would still need open-web egress, which a VM cannot have today (fact 4).
- agent-browser is built as tools for the calling agent: snapshot, then act on refs. Principal already treats web content as data (instructions.md lines 56-59).
- It matches kagi: one server file, one grant line.

The direct upstream needs three things that do not exist today. Each is a gate before the grant merges.

1. **LAN fence (host, root, one time).** An nftables rule drops every packet from uid `mcp-browser` to private, link-local and loopback ranges. It covers Chromium's child processes too, because they keep the uid. Without it, a page that steers the agent can reach Node-RED or Home Assistant and skip the chaperone's gates on `ha` and `nodered`.
2. **Install of the binary and Chromium.** A new handover install source, or a widened `github-release`: a bare-binary asset, plus a second pinned archive (Chrome for Testing, sha256) unpacked next to it. Both are fetched at build time, root-owned, hash-checked.
3. **Run config.** Entry `agent-browser mcp --tools core`, with env: the executable path into the pinned Chromium, `AGENT_BROWSER_ARGS=--no-sandbox` (fact 6), the socket dir under `run.state_dir`, content boundaries on, an output cap.

Proposed grant (`tools.browser` in `families/principal/family.toml`): open, snapshot, read, click, fill, type, press, select, check, uncheck, scroll, the five waits, get_text, get_url, get_title, back, forward, reload, tab_new, tab_list, tab_switch, tab_close, close. Left out: screenshot (fact 3), eval (arbitrary JS), flow_* (writes files), webmcp_* (page-provided tools).

## 4. The alternative, and why it is second

A thin delegate family `web-browser` with Chromium in its VM image and a new egress mode "any public host, ports 80 and 443". The vmm proxy already refuses names with a non-public record, so the LAN fence comes free, and Chromium keeps its sandbox inside a VM. The cost: a contract 01 §3.7 change (the no-wildcard rule), a proxy change, a larger guest image, the 120 s cap per delegation, and a second model loop. It is the stronger isolation and the larger build.

## 5. Decisions (the operator, 2026-11-13: all as recommended, B5 at the top)

- **B1, shape:** direct upstream `browser` (recommended) or delegate family `web-browser`.
- **B2, LAN fence:** a uid nftables rule on the host before the grant (recommended), or accept LAN reach.
- **B3, Chromium sandbox:** accept `--no-sandbox` under the chaperone unit (recommended with B2: a renderer exploit lands as `mcp-browser`, no home, read-only system), or give the upstream a drop-in that lifts `RestrictNamespaces` for it.
- **B4, tool list:** the list in section 3 (recommended), or add eval.
- **B6, syscall filter (the operator, 2026-11-14: fix A):** the chaperone unit's `SystemCallFilter=@system-service` kills Chrome on `pkey_alloc` and `landlock_create_ruleset` (section 8). The `browser.conf` drop-in adds an allow line for `pkey_alloc pkey_free pkey_mprotect landlock_create_ruleset landlock_add_rule landlock_restrict_self`. Any other refused syscall still kills the process and logs audit type 1326. The line widens the filter for every upstream of the chaperone, because they share one unit. Fix B, `SystemCallErrorNumber=EPERM`, was not chosen: it would make every refusal silent.

## 6. Build order and PRs

| # | Where | What | State |
|---|---|---|---|
| 1 | creche | `install.format = "binary"` for a bare release asset, both readers, vectors | la-creche/creche#565 |
| 2 | private-docs | contract 01b §3 row and §3.2 paragraph | la-creche/private-docs#77 |
| 3 | registry | `mcp/browser/server.toml`, plus the `CRECHE_FAMILY_REF` bump to the #565 merge commit | after #565 merges |
| 4 | The host | section 7 steps 1 to 6 (the operator) | after the `mcp-servers` tap |
| 5 | registry | `tools.browser` grant for principal and its instructions.md lines | after step 7.6 shows the upstream up |

## 7. Host steps for the operator (the host)

Checked read-only on 2026-11-13: Ubuntu 24.04.5, nftables 1.0.9 (the `nftables` unit is disabled, Docker owns iptables-nft), DNS through systemd-resolved at `127.0.0.53`, no `mcp-browser` user, no Chrome. Of Chrome's 32 needed libraries only `libasound.so.2` is absent, and apt pulls it.

agent-browser drives Chrome over CDP on `127.0.0.1` with a random port (`--remote-debugging-port=0`, `cli/src/native/cdp/chrome.rs:467`). So the fence lets `mcp-browser` reach `127.0.0.1` on the ephemeral range 32768-60999 only. Today one other listener sits in that range on all addresses: the kernel NFS lock manager (`nlockmgr`, tcp 37351). It speaks RPC, not HTTP.

1. **Chrome from Google's apt repo (sudo).**
   ```
   sudo install -d -m 0755 /etc/apt/keyrings
   curl -fsSL https://dl.google.com/linux/linux_signing_key.pub | sudo gpg --dearmor -o /etc/apt/keyrings/google-chrome.gpg
   echo 'deb [arch=amd64 signed-by=/etc/apt/keyrings/google-chrome.gpg] https://dl.google.com/linux/chrome/deb/ stable main' | sudo tee /etc/apt/sources.list.d/google-chrome.list
   sudo apt-get update && sudo apt-get install -y google-chrome-stable
   ```
   Check: `apt-cache policy google-chrome-stable` names the origin `o=Google LLC`. If it names another origin, fix the pattern of step 2 to match.
2. **Let unattended-upgrades update Chrome (sudo).**
   ```
   printf 'Unattended-Upgrade::Origins-Pattern {\n    "origin=Google LLC,archive=stable";\n};\n' | sudo tee /etc/apt/apt.conf.d/52unattended-google-chrome
   sudo unattended-upgrade --dry-run --debug 2>&1 | grep -i 'google'
   ```
3. **The user (sudo).** The `mcp-servers` build runs as it, and the fence names it.
   `sudo useradd --system --user-group --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin mcp-browser`
4. **The LAN fence (sudo), before any chaperone restart.**
   ```
   sudo install -d -m 0755 /etc/creche
   printf '%s\n' 'table inet creche_browser' 'delete table inet creche_browser' 'table inet creche_browser {' '  chain output {' '    type filter hook output priority filter; policy accept;' '    meta skuid != "mcp-browser" accept' '    ip daddr 127.0.0.53 meta l4proto { tcp, udp } th dport 53 accept' '    ip daddr 127.0.0.1 tcp dport 32768-60999 accept' '    ip daddr { 0.0.0.0/8, 10.0.0.0/8, 100.64.0.0/10, 127.0.0.0/8, 169.254.0.0/16, 172.16.0.0/12, 192.0.0.0/24, 192.168.0.0/16, 198.18.0.0/15, 224.0.0.0/4, 240.0.0.0/4 } counter drop' '    ip6 daddr { ::/128, ::1/128, ::ffff:0:0/96, 64:ff9b::/96, fc00::/7, fe80::/10, ff00::/8 } counter drop' '  }' '}' | sudo tee /etc/creche/browser-fence.nft
   printf '%s\n' '[Unit]' 'Description=Fence mcp-browser off private addresses' 'Before=creche-chaperone.service' '' '[Service]' 'Type=oneshot' 'RemainAfterExit=yes' 'ExecStart=/usr/sbin/nft -f /etc/creche/browser-fence.nft' 'ExecStop=/usr/sbin/nft delete table inet creche_browser' '' '[Install]' 'WantedBy=multi-user.target' | sudo tee /etc/systemd/system/creche-browser-fence.service
   sudo systemctl daemon-reload && sudo systemctl enable --now creche-browser-fence.service
   sudo nft list table inet creche_browser
   sudo -u mcp-browser curl -s -m 5 -o /dev/null -w '%{http_code}\n' http://192.0.2.11:1880/
   sudo -u mcp-browser curl -s -m 5 -o /dev/null -w '%{http_code}\n' https://example.org/
   ```
   Expected: the Node-RED call prints `000` (dropped), example.org prints `200` (the host cannot resolve example.com, section 8 fact 7).
5. **Tap the `mcp-servers` release** that the registry server-file PR starts. It builds `/opt/mcp/browser` as `mcp-browser` and makes `/var/lib/mcp/browser`.
6. **The chaperone drop-in, manifest and entry (sudo), then restart.** The drop-in also makes the chaperone refuse to start without the fence.
   ```
   printf '%s\n' '[Unit]' 'Requires=creche-browser-fence.service' 'After=creche-browser-fence.service' '' '[Service]' 'ReadWritePaths=-/var/lib/mcp/browser' 'SystemCallFilter=pkey_alloc pkey_free pkey_mprotect landlock_create_ruleset landlock_add_rule landlock_restrict_self' | sudo tee /etc/systemd/system/creche-chaperone.service.d/browser.conf
   sudo systemctl daemon-reload
   sudo install -d -m 0755 /etc/chaperone/plugins/effect/added/browser
   printf '%s\n' 'name = "browser"' 'version = "1.0.0"' 'kind = "effect"' 'hooks = ["upstream"]' 'command = [' '    "/usr/bin/env",' '    "AGENT_BROWSER_ARGS=--no-sandbox",' '    "AGENT_BROWSER_CONTENT_BOUNDARIES=1",' '    "AGENT_BROWSER_EXECUTABLE_PATH=/usr/bin/google-chrome-stable",' '    "AGENT_BROWSER_MAX_OUTPUT=200000",' '    "AGENT_BROWSER_NO_VERSION_CHECK=1",' '    "HOME=/var/lib/mcp/browser",' '    "/opt/mcp/browser/bin/agent-browser",' '    "mcp",' '    "--tools",' '    "core",' ']' | sudo tee /etc/chaperone/plugins/effect/added/browser/plugin.toml
   printf '\n[plugins.browser]\nuser = "mcp-browser"\n' | sudo tee -a /etc/chaperone/install.toml
   sudo systemctl restart creche-chaperone.service
   ```
   The manifest is the output of `bin/chaperone-install-draft.py` for the server file (checked 2026-11-13). Then the agent reads the chaperone journal for the `browser` upstream up, before the grant PR merges.

Proof after the grant: principal opens a public page and reads its snapshot. Asked to open `http://192.0.2.11:1880`, it gets a network error. `sudo nft list table inet creche_browser` shows the drop counter go up.

Open risk: a page heavier than `example.org` may need a syscall beyond B6's six. If Chrome dies at the proof, `sudo journalctl -k | grep type=1326` names the syscall. The mount-based limits of the chaperone unit were not tested (section 8).

## 8. Spike on the host (2026-11-14, as <user>, no sudo, scratch dir removed)

Chrome 157.0.8103.78 from Google's `.deb`, unpacked, plus `libasound2t64` from `apt-get download`. Each run was `systemd-run --user` with limits copied from `systemctl cat creche-chaperone.service`. The mount-based limits (`ProtectSystem`, `PrivateDevices`, `ProtectProc`, `ProcSubset`, `PrivateTmp`, `ProtectHome`) need root and were not tested.

Facts:
1. agent-browser 0.43.0 matched the pinned sha256. Over MCP it serves 33 tools under `--tools core`. All 26 names in `mcp/browser/server.toml` are among them.
2. `agent_browser_open` under all the copied limits: `Chrome exited early ... without writing DevToolsActivePort`. Memory peak 63 MB.
3. One limit at a time, headless Chrome with `--dump-dom`: only `SystemCallFilter=@system-service` kills Chrome (core dump, SIGSYS). `NoNewPrivileges`, `RestrictNamespaces`, `SystemCallArchitectures`, `RestrictAddressFamilies`, `RestrictRealtime` and `LockPersonality` each let Chrome start.
4. The kernel audit log names the refused syscalls: 330 `pkey_alloc` and 444 `landlock_create_ruleset`.
5. With `SystemCallErrorNumber=EPERM` added, so a refused syscall returns EPERM instead of killing, Chrome starts.
6. `RestrictAddressFamilies` (no `AF_NETLINK`) only logs `Could not create NETLINK socket` and `Failed to initialize a udev monitor`. Chrome keeps running.
7. Every run, the one with no limits included, failed the page load with `net::ERR_NAME_NOT_RESOLVED`. Cause: The host's resolver has no answer for `example.com` or `www.example.com` (`resolvectl query example.com`: `Name 'example.com' not found`). `example.org` and `en.wikipedia.org` resolve, and Chrome's net log shows its own DNS client resolving Google's hosts through 127.0.0.53. So the test URL was wrong, and Chrome's DNS works.
8. Page loads of `https://example.org` (`filter.sh`): with no limits, PASS. With the filter alone, killed. With the filter plus an allow list of `pkey_alloc pkey_free pkey_mprotect landlock_create_ruleset landlock_add_rule landlock_restrict_self` (fix A), PASS. With the filter plus `SystemCallErrorNumber=EPERM` (fix B), PASS. Under every copied chaperone limit, fix A and fix B each PASS.
9. The whole path under every copied chaperone limit, once with fix A and once with fix B: agent-browser over MCP, then `agent_browser_open` (`Example Domain`), `agent_browser_get_title`, `agent_browser_snapshot` (`- link "Learn more" [ref=e1]`), `agent_browser_close`. All ok. Page text arrives inside the nonce boundary.
10. The kernel log for the whole `filter.sh` run holds kills of syscalls 330 and 444 only, from the run without a fix. Fix A needs no syscall beyond its six for this page. A heavier page may still need one.

## 9. Release order (2026-11-14)

The host runs handover 0.1.88. Its `read_registry` refuses the whole registry over the unknown key `install.format`, and the running 0.1.88 builds `mcp-servers` even in a set that brings handover 0.1.90. The build also gives `/var/lib/mcp/browser` to `mcp-browser`. So registry#78 takes `mcp/browser/server.toml` out of main, and the order is:

1. Merge registry#78. Tap the next set (handover 0.1.90 and the rest, `mcp-servers` without the browser).
2. Run host steps 1-4 of section 7 (Chrome, auto-updates, the `mcp-browser` user, the fence).
3. A registry PR brings `mcp/browser/server.toml` back unchanged. Tap the `mcp-servers` redeploy it files.
4. Host step 6 (the chaperone drop-in with B6, the manifest, the restart). Then the grant PR and the proof.


State on 2026-11-14 23:40 UTC: steps 1 and 3 are done (release `01M7E18F`, then the `mcp-servers` redeploy `01M7E5E6` with `browser` live at `/opt/mcp/browser/bin/agent-browser`). Host steps 1, 2, 4 and 6 are NOT done. `browser-host.sh` stopped at `apt-get update` (a dead `coral-edgetpu` repo), so Chrome is not installed, the fence is off and the chaperone is untouched. The operator then asked for the VM option (section 10). Host steps stay on hold until he chooses.

## 10. The VM option: the browser upstream inside a CH microVM (2026-11-14, DECIDED)

The operator asked whether Chrome can run in a VM for a stronger sandbox. It can. This section describes a third shape, between section 3 (host) and section 4 (delegate family). Principal still calls the `browser` tools directly, but Chrome and agent-browser run in their own Cloud Hypervisor VM.

### 10.1 What the host plan leaves open

1. Chrome runs with `--no-sandbox` (B3). A renderer exploit gets code execution as `mcp-browser` on the host's own kernel. A kernel bug is then one step from root, `secrets.enc.env` and handover. Chrome renderer bugs are the browser bugs most often exploited in the wild.
2. The B6 allow line widens the syscall filter of every chaperone upstream, because they share one unit.
3. The LAN fence is an IP rule per uid. It does not see names, so a rebinding name passes until its private address is dropped.

### 10.2 The shape

```
principal VM ─vsock─ vmm ─TCP─ chaperone ─stdio─ browser-relay (uid mcp-browser)
                                                    │ unix socket, new
                                                    ▼
                                                   vmm ─vsock 1202─ browser VM
                                                                     guest-init → agent-browser mcp (uid browser)
                                                                     Chrome, sandbox ON
                                                                     egress: vmm CONNECT proxy, public names, 443 only
```

1. **`browser-relay`** is the upstream's entrypoint. It is a byte copier: stdio on one side, a unix socket to the vmm on the other. It parses no MCP. The chaperone runs it as `mcp-browser`, as today.
2. **The vmm** opens a second socket for this one route, in a root-made directory `/run/creche-browser` (`<user>:mcp-browser`, 0750). The route boots or restores the browser VM, then splices the socket to guest vsock port 1202. The 0700 `vmm.sock` stays operator-only.
3. **The browser VM** boots a new disk flavor `browser` (Chrome from Google's apt repo at build time, agent-browser at its pinned sha256). guest-init starts `agent-browser mcp --tools core` as the non-root guest user `browser`, with its stdio on vsock port 1202. Chrome keeps its own sandbox, because the guest allows user namespaces and seccomp-bpf.
4. **Egress** goes through the vmm's CONNECT proxy with `--proxy-server`. Chrome then sends names, not addresses, so the guest needs no DNS. The vmm resolves each name and refuses it if any record is not public (`egress/rule.rs:120-130`, decision CH69 rule 8). This rule is per VM kind and lives in the vmm. It does not change contract 01 §3.7, which governs family files only.

### 10.3 What it removes and what it costs

| | Host plan (section 3) | VM option |
|---|---|---|
| Chrome sandbox | off (`--no-sandbox`) | on |
| A renderer exploit lands in | The host's kernel, as `mcp-browser` | the guest kernel. Reaching the host needs a CH escape too |
| Chaperone unit | widened for every upstream (B6) | unchanged: no B6 line, no `ReadWritePaths`, no fence `Requires=` |
| LAN reach | nftables uid rule (B2) | vmm proxy: name check and address check, as for families |
| Chrome updates | unattended-upgrades, no tap | a `disks` release, then one tap |
| Cookies and profile | in `/var/lib/mcp/browser` | thrown away when the VM stops (V4) |
| Build left | host steps 1, 2, 4, 6 | a spike, then about 5 PRs and one host step |

The chaperone never restarts a dead upstream (chaperone design.md:343). The relay has to handle that: if the VM dies, the relay asks the vmm for a new one on the next call instead of exiting. A restore takes 113 ms on the host (CH design note fact 88).

### 10.4 Gaps to build (research 2026-11-14, file:line in creche)

1. **A service VM kind in the vmm.** Every route, slot and shape is keyed to a family session today (`create` answers `unknown_family` with no caregiver `config/`). The browser VM needs its own kind: no facade, no session row, one instance, its own memory and vCPU shape.
2. **The relay route and socket.** No MCP-over-vsock bridge exists. The nearest is the `acp` Upgrade, which copies bytes after 101 and parses none. The chaperone unit blocks `/dev/kvm` (`PrivateDevices=yes`) and `AF_VSOCK`, so the vmm has to broker the link. The unit is not loosened.
3. **guest-init runs one fixed program as root** (`/usr/local/bin/creche-facade`). It needs a mode that runs a named program as a non-root user on a vsock port. `ch_defconfig` has not been checked for `CONFIG_USER_NS` and `CONFIG_SECCOMP_FILTER`. If either one is missing, Chrome's sandbox cannot start, and the guest kernel would need its own build.
4. **The `browser` disk flavor.** It adds a Dockerfile target and a row in `disks`. It also needs a CI job that rebuilds the flavor when a new Chrome stable appears, so each Chrome security fix arrives as one tap.
5. **Egress for the service kind.** The vmm proxy takes only `CONNECT`, so HTTPS works and plain `http://` gets 405. Allowing port 80 would need absolute-form `GET` forwarding (V2).

The registry file changes too: the entrypoint becomes `browser-relay`, and the `github-release` install of agent-browser moves into the disk. The 26 declared tools stay the same, and so does the grant PR.

### 10.5 Build order

1. **Spike on the host (as <user>, no sudo, scratch dir).** Build a rootfs from a Dockerfile target and boot it with the installed `cloud-hypervisor`, with no network. It answers three questions:
   1. Does the guest kernel allow Chrome's sandbox (user namespaces, seccomp-bpf)?
   2. Does a page load through a CONNECT proxy on the host?
   3. Does agent-browser over vsock answer `tools/list`?
   4. Does `agent-browser mcp` start its stream server, headless (10.6)? Read `stream status --json` and one frame.

   The spike decides gap 3 and the live view's source before any PR.
2. **creche:** the `browser` disk flavor and the guest-init program mode (gaps 3 and 4).
3. **creche:** the vmm service kind, the relay route and socket, and the 443-only public rule (gaps 1, 2 and 5).
4. **creche:** `browser-relay`, built as part of the `vmm` component.
5. **private-docs:** a contract section for the service VM kind and its egress rule.
6. **registry:** the server file with the relay entrypoint. The `mcp-servers` tap and the `disks` and `vmm` taps follow it.
7. **Host (sudo, once):** a `tmpfiles.d` line for `/run/creche-browser`. Remove the Google key and `google-chrome.list` that `browser-host.sh` left.
8. **registry:** the grant PR, then the proof (`example.org` snapshot; `http://192.0.2.11:1880` refused by the proxy).
9. **creche:** the vmm viewer route and the read-only frame filter (10.6).
10. **Panda:** the live-view panel, behind the same login as the chat. Proof: The operator watches principal open `example.org`, and an input message sent from the panel never reaches the guest.

Kept from the host plan: #565's `install.format = "binary"` (it stays useful), the `mcp-browser` user (the relay runs as it), the 26-tool list (B4) and the content boundary and output cap settings. `/opt/mcp/browser` goes when the registry file changes.

### 10.6 Live view in Panda (the operator, 2026-11-14: add to the plan)

The operator wants to watch the browser in Panda while the agent acts.

1. **Source.** agent-browser starts a WebSocket stream server per browser session (docs: agent-browser.dev/streaming). It sends JSON messages: `frame` (a base64 JPEG from Chrome's screencast, with viewport metadata), `url` and `status`. `AGENT_BROWSER_STREAM_PORT` fixes the port, and a client can send `{"type":"config","maxFps":10}`. Not yet checked: that the stream server runs under `agent-browser mcp` in 0.43.0 (spike question 4). If it does not, the fallback is a small guest program that runs Chrome's `Page.startScreencast` over CDP itself.
2. **Path.** In the guest, the stream leaves on its own vsock port, 1203, apart from the MCP port 1202. The vmm serves it on a viewer route. Panda reaches that route through the same host path and the same Authentik login as the chat (<chat-domain>).
3. **Read-only.** The stream accepts `input_mouse`, `input_keyboard` and `input_touch` from the viewer. A click from the viewer would act on the page outside the chaperone's grants. So the vmm forwards only `frame`, `url` and `status` messages to the viewer, plus `config` and `ack` back to the guest. It drops every other message and logs each drop. Takeover (V6) is a later, separate decision.
4. **No model cost.** The frames never enter the agent's context, so they cost no tokens and do not touch the chaperone's image refusal (fact 3).
5. **One browser.** The chaperone runs one `browser` upstream, so every principal session shares one browser VM. The panel shows that browser, not one per conversation.
6. **Panda.** Panda is our repo (la-creche/Panda). It gains a panel that draws the frames on a canvas and shows the URL. `ToolCallCard.tsx:230` already renders images, but a live panel needs its own component.

Cost on top of the VM option: the vmm viewer route and the filter, the Panda panel, and the auth wiring. That is likely 2 to 3 PRs (build order steps 9 and 10).

### 10.7 Decisions (the operator, 2026-11-14: all as recommended)

- **V1, shape:** the browser upstream inside a service VM (recommended), the host plan of section 3 as is, or the delegate family of section 4.
- **V2, ports:** HTTPS only, through `CONNECT` (recommended: no proxy change, and plain-HTTP pages fail), or ports 80 and 443 (adds `GET` forwarding to the proxy).
- **V3, Chrome updates:** a CI job rebuilds the `browser` disk on each new Chrome stable, one tap each (recommended), or rebuilds by hand.
- **V4, browser state:** a fresh profile each time the VM starts, nothing saved (recommended), or a saved profile on a session disk (logins persist, and so would a page's tracking).
- **V5, meanwhile:** hold the host steps until the VM option lands (recommended), or run the host plan now as a stopgap and remove it later.
- **V6, live view (the operator, 2026-11-14: in the plan):** read-only, the vmm drops every viewer input (recommended), or allow takeover from the panel. Takeover would need its own design: who may click, and how the agent learns the page changed under it.
