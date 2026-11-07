//! The git mirror on host port 1103 (plan PR M1, decisions CH3, CH5f,
//! CH10-10, CH46 rule 4, CH69 rules 13 to 20, 24 and 30). Guest init
//! splices guest 127.0.0.1:9418 to a vsock dial of host port 1103
//! (decision CH46 rule 4), and Cloud Hypervisor connects each dial to
//! `vsock.sock_1103` in the run dir of the VM (decision CH24). The vmm
//! listens there only for a family whose `repos.json` lists a repo
//! (decision CH69 rule 13). Without the listener the guest dial fails, so
//! a VM with no list fails closed.
//!
//! 1. `bind` binds the listener beside the plane listeners, with their
//!    lifecycle: after `RunRoot::link_vm` emptied the slot and after
//!    `remove_stale`, before CH starts, for a cold boot and for each
//!    restore (plan PR V6, decision CH56 rule 1).
//! 2. Each link reads the first pkt-line of `git://` within `LINE_WAIT`,
//!    at most `LINE_CAP` bytes (decision CH69 rule 16). A line that does
//!    not come whole within the wait closes the link with no reply.
//! 3. Only `git-upload-pack /<name>`, then `host=<any>`, then nothing or
//!    `version=2` passes (decision CH69 rule 16). `<name>` must be a
//!    `RepoName`. The host value is not read.
//! 4. Each link reads `repos.json` again (decision CH69 rule 16), so a
//!    removed repo refuses the next clone at once. `<name>` must equal an
//!    entry of the list byte for byte (decision CH46 rule 4), and the
//!    mirror must hold that entry: `MirrorRoot::git_dir` opens each part
//!    with `O_NOFOLLOW` from the root handle (decision CH10-10). The path
//!    is built from the list entry, never from the client bytes.
//! 5. Every refusal of steps 3 and 4 gets the one pkt-line `DENIED`, then
//!    the close, so a family cannot tell a repo that is not listed from
//!    one that the mirror lacks (decision CH69 rule 16).
//! 6. The vmm runs `<VMM_GIT_PROGRAM> upload-pack --strict --timeout=60
//!    <mirror>/<name>/.git` as itself, david (decisions CH46 rule 4, CH69
//!    rules 17 and 19), with a cleared env that holds only
//!    `GIT_CONFIG_NOSYSTEM=1`, `GIT_CONFIG_GLOBAL=/dev/null` and, for a v2
//!    request, `GIT_PROTOCOL=version=2`. stderr goes to `/dev/null`. The
//!    child advertises all refs (decision CH69 rule 18). `ChildIo` joins
//!    its stdout and stdin, and `relay::Splice` carries the bytes with the
//!    half-close of decision CH67 rule 9. The drop of the link kills the
//!    child.
//! 7. A refused or faulted clone writes one stderr line, at most
//!    `NOTE_CAP` per `NOTE_WINDOW` for one VM. A good clone writes none
//!    (decision CH69 rule 24).
//! 8. The listener holds at most `LINK_CAP` links. A link past the cap is
//!    accepted and closed at once (decisions CH67 rule 3, CH69 rule 14).
//!    A failed accept is logged by kind, and the loop pauses
//!    `ACCEPT_PAUSE` and accepts again (decisions CH40 rule 1, CH69 rule
//!    30).
//! 9. A link does not set `last_use` and does not block a snapshot
//!    (decision CH69 rule 20). A restore cuts each open clone (fact 45).
//! 10. The drop of `Server` closes the listener and every link. The VM
//!     entry of the start table holds it, so the listener goes with the
//!     VM.

use std::fmt;
use std::future::poll_fn;
use std::io;
use std::os::unix::net::UnixListener as StdListener;
use std::path::PathBuf;
use std::pin::Pin;
use std::process::Stdio;
use std::sync::{Arc, Mutex, PoisonError};
use std::task::{Context, Poll};
use std::time::{Duration, Instant};

use creche_vmm_confine::family::FamilyRoot;
use creche_vmm_confine::launch::Program;
use creche_vmm_confine::mirror::{MirrorFault, MirrorRoot};
use creche_vmm_store::layout::{SLOTS, VmRunDir};
use creche_wire::{FamilyName, HostPort, RepoName, SessionId};
use tokio::io::{AsyncRead, AsyncWrite, ReadBuf};
use tokio::net::{UnixListener, UnixStream};
use tokio::process::{Child, ChildStdin, ChildStdout, Command};
use tokio::task::{JoinHandle, JoinSet, spawn_blocking};
use tokio::time::{sleep, timeout};

use crate::egress::proxy::{read_some, write_all};
use crate::planes::{BindError, SPLICE_CAP};
use crate::relay::Splice;
use crate::server::ACCEPT_PAUSE;

/// The most bytes of the first pkt-line, its 4 length digits included
/// (decision CH69 rule 16).
pub const LINE_CAP: usize = 1024;

/// The bound of the wait for the whole first pkt-line (decision CH69
/// rule 16).
pub const LINE_WAIT: Duration = Duration::from_secs(10);

/// The inactivity bound of `upload-pack` (decisions CH46 rule 4, CH69
/// rule 16).
const UPLOAD_TIMEOUT: &str = "--timeout=60";

/// The one answer to every refused clone: a pkt-line of 22 bytes
/// (decision CH69 rule 16).
pub const DENIED: &[u8] = b"0016ERR access denied\n";

/// The most open links of the listener of one VM (decision CH69 rule 14,
/// as decision CH68 rule 1 for 1100).
pub const LINK_CAP: usize = SPLICE_CAP;

/// The most stderr lines of one VM in one `NOTE_WINDOW` (decision CH69
/// rule 24).
pub const NOTE_CAP: u32 = 100;

/// The window of `NOTE_CAP` (decision CH69 rule 24).
pub const NOTE_WINDOW: Duration = Duration::from_secs(10);

/// The most fds of one link beside its guest stream. The checks hold two
/// at once: the family dir and `repos.json`, then two dirs of the mirror.
/// The spawn holds both ends of the stdin and the stdout pipe,
/// `/dev/null` and both ends of the exec pipe of std, 7 at once. After
/// the spawn the link holds the stdin and stdout ends and the pidfd of
/// tokio. The steps run one after another, so the spawn bounds them.
const LINK_FDS: u64 = 7;

/// The fds that the listeners of all `SLOTS` need when each one holds
/// `LINK_CAP` links: per VM the listener, and per link its stream and
/// `LINK_FDS`. `vmm serve` adds it to the raise of decision CH67 rule 4.
// Decision CH69 rule 14: this need (32832) adds to the raise of 69888
// (decision CH68 rule 2 and the 1104 need), so the raise is 102720. The
// plan estimate was 20544: it counted 5 fds per link, and the spawn holds
// 7 at once.
#[must_use]
pub fn fd_need() -> u64 {
    let links = u64::try_from(LINK_CAP).unwrap_or(u64::MAX);
    links
        .saturating_mul(LINK_FDS.saturating_add(1))
        .saturating_add(1)
        .saturating_mul(u64::from(SLOTS))
}

/// What each clone of every VM runs: the mirror root and the `git` of
/// `upload-pack` (decision CH69 rule 17).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Mirror {
    /// `VMM_MIRROR_ROOT`.
    pub root: MirrorRoot,
    /// `VMM_GIT_PROGRAM`. The compose step checks its file.
    pub git: Program,
}

/// The listener of one VM, bound and not yet served. A dial that the guest
/// makes before `serve` waits in the backlog. The drop closes it.
#[derive(Debug)]
pub struct Bound(StdListener);

/// Binds `vsock.sock_1103` in `run` (decision CH24). It blocks, so the
/// start runs it in `spawn_blocking`. The slot is empty, so a path that is
/// there fails the bind.
///
/// # Errors
///
/// A `BindError` when the listener did not bind.
pub fn bind(run: &VmRunDir) -> Result<Bound, BindError> {
    let path = run.host_listener(HostPort::GitMirror);
    StdListener::bind(&path)
        .and_then(|listener| listener.set_nonblocking(true).map(|()| listener))
        .map(Bound)
        .map_err(|source| BindError { path, source })
}

/// What each link of one VM reads and names: the mirror, the families dir
/// and the family, for the checks, and the session, for a stderr line.
#[derive(Debug)]
struct Clones {
    mirror: Mirror,
    families: FamilyRoot,
    family: FamilyName,
    session: SessionId,
    notes: Mutex<Notes>,
}

impl Bound {
    /// Serves the listener in a task of its own. Each clone reads the
    /// clone list of `family` in `families` and the mirror of `mirror`.
    ///
    /// # Errors
    ///
    /// The error of a listener that the runtime did not take.
    pub fn serve(
        self,
        mirror: Mirror,
        families: FamilyRoot,
        family: FamilyName,
        session: SessionId,
    ) -> io::Result<Server> {
        let listener = UnixListener::from_std(self.0)?;
        let clones = Arc::new(Clones {
            mirror,
            families,
            family,
            session,
            notes: Mutex::new(Notes::new(Instant::now())),
        });
        Ok(Server(tokio::spawn(accept(listener, clones))))
    }
}

/// The served listener of one VM. Its drop ends the listener task, and the
/// end of the task drops its listener and its open links, which kills
/// each child.
#[derive(Debug)]
pub struct Server(JoinHandle<()>);

impl Drop for Server {
    fn drop(&mut self) {
        self.0.abort();
    }
}

/// Accepts on `listener` until its task ends, and serves each link.
// Decision CH69 rule 30: as on vmm.sock (decision CH40 rule 1) and on
// 1100 to 1102 and 1104, a failed accept, such as EMFILE, is logged by
// kind, and the loop pauses ACCEPT_PAUSE and goes on. CH67 rule 11 binds
// only the guest rows of guest init.
async fn accept(listener: UnixListener, clones: Arc<Clones>) {
    let mut links = JoinSet::new();
    loop {
        let accepted = listener.accept().await;
        while links.try_join_next().is_some() {}
        match accepted {
            Ok((guest, _)) if links.len() < LINK_CAP => {
                links.spawn(serve_link(guest, Arc::clone(&clones)));
            }
            // Decision CH67 rule 3: a link past the cap closes at once.
            Ok((guest, _)) => {
                drop(guest);
                clones.note(&Note::Refused(Refusal::LinkCap));
            }
            Err(error) => {
                eprintln!("vmm: a mirror accept failed: {}", error.kind());
                sleep(ACCEPT_PAUSE).await;
            }
        }
    }
}

/// Why a clone was refused. Each one but `LinkCap` and `LineTimeout` gets
/// `DENIED`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Refusal {
    /// The listener held `LINK_CAP` links.
    LinkCap,
    /// The first pkt-line did not come whole within `LINE_WAIT`.
    LineTimeout,
    /// The first pkt-line is not the one request form.
    BadRequest,
    /// `repos.json` is present and does not read.
    ReposNotValid,
    /// The clone list does not name the repo.
    NotListed,
    /// The mirror does not hold the repo, or holds it behind a symlink.
    NotInMirror,
}

/// One stderr line of the mirror (decision CH69 rule 24).
#[derive(Debug)]
enum Note {
    /// A refused clone.
    Refused(Refusal),
    /// The mirror root did not open.
    Mirror(io::ErrorKind),
    /// The `git` child did not start.
    Git(io::ErrorKind),
}

impl fmt::Display for Note {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Refused(refusal) => {
                let reason = match refusal {
                    Refusal::LinkCap => "link_cap",
                    Refusal::LineTimeout => "line_timeout",
                    Refusal::BadRequest => "bad_request",
                    Refusal::ReposNotValid => "repos_not_valid",
                    Refusal::NotListed => "not_listed",
                    Refusal::NotInMirror => "not_in_mirror",
                };
                write!(formatter, "refused: {reason}")
            }
            Self::Mirror(kind) => write!(formatter, "faulted: the mirror root: {kind}"),
            Self::Git(kind) => write!(formatter, "faulted: cannot run git: {kind}"),
        }
    }
}

/// The count of the stderr lines of one VM in the window that began at
/// `since`.
#[derive(Debug)]
struct Notes {
    since: Instant,
    count: u32,
}

impl Notes {
    fn new(now: Instant) -> Self {
        Self {
            since: now,
            count: 0,
        }
    }

    /// Whether a line at `now` fits `NOTE_CAP`. A line that fits counts.
    fn admit(&mut self, now: Instant) -> bool {
        if now.saturating_duration_since(self.since) >= NOTE_WINDOW {
            *self = Self::new(now);
        }
        let fits = self.count < NOTE_CAP;
        if fits {
            self.count = self.count.saturating_add(1);
        }
        fits
    }
}

impl Clones {
    /// Writes `note` to stderr when it fits the cap of the VM.
    fn note(&self, note: &Note) {
        let mut notes = self.notes.lock().unwrap_or_else(PoisonError::into_inner);
        if notes.admit(Instant::now()) {
            drop(notes);
            let (family, session) = (&self.family, &self.session);
            eprintln!("vmm: a clone of the family {family}, session {session}, {note}");
        }
    }
}

/// One request of the one form of step 3.
#[derive(Debug, Clone, PartialEq, Eq)]
struct Request {
    /// The repo that the path names.
    name: RepoName,
    /// The request asks for protocol v2.
    v2: bool,
}

/// Serves one link: the request, the checks, then the splice to `git`.
async fn serve_link(guest: UnixStream, clones: Arc<Clones>) {
    let request = match timeout(LINE_WAIT, read_line(&guest)).await {
        Err(_) => return clones.note(&Note::Refused(Refusal::LineTimeout)),
        Ok(line) => line.as_deref().and_then(parse),
    };
    let Some(request) = request else {
        return deny(&guest, &clones, Note::Refused(Refusal::BadRequest)).await;
    };
    let (families, family, root) = (
        clones.families.clone(),
        clones.family.clone(),
        clones.mirror.root.clone(),
    );
    let name = request.name.clone();
    let Ok(found) = spawn_blocking(move || find(&families, &family, &root, &name)).await else {
        return;
    };
    let git_dir = match found {
        Ok(git_dir) => git_dir,
        Err(note) => return deny(&guest, &clones, note).await,
    };
    let mut child = match upload_pack(&clones.mirror.git, git_dir, request.v2) {
        Ok(child) => child,
        Err(error) => return deny(&guest, &clones, Note::Git(error.kind())).await,
    };
    let (Some(stdin), Some(stdout)) = (child.stdin.take(), child.stdout.take()) else {
        return;
    };
    let mut io = ChildIo {
        stdout,
        stdin: Some(stdin),
    };
    let mut guest = guest;
    let mut splice = Splice::new(Vec::new());
    poll_fn(|context| splice.carry(context, &mut guest, &mut io)).await;
    // The drop of `child` kills a child that still runs.
    drop(child);
}

/// Answers a refused clone with `DENIED` and writes `note`. The drop of
/// the link then closes it.
async fn deny(guest: &UnixStream, clones: &Clones, note: Note) {
    // A guest that is gone gets no answer, and its note still goes out.
    let _ = write_all(guest, DENIED).await;
    clones.note(&note);
}

/// The payload of the first pkt-line: 4 lowercase hex digits of its whole
/// length, at most `LINE_CAP`, then that many bytes less 4. `None` for a
/// length that does not read, a flush, a line past the cap, a failed read
/// or an EOF before the end. The read takes no byte past the line.
async fn read_line(guest: &UnixStream) -> Option<Vec<u8>> {
    let mut digits = [0; 4];
    read_exact(guest, &mut digits).await?;
    let text = std::str::from_utf8(&digits).ok()?;
    if !text
        .bytes()
        .all(|byte| matches!(byte, b'0'..=b'9' | b'a'..=b'f'))
    {
        return None;
    }
    let length = usize::from_str_radix(text, 16).ok()?;
    if length <= digits.len() || length > LINE_CAP {
        return None;
    }
    let mut payload = vec![0; length.saturating_sub(digits.len())];
    read_exact(guest, &mut payload).await?;
    Some(payload)
}

/// Fills `buffer` from `guest`. `None` for a failed read or an EOF first.
async fn read_exact(guest: &UnixStream, buffer: &mut [u8]) -> Option<()> {
    let mut filled = 0;
    while let Some(free) = buffer.get_mut(filled..).filter(|free| !free.is_empty()) {
        let read = read_some(guest, free).await.ok().filter(|&read| read > 0)?;
        filled = filled.saturating_add(read);
    }
    Some(())
}

/// The request of `payload`, the form of step 3:
/// `git-upload-pack /<name>\0host=<any>\0`, and for v2 then
/// `\0version=2\0`. Every other form is `None`.
fn parse(payload: &[u8]) -> Option<Request> {
    let parts: Vec<&[u8]> = payload.split(|&byte| byte == 0).collect();
    let (command, host, v2) = match parts.as_slice() {
        [command, host, b""] => (command, host, false),
        [command, host, b"", b"version=2", b""] => (command, host, true),
        _ => return None,
    };
    host.strip_prefix(b"host=")?;
    let name = command.strip_prefix(b"git-upload-pack /")?;
    let name = std::str::from_utf8(name).ok()?.parse().ok()?;
    Some(Request { name, v2 })
}

/// The git dir of `name` for `family`: in the clone list read now and in
/// the mirror. Blocks.
fn find(
    families: &FamilyRoot,
    family: &FamilyName,
    root: &MirrorRoot,
    name: &RepoName,
) -> Result<PathBuf, Note> {
    let repos = families
        .read_repos(family)
        .map_err(|_| Note::Refused(Refusal::ReposNotValid))?;
    // The path comes from the list entry, not from the client bytes
    // (decision CH46 rule 4).
    let listed = repos
        .repos
        .names()
        .iter()
        .find(|entry| *entry == name)
        .ok_or(Note::Refused(Refusal::NotListed))?;
    root.git_dir(listed).map_err(|fault| match fault {
        MirrorFault::Absent | MirrorFault::NotDir => Note::Refused(Refusal::NotInMirror),
        MirrorFault::Io(error) => Note::Mirror(error.kind()),
    })
}

/// Starts `git upload-pack` on `git_dir` (step 6).
fn upload_pack(git: &Program, git_dir: PathBuf, v2: bool) -> io::Result<Child> {
    let mut command = Command::new(git.path());
    command
        .arg("upload-pack")
        .arg("--strict")
        .arg(UPLOAD_TIMEOUT)
        .arg(git_dir)
        .env_clear()
        .env("GIT_CONFIG_NOSYSTEM", "1")
        .env("GIT_CONFIG_GLOBAL", "/dev/null")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .kill_on_drop(true);
    if v2 {
        command.env("GIT_PROTOCOL", "version=2");
    }
    command.spawn()
}

/// The stdout and the stdin of the `git` child as one stream, so `Splice`
/// carries it. A read reads stdout. A write writes stdin, and the shutdown
/// closes stdin, which is the EOF of `upload-pack`.
#[derive(Debug)]
struct ChildIo {
    stdout: ChildStdout,
    stdin: Option<ChildStdin>,
}

impl AsyncRead for ChildIo {
    fn poll_read(
        mut self: Pin<&mut Self>,
        context: &mut Context<'_>,
        buffer: &mut ReadBuf<'_>,
    ) -> Poll<io::Result<()>> {
        Pin::new(&mut self.stdout).poll_read(context, buffer)
    }
}

impl AsyncWrite for ChildIo {
    fn poll_write(
        mut self: Pin<&mut Self>,
        context: &mut Context<'_>,
        bytes: &[u8],
    ) -> Poll<io::Result<usize>> {
        match self.stdin.as_mut() {
            Some(stdin) => Pin::new(stdin).poll_write(context, bytes),
            None => Poll::Ready(Err(io::ErrorKind::BrokenPipe.into())),
        }
    }

    fn poll_flush(mut self: Pin<&mut Self>, context: &mut Context<'_>) -> Poll<io::Result<()>> {
        match self.stdin.as_mut() {
            Some(stdin) => Pin::new(stdin).poll_flush(context),
            None => Poll::Ready(Ok(())),
        }
    }

    fn poll_shutdown(mut self: Pin<&mut Self>, _: &mut Context<'_>) -> Poll<io::Result<()>> {
        self.stdin = None;
        Poll::Ready(Ok(()))
    }
}

#[cfg(test)]
mod tests {
    use std::time::{Duration, Instant};

    use super::{NOTE_CAP, NOTE_WINDOW, Notes, Request, parse};

    fn request(name: &str, v2: bool) -> Option<Request> {
        name.parse().ok().map(|name| Request { name, v2 })
    }

    #[test]
    fn the_one_form_parses() {
        let v0 = b"git-upload-pack /creche\0host=127.0.0.1:9418\0";
        assert_eq!(parse(v0), request("creche", false));
        let v2 = b"git-upload-pack /creche\0host=127.0.0.1\0\0version=2\0";
        assert_eq!(parse(v2), request("creche", true));
        let empty_host = b"git-upload-pack /creche\0host=\0";
        assert_eq!(parse(empty_host), request("creche", false));
    }

    #[test]
    fn every_other_form_is_refused() {
        let refused: [&[u8]; 14] = [
            b"git-receive-pack /creche\0host=h\0",
            b"git-upload-archive /creche\0host=h\0",
            b"git-upload-pack /creche.git\0host=h\0",
            b"git-upload-pack /~david/creche\0host=h\0",
            b"git-upload-pack /../creche\0host=h\0",
            b"git-upload-pack /owner/creche\0host=h\0",
            b"git-upload-pack creche\0host=h\0",
            b"git-upload-pack  /creche\0host=h\0",
            b"git-upload-pack /creche\0",
            b"git-upload-pack /creche\0port=h\0",
            b"git-upload-pack /creche\0host=h\0\0version=1\0",
            b"git-upload-pack /creche\0host=h\0\0version=2\0object-format=sha1\0",
            b"git-upload-pack /creche\0host=h\0\n",
            b"git-upload-pack /creche\n\0host=h\0",
        ];
        for payload in refused {
            assert_eq!(parse(payload), None, "{}", payload.escape_ascii());
        }
    }

    /// At most `NOTE_CAP` lines in one window, and a new window counts
    /// from 0 (decision CH69 rule 24).
    #[test]
    fn the_notes_hold_their_cap_per_window() {
        let start = Instant::now();
        let mut notes = Notes::new(start);
        let admitted = (0..NOTE_CAP + 5).filter(|_| notes.admit(start)).count();
        assert_eq!(admitted, 100);
        let late = start + NOTE_WINDOW - Duration::from_millis(1);
        assert!(!notes.admit(late));
        assert!(notes.admit(start + NOTE_WINDOW));
    }
}
