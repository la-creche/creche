//! The three disks of one job VM, in the run folder of the job (design
//! section 4.4).
//!
//! ```text
//!   <runs>/<job handle>/       mode 0700, one folder per job
//!     seed.img    the seed: VFAT, label CIDATA, read only in the VM
//!     data.raw    the data disk: a sparse raw file with no file system
//!     root.qcow2  the root disk: a whole copy of the released image
//! ```
//!
//! 1. The seed holds the files of `seed.rs`. `vfat.rs` writes it.
//! 2. The data disk is a sparse file of a typed size. The guest formats
//!    it at its first boot (`x-systemd.makefs` in `ci-runner-image/fstab`)
//!    and finds it by the serial `DATA_DISK_SERIAL`. Each job gets a new
//!    file, so each boot is a first boot.
//! 3. The host has no `qemu-img`, so it cannot make a qcow2 overlay. The
//!    root disk is a whole copy. The copy is a reflink (`FICLONE`) where
//!    the file system allows one, and else a sparse copy: only the data
//!    ranges of `SEEK_DATA` and `SEEK_HOLE`.
//!
//! `JobDisks` owns the folder. Its drop removes the folder, and
//! `JobDisks::remove` removes it and reports the fault. A write that
//! fails removes the folder too, so no half-written folder stays.
//!
//! Each file has the mode 0600, so only the daemon user reads the seed and
//! its runner token. A path names the job handle, never the token.

use std::fmt;
use std::fs::{self, DirBuilder, File, OpenOptions};
use std::io;
use std::os::unix::fs::{DirBuilderExt, FileExt, OpenOptionsExt};
use std::path::{Path, PathBuf};

use rustix::fs::SeekFrom;
use rustix::io::Errno;

use crate::seed::JobSeed;
use crate::vfat;

/// The virtio serial of the data disk. The guest finds the disk as
/// `/dev/disk/by-id/virtio-ci-data` (`ci-runner-image/fstab`). The
/// `--disk` argument of the data disk gives `serial=` this value.
pub const DATA_DISK_SERIAL: &str = "ci-data";

/// The name of the root disk in the run folder.
const ROOT_NAME: &str = "root.qcow2";

/// The name of the data disk in the run folder.
const DATA_NAME: &str = "data.raw";

/// The name of the seed in the run folder.
const SEED_NAME: &str = "seed.img";

/// The mode of the run folder: the daemon user only.
const FOLDER_MODE: u32 = 0o700;

/// The mode of each disk: the daemon user only. The seed holds a runner
/// token.
const DISK_MODE: u32 = 0o600;

/// The first four bytes of a qcow2 file: `QFI` and 0xfb.
const QCOW2_MAGIC: [u8; 4] = *b"QFI\xfb";

/// The bytes of one read and one write of the sparse copy.
const COPY_CHUNK: usize = 1024 * 1024;

/// The bytes of one GiB.
const GIB: u64 = 1024 * 1024 * 1024;

/// The largest data disk, in GiB. Design section 4.4 gives each job
/// 40 GiB. The cap keeps a config typo from a file the size of the host
/// disk: the file is sparse, but a job can fill it.
const DATA_GIB_MAX: u32 = 1024;

/// One step of the disks of a job.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DiskStep {
    /// The creation of the run folder.
    Folder,
    /// The write of the seed.
    Seed,
    /// The creation of the data disk.
    Data,
    /// The open of the released root disk.
    OpenRoot,
    /// The copy of the root disk into the run folder.
    CopyRoot,
    /// The removal of the run folder.
    Remove,
}

impl fmt::Display for DiskStep {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(match self {
            Self::Folder => "the creation of the run folder",
            Self::Seed => "the write of the seed",
            Self::Data => "the creation of the data disk",
            Self::OpenRoot => "the open of the released root disk",
            Self::CopyRoot => "the copy of the root disk",
            Self::Remove => "the removal of the run folder",
        })
    }
}

/// Why the disks of a job failed. An error names a path and the step,
/// never a byte of the seed.
#[derive(Debug)]
pub enum DiskError {
    /// A file operation failed.
    Io {
        /// The step that failed.
        step: DiskStep,
        /// The file or the folder of the step.
        path: PathBuf,
        /// The fault of the operation.
        error: io::Error,
    },
    /// The released root disk is not a regular file.
    RootNotFile(PathBuf),
    /// The released root disk does not start with the qcow2 magic.
    RootNotQcow2(PathBuf),
}

impl fmt::Display for DiskError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Io { step, path, error } => {
                write!(formatter, "{step} failed at {}: {error}", path.display())
            }
            Self::RootNotFile(path) => {
                write!(
                    formatter,
                    "the root disk {} is not a regular file",
                    path.display()
                )
            }
            Self::RootNotQcow2(path) => {
                write!(
                    formatter,
                    "the root disk {} is not a qcow2 file",
                    path.display()
                )
            }
        }
    }
}

impl std::error::Error for DiskError {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match self {
            Self::Io { error, .. } => Some(error),
            _ => None,
        }
    }
}

/// The fault of `step` at `path`, for `map_err`.
fn fault(step: DiskStep, path: &Path) -> impl FnOnce(io::Error) -> DiskError + '_ {
    move |error| DiskError::Io {
        step,
        path: path.to_path_buf(),
        error,
    }
}

/// Why a size is not a data disk size.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct DataSizeError(u32);

impl fmt::Display for DataSizeError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            formatter,
            "a data disk has 1 to {DATA_GIB_MAX} GiB, not {} GiB",
            self.0
        )
    }
}

impl std::error::Error for DataSizeError {}

/// The size of the data disk of each job: 1 to `DATA_GIB_MAX` GiB.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct DataDiskSize(u32);

impl DataDiskSize {
    /// A data disk of `gib` GiB.
    ///
    /// # Errors
    ///
    /// `DataSizeError` when `gib` is 0 or more than `DATA_GIB_MAX`.
    pub fn from_gib(gib: u32) -> Result<Self, DataSizeError> {
        if gib == 0 || gib > DATA_GIB_MAX {
            return Err(DataSizeError(gib));
        }
        Ok(Self(gib))
    }

    /// The size in bytes.
    #[must_use]
    pub fn bytes(self) -> u64 {
        u64::from(self.0) * GIB
    }
}

/// The released root disk: the `root.qcow2` of the image tree. Each job
/// gets a whole copy of it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RootImage(PathBuf);

impl RootImage {
    /// The root disk at `path`. Each copy checks that the file is a
    /// regular qcow2 file.
    #[must_use]
    pub fn new(path: PathBuf) -> Self {
        Self(path)
    }

    /// The path of the root disk.
    #[must_use]
    pub fn path(&self) -> &Path {
        &self.0
    }
}

/// How the root disk came into the run folder.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RootCopy {
    /// A reflink: the copy shares each block with the image until the VM
    /// writes it.
    Reflink,
    /// A sparse copy: each data range of the image, and holes for the
    /// rest.
    Sparse,
}

impl fmt::Display for RootCopy {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(match self {
            Self::Reflink => "reflink",
            Self::Sparse => "sparse",
        })
    }
}

/// The root disk of a job: the copy of the image in its run folder. Cloud
/// Hypervisor opens it with `image_type=qcow2`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RootDisk(PathBuf);

impl RootDisk {
    /// The path of the disk.
    #[must_use]
    pub fn path(&self) -> &Path {
        &self.0
    }
}

/// The data disk of a job: a sparse raw file with no file system. Cloud
/// Hypervisor opens it with `image_type=raw` and `serial=ci-data`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DataDisk(PathBuf);

impl DataDisk {
    /// The path of the disk.
    #[must_use]
    pub fn path(&self) -> &Path {
        &self.0
    }
}

/// The seed of a job: a VFAT image with the label `CIDATA`. Cloud
/// Hypervisor opens it with `image_type=raw` and `readonly=on`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SeedDisk(PathBuf);

impl SeedDisk {
    /// The path of the disk.
    #[must_use]
    pub fn path(&self) -> &Path {
        &self.0
    }
}

/// The run folder of one job. Its drop removes the folder and each file
/// in it, unless `remove` already did.
#[derive(Debug)]
struct RunFolder {
    /// The folder.
    path: PathBuf,
    /// Whether `remove` removed the folder.
    removed: bool,
}

impl RunFolder {
    /// Makes the folder `path`, with the mode 0700. A folder that exists
    /// is a fault: it belongs to another start of the same job.
    fn create(path: PathBuf) -> Result<Self, DiskError> {
        DirBuilder::new()
            .mode(FOLDER_MODE)
            .create(&path)
            .map_err(fault(DiskStep::Folder, &path))?;
        Ok(Self {
            path,
            removed: false,
        })
    }

    /// The path of the file `name` in the folder.
    fn file(&self, name: &str) -> PathBuf {
        self.path.join(name)
    }

    /// Removes the folder and each file in it.
    fn remove(&mut self) -> Result<(), DiskError> {
        fs::remove_dir_all(&self.path).map_err(fault(DiskStep::Remove, &self.path))?;
        self.removed = true;
        Ok(())
    }
}

impl Drop for RunFolder {
    fn drop(&mut self) {
        if self.removed {
            return;
        }
        // A drop cannot report a fault. `JobDisks::remove` reports one.
        let _ = fs::remove_dir_all(&self.path);
    }
}

/// The three disks of one job, in its run folder. Only `JobDisks::write`
/// makes one, after each disk is complete. The drop removes the folder.
#[derive(Debug)]
pub struct JobDisks {
    /// The run folder. Its drop removes it.
    folder: RunFolder,
    /// The copy of the released root disk.
    root: RootDisk,
    /// How the root disk was copied.
    root_copy: RootCopy,
    /// The sparse data disk.
    data: DataDisk,
    /// The VFAT seed.
    seed: SeedDisk,
}

impl JobDisks {
    /// Makes the run folder of the job of `seed` in `runs`, and writes
    /// the seed, a data disk of `data` and a copy of `root` into it.
    ///
    /// # Errors
    ///
    /// `DiskError` when a step fails. The folder is then gone.
    pub fn write(
        runs: &Path,
        seed: &JobSeed,
        root: &RootImage,
        data: DataDiskSize,
    ) -> Result<Self, DiskError> {
        let folder = RunFolder::create(runs.join(seed.handle().as_str()))?;

        let seed_path = folder.file(SEED_NAME);
        let mut seed_file = new_disk(&seed_path, DiskStep::Seed)?;
        vfat::write(&mut seed_file, &seed.files()).map_err(fault(DiskStep::Seed, &seed_path))?;

        let data_path = folder.file(DATA_NAME);
        new_disk(&data_path, DiskStep::Data)?
            .set_len(data.bytes())
            .map_err(fault(DiskStep::Data, &data_path))?;

        let root_path = folder.file(ROOT_NAME);
        let root_copy = copy_root(root, &root_path)?;

        Ok(Self {
            folder,
            root: RootDisk(root_path),
            root_copy,
            data: DataDisk(data_path),
            seed: SeedDisk(seed_path),
        })
    }

    /// The run folder.
    #[must_use]
    pub fn folder(&self) -> &Path {
        &self.folder.path
    }

    /// The root disk.
    #[must_use]
    pub fn root(&self) -> &RootDisk {
        &self.root
    }

    /// How the root disk was copied.
    #[must_use]
    pub fn root_copy(&self) -> RootCopy {
        self.root_copy
    }

    /// The data disk.
    #[must_use]
    pub fn data(&self) -> &DataDisk {
        &self.data
    }

    /// The seed.
    #[must_use]
    pub fn seed(&self) -> &SeedDisk {
        &self.seed
    }

    /// Removes the run folder and each file in it.
    ///
    /// # Errors
    ///
    /// `DiskError` with `DiskStep::Remove` when the removal fails. The
    /// drop of the value then tries once more.
    pub fn remove(mut self) -> Result<(), DiskError> {
        self.folder.remove()
    }
}

/// A new empty file at `path` with the mode 0600, for `step`.
fn new_disk(path: &Path, step: DiskStep) -> Result<File, DiskError> {
    OpenOptions::new()
        .read(true)
        .write(true)
        .create_new(true)
        .mode(DISK_MODE)
        .open(path)
        .map_err(fault(step, path))
}

/// Copies the released root disk to `target`: a reflink where the file
/// system allows one, else a sparse copy.
fn copy_root(root: &RootImage, target: &Path) -> Result<RootCopy, DiskError> {
    let source_path = root.path();
    let source = File::open(source_path).map_err(fault(DiskStep::OpenRoot, source_path))?;

    let metadata = source
        .metadata()
        .map_err(fault(DiskStep::OpenRoot, source_path))?;
    if !metadata.is_file() {
        return Err(DiskError::RootNotFile(source_path.to_path_buf()));
    }
    let mut magic = [0_u8; 4];
    match source.read_exact_at(&mut magic, 0) {
        Ok(()) if magic == QCOW2_MAGIC => {}
        Ok(()) => return Err(DiskError::RootNotQcow2(source_path.to_path_buf())),
        Err(error) if error.kind() == io::ErrorKind::UnexpectedEof => {
            return Err(DiskError::RootNotQcow2(source_path.to_path_buf()));
        }
        Err(error) => return Err(fault(DiskStep::OpenRoot, source_path)(error)),
    }

    let target_file = new_disk(target, DiskStep::CopyRoot)?;
    if reflink(&source, &target_file) {
        return Ok(RootCopy::Reflink);
    }
    sparse_copy(&source, &target_file, metadata.len())
        .map_err(fault(DiskStep::CopyRoot, target))?;
    Ok(RootCopy::Sparse)
}

/// Whether `FICLONE` made `target` share the blocks of `source`. A file
/// system with no reflink refuses it, and `target` then stays empty.
#[cfg(target_os = "linux")]
fn reflink(source: &File, target: &File) -> bool {
    rustix::fs::ioctl_ficlone(target, source).is_ok()
}

/// Only Linux has `FICLONE`.
#[cfg(not(target_os = "linux"))]
fn reflink(_source: &File, _target: &File) -> bool {
    false
}

/// Copies each data range of the first `len` bytes of `source` to the
/// same offset of the empty `target`, and gives `target` the length
/// `len`. The ranges in between stay holes.
fn sparse_copy(source: &File, target: &File, len: u64) -> io::Result<()> {
    let mut chunk = vec![0_u8; COPY_CHUNK];
    let mut offset = 0;

    while offset < len {
        // ENXIO: no data from `offset` to the end of the file.
        let start = match rustix::fs::seek(source, SeekFrom::Data(offset)) {
            Ok(start) => start,
            Err(Errno::NXIO) => break,
            Err(errno) => return Err(errno.into()),
        };
        let end = rustix::fs::seek(source, SeekFrom::Hole(start))?.min(len);
        copy_range(source, target, start, end, &mut chunk)?;
        offset = end;
    }

    target.set_len(len)
}

/// Copies the bytes `start..end` of `source` to the same offsets of
/// `target`, one `chunk` at a time.
fn copy_range(
    source: &File,
    target: &File,
    start: u64,
    end: u64,
    chunk: &mut [u8],
) -> io::Result<()> {
    let mut at = start;
    while at < end {
        let left = usize::try_from(end - at).unwrap_or(usize::MAX);
        let part = chunk
            .get_mut(..left.min(COPY_CHUNK))
            .ok_or(io::ErrorKind::InvalidInput)?;
        source.read_exact_at(part, at)?;
        target.write_all_at(part, at)?;
        at += u64::try_from(part.len()).map_err(|_| io::ErrorKind::InvalidInput)?;
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use std::fs::{self, File};
    use std::io::{Read, Write};
    use std::net::Ipv4Addr;
    use std::os::unix::fs::{FileExt, MetadataExt, PermissionsExt};
    use std::path::{Path, PathBuf};

    use fatfs::{FileSystem, FsOptions};
    use serde_json::json;

    use super::{
        DataDiskSize, DataSizeError, DiskError, DiskStep, JobDisks, QCOW2_MAGIC, RootCopy,
        RootImage, sparse_copy,
    };
    use crate::names::{Label, Labels};
    use crate::net::{BridgeNet, GuestNetwork};
    use crate::reply::Registration;
    use crate::seed::{EphemeralRunner, JobSeed};

    /// The token of the fake registration.
    const TOKEN: &str = "3f9a0c1e7b2d4f6a8c0e1b3d5f7a9c2e4b6d8f0a";

    /// The handle of the fake job, and so the name of its run folder.
    const HANDLE: &str = "2f923f44-2d31-4d08-ad46-e04a543826e0";

    /// A fresh dir of this test process under the temp dir.
    fn scratch(name: &str) -> PathBuf {
        let dir =
            std::env::temp_dir().join(format!("ci-runner-disks-{}-{name}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        dir
    }

    /// A seed of a fake registration.
    fn seed() -> JobSeed {
        let registration: Registration = serde_json::from_value(json!({
            "id": 3,
            "uuid": "a4087518-6f64-42b1-8383-c37ab78396af",
            "token": TOKEN,
        }))
        .unwrap();
        let runner = EphemeralRunner::new(
            "http://192.0.2.10:3000".parse().unwrap(),
            "ci-job-1".parse().unwrap(),
            Labels::new(["ubuntu-24.04".parse::<Label>().unwrap()]).unwrap(),
            registration,
        );
        let bridge = BridgeNet::new(Ipv4Addr::new(192, 0, 2, 1)).unwrap();
        let network = GuestNetwork::new(
            "52:54:00:c1:00:02".parse().unwrap(),
            bridge.guest(Ipv4Addr::new(192, 0, 2, 2)).unwrap(),
            Ipv4Addr::new(198, 51, 100, 53),
        )
        .unwrap();
        JobSeed::new(runner, HANDLE.parse().unwrap(), network)
    }

    /// A fake qcow2 image at `path`: the magic, a hole of 3 MiB, then a
    /// block of data, then a hole of 1 MiB at the end.
    fn image(path: &Path) -> Vec<u8> {
        let mut bytes = vec![0_u8; 5 * 1024 * 1024];
        bytes[..4].copy_from_slice(&QCOW2_MAGIC);
        bytes[3 * 1024 * 1024..4 * 1024 * 1024 - 7].fill(0xa5);

        let file = File::create_new(path).unwrap();
        file.write_all_at(&bytes[..4], 0).unwrap();
        file.write_all_at(&bytes[3 * 1024 * 1024..4 * 1024 * 1024], 3 * 1024 * 1024)
            .unwrap();
        file.set_len(5 * 1024 * 1024).unwrap();
        bytes
    }

    #[test]
    fn a_data_disk_has_1_to_1024_gib() {
        assert_eq!(DataDiskSize::from_gib(40).unwrap().bytes(), 40 << 30);
        assert_eq!(DataDiskSize::from_gib(1024).unwrap().bytes(), 1 << 40);
        assert_eq!(DataDiskSize::from_gib(0), Err(DataSizeError(0)));
        assert_eq!(DataDiskSize::from_gib(1025), Err(DataSizeError(1025)));
    }

    #[test]
    fn the_disks_of_a_job_land_in_its_run_folder_and_go_with_it() {
        let runs = scratch("write");
        let root = runs.join("root.qcow2");
        let bytes = image(&root);

        let disks = JobDisks::write(
            &runs,
            &seed(),
            &RootImage::new(root),
            DataDiskSize::from_gib(16).unwrap(),
        )
        .unwrap();
        let folder = runs.join(HANDLE);
        assert_eq!(disks.folder(), folder);
        assert_eq!(
            fs::metadata(&folder).unwrap().permissions().mode() & 0o777,
            0o700
        );
        assert_eq!(disks.root().path(), folder.join("root.qcow2"));
        assert_eq!(disks.data().path(), folder.join("data.raw"));
        assert_eq!(disks.seed().path(), folder.join("seed.img"));
        for path in [
            disks.root().path(),
            disks.data().path(),
            disks.seed().path(),
        ] {
            let mode = fs::metadata(path).unwrap().permissions().mode() & 0o777;
            assert_eq!(mode, 0o600, "{}", path.display());
            assert!(!path.to_string_lossy().contains(TOKEN));
        }

        // The temp dir of a test host has no reflink, or it has one: each
        // way the copy holds the bytes of the image.
        assert!(matches!(
            disks.root_copy(),
            RootCopy::Sparse | RootCopy::Reflink
        ));
        assert_eq!(fs::read(disks.root().path()).unwrap(), bytes);

        let data = fs::metadata(disks.data().path()).unwrap();
        assert_eq!(data.len(), 16 << 30);
        assert!(data.blocks() < 1024, "{} blocks", data.blocks());

        let mut seed = File::options()
            .read(true)
            .write(true)
            .open(disks.seed().path())
            .unwrap();
        let volume = FileSystem::new(&mut seed, FsOptions::new()).unwrap();
        assert_eq!(volume.volume_label(), "CIDATA");
        let mut user_data = String::new();
        volume
            .root_dir()
            .open_file("user-data")
            .unwrap()
            .read_to_string(&mut user_data)
            .unwrap();
        assert!(user_data.starts_with("#cloud-config\n"));
        assert!(user_data.contains(TOKEN));
        drop(volume);

        disks.remove().unwrap();
        assert!(!folder.exists());
        assert!(runs.join("root.qcow2").exists());
        fs::remove_dir_all(runs).unwrap();
    }

    #[test]
    fn the_drop_removes_the_run_folder() {
        let runs = scratch("drop");
        let root = runs.join("root.qcow2");
        image(&root);

        let disks = JobDisks::write(
            &runs,
            &seed(),
            &RootImage::new(root),
            DataDiskSize::from_gib(1).unwrap(),
        )
        .unwrap();
        let folder = disks.folder().to_path_buf();
        assert!(folder.exists());
        drop(disks);
        assert!(!folder.exists());
        fs::remove_dir_all(runs).unwrap();
    }

    #[test]
    fn a_root_disk_that_is_not_qcow2_leaves_no_run_folder() {
        let runs = scratch("refuse");
        let folder = runs.join(HANDLE);
        let size = DataDiskSize::from_gib(1).unwrap();

        let raw = runs.join("root.raw");
        File::create_new(&raw)
            .unwrap()
            .write_all(b"not a qcow2 disk")
            .unwrap();
        let short = runs.join("short");
        File::create_new(&short).unwrap().write_all(b"QF").unwrap();
        for path in [raw, short] {
            let error =
                JobDisks::write(&runs, &seed(), &RootImage::new(path.clone()), size).unwrap_err();
            assert!(
                matches!(&error, DiskError::RootNotQcow2(got) if *got == path),
                "{error}"
            );
            assert!(!folder.exists());
        }

        let error =
            JobDisks::write(&runs, &seed(), &RootImage::new(runs.clone()), size).unwrap_err();
        assert!(matches!(error, DiskError::RootNotFile(_)), "{error}");

        let error = JobDisks::write(&runs, &seed(), &RootImage::new(runs.join("absent")), size)
            .unwrap_err();
        assert!(
            matches!(
                error,
                DiskError::Io {
                    step: DiskStep::OpenRoot,
                    ..
                }
            ),
            "{error}"
        );
        assert!(!folder.exists());
        assert!(!error.to_string().contains(TOKEN));
        fs::remove_dir_all(runs).unwrap();
    }

    #[test]
    fn a_second_start_of_the_same_job_is_refused_and_keeps_the_first() {
        let runs = scratch("twice");
        let root = runs.join("root.qcow2");
        image(&root);
        let root = RootImage::new(root);
        let size = DataDiskSize::from_gib(1).unwrap();

        let first = JobDisks::write(&runs, &seed(), &root, size).unwrap();
        let error = JobDisks::write(&runs, &seed(), &root, size).unwrap_err();
        assert!(
            matches!(
                error,
                DiskError::Io {
                    step: DiskStep::Folder,
                    ..
                }
            ),
            "{error}"
        );
        assert!(first.seed().path().exists());
        first.remove().unwrap();
        fs::remove_dir_all(runs).unwrap();
    }

    #[test]
    fn a_sparse_copy_keeps_the_holes_and_the_length() {
        let dir = scratch("sparse");
        let source_path = dir.join("source");
        let bytes = image(&source_path);
        let target_path = dir.join("target");

        let source = File::open(&source_path).unwrap();
        let target = File::create_new(&target_path).unwrap();
        sparse_copy(&source, &target, 5 * 1024 * 1024).unwrap();
        drop(target);

        assert_eq!(fs::read(&target_path).unwrap(), bytes);
        let source_blocks = fs::metadata(&source_path).unwrap().blocks();
        let target_blocks = fs::metadata(&target_path).unwrap().blocks();
        assert!(
            target_blocks <= source_blocks,
            "{target_blocks} > {source_blocks}"
        );
        fs::remove_dir_all(dir).unwrap();
    }
}
