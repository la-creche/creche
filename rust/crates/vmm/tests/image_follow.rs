//! The kvm_ test of the digests follow (plan PR V1, decisions CH7-6,
//! CH58 rule 5, CH76 rule 8, CH77 rule 2). A disks release replaces
//! `digests.json` while the vmm runs. The follow reads the root-owned
//! file, waits `HOOK_TIMEOUT` plus one `POLL`, imports the new root disk
//! and swaps the set of new starts. A snapshot of a VM on the old disk
//! holds the old key, so the next start drops it and cold-boots on the
//! new disk.
//!
//! The test boots `examples/phase3_vm.rs` with the helpers of
//! `tests/common/mod.rs`, which runs the follow as `vmm serve` does. Its
//! `root` step gives the root disk that a new start reads. The rust job
//! proves the follow on the bytes (`src/image_root/follow/tests.rs`),
//! because only the runner's sudo makes a root-owned file (decision CH36
//! G20).
//!
//! `#[ignore]`: only the job `kvm` runs it, after the setup script, as
//! `tests/kvm_guest.rs` says.
#![cfg(target_os = "linux")]

mod common;

use std::fs;
use std::path::PathBuf;
use std::thread::sleep;
use std::time::{Duration, Instant};

use creche_vmm::image_root::DIGESTS_FILE;
use creche_vmm::image_root::follow::{HOOK_TIMEOUT, POLL};

use common::{BOOT, UP, boot, ext4, lines, sha256, stub_plane, sudo, work};

/// The name of the proof, for its unit and its work dir.
const NAME: &str = "disk-swap";

/// The pause between two `root` steps while the test waits for the
/// follow.
const RETRY: Duration = Duration::from_millis(200);

/// Decisions CH7-6 and CH76 rule 8: a VM sleeps on the old disk, a new
/// root-owned `digests.json` names a new disk, and the follow swaps the
/// set of new starts. The next start drops the snapshot and cold-boots.
#[test]
#[ignore = "needs /dev/kvm and bin/vm-smoke-setup.sh: the job kvm runs it"]
fn kvm_a_disk_swap_cold_boots_a_snapshot_of_the_old_disk() {
    let (litellm, _litellm_heads) = stub_plane();
    let (chaperone, _chaperone_heads) = stub_plane();
    let booted = boot(NAME, "linked-relay", (litellm, chaperone));
    let (mut vmm, vsock, session) = (booted.vmm, booted.vsock, booted.session);

    // 1. A cold boot on the old disk, then a sleep into one snapshot.
    vmm.start_vm("step 1 cold boot");
    assert_eq!(vmm.ask("sleep"), "slept 1", "step 1 sleep");
    let generations: Vec<PathBuf> = fs::read_dir(session.snapshots())
        .unwrap()
        .map(|entry| entry.unwrap().path())
        .collect();
    assert_eq!(generations.len(), 1, "step 1 sleep: {generations:?}");

    // 2. The disks release: a new root disk with one more file, and a new
    // `digests.json` that root owns. The rename swaps it in whole, as
    // handover swaps the disks tree.
    let work = work(NAME);
    let (image, tree) = (work.join("image"), work.join("tree"));
    fs::write(tree.join("etc/creche-standin/swap"), "the new disk\n").unwrap();
    ext4(&image.join("root2.img"), Some(&tree));
    let new = sha256(&image.join("root2.img"));
    let old = vmm.ask("root");
    assert_ne!(old, format!("root {new}"), "step 2: the disks are equal");
    let disk = format!(r#"{{"file":"root2.img","sha256":"{new}"}}"#);
    let kernel = sha256(&image.join("vmlinux"));
    let text = format!(
        r#"{{"kernel":{{"file":"vmlinux","sha256":"{kernel}"}},"base":{disk},"python":{disk}}}"#
    );
    let staged = image.join("digests.json.new");
    fs::write(&staged, text).unwrap();
    sudo(&["chown", "0:0"], &staged);
    let renamed = Instant::now();
    fs::rename(&staged, image.join(DIGESTS_FILE)).unwrap();

    // 3. New starts read the old disk for `HOOK_TIMEOUT` plus one `POLL`
    // (decision CH77 rule 2), then the new disk within a few polls.
    let deadline = renamed + HOOK_TIMEOUT + POLL * 4 + BOOT;
    loop {
        let answer = vmm.ask("root");
        if answer == format!("root {new}") {
            let waited = renamed.elapsed();
            assert!(
                waited >= HOOK_TIMEOUT + POLL,
                "step 3: the swap came after {waited:?}"
            );
            break;
        }
        assert_eq!(answer, old, "step 3: another root disk");
        assert!(Instant::now() < deadline, "step 3: no swap");
        sleep(RETRY);
    }

    // 4. The start drops the snapshot of the old key and cold-boots: the
    // new serial log holds the `up` line of a boot, and the new VM has its
    // vsock socket.
    vmm.start_vm("step 4 cold boot on the new disk");
    assert!(vsock.exists(), "step 4: no vsock socket");
    assert!(
        !generations[0].exists(),
        "step 4: the snapshot of the old disk stays"
    );
    assert!(
        lines(&session.serial_log()).iter().any(|line| line == UP),
        "step 4: the VM did not boot"
    );

    let answer = vmm.ask("stop");
    assert!(answer.starts_with("stopped "), "step 5 stop: {answer}");
    drop(vmm);
}
