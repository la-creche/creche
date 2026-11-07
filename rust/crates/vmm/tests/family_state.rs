//! The family-state reply on host port 1100 (plan PR V8, decisions CH5a,
//! CH23, CH31, CH43 rule 2) on real Unix sockets in the test process. A
//! test plays the guest side, as Cloud Hypervisor connects to
//! `vsock.sock_1100`, over a scratch family root in `/tmp`. The runtime
//! starts with paused time, so the hold of decision CH31 rule 1 runs with
//! no real wait.

use std::fs;
use std::io::{self, ErrorKind};
use std::net::Shutdown;
use std::path::PathBuf;
use std::time::{Duration, SystemTime};

use creche_vmm::family_state::{HOLD_CAP, LINK_CAP, Server, bind, fd_need};
use creche_vmm_confine::family::FamilyRoot;
use creche_vmm_confine::rfc3339;
use creche_vmm_store::layout::{RunRoot, Slot, VmRunDir};
use creche_wire::{FAMILY_STATE_CAP, HostPort, SessionId, strict};
use tokio::net::UnixStream;
use tokio::runtime::{Builder, Runtime};
use tokio::time::{Instant, sleep};

/// `creds.json` as caregiver writes it, with the two caregiver-only
/// fields that the reply drops.
const CREDS: &str = r#"{"epoch":3,"litellm_key":"sk-test-key","pep_token":"abababababababababababababababababababababababababababababababab","written_at":"2026-11-06T12:00:00Z","previous_pep_token":"cdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcd","previous_expires_at":"2026-11-06T12:05:00Z"}"#;

/// The bound of a test that waits for a change on the host.
const TRIES: u32 = 500;

/// A family root with the family `chat`, the run dir of one VM in `/tmp`,
/// and a paused runtime. Its drop removes the dir, also after a failed
/// assert.
struct Scratch {
    dir: PathBuf,
    run: VmRunDir,
    runtime: Runtime,
}

impl Drop for Scratch {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.dir);
    }
}

// `cfg(test)` lets the helpers unwrap (rust/clippy.toml).
#[cfg(test)]
impl Scratch {
    /// `/tmp`, not `temp_dir`: the run root bounds the socket paths under
    /// it, and the temp dir of macOS is too long. The family has creds and
    /// a config with no `model_live_at`.
    fn new(name: &str) -> Self {
        let dir = PathBuf::from(format!("/tmp/vmm-family-{}-{name}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        let root = RunRoot::new(dir.join("run")).unwrap();
        let id: SessionId = "3f2b8c1e-9d4a-4e6b-8a7c-0b1d2e3f4a5b".parse().unwrap();
        let run = root.link_vm(&id, Slot::new(0).unwrap()).unwrap();
        let runtime = Builder::new_current_thread()
            .enable_all()
            .start_paused(true)
            .build()
            .unwrap();
        let scratch = Self { dir, run, runtime };
        fs::create_dir_all(scratch.family("creds")).unwrap();
        fs::create_dir_all(scratch.family("config")).unwrap();
        fs::write(scratch.family("creds/creds.json"), CREDS).unwrap();
        scratch.runtime_json(None);
        fs::write(scratch.family("config/instructions.md"), "Be brief.").unwrap();
        scratch
    }

    /// A path in the family dir.
    fn family(&self, path: &str) -> PathBuf {
        self.dir.join("families/chat").join(path)
    }

    /// Writes `config/runtime.json`, with `model_live_at` when given.
    fn runtime_json(&self, live_at: Option<SystemTime>) {
        let live = live_at.map_or(String::new(), |at| {
            format!(r#","model_live_at":"{}""#, rfc3339::format(at))
        });
        let text = format!(
            r#"{{"shell":true,"sandbox_tools":["read"],"model_alias":"litellm/test"{live}}}"#
        );
        fs::write(self.family("config/runtime.json"), text).unwrap();
    }

    /// Binds and serves the listener of the VM.
    fn serve(&self) -> Server {
        let bound = bind(&self.run).unwrap();
        let families = FamilyRoot::new(self.dir.join("families")).unwrap();
        let id: SessionId = "3f2b8c1e-9d4a-4e6b-8a7c-0b1d2e3f4a5b".parse().unwrap();
        let _runtime = self.runtime.enter();
        bound.serve(families, "chat".parse().unwrap(), id).unwrap()
    }

    /// A guest dial of 1100, as CH makes it.
    async fn dial(&self) -> Guest {
        let path = self.run.host_listener(HostPort::FamilyState);
        Guest {
            stream: UnixStream::connect(path).await.unwrap(),
            pending: Vec::new(),
        }
    }
}

/// The guest end of one link.
struct Guest {
    stream: UnixStream,
    pending: Vec<u8>,
}

#[cfg(test)]
impl Guest {
    /// Writes all of `bytes`.
    async fn send(&self, bytes: &[u8]) {
        self.write(bytes).await.unwrap();
    }

    /// Writes all of `bytes`, or fails when the vmm closed the link.
    async fn write(&self, bytes: &[u8]) -> io::Result<()> {
        let mut left = bytes;
        while !left.is_empty() {
            self.stream.writable().await?;
            match self.stream.try_write(left) {
                Ok(written) => left = &left[written..],
                Err(error) if error.kind() == ErrorKind::WouldBlock => {}
                Err(error) => return Err(error),
            }
        }
        Ok(())
    }

    /// Shuts down the write half, so the vmm reads an EOF.
    fn end_writes(self) -> Self {
        let stream = self.stream.into_std().unwrap();
        stream.shutdown(Shutdown::Write).unwrap();
        Self {
            stream: UnixStream::from_std(stream).unwrap(),
            pending: self.pending,
        }
    }

    /// The next line with no LF, or `None` when the vmm closed the link.
    /// Linux resets a link whose reader closed it with bytes unread.
    async fn line(&mut self) -> Option<Vec<u8>> {
        let mut chunk = [0_u8; 65536];
        loop {
            if let Some(end) = self.pending.iter().position(|&byte| byte == b'\n') {
                let mut line: Vec<u8> = self.pending.drain(..=end).collect();
                line.pop();
                return Some(line);
            }
            self.stream.readable().await.unwrap();
            match self.stream.try_read(&mut chunk) {
                Ok(0) => {
                    assert!(self.pending.is_empty(), "a reply cut by the close");
                    return None;
                }
                Ok(read) => self.pending.extend_from_slice(&chunk[..read]),
                Err(error) if error.kind() == ErrorKind::WouldBlock => {}
                Err(error) if error.kind() == ErrorKind::ConnectionReset => return None,
                Err(error) => panic!("the read failed: {error}"),
            }
        }
    }

    /// Sends one pull and reads its reply as JSON. The line is strict.
    async fn pull(&mut self) -> serde_json::Value {
        self.send(b"{}\n").await;
        self.reply().await
    }

    /// Reads one reply as JSON. The line is strict.
    async fn reply(&mut self) -> serde_json::Value {
        let line = self.line().await.expect("the vmm closed the link");
        assert!(line.len() <= FAMILY_STATE_CAP);
        assert!(strict(&line));
        serde_json::from_slice(&line).unwrap()
    }
}

/// The key of the one-key object `reply`: the kind of the reply.
#[cfg(test)]
fn kind(reply: &serde_json::Value) -> &str {
    let object = reply.as_object().unwrap();
    assert_eq!(object.len(), 1);
    object.keys().next().unwrap()
}

/// One link carries many pulls, sent one at a time or in one write. Each
/// gets one `state` reply with the creds and the config, and none holds
/// the old token (decisions CH23 rule 1, CH43 rule 4).
#[test]
fn answers_each_pull_line_on_one_link() {
    let scratch = Scratch::new("pulls");
    let _server = scratch.serve();
    scratch.runtime.block_on(async {
        let mut guest = scratch.dial().await;
        let reply = guest.pull().await;
        assert_eq!(reply["state"]["creds"]["litellm_key"], "sk-test-key");
        assert_eq!(reply["state"]["config"]["instructions"], "Be brief.");
        assert!(!reply.to_string().contains("cdcdcd"));
        assert_eq!(guest.pull().await, reply);

        guest.send(b"{}\n{}\n{}\n").await;
        for _ in 0..3 {
            assert_eq!(guest.reply().await, reply);
        }
    });
}

/// A `state` reply of `FAMILY_STATE_CAP` bytes passes whole, with its LF.
#[test]
fn a_reply_at_the_cap_arrives_whole() {
    let scratch = Scratch::new("whole");
    let _server = scratch.serve();
    let instructions = scratch.family("config/instructions.md");
    fs::write(&instructions, "").unwrap();
    scratch.runtime.block_on(async {
        let mut guest = scratch.dial().await;
        guest.send(b"{}\n").await;
        let empty = guest.line().await.unwrap().len();
        // An ASCII letter is one byte in the file and one in the reply.
        fs::write(&instructions, "a".repeat(FAMILY_STATE_CAP - empty)).unwrap();
        guest.send(b"{}\n").await;
        assert_eq!(guest.line().await.unwrap().len(), FAMILY_STATE_CAP);
        assert_eq!(kind(&guest.pull().await), "state");
    });
}

/// A `state` reply waits until the host time passes `model_live_at`. A
/// time that passed holds nothing (decision CH31 rule 1).
#[test]
fn holds_a_state_until_model_live_at() {
    let scratch = Scratch::new("hold");
    let _server = scratch.serve();
    // The file holds whole seconds, so the hold is between 4 s and 5 s.
    scratch.runtime_json(Some(SystemTime::now() + Duration::from_secs(5)));
    scratch.runtime.block_on(async {
        let mut guest = scratch.dial().await;
        let sent = Instant::now();
        assert_eq!(kind(&guest.pull().await), "state");
        let held = sent.elapsed();
        assert!(held > Duration::from_secs(3), "held {held:?}");
        assert!(held <= Duration::from_millis(5010), "held {held:?}");
    });
    scratch.runtime_json(Some(SystemTime::now() - Duration::from_secs(5)));
    scratch.runtime.block_on(async {
        let mut guest = scratch.dial().await;
        let sent = Instant::now();
        assert_eq!(kind(&guest.pull().await), "state");
        assert!(sent.elapsed() < Duration::from_secs(1));
    });
}

/// A `model_live_at` an hour ahead holds the reply for `HOLD_CAP`, 10 s,
/// and no longer (decision CH31 rule 1).
#[test]
fn holds_at_most_10_s() {
    let scratch = Scratch::new("cap-hold");
    let _server = scratch.serve();
    scratch.runtime_json(Some(SystemTime::now() + Duration::from_secs(3600)));
    scratch.runtime.block_on(async {
        let mut guest = scratch.dial().await;
        let sent = Instant::now();
        assert_eq!(kind(&guest.pull().await), "state");
        let held = sent.elapsed();
        assert_eq!(HOLD_CAP, Duration::from_secs(10));
        assert!(held >= HOLD_CAP, "held {held:?}");
        assert!(
            held <= HOLD_CAP + Duration::from_millis(10),
            "held {held:?}"
        );
    });
}

/// Each read fault is its typed reply, and the link stays open, so the
/// next pull on it reads again (decisions CH23 rule 2, CH31 rules 3 and
/// 4, CH53 rule 2).
#[test]
fn a_typed_error_keeps_the_link_open() {
    let scratch = Scratch::new("typed");
    let _server = scratch.serve();
    scratch.runtime.block_on(async {
        let mut guest = scratch.dial().await;
        let creds = scratch.family("creds/creds.json");
        fs::remove_file(&creds).unwrap();
        assert_eq!(kind(&guest.pull().await), "creds_missing");
        let config = scratch.family("config");
        let config_old = scratch.family("config.old");
        fs::rename(&config, &config_old).unwrap();
        assert_eq!(kind(&guest.pull().await), "creds_missing");

        fs::write(&creds, CREDS).unwrap();
        assert_eq!(kind(&guest.pull().await), "config_missing");
        fs::rename(&config_old, &config).unwrap();
        let runtime = scratch.family("config/runtime.json");
        let good = fs::read(&runtime).unwrap();
        fs::write(&runtime, b"{}").unwrap();
        assert_eq!(kind(&guest.pull().await), "not_valid");
        fs::write(&runtime, &good).unwrap();
        let instructions = scratch.family("config/instructions.md");
        fs::write(&instructions, "a".repeat(FAMILY_STATE_CAP)).unwrap();
        assert_eq!(kind(&guest.pull().await), "too_large");

        fs::write(&instructions, "Be brief.").unwrap();
        assert_eq!(kind(&guest.pull().await), "state");
    });
}

/// A line that the strict reader refuses closes the link with no reply
/// (decisions CH43 rule 2, CH65 rule 2): a line that is not JSON, a key
/// given twice, a pull with a key, a line past the cap with no LF and a
/// line cut by an EOF. A new link still gets its reply.
#[test]
fn a_refused_pull_closes_the_link() {
    let scratch = Scratch::new("refused");
    let _server = scratch.serve();
    scratch.runtime.block_on(async {
        let past_cap = vec![b' '; FAMILY_STATE_CAP + 1];
        let lines: [&[u8]; 4] = [
            b"pull\n",
            b"{\"a\":{},\"a\":{}}\n",
            b"{\"a\":1}\n",
            &past_cap,
        ];
        for line in lines {
            let mut guest = scratch.dial().await;
            guest.send(line).await;
            assert_eq!(guest.line().await, None);
        }
        let guest = scratch.dial().await;
        guest.send(b"{}").await;
        let mut guest = guest.end_writes();
        assert_eq!(guest.line().await, None);

        let mut guest = scratch.dial().await;
        assert_eq!(kind(&guest.pull().await), "state");
    });
}

/// The listener holds `LINK_CAP` open links. The next dial closes at once
/// with no reply. After one link ends, a dial gets through again
/// (decision CH68 rule 1).
#[test]
fn a_link_past_the_cap_closes_at_once() {
    let scratch = Scratch::new("links");
    let _server = scratch.serve();
    scratch.runtime.block_on(async {
        let mut held = Vec::new();
        for _ in 0..LINK_CAP {
            let mut guest = scratch.dial().await;
            assert_eq!(kind(&guest.pull().await), "state");
            held.push(guest);
        }
        // The vmm may close the link before the pull is out, so the test
        // reads only.
        let mut refused = scratch.dial().await;
        assert_eq!(refused.line().await, None);

        drop(held.pop());
        for _ in 0..TRIES {
            let mut guest = scratch.dial().await;
            let sent = guest.write(b"{}\n").await.is_ok();
            if sent && guest.line().await.is_some() {
                return;
            }
            sleep(Duration::from_millis(1)).await;
        }
        panic!("no link got through after one ended");
    });
}

/// The drop of the server closes the listener, so a dial is refused.
#[test]
fn the_drop_closes_the_listener() {
    let scratch = Scratch::new("drop");
    let server = scratch.serve();
    let path = scratch.run.host_listener(HostPort::FamilyState);
    scratch.runtime.block_on(async {
        let mut guest = scratch.dial().await;
        assert_eq!(kind(&guest.pull().await), "state");
        drop(server);
        for _ in 0..TRIES {
            sleep(Duration::from_millis(1)).await;
            if let Err(error) = UnixStream::connect(&path).await {
                assert_eq!(error.kind(), ErrorKind::ConnectionRefused);
                return;
            }
        }
        panic!("1100 still listens");
    });
}

/// Each of the 64 slots with `LINK_CAP` links, each in a read: the
/// listener, and per link its stream and the 6 fds of one read, 64 × (1 +
/// 64 × 7). The vmm adds it to the bridge need at start
/// (decision CH68 rule 2).
#[test]
fn the_fd_need_holds_every_full_listener() {
    assert_eq!(LINK_CAP, 64);
    assert_eq!(fd_need(), 64 * (1 + 64 * 7));
}
