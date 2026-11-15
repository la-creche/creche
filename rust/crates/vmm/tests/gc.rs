//! `vmm gc`, the snapshot and image retention by hand. `gc::collect` runs
//! on a scratch state root with real store pairs and snapshot keys: the dry
//! run lists and removes nothing, `Act::Remove` removes the same items, and
//! a held state root lock refuses it. `tests/gc_binary.rs` runs the binary.
//!
//! No test of this binary starts a process. The state root lock is an
//! `flock` of an open file, and a process that another test thread starts
//! holds a copy of each open fd until its exec. Such a copy keeps a lock
//! that the test dropped, and the next take of the lock then fails.

use std::fs;
use std::path::PathBuf;

use creche_vmm::gc::{Act, GcError, Outcome, collect};
use creche_vmm_store::images::ImageDigest;
use creche_vmm_store::retention::{BUDGET as DESIGN_BUDGET, Reason};

mod gc_host;

use gc_host::{Host, OLD};

#[cfg(test)]
impl Host {
    fn collect(&self, act: Act) -> Result<Outcome, GcError> {
        collect(
            &self.scratch.state,
            &self.scratch.store,
            &self.current,
            DESIGN_BUDGET,
            act,
        )
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
