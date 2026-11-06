//! The `digests.json` of the image root (decisions CH57 rule 1, CH58
//! rules 4 and 5). A test cannot make a root-owned file, so each test
//! passes its own uid as the owner, and one case proves that `Owner::ROOT`
//! refuses the file of the test. The fixture takes its flavors from the
//! wire enum, as the vmm does.

use std::fs;
use std::os::unix::fs::{MetadataExt, PermissionsExt, symlink};
use std::path::PathBuf;

use creche_vmm::flavors::{PerFlavor, all};
use creche_vmm::image_root::{DIGESTS_CAP, DIGESTS_FILE, DigestsError, KERNEL_KEY, Owner, read};
use creche_vmm::start::Digests;
use creche_vmm_store::images::{ImageStore, ImportError};
use creche_vmm_store::layout::StateRoot;

/// The SHA-256 of no bytes.
const EMPTY: &str = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855";

/// The SHA-256 of `abc`.
const ABC: &str = "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad";

/// The SHA-256 of `hello world`.
const HELLO: &str = "b94d27b9934d3e08a52e52d7da7dabfac484efe37a5380ee9088f7ace2efcde9";

/// A scratch image root that its drop removes.
struct Root(PathBuf);

impl Drop for Root {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

#[cfg(test)]
impl Root {
    /// An image root with the file `vmlinux` (no bytes), one file
    /// `<flavor>.ext4` (`abc`) for each flavor, and a good `digests.json`.
    fn new(name: &str) -> Self {
        let dir =
            std::env::temp_dir().join(format!("creche-vmm-images-{}-{name}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(dir.join("images")).unwrap();
        fs::write(dir.join("images/vmlinux"), "").unwrap();
        for (flavor, _) in all().unwrap() {
            fs::write(dir.join(format!("images/{flavor}.ext4")), "abc").unwrap();
        }
        let root = Self(dir);
        root.write(&good());
        root
    }

    fn images(&self) -> PathBuf {
        self.0.join("images")
    }

    fn digests(&self) -> PathBuf {
        self.images().join(DIGESTS_FILE)
    }

    fn write(&self, text: &str) {
        fs::write(self.digests(), text).unwrap();
    }

    fn owner(&self) -> Owner {
        Owner::new(fs::metadata(&self.0).unwrap().uid())
    }

    fn read(&self) -> Result<Digests, String> {
        let set = read(&self.images(), self.owner()).map_err(|error| error.to_string())?;
        let state = StateRoot::new(self.0.join("state")).map_err(|error| format!("{error:?}"))?;
        let store = ImageStore::new(&state, PathBuf::from("/usr/bin/zstd"));
        set.import(&store).map_err(|error| error.to_string())
    }

    fn fault(&self) -> DigestsError {
        read(&self.images(), self.owner()).unwrap_err()
    }
}

#[cfg(test)]
fn entry(file: &str, sha256: &str) -> serde_json::Value {
    serde_json::json!({ "file": file, "sha256": sha256 })
}

/// The good `digests.json`: the kernel and one entry for each flavor.
#[cfg(test)]
fn good_json() -> serde_json::Map<String, serde_json::Value> {
    let mut json = serde_json::Map::new();
    json.insert(KERNEL_KEY.to_owned(), entry("vmlinux", EMPTY));
    for (flavor, _) in all().unwrap() {
        json.insert(flavor.to_owned(), entry(&format!("{flavor}.ext4"), ABC));
    }
    json
}

#[cfg(test)]
fn good() -> String {
    serde_json::Value::Object(good_json()).to_string()
}

#[test]
fn a_trusted_file_gives_the_digests_after_the_import() {
    let root = Root::new("good");
    let digests = root.read().unwrap();
    let expected = Digests {
        kernel: EMPTY.parse().unwrap(),
        roots: PerFlavor::every(|_| ABC.parse().unwrap()).unwrap(),
    };
    assert_eq!(digests, expected);
    // A second read finds each digest in the store and imports nothing.
    fs::remove_file(root.images().join("vmlinux")).unwrap();
    assert_eq!(root.read().unwrap(), expected);
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
    assert!(matches!(root.fault(), DigestsError::NotRegular));

    fs::remove_file(root.digests()).unwrap();
    fs::create_dir(root.digests()).unwrap();
    assert!(matches!(root.fault(), DigestsError::NotRegular));
}

/// The test owns its file, so `Owner::ROOT` refuses it. A test that runs
/// as root owns a root-owned file, so the case has nothing to prove.
#[test]
fn a_file_that_root_does_not_own_refuses() {
    let root = Root::new("owner");
    let uid = fs::metadata(root.digests()).unwrap().uid();
    if uid == 0 {
        return;
    }
    let fault = read(&root.images(), Owner::ROOT).unwrap_err();
    assert!(
        matches!(fault, DigestsError::NotOwned { uid: found } if found == uid),
        "{fault:?}"
    );
    assert_eq!(
        fault.to_string(),
        format!("VMM_IMAGE_ROOT: the digests.json is owned by uid {uid}, not by root")
    );
}

#[test]
fn a_file_that_others_may_write_refuses() {
    let root = Root::new("writable");
    for mode in [0o664, 0o646, 0o666] {
        fs::set_permissions(root.digests(), fs::Permissions::from_mode(mode)).unwrap();
        let fault = root.fault();
        assert!(
            matches!(fault, DigestsError::Writable { mode: found } if found == mode),
            "{mode:o} {fault:?}"
        );
    }
    fs::set_permissions(root.digests(), fs::Permissions::from_mode(0o444)).unwrap();
    assert!(root.read().is_ok());
}

#[test]
fn a_file_past_the_cap_refuses() {
    let root = Root::new("large");
    let padded = format!(
        "{}{}",
        good(),
        " ".repeat(usize::try_from(DIGESTS_CAP).unwrap())
    );
    root.write(&padded);
    assert!(matches!(root.fault(), DigestsError::TooLarge));
}

#[test]
fn a_malformed_file_refuses() {
    let root = Root::new("malformed");
    let other = |key: &str, value: serde_json::Value| {
        let mut json = good_json();
        json.insert(key.to_owned(), value);
        serde_json::Value::Object(json).to_string()
    };
    let cases = [
        String::new(),
        "{".to_owned(),
        "[]".to_owned(),
        format!("{} {{}}", good()),
        other("extra", entry("vmlinux", EMPTY)),
        other("Base", entry("base.ext4", ABC)),
        other("base", serde_json::json!({ "file": "base.ext4" })),
        other(
            "base",
            serde_json::json!({ "file": "base.ext4", "sha256": ABC, "size": 3 }),
        ),
        other("base", entry("base.ext4", &ABC.to_uppercase())),
        other("base", entry("base.ext4", ABC.get(1..).unwrap())),
        other(
            "base",
            entry("base.ext4", &format!("{}g", ABC.get(1..).unwrap())),
        ),
        other("base", entry("../base.ext4", ABC)),
        other("base", entry("sub/base.ext4", ABC)),
        other("base", entry("/base.ext4", ABC)),
        other("base", entry(".", ABC)),
        other("base", entry("..", ABC)),
        other("base", entry("base.ext4/", ABC)),
        other("base", entry("", ABC)),
    ];
    for case in cases {
        root.write(&case);
        let fault = root.fault();
        assert!(
            matches!(fault, DigestsError::Malformed(_)),
            "{case}: {fault:?}"
        );
    }
}

/// Decision CH58 rule 4: the kernel and each flavor of the wire enum, each
/// once. A file that lacks one of them, or repeats one, refuses.
#[test]
fn a_file_that_lacks_or_repeats_a_key_refuses() {
    let root = Root::new("keys");
    let keys: Vec<String> = good_json().keys().cloned().collect();
    for key in &keys {
        let mut json = good_json();
        json.remove(key);
        root.write(&serde_json::Value::Object(json).to_string());
        let fault = root.fault();
        assert!(
            matches!(fault, DigestsError::Malformed(_)),
            "{key}: {fault:?}"
        );
        assert!(fault.to_string().contains(&format!("`{key}`")), "{fault}");

        let text = good();
        let repeated = format!(
            "{},\"{key}\":{}}}",
            text.get(..text.len() - 1).unwrap(),
            entry("vmlinux", EMPTY)
        );
        root.write(&repeated);
        let fault = root.fault();
        assert!(
            matches!(fault, DigestsError::Malformed(_)),
            "{key}: {fault:?}"
        );
        assert!(fault.to_string().contains(&format!("`{key}`")), "{fault}");
    }
}

/// Decision CH58 rule 5: an entry that does not import refuses the start,
/// and the error names its file.
#[test]
fn a_file_that_does_not_import_names_the_file() {
    let root = Root::new("import");
    root.write(&other_sha("base", HELLO));
    let set = read(&root.images(), root.owner()).unwrap();
    let state = StateRoot::new(root.0.join("state")).unwrap();
    let store = ImageStore::new(&state, PathBuf::from("/usr/bin/zstd"));
    let error = set.import(&store).unwrap_err();
    assert_eq!(error.file, "base.ext4");
    assert!(matches!(error.source, ImportError::Mismatch { .. }));
    assert!(
        error
            .to_string()
            .starts_with("VMM_IMAGE_ROOT: cannot import base.ext4: the SHA-256 is "),
        "{error}"
    );

    // A missing disk refuses too. The failed import above stored no root
    // disk, so this import reads the file.
    fs::remove_file(root.images().join("base.ext4")).unwrap();
    root.write(&good());
    let set = read(&root.images(), root.owner()).unwrap();
    let error = set.import(&store).unwrap_err();
    assert_eq!(error.file, "base.ext4");
    assert!(matches!(error.source, ImportError::Io { .. }), "{error}");
}

/// The good `digests.json` with the sha256 of `key` set to `sha256`.
#[cfg(test)]
fn other_sha(key: &str, sha256: &str) -> String {
    let mut json = good_json();
    json.insert(key.to_owned(), entry(&format!("{key}.ext4"), sha256));
    serde_json::Value::Object(json).to_string()
}
