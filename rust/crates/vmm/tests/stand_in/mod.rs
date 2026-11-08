//! The stand-in proofs of the kvm_ tests of the vmm (plan PRs V10 and
//! E3): the ext4 disks, a root disk with the stand-in facade in one mode
//! (decision CH62 rule 12), the plain stub planes (decision CH62 rule 28)
//! and `boot`, the vmm of `tests/common/mod.rs` on such a disk with the
//! `SMALL` shape. `tests/kvm_guest.rs` and `tests/kvm_reach.rs` use them.
#![cfg(test)]

use std::fs::{self, File};
use std::io::{BufRead as _, BufReader, Write as _};
use std::net::{Ipv4Addr, SocketAddr, TcpListener};
use std::path::Path;
use std::process::Command;
use std::sync::mpsc::{self, Receiver};
use std::thread;

use creche_wire::guest::{FACADE_PROGRAM, SESSION_DISK_MOUNT};

use crate::common::{Booted, MKE2FS, RootDisk, Shape, Vmm, boot_with, files, sha256};

/// The size of each test disk.
const DISK_BYTES: u64 = 16 << 20;

/// The dirs of the test root disk: each mount point of the guest init
/// boot plan.
pub const ROOT_DIRS: [&str; 8] = [
    "dev",
    "proc",
    "sys",
    "tmp",
    "run",
    "root",
    "var/cache",
    "var/tmp",
];

/// The family VM shape of a stand-in proof: 1 vCPU and 256 MiB.
pub const SMALL: Shape = Shape { memory_mib: 256 };

/// Writes an ext4 disk at `disk`. With `tree`, the disk holds a copy of
/// it. The root dir is owned by root, as on the session disk of the vmm.
pub fn ext4(disk: &Path, tree: Option<&Path>) {
    File::create(disk).unwrap().set_len(DISK_BYTES).unwrap();
    let mut command = Command::new(MKE2FS);
    command.args(["-q", "-F", "-t", "ext4", "-E", "root_owner=0:0"]);
    if let Some(tree) = tree {
        command.arg("-d").arg(tree);
    }
    let output = command.arg("--").arg(disk).output().unwrap();
    assert!(
        output.status.success(),
        "mke2fs failed: {}",
        String::from_utf8_lossy(&output.stderr)
    );
}

/// Writes `root.img` in the image root `root`: a root disk with the
/// stand-in facade in mode `mode` at `FACADE_PROGRAM`, from a tree in
/// `work` (decision CH62 rule 12). `/etc/creche/env` comes from
/// `playpen/bin/creche-env-write` (decision CH7-7).
pub fn stand_in_disk(work: &Path, root: &Path, mode: &str) -> RootDisk {
    let files = files();
    let tree = work.join("tree");
    let session_dir = SESSION_DISK_MOUNT.trim_start_matches('/');
    for dir in ROOT_DIRS
        .into_iter()
        .chain([session_dir, "etc/creche", "etc/creche-standin"])
    {
        fs::create_dir_all(tree.join(dir)).unwrap();
    }
    let facade = tree.join(FACADE_PROGRAM.trim_start_matches('/'));
    fs::create_dir_all(facade.parent().unwrap()).unwrap();
    fs::copy(files.join("standin_facade"), &facade).unwrap();
    fs::write(tree.join("etc/creche-standin/mode"), format!("{mode}\n")).unwrap();
    let script =
        Path::new(env!("CARGO_MANIFEST_DIR")).join("../../../playpen/bin/creche-env-write");
    let env = Command::new("/bin/sh")
        .arg(script)
        .args(["LANG", "PATH"])
        .env_clear()
        .envs([("LANG", "C.UTF-8"), ("PATH", "/usr/bin:/bin")])
        .output()
        .unwrap();
    assert!(env.status.success(), "creche-env-write: {env:?}");
    fs::write(tree.join("etc/creche/env"), env.stdout).unwrap();
    let disk = root.join("root.img");
    ext4(&disk, Some(&tree));
    RootDisk {
        file: "root.img".to_owned(),
        sha256: sha256(&disk),
    }
}

/// One stub plane (decision CH62 rule 28, gap Q54-28 option A): a
/// listener on 127.0.0.1 with a port from the kernel. Its thread answers
/// each request with `200` and no body, and sends each request head on
/// the channel.
pub fn stub_plane() -> (SocketAddr, Receiver<String>) {
    let listener = TcpListener::bind((Ipv4Addr::LOCALHOST, 0)).unwrap();
    let address = listener.local_addr().unwrap();
    let (heads, received) = mpsc::channel();
    thread::spawn(move || {
        for stream in listener.incoming() {
            let stream = stream.unwrap();
            let mut reader = BufReader::new(&stream);
            let mut head = String::new();
            while !head.ends_with("\r\n\r\n") && reader.read_line(&mut head).unwrap() > 0 {}
            let ok = b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\nConnection: close\r\n\r\n";
            (&stream).write_all(ok).unwrap();
            let _ = heads.send(head);
        }
    });
    (address, received)
}

/// Starts the vmm of the proof `name` with the env of each key of the
/// vmm config (decisions CH59 rule 1, CH62 rule 29): the state root of
/// the runner, a scratch family root, root-owned copies of guest init and
/// ch-remote (decision CH36 G20), an image root with the stand-in facade
/// in mode `mode`, the `SMALL` shape, and the planes `litellm` and
/// `chaperone`. The family of the proof has no `egress.json` and no
/// `repos.json`.
pub fn boot(name: &str, mode: &str, planes: (SocketAddr, SocketAddr)) -> Booted {
    let disk = |work: &Path, root: &Path| stand_in_disk(work, root, mode);
    boot_with(name, disk, SMALL, planes, |name, env, _| {
        Vmm::start(name, env)
    })
}
