//! The compose step on temp paths with no KVM (plan PR L20). `build`
//! trusts only root-owned files: CH, ch-remote, git and the guest init
//! program (decisions CH67 rule 1, CH69 rule 17), and `digests.json`
//! (decision CH57 rule 1). A
//! test of the rust job runs as the runner user and cannot make a
//! root-owned file, so the rust job proves that the CH copy of the test
//! refuses the start before any import. The serve tests there start from
//! `parts`, with the checks of `check` done by the test: the CH version
//! check of the fixture and an import into the store. The serve tests put
//! a made initramfs into the store in place of the guest init program.
//! The `kvm_` tests run `build` itself on root-owned files: the job kvm
//! runs each ignored `kvm_` test, and its runner may use sudo (decision
//! CH36 G20).
//!
//! Each program that a part only holds gets its own path, and a test
//! reads the `Debug` text of the part. The store crate gives no accessor
//! of these paths, and only this test needs one.

use std::collections::HashMap;
use std::ffi::OsString;
use std::fs;
use std::future::pending;
use std::io::{Read, Write};
use std::os::unix::fs::{MetadataExt, PermissionsExt, symlink};
use std::os::unix::net::UnixStream;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::thread::sleep;
use std::time::{Duration, Instant};

use creche_vmm_confine::admission::GIB;
use creche_vmm_store::images::ImageStore;
use creche_vmm_store::initramfs::{Entry, Kind, pack};
use creche_vmm_store::key::check_cloud_hypervisor;
use creche_wire::{HOST_API_HEADER, HOST_API_PROTOCOL, PerFlavor, SandboxFlavor, SessionPage};
use tokio::runtime::{Builder, Runtime};
use tokio::sync::oneshot;

use super::{Checked, StartError, VERSION_WAIT, Vmm, build, parts};
use crate::ServeError;
use crate::config::{CH, CH_REMOTE, Config, GIT, GUEST_INIT as GUEST_INIT_KEY};
use crate::guest_init::GuestInitError;
use crate::image_root::follow::{Follow, HOOK_TIMEOUT, POLL};
use crate::image_root::{DIGESTS_FILE, DigestsError, parse};
use crate::root_owned::{OwnerError, ProgramError};
use crate::start::Digests;

/// The CH fixture of the version check (gap G10). Each scratch dir gets a
/// copy, so a `kvm_` test can make root the owner of the copy.
const CH_FIXTURE: &str = concat!(
    env!("CARGO_MANIFEST_DIR"),
    "/tests/fixtures/cloud-hypervisor"
);

/// The bytes of the guest init program of the test.
const GUEST_INIT: &[u8] = b"guest-init";

/// Each program key whose file `build` checks, with its file in `bin/`
/// (decisions CH67 rule 1, CH69 rule 17).
const PROGRAMS: [(&str, &str); 4] = [
    (CH, "cloud-hypervisor"),
    (CH_REMOTE, "ch-remote"),
    (GIT, "git"),
    (GUEST_INIT_KEY, "guest-init"),
];

/// A root-owned regular file of every host, which the owner check passes.
const ROOT_OWNED: &str = "/usr/bin/true";

/// The longest wait of one step.
const WAIT: Duration = Duration::from_secs(10);

/// The frozen mismatch reply of this protocol. Never change its form to
/// make a change pass.
fn mismatch() -> String {
    format!(r#"{{"code":"protocol_mismatch","protocol":{HOST_API_PROTOCOL}}}"#)
}

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

/// Copies `source` to `dest` as an executable through a `cp` child. The
/// write fd of the copy then lives only in that child, so no fork of a
/// parallel test can hold it open for writing, and the exec of `dest`
/// never fails with ETXTBSY. The rust job saw that race when the copy was
/// a `fs::copy` in this process (run 42406264853).
fn place_program(source: &Path, dest: &Path) {
    let status = Command::new("cp").arg(source).arg(dest).status().unwrap();
    assert!(status.success(), "cp {} failed", source.display());
    fs::set_permissions(dest, fs::Permissions::from_mode(0o755)).unwrap();
}

/// Writes `script` next to `dest`, then places it with `place_program`, so
/// the executed file never had a write fd in this process.
fn write_program(dest: &Path, script: &str) {
    let source = dest.with_extension("src");
    fs::write(&source, script).unwrap();
    place_program(&source, dest);
}

impl Scratch {
    /// The dirs, the images, a `digests.json` and the four programs of
    /// `PROGRAMS` that the test user owns, and a `systemctl` that logs each
    /// call and lists no unit. The `ch-remote` and the `git` of the test
    /// only exit 1: no test runs them.
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
        fs::write(dir.join("bin/guest-init"), GUEST_INIT).unwrap();
        place_program(Path::new(CH_FIXTURE), &dir.join("bin/cloud-hypervisor"));
        write_program(&dir.join("bin/ch-remote"), "#!/bin/sh\nexit 1\n");
        write_program(&dir.join("bin/git"), "#!/bin/sh\nexit 1\n");

        let systemctl = dir.join("bin/systemctl");
        let log = dir.join("systemctl.log");
        let script = format!("#!/bin/sh\nprintf '%s\\n' \"$*\" >> '{}'\n", log.display());
        write_program(&systemctl, &script);

        let text = |path: PathBuf| OsString::from(path);
        let env = HashMap::from([
            ("VMM_STATE_ROOT", text(dir.join("state"))),
            ("RUNTIME_DIRECTORY", text(dir.join("run"))),
            ("VMM_FAMILY_ROOT", text(dir.join("families"))),
            ("VMM_FAULTS_ROOT", text(dir.join("faults"))),
            ("VMM_IMAGE_ROOT", text(dir.join("images"))),
            ("VMM_CH_PROGRAM", text(dir.join("bin/cloud-hypervisor"))),
            ("VMM_CH_REMOTE_PROGRAM", text(dir.join("bin/ch-remote"))),
            ("VMM_GUEST_INIT_PROGRAM", text(dir.join("bin/guest-init"))),
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
            ("VMM_LITELLM_ADDR", OsString::from("192.0.2.10:4000")),
            ("VMM_CHAPERONE_ADDR", OsString::from("192.0.2.10:8300")),
            ("VMM_EGRESS_LOG", text(dir.join("egress.jsonl"))),
            ("VMM_MIRROR_ROOT", text(dir.join("mirror"))),
            ("VMM_GIT_PROGRAM", text(dir.join("bin/git"))),
        ]);
        Self { dir, env }
    }

    fn config(&self) -> Config {
        Config::from_lookup(|key| self.env.get(key).cloned()).unwrap()
    }

    /// Makes root the owner of `digests.json`, as the disk row of phase 5
    /// installs it, and of each program of `PROGRAMS`, as the vmm row
    /// installs them (decision CH67 rule 1). Only a `kvm_` test calls it.
    fn trust(&self) {
        sudo(
            &["chown", "0:0"],
            &self.dir.join("images").join(DIGESTS_FILE),
        );
        for (_, file) in PROGRAMS {
            sudo(&["chown", "0:0"], &self.dir.join("bin").join(file));
        }
    }

    /// The vmm from `parts`, after the checks of `check` that a test of
    /// the rust job can run: the CH version check of the fixture, and the
    /// parse of `digests.json` with no owner check and an import of each
    /// image into the store of the state root (`Follow::start`).
    fn parts(&self) -> Vmm {
        let config = self.config();
        let ch_version = check_cloud_hypervisor(config.ch.path(), VERSION_WAIT).unwrap();
        let images = ImageStore::new(&config.state, config.zstd.path().to_owned());
        let bytes = fs::read(config.image_root.join(DIGESTS_FILE)).unwrap();
        let set = parse(&config.image_root, bytes).unwrap();
        let (follow, digests) = Follow::start(set, images.clone()).unwrap();
        let initramfs = images.put_made(b"initramfs").unwrap().digest();
        let checked = Checked {
            ch_version,
            images,
            digests,
            follow,
            initramfs,
        };
        parts(config, checked).unwrap()
    }

    /// The `Debug` text of the path of `bin/<program>`.
    fn program(&self, program: &str) -> String {
        format!("{:?}", self.dir.join("bin").join(program))
    }
}

/// Runs `command` on `path` with the sudo of the runner, such as a
/// `chown 0:0`. Decision CH36 G20 allows sudo on GitHub's throwaway
/// runners only, never on the host, so it refuses outside GitHub Actions.
fn sudo(command: &[&str], path: &Path) {
    assert_eq!(
        std::env::var("GITHUB_ACTIONS").as_deref(),
        Ok("true"),
        "sudo runs only on a GitHub runner (decision CH36 G20)"
    );
    let status = Command::new("sudo")
        .arg("-n")
        .args(command)
        .arg(path)
        .status()
        .unwrap();
    assert!(status.success(), "sudo {command:?}: {status}");
}

/// The archive of the guest init program of the test: `dev`,
/// `dev/console` 5:1 and `/init` with mode 0500 (decision CH62 rule 18).
fn initramfs() -> Vec<u8> {
    let entry = |name: &str, mode, kind| Entry {
        name: name.to_owned(),
        mode,
        kind,
    };
    pack(&[
        entry("dev", 0o755, Kind::Dir),
        entry(
            "dev/console",
            0o600,
            Kind::CharDevice { major: 5, minor: 1 },
        ),
        entry("init", 0o500, Kind::File(GUEST_INIT.to_vec())),
    ])
    .unwrap()
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
    assert!(
        !remote.contains(&scratch.program("cloud-hypervisor")),
        "{remote}"
    );
    let snapshotter = format!("{:?}", api.snapshotter);
    assert!(
        snapshotter.contains(&scratch.program("fallocate")),
        "{snapshotter}"
    );
    assert!(
        snapshotter.contains(&scratch.program("ch-remote")),
        "{snapshotter}"
    );
    assert_eq!(start.ch.path(), scratch.dir.join("bin/cloud-hypervisor"));
    assert_eq!(
        start.systemd_run.path(),
        scratch.dir.join("bin/systemd-run")
    );
    assert_eq!(start.budget, 7 * GIB);
    assert_eq!(start.limits.ram_cap, 80 * GIB);
    // Decision CH62 rule 20: each plane key reaches the bridges.
    let rows = start
        .planes
        .rows()
        .map(|(port, addr)| (port.number(), addr.to_string()));
    let planes = [(1101, "192.0.2.10:4000"), (1102, "192.0.2.10:8300")];
    assert_eq!(rows, planes.map(|(port, addr)| (port, addr.to_owned())));
    // Decision CH69 rule 17: both mirror keys reach the mirror.
    assert_eq!(start.mirror.root.path(), scratch.dir.join("mirror"));
    assert_eq!(start.mirror.git.path(), scratch.dir.join("bin/git"));

    // Decisions CH57 rule 1 and CH58 rule 4: the digests of
    // `digests.json`, each in the digest store of the state root.
    let [kernel, base, python] = IMAGES.map(|(_, _, sha256)| sha256.parse().unwrap());
    let roots = PerFlavor::every(|flavor| match flavor {
        SandboxFlavor::Base => base,
        SandboxFlavor::Python => python,
    });
    assert_eq!(*start.digests.borrow(), Digests { kernel, roots });
    for digest in [kernel, base, python] {
        start.images.check(digest).unwrap();
    }

    // Decisions CH62 rules 17 and 18: the initramfs of the guest init
    // program of the key, in the digest store.
    let stored = start.images.check(start.initramfs).unwrap();
    assert_eq!(fs::read(stored.path()).unwrap(), initramfs());
}

/// Decision CH76 rule 8: `Vmm::run` runs the digests follow, so a new
/// root-owned `digests.json` reaches the set of new starts with no vmm
/// restart. A compose step that never runs the follow fails here.
/// Decision CH77 rule 2: the swap comes no sooner than `HOOK_TIMEOUT`
/// plus one `POLL` after the rename, and within a few polls after it. The `kvm_` test of
/// `tests/image_follow.rs` boots `examples/phase3_vm.rs`, which runs its
/// own follow, so only this test proves the compose path.
#[test]
#[ignore = "needs runner sudo for a root-owned file: the job kvm runs it"]
fn kvm_serve_follows_a_new_digests_file() {
    let scratch = Scratch::new("follow");
    scratch.trust();
    let vmm = build(scratch.config()).unwrap();
    let mut digests = vmm.hook.0.parts().0.digests.clone();
    let runtime = runtime();
    let (term, signal) = oneshot::channel::<()>();
    let run = runtime.spawn(vmm.run(async move { _ = signal.await }));

    // The base flavor moves to the python disk. The handover swap puts a
    // root-owned file in place with a rename.
    let entry = |(file, _, sha256): (&str, &str, &str)| serde_json::json!({ "file": file, "sha256": sha256 });
    let next = serde_json::json!({
        "kernel": entry(IMAGES[0]),
        "base": entry(IMAGES[2]),
        "python": entry(IMAGES[2]),
    });
    let staged = scratch.dir.join("images/next.json");
    fs::write(&staged, next.to_string()).unwrap();
    sudo(&["chown", "0:0"], &staged);
    let renamed = Instant::now();
    fs::rename(&staged, scratch.dir.join("images").join(DIGESTS_FILE)).unwrap();

    let [kernel, _, python] = IMAGES.map(|(_, _, sha256)| sha256.parse().unwrap());
    let want = Digests {
        kernel,
        roots: PerFlavor::every(|_| python),
    };
    let swapped = runtime.block_on(async {
        tokio::time::timeout(
            HOOK_TIMEOUT + POLL * 4,
            digests.wait_for(|set| *set == want),
        )
        .await
        .is_ok_and(|seen| seen.is_ok())
    });
    assert!(swapped, "{:?}", *digests.borrow());
    let waited = renamed.elapsed();
    assert!(
        waited >= HOOK_TIMEOUT + POLL,
        "the swap came after {waited:?}"
    );

    term.send(()).unwrap();
    let ended = runtime.block_on(async { tokio::time::timeout(WAIT, run).await });
    assert!(matches!(ended, Ok(Ok(Ok(())))), "{ended:?}");
}

/// Decisions CH62 rule 17, CH67 rule 1 and CH69 rule 17: the vmm runs CH,
/// ch-remote and git, and packs a guest init program, only when root owns
/// each file and no group or other user may write it. A root-owned guest
/// init program gives one initramfs across vmm starts (CH62 rule 18). A
/// group write bit on any of the four files refuses the start, and the
/// fault names its key. An owner other than root refuses too (the rust job
/// proves that one).
#[test]
#[ignore = "needs runner sudo for a root-owned file: the job kvm runs it"]
fn kvm_each_program_must_be_root_owned() {
    let scratch = Scratch::new("programs");
    scratch.trust();
    let first = build(scratch.config()).unwrap().hook.0.parts().0.initramfs;
    let again = build(scratch.config()).unwrap().hook.0.parts().0.initramfs;
    assert_eq!(first, again);

    for (key, file) in PROGRAMS {
        let path = scratch.dir.join("bin").join(file);
        let mode = fs::metadata(&path).unwrap().mode() & 0o7777;
        let widened = mode | 0o020;
        sudo(&["chmod", &format!("{widened:o}")], &path);
        let refused = build(scratch.config()).err();
        let program = match refused {
            Some(
                StartError::Program(program)
                | StartError::GuestInit(GuestInitError::Program(program)),
            ) => program,
            other => panic!("{key}: {other:?}"),
        };
        assert_eq!(program.key, key);
        assert!(
            matches!(program.fault, OwnerError::Writable { mode: found } if found == widened),
            "{key}: {program:?}"
        );
        sudo(&["chmod", &format!("{mode:o}")], &path);
    }
}

/// Decision CH67 rule 1: a CH program that root does not own refuses the
/// start before its version check, and no import runs: no digest reaches
/// the store. A test that runs as root owns a root-owned file, so the
/// case has nothing to prove there.
#[test]
fn a_ch_program_that_root_does_not_own_refuses_the_start() {
    let scratch = Scratch::new("untrusted");
    let uid = fs::metadata(scratch.dir.join("bin/cloud-hypervisor"))
        .unwrap()
        .uid();
    if uid == 0 {
        return;
    }
    let refused = build(scratch.config()).err();
    assert!(
        matches!(
            &refused,
            Some(StartError::Program(ProgramError {
                key: CH,
                fault: OwnerError::NotOwned { uid: found },
            })) if *found == uid
        ),
        "{refused:?}"
    );
    assert_eq!(
        refused.unwrap().to_string(),
        format!("VMM_CH_PROGRAM: the program is owned by uid {uid}, not by root")
    );
    let config = scratch.config();
    let images = ImageStore::new(&config.state, config.zstd.path().to_owned());
    for (file, _, sha256) in IMAGES {
        assert!(images.check(sha256.parse().unwrap()).is_err(), "{file}");
    }
}

/// The scratch with CH and ch-remote on `ROOT_OWNED`, so their check
/// passes in the rust job and the check of `VMM_GIT_PROGRAM` runs next.
/// No test runs either.
fn trusted_ch(scratch: &mut Scratch) {
    for key in [CH, CH_REMOTE] {
        scratch.env.insert(key, OsString::from(ROOT_OWNED));
    }
}

/// Decision CH69 rule 17: a git program that root does not own refuses the
/// start after the CH and ch-remote checks, before the CH version check,
/// and no import runs. A test that runs as root owns a root-owned file, so
/// the case has nothing to prove there.
#[test]
fn a_git_program_that_root_does_not_own_refuses_the_start() {
    let mut scratch = Scratch::new("git-owner");
    let uid = fs::metadata(scratch.dir.join("bin/git")).unwrap().uid();
    if uid == 0 {
        return;
    }
    trusted_ch(&mut scratch);
    let refused = build(scratch.config()).err();
    assert!(
        matches!(
            &refused,
            Some(StartError::Program(ProgramError {
                key: GIT,
                fault: OwnerError::NotOwned { uid: found },
            })) if *found == uid
        ),
        "{refused:?}"
    );
    assert_eq!(
        refused.unwrap().to_string(),
        format!("VMM_GIT_PROGRAM: the program is owned by uid {uid}, not by root")
    );
    let config = scratch.config();
    let images = ImageStore::new(&config.state, config.zstd.path().to_owned());
    for (file, _, sha256) in IMAGES {
        assert!(images.check(sha256.parse().unwrap()).is_err(), "{file}");
    }
}

/// Decision CH69 rule 17, as CH67 rule 1: a git program that is a symlink
/// refuses the start, also when it points at a root-owned file.
#[test]
fn a_symlinked_git_program_refuses_the_start() {
    let mut scratch = Scratch::new("git-symlink");
    trusted_ch(&mut scratch);
    let git = scratch.dir.join("bin/git");
    fs::remove_file(&git).unwrap();
    symlink(ROOT_OWNED, &git).unwrap();
    let refused = build(scratch.config()).err();
    assert!(
        matches!(
            &refused,
            Some(StartError::Program(ProgramError {
                key: GIT,
                fault: OwnerError::NotRegular,
            }))
        ),
        "{refused:?}"
    );
}

/// Decision CH57 rule 1: a `digests.json` that root does not own refuses
/// the start, and no import runs: no digest reaches the store.
#[test]
#[ignore = "needs runner sudo for a root-owned file: the job kvm runs it"]
fn kvm_a_digests_file_that_root_does_not_own_refuses_the_start() {
    let scratch = Scratch::new("digests");
    scratch.trust();
    let uid = fs::metadata(&scratch.dir).unwrap().uid();
    let digests = scratch.dir.join("images").join(DIGESTS_FILE);
    sudo(&["chown", &uid.to_string()], &digests);
    let refused = build(scratch.config()).err();
    assert!(
        matches!(
            refused,
            Some(StartError::Digests(DigestsError::Owner(OwnerError::NotOwned { uid: found })))
                if found == uid
        ),
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
    let (status, body) = ask(&socket, "list", &header, list);
    assert_eq!(status, 200, "{body}");
    let page: SessionPage = serde_json::from_str(&body).unwrap();
    assert_eq!((page.next, page.rows.len()), (None, 0));

    // The shipped handler of `check` (decision CH69 rule 1). The scratch
    // families dir has no `egress.json`, so the list is empty and the
    // answer needs no lookup (decision CH69 rule 3).
    let check = r#"{"family":"code","target":"github.com:443"}"#;
    let refused = r#"{"refused":{"reason":"not_listed"}}"#.to_owned();
    assert_eq!(ask(&socket, "check", &header, check), (200, refused));

    let bad = format!("{HOST_API_HEADER}: {}\r\n", HOST_API_PROTOCOL + 1);
    assert_eq!(ask(&socket, "list", &bad, list), (412, mismatch()));

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

/// The status and the body of the answer to `POST /<route>` with
/// `headers` and `body`. It waits until the vmm binds its socket.
fn ask(socket: &Path, route: &str, headers: &str, body: &str) -> (u16, String) {
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
        "POST /{route} HTTP/1.1\r\nhost: vmm\r\n{headers}content-type: application/json\r\n\
         connection: close\r\ncontent-length: {}\r\n\r\n{body}",
        body.len()
    )
    .unwrap();
    let mut text = String::new();
    stream.read_to_string(&mut text).unwrap();
    let (head, body) = text.split_once("\r\n\r\n").unwrap();
    (head.get(9..12).unwrap().parse().unwrap(), body.to_owned())
}
