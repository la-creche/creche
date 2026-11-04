//! The read of a family's `config/` and its VM shape in temp directories
//! (decisions CH5b, CH23 (3), CH33 rule 3, CH36 G6, CH43 rule 3).

use std::fs;
use std::os::unix::fs::symlink;
use std::path::PathBuf;
use std::time::Instant;

use creche_vmm_confine::family::{
    FamilyError, FamilyRoot, READ_TRIES, RETRY_PAUSE, SHAPE_CAP, ShapeFault,
};
use creche_vmm_store::layout::RootError;
use creche_wire::{FamilyName, FamilyShape};

const SHAPE: &str = r#"{"cpus":2,"flavor":"base","memory_mib":2048}"#;
const BIGGER: &str = r#"{"cpus":4,"flavor":"python","memory_mib":8192}"#;

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
