//! The VFAT image of the seed: the low-level writes. The crate `fatfs`
//! formats the image and writes each file in the process of the daemon.
//! The host has `mkfs.vfat` and no `mcopy`, so no host tool puts a file
//! into the image, and only root can mount one.
//!
//! cloud-init NoCloud finds the seed by the label `CIDATA`. `fatfs` writes
//! the label twice, into the boot sector and as the volume entry of the
//! root directory. `blkid` reads the label of the volume entry.

use std::fs::File;
use std::io::{self, Seek, SeekFrom, Write};

use fatfs::{FileSystem, FormatVolumeOptions, FsOptions};

/// The label of the seed, padded with spaces to the 11 bytes of a FAT
/// label.
const LABEL: [u8; 11] = *b"CIDATA     ";

/// The size of the image: 1 MiB, a FAT12 volume. The three files of a
/// seed hold a few KiB.
pub(crate) const IMAGE_BYTES: u64 = 1024 * 1024;

/// Formats `file` as a VFAT image of `IMAGE_BYTES` with the label
/// `CIDATA`, and writes each of `files` into its root directory.
///
/// # Errors
///
/// The I/O error of the first write that failed. A message of `fatfs`
/// names the fault, never the bytes of a file.
pub(crate) fn write(file: &mut File, files: &[(&str, Vec<u8>)]) -> io::Result<()> {
    file.set_len(IMAGE_BYTES)?;
    file.seek(SeekFrom::Start(0))?;
    fatfs::format_volume(&mut *file, FormatVolumeOptions::new().volume_label(LABEL))?;

    let volume = FileSystem::new(&mut *file, FsOptions::new())?;
    {
        let root = volume.root_dir();
        for (name, bytes) in files {
            let mut entry = root.create_file(name)?;
            entry.write_all(bytes)?;
            entry.flush()?;
        }
    }
    volume.unmount()
}

#[cfg(test)]
mod tests {
    use std::fs::{self, File, OpenOptions};
    use std::io::Read;

    use fatfs::{FatType, FileSystem, FsOptions};

    use super::{IMAGE_BYTES, write};

    #[test]
    fn the_image_has_the_label_and_each_file_with_its_long_name() {
        let dir = std::env::temp_dir().join(format!("ci-runner-vfat-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        let path = dir.join("seed.img");

        let files = [
            ("meta-data", b"{}".to_vec()),
            ("network-config", vec![b'n'; 5000]),
            ("user-data", b"#cloud-config\n{}\n".to_vec()),
        ];
        let mut file = File::create_new(&path).unwrap();
        write(&mut file, &files).unwrap();
        drop(file);
        assert_eq!(fs::metadata(&path).unwrap().len(), IMAGE_BYTES);

        let mut image = OpenOptions::new()
            .read(true)
            .write(true)
            .open(&path)
            .unwrap();
        let volume = FileSystem::new(&mut image, FsOptions::new()).unwrap();
        assert_eq!(volume.fat_type(), FatType::Fat12);
        assert_eq!(
            volume.read_volume_label_from_root_dir().unwrap().unwrap(),
            "CIDATA"
        );
        assert_eq!(volume.volume_label(), "CIDATA");

        let root = volume.root_dir();
        let names: Vec<String> = root
            .iter()
            .map(|entry| entry.unwrap())
            .filter(|entry| {
                !entry
                    .attributes()
                    .contains(fatfs::FileAttributes::VOLUME_ID)
            })
            .map(|entry| entry.file_name())
            .collect();
        assert_eq!(names, ["meta-data", "network-config", "user-data"]);
        for (name, bytes) in &files {
            let mut got = Vec::new();
            root.open_file(name).unwrap().read_to_end(&mut got).unwrap();
            assert_eq!(&got, bytes, "{name}");
        }
        drop(root);
        volume.unmount().unwrap();
        fs::remove_dir_all(dir).unwrap();
    }
}
