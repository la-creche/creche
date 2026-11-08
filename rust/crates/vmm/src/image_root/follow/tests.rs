//! The digests follow on the bytes (plan PR V1, decisions CH58 rule 5,
//! CH76 rule 8). `read_bytes` trusts only a root-owned file, and a test of
//! the rust job cannot make one. So these tests give `Follow::apply` each
//! read as `read_bytes` gives it. The kvm_ test of `tests/image_follow.rs`
//! proves the poll on a root-owned file and a cold boot on the new disk.

use std::fs;
use std::path::PathBuf;

use creche_vmm_store::images::{ImageDigest, ImageStore};
use creche_vmm_store::layout::StateRoot;
use creche_wire::{PerFlavor, SandboxFlavor};

use super::{Follow, RETRY_POLLS};
use crate::image_root::{DigestsError, KERNEL_KEY, parse};
use crate::start::Digests;

/// The SHA-256 of no bytes.
const EMPTY: &str = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855";

/// The SHA-256 of `abc`.
const ABC: &str = "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad";

/// The SHA-256 of `hello world`.
const HELLO: &str = "b94d27b9934d3e08a52e52d7da7dabfac484efe37a5380ee9088f7ace2efcde9";

/// A digest of no file of the test, so the store never holds it.
const NONE: &str = "0000000000000000000000000000000000000000000000000000000000000000";

/// A scratch image root and state root that its drop removes.
struct Root(PathBuf);

impl Drop for Root {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

impl Root {
    /// An image root with `vmlinux` (no bytes), `abc.ext4` (`abc`) and
    /// `hello.ext4` (`hello world`).
    fn new(name: &str) -> Self {
        let dir =
            std::env::temp_dir().join(format!("creche-vmm-follow-{}-{name}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(dir.join("images")).unwrap();
        fs::write(dir.join("images/vmlinux"), "").unwrap();
        fs::write(dir.join("images/abc.ext4"), "abc").unwrap();
        fs::write(dir.join("images/hello.ext4"), "hello world").unwrap();
        Self(dir)
    }

    fn images(&self) -> PathBuf {
        self.0.join("images")
    }

    fn store(&self) -> ImageStore {
        let state = StateRoot::new(self.0.join("state")).unwrap();
        ImageStore::new(&state, PathBuf::from("/usr/bin/zstd"))
    }

    /// The follow of the start set `start`, and the set of new starts.
    fn follow(&self, start: &[u8]) -> (Follow, tokio::sync::watch::Receiver<Digests>) {
        let set = parse(&self.images(), start.to_vec()).unwrap();
        Follow::start(set, self.store()).unwrap()
    }
}

/// A `digests.json` with the kernel `kernel` and each flavor on the root
/// disk `root`, each `(file, sha256)`.
fn digests(kernel: (&str, &str), root: (&str, &str)) -> Vec<u8> {
    let entry =
        |(file, sha256): (&str, &str)| serde_json::json!({ "file": file, "sha256": sha256 });
    let mut json = serde_json::Map::new();
    json.insert(KERNEL_KEY.to_owned(), entry(kernel));
    for (_, &flavor) in PerFlavor::every(SandboxFlavor::name).iter() {
        json.insert(flavor.to_owned(), entry(root));
    }
    serde_json::Value::Object(json).to_string().into_bytes()
}

/// The set of a kernel `kernel` and each root disk on `root`.
fn set(kernel: &str, root: &str) -> Digests {
    Digests {
        kernel: kernel.parse().unwrap(),
        roots: PerFlavor::every(|_| root.parse().unwrap()),
    }
}

fn digest(text: &str) -> ImageDigest {
    text.parse().unwrap()
}

/// Decision CH76 rule 8: new bytes import each entry, and then new starts
/// read the new set. The swap logs one line that names each digest.
#[test]
fn a_new_digests_file_switches_new_boots_after_every_import() {
    let root = Root::new("switch");
    let (mut follow, current) = root.follow(&digests(("vmlinux", EMPTY), ("abc.ext4", ABC)));
    assert_eq!(*current.borrow(), set(EMPTY, ABC));
    assert!(root.store().check(digest(HELLO)).is_err());

    let line = follow
        .apply(Ok(digests(("vmlinux", EMPTY), ("hello.ext4", HELLO))))
        .unwrap();
    assert!(
        line.starts_with("new starts use the new digests.json: "),
        "{line}"
    );
    assert!(line.contains(&format!("base {HELLO}")), "{line}");
    assert_eq!(*current.borrow(), set(EMPTY, HELLO));
    root.store().check(digest(HELLO)).unwrap();
}

/// Decision CH58 rule 5: an entry that does not import keeps the old set,
/// also when an entry before it imported. A malformed file keeps it too.
/// Each fault logs one line.
#[test]
fn a_failed_import_keeps_the_old_set() {
    let root = Root::new("failed");
    let (mut follow, current) = root.follow(&digests(("vmlinux", EMPTY), ("abc.ext4", ABC)));
    fs::write(root.images().join("kernel2"), "hello world").unwrap();

    // The new kernel imports, then the root disk has another digest.
    let line = follow
        .apply(Ok(digests(("kernel2", HELLO), ("abc.ext4", NONE))))
        .unwrap();
    assert!(line.contains("cannot import abc.ext4"), "{line}");
    assert!(
        line.ends_with(", so new starts keep the last set"),
        "{line}"
    );
    assert_eq!(*current.borrow(), set(EMPTY, ABC));
    root.store().check(digest(HELLO)).unwrap();

    let line = follow.apply(Ok(b"{".to_vec())).unwrap();
    assert!(line.contains("is malformed"), "{line}");
    assert_eq!(*current.borrow(), set(EMPTY, ABC));

    let line = follow.apply(Err(DigestsError::Missing)).unwrap();
    assert_eq!(
        line,
        "VMM_IMAGE_ROOT: the digests.json is missing, so new starts keep the last set"
    );
    assert_eq!(follow.apply(Err(DigestsError::Missing)), None);
    assert_eq!(*current.borrow(), set(EMPTY, ABC));
}

/// Decision CH76 rule 8: the bytes of the set of new starts import
/// nothing and log nothing. After a fault they are a new reading again,
/// and the store holds each digest, so the swap reads no file.
#[test]
fn an_unchanged_file_imports_nothing() {
    let root = Root::new("unchanged");
    let start = digests(("vmlinux", EMPTY), ("abc.ext4", ABC));
    let (mut follow, current) = root.follow(&start);
    assert_eq!(follow.apply(Ok(start.clone())), None);

    assert!(follow.apply(Err(DigestsError::Missing)).is_some());
    fs::remove_file(root.images().join("abc.ext4")).unwrap();
    let line = follow.apply(Ok(start.clone())).unwrap();
    assert!(line.starts_with("new starts use the new "), "{line}");
    assert_eq!(*current.borrow(), set(EMPTY, ABC));
    assert_eq!(follow.apply(Ok(start)), None);
}

/// Decisions CH48 (2) and CH76 rule 8: an import that fails tries the
/// same bytes again every `RETRY_POLLS` polls. A try that fails with the
/// same fault logs nothing. Once the fault clears, the next try swaps the
/// set with no vmm restart.
#[test]
fn a_failed_import_tries_again_until_it_swaps() {
    let root = Root::new("retry");
    let (mut follow, current) = root.follow(&digests(("vmlinux", EMPTY), ("abc.ext4", ABC)));

    // `hello.ext4` is missing, so the import fails.
    fs::remove_file(root.images().join("hello.ext4")).unwrap();
    let next = digests(("vmlinux", EMPTY), ("hello.ext4", HELLO));
    let line = follow.apply(Ok(next.clone())).unwrap();
    assert!(line.contains("cannot import hello.ext4"), "{line}");

    // The polls before the next try import nothing. The try fails with
    // the same fault, so it logs nothing.
    for _ in 0..RETRY_POLLS {
        assert_eq!(follow.apply(Ok(next.clone())), None);
    }
    assert_eq!(follow.apply(Ok(next.clone())), None);
    assert_eq!(*current.borrow(), set(EMPTY, ABC));

    // The file is back. The polls of the wait still import nothing, and
    // the try after them swaps the set.
    fs::write(root.images().join("hello.ext4"), "hello world").unwrap();
    for _ in 0..RETRY_POLLS {
        assert_eq!(follow.apply(Ok(next.clone())), None);
        assert!(root.store().check(digest(HELLO)).is_err());
    }
    let line = follow.apply(Ok(next.clone())).unwrap();
    assert!(line.contains(&format!("base {HELLO}")), "{line}");
    assert_eq!(*current.borrow(), set(EMPTY, HELLO));
    root.store().check(digest(HELLO)).unwrap();
    assert_eq!(follow.apply(Ok(next)), None);
}
