//! `lo` up through one RTM_NEWLINK message on a netlink route socket
//! (decisions CH6-2, CH6-3: the planes listen on 127.0.0.1, and the guest
//! has no network card).
//!
//! `request` and `read_ack` are pure, so the unit tests check the bytes on
//! any OS. Netlink uses the byte order of the host.

use std::io;

/// `RTM_NEWLINK` of `linux/rtnetlink.h`.
const RTM_NEWLINK: u16 = 16;

/// `NLM_F_REQUEST | NLM_F_ACK`: the kernel answers with an ack.
const REQUEST_ACK: u16 = 0x1 | 0x4;

/// `NLMSG_ERROR`: the type of an ack. Error 0 means success.
const NLMSG_ERROR: u16 = 2;

/// `IFF_UP` of `linux/if.h`.
const IFF_UP: u32 = 0x1;

/// `LOOPBACK_IFINDEX` of `net/net_namespace.h`: the kernel gives `lo`
/// index 1.
const LOOPBACK_INDEX: i32 = 1;

/// The length of the request: a 16-byte `nlmsghdr` and a 16-byte
/// `ifinfomsg`.
const REQUEST_LEN: u32 = 32;

/// The RTM_NEWLINK request that sets `IFF_UP` on `lo`.
pub fn request() -> Vec<u8> {
    let mut bytes = Vec::with_capacity(32);
    // nlmsghdr: length, type, flags, sequence 1, port 0 (the kernel).
    bytes.extend_from_slice(&REQUEST_LEN.to_ne_bytes());
    bytes.extend_from_slice(&RTM_NEWLINK.to_ne_bytes());
    bytes.extend_from_slice(&REQUEST_ACK.to_ne_bytes());
    bytes.extend_from_slice(&1_u32.to_ne_bytes());
    bytes.extend_from_slice(&0_u32.to_ne_bytes());
    // ifinfomsg: family AF_UNSPEC, pad, type, index, flags, change mask.
    bytes.extend_from_slice(&[0, 0]);
    bytes.extend_from_slice(&0_u16.to_ne_bytes());
    bytes.extend_from_slice(&LOOPBACK_INDEX.to_ne_bytes());
    bytes.extend_from_slice(&IFF_UP.to_ne_bytes());
    bytes.extend_from_slice(&IFF_UP.to_ne_bytes());
    bytes
}

/// Reads the ack of the kernel: `Ok` for error 0, else the errno.
///
/// # Errors
///
/// `InvalidData` for a reply that is no ack, else the errno of the ack.
pub fn read_ack(reply: &[u8]) -> io::Result<()> {
    let kind = reply
        .get(4..6)
        .and_then(|bytes| <[u8; 2]>::try_from(bytes).ok())
        .map(u16::from_ne_bytes);
    let error = reply
        .get(16..20)
        .and_then(|bytes| <[u8; 4]>::try_from(bytes).ok())
        .map(i32::from_ne_bytes);
    match (kind, error) {
        (Some(NLMSG_ERROR), Some(0)) => Ok(()),
        (Some(NLMSG_ERROR), Some(error)) => match error.checked_neg() {
            Some(errno) if errno > 0 => Err(io::Error::from_raw_os_error(errno)),
            _ => Err(io::Error::new(
                io::ErrorKind::InvalidData,
                format!("netlink ack error {error}"),
            )),
        },
        _ => Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "the netlink reply is no ack",
        )),
    }
}

/// Sends the request and reads the ack.
///
/// # Errors
///
/// The socket call that failed, or the errno of the ack.
#[cfg(target_os = "linux")]
pub fn up() -> io::Result<()> {
    use rustix::net::{AddressFamily, RecvFlags, SendFlags, SocketFlags, SocketType};

    // Protocol `None` is NETLINK_ROUTE. With no address, `send` goes to
    // the kernel.
    let socket = rustix::net::socket_with(
        AddressFamily::NETLINK,
        SocketType::RAW,
        SocketFlags::CLOEXEC,
        None,
    )?;
    let request = request();
    let sent = rustix::net::send(&socket, &request, SendFlags::empty())?;
    if sent != request.len() {
        return Err(io::Error::new(
            io::ErrorKind::WriteZero,
            "the netlink request went out in part",
        ));
    }
    // A failed request echoes the 32-byte request after the 20-byte ack
    // head. A cut reply still holds the head.
    let mut reply = [0_u8; 256];
    let (read, _) = rustix::net::recv(&socket, &mut reply[..], RecvFlags::empty())?;
    read_ack(reply.get(..read).unwrap_or_default())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn ack(kind: u16, error: i32) -> Vec<u8> {
        let mut bytes = 36_u32.to_ne_bytes().to_vec();
        bytes.extend_from_slice(&kind.to_ne_bytes());
        bytes.extend_from_slice(&[0; 10]);
        bytes.extend_from_slice(&error.to_ne_bytes());
        bytes.extend_from_slice(request().get(..16).unwrap());
        bytes
    }

    #[test]
    fn the_request_sets_lo_up() {
        let bytes = request();
        assert_eq!(bytes.len(), 32);
        let word = |at: usize| u32::from_ne_bytes(bytes[at..at + 4].try_into().unwrap());
        let half = |at: usize| u16::from_ne_bytes(bytes[at..at + 2].try_into().unwrap());
        assert_eq!(word(0), 32);
        assert_eq!(half(4), 16, "RTM_NEWLINK");
        assert_eq!(half(6), 5, "NLM_F_REQUEST | NLM_F_ACK");
        assert_eq!(word(20), 1, "the index of lo");
        assert_eq!(word(24), 1, "IFF_UP");
        assert_eq!(word(28), 1, "the change mask is IFF_UP");
    }

    #[test]
    fn an_ack_of_error_0_is_success() {
        read_ack(&ack(NLMSG_ERROR, 0)).unwrap();
    }

    #[test]
    fn an_ack_of_a_negative_error_is_the_errno() {
        let error = read_ack(&ack(NLMSG_ERROR, -1)).unwrap_err();
        assert_eq!(error.raw_os_error(), Some(1));
    }

    #[test]
    fn a_reply_of_another_type_is_refused() {
        let error = read_ack(&ack(RTM_NEWLINK, 0)).unwrap_err();
        assert_eq!(error.kind(), io::ErrorKind::InvalidData);
        let error = read_ack(&[0; 8]).unwrap_err();
        assert_eq!(error.kind(), io::ErrorKind::InvalidData);
    }
}
