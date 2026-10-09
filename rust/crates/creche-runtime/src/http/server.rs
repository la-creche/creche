//! The listeners of a service: the bind, and the serve loop.
//!
//! A service binds first and serves second. [`bind`] gives a [`Bound`] for
//! each address, and an error of a bind is an error before the first request.
//! [`serve`] then runs the router on each listener until the stop signal of
//! the process.
//!
//! A service binds a Unix socket or a LAN address. `BindAddress` of
//! `creche-contracts` refuses each text that means each interface. [`bind`]
//! also refuses a host name that has such an address.
//!
//! The stop of [`serve`] has three steps:
//!
//! 1. At the stop signal, each listener closes. No address takes a new
//!    connection. [`serve`] removes the file of each Unix socket, when the
//!    path still names the socket that [`bind`] made.
//! 2. A connection with no request closes at once. A connection with a
//!    request closes after its answer.
//! 3. At the drain limit, [`serve`] ends each connection that is still open
//!    and returns.
//!
//! The handler of a request is a tracked task when the router has the edge
//! layer of [`layers`](super::layers). Step 3 does not stop such a handler.
//! `Tasks::drain` waits for it.
//!
//! A listener sets no time limit on a connection. A client that connects and
//! sends no request holds one descriptor until it leaves or until the stop
//! of the service.
//!
//! A listener refuses three kinds of request before the router gets them,
//! as the server of the Python services does. The doc comment of [`serve`]
//! lists them.
//!
//! The doc comment of [`bind`] and of [`serve`] says what each function does
//! in another way than its Python origin.
//!
//! The Python origin is `attendance/src/attendance/__main__.py:68-186`, and
//! `uvicorn` for each other service.

use std::error::Error;
use std::fmt;
use std::fs;
use std::future::{self, Future, IntoFuture};
use std::io::{self, IoSlice};
use std::net::SocketAddr;
use std::os::unix::fs::{FileTypeExt, PermissionsExt};
use std::panic::{self, AssertUnwindSafe};
use std::path::{Path, PathBuf};
use std::pin::Pin;
use std::task::{Context, Poll};
use std::time::Duration;

use axum::Router;
use axum::body::Body;
use axum::extract::Request;
use axum::response::Response;
use axum::serve::Listener;
use creche_contracts::config::{BindAddress, SocketPath};
use http::header::{CONNECTION, CONTENT_TYPE, HOST};
use http::{HeaderValue, StatusCode, Version};
use rustix::io::Errno;
use tokio::io::{AsyncRead, AsyncWrite, ReadBuf};
use tokio::net::{TcpListener, TcpSocket, TcpStream, UnixListener, UnixSocket, UnixStream};
use tokio::runtime::Handle;
use tokio::sync::oneshot;
use tokio::task::{Id, JoinSet};
use tokio_util::sync::{CancellationToken, WaitForCancellationFutureOwned};
use tokio_util::task::TaskTracker;
use tokio_util::task::task_tracker::TaskTrackerToken;
use tower::ServiceExt;

use crate::readfile::{FileFacts, os_text};
use crate::tasks::{Drained, Tasks};

/// The target of each line that this module and the edge layer write to the
/// log.
pub(super) const LOG_TARGET: &str = "http";

/// The mode of a new socket: the owner and the group read and write. A
/// client needs the write bit to connect. The Python origin is
/// `attendance/src/attendance/__main__.py:37`.
const SOCKET_MODE: u32 = 0o660;

/// The mode of the directory of a socket. The setgid bit gives a new socket
/// the group of the directory. The Python origin is
/// `attendance/src/attendance/__main__.py:42`.
const SOCKET_DIR_MODE: u32 = 0o2750;

/// The setgid bit of a mode.
const SETGID: u32 = 0o2000;

/// How many connections wait for an accept. The Python origin is the
/// `backlog` default of `uvicorn` (`uvicorn/config.py:228`).
const BACKLOG: u32 = 2048;

/// The pause after an accept that failed. The Python origin is
/// `ACCEPT_RETRY_DELAY` of `asyncio/constants.py:11`.
const ACCEPT_RETRY: Duration = Duration::from_secs(1);

/// The text of an error for a call on a thread with no runtime.
const NO_RUNTIME: &str = "no runtime runs on this thread";

/// The text of a line for a wait in a runtime with no timer.
const NO_TIMER: &str = "the runtime of this thread has no timer";

/// The text of an error for a listener in a runtime with no I/O driver.
const NO_IO_DRIVER: &str = "the runtime of this thread has no I/O driver";

/// The text of an error for a step that its thread did not end.
const STEP_LOST: &str = "the runtime stopped the step before its end";

/// The text of an error for a socket path with no directory above it.
const NO_DIRECTORY: &str = "the socket is in no directory that this service can prepare";

/// The text of an error for a host with no address that this host has.
const NO_ADDRESS: &str = "the host has no address on this machine";

/// The text of an error for a host whose address stands for each interface.
const EACH_INTERFACE: &str = "the host has the address of each interface";

/// The text of an error for an IPv4 address in the IPv6 form.
const MAPPED_ADDRESS: &str = "an IPv6 listener does not bind an IPv4 address";

/// The text of the error that a cut connection gives to its reader.
const CUT: &str = "the service ended the connection at its stop";

/// The content type of a text answer of the Python server and of the Python
/// web framework.
pub(super) const PLAIN_TEXT: &str = "text/plain; charset=utf-8";

/// The body of the answer to a request that the Python server refuses. The
/// Python origin is `uvicorn/protocols/http/h11_impl.py:183`.
const INVALID_REQUEST: &str = "Invalid HTTP request received.";

/// The value of the `Connection` header of that answer. The server closes
/// the connection after an answer with this value.
const CLOSE: &str = "close";

/// Where a service listens.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Listen {
    /// A Unix socket.
    Unix {
        /// The path of the socket.
        socket: SocketPath,
        /// What [`bind`] does with the directory of the socket.
        dir: SocketDir,
    },
    /// A TCP address.
    Tcp(BindAddress),
}

/// What [`bind`] does with the directory of a Unix socket.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SocketDir {
    /// Make the directory with mode `2750`. A client in the group of the
    /// directory can then connect, and a new socket gets that group.
    PrepareSetgid,
    /// Do not change the directory. It must exist.
    LeaveAsItIs,
}

/// The step of a bind that failed.
///
/// The set is closed. It does not cross a process boundary.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum BindStep {
    /// Make the directory of the socket, or set its mode.
    PrepareDir,
    /// Remove the socket file of an earlier process.
    RemoveStale,
    /// Bind the address.
    Bind,
    /// Set the mode of the new socket.
    SetMode,
}

impl fmt::Display for BindStep {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Self::PrepareDir => "prepare the directory of the socket",
            Self::RemoveStale => "remove the old socket file",
            Self::Bind => "bind",
            Self::SetMode => "set the mode of the socket",
        })
    }
}

/// Why a service does not listen on an address.
///
/// Only [`bind`] gives a value. Read it through its four functions:
///
/// ```
/// use creche_runtime::http::server::{BindError, BindStep, Listen, SocketDir, bind};
///
/// let listen = Listen::Unix {
///     socket: "/creche-doc-test-no-such-dir/probe.sock".parse()?,
///     dir: SocketDir::LeaveAsItIs,
/// };
/// let runtime = tokio::runtime::Builder::new_current_thread()
///     .enable_all()
///     .build()?;
///
/// let error: BindError = runtime.block_on(bind(listen)).unwrap_err();
///
/// assert_eq!(error.step(), BindStep::Bind);
/// assert_eq!(error.address(), "/creche-doc-test-no-such-dir/probe.sock");
/// assert_eq!(error.kind(), std::io::ErrorKind::NotFound);
/// assert_eq!(error.os_text(), "No such file or directory");
/// # Ok::<(), Box<dyn std::error::Error>>(())
/// ```
///
/// Code outside this module cannot build a value or change a field. An
/// error then always names a step that failed for the address it names:
///
/// ```compile_fail,E0451
/// use creche_runtime::http::server::{BindError, BindStep, Listen, SocketDir, bind};
///
/// let error = BindError {
///     step: BindStep::Bind,
///     address: String::from("192.0.2.10:8340"),
///     kind: std::io::ErrorKind::AddrInUse,
///     os_text: String::from("Address already in use"),
/// };
/// ```
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct BindError {
    step: BindStep,
    address: String,
    kind: io::ErrorKind,
    os_text: String,
}

impl BindError {
    /// The step that failed.
    #[must_use]
    pub const fn step(&self) -> BindStep {
        self.step
    }

    /// The address: the path of the socket, or `host:port`.
    #[must_use]
    pub fn address(&self) -> &str {
        &self.address
    }

    /// The kind of the error of the operating system.
    #[must_use]
    pub const fn kind(&self) -> io::ErrorKind {
        self.kind
    }

    /// The text of the error, as `strerror` of Python gives it. It holds no
    /// number of the error.
    #[must_use]
    pub fn os_text(&self) -> &str {
        &self.os_text
    }
}

impl fmt::Display for BindError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            f,
            "cannot listen on {}: the step \"{}\" failed: {}",
            self.address, self.step, self.os_text
        )
    }
}

impl Error for BindError {}

/// One step of a bind that failed, before the error has its address.
struct Failed {
    step: BindStep,
    error: io::Error,
}

impl Failed {
    /// The error of the bind of `address`.
    fn at(&self, address: &str) -> BindError {
        BindError {
            step: self.step,
            address: address.to_owned(),
            kind: self.error.kind(),
            os_text: os_text(&self.error),
        }
    }
}

/// Makes the error of `step` from an error of the operating system.
fn failed_at(step: BindStep) -> impl Fn(io::Error) -> Failed {
    move |error| Failed { step, error }
}

/// Binds one address.
///
/// For a Unix socket the function makes these steps in this order:
///
/// 1. With [`SocketDir::PrepareSetgid`], it makes the directory of the
///    socket. It sets the mode `2750` only when the directory has another
///    mode. A mode that it cannot set is one `WARNING` line and no error.
/// 2. It removes the path, only when the path is a socket. A socket file
///    there is the file of an earlier process.
/// 3. It binds the path and sets the new socket to mode `0660`.
/// 4. It reads the inode of the new file. [`serve`] removes the file at its
///    stop only while the path has that inode.
/// 5. It starts to listen. A client cannot connect before this step, so no
///    client connects to a socket with another mode.
///
/// For a TCP address the function binds each address of the host, as
/// `asyncio` does. An IP address is one listener. A host name can be more
/// than one: `localhost` is `127.0.0.1` and `::1` on most hosts. An address
/// that this host does not have is not bound. The bind fails when no address
/// is left. It also fails for a host name that has the address of each
/// interface: a service never binds `0.0.0.0`.
///
/// Call the function inside the runtime of the service. On a thread with no
/// runtime, and in a runtime with no I/O driver, it returns an error and does
/// not panic.
///
/// The Python origins are `_prepare_socket_dir`, `_clear_stale_socket` and
/// `_publish_socket_mode` of
/// `attendance/src/attendance/__main__.py:120-160`, and the `startup` of
/// `:179-186`. The origin of step 4 is `create_unix_server` of
/// `asyncio/unix_events.py:345-352` (CPython 3.13). For a TCP address the
/// origin is `uvicorn.run`, for example
/// `door-owui/src/agent_door_owui/__main__.py:61`.
///
/// The function differs from those origins in three ways:
///
/// - The Python socket listens first and gets mode `0660` second
///   (`uvicorn/server.py:162-165`, then `__main__.py:156-160`), so a client
///   can connect before the last mode. Here the mode comes before the listen.
/// - Python sets the mode of `/` for a socket in the root directory
///   (`__main__.py:135-141`). With [`SocketDir::PrepareSetgid`] the function
///   refuses that socket.
/// - A Python service refuses only the text of an address of each interface,
///   for example `door-owui/src/agent_door_owui/config.py:87`, and it binds
///   a host name that has such an address. The function refuses that host
///   name too.
///
/// ```
/// use std::time::Duration;
///
/// use axum::Router;
/// use axum::routing::get;
/// use creche_runtime::http::server::{Bound, Listen, SocketDir, bind, serve};
/// use creche_runtime::tasks::{Drained, Tasks, shutdown_pair};
/// use creche_testkit::root::TempRoot;
///
/// let root = TempRoot::new()?;
/// let socket = root.path().join("sock/probe.sock");
/// let listen = Listen::Unix {
///     socket: socket.to_str().ok_or("the path is not text")?.parse()?,
///     dir: SocketDir::PrepareSetgid,
/// };
/// let runtime = tokio::runtime::Builder::new_current_thread()
///     .enable_all()
///     .build()?;
///
/// let drained = runtime.block_on(async {
///     let bound: Bound = bind(listen).await?;
///     assert_eq!(bound.local_port(), None);
///
///     let (trigger, shutdown) = shutdown_pair();
///     let tasks = Tasks::new(shutdown);
///     let app = Router::new().route("/healthz", get(|| async { "ok" }));
///
///     trigger.trigger();
///     let drained = serve(vec![bound], app, &tasks, Duration::from_secs(1)).await?;
///
///     Ok::<_, Box<dyn std::error::Error>>(drained)
/// })?;
///
/// assert_eq!(drained, Drained::Clean);
/// // The stop removed the file of the socket.
/// assert!(!socket.exists());
/// # Ok::<(), Box<dyn std::error::Error>>(())
/// ```
///
/// # Errors
///
/// [`BindError`] with the step that failed.
pub async fn bind(listen: Listen) -> Result<Bound, BindError> {
    match listen {
        Listen::Unix { socket, dir } => bind_unix(Host, socket, dir).await,
        Listen::Tcp(address) => bind_tcp(&address).await,
    }
}

/// The error of a bind on a thread with no runtime.
fn no_runtime(address: &str) -> BindError {
    Failed {
        step: BindStep::Bind,
        error: io::Error::other(NO_RUNTIME),
    }
    .at(address)
}

/// Binds the Unix socket `path` through `steps`.
async fn bind_unix<S>(steps: S, path: SocketPath, dir: SocketDir) -> Result<Bound, BindError>
where
    S: Steps + Send + 'static,
{
    let address = path.as_str().to_owned();
    let Ok(runtime) = Handle::try_current() else {
        return Err(no_runtime(&address));
    };

    // Each step before the listen is a call of the file system, so it runs
    // on the pool for blocking calls.
    let prepared = runtime.spawn_blocking(move || bound_socket(&steps, path.as_path(), dir));
    let (socket, file) = match prepared.await {
        Ok(Ok(bound)) => bound,
        Ok(Err(failed)) => return Err(failed.at(&address)),
        // The text of a join error can hold the message of a panic.
        Err(_) => {
            return Err(Failed {
                step: BindStep::Bind,
                error: io::Error::other(STEP_LOST),
            }
            .at(&address));
        }
    };
    let listener = registered(|| socket.listen(BACKLOG))
        .map_err(failed_at(BindStep::Bind))
        .map_err(|failed| failed.at(&address))?;

    Ok(Bound {
        address,
        port: None,
        sockets: vec![Socket::Unix(listener)],
        file,
    })
}

/// The steps of a Unix socket before it listens: the directory, the old
/// file, the bind, the mode and the inode of the new file.
///
/// The file is `None` when the path names no file after the bind: another
/// process removed it. [`serve`] then removes no file at its stop.
fn bound_socket(
    steps: &impl Steps,
    path: &Path,
    dir: SocketDir,
) -> Result<(UnixSocket, Option<SocketFile>), Failed> {
    if dir == SocketDir::PrepareSetgid {
        prepare_dir(steps, path).map_err(failed_at(BindStep::PrepareDir))?;
    }
    clear_stale(steps, path).map_err(failed_at(BindStep::RemoveStale))?;

    let socket = bound_unix(path).map_err(failed_at(BindStep::Bind))?;
    // The socket does not listen yet, so no client connects before the mode
    // is in place.
    steps
        .set_mode(path, SOCKET_MODE)
        .map_err(failed_at(BindStep::SetMode))?;
    let file = steps
        .inode_of(path)
        .map_err(failed_at(BindStep::Bind))?
        .map(|inode| SocketFile {
            path: path.to_owned(),
            inode,
        });

    Ok((socket, file))
}

/// The file of a Unix socket that [`bind`] made.
#[derive(Debug)]
struct SocketFile {
    path: PathBuf,
    /// The inode of the file after the bind. A process that takes the path
    /// later makes a file with another inode.
    inode: u64,
}

/// Removes `file` when its path still names the socket that [`bind`] made.
///
/// A path that names no file, and a path that another process took, are no
/// error. Each other error is one `ERROR` line: the stop continues, and the
/// next [`bind`] removes the file.
///
/// The Python origin is `_stop_serving` of
/// `asyncio/unix_events.py:474-493` (CPython 3.13).
fn remove_own(steps: &impl Steps, file: &SocketFile) {
    let removed = match steps.inode_of(&file.path) {
        Ok(Some(inode)) if inode == file.inode => steps.remove_file(&file.path),
        Ok(_) => return,
        Err(error) => Err(error),
    };

    match removed {
        Ok(()) => {}
        // Another process removed the file between the two calls.
        Err(error) if is_absent(&error) => {}
        Err(error) => crate::error!(
            LOG_TARGET,
            "could not remove the socket file {}: {}",
            file.path.display(),
            os_text(&error)
        ),
    }
}

/// Removes each file of `files` that this process still owns, on the pool
/// for blocking calls.
///
/// The pool runs the step to its end when the caller drops this future.
async fn remove_files<S>(steps: S, files: Vec<SocketFile>)
where
    S: Steps + Send + 'static,
{
    if files.is_empty() {
        return;
    }
    let Ok(runtime) = Handle::try_current() else {
        return;
    };
    let removed = runtime.spawn_blocking(move || {
        for file in &files {
            remove_own(&steps, file);
        }
    });

    // The step gives no value. A runtime that stops can end the wait.
    let _ = removed.await;
}

/// Makes the directory of the socket `path` and gives it the mode `2750`.
///
/// The function sets the mode only when it is wrong. A `chmod` by a process
/// that is not in the group of the directory removes the setgid bit and
/// gives no error. A mode that the function cannot set, and a setgid bit
/// that is gone after it, are each one `WARNING` line: the service can
/// serve, and only a client of the group cannot connect yet.
///
/// The Python origin is `_prepare_socket_dir` of
/// `attendance/src/attendance/__main__.py:126-153`.
fn prepare_dir(steps: &impl Steps, path: &Path) -> io::Result<()> {
    // Python sets the mode of `/` for a socket in the root directory. A
    // service must not change that directory.
    let dir = path
        .parent()
        .filter(|dir| dir.parent().is_some())
        .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput, NO_DIRECTORY))?;

    steps.make_dirs(dir)?;

    if steps.mode_of(dir)? == SOCKET_DIR_MODE {
        return Ok(());
    }

    if let Err(error) = steps.set_mode(dir, SOCKET_DIR_MODE) {
        crate::warning!(
            LOG_TARGET,
            "could not set mode on {}: {}",
            dir.display(),
            os_text(&error)
        );

        return Ok(());
    }

    if steps.mode_of(dir)? & SETGID == 0 {
        crate::warning!(
            LOG_TARGET,
            "{dir} lost its setgid bit: this process is not in the group of the directory. \
             Run: chmod g+s {dir}. Until then a client of that group cannot connect",
            dir = dir.display()
        );
    }

    Ok(())
}

/// Removes `path` when it is a socket: the file of an earlier process. Such
/// a file refuses the new bind.
///
/// The Python origin is `_clear_stale_socket` of
/// `attendance/src/attendance/__main__.py:120-123`.
fn clear_stale(steps: &impl Steps, path: &Path) -> io::Result<()> {
    if !steps.is_socket(path)? {
        return Ok(());
    }

    match steps.remove_file(path) {
        // Another process removed the file first. Python has `missing_ok`.
        Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(()),
        removed => removed,
    }
}

/// A new Unix socket that holds `path` and does not listen.
fn bound_unix(path: &Path) -> io::Result<UnixSocket> {
    let socket = UnixSocket::new_stream()?;
    socket.bind(path)?;

    Ok(socket)
}

/// Runs `listen`, which gives a socket to the runtime.
///
/// `tokio` panics there in a runtime with no I/O driver. The function gives
/// an error for that panic.
fn registered<T>(listen: impl FnOnce() -> io::Result<T>) -> io::Result<T> {
    match panic::catch_unwind(AssertUnwindSafe(listen)) {
        Ok(listener) => listener,
        Err(payload) => {
            drop(payload);

            Err(io::Error::other(NO_IO_DRIVER))
        }
    }
}

/// The calls of the file system that the bind of a Unix socket makes.
///
/// [`Host`] makes each call with the standard library. A test gives a value
/// that fails at one call, as the Python test replaces `Path.chmod`
/// (`attendance/tests/test_main.py:145`).
trait Steps {
    /// Makes `dir` and each directory above it.
    fn make_dirs(&self, dir: &Path) -> io::Result<()>;

    /// The permission bits of `path`.
    fn mode_of(&self, path: &Path) -> io::Result<u32>;

    /// Sets the permission bits of `path`.
    fn set_mode(&self, path: &Path, mode: u32) -> io::Result<()>;

    /// Whether `path` is a socket. A path that does not exist is no socket.
    fn is_socket(&self, path: &Path) -> io::Result<bool>;

    /// The inode of the file that `path` names. `None` for a path that names
    /// no file.
    fn inode_of(&self, path: &Path) -> io::Result<Option<u64>>;

    /// Removes one file.
    fn remove_file(&self, path: &Path) -> io::Result<()>;
}

/// The [`Steps`] of the host: each call is a call of the standard library.
struct Host;

impl Steps for Host {
    fn make_dirs(&self, dir: &Path) -> io::Result<()> {
        fs::create_dir_all(dir)
    }

    fn mode_of(&self, path: &Path) -> io::Result<u32> {
        // `FileFacts` holds the permission bits, as `stat.S_IMODE` of Python
        // gives them.
        Ok(FileFacts::from(&fs::metadata(path)?).mode())
    }

    fn set_mode(&self, path: &Path, mode: u32) -> io::Result<()> {
        fs::set_permissions(path, fs::Permissions::from_mode(mode))
    }

    fn is_socket(&self, path: &Path) -> io::Result<bool> {
        match fs::metadata(path) {
            Ok(facts) => Ok(facts.file_type().is_socket()),
            Err(error) if is_absent(&error) => Ok(false),
            Err(error) => Err(error),
        }
    }

    fn inode_of(&self, path: &Path) -> io::Result<Option<u64>> {
        match fs::metadata(path) {
            Ok(facts) => Ok(Some(FileFacts::from(&facts).ino())),
            Err(error) if is_absent(&error) => Ok(None),
            Err(error) => Err(error),
        }
    }

    fn remove_file(&self, path: &Path) -> io::Result<()> {
        fs::remove_file(path)
    }
}

/// Whether `error` says that a path names no file. `Path.is_socket` of
/// Python gives `False` for these four errors and raises each other one
/// (`pathlib/_abc.py:31` of CPython 3.13).
fn is_absent(error: &io::Error) -> bool {
    matches!(
        Errno::from_io_error(error),
        Some(Errno::NOENT | Errno::NOTDIR | Errno::BADF | Errno::LOOP)
    )
}

/// Why one address of a host got no socket.
enum Unbound {
    /// The host does not have the address. The bind continues with the next
    /// address.
    Skip(io::Error),
    /// The bind of the whole host fails.
    Stop(io::Error),
}

/// Binds the TCP address `address`: one socket for each address of its host.
///
/// The Python origin is `create_server` of `asyncio/base_events.py:1611-1645`
/// (CPython 3.13), which `uvicorn` calls with the host and the port.
async fn bind_tcp(address: &BindAddress) -> Result<Bound, BindError> {
    let text = address.to_string();
    let failed = |error: io::Error| failed_at(BindStep::Bind)(error).at(&text);

    if Handle::try_current().is_err() {
        return Err(no_runtime(&text));
    }

    let host = address.host().as_str();
    let found = tokio::net::lookup_host((host, address.port().get()))
        .await
        .map_err(failed)?;
    let listeners = listeners_on(found).map_err(failed)?;

    let mut sockets = Vec::with_capacity(listeners.len());
    let mut port = address.port().get();
    for listener in listeners {
        if let Ok(place) = listener.local_addr() {
            port = place.port();
        }
        sockets.push(Socket::Tcp(listener));
    }

    Ok(Bound {
        address: tcp_text(host, port),
        port: Some(port),
        sockets,
        file: None,
    })
}

/// One listener for each address of `places` that this host has.
///
/// An address that this host does not have gets no listener, and the bind
/// continues with the next address. Each other error of a bind stops the
/// whole call. The call fails when no address is left.
///
/// The Python origin is the loop over the addresses of a host in
/// `create_server` (`asyncio/base_events.py:1611-1645` of CPython 3.13).
fn listeners_on(places: impl IntoIterator<Item = SocketAddr>) -> io::Result<Vec<TcpListener>> {
    let mut distinct: Vec<SocketAddr> = Vec::new();
    for place in places {
        if !distinct.contains(&place) {
            distinct.push(place);
        }
    }

    // Each socket binds first. The listen comes second, as in `asyncio`.
    let mut bound = Vec::with_capacity(distinct.len());
    let mut skipped = None;
    for place in distinct {
        match bound_tcp(place) {
            Ok(socket) => bound.push(socket),
            Err(Unbound::Skip(error)) => skipped = Some(error),
            Err(Unbound::Stop(error)) => return Err(error),
        }
    }
    if bound.is_empty() {
        return Err(
            skipped.unwrap_or_else(|| io::Error::new(io::ErrorKind::AddrNotAvailable, NO_ADDRESS))
        );
    }

    bound
        .into_iter()
        .map(|socket| registered(|| socket.listen(BACKLOG)))
        .collect()
}

/// A new TCP socket that holds `place` and does not listen.
///
/// `SO_REUSEADDR` lets a new process bind the port of a process that
/// stopped, while the kernel still holds an old connection of that port.
/// `asyncio` sets it too (`asyncio/base_events.py:1611`).
///
/// `asyncio` also makes each IPv6 socket take IPv6 only
/// (`asyncio/base_events.py:1626`), and such a socket does not bind an IPv4
/// address in the IPv6 form, `::ffff:192.0.2.10`. The socket type of `tokio`
/// has no such switch, so the function refuses that form itself.
fn bound_tcp(place: SocketAddr) -> Result<TcpSocket, Unbound> {
    if is_each_interface(&place) {
        return Err(Unbound::Stop(io::Error::new(
            io::ErrorKind::InvalidInput,
            EACH_INTERFACE,
        )));
    }

    let made = match place {
        SocketAddr::V4(_) => TcpSocket::new_v4(),
        SocketAddr::V6(v6) if v6.ip().to_ipv4_mapped().is_some() => {
            return Err(Unbound::Skip(io::Error::new(
                io::ErrorKind::AddrNotAvailable,
                MAPPED_ADDRESS,
            )));
        }
        SocketAddr::V6(_) => TcpSocket::new_v6(),
    };
    // The host has no socket of this family. `asyncio` continues with the
    // next address.
    let socket = made.map_err(Unbound::Skip)?;
    socket.set_reuseaddr(true).map_err(Unbound::Stop)?;

    match socket.bind(place) {
        Ok(()) => Ok(socket),
        Err(error) if error.kind() == io::ErrorKind::AddrNotAvailable => Err(Unbound::Skip(error)),
        Err(error) => Err(Unbound::Stop(error)),
    }
}

/// Whether a listener on `place` takes a connection on each interface of the
/// host: `0.0.0.0`, `::`, and `0.0.0.0` in the IPv6 form.
///
/// `BindAddress` refuses each such text. A host name can still have such an
/// address, for example through the hosts file. The Python services check
/// only the text, for example
/// `door-owui/src/agent_door_owui/config.py:87`.
fn is_each_interface(place: &SocketAddr) -> bool {
    match place {
        SocketAddr::V4(v4) => v4.ip().is_unspecified(),
        SocketAddr::V6(v6) => {
            v6.ip().is_unspecified()
                || v6
                    .ip()
                    .to_ipv4_mapped()
                    .is_some_and(|mapped| mapped.is_unspecified())
        }
    }
}

/// The text of a TCP address: `host:port`. An IPv6 host gets brackets, as
/// the `Display` of `BindAddress` writes it.
fn tcp_text(host: &str, port: u16) -> String {
    if host.contains(':') {
        return format!("[{host}]:{port}");
    }

    format!("{host}:{port}")
}

/// One address that the process holds and does not serve yet.
///
/// A value holds each listener of the address. A client can connect, and the
/// connection waits until [`serve`] takes it. A dropped value closes each
/// listener. The file of a Unix socket then stays, and the next [`bind`]
/// removes it.
///
/// Only [`bind`] gives a value:
///
/// ```no_run
/// use creche_runtime::http::server::{BindError, Bound, Listen, bind};
///
/// # async fn start() -> Result<(), Box<dyn std::error::Error>> {
/// let bound: Bound = bind(Listen::Tcp("192.0.2.10:8340".parse()?)).await?;
/// assert_eq!(bound.describe(), "192.0.2.10:8340");
/// assert_eq!(bound.local_port(), Some(8340));
/// # Ok(())
/// # }
/// ```
///
/// Code outside this module cannot build a value or change a field. A value
/// then always names the address that its listeners hold:
///
/// ```compile_fail,E0451
/// use creche_runtime::http::server::{BindError, Bound, Listen, bind};
///
/// # async fn start() -> Result<(), Box<dyn std::error::Error>> {
/// let bound: Bound = bind(Listen::Tcp("192.0.2.10:8340".parse()?)).await?;
/// let other = Bound {
///     port: Some(8341),
///     ..bound
/// };
/// # Ok(())
/// # }
/// ```
#[derive(Debug)]
pub struct Bound {
    /// The address as text: the path of the socket, or `host:port`.
    address: String,
    /// The TCP port. `None` for a Unix socket.
    port: Option<u16>,
    /// Each listener of the address.
    sockets: Vec<Socket>,
    /// The file of a Unix socket. `None` for a TCP address.
    file: Option<SocketFile>,
}

impl Bound {
    /// The address as text, for the log: the path of the socket, or
    /// `host:port` with the port that the operating system gave.
    ///
    /// The Python origin is the start message of `uvicorn`, which names the
    /// address of each listener.
    #[must_use]
    pub fn describe(&self) -> String {
        self.address.clone()
    }

    /// The TCP port of the listener. `None` for a Unix socket.
    ///
    /// The function has no Python origin. A Python service reads the port
    /// from its config, for example
    /// `door-owui/src/agent_door_owui/__main__.py:61`.
    #[must_use]
    pub fn local_port(&self) -> Option<u16> {
        self.port
    }
}

/// One listener of an address.
#[derive(Debug)]
enum Socket {
    Tcp(TcpListener),
    Unix(UnixListener),
    /// A listener whose accept gives each error of the list, and then waits
    /// with no end.
    #[cfg(test)]
    Scripted(std::collections::VecDeque<io::Error>),
}

impl Socket {
    /// Waits for the next connection.
    async fn accept(&mut self) -> io::Result<Stream> {
        match self {
            // The trait `Listener` of `axum` has an `accept` for the two
            // listener types too, and that one gives no error.
            Self::Tcp(listener) => {
                let (stream, _peer) = TcpListener::accept(listener).await?;
                // `asyncio` turns the Nagle algorithm off for each TCP
                // connection (`asyncio/selector_events.py:948`): a small
                // write, for example one line of a stream, goes out at once.
                // A socket that refuses the option still serves.
                let _ = stream.set_nodelay(true);

                Ok(Stream::Tcp(stream))
            }
            Self::Unix(listener) => {
                let (stream, _peer) = UnixListener::accept(listener).await?;

                Ok(Stream::Unix(stream))
            }
            #[cfg(test)]
            Self::Scripted(errors) => match errors.pop_front() {
                Some(error) => Err(error),
                None => future::pending().await,
            },
        }
    }
}

/// The bytes of one connection.
enum Stream {
    Tcp(TcpStream),
    Unix(UnixStream),
}

/// One connection, which [`serve`] can end.
///
/// `axum::serve` gives each connection a task of its own, and no code here
/// holds that task. The cut is thus a signal that the connection reads: each
/// read and each write gives an error after it. The server of `hyper` then
/// closes the connection.
///
/// A connection also gives the end of the bytes of its client one poll late.
/// [`ClientEnd`] has the reason.
struct Connection {
    stream: Stream,
    /// Ready after the cut. Each read and each write polls it, so the task
    /// of the connection wakes at the cut.
    cut: Pin<Box<WaitForCancellationFutureOwned>>,
    /// The drop of this value takes the connection out of the count of the
    /// open connections.
    _open: TaskTrackerToken,
    end: ClientEnd,
}

/// Whether a read of a connection saw the end of the bytes of its client.
///
/// A client can send a whole request and close its side in the same moment.
/// `hyper` reads the request, and it reads again before it polls the future
/// of the request. At the end of the bytes it then drops that future, so no
/// handler starts. A Python service runs the handler of such a request to
/// its end: `uvicorn` starts the task of a handler when it reads the head of
/// a request (`uvicorn/protocols/http/h11_impl.py:259-263`).
///
/// The read that sees the end thus gives `hyper` no answer and wakes the
/// task. `hyper` polls the future of the request in that same pass, and the
/// edge layer starts the task of the handler there. The next read gives the
/// end. The client gets no answer, as from a Python service.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum ClientEnd {
    /// No read saw the end.
    NotSeen,
    /// One read saw the end. Each later read gives it.
    Seen,
}

impl Connection {
    /// Whether [`serve`] ended the connection.
    ///
    /// The read, each write and the flush ask this first. `hyper` waits in
    /// one of the three when the cut comes, and which one depends on the
    /// state of the request. For example, only the flush runs while a
    /// handler gives no answer and does not read a body that waits. Only
    /// the check that runs wakes the task of the connection at the cut.
    fn is_cut(&mut self, context: &mut Context<'_>) -> bool {
        self.cut.as_mut().poll(context).is_ready()
    }
}

/// The error of a read or a write after the cut.
fn cut_error() -> io::Error {
    io::Error::new(io::ErrorKind::ConnectionAborted, CUT)
}

impl AsyncRead for Connection {
    fn poll_read(
        self: Pin<&mut Self>,
        context: &mut Context<'_>,
        buffer: &mut ReadBuf<'_>,
    ) -> Poll<io::Result<()>> {
        let this = self.get_mut();
        if this.is_cut(context) {
            return Poll::Ready(Err(cut_error()));
        }
        if this.end == ClientEnd::Seen {
            return Poll::Ready(Ok(()));
        }

        let before = buffer.filled().len();
        let polled = match &mut this.stream {
            Stream::Tcp(stream) => Pin::new(stream).poll_read(context, buffer),
            Stream::Unix(stream) => Pin::new(stream).poll_read(context, buffer),
        };
        // A read into a buffer with room that gives no byte is the end.
        let at_end = matches!(polled, Poll::Ready(Ok(())))
            && buffer.filled().len() == before
            && buffer.remaining() > 0;
        if !at_end {
            return polled;
        }

        this.end = ClientEnd::Seen;
        context.waker().wake_by_ref();

        Poll::Pending
    }
}

impl AsyncWrite for Connection {
    fn poll_write(
        self: Pin<&mut Self>,
        context: &mut Context<'_>,
        bytes: &[u8],
    ) -> Poll<io::Result<usize>> {
        let this = self.get_mut();
        if this.is_cut(context) {
            return Poll::Ready(Err(cut_error()));
        }

        match &mut this.stream {
            Stream::Tcp(stream) => Pin::new(stream).poll_write(context, bytes),
            Stream::Unix(stream) => Pin::new(stream).poll_write(context, bytes),
        }
    }

    fn poll_write_vectored(
        self: Pin<&mut Self>,
        context: &mut Context<'_>,
        slices: &[IoSlice<'_>],
    ) -> Poll<io::Result<usize>> {
        let this = self.get_mut();
        if this.is_cut(context) {
            return Poll::Ready(Err(cut_error()));
        }

        match &mut this.stream {
            Stream::Tcp(stream) => Pin::new(stream).poll_write_vectored(context, slices),
            Stream::Unix(stream) => Pin::new(stream).poll_write_vectored(context, slices),
        }
    }

    fn is_write_vectored(&self) -> bool {
        match &self.stream {
            Stream::Tcp(stream) => stream.is_write_vectored(),
            Stream::Unix(stream) => stream.is_write_vectored(),
        }
    }

    fn poll_flush(self: Pin<&mut Self>, context: &mut Context<'_>) -> Poll<io::Result<()>> {
        let this = self.get_mut();
        if this.is_cut(context) {
            return Poll::Ready(Err(cut_error()));
        }

        match &mut this.stream {
            Stream::Tcp(stream) => Pin::new(stream).poll_flush(context),
            Stream::Unix(stream) => Pin::new(stream).poll_flush(context),
        }
    }

    fn poll_shutdown(self: Pin<&mut Self>, context: &mut Context<'_>) -> Poll<io::Result<()>> {
        match &mut self.get_mut().stream {
            Stream::Tcp(stream) => Pin::new(stream).poll_shutdown(context),
            Stream::Unix(stream) => Pin::new(stream).poll_shutdown(context),
        }
    }
}

/// What a listener does after an accept that failed.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum AcceptFailure {
    /// One connection failed before the accept. The listener takes the next
    /// connection at once.
    Connection,
    /// The host has no resource for a connection at this moment, or the
    /// error has no known cause. The listener writes one line and tries
    /// again after [`ACCEPT_RETRY`]. In a runtime with no timer it stops, as
    /// for [`AcceptFailure::Listener`].
    Passing,
    /// The descriptor is not a socket that listens. No retry corrects that,
    /// so the listener stops.
    Listener,
}

impl AcceptFailure {
    /// The class of `error`.
    ///
    /// The Python origin is `_accept_connection` of
    /// `asyncio/selector_events.py:167-209` (CPython 3.13). It continues
    /// after `ECONNABORTED`. It waits for one second after `EMFILE`,
    /// `ENFILE`, `ENOBUFS` and `ENOMEM`. It writes each other error to the
    /// log and continues.
    fn of(error: &io::Error) -> Self {
        let gone = matches!(
            error.kind(),
            io::ErrorKind::ConnectionAborted
                | io::ErrorKind::ConnectionReset
                | io::ErrorKind::ConnectionRefused
                | io::ErrorKind::Interrupted
        );
        if gone {
            return Self::Connection;
        }

        match Errno::from_io_error(error) {
            Some(Errno::BADF | Errno::INVAL | Errno::NOTSOCK | Errno::OPNOTSUPP) => Self::Listener,
            _ => Self::Passing,
        }
    }
}

/// One listener, as `axum::serve` takes it.
struct Accepting {
    socket: Socket,
    /// The address of the listener, for a line of the log and for an error.
    address: String,
    /// The signal that ends each connection of [`serve`].
    cut: CancellationToken,
    /// The count of the open connections of [`serve`].
    open: TaskTracker,
    /// Where the listener gives the error that stops it. `None` after that
    /// error.
    broken: Option<oneshot::Sender<ServeError>>,
}

impl Accepting {
    /// Does what the class of `error` says.
    async fn after(&mut self, error: &io::Error) {
        match AcceptFailure::of(error) {
            AcceptFailure::Connection => {}
            AcceptFailure::Passing => {
                // A listener that cannot wait must not try again in a loop.
                let Some(pause) = pause() else {
                    return self.stop_with(error).await;
                };

                crate::error!(
                    LOG_TARGET,
                    "the listener on {} did not accept a connection: {}. \
                     It tries again in {} s",
                    self.address,
                    os_text(error),
                    ACCEPT_RETRY.as_secs()
                );
                pause.await;
            }
            AcceptFailure::Listener => self.stop_with(error).await,
        }
    }

    /// Gives `error` to [`serve`] as the error that stops this listener, and
    /// then waits with no end. The task of the listener ends at that value.
    async fn stop_with(&mut self, error: &io::Error) {
        if let Some(broken) = self.broken.take() {
            // A `serve` that already stops does not read the value.
            let _ = broken.send(ServeError::Accept {
                address: self.address.clone(),
                kind: error.kind(),
                os_text: os_text(error),
            });
        }

        future::pending::<()>().await;
    }
}

/// The pause after an accept that failed. `None` when the runtime has no
/// timer: `sleep` panics there when the code makes the future.
fn pause() -> Option<tokio::time::Sleep> {
    panic::catch_unwind(|| tokio::time::sleep(ACCEPT_RETRY)).ok()
}

impl Listener for Accepting {
    type Io = Connection;
    type Addr = ();

    async fn accept(&mut self) -> (Self::Io, Self::Addr) {
        loop {
            match self.socket.accept().await {
                Ok(stream) => {
                    let connection = Connection {
                        stream,
                        // A child has a waiter list of its own. The polls of
                        // two connections thus share no lock.
                        cut: Box::pin(self.cut.child_token().cancelled_owned()),
                        _open: self.open.token(),
                        end: ClientEnd::NotSeen,
                    };

                    return (connection, ());
                }
                Err(error) => self.after(&error).await,
            }
        }
    }

    fn local_addr(&self) -> io::Result<Self::Addr> {
        Ok(())
    }
}

/// Whether `request` is one that `hyper` reads and that `h11` refuses. `h11`
/// is the reader of the Python server.
///
/// - A request of HTTP/1.1 with no `Host` header, and a request of each
///   version with more than one (`h11/_events.py:112-119`).
/// - A target with a byte past `0x7E` (`h11/_abnf.py:54` and `:83`).
fn refused_by_python(request: &Request) -> bool {
    let hosts = request.headers().get_all(HOST).iter().count();
    if hosts > 1 || (hosts == 0 && request.version() == Version::HTTP_11) {
        return true;
    }

    request
        .uri()
        .path_and_query()
        .is_some_and(|target| !target.as_str().is_ascii())
}

/// The answer of the Python server to a request that it refuses: status
/// 400, a text, and a connection that closes.
///
/// The Python origin is `send_400_response` of
/// `uvicorn/protocols/http/h11_impl.py:304-320`.
fn invalid_request() -> Response {
    let mut answer = Response::new(Body::from(INVALID_REQUEST));
    *answer.status_mut() = StatusCode::BAD_REQUEST;
    let headers = answer.headers_mut();
    headers.insert(CONTENT_TYPE, HeaderValue::from_static(PLAIN_TEXT));
    headers.insert(CONNECTION, HeaderValue::from_static(CLOSE));

    answer
}

/// Gives `request` to `app`, unless the Python server refuses it.
async fn checked(app: Router, request: Request) -> Response {
    if refused_by_python(&request) {
        return invalid_request();
    }

    app.oneshot(request)
        .await
        .unwrap_or_else(|never| match never {})
}

/// Why [`serve`] stopped before the stop signal.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ServeError {
    /// The accept loop of one listener stopped with an error.
    Accept {
        /// The address of the listener.
        address: String,
        /// The kind of the error of the operating system.
        kind: io::ErrorKind,
        /// The text of the error, as `strerror` of Python gives it.
        os_text: String,
    },
    /// The task of one listener panicked, or the runtime stopped it, or no
    /// runtime ran on the thread that called [`serve`].
    ListenerLost {
        /// The address of the listener.
        address: String,
    },
}

impl fmt::Display for ServeError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Accept {
                address, os_text, ..
            } => write!(f, "the listener on {address} stopped: {os_text}"),
            Self::ListenerLost { address } => {
                write!(f, "the task of the listener on {address} was lost")
            }
        }
    }
}

impl Error for ServeError {}

/// The tasks of the listeners of one [`serve`] call. A dropped value stops
/// each task.
struct Listeners {
    tasks: JoinSet<Option<ServeError>>,
    /// The address of each task, for the error of a task that panicked.
    addresses: Vec<(Id, String)>,
    /// The file of each Unix socket. [`serve`] takes them at its stop.
    files: Vec<SocketFile>,
}

impl Listeners {
    /// The error of a task that gave no value.
    fn lost(&self, task: Id) -> ServeError {
        let address = self
            .addresses
            .iter()
            .find(|(id, _)| *id == task)
            .map(|(_, address)| address.clone())
            .unwrap_or_default();

        ServeError::ListenerLost { address }
    }

    /// Waits for the next task that ended, and gives its error. `None` when
    /// the task had no error, and when each task ended.
    ///
    /// The future is cancel safe: a caller that drops it loses no task.
    async fn next_end(&mut self) -> Option<Option<ServeError>> {
        let ended = self.tasks.join_next_with_id().await?;

        Some(match ended {
            Ok((_, error)) => error,
            // The text of a join error can hold the message of a panic.
            Err(lost) => Some(self.lost(lost.id())),
        })
    }

    /// Waits for the first listener that stops with an error. The future
    /// never ends while each listener serves.
    async fn failure(&mut self) -> ServeError {
        loop {
            match self.next_end().await {
                Some(Some(error)) => return error,
                // A listener ends with no error only after the stop.
                Some(None) => {}
                // No listener is left. The stop signal ends the wait.
                None => future::pending::<()>().await,
            }
        }
    }

    /// Waits until each listener ended and each connection closed. The value
    /// is the first error of a listener.
    async fn all_closed(&mut self, open: &TaskTracker) -> Option<ServeError> {
        let mut first = None;
        while let Some(error) = self.next_end().await {
            first = first.or(error);
        }
        // A listener ends a moment before the last value of its last
        // connection drops.
        open.wait().await;

        first
    }
}

/// Starts one task for each listener of `bound`.
fn start(
    bound: Vec<Bound>,
    app: &Router,
    closing: &CancellationToken,
    cut: &CancellationToken,
    open: &TaskTracker,
) -> Result<Listeners, ServeError> {
    let mut listeners = Listeners {
        tasks: JoinSet::new(),
        addresses: Vec::new(),
        files: Vec::new(),
    };

    for one in bound {
        let Bound {
            address,
            sockets,
            file,
            ..
        } = one;
        listeners.files.extend(file);

        for socket in sockets {
            let Ok(runtime) = Handle::try_current() else {
                crate::error!(
                    LOG_TARGET,
                    "the listener on {address} did not start: {NO_RUNTIME}"
                );

                return Err(ServeError::ListenerLost { address });
            };
            let (broken, breaks) = oneshot::channel();
            let accepting = Accepting {
                socket,
                address: address.clone(),
                cut: cut.clone(),
                open: open.clone(),
                broken: Some(broken),
            };
            let task = listen_on(accepting, breaks, app.clone(), closing.clone());
            let handle = listeners.tasks.spawn_on(task, &runtime);

            listeners.addresses.push((handle.id(), address.clone()));
        }
    }

    Ok(listeners)
}

/// Serves `app` on one listener until `closing`, and then until each
/// connection of the listener closed. The value is the error that stopped
/// the listener before that.
async fn listen_on(
    accepting: Accepting,
    breaks: oneshot::Receiver<ServeError>,
    app: Router,
    closing: CancellationToken,
) -> Option<ServeError> {
    let served = axum::serve(accepting, app)
        .with_graceful_shutdown(closing.cancelled_owned())
        .into_future();

    tokio::select! {
        // The future of `axum::serve` gives no error.
        _ = served => None,
        Ok(error) = breaks => Some(error),
    }
}

/// Makes the future that waits for `closed`, for `limit` at most. `None`
/// when the runtime has no timer.
///
/// `timeout` panics when it has no timer. It reads the timer when the code
/// makes the future, and not at a poll.
fn limited<F: Future>(limit: Duration, closed: F) -> Option<tokio::time::Timeout<F>> {
    panic::catch_unwind(AssertUnwindSafe(|| tokio::time::timeout(limit, closed))).ok()
}

/// Serves `app` on each listener of `bound` until the stop signal of `tasks`.
///
/// After the stop signal the listeners take no new connection, and the
/// function removes the file of each Unix socket. It then waits for the open
/// requests, for `drain` at most, and returns. A stream that is still open
/// ends there.
///
/// [`Drained::TimedOut`] holds the count of the connections that were open
/// at the limit. The function ends each one: the next read and the next
/// write of such a connection fail. It does not wait for the task of the
/// connection. The handler of such a connection continues when the router
/// has the edge layer, and `Tasks::drain` waits for it.
///
/// A listener that stops with an error stops the other listeners too. The
/// function gives the open requests the same `drain` and then returns the
/// error.
///
/// The function takes the stop signal of `tasks` and tracks no task of its
/// own. A caller that drops the future closes each listener and ends each
/// connection.
///
/// A listener refuses three kinds of request before `app` gets them, as the
/// Python server does. The answer is status 400 with the text
/// `Invalid HTTP request received.`, and the connection closes
/// (`uvicorn/protocols/http/h11_impl.py:182-185` and `:304-320`):
///
/// - A request of HTTP/1.1 with no `Host` header.
/// - A request with more than one `Host` header.
/// - A target with a byte past `0x7E`, for example a letter that is not
///   ASCII.
///
/// The function removes the file of a Unix socket only while the path has
/// the inode that [`bind`] read. A path that another process took thus
/// stays, as `asyncio` of CPython 3.13 leaves it
/// (`asyncio/unix_events.py:474-493`). `asyncio` of CPython 3.12 removes no
/// file, and the function follows CPython 3.13. A file that the function
/// cannot remove is one `ERROR` line and no error of the stop. The file also
/// stays when the caller drops the future before the stop signal, and after
/// a process that the system killed. The next [`bind`] removes such a file.
///
/// The limit needs the timer of the runtime. In a runtime with no timer the
/// function does not wait: it writes one `ERROR` line and ends each
/// connection at the stop signal. On a thread with no runtime it starts no
/// listener and returns [`ServeError::ListenerLost`].
///
/// The Python origin is `serve` of
/// `attendance/src/attendance/__main__.py:68-84`, and `uvicorn.run` for each
/// other service.
///
/// The function differs from the server of the Python services in these
/// ways. A file of `uvicorn`, of `h11` or of `asyncio` below is the file of
/// the version that `uv.lock` and CPython 3.13 give.
///
/// - `uvicorn` closes a connection that sends no request for 5 seconds
///   (`uvicorn/config.py:229`). A listener here sets no timer: such a
///   connection stays open until its client leaves or the service stops.
/// - `uvicorn` waits with no limit for an open request and for an open
///   stream at a stop (`uvicorn/server.py:288-291`). The function ends each
///   open connection at `drain` and returns.
/// - `asyncio` writes an accept error with no known cause to the log and
///   continues at once (`asyncio/selector_events.py:209`). A listener here
///   waits one second after such an error, as `asyncio` does for `EMFILE`.
///   It stops for `EBADF`, `EINVAL`, `ENOTSOCK` and `EOPNOTSUPP`: the
///   descriptor is then no socket that listens.
/// - `uvicorn` adds the header `server: uvicorn` to each answer
///   (`uvicorn/config.py:221`). A listener here adds no such header.
/// - `uvicorn` answers bytes that are no HTTP request with status 400 and
///   the text `Invalid HTTP request received.`
///   (`uvicorn/protocols/http/h11_impl.py:182-185`). A listener here answers
///   status 400 with no body.
/// - `uvicorn` refuses the head of a request past 16 KiB with status 400
///   (`h11/_connection.py:69`). A listener here has the limits of `hyper`: a
///   head of 100 headers and of 417,792 bytes. It answers status 431 past a
///   limit.
/// - `uvicorn` sends the text of its status 400 also to a client that sent
///   `HEAD`. A listener here sends the headers of that answer and no body.
/// - `h11` takes each byte from `0x21` to `0x7E` in a target
///   (`h11/_abnf.py:54` and `:83`). A listener here has the grammar of the
///   crate `http` (`src/uri/path.rs:418-457` of `http` 1.5.0). It answers
///   status 400 with no body to `<`, `>` or a grave accent in a path, and to
///   `"`, `<` or `>` in a query.
/// - The Python framework takes a `#` in a target, and the text after it, as
///   a part of the path or of the query. A listener here drops the `#` and
///   the text after it.
/// - `h11` reads a header line that continues on the next line, and each
///   version text of the form `HTTP/1.2` (`h11/_abnf.py:84`). A listener
///   here answers status 400 with no body to such a header line, and to each
///   version but `HTTP/1.0` and `HTTP/1.1`.
/// - `uvicorn` gives an app a header value with a control byte of ASCII, for
///   each such byte but NUL and white space (`h11/_abnf.py:55-56`). A
///   listener here takes only the tab: it answers status 400 to each other
///   control byte, and no handler runs. The vector `byte-1c-at-the-end` of
///   the surfaces `runtime.bearer.*` holds such a header.
///
/// # Errors
///
/// [`ServeError`] when a listener stops before the stop signal.
pub async fn serve(
    bound: Vec<Bound>,
    app: Router,
    tasks: &Tasks,
    drain: Duration,
) -> Result<Drained, ServeError> {
    serve_through(Host, bound, app, tasks, drain).await
}

/// [`serve`] on the given [`Steps`].
async fn serve_through<S>(
    steps: S,
    bound: Vec<Bound>,
    app: Router,
    tasks: &Tasks,
    drain: Duration,
) -> Result<Drained, ServeError>
where
    S: Steps + Send + 'static,
{
    let closing = CancellationToken::new();
    let cut = CancellationToken::new();
    // Each way out of this function closes the listeners and ends the open
    // connections, also when the caller drops the future.
    let _close_at_end = closing.clone().drop_guard();
    let _cut_at_end = cut.clone().drop_guard();
    let open = TaskTracker::new();
    let app = Router::new().fallback(move |request: Request| checked(app.clone(), request));
    let mut listeners = start(bound, &app, &closing, &cut, &open)?;

    let failure = tokio::select! {
        () = tasks.shutdown().cancelled() => None,
        error = listeners.failure() => Some(error),
    };
    closing.cancel();
    open.close();
    remove_files(steps, std::mem::take(&mut listeners.files)).await;

    let waited = match limited(drain, listeners.all_closed(&open)) {
        Some(wait) => wait.await.ok(),
        None => {
            let why = match Handle::try_current() {
                Ok(_) => NO_TIMER,
                Err(_) => NO_RUNTIME,
            };
            crate::error!(
                LOG_TARGET,
                "the listeners did not wait for the open requests: {why}"
            );

            None
        }
    };
    let (drained, late) = match waited {
        Some(late) => (Drained::Clean, late),
        None => {
            let left = open.len();
            cut.cancel();
            listeners.tasks.shutdown().await;

            match left {
                // The last connection closed between the limit and the
                // count.
                0 => (Drained::Clean, None),
                left => (Drained::TimedOut { left }, None),
            }
        }
    };

    match failure.or(late) {
        Some(error) => Err(error),
        None => Ok(drained),
    }
}

#[cfg(test)]
pub(super) mod tests {
    use std::collections::VecDeque;
    use std::convert::Infallible;
    use std::net::Ipv4Addr;
    use std::os::unix::net::{UnixListener as StdUnixListener, UnixStream as StdUnixStream};
    use std::path::PathBuf;
    use std::process::{Command, Output, Stdio};
    use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
    use std::sync::{Arc, Mutex};
    use std::thread;
    use std::time::Instant;

    use axum::body::Body;
    use axum::response::Response;
    use axum::routing::get;
    use bytes::Bytes;
    use creche_testkit::root::TempRoot;
    use creche_testkit::stub::RawHttp;
    use hyper::body::Frame;
    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    use tokio::runtime::{Builder, Runtime};
    use tokio::sync::Notify;
    use tokio::task::JoinHandle;

    use crate::tasks::{ShutdownTrigger, locked, shutdown_pair};

    use super::*;

    // The test module of the edge layer uses each item below that has
    // `pub(in crate::http)`. This module is their one home.

    /// The longest time that a test waits for a step. The time is real, and
    /// a host with much load is slow.
    pub(in crate::http) const LIMIT: Duration = Duration::from_secs(60);

    /// A drain limit that no test of a clean stop reaches.
    pub(in crate::http) const LONG_DRAIN: Duration = Duration::from_secs(45);

    /// A drain limit that passes, in a test with one request open.
    pub(in crate::http) const SHORT_DRAIN: Duration = Duration::from_millis(300);

    /// One second more than the time after which `uvicorn` closes a
    /// connection that sends no request.
    const PAST_THE_PYTHON_LIMIT: Duration = Duration::from_secs(6);

    /// How many ports a test tries before it gives up.
    const PORT_TRIES: usize = 8;

    /// One whole request. The header `Connection: close` makes the server
    /// close the connection after the answer, so `RawHttp` returns.
    const GET_HEALTH: &[u8] = b"GET /healthz HTTP/1.1\r\nHost: test\r\nConnection: close\r\n\r\n";

    /// The same request with no `Connection` header: the server keeps the
    /// connection open after the answer.
    const GET_HEALTH_KEPT: &[u8] = b"GET /healthz HTTP/1.1\r\nHost: test\r\n\r\n";

    /// The answer of the route `/healthz`.
    const HEALTH_BODY: &str = "ok";

    /// The first bytes of a stream that stays open.
    const FIRST_CHUNK: &[u8] = b"first\n";

    /// The address of a listener with scripted errors.
    const SCRIPTED: &str = "scripted";

    /// The variable that selects the scenario of
    /// [`the_child_runs_one_scenario`].
    const CHILD_VARIABLE: &str = "CRECHE_RUNTIME_HTTP_SERVER_TEST_CHILD";

    /// The name of the child test, as the test program takes it.
    const CHILD_TEST: &str = "http::server::tests::the_child_runs_one_scenario";

    /// The target of the panic hook of the child.
    pub(in crate::http) const CHILD_PROGRAM: &str = "child";

    /// The line of a stop in a runtime with no timer.
    const NO_TIMER_LINE: &str = "ERROR the listeners did not wait for the open requests: the \
                                 runtime of this thread has no timer";

    /// A scenario that runs in a child: this test program again, with one
    /// test and one thread.
    ///
    /// A scenario that writes to the log runs there, so its lines do not go
    /// to the output of this test program, and the test can read them.
    ///
    /// A scenario that needs a listener to be closed at once runs there too.
    /// macOS makes a socket and sets its close-on-exec flag in two steps. A
    /// child that another test starts between the two steps holds a copy of
    /// the socket until it ends. In this test program, such a copy keeps a
    /// listener open after `serve` closed it. The child starts no program.
    struct Scenario {
        /// The value of [`CHILD_VARIABLE`] that selects the scenario.
        name: &'static str,
        /// What the child does.
        run: fn(),
        /// Checks the lines of this module: the level and the message of
        /// each one.
        check: fn(&[String]),
    }

    const SCENARIOS: [Scenario; 11] = [
        Scenario {
            name: "dir-mode",
            run: the_mode_of_the_directory_cannot_change,
            check: |lines| {
                assert_eq!(lines.len(), 1, "{lines:?}");
                assert!(
                    lines[0].starts_with("WARNING could not set mode on /"),
                    "{lines:?}"
                );
                assert!(lines[0].ends_with("/sock: read-only"), "{lines:?}");
            },
        },
        Scenario {
            name: "setgid-lost",
            run: the_directory_loses_its_setgid_bit,
            check: |lines| {
                assert_eq!(lines.len(), 1, "{lines:?}");
                assert!(lines[0].starts_with("WARNING /"), "{lines:?}");
                assert!(lines[0].contains("/sock lost its setgid bit"), "{lines:?}");
                assert!(lines[0].contains("Run: chmod g+s /"), "{lines:?}");
            },
        },
        Scenario {
            name: "socket-goes",
            run: the_socket_file_is_gone_after_serve_and_the_next_bind_takes_the_path,
            check: no_line,
        },
        Scenario {
            name: "socket-kept",
            run: a_socket_file_that_serve_cannot_remove_stays,
            check: |lines| {
                // One line for each of the two calls that fail.
                assert_eq!(lines.len(), 2, "{lines:?}");
                for line in lines {
                    assert!(
                        line.starts_with("ERROR could not remove the socket file /"),
                        "{lines:?}"
                    );
                    assert!(line.ends_with("/s.sock: read-only"), "{lines:?}");
                }
            },
        },
        Scenario {
            name: "restart",
            run: a_restart_binds_the_same_port_at_once,
            check: no_line,
        },
        Scenario {
            name: "listener-error",
            run: an_accept_error_of_a_listener_stops_each_listener,
            check: no_line,
        },
        Scenario {
            name: "accept-pause",
            run: an_accept_fails_two_times,
            check: |lines| {
                assert_eq!(
                    lines,
                    [
                        "ERROR the listener on scripted did not accept a connection: Too many \
                         open files. It tries again in 1 s"
                    ]
                );
            },
        },
        Scenario {
            name: "accept-no-timer",
            run: an_accept_fails_with_no_timer,
            // The listener writes no line of a next try. The one line is
            // the line of the stop that follows.
            check: |lines| assert_eq!(lines, [NO_TIMER_LINE]),
        },
        Scenario {
            name: "no-timer",
            run: a_runtime_has_no_timer,
            check: |lines| assert_eq!(lines, [NO_TIMER_LINE]),
        },
        Scenario {
            name: "no-io-driver",
            run: a_runtime_has_no_io_driver,
            check: no_line,
        },
        Scenario {
            name: "no-runtime",
            run: no_runtime_runs,
            check: |lines| {
                assert_eq!(
                    lines,
                    [
                        "ERROR the listener on scripted did not start: no runtime runs on this thread"
                    ]
                );
            },
        },
    ];

    /// The check of a scenario that writes no line of this module.
    fn no_line(lines: &[String]) {
        assert!(lines.is_empty(), "{lines:?}");
    }

    pub(in crate::http) fn runtime() -> Runtime {
        Builder::new_current_thread().enable_all().build().unwrap()
    }

    /// A runtime with one thread, and a runtime with two worker threads. A
    /// test of a stop or of a client that left runs on the two: a service
    /// selects its own count of threads.
    pub(in crate::http) fn each_runtime() -> [Runtime; 2] {
        let workers = Builder::new_multi_thread()
            .worker_threads(2)
            .enable_all()
            .build()
            .unwrap();

        [runtime(), workers]
    }

    /// Waits for `step`, for [`LIMIT`] at most.
    pub(in crate::http) async fn within<F: Future>(step: F) -> F::Output {
        tokio::time::timeout(LIMIT, step)
            .await
            .expect("the step did not end inside the limit")
    }

    fn mode_of(path: &Path) -> u32 {
        Host.mode_of(path).unwrap()
    }

    /// The path of a socket in the directory `sock` of `root`. The directory
    /// does not exist yet.
    fn socket_in(root: &TempRoot) -> PathBuf {
        root.path().join("sock").join("s.sock")
    }

    fn unix(path: &Path, dir: SocketDir) -> Listen {
        Listen::Unix {
            socket: path.to_str().unwrap().parse().unwrap(),
            dir,
        }
    }

    fn loopback(port: u16) -> Listen {
        Listen::Tcp(format!("127.0.0.1:{port}").parse().unwrap())
    }

    /// A port that no socket holds at this moment.
    fn free_port() -> u16 {
        std::net::TcpListener::bind((Ipv4Addr::LOCALHOST, 0))
            .unwrap()
            .local_addr()
            .unwrap()
            .port()
    }

    /// Binds a loopback port. Another process can take a free port before
    /// the bind, so the function tries more than one port.
    async fn bind_loopback() -> (Bound, u16) {
        for _ in 0..PORT_TRIES {
            let port = free_port();

            match bind(loopback(port)).await {
                Ok(bound) => return (bound, port),
                Err(error) if error.kind() == io::ErrorKind::AddrInUse => {}
                Err(error) => panic!("{error}"),
            }
        }

        panic!("no free port in {PORT_TRIES} tries");
    }

    fn health_app() -> Router {
        Router::new().route("/healthz", get(|| async { HEALTH_BODY }))
    }

    /// One `serve` call that runs in a task.
    struct Served {
        trigger: ShutdownTrigger,
        serving: JoinHandle<Result<Drained, ServeError>>,
    }

    impl Served {
        fn start(bound: Vec<Bound>, app: Router, drain: Duration) -> Self {
            Self::start_through(Host, bound, app, drain)
        }

        /// Starts `serve` with the steps of a test.
        fn start_through<S>(steps: S, bound: Vec<Bound>, app: Router, drain: Duration) -> Self
        where
            S: Steps + Send + 'static,
        {
            let (trigger, shutdown) = shutdown_pair();
            let tasks = Tasks::new(shutdown);
            let serving =
                tokio::spawn(async move { serve_through(steps, bound, app, &tasks, drain).await });

            Self { trigger, serving }
        }

        /// Triggers the stop signal and gives the value of `serve`.
        async fn stop(self) -> Result<Drained, ServeError> {
            self.trigger.trigger();

            within(self.serving).await.unwrap()
        }
    }

    /// Reads the answer of the route `/healthz` from a connection that the
    /// server keeps open after it.
    async fn read_kept_answer(stream: &mut tokio::net::UnixStream) -> Vec<u8> {
        let mut answer = Vec::new();

        while !answer.ends_with(HEALTH_BODY.as_bytes()) {
            let mut chunk = [0_u8; 512];
            let read = within(stream.read(&mut chunk)).await.unwrap();
            assert_ne!(read, 0, "the server closed a connection that it keeps");
            answer.extend_from_slice(&chunk[..read]);
        }

        answer
    }

    fn status_of(answer: &[u8]) -> u16 {
        let text = String::from_utf8_lossy(answer);
        let line = text.lines().next().unwrap_or_default().to_owned();

        line.split(' ')
            .nth(1)
            .and_then(|code| code.parse().ok())
            .unwrap_or_else(|| panic!("no status line: {text:?}"))
    }

    fn body_of(answer: &[u8]) -> Vec<u8> {
        let end = answer
            .windows(4)
            .position(|window| window == b"\r\n\r\n")
            .unwrap_or_else(|| panic!("no end of the head: {:?}", String::from_utf8_lossy(answer)));

        answer[end + 4..].to_vec()
    }

    fn header_of(answer: &[u8], name: &str) -> Option<String> {
        let text = String::from_utf8_lossy(answer);
        let head = text.split("\r\n\r\n").next().unwrap_or_default();

        head.lines().skip(1).find_map(|line| {
            let (key, value) = line.split_once(':')?;

            key.eq_ignore_ascii_case(name)
                .then(|| value.trim().to_owned())
        })
    }

    fn os_error(code: Errno) -> io::Error {
        io::Error::from_raw_os_error(code.raw_os_error())
    }

    /// An address whose accept gives each error of `errors`, and then waits.
    fn scripted(errors: Vec<io::Error>) -> Bound {
        Bound {
            address: String::from(SCRIPTED),
            port: None,
            sockets: vec![Socket::Scripted(VecDeque::from(errors))],
            file: None,
        }
    }

    /// One call of [`Steps`].
    #[derive(Debug, Clone, Copy, PartialEq, Eq)]
    enum Call {
        MakeDirs,
        ModeOf,
        SetDirMode,
        SetSocketMode,
        IsSocket,
        InodeOf,
        RemoveFile,
    }

    /// The steps of the host, with the changes that one test names.
    #[derive(Clone, Default)]
    struct Staged {
        /// The call that fails, and the kind of its error.
        fails: Option<(Call, io::ErrorKind)>,
        /// The mode that `mode_of` gives after the mode change of the
        /// directory, in place of the mode on the disk.
        mode_after: Option<u32>,
        /// Whether the mode of the directory was set.
        dir_set: Arc<AtomicBool>,
        /// Each `set_mode` call.
        modes: Arc<Mutex<Vec<(PathBuf, u32)>>>,
        /// The error of a connect to the socket, just before its mode
        /// change. `None` for a connect that the socket took.
        connects: Arc<Mutex<Vec<Option<io::ErrorKind>>>>,
    }

    impl Staged {
        fn failing(call: Call, kind: io::ErrorKind) -> Self {
            Self {
                fails: Some((call, kind)),
                ..Self::default()
            }
        }

        fn check(&self, call: Call) -> io::Result<()> {
            match self.fails {
                Some((failing, kind)) if failing == call => Err(io::Error::new(kind, "read-only")),
                _ => Ok(()),
            }
        }
    }

    impl Steps for Staged {
        fn make_dirs(&self, dir: &Path) -> io::Result<()> {
            self.check(Call::MakeDirs)?;

            Host.make_dirs(dir)
        }

        fn mode_of(&self, path: &Path) -> io::Result<u32> {
            self.check(Call::ModeOf)?;

            match self.mode_after {
                Some(mode) if self.dir_set.load(Ordering::SeqCst) => Ok(mode),
                _ => Host.mode_of(path),
            }
        }

        fn set_mode(&self, path: &Path, mode: u32) -> io::Result<()> {
            locked(&self.modes).push((path.to_owned(), mode));

            if mode == SOCKET_DIR_MODE {
                self.check(Call::SetDirMode)?;
                self.dir_set.store(true, Ordering::SeqCst);
            } else {
                let refused = StdUnixStream::connect(path).err().map(|error| error.kind());
                locked(&self.connects).push(refused);
                self.check(Call::SetSocketMode)?;
            }

            Host.set_mode(path, mode)
        }

        fn is_socket(&self, path: &Path) -> io::Result<bool> {
            self.check(Call::IsSocket)?;

            Host.is_socket(path)
        }

        fn inode_of(&self, path: &Path) -> io::Result<Option<u64>> {
            self.check(Call::InodeOf)?;

            Host.inode_of(path)
        }

        fn remove_file(&self, path: &Path) -> io::Result<()> {
            self.check(Call::RemoveFile)?;

            Host.remove_file(path)
        }
    }

    async fn bind_staged(steps: Staged, path: &Path, dir: SocketDir) -> Result<Bound, BindError> {
        bind_unix(steps, path.to_str().unwrap().parse().unwrap(), dir).await
    }

    /// The body of a stream that gives one chunk and then stays open.
    struct OpenStream {
        started: Arc<Notify>,
        sent: bool,
    }

    impl hyper::body::Body for OpenStream {
        type Data = Bytes;
        type Error = Infallible;

        fn poll_frame(
            mut self: Pin<&mut Self>,
            _context: &mut Context<'_>,
        ) -> Poll<Option<Result<Frame<Bytes>, Infallible>>> {
            if self.sent {
                // Nothing wakes this task again: the stream stays open.
                return Poll::Pending;
            }
            self.sent = true;
            self.started.notify_one();

            Poll::Ready(Some(Ok(Frame::data(Bytes::from_static(FIRST_CHUNK)))))
        }
    }

    /// The body of a stream that gives one chunk and ends at the stop
    /// signal, as the rules for a service say.
    struct UntilStop {
        started: Arc<Notify>,
        stop: Pin<Box<dyn Future<Output = ()> + Send>>,
        sent: bool,
    }

    impl hyper::body::Body for UntilStop {
        type Data = Bytes;
        type Error = Infallible;

        fn poll_frame(
            mut self: Pin<&mut Self>,
            context: &mut Context<'_>,
        ) -> Poll<Option<Result<Frame<Bytes>, Infallible>>> {
            if !self.sent {
                self.sent = true;
                self.started.notify_one();

                return Poll::Ready(Some(Ok(Frame::data(Bytes::from_static(FIRST_CHUNK)))));
            }

            self.stop.as_mut().poll(context).map(|()| None)
        }
    }

    /// An app whose route `/stream` gives an [`OpenStream`].
    fn open_stream_app(started: &Arc<Notify>) -> Router {
        let started = Arc::clone(started);

        Router::new().route(
            "/stream",
            get(move || {
                let started = Arc::clone(&started);

                async move {
                    Response::new(Body::new(OpenStream {
                        started,
                        sent: false,
                    }))
                }
            }),
        )
    }

    const GET_STREAM: &[u8] = b"GET /stream HTTP/1.1\r\nHost: test\r\n\r\n";

    /// Runs one scenario in a child: this test program again, with only the
    /// test `child_test`. The variable `variable` gives the child the name of
    /// the scenario. The child must end inside [`LIMIT`].
    pub(in crate::http) fn run_child(child_test: &str, variable: &str, scenario: &str) -> Output {
        let mut child = Command::new(std::env::current_exe().unwrap())
            .args([child_test, "--exact", "--nocapture", "--test-threads=1"])
            .env(variable, scenario)
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .unwrap();
        let deadline = Instant::now() + LIMIT;

        // A scenario writes a few lines, so a pipe never fills.
        while child.try_wait().unwrap().is_none() {
            if Instant::now() > deadline {
                child.kill().unwrap();
                child.wait().unwrap();

                panic!("the scenario {scenario} did not end");
            }

            thread::sleep(Duration::from_millis(10));
        }

        child.wait_with_output().unwrap()
    }

    #[test]
    fn a_bind_error_names_the_address_the_step_and_the_answer_of_the_system() {
        let error = BindError {
            step: BindStep::Bind,
            address: String::from("192.0.2.10:8090"),
            kind: io::ErrorKind::AddrInUse,
            os_text: String::from("Address already in use"),
        };

        assert_eq!(
            error.to_string(),
            "cannot listen on 192.0.2.10:8090: the step \"bind\" failed: Address already in use"
        );
    }

    #[test]
    fn each_bind_step_has_its_own_text() {
        let steps = [
            BindStep::PrepareDir,
            BindStep::RemoveStale,
            BindStep::Bind,
            BindStep::SetMode,
        ];
        let mut texts: Vec<String> = steps.iter().map(ToString::to_string).collect();
        texts.sort();
        texts.dedup();

        assert_eq!(texts.len(), steps.len());
    }

    #[test]
    fn a_serve_error_names_the_listener() {
        let accept = ServeError::Accept {
            address: String::from("192.0.2.10:8090"),
            kind: io::ErrorKind::Other,
            os_text: String::from("Too many open files"),
        };
        let lost = ServeError::ListenerLost {
            address: String::from("/run/creche/sessiond.sock"),
        };

        assert_eq!(
            accept.to_string(),
            "the listener on 192.0.2.10:8090 stopped: Too many open files"
        );
        assert_eq!(
            lost.to_string(),
            "the task of the listener on /run/creche/sessiond.sock was lost"
        );
    }

    #[test]
    fn each_future_of_the_module_goes_to_another_thread() {
        fn sendable<F: Future + Send>(future: F) {
            drop(future);
        }

        let (_trigger, shutdown) = shutdown_pair();
        let tasks = Tasks::new(shutdown);

        sendable(bind(loopback(8340)));
        sendable(serve(Vec::new(), Router::new(), &tasks, LONG_DRAIN));
    }

    /// The port of `test_serve_binds_the_socket_at_0660_under_a_setgid_dir`
    /// (`attendance/tests/test_main.py:93-132`).
    #[test]
    fn bind_gives_the_socket_mode_0660_under_a_setgid_directory() {
        runtime().block_on(async {
            let root = TempRoot::new().unwrap();
            let path = socket_in(&root);

            let bound = bind(unix(&path, SocketDir::PrepareSetgid)).await.unwrap();

            // No accept ran yet: `serve` did not start.
            assert_eq!(mode_of(path.parent().unwrap()), 0o2750);
            assert_eq!(mode_of(&path), 0o660);
            assert_eq!(bound.describe(), path.to_str().unwrap());
            assert_eq!(bound.local_port(), None);

            let served = Served::start(vec![bound], health_app(), LONG_DRAIN);
            let answer = within(RawHttp::unix(&path, GET_HEALTH)).await.unwrap();

            assert_eq!(status_of(&answer), 200);
            assert_eq!(body_of(&answer), HEALTH_BODY.as_bytes());
            assert_eq!(mode_of(&path), 0o660);
            assert_eq!(served.stop().await, Ok(Drained::Clean));
        });
    }

    #[test]
    fn no_client_connects_before_the_socket_has_its_mode() {
        runtime().block_on(async {
            let root = TempRoot::new().unwrap();
            let path = socket_in(&root);
            let steps = Staged::default();

            let bound = bind_staged(steps.clone(), &path, SocketDir::PrepareSetgid)
                .await
                .unwrap();

            assert_eq!(
                *locked(&steps.connects),
                [Some(io::ErrorKind::ConnectionRefused)]
            );
            assert_eq!(
                *locked(&steps.modes),
                [
                    (path.parent().unwrap().to_owned(), 0o2750),
                    (path.clone(), 0o660)
                ]
            );
            // After the bind the socket listens, and a client can connect.
            StdUnixStream::connect(&path).unwrap();
            drop(bound);
        });
    }

    #[test]
    fn a_directory_with_the_right_mode_gets_no_mode_change() {
        runtime().block_on(async {
            let root = TempRoot::new().unwrap();
            let path = socket_in(&root);
            let dir = path.parent().unwrap();
            fs::create_dir(dir).unwrap();
            fs::set_permissions(dir, fs::Permissions::from_mode(SOCKET_DIR_MODE)).unwrap();
            let steps = Staged::default();

            bind_staged(steps.clone(), &path, SocketDir::PrepareSetgid)
                .await
                .unwrap();

            assert_eq!(*locked(&steps.modes), [(path.clone(), 0o660)]);
            assert_eq!(mode_of(dir), 0o2750);
        });
    }

    #[test]
    fn the_socket_file_of_an_earlier_process_is_removed() {
        runtime().block_on(async {
            let root = TempRoot::new().unwrap();
            let path = socket_in(&root);
            fs::create_dir(path.parent().unwrap()).unwrap();
            // The listener of the standard library leaves its file, as a
            // killed process does.
            drop(StdUnixListener::bind(&path).unwrap());
            assert!(fs::metadata(&path).unwrap().file_type().is_socket());

            let bound = bind(unix(&path, SocketDir::PrepareSetgid)).await.unwrap();
            let served = Served::start(vec![bound], health_app(), LONG_DRAIN);
            let answer = within(RawHttp::unix(&path, GET_HEALTH)).await.unwrap();

            assert_eq!(status_of(&answer), 200);
            assert_eq!(served.stop().await, Ok(Drained::Clean));
        });
    }

    #[test]
    fn a_regular_file_at_the_path_is_not_removed() {
        runtime().block_on(async {
            let root = TempRoot::new().unwrap();
            let path = socket_in(&root);
            fs::create_dir(path.parent().unwrap()).unwrap();
            fs::write(&path, b"not a socket").unwrap();

            let error = bind(unix(&path, SocketDir::PrepareSetgid))
                .await
                .unwrap_err();

            assert_eq!(error.step(), BindStep::Bind);
            assert_eq!(error.kind(), io::ErrorKind::AddrInUse);
            assert_eq!(error.address(), path.to_str().unwrap());
            assert_eq!(fs::read(&path).unwrap(), b"not a socket");
        });
    }

    #[test]
    fn a_port_that_another_listener_holds_is_a_bind_error() {
        runtime().block_on(async {
            let holder = std::net::TcpListener::bind((Ipv4Addr::LOCALHOST, 0)).unwrap();
            let port = holder.local_addr().unwrap().port();

            let error = bind(loopback(port)).await.unwrap_err();

            assert_eq!(error.step(), BindStep::Bind);
            assert_eq!(error.kind(), io::ErrorKind::AddrInUse);
            assert_eq!(error.address(), format!("127.0.0.1:{port}"));
        });
    }

    #[test]
    fn an_address_that_the_host_does_not_have_is_a_bind_error() {
        runtime().block_on(async {
            let port = free_port();
            let listen = Listen::Tcp(format!("192.0.2.10:{port}").parse().unwrap());

            let error = bind(listen).await.unwrap_err();

            assert_eq!(error.step(), BindStep::Bind);
            assert_eq!(error.kind(), io::ErrorKind::AddrNotAvailable);
            assert_eq!(error.address(), format!("192.0.2.10:{port}"));
        });
    }

    /// The addresses of a host name can hold one that this host does not
    /// have, for example the IPv6 address of `localhost` on a host with no
    /// IPv6.
    #[test]
    fn an_address_that_the_host_does_not_have_gets_no_listener() {
        runtime().block_on(async {
            let mut bound = None;
            for _ in 0..PORT_TRIES {
                let port = free_port();
                let absent: SocketAddr = format!("192.0.2.10:{port}").parse().unwrap();
                let present: SocketAddr = format!("127.0.0.1:{port}").parse().unwrap();

                match listeners_on([absent, present, present]) {
                    Ok(listeners) => {
                        bound = Some((listeners, present));
                        break;
                    }
                    Err(error) if error.kind() == io::ErrorKind::AddrInUse => {}
                    Err(error) => panic!("{error}"),
                }
            }
            let (listeners, present) = bound.unwrap();

            // One listener: the host does not have the first address, and
            // the third address is the second one again.
            assert_eq!(listeners.len(), 1);
            assert_eq!(listeners[0].local_addr().unwrap(), present);

            let absent: SocketAddr = "192.0.2.10:8340".parse().unwrap();
            let none_left = listeners_on([absent]).unwrap_err();
            let no_address = listeners_on([]).unwrap_err();

            assert_eq!(none_left.kind(), io::ErrorKind::AddrNotAvailable);
            assert_ne!(none_left.to_string(), NO_ADDRESS);
            assert_eq!(no_address.kind(), io::ErrorKind::AddrNotAvailable);
            assert_eq!(no_address.to_string(), NO_ADDRESS);
        });
    }

    #[test]
    fn an_address_that_another_listener_holds_stops_the_bind_of_its_host() {
        runtime().block_on(async {
            let holder = std::net::TcpListener::bind((Ipv4Addr::LOCALHOST, 0)).unwrap();
            let held = holder.local_addr().unwrap();
            let absent: SocketAddr = "192.0.2.10:8340".parse().unwrap();
            let free: SocketAddr = format!("127.0.0.1:{}", free_port()).parse().unwrap();

            // The bind stops at the second address. It does not continue
            // with the third one, which it can bind.
            let error = listeners_on([absent, held, free]).unwrap_err();

            assert_eq!(error.kind(), io::ErrorKind::AddrInUse);
        });
    }

    #[test]
    fn an_ipv4_address_in_the_ipv6_form_is_a_bind_error() {
        runtime().block_on(async {
            let port = free_port();
            let listen = Listen::Tcp(format!("[::ffff:127.0.0.1]:{port}").parse().unwrap());

            let error = bind(listen).await.unwrap_err();

            assert_eq!(error.step(), BindStep::Bind);
            assert_eq!(error.kind(), io::ErrorKind::AddrNotAvailable);
            assert_eq!(error.os_text(), MAPPED_ADDRESS);
            assert_eq!(error.address(), format!("[::ffff:127.0.0.1]:{port}"));
        });
    }

    #[test]
    fn an_address_of_each_interface_gets_no_socket() {
        let port = free_port();

        for text in ["0.0.0.0", "[::]", "[::ffff:0.0.0.0]"] {
            let place: SocketAddr = format!("{text}:{port}").parse().unwrap();

            assert!(is_each_interface(&place), "{text}");
            let Err(Unbound::Stop(error)) = bound_tcp(place) else {
                panic!("{text} got a socket, or the bind continues");
            };
            assert_eq!(error.kind(), io::ErrorKind::InvalidInput, "{text}");
            assert_eq!(error.to_string(), EACH_INTERFACE, "{text}");
        }
        for text in ["127.0.0.1", "192.0.2.10", "[::1]", "[::ffff:192.0.2.10]"] {
            let place: SocketAddr = format!("{text}:{port}").parse().unwrap();

            assert!(!is_each_interface(&place), "{text}");
        }
    }

    #[test]
    fn a_host_name_binds_each_of_its_addresses() {
        runtime().block_on(async {
            let mut bound = None;
            for _ in 0..PORT_TRIES {
                let port = free_port();
                let listen = Listen::Tcp(format!("localhost:{port}").parse().unwrap());

                match bind(listen).await {
                    Ok(listeners) => {
                        bound = Some((listeners, port));
                        break;
                    }
                    Err(error) if error.kind() == io::ErrorKind::AddrInUse => {}
                    Err(error) => panic!("{error}"),
                }
            }
            let (bound, port) = bound.unwrap();

            assert_eq!(bound.describe(), format!("localhost:{port}"));
            assert_eq!(bound.local_port(), Some(port));
            assert!(!bound.sockets.is_empty());

            let served = Served::start(vec![bound], health_app(), LONG_DRAIN);
            let answer = within(RawHttp::tcp(port, GET_HEALTH)).await.unwrap();

            assert_eq!(status_of(&answer), 200);
            assert_eq!(served.stop().await, Ok(Drained::Clean));
        });
    }

    /// The form of `attendance` with a LAN address: one app on a socket and
    /// on a port (`attendance/src/attendance/__main__.py:97-115`).
    #[test]
    fn one_app_answers_on_a_socket_and_on_a_port() {
        for runtime in each_runtime() {
            runtime.block_on(async {
                let root = TempRoot::new().unwrap();
                let path = socket_in(&root);
                let on_socket = bind(unix(&path, SocketDir::PrepareSetgid)).await.unwrap();
                let (on_port, port) = bind_loopback().await;
                let served = Served::start(vec![on_socket, on_port], health_app(), LONG_DRAIN);

                let by_socket = within(RawHttp::unix(&path, GET_HEALTH)).await.unwrap();
                let by_port = within(RawHttp::tcp(port, GET_HEALTH)).await.unwrap();

                assert_eq!(status_of(&by_socket), 200);
                assert_eq!(body_of(&by_socket), HEALTH_BODY.as_bytes());
                assert_eq!(status_of(&by_port), 200);
                assert_eq!(body_of(&by_port), HEALTH_BODY.as_bytes());
                assert_eq!(served.stop().await, Ok(Drained::Clean));
            });
        }
    }

    #[test]
    fn the_text_of_a_tcp_address_has_brackets_for_ipv6() {
        assert_eq!(tcp_text("192.0.2.10", 8340), "192.0.2.10:8340");
        assert_eq!(tcp_text("localhost", 8340), "localhost:8340");
        assert_eq!(tcp_text("::1", 8340), "[::1]:8340");
        assert_eq!(
            tcp_text("::1", 8340),
            "[::1]:8340".parse::<BindAddress>().unwrap().to_string()
        );
    }

    #[test]
    fn an_accepted_tcp_connection_sends_a_small_write_at_once() {
        runtime().block_on(async {
            let (mut bound, port) = bind_loopback().await;
            let client = tokio::net::TcpStream::connect((Ipv4Addr::LOCALHOST, port))
                .await
                .unwrap();

            let accepted = within(bound.sockets[0].accept()).await.unwrap();

            let Stream::Tcp(stream) = accepted else {
                panic!("a TCP listener gave another stream");
            };
            assert!(stream.nodelay().unwrap());
            drop(client);
        });
    }

    #[test]
    fn the_directory_must_exist_when_bind_leaves_it() {
        runtime().block_on(async {
            let root = TempRoot::new().unwrap();
            let path = socket_in(&root);

            let error = bind(unix(&path, SocketDir::LeaveAsItIs)).await.unwrap_err();

            assert_eq!(error.step(), BindStep::Bind);
            assert_eq!(error.kind(), io::ErrorKind::NotFound);
            assert!(!path.parent().unwrap().exists());

            // The mode of a directory that exists stays as it is.
            fs::create_dir(path.parent().unwrap()).unwrap();
            let before = mode_of(path.parent().unwrap());
            let bound = bind(unix(&path, SocketDir::LeaveAsItIs)).await.unwrap();

            assert_eq!(mode_of(path.parent().unwrap()), before);
            assert_eq!(mode_of(&path), 0o660);
            drop(bound);
        });
    }

    #[test]
    fn a_socket_in_the_root_directory_is_refused_when_bind_prepares() {
        runtime().block_on(async {
            let path = Path::new("/creche-runtime-test-never.sock");

            let error = bind(unix(path, SocketDir::PrepareSetgid))
                .await
                .unwrap_err();

            assert_eq!(error.step(), BindStep::PrepareDir);
            assert_eq!(error.kind(), io::ErrorKind::InvalidInput);
            assert_eq!(error.os_text(), NO_DIRECTORY);
            assert!(!path.exists());
        });
    }

    #[test]
    fn each_step_that_fails_gives_its_own_error() {
        let table = [
            (Call::MakeDirs, BindStep::PrepareDir),
            (Call::ModeOf, BindStep::PrepareDir),
            (Call::IsSocket, BindStep::RemoveStale),
            (Call::RemoveFile, BindStep::RemoveStale),
            (Call::SetSocketMode, BindStep::SetMode),
            (Call::InodeOf, BindStep::Bind),
        ];

        runtime().block_on(async {
            for (call, step) in table {
                let root = TempRoot::new().unwrap();
                let path = socket_in(&root);
                fs::create_dir(path.parent().unwrap()).unwrap();
                // A socket file is there, so the bind calls `remove_file`.
                drop(StdUnixListener::bind(&path).unwrap());
                let steps = Staged::failing(call, io::ErrorKind::PermissionDenied);

                let error = bind_staged(steps, &path, SocketDir::PrepareSetgid)
                    .await
                    .unwrap_err();

                assert_eq!(error.step(), step, "{call:?}");
                assert_eq!(error.kind(), io::ErrorKind::PermissionDenied, "{call:?}");
                assert_eq!(error.os_text(), "read-only", "{call:?}");
                assert_eq!(error.address(), path.to_str().unwrap(), "{call:?}");
            }
        });
    }

    #[test]
    fn a_socket_whose_mode_cannot_change_never_listens() {
        runtime().block_on(async {
            let root = TempRoot::new().unwrap();
            let path = socket_in(&root);
            let steps = Staged::failing(Call::SetSocketMode, io::ErrorKind::PermissionDenied);

            let error = bind_staged(steps, &path, SocketDir::PrepareSetgid)
                .await
                .unwrap_err();

            assert_eq!(error.step(), BindStep::SetMode);
            // The file stays for the next start, and no client connects.
            assert!(fs::metadata(&path).unwrap().file_type().is_socket());
            assert_eq!(
                StdUnixStream::connect(&path).unwrap_err().kind(),
                io::ErrorKind::ConnectionRefused
            );
        });
    }

    #[test]
    fn a_socket_file_that_another_process_removed_first_is_no_error() {
        runtime().block_on(async {
            let root = TempRoot::new().unwrap();
            let path = socket_in(&root);
            fs::create_dir(path.parent().unwrap()).unwrap();
            drop(StdUnixListener::bind(&path).unwrap());
            // The bind then fails, because this test removes no file.
            let steps = Staged::failing(Call::RemoveFile, io::ErrorKind::NotFound);

            let error = bind_staged(steps, &path, SocketDir::PrepareSetgid)
                .await
                .unwrap_err();

            assert_eq!(error.step(), BindStep::Bind);
            assert_eq!(error.kind(), io::ErrorKind::AddrInUse);
        });
    }

    #[test]
    fn a_path_that_names_no_file_is_no_socket() {
        let root = TempRoot::new().unwrap();
        let file = root.path().join("file");
        fs::write(&file, b"x").unwrap();
        let link = root.path().join("link");
        std::os::unix::fs::symlink(root.path().join("absent"), &link).unwrap();
        let socket = root.path().join("s.sock");
        drop(StdUnixListener::bind(&socket).unwrap());
        let to_socket = root.path().join("to-socket");
        std::os::unix::fs::symlink(&socket, &to_socket).unwrap();

        assert!(!Host.is_socket(&root.path().join("absent")).unwrap());
        // A file is no directory, so no path goes through it.
        assert!(!Host.is_socket(&file.join("below")).unwrap());
        assert!(!Host.is_socket(&link).unwrap());
        assert!(!Host.is_socket(&file).unwrap());
        assert!(!Host.is_socket(root.path()).unwrap());
        assert!(Host.is_socket(&socket).unwrap());
        // `Path.is_socket` of Python follows a symlink, and the removal
        // takes the symlink itself.
        assert!(Host.is_socket(&to_socket).unwrap());
    }

    #[test]
    fn a_connection_with_no_request_closes_at_the_stop() {
        for runtime in each_runtime() {
            runtime.block_on(async {
                let root = TempRoot::new().unwrap();
                let path = socket_in(&root);
                let bound = bind(unix(&path, SocketDir::PrepareSetgid)).await.unwrap();
                let served = Served::start(vec![bound], health_app(), LONG_DRAIN);

                // The first client sends nothing. The second one got an answer
                // and keeps its connection.
                let mut silent = tokio::net::UnixStream::connect(&path).await.unwrap();
                let mut kept = tokio::net::UnixStream::connect(&path).await.unwrap();
                kept.write_all(GET_HEALTH_KEPT).await.unwrap();
                let answer = read_kept_answer(&mut kept).await;
                assert_eq!(status_of(&answer), 200);

                assert_eq!(served.stop().await, Ok(Drained::Clean));

                let mut rest = Vec::new();
                assert_eq!(within(silent.read_to_end(&mut rest)).await.unwrap(), 0);
                assert_eq!(within(kept.read_to_end(&mut rest)).await.unwrap(), 0);
            });
        }
    }

    /// `uvicorn` closes such a connection after 5 seconds
    /// (`uvicorn/config.py:229`). The wait of this test is real time.
    #[test]
    fn a_connection_that_sends_no_request_for_6_seconds_stays_open() {
        runtime().block_on(async {
            let root = TempRoot::new().unwrap();
            let path = socket_in(&root);
            let bound = bind(unix(&path, SocketDir::PrepareSetgid)).await.unwrap();
            let served = Served::start(vec![bound], health_app(), LONG_DRAIN);
            // The first answer shows that the server holds the connection.
            let mut kept = tokio::net::UnixStream::connect(&path).await.unwrap();
            kept.write_all(GET_HEALTH_KEPT).await.unwrap();
            assert_eq!(status_of(&read_kept_answer(&mut kept).await), 200);

            tokio::time::sleep(PAST_THE_PYTHON_LIMIT).await;

            kept.write_all(GET_HEALTH).await.unwrap();
            let mut answer = Vec::new();
            within(kept.read_to_end(&mut answer)).await.unwrap();
            assert_eq!(status_of(&answer), 200);
            assert_eq!(body_of(&answer), HEALTH_BODY.as_bytes());
            assert_eq!(served.stop().await, Ok(Drained::Clean));
        });
    }

    #[test]
    fn a_request_in_flight_gets_its_answer_at_the_stop() {
        for runtime in each_runtime() {
            runtime.block_on(async {
                let root = TempRoot::new().unwrap();
                let path = socket_in(&root);
                let entered = Arc::new(Notify::new());
                let release = Arc::new(Notify::new());
                let app = Router::new().route(
                    "/healthz",
                    get({
                        let entered = Arc::clone(&entered);
                        let release = Arc::clone(&release);

                        move || async move {
                            entered.notify_one();
                            release.notified().await;

                            HEALTH_BODY
                        }
                    }),
                );
                let bound = bind(unix(&path, SocketDir::PrepareSetgid)).await.unwrap();
                let served = Served::start(vec![bound], app, LONG_DRAIN);
                let client = tokio::spawn({
                    let path = path.clone();

                    async move { RawHttp::unix(&path, GET_HEALTH).await }
                });
                within(entered.notified()).await;

                served.trigger.trigger();
                // The listener is closed, and the open request still runs.
                tokio::task::yield_now().await;
                assert!(!served.serving.is_finished());
                release.notify_one();

                let answer = within(client).await.unwrap().unwrap();
                assert_eq!(status_of(&answer), 200);
                assert_eq!(body_of(&answer), HEALTH_BODY.as_bytes());
                assert_eq!(within(served.serving).await.unwrap(), Ok(Drained::Clean));
            });
        }
    }

    #[test]
    fn serve_returns_at_the_drain_limit_with_one_stream_open() {
        for runtime in each_runtime() {
            runtime.block_on(async {
                let root = TempRoot::new().unwrap();
                let path = socket_in(&root);
                let started = Arc::new(Notify::new());
                let bound = bind(unix(&path, SocketDir::PrepareSetgid)).await.unwrap();
                let served = Served::start(vec![bound], open_stream_app(&started), SHORT_DRAIN);
                let client = tokio::spawn({
                    let path = path.clone();

                    async move { RawHttp::unix(&path, GET_STREAM).await }
                });
                within(started.notified()).await;

                let stopped_at = Instant::now();
                let drained = served.stop().await;

                assert_eq!(drained, Ok(Drained::TimedOut { left: 1 }));
                assert!(stopped_at.elapsed() >= SHORT_DRAIN);
                // The stream ends there: the client reads the end of the bytes,
                // and its body has no last chunk.
                let answer = within(client).await.unwrap().unwrap();
                assert_eq!(status_of(&answer), 200);
                let body = body_of(&answer);
                assert!(
                    body.windows(FIRST_CHUNK.len())
                        .any(|window| window == FIRST_CHUNK),
                    "{body:?}"
                );
                assert!(!body.ends_with(b"0\r\n\r\n"), "{body:?}");
            });
        }
    }

    /// The handler gives no answer and does not read the body. The server
    /// then reads no more byte of the connection and writes none, so only
    /// the flush of the connection sees the cut.
    #[test]
    fn serve_ends_a_connection_whose_handler_reads_no_body_and_gives_no_answer() {
        /// More bytes than the server and the two sockets hold.
        const BODY: usize = 8 * 1024 * 1024;

        for runtime in each_runtime() {
            runtime.block_on(async {
                let root = TempRoot::new().unwrap();
                let path = socket_in(&root);
                let entered = Arc::new(Notify::new());
                let app = Router::new().route(
                    "/healthz",
                    get({
                        let entered = Arc::clone(&entered);

                        move |request: Request| async move {
                            entered.notify_one();
                            // The handler holds the body and never reads it.
                            future::pending::<()>().await;
                            drop(request);

                            HEALTH_BODY
                        }
                    }),
                );
                let bound = bind(unix(&path, SocketDir::PrepareSetgid)).await.unwrap();
                let served = Served::start(vec![bound], app, SHORT_DRAIN);
                let mut client = tokio::net::UnixStream::connect(&path).await.unwrap();
                let head = format!(
                    "GET /healthz HTTP/1.1\r\nHost: test\r\nContent-Length: {BODY}\r\n\r\n"
                );
                client.write_all(head.as_bytes()).await.unwrap();
                within(entered.notified()).await;
                // The client writes until no buffer takes a byte: the server
                // reads no more.
                let chunk = vec![b'a'; 64 * 1024];
                while tokio::time::timeout(SHORT_DRAIN, client.write_all(&chunk))
                    .await
                    .is_ok()
                {}

                assert_eq!(served.stop().await, Ok(Drained::TimedOut { left: 1 }));

                // The connection ended: the client reads its end, or an error.
                let mut rest = Vec::new();
                let _ = within(client.read_to_end(&mut rest)).await;
                assert!(rest.is_empty(), "{rest:?}");
            });
        }
    }

    #[test]
    fn a_stream_that_ends_at_the_stop_signal_gives_a_clean_stop() {
        for runtime in each_runtime() {
            runtime.block_on(async {
                let root = TempRoot::new().unwrap();
                let path = socket_in(&root);
                let started = Arc::new(Notify::new());
                let (trigger, shutdown) = shutdown_pair();
                let tasks = Tasks::new(shutdown.clone());
                let app = Router::new().route(
                    "/stream",
                    get({
                        let started = Arc::clone(&started);

                        move || async move {
                            Response::new(Body::new(UntilStop {
                                started,
                                stop: Box::pin(async move { shutdown.cancelled().await }),
                                sent: false,
                            }))
                        }
                    }),
                );
                let bound = bind(unix(&path, SocketDir::PrepareSetgid)).await.unwrap();
                let serving =
                    tokio::spawn(async move { serve(vec![bound], app, &tasks, LONG_DRAIN).await });
                let client = tokio::spawn({
                    let path = path.clone();

                    async move { RawHttp::unix(&path, GET_STREAM).await }
                });
                within(started.notified()).await;

                trigger.trigger();

                assert_eq!(within(serving).await.unwrap(), Ok(Drained::Clean));
                let answer = within(client).await.unwrap().unwrap();
                // The stream ended whole: the body has its last chunk.
                assert!(body_of(&answer).ends_with(b"0\r\n\r\n"));
            });
        }
    }

    #[test]
    fn a_dropped_serve_closes_the_listener_and_ends_each_connection() {
        for runtime in each_runtime() {
            runtime.block_on(async {
                let root = TempRoot::new().unwrap();
                let path = socket_in(&root);
                let started = Arc::new(Notify::new());
                let bound = bind(unix(&path, SocketDir::PrepareSetgid)).await.unwrap();
                let served = Served::start(vec![bound], open_stream_app(&started), LONG_DRAIN);
                let client = tokio::spawn({
                    let path = path.clone();

                    async move { RawHttp::unix(&path, GET_STREAM).await }
                });
                within(started.notified()).await;

                served.serving.abort();
                assert!(within(served.serving).await.unwrap_err().is_cancelled());

                let answer = within(client).await.unwrap().unwrap();
                assert_eq!(status_of(&answer), 200);
                // The task of each listener ends a moment after the drop.
                within(async {
                    while StdUnixStream::connect(&path).is_ok() {
                        tokio::time::sleep(Duration::from_millis(5)).await;
                    }
                })
                .await;
                // No stop signal came, so the file of the socket stays.
                assert!(fs::metadata(&path).unwrap().file_type().is_socket());
            });
        }
    }

    #[test]
    fn a_socket_file_that_another_process_took_stays_at_the_stop() {
        runtime().block_on(async {
            let root = TempRoot::new().unwrap();
            let path = socket_in(&root);
            let bound = bind(unix(&path, SocketDir::PrepareSetgid)).await.unwrap();
            let served = Served::start(vec![bound], health_app(), LONG_DRAIN);
            // The second socket exists beside the first one, so the two
            // files have two inodes. The rename then puts the second file
            // at the path of the first.
            let other = path.with_file_name("other.sock");
            let taker = StdUnixListener::bind(&other).unwrap();
            let taken = Host.inode_of(&other).unwrap();
            fs::rename(&other, &path).unwrap();

            assert_eq!(served.stop().await, Ok(Drained::Clean));

            assert_eq!(Host.inode_of(&path).unwrap(), taken);
            // The other process still takes a connection at the path.
            StdUnixStream::connect(&path).unwrap();
            drop(taker);
        });
    }

    #[test]
    fn a_socket_file_that_another_process_removed_is_no_error_at_the_stop() {
        runtime().block_on(async {
            let root = TempRoot::new().unwrap();
            let path = socket_in(&root);
            let bound = bind(unix(&path, SocketDir::PrepareSetgid)).await.unwrap();
            let served = Served::start(vec![bound], health_app(), LONG_DRAIN);
            fs::remove_file(&path).unwrap();

            assert_eq!(served.stop().await, Ok(Drained::Clean));
            assert!(!path.exists());
        });
    }

    #[test]
    fn an_address_that_no_serve_takes_leaves_its_socket_file() {
        runtime().block_on(async {
            let root = TempRoot::new().unwrap();
            let path = socket_in(&root);
            let bound = bind(unix(&path, SocketDir::PrepareSetgid)).await.unwrap();

            assert_eq!(
                bound.file.as_ref().map(|file| Some(file.inode)),
                Some(Host.inode_of(&path).unwrap())
            );
            drop(bound);

            // The next bind removes the file.
            assert!(fs::metadata(&path).unwrap().file_type().is_socket());
            drop(bind(unix(&path, SocketDir::PrepareSetgid)).await.unwrap());
        });
    }

    #[test]
    fn the_file_of_a_socket_is_removed_only_while_it_has_its_inode() {
        let root = TempRoot::new().unwrap();
        let path = root.path().join("s.sock");
        let holder = StdUnixListener::bind(&path).unwrap();
        let inode = Host.inode_of(&path).unwrap().unwrap();
        let file = |inode| SocketFile {
            path: path.clone(),
            inode,
        };

        remove_own(&Host, &file(inode.wrapping_add(1)));
        assert!(path.exists());

        remove_own(&Host, &file(inode));
        assert!(!path.exists());

        // The path names no file now.
        remove_own(&Host, &file(inode));
        assert_eq!(Host.inode_of(&path).unwrap(), None);
        drop(holder);
    }

    #[test]
    fn each_accept_error_has_its_class() {
        let table = [
            (Errno::CONNABORTED, AcceptFailure::Connection),
            (Errno::CONNRESET, AcceptFailure::Connection),
            (Errno::CONNREFUSED, AcceptFailure::Connection),
            (Errno::INTR, AcceptFailure::Connection),
            (Errno::MFILE, AcceptFailure::Passing),
            (Errno::NFILE, AcceptFailure::Passing),
            (Errno::NOBUFS, AcceptFailure::Passing),
            (Errno::NOMEM, AcceptFailure::Passing),
            (Errno::PROTO, AcceptFailure::Passing),
            (Errno::PERM, AcceptFailure::Passing),
            (Errno::NETDOWN, AcceptFailure::Passing),
            (Errno::BADF, AcceptFailure::Listener),
            (Errno::INVAL, AcceptFailure::Listener),
            (Errno::NOTSOCK, AcceptFailure::Listener),
            (Errno::OPNOTSUPP, AcceptFailure::Listener),
        ];

        for (code, class) in table {
            assert_eq!(AcceptFailure::of(&os_error(code)), class, "{code:?}");
        }
        // An error that the operating system did not make.
        assert_eq!(
            AcceptFailure::of(&io::Error::other("no code")),
            AcceptFailure::Passing
        );
    }

    #[test]
    fn an_answer_has_a_date_and_no_server_header() {
        runtime().block_on(async {
            let root = TempRoot::new().unwrap();
            let path = socket_in(&root);
            let bound = bind(unix(&path, SocketDir::PrepareSetgid)).await.unwrap();
            let served = Served::start(vec![bound], health_app(), LONG_DRAIN);

            let answer = within(RawHttp::unix(&path, GET_HEALTH)).await.unwrap();

            assert!(header_of(&answer, "date").is_some());
            assert_eq!(header_of(&answer, "server"), None);
            assert_eq!(header_of(&answer, "connection").as_deref(), Some("close"));
            assert_eq!(header_of(&answer, "content-length").as_deref(), Some("2"));
            assert_eq!(served.stop().await, Ok(Drained::Clean));
        });
    }

    #[test]
    fn bytes_that_are_no_request_get_status_400_and_no_body() {
        runtime().block_on(async {
            let root = TempRoot::new().unwrap();
            let path = socket_in(&root);
            let bound = bind(unix(&path, SocketDir::PrepareSetgid)).await.unwrap();
            let served = Served::start(vec![bound], health_app(), LONG_DRAIN);

            let answer = within(RawHttp::unix(&path, b"\x01\x02 not http\r\n\r\n"))
                .await
                .unwrap();

            assert_eq!(status_of(&answer), 400);
            assert!(body_of(&answer).is_empty());
            assert_eq!(served.stop().await, Ok(Drained::Clean));
        });
    }

    /// `h11` gives an app each control byte of a header value but NUL and
    /// white space (`h11/_abnf.py:55-56`). The vector `byte-1c-at-the-end`
    /// of the surfaces `runtime.bearer.*` holds the first header of the
    /// table, and three Python services take that request.
    #[test]
    fn a_header_value_with_a_control_byte_gets_status_400_and_no_handler() {
        let table: [(&[u8], u16); 6] = [
            (b"Bearer vectors-bearer-token\x1c", 400),
            (b"Bearer vectors-bearer-token\x1f", 400),
            (b"Bearer \x01vectors-bearer-token", 400),
            (b"Bearer vectors-bearer-token\x7f", 400),
            // The tab, and a byte above 127, are no control byte of ASCII.
            (b"Bearer vectors\tbearer-token", 200),
            (b"Bearer vectors-bearer-token\xa0", 200),
        ];

        runtime().block_on(async {
            let root = TempRoot::new().unwrap();
            let path = socket_in(&root);
            let calls = Arc::new(AtomicUsize::new(0));
            let app = Router::new().route(
                "/healthz",
                get({
                    let calls = Arc::clone(&calls);

                    move || async move {
                        calls.fetch_add(1, Ordering::SeqCst);

                        HEALTH_BODY
                    }
                }),
            );
            let bound = bind(unix(&path, SocketDir::PrepareSetgid)).await.unwrap();
            let served = Served::start(vec![bound], app, LONG_DRAIN);
            let mut answered = 0;

            for (value, status) in table {
                let mut request =
                    b"GET /healthz HTTP/1.1\r\nHost: test\r\nConnection: close\r\nAuthorization: "
                        .to_vec();
                request.extend_from_slice(value);
                request.extend_from_slice(b"\r\n\r\n");

                let answer = within(RawHttp::unix(&path, &request)).await.unwrap();

                assert_eq!(status_of(&answer), status, "{value:?}");
                if status == 200 {
                    answered += 1;
                } else {
                    assert!(body_of(&answer).is_empty(), "{value:?}");
                }
                assert_eq!(calls.load(Ordering::SeqCst), answered, "{value:?}");
            }
            assert_eq!(served.stop().await, Ok(Drained::Clean));
        });
    }

    /// `h11` refuses a request of HTTP/1.1 with no `Host` header, a request
    /// with two, and a target with a byte past `0x7E`
    /// (`h11/_events.py:112-119`, `h11/_abnf.py:83`). `uvicorn` then answers
    /// status 400 with a text (`uvicorn/protocols/http/h11_impl.py:304-320`).
    #[test]
    fn a_request_that_the_python_server_refuses_gets_its_answer() {
        let refused: [&[u8]; 6] = [
            b"GET /healthz HTTP/1.1\r\nConnection: keep-alive\r\n\r\n",
            b"GET /healthz HTTP/1.1\r\nHost: one\r\nHost: two\r\n\r\n",
            b"GET /healthz HTTP/1.0\r\nHost: one\r\nHost: two\r\n\r\n",
            b"GET /caf\xc3\xa9 HTTP/1.1\r\nHost: test\r\n\r\n",
            b"GET /healthz?b=\xc3\xa9 HTTP/1.1\r\nHost: test\r\n\r\n",
            b"POST /healthz HTTP/1.1\r\nContent-Length: 0\r\n\r\n",
        ];
        let served: [&[u8]; 2] = [
            // HTTP/1.0 has no rule for a request with no `Host` header.
            b"GET /healthz HTTP/1.0\r\n\r\n",
            // `0x7E` is the last byte that a target can hold.
            b"GET /healthz?b=~ HTTP/1.1\r\nHost: test\r\nConnection: close\r\n\r\n",
        ];

        runtime().block_on(async {
            let root = TempRoot::new().unwrap();
            let path = socket_in(&root);
            let calls = Arc::new(AtomicUsize::new(0));
            let app = Router::new().fallback({
                let calls = Arc::clone(&calls);

                move || async move {
                    calls.fetch_add(1, Ordering::SeqCst);

                    HEALTH_BODY
                }
            });
            let bound = bind(unix(&path, SocketDir::PrepareSetgid)).await.unwrap();
            let served_app = Served::start(vec![bound], app, LONG_DRAIN);

            for request in refused {
                // No request of the table asks for a close: the answer
                // closes the connection.
                let answer = within(RawHttp::unix(&path, request)).await.unwrap();

                assert_eq!(status_of(&answer), 400, "{request:?}");
                assert_eq!(
                    header_of(&answer, "content-type").as_deref(),
                    Some("text/plain; charset=utf-8"),
                    "{request:?}"
                );
                assert_eq!(
                    header_of(&answer, "connection").as_deref(),
                    Some("close"),
                    "{request:?}"
                );
                assert_eq!(
                    body_of(&answer),
                    b"Invalid HTTP request received.",
                    "{request:?}"
                );
            }
            assert_eq!(calls.load(Ordering::SeqCst), 0);

            // `uvicorn` sends the text to this client too.
            let head = within(RawHttp::unix(&path, b"HEAD /healthz HTTP/1.1\r\n\r\n"))
                .await
                .unwrap();

            assert_eq!(status_of(&head), 400);
            assert_eq!(header_of(&head, "content-length").as_deref(), Some("30"));
            assert!(body_of(&head).is_empty());
            assert_eq!(calls.load(Ordering::SeqCst), 0);

            for request in served {
                let answer = within(RawHttp::unix(&path, request)).await.unwrap();

                assert_eq!(status_of(&answer), 200, "{request:?}");
                assert_eq!(body_of(&answer), HEALTH_BODY.as_bytes(), "{request:?}");
            }
            assert_eq!(calls.load(Ordering::SeqCst), served.len());
            assert_eq!(served_app.stop().await, Ok(Drained::Clean));
        });
    }

    /// `h11` reads each request of the first table, and a Python service
    /// answers it. The Python framework has no route for the path of the
    /// first request of the second table: the `#` is a part of that path.
    #[test]
    fn a_listener_has_the_grammar_of_its_http_crates() {
        let refused: [&[u8]; 9] = [
            b"GET /healthz<x HTTP/1.1\r\nHost: test\r\n\r\n",
            b"GET /healthz>x HTTP/1.1\r\nHost: test\r\n\r\n",
            b"GET /healthz`x HTTP/1.1\r\nHost: test\r\n\r\n",
            b"GET /healthz?q=\"a\" HTTP/1.1\r\nHost: test\r\n\r\n",
            b"GET /healthz?q=<a HTTP/1.1\r\nHost: test\r\n\r\n",
            b"GET /healthz?q=a> HTTP/1.1\r\nHost: test\r\n\r\n",
            // A header line that continues on the next line.
            b"GET /healthz HTTP/1.1\r\nHost: test\r\nX-A: a\r\n b\r\n\r\n",
            b"GET /healthz HTTP/1.2\r\nHost: test\r\n\r\n",
            b"GET /healthz HTTP/2.0\r\nHost: test\r\n\r\n",
        ];
        let served: [(&[u8], &str); 3] = [
            (
                b"GET /healthz#part HTTP/1.1\r\nHost: test\r\nConnection: close\r\n\r\n",
                "/healthz",
            ),
            (
                b"GET /healthz?a=1#part HTTP/1.1\r\nHost: test\r\nConnection: close\r\n\r\n",
                "/healthz?a=1",
            ),
            // The other bytes that the Python framework writes as `%XX`
            // into a `Location` header.
            (
                b"GET /a\"{|}\\^?q={|}\\^` HTTP/1.1\r\nHost: test\r\nConnection: close\r\n\r\n",
                "/a\"{|}\\^?q={|}\\^`",
            ),
        ];

        runtime().block_on(async {
            let root = TempRoot::new().unwrap();
            let path = socket_in(&root);
            let calls = Arc::new(AtomicUsize::new(0));
            let app = Router::new().fallback({
                let calls = Arc::clone(&calls);

                move |request: Request| async move {
                    calls.fetch_add(1, Ordering::SeqCst);

                    request.uri().to_string()
                }
            });
            let bound = bind(unix(&path, SocketDir::PrepareSetgid)).await.unwrap();
            let served_app = Served::start(vec![bound], app, LONG_DRAIN);

            for request in refused {
                let answer = within(RawHttp::unix(&path, request)).await.unwrap();

                assert_eq!(status_of(&answer), 400, "{request:?}");
                assert!(body_of(&answer).is_empty(), "{request:?}");
            }
            assert_eq!(calls.load(Ordering::SeqCst), 0);

            for (request, target) in served {
                let answer = within(RawHttp::unix(&path, request)).await.unwrap();

                assert_eq!(status_of(&answer), 200, "{request:?}");
                assert_eq!(body_of(&answer), target.as_bytes(), "{request:?}");
            }
            assert_eq!(served_app.stop().await, Ok(Drained::Clean));
        });
    }

    #[test]
    fn a_head_past_16_kib_is_read() {
        runtime().block_on(async {
            let root = TempRoot::new().unwrap();
            let path = socket_in(&root);
            let bound = bind(unix(&path, SocketDir::PrepareSetgid)).await.unwrap();
            let served = Served::start(vec![bound], health_app(), LONG_DRAIN);
            let mut request =
                b"GET /healthz HTTP/1.1\r\nHost: test\r\nConnection: close\r\nX-Fill: ".to_vec();
            request.resize(request.len() + 20 * 1024, b'a');
            request.extend_from_slice(b"\r\n\r\n");

            let answer = within(RawHttp::unix(&path, &request)).await.unwrap();

            assert_eq!(status_of(&answer), 200);
            assert_eq!(served.stop().await, Ok(Drained::Clean));
        });
    }

    #[test]
    fn a_head_of_more_than_100_headers_gets_status_431() {
        runtime().block_on(async {
            let root = TempRoot::new().unwrap();
            let path = socket_in(&root);
            let bound = bind(unix(&path, SocketDir::PrepareSetgid)).await.unwrap();
            let served = Served::start(vec![bound], health_app(), LONG_DRAIN);
            let head = |fill: usize| {
                let mut request = b"GET /healthz HTTP/1.1\r\n".to_vec();
                // `Host`, `Connection` and the fill headers.
                request.extend_from_slice(b"Host: test\r\nConnection: close\r\n");
                for count in 0..fill {
                    request.extend_from_slice(format!("X-Fill-{count}: a\r\n").as_bytes());
                }
                request.extend_from_slice(b"\r\n");

                request
            };

            let at_the_limit = within(RawHttp::unix(&path, &head(98))).await.unwrap();
            let past_the_limit = within(RawHttp::unix(&path, &head(99))).await.unwrap();

            assert_eq!(status_of(&at_the_limit), 200);
            assert_eq!(status_of(&past_the_limit), 431);
            assert!(body_of(&past_the_limit).is_empty());
            assert_eq!(served.stop().await, Ok(Drained::Clean));
        });
    }

    #[test]
    fn a_head_past_the_byte_limit_gets_status_431() {
        /// The limit of `hyper` for the bytes of a head
        /// (`DEFAULT_MAX_BUFFER_SIZE` of `src/proto/h1/io.rs`).
        const HEAD_LIMIT: usize = 8192 + 4096 * 100;

        /// A distance from the limit. The server reads a head in parts, so
        /// the test asks for no answer at the limit itself.
        const MARGIN: usize = 4096;

        runtime().block_on(async {
            let root = TempRoot::new().unwrap();
            let path = socket_in(&root);
            let bound = bind(unix(&path, SocketDir::PrepareSetgid)).await.unwrap();
            let served = Served::start(vec![bound], health_app(), LONG_DRAIN);
            let head = |bytes: usize| {
                let mut request =
                    b"GET /healthz HTTP/1.1\r\nHost: test\r\nConnection: close\r\nX-Fill: "
                        .to_vec();
                request.resize(bytes - 4, b'a');
                request.extend_from_slice(b"\r\n\r\n");

                request
            };

            let below = within(RawHttp::unix(&path, &head(HEAD_LIMIT - MARGIN)))
                .await
                .unwrap();
            let past = within(RawHttp::unix(&path, &head(HEAD_LIMIT + MARGIN)))
                .await
                .unwrap();

            assert_eq!(HEAD_LIMIT, 417_792);
            assert_eq!(status_of(&below), 200);
            assert_eq!(status_of(&past), 431);
            assert!(body_of(&past).is_empty());
            assert_eq!(served.stop().await, Ok(Drained::Clean));
        });
    }

    #[test]
    fn bind_outside_a_runtime_is_an_error() {
        fn poll_once<F: Future>(future: F) -> Poll<F::Output> {
            let mut context = Context::from_waker(std::task::Waker::noop());

            std::pin::pin!(future).poll(&mut context)
        }

        let root = TempRoot::new().unwrap();
        let path = socket_in(&root);

        let unix_error = poll_once(bind(unix(&path, SocketDir::PrepareSetgid)));
        let tcp_error = poll_once(bind(loopback(8340)));

        for (polled, address) in [
            (unix_error, path.to_str().unwrap()),
            (tcp_error, "127.0.0.1:8340"),
        ] {
            let Poll::Ready(Err(error)) = polled else {
                panic!("the bind of {address} gave no error at its first poll");
            };

            assert_eq!(error.step(), BindStep::Bind);
            assert_eq!(error.kind(), io::ErrorKind::Other);
            assert_eq!(error.os_text(), NO_RUNTIME);
            assert_eq!(error.address(), address);
        }
        // No step ran.
        assert!(!path.parent().unwrap().exists());
    }

    #[test]
    fn the_child_runs_one_scenario() {
        let Some(name) = std::env::var_os(CHILD_VARIABLE) else {
            return;
        };
        let scenario = SCENARIOS
            .iter()
            .find(|scenario| name == scenario.name)
            .unwrap();

        crate::log::init(CHILD_PROGRAM);
        (scenario.run)();
    }

    #[test]
    fn each_scenario_writes_its_lines() {
        for scenario in SCENARIOS {
            let name = scenario.name;
            let child = run_child(CHILD_TEST, CHILD_VARIABLE, name);
            let stdout = String::from_utf8(child.stdout).unwrap();
            let stderr = String::from_utf8(child.stderr).unwrap();

            assert!(child.status.success(), "{name}: {stdout}\n{stderr}");
            assert!(stdout.contains("1 passed"), "{name}: {stdout}");

            // A line is the time, the level, the target and the message.
            let written: Vec<String> = stderr
                .lines()
                .filter_map(|line| {
                    let mut parts = line.splitn(4, ' ');
                    let (_time, level, target, message) =
                        (parts.next()?, parts.next()?, parts.next()?, parts.next()?);

                    (target == LOG_TARGET).then(|| format!("{level} {message}"))
                })
                .collect();

            (scenario.check)(&written);
        }
    }

    /// The file of a socket is gone after the stop of `serve`, and the next
    /// bind takes the path. `asyncio` of CPython 3.13 removes the file in
    /// the same way (`asyncio/unix_events.py:474-493`).
    fn the_socket_file_is_gone_after_serve_and_the_next_bind_takes_the_path() {
        runtime().block_on(async {
            let root = TempRoot::new().unwrap();
            let path = socket_in(&root);

            for _ in 0..2 {
                let bound = bind(unix(&path, SocketDir::PrepareSetgid)).await.unwrap();
                let served = Served::start(vec![bound], health_app(), LONG_DRAIN);
                let answer = within(RawHttp::unix(&path, GET_HEALTH)).await.unwrap();

                assert_eq!(status_of(&answer), 200);
                assert_eq!(served.stop().await, Ok(Drained::Clean));
                assert_eq!(
                    fs::symlink_metadata(&path).unwrap_err().kind(),
                    io::ErrorKind::NotFound
                );
                assert_eq!(
                    StdUnixStream::connect(&path).unwrap_err().kind(),
                    io::ErrorKind::NotFound
                );
            }
        });
    }

    /// The removal of the file fails, at the read of its inode and at the
    /// removal itself. The stop is clean, the file stays, and one line names
    /// it.
    fn a_socket_file_that_serve_cannot_remove_stays() {
        runtime().block_on(async {
            for call in [Call::InodeOf, Call::RemoveFile] {
                let root = TempRoot::new().unwrap();
                let path = socket_in(&root);
                let bound = bind(unix(&path, SocketDir::PrepareSetgid)).await.unwrap();
                let steps = Staged::failing(call, io::ErrorKind::PermissionDenied);
                let served = Served::start_through(steps, vec![bound], health_app(), LONG_DRAIN);

                assert_eq!(served.stop().await, Ok(Drained::Clean), "{call:?}");
                assert!(
                    fs::metadata(&path).unwrap().file_type().is_socket(),
                    "{call:?}"
                );
            }
        });
    }

    /// A new listener binds the port of a listener that stopped, at once.
    fn a_restart_binds_the_same_port_at_once() {
        runtime().block_on(async {
            let (bound, port) = bind_loopback().await;

            assert_eq!(bound.describe(), format!("127.0.0.1:{port}"));
            assert_eq!(bound.local_port(), Some(port));

            let served = Served::start(vec![bound], health_app(), LONG_DRAIN);
            // The server closes this connection first, so the kernel holds
            // the old connection of the port for a time.
            let answer = within(RawHttp::tcp(port, GET_HEALTH)).await.unwrap();

            assert_eq!(status_of(&answer), 200);
            assert_eq!(served.stop().await, Ok(Drained::Clean));

            let again = bind(loopback(port)).await.unwrap();
            let served = Served::start(vec![again], health_app(), LONG_DRAIN);
            let answer = within(RawHttp::tcp(port, GET_HEALTH)).await.unwrap();

            assert_eq!(status_of(&answer), 200);
            assert_eq!(served.stop().await, Ok(Drained::Clean));
        });
    }

    /// An accept error that stops one listener stops each listener of the
    /// same `serve` call.
    fn an_accept_error_of_a_listener_stops_each_listener() {
        runtime().block_on(async {
            let root = TempRoot::new().unwrap();
            let path = socket_in(&root);
            let healthy = bind(unix(&path, SocketDir::PrepareSetgid)).await.unwrap();
            let broken = scripted(vec![os_error(Errno::CONNABORTED), os_error(Errno::NOTSOCK)]);
            let started_at = Instant::now();

            let served = Served::start(vec![healthy, broken], health_app(), LONG_DRAIN);
            let stopped = within(served.serving).await.unwrap();

            let expected = os_error(Errno::NOTSOCK);
            assert_eq!(
                stopped,
                Err(ServeError::Accept {
                    address: String::from(SCRIPTED),
                    kind: expected.kind(),
                    os_text: os_text(&expected),
                })
            );
            // The other listener is closed too, and its file is gone.
            assert_eq!(
                StdUnixStream::connect(&path).unwrap_err().kind(),
                io::ErrorKind::NotFound
            );
            // The other listener closed at the error. No request was open, so
            // `serve` did not wait for the drain limit.
            assert!(started_at.elapsed() < LONG_DRAIN);
        });
    }

    /// The port of `test_prepare_socket_dir_warns_when_it_cannot_set_the_bit`
    /// (`attendance/tests/test_main.py:135-150`): a mode that the service
    /// cannot set is a warning, and the service still binds.
    fn the_mode_of_the_directory_cannot_change() {
        runtime().block_on(async {
            let root = TempRoot::new().unwrap();
            let path = socket_in(&root);
            let steps = Staged::failing(Call::SetDirMode, io::ErrorKind::PermissionDenied);

            let bound = bind_staged(steps, &path, SocketDir::PrepareSetgid)
                .await
                .unwrap();

            assert_ne!(mode_of(path.parent().unwrap()), 0o2750);
            assert_eq!(mode_of(&path), 0o660);
            drop(bound);
        });
    }

    /// The mode change gives no error, and the setgid bit is gone after it.
    fn the_directory_loses_its_setgid_bit() {
        runtime().block_on(async {
            let root = TempRoot::new().unwrap();
            let path = socket_in(&root);
            let steps = Staged {
                mode_after: Some(0o750),
                ..Staged::default()
            };

            let bound = bind_staged(steps, &path, SocketDir::PrepareSetgid)
                .await
                .unwrap();

            assert_eq!(mode_of(&path), 0o660);
            drop(bound);
        });
    }

    /// The host has no descriptor for one accept. The listener writes one
    /// line and waits one second. The next error stops it.
    fn an_accept_fails_two_times() {
        let paused = Builder::new_current_thread()
            .enable_all()
            .start_paused(true)
            .build()
            .unwrap();

        paused.block_on(async {
            let (_trigger, shutdown) = shutdown_pair();
            let tasks = Tasks::new(shutdown);
            let broken = scripted(vec![os_error(Errno::MFILE), os_error(Errno::BADF)]);
            let started_at = tokio::time::Instant::now();

            let stopped = serve(vec![broken], Router::new(), &tasks, LONG_DRAIN).await;

            let expected = os_error(Errno::BADF);
            assert_eq!(
                stopped,
                Err(ServeError::Accept {
                    address: String::from(SCRIPTED),
                    kind: expected.kind(),
                    os_text: os_text(&expected),
                })
            );
            assert!(started_at.elapsed() >= ACCEPT_RETRY);
        });
    }

    /// A runtime with no timer cannot wait before the next accept. The
    /// listener stops with the error of the accept, and does not panic.
    fn an_accept_fails_with_no_timer() {
        let no_timer = Builder::new_current_thread().enable_io().build().unwrap();

        no_timer.block_on(async {
            let (_trigger, shutdown) = shutdown_pair();
            let tasks = Tasks::new(shutdown);
            let broken = scripted(vec![os_error(Errno::MFILE)]);

            let stopped = serve(vec![broken], Router::new(), &tasks, LONG_DRAIN).await;

            let expected = os_error(Errno::MFILE);
            assert_eq!(
                stopped,
                Err(ServeError::Accept {
                    address: String::from(SCRIPTED),
                    kind: expected.kind(),
                    os_text: os_text(&expected),
                })
            );
        });
    }

    /// A runtime with no timer cannot hold the drain limit. `serve` does not
    /// panic: it ends each connection at the stop signal.
    fn a_runtime_has_no_timer() {
        let no_timer = Builder::new_current_thread().enable_io().build().unwrap();

        no_timer.block_on(async {
            let root = TempRoot::new().unwrap();
            let path = socket_in(&root);
            let started = Arc::new(Notify::new());
            let bound = bind(unix(&path, SocketDir::PrepareSetgid)).await.unwrap();
            let served = Served::start(vec![bound], open_stream_app(&started), LONG_DRAIN);
            let client = tokio::spawn({
                let path = path.clone();

                async move { RawHttp::unix(&path, GET_STREAM).await }
            });
            started.notified().await;

            served.trigger.trigger();

            assert_eq!(
                served.serving.await.unwrap(),
                Ok(Drained::TimedOut { left: 1 })
            );
            assert_eq!(status_of(&client.await.unwrap().unwrap()), 200);
        });
    }

    /// A runtime with no I/O driver cannot hold a listener. `bind` gives an
    /// error and does not panic.
    fn a_runtime_has_no_io_driver() {
        let no_io = Builder::new_current_thread().enable_time().build().unwrap();

        no_io.block_on(async {
            let root = TempRoot::new().unwrap();
            let path = socket_in(&root);

            let unix_error = bind(unix(&path, SocketDir::PrepareSetgid))
                .await
                .unwrap_err();
            let tcp_error = bind(loopback(free_port())).await.unwrap_err();

            for error in [unix_error, tcp_error] {
                assert_eq!(error.step(), BindStep::Bind);
                assert_eq!(error.kind(), io::ErrorKind::Other);
                assert_eq!(error.os_text(), NO_IO_DRIVER);
            }
        });
    }

    /// `serve` starts no listener on a thread with no runtime.
    fn no_runtime_runs() {
        let (_trigger, shutdown) = shutdown_pair();
        let tasks = Tasks::new(shutdown);
        let serving = serve(
            vec![scripted(Vec::new())],
            Router::new(),
            &tasks,
            LONG_DRAIN,
        );
        let mut context = Context::from_waker(std::task::Waker::noop());

        let polled = std::pin::pin!(serving).poll(&mut context);

        assert_eq!(
            polled,
            Poll::Ready(Err(ServeError::ListenerLost {
                address: String::from(SCRIPTED)
            }))
        );
    }
}
