//! The read side of the git mirror (decisions CH5f, CH46 rule 4, CH69
//! rules 16 and 17). The mirror sync of the host keeps one clone of each
//! repo in `<mirror>/<name>/.git` (fact 13). The vmm serves a clone of a
//! repo only when the clone list of the family names it and the mirror
//! holds it (decision CH10-10). This module answers the second half.
//!
//! `MirrorRoot::git_dir` opens the root, then `<name>` and `.git` from the
//! handle before, each with `O_DIRECTORY` and `O_NOFOLLOW` (decision CH69
//! rule 16). So a symlink at either step refuses the repo, and the vmm
//! never joins a client path onto the root: the caller passes a name of
//! the clone list. The root itself is the config key `VMM_MIRROR_ROOT`
//! (decision CH69 rule 17), and its open follows a symlink, as the open of
//! each other root of the config does.
//!
//! Each call blocks, so a caller in tokio runs it in `spawn_blocking`.

use std::io;
use std::path::{Component, Path, PathBuf};

use creche_vmm_store::layout::RootError;
use creche_wire::RepoName;
use rustix::fs::{Mode, OFlags};
use rustix::io::Errno;

use crate::family::DIR_FLAGS;

/// The git dir of each repo in its mirror dir.
const GIT_DIR: &str = ".git";

/// Why the mirror does not hold a repo.
#[derive(Debug)]
pub enum MirrorFault {
    /// `<name>` or `<name>/.git` is absent.
    Absent,
    /// `<name>` or `<name>/.git` is a symlink or not a dir.
    NotDir,
    /// The root did not open, or another error of the file system.
    Io(io::Error),
}

/// The mirror root: one dir per repo (decision CH69 rule 17).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct MirrorRoot(PathBuf);

impl MirrorRoot {
    /// Checks `path` and makes the mirror root.
    ///
    /// # Errors
    ///
    /// `NotAbsolute` or `ParentComponent` when the path breaks that rule.
    pub fn new(path: PathBuf) -> Result<Self, RootError> {
        if !path.is_absolute() {
            return Err(RootError::NotAbsolute);
        }
        if path.components().any(|part| part == Component::ParentDir) {
            return Err(RootError::ParentComponent);
        }
        Ok(Self(path))
    }

    /// The path of the root.
    #[must_use]
    pub fn path(&self) -> &Path {
        &self.0
    }

    /// The path `<root>/<name>/.git`, once each of `<name>` and `.git`
    /// opened as a dir with no follow of a symlink, each from the handle
    /// before it. Blocks.
    ///
    /// # Errors
    ///
    /// A `MirrorFault` when the mirror does not hold the repo.
    pub fn git_dir(&self, name: &RepoName) -> Result<PathBuf, MirrorFault> {
        let flags = OFlags::RDONLY | OFlags::DIRECTORY | OFlags::CLOEXEC;
        let root = rustix::fs::open(&self.0, flags, Mode::empty())
            .map_err(|errno| MirrorFault::Io(errno.into()))?;
        let repo =
            rustix::fs::openat(&root, name.as_str(), DIR_FLAGS, Mode::empty()).map_err(fault)?;
        drop(root);
        rustix::fs::openat(&repo, GIT_DIR, DIR_FLAGS, Mode::empty()).map_err(fault)?;
        Ok(self.0.join(name.as_str()).join(GIT_DIR))
    }
}

/// The fault of a failed open of `<name>` or `.git`. `ELOOP` is the
/// refusal of `O_NOFOLLOW`, `ENOTDIR` the refusal of `O_DIRECTORY`.
fn fault(errno: Errno) -> MirrorFault {
    match errno {
        Errno::NOENT => MirrorFault::Absent,
        Errno::LOOP | Errno::NOTDIR => MirrorFault::NotDir,
        errno => MirrorFault::Io(errno.into()),
    }
}
