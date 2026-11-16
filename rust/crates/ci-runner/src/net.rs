//! The network of one job VM, as its seed names it (design section 4.5).
//!
//! Root makes one bridge with the gateway address of a /24 and one tap
//! device per slot. Each slot has a MAC and a static IPv4 address in that
//! /24. The seed gives the guest a `network-config` that matches its NIC
//! by the MAC and sets that address, the default route through the
//! gateway and the DNS server.
//!
//! ```text
//!   BridgeNet      the gateway of the /24 of the bridge
//!     |
//!   GuestAddress   one host address of that /24, not the gateway
//!     |
//!   GuestNetwork   the MAC, the address and the DNS server of one VM
//! ```

use std::fmt;
use std::net::Ipv4Addr;
use std::str::FromStr;

/// The prefix length of the bridge net. Design section 4.5 gives the
/// bridge a /24.
pub const BRIDGE_PREFIX: u8 = 24;

/// The last octet of the network address of a /24.
const NETWORK_OCTET: u8 = 0;

/// The last octet of the broadcast address of a /24.
const BROADCAST_OCTET: u8 = 255;

/// The bit of the first MAC octet that marks a group address.
const MAC_GROUP_BIT: u8 = 0x01;

/// The bit of the first MAC octet that marks a locally administered
/// address.
const MAC_LOCAL_BIT: u8 = 0x02;

/// The length of a MAC as text: six pairs of hex digits and five colons.
const MAC_TEXT_LEN: usize = 17;

/// Why a value is not a part of the network of a job VM.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum NetError {
    /// The text is not six pairs of hex digits with a colon between each
    /// pair.
    MacText,
    /// The MAC is a group address, or it is not locally administered.
    MacKind,
    /// The address cannot be a host of a /24: an unspecified, loopback,
    /// link-local, multicast or broadcast address, or a host octet of 0
    /// or 255.
    NotHost(Ipv4Addr),
    /// The address is outside the /24 of the bridge.
    OutsideBridge(Ipv4Addr),
    /// The address is the gateway of the bridge.
    Gateway(Ipv4Addr),
}

impl fmt::Display for NetError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::MacText => formatter
                .write_str("a MAC is six pairs of hex digits with a colon between each pair"),
            Self::MacKind => formatter.write_str("a guest MAC is unicast and locally administered"),
            Self::NotHost(address) => write!(formatter, "{address} is not a host address of a /24"),
            Self::OutsideBridge(address) => {
                write!(formatter, "{address} is outside the /24 of the bridge")
            }
            Self::Gateway(address) => write!(formatter, "{address} is the gateway of the bridge"),
        }
    }
}

impl std::error::Error for NetError {}

/// Whether `address` can be a host of a /24.
fn is_host(address: Ipv4Addr) -> bool {
    let [first, .., last] = address.octets();

    first != 0
        && !address.is_loopback()
        && !address.is_link_local()
        && !address.is_multicast()
        && !address.is_broadcast()
        && last != NETWORK_OCTET
        && last != BROADCAST_OCTET
}

/// The MAC of the NIC of one job VM. It is unicast and locally
/// administered, so it is never the address of a real card. Cloud
/// Hypervisor takes it on `--net mac=`, and the `network-config` of the
/// seed matches the NIC by it.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub struct GuestMac([u8; 6]);

impl FromStr for GuestMac {
    type Err = NetError;

    /// Reads `52:54:00:c1:00:02`. Each hex digit can be upper or lower
    /// case.
    fn from_str(text: &str) -> Result<Self, Self::Err> {
        if text.len() != MAC_TEXT_LEN {
            return Err(NetError::MacText);
        }

        let mut octets = [0_u8; 6];
        let mut pairs = text.split(':');
        for octet in &mut octets {
            let pair = pairs.next().ok_or(NetError::MacText)?;
            if pair.len() != 2 || !pair.bytes().all(|byte| byte.is_ascii_hexdigit()) {
                return Err(NetError::MacText);
            }
            *octet = u8::from_str_radix(pair, 16).map_err(|_| NetError::MacText)?;
        }
        if pairs.next().is_some() {
            return Err(NetError::MacText);
        }

        let [first, ..] = octets;
        if first & MAC_GROUP_BIT != 0 || first & MAC_LOCAL_BIT == 0 {
            return Err(NetError::MacKind);
        }
        Ok(Self(octets))
    }
}

impl fmt::Display for GuestMac {
    /// Writes lowercase hex, as cloud-init and Cloud Hypervisor print it.
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        let [a, b, c, d, e, f] = self.0;
        write!(formatter, "{a:02x}:{b:02x}:{c:02x}:{d:02x}:{e:02x}:{f:02x}")
    }
}

/// The bridge net: the gateway address of the bridge, in a /24. Each job
/// VM routes through the gateway.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct BridgeNet {
    /// The address of the bridge.
    gateway: Ipv4Addr,
}

impl BridgeNet {
    /// The bridge net with the gateway `gateway`.
    ///
    /// # Errors
    ///
    /// `NetError::NotHost` when `gateway` cannot be a host of a /24.
    pub fn new(gateway: Ipv4Addr) -> Result<Self, NetError> {
        if !is_host(gateway) {
            return Err(NetError::NotHost(gateway));
        }
        Ok(Self { gateway })
    }

    /// The address of the bridge.
    #[must_use]
    pub fn gateway(self) -> Ipv4Addr {
        self.gateway
    }

    /// `address` as the address of one job VM in this net.
    ///
    /// # Errors
    ///
    /// `NetError::NotHost` when `address` cannot be a host of a /24,
    /// `NetError::OutsideBridge` when it is in another /24, and
    /// `NetError::Gateway` when it is the gateway.
    pub fn guest(self, address: Ipv4Addr) -> Result<GuestAddress, NetError> {
        if !is_host(address) {
            return Err(NetError::NotHost(address));
        }

        let [a, b, c, _] = address.octets();
        let [x, y, z, _] = self.gateway.octets();
        if [a, b, c] != [x, y, z] {
            return Err(NetError::OutsideBridge(address));
        }
        if address == self.gateway {
            return Err(NetError::Gateway(address));
        }
        Ok(GuestAddress {
            bridge: self,
            address,
        })
    }
}

/// The static address of one job VM: a host of the /24 of the bridge, and
/// not the gateway. Only `BridgeNet::guest` makes one.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct GuestAddress {
    /// The net of the address.
    bridge: BridgeNet,
    /// The address.
    address: Ipv4Addr,
}

impl GuestAddress {
    /// The address.
    #[must_use]
    pub fn address(self) -> Ipv4Addr {
        self.address
    }

    /// The gateway of the bridge, for the default route of the VM.
    #[must_use]
    pub fn gateway(self) -> Ipv4Addr {
        self.bridge.gateway
    }
}

impl fmt::Display for GuestAddress {
    /// Writes the address with its prefix, for example `192.0.2.2/24`.
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(formatter, "{}/{BRIDGE_PREFIX}", self.address)
    }
}

/// The network of one job VM: the MAC of its NIC, its address and its
/// DNS server.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct GuestNetwork {
    /// The MAC of the NIC.
    mac: GuestMac,
    /// The static address and the gateway.
    address: GuestAddress,
    /// The DNS server. Design section 4.5 lets each VM reach port 53 of
    /// one LAN DNS server.
    dns: Ipv4Addr,
}

impl GuestNetwork {
    /// The network of a VM with the NIC `mac`, the address `address` and
    /// the DNS server `dns`.
    ///
    /// # Errors
    ///
    /// `NetError::NotHost` when `dns` cannot be the address of a host.
    pub fn new(mac: GuestMac, address: GuestAddress, dns: Ipv4Addr) -> Result<Self, NetError> {
        if !is_host(dns) {
            return Err(NetError::NotHost(dns));
        }
        Ok(Self { mac, address, dns })
    }

    /// The MAC of the NIC.
    #[must_use]
    pub fn mac(self) -> GuestMac {
        self.mac
    }

    /// The static address and the gateway.
    #[must_use]
    pub fn address(self) -> GuestAddress {
        self.address
    }

    /// The DNS server.
    #[must_use]
    pub fn dns(self) -> Ipv4Addr {
        self.dns
    }
}

#[cfg(test)]
mod tests {
    use std::net::Ipv4Addr;

    use super::{BridgeNet, GuestMac, GuestNetwork, NetError};

    #[test]
    fn a_mac_is_unicast_and_locally_administered() {
        let mac: GuestMac = "52:54:00:C1:00:02".parse().unwrap();
        assert_eq!(mac.to_string(), "52:54:00:c1:00:02");

        for text in [
            "",
            "52:54:00:c1:00",
            "52:54:00:c1:00:02:03",
            "52-54-00-c1-00-02",
            "52:54:00:c1:00:0g",
            "52:54:00:c1:0:002",
            "+2:54:00:c1:00:02",
            "52:54:00:c1:00:02\n",
        ] {
            assert_eq!(text.parse::<GuestMac>(), Err(NetError::MacText), "{text:?}");
        }

        // 53: a group address. 50: a universal address of a vendor.
        for text in [
            "53:54:00:c1:00:02",
            "50:54:00:c1:00:02",
            "00:16:3e:00:00:01",
        ] {
            assert_eq!(text.parse::<GuestMac>(), Err(NetError::MacKind), "{text}");
        }
    }

    #[test]
    fn a_guest_address_is_a_host_of_the_bridge_net_and_not_its_gateway() {
        let bridge = BridgeNet::new(Ipv4Addr::new(192, 0, 2, 1)).unwrap();
        let guest = bridge.guest(Ipv4Addr::new(192, 0, 2, 2)).unwrap();
        assert_eq!(guest.to_string(), "192.0.2.2/24");
        assert_eq!(guest.gateway(), Ipv4Addr::new(192, 0, 2, 1));

        for (address, fault) in [
            (
                Ipv4Addr::new(192, 0, 3, 2),
                NetError::OutsideBridge(Ipv4Addr::new(192, 0, 3, 2)),
            ),
            (
                Ipv4Addr::new(192, 0, 2, 1),
                NetError::Gateway(Ipv4Addr::new(192, 0, 2, 1)),
            ),
            (
                Ipv4Addr::new(192, 0, 2, 0),
                NetError::NotHost(Ipv4Addr::new(192, 0, 2, 0)),
            ),
            (
                Ipv4Addr::new(192, 0, 2, 255),
                NetError::NotHost(Ipv4Addr::new(192, 0, 2, 255)),
            ),
        ] {
            assert_eq!(bridge.guest(address), Err(fault), "{address}");
        }
    }

    #[test]
    fn a_gateway_and_a_dns_server_are_host_addresses() {
        for address in [
            Ipv4Addr::UNSPECIFIED,
            Ipv4Addr::LOCALHOST,
            Ipv4Addr::BROADCAST,
            Ipv4Addr::new(169, 254, 0, 1),
            Ipv4Addr::new(224, 0, 0, 1),
            Ipv4Addr::new(10, 0, 0, 0),
            Ipv4Addr::new(0, 1, 2, 3),
        ] {
            assert_eq!(
                BridgeNet::new(address),
                Err(NetError::NotHost(address)),
                "{address}"
            );
        }

        let bridge = BridgeNet::new(Ipv4Addr::new(192, 0, 2, 1)).unwrap();
        let guest = bridge.guest(Ipv4Addr::new(192, 0, 2, 2)).unwrap();
        let mac = "52:54:00:c1:00:02".parse().unwrap();
        assert!(GuestNetwork::new(mac, guest, Ipv4Addr::new(198, 51, 100, 53)).is_ok());
        assert_eq!(
            GuestNetwork::new(mac, guest, Ipv4Addr::LOCALHOST),
            Err(NetError::NotHost(Ipv4Addr::LOCALHOST))
        );
    }
}
