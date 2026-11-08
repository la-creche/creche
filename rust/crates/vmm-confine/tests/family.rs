//! The read of a family's `config/`, its VM shape, its family state, its
//! egress allowlist and its clone list in temp directories (decisions
//! CH5b, CH6-4, CH10-10, CH23, CH31, CH33 rule 3, CH36 G6, CH43 rule 3,
//! CH62 rule 21, CH69 rules 3 and 4). The family state tests copy the fixture
//! family root `fixtures/family`, because no live host writes
//! `model_live_at` before the cut (decision CH45 rule 2).

use std::fs;
use std::os::unix::fs::symlink;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, mpsc};
use std::thread;
use std::time::{Duration, Instant, UNIX_EPOCH};

use creche_vmm_confine::family::{
    EGRESS_CAP, FamilyError, FamilyRoot, READ_TRIES, REPOS_CAP, RETRY_PAUSE, ReachFault, SHAPE_CAP,
    ShapeFault, StateFault, StateRead,
};
use creche_vmm_store::layout::RootError;
use creche_wire::{
    FAMILY_STATE_CAP, FamilyEgress, FamilyName, FamilyRepos, FamilyShape, FamilyStateReply,
};
use serde_json::{Value, json};

const SHAPE: &str = r#"{"cpus":2,"flavor":"base","memory_mib":2048}"#;
const BIGGER: &str = r#"{"cpus":4,"flavor":"python","memory_mib":8192}"#;
const EGRESS: &str = r#"{"hosts":[{"host":"github.com","port":443}]}"#;
const REPOS: &str = r#"{"repos":["chaperone","creche"]}"#;

// Each helper with `cfg(test)` may unwrap (rust/clippy.toml).

/// A family root in a new directory in `/tmp`, removed on drop.
struct Root(PathBuf);

#[cfg(test)]
impl Root {
    fn new(name: &str) -> Self {
        let root = PathBuf::from(format!("/tmp/vmm-family-{}-{name}", std::process::id()));
        let _ = fs::remove_dir_all(&root);
        fs::create_dir_all(&root).unwrap();
        Self(root)
    }

    fn families(&self) -> FamilyRoot {
        FamilyRoot::new(self.0.clone()).unwrap()
    }

    /// Writes `config/shape.json` of the family `code`, or of `dir`.
    fn config(&self, dir: &str, shape: &str) -> PathBuf {
        let config = self.0.join(dir);
        fs::create_dir_all(&config).unwrap();
        fs::write(config.join("shape.json"), shape).unwrap();
        config
    }
}

#[cfg(test)]
impl Root {
    /// A family root that holds a copy of `fixtures/family`.
    fn fixture(name: &str) -> Self {
        let root = Self::new(name);
        let fixture = Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures/family/code");
        let copied = Command::new("cp")
            .arg("-R")
            .arg(fixture)
            .arg(root.0.join("code"))
            .status()
            .unwrap();
        assert!(copied.success());
        root
    }

    /// A path in the family dir of `code`.
    fn at(&self, path: &str) -> PathBuf {
        self.0.join("code").join(path)
    }

    fn state(&self) -> Result<StateRead, StateFault> {
        self.families().read_state(&code())
    }
}

/// The reply of a read as JSON.
#[cfg(test)]
fn reply(read: &StateRead) -> Value {
    let line: FamilyStateReply = serde_json::from_slice(read.line()).unwrap();
    assert!(matches!(line, FamilyStateReply::State(_)));
    serde_json::from_slice(read.line()).unwrap()
}

/// The fixture `runtime.json` with `change` applied to its JSON.
#[cfg(test)]
fn runtime(change: impl FnOnce(&mut serde_json::Map<String, Value>)) -> String {
    let mut body = json!({
        "shell": true,
        "sandbox_tools": ["read"],
        "model_alias": "litellm/fixture-model",
    });
    change(body.as_object_mut().unwrap());
    body.to_string()
}

impl Drop for Root {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

#[cfg(test)]
fn code() -> FamilyName {
    "code".parse().unwrap()
}

#[cfg(test)]
fn shape(text: &str) -> FamilyShape {
    serde_json::from_str(text).unwrap()
}

#[test]
fn the_shape_reads_from_config_shape_json() {
    let root = Root::new("reads");
    root.config("code/config", SHAPE);
    assert!(root.families().exists(&code()).is_ok());
    assert_eq!(root.families().read_shape(&code()).unwrap(), shape(SHAPE));
}

#[test]
fn an_absent_family_or_config_dir_is_unknown() {
    let root = Root::new("absent");
    let families = root.families();
    assert!(matches!(
        families.exists(&code()),
        Err(FamilyError::Unknown)
    ));
    fs::create_dir_all(root.0.join("code")).unwrap();
    let start = Instant::now();
    assert!(matches!(
        families.exists(&code()),
        Err(FamilyError::Unknown)
    ));
    // An absent config/ may be the rename window, so each try waits.
    assert!(start.elapsed() >= RETRY_PAUSE * (READ_TRIES - 1));
    assert!(matches!(
        families.read_shape(&code()),
        Err(FamilyError::Unknown)
    ));
}

#[test]
fn a_bad_shape_file_is_not_valid() {
    let root = Root::new("bad");
    let config = root.config("code/config", SHAPE);
    let not_shape = [
        r#"{"cpus":9,"flavor":"base","memory_mib":2048}"#,
        r#"{"cpus":2,"flavor":"gpu","memory_mib":2048}"#,
        r#"{"cpus":2,"flavor":"base","memory_mib":2048,"disk":1}"#,
        r#"{"cpus":2,"cpus":2,"flavor":"base","memory_mib":2048}"#,
        r#"[2,"base",2048]"#,
        "",
    ];
    for text in not_shape {
        fs::write(config.join("shape.json"), text).unwrap();
        let read = root.families().read_shape(&code());
        let fault = Some(ShapeFault::NotShape);
        assert!(
            matches!(read, Err(FamilyError::NotValid(f)) if Some(f) == fault),
            "{text}"
        );
    }
    let padded = format!("{SHAPE}{}", " ".repeat(usize::try_from(SHAPE_CAP).unwrap()));
    fs::write(config.join("shape.json"), padded).unwrap();
    let read = root.families().read_shape(&code());
    assert!(matches!(
        read,
        Err(FamilyError::NotValid(ShapeFault::TooLarge))
    ));
    fs::remove_file(config.join("shape.json")).unwrap();
    let read = root.families().read_shape(&code());
    assert!(matches!(
        read,
        Err(FamilyError::NotValid(ShapeFault::Absent))
    ));
    // A config/ with no shape.json still exists for create.
    assert!(root.families().exists(&code()).is_ok());
}

#[test]
fn a_shape_file_that_is_no_regular_file_is_not_valid() {
    let root = Root::new("not-file");
    let other = root.config("other", SHAPE);
    let config = root.0.join("code/config");
    fs::create_dir_all(&config).unwrap();
    symlink(other.join("shape.json"), config.join("shape.json")).unwrap();
    let read = root.families().read_shape(&code());
    assert!(matches!(
        read,
        Err(FamilyError::NotValid(ShapeFault::NotFile))
    ));
    fs::remove_file(config.join("shape.json")).unwrap();
    fs::create_dir(config.join("shape.json")).unwrap();
    let read = root.families().read_shape(&code());
    assert!(matches!(
        read,
        Err(FamilyError::NotValid(ShapeFault::NotFile))
    ));
    // A FIFO with no writer: only `O_NONBLOCK` keeps the open from waiting
    // for a writer forever. The read runs on a thread, so a lost flag
    // fails the test at the timeout and does not hang it.
    fs::remove_dir(config.join("shape.json")).unwrap();
    let made = Command::new("mkfifo")
        .arg(config.join("shape.json"))
        .status()
        .unwrap();
    assert!(made.success());
    let families = root.families();
    let (sent, got) = mpsc::channel();
    thread::spawn(move || sent.send(families.read_shape(&code())));
    let read = got.recv_timeout(Duration::from_secs(5)).unwrap();
    assert!(matches!(
        read,
        Err(FamilyError::NotValid(ShapeFault::NotFile))
    ));
}

#[test]
fn a_symlinked_config_or_family_dir_is_refused() {
    let root = Root::new("symlink");
    let other = root.config("other/config", SHAPE);
    fs::create_dir_all(root.0.join("code")).unwrap();
    symlink(&other, root.0.join("code/config")).unwrap();
    assert!(matches!(
        root.families().exists(&code()),
        Err(FamilyError::NotDir)
    ));
    assert!(matches!(
        root.families().read_shape(&code()),
        Err(FamilyError::NotDir)
    ));
    fs::remove_dir_all(root.0.join("code")).unwrap();
    symlink(root.0.join("other"), root.0.join("code")).unwrap();
    assert!(matches!(
        root.families().read_shape(&code()),
        Err(FamilyError::NotDir)
    ));
    fs::remove_file(root.0.join("code")).unwrap();
    fs::write(root.0.join("code"), "not a dir").unwrap();
    assert!(matches!(
        root.families().exists(&code()),
        Err(FamilyError::NotDir)
    ));
}

#[test]
fn a_swap_during_the_read_reads_again() {
    let root = Root::new("swap");
    let config = root.config("code/config", SHAPE);
    let mut calls = 0;
    let read = root.families().with_config(&code(), |_| {
        calls += 1;
        if calls == 1 {
            // caregiver's folder swap (`atomic_replace_dir`) under the handle.
            root.config("code/config.tmp", BIGGER);
            fs::rename(&config, root.0.join("code/config.old")).unwrap();
            fs::rename(root.0.join("code/config.tmp"), &config).unwrap();
            fs::remove_dir_all(root.0.join("code/config.old")).unwrap();
        }
        Ok(calls)
    });
    assert_eq!(read.unwrap(), 2);
    assert_eq!(root.families().read_shape(&code()).unwrap(), shape(BIGGER));
}

#[test]
fn a_swap_during_every_read_gives_up() {
    let root = Root::new("swaps");
    let config = root.config("code/config", SHAPE);
    let mut calls = 0;
    let read = root.families().with_config(&code(), |_| {
        calls += 1;
        let next = root.0.join("code/config.tmp");
        fs::create_dir(&next).unwrap();
        fs::remove_dir_all(&config).unwrap();
        fs::rename(&next, &config).unwrap();
        Ok(())
    });
    assert!(matches!(read, Err(FamilyError::Swapped)));
    assert_eq!(calls, READ_TRIES);
}

#[test]
fn the_root_is_absolute_with_no_parent_part() {
    let relative = FamilyRoot::new(PathBuf::from("families"));
    assert!(matches!(relative, Err(RootError::NotAbsolute)));
    let parent = FamilyRoot::new(PathBuf::from("/srv/agents/../families"));
    assert!(matches!(parent, Err(RootError::ParentComponent)));
}

#[test]
fn reads_creds_and_config_into_one_state() {
    let root = Root::fixture("state");
    let read = root.state().unwrap();
    let expected = json!({"state": {
        "config": {
            "instructions": "You help the code family.\n",
            "model_alias": "litellm/fixture-model",
            "sandbox_tools": {
                "bash": true, "edit": false, "glob": false,
                "grep": true, "read": true, "write": false,
            },
            "skills": {
                "alpha": "---\nname: alpha\ndescription: The first fixture skill.\n---\nAlpha body.\n",
                "beta": "---\nname: beta\ndescription: The second fixture skill.\n---\nBeta body.\n",
            },
            "system_prompt": "replace",
        },
        "creds": {
            "litellm_key": "sk-fixture-key-0001",
            "pep_token": "ab".repeat(32),
        },
    }});
    assert_eq!(reply(&read), expected);
    assert!(!read.line().contains(&b'\n'));
    // 2026-11-06T12:00:10Z.
    let live = UNIX_EPOCH + Duration::from_secs(1_793_966_410);
    assert_eq!(read.model_live_at(), Some(live));
    // The skills go in ascending name order, whatever the dir order.
    let text = String::from_utf8(read.line().to_vec()).unwrap();
    assert!(text.find("\"alpha\"").unwrap() < text.find("\"beta\"").unwrap());
}

#[test]
fn drops_the_caregiver_only_fields() {
    let root = Root::fixture("drops");
    let read = root.state().unwrap();
    let text = String::from_utf8(read.line().to_vec()).unwrap();
    for gone in [
        "epoch",
        "written_at",
        "previous",
        "cdcd",
        "shell",
        "model_live_at",
    ] {
        assert!(!text.contains(gone), "{gone}");
    }
    // The Debug of a read writes no byte of the secrets (invariant 13).
    let debug = format!("{read:?}");
    assert!(!debug.contains("sk-") && !debug.contains("abab"), "{debug}");
    // A creds.json with no previous_* fields still reads, as caregiver
    // reads it.
    fs::write(
        root.at("creds/creds.json"),
        json!({"litellm_key": "sk-k", "pep_token": "ab".repeat(32)}).to_string(),
    )
    .unwrap();
    assert!(root.state().is_ok());
}

#[test]
fn absent_parts_take_their_defaults() {
    let root = Root::fixture("defaults");
    fs::write(root.at("config/runtime.json"), runtime(|_| {})).unwrap();
    fs::remove_file(root.at("config/instructions.md")).unwrap();
    fs::remove_dir_all(root.at("config/skills")).unwrap();
    let read = root.state().unwrap();
    let config = &reply(&read)["state"]["config"];
    assert_eq!(config["instructions"], "");
    assert_eq!(config["skills"], json!({}));
    assert_eq!(config["system_prompt"], "append");
    assert_eq!(read.model_live_at(), None);
}

#[test]
fn missing_creds_is_creds_missing() {
    let root = Root::fixture("no-creds");
    fs::remove_file(root.at("creds/creds.json")).unwrap();
    assert_eq!(root.state().unwrap_err(), StateFault::CredsMissing);
    fs::remove_dir(root.at("creds")).unwrap();
    assert_eq!(root.state().unwrap_err(), StateFault::CredsMissing);
    let other: FamilyName = "other".parse().unwrap();
    let read = root.families().read_state(&other);
    assert_eq!(read.unwrap_err(), StateFault::CredsMissing);
}

#[test]
fn missing_config_is_config_missing() {
    let root = Root::fixture("no-config");
    fs::remove_dir_all(root.at("config")).unwrap();
    let start = Instant::now();
    assert_eq!(root.state().unwrap_err(), StateFault::ConfigMissing);
    // An absent config/ may be the rename window, so each try waits.
    assert!(start.elapsed() >= RETRY_PAUSE * (READ_TRIES - 1));
}

#[test]
fn creds_missing_wins() {
    let root = Root::fixture("both");
    fs::remove_file(root.at("creds/creds.json")).unwrap();
    fs::remove_dir_all(root.at("config")).unwrap();
    assert_eq!(root.state().unwrap_err(), StateFault::CredsMissing);
    fs::create_dir(root.at("config")).unwrap();
    fs::write(root.at("config/runtime.json"), "{").unwrap();
    assert_eq!(root.state().unwrap_err(), StateFault::CredsMissing);
}

#[test]
fn a_bad_litellm_key_is_not_valid() {
    let root = Root::fixture("bad-creds");
    let token = "ab".repeat(32);
    let bad = [
        json!({"litellm_key": "pk-key", "pep_token": token}).to_string(),
        json!({"litellm_key": "sk-a key", "pep_token": token}).to_string(),
        json!({"litellm_key": "sk-key", "pep_token": "AB".repeat(32)}).to_string(),
        json!({"pep_token": token}).to_string(),
        json!({"litellm_key": "sk-key", "pep_token": token, "spend": 1}).to_string(),
        format!(r#"{{"litellm_key":"sk-a","litellm_key":"sk-b","pep_token":"{token}"}}"#),
        // serde refuses a duplicate field of the struct by itself. Only the
        // strict check refuses one inside a value that `IgnoredAny` reads.
        format!(r#"{{"litellm_key":"sk-key","pep_token":"{token}","written_at":{{"a":1,"a":2}}}}"#),
        "[]".to_owned(),
        String::new(),
    ];
    for text in bad {
        fs::write(root.at("creds/creds.json"), &text).unwrap();
        let read = root.state();
        assert_eq!(read.unwrap_err(), StateFault::NotValid, "{text}");
    }
}

#[test]
fn a_bad_runtime_file_is_not_valid() {
    let root = Root::fixture("bad-runtime");
    let bad = [
        runtime(|body| drop(body.remove("model_alias"))),
        runtime(|body| drop(body.insert("model_alias".into(), json!("Upper")))),
        runtime(|body| drop(body.remove("sandbox_tools"))),
        runtime(|body| drop(body.insert("sandbox_tools".into(), json!(["read", "read"])))),
        runtime(|body| drop(body.insert("sandbox_tools".into(), json!(["codemode"])))),
        runtime(|body| drop(body.insert("shell".into(), json!("yes")))),
        runtime(|body| drop(body.insert("system_prompt".into(), json!("prepend")))),
        runtime(|body| drop(body.insert("model_live_at".into(), Value::Null))),
        runtime(|body| drop(body.insert("model_live_at".into(), json!("2026-11-06T12:00:10.5Z")))),
        runtime(|body| drop(body.insert("config_rev".into(), json!(1)))),
        r#"{"shell":true,"shell":true,"sandbox_tools":[],"model_alias":"m"}"#.to_owned(),
    ];
    for text in bad {
        fs::write(root.at("config/runtime.json"), &text).unwrap();
        let read = root.state();
        assert_eq!(read.unwrap_err(), StateFault::NotValid, "{text}");
    }
    fs::remove_file(root.at("config/runtime.json")).unwrap();
    let read = root.state();
    assert_eq!(read.unwrap_err(), StateFault::NotValid);
    fs::write(root.at("config/runtime.json"), runtime(|_| {})).unwrap();
    fs::write(root.at("config/instructions.md"), [0xff, 0xfe]).unwrap();
    let read = root.state();
    assert_eq!(read.unwrap_err(), StateFault::NotValid);
    fs::write(root.at("config/instructions.md"), "").unwrap();
    fs::create_dir(root.at("config/skills/Not_A_Name")).unwrap();
    fs::write(root.at("config/skills/Not_A_Name/SKILL.md"), "x").unwrap();
    let read = root.state();
    assert_eq!(read.unwrap_err(), StateFault::NotValid);
    fs::remove_dir_all(root.at("config/skills/Not_A_Name")).unwrap();
    fs::create_dir(root.at("config/skills/gamma")).unwrap();
    let read = root.state();
    assert_eq!(read.unwrap_err(), StateFault::NotValid);
    fs::remove_dir(root.at("config/skills/gamma")).unwrap();
    fs::write(root.at("config/skills/notes.txt"), "x").unwrap();
    let read = root.state();
    assert_eq!(read.unwrap_err(), StateFault::NotValid);
}

#[test]
fn a_state_over_the_cap_is_too_large() {
    let root = Root::fixture("cap");
    fs::write(root.at("config/instructions.md"), "").unwrap();
    let empty = root.state().unwrap().line().len();
    // An ASCII letter is one byte in the file and one in the reply.
    let fits = FAMILY_STATE_CAP - empty;
    fs::write(root.at("config/instructions.md"), "a".repeat(fits)).unwrap();
    assert_eq!(root.state().unwrap().line().len(), FAMILY_STATE_CAP);
    fs::write(root.at("config/instructions.md"), "a".repeat(fits + 1)).unwrap();
    assert_eq!(root.state().unwrap_err(), StateFault::TooLarge);
    // A text past the budget stops the read before the reply is made.
    fs::write(root.at("config/instructions.md"), "").unwrap();
    let skill = root.at("config/skills/alpha/SKILL.md");
    fs::write(&skill, "a".repeat(FAMILY_STATE_CAP + 1)).unwrap();
    assert_eq!(root.state().unwrap_err(), StateFault::TooLarge);
    // A JSON file past the cap is not_valid: its excess may sit in fields
    // that the reply drops.
    fs::write(&skill, "a").unwrap();
    let padded = format!("{}{}", runtime(|_| {}), " ".repeat(FAMILY_STATE_CAP));
    fs::write(root.at("config/runtime.json"), padded).unwrap();
    let read = root.state();
    assert_eq!(read.unwrap_err(), StateFault::NotValid);
}

#[test]
fn a_symlinked_config_is_refused() {
    let root = Root::fixture("links");
    let other = Root::fixture("links-other");
    let refused = [
        "config",
        "config/runtime.json",
        "config/instructions.md",
        "config/skills/alpha",
        "config/skills",
        "creds/creds.json",
        "creds",
    ];
    for path in refused {
        let moved = root.0.join("moved");
        fs::rename(root.at(path), &moved).unwrap();
        symlink(other.at(path), root.at(path)).unwrap();
        assert_eq!(root.state().unwrap_err(), StateFault::NotValid, "{path}");
        fs::remove_file(root.at(path)).unwrap();
        fs::rename(&moved, root.at(path)).unwrap();
        assert!(root.state().is_ok(), "{path}");
    }
    fs::remove_dir_all(root.at("")).unwrap();
    symlink(other.at(""), root.0.join("code")).unwrap();
    let read = root.state();
    assert_eq!(read.unwrap_err(), StateFault::NotValid);
}

#[test]
fn a_swapped_config_dir_retries() {
    let root = Arc::new(Root::fixture("swapping"));
    // Two whole revisions: each has the same word in its instructions and
    // in each of its 200 skills, so a reply that mixes them shows. The 200
    // skills make each read take some ms, so swaps land inside reads. Such
    // a try sees the inode change and reads again. Without the retry, the
    // read is not_valid and the unwrap below fails.
    let write = |dir: &Path, word: &str| {
        fs::create_dir_all(dir).unwrap();
        fs::write(dir.join("runtime.json"), runtime(|_| {})).unwrap();
        fs::write(dir.join("instructions.md"), word).unwrap();
        for skill in 0..200 {
            let skill = dir.join(format!("skills/s{skill:03}"));
            fs::create_dir_all(&skill).unwrap();
            fs::write(skill.join("SKILL.md"), word).unwrap();
        }
    };
    fs::remove_dir_all(root.at("config")).unwrap();
    write(&root.at("config"), "one");
    let done = Arc::new(AtomicBool::new(false));
    let swapper = {
        let (root, done) = (Arc::clone(&root), Arc::clone(&done));
        thread::spawn(move || {
            // caregiver's folder swap (`atomic_replace_dir`), every 50 ms,
            // so the READ_TRIES tries of one read rarely all see a swap.
            for round in 0..12 {
                write(&root.at("config.tmp"), ["two", "one"][round % 2]);
                fs::rename(root.at("config"), root.at("config.old")).unwrap();
                fs::rename(root.at("config.tmp"), root.at("config")).unwrap();
                fs::remove_dir_all(root.at("config.old")).unwrap();
                thread::sleep(Duration::from_millis(50));
            }
            done.store(true, Ordering::SeqCst);
        })
    };
    let mut words = [0, 0];
    while !done.load(Ordering::SeqCst) {
        let read = root.state().unwrap();
        let config = reply(&read)["state"]["config"].clone();
        let word = config["instructions"].as_str().unwrap().to_owned();
        let skills = config["skills"].as_object().unwrap();
        assert_eq!(skills.len(), 200);
        assert!(skills.values().all(|text| *text == word.as_str()), "{word}");
        words[usize::from(word == "two")] += 1;
    }
    swapper.join().unwrap();
    assert!(words[0] > 0 && words[1] > 0, "{words:?}");
}

/// Writes `egress.json` of the family `code`.
#[cfg(test)]
fn write_egress(root: &Root, text: &str) -> PathBuf {
    fs::create_dir_all(root.0.join("code")).unwrap();
    let path = root.0.join("code/egress.json");
    fs::write(&path, text).unwrap();
    path
}

#[test]
fn read_egress_reads_the_allowlist() {
    let root = Root::new("egress");
    write_egress(&root, EGRESS);
    let read = root.families().read_egress(&code()).unwrap();
    assert_eq!(read, serde_json::from_str::<FamilyEgress>(EGRESS).unwrap());
}

/// An absent family dir and an absent file both allow no egress (decision
/// CH69 rule 3), with no wait.
#[test]
fn read_egress_reads_an_absent_file_as_no_egress() {
    let root = Root::new("egress-absent");
    let families = root.families();
    assert_eq!(families.read_egress(&code()).unwrap().hosts, []);
    fs::create_dir_all(root.0.join("code/config")).unwrap();
    assert_eq!(families.read_egress(&code()).unwrap().hosts, []);
}

#[test]
fn read_egress_refuses_a_symlink() {
    let root = Root::new("egress-symlink");
    let other = root.0.join("other.json");
    fs::write(&other, EGRESS).unwrap();
    fs::create_dir_all(root.0.join("code")).unwrap();
    symlink(&other, root.0.join("code/egress.json")).unwrap();
    let read = root.families().read_egress(&code());
    assert!(matches!(read, Err(ReachFault::NotFile)), "{read:?}");
    fs::remove_file(root.0.join("code/egress.json")).unwrap();
    fs::create_dir(root.0.join("code/egress.json")).unwrap();
    let read = root.families().read_egress(&code());
    assert!(matches!(read, Err(ReachFault::NotFile)), "{read:?}");
    // A symlinked family dir is refused too, not followed.
    fs::remove_dir_all(root.0.join("code")).unwrap();
    fs::create_dir_all(root.0.join("real")).unwrap();
    fs::write(root.0.join("real/egress.json"), EGRESS).unwrap();
    symlink(root.0.join("real"), root.0.join("code")).unwrap();
    let read = root.families().read_egress(&code());
    assert!(matches!(read, Err(ReachFault::NotDir)), "{read:?}");
}

/// The byte read takes the bytes of the file, so new bytes of the same
/// list compare unequal, and an absent file compares equal to another
/// absent file (decision CH69 rule 11). Bytes that do not read are bytes
/// still, and `list` refuses them.
#[test]
fn read_egress_bytes_compares_bytes_not_lists() {
    let root = Root::new("egress-bytes");
    let families = root.families();
    let absent = families.read_egress_bytes(&code()).unwrap();
    assert_eq!(absent.list().unwrap().hosts, []);
    let path = write_egress(&root, EGRESS);
    let first = families.read_egress_bytes(&code()).unwrap();
    assert_ne!(first, absent);
    fs::write(&path, format!("{EGRESS}\n")).unwrap();
    let second = families.read_egress_bytes(&code()).unwrap();
    assert_ne!(second, first);
    assert_eq!(second.list().unwrap(), first.list().unwrap());
    fs::write(&path, "not json").unwrap();
    let bad = families.read_egress_bytes(&code()).unwrap();
    assert!(matches!(bad.list(), Err(ReachFault::NotValid)));
    fs::write(&path, "still not json").unwrap();
    assert_ne!(families.read_egress_bytes(&code()).unwrap(), bad);
    fs::remove_file(&path).unwrap();
    assert_eq!(families.read_egress_bytes(&code()).unwrap(), absent);
}

/// A FIFO with no writer: only `O_NONBLOCK` keeps the open from waiting.
#[test]
fn read_egress_refuses_a_fifo_at_once() {
    let root = Root::new("egress-fifo");
    fs::create_dir_all(root.0.join("code")).unwrap();
    let made = Command::new("mkfifo")
        .arg(root.0.join("code/egress.json"))
        .status()
        .unwrap();
    assert!(made.success());
    let families = root.families();
    let (sent, got) = mpsc::channel();
    thread::spawn(move || sent.send(families.read_egress(&code())));
    let read = got.recv_timeout(Duration::from_secs(5)).unwrap();
    assert!(matches!(read, Err(ReachFault::NotFile)), "{read:?}");
}

#[test]
fn read_egress_refuses_an_unknown_key() {
    let root = Root::new("egress-key");
    let not_valid = [
        r#"{"hosts":[],"repos":[]}"#,
        r#"{"hosts":[{"host":"github.com","port":443,"scheme":"https"}]}"#,
        r#"{"hosts":[],"hosts":[]}"#,
        r#"{"hosts":[{"host":"GitHub.com","port":443}]}"#,
        r#"{"hosts":[{"host":"1.2.3.4","port":443}]}"#,
        r#"{"hosts":["github.com"]}"#,
        r#"[[]]"#,
        "",
    ];
    for text in not_valid {
        write_egress(&root, text);
        let read = root.families().read_egress(&code());
        assert!(
            matches!(read, Err(ReachFault::NotValid)),
            "{text}: {read:?}"
        );
    }
}

#[test]
fn read_egress_refuses_past_its_cap() {
    let root = Root::new("egress-cap");
    let cap = usize::try_from(EGRESS_CAP).unwrap();
    let at_cap = format!("{EGRESS}{}", " ".repeat(cap - EGRESS.len()));
    write_egress(&root, &at_cap);
    assert!(root.families().read_egress(&code()).is_ok());
    write_egress(&root, &format!("{at_cap} "));
    let read = root.families().read_egress(&code());
    assert!(matches!(read, Err(ReachFault::TooLarge)), "{read:?}");
}

/// caregiver refuses a family whose `egress.json` passes the cap, so the
/// two sides read one value from `vectors/family.json` of the wire crate
/// (decision CH71). The caregiver test holds its copy equal to the same
/// file.
#[test]
fn the_egress_cap_matches_the_wire_vectors() {
    assert_eq!(family_vectors()["egress_cap"], json!(EGRESS_CAP));
}

/// caregiver writes `repos.json` with no refusal of its own, because the
/// longest valid list fits the cap (decision CH73 rule 2). The two sides
/// read one value from `vectors/family.json` of the wire crate, as for
/// `egress_cap`. The caregiver test reads the same file.
#[test]
fn the_repos_cap_matches_the_wire_vectors() {
    assert_eq!(family_vectors()["repos_cap"], json!(REPOS_CAP));
}

/// `vectors/family.json` of the wire crate: the caps that the vmm and
/// caregiver share. The file holds no other key.
#[cfg(test)]
fn family_vectors() -> Value {
    let path = Path::new(env!("CARGO_MANIFEST_DIR")).join("../wire/vectors/family.json");
    let file: Value = serde_json::from_str(&fs::read_to_string(path).unwrap()).unwrap();
    let keys: Vec<&String> = file.as_object().unwrap().keys().collect();
    assert_eq!(keys, ["egress_cap", "repos_cap"]);
    file
}

/// Writes `repos.json` of the family `code`.
#[cfg(test)]
fn write_repos(root: &Root, text: &str) -> PathBuf {
    fs::create_dir_all(root.0.join("code")).unwrap();
    let path = root.0.join("code/repos.json");
    fs::write(&path, text).unwrap();
    path
}

#[test]
fn read_repos_reads_the_clone_list() {
    let root = Root::new("repos");
    write_repos(&root, REPOS);
    let read = root.families().read_repos(&code()).unwrap();
    assert_eq!(read, serde_json::from_str::<FamilyRepos>(REPOS).unwrap());
    let names: Vec<&str> = read
        .repos
        .names()
        .iter()
        .map(|name| name.as_str())
        .collect();
    assert_eq!(names, ["chaperone", "creche"]);
}

/// An absent family dir and an absent file both allow no clone (decisions
/// CH69 rules 4 and 13), with no wait.
#[test]
fn an_absent_repos_file_reads_as_empty() {
    let root = Root::new("repos-absent");
    let families = root.families();
    assert_eq!(
        families.read_repos(&code()).unwrap(),
        FamilyRepos::default()
    );
    fs::create_dir_all(root.0.join("code/config")).unwrap();
    assert!(
        families
            .read_repos(&code())
            .unwrap()
            .repos
            .names()
            .is_empty()
    );
}

/// The byte read of `repos.json` takes the bytes as the one of
/// `egress.json` does, with the cap `REPOS_CAP` (decision CH69 rule 11).
#[test]
fn read_repos_bytes_compares_bytes_not_lists() {
    let root = Root::new("repos-bytes");
    let families = root.families();
    let path = write_repos(&root, REPOS);
    let first = families.read_repos_bytes(&code()).unwrap();
    fs::write(&path, format!("{REPOS}\n")).unwrap();
    let second = families.read_repos_bytes(&code()).unwrap();
    assert_ne!(second, first);
    assert_eq!(second.list().unwrap(), first.list().unwrap());
    fs::write(&path, " ".repeat(usize::try_from(REPOS_CAP).unwrap() + 1)).unwrap();
    let read = families.read_repos_bytes(&code());
    assert!(matches!(read, Err(ReachFault::TooLarge)), "{read:?}");
}

/// A symlinked `repos.json`, a dir in its place and a symlinked family dir
/// are each refused, not followed.
#[test]
fn read_repos_refuses_a_symlink() {
    let root = Root::new("repos-symlink");
    let other = root.0.join("other.json");
    fs::write(&other, REPOS).unwrap();
    fs::create_dir_all(root.0.join("code")).unwrap();
    symlink(&other, root.0.join("code/repos.json")).unwrap();
    let read = root.families().read_repos(&code());
    assert!(matches!(read, Err(ReachFault::NotFile)), "{read:?}");
    fs::remove_file(root.0.join("code/repos.json")).unwrap();
    fs::create_dir(root.0.join("code/repos.json")).unwrap();
    let read = root.families().read_repos(&code());
    assert!(matches!(read, Err(ReachFault::NotFile)), "{read:?}");
    fs::remove_dir_all(root.0.join("code")).unwrap();
    fs::create_dir_all(root.0.join("real")).unwrap();
    fs::write(root.0.join("real/repos.json"), REPOS).unwrap();
    symlink(root.0.join("real"), root.0.join("code")).unwrap();
    let read = root.families().read_repos(&code());
    assert!(matches!(read, Err(ReachFault::NotDir)), "{read:?}");
}

/// The strict reader refuses a key given twice, and the type refuses a
/// name that is no repo name and a list past 32 (decisions CH65 rule 2,
/// CH69 rule 15).
#[test]
fn read_repos_refuses_a_bad_list() {
    let root = Root::new("repos-bad");
    let past: Vec<String> = (0..33).map(|index| format!("r{index}")).collect();
    let past = json!({ "repos": past }).to_string();
    let not_valid = [
        r#"{"repos":[],"repos":[]}"#,
        r#"{"repos":["owner/name"]}"#,
        r#"{"repos":["chaperone.git"]}"#,
        r#"{"repos":[".."]}"#,
        r#"{"repos":[],"hosts":[]}"#,
        past.as_str(),
        "",
    ];
    for text in not_valid {
        write_repos(&root, text);
        let read = root.families().read_repos(&code());
        assert!(
            matches!(read, Err(ReachFault::NotValid)),
            "{text}: {read:?}"
        );
    }
}

/// The longest list in its canonical form fits `REPOS_CAP`, and a file
/// one byte past the cap is refused.
#[test]
fn read_repos_refuses_past_its_cap() {
    let root = Root::new("repos-cap");
    let longest: Vec<String> = (0..32).map(|index| format!("{index:0>100}")).collect();
    let longest = json!({ "repos": longest }).to_string();
    assert_eq!(longest.len(), 3307);
    write_repos(&root, &longest);
    assert_eq!(
        root.families()
            .read_repos(&code())
            .unwrap()
            .repos
            .names()
            .len(),
        32
    );
    let cap = usize::try_from(REPOS_CAP).unwrap();
    let at_cap = format!("{REPOS}{}", " ".repeat(cap - REPOS.len()));
    write_repos(&root, &at_cap);
    assert!(root.families().read_repos(&code()).is_ok());
    write_repos(&root, &format!("{at_cap} "));
    let read = root.families().read_repos(&code());
    assert!(matches!(read, Err(ReachFault::TooLarge)), "{read:?}");
}
