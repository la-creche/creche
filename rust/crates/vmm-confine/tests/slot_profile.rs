//! SPIKE (decision CH51): the slot pool profiles against the base profile
//! and the names of plan PR D1. Each slot profile is the base rules with
//! four kinds of line changed, and its run dir rules name only the files
//! of its own slot. The kvm_slot_ tests, still to come, will prove the
//! same rules in the kernel.

use std::path::PathBuf;

use creche_vmm_confine::slot_profile::{
    BASE, RenderError, SLOTS, Slot, render, render_probe, render_set,
};
use creche_vmm_store::layout::{RunRoot, VmRunDir};
use creche_wire::HostPort;

const ID: &str = "0123abcd-0000-4000-8000-0000000c3500";

/// The run root of the vmm unit of uid 1000 (decision CH20).
const RUN_ROOT: &str = "/run/user/1000/vmm";

// Each helper with `cfg(test)` may unwrap (rust/clippy.toml).

#[cfg(test)]
fn slot(number: u16) -> Slot {
    Slot::new(number).unwrap()
}

/// The rule lines of a profile text, trimmed, with no blank line and no
/// comment.
#[cfg(test)]
fn rules(text: &str) -> Vec<String> {
    let lines = text.lines().map(str::trim);
    let lines = lines.filter(|line| !line.is_empty() && !line.starts_with('#'));
    lines.map(str::to_owned).collect()
}

/// The VM run dir of plan PR D1.
#[cfg(test)]
fn vm() -> VmRunDir {
    RunRoot::new(PathBuf::from(RUN_ROOT))
        .unwrap()
        .vm(&ID.parse().unwrap())
}

/// The file names of the VM run dir of plan PR D1, a host listener
/// included.
#[cfg(test)]
fn run_dir_names() -> Vec<String> {
    let vm = vm();
    let paths = [
        vm.api_socket(),
        vm.api_socket_lock(),
        vm.vsock_socket(),
        vm.host_listener(HostPort::Chaperone),
        vm.serial_log(),
    ];
    let name = |path: PathBuf| path.file_name().unwrap().to_str().unwrap().to_owned();
    paths.into_iter().map(name).collect()
}

/// Whether the owner rule `glob` of a run dir file matches `path`. The
/// run dir rules use `[0-9]*` for the uid and `*` for the port only.
#[cfg(test)]
fn run_rule_matches(glob: &str, path: &str) -> bool {
    let glob = glob.replace("[0-9]*", "1000");
    match glob.strip_suffix('*') {
        Some(prefix) => path
            .strip_prefix(prefix)
            .is_some_and(|tail| !tail.contains('/')),
        None => glob == path,
    }
}

/// The globs of the run dir rules of `profile`.
#[cfg(test)]
fn run_globs(profile: &str) -> Vec<String> {
    let rules = rules(profile).into_iter();
    let globs = rules.filter(|rule| rule.starts_with("owner /run/user/"));
    globs
        .map(|rule| rule.split(' ').nth(1).unwrap().to_owned())
        .collect()
}

#[test]
fn each_slot_profile_changes_only_four_kinds_of_line() {
    let base: Vec<String> = rules(BASE)
        .into_iter()
        .filter(|r| !r.starts_with("abi "))
        .collect();
    for number in [0, 1, 17, SLOTS - 1] {
        let slot = slot(number);
        let block = rules(&render(BASE, slot).unwrap());
        assert_eq!(block.len(), base.len());
        for (from, to) in base.iter().zip(&block) {
            let want = if from.starts_with("profile ") {
                format!("profile {} {} {{", slot.name(), slot.binary())
            } else if from == "/opt/components/vmm/bin/cloud-hypervisor mr," {
                format!("{} mr,", slot.binary())
            } else if from.starts_with("signal (send, receive)") {
                format!("signal (send, receive) peer={},", slot.name())
            } else {
                from.replace("/vmm/vm/*/", &slot.run_dir())
            };
            assert_eq!(to, &want, "slot {number}");
        }
    }
}

#[test]
fn a_slot_names_its_label_and_its_link() {
    let slot = slot(5);
    assert_eq!(slot.name(), "creche-ch-slot-5");
    assert_eq!(
        slot.binary(),
        "/opt/components/vmm/bin/ch-slot/5/cloud-hypervisor"
    );
    assert_eq!(Slot::new(SLOTS), None);
    assert_eq!(Slot::all().count(), usize::from(SLOTS));
}

#[test]
fn the_run_dir_rules_of_a_slot_match_only_its_own_files() {
    let names = run_dir_names();
    for number in [0, 1, 10, SLOTS - 1] {
        let globs = run_globs(&render(BASE, slot(number)).unwrap());
        assert_eq!(globs.len(), 5);
        for other in 0..SLOTS {
            for name in &names {
                let path = format!("{RUN_ROOT}/slot/{other}/{name}");
                let matched = globs.iter().filter(|glob| run_rule_matches(glob, &path));
                let want = usize::from(other == number);
                assert_eq!(matched.count(), want, "slot {number} rule for {path}");
            }
        }
        for name in &names {
            let path = vm().path().join(name);
            let path = path.to_str().unwrap();
            assert!(!globs.iter().any(|glob| run_rule_matches(glob, path)));
        }
        let vmm_sock = format!("{RUN_ROOT}/vmm.sock");
        assert!(!globs.iter().any(|glob| run_rule_matches(glob, &vmm_sock)));
    }
}

#[test]
fn the_set_holds_each_slot_once_and_the_abi_once() {
    let file = render_set(BASE, Slot::all()).unwrap();
    let lines = rules(&file);
    let abi = lines.iter().filter(|line| line.starts_with("abi "));
    assert_eq!(abi.count(), 1);
    let headers: Vec<&String> = lines.iter().filter(|l| l.starts_with("profile ")).collect();
    assert_eq!(headers.len(), usize::from(SLOTS));
    for (number, header) in (0..SLOTS).zip(headers) {
        let slot = slot(number);
        assert_eq!(
            header,
            &format!("profile {} {} {{", slot.name(), slot.binary())
        );
    }
    for word in [
        "creche-cloud-hypervisor",
        "/vmm/vm/",
        "bin/cloud-hypervisor ",
    ] {
        assert!(!file.contains(word), "{word}");
    }
}

#[test]
fn the_probe_variant_adds_one_rule_only() {
    let probe = "/opt/components/vmm/bin/ch-slot-probe/busybox";
    let slot = slot(SLOTS - 1);
    let plain = rules(&render(BASE, slot).unwrap());
    let variant: Vec<String> = rules(&render_probe(BASE, slot, probe).unwrap())
        .into_iter()
        .filter(|line| !line.starts_with("abi "))
        .collect();
    let (end, body) = plain.split_last().unwrap();
    let mut want = body.to_vec();
    want.push(format!("{probe} mr,"));
    want.push(end.clone());
    assert_eq!(variant, want);
}

#[test]
fn a_probe_path_with_a_glob_or_a_space_fails() {
    for probe in ["busybox", "/bin/*", "/bin/a b", "/bin/a,", "/bin/{a}"] {
        assert_eq!(
            render_probe(BASE, slot(0), probe),
            Err(RenderError::Probe(probe.to_owned()))
        );
    }
}

#[test]
fn a_base_with_a_rule_the_generator_does_not_read_fails() {
    let extra = BASE.replace(
        "owner /run/user/[0-9]*/vmm/vm/*/serial.log w,",
        "owner /run/user/[0-9]*/vmm/vm/*/serial.log w,\n  owner /run/user/[0-9]*/vmm/vm/*/x rw,",
    );
    assert!(matches!(
        render(&extra, slot(0)),
        Err(RenderError::Count {
            what: "run dir",
            found: 6,
            want: 5
        })
    ));
    let other = BASE.replace(
        "/dev/null rw,",
        "/dev/null rw,\n  /run/user/*/vmm/vm/** rw,",
    );
    assert!(matches!(render(&other, slot(0)), Err(RenderError::Left(_))));
    let named = BASE.replace(
        "/dev/null rw,",
        "/dev/null rw,\n  ptrace peer=creche-cloud-hypervisor,",
    );
    assert!(matches!(render(&named, slot(0)), Err(RenderError::Left(_))));
}
