# 03 — Channel protocol

Status: draft 23
Version: 0.19.0

Minor 13 adds `stop_ack`, the field `request` of `stop_process`, and
`process_stopped`. Minor 14 adds the two plane URLs of section 7.1 and the
reason `plane_url_invalid`. Minor 15 drops `env_epoch` and
`stale_credentials`. Minor 16 drops `supervisor.lock`, `lock_beat_s`,
`host_deadline_s` and the reason `control_mount_unwritable`. Minor 17
lets guest init listen on two vsock guest ports that only the vmm reaches
(§1 rule 1). Minor 18 drops the family's `max_resident_processes` from
`hello` (§3, §6 rule 8). Minor 19 drops the path of the previous playpen (decision CH10-8).
§1 loses the `exec` shape, and each section that only the playpen of
the previous playpen used leaves. The contract keeps the line framing (§2), the
size limits (§8), the credential file (§12) and the rule that the host
trusts no byte from a VM (§13). §8 and §13 lose the rows and rules that
only pi rpc used, and §13 states its rules for ACP lines. Each kept section
keeps its number. §15 lists the sections and rules that left and where
their rules went.

Amended on 2026-11-04 by decision CH5b and fact 36 of the design note
`docs/design/cloud-hypervisor-playpen.md`. No
message carries `env_epoch`, and no reader compares the `epoch` of
`creds.json` with anything (§12).

Amended on 2026-11-08 by decisions CH13 and CH14 of the same design note.
Guest init listens on vsock, and the vmm dials in (§1 rule 1).

`docs/invariants.md` lists each rule of this contract that the code does
not hold yet.

The rules that each line stream between the host and a VM shares.

## 1. Scope and shape

One VM serves one session, and the vmm owns each VM (decision CH1). The
host reaches a VM only through the vmm. The facade in the VM answers ACP
(contract 02 §5).

Rules that fix this shape:

1. No ingress from the network. A VM has no network card, so it has no
   port on the LAN and no ingress token (decision CH8-1). Since minor 17,
   guest init listens on two vsock guest ports, fixed in the wire crate as
   `creche_wire::GuestPort`: 1200 for the control link and 1201 for ACP
   (decisions CH13, CH14). Only the vmm reaches them, through the vsock
   socket of its own Cloud Hypervisor process. The host still reaches in:
   the vmm opens one control link, and one ACP link per door connection,
   which guest init splices to the facade relay socket. No new port reaches
   the host, so the host ports of CH8-1 stay as they are. A renumber of a
   guest port bumps `creche_wire::GUEST_INIT_PROTOCOL`, so an old snapshot
   cold-boots (CH14).
2. **ACP travels as lines.** Each ACP stream between `attendance` and the
   facade carries one JSON-RPC message per line. §2 frames each line, and
   `MAX_LINE_BYTES` caps it in both directions. The control link and the
   local link of guest init carry the lines of `creche_wire`, under their
   own cap `CONTROL_LINE_CAP`, 64 KiB (decision CH43 rule 1).
3. **The host side needs no Node.** The host has no `node` on its PATH, so
   `attendance` is a Python service. The facade runs inside the VM, whose
   root disk carries Node, so only the host side is constrained.
4. The host trusts no byte from a VM (§13).

## 2. Framing

1. LF (`\n`) is the only record delimiter, in both directions.
2. Each record is one strict JSON text whose value
   is an object. The byte cap of rule R1 is `MAX_LINE_BYTES` (rule 5).
3. A reader splits on `\n` only. It strips one optional trailing `\r`.
4. A reader must not use a generic line reader. Node's `readline` is not
   protocol-compliant here, because it also splits on U+2028 and U+2029, which
   are legal inside JSON strings (pi rpc doc, framing).
5. `MAX_LINE_BYTES` is 1048576 (1 MiB), measured after UTF-8 encoding and not
   counting the LF. The limit applies in both directions.
6. A sender never writes a line over the limit. §8 says what the facade
   writes instead.
7. A receiver that reads a line over the limit refuses it. §13 says what
   follows.

## 8. Size limits and what does not travel on the channel

| Item | Limit | Over the limit |
|---|---|---|
| any line, either direction | 1 MiB | §13 refuses it |
| `prompt` | 256 KiB | `attendance` rejects the turn at the door with `payload_too_large` |
| `persona` | 16 KiB | `attendance` truncates, contract 02 §11 |
| a name: `config_rev`, a nonce, `model` | 200 bytes | |
| a path inside the VM | 4096 bytes | |
| a reason name | 64 bytes | |
| an integer of a host line | 0 to 9007199254740991 | |

Each cap counts bytes of UTF-8, on both sides. 4 KiB is 4096 bytes. A cut ends
on a whole character.

The facade never writes a line over the limit, and never a line that is not
a strict JSON text. When the answer to a request
fails either check, the facade answers that request with a JSON-RPC error
of the same id. It drops each other message that fails, with a line on its
stderr (`facade/src/acp.ts` `strictWriter`).

Two payloads never travel on the channel at all:

1. **Credentials.** §12.
2. **Tool results from upstreams.** Those go between the facade and the chaperone
   directly, never through the host channel.

## 12. How the family key and token reach the facade, never on argv

One LiteLLM key and one chaperone token per family.

1. `caregiver` mints both (contract 05 §6.2).
2. It writes them to
   `/srv/agents/state/creche/families/<family>/creds/creds.json`, mode 0600, by
   writing a temp file in the same directory and calling `rename()`. `rename`
   is atomic. Unlink plus recreate is not.
3. The file carries `epoch`, an integer from 1 to 9007199254740991 that
   `caregiver` writes (contract 05 section 6.1). Every other reader ignores
   it. A rewrite can keep the epoch or lower it, so the epoch cannot order two
   writes (decision CH5b, fact 36).
4. The vmm reads `creds.json` and serves `litellm_key` and `pep_token` to
   the facade on the family-state pull. The facade pulls before each
   `session/prompt` (decision CH5a). So a rotation needs no new VM and no
   new link. `epoch`, `written_at` and the previous token stay on the host
   (decision CH23, `FamilyCreds` in `creche-wire`).
5. The vmm reads `creds.json` once per pull, with no retry, because
   `caregiver` replaces it with one rename (rule 2). A missing file answers
   `creds_missing` at once. Only the read of the `config/` directory
   retries, up to `READ_TRIES` (5) times, `RETRY_PAUSE` (10 ms) apart
   (decision CH50 rule 9, `rust/crates/vmm-confine/src/family.rs`). The
   two reads have no cross-check, so a `caregiver` write between them gives
   a mixed reply that the next pull fixes (decision CH62 rule 21).
6. A credential never rides on argv. argv is world-readable through
   `/proc/<pid>/cmdline` for the whole life of the process. The facade
   sends the token as a bearer on each request (`agent/src/chaperone.ts`).
7. Minor 19 drops this rule. It held a property of pi rpc, and no pi rpc
   process runs (§15). The number stays, so later rules keep theirs.
8. No ACP line carries a credential or an epoch. Only the family-state
   pull of rule 4 carries the key and the token.
9. On rotation, `caregiver` writes a new file. The facade gets the new key
   and token on its next pull, before the next prompt (rule 4). A turn that
   started before the rotation keeps the values of its own pull. When
   `caregiver` deletes the old key or the old token (contract 05 §6.3), the
   calls of that turn fail.

```json
{"epoch": 7, "litellm_key": "sk-...", "pep_token": "...", "written_at": "2026-09-18T19:20:00Z"}
```

Upstream credentials (GitHub, Kagi, Vikunja) never enter a VM at all. They
live at the chaperone (invariant 13). The family key and the family token are not
upstream credentials. They are the VM's own scoped authority, and the
daily budget caps what a leaked key can spend.

## 13. The host treats every byte from a VM as untrusted

The agent process is untrusted and enforcement sits outside the sandbox
(invariant 12). The facade runs inside the VM, so it is untrusted too.

1. **Shape.** Every ACP line from a VM must be a strict JSON text of at
   most `MAX_LINE_BYTES`, hold no line break, and
   be an object with `"jsonrpc": "2.0"`. `attendance` checks this envelope
   before it reads any other field (`attendance/src/attendance/acp_door.py`
   `check_frame`, `acp_link.py`).
2. **A refused line ends the stream.** The host never drops a bad line and
   reads on. On the ACP door, a refused line closes the door connection,
   with WebSocket code 1009 for a line over the cap and 1007 for each other
   refusal. On the link of a job, a refused line ends the link, and the turn
   fails with `channel_lost` (contract 02 §14).
3. **Addressing.** Each ACP stream serves one session (decision CH33 rule
   7), so a line reaches only the door connection or the job of that
   session. On the link of a job, a line must answer a request that the
   link sent, or be a `session/update` of the session of the link. Any
   other line ends the link (rule 2).
4. Minor 19 drops this rule. It checked the `turn_seq` of pi rpc, and no
   pi rpc process runs (§15). The number stays, so later rules keep theirs.
5. **No interpretation.** The host relays or records an ACP line. It never
   acts on one. The host never opens a path, never fetches a URL and never
   executes a string that came from a VM. Text inside a line carries no
   authority, the same way a folder persona carries none (contract 02 §11).
6. Minor 19 drops this rule. It capped a wrapped pi event (§15).
7. **Advisory numbers.** `usage` from a VM is advisory. LiteLLM is the
   authority for spend (contract 05 §7).
8. Minor 19 drops this rule. It checked the `turn_settled` of pi rpc
   (§15). Under rule 3, only the answer to a request that the link sent
   ends that request.
9. **No grants.** Nothing on this channel grants anything. The chaperone decides
   every tool call from the family's current grants, outside the VM
   (invariants 11, 12).

## 15. Sections that left

At minor 19, the sections below left. A citation of one of them, in this
contract or in another, names the text of minor 18. Each one served the
playpen of the previous playpen: one `exec` per family sandbox, the
`agent-supervisor` playpen and its `pi --mode rpc` processes. Decision
CH10-8 retires that playpen, and no code of the Cloud Hypervisor path speaks them.

| Minor 18 | What it held | Where its rules went |
|---|---|---|
| §3 | the handshake `ready` and `hello` | the vmm sends `ControlHello` on the control link, and guest init answers `ControlReady` (`creche_wire`, decision CH13) |
| §4, §5 | the messages of the host and of the playpen | ACP (contract 02 §5). The facade answers each ACP method |
| §6 | one pi process per session, the hold rule and the reap rule | gone: one VM serves one session (decision CH1), and the vmm admits each VM (contract 01 §3.9) |
| §7, §7.1 | the per-turn environment, the env file and its mounts | the facade reads fixed guest paths and pulls the family state from the vmm (decisions CH5a, CH5e). No env file and no mount exists |
| §7.2 | the `code-sandbox` link | gone with `files`, contract 01 minor 18 |
| §7.3 to §7.7 | the chaperone bridge, the per-turn file, the process record, `agent-pi-launch` and the tool state file | gone with the pi processes of the playpen and the control directory (decisions CH5d, CH10-8). The facade calls the chaperone itself (`agent/src/chaperone.ts`) |
| §9 | the backpressure of one pipe for every session of a family | gone: each session has its own ACP stream (decision CH33 rule 7) |
| §10 | the `exec` that kept the VM awake | gone: the vmm starts and stops each VM (decisions CH1, CH19) |
| §11 | what the host and the playpen do when the channel drops | the vmm owns the stop of the facade (decisions CH5d, CH19) |
| §14 | the results of probe 0a, on the previous playpen | gone: each result measured `exec` or pi rpc. §14.1 item 6 is §1 rule 3 now |

Minor 19 also drops the parts of the kept sections that only pi rpc used. A
citation of one of them names the text of minor 18, as above.

| Minor 18 | What it held | Where its rules went |
|---|---|---|
| §8, five rows, one name and three paragraphs | the caps of a wrapped pi event, a `log` message, `MAX_ENTRY_BYTES`, `MAX_ENTRIES_PER_READ` and a `steer` message, a pi entry id as a name, and how the playpen cut a pi event | gone with pi rpc. §8 says what the facade writes in place of a line it refuses |
| §13 rules 1 to 3 | a `type` from §5's table, a refusal budget of ten lines in 60 seconds, and the `session` and `turn` of a pi message | §13 rules 1 to 3 now hold the ACP envelope, the end of the stream on the first refused line, and the session of each stream |
| §13 rules 4, 6 and 8 | `turn_seq`, the cap of a wrapped pi event, and `turn_settled` | gone with pi rpc. ACP has no sequence number, and the answer to `session/prompt` ends a turn (§13 rule 3) |
