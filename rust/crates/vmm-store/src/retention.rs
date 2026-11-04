//! Retention of the memory snapshots (decisions CH2, CH8-7). A recent
//! session resumes from its snapshot. An older one cold-boots from its
//! session disk, which stays until a delete. Retention removes snapshot
//! dirs only, never a session disk.
//!
//! `sweep` reads every session under the state root and runs these steps:
//!
//! 1. Remove each leftover `snapshots/<n>.retired` dir. A failed claim
//!    makes one (decision CH44 rule 6), and no generation list counts it.
//! 2. Remove each generation that is not the newest of its session. Only
//!    the newest one may restore, because the session disk moved on past
//!    every older one (decision CH19 (5), `key.rs`).
//! 3. Remove the newest generation when a claim consumed it (decision
//!    CH50 rule 8). It never restores again (decision CH19 (5)). Also
//!    remove it when it is older than `MAX_AGE` (decision CH2).
//! 4. While the snapshots that stay take more than the budget, remove the
//!    oldest one first (decision CH8-7, fact 63).
//!
//! The budget counts allocated bytes (`st_blocks`), not the apparent size
//! (decision CH36 with gap G13 option A). After the dig of decision CH29 a
//! 2 GiB `memory-ranges` takes about 100 MB (fact 72).
//!
//! The age of a generation is the time since the last write of its
//! `memory-ranges` (decision CH50 rule 7). The take writes that file, and a restore never writes
//! it (fact 55). A generation without the file takes the time of its dir.
//! The dir time does not serve for all, because a claim adds the
//! `consumed` mark and so changes it.
//!
//! The caller names the generations in use. A VM restored from a snapshot
//! maps its files while it runs (fact 55), so `sweep` never removes a
//! generation in use. It also never removes a generation while an older
//! one of the same session stays, so an older generation never becomes the
//! newest and restores onto a session disk that moved on.

use std::cmp::Ordering;
use std::collections::HashSet;
use std::fs::{self, File};
use std::io;
use std::os::unix::fs::MetadataExt;
use std::path::{Path, PathBuf};
use std::time::{Duration, SystemTime};

use creche_wire::SessionId;

use crate::claim::CONSUMED;
use crate::key::remove_generation;
use crate::layout::{SessionDir, SnapshotGeneration, StateRoot};
use crate::snapshot::{MEMORY_RANGES, generations};

/// One GiB in bytes.
const GIB: u64 = 1 << 30;

/// The most age of a snapshot.
// Decision CH2: the first value of the retention window is 7 days.
pub const MAX_AGE: Duration = Duration::from_secs(7 * 24 * 60 * 60);

/// The design budget of the allocated bytes of all snapshots (decision
/// CH8-7, about 150 GiB). PR L20 makes it the default of an env key
/// (CH36 G14).
pub const BUDGET: u64 = 150 * GIB;

/// The generations in use: each one that a running VM restored from.
pub type InUse = HashSet<(SessionId, SnapshotGeneration)>;

/// Why `sweep` removed a generation.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Reason {
    /// A newer generation of the session exists (step 2).
    Superseded,
    /// The newest generation holds the consumed mark (step 3, decision
    /// CH50 rule 8).
    Consumed,
    /// The generation is older than `MAX_AGE` (step 3).
    Aged,
    /// The snapshots took more than the budget (step 4).
    Budget,
}

/// A generation that `sweep` removed.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Dropped {
    /// The session of the generation.
    pub session: SessionId,
    /// The removed generation.
    pub generation: SnapshotGeneration,
    /// Why `sweep` removed it.
    pub reason: Reason,
    /// The bytes that its files took on the host disk.
    pub allocated_bytes: u64,
}

/// A failed read or remove. `sweep` goes on with the next dir.
#[derive(Debug)]
pub struct Failure {
    /// The file or dir that the step failed on.
    pub path: PathBuf,
    /// Why the step failed.
    pub source: io::Error,
}

/// What one `sweep` did.
#[derive(Debug, Default)]
pub struct Swept {
    /// The removed generations, in the order of the removes.
    pub dropped: Vec<Dropped>,
    /// The removed `<n>.retired` dirs.
    pub retired: Vec<PathBuf>,
    /// The allocated bytes of the generations that stay.
    pub kept_bytes: u64,
    /// The failed steps.
    pub failures: Vec<Failure>,
}

/// A generation that stays after steps 1 to 3.
#[derive(Debug)]
struct Kept {
    id: SessionId,
    session: SessionDir,
    generation: SnapshotGeneration,
    taken: SystemTime,
    bytes: u64,
    /// True when step 4 may remove it: not in use, and no older
    /// generation of the session stays.
    may_drop: bool,
}

/// Runs the four steps over every session of `state`. `now` is the current
/// time, `budget` the most allocated bytes of all snapshots (`BUDGET` by
/// design). Run it while no take, key check or claim of a session runs, or
/// name the newest generation of that session in `in_use`.
///
/// # Errors
///
/// A failed read of `sessions/`. An absent dir gives an empty `Swept`.
/// Each later failure goes to `Swept::failures`, and the sweep goes on.
pub fn sweep(state: &StateRoot, now: SystemTime, budget: u64, in_use: &InUse) -> io::Result<Swept> {
    let mut swept = Swept::default();
    let mut kept = Vec::new();
    for id in sessions(state)? {
        let session = state.session(&id);
        remove_retired(&session, &mut swept);
        match generations(&session) {
            Ok(list) => first_pass(id, session, &list, now, in_use, &mut kept, &mut swept),
            Err(source) => swept.failures.push(Failure {
                path: session.snapshots(),
                source,
            }),
        }
    }

    // Step 4: the oldest first, then by session and generation, so the
    // order never depends on the order of `read_dir`.
    let mut total = kept
        .iter()
        .fold(0_u64, |sum, each| sum.saturating_add(each.bytes));
    let mut order: Vec<&Kept> = kept.iter().filter(|each| each.may_drop).collect();
    order.sort_by(|left, right| oldest_first(left, right));
    for each in order {
        if total <= budget {
            break;
        }
        if drop_one(each, Reason::Budget, &mut swept) {
            total = total.saturating_sub(each.bytes);
        }
    }
    swept.kept_bytes = total;
    Ok(swept)
}

/// Steps 2 and 3 over the generations of one session, oldest first.
fn first_pass(
    id: SessionId,
    session: SessionDir,
    list: &[SnapshotGeneration],
    now: SystemTime,
    in_use: &InUse,
    kept: &mut Vec<Kept>,
    swept: &mut Swept,
) {
    let newest = list.last().copied();
    let mut older_stays = false;
    for &generation in list {
        let dir = session.snapshot(generation);
        let Measured {
            taken,
            bytes,
            consumed,
        } = match measure(&dir) {
            Ok(found) => found,
            Err(source) => {
                swept.failures.push(Failure { path: dir, source });
                older_stays = true;
                continue;
            }
        };
        let used = in_use.contains(&(id.clone(), generation));
        // Decision CH50 rule 7: the 7 days of CH2 run from the take, so the
        // age runs from the last write of `memory-ranges`. A time after
        // `now` gives age 0.
        let age = now.duration_since(taken).unwrap_or(Duration::ZERO);
        // Decision CH50 rule 8: a consumed newest generation with no VM on
        // it drops at this sweep. Lifecycle names a generation in `in_use`
        // from the key check until its Cloud Hypervisor ends, so a restore
        // that starts keeps its files.
        let reason = if used || older_stays {
            None
        } else if Some(generation) != newest {
            Some(Reason::Superseded)
        } else if consumed {
            Some(Reason::Consumed)
        } else if age > MAX_AGE {
            Some(Reason::Aged)
        } else {
            None
        };
        let each = Kept {
            id: id.clone(),
            session: session.clone(),
            generation,
            taken,
            bytes,
            may_drop: !used && !older_stays,
        };
        let removed = reason.is_some_and(|reason| drop_one(&each, reason, swept));
        if !removed {
            older_stays = true;
            kept.push(Kept {
                may_drop: each.may_drop && reason.is_none(),
                ..each
            });
        }
    }
}

/// Removes one generation. True when the remove succeeded.
fn drop_one(each: &Kept, reason: Reason, swept: &mut Swept) -> bool {
    match remove_generation(&each.session, each.generation) {
        Ok(()) => {
            swept.dropped.push(Dropped {
                session: each.id.clone(),
                generation: each.generation,
                reason,
                allocated_bytes: each.bytes,
            });
            true
        }
        Err(error) => {
            swept.failures.push(Failure {
                path: error.path,
                source: error.source,
            });
            false
        }
    }
}

fn oldest_first(left: &Kept, right: &Kept) -> Ordering {
    (left.taken, &left.id, left.generation).cmp(&(right.taken, &right.id, right.generation))
}

/// The session ids under `sessions/`, sorted. A name that is no session id
/// or no dir does not count.
fn sessions(state: &StateRoot) -> io::Result<Vec<SessionId>> {
    let entries = match fs::read_dir(state.sessions_dir()) {
        Ok(entries) => entries,
        Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(Vec::new()),
        Err(error) => return Err(error),
    };
    let mut found = Vec::new();
    for entry in entries {
        let entry = entry?;
        let id = entry
            .file_name()
            .to_str()
            .and_then(|text| text.parse().ok());
        if let Some(id) = id
            && entry.file_type()?.is_dir()
        {
            found.push(id);
        }
    }
    found.sort_unstable();
    Ok(found)
}

/// What `measure` reads of one generation dir.
struct Measured {
    /// The time of the last write of `memory-ranges`, or of the dir
    /// without it.
    taken: SystemTime,
    /// The allocated bytes of the entries of the dir.
    bytes: u64,
    /// True when the dir holds the consumed mark of a claim.
    consumed: bool,
}

/// Reads the take time, the allocated bytes and the consumed mark of
/// `dir`.
fn measure(dir: &Path) -> io::Result<Measured> {
    let mut bytes = 0_u64;
    let mut consumed = false;
    for entry in fs::read_dir(dir)? {
        let entry = entry?;
        consumed |= entry.file_name() == CONSUMED;
        let blocks = entry.metadata()?.blocks();
        bytes = bytes.saturating_add(blocks.saturating_mul(512));
    }
    let taken = match fs::symlink_metadata(dir.join(MEMORY_RANGES)) {
        Ok(meta) if meta.is_file() => meta.modified()?,
        Ok(_) => fs::symlink_metadata(dir)?.modified()?,
        Err(error) if error.kind() == io::ErrorKind::NotFound => {
            fs::symlink_metadata(dir)?.modified()?
        }
        Err(error) => return Err(error),
    };
    Ok(Measured {
        taken,
        bytes,
        consumed,
    })
}

/// Step 1: removes each `<n>.retired` dir of `session` and syncs
/// `snapshots/` (decision CH44 rule 6). A failed read of `snapshots/`
/// counts once, as a failure of `generations` right after.
fn remove_retired(session: &SessionDir, swept: &mut Swept) {
    let snapshots = session.snapshots();
    let Ok(entries) = fs::read_dir(&snapshots) else {
        return;
    };
    let mut removed = false;
    for entry in entries.flatten() {
        let name = entry.file_name();
        let retired = name
            .to_str()
            .and_then(|text| text.strip_suffix(".retired"))
            .is_some_and(|number| number.parse::<u64>().is_ok());
        if !retired || !entry.file_type().is_ok_and(|kind| kind.is_dir()) {
            continue;
        }
        let path = entry.path();
        match fs::remove_dir_all(&path) {
            Ok(()) => {
                swept.retired.push(path);
                removed = true;
            }
            Err(source) => swept.failures.push(Failure { path, source }),
        }
    }
    if removed && let Err(source) = File::open(&snapshots).and_then(|dir| dir.sync_all()) {
        swept.failures.push(Failure {
            path: snapshots,
            source,
        });
    }
}
