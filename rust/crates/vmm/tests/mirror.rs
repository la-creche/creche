//! The git mirror of host port 1103 (plan PR M1, decisions CH10-10, CH46
//! rule 4, CH69 rules 13 to 17) on a temp Unix socket in the test process.
//! A test dials `vsock.sock_1103` as Cloud Hypervisor does: hybrid vsock
//! connects to the host socket with no handshake. A clone test runs a real
//! `git clone git://127.0.0.1:<port>/<name>` through a TCP to Unix forward
//! in this file, as guest init bridges guest 127.0.0.1:9418. A scratch
//! family root holds the family `code` with a `repos.json`, and a scratch
//! mirror holds real repos. No test reaches the network.

use std::io::{ErrorKind, Read, Write};
use std::net::{Shutdown, TcpListener, TcpStream};
use std::os::unix::fs::{PermissionsExt, symlink};
use std::os::unix::net::UnixStream;
use std::path::{Path, PathBuf};
use std::process::{Command, Output, Stdio};
use std::thread::spawn;
use std::time::{Duration, Instant};

use creche_vmm::egress::proxy;
use creche_vmm::mirror::{self, DENIED, LINE_CAP, LINE_WAIT, LINK_CAP, Mirror, Server, bind};
use creche_vmm::{family_state, planes};
use creche_vmm_confine::family::FamilyRoot;
use creche_vmm_confine::launch::Program;
use creche_vmm_confine::mirror::MirrorRoot;
use creche_vmm_store::layout::{RunRoot, Slot, VmRunDir};
use creche_wire::{HostPort, SessionId};
use tokio::runtime::{Builder, Runtime};

/// The bound of each wait of a test.
const WAIT: Duration = Duration::from_secs(30);

/// The session of the VM.
const SESSION: &str = "3f2b8c1e-9d4a-4e6b-8a7c-0b1d2e3f4a5b";

/// The git of the host: the server runs it, and so does each client.
const HOST_GIT: &str = "/usr/bin/git";

/// The clone list of the family `code`.
const REPOS: &str = r#"{"repos":["creche","gone"]}"#;

/// The run dir of one VM, the family root and the mirror in `/tmp`, and
/// the runtime of the mirror. Its drop removes the dir, also after a
/// failed assert.
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
    /// it, and the temp dir of macOS is too long. The mirror holds the
    /// repos `creche` and `other`, each with one commit, and the list
    /// names `creche` and `gone`, which the mirror lacks.
    fn new(name: &str, runtime: Runtime) -> Self {
        let dir = PathBuf::from(format!("/tmp/vmm-mirror-{}-{name}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(dir.join("families/code")).unwrap();
        std::fs::create_dir_all(dir.join("mirror")).unwrap();
        let root = RunRoot::new(dir.join("run")).unwrap();
        let run = root.link_vm(&session(), Slot::new(0).unwrap()).unwrap();
        let families = FamilyRoot::new(dir.join("families")).unwrap();
        let scratch = Self {
            dir,
            run,
            families,
            runtime,
        };
        for repo in ["creche", "other"] {
            scratch.make_repo(repo);
        }
        scratch.repos(REPOS);
        scratch
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

    /// A repo `<mirror>/<name>` with one commit, as the mirror sync keeps
    /// it.
    fn make_repo(&self, name: &str) {
        let repo = self.dir.join("mirror").join(name);
        run(git(&self.dir).args(["init", "--quiet"]).arg(&repo));
        let mut commit = git(&self.dir);
        commit.arg("-C").arg(&repo);
        commit.args(["-c", "user.name=t", "-c", "user.email=t@example.com"]);
        run(commit.args(["commit", "--quiet", "--allow-empty", "-m", name]));
    }

    /// The commit of HEAD of the git dir or work tree `repo`.
    fn head(&self, repo: &Path) -> String {
        let output = run(git(&self.dir)
            .arg("-C")
            .arg(repo)
            .args(["rev-parse", "HEAD"]));
        String::from_utf8(output.stdout).unwrap().trim().to_owned()
    }

    /// Binds and serves the mirror of the family `code` with `git`.
    fn serve_with(&self, git: &Path) -> Server {
        let bound = bind(&self.run).unwrap();
        let _runtime = self.runtime.enter();
        let mirror = Mirror {
            root: MirrorRoot::new(self.dir.join("mirror")).unwrap(),
            git: Program::new(git.to_owned()).unwrap(),
        };
        let family = "code".parse().unwrap();
        bound
            .serve(mirror, self.families.clone(), family, session())
            .unwrap()
    }

    /// A git program `name` in the scratch dir: a shell script of `body`.
    /// The script goes through a copy, so no open fd of the write is left
    /// for a fork of another test thread to keep, which would make the
    /// exec fail with ETXTBSY.
    fn fake_git(&self, name: &str, body: &str) -> PathBuf {
        let program = self.dir.join(name);
        let source = self.dir.join(format!("{name}.src"));
        std::fs::write(&source, format!("#!/bin/sh\n{body}")).unwrap();
        run(Command::new("cp").arg(&source).arg(&program));
        std::fs::set_permissions(&program, std::fs::Permissions::from_mode(0o755)).unwrap();
        program
    }

    /// Binds and serves the mirror with the git of the host.
    fn serve(&self) -> Server {
        self.serve_with(Path::new(HOST_GIT))
    }

    /// Writes `repos.json` of the family `code`.
    fn repos(&self, text: &str) {
        std::fs::write(self.dir.join("families/code/repos.json"), text).unwrap();
    }

    /// A guest dial of 1103, as CH makes it.
    fn dial(&self) -> UnixStream {
        let stream = UnixStream::connect(self.run.host_listener(HostPort::GitMirror)).unwrap();
        stream.set_read_timeout(Some(WAIT)).unwrap();
        stream
    }

    /// Sends `bytes` on a new link, half-closes it and gives every byte of
    /// the answer. On Linux a close by the vmm with bytes of the request
    /// still unread resets the link (rust job run 42420171022), so a reset
    /// counts as the end of the answer, and the half-close may then find the
    /// link gone.
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

    /// A TCP port on 127.0.0.1 whose each connection is spliced to a new
    /// dial of 1103, as guest init bridges guest port 9418.
    fn forward(&self) -> u16 {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        let path = self.run.host_listener(HostPort::GitMirror);
        spawn(move || {
            for tcp in listener.incoming() {
                let tcp = tcp.unwrap();
                let unix = UnixStream::connect(&path).unwrap();
                splice(tcp, unix);
            }
        });
        port
    }

    /// Runs `git clone` of `name` over `port` with protocol `version` and
    /// `GIT_TRACE_PACKET`, into a new dir. It gives the output and the dir.
    fn clone(&self, port: u16, name: &str, version: u8) -> (Output, PathBuf) {
        let dest = self
            .dir
            .join(format!("clone-{name}-{version}-{}", unique()));
        let url = format!("git://127.0.0.1:{port}/{name}");
        let mut command = git(&self.dir);
        command.env("GIT_TRACE_PACKET", "1");
        command.args(["-c", &format!("protocol.version={version}")]);
        command.args(["clone", "--quiet", &url]).arg(&dest);
        (bounded(&mut command), dest)
    }
}

/// A number that differs on each call, for the name of a clone dir.
#[cfg(test)]
fn unique() -> u128 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_nanos()
}

/// Copies bytes both ways between `tcp` and `unix`, and half-closes the
/// far side at each EOF.
#[cfg(test)]
fn splice(tcp: TcpStream, unix: UnixStream) {
    let (mut tcp_in, mut unix_out) = (tcp.try_clone().unwrap(), unix.try_clone().unwrap());
    spawn(move || {
        let _ = std::io::copy(&mut tcp_in, &mut unix_out);
        let _ = unix_out.shutdown(Shutdown::Write);
    });
    let (mut unix_in, mut tcp_out) = (unix, tcp);
    spawn(move || {
        let _ = std::io::copy(&mut unix_in, &mut tcp_out);
        let _ = tcp_out.shutdown(Shutdown::Write);
    });
}

/// A `git` command with no config of the host or the user.
#[cfg(test)]
fn git(home: &Path) -> Command {
    let mut command = Command::new(HOST_GIT);
    command
        .env_clear()
        .env("GIT_CONFIG_NOSYSTEM", "1")
        .env("GIT_CONFIG_GLOBAL", "/dev/null")
        .env("HOME", home)
        .env("PATH", "/usr/bin:/bin");
    command
}

/// Runs `command` within `WAIT` and gives its output. A run past `WAIT`
/// gets SIGKILL and fails the test.
#[cfg(test)]
fn bounded(command: &mut Command) -> Output {
    let mut child = command
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    let deadline = Instant::now() + WAIT;
    while child.try_wait().unwrap().is_none() {
        if Instant::now() >= deadline {
            child.kill().unwrap();
            panic!("{command:?} ran past {WAIT:?}");
        }
        std::thread::sleep(Duration::from_millis(10));
    }
    child.wait_with_output().unwrap()
}

/// Runs `command` within `WAIT`, and fails the test unless it succeeds.
#[cfg(test)]
fn run(command: &mut Command) -> Output {
    let output = bounded(command);
    let stderr = String::from_utf8_lossy(&output.stderr);
    assert!(output.status.success(), "{command:?}: {stderr}");
    output
}

#[cfg(test)]
fn session() -> SessionId {
    SESSION.parse().unwrap()
}

/// The pkt-line of `payload`: 4 hex digits of the whole length, then the
/// payload.
#[cfg(test)]
fn pkt(payload: &[u8]) -> Vec<u8> {
    let mut line = format!("{:04x}", payload.len() + 4).into_bytes();
    line.extend_from_slice(payload);
    line
}

/// The first pkt-line of a v0 clone of `path`, as git writes it.
#[cfg(test)]
fn request(path: &str) -> Vec<u8> {
    pkt(format!("git-upload-pack {path}\0host=127.0.0.1:9418\0").as_bytes())
}

/// The phase 7 proof line of plan PR M1: a repo that the mirror holds and
/// the list does not name is not cloned, and git reports the one error.
#[test]
fn a_clone_outside_repos_fails() {
    let scratch = Scratch::threaded("outside");
    let _server = scratch.serve();
    let port = scratch.forward();
    let (output, dest) = scratch.clone(port, "other", 0);
    assert!(!output.status.success());
    let stderr = String::from_utf8_lossy(&output.stderr);
    assert!(stderr.contains("remote error: access denied"), "{stderr}");
    assert!(!dest.join(".git").exists());
    assert_eq!(scratch.ask(&request("/other")), DENIED);
}

/// A clone of a listed repo that the mirror holds gets its HEAD (decision
/// CH10-10).
#[test]
fn a_clone_of_a_listed_repo_matches_the_mirror_head() {
    let scratch = Scratch::threaded("listed");
    let _server = scratch.serve();
    let port = scratch.forward();
    let (output, dest) = scratch.clone(port, "creche", 0);
    let stderr = String::from_utf8_lossy(&output.stderr);
    assert!(output.status.success(), "{stderr}");
    // A v0 request sets no `GIT_PROTOCOL`, so the server answers in v0
    // (decision CH69 rule 16).
    assert!(!stderr.contains("< version 2"), "{stderr}");
    let mirror = scratch.dir.join("mirror/creche");
    assert_eq!(scratch.head(&dest), scratch.head(&mirror));
}

/// `upload-pack` gets the exact argv, only the two config lines as its
/// env, and `/dev/null` as stderr (decisions CH46 rule 4, CH69 rules 16
/// and 17). The git program of the test writes its args, its env and
/// whether fd 2 is `/dev/null`, then one pkt-line.
#[test]
fn upload_pack_gets_its_args_and_only_its_env() {
    let scratch = Scratch::threaded("argv");
    let (args, env, stderr) = (
        scratch.dir.join("args"),
        scratch.dir.join("env"),
        scratch.dir.join("stderr"),
    );
    let body = format!(
        "printf '%s\\n' \"$@\" > '{}'\n/usr/bin/env > '{}'\n\
         if [ /dev/fd/2 -ef /dev/null ]; then echo null; else echo other; fi > '{}'\n\
         printf '0008run\\n'\n",
        args.display(),
        env.display(),
        stderr.display()
    );
    let _server = scratch.serve_with(&scratch.fake_git("git-records", &body));
    assert_eq!(scratch.ask(&request("/creche")), b"0008run\n");
    let git_dir = scratch.dir.join("mirror/creche/.git");
    let want = format!(
        "upload-pack\n--strict\n--timeout=60\n{}\n",
        git_dir.display()
    );
    assert_eq!(std::fs::read_to_string(&args).unwrap(), want);
    // sh itself adds `PWD`, `SHLVL` and `_`.
    let env = std::fs::read_to_string(&env).unwrap();
    let mut lines: Vec<&str> = env
        .lines()
        .filter(|line| {
            !["PWD=", "SHLVL=", "_="]
                .iter()
                .any(|key| line.starts_with(key))
        })
        .collect();
    lines.sort_unstable();
    assert_eq!(
        lines,
        ["GIT_CONFIG_GLOBAL=/dev/null", "GIT_CONFIG_NOSYSTEM=1"]
    );
    assert_eq!(std::fs::read_to_string(&stderr).unwrap(), "null\n");
}

/// A v2 request sets `GIT_PROTOCOL`, so `upload-pack` answers in v2: the
/// trace of the client reads `version 2` from the server (decision CH69
/// rule 16).
#[test]
fn a_v2_clone_of_a_listed_repo_works() {
    let scratch = Scratch::threaded("v2");
    let _server = scratch.serve();
    let port = scratch.forward();
    let (output, dest) = scratch.clone(port, "creche", 2);
    let stderr = String::from_utf8_lossy(&output.stderr);
    assert!(output.status.success(), "{stderr}");
    assert!(stderr.contains("< version 2"), "{stderr}");
    let mirror = scratch.dir.join("mirror/creche");
    assert_eq!(scratch.head(&dest), scratch.head(&mirror));
}

/// A listed repo that the mirror lacks gets the same bytes as a repo that
/// the list does not name, so a family cannot probe the mirror (decision
/// CH69 rule 16).
#[test]
fn a_repo_in_repos_but_not_in_the_mirror_gets_the_same_err() {
    let scratch = Scratch::threaded("same");
    let _server = scratch.serve();
    assert_eq!(DENIED, b"0016ERR access denied\n");
    assert_eq!(scratch.ask(&request("/gone")), DENIED);
    assert_eq!(scratch.ask(&request("/other")), DENIED);
    assert_eq!(scratch.ask(&request("/absent")), DENIED);
}

/// Each link reads the list again, so a removed repo is refused at the
/// next link with no restart of the mirror (decision CH69 rule 16).
#[test]
fn a_removed_repo_is_refused_at_the_next_link() {
    let scratch = Scratch::threaded("removed");
    let _server = scratch.serve();
    let first = scratch.ask(&request("/creche"));
    assert_ne!(first, DENIED);
    assert!(!first.is_empty());
    scratch.repos(r#"{"repos":["gone"]}"#);
    assert_eq!(scratch.ask(&request("/creche")), DENIED);
    std::fs::remove_file(scratch.dir.join("families/code/repos.json")).unwrap();
    assert_eq!(scratch.ask(&request("/creche")), DENIED);
}

/// Only `/<name>` with the exact name passes: no `.git` suffix, no `~`,
/// no `..`, no second part (decisions CH46 rule 4, CH69 rule 16).
#[test]
fn a_path_with_dot_git_tilde_or_dot_dot_is_refused() {
    let scratch = Scratch::threaded("paths");
    let _server = scratch.serve();
    for path in [
        "/creche.git",
        "/~creche",
        "/~david/creche",
        "/../creche",
        "/creche/..",
        "/creche/",
        "//creche",
        "creche",
        "/CRECHE",
        "/creche/.git",
    ] {
        assert_eq!(scratch.ask(&request(path)), DENIED, "{path}");
    }
}

/// Only `git-upload-pack` passes: a push and an archive are refused
/// (decision CH46 rule 4).
#[test]
fn receive_pack_is_refused() {
    let scratch = Scratch::threaded("receive");
    let _server = scratch.serve();
    for command in ["git-receive-pack", "git-upload-archive"] {
        let line = pkt(format!("{command} /creche\0host=127.0.0.1\0").as_bytes());
        assert_eq!(scratch.ask(&line), DENIED, "{command}");
    }
}

/// A listed name whose mirror dir, or whose `.git`, is a symlink is
/// refused, also when it points at a real repo (decision CH69 rule 16).
#[test]
fn a_symlinked_mirror_repo_is_refused() {
    let scratch = Scratch::threaded("symlink");
    let mirror = scratch.dir.join("mirror");
    symlink(mirror.join("creche"), mirror.join("linked")).unwrap();
    std::fs::create_dir(mirror.join("inner")).unwrap();
    symlink(mirror.join("creche/.git"), mirror.join("inner/.git")).unwrap();
    scratch.repos(r#"{"repos":["linked","inner"]}"#);
    let _server = scratch.serve();
    assert_eq!(scratch.ask(&request("/linked")), DENIED);
    assert_eq!(scratch.ask(&request("/inner")), DENIED);
}

/// A first pkt-line of `LINE_CAP` bytes is read whole, and a length past
/// it is refused and closed with no more read (decision CH69 rule 16).
#[test]
fn a_first_line_past_1024_bytes_is_closed() {
    assert_eq!(LINE_CAP, 1024);
    let scratch = Scratch::threaded("cap");
    let _server = scratch.serve();
    let head = "git-upload-pack /creche\0host=";
    let host = "h".repeat(LINE_CAP - 4 - head.len() - 1);
    let at_cap = pkt(format!("{head}{host}\0").as_bytes());
    assert_eq!(at_cap.len(), LINE_CAP);
    let answer = scratch.ask(&at_cap);
    assert!(!answer.is_empty() && answer != DENIED);
    let mut past = b"0401".to_vec();
    past.extend(std::iter::repeat_n(b'x', LINE_CAP - 3));
    assert_eq!(scratch.ask(&past), DENIED);
    for bad in [&b"0000"[..], b"0003", b"00G0", b"0016ERR"] {
        assert_eq!(scratch.ask(bad), DENIED, "{}", bad.escape_ascii());
    }
}

/// A first line that is not whole after `LINE_WAIT` closes the link with
/// no answer. The tokio clock is paused, so the 10 s pass at once.
#[test]
fn a_first_line_slower_than_10_s_is_closed() {
    assert_eq!(LINE_WAIT, Duration::from_secs(10));
    let runtime = Builder::new_current_thread()
        .enable_all()
        .start_paused(true)
        .build()
        .unwrap();
    let scratch = Scratch::new("slow", runtime);
    let _server = scratch.serve();
    let path = scratch.run.host_listener(HostPort::GitMirror);
    let (early, read) = scratch.runtime.block_on(async {
        let guest = tokio::net::UnixStream::connect(path).await.unwrap();
        guest.writable().await.unwrap();
        let part = b"0032git-upload-pack /cre";
        assert_eq!(guest.try_write(part).unwrap(), part.len());
        let mut buffer = [0; 64];
        let early = LINE_WAIT - Duration::from_secs(1);
        let early = tokio::time::timeout(early, guest.readable()).await.is_err();
        let started = tokio::time::Instant::now();
        guest.readable().await.unwrap();
        let read = guest.try_read(&mut buffer).unwrap();
        assert!(started.elapsed() <= Duration::from_secs(2));
        (early, read)
    });
    assert!(early, "the link closed before the wait");
    assert_eq!(read, 0, "the link got an answer");
}

/// The link past `LINK_CAP` is accepted and closed at once (decisions
/// CH67 rule 3, CH69 rule 14).
#[test]
fn the_65th_link_is_closed_at_once() {
    assert_eq!(LINK_CAP, 64);
    let scratch = Scratch::threaded("links");
    let _server = scratch.serve();
    let held: Vec<UnixStream> = (0..LINK_CAP).map(|_| scratch.dial()).collect();
    let mut past = scratch.dial();
    let started = Instant::now();
    let mut bytes = Vec::new();
    past.read_to_end(&mut bytes).unwrap();
    assert!(bytes.is_empty());
    assert!(started.elapsed() < LINE_WAIT);
    drop(held);
}

/// A link that ended frees its place under `LINK_CAP`: `LINK_CAP` + 1
/// links one after another each get `DENIED`, so the listener reaps each
/// link that ended (decisions CH67 rule 3, CH69 rule 14).
#[test]
fn links_one_after_another_pass_the_cap() {
    let scratch = Scratch::threaded("serial");
    let _server = scratch.serve();
    for link in 0..=LINK_CAP {
        assert_eq!(scratch.ask(&request("/other")), DENIED, "link {link}");
    }
}

/// The child of `a_bad_clone_logs_one_line_and_a_good_clone_none`, run in
/// a process of its own, so its stderr holds only the lines of the vmm. It
/// runs a good clone, then a repo that the list does not name, a listed
/// repo that the mirror lacks, a clone with no mirror root and a link
/// past the cap.
#[test]
#[ignore = "the child of a_bad_clone_logs_one_line_and_a_good_clone_none"]
fn clones_for_the_log_test() {
    let scratch = Scratch::threaded("log");
    let server = scratch.serve();
    let port = scratch.forward();
    let (output, _) = scratch.clone(port, "creche", 0);
    assert!(output.status.success());
    assert_eq!(scratch.ask(&request("/other")), DENIED);
    assert_eq!(scratch.ask(&request("/gone")), DENIED);
    let mirror = scratch.dir.join("mirror");
    std::fs::rename(&mirror, scratch.dir.join("mirror-gone")).unwrap();
    assert_eq!(scratch.ask(&request("/creche")), DENIED);
    let held: Vec<UnixStream> = (0..LINK_CAP).map(|_| scratch.dial()).collect();
    let mut past = scratch.dial();
    let mut bytes = Vec::new();
    past.read_to_end(&mut bytes).unwrap();
    assert!(bytes.is_empty());
    // The drop of `Server` cuts the held links. Tokio ends an aborted task
    // later, so the test waits until each held link reads the end of its
    // cut before it closes its own side. Else a link task that still runs
    // reads that close as an empty request and writes a `bad_request` line
    // (rust job run 42421954640).
    drop(server);
    for mut link in held {
        let mut bytes = Vec::new();
        if let Err(error) = link.read_to_end(&mut bytes) {
            assert_eq!(error.kind(), ErrorKind::ConnectionReset, "{error}");
        }
        assert!(bytes.is_empty());
    }
}

/// A refused or faulted clone writes one stderr line, and a good clone
/// none (decision CH69 rule 24). The test runs its own binary as the
/// child `clones_for_the_log_test` and reads the real stderr of the vmm
/// code.
#[test]
fn a_bad_clone_logs_one_line_and_a_good_clone_none() {
    let output = Command::new(std::env::current_exe().unwrap())
        .args(["--exact", "clones_for_the_log_test", "--ignored"])
        .args(["--nocapture", "--test-threads=1"])
        .output()
        .unwrap();
    let stderr = String::from_utf8(output.stderr).unwrap();
    let stdout = String::from_utf8(output.stdout).unwrap();
    assert!(output.status.success(), "{stdout}{stderr}");
    assert!(stdout.contains("1 passed"), "{stdout}");
    let line = |text: &str| format!("vmm: a clone of the family code, session {SESSION}, {text}");
    let want = [
        line("refused: not_listed"),
        line("refused: not_in_mirror"),
        line("faulted: the mirror root: entity not found"),
        line("refused: link_cap"),
    ];
    assert_eq!(stderr.lines().collect::<Vec<_>>(), want, "{stderr}");
}

/// The drop of `Server` kills the child of an open clone and closes 1103.
/// The git program of the test writes its pid and one pkt-line, then
/// execs `sleep`, which reads no stdin. So only the kill of the drop ends
/// it: the close of its pipes does not.
#[test]
fn the_child_dies_when_the_server_drops() {
    let scratch = Scratch::threaded("child");
    let pid_file = scratch.dir.join("git.pid");
    let body = format!(
        "echo $$ > '{}'\nprintf '0008run\\n'\nexec /bin/sleep 600\n",
        pid_file.display()
    );
    let server = scratch.serve_with(&scratch.fake_git("git-sleeps", &body));
    let mut held = scratch.dial();
    held.write_all(&request("/creche")).unwrap();
    let mut first = [0; 8];
    held.read_exact(&mut first).unwrap();
    assert_eq!(&first, b"0008run\n");
    let pid = read_pid(&pid_file);
    assert!(alive(&pid), "the child of pid {pid} does not run");
    drop(server);
    wait_dead(&pid);
    let path = scratch.run.host_listener(HostPort::GitMirror);
    let refused = UnixStream::connect(path).err().map(|error| error.kind());
    assert_eq!(refused, Some(std::io::ErrorKind::ConnectionRefused));
}

/// The drop of `Server` kills the whole process group of the child, so a
/// child of git, as `pack-objects` is, dies too (decision CH69 rule 14).
/// The git program of the test starts a `sleep` in the background, writes
/// both pids and one pkt-line, then execs `sleep`.
#[test]
fn a_grandchild_of_git_dies_when_the_server_drops() {
    let scratch = Scratch::threaded("grandchild");
    let (pid_file, grandchild_file) = (scratch.dir.join("git.pid"), scratch.dir.join("sub.pid"));
    let body = format!(
        "/bin/sleep 600 &\necho $! > '{}'\necho $$ > '{}'\nprintf '0008run\\n'\nexec /bin/sleep 600\n",
        grandchild_file.display(),
        pid_file.display()
    );
    let server = scratch.serve_with(&scratch.fake_git("git-forks", &body));
    let mut held = scratch.dial();
    held.write_all(&request("/creche")).unwrap();
    let mut first = [0; 8];
    held.read_exact(&mut first).unwrap();
    assert_eq!(&first, b"0008run\n");
    let (pid, grandchild) = (read_pid(&pid_file), read_pid(&grandchild_file));
    assert!(alive(&grandchild), "{grandchild} does not run");
    drop(server);
    wait_dead(&pid);
    wait_dead(&grandchild);
}

/// A link ends when the child exits and each of its bytes reached the
/// guest, also while the guest keeps the link open and sends nothing. The
/// child is reaped first, so no zombie waits for the guest (decision CH69
/// rule 14). The git program of the test writes its pid and one pkt-line,
/// then exits.
#[test]
fn a_link_ends_when_git_exits() {
    let scratch = Scratch::threaded("exits");
    let pid_file = scratch.dir.join("git.pid");
    let body = format!("echo $$ > '{}'\nprintf '0008run\\n'\n", pid_file.display());
    let _server = scratch.serve_with(&scratch.fake_git("git-exits", &body));
    let mut held = scratch.dial();
    held.write_all(&request("/creche")).unwrap();
    let mut answer = Vec::new();
    held.read_to_end(&mut answer).unwrap();
    assert_eq!(answer, b"0008run\n");
    let pid = read_pid(&pid_file);
    let deadline = Instant::now() + WAIT;
    while !gone(&pid) {
        assert!(Instant::now() < deadline, "the pid {pid} is not reaped");
        std::thread::sleep(Duration::from_millis(10));
    }
    // The vmm closes the link after the reap, so a write fails then.
    while held.write_all(b"0000").is_ok() {
        assert!(Instant::now() < deadline, "the link stays open");
        std::thread::sleep(Duration::from_millis(10));
    }
}

/// Whether no process has the pid `pid`, also no zombie.
#[cfg(test)]
fn gone(pid: &str) -> bool {
    let output = Command::new("ps")
        .args(["-o", "stat=", "-p", pid])
        .output()
        .unwrap();
    String::from_utf8_lossy(&output.stdout).trim().is_empty()
}

/// The pid that a git program of a test wrote to `file`.
#[cfg(test)]
fn read_pid(file: &Path) -> String {
    std::fs::read_to_string(file).unwrap().trim().to_owned()
}

/// Waits until the process `pid` no longer runs, and fails the test after
/// `WAIT`.
#[cfg(test)]
fn wait_dead(pid: &str) {
    let deadline = Instant::now() + WAIT;
    while alive(pid) {
        assert!(Instant::now() < deadline, "the pid {pid} runs on");
        std::thread::sleep(Duration::from_millis(10));
    }
}

/// Whether the process `pid` runs: `ps` lists it, and not as a zombie.
#[cfg(test)]
fn alive(pid: &str) -> bool {
    let output = Command::new("ps")
        .args(["-o", "stat=", "-p", pid])
        .output()
        .unwrap();
    let stat = String::from_utf8_lossy(&output.stdout);
    let stat = stat.trim();
    !stat.is_empty() && !stat.starts_with('Z')
}

/// The raise of `vmm serve` holds every mirror at its cap (decisions CH67
/// rule 4, CH69 rule 14): per VM the listener, per link its stream and
/// seven more fds.
#[test]
fn the_fd_need_holds_every_full_mirror() {
    assert_eq!(mirror::fd_need(), (8 * 64 + 1) * 64);
    let raise = planes::fd_need() + family_state::fd_need() + mirror::fd_need() + proxy::fd_need();
    assert_eq!(raise, 102_720);
}
