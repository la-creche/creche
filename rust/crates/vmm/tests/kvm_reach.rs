//! The kvm_ tests of the edit follow (plan PR E3, decisions CH49 rules 3
//! and 4, CH69 rules 11 and 28). They are the spike of CH49 rule 4: does
//! Cloud Hypervisor see a host listener that the vmm binds after a boot,
//! and after a restore? The follow assumes that CH connects each guest
//! dial to `vsock.sock_<port>` at the dial (decision CH24), so it binds
//! 1104 on a running VM. A red test here means the fallback of CH49 rule
//! 4: the bound set fixed at each boot and restore.
//!
//! Each test boots `examples/phase3_vm.rs` with the helpers of
//! `tests/common/mod.rs`, the real guest init and the stand-in facade in
//! mode `reach` (plan PR A1). The family has no `egress.json` at the
//! start. A 1201 link sends `dial` lines, so the stand-in opens guest TCP
//! to 127.0.0.1:3128, which guest init splices to host port 1104
//! (decision CH46 rule 1). The CONNECT names an IP literal, which the
//! proxy refuses with 403 before any lookup (decision CH6-4), so no test
//! needs the internet. With no 1104 listener the guest dial fails, and
//! guest init closes the stream with no reply (decision CH49 rule 3).
//!
//! `#[ignore]`: only the job `kvm` runs them, after the setup script, as
//! `tests/kvm_guest.rs` says.
#![cfg(target_os = "linux")]

mod common;

use std::fs;
use std::io::{BufRead as _, BufReader, Write as _};
use std::os::unix::net::UnixStream;
use std::path::{Path, PathBuf};
use std::thread::sleep;
use std::time::{Duration, Instant};

use creche_vmm_store::claim::CONSUMED;
use creche_wire::guest::EGRESS_PROXY_PORT;
use creche_wire::{FacadeState, GuestPort};

use common::{BOOT, Booted, FAMILY, UP, boot, family_root, lines, stub_plane, work};

/// A CONNECT to an IP literal of TEST-NET-1 (RFC 5737).
const CONNECT: &[u8] = b"CONNECT 192.0.2.1:443 HTTP/1.1\r\nHost: 192.0.2.1:443\r\n\r\n";

/// The start of the first line of a refusal of the proxy.
const FORBIDDEN: &[u8] = b"HTTP/1.1 403 ";

/// An allowlist with one host, in the canonical form of caregiver
/// (decision CH69 rule 5).
const LIST: &str = r#"{"hosts":[{"host":"github.com","port":443}]}"#;

/// An allowlist with no host, which binds no 1104 (decision CH49 rule 3).
const EMPTY: &str = r#"{"hosts":[]}"#;

/// The pause between two tries of a CONNECT that waits for the follow.
const RETRY: Duration = Duration::from_millis(200);

// Each helper with `cfg(test)` may unwrap (rust/clippy.toml).

/// `bytes` in the lowercase hex of mode `reach`, or `-` for no bytes.
fn hex(bytes: &[u8]) -> String {
    if bytes.is_empty() {
        return "-".to_owned();
    }
    bytes.iter().map(|byte| format!("{byte:02x}")).collect()
}

/// A 1201 link to the stand-in in mode `reach`: `CONNECT 1201` on the
/// vsock socket of CH, as `AcpLink::dial` dials (decision CH13).
struct Link {
    reader: BufReader<UnixStream>,
    stream: UnixStream,
    next: usize,
}

#[cfg(test)]
impl Link {
    /// Opens the link on `vsock`. A fault fails the proof `step`.
    fn open(vsock: &Path, step: &str) -> Self {
        let stream = UnixStream::connect(vsock).unwrap();
        stream.set_read_timeout(Some(BOOT)).unwrap();
        writeln!(&stream, "CONNECT {}", GuestPort::Acp.number()).unwrap();
        let mut link = Self {
            reader: BufReader::new(stream.try_clone().unwrap()),
            stream,
            next: 0,
        };
        let ok = link.line();
        assert!(ok.starts_with("OK "), "{step}: CONNECT gave {ok:?}");
        link
    }

    /// One answer line. Only a line that ends in its LF counts.
    fn line(&mut self) -> String {
        let mut line = String::new();
        self.reader.read_line(&mut line).unwrap();
        assert!(line.ends_with('\n'), "the 1201 link ended: {line:?}");
        line.trim_end().to_owned()
    }

    /// Sends `CONNECT` on a new guest stream to 127.0.0.1:3128 and
    /// whether the first reply line is a 403 of the proxy. A stream that
    /// stays open is closed again, so the link holds none.
    fn refused(&mut self) -> bool {
        self.next += 1;
        let id = format!("c{}", self.next);
        writeln!(
            &self.stream,
            "dial {id} {EGRESS_PROXY_PORT} {}",
            hex(CONNECT)
        )
        .unwrap();
        let answer = self.line();
        let words: Vec<&str> = answer.split(' ').collect();
        if words.last() == Some(&"open") {
            writeln!(&self.stream, "close {id}").unwrap();
            assert_eq!(self.line(), format!("closed {id}"));
        }
        match words.as_slice() {
            ["reply", got, bytes, _] if *got == id => bytes.starts_with(&hex(FORBIDDEN)),
            ["failed", got, _] if *got == id => false,
            _ => panic!("no answer of mode reach: {answer:?}"),
        }
    }

    /// Tries a CONNECT every `RETRY` until its refusal is `refused`,
    /// within `BOOT`. The follow polls every 2 s (decision CH69 rule 11).
    fn until(&mut self, refused: bool, step: &str) {
        let deadline = Instant::now() + BOOT;
        while self.refused() != refused {
            assert!(Instant::now() < deadline, "{step}: no change in {BOOT:?}");
            sleep(RETRY);
        }
    }
}

/// The `egress.json` of the family of the proof `name`.
#[cfg(test)]
fn egress_file(name: &str) -> PathBuf {
    family_root(&work(name)).join(FAMILY).join("egress.json")
}

/// Boots the proof `name` with the stand-in in mode `reach` and stub
/// planes that no step reads.
#[cfg(test)]
fn boot_reach(name: &str) -> Booted {
    let (litellm, _) = stub_plane();
    let (chaperone, _) = stub_plane();
    boot(name, "reach", (litellm, chaperone))
}

/// After a cold boot with no egress list, a CONNECT gets no 403: no
/// listener exists. An edit that lists a host binds 1104 on the running
/// VM, and CH connects the next guest dial to it: the CONNECT gets 403
/// (decisions CH49 rules 3 and 4, CH69 rule 28). An edit that empties the
/// list removes it again.
#[test]
#[ignore = "needs /dev/kvm and bin/vm-smoke-setup.sh: the job kvm runs it"]
fn kvm_reach_binds_1104_after_an_egress_edit() {
    let name = "reach-boot";
    let mut booted = boot_reach(name);
    booted.vmm.start_vm("step 1 cold boot");

    // 2. No list, no 1104: guest init closes the stream with no reply.
    let mut link = Link::open(&booted.vsock, "step 2 no list");
    assert!(!link.refused(), "step 2 no list: a 403 with no listener");

    // 3. The follow binds 1104 after the edit, and CH finds it.
    fs::write(egress_file(name), LIST).unwrap();
    link.until(true, "step 3 edit after a boot (CH49 rule 4)");

    // 4. The follow removes 1104 after the list empties.
    fs::write(egress_file(name), EMPTY).unwrap();
    link.until(false, "step 4 emptied list (CH49 rule 3)");
    drop(link);
    drop(booted);
}

/// After a restore of a VM with no egress list, an edit that lists a host
/// binds 1104, and CH connects the next guest dial to it: the CONNECT
/// gets 403 (decisions CH49 rule 4, CH69 rule 28, plan risk 6).
#[test]
#[ignore = "needs /dev/kvm and bin/vm-smoke-setup.sh: the job kvm runs it"]
fn kvm_reach_binds_1104_after_a_restore_and_an_edit() {
    let name = "reach-restore";
    let mut booted = boot_reach(name);
    booted.vmm.start_vm("step 1 cold boot");

    // 2. The facade of mode `reach` opens its local link, so the sleep
    // gate passes (decision CH32 rule 6).
    let deadline = Instant::now() + BOOT;
    loop {
        let answer = booted.vmm.ask("facade");
        let facade = answer.strip_prefix("facade ").unwrap();
        if let Ok(FacadeState::Running(_)) = serde_json::from_str::<FacadeState>(facade) {
            break;
        }
        assert!(Instant::now() < deadline, "step 2 running: {answer}");
        sleep(Duration::from_millis(100));
    }

    // 3. Sleep, then a restore that claims the one generation. The new
    // serial log has no `up` line, which a cold boot writes.
    assert_eq!(booted.vmm.ask("sleep"), "slept 1", "step 3 sleep");
    let generations: Vec<PathBuf> = fs::read_dir(booted.session.snapshots())
        .unwrap()
        .map(|entry| entry.unwrap().path())
        .collect();
    assert_eq!(generations.len(), 1, "step 3 sleep: {generations:?}");
    booted.vmm.start_vm("step 3 restore");
    assert!(
        generations[0].join(CONSUMED).is_file(),
        "step 3 restore: no claim"
    );
    assert!(
        !lines(&booted.session.serial_log())
            .iter()
            .any(|line| line == UP),
        "step 3 restore: the VM booted"
    );

    // 4. No list, no 1104 after the restore.
    let mut link = Link::open(&booted.vsock, "step 4 no list");
    assert!(!link.refused(), "step 4 no list: a 403 with no listener");

    // 5. The follow binds 1104 after the edit, and the restored CH finds
    // it.
    fs::write(egress_file(name), LIST).unwrap();
    link.until(true, "step 5 edit after a restore (CH49 rule 4)");
    drop(link);
    drop(booted);
}
