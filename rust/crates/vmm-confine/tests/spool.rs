//! The file side of the git platform spool (decision CH3) on a scratch
//! spool root in `/tmp`: the put file, its mode and its rename, the
//! removal of a put that failed, the get open and the start sweep.

use std::fs::{self, File};
use std::io::Read;
use std::os::unix::fs::{PermissionsExt, symlink};
use std::path::PathBuf;

use creche_vmm_confine::spool::{FETCH_DIR, FetchFault, PUSH_DIR, SpoolRoot};
use creche_vmm_store::layout::RootError;
use creche_wire::FamilyName;
use creche_wire::spool::{BUNDLE_CAP, BundleId};
use rustix::fs::Mode;

/// A scratch spool root with `push/` and `fetch/`. Its drop removes it.
struct Scratch(PathBuf);

impl Drop for Scratch {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

// `cfg(test)` lets the helpers unwrap (rust/clippy.toml).
#[cfg(test)]
impl Scratch {
    fn new(name: &str) -> Self {
        let dir = PathBuf::from(format!("/tmp/vmm-spool-{}-{name}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(dir.join(PUSH_DIR)).unwrap();
        fs::create_dir_all(dir.join(FETCH_DIR)).unwrap();
        Self(dir)
    }

    fn root(&self) -> SpoolRoot {
        SpoolRoot::new(self.0.clone()).unwrap()
    }

    /// The sorted names in `folder`.
    fn names(&self, folder: &str) -> Vec<String> {
        let mut names: Vec<String> = fs::read_dir(self.0.join(folder))
            .unwrap()
            .map(|entry| entry.unwrap().file_name().into_string().unwrap())
            .collect();
        names.sort();
        names
    }
}

#[cfg(test)]
fn code() -> FamilyName {
    "code".parse().unwrap()
}

#[test]
fn the_root_must_be_absolute_with_no_parent_part() {
    assert!(matches!(
        SpoolRoot::new(PathBuf::from("spool")),
        Err(RootError::NotAbsolute)
    ));
    assert!(matches!(
        SpoolRoot::new(PathBuf::from("/var/../spool")),
        Err(RootError::ParentComponent)
    ));
}

/// A put writes under the temporary name, and the commit renames it to
/// its id with the mode 0640, also under a umask that clears the group
/// bits, as the vmm unit sets.
#[test]
fn a_put_commits_to_its_id_with_mode_0640() {
    // The umask of the vmm unit (systemd/creche-vmm.service).
    rustix::process::umask(Mode::from_raw_mode(0o077));
    let scratch = Scratch::new("put");
    let mut file = scratch.root().create_push(&code()).unwrap();
    let names = scratch.names(PUSH_DIR);
    let [part] = names.as_slice() else {
        panic!("{names:?}");
    };
    let id: BundleId = part
        .strip_prefix('.')
        .and_then(|rest| rest.strip_suffix(".part"))
        .unwrap()
        .parse()
        .unwrap();
    assert_eq!(id.family(), &code());
    file.write(b"# v2 git bundle\n").unwrap();
    assert_eq!(file.commit().unwrap(), id);
    assert_eq!(scratch.names(PUSH_DIR), [id.to_string()]);
    let path = scratch.0.join(PUSH_DIR).join(id.as_str());
    assert_eq!(fs::read(&path).unwrap(), b"# v2 git bundle\n");
    let mode = fs::metadata(&path).unwrap().permissions().mode();
    assert_eq!(mode & 0o7777, 0o640);
}

/// Two puts get two ids.
#[test]
fn two_puts_get_two_ids() {
    let scratch = Scratch::new("two");
    let first = scratch.root().create_push(&code()).unwrap();
    let second = scratch.root().create_push(&code()).unwrap();
    assert_ne!(first.commit().unwrap(), second.commit().unwrap());
}

/// The drop of a put that did not commit removes its temporary file, so a
/// failed put leaves no file.
#[test]
fn a_put_that_does_not_commit_leaves_no_file() {
    let scratch = Scratch::new("drop");
    let mut file = scratch.root().create_push(&code()).unwrap();
    file.write(&[0; 4096]).unwrap();
    drop(file);
    assert!(scratch.names(PUSH_DIR).is_empty());
}

#[test]
fn a_put_without_its_folder_fails() {
    let scratch = Scratch::new("no-push");
    fs::remove_dir(scratch.0.join(PUSH_DIR)).unwrap();
    assert!(scratch.root().create_push(&code()).is_err());
    let link = scratch.0.join("elsewhere");
    fs::create_dir(&link).unwrap();
    symlink(&link, scratch.0.join(PUSH_DIR)).unwrap();
    assert!(scratch.root().create_push(&code()).is_err());
    assert!(fs::read_dir(&link).unwrap().next().is_none());
}

#[test]
fn remove_push_removes_the_file_and_a_gone_one_is_no_fault() {
    let scratch = Scratch::new("remove");
    let file = scratch.root().create_push(&code()).unwrap();
    let id = file.commit().unwrap();
    scratch.root().remove_push(&id).unwrap();
    assert!(scratch.names(PUSH_DIR).is_empty());
    scratch.root().remove_push(&id).unwrap();
}

/// A get opens a regular file of `fetch/` and gives its size.
#[test]
fn a_get_opens_a_bundle_of_fetch() {
    let scratch = Scratch::new("get");
    let id: BundleId = "code-01JC9Z6A0B1C2D3E4F5G6H7J8K.bundle".parse().unwrap();
    fs::write(scratch.0.join(FETCH_DIR).join(id.as_str()), b"bundle").unwrap();
    let mut fetched = scratch.root().open_fetch(&id).unwrap();
    assert_eq!(fetched.size, 6);
    let mut bytes = Vec::new();
    fetched.file.read_to_end(&mut bytes).unwrap();
    assert_eq!(bytes, b"bundle");
}

/// A missing file, a symlink and a dir are each not found, and a file
/// past the cap is too large.
#[test]
fn a_get_refuses_what_is_not_a_bundle() {
    let scratch = Scratch::new("get-refused");
    let fetch = scratch.0.join(FETCH_DIR);
    let id: BundleId = "code-01JC9Z6A0B1C2D3E4F5G6H7J8K.bundle".parse().unwrap();
    let root = scratch.root();
    assert!(matches!(root.open_fetch(&id), Err(FetchFault::NotFound)));

    fs::write(scratch.0.join("target"), b"bundle").unwrap();
    symlink(scratch.0.join("target"), fetch.join(id.as_str())).unwrap();
    assert!(matches!(root.open_fetch(&id), Err(FetchFault::NotFound)));
    fs::remove_file(fetch.join(id.as_str())).unwrap();

    fs::create_dir(fetch.join(id.as_str())).unwrap();
    assert!(matches!(root.open_fetch(&id), Err(FetchFault::NotFound)));
    fs::remove_dir(fetch.join(id.as_str())).unwrap();

    let file = File::create(fetch.join(id.as_str())).unwrap();
    file.set_len(BUNDLE_CAP + 1).unwrap();
    assert!(matches!(root.open_fetch(&id), Err(FetchFault::TooLarge)));
    file.set_len(BUNDLE_CAP).unwrap();
    assert_eq!(root.open_fetch(&id).unwrap().size, BUNDLE_CAP);

    fs::remove_dir_all(&fetch).unwrap();
    assert!(matches!(root.open_fetch(&id), Err(FetchFault::Io(_))));
}

/// The start sweep removes each bundle and each temporary file of
/// `push/`, and keeps every other name and each dir.
#[test]
fn the_sweep_removes_only_the_names_of_the_vmm() {
    let scratch = Scratch::new("sweep");
    let push = scratch.0.join(PUSH_DIR);
    let id = scratch
        .root()
        .create_push(&code())
        .unwrap()
        .commit()
        .unwrap();
    let part_name = ".code-01JC9Z6A0B1C2D3E4F5G6H7J8K.bundle.part".to_owned();
    fs::write(push.join(&part_name), b"").unwrap();
    fs::write(push.join("notes.txt"), b"").unwrap();
    fs::create_dir(push.join("chat-01JC9Z6A0B1C2D3E4F5G6H7J8K.bundle")).unwrap();
    let mut before = vec![
        id.to_string(),
        part_name,
        "notes.txt".to_owned(),
        "chat-01JC9Z6A0B1C2D3E4F5G6H7J8K.bundle".to_owned(),
    ];
    before.sort();
    assert_eq!(scratch.names(PUSH_DIR), before);
    assert_eq!(scratch.root().sweep_push().unwrap(), 2);
    assert_eq!(
        scratch.names(PUSH_DIR),
        ["chat-01JC9Z6A0B1C2D3E4F5G6H7J8K.bundle", "notes.txt"]
    );
}

#[test]
fn the_sweep_of_an_absent_folder_removes_nothing() {
    let scratch = Scratch::new("sweep-absent");
    fs::remove_dir(scratch.0.join(PUSH_DIR)).unwrap();
    assert_eq!(scratch.root().sweep_push().unwrap(), 0);
}
