//! The pass of the two verify hooks on root-owned files (decisions CH76
//! rules 2 and 6). `vmm verify` runs on a root-owned copy of the vmm
//! program, of `ch-remote` and of `guest-init` from
//! `bin/vm-smoke-setup.sh`, and on the CH that the setup script builds
//! with the argv of `bin/lib/chbuild.sh` and installs at `CH_BINARY`
//! (decision CH75). `vmm check-images` runs on a root-owned image root of
//! `.zst` files, as the disks row installs it.
//!
//! `#[ignore]`: only the job `kvm` runs them, after the setup script, with
//! `cargo test --locked --no-fail-fast -- --ignored kvm_`. They need no
//! `/dev/kvm`, only the runner's sudo, which makes the root-owned files
//! (decision CH36 G20). `tests/verify.rs` and `tests/check_images.rs`
//! prove the refusals in the rust job.
#![cfg(target_os = "linux")]

use std::collections::BTreeSet;
use std::ffi::OsString;
use std::fs;
use std::path::{Path, PathBuf};
use std::process::{Command, Output};

use creche_vmm::config::{CH, CH_REMOTE, GUEST_INIT, IMAGE_ROOT, ZSTD};
use creche_vmm::image_root::{DIGESTS_FILE, KERNEL_KEY};
use creche_vmm::verify::{CH_VERSION, GUEST_INIT_STATIC, VMM};
use creche_vmm_confine::slot_profile::CH_BINARY;
use creche_wire::{PerFlavor, SandboxFlavor};
use serde_json::Value;

/// The variable that names the files of the setup script.
const DIR: &str = "CRECHE_KVM_DIR";

/// The host programs of the ubuntu-24.04 runner.
const ZSTD_PROGRAM: &str = "/usr/bin/zstd";
const SHA256SUM: &str = "/usr/bin/sha256sum";

/// A work dir under the files of the setup script that its drop removes.
/// The runner owns the dir, so the drop also removes a root-owned file in
/// it.
struct Work(PathBuf);

impl Drop for Work {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

#[cfg(test)]
impl Work {
    fn new(name: &str) -> Self {
        let files = files();
        let dir = files.join(format!("kvm-verify-{}-{name}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir(&dir).unwrap();
        Self(dir)
    }

    /// Writes `vmm.env` with the header of `bin/fleet-up` and `keys`.
    fn env_file(&self, keys: &[(&str, &Path)]) -> PathBuf {
        let mut text = String::from("# Written by bin/fleet-up, mode 0600.\n");
        for (key, path) in keys {
            text.push_str(&format!("{key}={}\n", path.display()));
        }
        let path = self.0.join("vmm.env");
        fs::write(&path, text).unwrap();
        path
    }

    /// A root-owned copy of `source` named `name` in the dir. A `cp` child
    /// writes the copy, as `tests/binary.rs` `place_program` does. The
    /// write fd then lives only in that child, so no fork of a parallel
    /// test holds it open for writing, and the exec of the copy never
    /// fails with ETXTBSY.
    fn root_copy(&self, source: &Path, name: &str) -> PathBuf {
        let copy = self.0.join(name);
        let status = Command::new("cp").arg(source).arg(&copy).status().unwrap();
        assert!(status.success(), "cp {} failed", source.display());
        sudo_chown_root(&copy);
        copy
    }

    /// An image root of the kernel and one disk for each flavor, each a
    /// `.zst` file of `bytes(key)`, and a `digests.json` that names the
    /// SHA-256 of `named(key)`. Root owns each file.
    fn image_root(
        &self,
        bytes: impl Fn(&str) -> String,
        named: impl Fn(&str) -> String,
    ) -> PathBuf {
        let root = self.0.join("disks");
        let plain = self.0.join("plain");
        fs::create_dir(&root).unwrap();
        fs::create_dir(&plain).unwrap();
        let mut json = serde_json::Map::new();
        for key in keys() {
            let file = format!("{key}.ext4.zst");
            let source = plain.join(key);
            fs::write(&source, bytes(key)).unwrap();
            zstd(&source, &root.join(&file));
            fs::write(&source, named(key)).unwrap();
            json.insert(
                key.to_owned(),
                serde_json::json!({ "file": file, "sha256": sha256(&source) }),
            );
            sudo_chown_root(&root.join(&file));
        }
        let digests = root.join(DIGESTS_FILE);
        fs::write(&digests, Value::Object(json).to_string()).unwrap();
        sudo_chown_root(&digests);
        root
    }
}

#[cfg(test)]
/// The directory of the setup script.
fn files() -> PathBuf {
    let dir = std::env::var_os(DIR)
        .unwrap_or_else(|| panic!("{DIR} is not set: run bin/vm-smoke-setup.sh first"));
    PathBuf::from(dir)
}

#[cfg(test)]
/// Makes root the owner of `path` with the sudo of the runner. Decision
/// CH36 G20 allows sudo on GitHub's throwaway runners only, never on the
/// host.
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

#[cfg(test)]
/// `kernel` and the wire name of each flavor, the keys of `digests.json`.
fn keys() -> Vec<&'static str> {
    std::iter::once(KERNEL_KEY)
        .chain(
            PerFlavor::every(SandboxFlavor::name)
                .iter()
                .map(|(_, &name)| name),
        )
        .collect()
}

#[cfg(test)]
/// Compresses `source` into `out`.
fn zstd(source: &Path, out: &Path) {
    let status = Command::new(ZSTD_PROGRAM)
        .args(["-q", "-o"])
        .arg(out)
        .arg("--")
        .arg(source)
        .status()
        .unwrap();
    assert!(status.success(), "zstd: {status}");
}

#[cfg(test)]
/// The SHA-256 of `path` in hex.
fn sha256(path: &Path) -> String {
    let output = Command::new(SHA256SUM)
        .arg("--")
        .arg(path)
        .output()
        .unwrap();
    assert!(output.status.success(), "sha256sum: {output:?}");
    let text = String::from_utf8(output.stdout).unwrap();
    text.split_whitespace().next().unwrap().to_owned()
}

#[cfg(test)]
/// Runs `program` in the hook mode `mode` on `env_file` and gives its one
/// JSON line.
fn hook(program: &Path, mode: &str, env_file: &Path) -> (Output, Value) {
    let output = Command::new(program)
        .args([mode, "--json", "--env-file"])
        .arg(env_file)
        .env_clear()
        .output()
        .unwrap();
    let text = String::from_utf8(output.stdout.clone()).unwrap();
    let line = text
        .strip_suffix('\n')
        .unwrap_or_else(|| panic!("no line: {output:?}"));
    assert!(!line.contains('\n'), "{text}");
    (output.clone(), serde_json::from_str(line).unwrap())
}

#[cfg(test)]
/// Each check name of `report` whose `ok` is `ok`.
fn names(report: &Value, ok: bool) -> Vec<String> {
    report["checks"]
        .as_array()
        .unwrap()
        .iter()
        .filter(|check| check["ok"] == ok)
        .map(|check| check["name"].as_str().unwrap().to_owned())
        .collect()
}

#[cfg(test)]
/// The file names in `dir`.
fn listing(dir: &Path) -> BTreeSet<OsString> {
    fs::read_dir(dir)
        .unwrap()
        .map(|entry| entry.unwrap().file_name())
        .collect()
}

/// `vmm verify` passes on root-owned copies of the CI build: the vmm
/// program, the CH of decision CH75, `ch-remote` and the static guest
/// init.
#[test]
#[ignore = "needs bin/vm-smoke-setup.sh and runner sudo: the job kvm runs it"]
fn kvm_verify_passes_on_the_ci_built_programs() {
    let work = Work::new("pass");
    let files = files();
    let vmm = work.root_copy(Path::new(env!("CARGO_BIN_EXE_vmm")), "vmm");
    let ch_remote = work.root_copy(&files.join("ch-remote"), "ch-remote");
    let guest_init = work.root_copy(&files.join("guest-init"), "guest-init");
    let env_file = work.env_file(&[
        (CH, Path::new(CH_BINARY)),
        (CH_REMOTE, &ch_remote),
        (GUEST_INIT, &guest_init),
    ]);
    let (output, report) = hook(&vmm, "verify", &env_file);
    assert_eq!(output.status.code(), Some(0), "{report}");
    assert_eq!(report["ok"], true, "{report}");
    for name in [
        VMM,
        CH,
        CH_REMOTE,
        GUEST_INIT,
        CH_VERSION,
        GUEST_INIT_STATIC,
    ] {
        assert!(
            names(&report, true).iter().any(|found| found == name),
            "{name}: {report}"
        );
    }

    // A root-owned dynamic program is no guest init. The runner's bash
    // has a PT_INTERP header, so only its static check fails.
    let env_file = work.env_file(&[
        (CH, Path::new(CH_BINARY)),
        (CH_REMOTE, &ch_remote),
        (GUEST_INIT, Path::new("/usr/bin/bash")),
    ]);
    let (output, report) = hook(&vmm, "verify", &env_file);
    assert_eq!(output.status.code(), Some(1), "{report}");
    assert_eq!(names(&report, false), [GUEST_INIT_STATIC], "{report}");
}

/// `vmm check-images` passes on a root-owned image root of `.zst` files
/// and writes no file in it.
#[test]
#[ignore = "needs runner sudo for root-owned files: the job kvm runs it"]
fn kvm_check_images_passes_on_root_owned_disks() {
    let work = Work::new("images");
    let root = work.image_root(
        |key| format!("the {key} bytes"),
        |key| format!("the {key} bytes"),
    );
    let before = listing(&root);
    let env_file = work.env_file(&[(IMAGE_ROOT, &root), (ZSTD, Path::new(ZSTD_PROGRAM))]);
    let (output, report) = hook(
        Path::new(env!("CARGO_BIN_EXE_vmm")),
        "check-images",
        &env_file,
    );
    assert_eq!(output.status.code(), Some(0), "{report}");
    assert_eq!(report["ok"], true, "{report}");
    let passed = names(&report, true);
    for key in keys() {
        assert!(passed.iter().any(|found| found == key), "{key}: {report}");
    }
    assert_eq!(listing(&root), before);
}

/// A disk whose decompressed bytes differ from its entry fails, and the
/// report names the entry. Each other entry passes.
#[test]
#[ignore = "needs runner sudo for root-owned files: the job kvm runs it"]
fn kvm_check_images_fails_on_a_changed_disk() {
    let work = Work::new("changed");
    let changed = SandboxFlavor::Python.name();
    let root = work.image_root(
        |key| {
            if key == changed {
                "a changed disk".to_owned()
            } else {
                format!("the {key} bytes")
            }
        },
        |key| format!("the {key} bytes"),
    );
    let env_file = work.env_file(&[(IMAGE_ROOT, &root), (ZSTD, Path::new(ZSTD_PROGRAM))]);
    let (output, report) = hook(
        Path::new(env!("CARGO_BIN_EXE_vmm")),
        "check-images",
        &env_file,
    );
    assert_eq!(output.status.code(), Some(1), "{report}");
    assert_eq!(names(&report, false), [changed], "{report}");
    let detail = report["checks"]
        .as_array()
        .unwrap()
        .iter()
        .find(|check| check["name"] == changed)
        .unwrap()["detail"]
        .as_str()
        .unwrap()
        .to_owned();
    assert!(
        detail.starts_with(&format!("{changed}.ext4.zst: the SHA-256 is ")),
        "{detail}"
    );
}
