//! The git platform spool on host port 1105 (decision CH3, phase 12 PR2).
//! `creche_wire::spool` holds the protocol: one `put <size>` or `get <id>`
//! line, one reply line, and the bundle bytes. Cloud Hypervisor connects
//! each guest dial of 1105 to `vsock.sock_1105` in the run dir of the VM
//! (decision CH24). The vmm listens there only for a family whose
//! `git-platform.json` grants `fetch` or `push`, or is present and does
//! not read, as for 1103 (decision CH73 rule 1). Without the listener the
//! guest dial fails, so a VM with no grant fails closed.
//!
//! 1. `bind` binds the listener beside the plane listeners, with their
//!    lifecycle: after `RunRoot::link_vm` emptied the slot and after
//!    `remove_stale`, before CH starts, for a cold boot and for each
//!    restore.
//! 2. Each link reads one request line within `LINE_WAIT`, at most
//!    `LINE_CAP` bytes. The read takes no byte past the LF, so the bundle
//!    bytes of a put stay in the stream.
//! 3. Each request reads `git-platform.json` again, so a removed grant
//!    refuses the next request at once. A put needs `push` and a get needs
//!    `fetch`. A file that does not read grants nothing.
//! 4. A put of more than `BUNDLE_CAP` bytes is refused before any file is
//!    made. A put writes `push/.<id>.part`, then renames it to
//!    `push/<id>` with the mode 0640 (`creche_vmm_confine::spool`). A put
//!    that ends early or passes `BODY_WAIT` removes its file.
//! 5. The family of each id is the family of the VM that this listener
//!    serves, never a value of the guest bytes. A get of an id of another
//!    family is refused, and the path of a get is built from a checked
//!    `BundleId`, never from the client bytes.
//! 6. The vmm removes what it wrote in `push/`:
//!    1. The upstream copies a bundle before its push, so a bundle is no
//!       longer needed once the `push` tool returns. The vmm cannot see
//!       that call, so it keeps each bundle of a VM for `PUSH_TTL` (15
//!       minutes, the TTL of `fetch/`) and at most `PUSH_KEEP` of them.
//!       Each new put removes the bundles of its VM past the TTL and then
//!       the oldest past `PUSH_KEEP`.
//!    2. The end of the VM removes each bundle that it put: the drop of
//!       the last link and the listener of the VM.
//!    3. `vmm serve` removes each bundle and each temporary file of
//!       `push/` at its start, before any VM runs
//!       (`SpoolRoot::sweep_push`). So a crash leaves no file for long.
//!
//!    `fetch/` belongs to the upstream, and the vmm removes nothing there.
//! 7. A refused or faulted request writes one stderr line, at most
//!    `mirror::NOTE_CAP` per `mirror::NOTE_WINDOW` for one VM. A good
//!    request writes none.
//! 8. The listener holds at most `LINK_CAP` links. A link past the cap is
//!    accepted and closed at once. A failed accept is logged by kind, and
//!    the loop pauses `ACCEPT_PAUSE` and accepts again (decision CH40 rule
//!    1).
//! 9. A link does not set `last_use` and does not block a snapshot, as on
//!    1103 and 1104 (decision CH69 rule 20). A restore cuts each open
//!    link.
//! 10. The drop of `Server` closes the listener and every link.
//!     `Server::close` closes only the listener, after an edit that
//!     removes the grant. Each open link runs to its own end in
//!     `Draining`, and `Draining::reopen` serves a new listener with the
//!     same links (decision CH74 rule 2).

use std::collections::VecDeque;
use std::fmt;
use std::io::{self, Read};
use std::os::unix::net::UnixListener as StdListener;
use std::sync::{Arc, Mutex, PoisonError};
use std::time::{Duration, Instant};

use creche_vmm_confine::family::FamilyRoot;
use creche_vmm_confine::spool::{FetchFault, FetchFile, PushFile, SpoolRoot};
use creche_vmm_store::layout::{SLOTS, VmRunDir};
use creche_wire::spool::{
    BODY_WAIT, BUNDLE_CAP, BundleId, ERR, FETCH_TTL, GET, LINE_CAP, LINE_WAIT, OK, PUT, Refusal,
};
use creche_wire::{FamilyGitPlatform, FamilyName, HostPort, SessionId};
use tokio::net::{UnixListener, UnixStream};
use tokio::task::spawn_blocking;
use tokio::time::{sleep, timeout};

use crate::egress::proxy::{read_some, write_all};
use crate::links::{Accept, Links};
use crate::mirror::Notes;
use crate::planes::BindError;
use crate::server::ACCEPT_PAUSE;

/// The most open links of the listener of one VM. A link of a put holds
/// up to 64 MiB of disk, so the cap is small.
pub const LINK_CAP: usize = 4;

/// How long the vmm keeps a bundle in `push/` for the upstream: the TTL
/// of `fetch/`.
pub const PUSH_TTL: Duration = FETCH_TTL;

/// The most bundles of one VM that stay in `push/` after their put. With
/// the puts of `LINK_CAP` open links, one VM holds at most
/// `PUSH_KEEP + LINK_CAP` bundles there.
pub const PUSH_KEEP: usize = 2;

/// The bytes of one read of the guest stream or of a fetched file.
const CHUNK: usize = 256 * 1024;

/// The most fds of one link beside its guest stream. A request reads the
/// grant through 2 (the family dir and the file), then a put holds 2
/// (`push/` and its file) and a get 2 (`fetch/` and its file). The steps
/// run one after another.
const LINK_FDS: u64 = 2;

/// The fds that the listeners of all `SLOTS` need when each one holds
/// `LINK_CAP` links: per VM the listener, and per link its stream and
/// `LINK_FDS`. `vmm serve` adds it to the raise of decision CH67 rule 4.
#[must_use]
pub fn fd_need() -> u64 {
    let links = u64::try_from(LINK_CAP).unwrap_or(u64::MAX);
    links
        .saturating_mul(LINK_FDS.saturating_add(1))
        .saturating_add(1)
        .saturating_mul(u64::from(SLOTS))
}

/// The listener of one VM, bound and not yet served. A dial that the guest
/// makes before `serve` waits in the backlog. The drop closes it.
#[derive(Debug)]
pub struct Bound(StdListener);

/// Binds `vsock.sock_1105` in `run` (decision CH24). It blocks, so the
/// start runs it in `spawn_blocking`. The slot is empty, so a path that is
/// there fails the bind.
///
/// # Errors
///
/// A `BindError` when the listener did not bind.
pub fn bind(run: &VmRunDir) -> Result<Bound, BindError> {
    let path = run.host_listener(HostPort::GitPlatformSpool);
    StdListener::bind(&path)
        .and_then(|listener| listener.set_nonblocking(true).map(|()| listener))
        .map(Bound)
        .map_err(|source| BindError { path, source })
}

/// What each link of one VM reads, writes and names. Its drop removes
/// each bundle that the VM put and the vmm still keeps (step 6.2).
#[derive(Debug)]
struct Spool {
    root: SpoolRoot,
    families: FamilyRoot,
    family: FamilyName,
    session: SessionId,
    notes: Mutex<Notes>,
    /// The bundles that this VM put and that `push/` still holds, oldest
    /// first, each with the time of its put.
    puts: Mutex<VecDeque<(BundleId, Instant)>>,
}

impl Drop for Spool {
    fn drop(&mut self) {
        let puts = self.puts.get_mut().unwrap_or_else(PoisonError::into_inner);
        for (id, _) in puts.drain(..) {
            // A bundle that stays goes at the next start of the vmm.
            let _ = self.root.remove_push(&id);
        }
    }
}

impl Bound {
    /// Serves the listener in a task of its own. Each request reads the
    /// grant of `family` in `families` and the spool of `root`.
    ///
    /// # Errors
    ///
    /// The error of a listener that the runtime did not take.
    pub fn serve(
        self,
        root: SpoolRoot,
        families: FamilyRoot,
        family: FamilyName,
        session: SessionId,
    ) -> io::Result<Server> {
        let spool = Arc::new(Spool {
            root,
            families,
            family,
            session,
            notes: Mutex::new(Notes::new(Instant::now())),
            puts: Mutex::default(),
        });
        let links = Links::default();
        Draining { spool, links }.reopen(self)
    }
}

/// The served listener of one VM. Its drop ends the listener task and
/// cuts its open links.
#[derive(Debug)]
pub struct Server {
    /// The accept task. Its drop aborts it.
    accept: Accept,
    /// The open links and what a link reads.
    draining: Draining,
}

impl Server {
    /// Closes the listener and keeps each open link (decision CH74 rule
    /// 2). When it returns, the listener is closed, and the caller removes
    /// its socket file.
    pub async fn close(self) -> Draining {
        self.accept.end().await;
        self.draining
    }
}

/// The open links of a closed listener of one VM. Each request runs to
/// its own end. The drop cuts each link that still runs.
#[derive(Debug)]
pub struct Draining {
    spool: Arc<Spool>,
    links: Links,
}

impl Draining {
    /// Serves `bound`, a new listener at the path of the closed one, with
    /// the links of the closed one. They count against `LINK_CAP`.
    ///
    /// # Errors
    ///
    /// The error of a listener that the runtime did not take.
    pub fn reopen(self, bound: Bound) -> io::Result<Server> {
        let listener = UnixListener::from_std(bound.0)?;
        let task = accept(listener, Arc::clone(&self.spool), self.links.clone());
        Ok(Server {
            accept: Accept::spawn(task),
            draining: self,
        })
    }
}

/// Accepts on `listener` until its task ends, and serves each link in
/// `links`. A failed accept is logged by kind, and the loop pauses
/// `ACCEPT_PAUSE` and goes on (decision CH40 rule 1).
async fn accept(listener: UnixListener, spool: Arc<Spool>, links: Links) {
    loop {
        match listener.accept().await {
            Ok((guest, _)) => {
                if !links.admit(LINK_CAP, serve_link(guest, Arc::clone(&spool))) {
                    spool.note(&Note::LinkCap);
                }
            }
            Err(error) => {
                eprintln!("vmm: a spool accept failed: {}", error.kind());
                sleep(ACCEPT_PAUSE).await;
            }
        }
    }
}

/// One stderr line of the spool (step 7).
#[derive(Debug)]
enum Note {
    /// The listener held `LINK_CAP` links.
    LinkCap,
    /// A refused request.
    Refused(Refusal),
    /// A spool fault, with the kind of its error.
    Fault(&'static str, io::ErrorKind),
}

impl fmt::Display for Note {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::LinkCap => formatter.write_str("refused: link_cap"),
            Self::Refused(refusal) => write!(formatter, "refused: {refusal}"),
            Self::Fault(step, kind) => write!(formatter, "faulted: {step}: {kind}"),
        }
    }
}

impl From<Refusal> for Note {
    fn from(refusal: Refusal) -> Self {
        Self::Refused(refusal)
    }
}

impl Spool {
    /// Writes `note` to stderr when it fits the cap of the VM.
    fn note(&self, note: &Note) {
        let mut notes = self.notes.lock().unwrap_or_else(PoisonError::into_inner);
        if notes.admit(Instant::now()) {
            drop(notes);
            let (family, session) = (&self.family, &self.session);
            eprintln!("vmm: a spool request of the family {family}, session {session}, {note}");
        }
    }

    /// The grant of the family, read now. A file that does not read grants
    /// nothing.
    async fn grant(self: &Arc<Self>) -> FamilyGitPlatform {
        let spool = Arc::clone(self);
        spawn_blocking(move || spool.families.read_git_platform(&spool.family))
            .await
            .ok()
            .and_then(Result::ok)
            .unwrap_or_default()
    }

    /// Records the put of `id`, then removes the bundles of this VM past
    /// `PUSH_TTL` and the oldest past `PUSH_KEEP` (step 6.1).
    async fn keep(self: &Arc<Self>, id: BundleId) {
        let now = Instant::now();
        let gone: Vec<BundleId> = {
            let mut puts = self.puts.lock().unwrap_or_else(PoisonError::into_inner);
            puts.push_back((id, now));
            let mut gone = Vec::new();
            while let Some((_, at)) = puts.front() {
                let old = now.saturating_duration_since(*at) >= PUSH_TTL;
                if !old && puts.len() <= PUSH_KEEP {
                    break;
                }
                if let Some((id, _)) = puts.pop_front() {
                    gone.push(id);
                }
            }
            gone
        };
        if gone.is_empty() {
            return;
        }
        let spool = Arc::clone(self);
        let removed = spawn_blocking(move || {
            gone.iter()
                .map(|id| spool.root.remove_push(id))
                .find_map(Result::err)
        })
        .await;
        if let Ok(Some(error)) = removed {
            self.note(&Note::Fault("removing an old bundle", error.kind()));
        }
    }
}

/// One request of step 2.
#[derive(Debug, Clone, PartialEq, Eq)]
enum Request {
    /// `put <size>`, with a size of 1 to `u64::MAX`. The cap is checked
    /// after the grant.
    Put(u64),
    /// `get <id>`, with the text of the id. It is checked after the grant.
    Get(String),
}

/// The request of `line`, or `None` for every other form. A size in
/// decimal has no sign and no leading zero. A size past `u64` reads as
/// `u64::MAX`, which passes the cap.
fn parse(line: &[u8]) -> Option<Request> {
    let line = std::str::from_utf8(line).ok()?;
    let (word, value) = line.split_once(' ')?;
    if word == GET {
        let printable = !value.is_empty() && value.bytes().all(|byte| byte.is_ascii_graphic());
        return printable.then(|| Request::Get(value.to_owned()));
    }
    if word != PUT || value.starts_with('0') || value.is_empty() {
        return None;
    }
    if !value.bytes().all(|byte| byte.is_ascii_digit()) {
        return None;
    }
    Some(Request::Put(value.parse().unwrap_or(u64::MAX)))
}

/// Serves one link: the request line, the grant, then the put or the get.
async fn serve_link(guest: UnixStream, spool: Arc<Spool>) {
    let request = match timeout(LINE_WAIT, read_line(&guest)).await {
        Ok(line) => line.as_deref().and_then(parse),
        Err(_) => None,
    };
    let Some(request) = request else {
        return refuse(&guest, &spool, Refusal::BadRequest).await;
    };
    let grant = spool.grant().await;
    let outcome = match request {
        Request::Put(_) if !grant.push => Err(Refusal::NotGranted.into()),
        Request::Get(_) if !grant.fetch => Err(Refusal::NotGranted.into()),
        Request::Put(size) => put(&guest, &spool, size).await,
        Request::Get(id) => get(&guest, &spool, &id).await,
    };
    if let Err(note) = outcome {
        let refusal = match note {
            Note::Refused(refusal) => refusal,
            Note::LinkCap | Note::Fault(..) => Refusal::SpoolFault,
        };
        // A guest that is gone gets no reply, and its note still goes out.
        let _ = reply(&guest, ERR, refusal.word()).await;
        spool.note(&note);
    }
}

/// Answers a refused request and writes its note.
async fn refuse(guest: &UnixStream, spool: &Spool, refusal: Refusal) {
    let _ = reply(guest, ERR, refusal.word()).await;
    spool.note(&refusal.into());
}

/// Writes the reply line `<word> <value>`.
async fn reply(guest: &UnixStream, word: &str, value: &str) -> io::Result<()> {
    write_all(guest, format!("{word} {value}\n").as_bytes()).await
}

/// A put of `size` bytes (step 4): the cap, the file, the bytes within
/// `BODY_WAIT`, the rename, the reply, then the removal of old bundles.
async fn put(guest: &UnixStream, spool: &Arc<Spool>, size: u64) -> Result<(), Note> {
    if size > BUNDLE_CAP {
        return Err(Refusal::TooLarge.into());
    }
    let (root, family) = (spool.root.clone(), spool.family.clone());
    let file = spawn_blocking(move || root.create_push(&family))
        .await
        .map_err(|_| Note::Fault("opening the bundle", io::ErrorKind::Other))?
        .map_err(|error| Note::Fault("opening the bundle", error.kind()))?;
    let file = timeout(BODY_WAIT, receive(guest, file, size))
        .await
        .map_err(|_| Note::Refused(Refusal::ShortBody))??;
    let id = spawn_blocking(move || file.commit())
        .await
        .map_err(|_| Note::Fault("renaming the bundle", io::ErrorKind::Other))?
        .map_err(|error| Note::Fault("renaming the bundle", error.kind()))?;
    // The bundle stays until its TTL, also when the guest is gone.
    let sent = reply(guest, OK, id.as_str()).await;
    spool.keep(id).await;
    sent.map_err(|error| Note::Fault("replying", error.kind()))
}

/// Copies `size` bytes of the guest into `file`. An end of the stream
/// before `size` bytes is `ShortBody`. The drop of `file` on any error
/// removes it.
async fn receive(guest: &UnixStream, mut file: PushFile, size: u64) -> Result<PushFile, Note> {
    let mut left = size;
    let mut buffer = vec![0; CHUNK];
    while left > 0 {
        let want = usize::try_from(left).map_or(CHUNK, |left| left.min(CHUNK));
        let free = buffer.get_mut(..want).unwrap_or_default();
        let read = read_some(guest, free)
            .await
            .map_err(|_| Note::Refused(Refusal::ShortBody))?;
        if read == 0 {
            return Err(Refusal::ShortBody.into());
        }
        let chunk = buffer.get(..read).unwrap_or_default().to_vec();
        file = spawn_blocking(move || file.write(&chunk).map(|()| file))
            .await
            .map_err(|_| Note::Fault("writing the bundle", io::ErrorKind::Other))?
            .map_err(|error| Note::Fault("writing the bundle", error.kind()))?;
        left = left.saturating_sub(u64::try_from(read).unwrap_or(u64::MAX));
    }
    Ok(file)
}

/// A get of the id `text` (step 5): the id, its family, the file, then the
/// reply and the bytes within `BODY_WAIT`.
async fn get(guest: &UnixStream, spool: &Spool, text: &str) -> Result<(), Note> {
    let id: BundleId = text.parse().map_err(|_| Note::Refused(Refusal::BadId))?;
    if id.family() != &spool.family {
        return Err(Refusal::OtherFamily.into());
    }
    let root = spool.root.clone();
    let opened = spawn_blocking(move || root.open_fetch(&id))
        .await
        .map_err(|_| Note::Fault("opening the bundle", io::ErrorKind::Other))?;
    let fetched = match opened {
        Ok(fetched) => fetched,
        Err(FetchFault::NotFound) => return Err(Refusal::NotFound.into()),
        Err(FetchFault::TooLarge) => return Err(Refusal::TooLarge.into()),
        Err(FetchFault::Io(error)) => return Err(Note::Fault("opening the bundle", error.kind())),
    };
    reply(guest, OK, &fetched.size.to_string())
        .await
        .map_err(|error| Note::Fault("replying", error.kind()))?;
    // After the reply line no `err` line may follow, so a fault here only
    // closes the link and writes its note.
    match timeout(BODY_WAIT, send(guest, fetched)).await {
        Ok(Ok(())) => Ok(()),
        Ok(Err(error)) => {
            spool.note(&Note::Fault("sending the bundle", error.kind()));
            Ok(())
        }
        Err(_) => {
            spool.note(&Note::Fault("sending the bundle", io::ErrorKind::TimedOut));
            Ok(())
        }
    }
}

/// Sends the `size` bytes of `fetched` to the guest. A file that ends
/// early ends the send, and the guest sees fewer bytes than the reply
/// named.
async fn send(guest: &UnixStream, fetched: FetchFile) -> io::Result<()> {
    let FetchFile { mut file, size } = fetched;
    let mut left = size;
    while left > 0 {
        let want = usize::try_from(left).map_or(CHUNK, |left| left.min(CHUNK));
        let (back, chunk) = spawn_blocking(move || {
            let mut chunk = vec![0; want];
            let read = file.read(&mut chunk)?;
            chunk.truncate(read);
            Ok::<_, io::Error>((file, chunk))
        })
        .await
        .map_err(|_| io::Error::other("a blocking read panicked"))??;
        if chunk.is_empty() {
            return Err(io::ErrorKind::UnexpectedEof.into());
        }
        write_all(guest, &chunk).await?;
        file = back;
        left = left.saturating_sub(u64::try_from(chunk.len()).unwrap_or(u64::MAX));
    }
    Ok(())
}

/// The request line: the bytes before the first LF, at most `LINE_CAP`.
/// `None` for a longer line, a CR, a failed read or an EOF first. The read
/// takes no byte past the LF.
async fn read_line(guest: &UnixStream) -> Option<Vec<u8>> {
    let mut line = Vec::new();
    loop {
        let mut byte = [0; 1];
        let read = read_some(guest, &mut byte).await.ok()?;
        match (read, byte) {
            (0, _) | (_, [b'\r']) => return None,
            (_, [b'\n']) => return Some(line),
            (_, [other]) => line.push(other),
        }
        if line.len() > LINE_CAP {
            return None;
        }
    }
}

#[cfg(test)]
mod tests {
    use super::{Request, parse};

    #[test]
    fn the_two_forms_parse() {
        assert_eq!(parse(b"put 1"), Some(Request::Put(1)));
        assert_eq!(parse(b"put 67108864"), Some(Request::Put(67_108_864)));
        let past = b"put 99999999999999999999999";
        assert_eq!(parse(past), Some(Request::Put(u64::MAX)));
        let id = "code-01JC9Z6A0B1C2D3E4F5G6H7J8K.bundle";
        assert_eq!(
            parse(format!("get {id}").as_bytes()),
            Some(Request::Get(id.to_owned()))
        );
        assert_eq!(parse(b"get x"), Some(Request::Get("x".to_owned())));
    }

    #[test]
    fn every_other_form_is_refused() {
        let refused: [&[u8]; 13] = [
            b"",
            b"put",
            b"put ",
            b"put 0",
            b"put 012",
            b"put -1",
            b"put +1",
            b"put 1 2",
            b"put 1.0",
            b"PUT 1",
            b"get ",
            b"get a b",
            b"del code-01JC9Z6A0B1C2D3E4F5G6H7J8K.bundle",
        ];
        for line in refused {
            assert_eq!(parse(line), None, "{}", line.escape_ascii());
        }
        assert_eq!(parse(b"get \xff"), None);
    }
}
