//! The socket of a service VM (`service::link`, browser design 10.2 items
//! 1 and 2, the attachments design note) on a scratch dir in `/tmp`. A
//! fake hook dials a fake vsock socket that plays guest init on port
//! 1201, so each link reaches a guest stream that the test reads and
//! writes. Each proof runs for each service image.

use std::fs::{self, Permissions};
use std::io::{BufRead, BufReader, Read, Write};
use std::os::unix::fs::{FileTypeExt, MetadataExt, PermissionsExt};
use std::os::unix::net::{UnixListener, UnixStream};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::thread::spawn;
use std::time::Duration;

use creche_vmm::relay::AcpLink;
use creche_vmm::service::ServiceError;
use creche_vmm::service::link::{
    BindFault, LINK_CAP, MCP_LINE, SCAN_BUSY_LINE, SCAN_LINE, SCAN_LINK_CAP, SOCKET_MODE, Served,
    ServiceVm, bind, busy_line, kind_line, link_cap, uid_of,
};
use creche_wire::{PerService, ServiceImage};
use tokio::runtime::{Builder, Runtime};

/// The bound of each read of a test.
const WAIT: Duration = Duration::from_secs(5);

/// The shared scan vector of the attachments design note.
const SCAN_VECTOR: &str = include_str!("../../wire/vectors/scan.json");

/// Each service image, in the order of the enum.
fn images() -> Vec<ServiceImage> {
    PerService::every(|_| ())
        .iter()
        .map(|(image, ())| image)
        .collect()
}

/// The fake hook: each link of `image` dials the fake guest at `vsock`,
/// or fails when `fail` is set.
struct Hook {
    image: ServiceImage,
    vsock: PathBuf,
    fail: bool,
    calls: AtomicUsize,
}

impl ServiceVm for Hook {
    async fn link(&self, image: ServiceImage) -> Result<AcpLink, ServiceError> {
        self.calls.fetch_add(1, Ordering::SeqCst);
        assert_eq!(image, self.image);
        if self.fail {
            return Err(ServiceError("the fake VM did not start".to_owned()));
        }
        AcpLink::dial(&self.vsock)
            .await
            .map_err(|error| ServiceError(format!("{error:?}")))
    }
}

/// A scratch dir with the dir of the socket, mode 0750, and the runtime.
struct Scratch {
    dir: PathBuf,
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
    fn new(name: &str) -> Self {
        let dir = PathBuf::from(format!("/tmp/vmm-service-{}-{name}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(dir.join("browser")).unwrap();
        fs::set_permissions(dir.join("browser"), Permissions::from_mode(0o750)).unwrap();
        let runtime = Builder::new_multi_thread()
            .worker_threads(2)
            .enable_all()
            .build()
            .unwrap();
        Self { dir, runtime }
    }

    fn socket(&self) -> PathBuf {
        self.dir.join("browser/relay.sock")
    }

    /// Plays guest init on port 1201: `OK`, then each accepted stream goes
    /// to `guests`.
    fn guest(&self) -> (PathBuf, Arc<Mutex<Vec<UnixStream>>>) {
        let vsock = self.dir.join("vsock.sock");
        let listener = UnixListener::bind(&vsock).unwrap();
        let guests = Arc::new(Mutex::new(Vec::new()));
        let held = Arc::clone(&guests);
        spawn(move || {
            for stream in listener.incoming() {
                let mut stream = stream.unwrap();
                let mut reader = BufReader::new(stream.try_clone().unwrap());
                let mut line = String::new();
                reader.read_line(&mut line).unwrap();
                assert_eq!(line, "CONNECT 1201\n");
                stream.write_all(b"OK 7\n").unwrap();
                held.lock().unwrap().push(stream);
            }
        });
        (vsock, guests)
    }

    /// Binds and serves the socket for the peer `peer` with `hook`, for
    /// the image of `hook`.
    fn serve(&self, hook: &Arc<Hook>, peer: u32) -> Served {
        let bound = bind(&self.socket()).unwrap();
        let _runtime = self.runtime.enter();
        bound.serve(Arc::clone(hook), hook.image, peer).unwrap()
    }

    /// A client link of the relay.
    fn dial(&self) -> UnixStream {
        let stream = UnixStream::connect(self.socket()).unwrap();
        stream.set_read_timeout(Some(WAIT)).unwrap();
        stream
    }
}

/// The uid of the test process: the owner of the scratch dir that it
/// made.
#[cfg(test)]
fn own_uid(scratch: &Scratch) -> u32 {
    fs::metadata(&scratch.dir).unwrap().uid()
}

#[cfg(test)]
fn hook(image: ServiceImage, vsock: &Path, fail: bool) -> Arc<Hook> {
    Arc::new(Hook {
        image,
        vsock: vsock.to_owned(),
        fail,
        calls: AtomicUsize::new(0),
    })
}

/// Waits up to `WAIT` for `done`.
#[cfg(test)]
fn eventually(done: impl Fn() -> bool) {
    let deadline = std::time::Instant::now() + WAIT;
    while !done() {
        assert!(
            std::time::Instant::now() < deadline,
            "the condition did not hold"
        );
        std::thread::sleep(Duration::from_millis(1));
    }
}

/// The kind line of each image: `mcp` for the browser, and `scan` for
/// the scanner. The scanner line is `kind_line` of the shared scan vector
/// (attachments design note, section 4).
#[test]
fn each_image_has_its_kind_line() {
    assert_eq!(kind_line(ServiceImage::Browser), b"mcp\n");
    assert_eq!(kind_line(ServiceImage::Browser), MCP_LINE);
    assert_eq!(kind_line(ServiceImage::Scanner), b"scan\n");
    assert_eq!(kind_line(ServiceImage::Scanner), SCAN_LINE);

    let vector: serde_json::Value = serde_json::from_str(SCAN_VECTOR).unwrap();
    let line = format!("{}\n", vector["kind_line"].as_str().unwrap());
    assert_eq!(SCAN_LINE, line.as_bytes());
}

/// The link cap of each image: 4 for the browser, and 8 for the scanner.
/// The scanner cap holds the files of one message (`max_files` of the
/// shared scan vector).
#[test]
fn each_image_has_its_link_cap() {
    assert_eq!(link_cap(ServiceImage::Browser), 4);
    assert_eq!(link_cap(ServiceImage::Browser), LINK_CAP);
    assert_eq!(link_cap(ServiceImage::Scanner), 8);
    assert_eq!(link_cap(ServiceImage::Scanner), SCAN_LINK_CAP);

    let vector: serde_json::Value = serde_json::from_str(SCAN_VECTOR).unwrap();
    let files = usize::try_from(vector["max_files"].as_u64().unwrap()).unwrap();
    assert!(SCAN_LINK_CAP >= files);
}

/// A link of the peer uid reaches the guest. The guest reads the kind
/// line of the image first, then the bytes of the client, and the client
/// reads the bytes of the guest (rules 2 to 4).
#[test]
fn a_link_of_the_peer_reaches_the_guest_after_the_kind_line() {
    for image in images() {
        let scratch = Scratch::new(&format!("link-{}", image.name()));
        let (vsock, guests) = scratch.guest();
        let hook = hook(image, &vsock, false);
        let _served = scratch.serve(&hook, own_uid(&scratch));
        let meta = fs::symlink_metadata(scratch.socket()).unwrap();
        assert!(meta.file_type().is_socket());
        assert_eq!(meta.permissions().mode() & 0o777, SOCKET_MODE);

        let mut client = scratch.dial();
        client.write_all(b"{\"id\":1}\n").unwrap();
        eventually(|| !guests.lock().unwrap().is_empty());
        let mut guest = guests.lock().unwrap().remove(0);
        guest.set_read_timeout(Some(WAIT)).unwrap();
        let line = kind_line(image);
        let mut first = vec![0; line.len() + 9];
        guest.read_exact(&mut first).unwrap();
        assert_eq!(first, [line, b"{\"id\":1}\n"].concat());

        guest.write_all(b"{\"result\":{}}\n").unwrap();
        let mut reply = vec![0; 14];
        client.read_exact(&mut reply).unwrap();
        assert_eq!(reply, b"{\"result\":{}}\n");
        assert_eq!(hook.calls.load(Ordering::SeqCst), 1);
    }
}

/// The busy line of the scanner is `reply_refused` of the shared scan
/// vector with the reason `busy`. The browser has no busy line.
#[test]
fn the_scanner_busy_line_is_the_vector_refusal() {
    let vector: serde_json::Value = serde_json::from_str(SCAN_VECTOR).unwrap();
    let reasons = vector["reasons"].as_array().unwrap();
    assert!(reasons.contains(&serde_json::Value::from("busy")));
    let mut refused = vector["reply_refused"].clone();
    refused["refused"]["reason"] = serde_json::Value::from("busy");
    let line = format!("{refused}\n");
    assert_eq!(SCAN_BUSY_LINE, line.as_bytes());
    assert_eq!(busy_line(ServiceImage::Scanner), Some(SCAN_BUSY_LINE));
    assert_eq!(busy_line(ServiceImage::Browser), None);
}

/// The socket of each image holds at most `link_cap` links. The link
/// past the cap closes at once, and no VM start runs for it (rule 5). A
/// browser link past the cap reads no byte. A scanner link past the cap
/// reads the busy line, then the close.
#[test]
fn a_link_past_the_cap_closes() {
    for image in images() {
        let scratch = Scratch::new(&format!("cap-{}", image.name()));
        let (vsock, guests) = scratch.guest();
        let hook = hook(image, &vsock, false);
        let _served = scratch.serve(&hook, own_uid(&scratch));
        let cap = link_cap(image);
        let _open: Vec<UnixStream> = (0..cap).map(|_| scratch.dial()).collect();
        eventually(|| guests.lock().unwrap().len() == cap);

        let mut past = scratch.dial();
        let mut rest = Vec::new();
        past.read_to_end(&mut rest).unwrap();
        assert_eq!(
            rest,
            busy_line(image).unwrap_or_default(),
            "{}",
            image.name()
        );
        assert_eq!(hook.calls.load(Ordering::SeqCst), cap);
    }
}

/// A peer with another uid gets a close, and no VM starts for it.
#[test]
fn a_peer_with_another_uid_is_closed() {
    for image in images() {
        let scratch = Scratch::new(&format!("peer-{}", image.name()));
        let (vsock, _guests) = scratch.guest();
        let hook = hook(image, &vsock, false);
        let _served = scratch.serve(&hook, own_uid(&scratch).wrapping_add(1));
        let mut client = scratch.dial();
        let mut rest = Vec::new();
        assert_eq!(client.read_to_end(&mut rest).unwrap(), 0);
        assert_eq!(hook.calls.load(Ordering::SeqCst), 0);
    }
}

/// A VM that does not start closes the link.
#[test]
fn a_failed_start_closes_the_link() {
    for image in images() {
        let scratch = Scratch::new(&format!("fail-{}", image.name()));
        let (vsock, _guests) = scratch.guest();
        let hook = hook(image, &vsock, true);
        let _served = scratch.serve(&hook, own_uid(&scratch));
        let mut client = scratch.dial();
        let mut rest = Vec::new();
        assert_eq!(client.read_to_end(&mut rest).unwrap(), 0);
        assert_eq!(hook.calls.load(Ordering::SeqCst), 1);
    }
}

/// The bind refuses a missing dir, a dir open to others and a file that
/// is no socket. It replaces the socket of an earlier vmm (rule 1).
#[test]
fn the_bind_checks_the_dir_and_the_path() {
    let scratch = Scratch::new("bind");
    let socket = scratch.socket();
    let missing = scratch.dir.join("absent/relay.sock");
    assert!(matches!(bind(&missing), Err(BindFault::NoDir(_))));

    fs::set_permissions(scratch.dir.join("browser"), Permissions::from_mode(0o755)).unwrap();
    assert!(matches!(bind(&socket), Err(BindFault::DirOpen(_, 0o755))));
    fs::set_permissions(scratch.dir.join("browser"), Permissions::from_mode(0o750)).unwrap();

    fs::write(&socket, b"").unwrap();
    assert!(matches!(bind(&socket), Err(BindFault::NotSocket(_))));
    fs::remove_file(&socket).unwrap();

    let earlier = UnixListener::bind(&socket).unwrap();
    drop(earlier);
    let bound = bind(&socket).unwrap();
    drop(bound);
    assert!(
        fs::symlink_metadata(&socket)
            .unwrap()
            .file_type()
            .is_socket()
    );
}

/// `uid_of` reads the third field of the line of the user, and skips a
/// line that does not read.
#[test]
fn the_uid_comes_from_the_passwd_line() {
    let scratch = Scratch::new("passwd");
    let passwd = scratch.dir.join("passwd");
    let text = "root:x:0:0:root:/root:/bin/bash\nbroken line\n\
        mcp-browser:x:974:970::/nonexistent:/usr/sbin/nologin\n";
    fs::write(&passwd, text).unwrap();
    assert_eq!(uid_of("mcp-browser", &passwd).unwrap(), Some(974));
    assert_eq!(uid_of("root", &passwd).unwrap(), Some(0));
    assert_eq!(uid_of("mcp-memory", &passwd).unwrap(), None);
    assert!(uid_of("root", &scratch.dir.join("absent")).is_err());
}
