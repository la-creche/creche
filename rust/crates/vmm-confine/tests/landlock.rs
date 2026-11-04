//! The Landlock ruleset of a launch, in force (decisions CH8-3, CH30,
//! CH8-1 and CH8-8). The payload is the example in its `probe` role. It
//! runs after the launch step and the fake `systemd-run`, so the test also
//! shows that the ruleset lets both execs through. It needs Landlock ABI 4
//! and `/dev/kvm`, which the host and the CI runner have (facts 77, 79).
#![cfg(target_os = "linux")]

mod common;

use std::fs;
use std::net::{Ipv4Addr, TcpListener};
use std::num::{NonZeroU32, NonZeroU64};

use creche_vmm_confine::launch::{Launch, Payload, Program};
use creche_vmm_confine::scope::Scope;
use creche_vmm_confine::spawner::Spawner;

use common::{TempVm, example};

const ID: &str = "0123abcd-0000-4000-8000-00000000c6c6";

#[test]
fn the_payload_reaches_only_its_files_and_no_tcp() {
    let vm = TempVm::new("probe", ID);
    let outside = vm.path().join("outside.txt");
    fs::write(&outside, "outside").unwrap();
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
    let launch = Launch::new(program, scope, payload, vm.files.clone());

    let spawner = Spawner::start(example()).unwrap();
    let status = spawner.launch(launch).unwrap().wait().unwrap();
    assert!(status.success(), "{status}");
    let report = fs::read_to_string(run.join("probe.txt")).unwrap();
    let expected = "\
read_root_disk ok
write_root_disk PermissionDenied
write_session_disk ok
read_outside PermissionDenied
connect_tcp PermissionDenied
";
    assert_eq!(report, expected);
}
