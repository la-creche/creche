//! The scratch host of `tests/gc.rs` and `tests/gc_binary.rs`: a state
//! root with real store pairs, a stale and a current snapshot, and an
//! import temp file. Each of the two test binaries uses each item.
#![cfg(test)]

use std::fs;
use std::path::PathBuf;

use creche_vmm_store::images::{ImageDigest, ImageStore};
use creche_vmm_store::key::{CH_REV, CurrentImages};
use creche_vmm_store::layout::{SnapshotGeneration, StateRoot};
use creche_wire::{GUEST_INIT_PROTOCOL, PerFlavor, SandboxFlavor, SessionId};

/// A session whose snapshot names an old root disk and initramfs.
pub const OLD: &str = "0f8e2a4c-1b3d-4e5f-8a9b-0c1d2e3f4a5b";
/// A session whose snapshot names the current images.
pub const NEW: &str = "1f8e2a4c-1b3d-4e5f-8a9b-0c1d2e3f4a5b";

/// A scratch dir that its drop removes.
pub struct Scratch {
    pub dir: PathBuf,
    pub state: StateRoot,
    pub store: ImageStore,
}

impl Drop for Scratch {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.dir);
    }
}

impl Scratch {
    pub fn new(name: &str) -> Self {
        let dir = std::env::temp_dir().join(format!("creche-vmm-gc-{}-{name}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        let state = StateRoot::new(dir.join("state")).unwrap();
        let store = ImageStore::new(&state, PathBuf::from("/usr/bin/zstd"));
        Self { dir, state, store }
    }

    /// Imports a plain file of `text` into the store and gives its digest.
    pub fn image(&self, text: &str) -> ImageDigest {
        let source = self.dir.join("source");
        fs::write(&source, text).unwrap();
        let digest = ImageDigest::of(text.as_bytes());
        self.store.import(&source, digest).unwrap();
        digest
    }

    /// Writes generation 1 of `id` with a key of `kernel`, `initramfs`
    /// and the `coding` root disk `root`.
    pub fn snapshot(
        &self,
        id: &str,
        kernel: ImageDigest,
        initramfs: ImageDigest,
        root: ImageDigest,
    ) {
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

    pub fn exists(&self, digest: ImageDigest) -> bool {
        self.store.path(&digest).exists() && self.store.identity_path(&digest).exists()
    }
}

/// The store and snapshots of a host after a disks and a vmm release:
/// session `OLD` keeps a snapshot of the old root disk and initramfs,
/// session `NEW` one of the current images, and an import left a temp
/// file.
pub struct Host {
    pub scratch: Scratch,
    pub current: CurrentImages,
    pub old_root: ImageDigest,
    pub old_initramfs: ImageDigest,
    pub leftover: PathBuf,
}

impl Host {
    pub fn new(name: &str) -> Self {
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

    /// The snapshot dir of session `id`.
    pub fn snapshot(&self, id: &str) -> PathBuf {
        let id: SessionId = id.parse().unwrap();
        self.scratch
            .state
            .session(&id)
            .snapshot(SnapshotGeneration(1))
    }

    pub fn current_stays(&self) {
        let current = &self.current;
        for digest in current.digests() {
            assert!(self.scratch.exists(digest), "{digest}");
        }
        assert!(self.snapshot(NEW).is_dir());
    }
}
