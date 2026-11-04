//! The AppArmor profile of CH against the names of plan PR D1 (decisions
//! CH8-1, CH24, CH20 and CH39 rule 3). A pathname Unix socket is mediated
//! by the file rules, so the write rules are the whole socket allow list.
//! The test reads every rule line of the profile, and fails on a line of a
//! form it does not read. It matches each VM file with the AppArmor globs
//! that the profile uses: `*`, `**` and `[0-9]`.

use std::path::{Path, PathBuf};

use creche_vmm_confine::landlock::{KVM, URANDOM};
use creche_vmm_store::layout::{RunRoot, SnapshotGeneration, StateRoot, VmRunDir};
use creche_wire::HostPort;

const PROFILE: &str = include_str!("../apparmor/cloud-hypervisor.profile");

const ID: &str = "0123abcd-0000-4000-8000-0000000c15c0";

/// The run root of the vmm unit of uid 1000 (decision CH20).
const RUN_ROOT: &str = "/run/user/1000/vmm";

// Each helper with `cfg(test)` may unwrap (rust/clippy.toml).

/// One file rule: `[owner] <glob> <rights>,`.
#[derive(Debug, PartialEq, Eq)]
struct FileRule<'a> {
    owner: bool,
    glob: &'a str,
    rights: &'a str,
}

/// The header line of the profile.
const HEADER: &str = "profile creche-cloud-hypervisor /opt/components/vmm/bin/cloud-hypervisor {";

/// The only lines of the profile that are not file rules. Any other line
/// fails `file_rules`, so a rule of a form that this test does not read,
/// such as `file,`, `allow …` or `network`, cannot pass.
const OTHER_LINES: &[&str] = &[
    "abi <abi/4.0>,",
    HEADER,
    "}",
    "unix type=stream,",
    "deny unix addr=@**,",
    "deny unix peer=(addr=@**),",
    "signal (receive) peer=unconfined,",
    "signal (send, receive) peer=creche-cloud-hypervisor,",
];

/// The rule lines of the profile: each trimmed line that is not blank
/// and is no comment.
#[cfg(test)]
fn rule_lines() -> impl Iterator<Item = &'static str> {
    let lines = PROFILE.lines().map(str::trim);
    lines.filter(|line| !line.is_empty() && !line.starts_with('#'))
}

/// The file rule of `line`: `[owner ]/<glob> <rights>,`, with a glob of
/// no space and rights of `r`, `w`, `k` and `m` only.
#[cfg(test)]
fn file_rule(line: &'static str) -> Option<FileRule<'static>> {
    let body = line.strip_prefix("owner ").unwrap_or(line);
    let (glob, rights) = body.strip_suffix(',')?.split_once(' ')?;
    let known = |right: char| "rwkm".contains(right);
    let rule = glob.starts_with('/') && !rights.is_empty() && rights.chars().all(known);
    rule.then_some(FileRule {
        owner: body.len() < line.len(),
        glob,
        rights,
    })
}

/// Every file rule of the profile. A rule line that is no file rule and
/// not one of `OTHER_LINES` fails the test, so the rules here are all the
/// rules that the profile holds.
#[cfg(test)]
fn file_rules() -> Vec<FileRule<'static>> {
    let rules = rule_lines().filter(|line| !OTHER_LINES.contains(line));
    let parse =
        |line| file_rule(line).unwrap_or_else(|| panic!("a rule this test does not read: {line}"));
    rules.map(parse).collect()
}

/// Whether `glob` matches `path`: `**` matches any bytes, `*` any bytes
/// but `/`, and `[a-b]` one byte of the range. The profile uses no other
/// glob, and this function fails on one.
#[cfg(test)]
fn matches(glob: &[u8], path: &[u8]) -> bool {
    match glob {
        [] => path.is_empty(),
        [b'*', b'*', rest @ ..] => (0..=path.len()).any(|at| matches(rest, &path[at..])),
        [b'*', rest @ ..] => {
            let end = path.iter().position(|&byte| byte == b'/');
            (0..=end.unwrap_or(path.len())).any(|at| matches(rest, &path[at..]))
        }
        [b'[', low, b'-', high, b']', rest @ ..] => match path {
            [byte, tail @ ..] => (*low..=*high).contains(byte) && matches(rest, tail),
            [] => false,
        },
        [b'{' | b'?' | b'@' | b'[', ..] => panic!("a glob this test does not read"),
        [byte, rest @ ..] => path.first() == Some(byte) && matches(rest, &path[1..]),
    }
}

/// The file rules that match `path`.
#[cfg(test)]
fn rules_for(path: &Path) -> Vec<FileRule<'static>> {
    let path = path.to_str().unwrap().as_bytes();
    let found = file_rules().into_iter();
    found
        .filter(|rule| matches(rule.glob.as_bytes(), path))
        .collect()
}

/// The rights of the one rule that matches `path`.
#[cfg(test)]
fn rights(path: &Path) -> &'static str {
    match rules_for(path).as_slice() {
        [rule] => rule.rights,
        rules => panic!("{} matches {rules:?}", path.display()),
    }
}

#[cfg(test)]
fn vm() -> VmRunDir {
    let run = RunRoot::new(PathBuf::from(RUN_ROOT)).unwrap();
    run.vm(&ID.parse().unwrap())
}

#[test]
fn the_profile_attaches_to_the_installed_binary() {
    assert!(PROFILE.lines().any(|line| line == HEADER));
    assert_eq!(
        rights(Path::new("/opt/components/vmm/bin/cloud-hypervisor")),
        "mr"
    );
}

#[test]
fn each_file_of_the_vm_run_dir_has_its_rights() {
    let vm = vm();
    assert_eq!(rights(&vm.api_socket()), "rw");
    assert_eq!(rights(&vm.vsock_socket()), "rw");
    assert_eq!(rights(&vm.api_socket_lock()), "wk");
    assert_eq!(rights(&vm.serial_log()), "w");
    let all = [
        vm.api_socket(),
        vm.vsock_socket(),
        vm.api_socket_lock(),
        vm.serial_log(),
    ];
    for path in all {
        assert!(rules_for(&path).iter().all(|rule| rule.owner));
    }
}

#[test]
fn one_pattern_covers_every_host_listener() {
    let vm = vm();
    for &port in HostPort::ALL {
        let rules = rules_for(&vm.host_listener(port));
        assert_eq!(rules.len(), 1, "port {}", port.number());
        assert!(
            rules
                .iter()
                .all(|rule| rule.glob.ends_with("/vsock.sock_*"))
        );
    }
    let listeners = file_rules().into_iter();
    assert_eq!(listeners.filter(|rule| rule.glob.contains("_*")).count(), 1);
}

#[test]
fn no_rule_names_a_socket_of_the_host() {
    let run = RunRoot::new(PathBuf::from(RUN_ROOT)).unwrap();
    let host = [
        run.vmm_socket(),
        PathBuf::from("/run/user/1000/bus"),
        PathBuf::from("/run/user/1000/systemd/private"),
        PathBuf::from("/run/systemd/private"),
        PathBuf::from("/run/systemd/journal/stdout"),
        PathBuf::from("/run/dbus/system_bus_socket"),
        PathBuf::from("/run/docker.sock"),
    ];
    for path in host {
        assert_eq!(rules_for(&path), [], "{}", path.display());
    }
}

#[test]
fn the_state_files_have_their_rights() {
    let state = StateRoot::new(PathBuf::from("/srv/agents/state/creche/vmm")).unwrap();
    let session = state.session(&ID.parse().unwrap());
    let snapshot = session.snapshot(SnapshotGeneration(3));
    let digest = format!("{:064x}", 0xc15);
    assert_eq!(rights(&state.store_dir().join(digest)), "rk");
    assert_eq!(rights(&session.disk()), "rwk");
    assert_eq!(rights(&snapshot.join("memory-ranges")), "rw");
    assert_eq!(rights(&session.snapshot_key(SnapshotGeneration(3))), "rw");
    assert_eq!(rules_for(&state.row_file(&ID.parse().unwrap())), []);
}

#[test]
fn the_devices_are_those_of_the_landlock_rules() {
    assert_eq!(rights(Path::new(KVM)), "rw");
    assert_eq!(rights(Path::new(URANDOM)), "r");
}

#[test]
fn no_inet_socket_and_no_abstract_unix_socket() {
    let lines: Vec<&str> = rule_lines().collect();
    assert!(lines.contains(&"unix type=stream,"));
    assert!(lines.contains(&"deny unix addr=@**,"));
    assert!(lines.contains(&"deny unix peer=(addr=@**),"));
}

#[test]
fn each_rule_line_has_a_form_this_test_reads() {
    let rules = file_rules();
    let others = rule_lines().filter(|line| OTHER_LINES.contains(line));
    assert_eq!(rules.len() + others.count(), rule_lines().count());
    for line in OTHER_LINES {
        assert_eq!(
            rule_lines().filter(|rule| rule == line).count(),
            1,
            "{line}"
        );
    }
}

#[test]
fn no_rule_grants_all_files_or_a_network_socket() {
    for line in rule_lines() {
        let words: Vec<&str> = line
            .split(|char: char| char.is_whitespace() || char == ',')
            .collect();
        assert!(!words.contains(&"file"), "{line}");
        assert!(!words.contains(&"network"), "{line}");
        assert!(!words.contains(&"allow"), "{line}");
        assert!(!words.contains(&"audit"), "{line}");
    }
}
