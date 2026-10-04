//! A directory that one test fills, and that goes away with the value.

use std::fs::{self, DirBuilder};
use std::io;
use std::os::unix::fs::{DirBuilderExt, PermissionsExt};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};

/// The start of the name of each directory. It is short on purpose: the path
/// of a Unix socket has 104 bytes at most on macOS.
const NAME_START: &str = "ct";

/// The mode of the directory: only the owner enters. A test puts a token file
/// there.
const OWNER_ONLY: u32 = 0o700;

/// The count of the directories that this process made.
static MADE: AtomicU64 = AtomicU64::new(0);

/// A directory under the temporary directory of the host, for one test.
///
/// The name holds the id of the process and a count, so two tests of one
/// process and two test processes never share a directory. The value removes
/// the directory and each file in it when it drops. It also removes a
/// directory that the test left with no mode bit for its owner.
///
/// ```
/// use creche_testkit::root::TempRoot;
///
/// let root = TempRoot::new()?;
/// let file = root.path().join("token");
/// std::fs::write(&file, b"0123456789abcdef")?;
/// assert!(file.is_file());
///
/// let path = root.path().to_owned();
/// drop(root);
/// assert!(!path.exists());
/// # Ok::<(), std::io::Error>(())
/// ```
///
/// Code outside this module cannot build a value from a path. A value that
/// names another directory removes that directory when it drops:
///
/// ```compile_fail,E0451
/// use creche_testkit::root::TempRoot;
///
/// let root = TempRoot {
///     path: std::path::PathBuf::from("/"),
/// };
/// ```
#[derive(Debug)]
pub struct TempRoot {
    path: PathBuf,
}

impl TempRoot {
    /// Makes a new, empty directory with mode `0700`.
    ///
    /// # Errors
    ///
    /// The error of the operating system when it cannot make the directory.
    pub fn new() -> io::Result<Self> {
        let count = MADE.fetch_add(1, Ordering::Relaxed);
        let name = format!("{NAME_START}-{}-{count}", std::process::id());

        Self::at(std::env::temp_dir().join(name))
    }

    /// Makes the directory `path`, which this process owns by its name.
    ///
    /// A directory with that name is a leftover: a process with the same id
    /// made it, and something killed that process before the drop. The
    /// function removes the leftover tree and makes the directory one more
    /// time.
    fn at(path: PathBuf) -> io::Result<Self> {
        let mut builder = DirBuilder::new();
        builder.mode(OWNER_ONLY);

        match builder.create(&path) {
            Ok(()) => {}
            Err(error) if error.kind() == io::ErrorKind::AlreadyExists => {
                remove_tree(&path)?;
                builder.create(&path)?;
            }
            Err(error) => return Err(error),
        }

        Ok(Self { path })
    }

    /// The path of the directory.
    #[must_use]
    pub fn path(&self) -> &Path {
        &self.path
    }
}

impl Drop for TempRoot {
    fn drop(&mut self) {
        // A test that removed the directory itself leaves nothing to remove,
        // and a drop has no caller to give an error to.
        let _ = remove_tree(&self.path);
    }
}

/// Removes the tree at `path`.
///
/// The first try fails on a directory that its owner cannot list or cannot
/// write. A test that sets such a mode and fails before it restores the mode
/// leaves that tree. The function then opens each directory of the tree to
/// its owner and tries one more time.
fn remove_tree(path: &Path) -> io::Result<()> {
    if fs::remove_dir_all(path).is_ok() {
        return Ok(());
    }

    open_each_dir(path);

    fs::remove_dir_all(path)
}

/// Sets the mode `0700` on the directory `root` and on each directory below
/// it.
///
/// The function follows no symlink, so it changes no mode outside the tree.
/// It skips a step that fails: the remove that follows gives the error.
fn open_each_dir(root: &Path) {
    let mut pending = vec![root.to_owned()];

    while let Some(dir) = pending.pop() {
        // `symlink_metadata` reads a symlink itself and not its target.
        if !fs::symlink_metadata(&dir).is_ok_and(|metadata| metadata.is_dir()) {
            continue;
        }

        let _ = fs::set_permissions(&dir, fs::Permissions::from_mode(OWNER_ONLY));

        let Ok(entries) = fs::read_dir(&dir) else {
            continue;
        };

        pending.extend(entries.flatten().map(|entry| entry.path()));
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The longest name of a directory, in bytes: the start, an id of a
    /// process of 10 digits and a count of 6 digits. A socket path of 104
    /// bytes then has room for the temporary directory of the host and for
    /// the name of the socket.
    const NAME_MAX: usize = 20;

    #[test]
    fn a_new_root_is_an_empty_directory_that_only_the_owner_enters() {
        let root = TempRoot::new().unwrap();
        let metadata = fs::metadata(root.path()).unwrap();

        assert!(metadata.is_dir());
        assert_eq!(metadata.permissions().mode() & 0o7777, 0o700);
        assert_eq!(fs::read_dir(root.path()).unwrap().count(), 0);
    }

    #[test]
    fn two_roots_are_two_directories() {
        let first = TempRoot::new().unwrap();
        let second = TempRoot::new().unwrap();

        assert_ne!(first.path(), second.path());
        assert!(first.path().is_dir());
        assert!(second.path().is_dir());
    }

    #[test]
    fn the_name_holds_the_id_of_the_process() {
        let root = TempRoot::new().unwrap();
        let name = root.path().file_name().unwrap().to_str().unwrap();

        assert!(
            name.starts_with(&format!("{NAME_START}-{}-", std::process::id())),
            "{name}"
        );
        assert_eq!(root.path().parent().unwrap(), std::env::temp_dir());
    }

    #[test]
    fn the_drop_removes_the_tree() {
        let root = TempRoot::new().unwrap();
        let path = root.path().to_owned();
        fs::create_dir(path.join("faults")).unwrap();
        fs::write(path.join("faults").join("chat.json"), b"{}").unwrap();
        drop(root);

        assert!(!path.exists());
    }

    #[test]
    fn the_drop_of_a_root_that_the_test_removed_does_nothing() {
        let root = TempRoot::new().unwrap();
        fs::remove_dir_all(root.path()).unwrap();
        drop(root);
    }

    #[test]
    fn a_leftover_tree_with_the_same_name_is_removed_first() {
        let outer = TempRoot::new().unwrap();
        let path = outer.path().join("leftover");
        fs::create_dir(&path).unwrap();
        fs::write(path.join("old.json"), b"{}").unwrap();
        fs::set_permissions(&path, fs::Permissions::from_mode(0o755)).unwrap();

        let root = TempRoot::at(path.clone()).unwrap();

        assert_eq!(root.path(), path);
        assert_eq!(fs::read_dir(&path).unwrap().count(), 0);
        assert_eq!(
            fs::metadata(&path).unwrap().permissions().mode() & 0o7777,
            0o700
        );
    }

    /// Makes a directory at `path` that holds one file and one directory,
    /// and then takes each bit of its mode away. The owner then cannot list
    /// it and cannot remove what it holds.
    fn closed_dir(path: &Path) {
        fs::create_dir_all(path.join("inner")).unwrap();
        fs::write(path.join("old.json"), b"{}").unwrap();
        fs::set_permissions(path, fs::Permissions::from_mode(0o000)).unwrap();
    }

    #[test]
    fn a_leftover_tree_with_a_closed_directory_is_removed_first() {
        let outer = TempRoot::new().unwrap();
        let path = outer.path().join("leftover");
        closed_dir(&path.join("closed"));

        let root = TempRoot::at(path.clone()).unwrap();

        assert_eq!(root.path(), path);
        assert_eq!(fs::read_dir(&path).unwrap().count(), 0);
    }

    #[test]
    fn a_leftover_root_that_is_closed_itself_is_removed_first() {
        let outer = TempRoot::new().unwrap();
        let path = outer.path().join("leftover");
        closed_dir(&path);

        let root = TempRoot::at(path.clone()).unwrap();

        assert_eq!(fs::read_dir(root.path()).unwrap().count(), 0);
    }

    #[test]
    fn the_drop_removes_a_tree_with_a_closed_directory() {
        let root = TempRoot::new().unwrap();
        let path = root.path().to_owned();
        closed_dir(&path.join("faults").join("closed"));
        drop(root);

        assert!(!path.exists());
    }

    #[test]
    fn the_second_try_changes_no_target_of_a_symlink() {
        let outside = TempRoot::new().unwrap();
        let target = outside.path().join("target");
        fs::create_dir(&target).unwrap();
        fs::write(target.join("kept.json"), b"{}").unwrap();
        fs::set_permissions(&target, fs::Permissions::from_mode(0o500)).unwrap();

        let root = TempRoot::new().unwrap();
        let path = root.path().to_owned();
        std::os::unix::fs::symlink(&target, path.join("link")).unwrap();
        closed_dir(&path.join("closed"));
        drop(root);

        assert!(!path.exists());
        assert!(target.join("kept.json").is_file());
        assert_eq!(
            fs::metadata(&target).unwrap().permissions().mode() & 0o7777,
            0o500
        );
    }

    #[test]
    fn a_leftover_file_with_the_same_name_is_an_error() {
        let outer = TempRoot::new().unwrap();
        let path = outer.path().join("leftover");
        fs::write(&path, b"{}").unwrap();

        assert!(TempRoot::at(path.clone()).is_err());
        assert!(path.is_file());
    }

    #[test]
    fn a_root_that_the_system_cannot_make_is_an_error() {
        let outer = TempRoot::new().unwrap();
        let path = outer.path().join("absent").join("root");

        assert_eq!(
            TempRoot::at(path).unwrap_err().kind(),
            io::ErrorKind::NotFound
        );
    }

    #[test]
    fn the_name_is_short_so_a_socket_path_fits_under_it() {
        let root = TempRoot::new().unwrap();
        let name = root.path().file_name().unwrap();

        assert!(name.len() <= NAME_MAX, "{} bytes", name.len());
    }
}
