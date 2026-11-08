//! `vmm verify --json --env-file <path>`, the verify hook of the vmm row
//! (decision CH76 rule 2). It runs with no KVM and no sudo. A test of the
//! rust job runs as the runner user, so the vmm program of cargo, and each
//! copy that the test makes, has an owner other than root, and verify
//! fails. `tests/kvm_verify.rs` proves the pass on root-owned copies of
//! the CI build (decision CH36 G20).

use std::fs;
use std::os::unix::fs::MetadataExt;
use std::path::{Path, PathBuf};
use std::process::{Command, Output};

use creche_vmm::config::{CH, CH_REMOTE, GUEST_INIT};
use creche_vmm::verify::{CH_VERSION, ENV_FILE, GUEST_INIT_STATIC, VMM};
use serde_json::Value;

/// The CH fixture of the version check (gap G10).
const CH_FIXTURE: &str = concat!(
    env!("CARGO_MANIFEST_DIR"),
    "/tests/fixtures/cloud-hypervisor"
);

/// A program that root owns on Linux and on macOS, and that is no static
/// x86_64 ELF program: a dynamic ELF on Linux, Mach-O on macOS.
const ROOT_BASH: &str = "/bin/bash";

/// A scratch dir that its drop removes.
struct Scratch(PathBuf);

impl Drop for Scratch {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

#[cfg(test)]
impl Scratch {
    fn new(name: &str) -> Self {
        let dir =
            std::env::temp_dir().join(format!("creche-vmm-verify-{}-{name}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        Self(dir)
    }

    /// Writes `vmm.env` with the header of `bin/fleet-up` and `keys`.
    fn env_file(&self, keys: &[(&str, &Path)]) -> PathBuf {
        let mut text = String::from("# Written by bin/fleet-up, mode 0600.\n");
        for (key, path) in keys {
            text.push_str(&format!("{key}={}\n", path.display()));
        }
        let path = self.0.join("vmm.env");
        fs::write(&path, text).unwrap();
        path
    }

    /// A copy of the CH fixture that the test user owns.
    fn copy(&self, name: &str) -> PathBuf {
        let path = self.0.join(name);
        fs::copy(CH_FIXTURE, &path).unwrap();
        path
    }
}

#[cfg(test)]
/// Runs `vmm verify` on `env_file`.
fn verify(env_file: &Path) -> Output {
    Command::new(env!("CARGO_BIN_EXE_vmm"))
        .args(["verify", "--json", "--env-file"])
        .arg(env_file)
        .env_clear()
        .output()
        .unwrap()
}

#[cfg(test)]
/// The one JSON line of stdout.
fn json_line(output: &Output) -> Value {
    let text = String::from_utf8(output.stdout.clone()).unwrap();
    let line = text.strip_suffix('\n').unwrap();
    assert!(!line.contains('\n'), "{text}");
    serde_json::from_str(line).unwrap()
}

#[cfg(test)]
/// The check `name` of `report`: its `ok` and its detail.
fn check(report: &Value, name: &str) -> (bool, String) {
    let found = report["checks"]
        .as_array()
        .unwrap()
        .iter()
        .find(|check| check["name"] == name)
        .unwrap_or_else(|| panic!("no check {name} in {report}"));
    (
        found["ok"].as_bool().unwrap(),
        found["detail"].as_str().unwrap().to_owned(),
    )
}

/// The test user owns each program, so each owner check fails and names
/// the owner, and verify runs no CH that it does not trust. A test that
/// runs as root has nothing to prove here.
#[test]
fn a_program_the_vmm_user_owns_fails_verify() {
    let scratch = Scratch::new("owner");
    let ch = scratch.copy("cloud-hypervisor");
    let uid = fs::metadata(&ch).unwrap().uid();
    if uid == 0 {
        return;
    }
    let env_file = scratch.env_file(&[
        (CH, &ch),
        (CH_REMOTE, &scratch.copy("ch-remote")),
        (GUEST_INIT, &scratch.copy("guest-init")),
    ]);
    let output = verify(&env_file);
    assert_eq!(output.status.code(), Some(1), "{output:?}");
    let report = json_line(&output);
    assert_eq!(report["ok"], false);
    assert!(check(&report, ENV_FILE).0);
    let owned_by = format!("is owned by uid {uid}, not by root");
    for name in [VMM, CH, CH_REMOTE, GUEST_INIT] {
        let (ok, detail) = check(&report, name);
        assert!(!ok && detail.contains(&owned_by), "{name}: {detail}");
    }
    assert_eq!(
        check(&report, CH_VERSION),
        (false, format!("not run: {CH} failed"))
    );
    assert_eq!(
        check(&report, GUEST_INIT_STATIC),
        (false, format!("not run: {GUEST_INIT} failed"))
    );
}

/// The vmm compiles in no default (decision CH59 rule 1), so a key that
/// the file lacks fails verify, and the report names the key.
#[test]
fn a_missing_key_fails_verify_and_names_it() {
    let scratch = Scratch::new("missing");
    let ch = scratch.copy("cloud-hypervisor");
    let env_file = scratch.env_file(&[(CH, &ch), (GUEST_INIT, &ch)]);
    let output = verify(&env_file);
    assert_eq!(output.status.code(), Some(1), "{output:?}");
    let report = json_line(&output);
    assert_eq!(
        check(&report, ENV_FILE),
        (false, format!("{CH_REMOTE}: the key has no value"))
    );
    assert_eq!(report["checks"].as_array().unwrap().len(), 1);

    let output = verify(&scratch.0.join("absent.env"));
    assert_eq!(output.status.code(), Some(1), "{output:?}");
    let (ok, detail) = check(&json_line(&output), ENV_FILE);
    assert!(
        !ok && detail.starts_with("the env file cannot be read"),
        "{detail}"
    );
}

/// Each run writes one JSON object on one line: `ok` and the checks in
/// their order. A guest init that root owns but that is no static x86_64
/// ELF program fails its own check.
#[test]
fn verify_writes_one_json_line() {
    let scratch = Scratch::new("line");
    let ch = scratch.copy("cloud-hypervisor");
    let bash = Path::new(ROOT_BASH);
    let env_file = scratch.env_file(&[(CH, &ch), (CH_REMOTE, &ch), (GUEST_INIT, bash)]);
    let output = verify(&env_file);
    assert_eq!(output.status.code(), Some(1), "{output:?}");
    assert!(output.stderr.is_empty(), "{output:?}");
    let report = json_line(&output);
    let names: Vec<&str> = report["checks"]
        .as_array()
        .unwrap()
        .iter()
        .map(|check| check["name"].as_str().unwrap())
        .collect();
    assert_eq!(
        names,
        [
            ENV_FILE,
            VMM,
            CH,
            CH_REMOTE,
            GUEST_INIT,
            CH_VERSION,
            GUEST_INIT_STATIC
        ]
    );
    if fs::metadata(bash).unwrap().uid() == 0 {
        assert_eq!(check(&report, GUEST_INIT), (true, ROOT_BASH.to_owned()));
        let (ok, detail) = check(&report, GUEST_INIT_STATIC);
        assert!(!ok, "{detail}");
    }
}

/// A wrong argument list exits 78 with the usage line, as for each mode.
#[test]
fn a_wrong_argument_list_exits_78() {
    let output = Command::new(env!("CARGO_BIN_EXE_vmm"))
        .args(["verify", "--env-file", "/srv/vmm.env"])
        .output()
        .unwrap();
    assert_eq!(output.status.code(), Some(78), "{output:?}");
    assert!(output.stdout.is_empty());
    let stderr = String::from_utf8(output.stderr).unwrap();
    assert!(
        stderr.contains("vmm verify --json --env-file <path>"),
        "{stderr}"
    );
}
