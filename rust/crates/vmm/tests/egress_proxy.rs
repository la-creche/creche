//! The CONNECT proxy of host port 1104 (plan PR E2, decisions CH6-4, CH69
//! rules 7, 9, 10 and 14, CH70 rule 2) on a temp Unix socket in the test
//! process. A test plays the guest side, as Cloud Hypervisor connects to
//! `vsock.sock_1104`. A scratch family root holds the family `code` with
//! an `egress.json`. No test reaches the network: each listed name
//! resolves through `/etc/hosts` (`localhost`) or is refused before any
//! lookup. The kvm job proves an allowed tunnel (plan PR E4, decision
//! CH69 rule 22).

use std::io::{Read, Write};
use std::os::unix::fs::PermissionsExt;
use std::os::unix::net::UnixStream;
use std::path::PathBuf;
use std::sync::Arc;
use std::time::{Duration, Instant};

use creche_vmm::egress::log::{LOG_CAP, Line, Log, ProxyReason, Reason, Verdict};
use creche_vmm::egress::proxy::{self, HEAD_CAP, HEAD_WAIT, LINK_CAP, Server, bind};
use creche_vmm::handlers::check::Checks;
use creche_vmm_confine::family::FamilyRoot;
use creche_vmm_store::layout::{RunRoot, Slot, VmRunDir};
use creche_wire::{CheckReply, CheckRequest, HostPort, Serve as _, SessionId};
use serde_json::{Value, json};
use tokio::runtime::{Builder, Runtime};

/// The bound of each wait of a test.
const WAIT: Duration = Duration::from_secs(5);

/// The session of the VM.
const SESSION: &str = "3f2b8c1e-9d4a-4e6b-8a7c-0b1d2e3f4a5b";

/// The allowlist of the family `code`.
const EGRESS: &str =
    r#"{"hosts":[{"host":"github.com","port":443},{"host":"localhost","port":443}]}"#;

/// As `EGRESS`, with `localhost` removed.
const REMOVED: &str = r#"{"hosts":[{"host":"github.com","port":443}]}"#;

/// The run dir of one VM, the family root and the log in `/tmp`, and the
/// runtime of the proxy. Its drop removes the dir, also after a failed
/// assert.
struct Scratch {
    dir: PathBuf,
    run: VmRunDir,
    families: FamilyRoot,
    runtime: Runtime,
}

impl Drop for Scratch {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.dir);
    }
}

// `cfg(test)` lets the helpers unwrap and index (rust/clippy.toml).
#[cfg(test)]
impl Scratch {
    /// `/tmp`, not `temp_dir`: the run root bounds the socket paths under
    /// it, and the temp dir of macOS is too long.
    fn new(name: &str, runtime: Runtime) -> Self {
        let dir = PathBuf::from(format!("/tmp/vmm-egress-{}-{name}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(dir.join("families/code")).unwrap();
        let root = RunRoot::new(dir.join("run")).unwrap();
        let run = root.link_vm(&session(), Slot::new(0).unwrap()).unwrap();
        let families = FamilyRoot::new(dir.join("families")).unwrap();
        Self {
            dir,
            run,
            families,
            runtime,
        }
    }

    /// A scratch on a multi-thread runtime.
    fn threaded(name: &str) -> Self {
        let runtime = Builder::new_multi_thread()
            .worker_threads(2)
            .enable_all()
            .build()
            .unwrap();
        Self::new(name, runtime)
    }

    /// The path of the log.
    fn log_path(&self) -> PathBuf {
        self.dir.join("egress.jsonl")
    }

    /// Binds and serves the proxy of the family `code`.
    fn serve(&self) -> Server {
        let log = Arc::new(Log::open(self.log_path()).unwrap());
        let bound = bind(&self.run).unwrap();
        let _runtime = self.runtime.enter();
        let family = "code".parse().unwrap();
        bound
            .serve(self.families.clone(), family, session(), log)
            .unwrap()
    }

    /// Writes `egress.json` of the family `code`.
    fn egress(&self, text: &str) {
        std::fs::write(self.dir.join("families/code/egress.json"), text).unwrap();
    }

    /// A guest dial of 1104, as CH makes it.
    fn dial(&self) -> UnixStream {
        let stream = UnixStream::connect(self.run.host_listener(HostPort::Egress)).unwrap();
        stream.set_read_timeout(Some(WAIT)).unwrap();
        stream
    }

    /// Sends `head` on a new link and gives the whole reply.
    fn ask(&self, head: &str) -> String {
        let mut stream = self.dial();
        stream.write_all(head.as_bytes()).unwrap();
        let mut reply = String::new();
        stream.read_to_string(&mut reply).unwrap();
        reply
    }

    /// The status of the reply to a CONNECT of `target`.
    fn connect(&self, target: &str) -> u16 {
        status(&self.ask(&format!(
            "CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n"
        )))
    }

    /// The verdict of the route `check` on `target` for the family `code`.
    fn check(&self, target: &str) -> CheckReply {
        let checks = Checks::new(self.families.clone());
        let request: CheckRequest =
            serde_json::from_value(json!({"family": "code", "target": target})).unwrap();
        self.runtime.block_on(checks.serve(request)).unwrap()
    }

    /// The lines of the log, once it holds `count`. A read that runs during
    /// an append can see part of it (rust job run 42414879212), so only a
    /// line that ends in its LF counts.
    fn lines(&self, count: usize) -> Vec<Value> {
        let deadline = Instant::now() + WAIT;
        loop {
            let text = std::fs::read_to_string(self.log_path()).unwrap();
            let lines: Vec<Value> = text
                .split_inclusive('\n')
                .filter_map(|line| line.strip_suffix('\n'))
                .map(|line| serde_json::from_str(line).unwrap())
                .collect();
            if lines.len() >= count {
                assert_eq!(lines.len(), count, "{text}");
                return lines;
            }
            assert!(Instant::now() < deadline, "{} lines: {text}", lines.len());
            std::thread::sleep(Duration::from_millis(5));
        }
    }
}

#[cfg(test)]
fn session() -> SessionId {
    SESSION.parse().unwrap()
}

/// The status code of an HTTP reply.
#[cfg(test)]
fn status(reply: &str) -> u16 {
    reply.get(9..12).unwrap().parse().unwrap()
}

/// The reason of a refusal of `check` as the wire writes it.
#[cfg(test)]
fn reason(reply: &CheckReply) -> Value {
    match reply {
        CheckReply::Refused(refused) => serde_json::to_value(refused.reason).unwrap(),
        CheckReply::Allowed(allowed) => panic!("allowed: {allowed:?}"),
    }
}

/// The phase 7 proof line: a CONNECT to an IP literal gets 403 and a log
/// line with the reason of `check` (decisions CH6-4, CH70 rule 2).
#[test]
fn a_connect_to_an_ip_literal_is_refused() {
    let scratch = Scratch::threaded("literal");
    scratch.egress(EGRESS);
    let _server = scratch.serve();
    let reply = scratch.ask("CONNECT 140.82.112.3:443 HTTP/1.1\r\n\r\n");
    assert!(reply.starts_with("HTTP/1.1 403 Forbidden\r\n"), "{reply}");
    let line = &scratch.lines(1)[0];
    assert_eq!(line["verdict"], "refused");
    assert_eq!(line["reason"], "ip_literal");
    assert_eq!(line["host"], Value::Null);
}

/// The phase 7 proof line: for each target the verdict of `check` is the
/// verdict of a CONNECT, and the log names the reason of `check`
/// (decisions CH6-3, CH69 rules 2 and 9, CH70 rule 2). The sixth row is
/// a host that the list no longer holds. The last row is a listed name
/// with no address, which gets 502.
#[test]
fn check_agrees_with_the_proxy() {
    let scratch = Scratch::threaded("agree");
    let _server = scratch.serve();
    // The 64-letter label is over the 63 of a DNS label, and RFC 6761
    // reserves `.invalid`, so the lookup fails with no network.
    let host = format!("{}.invalid", "a".repeat(64));
    let (invalid, invalid_list) = (
        format!("{host}:443"),
        format!(r#"{{"hosts":[{{"host":"{host}","port":443}}]}}"#),
    );
    let rows = [
        (EGRESS, "140.82.112.3:443", 403),
        (EGRESS, "pypi.org:443", 403),
        (EGRESS, "github.com:8443", 403),
        (EGRESS, "localhost:443", 403),
        (EGRESS, "github.com", 400),
        (REMOVED, "localhost:443", 403),
        (invalid_list.as_str(), invalid.as_str(), 502),
    ];
    for (count, (list, target, code)) in rows.into_iter().enumerate() {
        scratch.egress(list);
        let checked = scratch.check(target);
        assert_eq!(scratch.connect(target), code, "{target}");
        let lines = scratch.lines(count + 1);
        let line = lines.last().unwrap();
        assert_eq!(line["reason"], reason(&checked), "{target}");
        assert_eq!(line["verdict"], "refused", "{target}");
    }
    let lines = scratch.lines(rows.len());
    let reasons: Vec<&Value> = lines.iter().map(|line| &line["reason"]).collect();
    let expected = [
        "ip_literal",
        "not_listed",
        "not_listed",
        "not_public",
        "bad_target",
        "not_listed",
        "no_address",
    ];
    assert_eq!(reasons, expected);
}

/// A list that does not read refuses each CONNECT with the reason that
/// `check` gives, `not_valid` (decision CH70 rule 2).
#[test]
fn a_bad_list_is_not_valid_for_the_proxy_too() {
    let scratch = Scratch::threaded("bad-list");
    scratch.egress("{");
    let _server = scratch.serve();
    assert_eq!(reason(&scratch.check("github.com:443")), "not_valid");
    assert_eq!(scratch.connect("github.com:443"), 403);
    assert_eq!(scratch.lines(1)[0]["reason"], "not_valid");
}

/// Another method gets 405 with `Allow: CONNECT` (decision CH69 rules 7
/// and 9).
#[test]
fn a_method_other_than_connect_gets_405() {
    let scratch = Scratch::threaded("method");
    scratch.egress(EGRESS);
    let _server = scratch.serve();
    let reply = scratch.ask("GET http://github.com/ HTTP/1.1\r\nHost: github.com\r\n\r\n");
    assert!(reply.starts_with("HTTP/1.1 405 "), "{reply}");
    assert!(reply.contains("\r\nAllow: CONNECT\r\n"), "{reply}");
    assert_eq!(scratch.lines(1)[0]["reason"], "not_connect");
}

/// A head that fills `HEAD_CAP` with no CRLF CRLF gets 400, and so does a
/// request line that does not read (decision CH69 rule 9).
#[test]
fn a_head_past_8_kib_gets_400() {
    assert_eq!(HEAD_CAP, 8 * 1024);
    let scratch = Scratch::threaded("cap");
    scratch.egress(EGRESS);
    let _server = scratch.serve();
    let long = format!(
        "CONNECT github.com:443 HTTP/1.1\r\nX: {}",
        "a".repeat(HEAD_CAP)
    );
    let reply = scratch.ask(long.get(..HEAD_CAP).unwrap());
    assert_eq!(status(&reply), 400, "{reply}");
    for head in [
        "CONNECT github.com:443\r\n\r\n",
        "CONNECT  github.com:443 HTTP/1.1\r\n\r\n",
        "CONNECT github.com:443 HTTP/2\r\n\r\n",
    ] {
        assert_eq!(status(&scratch.ask(head)), 400, "{head:?}");
    }
    let lines = scratch.lines(4);
    assert!(lines.iter().all(|line| line["reason"] == "bad_head"));
}

/// A CRLF CRLF that two reads split still ends the head, and a whole
/// head of exactly `HEAD_CAP` bytes passes the cap (decision CH69 rule
/// 9). Both get 405.
#[test]
fn a_split_head_and_a_head_at_the_cap_read_whole() {
    let scratch = Scratch::threaded("split");
    scratch.egress(EGRESS);
    let _server = scratch.serve();
    let mut split = scratch.dial();
    split.write_all(b"GET / HTTP/1.1\r\n\r").unwrap();
    std::thread::sleep(Duration::from_millis(100));
    split.write_all(b"\n").unwrap();
    let mut reply = String::new();
    split.read_to_string(&mut reply).unwrap();
    assert_eq!(status(&reply), 405, "{reply}");

    let start = "GET / HTTP/1.1\r\nX: ";
    let fill = "a".repeat(HEAD_CAP - start.len() - 4);
    let head = format!("{start}{fill}\r\n\r\n");
    assert_eq!(head.len(), HEAD_CAP);
    assert_eq!(status(&scratch.ask(&head)), 405);
    let lines = scratch.lines(2);
    assert!(lines.iter().all(|line| line["reason"] == "not_connect"));
}

/// A head that is not whole after `HEAD_WAIT` closes the link with no
/// reply (decision CH69 rule 9, as decision CH40 rule 2). The tokio clock
/// is paused, so the 30 s pass at once.
#[test]
fn a_head_slower_than_30_s_is_closed() {
    assert_eq!(HEAD_WAIT, Duration::from_secs(30));
    let runtime = Builder::new_current_thread()
        .enable_all()
        .start_paused(true)
        .build()
        .unwrap();
    let scratch = Scratch::new("slow", runtime);
    scratch.egress(EGRESS);
    let _server = scratch.serve();
    let path = scratch.run.host_listener(HostPort::Egress);
    let (early, read) = scratch.runtime.block_on(async {
        let guest = tokio::net::UnixStream::connect(path).await.unwrap();
        guest.writable().await.unwrap();
        let part = b"CONNECT github.com:443";
        assert_eq!(guest.try_write(part).unwrap(), part.len());
        let mut buffer = [0; 64];
        let early = HEAD_WAIT - Duration::from_secs(1);
        let early = tokio::time::timeout(early, guest.readable()).await.is_err();
        let started = tokio::time::Instant::now();
        guest.readable().await.unwrap();
        let read = guest.try_read(&mut buffer).unwrap();
        assert!(started.elapsed() <= Duration::from_secs(2));
        (early, read)
    });
    assert!(early, "the link closed before the wait");
    assert_eq!(read, 0, "the link got a reply");
    assert_eq!(scratch.lines(1)[0]["reason"], "head_timeout");
}

/// A host that the list loses is refused at the next CONNECT, with no
/// restart of the proxy (decision CH6-4, fact 43).
#[test]
fn a_removed_host_is_refused_at_the_next_connect() {
    let scratch = Scratch::threaded("removed");
    scratch.egress(EGRESS);
    let _server = scratch.serve();
    assert_eq!(scratch.connect("localhost:443"), 403);
    assert_eq!(scratch.lines(1)[0]["reason"], "not_public");
    scratch.egress(REMOVED);
    assert_eq!(scratch.connect("localhost:443"), 403);
    assert_eq!(scratch.lines(2)[1]["reason"], "not_listed");
}

/// Each link writes exactly one line with every field of decision CH69
/// rule 10.
#[test]
fn each_connection_writes_one_log_line() {
    let scratch = Scratch::threaded("lines");
    scratch.egress(EGRESS);
    let server = scratch.serve();
    // Each link waits for the line of the one before, so the lines keep
    // the order of the links.
    scratch.connect("localhost:443");
    scratch.lines(1);
    scratch.ask("GET / HTTP/1.1\r\n\r\n");
    scratch.lines(2);
    drop(scratch.dial());
    let lines = scratch.lines(3);
    let keys = [
        "address",
        "bytes_down",
        "bytes_up",
        "duration_ms",
        "family",
        "host",
        "port",
        "reason",
        "session",
        "time",
        "verdict",
    ];
    for line in &lines {
        let object = line.as_object().unwrap();
        let mut found: Vec<&str> = object.keys().map(String::as_str).collect();
        found.sort_unstable();
        assert_eq!(found, keys, "{line}");
        assert_eq!(line["family"], "code");
        assert_eq!(line["session"], SESSION);
        assert_eq!(line["verdict"], "refused");
        assert_eq!(line["bytes_up"], 0);
        assert!(line["time"].as_str().unwrap().ends_with('Z'));
    }
    assert_eq!(lines[0]["host"], "localhost");
    assert_eq!(lines[0]["port"], 443);
    assert_eq!(lines[1]["reason"], "not_connect");
    assert_eq!(lines[2]["reason"], "bad_head");
    // The drop of the server writes no line for a link that has ended.
    drop(server);
    std::thread::sleep(Duration::from_millis(50));
    assert_eq!(scratch.lines(3).len(), 3);
}

/// A write that would pass `LOG_CAP` renames the file to `<path>.1` and
/// starts a new one (decision CH69 rule 10).
#[test]
fn the_log_rolls_over_at_its_cap() {
    assert_eq!(LOG_CAP, 64 * 1024 * 1024);
    let scratch = Scratch::threaded("roll");
    scratch.egress(EGRESS);
    let full = std::fs::File::create(scratch.log_path()).unwrap();
    full.set_len(LOG_CAP - 10).unwrap();
    drop(full);
    let _server = scratch.serve();
    scratch.connect("pypi.org:443");
    let rolled = scratch.dir.join("egress.jsonl.1");
    let deadline = Instant::now() + WAIT;
    while !rolled.exists() {
        assert!(Instant::now() < deadline, "no rollover");
        std::thread::sleep(Duration::from_millis(5));
    }
    let lines = scratch.lines(1);
    assert_eq!(lines[0]["reason"], "not_listed");
    assert_eq!(std::fs::metadata(rolled).unwrap().len(), LOG_CAP - 10);
    let mode = std::fs::metadata(scratch.log_path()).unwrap().permissions();
    assert_eq!(mode.mode() & 0o777, 0o600);
}

/// A rollover whose rename went and whose open failed retries only the
/// open at the next write, so the log holds two files again (decision
/// CH69 rule 10). The test runs its own binary as the child
/// `rolls_after_a_failed_open_for_the_child` under `ulimit -n 64`, so the
/// child can use up its fds, and reads the real stderr of the vmm code.
#[test]
fn a_failed_open_after_the_rename_is_retried() {
    let output = std::process::Command::new("sh")
        .args(["-c", "ulimit -n 64 && exec \"$0\" \"$@\""])
        .arg(std::env::current_exe().unwrap())
        .args(["--exact", "rolls_after_a_failed_open_for_the_child"])
        .args(["--ignored", "--nocapture", "--test-threads=1"])
        .output()
        .unwrap();
    let stderr = String::from_utf8(output.stderr).unwrap();
    let stdout = String::from_utf8(output.stdout).unwrap();
    assert!(output.status.success(), "{stdout}{stderr}");
    assert!(stdout.contains("1 passed"), "{stdout}");
    let lines: Vec<&str> = stderr.lines().collect();
    assert_eq!(lines.len(), 1, "{stderr}");
    assert!(
        lines[0].starts_with("vmm: egress log rollover failed: "),
        "{stderr}"
    );
}

/// The child of `a_failed_open_after_the_rename_is_retried`. It opens the
/// log, then opens `/dev/null` until no fd is left, so the open of the
/// rollover fails after its rename. Then it frees the fds and writes
/// again.
#[test]
#[ignore = "the child of a_failed_open_after_the_rename_is_retried"]
fn rolls_after_a_failed_open_for_the_child() {
    let scratch = Scratch::threaded("reopen");
    let (path, rolled) = (scratch.log_path(), scratch.dir.join("egress.jsonl.1"));
    let full = std::fs::File::create(&path).unwrap();
    full.set_len(LOG_CAP - 10).unwrap();
    drop(full);
    let log = Log::open(path.clone()).unwrap();
    let line = Line {
        time: "2026-11-07T00:00:00Z".to_owned(),
        family: "code".parse().unwrap(),
        session: session(),
        host: None,
        port: None,
        verdict: Verdict::Refused,
        reason: Some(Reason::Proxy(ProxyReason::BadHead)),
        address: None,
        bytes_up: 0,
        bytes_down: 0,
        duration_ms: 0,
    };
    let size = u64::try_from(serde_json::to_vec(&line).unwrap().len() + 1).unwrap();

    let mut held = Vec::new();
    while let Ok(file) = std::fs::File::open("/dev/null") {
        held.push(file);
    }
    log.write(&line);
    assert!(!path.exists(), "the open went");
    assert_eq!(
        std::fs::metadata(&rolled).unwrap().len(),
        LOG_CAP - 10 + size
    );

    drop(held);
    log.write(&line);
    assert_eq!(std::fs::metadata(&path).unwrap().len(), size);
    assert_eq!(
        std::fs::metadata(&rolled).unwrap().len(),
        LOG_CAP - 10 + size
    );
    let mode = std::fs::metadata(&path).unwrap().permissions();
    assert_eq!(mode.mode() & 0o777, 0o600);
}

/// The link past `LINK_CAP` is accepted, logged and closed at once
/// (decisions CH67 rule 3, CH69 rule 14).
#[test]
fn the_65th_link_is_closed_at_once() {
    assert_eq!(LINK_CAP, 64);
    let scratch = Scratch::threaded("links");
    scratch.egress(EGRESS);
    let _server = scratch.serve();
    let held: Vec<UnixStream> = (0..LINK_CAP).map(|_| scratch.dial()).collect();
    let mut past = scratch.dial();
    let started = Instant::now();
    let mut bytes = Vec::new();
    past.read_to_end(&mut bytes).unwrap();
    assert!(bytes.is_empty());
    assert!(started.elapsed() < WAIT);
    let line = &scratch.lines(1)[0];
    assert_eq!(line["reason"], "link_cap");
    drop(held);
}

/// Links that end free their place under the cap, so `LINK_CAP` + 2
/// links one after another each get a reply and a line (decision CH67
/// rule 3).
#[test]
fn ended_links_free_the_cap() {
    let scratch = Scratch::threaded("reap");
    scratch.egress(EGRESS);
    let _server = scratch.serve();
    for count in 1..=LINK_CAP + 2 {
        let reply = scratch.ask("GET / HTTP/1.1\r\n\r\n");
        assert!(reply.starts_with("HTTP/1.1 405 "), "link {count}: {reply}");
        let lines = scratch.lines(count);
        assert_eq!(lines[count - 1]["reason"], "not_connect", "link {count}");
    }
}

/// The drop of `Server` closes the listener and cuts each open link, and
/// the cut link writes its line with no reason (decision CH69 rule 10).
/// The held link is accepted before the link whose line the test waits
/// for, so the proxy serves it when the drop comes.
#[test]
fn the_drop_cuts_each_link_and_closes_1104() {
    let scratch = Scratch::threaded("cut");
    scratch.egress(EGRESS);
    let server = scratch.serve();
    let mut held = scratch.dial();
    held.write_all(b"CONNECT gith").unwrap();
    scratch.ask("GET / HTTP/1.1\r\n\r\n");
    assert_eq!(scratch.lines(1)[0]["reason"], "not_connect");
    drop(server);
    let mut bytes = Vec::new();
    held.read_to_end(&mut bytes).unwrap();
    assert!(bytes.is_empty());
    let line = &scratch.lines(2)[1];
    assert_eq!(line["reason"], Value::Null);
    assert_eq!(line["verdict"], "refused");
    assert_eq!(line["host"], Value::Null);
    let path = scratch.run.host_listener(HostPort::Egress);
    let deadline = Instant::now() + WAIT;
    loop {
        match UnixStream::connect(&path) {
            Err(error) if error.kind() == std::io::ErrorKind::ConnectionRefused => break,
            dialed => assert!(Instant::now() < deadline, "1104 listens: {dialed:?}"),
        }
        std::thread::sleep(Duration::from_millis(5));
    }
}

/// The raise of `vmm serve` holds every proxy at its cap (decisions CH67
/// rule 4, CH69 rule 14): per VM the listener, per link its stream and
/// four more fds. `tests/mirror.rs` holds the whole raise.
#[test]
fn the_fd_need_holds_every_full_proxy() {
    assert_eq!(proxy::fd_need(), (5 * 64 + 1) * 64);
}
