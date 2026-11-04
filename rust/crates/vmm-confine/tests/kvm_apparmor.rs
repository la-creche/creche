//! The AppArmor profile of CH, in force (decisions CH8-1 and CH24, plan
//! risk 5). `bin/vm-smoke-setup.sh` installs CH at the path that the
//! profile attaches to and loads the profile. Each test starts CH there
//! through `setpriv --no-new-privs`, the state that the Landlock launch
//! step leaves, so the attach under no_new_privs is proved too.
//!
//! The boot test runs the busybox guest under the profile with the VM run
//! dir of plan PR D1, and its API socket serves. The connect tests give
//! CH `--tpm socket=<path>`: CH connects there while it makes the VM, with
//! no guest program and no shared memory. A test listener on a
//! `vsock.sock_<port>` path gets the connect. A test `vmm.sock` gets none,
//! and the user bus refuses CH with EACCES.
#![cfg(target_os = "linux")]

mod kvm;

use std::fs;
use std::io::{ErrorKind, Read};
use std::os::unix::net::UnixListener;
use std::path::{Path, PathBuf};
use std::process::{Command, ExitStatus, Stdio};
use std::thread::sleep;
use std::time::{Duration, Instant};

use creche_vmm_store::layout::{RunRoot, StateRoot, VmRunDir};
use creche_wire::HostPort;

use kvm::{BOOT, Vm, booted, files};

/// The install path that the profile attaches to (gap G28).
const INSTALLED: &str = "/opt/components/vmm/bin/cloud-hypervisor";

/// The label of a process under the profile, in enforce mode.
const LABEL: &str = "creche-cloud-hypervisor (enforce)";

/// util-linux on the runner. It sets no_new_privs, then execs CH.
const SETPRIV: &str = "/usr/bin/setpriv";

// Each helper with `cfg(test)` may unwrap (rust/clippy.toml).

/// One VM: its run dir under the real run root `/run/user/<uid>/vmm`
/// (decision CH20), and a state root with the kernel and the initramfs in
/// its store. The run dir must not exist before, so a drop removes only
/// the two dirs that the setup made.
struct Setup {
    run: RunRoot,
    vm: VmRunDir,
    state: PathBuf,
    kernel: PathBuf,
    initramfs: PathBuf,
}

#[cfg(test)]
impl Setup {
    fn new(name: &str, id: &str) -> Self {
        let files = files();
        let uid = rustix::process::getuid().as_raw();
        let run = RunRoot::new(PathBuf::from(format!("/run/user/{uid}/vmm"))).unwrap();
        let vm = run.vm(&id.parse().unwrap());
        absent(vm.path());
        fs::create_dir_all(vm.path()).unwrap();
        let state = files.join(format!("apparmor-{name}-{}", std::process::id()));
        let _ = fs::remove_dir_all(&state);
        let store = StateRoot::new(state.clone()).unwrap().store_dir();
        fs::create_dir_all(&store).unwrap();
        // The store names each file by its SHA-256 (fact 56). The profile
        // matches the name only, so a fixed name stands in.
        let kernel = store.join(format!("{:064}", 1));
        let initramfs = store.join(format!("{:064}", 2));
        fs::copy(files.join("vmlinux"), &kernel).unwrap();
        fs::copy(files.join("initramfs.cpio"), &initramfs).unwrap();
        Self {
            run,
            vm,
            state,
            kernel,
            initramfs,
        }
    }

    /// CH at the install path with the VM files of this setup.
    fn ch(&self) -> Command {
        let mut command = Command::new(SETPRIV);
        command
            .args(["--no-new-privs", INSTALLED])
            .arg("--api-socket")
            .arg(format!("path={}", self.vm.api_socket().display()))
            .arg("--kernel")
            .arg(&self.kernel)
            .arg("--initramfs")
            .arg(&self.initramfs)
            .args(["--cmdline", "console=ttyS0 rdinit=/init"])
            .args(["--cpus", "boot=1", "--memory", "size=256M"])
            .arg("--vsock")
            .arg(format!("cid=3,socket={}", self.vm.vsock_socket().display()))
            .arg("--serial")
            .arg(format!("file={}", self.vm.serial_log().display()))
            .args(["--console", "off"])
            .stdin(Stdio::null())
            .stderr(Stdio::piped());
        command
    }

    /// Starts CH with a TPM at `socket` and waits for it to exit. Each
    /// connect that reaches `listener` is accepted and closed, so CH
    /// exits. The result says whether one came.
    fn tpm(&self, socket: &Path, listener: Option<&UnixListener>) -> (ExitStatus, String, bool) {
        let mut vm = Vm(self
            .ch()
            .arg("--tpm")
            .arg(format!("socket={}", socket.display()))
            .spawn()
            .unwrap());
        let accept = || {
            listener.is_some_and(|listener| match listener.accept() {
                Ok(_) => true,
                Err(error) if error.kind() == ErrorKind::WouldBlock => false,
                Err(error) => panic!("accept: {error}"),
            })
        };
        let deadline = Instant::now() + BOOT;
        let mut connected = false;
        let status = loop {
            connected |= accept();
            if let Some(status) = vm.0.try_wait().unwrap() {
                break status;
            }
            assert!(Instant::now() < deadline, "CH did not exit in {BOOT:?}");
            sleep(Duration::from_millis(20));
        };
        // A connect that CH made just before it exited waits in the
        // backlog.
        connected |= accept();
        let mut stderr = String::new();
        vm.0.stderr
            .take()
            .unwrap()
            .read_to_string(&mut stderr)
            .unwrap();
        (status, stderr, connected)
    }
}

impl Drop for Setup {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(self.vm.path());
        let _ = fs::remove_dir_all(&self.state);
    }
}

/// Fails if `path` exists. The tests use the real run root, where a live
/// vmm may own `vmm.sock` and its VM dirs, so a test removes only the
/// paths it made and never one it found.
#[cfg(test)]
fn absent(path: &Path) {
    assert!(
        fs::symlink_metadata(path).is_err(),
        "{} exists: a vmm may be live, so the test stops",
        path.display()
    );
}

/// A listener at a new path, which does not block on accept. A drop
/// removes the path.
struct Listener(UnixListener, PathBuf);

impl Drop for Listener {
    fn drop(&mut self) {
        let _ = fs::remove_file(&self.1);
    }
}

#[cfg(test)]
fn listen(path: &Path) -> Listener {
    absent(path);
    let listener = UnixListener::bind(path).unwrap();
    listener.set_nonblocking(true).unwrap();
    Listener(listener, path.to_owned())
}

#[test]
#[ignore = "needs /dev/kvm and bin/vm-smoke-setup.sh: the job kvm runs it"]
fn kvm_ch_boots_under_its_apparmor_profile() {
    let setup = Setup::new("boot", "0123abcd-0000-4000-8000-0000000c15b0");
    let mut vm = Vm(setup.ch().spawn().unwrap());
    let serial = setup.vm.serial_log();
    let deadline = Instant::now() + BOOT;
    while !booted(&serial) {
        if let Some(status) = vm.0.try_wait().unwrap() {
            let mut stderr = String::new();
            let _ = vm.0.stderr.take().unwrap().read_to_string(&mut stderr);
            panic!("CH exited with {status} before the marker. Stderr:\n{stderr}");
        }
        assert!(Instant::now() < deadline, "no marker in {BOOT:?}");
        sleep(Duration::from_millis(20));
    }

    let attr = format!("/proc/{}/attr/apparmor/current", vm.0.id());
    assert_eq!(fs::read_to_string(attr).unwrap().trim_end(), LABEL);
    let ping = Command::new(files().join("ch-remote"))
        .arg("--api-socket")
        .arg(setup.vm.api_socket())
        .arg("ping")
        .output()
        .unwrap();
    assert!(ping.status.success(), "ping: {ping:?}");
}

#[test]
#[ignore = "needs /dev/kvm and bin/vm-smoke-setup.sh: the job kvm runs it"]
fn kvm_ch_connects_to_a_host_listener() {
    let setup = Setup::new("listener", "0123abcd-0000-4000-8000-0000000c15b1");
    let path = setup.vm.host_listener(HostPort::Chaperone);
    let listener = listen(&path);
    let (status, stderr, connected) = setup.tpm(&path, Some(&listener.0));
    assert!(
        connected,
        "no connect to {}: {status}\n{stderr}",
        path.display()
    );
}

#[test]
#[ignore = "needs /dev/kvm and bin/vm-smoke-setup.sh: the job kvm runs it"]
fn kvm_ch_cannot_connect_to_vmm_sock() {
    let setup = Setup::new("vmm-sock", "0123abcd-0000-4000-8000-0000000c15b2");
    let path = setup.run.vmm_socket();
    let listener = listen(&path);
    let (status, stderr, connected) = setup.tpm(&path, Some(&listener.0));
    drop(listener);
    assert!(!connected, "CH reached {}", path.display());
    assert!(!status.success());
    assert!(stderr.contains("Permission denied"), "stderr:\n{stderr}");
}

#[test]
#[ignore = "needs /dev/kvm and bin/vm-smoke-setup.sh: the job kvm runs it"]
fn kvm_ch_cannot_connect_to_the_user_bus() {
    let setup = Setup::new("bus", "0123abcd-0000-4000-8000-0000000c15b3");
    let uid = rustix::process::getuid().as_raw();
    let bus = PathBuf::from(format!("/run/user/{uid}/bus"));
    let (status, stderr, _) = setup.tpm(&bus, None);
    assert!(!status.success());
    assert!(stderr.contains("Permission denied"), "stderr:\n{stderr}");
}
