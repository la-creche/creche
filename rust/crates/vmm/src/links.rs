//! The links and the accept task of the reach listeners of one VM: the
//! mirror of host port 1103 and the egress proxy of host port 1104 (plan
//! PR E3, decision CH74 rule 2). An edit that empties a list closes the
//! listener only. Each open link runs to its own end, and a later edit
//! that lists an entry again serves a new listener with the same links.

use std::future::Future;
use std::sync::{Arc, Mutex, PoisonError};

use tokio::task::{JoinHandle, JoinSet};

/// The open links of one listener of one VM. A close of the listener
/// keeps them, and a reopen counts them against the cap of the new
/// listener. So one VM holds at most the cap of links on one port, and
/// each `fd_need` holds across a close and a reopen. The drop of the last
/// handle aborts each link.
#[derive(Debug, Clone, Default)]
pub(crate) struct Links(Arc<Mutex<JoinSet<()>>>);

impl Links {
    /// Spawns `link` when fewer than `cap` links are open, after the reap
    /// of each link that ended. Else it drops `link`, which closes its
    /// stream, and gives false.
    pub(crate) fn admit(
        &self,
        cap: usize,
        link: impl Future<Output = ()> + Send + 'static,
    ) -> bool {
        let mut links = self.0.lock().unwrap_or_else(PoisonError::into_inner);
        while links.try_join_next().is_some() {}
        if links.len() >= cap {
            return false;
        }
        links.spawn(link);
        true
    }
}

/// The accept task of one listener. Its drop aborts the task, and the end
/// of the task drops the listener.
#[derive(Debug)]
pub(crate) struct Accept(JoinHandle<()>);

impl Accept {
    /// Spawns `task`, the accept loop of one listener.
    pub(crate) fn spawn(task: impl Future<Output = ()> + Send + 'static) -> Self {
        Self(tokio::spawn(task))
    }

    /// Aborts the task and waits for its end. When it returns, the
    /// listener is closed, and no new link starts.
    pub(crate) async fn end(mut self) {
        self.0.abort();
        // The loop never returns, so the task ends at the abort or at a
        // panic. Either end drops the listener.
        let _ = (&mut self.0).await;
    }
}

impl Drop for Accept {
    fn drop(&mut self) {
        self.0.abort();
    }
}
