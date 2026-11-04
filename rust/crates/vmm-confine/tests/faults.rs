//! The fault file of the vmm in temp directories (decision CH21-1,
//! contract 05 §3.3.1, gap G18 of decision CH36).

use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use creche_vmm_confine::faults::{FaultBook, FaultCode, FaultDir, REFRESH};
use creche_wire::FamilyName;
use serde_json::Value;

/// The `since` of the fixture: 2026-09-19T11:58:41Z.
const SINCE: u64 = 1_789_819_121;

/// The `written_at` of the fixture: 2026-09-19T11:59:55Z.
const WRITTEN: u64 = 1_789_819_195;

/// The message of the fixture.
const MESSAGE: &str = "playpen answered fatal: mount_dir_unset";

// Each helper with `cfg(test)` may unwrap (rust/clippy.toml).

/// A faults root in a new directory in `/tmp`, removed on drop.
struct Root(PathBuf);

#[cfg(test)]
impl Root {
    fn new(name: &str) -> Self {
        let root = PathBuf::from(format!("/tmp/vmm-faults-{}-{name}", std::process::id()));
        let _ = fs::remove_dir_all(&root);
        fs::create_dir_all(&root).unwrap();
        Self(root)
    }

    fn book(&self, now: u64) -> FaultBook {
        FaultBook::start(FaultDir::open(&self.0).unwrap(), at(now)).unwrap()
    }

    fn read(&self, family: &str) -> Value {
        serde_json::from_slice(&fs::read(self.file(family)).unwrap()).unwrap()
    }

    fn file(&self, family: &str) -> PathBuf {
        self.0.join("vmm").join(format!("{family}.json"))
    }
}

impl Drop for Root {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

fn at(secs: u64) -> SystemTime {
    UNIX_EPOCH + Duration::from_secs(secs)
}

#[cfg(test)]
fn chat() -> FamilyName {
    "chat".parse().unwrap()
}

#[cfg(test)]
fn mode(path: &Path) -> u32 {
    fs::metadata(path).unwrap().permissions().mode() & 0o777
}

#[test]
fn the_bytes_match_the_caregiver_fixture() {
    let root = Root::new("fixture");
    let mut book = root.book(SINCE);
    book.raise(
        &chat(),
        FaultCode::SandboxStartFailed,
        MESSAGE.to_owned(),
        at(SINCE),
    )
    .unwrap();
    book.refresh(at(WRITTEN)).unwrap();
    let fixture = include_bytes!("fixtures/caregiver_fault.json");
    assert_eq!(fs::read(root.file("chat")).unwrap(), fixture);
}

#[test]
fn a_write_is_atomic_with_the_contract_modes() {
    let root = Root::new("atomic");
    let mut book = root.book(SINCE);
    book.raise(
        &chat(),
        FaultCode::EgressAssertFailed,
        "net".to_owned(),
        at(SINCE),
    )
    .unwrap();
    let names: Vec<_> = fs::read_dir(root.0.join("vmm"))
        .unwrap()
        .map(|entry| entry.unwrap().file_name())
        .collect();
    assert_eq!(names, ["chat.json"]);
    assert_eq!(mode(&root.0.join("vmm")), 0o750);
    assert_eq!(mode(&root.file("chat")), 0o640);
    assert_eq!(
        root.read("chat")["faults"][0]["code"],
        "egress_assert_failed"
    );
    assert_eq!(root.read("chat")["faults"][0]["blocks_turns"], true);
}

#[test]
fn an_open_code_keeps_its_since_and_a_new_code_adds_an_entry() {
    let root = Root::new("since");
    let mut book = root.book(SINCE);
    let family = chat();
    book.raise(
        &family,
        FaultCode::SandboxStartFailed,
        "one".to_owned(),
        at(SINCE),
    )
    .unwrap();
    book.raise(
        &family,
        FaultCode::SandboxStartFailed,
        "two".to_owned(),
        at(WRITTEN),
    )
    .unwrap();
    book.raise(
        &family,
        FaultCode::EgressAssertFailed,
        "net".to_owned(),
        at(WRITTEN),
    )
    .unwrap();
    let faults = &root.read("chat")["faults"];
    assert_eq!(faults[0]["since"], "2026-09-19T11:58:41Z");
    assert_eq!(faults[0]["message"], "two");
    assert_eq!(faults[1]["since"], "2026-09-19T11:59:55Z");
    assert_eq!(faults.as_array().unwrap().len(), 2);
}

#[test]
fn control_ready_writes_an_empty_list() {
    let root = Root::new("clear");
    let mut book = root.book(SINCE);
    book.raise(
        &chat(),
        FaultCode::SandboxStartFailed,
        MESSAGE.to_owned(),
        at(SINCE),
    )
    .unwrap();
    book.clear(&chat(), at(WRITTEN)).unwrap();
    assert_eq!(root.read("chat")["faults"], serde_json::json!([]));
    assert_eq!(root.read("chat")["written_at"], "2026-09-19T11:59:55Z");
    // A family with no open fault gets no file.
    book.clear(&"jobs".parse().unwrap(), at(WRITTEN)).unwrap();
    assert!(!root.file("jobs").exists());
}

#[test]
fn a_refresh_rewrites_inside_90_seconds() {
    assert!(REFRESH < Duration::from_secs(90));
    let root = Root::new("refresh");
    let mut book = root.book(SINCE);
    book.raise(
        &chat(),
        FaultCode::SandboxStartFailed,
        MESSAGE.to_owned(),
        at(SINCE),
    )
    .unwrap();
    book.refresh(at(SINCE) + REFRESH).unwrap();
    assert_eq!(root.read("chat")["written_at"], "2026-09-19T11:59:11Z");
    assert_eq!(
        root.read("chat")["faults"][0]["since"],
        "2026-09-19T11:58:41Z"
    );
    // A cleared family is not rewritten.
    book.clear(&chat(), at(WRITTEN)).unwrap();
    book.refresh(at(WRITTEN) + REFRESH).unwrap();
    assert_eq!(root.read("chat")["written_at"], "2026-09-19T11:59:55Z");
}

#[test]
fn a_start_clears_every_file_and_removes_a_temp_file() {
    let root = Root::new("start");
    let mut book = root.book(SINCE);
    book.raise(
        &chat(),
        FaultCode::SandboxStartFailed,
        MESSAGE.to_owned(),
        at(SINCE),
    )
    .unwrap();
    let vmm = root.0.join("vmm");
    fs::write(vmm.join(".jobs.json.tmp"), "{").unwrap();
    fs::write(vmm.join("README"), "kept").unwrap();
    drop(book);
    let _book = root.book(WRITTEN);
    assert_eq!(root.read("chat")["faults"], serde_json::json!([]));
    assert_eq!(root.read("chat")["written_at"], "2026-09-19T11:59:55Z");
    assert!(!vmm.join(".jobs.json.tmp").exists());
    assert!(vmm.join("README").exists());
}

#[test]
fn the_faults_root_is_absolute() {
    assert!(FaultDir::open(Path::new("faults")).is_err());
    assert!(FaultDir::open(Path::new("/tmp/../faults")).is_err());
}
