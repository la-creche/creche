//! The fixed vsock port numbers between the vmm and the guest init
//! (decision CH14), and the number of the guest init protocol (decision
//! CH7-6).
//!
//! Only the vmm and the guest init read these values, so they have no serde
//! and no schema entry. A change of a port number needs a new
//! `GUEST_INIT_PROTOCOL`, so that a VM with an old snapshot cold-boots.

/// The number of the guest init protocol (decisions CH7-6, CH14, CH36). The vmm
/// and the guest init of one release share it. It is part of the snapshot
/// key: on a mismatch the vmm drops the snapshot and cold-boots. It is
/// separate from `HOST_API_PROTOCOL`.
pub const GUEST_INIT_PROTOCOL: u32 = 1;

/// Defines a port enum, its list `ALL` and the number of each port from one
/// table. A variant thus cannot miss its number or its place in `ALL`.
macro_rules! port_enum {
    (
        $(#[$meta:meta])*
        $name:ident { $($(#[$doc:meta])* $variant:ident = $number:literal,)+ }
    ) => {
        $(#[$meta])*
        #[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
        pub enum $name {
            $($(#[$doc])* $variant,)+
        }

        impl $name {
            /// Every port of this direction, in number order.
            pub const ALL: &[Self] = &[$(Self::$variant,)+];

            /// The vsock port number.
            #[must_use]
            pub const fn number(self) -> u32 {
                match self {
                    $(Self::$variant => $number,)+
                }
            }

            /// The port of `number`, or `None` for a number with no port.
            #[must_use]
            pub fn from_number(number: u32) -> Option<Self> {
                Self::ALL.iter().copied().find(|port| port.number() == number)
            }
        }
    };
}

port_enum! {
    /// A host port: the VM dials the vmm on it.
    ///
    /// 1105 is held for the spool of the creche git platform (decision
    /// CH3). It gets a variant in the change that first uses it.
    HostPort {
        /// The family state.
        FamilyState = 1100,
        /// The LiteLLM plane.
        Litellm = 1101,
        /// The chaperone plane.
        Chaperone = 1102,
        /// The git mirror, for families with a non-empty clone list
        /// `repos.json` (decision CH69 rule 13, as CH49 rule 3 for 1104).
        GitMirror = 1103,
        /// The egress proxy, for families with a non-empty egress list
        /// (today code and creche, decision CH49 rule 3).
        Egress = 1104,
    }
}

port_enum! {
    /// A guest port: the vmm sends `CONNECT` to the guest init on it
    /// (decision CH13).
    GuestPort {
        /// The control link. It carries the guest init handshake.
        Control = 1200,
        /// One ACP link per door connection.
        Acp = 1201,
    }
}

#[cfg(test)]
mod tests {
    use std::collections::HashSet;

    use super::{GUEST_INIT_PROTOCOL, GuestPort, HostPort};

    // A change of a port number here needs a new GUEST_INIT_PROTOCOL in
    // the same change (decision CH14).
    #[test]
    fn each_number_is_pinned() {
        let host: Vec<_> = HostPort::ALL
            .iter()
            .map(|&port| (port, port.number()))
            .collect();
        let guest: Vec<_> = GuestPort::ALL
            .iter()
            .map(|&port| (port, port.number()))
            .collect();

        let host_pins = [
            (HostPort::FamilyState, 1100),
            (HostPort::Litellm, 1101),
            (HostPort::Chaperone, 1102),
            (HostPort::GitMirror, 1103),
            (HostPort::Egress, 1104),
        ];
        assert_eq!(host, host_pins);
        assert_eq!(guest, [(GuestPort::Control, 1200), (GuestPort::Acp, 1201)]);
        assert_eq!(GUEST_INIT_PROTOCOL, 1);
    }

    #[test]
    fn all_numbers_are_distinct() {
        let host = HostPort::ALL.iter().map(|port| port.number());
        let guest = GuestPort::ALL.iter().map(|port| port.number());
        let numbers: HashSet<u32> = host.chain(guest).collect();

        assert_eq!(numbers.len(), HostPort::ALL.len() + GuestPort::ALL.len());
    }

    #[test]
    fn from_number_inverts_number() {
        for &port in HostPort::ALL {
            assert_eq!(HostPort::from_number(port.number()), Some(port));
        }
        for &port in GuestPort::ALL {
            assert_eq!(GuestPort::from_number(port.number()), Some(port));
        }
    }

    #[test]
    fn a_number_with_no_port_is_none() {
        assert_eq!(HostPort::from_number(1105), None);
        assert_eq!(HostPort::from_number(1200), None);
        assert_eq!(GuestPort::from_number(1100), None);
        assert_eq!(GuestPort::from_number(1202), None);
    }
}
