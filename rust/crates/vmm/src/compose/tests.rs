//! The compose step on temp paths with no KVM (plan PR L20). `build`
//! trusts only a root-owned `digests.json` (decision CH57 rule 1). A test
//! of the rust job runs as the runner user and cannot make a root-owned
//! file, so the rust job proves that the file of the test refuses the
//! start before any import. The serve tests there start from `parts`,
//! with the checks of `check` done by the test: the CH version check of
//! the fixture and an import into the store. The `kvm_` tests run `build`
//! itself on a root-owned file: the job kvm runs each ignored `kvm_` test,
//! and its runner may use sudo (decision CH36 G20).
//!
//! Each program that a part only holds gets its own path, and a test
//! reads the `Debug` text of the part. The store crate gives no accessor
//! of these paths, and only this test needs one.

use std::collections::HashMap;
use std::ffi::OsString;
use std::fs;
use std::future::pending;
use std::io::{Read, Write};
use std::os::unix::fs::{MetadataExt, PermissionsExt};
use std::os::unix::net::UnixStream;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::thread::sleep;
use std::time::{Duration, Instant};

use creche_vmm_confine::admission::GIB;
use creche_vmm_store::images::ImageStore;
use creche_vmm_store::key::check_cloud_hypervisor;
use creche_wire::{HOST_API_HEADER, HOST_API_PROTOCOL, PerFlavor, SandboxFlavor, SessionPage};
use tokio::runtime::{Builder, Runtime};
use tokio::sync::oneshot;

use super::{Checked, StartError, VERSION_WAIT, Vmm, build, parts};
use crate::ServeError;
use crate::config::Config;
use crate::image_root::{DIGESTS_FILE, DigestsError};
use crate::start::Digests;

/// The CH fixture of the version check (gap G10).
const CH: &str = concat!(
    env!("CARGO_MANIFEST_DIR"),
    "/tests/fixtures/cloud-hypervisor"
);

/// The longest wait of one step.
const WAIT: Duration = Duration::from_secs(10);

/// The frozen mismatch reply. Never change it to make a change pass.
const MISMATCH: &str = r#"{"code":"protocol_mismatch","protocol":1}"#;

/// The kernel, the base disk and the python disk, each with the SHA-256
/// of its bytes.
const IMAGES: [(&str, &str, &str); 3] = [
    (
        "vmlinux",
        "",
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    ),
    (
        "base.ext4",
        "abc",
        "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
    ),
    (
        "python.ext4",
        "hello world",
        "b94d27b9934d3e08a52e52d7da7dabfac484efe37a5380ee9088f7ace2efcde9",
    ),
];

/// A scratch dir under `/tmp` that its drop removes, also after a failed
/// assert. `/tmp`, not `temp_dir`: the run root bounds the socket paths
/// under it (`SOCKET_PATH_MAX`), and the temp dir of macOS is too long.
struct Scratch {
    dir: PathBuf,
    env: HashMap<&'static str, OsString>,
}

impl Drop for Scratch {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.dir);
    }
}

impl Scratch {
    /// The dirs, the images, a `digests.json` that the test user owns, and a
    /// `systemctl` that logs each call and lists no unit.
    fn new(name: &str) -> Self {
        let dir = Path::new("/tmp").join(format!("creche-vmm-{}-{name}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        for sub in ["state", "run", "families", "faults", "images", "bin"] {
            fs::create_dir_all(dir.join(sub)).unwrap();
        }
        for (file, bytes, _) in IMAGES {
            fs::write(dir.join("images").join(file), bytes).unwrap();
        }
        let entry = |(file, _, sha256): (&str, &str, &str)| serde_json::json!({ "file": file, "sha256": sha256 });
        let digests = serde_json::json!({
            "kernel": entry(IMAGES[0]),
            "base": entry(IMAGES[1]),
            "python": entry(IMAGES[2]),
        });
        fs::write(dir.join("images").join(DIGESTS_FILE), digests.to_string()).unwrap();

        let systemctl = dir.join("bin/systemctl");
        let log = dir.join("systemctl.log");
        let script = format!("#!/bin/sh\nprintf '%s\\n' \"$*\" >> '{}'\n", log.display());
        fs::write(&systemctl, script).unwrap();
        fs::set_permissions(&systemctl, fs::Permissions::from_mode(0o755)).unwrap();

        let text = |path: PathBuf| OsString::from(path);
        let env = HashMap::from([
            ("VMM_STATE_ROOT", text(dir.join("state"))),
            ("RUNTIME_DIRECTORY", text(dir.join("run"))),
            ("VMM_FAMILY_ROOT", text(dir.join("families"))),
            ("VMM_FAULTS_ROOT", text(dir.join("faults"))),
            ("VMM_IMAGE_ROOT", text(dir.join("images"))),
            ("VMM_CH_PROGRAM", OsString::from(CH)),
            ("VMM_CH_REMOTE_PROGRAM", text(dir.join("bin/ch-remote"))),
            (
                "AGENT_SYSTEMD_RUN_PROGRAM",
                text(dir.join("bin/systemd-run")),
            ),
            ("AGENT_SYSTEMCTL_PROGRAM", text(systemctl)),
            ("VMM_ZSTD_PROGRAM", text(dir.join("bin/zstd"))),
            ("VMM_MKE2FS_PROGRAM", text(dir.join("bin/mke2fs"))),
            ("VMM_FALLOCATE_PROGRAM", text(dir.join("bin/fallocate"))),
            ("VMM_RAM_CAP_GIB", OsString::from("80")),
            ("VMM_FREE_FLOOR_GIB", OsString::from("0")),
            ("VMM_SNAPSHOT_BUDGET_GIB", OsString::from("7")),
        ]);
        Self { dir, env }
    }

    fn config(&self) -> Config {
        Config::from_lookup(|key| self.env.get(key).cloned()).unwrap()
    }

    /// Makes root the owner of `digests.json`, as the disk row of phase 5
    /// installs it. Only a `kvm_` test calls it.
    fn trust(&self) {
        chown_root(&self.dir.join("images").join(DIGESTS_FILE));
    }

    /// The vmm from `parts`, after the checks of `check` that a test of
    /// the rust job can run: the CH version check of the fixture and an
    /// import of each image into the store of the state root.
    fn parts(&self) -> Vmm {
        let config = self.config();
        let ch_version = check_cloud_hypervisor(config.ch.path(), VERSION_WAIT).unwrap();
        let images = ImageStore::new(&config.state, config.zstd.path().to_owned());
        let [kernel, base, python] = IMAGES.map(|(file, _, sha256)| {
            let digest = sha256.parse().unwrap();
            images
                .import(&self.dir.join("images").join(file), digest)
                .unwrap();
            digest
        });
        let roots = PerFlavor::every(|flavor| match flavor {
            SandboxFlavor::Base => base,
            SandboxFlavor::Python => python,
        });
        let checked = Checked {
            ch_version,
            images,
            digests: Digests { kernel, roots },
        };
        parts(config, checked).unwrap()
    }

    /// The `Debug` text of the path of `bin/<program>`.
    fn program(&self, program: &str) -> String {
        format!("{:?}", self.dir.join("bin").join(program))
    }
}

/// Makes root the owner of `path` with the sudo of the runner. Decision
/// CH36 G20 allows sudo on GitHub's throwaway runners only, never on the
/// host, so it refuses outside GitHub Actions.
fn chown_root(path: &Path) {
    assert_eq!(
        std::env::var("GITHUB_ACTIONS").as_deref(),
        Ok("true"),
        "sudo runs only on a GitHub runner (decision CH36 G20)"
    );
    let status = Command::new("sudo")
        .args(["-n", "chown", "0:0"])
        .arg(path)
        .status()
        .unwrap();
    assert!(status.success(), "sudo chown: {status}");
}

fn runtime() -> Runtime {
    Builder::new_multi_thread()
        .enable_io()
        .enable_time()
        .build()
        .unwrap()
}

/// Each key reaches its part. A swapped program path, a skipped leftover
/// sweep, a faults dir on another root or the design budget in place of
/// the key each fail here.
#[test]
#[ignore = "needs runner sudo for a root-owned file: the job kvm runs it"]
fn kvm_build_wires_each_key_to_its_part() {
    let scratch = Scratch::new("wire");
    scratch.trust();
    let vmm = build(scratch.config()).unwrap();

    // Decision CH19 (4): the leftover sweep lists the VM scopes first.
    let log = fs::read_to_string(scratch.dir.join("systemctl.log")).unwrap();
    assert!(log.starts_with("--user list-units"), "{log}");
    // `FaultDir::open` makes the `vmm` dir of the faults root.
    assert!(scratch.dir.join("faults/vmm").is_dir());

    let (start, api) = vmm.hook.0.parts();
    let images = format!("{:?}", start.images);
    assert!(images.contains(&scratch.program("zstd")), "{images}");
    let disks = format!("{:?}", start.disks);
    assert!(disks.contains(&scratch.program("mke2fs")), "{disks}");
    let remote = format!("{:?}", api.remote);
    assert!(remote.contains(&scratch.program("ch-remote")), "{remote}");
    assert!(!remote.contains(CH), "{remote}");
    let snapshotter = format!("{:?}", api.snapshotter);
    assert!(
        snapshotter.contains(&scratch.program("fallocate")),
        "{snapshotter}"
    );
    assert!(
        snapshotter.contains(&scratch.program("ch-remote")),
        "{snapshotter}"
    );
    assert_eq!(start.ch.path(), Path::new(CH));
    assert_eq!(
        start.systemd_run.path(),
        scratch.dir.join("bin/systemd-run")
    );
    assert_eq!(start.budget, 7 * GIB);
    assert_eq!(start.limits.ram_cap, 80 * GIB);

    // Decisions CH57 rule 1 and CH58 rule 4: the digests of
    // `digests.json`, each in the digest store of the state root.
    let [kernel, base, python] = IMAGES.map(|(_, _, sha256)| sha256.parse().unwrap());
    let roots = PerFlavor::every(|flavor| match flavor {
        SandboxFlavor::Base => base,
        SandboxFlavor::Python => python,
    });
    assert_eq!(start.digests, Digests { kernel, roots });
    for digest in [kernel, base, python] {
        start.images.check(digest).unwrap();
    }
}

/// Decision CH57 rule 1: a `digests.json` that root does not own refuses
/// the start, and no import runs: no digest reaches the store. A test
/// that runs as root owns a root-owned file, so the case has nothing to
/// prove there.
#[test]
fn a_digests_file_that_root_does_not_own_refuses_the_start() {
    let scratch = Scratch::new("untrusted");
    let uid = fs::metadata(&scratch.dir).unwrap().uid();
    if uid == 0 {
        return;
    }
    let refused = build(scratch.config()).err();
    assert!(
        matches!(refused, Some(StartError::Digests(DigestsError::NotOwned { uid: found })) if found == uid),
        "{refused:?}"
    );
    let config = scratch.config();
    let images = ImageStore::new(&config.state, config.zstd.path().to_owned());
    for (file, _, sha256) in IMAGES {
        assert!(images.check(sha256.parse().unwrap()).is_err(), "{file}");
    }
}

/// Decision CH58 rule 5: a file that a root-owned `digests.json` names
/// and the image root lacks refuses the start.
#[test]
#[ignore = "needs runner sudo for a root-owned file: the job kvm runs it"]
fn kvm_a_missing_image_refuses_the_start() {
    let scratch = Scratch::new("broken");
    scratch.trust();
    fs::remove_file(scratch.dir.join("images/base.ext4")).unwrap();
    let refused = build(scratch.config()).err();
    let Some(StartError::Import(error)) = refused else {
        panic!("{refused:?}");
    };
    assert_eq!(error.file, "base.ext4");
    assert!(
        error
            .to_string()
            .starts_with("VMM_IMAGE_ROOT: cannot import base.ext4: ")
    );
}

/// The plan proof of L20 with no KVM: the vmm answers an empty list page
/// and a 412 on a bad protocol header, and its run ends with no error
/// when the signal fires (decisions CH19 (4), CH25 rule 1, CH26 rule 2).
#[test]
fn serve_lists_refuses_and_stops_on_the_signal() {
    let scratch = Scratch::new("serve");
    let vmm = scratch.parts();
    let runtime = runtime();
    let (term, signal) = oneshot::channel::<()>();
    let run = runtime.spawn(vmm.run(async move { _ = signal.await }));
    let socket = scratch.dir.join("run/vmm.sock");

    let list = r#"{"cursor":null,"cwd":null,"family":"chat"}"#;
    let header = format!("{HOST_API_HEADER}: {HOST_API_PROTOCOL}\r\n");
    let (status, body) = ask(&socket, &header, list);
    assert_eq!(status, 200, "{body}");
    let page: SessionPage = serde_json::from_str(&body).unwrap();
    assert_eq!((page.next, page.rows.len()), (None, 0));

    let bad = format!("{HOST_API_HEADER}: 2\r\n");
    assert_eq!(ask(&socket, &bad, list), (412, MISMATCH.to_owned()));

    term.send(()).unwrap();
    let ended = runtime.block_on(async { tokio::time::timeout(WAIT, run).await });
    assert!(matches!(ended, Ok(Ok(Ok(())))), "{ended:?}");
}

/// A server that stops before the signal fails the run, so `main.rs`
/// exits 78 (owner item 86). Here the run root is a regular file, so the
/// bind of `vmm.sock` fails.
#[test]
fn a_server_that_ends_before_the_signal_fails_the_run() {
    let mut scratch = Scratch::new("bind");
    let file = scratch.dir.join("run-file");
    fs::write(&file, "").unwrap();
    scratch.env.insert("RUNTIME_DIRECTORY", file.into());
    let vmm = scratch.parts();

    let ended = runtime().block_on(async { tokio::time::timeout(WAIT, vmm.run(pending())).await });
    let Ok(Err(error)) = ended else {
        panic!("{ended:?}");
    };
    assert!(
        matches!(error, StartError::Serve(ServeError::Bind(_))),
        "{error:?}"
    );
    assert!(error.to_string().starts_with("cannot bind the socket: "));
}

/// The status and the body of the answer to `POST /list` with `headers`
/// and `body`. It waits until the vmm binds its socket.
fn ask(socket: &Path, headers: &str, body: &str) -> (u16, String) {
    let deadline = Instant::now() + WAIT;
    let mut stream = loop {
        match UnixStream::connect(socket) {
            Err(_) if Instant::now() < deadline => sleep(Duration::from_millis(10)),
            connected => break connected.unwrap(),
        }
    };
    stream.set_read_timeout(Some(WAIT)).unwrap();
    write!(
        stream,
        "POST /list HTTP/1.1\r\nhost: vmm\r\n{headers}content-type: application/json\r\n\
         connection: close\r\ncontent-length: {}\r\n\r\n{body}",
        body.len()
    )
    .unwrap();
    let mut text = String::new();
    stream.read_to_string(&mut text).unwrap();
    let (head, body) = text.split_once("\r\n\r\n").unwrap();
    (head.get(9..12).unwrap().parse().unwrap(), body.to_owned())
}
