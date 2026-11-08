//! The image root of the vmm (decision CH57 rule 1): the dir of
//! `VMM_IMAGE_ROOT`, where the disk row of phase 5 installs the root disks,
//! the kernel and `digests.json`.
//!
//! `digests.json` holds the decompressed digest of each file (decision CH41
//! rule 1). Handover checks the release signature at install, so the vmm
//! trusts the file only when root owns it and the vmm user cannot write
//! it. A missing, malformed or writable file refuses the start of the vmm.
//! `read` opens the file once with `root_owned::open`, which runs each
//! check on the open file, so the bytes that it parses are the bytes that
//! it checked.
//!
//! `digests.json` holds `kernel` plus one entry for each value of the wire
//! flavor enum `SandboxFlavor`, each `{"file": <one file name in the image
//! root>, "sha256": <64 lowercase hex characters>}`, and no other key
//! (decision CH58 rule 4). The key list comes from the enum
//! (`crate::flavors`), so a new flavor needs no vmm change. The file name
//! lets the disk row of phase 5 name its files, and the vmm compiles in
//! no name.
//!
//! The compose step then imports each named file into the digest store of
//! the state root (`ImageStore::import`, through `follow::Follow::start`)
//! and gives the `Digests` of `StartConfig`. An import reads no byte when the store already holds the
//! digest with its identity. An entry that does not import refuses the
//! start of the vmm (decision CH58 rule 5).
//!
//! `vmm check-images`, the verify hook of the disks row, reads the file
//! with the same `read` and runs `ImageSet::check` (decision CH76 rule 6).
//! The check hashes each named file as the import reads it and writes no
//! file, so a bad release fails its hook.
//!
//! A disks release changes the file while the vmm runs. `follow` reads it
//! every 5 s with the same checks, imports each new entry and then swaps
//! the `Digests` of each new start (decision CH76 rule 8). handover runs
//! the hook after it swaps the tree, so the follow may import and swap
//! to the new set while the hook runs. Each import checks each digest
//! itself (decision CH58 rule 5). A failed hook restores the previous
//! tree, and the follow then swaps back to its set. CONTRACT-QUESTION
//! (vmm AGENTS.md Known gaps 33): no row says whether the follow must
//! wait for the end of the hook.

use std::fmt;
use std::io::{self, Read};
use std::path::{Component, Path, PathBuf};

use creche_vmm_store::images::{ImageDigest, ImageStore, ImportError, hash_source};
use creche_wire::{PerFlavor, SandboxFlavor};
use serde::Deserialize;
use serde::de::value::StrDeserializer;
use serde::de::{Deserializer, Error as _, MapAccess, Visitor};

use crate::root_owned::{self, OwnerError};
use crate::start::Digests;

/// The name of the digest file in the image root (decision CH57 rule 1).
pub const DIGESTS_FILE: &str = "digests.json";

/// The key of the kernel entry in `digests.json` (decision CH58 rule 4).
pub const KERNEL_KEY: &str = "kernel";

/// The most bytes of `digests.json`. No row sets it. The kernel and a few
/// flavors take less than 1 KiB, so a larger file is no file of the disk
/// row.
pub const DIGESTS_CAP: u64 = 64 * 1024;

/// Why the vmm does not trust `digests.json`. Each refuses the start.
#[derive(Debug)]
pub enum DigestsError {
    /// The image root has no `digests.json`.
    Missing,
    /// The file failed a check of `root_owned::open`, or its read failed.
    Owner(OwnerError),
    /// The file has more than `DIGESTS_CAP` bytes.
    TooLarge,
    /// The bytes are not the strict JSON shape of the module doc.
    Malformed(serde_json::Error),
}

impl fmt::Display for DigestsError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(formatter, "VMM_IMAGE_ROOT: the {DIGESTS_FILE} ")?;
        match self {
            Self::Missing => formatter.write_str("is missing"),
            Self::Owner(error) => write!(formatter, "{error}"),
            Self::TooLarge => write!(formatter, "has more than {DIGESTS_CAP} bytes"),
            Self::Malformed(error) => write!(formatter, "is malformed: {error}"),
        }
    }
}

impl std::error::Error for DigestsError {}

/// An import that failed. It names the file in the image root.
#[derive(Debug)]
pub struct ImageImportError {
    /// The file in the image root.
    pub file: String,
    /// Why the import failed.
    pub source: ImportError,
}

impl fmt::Display for ImageImportError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            formatter,
            "VMM_IMAGE_ROOT: cannot import {}: {}",
            self.file, self.source
        )
    }
}

impl std::error::Error for ImageImportError {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        Some(&self.source)
    }
}

/// The digest check of one entry of `digests.json` (decision CH76 rule
/// 6). `ImageSet::check` gives one for each entry.
#[derive(Debug)]
pub struct EntryCheck {
    /// `kernel` or the wire name of a flavor.
    pub key: &'static str,
    /// The file in the image root.
    pub file: String,
    /// `Ok` when the bytes have the digest of the entry. Else `Mismatch`,
    /// `Decompress` or `Io`.
    pub result: Result<(), ImportError>,
}

/// One file name in the image root: one normal path component.
#[derive(Debug, Clone, PartialEq, Eq, Deserialize)]
#[serde(try_from = "String")]
struct FileName(String);

impl TryFrom<String> for FileName {
    type Error = &'static str;

    fn try_from(name: String) -> Result<Self, Self::Error> {
        let mut parts = Path::new(&name).components();
        match (parts.next(), parts.next()) {
            (Some(Component::Normal(part)), None) if part == name.as_str() => Ok(Self(name)),
            _ => Err("a file is one file name in the image root"),
        }
    }
}

/// One digest of the file, in the text form of `ImageDigest`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize)]
#[serde(try_from = "String")]
struct Sha256(ImageDigest);

impl TryFrom<String> for Sha256 {
    type Error = &'static str;

    fn try_from(text: String) -> Result<Self, Self::Error> {
        text.parse()
            .map(Self)
            .map_err(|_| "a sha256 is 64 lowercase hex characters")
    }
}

/// One entry of `digests.json`.
#[derive(Debug, Clone, PartialEq, Eq, Deserialize)]
#[serde(deny_unknown_fields)]
struct Entry {
    file: FileName,
    sha256: Sha256,
}

/// The whole `digests.json`: the kernel and one root disk for each flavor
/// (decision CH58 rule 4).
#[derive(Debug, Clone, PartialEq, Eq)]
struct Manifest {
    kernel: Entry,
    roots: PerFlavor<Entry>,
}

impl<'de> Deserialize<'de> for Manifest {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        deserializer.deserialize_map(ManifestVisitor)
    }
}

/// Reads the keys of `digests.json`: `kernel` once, and each other key
/// once as the wire name of a flavor.
struct ManifestVisitor;

impl<'de> Visitor<'de> for ManifestVisitor {
    type Value = Manifest;

    fn expecting(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str("an object of `kernel` and one entry for each flavor")
    }

    fn visit_map<A: MapAccess<'de>>(self, mut map: A) -> Result<Manifest, A::Error> {
        let mut kernel = None;
        let mut roots = PerFlavor::every(|_| None);
        while let Some(key) = map.next_key::<String>()? {
            if key == KERNEL_KEY {
                if kernel.is_some() {
                    return Err(A::Error::duplicate_field(KERNEL_KEY));
                }
                kernel = Some(map.next_value()?);
                continue;
            }
            let flavor = SandboxFlavor::deserialize(StrDeserializer::<A::Error>::new(&key))
                .map_err(|_: A::Error| {
                    A::Error::custom(format_args!(
                        "unknown key `{key}`: a key is `{KERNEL_KEY}` or a flavor"
                    ))
                })?;
            let slot = roots.get_mut(flavor);
            if slot.is_some() {
                return Err(A::Error::custom(format_args!(
                    "two entries for the flavor `{key}`"
                )));
            }
            *slot = Some(map.next_value()?);
        }
        let kernel = kernel.ok_or_else(|| A::Error::missing_field(KERNEL_KEY))?;
        let roots = PerFlavor::try_every(|flavor| {
            roots.get_mut(flavor).take().ok_or_else(|| {
                A::Error::custom(format_args!("no entry for the flavor `{}`", flavor.name()))
            })
        })?;
        Ok(Manifest { kernel, roots })
    }
}

/// The files that a trusted `digests.json` names. Only `read` and `parse`
/// make one.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ImageSet {
    root: PathBuf,
    manifest: Manifest,
    /// The bytes that `manifest` parsed from. `follow` compares each new
    /// reading with them (decision CH76 rule 8).
    bytes: Vec<u8>,
}

/// Reads `digests.json` in `root` after the checks of `root_owned::open`:
/// root owns it and no group or other user may write it (decision CH57
/// rule 1).
///
/// # Errors
///
/// A `DigestsError`. Each one refuses the start of the vmm.
pub fn read(root: &Path) -> Result<ImageSet, DigestsError> {
    parse(root, read_bytes(root)?)
}

/// The image set of `bytes` in `root`, with the strict parse of the
/// module doc. The caller read `bytes` with `read_bytes`, so root owns
/// their file. Only `read`, `follow` and the tests of the crate call it.
///
/// # Errors
///
/// `DigestsError::Malformed`.
pub(crate) fn parse(root: &Path, bytes: Vec<u8>) -> Result<ImageSet, DigestsError> {
    let manifest = serde_json::from_slice(&bytes).map_err(DigestsError::Malformed)?;
    Ok(ImageSet {
        root: root.to_owned(),
        manifest,
        bytes,
    })
}

/// The bytes of `digests.json` in `root` after the checks of `read`, at
/// most `DIGESTS_CAP`.
fn read_bytes(root: &Path) -> Result<Vec<u8>, DigestsError> {
    let file = root_owned::open(&root.join(DIGESTS_FILE)).map_err(|fault| match fault {
        OwnerError::Io(error) if error.kind() == io::ErrorKind::NotFound => DigestsError::Missing,
        fault => DigestsError::Owner(fault),
    })?;
    let mut bytes = Vec::new();
    file.take(DIGESTS_CAP + 1)
        .read_to_end(&mut bytes)
        .map_err(|error| DigestsError::Owner(OwnerError::Io(error)))?;
    if u64::try_from(bytes.len()).unwrap_or(u64::MAX) > DIGESTS_CAP {
        return Err(DigestsError::TooLarge);
    }
    Ok(bytes)
}

impl ImageSet {
    /// Imports the kernel and each root disk into `store`, one file at a
    /// time, and gives their digests. Blocks.
    ///
    /// # Errors
    ///
    /// The first import that failed. It refuses the start of the vmm
    /// (decision CH58 rule 5).
    pub fn import(&self, store: &ImageStore) -> Result<Digests, ImageImportError> {
        let import = |entry: &Entry| {
            let source = self.root.join(&entry.file.0);
            store
                .import(&source, entry.sha256.0)
                .map(|stored| stored.digest)
                .map_err(|source| ImageImportError {
                    file: entry.file.0.clone(),
                    source,
                })
        };
        Ok(Digests {
            kernel: import(&self.manifest.kernel)?,
            roots: self.manifest.roots.try_map(|_, entry| import(entry))?,
        })
    }

    /// Hashes the kernel and each root disk with `hash_source` of the
    /// store, one file at a time, and compares each digest with its entry
    /// (decision CH76 rule 6). `zstd` decompresses a `.zst` file. It
    /// writes no file and checks every entry, also after a failed one.
    /// Blocks.
    #[must_use]
    pub fn check(&self, zstd: &Path) -> Vec<EntryCheck> {
        let check = |key, entry: &Entry| {
            let result = hash_source(zstd, &self.root.join(&entry.file.0)).and_then(|actual| {
                let expected = entry.sha256.0;
                if actual == expected {
                    Ok(())
                } else {
                    Err(ImportError::Mismatch { expected, actual })
                }
            });
            EntryCheck {
                key,
                file: entry.file.0.clone(),
                result,
            }
        };
        std::iter::once(check(KERNEL_KEY, &self.manifest.kernel))
            .chain(
                self.manifest
                    .roots
                    .iter()
                    .map(|(flavor, entry)| check(flavor.name(), entry)),
            )
            .collect()
    }
}

pub mod follow;

#[cfg(test)]
mod tests;
