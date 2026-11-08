//! `vmm check-images --json --env-file <path>`, the verify hook of the
//! disks row (decision CH76 rule 6). `image_root::read` trusts only a
//! root-owned `digests.json` (decision CH57 rule 1), and a test of the
//! rust job cannot make one. So the rust job proves the refusals here,
//! and `src/image_root/tests.rs` proves the digest check of each entry on
//! the bytes. `tests/kvm_verify.rs` proves the pass and a changed disk on
//! root-owned files (decision CH36 G20).

use std::fs;
use std::os::unix::fs::MetadataExt;
use std::path::{Path, PathBuf};
use std::process::{Command, Output};

use creche_vmm::config::{IMAGE_ROOT, ZSTD};
use creche_vmm::image_root::{DIGESTS_FILE, KERNEL_KEY};
use creche_vmm::verify::ENV_FILE;
use creche_wire::{PerFlavor, SandboxFlavor};
use serde_json::Value;

/// The SHA-256 of no bytes.
const EMPTY: &str = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855";

/// A scratch image root that its drop removes.
struct Scratch(PathBuf);

impl Drop for Scratch {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

#[cfg(test)]
impl Scratch {
    /// An image root with an empty file for the kernel and each flavor,
    /// and a good `digests.json` that the test user owns.
    fn new(name: &str) -> Self {
        let dir = std::env::temp_dir().join(format!(
            "creche-vmm-check-images-{}-{name}",
            std::process::id()
        ));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(dir.join("images")).unwrap();
        let mut json = serde_json::Map::new();
        let flavors = PerFlavor::every(SandboxFlavor::name);
        let names = std::iter::once(KERNEL_KEY).chain(flavors.iter().map(|(_, &name)| name));
        for key in names {
            let file = format!("{key}.img");
            fs::write(dir.join("images").join(&file), "").unwrap();
            json.insert(
                key.to_owned(),
                serde_json::json!({ "file": file, "sha256": EMPTY }),
            );
        }
        let digests = Value::Object(json).to_string();
        fs::write(dir.join("images").join(DIGESTS_FILE), digests).unwrap();
        Self(dir)
    }

    fn images(&self) -> PathBuf {
        self.0.join("images")
    }

    /// Writes `vmm.env` with a `#` header and `keys`.
    fn env_file(&self, keys: &[(&str, &Path)]) -> PathBuf {
        let mut text = String::from("# Written by bin/fleet-up, mode 0600.\n");
        for (key, path) in keys {
            text.push_str(&format!("{key}={}\n", path.display()));
        }
        let path = self.0.join("vmm.env");
        fs::write(&path, text).unwrap();
        path
    }
}

#[cfg(test)]
/// Runs `vmm check-images` on `env_file` and gives its one JSON line.
fn check_images(env_file: &Path) -> (Output, Value) {
    let output = Command::new(env!("CARGO_BIN_EXE_vmm"))
        .args(["check-images", "--json", "--env-file"])
        .arg(env_file)
        .env_clear()
        .output()
        .unwrap();
    let text = String::from_utf8(output.stdout.clone()).unwrap();
    let line = text.strip_suffix('\n').unwrap();
    assert!(!line.contains('\n'), "{text}");
    let report = serde_json::from_str(line).unwrap();
    (output, report)
}

#[cfg(test)]
/// The checks of `report`: each name with its `ok` and its detail.
fn checks(report: &Value) -> Vec<(String, bool, String)> {
    report["checks"]
        .as_array()
        .unwrap()
        .iter()
        .map(|check| {
            (
                check["name"].as_str().unwrap().to_owned(),
                check["ok"].as_bool().unwrap(),
                check["detail"].as_str().unwrap().to_owned(),
            )
        })
        .collect()
}

/// A good `digests.json` that the test user owns fails, and no entry is
/// hashed. A test that runs as root has nothing to prove here.
#[test]
fn a_digests_json_the_vmm_user_owns_fails() {
    let scratch = Scratch::new("owner");
    let images = scratch.images();
    let uid = fs::metadata(images.join(DIGESTS_FILE)).unwrap().uid();
    if uid == 0 {
        return;
    }
    let env_file = scratch.env_file(&[(IMAGE_ROOT, &images), (ZSTD, Path::new("/usr/bin/zstd"))]);
    let (output, report) = check_images(&env_file);
    assert_eq!(output.status.code(), Some(1), "{output:?}");
    assert_eq!(report["ok"], false);
    let found = checks(&report);
    assert_eq!(found.len(), 2, "{found:?}");
    assert_eq!(found[0].0, ENV_FILE);
    assert!(found[0].1);
    assert_eq!(
        found[1],
        (
            DIGESTS_FILE.to_owned(),
            false,
            format!("VMM_IMAGE_ROOT: the digests.json is owned by uid {uid}, not by root")
        )
    );
}

/// The vmm compiles in no default (decision CH59 rule 1), so a key that
/// the file lacks fails, and the report names the key. A missing
/// `digests.json` fails with the fault of the vmm start.
#[test]
fn a_missing_key_or_file_fails_check_images() {
    let scratch = Scratch::new("missing");
    let images = scratch.images();
    let env_file = scratch.env_file(&[(IMAGE_ROOT, &images)]);
    let (output, report) = check_images(&env_file);
    assert_eq!(output.status.code(), Some(1), "{output:?}");
    assert_eq!(
        checks(&report),
        [(
            ENV_FILE.to_owned(),
            false,
            format!("{ZSTD}: the key has no value")
        )]
    );

    fs::remove_file(images.join(DIGESTS_FILE)).unwrap();
    let env_file = scratch.env_file(&[(IMAGE_ROOT, &images), (ZSTD, Path::new("/usr/bin/zstd"))]);
    let (output, report) = check_images(&env_file);
    assert_eq!(output.status.code(), Some(1), "{output:?}");
    let found = checks(&report);
    assert_eq!(
        found.last().unwrap(),
        &(
            DIGESTS_FILE.to_owned(),
            false,
            "VMM_IMAGE_ROOT: the digests.json is missing".to_owned()
        )
    );
}
