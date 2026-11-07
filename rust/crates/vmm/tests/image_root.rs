//! The file checks of `image_root::read` (decision CH57 rule 1). `read`
//! trusts only a file that root owns. A test of the rust job runs as the
//! runner user and cannot make a root-owned file, so the rust job proves
//! that such a file refuses. The `kvm_` tests prove the accept path: the
//! job kvm runs each ignored `kvm_` test, and its runner may use sudo
//! (decision CH36 G20). `src/image_root/tests.rs` proves the parse and the
//! import on the bytes.

use std::fs;
use std::os::unix::fs::{MetadataExt, PermissionsExt, symlink};
use std::path::{Path, PathBuf};
use std::process::Command;

use creche_vmm::image_root::{DIGESTS_CAP, DIGESTS_FILE, DigestsError, KERNEL_KEY, read};
use creche_vmm::root_owned::OwnerError;
use creche_vmm::start::Digests;
use creche_vmm_store::images::ImageStore;
use creche_vmm_store::layout::StateRoot;
use creche_wire::{PerFlavor, SandboxFlavor};

/// The SHA-256 of no bytes.
const EMPTY: &str = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855";

/// The SHA-256 of `abc`.
const ABC: &str = "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad";

/// A scratch image root that its drop removes. The test user owns the
/// dir, so the drop also removes a root-owned file in it.
struct Root(PathBuf);

impl Drop for Root {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

#[cfg(test)]
impl Root {
    /// An image root with the file `vmlinux` (no bytes), one file
    /// `<flavor>.ext4` (`abc`) for each flavor, and a good `digests.json`
    /// that the test user owns.
    fn new(name: &str) -> Self {
        let dir =
            std::env::temp_dir().join(format!("creche-vmm-images-{}-{name}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(dir.join("images")).unwrap();
        fs::write(dir.join("images/vmlinux"), "").unwrap();
        for (_, &flavor) in PerFlavor::every(SandboxFlavor::name).iter() {
            fs::write(dir.join(format!("images/{flavor}.ext4")), "abc").unwrap();
        }
        let root = Self(dir);
        fs::write(root.digests(), good()).unwrap();
        root
    }

    fn images(&self) -> PathBuf {
        self.0.join("images")
    }

    fn digests(&self) -> PathBuf {
        self.images().join(DIGESTS_FILE)
    }

    /// Writes `text` as a new `digests.json` with `mode`, then makes root
    /// its owner.
    fn write_as_root(&self, text: &str, mode: u32) {
        let _ = fs::remove_file(self.digests());
        fs::write(self.digests(), text).unwrap();
        fs::set_permissions(self.digests(), fs::Permissions::from_mode(mode)).unwrap();
        chown_root(&self.digests());
    }

    fn read(&self) -> Result<Digests, String> {
        let set = read(&self.images()).map_err(|error| error.to_string())?;
        let state = StateRoot::new(self.0.join("state")).map_err(|error| format!("{error:?}"))?;
        let store = ImageStore::new(&state, PathBuf::from("/usr/bin/zstd"));
        set.import(&store).map_err(|error| error.to_string())
    }

    fn fault(&self) -> DigestsError {
        read(&self.images()).unwrap_err()
    }
}

/// Makes root the owner of `path` with the sudo of the runner. Decision
/// CH36 G20 allows sudo on GitHub's throwaway runners only, never on the
/// host, so it refuses outside GitHub Actions.
#[cfg(test)]
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

/// The good `digests.json`: the kernel and one entry for each flavor.
#[cfg(test)]
fn good() -> String {
    let entry = |file: &str, sha256: &str| serde_json::json!({ "file": file, "sha256": sha256 });
    let mut json = serde_json::Map::new();
    json.insert(KERNEL_KEY.to_owned(), entry("vmlinux", EMPTY));
    for (_, &flavor) in PerFlavor::every(SandboxFlavor::name).iter() {
        json.insert(flavor.to_owned(), entry(&format!("{flavor}.ext4"), ABC));
    }
    serde_json::Value::Object(json).to_string()
}

#[test]
fn a_missing_file_refuses() {
    let root = Root::new("missing");
    fs::remove_file(root.digests()).unwrap();
    let fault = root.fault();
    assert!(matches!(fault, DigestsError::Missing), "{fault:?}");
    assert_eq!(
        fault.to_string(),
        "VMM_IMAGE_ROOT: the digests.json is missing"
    );
}

#[test]
fn a_symlink_or_a_dir_refuses() {
    let root = Root::new("link");
    let target = root.0.join("elsewhere.json");
    fs::rename(root.digests(), &target).unwrap();
    symlink(&target, root.digests()).unwrap();
    assert!(matches!(
        root.fault(),
        DigestsError::Owner(OwnerError::NotRegular)
    ));

    fs::remove_file(root.digests()).unwrap();
    fs::create_dir(root.digests()).unwrap();
    assert!(matches!(
        root.fault(),
        DigestsError::Owner(OwnerError::NotRegular)
    ));
}

/// A good file that the test user owns refuses, and so the import never
/// runs: no digest reaches the store. A test that runs as root owns a
/// root-owned file, so the case has nothing to prove there.
#[test]
fn a_file_that_root_does_not_own_refuses() {
    let root = Root::new("owner");
    let uid = fs::metadata(root.digests()).unwrap().uid();
    if uid == 0 {
        return;
    }
    let fault = root.read().unwrap_err();
    assert_eq!(
        fault,
        format!("VMM_IMAGE_ROOT: the digests.json is owned by uid {uid}, not by root")
    );
    assert!(matches!(
        root.fault(),
        DigestsError::Owner(OwnerError::NotOwned { uid: found }) if found == uid
    ));
    let state = StateRoot::new(root.0.join("state")).unwrap();
    let store = ImageStore::new(&state, PathBuf::from("/usr/bin/zstd"));
    for digest in [EMPTY, ABC] {
        assert!(store.check(digest.parse().unwrap()).is_err(), "{digest}");
    }
}

/// The accept path: a root-owned file that only root may write gives the
/// digests after the import.
#[test]
#[ignore = "needs runner sudo for a root-owned file: the job kvm runs it"]
fn kvm_a_root_owned_file_gives_the_digests() {
    let root = Root::new("trusted");
    for mode in [0o644, 0o444] {
        root.write_as_root(&good(), mode);
        let expected = Digests {
            kernel: EMPTY.parse().unwrap(),
            roots: PerFlavor::every(|_| ABC.parse().unwrap()),
        };
        assert_eq!(root.read().unwrap(), expected, "{mode:o}");
    }
}

#[test]
#[ignore = "needs runner sudo for a root-owned file: the job kvm runs it"]
fn kvm_a_root_owned_file_that_others_may_write_refuses() {
    let root = Root::new("writable");
    for mode in [0o664, 0o646, 0o666] {
        root.write_as_root(&good(), mode);
        let fault = root.fault();
        assert!(
            matches!(fault, DigestsError::Owner(OwnerError::Writable { mode: found }) if found == mode),
            "{mode:o} {fault:?}"
        );
    }
}

#[test]
#[ignore = "needs runner sudo for a root-owned file: the job kvm runs it"]
fn kvm_a_root_owned_file_past_the_cap_or_malformed_refuses() {
    let root = Root::new("large");
    let padded = format!(
        "{}{}",
        good(),
        " ".repeat(usize::try_from(DIGESTS_CAP).unwrap())
    );
    root.write_as_root(&padded, 0o644);
    assert!(matches!(root.fault(), DigestsError::TooLarge));

    root.write_as_root("{", 0o644);
    assert!(matches!(root.fault(), DigestsError::Malformed(_)));
}
