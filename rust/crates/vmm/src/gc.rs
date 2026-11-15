//! `vmm gc`, the snapshot and image retention of the vmm by hand. The
//! hourly sweep of `vmm serve` runs the same two passes (see "The sleep and
//! the sweep" in `AGENTS.md`), but its image collection waits for an empty
//! VM table. The operator runs gc to see, or to remove, what the passes
//! remove now.
//!
//! 1. It reads the env file of the vmm unit (`crate::env_file`) and
//!    `GcConfig` from it: the state root, the image root, the guest init
//!    program, the `zstd` of the store and the snapshot budget.
//! 2. The current images: the digests that `digests.json` names
//!    (`image_root::read`, with its root-owned check) and the digest of the
//!    guest init initramfs (`guest_init::initramfs_digest`, with the check
//!    of `write_initramfs`). A vmm start reads the same two.
//! 3. The dry run, the default: `retention::plan`, then
//!    `image_retention::plan`, which skips the key of each generation that
//!    the first plan lists. It removes nothing and takes no lock, so it
//!    may run beside `vmm serve`. A take, a claim or a drop of that vmm
//!    can then change what a later run removes.
//! 4. `--apply`: `StateRoot::try_lock`. While `vmm serve` runs, it holds
//!    the lock, and gc refuses. With the lock, `retention::sweep` with no
//!    generation in use, then `image_retention::collect`. A `vmm serve`
//!    that starts meanwhile waits for the lock at its start.
//!
//! It writes one line per item on stdout and one total line. `src/main.rs`
//! exits 0 when no step failed, 1 when a step failed, and 78 when gc
//! refused before any remove.

use std::collections::HashSet;
use std::fmt;
use std::io;
use std::path::{Path, PathBuf};
use std::time::SystemTime;

use creche_vmm_store::image_retention::{self, Collected, Failure as ImageFailure};
use creche_vmm_store::images::ImageStore;
use creche_vmm_store::key::CurrentImages;
use creche_vmm_store::layout::StateRoot;
use creche_vmm_store::retention::{self, InUse, Reason, Swept};

use crate::config::{ConfigError, GcConfig};
use crate::env_file;
use crate::guest_init::{GuestInitError, initramfs_digest};
use crate::image_root::{self, DigestsError};

/// What gc does with each item that a pass names.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Act {
    /// The dry run: list each item and remove nothing.
    List,
    /// Take the state root lock and remove each item.
    Remove,
}

/// Why gc refused before any remove.
#[derive(Debug)]
pub enum GcError {
    /// The env file does not read.
    EnvFile(String),
    /// A key of `GcConfig` is missing or bad.
    Config(ConfigError),
    /// `digests.json` does not read or fails its check.
    Digests(DigestsError),
    /// The guest init program fails its check or does not pack.
    GuestInit(GuestInitError),
    /// Another process holds the state root lock: a `vmm serve` runs.
    Busy(PathBuf),
    /// The state root lock failed.
    Lock(PathBuf, io::Error),
}

impl fmt::Display for GcError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::EnvFile(error) => write!(formatter, "{error}"),
            Self::Config(error) => write!(formatter, "{error}"),
            Self::Digests(error) => write!(formatter, "{error}"),
            Self::GuestInit(error) => write!(formatter, "{error}"),
            Self::Busy(path) => write!(
                formatter,
                "another process holds the lock of {}: stop vmm serve before gc --apply",
                path.display()
            ),
            Self::Lock(path, error) => {
                write!(formatter, "cannot lock {}: {error}", path.display())
            }
        }
    }
}

impl std::error::Error for GcError {}

/// What one run listed or removed.
#[derive(Debug)]
pub struct Outcome {
    act: Act,
    swept: Swept,
    /// The text of the refusal when the image scan failed.
    collected: Result<Collected, String>,
    /// The sweep refused: `sessions/` does not read.
    sweep_error: Option<String>,
}

impl Outcome {
    /// True when a step failed.
    #[must_use]
    pub fn failed(&self) -> bool {
        self.sweep_error.is_some()
            || !self.swept.failures.is_empty()
            || self
                .collected
                .as_ref()
                .map_or(true, |collected| !collected.failures.is_empty())
    }

    /// The listed or removed snapshot generations.
    #[must_use]
    pub fn swept(&self) -> &Swept {
        &self.swept
    }

    /// The listed or removed store files, or the refusal of the image pass.
    pub fn collected(&self) -> Result<&Collected, &str> {
        self.collected.as_ref().map_err(String::as_str)
    }

    /// The allocated bytes of every listed or removed item.
    #[must_use]
    pub fn bytes(&self) -> u64 {
        let snapshots = self
            .swept
            .dropped
            .iter()
            .fold(0_u64, |sum, each| sum.saturating_add(each.allocated_bytes));
        let images = self
            .collected
            .as_ref()
            .map_or(0, |collected| collected.reclaimed_bytes);
        snapshots.saturating_add(images)
    }

    /// One line per item, then the total line.
    #[must_use]
    pub fn lines(&self) -> Vec<String> {
        let verb = match self.act {
            Act::List => "would remove",
            Act::Remove => "removed",
        };
        let mut lines = vec![match self.act {
            Act::List => "vmm gc: dry run, nothing is removed".to_owned(),
            Act::Remove => "vmm gc: removing".to_owned(),
        }];
        if let Some(error) = &self.sweep_error {
            lines.push(format!("failed: the snapshot sweep: {error}"));
        }
        for each in &self.swept.dropped {
            lines.push(format!(
                "{verb} snapshot {} generation {} ({}, {} bytes)",
                each.session,
                each.generation.0,
                reason(each.reason),
                each.allocated_bytes
            ));
        }
        for path in &self.swept.retired {
            lines.push(format!("{verb} retired {}", path.display()));
        }
        for failure in &self.swept.failures {
            lines.push(format!(
                "failed {}: {}",
                failure.path.display(),
                failure.source
            ));
        }
        match &self.collected {
            Err(error) => lines.push(format!("failed: the image pass: {error}")),
            Ok(collected) => {
                for removed in &collected.removed {
                    lines.push(format!(
                        "{verb} image {} ({} bytes)",
                        removed.digest, removed.allocated_bytes
                    ));
                }
                for leftover in &collected.leftovers {
                    lines.push(format!(
                        "{verb} leftover {} ({} bytes)",
                        leftover.path.display(),
                        leftover.allocated_bytes
                    ));
                }
                for failure in &collected.failures {
                    lines.push(match failure {
                        ImageFailure::Check { digest, source } => {
                            format!("failed image {digest}: {source}")
                        }
                        ImageFailure::Io { path, source } => {
                            format!("failed {}: {source}", path.display())
                        }
                    });
                }
            }
        }
        lines.push(match self.act {
            Act::List => format!(
                "would reclaim {} bytes: vmm gc --apply removes them",
                self.bytes()
            ),
            Act::Remove => format!("reclaimed {} bytes", self.bytes()),
        });
        lines
    }
}

/// The word of each reason in a line.
const fn reason(reason: Reason) -> &'static str {
    match reason {
        Reason::Superseded => "superseded",
        Reason::Consumed => "consumed",
        Reason::Stale => "stale",
        Reason::Aged => "aged",
        Reason::Budget => "budget",
    }
}

/// Runs the steps of the module doc with the env file at `env_file`.
/// Blocks.
///
/// # Errors
///
/// A `GcError` before any remove.
pub fn run(env_file: &Path, act: Act) -> Result<Outcome, GcError> {
    let env = env_file::read(env_file).map_err(|error| GcError::EnvFile(error.to_string()))?;
    let config = GcConfig::from_lookup(|key| env.lookup(key)).map_err(GcError::Config)?;
    let digests = image_root::read(&config.image_root)
        .map_err(GcError::Digests)?
        .digests();
    let initramfs = initramfs_digest(config.guest_init.path()).map_err(GcError::GuestInit)?;
    let store = ImageStore::new(&config.state, config.zstd.path().to_owned());
    collect(
        &config.state,
        &store,
        &digests.with_initramfs(initramfs),
        config.budget,
        act,
    )
}

/// Steps 3 and 4 of the module doc over `state` and `store`, with the
/// images of `current` and the snapshot `budget`. Blocks.
///
/// # Errors
///
/// `Busy` or `Lock` for `Act::Remove` when the lock is not taken.
pub fn collect(
    state: &StateRoot,
    store: &ImageStore,
    current: &CurrentImages,
    budget: u64,
    act: Act,
) -> Result<Outcome, GcError> {
    // No VM runs while gc holds the lock, and the dry run removes nothing,
    // so no generation is in use.
    let in_use = InUse::new();
    let now = SystemTime::now();
    let marked = current.digests();
    match act {
        Act::List => {
            let (swept, sweep_error) = split(retention::plan(state, now, budget, &in_use, current));
            let dropped: HashSet<_> = swept
                .dropped
                .iter()
                .map(|each| (each.session.clone(), each.generation))
                .collect();
            let collected = image_retention::plan(store, state, &marked, &dropped)
                .map_err(|error| scan_text(&error));
            Ok(Outcome {
                act,
                swept,
                collected,
                sweep_error,
            })
        }
        Act::Remove => {
            let lock = state
                .try_lock()
                .map_err(|error| GcError::Lock(state.path().to_owned(), error))?
                .ok_or_else(|| GcError::Busy(state.path().to_owned()))?;
            let (swept, sweep_error) =
                split(retention::sweep(state, now, budget, &in_use, current));
            let collected =
                image_retention::collect(store, state, &marked).map_err(|error| scan_text(&error));
            drop(lock);
            Ok(Outcome {
                act,
                swept,
                collected,
                sweep_error,
            })
        }
    }
}

/// The `Swept` of a pass, or an empty one with the error text.
fn split(swept: io::Result<Swept>) -> (Swept, Option<String>) {
    match swept {
        Ok(swept) => (swept, None),
        Err(error) => (Swept::default(), Some(error.to_string())),
    }
}

/// The text of a refused image scan with its cause.
fn scan_text(error: &image_retention::ScanError) -> String {
    format!("{error}: {}", error.source)
}
