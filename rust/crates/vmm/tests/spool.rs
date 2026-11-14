//! The git platform spool of host port 1105 (decision CH3) on a temp Unix
//! socket in the test process. A test dials `vsock.sock_1105` as Cloud
//! Hypervisor does: hybrid vsock connects to the host socket with no
//! handshake. A scratch family root holds the family `code` with a
//! `git-platform.json`, and a scratch spool holds `push/` and `fetch/`.

use std::fs;
use std::io::{ErrorKind, Read, Write};
use std::net::Shutdown;
use std::os::unix::fs::PermissionsExt;
use std::os::unix::net::UnixStream;
use std::path::PathBuf;
use std::time::{Duration, Instant};

use creche_vmm::spool::{LINK_CAP, PUSH_KEEP, Server, bind, fd_need};
use creche_vmm_confine::family::FamilyRoot;
use creche_vmm_confine::spool::{FETCH_DIR, PUSH_DIR, SpoolRoot};
use creche_vmm_store::layout::{RunRoot, Slot, VmRunDir};
use creche_wire::spool::{BUNDLE_CAP, BundleId, LINE_CAP};
use creche_wire::{HostPort, SessionId};
use tokio::runtime::{Builder, Runtime};

/// The bound of each wait of a test.
const WAIT: Duration = Duration::from_secs(30);

/// The session of the VM.
const SESSION: &str = "3f2b8c1e-9d4a-4e6b-8a7c-0b1d2e3f4a5b";

/// A grant of both tools.
const BOTH: &str = r#"{"fetch":true,"push":true}"#;

/// A bundle id of the family `code` in `fetch/`.
const FETCHED: &str = "code-01JC9Z6A0B1C2D3E4F5G6H7J8K.bundle";

/// The run dir of one VM, the family root and the spool in `/tmp`, and
/// the runtime of the spool. Its drop removes the dir, also after a
/// failed assert.
struct Scratch {
    dir: PathBuf,
    run: VmRunDir,
    families: FamilyRoot,
    runtime: Runtime,
}

impl Drop for Scratch {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.dir);
    }
}

// `cfg(test)` lets the helpers unwrap and index (rust/clippy.toml).
#[cfg(test)]
impl Scratch {
    /// `/tmp`, not `temp_dir`: the run root bounds the socket paths under
    /// it, and the temp dir of macOS is too long. The family `code` holds
    /// a grant of both tools.
    fn new(name: &str) -> Self {
        let dir = PathBuf::from(format!("/tmp/vmm-spool-{}-{name}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(dir.join("families/code")).unwrap();
        fs::create_dir_all(dir.join("spool").join(PUSH_DIR)).unwrap();
        fs::create_dir_all(dir.join("spool").join(FETCH_DIR)).unwrap();
        let root = RunRoot::new(dir.join("run")).unwrap();
        let run = root.link_vm(&session(), Slot::new(0).unwrap()).unwrap();
        let families = FamilyRoot::new(dir.join("families")).unwrap();
        let runtime = Builder::new_multi_thread()
            .worker_threads(2)
            .enable_all()
            .build()
            .unwrap();
        let scratch = Self {
            dir,
            run,
            families,
            runtime,
        };
        scratch.grant(BOTH);
        scratch
    }

    /// Writes `git-platform.json` of the family `code`.
    fn grant(&self, text: &str) {
        fs::write(self.dir.join("families/code/git-platform.json"), text).unwrap();
    }

    /// Binds and serves the spool of the family `code`.
    fn serve(&self) -> Server {
        let bound = bind(&self.run).unwrap();
        let _runtime = self.runtime.enter();
        let root = SpoolRoot::new(self.dir.join("spool")).unwrap();
        let family = "code".parse().unwrap();
        bound
            .serve(root, self.families.clone(), family, session())
            .unwrap()
    }

    /// A guest dial of 1105, as CH makes it.
    fn dial(&self) -> UnixStream {
        let path = self.run.host_listener(HostPort::GitPlatformSpool);
        let stream = UnixStream::connect(path).unwrap();
        stream.set_read_timeout(Some(WAIT)).unwrap();
        stream
    }

    /// Sends `bytes` on a new link, half-closes it and gives every byte of
    /// the answer. A reset counts as the end of the answer, as in
    /// `tests/mirror.rs`.
    fn ask(&self, bytes: &[u8]) -> Vec<u8> {
        let mut stream = self.dial();
        stream.write_all(bytes).unwrap();
        if let Err(error) = stream.shutdown(Shutdown::Write) {
            assert_eq!(error.kind(), ErrorKind::NotConnected, "{error}");
        }
        let mut answer = Vec::new();
        if let Err(error) = stream.read_to_end(&mut answer) {
            assert_eq!(error.kind(), ErrorKind::ConnectionReset, "{error}");
        }
        answer
    }

    /// Puts `bundle` and gives the reply line.
    fn put(&self, bundle: &[u8]) -> String {
        let mut request = format!("put {}\n", bundle.len()).into_bytes();
        request.extend_from_slice(bundle);
        String::from_utf8(self.ask(&request)).unwrap()
    }

    /// The sorted names in `folder` of the spool.
    fn names(&self, folder: &str) -> Vec<String> {
        let mut names: Vec<String> = fs::read_dir(self.dir.join("spool").join(folder))
            .unwrap()
            .map(|entry| entry.unwrap().file_name().into_string().unwrap())
            .collect();
        names.sort();
        names
    }

    /// Waits until `push/` holds `count` names.
    fn wait_for_push(&self, count: usize) {
        let deadline = Instant::now() + WAIT;
        while self.names(PUSH_DIR).len() != count {
            assert!(Instant::now() < deadline, "{:?}", self.names(PUSH_DIR));
            std::thread::sleep(Duration::from_millis(5));
        }
    }
}

#[cfg(test)]
fn session() -> SessionId {
    SESSION.parse().unwrap()
}

/// The id of an `ok <id>` reply line.
#[cfg(test)]
fn put_id(reply: &str) -> BundleId {
    let id = reply
        .strip_prefix("ok ")
        .unwrap()
        .strip_suffix('\n')
        .unwrap();
    id.parse().unwrap()
}

/// A put writes `push/<family>-<ulid>.bundle` with the mode 0640, and the
/// reply names it. No temporary file stays. `tests/spool.rs` of
/// `creche-vmm-confine` proves the mode under the umask of the vmm unit.
#[test]
fn a_put_writes_the_bundle_with_its_name_and_mode() {
    let scratch = Scratch::new("put");
    let _server = scratch.serve();
    let reply = scratch.put(b"# v2 git bundle\n");
    let id = put_id(&reply);
    assert_eq!(id.family().as_str(), "code");
    assert_eq!(scratch.names(PUSH_DIR), [id.to_string()]);
    let path = scratch.dir.join("spool").join(PUSH_DIR).join(id.as_str());
    assert_eq!(fs::read(&path).unwrap(), b"# v2 git bundle\n");
    let mode = fs::metadata(&path).unwrap().permissions().mode();
    assert_eq!(mode & 0o7777, 0o640);
}

/// A put of a bundle larger than one read of the guest stream arrives
/// whole.
#[test]
fn a_put_of_many_chunks_arrives_whole() {
    let scratch = Scratch::new("put-big");
    let _server = scratch.serve();
    let bundle: Vec<u8> = (0..3_000_000_u32)
        .map(|index| u8::try_from(index % 251).unwrap())
        .collect();
    let id = put_id(&scratch.put(&bundle));
    let path = scratch.dir.join("spool").join(PUSH_DIR).join(id.as_str());
    assert_eq!(fs::read(path).unwrap(), bundle);
}

/// A put past the cap is refused before any file is made (decision CH3:
/// the cap of the upstream).
#[test]
fn a_put_over_the_cap_is_refused_and_leaves_no_file() {
    let scratch = Scratch::new("put-cap");
    let _server = scratch.serve();
    let over = format!("put {}\n", BUNDLE_CAP + 1);
    assert_eq!(scratch.ask(over.as_bytes()), b"err too_large\n");
    let past = b"put 99999999999999999999999\n";
    assert_eq!(scratch.ask(past), b"err too_large\n");
    assert!(scratch.names(PUSH_DIR).is_empty());
}

/// A put whose guest stops before its size gets `short_body`, and its
/// temporary file goes.
#[test]
fn a_short_put_leaves_no_file() {
    let scratch = Scratch::new("put-short");
    let _server = scratch.serve();
    assert_eq!(scratch.ask(b"put 100\n0123456789"), b"err short_body\n");
    assert!(scratch.names(PUSH_DIR).is_empty());
}

/// A put needs the `push` grant and a get the `fetch` grant, each read at
/// the request. A grant file that does not read grants nothing.
#[test]
fn each_request_needs_its_grant() {
    let scratch = Scratch::new("grant");
    fs::write(scratch.dir.join("spool/fetch").join(FETCHED), b"x").unwrap();
    let _server = scratch.serve();
    let get = format!("get {FETCHED}\n");
    scratch.grant(r#"{"fetch":true,"push":false}"#);
    assert_eq!(scratch.ask(b"put 5\n"), b"err not_granted\n");
    assert_eq!(scratch.ask(get.as_bytes()), b"ok 1\nx");
    scratch.grant(r#"{"fetch":false,"push":true}"#);
    assert_eq!(scratch.ask(get.as_bytes()), b"err not_granted\n");
    for bad in ["{", ""] {
        scratch.grant(bad);
        assert_eq!(scratch.ask(b"put 5\n"), b"err not_granted\n");
        assert_eq!(scratch.ask(get.as_bytes()), b"err not_granted\n");
    }
    fs::remove_file(scratch.dir.join("families/code/git-platform.json")).unwrap();
    assert_eq!(scratch.ask(get.as_bytes()), b"err not_granted\n");
    assert!(scratch.names(PUSH_DIR).is_empty());
}

/// A get sends `ok <size>`, then the bytes of `fetch/<id>`.
#[test]
fn a_get_sends_the_bundle() {
    let scratch = Scratch::new("get");
    let bundle: Vec<u8> = (0..700_000_u32)
        .map(|index| u8::try_from(index % 253).unwrap())
        .collect();
    fs::write(scratch.dir.join("spool/fetch").join(FETCHED), &bundle).unwrap();
    let _server = scratch.serve();
    let answer = scratch.ask(format!("get {FETCHED}\n").as_bytes());
    let mut wanted = b"ok 700000\n".to_vec();
    wanted.extend_from_slice(&bundle);
    assert_eq!(answer, wanted);
}

/// A get of an id of another family is refused, also when `fetch/` holds
/// the file. The family is the one of the VM, never of the guest bytes.
#[test]
fn a_get_of_another_family_is_refused() {
    let scratch = Scratch::new("get-other");
    let other = "chat-01JC9Z6A0B1C2D3E4F5G6H7J8K.bundle";
    fs::write(scratch.dir.join("spool/fetch").join(other), b"secret").unwrap();
    let _server = scratch.serve();
    let answer = scratch.ask(format!("get {other}\n").as_bytes());
    assert_eq!(answer, b"err other_family\n");
}

#[test]
fn a_get_of_a_missing_id_is_refused() {
    let scratch = Scratch::new("get-missing");
    let _server = scratch.serve();
    let answer = scratch.ask(format!("get {FETCHED}\n").as_bytes());
    assert_eq!(answer, b"err not_found\n");
}

/// A malformed id is refused, and so is the temporary name of the
/// upstream, so no guest names a path of its own.
#[test]
fn a_malformed_id_is_refused() {
    let scratch = Scratch::new("get-bad");
    let part = format!(".{FETCHED}.part");
    fs::write(scratch.dir.join("spool/fetch").join(&part), b"x").unwrap();
    let _server = scratch.serve();
    for id in [
        "../push/x",
        "code",
        part.as_str(),
        "code-01JC9Z6A0B1C2D3E4F5G6H7J8K",
    ] {
        let answer = scratch.ask(format!("get {id}\n").as_bytes());
        assert_eq!(answer, b"err bad_id\n", "{id}");
    }
}

/// A line that is not one of the two forms, a line past the cap, a CR
/// and an EOF before the LF are each `bad_request`.
#[test]
fn a_bad_request_line_is_refused() {
    let scratch = Scratch::new("bad");
    let _server = scratch.serve();
    let long = format!("get {}\n", "a".repeat(LINE_CAP));
    let refused: [&[u8]; 5] = [
        b"hello\n",
        b"put 0\n",
        b"put 5\r\n",
        b"put 5",
        long.as_bytes(),
    ];
    for request in refused {
        assert_eq!(
            scratch.ask(request),
            b"err bad_request\n",
            "{}",
            request.escape_ascii()
        );
    }
    assert!(scratch.names(PUSH_DIR).is_empty());
}

/// Each new put keeps at most `PUSH_KEEP` bundles of the VM, the newest
/// ones (step 6.1 of `src/spool.rs`).
#[test]
fn the_vm_keeps_at_most_push_keep_bundles() {
    let scratch = Scratch::new("keep");
    let _server = scratch.serve();
    let ids: Vec<BundleId> = (0..=PUSH_KEEP)
        .map(|index| put_id(&scratch.put(format!("bundle {index}").as_bytes())))
        .collect();
    scratch.wait_for_push(PUSH_KEEP);
    let mut kept: Vec<String> = ids[1..].iter().map(ToString::to_string).collect();
    kept.sort();
    assert_eq!(scratch.names(PUSH_DIR), kept);
}

/// The end of the VM removes each bundle that it put (step 6.2): the drop
/// of `Server`. A file of another writer stays.
#[test]
fn the_drop_of_the_server_removes_its_bundles() {
    let scratch = Scratch::new("drop");
    let other = "code-01JC9Z6A0B1C2D3E4F5G6H7J8K.bundle";
    fs::write(scratch.dir.join("spool/push").join(other), b"").unwrap();
    let server = scratch.serve();
    put_id(&scratch.put(b"bundle"));
    assert_eq!(scratch.names(PUSH_DIR).len(), 2);
    drop(server);
    scratch.wait_for_push(1);
    assert_eq!(scratch.names(PUSH_DIR), [other]);
}

/// A closed listener keeps its open links, and a reopen serves the same
/// spool (decision CH74 rule 2).
#[test]
fn a_reopen_keeps_the_bundles_of_the_vm() {
    let scratch = Scratch::new("reopen");
    let server = scratch.serve();
    let id = put_id(&scratch.put(b"bundle"));
    let draining = scratch.runtime.block_on(server.close());
    fs::remove_file(scratch.run.host_listener(HostPort::GitPlatformSpool)).unwrap();
    let bound = bind(&scratch.run).unwrap();
    let reopened = {
        let _runtime = scratch.runtime.enter();
        draining.reopen(bound).unwrap()
    };
    assert_eq!(scratch.names(PUSH_DIR), [id.to_string()]);
    put_id(&scratch.put(b"second"));
    assert_eq!(scratch.names(PUSH_DIR).len(), 2);
    drop(reopened);
    scratch.wait_for_push(0);
}

/// A link past `LINK_CAP` is accepted and closed at once, with no reply.
#[test]
fn a_link_past_the_cap_closes_at_once() {
    let scratch = Scratch::new("cap");
    let _server = scratch.serve();
    let held: Vec<UnixStream> = (0..LINK_CAP)
        .map(|_| {
            let mut stream = scratch.dial();
            stream.write_all(b"put 10\n").unwrap();
            stream
        })
        .collect();
    let mut extra = scratch.dial();
    let mut answer = Vec::new();
    if let Err(error) = extra.read_to_end(&mut answer) {
        assert_eq!(error.kind(), ErrorKind::ConnectionReset, "{error}");
    }
    assert!(answer.is_empty(), "{answer:?}");
    // Each held link ends with `short_body` and removes its file. The
    // listener reaps an ended link at its next accept.
    drop(held);
    scratch.wait_for_push(0);
    let deadline = Instant::now() + WAIT;
    while !scratch.put(b"after").starts_with("ok ") {
        assert!(Instant::now() < deadline, "the held links did not end");
        std::thread::sleep(Duration::from_millis(5));
    }
}

/// The raise of `vmm serve` holds every spool at its cap: per VM the
/// listener, per link its stream and two more fds.
#[test]
fn the_fd_need_holds_every_full_spool() {
    assert_eq!(fd_need(), (3 * 4 + 1) * 64);
}
