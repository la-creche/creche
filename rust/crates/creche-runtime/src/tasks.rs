//! The stop of a process, the tasks that must end before it, and a lock that
//! a panic cannot close.
//!
//! In `tokio`, a future that its caller drops stops at its next `await` and
//! runs no cleanup. The Python services rely on the opposite: a handler runs
//! to its end after its client left. This module holds the parts that give a
//! Rust service the Python behavior on purpose:
//!
//! - [`Shutdown`] is the one stop signal of a process. A loop or a stream
//!   waits on it beside its own work. A service must not use the drop of a
//!   future as its cleanup.
//! - [`Tasks`] tracks each task that must end before the process exits. A
//!   caller that drops its [`Completion`] does not stop the task.
//! - [`locked`] takes a lock also after a panic poisoned it.
//!
//! `rust/AGENTS.md`, "The rules for a service", holds the rules that use
//! these parts.
//!
//! The Python origins are `attendance/src/attendance/tasks.py` and the
//! upkeep loop of `attendance/src/attendance/service.py:285-319`. This module
//! is a new design and not a translation of them.
//!
//! Each function body here is a stub. `AGENTS.md` of this crate lists the
//! stubs and the packet that writes them.

use std::error::Error;
use std::fmt;
use std::future::Future;
use std::marker::PhantomData;
use std::pin::Pin;
use std::sync::{Mutex, MutexGuard};
use std::task::{Context, Poll};
use std::time::Duration;

/// Makes the stop signal of a process: the value that triggers it, and the
/// value that each task holds.
///
/// `service::run` calls this one time for each process.
#[expect(
    clippy::todo,
    reason = "skeleton: packet foundation-tasks-signals writes this body"
)]
#[must_use]
pub fn shutdown_pair() -> (ShutdownTrigger, Shutdown) {
    todo!()
}

/// The stop signal of a process, as a task holds it.
///
/// A clone is the same signal. A task cannot trigger the signal: only
/// [`ShutdownTrigger`] can.
#[derive(Debug, Clone)]
pub struct Shutdown(());

impl Shutdown {
    /// Waits for the stop signal. The future is ready at once when the signal
    /// was triggered before the call.
    ///
    /// The future is cancel safe: a caller can drop it in a `select!` and
    /// call the function again.
    #[expect(
        clippy::todo,
        reason = "skeleton: packet foundation-tasks-signals writes this body"
    )]
    pub async fn cancelled(&self) {
        todo!()
    }

    /// Whether the stop signal was triggered.
    #[expect(
        clippy::todo,
        reason = "skeleton: packet foundation-tasks-signals writes this body"
    )]
    #[must_use]
    pub fn is_cancelled(&self) -> bool {
        todo!()
    }
}

/// The value that triggers the stop signal of a process.
///
/// A clone triggers the same signal. The signal handlers hold one, and
/// `service::run` holds one and triggers it after `main` returns. No task of
/// a service gets one.
#[derive(Debug, Clone)]
pub struct ShutdownTrigger(());

impl ShutdownTrigger {
    /// Triggers the stop signal. A second call has no effect.
    #[expect(
        clippy::todo,
        reason = "skeleton: packet foundation-tasks-signals writes this body"
    )]
    pub fn trigger(&self) {
        todo!()
    }
}

/// The tasks that must end before the process exits.
///
/// A clone is the same set of tasks. `service::run` makes one for each
/// process and drains it after the stop signal.
#[derive(Debug, Clone)]
pub struct Tasks(());

impl Tasks {
    /// An empty set of tasks that stops at `shutdown`.
    #[expect(
        clippy::todo,
        unused_variables,
        reason = "skeleton: packet foundation-tasks-signals writes this body"
    )]
    #[must_use]
    pub fn new(shutdown: Shutdown) -> Self {
        todo!()
    }

    /// The stop signal of these tasks.
    #[expect(
        clippy::todo,
        reason = "skeleton: packet foundation-tasks-signals writes this body"
    )]
    #[must_use]
    pub fn shutdown(&self) -> &Shutdown {
        todo!()
    }

    /// Starts `work` as a task that runs to its end, also when the caller
    /// drops the [`Completion`].
    ///
    /// Use it for a write of a line or of a file, and for each step that a
    /// stop must not cut in two. [`Tasks::drain`] waits for the task. A task
    /// that panics writes one `ERROR` line with `name`, and its
    /// [`Completion`] gives [`TaskLost::Panicked`].
    #[expect(
        clippy::todo,
        unused_variables,
        reason = "skeleton: packet foundation-tasks-signals writes this body"
    )]
    pub fn spawn_must_complete<F>(&self, name: &'static str, work: F) -> Completion<F::Output>
    where
        F: Future + Send + 'static,
        F::Output: Send + 'static,
    {
        todo!()
    }

    /// Starts `work` on the pool for blocking calls. The work runs to its
    /// end, also when the caller drops the [`Completion`].
    ///
    /// Use it for `fsync`, for `std::fs` and for each other call that blocks.
    /// The work must end by itself: nothing can stop a blocking call. After
    /// the drain limit of the program, the process exits without it.
    #[expect(
        clippy::todo,
        unused_variables,
        reason = "skeleton: packet foundation-tasks-signals writes this body"
    )]
    pub fn spawn_blocking<F, T>(&self, name: &'static str, work: F) -> Completion<T>
    where
        F: FnOnce() -> T + Send + 'static,
        T: Send + 'static,
    {
        todo!()
    }

    /// Starts a loop that calls `body`, waits for `pause` and calls `body`
    /// again, until the stop signal.
    ///
    /// Each pass runs as its own task. A pass that panics writes one `ERROR`
    /// line with `name`, and the loop continues with the next pass. Leave
    /// each value that a pass changes in a valid state after each statement.
    /// The pass after a panic then finds a value that it can use.
    #[expect(
        clippy::todo,
        unused_variables,
        reason = "skeleton: packet foundation-tasks-signals writes this body"
    )]
    pub fn spawn_loop<F, Fut>(&self, name: &'static str, pause: Duration, body: F)
    where
        F: FnMut() -> Fut + Send + 'static,
        Fut: Future<Output = ()> + Send + 'static,
    {
        todo!()
    }

    /// Waits for each tracked task, for `limit` at most. Call it after the
    /// stop signal.
    ///
    /// The future is cancel safe: a caller that drops it stops only the wait.
    /// The tasks continue.
    #[expect(
        clippy::todo,
        unused_variables,
        reason = "skeleton: packet foundation-tasks-signals writes this body"
    )]
    pub async fn drain(&self, limit: Duration) -> Drained {
        todo!()
    }
}

/// The result of a task of [`Tasks`], as a future.
///
/// The output is the value of the task, or [`TaskLost`]. To drop the value
/// does not stop the task.
pub struct Completion<T>(PhantomData<T>);

impl<T> fmt::Debug for Completion<T> {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("Completion").finish_non_exhaustive()
    }
}

impl<T> Future for Completion<T> {
    type Output = Result<T, TaskLost>;

    #[expect(
        clippy::todo,
        unused_variables,
        reason = "skeleton: packet foundation-tasks-signals writes this body"
    )]
    fn poll(self: Pin<&mut Self>, context: &mut Context<'_>) -> Poll<Self::Output> {
        todo!()
    }
}

/// Why a task gave no value.
///
/// The set is closed. It does not cross a process boundary.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TaskLost {
    /// The task panicked. The log holds one `ERROR` line with its name.
    Panicked,
    /// The runtime stopped before the task ended.
    Cancelled,
}

impl fmt::Display for TaskLost {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Self::Panicked => "the task panicked",
            Self::Cancelled => "the runtime stopped before the task ended",
        })
    }
}

impl Error for TaskLost {}

/// What [`Tasks::drain`] found at its end.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Drained {
    /// Each tracked task ended.
    Clean,
    /// The limit passed first.
    TimedOut {
        /// The count of the tasks that still run.
        left: usize,
    },
}

/// Takes `mutex`, also after a panic poisoned it.
///
/// A panic while a task holds a lock poisons the lock, and each later
/// `lock()` then gives an error. Python has no such state: the next request
/// gets the value. This function takes the value anyway and writes one
/// `ERROR` line. It never returns an error and never panics.
///
/// Leave each value under a lock in a valid state after each statement. The
/// caller after a panic then gets a value that it can use. Do not hold the
/// guard across an `await`: the lint gate refuses that.
#[expect(
    clippy::todo,
    unused_variables,
    reason = "skeleton: packet foundation-tasks-signals writes this body"
)]
pub fn locked<T>(mutex: &Mutex<T>) -> MutexGuard<'_, T> {
    todo!()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_lost_task_names_its_reason() {
        assert_eq!(TaskLost::Panicked.to_string(), "the task panicked");
        assert_eq!(
            TaskLost::Cancelled.to_string(),
            "the runtime stopped before the task ended"
        );
    }

    #[test]
    fn a_task_handle_goes_to_another_thread() {
        fn sendable<T: Send>() {}
        fn shareable<T: Send + Sync>() {}

        sendable::<Completion<Vec<u8>>>();
        shareable::<Shutdown>();
        shareable::<ShutdownTrigger>();
        shareable::<Tasks>();
    }
}
