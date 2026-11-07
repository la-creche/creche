//! The one egress decision of `egress::rule` (decisions CH6-3, CH6-4,
//! CH69 rules 2 and 8). No test reaches the network: a listed name
//! resolves through `/etc/hosts` (`localhost`), and `judge` takes
//! addresses that the test gives.

use std::net::{IpAddr, SocketAddr};

use creche_vmm::egress::rule::{Target, decide, is_public, judge};
use creche_wire::{CheckReason, CheckReply, FamilyEgress};
use tokio::runtime::Builder;

/// The allowlist of `hosts`, each `host:port`.
#[cfg(test)]
fn list(hosts: &[(&str, u16)]) -> FamilyEgress {
    let hosts: Vec<_> = hosts
        .iter()
        .map(|(host, port)| serde_json::json!({ "host": host, "port": port }))
        .collect();
    serde_json::from_value(serde_json::json!({ "hosts": hosts })).unwrap()
}

/// The verdict of `list` on `target`, through `Target::parse` and
/// `decide`, as the route `check` gives it.
#[cfg(test)]
fn verdict(list: &FamilyEgress, target: &str) -> CheckReply {
    let runtime = Builder::new_current_thread().enable_all().build().unwrap();
    match Target::parse(target) {
        Ok(target) => runtime.block_on(decide(list, &target)),
        Err(reason) => refused(reason),
    }
}

#[cfg(test)]
fn refused(reason: CheckReason) -> CheckReply {
    serde_json::from_value(serde_json::json!({ "refused": { "reason": reason } })).unwrap()
}

/// The verdict of `judge` on `addresses`, each on port 443.
#[cfg(test)]
fn judged(addresses: &[&str]) -> CheckReply {
    judge(
        addresses
            .iter()
            .map(|address| SocketAddr::new(address.parse().unwrap(), 443)),
    )
}

#[test]
fn an_ip_literal_target_is_refused() {
    let listed = list(&[("github.com", 443)]);
    for target in [
        "140.82.112.3:443",
        "140.82.112.3",
        "2606:50c0:8000::153",
        "2606:50c0:8000::153:443",
        "[2606:50c0:8000::153]:443",
        "[::1]",
        "[::ffff:127.0.0.1]:443",
        "::ffff:127.0.0.1",
        // inet_aton forms: getaddrinfo reads each as an IPv4 address.
        "2130706433:443",
        "0x7f.1:443",
        "127.1:443",
        "0X7F000001:443",
    ] {
        assert_eq!(
            Target::parse(target),
            Err(CheckReason::IpLiteral),
            "{target}"
        );
        assert_eq!(
            verdict(&listed, target),
            refused(CheckReason::IpLiteral),
            "{target}"
        );
    }
}

#[test]
fn a_target_that_is_no_host_and_port_is_refused() {
    for target in [
        "github.com",
        "github.com:",
        "github.com:0",
        "github.com:65536",
        "github.com:+443",
        "github.com:443 ",
        "github.com:4430000",
        ":443",
        "*.github.com:443",
        "git_hub.com:443",
        "https://github.com:443",
        "",
    ] {
        assert_eq!(
            Target::parse(target),
            Err(CheckReason::BadTarget),
            "{target}"
        );
    }
}

#[test]
fn a_trailing_dot_is_refused() {
    assert_eq!(
        Target::parse("github.com.:443"),
        Err(CheckReason::BadTarget)
    );
    assert!(Target::parse("github.com:443").is_ok());
}

/// The list check comes before any lookup, so this needs no network.
#[test]
fn an_unlisted_host_is_refused() {
    let listed = list(&[("github.com", 443)]);
    for target in ["pypi.org:443", "api.github.com:443", "github.co:443"] {
        assert_eq!(
            verdict(&listed, target),
            refused(CheckReason::NotListed),
            "{target}"
        );
    }
    assert_eq!(
        verdict(&list(&[]), "github.com:443"),
        refused(CheckReason::NotListed)
    );
}

#[test]
fn a_listed_host_on_another_port_is_refused() {
    let listed = list(&[("github.com", 443)]);
    for target in ["github.com:80", "github.com:8443", "github.com:22"] {
        assert_eq!(
            verdict(&listed, target),
            refused(CheckReason::NotListed),
            "{target}"
        );
    }
}

/// `localhost` resolves through `/etc/hosts` to loopback, with no network.
/// A listed name that resolves to a loopback address is refused whole.
#[test]
fn a_name_that_resolves_to_loopback_is_refused() {
    let listed = list(&[("localhost", 443)]);
    assert_eq!(
        verdict(&listed, "localhost:443"),
        refused(CheckReason::NotPublic)
    );
}

/// The target matches the list in lower case. A refusal of `not_public`
/// shows that the name passed the list.
#[test]
fn the_host_is_matched_without_case() {
    let listed = list(&[("localhost", 443)]);
    for target in ["LOCALHOST:443", "LocalHost:443"] {
        assert_eq!(Target::parse(target), Target::parse("localhost:443"));
        assert_eq!(
            verdict(&listed, target),
            refused(CheckReason::NotPublic),
            "{target}"
        );
    }
}

#[test]
fn public_records_are_allowed_in_the_resolver_order() {
    let allowed = judged(&["140.82.112.3", "2606:50c0:8000::153", "140.82.112.3"]);
    let expected = serde_json::json!({"allowed": {"addresses": [
        "140.82.112.3",
        "2606:50c0:8000:0000:0000:0000:0000:0153",
    ]}});
    assert_eq!(serde_json::to_value(allowed).unwrap(), expected);
}

/// An IPv4-mapped IPv6 record is judged and written as its IPv4 address.
#[test]
fn a_mapped_record_is_judged_as_ipv4() {
    let allowed = judged(&["::ffff:140.82.112.3"]);
    let expected = serde_json::json!({"allowed": {"addresses": ["140.82.112.3"]}});
    assert_eq!(serde_json::to_value(allowed).unwrap(), expected);
    assert_eq!(
        judged(&["::ffff:127.0.0.1"]),
        refused(CheckReason::NotPublic)
    );
    assert_eq!(
        judged(&["::ffff:10.0.0.1"]),
        refused(CheckReason::NotPublic)
    );
}

/// One record that is not public refuses the whole name (decision CH69
/// rule 8), wherever it is in the order.
#[test]
fn one_private_record_refuses_the_name() {
    assert_eq!(
        judged(&["140.82.112.3", "10.0.0.39"]),
        refused(CheckReason::NotPublic)
    );
    assert_eq!(
        judged(&["fd00::1", "140.82.112.3"]),
        refused(CheckReason::NotPublic)
    );
    assert_eq!(judged(&[]), refused(CheckReason::NoAddress));
}

/// Each range of decision CH69 rule 8, at its first and last address, is
/// not public. The address just past each end is public, unless it falls
/// in the next range. One public address per family passes.
#[test]
fn each_special_range_is_not_public() {
    let not_public = [
        // IPv4: RFC 6890 and the ranges that CH69 rule 8 adds.
        "0.0.0.0",
        "0.255.255.255",
        "10.0.0.0",
        "10.255.255.255",
        "100.64.0.0",
        "100.127.255.255",
        "127.0.0.0",
        "127.255.255.255",
        "169.254.0.0",
        "169.254.255.255",
        "172.16.0.0",
        "172.31.255.255",
        "192.0.0.0",
        "192.0.0.255",
        "192.0.2.0",
        "192.0.2.255",
        "192.88.99.0",
        "192.88.99.255",
        "192.168.0.0",
        "192.168.255.255",
        "198.18.0.0",
        "198.19.255.255",
        "198.51.100.0",
        "198.51.100.255",
        "203.0.113.0",
        "203.0.113.255",
        "224.0.0.0",
        "239.255.255.255",
        "240.0.0.0",
        "255.255.255.255",
        // IPv6: all outside 2000::/3, and two blocks inside it.
        "::",
        "::1",
        "::ffff:0:1",
        "64:ff9b::808:808",
        "100::1",
        "1fff:ffff:ffff:ffff:ffff:ffff:ffff:ffff",
        "4000::",
        "fc00::1",
        "fd00::1",
        "fe80::1",
        "ff02::1",
        "2001::",
        "2001:1ff:ffff:ffff:ffff:ffff:ffff:ffff",
        "2001:db8::",
        "2001:db8:ffff:ffff:ffff:ffff:ffff:ffff",
    ];
    for address in not_public {
        let address: IpAddr = address.parse().unwrap();
        assert!(!is_public(address), "{address}");
    }
    let public = [
        "1.0.0.0",
        "9.255.255.255",
        "11.0.0.0",
        "100.63.255.255",
        "100.128.0.0",
        "126.255.255.255",
        "128.0.0.0",
        "169.253.255.255",
        "169.255.0.0",
        "172.15.255.255",
        "172.32.0.0",
        "192.0.1.0",
        "192.0.3.0",
        "192.88.98.255",
        "192.88.100.0",
        "192.167.255.255",
        "192.169.0.0",
        "198.17.255.255",
        "198.20.0.0",
        "198.51.99.255",
        "198.51.101.0",
        "203.0.112.255",
        "203.0.114.0",
        "223.255.255.255",
        "140.82.112.3",
        "2000::",
        "2001:200::",
        "2001:db7:ffff:ffff:ffff:ffff:ffff:ffff",
        "2001:db9::",
        "2606:50c0:8000::153",
        "3fff:ffff:ffff:ffff:ffff:ffff:ffff:ffff",
    ];
    for address in public {
        let address: IpAddr = address.parse().unwrap();
        assert!(is_public(address), "{address}");
    }
}
