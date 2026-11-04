//! The digest store of the vmm: one read-only copy of each root disk and of
//! the kernel, named by the SHA-256 of its bytes (fact 56).
//!
//! A release renames the old source tree and deletes the older one
//! (fact 56). So a VM or a snapshot never names a file under the source
//! tree. It names the store copy, which no release touches. The kernel
//! uses the same store (section 3.2.4, gap G8).
//!
//! The vmm hashes each file once, at import, with `sha2` (decision CH36
//! with gap G8). The import copies the source into a temp file, hashes the
//! bytes while it copies, refuses a mismatch with the expected digest, sets
//! mode 0444 and renames the temp file into place. Then it records the
//! identity of the copy beside it. Each VM start compares that identity and
//! hashes nothing (`ImageStore::check`). This is the reading of CH8-8.
//!
//! Root installs the signed `.zst` files unchanged, and the vmm
//! decompresses them (decision CH35 rule 4). The import runs `zstd -d` as a
//! subprocess and hashes its output. That is the open detail of gap G9 in
//! decision CH36 item 35, and it waited on a read-only check that Hyperion
//! has zstd. The check ran on 2026-11-03 as david, under the G31 standing
//! go: `/usr/bin/zstd`, Zstandard CLI v1.5.5, from the Ubuntu package
//! `zstd`. So the vmm config passes `/usr/bin/zstd`.

use std::fmt;
use std::fs::{self, File, OpenOptions};
use std::io::{self, Read, Write};
use std::os::unix::fs::{MetadataExt, OpenOptionsExt, PermissionsExt};
use std::path::{Path, PathBuf};
use std::process::{Command, ExitStatus, Stdio};
use std::str::FromStr;

use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};

use crate::layout::StateRoot;

/// The mode of every store copy: read-only for all (gap G8).
pub const STORE_MODE: u32 = 0o444;

/// The bytes of one read and one write of a copy.
const CHUNK: usize = 1 << 20;

/// The SHA-256 of a file, written as 64 lowercase hex characters.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub struct ImageDigest([u8; 32]);

/// The text is not 64 lowercase hex characters.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct DigestParseError;

impl fmt::Display for DigestParseError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str("a digest is 64 lowercase hex characters")
    }
}

impl std::error::Error for DigestParseError {}

impl FromStr for ImageDigest {
    type Err = DigestParseError;

    fn from_str(text: &str) -> Result<Self, Self::Err> {
        let text = text.as_bytes();
        let mut bytes = [0_u8; 32];
        if text.len() != bytes.len().saturating_mul(2) {
            return Err(DigestParseError);
        }
        for (byte, pair) in bytes.iter_mut().zip(text.chunks_exact(2)) {
            let [high, low] = pair else {
                return Err(DigestParseError);
            };
            *byte = nibble(*high)?
                .saturating_mul(16)
                .saturating_add(nibble(*low)?);
        }
        Ok(Self(bytes))
    }
}

/// The value of one lowercase hex character.
fn nibble(character: u8) -> Result<u8, DigestParseError> {
    match character {
        b'0'..=b'9' => Ok(character.saturating_sub(b'0')),
        b'a'..=b'f' => Ok(character.saturating_sub(b'a').saturating_add(10)),
        _ => Err(DigestParseError),
    }
}

impl fmt::Display for ImageDigest {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        self.0
            .iter()
            .try_for_each(|byte| write!(formatter, "{byte:02x}"))
    }
}

/// What identifies one store copy on the host disk. The import records it,
/// and each VM start compares it (gap G8, PR D6).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct FileIdentity {
    /// The device of the filesystem.
    pub device: u64,
    /// The inode.
    pub inode: u64,
    /// The size in bytes.
    pub size: u64,
    /// The modification time: seconds, then nanoseconds.
    pub mtime: (i64, i64),
    /// The status change time: seconds, then nanoseconds.
    pub ctime: (i64, i64),
    /// The mode, with the file type bits.
    pub mode: u32,
}

impl FileIdentity {
    /// The identity in `metadata`.
    #[must_use]
    pub fn of(metadata: &fs::Metadata) -> Self {
        Self {
            device: metadata.dev(),
            inode: metadata.ino(),
            size: metadata.size(),
            mtime: (metadata.mtime(), metadata.mtime_nsec()),
            ctime: (metadata.ctime(), metadata.ctime_nsec()),
            mode: metadata.mode(),
        }
    }
}

/// One file in the store and the identity that its import recorded.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct StoredImage {
    /// The SHA-256 of the bytes. It names the file.
    pub digest: ImageDigest,
    /// The store copy.
    pub path: PathBuf,
    /// The identity that the import recorded.
    pub identity: FileIdentity,
}

/// Why an import put no file in the store.
#[derive(Debug)]
pub enum ImportError {
    /// A read, a write or a sync failed.
    Io {
        /// The file or dir that the step failed on.
        path: PathBuf,
        /// Why the step failed.
        source: io::Error,
    },
    /// An identity file does not hold an identity.
    Parse {
        /// The identity file.
        path: PathBuf,
        /// Why the text is not an identity.
        source: serde_json::Error,
    },
    /// The bytes have another SHA-256 than the expected one.
    Mismatch {
        /// The digest that the caller named.
        expected: ImageDigest,
        /// The digest of the bytes.
        actual: ImageDigest,
    },
    /// `zstd -d` ended with a failure.
    Decompress {
        /// The exit status of `zstd`.
        status: ExitStatus,
        /// What `zstd` wrote to stderr.
        stderr: String,
    },
}

impl fmt::Display for ImportError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Io { path, .. } => write!(formatter, "cannot import at {}", path.display()),
            Self::Parse { path, .. } => write!(formatter, "{} holds no identity", path.display()),
            Self::Mismatch { expected, actual } => {
                write!(formatter, "the SHA-256 is {actual}, not {expected}")
            }
            Self::Decompress { status, stderr } => {
                write!(formatter, "zstd -d ended with {status}: {}", stderr.trim())
            }
        }
    }
}

impl std::error::Error for ImportError {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match self {
            Self::Io { source, .. } => Some(source),
            Self::Parse { source, .. } => Some(source),
            Self::Mismatch { .. } | Self::Decompress { .. } => None,
        }
    }
}

/// Maps an `io::Error` on `path` to an `ImportError`.
fn io_at(path: &Path) -> impl FnOnce(io::Error) -> ImportError {
    let path = path.to_owned();
    move |source| ImportError::Io { path, source }
}

/// The mode of a store copy with its file type bits: a regular file with
/// mode 0444 (gap G8).
const STORE_FILE_MODE: u32 = 0o100_000 | STORE_MODE;

/// A store copy that passed the check at VM start (decision CH8-8 as read
/// by CH36 with gap G8). Only `ImageStore::check` makes one, so only a
/// checked copy can become a `--disk` or `--kernel` argument (PR C7).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CheckedImage {
    digest: ImageDigest,
    path: PathBuf,
}

impl CheckedImage {
    /// The SHA-256 of the bytes. A snapshot key holds it (PR D10).
    #[must_use]
    pub fn digest(&self) -> ImageDigest {
        self.digest
    }

    /// The store copy.
    #[must_use]
    pub fn path(&self) -> &Path {
        &self.path
    }
}

/// Why a store copy did not pass the check at VM start.
#[derive(Debug)]
pub enum CheckError {
    /// A read or an open failed. A copy that was never imported gives
    /// `NotFound` on its identity file.
    Io {
        /// The file that the step failed on.
        path: PathBuf,
        /// Why the step failed.
        source: io::Error,
    },
    /// The identity file does not hold an identity.
    Parse {
        /// The identity file.
        path: PathBuf,
        /// Why the text is not an identity.
        source: serde_json::Error,
    },
    /// The store path holds no regular file with mode 0444.
    BadMode {
        /// The store copy.
        path: PathBuf,
        /// The mode found, with the file type bits.
        mode: u32,
    },
    /// The copy has another identity than the import recorded.
    Changed {
        /// The store copy.
        path: PathBuf,
        /// The identity that the import recorded. A box keeps the error
        /// small.
        recorded: Box<FileIdentity>,
        /// The identity of the copy now.
        found: Box<FileIdentity>,
    },
}

impl fmt::Display for CheckError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Io { path, .. } => write!(formatter, "cannot check {}", path.display()),
            Self::Parse { path, .. } => write!(formatter, "{} holds no identity", path.display()),
            Self::BadMode { path, mode } => write!(
                formatter,
                "{} has mode {mode:o}, not a regular file with mode 444",
                path.display()
            ),
            Self::Changed { path, .. } => {
                write!(formatter, "{} changed after its import", path.display())
            }
        }
    }
}

impl std::error::Error for CheckError {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match self {
            Self::Io { source, .. } => Some(source),
            Self::Parse { source, .. } => Some(source),
            Self::BadMode { .. } | Self::Changed { .. } => None,
        }
    }
}

/// Maps an `io::Error` on `path` to a `CheckError`.
fn check_io_at(path: &Path) -> impl FnOnce(io::Error) -> CheckError {
    let path = path.to_owned();
    move |source| CheckError::Io { path, source }
}

// Decision CH41 rule 1: for a `.zst` source the vmm checks the SHA-256 of
// the decompressed bytes, the bytes that Cloud Hypervisor reads, and that
// digest names the store copy. Root checks the signature of the `.zst` file
// (decision CH35 rule 4).

/// The digest store under the state root.
#[derive(Debug, Clone)]
pub struct ImageStore {
    dir: PathBuf,
    zstd: PathBuf,
}

impl ImageStore {
    /// The store of `root`. `zstd` is the program that decompresses a
    /// `.zst` source. The vmm config passes it in (gap G9).
    #[must_use]
    pub fn new(root: &StateRoot, zstd: PathBuf) -> Self {
        Self {
            dir: root.store_dir(),
            zstd,
        }
    }

    /// The store copy of `digest`.
    #[must_use]
    pub fn path(&self, digest: &ImageDigest) -> PathBuf {
        self.dir.join(digest.to_string())
    }

    /// The identity file of the store copy of `digest`.
    #[must_use]
    pub fn identity_path(&self, digest: &ImageDigest) -> PathBuf {
        self.dir.join(format!("{digest}.identity"))
    }

    /// Copies `source` into the store as `expected`. A source whose name
    /// ends in `.zst` goes through `zstd -d`. When the store holds
    /// `expected` with its identity, the import reads no byte and returns
    /// it. The vmm imports one file at a time, so two imports never share
    /// a temp file.
    ///
    /// # Errors
    ///
    /// `Mismatch` when the bytes have another SHA-256, `Decompress` when
    /// `zstd` fails, `Io` or `Parse` on a failed step. The store then holds
    /// no copy of `expected`.
    pub fn import(&self, source: &Path, expected: ImageDigest) -> Result<StoredImage, ImportError> {
        let path = self.path(&expected);
        let identity_path = self.identity_path(&expected);
        match fs::read(&identity_path) {
            Ok(text) => {
                let identity =
                    serde_json::from_slice(&text).map_err(|source| ImportError::Parse {
                        path: identity_path,
                        source,
                    })?;
                return Ok(StoredImage {
                    digest: expected,
                    path,
                    identity,
                });
            }
            Err(error) if error.kind() == io::ErrorKind::NotFound => {}
            Err(error) => return Err(io_at(&identity_path)(error)),
        }

        fs::create_dir_all(&self.dir).map_err(io_at(&self.dir))?;
        let temp = self.dir.join(format!("{expected}.tmp"));
        let result = self.copy_into(source, &temp, expected, &path);
        if result.is_err() {
            let _ = fs::remove_file(&temp);
        }
        result?;

        let identity = FileIdentity::of(&fs::metadata(&path).map_err(io_at(&path))?);
        let text = serde_json::to_vec(&identity).map_err(|source| ImportError::Parse {
            path: identity_path.clone(),
            source,
        })?;
        let identity_temp = self.dir.join(format!("{expected}.identity.tmp"));
        write_synced(&identity_temp, &text).map_err(io_at(&identity_temp))?;
        fs::rename(&identity_temp, &identity_path).map_err(io_at(&identity_path))?;
        sync_dir(&self.dir).map_err(io_at(&self.dir))?;
        Ok(StoredImage {
            digest: expected,
            path,
            identity,
        })
    }

    /// Checks the store copy of `digest` at a VM start and hashes no byte
    /// (decision CH36 with gap G8, the reading of CH8-8). The check reads
    /// the identity that the import recorded. It refuses a store path that
    /// holds no regular file with mode 0444, so a link, a dir or a FIFO is
    /// never opened. Then it opens the copy read-only and compares device,
    /// inode, size, mtime, ctime and mode with the record. For a `.zst`
    /// source, `digest` is the SHA-256 of the decompressed bytes
    /// (decision CH41 rule 1).
    ///
    /// CONTRACT-QUESTION: no row decides what the vmm does with a copy
    /// that fails the check. `import` returns the recorded entry and does
    /// not copy again. The narrowest reading: the check removes and
    /// repairs nothing, and the start fails (`AGENTS.md`, Known gaps).
    ///
    /// # Errors
    ///
    /// `BadMode` for a wrong file type or mode, `Changed` for another
    /// identity, `Io` or `Parse` on a failed read.
    pub fn check(&self, digest: ImageDigest) -> Result<CheckedImage, CheckError> {
        let identity_path = self.identity_path(&digest);
        let text = fs::read(&identity_path).map_err(check_io_at(&identity_path))?;
        let recorded: FileIdentity =
            serde_json::from_slice(&text).map_err(|source| CheckError::Parse {
                path: identity_path,
                source,
            })?;

        let path = self.path(&digest);
        let mode = fs::symlink_metadata(&path)
            .map_err(check_io_at(&path))?
            .mode();
        if mode != STORE_FILE_MODE {
            return Err(CheckError::BadMode { path, mode });
        }
        let file = File::open(&path).map_err(check_io_at(&path))?;
        let found = FileIdentity::of(&file.metadata().map_err(check_io_at(&path))?);
        if found.mode != STORE_FILE_MODE {
            return Err(CheckError::BadMode {
                path,
                mode: found.mode,
            });
        }
        if found != recorded {
            return Err(CheckError::Changed {
                path,
                recorded: Box::new(recorded),
                found: Box::new(found),
            });
        }
        Ok(CheckedImage { digest, path })
    }

    /// Copies the bytes of `source` into `temp`, checks their digest, sets
    /// the mode and renames `temp` to `path`.
    fn copy_into(
        &self,
        source: &Path,
        temp: &Path,
        expected: ImageDigest,
        path: &Path,
    ) -> Result<(), ImportError> {
        let mut out = OpenOptions::new()
            .write(true)
            .create(true)
            .truncate(true)
            .mode(0o600)
            .open(temp)
            .map_err(io_at(temp))?;
        let actual = if source.extension().is_some_and(|ext| ext == "zst") {
            self.decompress(source, &mut out, temp)?
        } else {
            let mut input = File::open(source).map_err(io_at(source))?;
            hash_copy(&mut input, &mut out, source, temp)?
        };
        if actual != expected {
            return Err(ImportError::Mismatch { expected, actual });
        }
        out.sync_all().map_err(io_at(temp))?;
        drop(out);
        fs::set_permissions(temp, fs::Permissions::from_mode(STORE_MODE)).map_err(io_at(temp))?;
        fs::rename(temp, path).map_err(io_at(path))?;
        sync_dir(&self.dir).map_err(io_at(&self.dir))
    }

    /// Runs `zstd -d` on `source` and copies its output into `out`. The
    /// host zstd is v1.5.5 at `/usr/bin/zstd` (the G9 check of 2026-11-03,
    /// decision CH36). `-d -c -q` and `--` are in that version.
    fn decompress(
        &self,
        source: &Path,
        out: &mut File,
        temp: &Path,
    ) -> Result<ImageDigest, ImportError> {
        let mut child = Command::new(&self.zstd)
            .args(["-d", "-c", "-q", "--"])
            .arg(source)
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .map_err(io_at(&self.zstd))?;
        let copied = match child.stdout.take() {
            Some(mut stdout) => hash_copy(&mut stdout, out, source, temp),
            None => Err(io_at(&self.zstd)(io::ErrorKind::BrokenPipe.into())),
        };
        if copied.is_err() {
            let _ = child.kill();
        }
        let output = child.wait_with_output().map_err(io_at(&self.zstd))?;
        let digest = copied?;
        if !output.status.success() {
            return Err(ImportError::Decompress {
                status: output.status,
                stderr: String::from_utf8_lossy(&output.stderr).into_owned(),
            });
        }
        Ok(digest)
    }
}

/// Copies `input` into `out` and returns the SHA-256 of the bytes.
fn hash_copy(
    input: &mut impl Read,
    out: &mut File,
    from: &Path,
    to: &Path,
) -> Result<ImageDigest, ImportError> {
    let mut hasher = Sha256::new();
    let mut buffer = vec![0_u8; CHUNK];
    loop {
        let read = match input.read(&mut buffer) {
            Ok(0) => break,
            Ok(read) => read,
            Err(error) if error.kind() == io::ErrorKind::Interrupted => continue,
            Err(error) => return Err(io_at(from)(error)),
        };
        let chunk = buffer.get(..read).unwrap_or_default();
        hasher.update(chunk);
        out.write_all(chunk).map_err(io_at(to))?;
    }
    Ok(ImageDigest(hasher.finalize().into()))
}

/// Writes `text` to a new `path` and syncs it.
fn write_synced(path: &Path, text: &[u8]) -> io::Result<()> {
    let mut file = File::create(path)?;
    file.write_all(text)?;
    file.sync_all()
}

/// Syncs `dir`, so that a rename in it lasts across a crash.
fn sync_dir(dir: &Path) -> io::Result<()> {
    File::open(dir)?.sync_all()
}
