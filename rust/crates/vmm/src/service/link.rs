//! The socket of a service VM (browser design 10.2 items 1 and 2, the
//! attachments design note). The chaperone runs `browser-relay` as the
//! user of `VMM_BROWSER_PEER_USER`, and the relay joins its stdio to the
//! browser socket. The chaperone unit blocks `/dev/kvm` and `AF_VSOCK`, so
//! the vmm brokers the link and the unit is not loosened. attendance runs
//! as the user of `VMM_SCANNER_PEER_USER` and opens one link of the
//! scanner socket for each attached file.
//!
//! 1. Root makes the dir of the socket once: the vmm user owns it, the
//!    peer group is its group, the mode is 2750. `bind` refuses a dir that
//!    others may read, write or search. It removes a socket of an earlier
//!    vmm at the path, refuses any other file there, binds the socket and
//!    sets the mode 0660. The setgid bit of the dir gives the socket the
//!    peer group, so the relay may connect.
//! 2. Each link gets one check of `SO_PEERCRED`: a peer with another uid
//!    gets a close and one stderr line. Then `ServiceVm::link` starts the
//!    VM when it does not run and dials guest port 1201. A failed start
//!    closes the link with one stderr line.
//! 3. The vmm writes the kind line of the image on the guest link
//!    (`kind_line`). For the browser it is `mcp`: the browser facade then
//!    joins the link to `agent-browser mcp`. So the relay sends MCP bytes
//!    from its first byte, and no relay can open the `view` link of the
//!    live view. For the scanner it is `scan`: the scanner facade then
//!    reads one request line.
//! 4. `Splice` carries the bytes both ways with the half-close of
//!    decision CH67 rule 9. It parses none of them.
//! 5. The listener holds at most `link_cap` links of its image. A link
//!    past the cap closes at once. On the scanner socket it first gets
//!    the one line `SCAN_BUSY_LINE` (`busy_line`), so attendance can tell
//!    a full socket from a fault. The browser link gets a plain close. A failed accept is logged by kind, and the loop
//!    pauses `ACCEPT_PAUSE` and accepts again (decision CH40 rule 1).
//! 6. The drop of `Served` closes the listener and every link. The VM
//!    runs on: the shutdown stops it.

use std::fmt;
use std::fs::{self, Permissions};
use std::future::{Future, poll_fn};
use std::io::{self, BufRead, BufReader};
use std::os::unix::fs::{FileTypeExt, MetadataExt, PermissionsExt};
use std::os::unix::net::UnixListener as StdListener;
use std::path::{Path, PathBuf};
use std::sync::Arc;

use creche_wire::ServiceImage;
use tokio::net::{UnixListener, UnixStream};
use tokio::time::sleep;

use crate::egress::proxy::write_all;
use crate::links::{Accept, Links};
use crate::relay::{AcpLink, Splice};
use crate::server::ACCEPT_PAUSE;
use crate::service::ServiceError;

/// The most open links of the browser socket. The chaperone runs one
/// relay, and the relay opens one link at a time.
pub const LINK_CAP: usize = 4;

/// The most open links of the scanner socket. attendance opens one link
/// for each file, and a message has at most 4 files (`max_files` of
/// `rust/crates/wire/vectors/scan.json`). So two messages scan at once.
pub const SCAN_LINK_CAP: usize = 8;

/// The kind line that the vmm writes first on each guest link of the
/// browser. The browser facade reads it
/// (`playpen/guest/creche_browser_facade.py`).
pub const MCP_LINE: &[u8] = b"mcp\n";

/// The kind line that the vmm writes first on each guest link of the
/// scanner: `kind_line` of `rust/crates/wire/vectors/scan.json`. The
/// scanner facade reads it (`playpen/guest/creche_scanner_facade.py`).
pub const SCAN_LINE: &[u8] = b"scan\n";

/// The line that a scanner link past `SCAN_LINK_CAP` gets before its
/// close: `reply_refused` of `rust/crates/wire/vectors/scan.json` with the
/// reason `busy`.
pub const SCAN_BUSY_LINE: &[u8] = b"{\"refused\":{\"reason\":\"busy\"}}\n";

/// The line that a link past the cap of `image` gets before its close,
/// or `None` for a plain close.
#[must_use]
pub const fn busy_line(image: ServiceImage) -> Option<&'static [u8]> {
    match image {
        ServiceImage::Browser => None,
        ServiceImage::Scanner => Some(SCAN_BUSY_LINE),
    }
}

/// The kind line of each guest link of `image`.
#[must_use]
pub const fn kind_line(image: ServiceImage) -> &'static [u8] {
    match image {
        ServiceImage::Browser => MCP_LINE,
        ServiceImage::Scanner => SCAN_LINE,
    }
}

/// The most open links of the socket of `image`.
#[must_use]
pub const fn link_cap(image: ServiceImage) -> usize {
    match image {
        ServiceImage::Browser => LINK_CAP,
        ServiceImage::Scanner => SCAN_LINK_CAP,
    }
}

/// The mode of the socket: the vmm user and the peer group.
pub const SOCKET_MODE: u32 = 0o660;

/// The mode bits of the dir that refuse a bind: any bit of others.
const DIR_OTHERS: u32 = 0o007;

/// The start of the VM of a service image and the dial of its guest port
/// 1201. `ColdBoot` is the hook of the vmm, and a test gives a fake.
pub trait ServiceVm: Send + Sync + 'static {
    /// A link to guest port 1201 of the VM of `image`. It starts the VM
    /// when it does not run.
    ///
    /// # Errors
    ///
    /// A `ServiceError` that names why no link came.
    fn link(
        &self,
        image: ServiceImage,
    ) -> impl Future<Output = Result<AcpLink, ServiceError>> + Send;
}

/// Why the socket did not bind.
#[derive(Debug)]
pub enum BindFault {
    /// The dir of the socket is not there: root has not made it.
    NoDir(PathBuf),
    /// The path of the socket has no parent dir.
    NoParent(PathBuf),
    /// The dir of the socket is no dir.
    NotDir(PathBuf),
    /// Others may read, write or search the dir. It holds the mode.
    DirOpen(PathBuf, u32),
    /// A file that is no socket is at the path.
    NotSocket(PathBuf),
    /// A step failed.
    Io(PathBuf, io::Error),
}

impl fmt::Display for BindFault {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::NoDir(dir) => write!(formatter, "{} is absent", dir.display()),
            Self::NoParent(path) => write!(formatter, "{} has no dir", path.display()),
            Self::NotDir(dir) => write!(formatter, "{} is no dir", dir.display()),
            Self::DirOpen(dir, mode) => {
                write!(
                    formatter,
                    "{} has the mode {mode:o}, open to others",
                    dir.display()
                )
            }
            Self::NotSocket(path) => write!(formatter, "{} is no socket", path.display()),
            Self::Io(path, error) => write!(formatter, "{}: {error}", path.display()),
        }
    }
}

impl std::error::Error for BindFault {}

/// The bound socket, not yet served. The drop closes it.
#[derive(Debug)]
pub struct Bound(StdListener);

/// Binds the socket at `path` (rule 1). It blocks, so the compose step
/// runs it in its blocking step.
///
/// # Errors
///
/// A `BindFault` when the socket did not bind.
pub fn bind(path: &Path) -> Result<Bound, BindFault> {
    let dir = path
        .parent()
        .ok_or_else(|| BindFault::NoParent(path.to_owned()))?;
    let meta = match fs::symlink_metadata(dir) {
        Ok(meta) => meta,
        Err(error) if error.kind() == io::ErrorKind::NotFound => {
            return Err(BindFault::NoDir(dir.to_owned()));
        }
        Err(error) => return Err(BindFault::Io(dir.to_owned(), error)),
    };
    if !meta.is_dir() {
        return Err(BindFault::NotDir(dir.to_owned()));
    }
    let mode = meta.mode() & 0o7777;
    if mode & DIR_OTHERS != 0 {
        return Err(BindFault::DirOpen(dir.to_owned(), mode));
    }

    let io = |error| BindFault::Io(path.to_owned(), error);
    match fs::symlink_metadata(path) {
        Ok(meta) if meta.file_type().is_socket() => fs::remove_file(path).map_err(io)?,
        Ok(_) => return Err(BindFault::NotSocket(path.to_owned())),
        Err(error) if error.kind() == io::ErrorKind::NotFound => {}
        Err(error) => return Err(io(error)),
    }
    let listener = StdListener::bind(path).map_err(io)?;
    fs::set_permissions(path, Permissions::from_mode(SOCKET_MODE)).map_err(io)?;
    listener.set_nonblocking(true).map_err(io)?;
    Ok(Bound(listener))
}

/// The served socket. Its drop ends the accept task and every link.
#[derive(Debug)]
pub struct Served {
    _accept: Accept,
    _links: Links,
}

impl Bound {
    /// Serves the socket in a task of its own. Each link of a peer with
    /// the uid `peer` gets a link to the VM of `image` from `hook`.
    ///
    /// # Errors
    ///
    /// The error of a listener that the runtime did not take.
    pub fn serve<S: ServiceVm>(
        self,
        hook: Arc<S>,
        image: ServiceImage,
        peer: u32,
    ) -> io::Result<Served> {
        let listener = UnixListener::from_std(self.0)?;
        let links = Links::default();
        let task = accept(listener, Arc::new((hook, image, peer)), links.clone());
        Ok(Served {
            _accept: Accept::spawn(task),
            _links: links,
        })
    }
}

/// What each link needs: the hook, the image and the uid of the peer.
type Parts<S> = Arc<(Arc<S>, ServiceImage, u32)>;

/// Accepts on `listener` until its task ends, and serves each link in
/// `links` (rule 5).
async fn accept<S: ServiceVm>(listener: UnixListener, parts: Parts<S>, links: Links) {
    let mut number: u64 = 0;
    loop {
        match listener.accept().await {
            Ok((client, _)) => {
                number = number.saturating_add(1);
                let (name, cap) = (parts.1.name(), link_cap(parts.1));
                if links.full(cap) {
                    refuse_busy(&client, parts.1);
                    eprintln!("vmm: {name} link {number} closed: {cap} links are open");
                    continue;
                }
                if !links.admit(cap, serve_link(client, Arc::clone(&parts), number)) {
                    eprintln!("vmm: {name} link {number} closed: {cap} links are open");
                }
            }
            Err(error) => {
                eprintln!("vmm: a service accept failed: {}", error.kind());
                sleep(ACCEPT_PAUSE).await;
            }
        }
    }
}

/// Writes the busy line of `image` on `client`, a link past the cap
/// (rule 5). The write does not wait: a new socket has an empty buffer,
/// so the short line fits. A write that does not fit is skipped, and the
/// close follows as for the browser.
fn refuse_busy(client: &UnixStream, image: ServiceImage) {
    let Some(line) = busy_line(image) else {
        return;
    };
    match client.try_write(line) {
        Ok(written) if written == line.len() => {}
        Ok(written) => eprintln!("vmm: a busy line got {written} bytes of {}", line.len()),
        Err(error) => eprintln!("vmm: a busy line failed: {}", error.kind()),
    }
}

/// Serves one link (rules 2 to 4).
async fn serve_link<S: ServiceVm>(mut client: UnixStream, parts: Parts<S>, number: u64) {
    let (hook, image, peer) = (&parts.0, parts.1, parts.2);
    let name = image.name();
    let uid = match client.peer_cred() {
        Ok(cred) => cred.uid(),
        Err(error) => {
            eprintln!(
                "vmm: {name} link {number} closed: no peer uid: {}",
                error.kind()
            );
            return;
        }
    };
    if uid != peer {
        eprintln!("vmm: {name} link {number} closed: the peer uid {uid} is not {peer}");
        return;
    }
    let link = match hook.link(image).await {
        Ok(link) => link,
        Err(error) => {
            eprintln!("vmm: {name} link {number} closed: {error}");
            return;
        }
    };
    let (mut guest, pending) = link.into_parts();
    if let Err(error) = write_all(&guest, kind_line(image)).await {
        eprintln!(
            "vmm: {name} link {number} closed: the kind line: {}",
            error.kind()
        );
        return;
    }
    eprintln!("vmm: {name} link {number} open");
    // The near stream is the guest, so the bytes that guest init sent
    // after `OK` reach the client first.
    let mut splice = Splice::new(pending);
    poll_fn(|context| splice.carry(context, &mut client, &mut guest)).await;
    let (up, down) = splice.carried();
    eprintln!("vmm: {name} link {number} closed after {up} bytes up and {down} down");
}

/// The uid of `user` in the passwd file at `passwd`, or `None` when no
/// line names it. A line has seven fields split by `:`. A line that does
/// not read is skipped.
///
/// # Errors
///
/// The error of the read.
pub fn uid_of(user: &str, passwd: &Path) -> io::Result<Option<u32>> {
    let file = fs::File::open(passwd)?;
    for line in BufReader::new(file).lines() {
        let line = line?;
        let fields: Vec<&str> = line.split(':').collect();
        let (Some(name), Some(uid), 7) = (fields.first(), fields.get(2), fields.len()) else {
            continue;
        };
        if *name == user {
            return Ok(uid.parse().ok());
        }
    }
    Ok(None)
}
