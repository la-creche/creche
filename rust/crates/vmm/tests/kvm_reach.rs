//! The kvm_ tests of phase 7 egress, in two groups, and the kvm_ test of
//! the digests follow, which shares the real `vmm serve` of the second.
//!
//! The E3 tests prove the edit follow (plan PR E3, decisions CH49 rules 3
//! and 4, CH69 rules 11 and 28). They are the spike of CH49 rule 4: does
//! Cloud Hypervisor see a host listener that the vmm binds after a boot,
//! and after a restore? The follow assumes that CH connects each guest
//! dial to `vsock.sock_<port>` at the dial (decision CH24), so it binds
//! 1104 on a running VM. A red test here means the fallback of CH49 rule
//! 4: the bound set fixed at each boot and restore. Each E3 test boots
//! `examples/phase3_vm.rs` with the helpers of `tests/common/mod.rs`, the
//! real guest init and the stand-in facade in mode `reach` (plan PR A1).
//! The family has no `egress.json` at the start. A 1201 link sends `dial`
//! lines, so the stand-in opens guest TCP to 127.0.0.1:3128, which guest
//! init splices to host port 1104 (decision CH46 rule 1). The CONNECT
//! names an IP literal, which the proxy refuses with 403 before any lookup
//! (decision CH6-4), so no E3 test needs the internet. With no 1104
//! listener the guest dial fails, and guest init closes the stream with no
//! reply (decision CH49 rule 3).
//!
//! The E4 tests are the phase 7 proof on the real `vmm serve` (plan PR
//! E4, decision CH69 rule 31). The example of E3 binds no `vmm.sock`, so
//! it serves no `check` and no Upgrade. Each E4 test starts `vmm serve`
//! under `systemd-run` with the env of `boot_with`, creates the session
//! over `vmm.sock` and sends the lines of mode `reach` on its `acp`
//! Upgrade, as a door client does. The E4 tests that tunnel to
//! github.com:443 need the internet and DNS on the runner (decision CH69
//! rule 22, plan risk 4).
//!
//! The V1 test proves the digests follow of the real `vmm serve` (plan PR
//! V1, decisions CH7-6, CH58 rule 5, CH76 rule 8, CH77 rule 2). A disks
//! release replaces `digests.json` while the vmm runs. The follow reads
//! the root-owned file, waits `HOOK_TIMEOUT` plus one `POLL`, imports the
//! new root disk and swaps the set of new starts. The vmm log line of the
//! swap names the new set. A snapshot of a VM on the old disk holds the
//! old key, so the next start drops it and cold-boots on the new disk. The
//! VM sleeps on the idle timeout of `vmm serve`, 300 s, while the follow
//! waits its 305 s. So the job kvm waits about 5 min once for both
//! proofs, and the test proves that the compose step runs the follow. The
//! rust job proves the follow on the bytes
//! (`src/image_root/follow/tests.rs`), because only the runner's sudo
//! makes a root-owned file (decision CH36 G20).
//!
//! `#[ignore]`: only the job `kvm` runs them, after the setup script, as
//! `tests/kvm_guest.rs` says.
#![cfg(target_os = "linux")]

mod common;
mod stand_in;

use std::ffi::OsStr;
use std::fs;
use std::io::{self, BufRead as _, BufReader, Read as _, Write as _};
use std::os::unix::net::UnixStream;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::mpsc::{self, Receiver};
use std::thread::{self, sleep};
use std::time::{Duration, Instant};

use creche_vmm::image_root::DIGESTS_FILE;
use creche_vmm::image_root::follow::{HOOK_TIMEOUT, POLL};
use creche_vmm::mirror::DENIED;
use creche_vmm::sleep::IDLE_CHECK;
use creche_vmm_store::claim::CONSUMED;
use creche_vmm_store::layout::RunRoot;
use creche_vmm_store::sleep_gate::IDLE_TIMEOUT;
use creche_wire::guest::{EGRESS_PROXY_PORT, MIRROR_PORT};
use creche_wire::{
    AcpRoute, CheckReason, CheckRefused, CheckReply, CheckRoute, CreateRoute, FacadeState,
    GuestPort, HOST_API_HEADER, HOST_API_PROTOCOL, HostPort, JsonRoute, SessionId, UpgradeRoute,
};
use serde_json::{Value, json};

use common::{
    BOOT, Booted, FAMILY, HOST_GIT, SESSION, SYSTEMCTL, UP, boot_with, family_root, lines, sha256,
    sudo, transient, work,
};
use stand_in::{SMALL, boot, ext4, stand_in_disk, stub_plane};

/// A CONNECT to an IP literal of TEST-NET-1 (RFC 5737).
const CONNECT: &[u8] = b"CONNECT 192.0.2.1:443 HTTP/1.1\r\nHost: 192.0.2.1:443\r\n\r\n";

/// The start of the first line of a refusal of the proxy.
const FORBIDDEN: &[u8] = b"HTTP/1.1 403 ";

/// An allowlist with one host, in the canonical form of caregiver
/// (decision CH69 rule 5).
const LIST: &str = r#"{"hosts":[{"host":"github.com","port":443}]}"#;

/// An allowlist with no host, which binds no 1104 (decision CH49 rule 3).
const EMPTY: &str = r#"{"hosts":[]}"#;

/// The pause between two tries of a CONNECT that waits for the follow.
const RETRY: Duration = Duration::from_millis(200);

// Each helper with `cfg(test)` may unwrap (rust/clippy.toml).

/// `bytes` in the lowercase hex of mode `reach`, or `-` for no bytes.
fn hex(bytes: &[u8]) -> String {
    if bytes.is_empty() {
        return "-".to_owned();
    }
    bytes.iter().map(|byte| format!("{byte:02x}")).collect()
}

/// A 1201 link to the stand-in in mode `reach`: `CONNECT 1201` on the
/// vsock socket of CH, as `AcpLink::dial` dials (decision CH13).
struct Link {
    reader: BufReader<UnixStream>,
    stream: UnixStream,
    next: usize,
}

#[cfg(test)]
impl Link {
    /// Opens the link on `vsock`. A fault fails the proof `step`.
    fn open(vsock: &Path, step: &str) -> Self {
        let stream = UnixStream::connect(vsock).unwrap();
        stream.set_read_timeout(Some(BOOT)).unwrap();
        writeln!(&stream, "CONNECT {}", GuestPort::Acp.number()).unwrap();
        let mut link = Self {
            reader: BufReader::new(stream.try_clone().unwrap()),
            stream,
            next: 0,
        };
        let ok = link.line();
        assert!(ok.starts_with("OK "), "{step}: CONNECT gave {ok:?}");
        link
    }

    /// One answer line. Only a line that ends in its LF counts.
    fn line(&mut self) -> String {
        let mut line = String::new();
        self.reader.read_line(&mut line).unwrap();
        assert!(line.ends_with('\n'), "the 1201 link ended: {line:?}");
        line.trim_end().to_owned()
    }

    /// Sends `CONNECT` on a new guest stream to 127.0.0.1:3128 and
    /// whether the first reply line is a 403 of the proxy. A stream that
    /// stays open is closed again, so the link holds none.
    fn refused(&mut self) -> bool {
        self.next += 1;
        let id = format!("c{}", self.next);
        writeln!(
            &self.stream,
            "dial {id} {EGRESS_PROXY_PORT} {}",
            hex(CONNECT)
        )
        .unwrap();
        let answer = self.line();
        let words: Vec<&str> = answer.split(' ').collect();
        if words.last() == Some(&"open") {
            writeln!(&self.stream, "close {id}").unwrap();
            assert_eq!(self.line(), format!("closed {id}"));
        }
        match words.as_slice() {
            ["reply", got, bytes, _] if *got == id => bytes.starts_with(&hex(FORBIDDEN)),
            ["failed", got, _] if *got == id => false,
            _ => panic!("no answer of mode reach: {answer:?}"),
        }
    }

    /// Tries a CONNECT every `RETRY` until its refusal is `refused`,
    /// within `BOOT`. The follow polls every 2 s (decision CH69 rule 11).
    fn until(&mut self, refused: bool, step: &str) {
        let deadline = Instant::now() + BOOT;
        while self.refused() != refused {
            assert!(Instant::now() < deadline, "{step}: no change in {BOOT:?}");
            sleep(RETRY);
        }
    }
}

/// The `egress.json` of the family of the proof `name`.
#[cfg(test)]
fn egress_file(name: &str) -> PathBuf {
    family_root(&work(name)).join(FAMILY).join("egress.json")
}

/// Boots the proof `name` with the stand-in in mode `reach` and stub
/// planes that no step reads.
#[cfg(test)]
fn boot_reach(name: &str) -> Booted {
    let (litellm, _) = stub_plane();
    let (chaperone, _) = stub_plane();
    boot(name, "reach", (litellm, chaperone))
}

/// After a cold boot with no egress list, a CONNECT gets no 403: no
/// listener exists. An edit that lists a host binds 1104 on the running
/// VM, and CH connects the next guest dial to it: the CONNECT gets 403
/// (decisions CH49 rules 3 and 4, CH69 rule 28). An edit that empties the
/// list removes it again.
#[test]
#[ignore = "needs /dev/kvm and bin/vm-smoke-setup.sh: the job kvm runs it"]
fn kvm_reach_binds_1104_after_an_egress_edit() {
    let name = "reach-boot";
    let mut booted = boot_reach(name);
    booted.vmm.start_vm("step 1 cold boot");

    // 2. No list, no 1104: guest init closes the stream with no reply.
    let mut link = Link::open(&booted.vsock, "step 2 no list");
    assert!(!link.refused(), "step 2 no list: a 403 with no listener");

    // 3. The follow binds 1104 after the edit, and CH finds it.
    fs::write(egress_file(name), LIST).unwrap();
    link.until(true, "step 3 edit after a boot (CH49 rule 4)");

    // 4. The follow removes 1104 after the list empties.
    fs::write(egress_file(name), EMPTY).unwrap();
    link.until(false, "step 4 emptied list (CH49 rule 3)");
    drop(link);
    drop(booted);
}

/// After a restore of a VM with no egress list, an edit that lists a host
/// binds 1104, and CH connects the next guest dial to it: the CONNECT
/// gets 403 (decisions CH49 rule 4, CH69 rule 28, plan risk 6).
#[test]
#[ignore = "needs /dev/kvm and bin/vm-smoke-setup.sh: the job kvm runs it"]
fn kvm_reach_binds_1104_after_a_restore_and_an_edit() {
    let name = "reach-restore";
    let mut booted = boot_reach(name);
    booted.vmm.start_vm("step 1 cold boot");

    // 2. The facade of mode `reach` opens its local link, so the sleep
    // gate passes (decision CH32 rule 6).
    let deadline = Instant::now() + BOOT;
    loop {
        let answer = booted.vmm.ask("facade");
        let facade = answer.strip_prefix("facade ").unwrap();
        if let Ok(FacadeState::Running(_)) = serde_json::from_str::<FacadeState>(facade) {
            break;
        }
        assert!(Instant::now() < deadline, "step 2 running: {answer}");
        sleep(Duration::from_millis(100));
    }

    // 3. Sleep, then a restore that claims the one generation. The new
    // serial log has no `up` line, which a cold boot writes.
    assert_eq!(booted.vmm.ask("sleep"), "slept 1", "step 3 sleep");
    let generations: Vec<PathBuf> = fs::read_dir(booted.session.snapshots())
        .unwrap()
        .map(|entry| entry.unwrap().path())
        .collect();
    assert_eq!(generations.len(), 1, "step 3 sleep: {generations:?}");
    booted.vmm.start_vm("step 3 restore");
    assert!(
        generations[0].join(CONSUMED).is_file(),
        "step 3 restore: no claim"
    );
    assert!(
        !lines(&booted.session.serial_log())
            .iter()
            .any(|line| line == UP),
        "step 3 restore: the VM booted"
    );

    // 4. No list, no 1104 after the restore.
    let mut link = Link::open(&booted.vsock, "step 4 no list");
    assert!(!link.refused(), "step 4 no list: a 403 with no listener");

    // 5. The follow binds 1104 after the edit, and the restored CH finds
    // it.
    fs::write(egress_file(name), LIST).unwrap();
    link.until(true, "step 5 edit after a restore (CH49 rule 4)");
    drop(link);
    drop(booted);
}

// The E4 tests on the real `vmm serve` (plan PR E4, see the module doc).

/// The allowed host of decision CH69 rule 22.
const GITHUB: &str = "github.com";

/// A host that stays listed when a removal takes github.com out. No test
/// dials it.
const KEPT: &str = "example.com";

/// The first line of the 200 of an allowed CONNECT. The blank line of
/// the head follows it.
const ESTABLISHED: &[u8] = b"HTTP/1.1 200 Connection established\r\n";

/// The time that a held tunnel waits so a poll of the follow passes: 2 s
/// (decision CH69 rule 11) and a margin.
const FOLLOW: Duration = Duration::from_secs(5);

/// The listed repo and a repo that the mirror holds and the list does not
/// name.
const LISTED: &str = "creche";
const UNLISTED: &str = "other";

/// `target` as `host:443`.
fn on_443(host: &str) -> String {
    format!("{host}:443")
}

/// A CONNECT head to `target`.
fn connect(target: &str) -> Vec<u8> {
    format!("CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n").into_bytes()
}

/// An allowlist of `hosts` on port 443, sorted as caregiver writes it
/// (decision CH69 rule 5).
fn list(hosts: &[&str]) -> String {
    let hosts: Vec<Value> = hosts
        .iter()
        .map(|host| json!({"host": host, "port": 443}))
        .collect();
    json!({ "hosts": hosts }).to_string()
}

/// `bytes` after their length in 2 bytes, big endian, as TLS sizes a
/// vector.
#[cfg(test)]
fn sized(bytes: &[u8]) -> Vec<u8> {
    let length = u16::try_from(bytes.len()).unwrap().to_be_bytes();
    [&length[..], bytes].concat()
}

/// A TLS 1.2 ClientHello to github.com in one record (RFC 5246): two
/// ECDHE AES-128-GCM suites, no session, the extensions `server_name`,
/// `supported_groups`, `ec_point_formats` and `signature_algorithms`. The
/// server answers it with a handshake record, so the tunnel carries bytes
/// both ways. github.com sends no byte for junk: it closes the link.
#[cfg(test)]
fn client_hello() -> Vec<u8> {
    let extension = |kind: u16, body: &[u8]| [&kind.to_be_bytes()[..], &sized(body)].concat();
    let name = [&[0][..], &sized(GITHUB.as_bytes())].concat();
    let extensions = [
        extension(0, &sized(&name)),
        extension(10, &sized(&[0x00, 0x1d, 0x00, 0x17])),
        extension(11, &[1, 0]),
        extension(13, &sized(&[0x04, 0x03, 0x08, 0x04, 0x04, 0x01])),
    ]
    .concat();
    let body = [
        &[0x03, 0x03][..],
        &[0; 32],
        &[0],
        &sized(&[0xc0, 0x2f, 0xc0, 0x2b]),
        &[1, 0],
        &sized(&extensions),
    ]
    .concat();
    let length = u32::try_from(body.len()).unwrap().to_be_bytes();
    let handshake = [&[1][..], &length[1..], &body].concat();
    [&[0x16, 0x03, 0x01][..], &sized(&handshake)].concat()
}

/// The bytes of `text` in the hex of mode `reach`, `-` for none.
#[cfg(test)]
fn unhex(text: &str) -> Vec<u8> {
    if text == "-" {
        return Vec::new();
    }
    text.as_bytes()
        .chunks(2)
        .map(|pair| u8::from_str_radix(std::str::from_utf8(pair).unwrap(), 16).unwrap())
        .collect()
}

/// The first pkt-line of a v0 clone of `repo`, as git writes it.
fn upload_request(repo: &str) -> Vec<u8> {
    let payload = format!("git-upload-pack /{repo}\0host=127.0.0.1:{MIRROR_PORT}\0");
    format!("{:04x}{payload}", payload.len() + 4).into_bytes()
}

/// One answer of mode `reach` to an action on one stream.
#[derive(Debug, PartialEq, Eq)]
enum Answer {
    /// The bytes of one read, and whether the stream stays open.
    Reply(Vec<u8>, bool),
    /// The action failed with this io error kind.
    Failed(String),
}

#[cfg(test)]
impl Link {
    /// Sends the action `line` on the stream `id` and reads its answer.
    fn act(&mut self, id: &str, line: &str) -> Answer {
        writeln!(&self.stream, "{line}").unwrap();
        let answer = self.line();
        match answer.split(' ').collect::<Vec<_>>().as_slice() {
            ["reply", got, bytes, state] if *got == id => {
                Answer::Reply(unhex(bytes), *state == "open")
            }
            ["failed", got, kind] if *got == id => Answer::Failed((*kind).to_owned()),
            _ => panic!("{id}: no answer of mode reach: {answer:?}"),
        }
    }

    /// Opens the guest stream `id` to 127.0.0.1:`port` with `bytes`.
    fn dial(&mut self, id: &str, port: u16, bytes: &[u8]) -> Answer {
        self.act(id, &format!("dial {id} {port} {}", hex(bytes)))
    }

    /// Writes `bytes` on the open stream `id`, or reads its next line.
    fn send(&mut self, id: &str, bytes: &[u8]) -> Answer {
        self.act(id, &format!("send {id} {}", hex(bytes)))
    }

    /// Closes the stream `id`.
    fn close(&mut self, id: &str) {
        writeln!(&self.stream, "close {id}").unwrap();
        assert_eq!(self.line(), format!("closed {id}"));
    }

    /// The status line of a CONNECT to `target` on the new stream `id`,
    /// which is then closed.
    fn status(&mut self, id: &str, target: &str) -> Vec<u8> {
        let Answer::Reply(line, open) = self.dial(id, EGRESS_PROXY_PORT, &connect(target)) else {
            panic!("{id}: the CONNECT to {target} failed");
        };
        if open {
            self.close(id);
        }
        line
    }

    /// Opens a tunnel to github.com:443 on the stream `id` and reads the
    /// head of the 200 to its blank line. Each read of mode `reach` ends
    /// at the first LF, so the blank line takes a read of its own.
    fn tunnel(&mut self, id: &str) {
        let first = self.dial(id, EGRESS_PROXY_PORT, &connect(&on_443(GITHUB)));
        assert_eq!(first, Answer::Reply(ESTABLISHED.to_vec(), true), "{id}");
        assert_eq!(self.send(id, b""), Answer::Reply(b"\r\n".to_vec(), true));
    }

    /// Sends the ClientHello on the tunnel `id`. A TLS 1.2 handshake
    /// record from github.com comes back.
    fn hello(&mut self, id: &str) {
        match self.send(id, &client_hello()) {
            Answer::Reply(bytes, true) if bytes.starts_with(&[0x16, 0x03, 0x03]) => {}
            other => panic!("{id}: no handshake record from {GITHUB}: {other:?}"),
        }
    }
}

/// The real `vmm serve` as a transient user service (decision CH69 rule
/// 31). A thread writes each line of its stderr, the vmm log, to the job
/// log and sends it on `log`. A drop stops the unit, so `BindsTo=` stops
/// each VM scope (decision CH61 rule 1), and reaps `systemd-run`.
struct Served {
    unit: String,
    child: Child,
    run: RunRoot,
    log: Receiver<String>,
}

#[cfg(test)]
impl Served {
    /// Starts the unit of the proof `name` with `env` and waits for
    /// `vmm.sock` in `run`. A vmm that exits first fails the proof, and
    /// its log above names the cause.
    // `vmm serve` checks the version of `VMM_CH_PROGRAM` itself and
    // accepts only `CH_VERSION`, v54.0.0 (fact 78). `bin/vm-smoke-setup.sh`
    // builds that CH at the fact 78 rev (decisions CH75, CH36 G19), so
    // this test runs the real `vmm serve` with no stand-in for the check
    // (CH69 rule 31).
    fn start(name: &str, env: &[(&str, &OsStr)], run: &RunRoot) -> Self {
        let unit = format!("kvm-serve-{name}-{}.service", std::process::id());
        let mut child = transient(&unit, env)
            .args([env!("CARGO_BIN_EXE_vmm"), "serve"])
            .stdin(Stdio::null())
            .stderr(Stdio::piped())
            .spawn()
            .unwrap();
        let stderr = BufReader::new(child.stderr.take().unwrap());
        let (lines, log) = mpsc::channel();
        thread::spawn(move || {
            for line in stderr.lines().map_while(Result::ok) {
                let _ = writeln!(io::stderr(), "{line}");
                let _ = lines.send(line);
            }
        });
        let mut served = Self {
            unit,
            child,
            run: run.clone(),
            log,
        };
        let deadline = Instant::now() + BOOT;
        while UnixStream::connect(run.vmm_socket()).is_err() {
            if let Some(status) = served.child.try_wait().unwrap() {
                panic!("vmm serve ended with {status} before it bound vmm.sock");
            }
            assert!(Instant::now() < deadline, "no vmm.sock in {BOOT:?}");
            sleep(Duration::from_millis(50));
        }
        served
    }

    /// Sends `POST /<route>` with the JSON `body` on a new link to
    /// `vmm.sock`, with the Upgrade token `upgrade` if any. It gives the
    /// status, the body and the link. After 101 the link is the relay.
    fn post(&self, route: &str, body: &str, upgrade: Option<&str>) -> (u16, String, UnixStream) {
        let mut stream = UnixStream::connect(self.run.vmm_socket()).unwrap();
        stream.set_read_timeout(Some(BOOT)).unwrap();
        let upgrade = upgrade.map_or_else(String::new, |token| {
            format!("connection: upgrade\r\nupgrade: {token}\r\n")
        });
        write!(
            stream,
            "POST /{route} HTTP/1.1\r\nhost: vmm\r\n{HOST_API_HEADER}: {HOST_API_PROTOCOL}\r\n\
             content-type: application/json\r\n{upgrade}content-length: {}\r\n\r\n{body}",
            body.len()
        )
        .unwrap();
        let mut head = Vec::new();
        while !head.ends_with(b"\r\n\r\n") {
            let mut byte = [0_u8];
            stream.read_exact(&mut byte).unwrap();
            head.push(byte[0]);
        }
        let head = String::from_utf8(head).unwrap().to_lowercase();
        let status = head.get(9..12).unwrap().parse().unwrap();
        let length = head
            .lines()
            .find_map(|line| line.strip_prefix("content-length: "))
            .map_or(0, |length| length.parse().unwrap());
        let mut reply = vec![0_u8; length];
        stream.read_exact(&mut reply).unwrap();
        (status, String::from_utf8(reply).unwrap(), stream)
    }

    /// The 200 reply of the JSON route `R` to `body`.
    fn ask<R: JsonRoute>(&self, body: &Value) -> String {
        let (status, reply, _) = self.post(R::NAME, &body.to_string(), None);
        assert_eq!(status, 200, "{}: {reply}", R::NAME);
        reply
    }

    /// The verdict of the route `check` on `target` for the family of the
    /// proof (decisions CH6-3, CH69 rule 1).
    fn check(&self, target: &str) -> CheckReply {
        let body = json!({"family": FAMILY, "target": target});
        serde_json::from_str(&self.ask::<CheckRoute>(&body)).unwrap()
    }

    /// Creates the session of the proof, as attendance does (decision
    /// CH21-4).
    fn create(&self) {
        let create = json!({
            "created_at": 1_793_577_600_000_u64,
            "cwd": "/root",
            "family": FAMILY,
            "id": SESSION,
            "origin": {"door": {}},
        });
        self.ask::<CreateRoute>(&create);
    }

    /// Opens the `acp` Upgrade of the session, which starts the VM. A
    /// drop of the stream closes the door link.
    fn acp(&self) -> UnixStream {
        let body = json!({"family": FAMILY, "id": SESSION}).to_string();
        let (status, reply, stream) = self.post(AcpRoute::NAME, &body, Some(AcpRoute::TOKEN));
        assert_eq!(status, 101, "acp: {reply}");
        stream
    }

    /// Creates the session and opens its `acp` Upgrade. The stand-in
    /// facade of mode `reach` answers on it.
    fn open(&self) -> Link {
        self.create();
        let stream = self.acp();
        Link {
            reader: BufReader::new(stream.try_clone().unwrap()),
            stream,
            next: 0,
        }
    }

    /// The socket file of the host listener `port` of the VM.
    fn listener(&self, port: HostPort) -> PathBuf {
        let id: SessionId = SESSION.parse().unwrap();
        self.run.vm(&id).host_listener(port)
    }
}

impl Drop for Served {
    fn drop(&mut self) {
        let _ = Command::new(SYSTEMCTL)
            .args(["--user", "stop", "--"])
            .arg(&self.unit)
            .status();
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

/// Starts the real vmm of the proof `name` with the stand-in in mode
/// `reach` and stub planes that no step reads. The VM starts at `open`.
#[cfg(test)]
fn serve_reach(name: &str) -> Booted<Served> {
    let (litellm, _) = stub_plane();
    let (chaperone, _) = stub_plane();
    let disk = |work: &Path, root: &Path| stand_in_disk(work, root, "reach");
    boot_with(name, disk, SMALL, (litellm, chaperone), |name, env, run| {
        Served::start(name, env, run)
    })
}

/// The first whole line of the egress log of the proof `name` that `pick`
/// takes, within `BOOT`. The proxy writes the line of a tunnel at its end
/// (decision CH69 rule 10).
#[cfg(test)]
fn logged(name: &str, pick: impl Fn(&Value) -> bool) -> Value {
    let path = work(name).join("egress.jsonl");
    let deadline = Instant::now() + BOOT;
    loop {
        let text = fs::read_to_string(&path).unwrap_or_default();
        let line = text
            .split_inclusive('\n')
            .filter(|line| line.ends_with('\n'))
            .map(|line| serde_json::from_str::<Value>(line).unwrap())
            .find(|line| pick(line));
        if let Some(line) = line {
            return line;
        }
        assert!(Instant::now() < deadline, "no such log line: {text}");
        sleep(RETRY);
    }
}

/// Asserts the log line of the one tunnel to github.com:443: the checked
/// address, `bytes_up` equal to the ClientHello and `bytes_down` above 0.
/// The guest sends far fewer bytes than github.com, so a swap of the two
/// counts fails.
#[cfg(test)]
fn assert_tunnel(name: &str) {
    let line = logged(name, |line| line["verdict"] == "allowed");
    assert_eq!(line["host"], GITHUB, "{line}");
    assert_eq!(line["port"], 443, "{line}");
    assert_eq!(line["reason"], Value::Null, "{line}");
    assert!(line["address"].is_string(), "{line}");
    assert_eq!(line["bytes_up"], client_hello().len(), "{line}");
    assert!(line["bytes_down"].as_u64().unwrap() > 0, "{line}");
}

/// The log line of a refusal for `reason` (decision CH70 rule 2).
#[cfg(test)]
fn assert_refused(name: &str, reason: CheckReason) {
    let reason = serde_json::to_value(reason).unwrap();
    let line = logged(name, |line| line["reason"] == reason);
    assert_eq!(line["verdict"], "refused", "{line}");
    assert_eq!(line["bytes_up"], 0, "{line}");
}

/// Writes the mirror of the proof `name`: the repos `LISTED` and
/// `UNLISTED` with one commit each, as the mirror sync keeps them
/// (decision CH5f), and a `repos.json` that lists `LISTED` (decision
/// CH69 rules 5 and 15). It gives the commit of HEAD of `LISTED`.
#[cfg(test)]
fn mirror(name: &str) -> String {
    let work = work(name);
    let git = |args: &[&str]| {
        let output = Command::new(HOST_GIT)
            .args(["-c", "user.name=t", "-c", "user.email=t@example.com"])
            .args(args)
            .env_clear()
            .envs([
                ("GIT_CONFIG_NOSYSTEM", "1"),
                ("GIT_CONFIG_GLOBAL", "/dev/null"),
            ])
            .env("HOME", &work)
            .env("PATH", "/usr/bin:/bin")
            .output()
            .unwrap();
        assert!(output.status.success(), "git {args:?}: {output:?}");
        String::from_utf8(output.stdout).unwrap()
    };
    for repo in [LISTED, UNLISTED] {
        let dir = work.join("mirror").join(repo);
        let dir = dir.to_str().unwrap();
        git(&["init", "--quiet", dir]);
        git(&["-C", dir, "commit", "--quiet", "--allow-empty", "-m", repo]);
    }
    let repos = json!({ "repos": [LISTED] }).to_string();
    fs::write(family_root(&work).join(FAMILY).join("repos.json"), repos).unwrap();
    let listed = work.join("mirror").join(LISTED);
    git(&["-C", listed.to_str().unwrap(), "rev-parse", "HEAD"])
        .trim_end()
        .to_owned()
}

/// An allowed CONNECT tunnels to github.com:443, and the tunnel carries
/// bytes both ways (section 5, decisions CH6-4, CH69 rule 22). Its log
/// line names the checked address and counts each way.
#[test]
#[ignore = "needs /dev/kvm, bin/vm-smoke-setup.sh and the internet: the job kvm runs it"]
fn kvm_reach_tunnels_to_an_allowed_host() {
    let name = "reach-tunnel";
    let booted = serve_reach(name);
    fs::write(egress_file(name), list(&[GITHUB])).unwrap();
    let mut link = booted.vmm.open();
    link.tunnel("t1");
    link.hello("t1");
    link.close("t1");
    assert_tunnel(name);
    drop(link);
    drop(booted);
}

/// A CONNECT to an IP literal gets 403 and an `ip_literal` line, also
/// with a list (decision CH6-4).
#[test]
#[ignore = "needs /dev/kvm and bin/vm-smoke-setup.sh: the job kvm runs it"]
fn kvm_reach_refuses_an_ip_literal() {
    let name = "reach-literal";
    let booted = serve_reach(name);
    fs::write(egress_file(name), list(&[GITHUB])).unwrap();
    let mut link = booted.vmm.open();
    assert!(link.refused(), "an IP literal got no 403");
    assert_refused(name, CheckReason::IpLiteral);
    drop(link);
    drop(booted);
}

/// `check` over `vmm.sock` gives the verdict of the proxy for each target:
/// an allowed host, an IP literal and a host that the list does not name
/// (decisions CH6-3, CH69 rules 1 and 2).
#[test]
#[ignore = "needs /dev/kvm, bin/vm-smoke-setup.sh and the internet: the job kvm runs it"]
fn kvm_reach_check_agrees_with_the_proxy() {
    let name = "reach-check";
    let booted = serve_reach(name);
    fs::write(egress_file(name), list(&[GITHUB])).unwrap();
    let mut link = booted.vmm.open();

    // 1. github.com: a tunnel, and `check` allows it.
    link.tunnel("t1");
    link.hello("t1");
    link.close("t1");
    assert_tunnel(name);
    match booted.vmm.check(&on_443(GITHUB)) {
        CheckReply::Allowed(allowed) => assert!(!allowed.addresses.is_empty()),
        refused @ CheckReply::Refused(_) => panic!("check refused {GITHUB}: {refused:?}"),
    }

    // 2. An IP literal and a host that is not listed: 403 and the reason
    // that `check` gives.
    let literal = "192.0.2.1:443";
    for (id, target, reason) in [
        ("r1", literal, CheckReason::IpLiteral),
        ("r2", &on_443(KEPT), CheckReason::NotListed),
    ] {
        assert!(link.status(id, target).starts_with(FORBIDDEN), "{target}");
        assert_refused(name, reason);
        let refused = CheckReply::Refused(CheckRefused { reason });
        assert_eq!(booted.vmm.check(target), refused, "{target}");
    }
    drop(link);
    drop(booted);
}

/// Fact 43: after an edit that removes github.com, while another host
/// stays listed, the next CONNECT to it gets 403 at once, because the
/// proxy reads the list at each CONNECT (decision CH6-4). The tunnel that
/// was open before the edit still carries bytes both ways, also after a
/// poll of the follow (decision CH69 rule 11).
#[test]
#[ignore = "needs /dev/kvm, bin/vm-smoke-setup.sh and the internet: the job kvm runs it"]
fn kvm_reach_a_removal_refuses_new_tunnels_and_keeps_open_ones() {
    let name = "reach-removal";
    let booted = serve_reach(name);
    fs::write(egress_file(name), list(&[KEPT, GITHUB])).unwrap();
    let mut link = booted.vmm.open();
    link.tunnel("t1");

    // 1. The removal refuses the next CONNECT.
    fs::write(egress_file(name), list(&[KEPT])).unwrap();
    let status = link.status("n1", &on_443(GITHUB));
    assert!(status.starts_with(FORBIDDEN), "a CONNECT after the removal");
    assert_refused(name, CheckReason::NotListed);

    // 2. A poll of the follow passes. The list is not empty, so 1104
    // stays, and the held tunnel carries bytes both ways.
    sleep(FOLLOW);
    assert!(booted.vmm.listener(HostPort::Egress).exists(), "no 1104");
    link.hello("t1");
    link.close("t1");
    assert_tunnel(name);
    drop(link);
    drop(booted);
}

/// Decision CH74 rule 2: an edit that empties `egress.json` unbinds 1104
/// and removes its socket file, so a new dial of guest 3128 fails. A
/// tunnel that was open before the edit runs to its own end, and carries
/// bytes both ways after the edit.
#[test]
#[ignore = "needs /dev/kvm, bin/vm-smoke-setup.sh and the internet: the job kvm runs it"]
fn kvm_reach_an_emptied_list_unbinds_1104_and_keeps_open_tunnels() {
    let name = "reach-emptied";
    let booted = serve_reach(name);
    fs::write(egress_file(name), list(&[GITHUB])).unwrap();
    let mut link = booted.vmm.open();
    let socket = booted.vmm.listener(HostPort::Egress);
    assert!(socket.exists(), "step 1: no 1104 at the start");
    link.tunnel("t1");

    // 2. The follow unbinds 1104 and removes its socket file.
    fs::write(egress_file(name), EMPTY).unwrap();
    link.until(false, "step 2 emptied list (CH74 rule 2)");
    let deadline = Instant::now() + BOOT;
    while socket.exists() {
        assert!(Instant::now() < deadline, "step 2: the 1104 socket stays");
        sleep(RETRY);
    }
    match link.dial("n1", EGRESS_PROXY_PORT, &connect(&on_443(GITHUB))) {
        Answer::Reply(bytes, false) if bytes.is_empty() => {}
        Answer::Failed(_) => {}
        other => panic!("step 2: a new dial of 3128 got {other:?}"),
    }

    // 3. The held tunnel still carries bytes both ways.
    link.hello("t1");
    link.close("t1");
    assert_tunnel(name);
    drop(link);
    drop(booted);
}

/// A dial of guest 9418 with the request of a listed repo gets the
/// advertisement of the mirror, whose first line names its HEAD
/// (decisions CH46 rule 4, CH69 rules 16 and 18).
#[test]
#[ignore = "needs /dev/kvm and bin/vm-smoke-setup.sh: the job kvm runs it"]
fn kvm_reach_clones_a_listed_repo() {
    let name = "reach-clone";
    let booted = serve_reach(name);
    let head = mirror(name);
    let mut link = booted.vmm.open();
    let Answer::Reply(line, true) = link.dial("g1", MIRROR_PORT, &upload_request(LISTED)) else {
        panic!("no advertisement of {LISTED}");
    };
    let line = String::from_utf8_lossy(&line).into_owned();
    assert!(
        line.get(4..)
            .unwrap()
            .starts_with(&format!("{head} HEAD\0")),
        "{line:?}"
    );
    link.close("g1");
    drop(link);
    drop(booted);
}

/// A dial of guest 9418 for a repo that the mirror holds and the list
/// does not name gets the one `ERR` pkt-line, then the close (decisions
/// CH10-10, CH69 rule 16).
#[test]
#[ignore = "needs /dev/kvm and bin/vm-smoke-setup.sh: the job kvm runs it"]
fn kvm_reach_refuses_a_repo_outside_repos() {
    let name = "reach-unlisted";
    let booted = serve_reach(name);
    mirror(name);
    let mut link = booted.vmm.open();
    let request = upload_request(UNLISTED);
    assert_eq!(
        link.dial("g1", MIRROR_PORT, &request),
        Answer::Reply(DENIED.to_vec(), true)
    );
    assert_eq!(link.send("g1", b""), Answer::Reply(Vec::new(), false));
    drop(link);
    drop(booted);
}

// The digests follow on the real `vmm serve` (plan PR V1, see the module
// doc).

/// Decisions CH7-6, CH76 rule 8 and CH77 rule 2: a VM sleeps on the old
/// disk, a new root-owned `digests.json` names a new disk, and the follow
/// of `vmm serve` swaps the set of new starts no sooner than
/// `HOOK_TIMEOUT` plus one `POLL` after the rename. The next start drops
/// the snapshot and cold-boots. A compose step that never runs the
/// follow fails here.
#[test]
#[ignore = "needs /dev/kvm and bin/vm-smoke-setup.sh: the job kvm runs it"]
fn kvm_a_disk_swap_cold_boots_a_snapshot_of_the_old_disk() {
    let name = "disk-swap";
    let booted = serve_reach(name);
    let (served, vsock, session) = (booted.vmm, booted.vsock, booted.session);

    // 1. A cold boot on the old disk. The close of the door link starts
    // the idle timeout of the VM (decision CH50 rule 5).
    served.create();
    drop(served.acp());
    let closed = Instant::now();
    assert!(
        lines(&session.serial_log()).iter().any(|line| line == UP),
        "step 1: the VM did not boot"
    );

    // 2. The disks release: a new root disk with one more file, and a new
    // `digests.json` that root owns. The rename swaps it in whole, as
    // handover swaps the disks tree.
    let work = work(name);
    let (image, tree) = (work.join("image"), work.join("tree"));
    fs::write(tree.join("etc/creche-standin/swap"), "the new disk\n").unwrap();
    ext4(&image.join("root2.img"), Some(&tree));
    let new = sha256(&image.join("root2.img"));
    let old = sha256(&image.join("root.img"));
    assert_ne!(old, new, "step 2: the disks are equal");
    let disk = format!(r#"{{"file":"root2.img","sha256":"{new}"}}"#);
    let kernel = sha256(&image.join("vmlinux"));
    let text = format!(
        r#"{{"kernel":{{"file":"vmlinux","sha256":"{kernel}"}},"base":{disk},"python":{disk}}}"#
    );
    let staged = image.join("digests.json.new");
    fs::write(&staged, text).unwrap();
    sudo(&["chown", "0:0"], &staged);
    let renamed = Instant::now();
    fs::rename(&staged, image.join(DIGESTS_FILE)).unwrap();

    // 3. The follow and the idle timeout wait at the same time. New
    // starts keep the old disk for `HOOK_TIMEOUT` plus one `POLL`
    // (decision CH77 rule 2), then the vmm log names the new set within a
    // few polls. The VM of step 1 sleeps within `IDLE_TIMEOUT` plus one
    // `IDLE_CHECK`, and the vmm log says so. Its snapshot holds the key of
    // its start on the old disk, also when it sleeps after the swap.
    let swap = format!(
        "vmm: new starts use the new {DIGESTS_FILE}: kernel {kernel}, base {new}, python {new}"
    );
    let slept = format!("vmm: the VM of {SESSION} sleeps");
    let settle = HOOK_TIMEOUT + POLL..HOOK_TIMEOUT + POLL * 4;
    let deadline = (renamed + settle.end).max(closed + IDLE_TIMEOUT + IDLE_CHECK + BOOT);
    let (mut waited, mut asleep) = (None, false);
    while waited.is_none() || !asleep {
        let left = deadline.saturating_duration_since(Instant::now());
        let line = served.log.recv_timeout(left).unwrap_or_else(|error| {
            panic!("step 3: the swap after {waited:?}, the sleep {asleep}: {error}")
        });
        if line.starts_with("vmm: new starts use the new ") {
            assert_eq!(line, swap, "step 3: another set");
            waited = Some(renamed.elapsed());
        }
        asleep |= line == slept;
    }
    let waited = waited.unwrap();
    assert!(
        settle.contains(&waited),
        "step 3: the swap came after {waited:?}"
    );

    // 4. The sleep left one snapshot.
    let generations: Vec<PathBuf> = fs::read_dir(session.snapshots())
        .unwrap()
        .map(|entry| entry.unwrap().path())
        .collect();
    assert_eq!(generations.len(), 1, "step 4 sleep: {generations:?}");

    // 5. The start drops the snapshot of the old key and cold-boots: the
    // new serial log holds the `up` line of a boot, and the new VM has its
    // vsock socket.
    let link = served.acp();
    assert!(vsock.exists(), "step 5: no vsock socket");
    assert!(
        !generations[0].exists(),
        "step 5: the snapshot of the old disk stays"
    );
    assert!(
        lines(&session.serial_log()).iter().any(|line| line == UP),
        "step 5: the VM did not boot"
    );
    drop(link);
    drop(served);
}
