//! `vmm gc`, the snapshot and image retention by hand. `gc::collect` runs
//! on a scratch state root with real store pairs and snapshot keys: the dry
//! run lists and removes nothing, `Act::Remove` removes the same items, and
//! a held state root lock refuses it. `image_root::read` and
//! `guest_init::initramfs_digest` trust only root-owned files, and a test
//! of the rust job cannot make one. So the binary tests of the rust job
//! prove the refusals, each with exit 78 and no file removed. The ignored
//! `kvm_` test makes both files root-owned with the runner's sudo
//! (decision CH36 G20) and runs the binary end to end. Only the job `kvm`
//! runs it.

use std::fs;
use std::os::unix::fs::MetadataExt;
use std::path::{Path, PathBuf};
use std::process::{Command, Output};

use creche_vmm::config::{BUDGET, GUEST_INIT, IMAGE_ROOT, STATE_ROOT, ZSTD};
use creche_vmm::gc::{Act, GcError, Outcome, collect};
use creche_vmm::guest_init::initramfs_digest;
use creche_vmm::image_root::{DIGESTS_FILE, KERNEL_KEY};
use creche_vmm_store::images::{ImageDigest, ImageStore};
use creche_vmm_store::key::{CH_REV, CurrentImages};
use creche_vmm_store::layout::{SnapshotGeneration, StateRoot};
use creche_vmm_store::retention::{BUDGET as DESIGN_BUDGET, Reason};
use creche_wire::{GUEST_INIT_PROTOCOL, PerFlavor, SandboxFlavor, SessionId};

/// A session whose snapshot names an old root disk and initramfs.
const OLD: &str = "0f8e2a4c-1b3d-4e5f-8a9b-0c1d2e3f4a5b";
/// A session whose snapshot names the current images.
const NEW: &str = "1f8e2a4c-1b3d-4e5f-8a9b-0c1d2e3f4a5b";

/// A scratch dir that its drop removes.
struct Scratch {
    dir: PathBuf,
    state: StateRoot,
    store: ImageStore,
}

impl Drop for Scratch {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.dir);
    }
}

#[cfg(test)]
impl Scratch {
    fn new(name: &str) -> Self {
        let dir = std::env::temp_dir().join(format!("creche-vmm-gc-{}-{name}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        let state = StateRoot::new(dir.join("state")).unwrap();
        let store = ImageStore::new(&state, PathBuf::from("/usr/bin/zstd"));
        Self { dir, state, store }
    }

    /// Imports a plain file of `text` into the store and gives its digest.
    fn image(&self, text: &str) -> ImageDigest {
        let source = self.dir.join("source");
        fs::write(&source, text).unwrap();
        let digest = ImageDigest::of(text.as_bytes());
        self.store.import(&source, digest).unwrap();
        digest
    }

    /// Writes generation 1 of `id` with a key of `kernel`, `initramfs`
    /// and the `coding` root disk `root`.
    fn snapshot(&self, id: &str, kernel: ImageDigest, initramfs: ImageDigest, root: ImageDigest) {
        let id: SessionId = id.parse().unwrap();
        let session = self.state.session(&id);
        let dir = session.snapshot(SnapshotGeneration(1));
        fs::create_dir_all(&dir).unwrap();
        fs::write(session.disk(), "disk").unwrap();
        fs::write(dir.join("memory-ranges"), vec![1_u8; 4096]).unwrap();
        let key = serde_json::json!({
            "ch_rev": CH_REV,
            "kernel": kernel.to_string(),
            "initramfs": initramfs.to_string(),
            "root_disk": root.to_string(),
            "guest_init_protocol": GUEST_INIT_PROTOCOL,
            "cpus": 8,
            "memory_mib": 16384,
            "flavor": SandboxFlavor::Coding.name(),
        });
        fs::write(dir.join("key.json"), key.to_string()).unwrap();
    }

    fn exists(&self, digest: ImageDigest) -> bool {
        self.store.path(&digest).exists() && self.store.identity_path(&digest).exists()
    }
}

/// The store and snapshots of a host after a disks and a vmm release:
/// session `OLD` keeps a snapshot of the old root disk and initramfs,
/// session `NEW` one of the current images, and an import left a temp
/// file.
struct Host {
    scratch: Scratch,
    current: CurrentImages,
    old_root: ImageDigest,
    old_initramfs: ImageDigest,
    leftover: PathBuf,
}

#[cfg(test)]
impl Host {
    fn new(name: &str) -> Self {
        let scratch = Scratch::new(name);
        let kernel = scratch.image("kernel");
        let initramfs = scratch.image("initramfs 2");
        let roots = PerFlavor::every(|flavor| scratch.image(&format!("root 2 {}", flavor.name())));
        let old_root = scratch.image("root 1 coding");
        let old_initramfs = scratch.image("initramfs 1");
        scratch.snapshot(OLD, kernel, old_initramfs, old_root);
        scratch.snapshot(NEW, kernel, initramfs, *roots.get(SandboxFlavor::Coding));
        let leftover = scratch
            .state
            .store_dir()
            .join(format!("{}.tmp", ImageDigest::of(b"killed import")));
        fs::write(&leftover, "part").unwrap();
        let current = CurrentImages {
            kernel,
            initramfs,
            roots,
        };
        Self {
            scratch,
            current,
            old_root,
            old_initramfs,
            leftover,
        }
    }

    fn collect(&self, act: Act) -> Result<Outcome, GcError> {
        collect(
            &self.scratch.state,
            &self.scratch.store,
            &self.current,
            DESIGN_BUDGET,
            act,
        )
    }

    /// The snapshot dir of session `id`.
    fn snapshot(&self, id: &str) -> PathBuf {
        let id: SessionId = id.parse().unwrap();
        self.scratch
            .state
            .session(&id)
            .snapshot(SnapshotGeneration(1))
    }

    fn current_stays(&self) {
        let current = &self.current;
        for digest in current.digests() {
            assert!(self.scratch.exists(digest), "{digest}");
        }
        assert!(self.snapshot(NEW).is_dir());
    }
}

/// The listed items of `outcome`: the snapshot sessions with reasons, the
/// image digests and the leftover paths.
#[cfg(test)]
fn items(outcome: &Outcome) -> (Vec<(String, Reason)>, Vec<ImageDigest>, Vec<PathBuf>) {
    let swept = outcome
        .swept()
        .dropped
        .iter()
        .map(|each| (each.session.to_string(), each.reason))
        .collect();
    let collected = outcome.collected().unwrap();
    let mut images: Vec<_> = collected.removed.iter().map(|each| each.digest).collect();
    images.sort_unstable();
    let leftovers = collected
        .leftovers
        .iter()
        .map(|each| each.path.clone())
        .collect();
    (swept, images, leftovers)
}

/// The dry run lists the stale snapshot, the images that only its key
/// names and the leftover. It removes nothing, and the current images and
/// the current snapshot stay out of the list.
#[test]
fn the_dry_run_lists_and_removes_nothing() {
    let host = Host::new("dry");
    let outcome = host.collect(Act::List).unwrap();

    let mut old = vec![host.old_root, host.old_initramfs];
    old.sort_unstable();
    let expected = (
        vec![(OLD.to_owned(), Reason::Stale)],
        old.clone(),
        vec![host.leftover.clone()],
    );
    assert_eq!(items(&outcome), expected);
    assert!(!outcome.failed(), "{:?}", outcome.lines());
    assert!(host.snapshot(OLD).is_dir());
    for digest in old {
        assert!(host.scratch.exists(digest), "{digest}");
    }
    assert!(host.leftover.exists());
    host.current_stays();

    let lines = outcome.lines();
    assert_eq!(lines[0], "vmm gc: dry run, nothing is removed");
    assert!(
        lines[1].starts_with(&format!(
            "would remove snapshot {OLD} generation 1 (stale, "
        )),
        "{lines:?}"
    );
    let last = format!(
        "would reclaim {} bytes: vmm gc --apply removes them",
        outcome.bytes()
    );
    assert_eq!(lines.last(), Some(&last));
    assert!(outcome.bytes() > 0);
}

/// `Act::Remove` removes what the dry run listed, and a second run finds
/// nothing.
#[test]
fn the_apply_removes_what_the_dry_run_listed() {
    let host = Host::new("apply");
    let listed = items(&host.collect(Act::List).unwrap());
    let outcome = host.collect(Act::Remove).unwrap();

    assert_eq!(items(&outcome), listed);
    assert!(!outcome.failed(), "{:?}", outcome.lines());
    assert!(!host.snapshot(OLD).exists());
    assert!(!host.scratch.exists(host.old_root));
    assert!(!host.scratch.store.identity_path(&host.old_root).exists());
    assert!(!host.scratch.exists(host.old_initramfs));
    assert!(!host.leftover.exists());
    host.current_stays();
    let lines = outcome.lines();
    assert_eq!(lines[0], "vmm gc: removing");
    assert_eq!(
        lines.last(),
        Some(&format!("reclaimed {} bytes", outcome.bytes()))
    );

    let again = host.collect(Act::Remove).unwrap();
    assert_eq!(items(&again), (vec![], vec![], vec![]));
    assert_eq!(again.bytes(), 0);
}

/// While another holder has the state root lock, as a running `vmm serve`
/// has, `Act::Remove` refuses and removes nothing. The dry run still runs.
#[test]
fn a_held_state_root_lock_refuses_the_apply() {
    let host = Host::new("busy");
    let held = host.scratch.state.lock().unwrap();

    let refused = host.collect(Act::Remove).unwrap_err();
    assert!(matches!(refused, GcError::Busy(_)), "{refused}");
    assert!(refused.to_string().contains("stop vmm serve"), "{refused}");
    assert!(host.snapshot(OLD).is_dir());
    assert!(host.scratch.exists(host.old_root));
    assert!(host.leftover.exists());
    assert!(host.collect(Act::List).is_ok());

    drop(held);
    assert!(host.collect(Act::Remove).is_ok());
    assert!(!host.snapshot(OLD).exists());
}

/// A refused image scan, such as an unexpected entry under `sessions/`,
/// removes no image and fails the run.
#[test]
fn a_refused_image_scan_fails_the_run() {
    let host = Host::new("scan");
    fs::write(host.scratch.state.sessions_dir().join("surprise"), "x").unwrap();

    let outcome = host.collect(Act::Remove).unwrap();
    assert!(outcome.failed());
    assert!(outcome.collected().is_err());
    assert!(host.scratch.exists(host.old_root));
    assert!(host.leftover.exists());
    let lines = outcome.lines();
    assert!(
        lines
            .iter()
            .any(|line| line.starts_with("failed: the image pass: ")),
        "{lines:?}"
    );
}

/// Writes `vmm.env` in `dir` with `keys`.
#[cfg(test)]
fn env_file(dir: &Path, keys: &[(&str, &Path)]) -> PathBuf {
    let mut text = String::from("# Written by bin/fleet-up, mode 0600.\n");
    for (key, path) in keys {
        text.push_str(&format!("{key}={}\n", path.display()));
    }
    text.push_str(&format!("{BUDGET}=150\n"));
    let path = dir.join("vmm.env");
    fs::write(&path, text).unwrap();
    path
}

/// Runs `vmm gc` with `words` before `--env-file <env_file>`.
#[cfg(test)]
fn gc(words: &[&str], env_file: &Path) -> Output {
    Command::new(env!("CARGO_BIN_EXE_vmm"))
        .arg("gc")
        .args(words)
        .arg("--env-file")
        .arg(env_file)
        .env_clear()
        .output()
        .unwrap()
}

/// A missing env file and a missing key each exit 78, name the cause and
/// write no stdout line.
#[test]
fn a_bad_env_file_exits_78() {
    let scratch = Scratch::new("env");
    let output = gc(&[], &scratch.dir.join("absent.env"));
    assert_eq!(output.status.code(), Some(78));
    assert!(output.stdout.is_empty());
    let stderr = String::from_utf8(output.stderr).unwrap();
    assert!(stderr.starts_with("vmm: gc: "), "{stderr}");

    let path = env_file(&scratch.dir, &[(STATE_ROOT, &scratch.dir.join("state"))]);
    let output = gc(&["--apply"], &path);
    assert_eq!(output.status.code(), Some(78));
    let stderr = String::from_utf8(output.stderr).unwrap();
    assert_eq!(
        stderr,
        format!("vmm: gc: {IMAGE_ROOT}: the key has no value\n")
    );
}

/// A `digests.json` that the test user owns exits 78 before any remove,
/// in the dry run and with `--apply` (decision CH57 rule 1). The vmm of a
/// host reads only a root-owned one.
#[test]
fn an_untrusted_digests_file_exits_78_and_removes_nothing() {
    let host = Host::new("untrusted");
    let images = host.scratch.dir.join("images");
    fs::create_dir_all(&images).unwrap();
    let mut json = serde_json::Map::new();
    let flavors = PerFlavor::every(SandboxFlavor::name);
    for key in std::iter::once(KERNEL_KEY).chain(flavors.iter().map(|(_, &name)| name)) {
        let entry = serde_json::json!({ "file": format!("{key}.img"), "sha256": host.current.kernel.to_string() });
        json.insert(key.to_owned(), entry);
    }
    let digests = images.join(DIGESTS_FILE);
    fs::write(&digests, serde_json::Value::Object(json).to_string()).unwrap();
    let uid = fs::metadata(&digests).unwrap().uid();
    let program = host.scratch.dir.join("guest-init");
    fs::write(&program, "init").unwrap();
    let path = env_file(
        &host.scratch.dir,
        &[
            (STATE_ROOT, host.scratch.state.path()),
            (IMAGE_ROOT, &images),
            (GUEST_INIT, &program),
            (ZSTD, Path::new("/usr/bin/zstd")),
        ],
    );
    for words in [&[][..], &["--apply"][..]] {
        let output = gc(words, &path);
        let stderr = String::from_utf8(output.stderr).unwrap();
        if uid != 0 {
            assert_eq!(output.status.code(), Some(78), "{stderr}");
            assert!(stderr.starts_with("vmm: gc: "), "{stderr}");
            assert!(stderr.contains(&format!("uid {uid}")), "{stderr}");
        }
        assert!(host.snapshot(OLD).is_dir());
        assert!(host.leftover.exists());
    }
}

/// Makes root the owner of `path` with the runner's sudo (decision CH36
/// G20), as `tests/kvm_verify.rs` does.
#[cfg(test)]
fn sudo_chown_root(path: &Path) {
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

/// The binary end to end on root-owned files: the dry run lists the stale
/// snapshot and the images that only its key names, and exits 0 with no
/// remove. `--apply` removes them, and the current images stay. The
/// current initramfs is the digest of the root-owned guest init program,
/// as at a vmm start.
#[test]
#[ignore = "needs runner sudo for a root-owned file: the job kvm runs it"]
fn kvm_the_binary_lists_then_removes() {
    let scratch = Scratch::new("kvm");
    let program = scratch.dir.join("guest-init");
    fs::write(&program, "the guest init program").unwrap();
    sudo_chown_root(&program);
    let initramfs = initramfs_digest(&program).unwrap();

    let kernel = scratch.image("kernel");
    let roots = PerFlavor::every(|flavor| scratch.image(&format!("root 2 {}", flavor.name())));
    let old_root = scratch.image("root 1 coding");
    let old_initramfs = scratch.image("initramfs 1");
    scratch.snapshot(OLD, kernel, old_initramfs, old_root);
    scratch.snapshot(NEW, kernel, initramfs, *roots.get(SandboxFlavor::Coding));

    let images = scratch.dir.join("images");
    fs::create_dir_all(&images).unwrap();
    let mut json = serde_json::Map::new();
    json.insert(
        KERNEL_KEY.to_owned(),
        serde_json::json!({ "file": "vmlinux", "sha256": kernel.to_string() }),
    );
    for (flavor, digest) in roots.iter() {
        let name = flavor.name();
        let entry =
            serde_json::json!({ "file": format!("{name}.ext4"), "sha256": digest.to_string() });
        json.insert(name.to_owned(), entry);
    }
    let digests = images.join(DIGESTS_FILE);
    fs::write(&digests, serde_json::Value::Object(json).to_string()).unwrap();
    sudo_chown_root(&digests);
    let path = env_file(
        &scratch.dir,
        &[
            (STATE_ROOT, scratch.state.path()),
            (IMAGE_ROOT, &images),
            (GUEST_INIT, &program),
            (ZSTD, Path::new("/usr/bin/zstd")),
        ],
    );
    let id: SessionId = OLD.parse().unwrap();
    let stale = scratch.state.session(&id).snapshot(SnapshotGeneration(1));

    let output = gc(&[], &path);
    let stdout = String::from_utf8(output.stdout).unwrap();
    assert_eq!(output.status.code(), Some(0), "{stdout}");
    assert!(
        stdout.starts_with("vmm gc: dry run, nothing is removed\n"),
        "{stdout}"
    );
    let line = format!("would remove snapshot {OLD} generation 1 (stale, ");
    assert!(stdout.contains(&line), "{stdout}");
    for digest in [old_root, old_initramfs] {
        assert!(
            stdout.contains(&format!("would remove image {digest} (")),
            "{stdout}"
        );
        assert!(scratch.exists(digest));
    }
    assert_eq!(stdout.matches("would remove image ").count(), 2, "{stdout}");
    assert!(stale.is_dir());

    let output = gc(&["--apply"], &path);
    let stdout = String::from_utf8(output.stdout).unwrap();
    assert_eq!(output.status.code(), Some(0), "{stdout}");
    assert!(stdout.starts_with("vmm gc: removing\n"), "{stdout}");
    assert!(!stale.exists());
    for digest in [old_root, old_initramfs] {
        assert!(!scratch.exists(digest), "{digest}");
    }
    for (_, digest) in roots.iter() {
        assert!(scratch.exists(*digest), "{digest}");
    }
    assert!(scratch.exists(kernel));
}
