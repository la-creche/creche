//! The systemd readiness notice of `vmm serve`.

use std::ffi::OsStr;
use std::io;
use std::os::unix::ffi::OsStrExt;
use std::os::unix::net::UnixDatagram;
use std::path::Path;

#[cfg(target_os = "linux")]
use std::os::linux::net::SocketAddrExt;
#[cfg(target_os = "linux")]
use std::os::unix::net::SocketAddr;

/// The environment key that systemd gives to a `Type=notify` service.
const NOTIFY_SOCKET: &str = "NOTIFY_SOCKET";

/// The one state transition that this process reports.
const READY: &[u8] = b"READY=1";

/// Reports readiness when systemd gave this process a notification socket.
/// A run outside a service unit has no socket and needs no report.
pub(crate) fn notify() -> io::Result<()> {
    notify_to(std::env::var_os(NOTIFY_SOCKET).as_deref())
}

fn notify_to(target: Option<&OsStr>) -> io::Result<()> {
    let Some(target) = target else {
        return Ok(());
    };
    if target.is_empty() {
        return Err(io::Error::from(io::ErrorKind::InvalidInput));
    }

    let socket = UnixDatagram::unbound()?;
    let bytes = target.as_bytes();
    let sent = match bytes.strip_prefix(b"@") {
        Some(name) => send_abstract(&socket, name)?,
        None => socket.send_to(READY, Path::new(target))?,
    };
    if sent != READY.len() {
        return Err(io::Error::from(io::ErrorKind::WriteZero));
    }

    Ok(())
}

#[cfg(target_os = "linux")]
fn send_abstract(socket: &UnixDatagram, name: &[u8]) -> io::Result<usize> {
    let target = SocketAddr::from_abstract_name(name)?;
    socket.send_to_addr(READY, &target)
}

#[cfg(not(target_os = "linux"))]
fn send_abstract(_socket: &UnixDatagram, _name: &[u8]) -> io::Result<usize> {
    Err(io::Error::from(io::ErrorKind::Unsupported))
}

#[cfg(test)]
mod tests {
    use std::ffi::OsStr;
    use std::fs;
    use std::os::unix::net::UnixDatagram;
    use std::path::PathBuf;

    use super::{READY, notify_to};

    fn path() -> PathBuf {
        PathBuf::from(format!("creche-vmm-notify-{}.sock", std::process::id()))
    }

    #[test]
    fn no_notification_socket_is_a_direct_run() {
        assert!(notify_to(None).is_ok());
    }

    #[test]
    fn ready_goes_to_a_filesystem_socket() {
        let path = path();
        let _ = fs::remove_file(&path);
        let receiver = UnixDatagram::bind(&path).unwrap();

        notify_to(Some(path.as_os_str())).unwrap();

        let mut bytes = [0_u8; 16];
        let read = receiver.recv(&mut bytes).unwrap();
        assert_eq!(&bytes[..read], READY);
        fs::remove_file(path).unwrap();
    }

    #[test]
    fn an_empty_notification_socket_is_refused() {
        let error = notify_to(Some(OsStr::new(""))).unwrap_err();

        assert_eq!(error.kind(), std::io::ErrorKind::InvalidInput);
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn ready_goes_to_an_abstract_socket() {
        use std::ffi::OsString;
        use std::os::linux::net::SocketAddrExt;
        use std::os::unix::net::SocketAddr;

        let name = format!("creche-vmm-notify-{}", std::process::id());
        let address = SocketAddr::from_abstract_name(name.as_bytes()).unwrap();
        let receiver = UnixDatagram::bind_addr(&address).unwrap();
        let target = OsString::from(format!("@{name}"));

        notify_to(Some(&target)).unwrap();

        let mut bytes = [0_u8; 16];
        let read = receiver.recv(&mut bytes).unwrap();
        assert_eq!(&bytes[..read], READY);
    }
}
