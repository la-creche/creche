//! The program `vmm gc`. `image_root::read` and
//! `guest_init::initramfs_digest` trust only root-owned files, and a test
//! of the rust job cannot make one. So the binary tests of the rust job
//! prove the refusals, each with exit 78 and no file removed. The ignored
//! `kvm_` test makes both files root-owned with the runner's sudo
//! (decision CH36 G20) and runs the binary end to end. Only the job `kvm`
//! runs it. These tests start processes, so they are not in `tests/gc.rs`
//! with the lock tests (see its module doc).

use std::fs;
use std::os::unix::fs::MetadataExt;
use std::path::{Path, PathBuf};
use std::process::{Command, Output};

use creche_vmm::config::{BUDGET, GUEST_INIT, IMAGE_ROOT, STATE_ROOT, ZSTD};
use creche_vmm::guest_init::initramfs_digest;
use creche_vmm::image_root::{DIGESTS_FILE, KERNEL_KEY};
use creche_vmm_store::layout::SnapshotGeneration;
use creche_wire::{PerFlavor, SandboxFlavor, SessionId};

mod gc_host;

use gc_host::{Host, NEW, OLD, Scratch};

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
        assert!(host.scratch.exists(host.old_root));
        assert!(host.scratch.exists(host.old_initramfs));
        assert!(host.leftover.exists());
        host.current_stays();
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
