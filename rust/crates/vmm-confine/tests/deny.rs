//! The proof of deny-by-default against fake command line files and temp
//! run directories (decisions CH6-3, CH8-1, CH14, fact 44).

use std::fs;
use std::path::PathBuf;

use creche_vmm_confine::deny::{EgressAssertFailed, check, cmdline_of};
use creche_vmm_confine::landlock::{HostPath, Start};
use creche_vmm_confine::launch::Program;
use creche_vmm_store::layout::{RunRoot, VmRunDir};
use creche_wire::HostPort;

const ID: &str = "0123abcd-0000-4000-8000-00000000c8c8";
const CH: &str = "/opt/components/vmm/bin/cloud-hypervisor";

/// The planes of phase 3 (decision CH14).
const PLANES: &[HostPort] = &[
    HostPort::FamilyState,
    HostPort::Litellm,
    HostPort::Chaperone,
];

// Each helper with `cfg(test)` may unwrap (rust/clippy.toml).

/// A command line file and a VM run directory in a new directory in
/// `/tmp`, removed on drop. The run directory holds the CH sockets and
/// the lock file of the API socket, which are not listeners.
struct Fake {
    root: PathBuf,
    run: VmRunDir,
}

#[cfg(test)]
impl Fake {
    fn new(name: &str, args: &[&str]) -> Self {
        Self::raw(name, &[&[CH], args].concat())
    }

    /// The fake with exactly `args` in the command line file.
    fn raw(name: &str, args: &[&str]) -> Self {
        let root = PathBuf::from(format!("/tmp/vmm-deny-{}-{name}", std::process::id()));
        let _ = fs::remove_dir_all(&root);
        let run = RunRoot::new(root.clone()).unwrap().vm(&ID.parse().unwrap());
        fs::create_dir_all(run.path()).unwrap();
        for file in [run.vsock_socket(), run.api_socket(), run.api_socket_lock()] {
            fs::write(file, "").unwrap();
        }
        let mut cmdline = Vec::new();
        for arg in args {
            cmdline.extend_from_slice(arg.as_bytes());
            cmdline.push(0);
        }
        fs::write(root.join("cmdline"), cmdline).unwrap();
        Self { root, run }
    }

    fn listen(&self, ports: &[HostPort]) {
        for &port in ports {
            fs::write(self.run.host_listener(port), "").unwrap();
        }
    }

    fn check(&self, start: &Start, bound: &[HostPort]) -> Result<(), EgressAssertFailed> {
        check(&self.root.join("cmdline"), &ch(), start, &self.run, bound)
    }
}

impl Drop for Fake {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.root);
    }
}

#[cfg(test)]
fn ch() -> Program {
    Program::new(PathBuf::from(CH)).unwrap()
}

#[cfg(test)]
fn host(path: &str) -> HostPath {
    HostPath::new(PathBuf::from(path)).unwrap()
}

/// A boot of the store kernel and the guest init initramfs (decision CH62
/// rule 18).
#[cfg(test)]
fn boot() -> Start {
    Start::Boot {
        kernel: host("/state/store/kernel"),
        initramfs: host("/state/store/initramfs"),
    }
}

/// A restore that carries the initramfs of its snapshot (decision CH61
/// rule 3).
#[cfg(test)]
fn restore() -> Start {
    Start::Restore {
        initramfs: host("/state/store/initramfs"),
    }
}

const BOOT: &[&str] = &[
    "--cpus",
    "boot=2",
    "--vsock",
    "cid=3,socket=/run/v",
    "--api-socket",
    "path=/run/a",
];
const RESTORE: &[&str] = &[
    "--api-socket",
    "path=/run/a",
    "--restore",
    "source_url=file:///s",
];

#[test]
fn an_empty_bound_set_with_no_listener_passes() {
    let fake = Fake::new("empty", BOOT);
    fake.check(&boot(), &[]).unwrap();
}

#[test]
fn a_restore_with_no_vsock_argument_passes() {
    let fake = Fake::new("restore", RESTORE);
    fake.check(&restore(), &[]).unwrap();
}

#[test]
fn every_bound_port_with_its_listener_passes() {
    let fake = Fake::new("planes", BOOT);
    fake.listen(PLANES);
    fake.check(&boot(), PLANES).unwrap();
}

#[test]
fn an_extra_listener_fails() {
    let fake = Fake::new("extra", BOOT);
    fake.listen(&[HostPort::Egress]);
    match fake.check(&boot(), &[]) {
        Err(EgressAssertFailed::Extra(name)) => assert_eq!(name, "vsock.sock_1104"),
        other => panic!("expected Extra, got {other:?}"),
    }
}

#[test]
fn a_listener_of_a_number_with_no_port_fails() {
    let fake = Fake::new("unknown", BOOT);
    fs::write(fake.run.path().join("vsock.sock_1105"), "").unwrap();
    assert!(matches!(
        fake.check(&boot(), &[]),
        Err(EgressAssertFailed::Extra(_))
    ));
}

#[test]
fn a_bound_port_with_no_listener_fails() {
    let fake = Fake::new("missing", BOOT);
    fake.listen(&[HostPort::FamilyState, HostPort::Litellm]);
    match fake.check(&boot(), PLANES) {
        Err(EgressAssertFailed::Missing(port)) => assert_eq!(port, HostPort::Chaperone),
        other => panic!("expected Missing, got {other:?}"),
    }
}

#[test]
fn a_net_argument_fails_in_each_form() {
    for (name, net) in [("net", "--net"), ("net-eq", "--net=tap=")] {
        let args = [BOOT, &[net, "fd=3"]].concat();
        let fake = Fake::new(name, &args);
        assert!(matches!(
            fake.check(&boot(), &[]),
            Err(EgressAssertFailed::Net)
        ));
    }
}

#[test]
fn a_boot_needs_exactly_one_vsock() {
    let two = Fake::new("two", &[BOOT, &["--vsock=cid=4,socket=/run/w"]].concat());
    assert!(matches!(
        two.check(&boot(), &[]),
        Err(EgressAssertFailed::Vsock(2))
    ));
    let none = Fake::new("none", RESTORE);
    assert!(matches!(
        none.check(&boot(), &[]),
        Err(EgressAssertFailed::Vsock(0))
    ));
}

#[test]
fn a_restore_with_a_vsock_argument_fails() {
    let fake = Fake::new("restore-vsock", &[RESTORE, &["--vsock", "cid=3"]].concat());
    assert!(matches!(
        fake.check(&restore(), &[]),
        Err(EgressAssertFailed::Vsock(1))
    ));
}

#[test]
fn a_missing_cmdline_or_run_dir_fails() {
    let fake = Fake::new("gone", BOOT);
    let missing = PathBuf::from("/nonexistent/cmdline");
    let result = check(&missing, &ch(), &boot(), &fake.run, &[]);
    assert!(matches!(result, Err(EgressAssertFailed::Cmdline(_))));
    fs::remove_dir_all(fake.run.path()).unwrap();
    assert!(matches!(
        fake.check(&boot(), &[]),
        Err(EgressAssertFailed::RunDir(_))
    ));
}

#[test]
fn the_cmdline_of_a_pid_is_under_proc() {
    assert_eq!(cmdline_of(4242), PathBuf::from("/proc/4242/cmdline"));
}

#[test]
fn a_process_that_is_not_ch_fails() {
    let systemd_run = [
        &["/usr/bin/systemd-run", "--user", "--scope", "--", CH][..],
        RESTORE,
    ]
    .concat();
    let cases: [(&str, &[&str]); 4] = [
        // A zombie CH: its command line reads as empty.
        ("zombie", &[]),
        ("launch", &["/proc/self/exe", "launch"]),
        ("systemd-run", &systemd_run),
        ("other", &[&format!("{CH}x"), "--vsock", "cid=3"]),
    ];
    for (name, args) in cases {
        let fake = Fake::raw(name, args);
        for start in [boot(), restore()] {
            let result = fake.check(&start, &[]);
            assert!(
                matches!(result, Err(EgressAssertFailed::NotCh)),
                "{name}: {result:?}"
            );
        }
    }
}
