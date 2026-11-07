//! The one decision of an egress target (decisions CH6-3, CH6-4, CH69
//! rules 2 and 8). The route `check` and the proxy both call it, so a
//! check answers what a CONNECT gets.
//!
//! 1. `Target::parse` reads `host:port`. It refuses an IP literal with
//!    `ip_literal`. It lowers the case of the host and refuses any other
//!    text that is no host name and port with `bad_target`.
//! 2. `decide` refuses a host and port that the allowlist does not hold
//!    with `not_listed`, before any lookup.
//! 3. `decide` resolves the name with the system resolver (tokio
//!    `lookup_host`, which runs getaddrinfo off the runtime).
//! 4. `judge` maps each IPv4-mapped IPv6 record to IPv4. One record that
//!    is not public refuses the whole name with `not_public`. No record is
//!    `no_address`. Else the verdict allows the target at the records, in
//!    the resolver's order.

use std::net::{IpAddr, Ipv4Addr, Ipv6Addr, SocketAddr};
use std::num::NonZeroU16;

use creche_wire::{
    CheckAllowed, CheckReason, CheckRefused, CheckReply, FamilyEgress, HostName, IpAddress,
};
use tokio::net::lookup_host;

/// A target of a check or of a CONNECT: a host name and a port.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Target {
    host: HostName,
    port: NonZeroU16,
}

impl Target {
    /// Reads `text` as `host:port` (decision CH6-4).
    ///
    /// # Errors
    ///
    /// `IpLiteral` for an IPv4 or IPv6 address, bracketed or not, with a
    /// port or none. `BadTarget` for a host that is no host name in lower
    /// case (a trailing dot, `*`), a host with no port, and a port that is
    /// not 1 to 65535.
    pub fn parse(text: &str) -> Result<Self, CheckReason> {
        if is_ipv6_literal(text) {
            return Err(CheckReason::IpLiteral);
        }
        let (host, port) = match text.split_once(':') {
            Some((host, port)) => (host, Some(port)),
            None => (text, None),
        };
        let host = host.to_ascii_lowercase();
        if is_numeric_host(&host) {
            return Err(CheckReason::IpLiteral);
        }
        let port = port.and_then(parse_port).ok_or(CheckReason::BadTarget)?;
        let host = host.parse().map_err(|_| CheckReason::BadTarget)?;
        Ok(Self { host, port })
    }
}

/// Whether `text` is an IPv6 address, bare or in brackets, with a port or
/// none.
fn is_ipv6_literal(text: &str) -> bool {
    let bracketed = text
        .strip_prefix('[')
        .and_then(|rest| rest.split_once(']'))
        .map(|(inside, _)| inside);
    let before_port = text.rsplit_once(':').map(|(host, _)| host);
    [bracketed, Some(text), before_port]
        .into_iter()
        .flatten()
        .any(|host| host.parse::<Ipv6Addr>().is_ok())
}

/// Whether getaddrinfo would read `host` as an IPv4 address with no
/// lookup: 1 to 4 dot-separated parts, each decimal or `0x` hex
/// (inet_aton). Each such name is an IP literal (decision CH6-4), so a
/// family file entry such as `2130706433` never reaches 127.0.0.1.
fn is_numeric_host(host: &str) -> bool {
    let parts: Vec<&str> = host.split('.').collect();
    let numeric = |part: &&str| match part.strip_prefix("0x") {
        Some(hex) => hex.bytes().all(|byte| byte.is_ascii_hexdigit()),
        None => !part.is_empty() && part.bytes().all(|byte| byte.is_ascii_digit()),
    };
    parts.len() <= 4 && parts.iter().all(numeric)
}

/// The port of `text`: 1 to 5 ASCII digits of 1 to 65535. `u16::from_str`
/// alone would also take a `+` first.
fn parse_port(text: &str) -> Option<NonZeroU16> {
    let digits = (1..=5).contains(&text.len()) && text.bytes().all(|byte| byte.is_ascii_digit());
    digits.then(|| text.parse().ok()).flatten()
}

/// The verdict of the family allowlist `list` on `target` (decisions
/// CH6-3, CH6-4, CH69 rules 2 and 8).
pub async fn decide(list: &FamilyEgress, target: &Target) -> CheckReply {
    let listed = list
        .hosts
        .iter()
        .any(|entry| entry.host == target.host && entry.port == target.port);
    if !listed {
        return refused(CheckReason::NotListed);
    }
    match lookup_host((target.host.as_str(), target.port.get())).await {
        Ok(records) => judge(records),
        Err(_) => refused(CheckReason::NoAddress),
    }
}

/// The verdict on the resolved `records` of one name (decision CH69 rule
/// 8). One record that is not public refuses the whole name, because one
/// private record is enough to show a rebinding name.
pub fn judge(records: impl IntoIterator<Item = SocketAddr>) -> CheckReply {
    let mut addresses: Vec<IpAddr> = Vec::new();
    for record in records {
        let address = record.ip().to_canonical();
        if !is_public(address) {
            return refused(CheckReason::NotPublic);
        }
        if !addresses.contains(&address) {
            addresses.push(address);
        }
    }
    if addresses.is_empty() {
        return refused(CheckReason::NoAddress);
    }
    CheckReply::Allowed(CheckAllowed {
        addresses: addresses.into_iter().map(IpAddress::from).collect(),
    })
}

/// The refusal of `reason`.
fn refused(reason: CheckReason) -> CheckReply {
    CheckReply::Refused(CheckRefused { reason })
}

/// Whether `address` is public (decision CH69 rule 8). The caller maps an
/// IPv4-mapped IPv6 address to IPv4 first.
#[must_use]
pub fn is_public(address: IpAddr) -> bool {
    match address {
        IpAddr::V4(address) => is_public_v4(address),
        IpAddr::V6(address) => is_public_v6(address),
    }
}

/// The IPv4 ranges that are not public: each special-purpose range of RFC
/// 6890, plus 100.64.0.0/10 (shared space), 0.0.0.0/8, 224.0.0.0/4
/// (multicast) and 240.0.0.0/4, which holds 255.255.255.255 (decision CH69
/// rule 8).
const NOT_PUBLIC_V4: [(Ipv4Addr, u8); 15] = [
    (Ipv4Addr::new(0, 0, 0, 0), 8),
    (Ipv4Addr::new(10, 0, 0, 0), 8),
    (Ipv4Addr::new(100, 64, 0, 0), 10),
    (Ipv4Addr::new(127, 0, 0, 0), 8),
    (Ipv4Addr::new(169, 254, 0, 0), 16),
    (Ipv4Addr::new(172, 16, 0, 0), 12),
    (Ipv4Addr::new(192, 0, 0, 0), 24),
    (Ipv4Addr::new(192, 0, 2, 0), 24),
    (Ipv4Addr::new(192, 88, 99, 0), 24),
    (Ipv4Addr::new(192, 168, 0, 0), 16),
    (Ipv4Addr::new(198, 18, 0, 0), 15),
    (Ipv4Addr::new(198, 51, 100, 0), 24),
    (Ipv4Addr::new(203, 0, 113, 0), 24),
    (Ipv4Addr::new(224, 0, 0, 0), 4),
    (Ipv4Addr::new(240, 0, 0, 0), 4),
];

/// Whether an IPv4 address is in no range of `NOT_PUBLIC_V4`.
fn is_public_v4(address: Ipv4Addr) -> bool {
    let bits = u32::from(address);
    NOT_PUBLIC_V4.iter().all(|&(network, length)| {
        let mask = u32::MAX.checked_shl(32 - u32::from(length)).unwrap_or(0);
        bits & mask != u32::from(network)
    })
}

/// Whether an IPv6 address is public: in 2000::/3, and in neither
/// 2001:db8::/32 (documentation) nor 2001::/23 (the IETF special block)
/// (decision CH69 rule 8). Each other IPv6 address is not public, so
/// loopback, link-local, unique-local, multicast and NAT64 are refused.
fn is_public_v6(address: Ipv6Addr) -> bool {
    let bits = u128::from(address);
    let within = |network: u128, length: u32| {
        let mask = u128::MAX.checked_shl(128 - length).unwrap_or(0);
        bits & mask == network
    };
    within(0x2000_u128 << 112, 3)
        && !within(0x2001_0db8_u128 << 96, 32)
        && !within(0x2001_u128 << 112, 23)
}
