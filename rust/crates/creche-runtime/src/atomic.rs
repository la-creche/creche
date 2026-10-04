//! A file write that a reader never sees half done.
//!
//! [`write()`] writes the bytes to a temporary file beside the target, syncs
//! that file and renames it onto the target. A reader gets the old file or
//! the new file. A process that stops in the middle leaves the old file.
//!
//! No function here is `async`. A write blocks, and a write that started must
//! end. Run it with `Tasks::spawn_blocking` and await the `Completion`
//! (`rust/AGENTS.md`, "The rules for a service", rule 2 and rule 7).
//!
//! The Python services hold three copies of this write:
//! `attendance/src/attendance/atomic.py`, `caregiver/src/caregiver/atomic.py`
//! and `chaperone/src/chaperone/faults.py`. The three differ in the name of
//! the temporary file and in what they sync. [`Write`] names each choice.
//! The trigger door and `handover` hold more copies, and the doc comment of
//! each function names them. The table `DEVIATIONS` in the tests of this
//! module names each difference from a Python copy.
//!
//! # The temporary file
//!
//! The name of the temporary file is `.<name>.<pid>.<count>.tmp`, in the
//! directory of the target. `<name>` is the file name of the target, `<pid>`
//! is the id of the process, and `<count>` is a count that the process keeps.
//!
//! - Two writes of one process never share a name. Two live processes of one
//!   host never share one, because they have two ids.
//! - The name starts with a dot and ends in `.tmp`. A reader that lists
//!   `*.json` in the directory does not take the file.
//! - A process that something kills in the middle of a write leaves its
//!   temporary file. A later process with the same id can make the same
//!   name. No live process owns that file, so the write removes it and
//!   creates its own file.

// CONTRACT-QUESTION: contract 04 §1.3 step 2 names the temporary file of a
// grant file `<family>.json.tmp`. Contract 05 §2 rule 2 and §3.3.1 rule 3
// give the temporary file no name. The Python copies use five forms of the
// name, and the Python writer of the grant file
// (`caregiver/src/caregiver/atomic.py:30`) does not use the name of contract
// 04. The reading here is the form of `attendance/src/attendance/atomic.py:45`
// for each write: two writers of one target then never write one temporary
// file. No reader opens a temporary file by its name. A change to the name
// of contract 04 costs one function, `temp_name`.

use std::error::Error;
use std::ffi::{OsStr, OsString};
use std::fmt;
use std::fs::{self, File, OpenOptions, Permissions};
use std::io::{self, Write as _};
use std::os::unix::fs::{OpenOptionsExt, PermissionsExt};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};

use crate::readfile::os_text;

/// The end of the name of a temporary file.
const TEMP_SUFFIX: &str = ".tmp";

/// The end of the name of the old tree that [`replace_dir`] moves away.
const OLD_SUFFIX: &str = ".old";

/// The directory of a path that has one part only, for example `status.json`.
const THIS_DIR: &str = ".";

/// What an error says for a target that has no file name, for example `/`.
const NO_FILE_NAME: &str = "the path has no file name";

/// The count of the temporary files that this process named.
static NAMED: AtomicU64 = AtomicU64::new(0);

/// The mode of a file that [`write()`] or [`write_new`] makes.
///
/// The set is closed. A caller cannot ask for a mode that the group or each
/// user can write.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum FileMode {
    /// `0600`: only the owner reads and writes. A token file has this mode.
    Private,
    /// `0640`: the group also reads. A fault file has this mode.
    GroupRead,
    /// `0644`: each user reads.
    PublicRead,
}

impl FileMode {
    /// The permission bits of the mode. The Python names are the constants of
    /// `attendance/src/attendance/atomic.py:22-24`.
    const fn bits(self) -> u32 {
        match self {
            Self::Private => 0o600,
            Self::GroupRead => 0o640,
            Self::PublicRead => 0o644,
        }
    }
}

/// The mode of a directory that [`ensure_dir`] makes.
///
/// The set is closed.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DirMode {
    /// `0700`: only the owner enters.
    Private,
    /// `0750`: the group also reads and enters.
    Group,
    /// `2750`: as [`DirMode::Group`], and a new entry gets the group of the
    /// directory. The directory of a shared socket has this mode.
    SetgidGroup,
}

impl DirMode {
    /// The permission bits of the mode. The Python names are the constants of
    /// `attendance/src/attendance/atomic.py:25-26`. The third mode is the
    /// mode that `attendance/src/attendance/__main__.py:42` demands for the
    /// directory of its socket.
    const fn bits(self) -> u32 {
        match self {
            Self::Private => 0o700,
            Self::Group => 0o750,
            Self::SetgidGroup => 0o2750,
        }
    }
}

/// What [`write()`] does when the directory of the target is absent.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Parents {
    /// Make each directory that is absent.
    Create,
    /// Refuse the write. The caller made the directory, with its own mode.
    MustExist,
}

/// Whether [`write()`] syncs the directory after the rename.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DirSync {
    /// Sync the directory. After a loss of power the new name is then on the
    /// disk. This is the strictest level, and the default of a port.
    Sync,
    /// Do not sync the directory. Use it only for a file that the service
    /// makes again at its next start.
    Skip,
}

/// How [`write()`] writes one file.
///
/// Each field is a decision that a Python copy makes without a name. A port
/// of a service states each one.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Write {
    /// The mode of the new file.
    pub mode: FileMode,
    /// What to do when the directory is absent.
    pub parents: Parents,
    /// Whether to sync the directory after the rename.
    pub dir_sync: DirSync,
}

/// The step of a write that failed.
///
/// The set is closed. It does not cross a process boundary.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum WriteStep {
    /// Make the directories above the target.
    MakeParents,
    /// Create the temporary file.
    CreateTemp,
    /// Write the bytes into the temporary file.
    WriteTemp,
    /// Sync the temporary file.
    SyncTemp,
    /// Set the mode of a file or of a directory.
    SetMode,
    /// Rename a file or a directory onto its target.
    Rename,
    /// Link the temporary file to a name that must not exist.
    Link,
    /// Sync the directory of the target.
    SyncDir,
    /// Remove an old copy, a leftover temporary file, or the temporary file
    /// after the link.
    RemoveOld,
}

impl fmt::Display for WriteStep {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Self::MakeParents => "make the directories above it",
            Self::CreateTemp => "create the temporary file",
            Self::WriteTemp => "write the temporary file",
            Self::SyncTemp => "sync the temporary file",
            Self::SetMode => "set the mode",
            Self::Rename => "rename onto the target",
            Self::Link => "link the new name",
            Self::SyncDir => "sync the directory",
            Self::RemoveOld => "remove the old copy",
        })
    }
}

/// Why a write did not complete.
///
/// The error names the step, the path and the answer of the operating system.
/// It never holds a byte of the file.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct WriteError {
    /// The step that failed.
    pub step: WriteStep,
    /// The path that the step was about.
    pub path: PathBuf,
    /// The kind of the error of the operating system.
    pub kind: io::ErrorKind,
    /// The text of the error, as `strerror` of Python gives it.
    pub os_text: String,
}

impl fmt::Display for WriteError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            f,
            "cannot write {}: the step \"{}\" failed: {}",
            self.path.display(),
            self.step,
            self.os_text
        )
    }
}

impl Error for WriteError {}

/// Writes `bytes` to `path` through a temporary file, a sync and a rename.
///
/// A reader of `path` gets the old content or the new content, and never a
/// part of one.
///
/// The steps, in this order:
///
/// 1. With [`Parents::Create`], make each directory above `path` that is
///    absent.
/// 2. Create the temporary file with the mode. The create refuses a name
///    that exists. For such a name, remove the leftover file and create the
///    file one more time.
/// 3. Write the bytes.
/// 4. Sync the temporary file.
/// 5. Set the mode again. The umask of the process can narrow the mode of
///    step 2.
/// 6. Rename the temporary file onto `path`.
/// 7. With [`DirSync::Sync`], sync the directory.
///
/// When a step from 2 to 6 fails, the function removes the temporary file,
/// and `path` keeps its old content. When step 7 fails, `path` has the new
/// content. After a loss of power, `path` can then have the old content.
///
/// The Python origins:
///
/// - `attendance/src/attendance/atomic.py:42-61`.
/// - `caregiver/src/caregiver/atomic.py:26-44`.
/// - `chaperone/src/chaperone/faults.py:68-89`.
/// - `door-trigger/src/agent_door_trigger/quiet/state.py:68-81`.
/// - `handover/src/handover/follow/__init__.py:308-325`.
/// - `handover/src/handover/executor/roster.py:134-142`.
///
/// ```
/// use creche_runtime::atomic::{self, DirSync, FileMode, Parents, Write};
/// use creche_testkit::root::TempRoot;
///
/// let root = TempRoot::new()?;
/// let path = root.path().join("faults/pep/chat.json");
/// let how = Write {
///     mode: FileMode::GroupRead,
///     parents: Parents::Create,
///     dir_sync: DirSync::Sync,
/// };
///
/// atomic::write(&path, b"{}\n", how)?;
/// assert_eq!(std::fs::read(&path)?, b"{}\n");
/// # Ok::<(), Box<dyn std::error::Error>>(())
/// ```
///
/// # Errors
///
/// [`WriteError`] with the step that failed.
pub fn write(path: &Path, bytes: &[u8], how: Write) -> Result<(), WriteError> {
    write_with(&Host, path, bytes, how)
}

/// Writes `bytes` to `path` only when no file has that name.
///
/// The function never replaces a file. Two writers of one name cannot both
/// succeed. The directory must exist.
///
/// The steps, in this order:
///
/// 1. Make the temporary file with the bytes and the mode, as steps 2 to 5
///    of [`write()`] do.
/// 2. Link the temporary file to `path`. A link refuses a name that exists.
///    A rename replaces such a name.
/// 3. Remove the temporary file.
///
/// The function does not sync the directory, as the Python copies do not.
/// After a loss of power, `path` can be absent.
///
/// The Python origins are `handover/src/handover/executor/spool.py:358-385`
/// and `handover/src/handover/requester/file.py:188-210`. The two open the
/// file relative to a directory descriptor. This function takes a path.
///
/// ```
/// use std::io::ErrorKind;
///
/// use creche_runtime::atomic::{self, FileMode, WriteStep};
/// use creche_testkit::root::TempRoot;
///
/// let root = TempRoot::new()?;
/// let path = root.path().join("request.json");
///
/// atomic::write_new(&path, b"first", FileMode::GroupRead)?;
/// let Err(error) = atomic::write_new(&path, b"second", FileMode::GroupRead) else {
///     return Err("the second writer replaced the file".into());
/// };
///
/// assert_eq!(error.step, WriteStep::Link);
/// assert_eq!(error.kind, ErrorKind::AlreadyExists);
/// assert_eq!(std::fs::read(&path)?, b"first");
/// # Ok::<(), Box<dyn std::error::Error>>(())
/// ```
///
/// # Errors
///
/// [`WriteError`] with the step that failed. A name that exists fails at
/// [`WriteStep::Link`], with the kind `AlreadyExists`. A failure at
/// [`WriteStep::RemoveOld`] comes after the link: `path` then has the bytes.
pub fn write_new(path: &Path, bytes: &[u8], mode: FileMode) -> Result<(), WriteError> {
    write_new_with(&Host, path, bytes, mode)
}

/// Puts the directory `staging` in the place of the directory `target`.
///
/// The caller fills `staging` first. A reader then finds the old tree or the
/// new tree at `target`. No reader finds a tree with a part of its files.
///
/// The kernel renames a directory only onto a name that is absent or onto
/// an empty directory. The function thus makes two renames, and `target` is
/// absent between the two.
///
/// The steps, in this order:
///
/// 1. Make each directory above `target` that is absent.
/// 2. When `target` exists, remove the directory `<target>.old` that an
///    earlier call left. Then rename `target` to `<target>.old`.
/// 3. Rename `staging` to `target`.
/// 4. Sync the directory of `target`.
/// 5. Remove `<target>.old`. The function drops an error of this step.
///
/// When step 3 fails, the old tree stays at `<target>.old`, and `target` is
/// absent. The next call that succeeds removes that old tree.
///
/// The Python origin is `caregiver/src/caregiver/atomic.py:47-69`.
///
/// # Errors
///
/// [`WriteError`] with the step that failed.
pub fn replace_dir(staging: &Path, target: &Path) -> Result<(), WriteError> {
    replace_dir_with(&Host, staging, target)
}

/// Makes the directory `path` with `mode`. A directory that exists gets the
/// mode.
///
/// The function makes each directory above `path` that is absent. Such a
/// directory gets the mode that the umask of the process gives, and only
/// `path` gets `mode`.
///
/// The Python origins are `attendance/src/attendance/atomic.py:114-117` and
/// `chaperone/src/chaperone/faults.py:105-106`.
///
/// # Errors
///
/// [`WriteError`] with the step that failed.
pub fn ensure_dir(path: &Path, mode: DirMode) -> Result<(), WriteError> {
    ensure_dir_with(&Host, path, mode)
}

/// The calls of the operating system that this module makes, one for each
/// step of a write.
///
/// [`Host`] makes each call with the standard library. A test gives a value
/// that fails at one named [`WriteStep`], so a test can show what a write
/// leaves behind when the disk is full.
trait Steps {
    /// The count that the next temporary name holds.
    fn next_count(&self) -> u64;

    /// [`WriteStep::MakeParents`]: makes `dir` and each directory above it.
    fn make_dirs(&self, dir: &Path) -> io::Result<()>;

    /// [`WriteStep::CreateTemp`]: creates the file `temp`, and refuses a
    /// name that exists.
    fn create_temp(&self, temp: &Path, mode: FileMode) -> io::Result<File>;

    /// [`WriteStep::WriteTemp`]: writes each byte.
    fn write_temp(&self, file: &mut File, bytes: &[u8]) -> io::Result<()>;

    /// [`WriteStep::SyncTemp`]: syncs the content and the facts of the file.
    fn sync_temp(&self, file: &File) -> io::Result<()>;

    /// [`WriteStep::SetMode`]: sets the mode of the open file.
    fn set_file_mode(&self, file: &File, mode: FileMode) -> io::Result<()>;

    /// [`WriteStep::SetMode`]: sets the mode of the directory `dir`.
    fn set_dir_mode(&self, dir: &Path, mode: DirMode) -> io::Result<()>;

    /// [`WriteStep::Rename`]: renames `from` onto `to`.
    fn rename(&self, from: &Path, to: &Path) -> io::Result<()>;

    /// [`WriteStep::Link`]: gives the file `from` the second name `to`, and
    /// refuses a name that exists.
    fn link(&self, from: &Path, to: &Path) -> io::Result<()>;

    /// [`WriteStep::SyncDir`]: syncs the directory `dir`.
    fn sync_dir(&self, dir: &Path) -> io::Result<()>;

    /// [`WriteStep::RemoveOld`]: removes one file. It removes a symlink and
    /// not the file that the symlink names.
    fn remove_file(&self, path: &Path) -> io::Result<()>;

    /// [`WriteStep::RemoveOld`]: removes one directory and each entry in it.
    fn remove_tree(&self, path: &Path) -> io::Result<()>;
}

/// The [`Steps`] of the host: each call is a call of the standard library.
struct Host;

impl Steps for Host {
    fn next_count(&self) -> u64 {
        NAMED.fetch_add(1, Ordering::Relaxed)
    }

    fn make_dirs(&self, dir: &Path) -> io::Result<()> {
        fs::create_dir_all(dir)
    }

    fn create_temp(&self, temp: &Path, mode: FileMode) -> io::Result<File> {
        open_temp(temp, mode.bits())
    }

    fn write_temp(&self, file: &mut File, bytes: &[u8]) -> io::Result<()> {
        file.write_all(bytes)
    }

    fn sync_temp(&self, file: &File) -> io::Result<()> {
        file.sync_all()
    }

    fn set_file_mode(&self, file: &File, mode: FileMode) -> io::Result<()> {
        file.set_permissions(Permissions::from_mode(mode.bits()))
    }

    fn set_dir_mode(&self, dir: &Path, mode: DirMode) -> io::Result<()> {
        fs::set_permissions(dir, Permissions::from_mode(mode.bits()))
    }

    fn rename(&self, from: &Path, to: &Path) -> io::Result<()> {
        fs::rename(from, to)
    }

    fn link(&self, from: &Path, to: &Path) -> io::Result<()> {
        fs::hard_link(from, to)
    }

    fn sync_dir(&self, dir: &Path) -> io::Result<()> {
        File::open(dir)?.sync_all()
    }

    fn remove_file(&self, path: &Path) -> io::Result<()> {
        fs::remove_file(path)
    }

    fn remove_tree(&self, path: &Path) -> io::Result<()> {
        fs::remove_dir_all(path)
    }
}

/// Opens the new file `temp` for a write, with the permission bits `bits`.
///
/// This function is the one open of a temporary file. The open refuses a
/// name that exists. It does not follow a symlink at that name, and it never
/// writes into a file that another writer made.
fn open_temp(temp: &Path, bits: u32) -> io::Result<File> {
    OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(bits)
        .open(temp)
}

/// The error of one step.
fn failed(step: WriteStep, path: &Path, error: &io::Error) -> WriteError {
    WriteError {
        step,
        path: path.to_owned(),
        kind: error.kind(),
        os_text: os_text(error),
    }
}

/// The error for a target that has no file name. Python raises `ValueError`
/// for such a path (`Path.with_name`), so the write refuses it.
fn no_file_name(step: WriteStep, path: &Path) -> WriteError {
    WriteError {
        step,
        path: path.to_owned(),
        kind: io::ErrorKind::InvalidInput,
        os_text: String::from(NO_FILE_NAME),
    }
}

/// The directory that holds `path`. A path of one part is in the working
/// directory, as `Path.parent` of Python says.
fn dir_of(path: &Path) -> &Path {
    match path.parent() {
        Some(parent) if !parent.as_os_str().is_empty() => parent,
        _ => Path::new(THIS_DIR),
    }
}

/// The name of the temporary file for a target with the file name `name`:
/// `.<name>.<pid>.<count>.tmp`.
///
/// The Python origin is `attendance/src/attendance/atomic.py:45`.
fn temp_name(name: &OsStr, pid: u32, count: u64) -> OsString {
    let mut temp = OsString::from(".");
    temp.push(name);
    temp.push(format!(".{pid}.{count}{TEMP_SUFFIX}"));

    temp
}

/// The path of the temporary file for the target `path`, beside the target.
fn temp_path(steps: &impl Steps, path: &Path) -> Result<PathBuf, WriteError> {
    let Some(name) = path.file_name() else {
        return Err(no_file_name(WriteStep::CreateTemp, path));
    };

    Ok(path.with_file_name(temp_name(name, std::process::id(), steps.next_count())))
}

/// Creates the temporary file. For a name that exists, the function removes
/// the leftover file and creates the file one more time.
///
/// A killed process with the same id left that file, and no live process
/// owns it. The create never writes into a file that exists, and it never
/// follows a symlink at that name.
fn create_temp(steps: &impl Steps, temp: &Path, mode: FileMode) -> Result<File, WriteError> {
    match steps.create_temp(temp, mode) {
        Ok(file) => return Ok(file),
        Err(error) if error.kind() == io::ErrorKind::AlreadyExists => {}
        Err(error) => return Err(failed(WriteStep::CreateTemp, temp, &error)),
    }

    match steps.remove_file(temp) {
        Ok(()) => {}
        Err(error) if error.kind() == io::ErrorKind::NotFound => {}
        Err(error) => return Err(failed(WriteStep::RemoveOld, temp, &error)),
    }

    steps
        .create_temp(temp, mode)
        .map_err(|error| failed(WriteStep::CreateTemp, temp, &error))
}

/// Writes the bytes into the open temporary file, syncs it and sets its
/// mode.
fn fill(
    steps: &impl Steps,
    file: &mut File,
    temp: &Path,
    bytes: &[u8],
    mode: FileMode,
) -> Result<(), WriteError> {
    steps
        .write_temp(file, bytes)
        .map_err(|error| failed(WriteStep::WriteTemp, temp, &error))?;
    steps
        .sync_temp(file)
        .map_err(|error| failed(WriteStep::SyncTemp, temp, &error))?;

    // The mode goes onto the open file and not onto the path. No other
    // process can then put another file at the path before the call.
    steps
        .set_file_mode(file, mode)
        .map_err(|error| failed(WriteStep::SetMode, temp, &error))
}

/// Makes the temporary file `temp` with the bytes and the mode, synced and
/// closed. When a step fails, no temporary file stays.
fn stage(steps: &impl Steps, temp: &Path, bytes: &[u8], mode: FileMode) -> Result<(), WriteError> {
    let mut file = create_temp(steps, temp, mode)?;
    let filled = fill(steps, &mut file, temp, bytes, mode);
    drop(file);

    if filled.is_err() {
        discard(temp);
    }

    filled
}

/// Removes the temporary file of a write that failed. The write already has
/// its error, so the function drops an error of the remove.
fn discard(temp: &Path) {
    let _ = fs::remove_file(temp);
}

/// [`write()`] on the given [`Steps`].
fn write_with(steps: &impl Steps, path: &Path, bytes: &[u8], how: Write) -> Result<(), WriteError> {
    let temp = temp_path(steps, path)?;
    let dir = dir_of(path);

    if how.parents == Parents::Create {
        steps
            .make_dirs(dir)
            .map_err(|error| failed(WriteStep::MakeParents, dir, &error))?;
    }

    stage(steps, &temp, bytes, how.mode)?;

    if let Err(error) = steps.rename(&temp, path) {
        discard(&temp);

        return Err(failed(WriteStep::Rename, path, &error));
    }

    match how.dir_sync {
        DirSync::Sync => steps
            .sync_dir(dir)
            .map_err(|error| failed(WriteStep::SyncDir, dir, &error)),
        DirSync::Skip => Ok(()),
    }
}

/// [`write_new`] on the given [`Steps`].
fn write_new_with(
    steps: &impl Steps,
    path: &Path,
    bytes: &[u8],
    mode: FileMode,
) -> Result<(), WriteError> {
    let temp = temp_path(steps, path)?;

    stage(steps, &temp, bytes, mode)?;

    if let Err(error) = steps.link(&temp, path) {
        discard(&temp);

        return Err(failed(WriteStep::Link, path, &error));
    }

    match steps.remove_file(&temp) {
        Ok(()) => Ok(()),
        Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(()),
        Err(error) => Err(failed(WriteStep::RemoveOld, &temp, &error)),
    }
}

/// Whether `path` is a directory and not a symlink to one.
fn is_real_dir(path: &Path) -> bool {
    fs::symlink_metadata(path).is_ok_and(|metadata| metadata.is_dir())
}

/// [`replace_dir`] on the given [`Steps`].
fn replace_dir_with(steps: &impl Steps, staging: &Path, target: &Path) -> Result<(), WriteError> {
    let Some(name) = target.file_name() else {
        return Err(no_file_name(WriteStep::Rename, target));
    };
    let mut old_name = name.to_owned();
    old_name.push(OLD_SUFFIX);
    let displaced = target.with_file_name(old_name);
    let dir = dir_of(target);

    steps
        .make_dirs(dir)
        .map_err(|error| failed(WriteStep::MakeParents, dir, &error))?;

    // `exists` follows a symlink, as `Path.exists` of Python does.
    if target.exists() {
        // A call that stopped after its first rename left this tree. Only a
        // directory is removed. The rename refuses each other entry.
        if is_real_dir(&displaced) {
            steps
                .remove_tree(&displaced)
                .map_err(|error| failed(WriteStep::RemoveOld, &displaced, &error))?;
        }

        steps
            .rename(target, &displaced)
            .map_err(|error| failed(WriteStep::Rename, &displaced, &error))?;
    }

    steps
        .rename(staging, target)
        .map_err(|error| failed(WriteStep::Rename, target, &error))?;
    steps
        .sync_dir(dir)
        .map_err(|error| failed(WriteStep::SyncDir, dir, &error))?;

    // The new tree is in its place. An old tree that stays costs disk space
    // only, and the next call removes it.
    if is_real_dir(&displaced) {
        let _ = steps.remove_tree(&displaced);
    }

    Ok(())
}

/// [`ensure_dir`] on the given [`Steps`].
fn ensure_dir_with(steps: &impl Steps, path: &Path, mode: DirMode) -> Result<(), WriteError> {
    steps
        .make_dirs(path)
        .map_err(|error| failed(WriteStep::MakeParents, path, &error))?;

    steps
        .set_dir_mode(path, mode)
        .map_err(|error| failed(WriteStep::SetMode, path, &error))
}

#[cfg(test)]
mod tests {
    use std::cell::RefCell;
    use std::os::unix::fs::{MetadataExt, symlink};

    use creche_testkit::root::TempRoot;

    use super::*;

    /// The number of the error "No space left on device", on Linux and on
    /// macOS.
    const NO_SPACE: i32 = 28;

    /// The text of that error.
    const NO_SPACE_TEXT: &str = "No space left on device";

    /// The strictest write: a mode for the group, each parent, each sync.
    const STRICT: Write = Write {
        mode: FileMode::GroupRead,
        parents: Parents::Create,
        dir_sync: DirSync::Sync,
    };

    /// A [`Steps`] value for a test. It makes each call as [`Host`] does.
    ///
    /// It also does what a test cannot ask of the host: it fails one step,
    /// it gives each temporary file a count that the test knows, it narrows
    /// the mode of a create as a umask does, and it lets a test change the
    /// directory in the middle of a write. It keeps each step and each
    /// temporary path that it saw.
    struct Probe {
        /// The step that fails, and how many calls of that step pass first.
        fails: Option<(WriteStep, usize)>,
        /// The count in the name of each temporary file.
        count: u64,
        /// The mode bits that the create takes away.
        umask: u32,
        /// What another process does to the path of the temporary file
        /// before the write sets the mode.
        before_mode: Option<fn(&Path)>,
        /// Each step, in the order of the calls.
        seen: RefCell<Vec<WriteStep>>,
        /// The path of each temporary file that the code asked for.
        temps: RefCell<Vec<PathBuf>>,
    }

    impl Probe {
        /// A value that fails no step.
        fn new() -> Self {
            Self {
                fails: None,
                count: 7,
                umask: 0,
                before_mode: None,
                seen: RefCell::new(Vec::new()),
                temps: RefCell::new(Vec::new()),
            }
        }

        /// A value that fails each call of `step`.
        fn failing(step: WriteStep) -> Self {
            Self::failing_after(step, 0)
        }

        /// A value that lets `passes` calls of `step` pass, and fails each
        /// later call of that step.
        fn failing_after(step: WriteStep, passes: usize) -> Self {
            Self {
                fails: Some((step, passes)),
                ..Self::new()
            }
        }

        /// Keeps the step. Gives the error of a full disk for the step that
        /// this value fails.
        fn pass(&self, step: WriteStep) -> io::Result<()> {
            let earlier = self.count_of(step);
            self.seen.borrow_mut().push(step);

            match self.fails {
                Some((fails, passes)) if fails == step && earlier >= passes => {
                    Err(io::Error::from_raw_os_error(NO_SPACE))
                }
                _ => Ok(()),
            }
        }

        fn count_of(&self, step: WriteStep) -> usize {
            self.seen
                .borrow()
                .iter()
                .filter(|seen| **seen == step)
                .count()
        }

        fn steps(&self) -> Vec<WriteStep> {
            self.seen.borrow().clone()
        }
    }

    impl Steps for Probe {
        fn next_count(&self) -> u64 {
            self.count
        }

        fn make_dirs(&self, dir: &Path) -> io::Result<()> {
            self.pass(WriteStep::MakeParents)?;

            Host.make_dirs(dir)
        }

        fn create_temp(&self, temp: &Path, mode: FileMode) -> io::Result<File> {
            self.temps.borrow_mut().push(temp.to_owned());
            self.pass(WriteStep::CreateTemp)?;

            open_temp(temp, mode.bits() & !self.umask)
        }

        fn write_temp(&self, file: &mut File, bytes: &[u8]) -> io::Result<()> {
            self.pass(WriteStep::WriteTemp)?;

            Host.write_temp(file, bytes)
        }

        fn sync_temp(&self, file: &File) -> io::Result<()> {
            self.pass(WriteStep::SyncTemp)?;

            Host.sync_temp(file)
        }

        fn set_file_mode(&self, file: &File, mode: FileMode) -> io::Result<()> {
            self.pass(WriteStep::SetMode)?;

            if let (Some(change), Some(temp)) = (self.before_mode, self.temps.borrow().last()) {
                change(temp);
            }

            Host.set_file_mode(file, mode)
        }

        fn set_dir_mode(&self, dir: &Path, mode: DirMode) -> io::Result<()> {
            self.pass(WriteStep::SetMode)?;

            Host.set_dir_mode(dir, mode)
        }

        fn rename(&self, from: &Path, to: &Path) -> io::Result<()> {
            self.pass(WriteStep::Rename)?;

            Host.rename(from, to)
        }

        fn link(&self, from: &Path, to: &Path) -> io::Result<()> {
            self.pass(WriteStep::Link)?;

            Host.link(from, to)
        }

        fn sync_dir(&self, dir: &Path) -> io::Result<()> {
            self.pass(WriteStep::SyncDir)?;

            Host.sync_dir(dir)
        }

        fn remove_file(&self, path: &Path) -> io::Result<()> {
            self.pass(WriteStep::RemoveOld)?;

            Host.remove_file(path)
        }

        fn remove_tree(&self, path: &Path) -> io::Result<()> {
            self.pass(WriteStep::RemoveOld)?;

            Host.remove_tree(path)
        }
    }

    /// The permission bits of the entry at `path`.
    fn mode_of(path: &Path) -> u32 {
        fs::metadata(path).unwrap().mode() & 0o7777
    }

    /// The name of each entry of the directory `dir`, in sorted order.
    fn names_in(dir: &Path) -> Vec<String> {
        let mut names: Vec<String> = fs::read_dir(dir)
            .unwrap()
            .map(|entry| entry.unwrap().file_name().into_string().unwrap())
            .collect();
        names.sort();

        names
    }

    /// The temporary file that a [`Probe`] names for the target `path`.
    fn probe_temp(path: &Path) -> PathBuf {
        let name = path.file_name().unwrap().to_str().unwrap();

        path.with_file_name(format!(".{name}.{}.7.tmp", std::process::id()))
    }

    /// The error of the step that a [`Probe`] failed.
    fn no_space(step: WriteStep, path: &Path) -> WriteError {
        WriteError {
            step,
            path: path.to_owned(),
            kind: io::ErrorKind::StorageFull,
            os_text: String::from(NO_SPACE_TEXT),
        }
    }

    #[test]
    fn an_error_names_the_step_the_path_and_the_answer_of_the_system() {
        let error = WriteError {
            step: WriteStep::Rename,
            path: PathBuf::from("/srv/state/faults/pep/chat.json"),
            kind: io::ErrorKind::PermissionDenied,
            os_text: String::from("Permission denied"),
        };

        assert_eq!(
            error.to_string(),
            "cannot write /srv/state/faults/pep/chat.json: the step \"rename onto the target\" \
             failed: Permission denied"
        );
    }

    #[test]
    fn each_step_has_its_own_text() {
        let steps = [
            WriteStep::MakeParents,
            WriteStep::CreateTemp,
            WriteStep::WriteTemp,
            WriteStep::SyncTemp,
            WriteStep::SetMode,
            WriteStep::Rename,
            WriteStep::Link,
            WriteStep::SyncDir,
            WriteStep::RemoveOld,
        ];
        let mut texts: Vec<String> = steps.iter().map(ToString::to_string).collect();
        texts.sort();
        texts.dedup();

        assert_eq!(texts.len(), steps.len());
    }

    // --- write: the tests of caregiver/tests/test_atomic.py ---

    #[test]
    fn a_write_makes_the_file_with_its_content_and_its_mode() {
        let root = TempRoot::new().unwrap();

        for (name, mode, bits) in [
            ("creds.json", FileMode::Private, 0o600),
            ("grant.json", FileMode::GroupRead, 0o640),
            ("status.json", FileMode::PublicRead, 0o644),
        ] {
            let target = root.path().join(name);
            let how = Write { mode, ..STRICT };

            write(&target, b"{\"epoch\": 1}", how).unwrap();

            assert_eq!(fs::read(&target).unwrap(), b"{\"epoch\": 1}");
            assert_eq!(mode_of(&target), bits, "{name}");
        }
    }

    #[test]
    fn a_write_makes_each_directory_that_is_absent() {
        let root = TempRoot::new().unwrap();
        let target = root.path().join("families/chat/creds/creds.json");

        write(&target, b"{}", STRICT).unwrap();

        assert_eq!(fs::read(&target).unwrap(), b"{}");
    }

    #[test]
    fn a_write_leaves_no_temporary_file() {
        let root = TempRoot::new().unwrap();
        let target = root.path().join("status.json");

        write(&target, b"{}", STRICT).unwrap();

        assert_eq!(names_in(root.path()), ["status.json"]);
    }

    #[test]
    fn a_write_replaces_each_byte_of_the_old_content() {
        let root = TempRoot::new().unwrap();
        let target = root.path().join("grant.json");

        write(&target, b"a much longer first version of the file", STRICT).unwrap();
        write(&target, b"short", STRICT).unwrap();

        assert_eq!(fs::read(&target).unwrap(), b"short");
    }

    #[test]
    fn a_failed_write_removes_its_temporary_file_and_keeps_the_old_content() {
        // The Python test replaces `os.fsync` with a function that raises.
        let root = TempRoot::new().unwrap();
        let target = root.path().join("grant.json");
        write(&target, b"original", STRICT).unwrap();
        let probe = Probe::failing(WriteStep::SyncTemp);

        let error = write_with(&probe, &target, b"replacement", STRICT).unwrap_err();

        assert_eq!(error, no_space(WriteStep::SyncTemp, &probe_temp(&target)));
        assert_eq!(fs::read(&target).unwrap(), b"original");
        assert_eq!(names_in(root.path()), ["grant.json"]);
    }

    #[test]
    fn a_private_file_has_no_bit_for_the_group_or_for_each_user() {
        let root = TempRoot::new().unwrap();
        let target = root.path().join("creds.json");
        let how = Write {
            mode: FileMode::Private,
            ..STRICT
        };

        write(&target, b"{}", how).unwrap();

        assert_eq!(mode_of(&target) & 0o077, 0);
    }

    // --- write: the temporary file ---

    #[test]
    fn the_temporary_name_starts_with_a_dot_and_ends_in_tmp() {
        let name = temp_name(OsStr::new("status.json"), 4321, 17);

        assert_eq!(name, OsStr::new(".status.json.4321.17.tmp"));

        // A reader that lists `*.json` does not take the file, and a reader
        // that skips each name with a dot at its start does not take it.
        let text = name.to_str().unwrap();
        assert!(text.starts_with('.'));
        assert!(text.ends_with(".tmp"));
        assert!(!text.ends_with(".json"));
    }

    #[test]
    fn the_temporary_file_is_beside_the_target_and_holds_the_process_id() {
        let root = TempRoot::new().unwrap();
        let target = root.path().join("faults/pep/chat.json");
        let probe = Probe::new();

        write_with(&probe, &target, b"{}", STRICT).unwrap();

        let expected = root.path().join(format!(
            "faults/pep/.chat.json.{}.7.tmp",
            std::process::id()
        ));
        assert_eq!(*probe.temps.borrow(), [expected]);
    }

    #[test]
    fn two_writes_of_one_process_have_two_temporary_names() {
        let target = Path::new("/srv/state/status.json");
        let first = temp_path(&Host, target).unwrap();
        let second = temp_path(&Host, target).unwrap();

        assert_ne!(first, second);
        assert_eq!(first.parent(), target.parent());
        assert_eq!(second.parent(), target.parent());
    }

    #[test]
    fn a_leftover_temporary_file_does_not_stop_a_write() {
        // A killed process with the same id left the file.
        let root = TempRoot::new().unwrap();
        let target = root.path().join("status.json");
        let leftover = probe_temp(&target);
        fs::write(&leftover, b"half of an old docu").unwrap();
        let probe = Probe::new();

        write_with(&probe, &target, b"{\"state\":\"in_sync\"}", STRICT).unwrap();

        assert_eq!(fs::read(&target).unwrap(), b"{\"state\":\"in_sync\"}");
        assert_eq!(mode_of(&target), 0o640);
        assert_eq!(names_in(root.path()), ["status.json"]);
        assert_eq!(
            probe.steps(),
            [
                WriteStep::MakeParents,
                WriteStep::CreateTemp,
                WriteStep::RemoveOld,
                WriteStep::CreateTemp,
                WriteStep::WriteTemp,
                WriteStep::SyncTemp,
                WriteStep::SetMode,
                WriteStep::Rename,
                WriteStep::SyncDir,
            ]
        );
    }

    #[test]
    fn a_symlink_at_the_temporary_name_is_removed_and_not_followed() {
        let root = TempRoot::new().unwrap();
        let target = root.path().join("status.json");
        let other = root.path().join("other.txt");
        fs::write(&other, b"the file of another writer").unwrap();
        symlink(&other, probe_temp(&target)).unwrap();

        write_with(&Probe::new(), &target, b"{}", STRICT).unwrap();

        assert_eq!(fs::read(&target).unwrap(), b"{}");
        assert_eq!(fs::read(&other).unwrap(), b"the file of another writer");
        assert_eq!(names_in(root.path()), ["other.txt", "status.json"]);
    }

    #[test]
    fn the_host_create_refuses_a_name_that_exists_and_follows_no_symlink() {
        // The create of the host itself, with no test value between.
        let root = TempRoot::new().unwrap();
        let other = root.path().join("other.txt");
        fs::write(&other, b"the file of another writer").unwrap();
        let leftover = root.path().join(".status.json.1.0.tmp");
        fs::write(&leftover, b"half of an old docu").unwrap();
        let link = root.path().join(".status.json.1.1.tmp");
        symlink(&other, &link).unwrap();

        for temp in [&leftover, &link] {
            let error = Host.create_temp(temp, FileMode::GroupRead).unwrap_err();

            assert_eq!(
                error.kind(),
                io::ErrorKind::AlreadyExists,
                "{}",
                temp.display()
            );
        }

        assert_eq!(fs::read(&leftover).unwrap(), b"half of an old docu");
        assert_eq!(fs::read(&other).unwrap(), b"the file of another writer");
    }

    #[test]
    fn a_leftover_that_the_write_cannot_remove_stops_the_write() {
        let root = TempRoot::new().unwrap();
        let target = root.path().join("status.json");
        write(&target, b"original", STRICT).unwrap();
        let leftover = probe_temp(&target);
        fs::create_dir(&leftover).unwrap();

        let error = write_with(&Probe::new(), &target, b"replacement", STRICT).unwrap_err();

        assert_eq!(error.step, WriteStep::RemoveOld);
        assert_eq!(error.path, leftover);
        assert_eq!(fs::read(&target).unwrap(), b"original");
    }

    #[test]
    fn the_second_mode_call_gives_the_mode_that_the_umask_took() {
        let root = TempRoot::new().unwrap();

        for (name, mode, bits) in [
            ("creds.json", FileMode::Private, 0o600),
            ("grant.json", FileMode::GroupRead, 0o640),
            ("status.json", FileMode::PublicRead, 0o644),
        ] {
            let target = root.path().join(name);
            let probe = Probe {
                umask: 0o077,
                ..Probe::new()
            };

            write_with(&probe, &target, b"{}", Write { mode, ..STRICT }).unwrap();

            assert_eq!(mode_of(&target), bits, "{name}");
        }
    }

    // --- write: the steps ---

    #[test]
    fn a_write_syncs_the_file_before_the_rename_and_the_directory_after_it() {
        let root = TempRoot::new().unwrap();
        let probe = Probe::new();

        write_with(&probe, &root.path().join("chat/status.json"), b"{}", STRICT).unwrap();

        assert_eq!(
            probe.steps(),
            [
                WriteStep::MakeParents,
                WriteStep::CreateTemp,
                WriteStep::WriteTemp,
                WriteStep::SyncTemp,
                WriteStep::SetMode,
                WriteStep::Rename,
                WriteStep::SyncDir,
            ]
        );
    }

    #[test]
    fn a_write_does_only_the_steps_that_the_caller_names() {
        let root = TempRoot::new().unwrap();
        let probe = Probe::new();
        let how = Write {
            mode: FileMode::PublicRead,
            parents: Parents::MustExist,
            dir_sync: DirSync::Skip,
        };

        write_with(&probe, &root.path().join("status.json"), b"{}", how).unwrap();

        assert_eq!(
            probe.steps(),
            [
                WriteStep::CreateTemp,
                WriteStep::WriteTemp,
                WriteStep::SyncTemp,
                WriteStep::SetMode,
                WriteStep::Rename,
            ]
        );
    }

    #[test]
    fn a_write_into_an_absent_directory_fails_when_the_caller_says_so() {
        let root = TempRoot::new().unwrap();
        let target = root.path().join("grants/chat.json");
        let how = Write {
            parents: Parents::MustExist,
            ..STRICT
        };

        let error = write(&target, b"{}", how).unwrap_err();

        assert_eq!(error.step, WriteStep::CreateTemp);
        assert_eq!(error.kind, io::ErrorKind::NotFound);
        assert_eq!(error.os_text, "No such file or directory");
        assert!(names_in(root.path()).is_empty());
    }

    #[test]
    fn each_step_before_the_rename_keeps_the_old_content_and_leaves_no_file() {
        for step in [
            WriteStep::MakeParents,
            WriteStep::CreateTemp,
            WriteStep::WriteTemp,
            WriteStep::SyncTemp,
            WriteStep::SetMode,
            WriteStep::Rename,
        ] {
            let root = TempRoot::new().unwrap();
            let target = root.path().join("grant.json");
            write(&target, b"original", STRICT).unwrap();
            let probe = Probe::failing(step);
            let about = match step {
                WriteStep::MakeParents => root.path().to_owned(),
                WriteStep::Rename => target.clone(),
                _ => probe_temp(&target),
            };

            let error = write_with(&probe, &target, b"replacement", STRICT).unwrap_err();

            assert_eq!(error, no_space(step, &about), "{step:?}");
            assert_eq!(fs::read(&target).unwrap(), b"original", "{step:?}");
            assert_eq!(names_in(root.path()), ["grant.json"], "{step:?}");
            assert_eq!(probe.steps().last(), Some(&step), "{step:?}");
        }
    }

    #[test]
    fn a_failed_first_write_leaves_no_file_at_all() {
        for step in [
            WriteStep::CreateTemp,
            WriteStep::WriteTemp,
            WriteStep::SyncTemp,
            WriteStep::SetMode,
            WriteStep::Rename,
        ] {
            let root = TempRoot::new().unwrap();
            let target = root.path().join("grant.json");

            let error = write_with(&Probe::failing(step), &target, b"first", STRICT).unwrap_err();

            assert_eq!(error.step, step);
            assert!(names_in(root.path()).is_empty(), "{step:?}");
        }
    }

    #[test]
    fn a_failed_directory_sync_is_an_error_after_the_new_content_is_in_place() {
        let root = TempRoot::new().unwrap();
        let target = root.path().join("grant.json");
        write(&target, b"original", STRICT).unwrap();

        let error = write_with(
            &Probe::failing(WriteStep::SyncDir),
            &target,
            b"replacement",
            STRICT,
        )
        .unwrap_err();

        assert_eq!(error, no_space(WriteStep::SyncDir, root.path()));
        assert_eq!(fs::read(&target).unwrap(), b"replacement");
        assert_eq!(names_in(root.path()), ["grant.json"]);
    }

    #[test]
    fn a_write_onto_a_symlink_replaces_the_symlink() {
        let root = TempRoot::new().unwrap();
        let other = root.path().join("other.json");
        fs::write(&other, b"the file of another writer").unwrap();
        let target = root.path().join("status.json");
        symlink(&other, &target).unwrap();

        write(&target, b"{}", STRICT).unwrap();

        assert!(fs::symlink_metadata(&target).unwrap().is_file());
        assert_eq!(fs::read(&target).unwrap(), b"{}");
        assert_eq!(fs::read(&other).unwrap(), b"the file of another writer");
    }

    #[test]
    fn a_write_onto_a_directory_fails_at_the_rename() {
        let root = TempRoot::new().unwrap();
        let target = root.path().join("status.json");
        fs::create_dir(&target).unwrap();

        let error = write(&target, b"{}", STRICT).unwrap_err();

        assert_eq!(error.step, WriteStep::Rename);
        assert_eq!(error.path, target);
        assert_eq!(names_in(root.path()), ["status.json"]);
    }

    #[test]
    fn a_target_with_no_file_name_is_refused_before_each_step() {
        for target in ["/", "/srv/state/.."] {
            let probe = Probe::new();

            let error = write_with(&probe, Path::new(target), b"{}", STRICT).unwrap_err();

            assert_eq!(
                error,
                WriteError {
                    step: WriteStep::CreateTemp,
                    path: PathBuf::from(target),
                    kind: io::ErrorKind::InvalidInput,
                    os_text: String::from("the path has no file name"),
                }
            );
            assert!(probe.steps().is_empty());
        }
    }

    #[test]
    fn a_path_of_one_part_is_in_the_working_directory() {
        assert_eq!(dir_of(Path::new("status.json")), Path::new("."));
        assert_eq!(dir_of(Path::new("/status.json")), Path::new("/"));
        assert_eq!(
            dir_of(Path::new("/srv/state/status.json")),
            Path::new("/srv/state")
        );
        assert_eq!(dir_of(Path::new("state/status.json")), Path::new("state"));
    }

    #[test]
    fn the_host_syncs_a_directory_and_refuses_an_absent_one() {
        let root = TempRoot::new().unwrap();

        Host.sync_dir(root.path()).unwrap();

        let error = Host.sync_dir(&root.path().join("no-such-dir")).unwrap_err();
        assert_eq!(error.kind(), io::ErrorKind::NotFound);
    }

    // --- write_new ---

    #[test]
    fn a_new_file_gets_its_content_and_its_mode_and_no_temporary_file_stays() {
        let root = TempRoot::new().unwrap();

        for (name, mode, bits) in [
            ("secret.enc", FileMode::Private, 0o600),
            ("entry.json", FileMode::GroupRead, 0o640),
            ("note.json", FileMode::PublicRead, 0o644),
        ] {
            let target = root.path().join(name);

            write_new(&target, b"{\"id\":1}", mode).unwrap();

            assert_eq!(fs::read(&target).unwrap(), b"{\"id\":1}");
            assert_eq!(mode_of(&target), bits, "{name}");
        }

        assert_eq!(
            names_in(root.path()),
            ["entry.json", "note.json", "secret.enc"]
        );
    }

    #[test]
    fn a_new_file_never_replaces_a_name_that_exists() {
        let root = TempRoot::new().unwrap();
        let target = root.path().join("entry.json");
        write_new(&target, b"first", FileMode::GroupRead).unwrap();

        let error = write_new(&target, b"second", FileMode::GroupRead).unwrap_err();

        assert_eq!(error.step, WriteStep::Link);
        assert_eq!(error.path, target);
        assert_eq!(error.kind, io::ErrorKind::AlreadyExists);
        assert_eq!(error.os_text, "File exists");
        assert_eq!(fs::read(&target).unwrap(), b"first");
        assert_eq!(names_in(root.path()), ["entry.json"]);
    }

    #[test]
    fn a_new_file_does_not_replace_a_symlink_or_a_directory() {
        let root = TempRoot::new().unwrap();
        let link = root.path().join("link.json");
        symlink(root.path().join("no-such-file"), &link).unwrap();
        let dir = root.path().join("dir.json");
        fs::create_dir(&dir).unwrap();

        for target in [&link, &dir] {
            let error = write_new(target, b"{}", FileMode::GroupRead).unwrap_err();

            assert_eq!(error.step, WriteStep::Link);
            assert_eq!(error.kind, io::ErrorKind::AlreadyExists);
        }

        assert!(
            fs::symlink_metadata(&link)
                .unwrap()
                .file_type()
                .is_symlink()
        );
        assert!(!root.path().join("no-such-file").exists());
        assert_eq!(names_in(root.path()), ["dir.json", "link.json"]);
    }

    #[test]
    fn a_new_file_needs_its_directory() {
        let root = TempRoot::new().unwrap();
        let target = root.path().join("done/entry.json");

        let error = write_new(&target, b"{}", FileMode::GroupRead).unwrap_err();

        assert_eq!(error.step, WriteStep::CreateTemp);
        assert_eq!(error.kind, io::ErrorKind::NotFound);
        assert!(names_in(root.path()).is_empty());
    }

    #[test]
    fn a_new_file_is_linked_and_not_renamed_and_has_no_directory_sync() {
        let root = TempRoot::new().unwrap();
        let probe = Probe::new();

        write_new_with(
            &probe,
            &root.path().join("entry.json"),
            b"{}",
            FileMode::GroupRead,
        )
        .unwrap();

        assert_eq!(
            probe.steps(),
            [
                WriteStep::CreateTemp,
                WriteStep::WriteTemp,
                WriteStep::SyncTemp,
                WriteStep::SetMode,
                WriteStep::Link,
                WriteStep::RemoveOld,
            ]
        );
    }

    #[test]
    fn each_failed_step_of_a_new_file_leaves_no_file_at_all() {
        for step in [
            WriteStep::CreateTemp,
            WriteStep::WriteTemp,
            WriteStep::SyncTemp,
            WriteStep::SetMode,
            WriteStep::Link,
        ] {
            let root = TempRoot::new().unwrap();
            let target = root.path().join("entry.json");
            let about = match step {
                WriteStep::Link => target.clone(),
                _ => probe_temp(&target),
            };

            let error = write_new_with(&Probe::failing(step), &target, b"{}", FileMode::GroupRead)
                .unwrap_err();

            assert_eq!(error, no_space(step, &about), "{step:?}");
            assert!(names_in(root.path()).is_empty(), "{step:?}");
        }
    }

    #[test]
    fn a_failed_remove_after_the_link_is_an_error_and_the_new_file_is_whole() {
        let root = TempRoot::new().unwrap();
        let target = root.path().join("entry.json");

        let error = write_new_with(
            &Probe::failing(WriteStep::RemoveOld),
            &target,
            b"{\"id\":1}",
            FileMode::GroupRead,
        )
        .unwrap_err();

        assert_eq!(error, no_space(WriteStep::RemoveOld, &probe_temp(&target)));
        assert_eq!(fs::read(&target).unwrap(), b"{\"id\":1}");
        assert_eq!(mode_of(&target), 0o640);
    }

    #[test]
    fn a_leftover_temporary_file_does_not_stop_a_new_file() {
        let root = TempRoot::new().unwrap();
        let target = root.path().join("entry.json");
        fs::write(probe_temp(&target), b"half of an old entry").unwrap();

        write_new_with(&Probe::new(), &target, b"{}", FileMode::GroupRead).unwrap();

        assert_eq!(fs::read(&target).unwrap(), b"{}");
        assert_eq!(names_in(root.path()), ["entry.json"]);
    }

    // --- replace_dir: the tests of caregiver/tests/test_atomic.py ---

    /// Makes the directory `dir` with one file `instructions.md`.
    fn tree_with(dir: &Path, text: &str) {
        fs::create_dir_all(dir).unwrap();
        fs::write(dir.join("instructions.md"), text).unwrap();
    }

    /// The text of `instructions.md` in the directory `dir`.
    fn text_in(dir: &Path) -> String {
        fs::read_to_string(dir.join("instructions.md")).unwrap()
    }

    #[test]
    fn a_new_tree_goes_to_a_target_that_is_absent() {
        let root = TempRoot::new().unwrap();
        let staging = root.path().join("config.tmp");
        tree_with(&staging, "hello");
        let target = root.path().join("config");

        replace_dir(&staging, &target).unwrap();

        assert_eq!(text_in(&target), "hello");
        assert!(!staging.exists());
    }

    #[test]
    fn a_new_tree_replaces_each_file_of_the_old_tree() {
        let root = TempRoot::new().unwrap();
        let target = root.path().join("config");
        tree_with(&target, "old");
        fs::write(target.join("stale-file.txt"), "should not survive").unwrap();
        let staging = root.path().join("config.tmp");
        tree_with(&staging, "new");

        replace_dir(&staging, &target).unwrap();

        assert_eq!(text_in(&target), "new");
        assert!(!target.join("stale-file.txt").exists());
        assert_eq!(names_in(root.path()), ["config"]);
    }

    #[test]
    fn a_new_tree_gets_each_directory_above_it() {
        let root = TempRoot::new().unwrap();
        let staging = root.path().join("config.tmp");
        fs::create_dir(&staging).unwrap();
        let target = root.path().join("families/chat/config");

        replace_dir(&staging, &target).unwrap();

        assert!(target.is_dir());
    }

    #[test]
    fn the_old_tree_of_a_call_that_stopped_does_not_stop_the_next_call() {
        // A call that stopped after its first rename left `config.old`.
        let root = TempRoot::new().unwrap();
        let target = root.path().join("config");
        tree_with(&target, "current");
        let stale = root.path().join("config.old");
        tree_with(&stale, "from a crashed attempt");
        let staging = root.path().join("config.tmp");
        tree_with(&staging, "newest");

        replace_dir(&staging, &target).unwrap();

        assert_eq!(text_in(&target), "newest");
        assert!(!stale.exists());
    }

    // --- replace_dir: the steps ---

    #[test]
    fn a_swap_renames_two_times_then_syncs_then_removes_the_old_tree() {
        let root = TempRoot::new().unwrap();
        let target = root.path().join("chat/config");
        tree_with(&target, "current");
        tree_with(
            &root.path().join("chat/config.old"),
            "from a crashed attempt",
        );
        let staging = root.path().join("chat/config.tmp");
        tree_with(&staging, "newest");
        let probe = Probe::new();

        replace_dir_with(&probe, &staging, &target).unwrap();

        assert_eq!(
            probe.steps(),
            [
                WriteStep::MakeParents,
                WriteStep::RemoveOld,
                WriteStep::Rename,
                WriteStep::Rename,
                WriteStep::SyncDir,
                WriteStep::RemoveOld,
            ]
        );
        assert_eq!(names_in(&root.path().join("chat")), ["config"]);
    }

    #[test]
    fn a_swap_that_fails_at_its_first_rename_keeps_the_old_tree_in_place() {
        let root = TempRoot::new().unwrap();
        let target = root.path().join("config");
        tree_with(&target, "current");
        let staging = root.path().join("config.tmp");
        tree_with(&staging, "newest");

        let error =
            replace_dir_with(&Probe::failing(WriteStep::Rename), &staging, &target).unwrap_err();

        assert_eq!(
            error,
            no_space(WriteStep::Rename, &root.path().join("config.old"))
        );
        assert_eq!(text_in(&target), "current");
        assert_eq!(text_in(&staging), "newest");
    }

    #[test]
    fn a_swap_that_fails_at_its_second_rename_leaves_the_old_tree_aside() {
        // The target is absent until the next call, and the old tree is not
        // lost.
        let root = TempRoot::new().unwrap();
        let target = root.path().join("config");
        tree_with(&target, "current");
        let staging = root.path().join("config.tmp");
        tree_with(&staging, "newest");
        let probe = Probe::failing_after(WriteStep::Rename, 1);

        let error = replace_dir_with(&probe, &staging, &target).unwrap_err();

        assert_eq!(error, no_space(WriteStep::Rename, &target));
        assert!(!target.exists());
        assert_eq!(text_in(&root.path().join("config.old")), "current");
        assert_eq!(names_in(root.path()), ["config.old", "config.tmp"]);

        // The next call puts the new tree in place and removes the old one.
        replace_dir(&staging, &target).unwrap();

        assert_eq!(text_in(&target), "newest");
        assert_eq!(names_in(root.path()), ["config"]);
    }

    #[test]
    fn a_swap_with_no_staging_tree_fails_at_the_rename() {
        let root = TempRoot::new().unwrap();
        let target = root.path().join("config");

        let error = replace_dir(&root.path().join("config.tmp"), &target).unwrap_err();

        assert_eq!(error.step, WriteStep::Rename);
        assert_eq!(error.path, target);
        assert_eq!(error.kind, io::ErrorKind::NotFound);
    }

    #[test]
    fn a_failed_directory_sync_of_a_swap_leaves_the_new_tree_in_place() {
        let root = TempRoot::new().unwrap();
        let target = root.path().join("config");
        tree_with(&target, "current");
        let staging = root.path().join("config.tmp");
        tree_with(&staging, "newest");

        let error =
            replace_dir_with(&Probe::failing(WriteStep::SyncDir), &staging, &target).unwrap_err();

        assert_eq!(error, no_space(WriteStep::SyncDir, root.path()));
        assert_eq!(text_in(&target), "newest");
        assert_eq!(text_in(&root.path().join("config.old")), "current");
    }

    #[test]
    fn a_stale_old_tree_that_the_swap_cannot_remove_stops_the_swap() {
        let root = TempRoot::new().unwrap();
        let target = root.path().join("config");
        tree_with(&target, "current");
        tree_with(&root.path().join("config.old"), "from a crashed attempt");
        let staging = root.path().join("config.tmp");
        tree_with(&staging, "newest");

        let error =
            replace_dir_with(&Probe::failing(WriteStep::RemoveOld), &staging, &target).unwrap_err();

        assert_eq!(
            error,
            no_space(WriteStep::RemoveOld, &root.path().join("config.old"))
        );
        assert_eq!(text_in(&target), "current");
        assert_eq!(text_in(&staging), "newest");
    }

    #[test]
    fn the_last_remove_of_a_swap_can_fail_and_the_swap_succeeds() {
        let root = TempRoot::new().unwrap();
        let target = root.path().join("config");
        tree_with(&target, "current");
        let staging = root.path().join("config.tmp");
        tree_with(&staging, "newest");

        replace_dir_with(&Probe::failing(WriteStep::RemoveOld), &staging, &target).unwrap();

        assert_eq!(text_in(&target), "newest");
        assert_eq!(text_in(&root.path().join("config.old")), "current");
    }

    #[test]
    fn a_file_at_the_old_name_stops_the_swap_and_stays() {
        // Python raises here too: `shutil.rmtree` refuses a file.
        let root = TempRoot::new().unwrap();
        let target = root.path().join("config");
        tree_with(&target, "current");
        fs::write(root.path().join("config.old"), b"not a tree").unwrap();
        let staging = root.path().join("config.tmp");
        tree_with(&staging, "newest");

        let error = replace_dir(&staging, &target).unwrap_err();

        assert_eq!(error.step, WriteStep::Rename);
        assert_eq!(error.kind, io::ErrorKind::NotADirectory);
        assert_eq!(text_in(&target), "current");
        assert_eq!(
            fs::read(root.path().join("config.old")).unwrap(),
            b"not a tree"
        );
    }

    #[test]
    fn a_swap_target_with_no_file_name_is_refused_before_each_step() {
        let root = TempRoot::new().unwrap();
        let staging = root.path().join("config.tmp");
        fs::create_dir(&staging).unwrap();
        let probe = Probe::new();

        let error = replace_dir_with(&probe, &staging, Path::new("/")).unwrap_err();

        assert_eq!(error.step, WriteStep::Rename);
        assert_eq!(error.kind, io::ErrorKind::InvalidInput);
        assert!(probe.steps().is_empty());
        assert!(staging.is_dir());
    }

    // --- ensure_dir ---

    #[test]
    fn a_directory_gets_its_mode_and_each_directory_above_it() {
        let root = TempRoot::new().unwrap();

        for (name, mode, bits) in [
            ("a/private", DirMode::Private, 0o700),
            ("b/group", DirMode::Group, 0o750),
            ("c/sock", DirMode::SetgidGroup, 0o2750),
        ] {
            let dir = root.path().join(name);

            ensure_dir(&dir, mode).unwrap();

            assert!(dir.is_dir());
            assert_eq!(mode_of(&dir), bits, "{name}");
        }
    }

    #[test]
    fn a_directory_that_exists_gets_the_mode_and_keeps_its_entries() {
        let root = TempRoot::new().unwrap();
        let dir = root.path().join("faults");
        fs::create_dir(&dir).unwrap();
        fs::set_permissions(&dir, Permissions::from_mode(0o777)).unwrap();
        fs::write(dir.join("chat.json"), b"{}").unwrap();

        ensure_dir(&dir, DirMode::Group).unwrap();

        assert_eq!(mode_of(&dir), 0o750);
        assert_eq!(names_in(&dir), ["chat.json"]);
    }

    #[test]
    fn a_file_at_the_name_of_the_directory_is_an_error() {
        let root = TempRoot::new().unwrap();
        let path = root.path().join("faults");
        fs::write(&path, b"").unwrap();

        let error = ensure_dir(&path, DirMode::Group).unwrap_err();

        assert_eq!(error.step, WriteStep::MakeParents);
        assert_eq!(error.path, path);
        assert_eq!(error.kind, io::ErrorKind::AlreadyExists);
    }

    #[test]
    fn each_failed_step_of_a_directory_is_an_error_with_its_step() {
        let root = TempRoot::new().unwrap();
        let dir = root.path().join("faults/pep");

        let error = ensure_dir_with(
            &Probe::failing(WriteStep::MakeParents),
            &dir,
            DirMode::Group,
        )
        .unwrap_err();
        assert_eq!(error, no_space(WriteStep::MakeParents, &dir));
        assert!(names_in(root.path()).is_empty());

        let error =
            ensure_dir_with(&Probe::failing(WriteStep::SetMode), &dir, DirMode::Group).unwrap_err();
        assert_eq!(error, no_space(WriteStep::SetMode, &dir));
    }

    #[test]
    fn a_directory_is_made_and_then_gets_its_mode() {
        let root = TempRoot::new().unwrap();
        let probe = Probe::new();

        ensure_dir_with(&probe, &root.path().join("faults"), DirMode::Group).unwrap();

        assert_eq!(probe.steps(), [WriteStep::MakeParents, WriteStep::SetMode]);
    }

    // --- the differences from the Python copies ---

    /// One difference between this module and a Python copy of a write.
    struct Deviation {
        /// The Python file and the lines of the copy.
        python: &'static str,
        /// What the copy does.
        copy: &'static str,
        /// What this module does.
        here: &'static str,
        /// The check that this module does what the row says.
        holds: fn(),
    }

    const ONE_NAME: &str = "The name is .<name>.<pid>.<count>.tmp, the form of \
        attendance/src/attendance/atomic.py:45.";

    const ALWAYS_SYNCED: &str = "Each write syncs the temporary file before the rename.";

    const MODE_ON_THE_PATH: &str = "The copy sets the mode with a call on the path of the \
        temporary file.";

    const MODE_ON_THE_FILE: &str = "The write sets the mode with a call on the open file, so \
        the mode goes to the file that the write made.";

    const WRITTEN_OVER: &str = "The open writes over a leftover temporary file.";

    const REMOVED_AND_CREATED: &str = "The create refuses a name that exists. The write removes \
        the leftover file and creates its own file.";

    /// Each difference on purpose between this module and a Python copy. No
    /// vector covers a write, so a row names the Python lines.
    const DEVIATIONS: &[Deviation] = &[
        Deviation {
            python: "caregiver/src/caregiver/atomic.py:30",
            copy: "mkstemp gives the temporary file a random name: .<name>.<random>.tmp.",
            here: ONE_NAME,
            holds: the_temporary_name_has_one_form,
        },
        Deviation {
            python: "chaperone/src/chaperone/faults.py:71",
            copy: "The temporary file is <name>.tmp.",
            here: ONE_NAME,
            holds: the_temporary_name_has_one_form,
        },
        Deviation {
            python: "door-trigger/src/agent_door_trigger/quiet/state.py:73",
            copy: "mkstemp gives the temporary file a random name: .<family>.<random>.tmp.",
            here: ONE_NAME,
            holds: the_temporary_name_has_one_form,
        },
        Deviation {
            python: "handover/src/handover/follow/__init__.py:312",
            copy: "The temporary file is .<name>.tmp.",
            here: ONE_NAME,
            holds: the_temporary_name_has_one_form,
        },
        Deviation {
            python: "handover/src/handover/executor/roster.py:135",
            copy: "The temporary file is <name>.new.",
            here: ONE_NAME,
            holds: the_temporary_name_has_one_form,
        },
        Deviation {
            python: "attendance/src/attendance/atomic.py:48",
            copy: WRITTEN_OVER,
            here: REMOVED_AND_CREATED,
            holds: a_leftover_is_removed_and_not_written,
        },
        Deviation {
            python: "chaperone/src/chaperone/faults.py:72",
            copy: WRITTEN_OVER,
            here: REMOVED_AND_CREATED,
            holds: a_leftover_is_removed_and_not_written,
        },
        Deviation {
            python: "attendance/src/attendance/atomic.py:42-61",
            copy: "The copy does not sync the directory after the rename.",
            here: "DirSync names the choice. A port takes DirSync::Sync, and the write then \
                   syncs the directory.",
            holds: the_caller_names_the_directory_sync,
        },
        Deviation {
            python: "door-trigger/src/agent_door_trigger/quiet/state.py:74-78",
            copy: "The copy syncs neither the temporary file nor the directory.",
            here: ALWAYS_SYNCED,
            holds: each_write_syncs_its_file,
        },
        Deviation {
            python: "handover/src/handover/executor/roster.py:137-142",
            copy: "The copy syncs neither the temporary file nor the directory.",
            here: ALWAYS_SYNCED,
            holds: each_write_syncs_its_file,
        },
        Deviation {
            python: "chaperone/src/chaperone/faults.py:68-89",
            copy: "A write that fails leaves its temporary file.",
            here: "A write that fails before the rename removes its temporary file.",
            holds: a_failed_write_leaves_no_file,
        },
        Deviation {
            python: "handover/src/handover/follow/__init__.py:316-325",
            copy: "A write that fails leaves its temporary file. The next write removes it.",
            here: "A write that fails before the rename removes its temporary file.",
            holds: a_failed_write_leaves_no_file,
        },
        Deviation {
            python: "attendance/src/attendance/atomic.py:57",
            copy: MODE_ON_THE_PATH,
            here: MODE_ON_THE_FILE,
            holds: the_mode_goes_onto_the_open_file,
        },
        Deviation {
            python: "caregiver/src/caregiver/atomic.py:38",
            copy: MODE_ON_THE_PATH,
            here: MODE_ON_THE_FILE,
            holds: the_mode_goes_onto_the_open_file,
        },
        Deviation {
            python: "chaperone/src/chaperone/faults.py:80",
            copy: MODE_ON_THE_PATH,
            here: MODE_ON_THE_FILE,
            holds: the_mode_goes_onto_the_open_file,
        },
        Deviation {
            python: "handover/src/handover/follow/__init__.py:311",
            copy: "The copy makes the directory of the file with a mode of its own.",
            here: "Parents::Create makes a directory with the mode that the umask gives. A \
                   port calls ensure_dir first and writes with Parents::MustExist.",
            holds: a_port_makes_the_directory_first,
        },
        Deviation {
            python: "handover/src/handover/executor/spool.py:365-368",
            copy: "The temporary file is .<name>.tmp. The copy removes a file with that name \
                   before the create, and it opens each name relative to a directory \
                   descriptor.",
            here: "write_new takes a path. The name of the temporary file is \
                   .<name>.<pid>.<count>.tmp, and the write removes only a leftover file.",
            holds: a_new_file_uses_the_one_name,
        },
        Deviation {
            python: "handover/src/handover/requester/file.py:190-193",
            copy: "The temporary file is .<name>.tmp. The copy removes a file with that name \
                   before the create, and it opens each name relative to a directory \
                   descriptor.",
            here: "write_new takes a path. The name of the temporary file is \
                   .<name>.<pid>.<count>.tmp, and the write removes only a leftover file.",
            holds: a_new_file_uses_the_one_name,
        },
        Deviation {
            python: "handover/src/handover/intake/store.py:320-335",
            copy: "The copy creates the name itself, with a create that refuses a name that \
                   exists. Then it writes the bytes.",
            here: "write_new writes a temporary file and links it to the name. The name gets a \
                   file that is whole.",
            holds: a_new_file_is_whole_before_it_has_its_name,
        },
    ];

    fn the_temporary_name_has_one_form() {
        let root = TempRoot::new().unwrap();
        let target = root.path().join("chat.json");
        let probe = Probe::new();

        write_with(&probe, &target, b"{}", STRICT).unwrap();

        assert_eq!(*probe.temps.borrow(), [probe_temp(&target)]);
        assert_eq!(
            temp_name(OsStr::new("chat.json"), 12, 3),
            OsStr::new(".chat.json.12.3.tmp")
        );
    }

    fn a_leftover_is_removed_and_not_written() {
        let root = TempRoot::new().unwrap();
        let target = root.path().join("chat.json");
        let other = root.path().join("other.txt");
        fs::write(&other, b"the file of another writer").unwrap();
        symlink(&other, probe_temp(&target)).unwrap();
        let probe = Probe::new();

        write_with(&probe, &target, b"{}", STRICT).unwrap();

        assert_eq!(fs::read(&other).unwrap(), b"the file of another writer");
        assert_eq!(probe.count_of(WriteStep::CreateTemp), 2);
        assert_eq!(probe.count_of(WriteStep::RemoveOld), 1);
    }

    fn the_caller_names_the_directory_sync() {
        let root = TempRoot::new().unwrap();
        let target = root.path().join("chat.json");

        for (dir_sync, count) in [(DirSync::Sync, 1), (DirSync::Skip, 0)] {
            let probe = Probe::new();

            write_with(&probe, &target, b"{}", Write { dir_sync, ..STRICT }).unwrap();

            assert_eq!(probe.count_of(WriteStep::SyncDir), count, "{dir_sync:?}");
        }
    }

    fn each_write_syncs_its_file() {
        let root = TempRoot::new().unwrap();
        let target = root.path().join("chat.json");
        let laxest = Write {
            mode: FileMode::PublicRead,
            parents: Parents::MustExist,
            dir_sync: DirSync::Skip,
        };
        let probe = Probe::new();

        write_with(&probe, &target, b"{}", laxest).unwrap();

        let steps = probe.steps();
        let sync = steps.iter().position(|step| *step == WriteStep::SyncTemp);
        let rename = steps.iter().position(|step| *step == WriteStep::Rename);
        assert!(sync.is_some() && sync < rename, "{steps:?}");
    }

    fn a_failed_write_leaves_no_file() {
        let root = TempRoot::new().unwrap();
        let target = root.path().join("chat.json");

        write_with(&Probe::failing(WriteStep::SyncTemp), &target, b"{}", STRICT).unwrap_err();

        assert!(names_in(root.path()).is_empty());
    }

    /// Puts a symlink to the file `private.key` at the path `temp`, as a
    /// second writer of the directory can.
    fn swap_for_a_symlink(temp: &Path) {
        fs::remove_file(temp).unwrap();
        symlink(temp.with_file_name("private.key"), temp).unwrap();
    }

    fn the_mode_goes_onto_the_open_file() {
        let root = TempRoot::new().unwrap();
        let other = root.path().join("private.key");
        fs::write(&other, b"the file of another writer").unwrap();
        fs::set_permissions(&other, Permissions::from_mode(0o600)).unwrap();
        let probe = Probe {
            before_mode: Some(swap_for_a_symlink),
            ..Probe::new()
        };

        write_with(&probe, &root.path().join("chat.json"), b"{}", STRICT).unwrap();

        // The mode goes to the file that the write made. The other file
        // keeps its mode.
        assert_eq!(mode_of(&other), 0o600);
    }

    fn a_port_makes_the_directory_first() {
        let root = TempRoot::new().unwrap();
        let dir = root.path().join("follow");
        let how = Write {
            mode: FileMode::Private,
            parents: Parents::MustExist,
            dir_sync: DirSync::Sync,
        };

        ensure_dir(&dir, DirMode::Private).unwrap();
        write(&dir.join("marker.json"), b"{}", how).unwrap();

        assert_eq!(mode_of(&dir), 0o700);
        assert_eq!(mode_of(&dir.join("marker.json")), 0o600);
    }

    fn a_new_file_uses_the_one_name() {
        let root = TempRoot::new().unwrap();
        let target = root.path().join("entry.json");
        // A file with the name of the Python copy is the file of another
        // writer. The write does not remove it.
        let python_temp = root.path().join(".entry.json.tmp");
        fs::write(&python_temp, b"the file of another writer").unwrap();
        let probe = Probe::new();

        write_new_with(&probe, &target, b"{}", FileMode::GroupRead).unwrap();

        assert_eq!(*probe.temps.borrow(), [probe_temp(&target)]);
        assert_eq!(
            fs::read(&python_temp).unwrap(),
            b"the file of another writer"
        );
    }

    fn a_new_file_is_whole_before_it_has_its_name() {
        let root = TempRoot::new().unwrap();
        let target = root.path().join("secret.enc");

        // The write stops before the link: the name is absent, and no part
        // of the bytes is at the name.
        write_new_with(
            &Probe::failing(WriteStep::Link),
            &target,
            b"sealed bytes",
            FileMode::Private,
        )
        .unwrap_err();
        assert!(!target.exists());

        let probe = Probe::new();
        write_new_with(&probe, &target, b"sealed bytes", FileMode::Private).unwrap();
        let steps = probe.steps();
        let sync = steps.iter().position(|step| *step == WriteStep::SyncTemp);
        let link = steps.iter().position(|step| *step == WriteStep::Link);
        assert!(sync.is_some() && sync < link, "{steps:?}");
    }

    #[test]
    fn each_deviation_names_its_python_lines_and_holds() {
        for row in DEVIATIONS {
            let (file, lines) = row.python.rsplit_once(':').unwrap();

            assert!(file.ends_with(".py"), "{}", row.python);
            assert!(
                lines
                    .bytes()
                    .all(|byte| byte.is_ascii_digit() || byte == b'-'),
                "{}",
                row.python
            );
            assert!(
                !row.copy.is_empty() && !row.here.is_empty(),
                "{}",
                row.python
            );
            assert_ne!(row.copy, row.here, "{}", row.python);

            (row.holds)();
        }
    }
}
