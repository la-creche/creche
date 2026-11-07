//! The slot profiles against the committed file and the names of the store
//! layout (decisions CH8-1, CH24, CH51, CH55, CH50 rule 11). Each slot gets
//! the same rules but its label and its run dir, and its run dir rules
//! match only the files of its own slot. `tests/kvm_slot.rs` proves the
//! same rules in the kernel.
//!
//! To write the file after a change of the generator, run
//! `CRECHE_SLOT_PROFILES=write cargo test -p creche-vmm-confine --test slot_profile`
//! in rust/.

use std::path::{Path, PathBuf};

use creche_vmm_confine::slot_profile::{FILE, label, render, render_file};
use creche_vmm_store::layout::{RunRoot, SLOTS, Slot, SnapshotGeneration, StateRoot, VmRunDir};
use creche_wire::{HostPort, SessionId};

const ID: &str = "0123abcd-0000-4000-8000-0000000c1700";

/// The run root of the vmm unit of uid 1000 (decision CH20).
const RUN_ROOT: &str = "/run/user/1000/vmm";

/// A home that `@{HOME}` matches.
const HOME: &str = "/home/david/";

/// The variable that makes the test write the file instead of checking it.
const WRITE_VAR: &str = "CRECHE_SLOT_PROFILES";

// Each helper with `cfg(test)` may unwrap (rust/clippy.toml).

#[cfg(test)]
fn slot(number: u16) -> Slot {
    Slot::new(number).unwrap()
}

#[cfg(test)]
fn id() -> SessionId {
    ID.parse().unwrap()
}

/// The rule lines of a profile text, trimmed, with no blank line and no
/// comment.
#[cfg(test)]
fn rules(text: &str) -> Vec<&str> {
    let lines = text.lines().map(str::trim);
    lines
        .filter(|line| !line.is_empty() && !line.starts_with('#'))
        .collect()
}

/// The words of `rule` after its leading qualifiers, with no comma.
#[cfg(test)]
fn body(rule: &str) -> Vec<&str> {
    let qualifier = |word: &&str| matches!(*word, "audit" | "allow" | "owner" | "file");
    let words = rule.trim_end_matches(',').split_whitespace();
    words.skip_while(qualifier).collect()
}

/// The path glob of each allow rule of `block`, with or without `owner`,
/// with `@{HOME}` and the uid glob put in. A rule that names a path
/// without `owner` grants it too, so the test reads every rule but a
/// deny rule.
#[cfg(test)]
fn path_globs(block: &str) -> Vec<String> {
    let allows = rules(block).into_iter().map(body);
    let allows = allows.filter(|words| words.first() != Some(&"deny"));
    let paths = allows.flatten();
    let paths = paths.filter(|word| word.starts_with('/') || word.starts_with("@{"));
    let fill = |glob: &str| glob.replace("@{HOME}/", HOME).replace("[0-9]*", "1000");
    paths.map(fill).collect()
}

/// The signal rules of `block`, allow and deny, as written.
#[cfg(test)]
fn signal_rules(block: &str) -> Vec<&str> {
    let signal = |rule: &&str| body(rule).iter().any(|word| word.starts_with("signal"));
    rules(block).into_iter().filter(signal).collect()
}

/// Whether the AppArmor glob `glob` matches `path`. `**` matches any bytes,
/// and `*` any bytes with no `/`. The globs here use no other syntax.
#[cfg(test)]
fn matches(glob: &[u8], path: &[u8]) -> bool {
    match glob {
        [b'*', b'*', rest @ ..] => (0..=path.len()).any(|at| matches(rest, &path[at..])),
        [b'*', rest @ ..] => {
            let end = path.iter().position(|&byte| byte == b'/');
            (0..=end.unwrap_or(path.len())).any(|at| matches(rest, &path[at..]))
        }
        [want, rest @ ..] => matches!(path, [got, tail @ ..] if got == want && matches(rest, tail)),
        [] => path.is_empty(),
    }
}

/// The paths of the VM run dir `vm` that CH binds, connects or writes.
#[cfg(test)]
fn run_files(vm: &VmRunDir) -> Vec<PathBuf> {
    vec![
        vm.api_socket(),
        vm.api_socket_lock(),
        vm.vsock_socket(),
        vm.host_listener(HostPort::Chaperone),
    ]
}

/// Whether some allow rule of `block` matches `path`.
#[cfg(test)]
fn allowed(block: &str, path: &Path) -> bool {
    let path = path.to_str().unwrap();
    path_globs(block)
        .iter()
        .any(|glob| matches(glob.as_bytes(), path.as_bytes()))
}

#[test]
fn the_committed_file_equals_the_generator() {
    let path = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join(FILE);
    let file = render_file();
    if std::env::var(WRITE_VAR).as_deref() == Ok("write") {
        std::fs::write(&path, &file).unwrap();
    }
    let committed = std::fs::read_to_string(&path).unwrap_or_default();
    assert!(
        committed == file,
        "{FILE} differs from slot_profile::render_file. Run `{WRITE_VAR}=write cargo test -p creche-vmm-confine --test slot_profile` in rust/ and commit the file."
    );
}

#[test]
fn a_slot_block_has_the_fixed_rules_and_its_own_label_and_dir() {
    let want = "\
profile creche-ch-slot-7 {
  /opt/components/vmm/bin/cloud-hypervisor mr,
  /dev/kvm rw,
  /dev/urandom r,
  /dev/null rw,
  unix type=stream,
  deny unix (bind, connect) addr=@**,
  deny unix (connect, send, receive) peer=(addr=@**),
  signal (receive) peer=unconfined,
  signal (send, receive) peer=creche-ch-slot-7,
  owner /run/user/[0-9]*/vmm/slot/7/api.sock rw,
  owner /run/user/[0-9]*/vmm/slot/7/api.sock.lock wk,
  owner /run/user/[0-9]*/vmm/slot/7/vsock.sock rw,
  owner /run/user/[0-9]*/vmm/slot/7/vsock.sock_* rw,
  owner @{HOME}/.local/state/creche-vmm/store/* rk,
  owner @{HOME}/.local/state/creche-vmm/sessions/*/disk.ext4 rwk,
  owner @{HOME}/.local/state/creche-vmm/sessions/*/serial.log w,
  owner @{HOME}/.local/state/creche-vmm/sessions/*/snapshots/** rw,
}
";
    assert_eq!(render(slot(7)), want);
}

#[test]
fn each_slot_names_only_its_own_label() {
    for (number, header) in [
        (0, "profile creche-ch-slot-0 {"),
        (63, "profile creche-ch-slot-63 {"),
    ] {
        let block = render(slot(number));
        assert_eq!(rules(&block).first(), Some(&header));
        let labels = block.matches("creche-ch-slot-").count();
        assert_eq!(labels, 2, "slot {number}: the header and the signal rule");
    }
    assert_eq!(label(slot(5)), "creche-ch-slot-5");
}

#[test]
fn each_slot_signals_only_its_own_label() {
    for other in Slot::all() {
        let block = render(other);
        let own = format!("signal (send, receive) peer={},", label(other));
        let want = vec!["signal (receive) peer=unconfined,", own.as_str()];
        assert_eq!(signal_rules(&block), want, "slot {}", other.number());
    }
}

#[test]
fn the_run_dir_rules_of_a_slot_match_only_its_own_files() {
    let root = RunRoot::new(PathBuf::from(RUN_ROOT)).unwrap();
    let names: Vec<_> = run_files(&root.vm(&id()))
        .into_iter()
        .map(|path| path.file_name().unwrap().to_owned())
        .collect();
    for number in [0, 1, 10, SLOTS - 1] {
        let block = render(slot(number));
        for other in Slot::all() {
            for name in &names {
                let path = root.slot(other).join(name);
                let want = other.number() == number;
                assert_eq!(allowed(&block, &path), want, "slot {number}: {path:?}");
            }
        }
        for path in run_files(&root.vm(&id())) {
            assert!(!allowed(&block, &path), "slot {number}: {path:?}");
        }
        assert!(!allowed(&block, &root.vmm_socket()), "slot {number}");
    }
}

#[test]
fn the_state_rules_name_the_store_layout_under_the_default_root() {
    let state = StateRoot::new(PathBuf::from(format!("{HOME}.local/state/creche-vmm"))).unwrap();
    let session = state.session(&id());
    let block = render(slot(3));
    let generation = SnapshotGeneration(2);
    for path in [
        state.store_dir().join("root.ext4"),
        session.disk(),
        session.serial_log(),
        session.snapshot(generation).join("memory-ranges"),
        session.snapshot_temp(generation).join("config.json"),
    ] {
        assert!(allowed(&block, &path), "{path:?}");
    }
    // The serial log left the run dir (decision CH59 rule 2).
    let run = RunRoot::new(PathBuf::from(RUN_ROOT)).unwrap();
    let old_log = run.slot(slot(3)).join("serial.log");
    for path in [state.row_file(&id()), session.disk_temp(), old_log] {
        assert!(!allowed(&block, &path), "{path:?}");
    }
    let other = StateRoot::new(PathBuf::from("/srv/agents/state/vmm")).unwrap();
    assert!(!allowed(&block, &other.session(&id()).disk()));
}

#[test]
fn the_file_holds_the_abi_and_the_include_once_and_each_slot_once() {
    let file = render_file();
    let lines = rules(&file);
    assert_eq!(lines.iter().filter(|l| l.starts_with("abi ")).count(), 1);
    let include = lines.iter().filter(|l| **l == "include <tunables/global>");
    assert_eq!(include.count(), 1);
    let headers: Vec<&&str> = lines.iter().filter(|l| l.starts_with("profile ")).collect();
    assert_eq!(headers.len(), usize::from(SLOTS));
    for (other, header) in Slot::all().zip(headers) {
        assert_eq!(*header, &format!("profile {} {{", label(other)));
    }
    assert!(!file.contains("/vmm/vm/"));
}
