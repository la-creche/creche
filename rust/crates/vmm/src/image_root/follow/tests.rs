//! The digests follow on the bytes (plan PR V1, decisions CH58 rule 5,
//! CH76 rule 8, CH77 rules 2 and 3). `read_bytes` trusts only a root-owned file, and a test of
//! the rust job cannot make one. So these tests give `Follow::apply` each
//! read as `read_bytes` gives it, one call for each poll. The kvm_ test of `tests/image_follow.rs`
//! proves the poll on a root-owned file and a cold boot on the new disk.

use std::fs;
use std::path::PathBuf;
use std::time::Duration;

use creche_vmm_store::images::{ImageDigest, ImageStore};
use creche_vmm_store::layout::StateRoot;
use creche_wire::{PerFlavor, SandboxFlavor};

use super::{Follow, HOOK_TIMEOUT, POLL, RETRY_POLLS, SETTLE_POLLS};
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

/// The first line of the wait of decision CH77 rule 2.
const WAITS: &str = "a new digests.json imports once its bytes stay the same for 300 s";

/// Reads `bytes` at each poll of the wait of decision CH77 rule 2: the
/// first read logs the wait, and each later read imports nothing. It
/// gives the line of the poll after the wait, the first that may import.
fn settle(follow: &mut Follow, bytes: &[u8]) -> Option<String> {
    let first = follow.apply(Ok(bytes.to_vec()));
    assert!(
        first.as_deref().is_some_and(|line| line.starts_with(WAITS)),
        "{first:?}"
    );
    for _ in 1..SETTLE_POLLS {
        assert_eq!(follow.apply(Ok(bytes.to_vec())), None);
    }
    follow.apply(Ok(bytes.to_vec()))
}

/// Decision CH76 rule 8: new bytes import each entry after their wait,
/// and then new starts read the new set. The swap logs one line that
/// names each digest.
#[test]
fn a_new_digests_file_switches_new_boots_after_every_import() {
    let root = Root::new("switch");
    let (mut follow, current) = root.follow(&digests(("vmlinux", EMPTY), ("abc.ext4", ABC)));
    assert_eq!(*current.borrow(), set(EMPTY, ABC));
    assert!(root.store().check(digest(HELLO)).is_err());

    let line = settle(
        &mut follow,
        &digests(("vmlinux", EMPTY), ("hello.ext4", HELLO)),
    )
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
    let line = settle(
        &mut follow,
        &digests(("kernel2", HELLO), ("abc.ext4", NONE)),
    )
    .unwrap();
    assert!(line.contains("cannot import abc.ext4"), "{line}");
    assert!(
        line.ends_with(", so new starts keep the last set"),
        "{line}"
    );
    assert_eq!(*current.borrow(), set(EMPTY, ABC));
    root.store().check(digest(HELLO)).unwrap();

    let line = settle(&mut follow, b"{").unwrap();
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

/// Decisions CH48 (2), CH76 rule 8 and CH77 rule 3: an import that fails
/// tries the same bytes again every `RETRY_POLLS` polls, one minute. The
/// tries need no new wait, since the bytes stay the same. A try that fails with the
/// same fault logs nothing. Once the fault clears, the next try swaps the
/// set with no vmm restart.
#[test]
fn a_failed_import_tries_again_until_it_swaps() {
    let root = Root::new("retry");
    let (mut follow, current) = root.follow(&digests(("vmlinux", EMPTY), ("abc.ext4", ABC)));

    // `hello.ext4` is missing, so the import fails.
    fs::remove_file(root.images().join("hello.ext4")).unwrap();
    let next = digests(("vmlinux", EMPTY), ("hello.ext4", HELLO));
    let line = settle(&mut follow, &next).unwrap();
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

/// Decision CH77 rule 2: the wait is the 300 s of the disks verify hook,
/// and its polls span at least that long. Decision CH77 rule 3: a retry
/// comes every 12 polls, one minute.
#[test]
fn the_wait_outlasts_the_hook_and_a_retry_comes_each_minute() {
    assert_eq!(HOOK_TIMEOUT, Duration::from_secs(300));
    assert_eq!(SETTLE_POLLS, 60);
    assert!(POLL * u32::try_from(SETTLE_POLLS).unwrap() >= HOOK_TIMEOUT);
    assert_eq!(RETRY_POLLS, 12);
    assert_eq!(POLL * RETRY_POLLS, Duration::from_secs(60));
}

/// Decision CH77 rule 2: new bytes import nothing for `SETTLE_POLLS`
/// polls, then import once. The store holds no new digest and new
/// starts read the old set until then.
#[test]
fn bytes_that_stay_the_same_for_the_hook_timeout_import_once() {
    let root = Root::new("settle");
    let (mut follow, current) = root.follow(&digests(("vmlinux", EMPTY), ("abc.ext4", ABC)));
    let next = digests(("vmlinux", EMPTY), ("hello.ext4", HELLO));

    let line = follow.apply(Ok(next.clone())).unwrap();
    assert!(line.starts_with(WAITS), "{line}");
    for _ in 1..SETTLE_POLLS {
        assert_eq!(follow.apply(Ok(next.clone())), None);
        assert_eq!(*current.borrow(), set(EMPTY, ABC));
        assert!(root.store().check(digest(HELLO)).is_err());
    }

    let line = follow.apply(Ok(next.clone())).unwrap();
    assert!(line.starts_with("new starts use the new "), "{line}");
    assert_eq!(*current.borrow(), set(EMPTY, HELLO));
    root.store().check(digest(HELLO)).unwrap();
    for _ in 0..SETTLE_POLLS + u64::from(RETRY_POLLS) {
        assert_eq!(follow.apply(Ok(next.clone())), None);
    }
}

/// Decision CH77 rule 2: new bytes that change again inside the wait
/// import nothing. The newest bytes start their own wait, and only they
/// import once it ends.
#[test]
fn bytes_that_change_inside_the_wait_import_nothing() {
    let root = Root::new("change");
    let (mut follow, current) = root.follow(&digests(("vmlinux", EMPTY), ("abc.ext4", ABC)));
    let first = digests(("vmlinux", EMPTY), ("hello.ext4", HELLO));
    let second = digests(("abc.ext4", ABC), ("abc.ext4", ABC));

    // `first` reads the same for each poll of the wait but its last.
    assert!(follow.apply(Ok(first.clone())).is_some());
    for _ in 1..SETTLE_POLLS {
        assert_eq!(follow.apply(Ok(first.clone())), None);
    }

    // `second` ends the wait of `first` and starts its own.
    let line = settle(&mut follow, &second).unwrap();
    assert!(line.contains(&format!("kernel {ABC}")), "{line}");
    assert_eq!(*current.borrow(), set(ABC, ABC));
    assert!(root.store().check(digest(HELLO)).is_err());

    // A read that takes no bytes ends a wait too.
    assert!(follow.apply(Ok(first.clone())).is_some());
    assert!(follow.apply(Err(DigestsError::Missing)).is_some());
    let line = follow.apply(Ok(first)).unwrap();
    assert!(line.starts_with(WAITS), "{line}");
    assert!(root.store().check(digest(HELLO)).is_err());
}

/// Decision CH77 rule 2: a file that its failed hook rolls back to the
/// bytes of the set of new starts inside the wait imports nothing, also
/// after the wait would have ended. The same new bytes later start a
/// new wait.
#[test]
fn a_rolled_back_file_imports_nothing() {
    let root = Root::new("rollback");
    let start = digests(("vmlinux", EMPTY), ("abc.ext4", ABC));
    let (mut follow, current) = root.follow(&start);
    let next = digests(("vmlinux", EMPTY), ("hello.ext4", HELLO));

    assert!(follow.apply(Ok(next.clone())).is_some());
    for _ in 1..SETTLE_POLLS / 2 {
        assert_eq!(follow.apply(Ok(next.clone())), None);
    }

    // The rollback puts the old bytes back.
    for _ in 0..SETTLE_POLLS + u64::from(RETRY_POLLS) {
        assert_eq!(follow.apply(Ok(start.clone())), None);
    }
    assert_eq!(*current.borrow(), set(EMPTY, ABC));
    assert!(root.store().check(digest(HELLO)).is_err());

    // The same new bytes again wait from the start.
    let line = follow.apply(Ok(next.clone())).unwrap();
    assert!(line.starts_with(WAITS), "{line}");
    for _ in 1..SETTLE_POLLS {
        assert_eq!(follow.apply(Ok(next.clone())), None);
    }
    assert!(root.store().check(digest(HELLO)).is_err());
    assert!(follow.apply(Ok(next)).is_some());
    assert_eq!(*current.borrow(), set(EMPTY, HELLO));
}
