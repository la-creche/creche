//! The file side of the git platform spool (decision CH3, phase 12 PR2).
//! `creche_wire::spool` holds the protocol of host port 1105, and
//! `rust/crates/vmm/src/spool.rs` serves it. This module opens, writes,
//! reads and removes the bundle files.
//!
//! The spool root holds two folders, each with one writer:
//!
//! 1. `push/`: the vmm writes each bundle that a guest puts, and removes
//!    it. root makes the folder with the owner of the vmm, the group of
//!    the upstream and the mode 2750. The vmm unit has `UMask=0077`, so
//!    the vmm sets each file to `PUSH_MODE` after the open, and the
//!    upstream reads it through its group.
//! 2. `fetch/`: the upstream writes each bundle that it fetched, and
//!    removes it after the TTL. root makes the folder with the owner of
//!    the upstream, the group of the vmm and the mode 2750. The vmm reads
//!    each file through that group and never writes there.
//!
//! Each open of a folder and of a file takes `O_NOFOLLOW`, so a symlink
//! at the last part of a path is refused. Each file name is a
//! `BundleId`, or the temporary name `.<id>.part` of a put that did not
//! end yet. A `BundleId` holds no `/` and never starts with a dot, so a
//! guest can name no other path. The root itself is the config key
//! `VMM_GIT_SPOOL_ROOT`, and its open follows a symlink, as the open of
//! each other root of the config does.
//!
//! Each call blocks, so a caller in tokio runs it in `spawn_blocking`.

use std::fs::File;
use std::io::{self, Read, Write};
use std::os::fd::{AsFd, OwnedFd};
use std::path::{Component, Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

use creche_vmm_store::layout::RootError;
use creche_wire::FamilyName;
use creche_wire::spool::{BUNDLE_CAP, BundleId};
use rustix::fs::{AtFlags, FileType, Mode, OFlags};
use rustix::io::Errno;

use crate::family::DIR_FLAGS;
use crate::landlock::URANDOM;

/// The folder of the bundles that guests put.
pub const PUSH_DIR: &str = "push";

/// The folder of the bundles that the upstream fetched.
pub const FETCH_DIR: &str = "fetch";

/// The mode of each bundle in `push/`, 0640: the upstream reads it
/// through the group of the folder.
pub const PUSH_MODE: Mode = Mode::RUSR.union(Mode::WUSR).union(Mode::RGRP);

/// The flags of the open of a new bundle in `push/`.
const CREATE_FLAGS: OFlags = OFlags::WRONLY
    .union(OFlags::CREATE)
    .union(OFlags::EXCL)
    .union(OFlags::NOFOLLOW)
    .union(OFlags::CLOEXEC);

/// The flags of the open of a bundle in `fetch/`. `O_NONBLOCK` keeps a
/// FIFO from blocking the open.
const READ_FLAGS: OFlags = OFlags::RDONLY
    .union(OFlags::NOFOLLOW)
    .union(OFlags::NONBLOCK)
    .union(OFlags::CLOEXEC);

/// The random bytes of one ULID: 80 bits.
const RANDOM_BYTES: usize = 10;

/// The temporary name of a put that did not end: `.<id>.part`.
fn part_name(id: &BundleId) -> String {
    format!(".{id}.part")
}

/// Why a get finds no bundle to send.
#[derive(Debug)]
pub enum FetchFault {
    /// `fetch/` holds no file of the id, or holds a symlink, a dir or
    /// another file that is not regular.
    NotFound,
    /// The file has more bytes than `BUNDLE_CAP`.
    TooLarge,
    /// `fetch/` did not open, or another error of the file system.
    Io(io::Error),
}

/// The spool root: `push/` and `fetch/` (decision CH3).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SpoolRoot(PathBuf);

impl SpoolRoot {
    /// Checks `path` and makes the spool root. The folders need not exist
    /// yet: root makes them, and a request before that fails.
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

    /// Opens a new bundle of `family` in `push/`, under its temporary name
    /// and with the mode `PUSH_MODE`. The id comes from the wall time and
    /// 80 bits of `/dev/urandom`.
    ///
    /// # Errors
    ///
    /// The error of the random read, of the open of `push/` or of the
    /// file, or of its mode.
    pub fn create_push(&self, family: &FamilyName) -> io::Result<PushFile> {
        let mut random = [0; RANDOM_BYTES];
        File::open(URANDOM)?.read_exact(&mut random)?;
        let millis = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map_or(0, |since| {
                u64::try_from(since.as_millis()).unwrap_or(u64::MAX)
            });
        let id = BundleId::mint(family, millis, random);
        let dir = self.open_dir(PUSH_DIR)?;
        let part = part_name(&id);
        let fd = rustix::fs::openat(&dir, part.as_str(), CREATE_FLAGS, PUSH_MODE)?;
        let file = PushFile {
            dir,
            part,
            id,
            file: Some(File::from(fd)),
        };
        // The umask of the vmm unit clears the group bits of the open.
        if let Some(open) = &file.file {
            rustix::fs::fchmod(open, PUSH_MODE)?;
        }
        file.check_open()?;
        Ok(file)
    }

    /// Removes `push/<id>`. A file that is gone already is no fault.
    ///
    /// # Errors
    ///
    /// The error of the open of `push/` or of the unlink.
    pub fn remove_push(&self, id: &BundleId) -> io::Result<()> {
        let dir = self.open_dir(PUSH_DIR)?;
        match rustix::fs::unlinkat(&dir, id.as_str(), AtFlags::empty()) {
            Ok(()) | Err(Errno::NOENT) => Ok(()),
            Err(errno) => Err(errno.into()),
        }
    }

    /// Opens `fetch/<id>` for a get: a regular file of at most
    /// `BUNDLE_CAP` bytes.
    ///
    /// # Errors
    ///
    /// A `FetchFault` when there is no such file or it passes the cap.
    pub fn open_fetch(&self, id: &BundleId) -> Result<FetchFile, FetchFault> {
        let dir = self.open_dir(FETCH_DIR).map_err(FetchFault::Io)?;
        let fd = match rustix::fs::openat(&dir, id.as_str(), READ_FLAGS, Mode::empty()) {
            Ok(fd) => fd,
            // `ELOOP` is the refusal of `O_NOFOLLOW`.
            Err(Errno::NOENT | Errno::LOOP) => return Err(FetchFault::NotFound),
            Err(errno) => return Err(FetchFault::Io(errno.into())),
        };
        let stat = rustix::fs::fstat(&fd).map_err(|errno| FetchFault::Io(errno.into()))?;
        if FileType::from_raw_mode(stat.st_mode) != FileType::RegularFile {
            return Err(FetchFault::NotFound);
        }
        let size = u64::try_from(stat.st_size).unwrap_or(u64::MAX);
        if size > BUNDLE_CAP {
            return Err(FetchFault::TooLarge);
        }
        Ok(FetchFile {
            file: File::from(fd),
            size,
        })
    }

    /// Removes each bundle and each temporary file in `push/`. The vmm
    /// runs it at its start, when no VM runs, so no guest waits for one of
    /// these files. A name that the vmm never writes stays. An absent
    /// `push/` holds nothing. Answers how many files it removed.
    ///
    /// # Errors
    ///
    /// The error of the open or the read of `push/`, or of an unlink.
    pub fn sweep_push(&self) -> io::Result<usize> {
        let path = self.0.join(PUSH_DIR);
        let entries = match std::fs::read_dir(&path) {
            Ok(entries) => entries,
            Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(0),
            Err(error) => return Err(error),
        };
        let dir = self.open_dir(PUSH_DIR)?;
        let mut removed = 0_usize;
        for entry in entries {
            let entry = entry?;
            let name = entry.file_name();
            let Some(name) = name.to_str() else {
                continue;
            };
            let ours = name.parse::<BundleId>().is_ok()
                || name
                    .strip_prefix('.')
                    .and_then(|rest| rest.strip_suffix(".part"))
                    .is_some_and(|id| id.parse::<BundleId>().is_ok());
            if !ours || !entry.file_type()?.is_file() {
                continue;
            }
            match rustix::fs::unlinkat(&dir, name, AtFlags::empty()) {
                Ok(()) => removed = removed.saturating_add(1),
                Err(Errno::NOENT) => {}
                Err(errno) => return Err(errno.into()),
            }
        }
        Ok(removed)
    }

    /// Opens the folder `name` of the root, with no follow of a symlink.
    fn open_dir(&self, name: &str) -> io::Result<OwnedFd> {
        Ok(rustix::fs::open(
            self.0.join(name),
            DIR_FLAGS,
            Mode::empty(),
        )?)
    }
}

/// A bundle of `push/` while the vmm writes it, under its temporary name.
/// `commit` renames it to its id. The drop of a file that did not commit
/// removes it, so a put that fails leaves no file.
#[derive(Debug)]
pub struct PushFile {
    dir: OwnedFd,
    part: String,
    id: BundleId,
    file: Option<File>,
}

impl PushFile {
    /// The id that `commit` gives the file.
    #[must_use]
    pub const fn id(&self) -> &BundleId {
        &self.id
    }

    /// Appends `bytes` to the file.
    ///
    /// # Errors
    ///
    /// The error of the write.
    pub fn write(&mut self, bytes: &[u8]) -> io::Result<()> {
        match self.file.as_mut() {
            Some(file) => file.write_all(bytes),
            None => Err(io::ErrorKind::BrokenPipe.into()),
        }
    }

    /// Writes the file to the disk, then renames it to `push/<id>` in one
    /// step, so the upstream never reads a part of it.
    ///
    /// # Errors
    ///
    /// The error of the sync or of the rename. The drop then removes the
    /// temporary file.
    pub fn commit(mut self) -> io::Result<BundleId> {
        if let Some(file) = self.file.take() {
            file.sync_all()?;
        }
        rustix::fs::renameat(&self.dir, self.part.as_str(), &self.dir, self.id.as_str())?;
        self.part.clear();
        Ok(self.id.clone())
    }

    /// Checks that the open file is a regular file with `PUSH_MODE`.
    fn check_open(&self) -> io::Result<()> {
        let Some(file) = &self.file else {
            return Err(io::ErrorKind::BrokenPipe.into());
        };
        let stat = rustix::fs::fstat(file.as_fd())?;
        let regular = FileType::from_raw_mode(stat.st_mode) == FileType::RegularFile;
        if !regular || Mode::from_raw_mode(stat.st_mode) != PUSH_MODE {
            return Err(io::Error::other(
                "the new bundle is not a 0640 regular file",
            ));
        }
        Ok(())
    }
}

impl Drop for PushFile {
    fn drop(&mut self) {
        if self.part.is_empty() {
            return;
        }
        // A file that is gone already needs nothing. Any other error leaves
        // a temporary name, which the sweep of the next vmm start removes.
        let _ = rustix::fs::unlinkat(&self.dir, self.part.as_str(), AtFlags::empty());
    }
}

/// A bundle of `fetch/`, open for a get.
#[derive(Debug)]
pub struct FetchFile {
    /// The open file.
    pub file: File,
    /// Its size in bytes, at most `BUNDLE_CAP`.
    pub size: u64,
}
