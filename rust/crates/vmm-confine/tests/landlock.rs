//! The Landlock ruleset of a launch comes after the slot label (decisions
//! CH8-3, CH30, CH55). The payload is the example in its `probe` role,
//! which writes the result of five accesses to `probe.txt`. The launch
//! stage is `stage::run`, which writes the real exec attribute (decision
//! CH58 rule 1). The rust job loads no slot profile, so the stage stops at
//! the label, and the probe never runs. The kvm job loads the slot
//! profiles and proves the denials of the ruleset under the label (the
//! C15 rework, #443).
#![cfg(target_os = "linux")]

mod common;

use std::net::{Ipv4Addr, TcpListener};
use std::num::{NonZeroU32, NonZeroU64};
use std::path::PathBuf;

use creche_vmm_confine::launch::{Launch, Payload, Program};
use creche_vmm_confine::scope::Scope;
use creche_vmm_confine::spawner::Spawner;
use creche_vmm_store::layout::Slot;

use common::{TempVm, example};

const ID: &str = "0123abcd-0000-4000-8000-00000000c6c6";

#[test]
fn a_payload_with_no_slot_profile_never_runs_its_probe() {
    let vm = TempVm::new("probe", ID);
    // A file outside the VM files that every Linux host has.
    let outside = PathBuf::from("/etc/passwd");
    let listener = TcpListener::bind((Ipv4Addr::LOCALHOST, 0)).unwrap();
    let port = listener.local_addr().unwrap().port().to_string();

    let files = &vm.files;
    let run = files.run_dir().path();
    let args = [
        run,
        files.root_disk().path(),
        files.session_disk().path(),
        &outside,
    ]
    .map(|path| path.to_str().unwrap().to_owned());
    let mut args = Vec::from(args);
    args.insert(0, "probe".to_owned());
    args.push(port);
    let program = Program::new(example()).unwrap();
    let scope = Scope::new(
        ID.parse().unwrap(),
        NonZeroU64::new(1 << 30).unwrap(),
        NonZeroU32::new(100).unwrap(),
    );
    let payload = Payload::new(program.clone(), args);
    let slot = Slot::new(0).unwrap();
    let launch = Launch::new(program, scope, payload, vm.files.clone()).in_slot(slot);

    let spawner = Spawner::start(example()).unwrap();
    let status = spawner.launch(launch).unwrap().wait().unwrap();
    assert_eq!(status.code(), Some(78), "{status}");
    assert!(!run.join("probe.txt").exists());
}
