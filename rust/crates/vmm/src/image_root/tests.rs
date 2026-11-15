//! The parse of `digests.json` and the import of its files (decisions
//! CH58 rules 4 and 5). `read` trusts only a root-owned file, and a test
//! of the rust job cannot make one. So these tests parse the bytes with
//! the `Deserialize` of `Manifest`, the parse that `read` runs, and build
//! the `ImageSet` from its fields. `tests/image_root.rs` proves the file
//! checks of `read`, and its `kvm_` tests prove a root-owned file end to
//! end. The fixture takes its flavors and its service images from the
//! wire enums, as the vmm does.

use std::fs;
use std::path::PathBuf;

use creche_vmm_store::images::{ImageStore, ImportError};
use creche_vmm_store::layout::StateRoot;
use creche_wire::{PerFlavor, PerService, SandboxFlavor, ServiceImage};

use super::{DIGESTS_FILE, EntryFault, ImageSet, KERNEL_KEY, Manifest};
use crate::start::Digests;

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

impl Root {
    /// An image root with the file `vmlinux` (no bytes) and one file
    /// `<name>.ext4` (`abc`) for each flavor and each service image.
    fn new(name: &str) -> Self {
        let dir =
            std::env::temp_dir().join(format!("creche-vmm-manifest-{}-{name}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(dir.join("images")).unwrap();
        fs::write(dir.join("images/vmlinux"), "").unwrap();
        for name in disk_names() {
            fs::write(dir.join(format!("images/{name}.ext4")), "abc").unwrap();
        }
        Self(dir)
    }

    fn images(&self) -> PathBuf {
        self.0.join("images")
    }

    /// The image set of `text` in this root.
    fn set(&self, text: &str) -> ImageSet {
        ImageSet {
            root: self.images(),
            manifest: parse(text).unwrap(),
            bytes: text.as_bytes().to_vec(),
        }
    }

    fn store(&self) -> ImageStore {
        let state = StateRoot::new(self.0.join("state")).unwrap();
        ImageStore::new(&state, PathBuf::from("/usr/bin/zstd"))
    }
}

/// The parse of `read`: the strict shape of the module doc.
fn parse(text: &str) -> Result<Manifest, serde_json::Error> {
    serde_json::from_str(text)
}

fn entry(file: &str, sha256: &str) -> serde_json::Value {
    serde_json::json!({ "file": file, "sha256": sha256 })
}

/// The wire name of each flavor, then of each service image: the keys of
/// `digests.json` that name a root disk, in the order of `check`.
fn disk_names() -> Vec<&'static str> {
    let flavors = PerFlavor::every(SandboxFlavor::name);
    let services = PerService::every(ServiceImage::name);
    let flavors = flavors.iter().map(|(_, &name)| name);
    flavors
        .chain(services.iter().map(|(_, &name)| name))
        .collect()
}

/// The good `digests.json`: the kernel and one entry for each flavor and
/// each service image.
fn good_json() -> serde_json::Map<String, serde_json::Value> {
    let mut json = serde_json::Map::new();
    json.insert(KERNEL_KEY.to_owned(), entry("vmlinux", EMPTY));
    for name in disk_names() {
        json.insert(name.to_owned(), entry(&format!("{name}.ext4"), ABC));
    }
    json
}

fn good() -> String {
    serde_json::Value::Object(good_json()).to_string()
}

/// The good `digests.json` with the sha256 of `key` set to `sha256`.
fn other_sha(key: &str, sha256: &str) -> String {
    let mut json = good_json();
    json.insert(key.to_owned(), entry(&format!("{key}.ext4"), sha256));
    serde_json::Value::Object(json).to_string()
}

#[test]
fn a_good_file_gives_the_digests_after_the_import() {
    let root = Root::new("good");
    let store = root.store();
    let set = root.set(&good());
    let expected = Digests {
        kernel: EMPTY.parse().unwrap(),
        roots: PerFlavor::every(|_| ABC.parse().unwrap()),
        services: PerService::every(|_| Some(ABC.parse().unwrap())),
    };
    assert_eq!(set.import(&store).unwrap(), expected);
    // A second import finds each digest in the store and reads no file.
    fs::remove_file(root.images().join("vmlinux")).unwrap();
    assert_eq!(set.import(&store).unwrap(), expected);
}

/// `digests` gives the digests of the file with no import: the digests
/// that the import gives, also with no file in the image root.
#[test]
fn the_digests_of_a_set_need_no_import() {
    let root = Root::new("digests");
    let set = root.set(&other_sha("python", HELLO));
    fs::remove_dir_all(root.images()).unwrap();
    let expected = Digests {
        kernel: EMPTY.parse().unwrap(),
        roots: PerFlavor::every(|flavor| match flavor {
            SandboxFlavor::Python => HELLO.parse().unwrap(),
            SandboxFlavor::Base | SandboxFlavor::Coding => ABC.parse().unwrap(),
        }),
        services: PerService::every(|_| Some(ABC.parse().unwrap())),
    };
    assert_eq!(set.digests(), expected);
}

#[test]
fn a_malformed_file_refuses() {
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
        other("Browser", entry("browser.ext4", ABC)),
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
    assert!(parse(&good()).is_ok());
    for case in cases {
        assert!(parse(&case).is_err(), "{case}");
    }
}

/// Decision CH58 rule 4: the kernel and each flavor of the wire enum,
/// each once. A file that lacks one of them refuses. A file that repeats
/// any key, a service image too, refuses.
#[test]
fn a_file_that_lacks_or_repeats_a_key_refuses() {
    let services = PerService::every(ServiceImage::name);
    let keys: Vec<String> = good_json().keys().cloned().collect();
    for key in &keys {
        if !services.iter().any(|(_, &name)| name == key) {
            let mut json = good_json();
            json.remove(key);
            let error = parse(&serde_json::Value::Object(json).to_string()).unwrap_err();
            assert!(error.to_string().contains(&format!("`{key}`")), "{error}");
        }

        let text = good();
        let repeated = format!(
            "{},\"{key}\":{}}}",
            text.get(..text.len() - 1).unwrap(),
            entry("vmlinux", EMPTY)
        );
        let error = parse(&repeated).unwrap_err();
        assert!(error.to_string().contains(&format!("`{key}`")), "{error}");
    }
}

/// Decision CH58 rule 5: an entry that does not import refuses the start,
/// and the error names its file.
#[test]
fn a_file_that_does_not_import_names_the_file() {
    let root = Root::new("import");
    let store = root.store();
    let error = root
        .set(&other_sha("base", HELLO))
        .import(&store)
        .unwrap_err();
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
    let error = root.set(&good()).import(&store).unwrap_err();
    assert_eq!(error.file, "base.ext4");
    assert!(matches!(error.source, ImportError::Io { .. }), "{error}");
}

/// The shared vector `rust/crates/wire/vectors/digests.json`: one good
/// file in the shape that `disks/bin/playpen-release-disks` writes for the
/// disks row (decisions CH58 rule 4, CH76 rule 5).
/// `bin/tests/test_playpen_release_disks.py` holds the output of the
/// script to the same keys and file names, so the two cannot drift apart.
const SHARED_VECTOR: &str = include_str!("../../../wire/vectors/digests.json");

#[test]
fn the_shared_vector_reads() {
    let manifest = parse(SHARED_VECTOR).unwrap();
    assert_eq!(manifest.kernel.file.0, "vmlinux-x86_64.zst");
    for (flavor, entry) in manifest.roots.iter() {
        assert_eq!(entry.file.0, format!("{}.ext4.zst", flavor.name()));
    }
    for (image, entry) in manifest.services.iter() {
        let entry = entry.as_ref().unwrap();
        assert_eq!(entry.file.0, format!("{}.ext4.zst", image.name()));
    }
}

/// Decision CH76 rule 6: `check` hashes each entry, passes each good one,
/// and writes no file: no store dir appears.
#[test]
fn a_good_set_checks_and_writes_no_file() {
    let root = Root::new("check");
    let checks = root.set(&good()).check(&PathBuf::from("/usr/bin/zstd"));
    let keys: Vec<&str> = checks.iter().map(|check| check.key).collect();
    let expected: Vec<&str> = std::iter::once(KERNEL_KEY).chain(disk_names()).collect();
    assert_eq!(keys, expected);
    for check in &checks {
        assert!(check.result.is_ok(), "{check:?}");
    }
    assert!(!root.0.join("state").exists());
}

/// A wrong digest fails its entry and names it, and the check goes on to
/// each later entry. A missing file fails its entry too.
#[test]
fn a_wrong_digest_fails_and_names_the_entry() {
    let root = Root::new("check-wrong");
    fs::remove_file(root.images().join("vmlinux")).unwrap();
    let checks = root
        .set(&other_sha("base", HELLO))
        .check(&PathBuf::from("/usr/bin/zstd"));
    let failed: Vec<(&str, &str)> = checks
        .iter()
        .filter(|check| check.result.is_err())
        .map(|check| (check.key, check.file.as_str()))
        .collect();
    assert_eq!(failed, [(KERNEL_KEY, "vmlinux"), ("base", "base.ext4")]);
    let base = checks.iter().find(|check| check.key == "base").unwrap();
    assert!(
        matches!(
            base.result,
            Err(EntryFault::Import(ImportError::Mismatch { .. }))
        ),
        "{base:?}"
    );
    let kernel = checks.first().unwrap();
    assert!(
        matches!(
            kernel.result,
            Err(EntryFault::Import(ImportError::Io { .. }))
        ),
        "{kernel:?}"
    );
    assert_eq!(checks.len(), 1 + disk_names().len());
}

/// The browser design 10.5.2: the root disk of a service image imports
/// and checks as a root disk of a flavor does, and a wrong digest of it
/// refuses the import and names its file.
#[test]
fn a_service_image_imports_and_checks() {
    let root = Root::new("service");
    let store = root.store();
    let image = ServiceImage::Browser.name();
    let file = format!("{image}.ext4");
    let digests = root.set(&good()).import(&store).unwrap();
    let browser = digests.services.get(ServiceImage::Browser).unwrap();
    assert_eq!(browser, ABC.parse().unwrap());
    store.check(browser).unwrap();

    let wrong = root.set(&other_sha(image, HELLO));
    let checks = wrong.check(&PathBuf::from("/usr/bin/zstd"));
    let failed: Vec<(&str, &str)> = checks
        .iter()
        .filter(|check| check.result.is_err())
        .map(|check| (check.key, check.file.as_str()))
        .collect();
    assert_eq!(failed, [(image, file.as_str())]);
    let fresh = Root::new("service-wrong");
    let error = fresh.set(&other_sha(image, HELLO)).import(&fresh.store());
    assert_eq!(error.unwrap_err().file, file);
}

/// The good `digests.json` with no entry for any service image: the file
/// of a disks row from before the browser disk.
fn without_services() -> String {
    let mut json = good_json();
    for (_, &name) in PerService::every(ServiceImage::name).iter() {
        json.remove(name);
    }
    serde_json::Value::Object(json).to_string()
}

/// The module doc: the vmm start and the follow take a file with no entry
/// for a service image, and its digests say that the image is absent.
/// Handover step 9 restarts a new vmm on the old file, and a refusal
/// would exit 78 and keep the vmm down.
#[test]
fn a_file_without_a_service_image_imports_it_as_absent() {
    let root = Root::new("absent");
    let set = root.set(&without_services());
    let digests = set.import(&root.store()).unwrap();
    assert_eq!(digests.services, PerService::every(|_| None));
    assert_eq!(
        *digests.roots.get(SandboxFlavor::Base),
        ABC.parse().unwrap()
    );
    assert_eq!(set.absent_services(), ["browser"]);
    assert!(root.set(&good()).absent_services().is_empty());
}

/// The disks verify hook stays strict: `check` fails each service image
/// with no entry, so a new disks row must ship the browser disk.
#[test]
fn a_file_without_a_service_image_fails_the_check() {
    let root = Root::new("absent-check");
    let checks = root
        .set(&without_services())
        .check(&PathBuf::from("/usr/bin/zstd"));
    let failed: Vec<(&str, &str)> = checks
        .iter()
        .filter(|check| check.result.is_err())
        .map(|check| (check.key, check.file.as_str()))
        .collect();
    assert_eq!(failed, [("browser", DIGESTS_FILE)]);
    let browser = checks.last().unwrap();
    assert!(
        matches!(browser.result, Err(EntryFault::Absent)),
        "{browser:?}"
    );
    assert_eq!(checks.len(), 1 + disk_names().len());
}

/// An absent service image is the one leniency: a malformed entry of it
/// still refuses the parse, as an unknown key does.
#[test]
fn a_malformed_service_entry_still_refuses() {
    let mut json = good_json();
    json.insert(
        "browser".to_owned(),
        serde_json::json!({ "file": "browser.ext4" }),
    );
    assert!(parse(&serde_json::Value::Object(json).to_string()).is_err());
}
