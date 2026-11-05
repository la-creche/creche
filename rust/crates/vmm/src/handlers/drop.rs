//! The `drop` route (decisions CH21-2, CH21-10, CH27). A drop cannot be
//! undone. It marks each session of the target, which stops its open
//! relay (decision CH26 rule 5). It waits for an Upgrade that the mark
//! stopped, then stops every VM of the target at once (decision CH33 rule
//! 11). Last it removes the data of each session through `remove_session`
//! of `creche-vmm-store`, which removes the row last (PR D14).

use std::future::{Future, poll_fn};
use std::pin::Pin;
use std::sync::{Arc, Mutex};
use std::task::Poll;

use creche_vmm_store::remove::remove_session;
use creche_vmm_store::rows::Rows;
use creche_wire::{
    DropError, DropFamily, DropRoute, DropSession, DropTarget, Dropped, FamilyName, Serve,
    SessionId,
};
use tokio::task::spawn_blocking;

use crate::relay::{DropMark, Relays};
use crate::stop::StopError;

/// A stop of one VM that did not end with the process gone and its files
/// removed. A second stop that waits for the first returns the same one.
#[derive(Debug, Clone)]
pub struct VmStopFailed {
    /// The pid of the Cloud Hypervisor process, for the log (decision CH50
    /// rule 4).
    pub pid: u32,
    /// Why the stop failed.
    pub error: Arc<StopError>,
}

/// The stop of the VM of one session. `start::ColdBoot` is the hook of
/// the vmm: it calls `stop::stop` on the VM that it started. The tests
/// fake it.
pub trait StopVm: Send + Sync + 'static {
    /// Stops the VM of `id`, or returns at once when no VM of `id` runs.
    /// A kill is no error (decision CH19 (3)).
    ///
    /// On an error the hook keeps the VM in its table, so admission keeps
    /// counting the RAM of the VM (decision CH50 rule 4). Two drops of one
    /// session can overlap. A stop that finds a stop of the same VM
    /// running waits for it and returns its outcome, so no drop removes
    /// the files of a VM that still runs.
    ///
    /// # Errors
    ///
    /// `VmStopFailed` when the process may still run or its files stay.
    fn stop(&self, id: &SessionId) -> impl Future<Output = Result<(), VmStopFailed>> + Send;
}

/// The handler of `drop`. It shares the rows and the relay registry with
/// the other handlers.
#[derive(Debug)]
pub struct Drops<S> {
    rows: Arc<Mutex<Rows>>,
    relays: Relays,
    stop: Arc<S>,
}

impl<S: StopVm> Drops<S> {
    /// The handler over `rows` and `relays`, with the hook `stop`.
    pub fn new(rows: Arc<Mutex<Rows>>, relays: Relays, stop: S) -> Self {
        let stop = Arc::new(stop);
        Self { rows, relays, stop }
    }

    /// A handle on the same rows, relays and hook, for the task of one
    /// drop.
    fn share(&self) -> Self {
        Self {
            rows: Arc::clone(&self.rows),
            relays: self.relays.clone(),
            stop: Arc::clone(&self.stop),
        }
    }

    /// The family and the ids of `target`. A session that is absent or
    /// that another family owns gives `not_found` (decision CH33 rules 4
    /// and 5). A family with no rows gives no id.
    fn ids(&self, target: DropTarget) -> Result<(FamilyName, Vec<SessionId>), DropError> {
        let rows = self.rows.lock().map_err(|_| DropError::Internal {})?;
        match target {
            DropTarget::Session(DropSession { family, id }) => match rows.get(&id) {
                Some(row) if row.family == family => Ok((family, vec![id])),
                _ => Err(DropError::NotFound {}),
            },
            DropTarget::Family(DropFamily { family }) => {
                let ids = rows
                    .newest_first()
                    .filter(|(_, row)| row.family == family)
                    .map(|(_, row)| row.id.clone())
                    .collect();
                Ok((family, ids))
            }
        }
    }

    /// Removes the data and then the row of each id in `ids`. It returns
    /// whether every removal worked, and how many rows it removed.
    async fn remove(&self, family: FamilyName, ids: Vec<SessionId>) -> Result<(bool, usize), ()> {
        let rows = Arc::clone(&self.rows);
        // Each removal syncs files and dirs, so it runs off the runtime
        // (decision CH36 G21).
        spawn_blocking(move || {
            let (mut whole, mut removed) = (true, 0_usize);
            for id in ids {
                let target = DropSession {
                    family: family.clone(),
                    id,
                };
                // The lock lasts one removal. The acp, create and list
                // handlers lock the rows on a runtime worker (decision
                // CH36 G21), so between two removals they get it.
                let mut rows = rows.lock().map_err(|_| ())?;
                match remove_session(&mut rows, &target) {
                    Ok(Some(_)) => removed = removed.saturating_add(1),
                    // A drop that overlapped this one removed the row.
                    Ok(None) => {}
                    Err(error) => {
                        eprintln!("vmm: drop of {}: {error}: {}", target.id, error.source);
                        whole = false;
                    }
                }
            }
            Ok((whole, removed))
        })
        .await
        .map_err(|_| ())?
    }
}

impl<S: StopVm> Serve<DropRoute> for Drops<S> {
    /// Runs the drop in a task of its own and waits for it. hyper drops
    /// this future when the client hangs up, because `server/accept.rs`
    /// leaves half-close off. The task runs on with its marks, so a retry
    /// joins the live slot and waits for the same start, and no hang-up
    /// stops a VM halfway. A task that panicked answers `internal`.
    async fn serve(&self, target: DropTarget) -> Result<Dropped, DropError> {
        tokio::spawn(self.share().run(target))
            .await
            .unwrap_or(Err(DropError::Internal {}))
    }
}

impl<S: StopVm> Drops<S> {
    /// The body of one drop. It owns its handle, so it runs in a task.
    async fn run(self, target: DropTarget) -> Result<Dropped, DropError> {
        let one = matches!(target, DropTarget::Session(_));
        let (family, ids) = self.ids(target)?;
        // The mark stops each open relay first (decision CH26 rule 5), and
        // an Upgrade under the mark gets `not_found` (decision CH34 rule
        // 6). The drop keeps each mark until the row is gone.
        let mut marks: Vec<DropMark> = ids.iter().map(|id| self.relays.mark_drop(id)).collect();
        // A start that the mark stopped may still boot a VM. The drop
        // waits for it, so the stops below find that VM too.
        // Decision CH54 rule 1: this wait has no bound, as the start has
        // none (decision CH50 rule 1). So a drop that overlaps a long
        // start may outlast the 60 s wait of the client. The client then
        // retries, and the retry waits for the same start.
        for mark in &mut marks {
            mark.claim_ended().await;
        }
        let stops = all(ids.iter().map(|id| self.stop.stop(id)).collect()).await;
        let mut stopped = Vec::with_capacity(ids.len());
        let mut whole = true;
        for (id, result) in ids.into_iter().zip(stops) {
            match result {
                Ok(()) => stopped.push(id),
                // Decision CH50 rule 4: the row of a VM that did not stop
                // stays, so a retried drop finishes the job. `Hung` and
                // every other stop error answer `internal` 500.
                Err(VmStopFailed { pid, error }) => {
                    eprintln!("vmm: drop of {id}: the VM (pid {pid}) did not stop: {error}");
                    whole = false;
                }
            }
        }
        let (removed_all, removed) = self
            .remove(family, stopped)
            .await
            .map_err(|()| DropError::Internal {})?;
        drop(marks);
        if !(whole && removed_all) {
            return Err(DropError::Internal {});
        }
        // An overlapping drop of the one session removed its row first.
        if one && removed == 0 {
            return Err(DropError::NotFound {});
        }
        Ok(Dropped {})
    }
}

/// Runs each future in `futures` at once and returns their outputs in
/// order (decision CH33 rule 11: every VM of the target stops at once).
async fn all<F: Future>(futures: Vec<F>) -> Vec<F::Output> {
    let mut futures: Vec<Pin<Box<F>>> = futures.into_iter().map(Box::pin).collect();
    let mut outputs: Vec<Option<F::Output>> = futures.iter().map(|_| None).collect();
    poll_fn(|context| {
        let mut pending = false;
        for (future, output) in futures.iter_mut().zip(outputs.iter_mut()) {
            if output.is_none() {
                match future.as_mut().poll(context) {
                    Poll::Ready(value) => *output = Some(value),
                    Poll::Pending => pending = true,
                }
            }
        }
        if pending {
            Poll::Pending
        } else {
            Poll::Ready(())
        }
    })
    .await;
    outputs.into_iter().flatten().collect()
}
