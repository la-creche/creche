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
//! A panic of a tracked task writes one `ERROR` line with the target `tasks`
//! and the name of the task. The line never holds the message of the panic:
//! that message can hold a part of a request or of a file.
//!
//! Call each function of [`Tasks`] inside the runtime that `service::run`
//! builds. [`Tasks::spawn_loop`] and [`Tasks::drain`] use the timer of that
//! runtime.
//!
//! ```
//! use std::time::Duration;
//!
//! use creche_runtime::tasks::{Drained, Tasks, shutdown_pair};
//!
//! let runtime = tokio::runtime::Builder::new_current_thread()
//!     .enable_all()
//!     .build()?;
//! let drained = runtime.block_on(async {
//!     let (trigger, shutdown) = shutdown_pair();
//!     let tasks = Tasks::new(shutdown);
//!
//!     let written = tasks.spawn_must_complete("ledger-write", async { 7 });
//!     assert_eq!(written.await, Ok(7));
//!
//!     trigger.trigger();
//!     tasks.drain(Duration::from_secs(1)).await
//! });
//! assert_eq!(drained, Drained::Clean);
//! # Ok::<(), std::io::Error>(())
//! ```

use std::error::Error;
use std::fmt;
use std::future::Future;
use std::panic::{self, AssertUnwindSafe};
use std::pin::Pin;
use std::sync::{Mutex, MutexGuard};
use std::task::{Context, Poll};
use std::time::Duration;

use tokio::runtime::Handle;
use tokio::task::JoinHandle;
use tokio_util::sync::CancellationToken;
use tokio_util::task::TaskTracker;

/// The target of each line that this module writes to the log.
const LOG_TARGET: &str = "tasks";

/// Makes the stop signal of a process: the value that triggers it, and the
/// value that each task holds.
///
/// `service::run` calls this one time for each process.
///
/// ```
/// use creche_runtime::tasks::shutdown_pair;
///
/// let (trigger, shutdown) = shutdown_pair();
/// assert!(!shutdown.is_cancelled());
///
/// trigger.trigger();
/// assert!(shutdown.is_cancelled());
/// ```
#[must_use]
pub fn shutdown_pair() -> (ShutdownTrigger, Shutdown) {
    let token = CancellationToken::new();

    (
        ShutdownTrigger {
            token: token.clone(),
        },
        Shutdown { token },
    )
}

/// The stop signal of a process, as a task holds it.
///
/// A clone is the same signal. A task cannot trigger the signal: only
/// [`ShutdownTrigger`] can.
///
/// The Python origins are the `should_exit` flag that the handler of
/// `attendance/src/attendance/__main__.py:201-203` sets, and the stop event
/// of `caregiver/src/caregiver/loop.py:1205-1240`.
///
/// ```
/// use creche_runtime::tasks::{Shutdown, shutdown_pair};
///
/// let (_trigger, shutdown) = shutdown_pair();
/// let copy: Shutdown = shutdown.clone();
/// assert!(!copy.is_cancelled());
/// ```
///
/// Code outside this module cannot build a value from a token of its own.
/// Such a value is a stop signal that no signal handler triggers:
///
/// ```compile_fail,E0451
/// use creche_runtime::tasks::{Shutdown, shutdown_pair};
///
/// let shutdown = Shutdown {
///     token: tokio_util::sync::CancellationToken::new(),
/// };
/// ```
#[derive(Debug, Clone)]
pub struct Shutdown {
    token: CancellationToken,
}

impl Shutdown {
    /// Waits for the stop signal. The future is ready at once when the signal
    /// was triggered before the call.
    ///
    /// The future is cancel safe: a caller can drop it in a `select!` and
    /// call the function again.
    pub async fn cancelled(&self) {
        self.token.cancelled().await;
    }

    /// Whether the stop signal was triggered.
    #[must_use]
    pub fn is_cancelled(&self) -> bool {
        self.token.is_cancelled()
    }
}

/// The value that triggers the stop signal of a process.
///
/// A clone triggers the same signal. The signal handlers hold one, and
/// `service::run` holds one and triggers it after `main` returns. No task of
/// a service gets one.
///
/// ```
/// use creche_runtime::tasks::{ShutdownTrigger, shutdown_pair};
///
/// let (trigger, shutdown) = shutdown_pair();
/// let copy: ShutdownTrigger = trigger.clone();
/// copy.trigger();
/// assert!(shutdown.is_cancelled());
/// ```
///
/// Code outside this module cannot build a value from a token of its own:
///
/// ```compile_fail,E0451
/// use creche_runtime::tasks::{ShutdownTrigger, shutdown_pair};
///
/// let trigger = ShutdownTrigger {
///     token: tokio_util::sync::CancellationToken::new(),
/// };
/// ```
#[derive(Debug, Clone)]
pub struct ShutdownTrigger {
    token: CancellationToken,
}

impl ShutdownTrigger {
    /// Triggers the stop signal. A second call has no effect.
    pub fn trigger(&self) {
        self.token.cancel();
    }

    /// The stop signal that this value triggers.
    ///
    /// `signals::install` gets only the trigger. It makes the value that
    /// ends the SIGHUP items from it.
    pub(crate) fn shutdown(&self) -> Shutdown {
        Shutdown {
            token: self.token.clone(),
        }
    }
}

/// The tasks that must end before the process exits.
///
/// A clone is the same set of tasks. `service::run` makes one for each
/// process and drains it after the stop signal.
///
/// ```
/// use creche_runtime::tasks::{Tasks, shutdown_pair};
///
/// let (trigger, shutdown) = shutdown_pair();
/// let tasks = Tasks::new(shutdown);
/// trigger.trigger();
/// assert!(tasks.shutdown().is_cancelled());
/// ```
///
/// Code outside this module cannot build a value from its parts:
///
/// ```compile_fail,E0451
/// use creche_runtime::tasks::{Tasks, shutdown_pair};
///
/// let (_trigger, shutdown) = shutdown_pair();
/// let tasks = Tasks {
///     shutdown,
///     tracker: tokio_util::task::TaskTracker::new(),
/// };
/// ```
#[derive(Debug, Clone)]
pub struct Tasks {
    shutdown: Shutdown,
    tracker: TaskTracker,
}

impl Tasks {
    /// An empty set of tasks that stops at `shutdown`.
    #[must_use]
    pub fn new(shutdown: Shutdown) -> Self {
        Self {
            shutdown,
            tracker: TaskTracker::new(),
        }
    }

    /// The stop signal of these tasks.
    #[must_use]
    pub fn shutdown(&self) -> &Shutdown {
        &self.shutdown
    }

    /// Starts `work` as a task that runs to its end, also when the caller
    /// drops the [`Completion`].
    ///
    /// Use it for a write of a line or of a file, and for each step that a
    /// stop must not cut in two. [`Tasks::drain`] waits for the task. A task
    /// that panics writes one `ERROR` line with `name`, and its
    /// [`Completion`] gives [`TaskLost::Panicked`].
    ///
    /// The stop signal does not stop the task. A task that must stop early
    /// waits on [`Tasks::shutdown`] beside its own work.
    ///
    /// On a thread with no runtime, the function starts nothing. It writes
    /// one `ERROR` line, and the [`Completion`] gives
    /// [`TaskLost::Cancelled`].
    ///
    /// The Python origin is the done-callback of
    /// `attendance/src/attendance/tasks.py:17-31`. It writes one line for a
    /// task that ended with an error.
    pub fn spawn_must_complete<F>(&self, name: &'static str, work: F) -> Completion<F::Output>
    where
        F: Future + Send + 'static,
        F::Output: Send + 'static,
    {
        Completion {
            task: self.start(Role::Task, name, work),
        }
    }

    /// Starts `work` on the pool for blocking calls. The work runs to its
    /// end, also when the caller drops the [`Completion`].
    ///
    /// Use it for `fsync`, for `std::fs` and for each other call that blocks.
    /// The work must end by itself: nothing can stop a blocking call. After
    /// the drain limit of the program, the process exits without it.
    ///
    /// [`Tasks::drain`] waits for the work. Work that panics writes one
    /// `ERROR` line with `name`, and its [`Completion`] gives
    /// [`TaskLost::Panicked`]. On a thread with no runtime, the function
    /// starts nothing, as [`Tasks::spawn_must_complete`] does.
    ///
    /// The pool of `tokio` panics when it has no thread and the operating
    /// system gives it none. This function does not guard that panic.
    ///
    /// The Python origin is a blocking call on the event loop, for example
    /// the `fsync` of `attendance/src/attendance/atomic.py:52`. Python runs
    /// that call to its end too.
    pub fn spawn_blocking<F, T>(&self, name: &'static str, work: F) -> Completion<T>
    where
        F: FnOnce() -> T + Send + 'static,
        T: Send + 'static,
    {
        let Ok(runtime) = Handle::try_current() else {
            report_not_started(Role::Task, name);

            return Completion { task: None };
        };
        let guarded = move || {
            panic::catch_unwind(AssertUnwindSafe(work)).map_err(|payload| {
                report_panic(Role::Task, name);
                drop(payload);

                TaskLost::Panicked
            })
        };

        Completion {
            task: Some(self.tracker.spawn_blocking_on(guarded, &runtime)),
        }
    }

    /// Starts a loop that calls `body`, waits for `pause` and calls `body`
    /// again, until the stop signal.
    ///
    /// Each pass runs as its own task. A pass that panics writes one `ERROR`
    /// line with `name`, and the loop continues with the next pass. Leave
    /// each value that a pass changes in a valid state after each statement.
    /// The pass after a panic then finds a value that it can use.
    ///
    /// The sequence of the loop:
    ///
    /// 1. The loop ends when the stop signal was triggered.
    /// 2. The loop calls `body` and runs the pass to its end. The stop signal
    ///    does not stop a pass. A pass that must stop early waits on
    ///    [`Tasks::shutdown`] beside its own work.
    /// 3. The loop waits for `pause` or for the stop signal. The time of
    ///    `pause` starts at the end of the pass.
    ///
    /// A panic ends the pass, so the steps after the panic do not run in that
    /// pass. Give each step that must run without the others its own loop.
    ///
    /// [`Tasks::drain`] waits for the loop and for its pass. On a thread with
    /// no runtime, the function starts nothing and writes one `ERROR` line.
    ///
    /// The Python origin is the upkeep loop of
    /// `attendance/src/attendance/service.py:297-319`, where each step has
    /// its own guard. The refresh loop of
    /// `door-trigger/src/agent_door_trigger/webhooks.py:212-226` has the same
    /// form.
    pub fn spawn_loop<F, Fut>(&self, name: &'static str, pause: Duration, mut body: F)
    where
        F: FnMut() -> Fut + Send + 'static,
        Fut: Future<Output = ()> + Send + 'static,
    {
        let tasks = self.clone();
        let turns = async move {
            while !tasks.shutdown.is_cancelled() {
                tasks.one_pass(name, &mut body).await;

                let paused = tasks
                    .shutdown
                    .token
                    .run_until_cancelled(tokio::time::sleep(pause));

                if paused.await.is_none() {
                    break;
                }
            }
        };

        // No caller waits for the loop: it has no value to give.
        drop(self.start(Role::Loop, name, turns));
    }

    /// Runs one pass of a loop to its end, as its own task.
    async fn one_pass<F, Fut>(&self, name: &'static str, body: &mut F)
    where
        F: FnMut() -> Fut + Send + 'static,
        Fut: Future<Output = ()> + Send + 'static,
    {
        // The call of `body` makes the future of the pass. A panic in that
        // call is a panic of the pass.
        let pass = match panic::catch_unwind(AssertUnwindSafe(body)) {
            Ok(pass) => pass,
            Err(payload) => {
                report_panic(Role::Pass, name);
                drop(payload);

                return;
            }
        };
        let Some(task) = self.start(Role::Pass, name, pass) else {
            return;
        };

        // The pass wrote its line when it panicked. The runtime gives an
        // error here only at its own stop, and the loop then ends with it.
        let _ = task.await;
    }

    /// Starts `work` as a tracked task. A panic of the work is a value and a
    /// line in the log. `None` when no runtime runs on this thread.
    fn start<F>(&self, role: Role, name: &'static str, work: F) -> Option<Guarded<F::Output>>
    where
        F: Future + Send + 'static,
        F::Output: Send + 'static,
    {
        let Ok(runtime) = Handle::try_current() else {
            report_not_started(role, name);

            return None;
        };
        let guard = Guard {
            role,
            name,
            work: Some(Box::pin(work)),
        };

        Some(self.tracker.spawn_on(guard, &runtime))
    }

    /// Waits for each tracked task, for `limit` at most. Call it after the
    /// stop signal.
    ///
    /// The future is cancel safe: a caller that drops it stops only the wait.
    /// The tasks continue.
    ///
    /// The count of [`Drained::TimedOut`] holds each task that still runs at
    /// the limit. A loop is one task, and its pass is one more. A task that
    /// starts during the wait is a tracked task too.
    ///
    /// The Python origin is the stop of `caregiver/src/caregiver/loop.py`.
    /// It waits for each pass for a limit (`:312-323`). Then it says how many
    /// passes still run (`:262-274`).
    pub async fn drain(&self, limit: Duration) -> Drained {
        // The wait ends only on a closed tracker. A closed tracker still
        // takes a new task.
        self.tracker.close();

        if tokio::time::timeout(limit, self.tracker.wait())
            .await
            .is_ok()
        {
            return Drained::Clean;
        }

        match self.tracker.len() {
            // The last task ended between the limit and this count.
            0 => Drained::Clean,
            left => Drained::TimedOut { left },
        }
    }
}

/// The handle of a tracked task. The task gives its value, or
/// [`TaskLost::Panicked`] for a panic.
type Guarded<T> = JoinHandle<Result<T, TaskLost>>;

/// What a tracked task is. The line of a panic differs for each one.
#[derive(Debug, Clone, Copy)]
enum Role {
    /// A task of [`Tasks::spawn_must_complete`] or of
    /// [`Tasks::spawn_blocking`].
    Task,
    /// The task that starts each pass of a loop.
    Loop,
    /// One pass of a loop.
    Pass,
}

/// Writes the one line of a tracked task that panicked. The line holds the
/// name and never the message of the panic.
fn report_panic(role: Role, name: &'static str) {
    match role {
        Role::Task => crate::error!(LOG_TARGET, "the task {name} stopped with a panic"),
        Role::Loop => crate::error!(LOG_TARGET, "the loop {name} stopped with a panic"),
        Role::Pass => crate::error!(
            LOG_TARGET,
            "a pass of the loop {name} stopped with a panic, and the loop continues"
        ),
    }
}

/// Writes the one line of a task or of a loop that the caller started
/// outside a runtime.
fn report_not_started(role: Role, name: &'static str) {
    let what = match role {
        Role::Task => "task",
        Role::Loop | Role::Pass => "loop",
    };

    crate::error!(
        LOG_TARGET,
        "the {what} {name} did not start: no runtime runs on this thread"
    );
}

/// The future of a tracked task: the work, with a guard against its panic.
///
/// `tokio` also ends only the task on a panic. This guard is here for the
/// line in the log: the task writes it itself, also when no caller waits for
/// the task.
struct Guard<F> {
    role: Role,
    name: &'static str,
    /// `None` after the end of the work.
    work: Option<Pin<Box<F>>>,
}

impl<F> Guard<F> {
    /// Drops the work. `false` when that drop panicked.
    fn release(&mut self) -> bool {
        let work = self.work.take();

        panic::catch_unwind(AssertUnwindSafe(move || drop(work))).is_ok()
    }

    /// The result of a work that panicked, after the one line in the log.
    fn lost<T>(&self) -> Poll<Result<T, TaskLost>> {
        report_panic(self.role, self.name);

        Poll::Ready(Err(TaskLost::Panicked))
    }
}

impl<F: Future> Future for Guard<F> {
    type Output = Result<F::Output, TaskLost>;

    fn poll(mut self: Pin<&mut Self>, context: &mut Context<'_>) -> Poll<Self::Output> {
        let Some(work) = self.work.as_mut() else {
            // The work ended at an earlier poll, and that poll gave its
            // result.
            return Poll::Ready(Err(TaskLost::Cancelled));
        };

        match panic::catch_unwind(AssertUnwindSafe(|| work.as_mut().poll(context))) {
            Ok(Poll::Pending) => Poll::Pending,
            Ok(Poll::Ready(value)) => {
                if self.release() {
                    Poll::Ready(Ok(value))
                } else {
                    self.lost()
                }
            }
            Err(payload) => {
                // The drop of a work that panicked can panic again. The task
                // has one result for the two.
                let _ = self.release();
                let lost = self.lost();
                drop(payload);

                lost
            }
        }
    }
}

/// The result of a task of [`Tasks`], as a future.
///
/// The output is the value of the task, or [`TaskLost`]. To drop the value
/// does not stop the task.
///
/// The future is cancel safe: a caller can wait on `&mut completion` in a
/// `select!` and wait again later. A poll after the output gives
/// [`TaskLost::Cancelled`] and does not panic.
///
/// ```
/// use creche_runtime::tasks::{Completion, TaskLost, Tasks, shutdown_pair};
///
/// let runtime = tokio::runtime::Builder::new_current_thread().build()?;
/// let (_trigger, shutdown) = shutdown_pair();
/// let tasks = Tasks::new(shutdown);
///
/// let written: Result<u8, TaskLost> = runtime.block_on(async {
///     let completion: Completion<u8> = tasks.spawn_must_complete("ledger-write", async { 7 });
///
///     completion.await
/// });
/// assert_eq!(written, Ok(7));
/// # Ok::<(), std::io::Error>(())
/// ```
///
/// Code outside this module cannot build a value. Only a task of [`Tasks`]
/// gives one:
///
/// ```compile_fail,E0451
/// use creche_runtime::tasks::{Completion, TaskLost, Tasks, shutdown_pair};
///
/// let completion = Completion::<u8> { task: None };
/// ```
pub struct Completion<T> {
    /// `None` when no task started, and after the output.
    task: Option<Guarded<T>>,
}

impl<T> fmt::Debug for Completion<T> {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("Completion").finish_non_exhaustive()
    }
}

impl<T> Future for Completion<T> {
    type Output = Result<T, TaskLost>;

    fn poll(self: Pin<&mut Self>, context: &mut Context<'_>) -> Poll<Self::Output> {
        let this = self.get_mut();
        let Some(task) = this.task.as_mut() else {
            return Poll::Ready(Err(TaskLost::Cancelled));
        };
        let Poll::Ready(joined) = Pin::new(task).poll(context) else {
            return Poll::Pending;
        };
        this.task = None;

        // The text of a join error holds the message of a panic. No code
        // here prints the error.
        Poll::Ready(match joined {
            Ok(result) => result,
            Err(error) if error.is_panic() => Err(TaskLost::Panicked),
            Err(_) => Err(TaskLost::Cancelled),
        })
    }
}

/// Why a task gave no value.
///
/// The set is closed. It does not cross a process boundary.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TaskLost {
    /// The task panicked. The log holds one `ERROR` line with its name.
    Panicked,
    /// The runtime stopped before the task ended, or no runtime ran on the
    /// thread that started the task.
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
/// The function also removes the poison from the lock. One panic thus gives
/// one line, and not one line for each later caller.
///
/// Leave each value under a lock in a valid state after each statement. The
/// caller after a panic then gets a value that it can use. Do not hold the
/// guard across an `await`: the lint gate refuses that.
///
/// The Python origin is each `threading.Lock` of a service, for example
/// `caregiver/src/caregiver/status.py:255-270`.
///
/// ```
/// use std::sync::Mutex;
///
/// use creche_runtime::tasks::locked;
///
/// let count = Mutex::new(41);
/// *locked(&count) += 1;
/// assert_eq!(*locked(&count), 42);
/// ```
pub fn locked<T>(mutex: &Mutex<T>) -> MutexGuard<'_, T> {
    match mutex.lock() {
        Ok(guard) => guard,
        Err(poisoned) => {
            let guard = poisoned.into_inner();
            // This thread holds the lock, so no other thread reads the
            // poison before it is gone.
            mutex.clear_poison();
            crate::error!(
                LOG_TARGET,
                "a panic poisoned a lock, and the next caller takes the value as it is"
            );

            guard
        }
    }
}

#[cfg(test)]
mod tests {
    use std::process::{Command, Output, Stdio};
    use std::sync::Arc;
    use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
    use std::sync::mpsc;
    use std::thread;
    use std::time::Instant;

    use creche_testkit::root::TempRoot;
    use tokio::runtime::{Builder, Runtime};
    use tokio::sync::oneshot;

    use super::*;

    /// Each difference from a Python copy, on purpose. No vector covers this
    /// module, so a row names the Python file and the line.
    const DEVIATIONS: [(&str, &str); 5] = [
        (
            "attendance/src/attendance/tasks.py:31",
            "Python writes the error of the task and its trace. The line here holds only the \
             name of the task: the message of a panic can hold a part of a request.",
        ),
        (
            "attendance/src/attendance/service.py:319",
            "Python writes the trace of the step that failed. The line here holds only the \
             name of the loop.",
        ),
        (
            "attendance/src/attendance/service.py:304",
            "The Python loop waits before its first pass. `spawn_loop` runs a pass first.",
        ),
        (
            "door-trigger/src/agent_door_trigger/webhooks.py:214",
            "The Python loop waits before its first pass. `spawn_loop` runs a pass first.",
        ),
        (
            "attendance/src/attendance/service.py:305-307",
            "Python gives each step of a pass its own guard, so the step after a failure \
             runs in the same pass. A panic ends a pass of `spawn_loop`. A service gives each \
             such step its own loop.",
        ),
    ];

    /// The longest time that a test waits for a step. The time is real in
    /// most tests, and a host with much load is slow.
    const LIMIT: Duration = Duration::from_secs(60);

    /// A drain limit that passes, in a test with a paused clock.
    const SHORT: Duration = Duration::from_secs(5);

    /// The pause of a loop, in a test with a paused clock.
    const PAUSE: Duration = Duration::from_secs(10);

    /// The time of three pauses and one half: a loop then ran four passes.
    const FOUR_PASSES: Duration = Duration::from_secs(35);

    /// The variable that selects the scenario of
    /// [`the_child_runs_one_scenario`].
    const CHILD_VARIABLE: &str = "CRECHE_RUNTIME_TASKS_TEST_CHILD";

    /// The name of the child test, as the test program takes it.
    const CHILD_TEST: &str = "tasks::tests::the_child_runs_one_scenario";

    /// The target of the panic hook of the child.
    const CHILD_PROGRAM: &str = "child";

    /// The message of each panic of a scenario. No line of the log can hold
    /// it.
    const PANIC_MESSAGE: &str = "walrus-ledger-secret-2931";

    /// How many times a scenario with no timer lets the other tasks run.
    const YIELDS: usize = 32;

    /// The line of a task that panicked.
    const LEDGER_LINE: &str = "the task ledger-write stopped with a panic";

    /// The line of a pass that panicked.
    const PASS_LINE: &str =
        "a pass of the loop upkeep stopped with a panic, and the loop continues";

    /// The line of a lock that a panic poisoned.
    const LOCK_LINE: &str = "a panic poisoned a lock, and the next caller takes the value as it is";

    /// A scenario that writes to the log. It runs in a child, so its lines do
    /// not go to the output of this test program, and the test can read them.
    struct Scenario {
        /// The value of [`CHILD_VARIABLE`] that selects the scenario.
        name: &'static str,
        /// What the child does.
        run: fn(),
        /// The message of each line that this module must write.
        lines: &'static [&'static str],
    }

    const SCENARIOS: [Scenario; 11] = [
        Scenario {
            name: "task",
            run: a_task_panics,
            lines: &[LEDGER_LINE],
        },
        Scenario {
            name: "dropped",
            run: a_task_with_no_caller_panics,
            lines: &[LEDGER_LINE],
        },
        Scenario {
            name: "blocking",
            run: a_blocking_task_panics,
            lines: &["the task index-sync stopped with a panic"],
        },
        Scenario {
            name: "pass",
            run: a_pass_panics,
            lines: &[PASS_LINE],
        },
        Scenario {
            name: "body",
            run: the_call_of_a_body_panics,
            lines: &[PASS_LINE],
        },
        Scenario {
            name: "each-pass",
            run: each_pass_panics,
            lines: &[PASS_LINE, PASS_LINE, PASS_LINE, PASS_LINE],
        },
        Scenario {
            name: "no-timer",
            run: a_loop_has_no_timer,
            lines: &["the loop upkeep stopped with a panic"],
        },
        Scenario {
            name: "no-runtime",
            run: no_runtime_runs,
            lines: &[
                "the task ledger-write did not start: no runtime runs on this thread",
                "the task index-sync did not start: no runtime runs on this thread",
                "the loop upkeep did not start: no runtime runs on this thread",
            ],
        },
        Scenario {
            name: "lock",
            run: a_lock_is_poisoned,
            lines: &[LOCK_LINE, LOCK_LINE],
        },
        Scenario {
            name: "drop",
            run: the_drop_of_a_work_panics_too,
            lines: &[LEDGER_LINE],
        },
        Scenario {
            name: "late-drop",
            run: the_drop_of_a_ready_work_panics,
            lines: &[LEDGER_LINE],
        },
    ];

    fn runtime() -> Runtime {
        Builder::new_current_thread().enable_all().build().unwrap()
    }

    /// A runtime whose clock moves only when each task waits. A test then
    /// waits for no real time.
    fn paused_runtime() -> Runtime {
        Builder::new_current_thread()
            .enable_all()
            .start_paused(true)
            .build()
            .unwrap()
    }

    fn new_tasks() -> (ShutdownTrigger, Tasks) {
        let (trigger, shutdown) = shutdown_pair();

        (trigger, Tasks::new(shutdown))
    }

    /// A body that counts its passes.
    fn counting(passes: &Arc<AtomicUsize>) -> impl FnMut() -> std::future::Ready<()> + use<> {
        let passes = Arc::clone(passes);

        move || {
            passes.fetch_add(1, Ordering::SeqCst);

            std::future::ready(())
        }
    }

    /// Runs one scenario in a child: this test program again, with only the
    /// child test. The child must end inside [`LIMIT`].
    fn run_child(scenario: &str) -> Output {
        let mut child = Command::new(std::env::current_exe().unwrap())
            .args([CHILD_TEST, "--exact", "--nocapture", "--test-threads=1"])
            .env(CHILD_VARIABLE, scenario)
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .unwrap();
        let deadline = Instant::now() + LIMIT;

        // A scenario writes a few lines, so a pipe never fills.
        while child.try_wait().unwrap().is_none() {
            if Instant::now() > deadline {
                child.kill().unwrap();
                child.wait().unwrap();

                panic!("the scenario {scenario} did not end");
            }

            thread::sleep(Duration::from_millis(10));
        }

        child.wait_with_output().unwrap()
    }

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

    #[test]
    fn each_future_of_the_module_goes_to_another_thread() {
        fn sendable<F: Future + Send>(future: F) {
            drop(future);
        }

        let (_trigger, tasks) = new_tasks();

        sendable(tasks.shutdown().cancelled());
        sendable(tasks.drain(SHORT));
    }

    #[test]
    fn each_deviation_names_a_python_file_and_a_line() {
        for (place, difference) in DEVIATIONS {
            let (file, lines) = place.rsplit_once(':').unwrap();

            assert!(file.ends_with(".py"), "{place}");
            assert!(
                lines
                    .split('-')
                    .all(|line| !line.is_empty() && line.bytes().all(|byte| byte.is_ascii_digit())),
                "{place}"
            );
            assert!(difference.ends_with('.'), "{place}");
        }
    }

    #[test]
    fn a_trigger_reaches_each_clone_of_the_stop_signal() {
        let (trigger, shutdown) = shutdown_pair();
        let copy = shutdown.clone();

        assert!(!shutdown.is_cancelled());
        assert!(!copy.is_cancelled());
        assert!(!trigger.shutdown().is_cancelled());

        trigger.clone().trigger();

        assert!(shutdown.is_cancelled());
        assert!(copy.is_cancelled());
        assert!(trigger.shutdown().is_cancelled());
        assert!(Tasks::new(shutdown).shutdown().is_cancelled());
    }

    #[test]
    fn a_second_trigger_has_no_effect() {
        let (trigger, shutdown) = shutdown_pair();

        trigger.trigger();
        trigger.trigger();

        assert!(shutdown.is_cancelled());
        runtime().block_on(shutdown.cancelled());
    }

    #[test]
    fn a_task_that_waits_wakes_at_the_stop_signal() {
        runtime().block_on(async {
            let (trigger, shutdown) = shutdown_pair();
            let waiter = tokio::spawn({
                let shutdown = shutdown.clone();

                async move { shutdown.cancelled().await }
            });
            tokio::task::yield_now().await;

            assert!(!waiter.is_finished());

            trigger.trigger();
            waiter.await.unwrap();

            // The signal was triggered before this call.
            shutdown.cancelled().await;
        });
    }

    #[test]
    fn the_wait_for_the_stop_signal_is_cancel_safe() {
        async fn stopped(shutdown: &Shutdown) -> bool {
            tokio::select! {
                biased;
                () = shutdown.cancelled() => true,
                () = std::future::ready(()) => false,
            }
        }

        runtime().block_on(async {
            let (trigger, shutdown) = shutdown_pair();

            // Each call drops a wait that did not end.
            for _ in 0..3 {
                assert!(!stopped(&shutdown).await);
            }

            trigger.trigger();

            assert!(stopped(&shutdown).await);
        });
    }

    #[test]
    fn a_completion_gives_the_value_of_its_task() {
        runtime().block_on(async {
            let (_trigger, tasks) = new_tasks();

            let written = tasks.spawn_must_complete("ledger-write", async { 7_u8 });
            let synced = tasks.spawn_blocking("index-sync", || String::from("synced"));

            assert_eq!(written.await, Ok(7));
            assert_eq!(synced.await, Ok(String::from("synced")));
        });
    }

    #[test]
    fn a_must_complete_task_ends_after_its_caller_is_dropped() {
        runtime().block_on(async {
            let (trigger, tasks) = new_tasks();
            let (open, gate) = oneshot::channel::<()>();
            let (start, started) = oneshot::channel::<()>();
            let done = Arc::new(AtomicBool::new(false));

            // The caller starts the work and waits for it, as a handler does.
            let caller = tokio::spawn({
                let tasks = tasks.clone();
                let done = Arc::clone(&done);

                async move {
                    let work = async move {
                        start.send(()).unwrap();
                        gate.await.unwrap();
                        done.store(true, Ordering::SeqCst);
                    };

                    tasks.spawn_must_complete("ledger-write", work).await
                }
            });
            started.await.unwrap();

            // The client left: the runtime drops the caller at its `await`.
            caller.abort();

            assert!(caller.await.unwrap_err().is_cancelled());
            assert!(!done.load(Ordering::SeqCst));

            open.send(()).unwrap();
            trigger.trigger();

            assert_eq!(tasks.drain(LIMIT).await, Drained::Clean);
            assert!(done.load(Ordering::SeqCst));
        });
    }

    #[test]
    fn a_blocking_task_writes_its_file_after_its_caller_is_dropped() {
        let root = TempRoot::new().unwrap();
        let file = root.path().join("ledger");

        runtime().block_on(async {
            let (trigger, tasks) = new_tasks();
            let (open, gate) = mpsc::channel::<()>();
            let (start, started) = oneshot::channel::<()>();

            let caller = tokio::spawn({
                let tasks = tasks.clone();
                let file = file.clone();

                async move {
                    let work = move || {
                        start.send(()).unwrap();
                        gate.recv().unwrap();
                        std::fs::write(&file, b"one line\n").unwrap();
                    };

                    tasks.spawn_blocking("ledger-write", work).await
                }
            });
            started.await.unwrap();
            caller.abort();

            assert!(caller.await.unwrap_err().is_cancelled());
            assert!(!file.exists());

            open.send(()).unwrap();
            trigger.trigger();

            assert_eq!(tasks.drain(LIMIT).await, Drained::Clean);
        });

        assert_eq!(std::fs::read(&file).unwrap(), b"one line\n");
    }

    #[test]
    fn a_dropped_completion_does_not_stop_its_task() {
        runtime().block_on(async {
            let (_trigger, tasks) = new_tasks();
            let (open, gate) = oneshot::channel::<()>();
            let (tell, told) = oneshot::channel::<u8>();

            drop(tasks.spawn_must_complete("ledger-write", async move {
                gate.await.unwrap();
                tell.send(7).unwrap();
            }));
            open.send(()).unwrap();

            assert_eq!(told.await, Ok(7));
            assert_eq!(tasks.drain(LIMIT).await, Drained::Clean);
        });
    }

    #[test]
    fn the_stop_signal_does_not_stop_a_task() {
        runtime().block_on(async {
            let (trigger, tasks) = new_tasks();
            let (open, gate) = oneshot::channel::<()>();
            let written = tasks.spawn_must_complete("ledger-write", async move {
                gate.await.unwrap();

                7_u8
            });

            trigger.trigger();
            tokio::task::yield_now().await;
            open.send(()).unwrap();

            assert_eq!(written.await, Ok(7));
        });
    }

    #[test]
    fn a_completion_is_cancel_safe() {
        runtime().block_on(async {
            let (_trigger, tasks) = new_tasks();
            let (open, gate) = oneshot::channel::<()>();
            let mut written = tasks.spawn_must_complete("ledger-write", async move {
                gate.await.unwrap();

                7_u8
            });

            // Each turn drops a wait that did not end.
            for _ in 0..3 {
                let early = tokio::select! {
                    biased;
                    result = &mut written => Some(result),
                    () = tokio::task::yield_now() => None,
                };

                assert_eq!(early, None);
            }

            open.send(()).unwrap();

            assert_eq!((&mut written).await, Ok(7));
        });
    }

    #[test]
    fn a_poll_after_the_output_gives_cancelled_and_does_not_panic() {
        runtime().block_on(async {
            let (_trigger, tasks) = new_tasks();
            let mut written = tasks.spawn_must_complete("ledger-write", async { 7_u8 });

            assert_eq!((&mut written).await, Ok(7));
            assert_eq!((&mut written).await, Err(TaskLost::Cancelled));
            assert_eq!(written.await, Err(TaskLost::Cancelled));
        });
    }

    #[test]
    fn a_task_gives_cancelled_when_its_runtime_stops_first() {
        let (_trigger, tasks) = new_tasks();
        let first = runtime();
        let written = {
            let _inside = first.enter();

            tasks.spawn_must_complete("ledger-write", std::future::pending::<()>())
        };
        drop(first);

        let late = runtime();

        assert_eq!(late.block_on(written), Err(TaskLost::Cancelled));
        assert_eq!(late.block_on(tasks.drain(LIMIT)), Drained::Clean);
    }

    #[test]
    fn a_drain_with_no_task_is_clean_at_once() {
        runtime().block_on(async {
            let (_trigger, tasks) = new_tasks();

            assert_eq!(tasks.drain(Duration::ZERO).await, Drained::Clean);
            assert_eq!(tasks.drain(Duration::ZERO).await, Drained::Clean);
        });
    }

    #[test]
    fn a_drain_ends_with_its_last_task_and_not_at_its_limit() {
        paused_runtime().block_on(async {
            let (trigger, tasks) = new_tasks();

            drop(tasks.spawn_must_complete("ledger-write", tokio::time::sleep(SHORT)));
            trigger.trigger();
            let start = tokio::time::Instant::now();

            assert_eq!(tasks.drain(LIMIT).await, Drained::Clean);
            assert_eq!(start.elapsed(), SHORT);
        });
    }

    #[test]
    fn a_drain_past_its_limit_gives_the_count_of_the_tasks_that_still_run() {
        paused_runtime().block_on(async {
            let (trigger, tasks) = new_tasks();
            let (open_first, first_gate) = oneshot::channel::<()>();
            let (open_second, second_gate) = oneshot::channel::<()>();

            drop(tasks.spawn_must_complete("first", async move { first_gate.await.unwrap() }));
            drop(tasks.spawn_must_complete("second", async move { second_gate.await.unwrap() }));
            drop(tasks.spawn_must_complete("third", async {}));
            trigger.trigger();

            assert_eq!(tasks.drain(SHORT).await, Drained::TimedOut { left: 2 });
            assert_eq!(
                tasks.drain(Duration::ZERO).await,
                Drained::TimedOut { left: 2 }
            );

            open_first.send(()).unwrap();

            assert_eq!(tasks.drain(SHORT).await, Drained::TimedOut { left: 1 });

            open_second.send(()).unwrap();

            assert_eq!(tasks.drain(SHORT).await, Drained::Clean);
        });
    }

    #[test]
    fn a_drain_counts_a_blocking_task_that_still_runs() {
        runtime().block_on(async {
            let (trigger, tasks) = new_tasks();
            let (open, gate) = mpsc::channel::<()>();
            let (start, started) = oneshot::channel::<()>();
            let synced = tasks.spawn_blocking("index-sync", move || {
                start.send(()).unwrap();
                gate.recv().unwrap();
            });
            started.await.unwrap();
            trigger.trigger();

            assert_eq!(
                tasks.drain(Duration::from_millis(20)).await,
                Drained::TimedOut { left: 1 }
            );

            open.send(()).unwrap();

            assert_eq!(tasks.drain(LIMIT).await, Drained::Clean);
            assert_eq!(synced.await, Ok(()));
        });
    }

    #[test]
    fn a_drain_is_cancel_safe() {
        paused_runtime().block_on(async {
            let (trigger, tasks) = new_tasks();
            let (open, gate) = oneshot::channel::<()>();
            let done = Arc::new(AtomicBool::new(false));

            drop(tasks.spawn_must_complete("ledger-write", {
                let done = Arc::clone(&done);

                async move {
                    gate.await.unwrap();
                    done.store(true, Ordering::SeqCst);
                }
            }));
            trigger.trigger();

            // The other branch ends first, so the select drops the drain.
            let early = tokio::select! {
                biased;
                drained = tasks.drain(LIMIT) => Some(drained),
                () = tokio::time::sleep(SHORT) => None,
            };

            assert_eq!(early, None);
            assert!(!done.load(Ordering::SeqCst));

            open.send(()).unwrap();

            assert_eq!(tasks.drain(LIMIT).await, Drained::Clean);
            assert!(done.load(Ordering::SeqCst));
        });
    }

    #[test]
    fn a_drain_waits_for_a_task_that_starts_during_the_drain() {
        runtime().block_on(async {
            let (trigger, tasks) = new_tasks();
            let (open, gate) = oneshot::channel::<()>();
            let done = Arc::new(AtomicBool::new(false));

            // The first task starts the second task on a clone, after the
            // drain closed the set.
            drop(tasks.spawn_must_complete("first", {
                let tasks = tasks.clone();
                let done = Arc::clone(&done);

                async move {
                    gate.await.unwrap();
                    drop(tasks.spawn_must_complete("second", async move {
                        tokio::task::yield_now().await;
                        done.store(true, Ordering::SeqCst);
                    }));
                }
            }));
            trigger.trigger();

            let drain = tokio::spawn({
                let tasks = tasks.clone();

                async move { tasks.drain(LIMIT).await }
            });
            tokio::task::yield_now().await;
            open.send(()).unwrap();

            assert_eq!(drain.await.unwrap(), Drained::Clean);
            assert!(done.load(Ordering::SeqCst));
        });
    }

    #[test]
    fn a_loop_runs_a_pass_and_then_waits_for_the_pause() {
        paused_runtime().block_on(async {
            let (trigger, tasks) = new_tasks();
            let passes = Arc::new(AtomicUsize::new(0));

            tasks.spawn_loop("upkeep", PAUSE, counting(&passes));
            tokio::task::yield_now().await;
            tokio::task::yield_now().await;

            // The first pass has no pause before it.
            assert_eq!(passes.load(Ordering::SeqCst), 1);

            tokio::time::sleep(FOUR_PASSES).await;

            assert_eq!(passes.load(Ordering::SeqCst), 4);

            trigger.trigger();

            // The loop waits for its pause now. The stop signal ends that
            // wait at once.
            assert_eq!(tasks.drain(Duration::from_secs(1)).await, Drained::Clean);

            tokio::time::sleep(FOUR_PASSES).await;

            assert_eq!(passes.load(Ordering::SeqCst), 4);
        });
    }

    #[test]
    fn the_pause_of_a_loop_starts_at_the_end_of_the_pass() {
        paused_runtime().block_on(async {
            let (trigger, tasks) = new_tasks();
            let passes = Arc::new(AtomicUsize::new(0));

            tasks.spawn_loop("upkeep", PAUSE, {
                let passes = Arc::clone(&passes);

                move || {
                    let passes = Arc::clone(&passes);

                    async move {
                        passes.fetch_add(1, Ordering::SeqCst);
                        tokio::time::sleep(PAUSE).await;
                    }
                }
            });

            // A pass and its pause are 20 seconds. The passes start at 0, at
            // 20 and at 40 seconds.
            tokio::time::sleep(Duration::from_secs(45)).await;

            assert_eq!(passes.load(Ordering::SeqCst), 3);

            trigger.trigger();

            assert_eq!(tasks.drain(LIMIT).await, Drained::Clean);
        });
    }

    #[test]
    fn a_pass_in_flight_runs_to_its_end_after_the_stop_signal() {
        paused_runtime().block_on(async {
            let (trigger, tasks) = new_tasks();
            let (open, gate) = oneshot::channel::<()>();
            let mut gate = Some(gate);
            let passes = Arc::new(AtomicUsize::new(0));
            let done = Arc::new(AtomicBool::new(false));

            tasks.spawn_loop("upkeep", PAUSE, {
                let passes = Arc::clone(&passes);
                let done = Arc::clone(&done);

                move || {
                    let gate = gate.take();
                    let done = Arc::clone(&done);
                    passes.fetch_add(1, Ordering::SeqCst);

                    async move {
                        if let Some(gate) = gate {
                            gate.await.unwrap();
                        }

                        done.store(true, Ordering::SeqCst);
                    }
                }
            });
            tokio::task::yield_now().await;
            tokio::task::yield_now().await;
            trigger.trigger();

            // The loop is one task, and its pass is one more.
            assert_eq!(tasks.drain(SHORT).await, Drained::TimedOut { left: 2 });
            assert!(!done.load(Ordering::SeqCst));

            open.send(()).unwrap();

            assert_eq!(tasks.drain(SHORT).await, Drained::Clean);
            assert!(done.load(Ordering::SeqCst));
            assert_eq!(passes.load(Ordering::SeqCst), 1);
        });
    }

    #[test]
    fn a_loop_that_starts_after_the_stop_signal_runs_no_pass() {
        paused_runtime().block_on(async {
            let (trigger, tasks) = new_tasks();
            let passes = Arc::new(AtomicUsize::new(0));
            trigger.trigger();

            tasks.spawn_loop("upkeep", PAUSE, counting(&passes));

            assert_eq!(tasks.drain(SHORT).await, Drained::Clean);
            assert_eq!(passes.load(Ordering::SeqCst), 0);
        });
    }

    #[test]
    fn a_lock_with_no_poison_gives_its_value() {
        let ledger = Mutex::new(vec![1_u8]);

        locked(&ledger).push(2);

        assert_eq!(*locked(&ledger), [1, 2]);
        assert!(!ledger.is_poisoned());
    }

    /// The child of [`each_scenario_writes_its_lines_and_no_panic_message`].
    /// Without the variable it does nothing. With the variable it sets the
    /// panic hook, as a service does, and runs the one scenario that the
    /// variable names.
    #[test]
    fn the_child_runs_one_scenario() {
        let Some(name) = std::env::var_os(CHILD_VARIABLE) else {
            return;
        };
        let scenario = SCENARIOS
            .iter()
            .find(|scenario| name == scenario.name)
            .unwrap();

        crate::log::init(CHILD_PROGRAM);
        (scenario.run)();
    }

    #[test]
    fn each_scenario_writes_its_lines_and_no_panic_message() {
        let start = format!(" ERROR {LOG_TARGET} ");

        for scenario in SCENARIOS {
            let name = scenario.name;
            let child = run_child(name);
            let stdout = String::from_utf8(child.stdout).unwrap();
            let stderr = String::from_utf8(child.stderr).unwrap();

            assert!(child.status.success(), "{name}: {stdout}\n{stderr}");
            assert!(stdout.contains("1 passed"), "{name}: {stdout}");

            let written: Vec<&str> = stderr
                .lines()
                .filter_map(|line| line.split_once(&start))
                .map(|(_, message)| message)
                .collect();

            assert_eq!(written, scenario.lines, "{name}: {stderr}");
            assert!(!stderr.contains(PANIC_MESSAGE), "{name}: {stderr}");
            assert!(!stdout.contains(PANIC_MESSAGE), "{name}: {stdout}");
        }
    }

    /// A task panics. Its caller gets `Panicked`, and the next task of the
    /// same set gives its value.
    fn a_task_panics() {
        runtime().block_on(async {
            let (trigger, tasks) = new_tasks();

            let written = tasks.spawn_must_complete("ledger-write", async {
                tokio::task::yield_now().await;

                panic!("{PANIC_MESSAGE}")
            });
            let lost: Result<(), TaskLost> = written.await;

            assert_eq!(lost, Err(TaskLost::Panicked));

            let next = tasks.spawn_must_complete("index-sync", async { 7_u8 });

            assert_eq!(next.await, Ok(7));

            trigger.trigger();

            assert_eq!(tasks.drain(LIMIT).await, Drained::Clean);
        });
    }

    /// A task panics after its caller dropped the `Completion`. The task
    /// writes the line itself.
    fn a_task_with_no_caller_panics() {
        runtime().block_on(async {
            let (_trigger, tasks) = new_tasks();
            let (open, gate) = oneshot::channel::<()>();

            drop(tasks.spawn_must_complete("ledger-write", async move {
                gate.await.unwrap();

                panic!("{PANIC_MESSAGE}")
            }));
            open.send(()).unwrap();

            assert_eq!(tasks.drain(LIMIT).await, Drained::Clean);
        });
    }

    fn a_blocking_task_panics() {
        runtime().block_on(async {
            let (_trigger, tasks) = new_tasks();

            let synced = tasks.spawn_blocking("index-sync", || panic!("{PANIC_MESSAGE}"));
            let lost: Result<(), TaskLost> = synced.await;

            assert_eq!(lost, Err(TaskLost::Panicked));

            let next = tasks.spawn_blocking("ledger-write", || 7_u8);

            assert_eq!(next.await, Ok(7));
            assert_eq!(tasks.drain(LIMIT).await, Drained::Clean);
        });
    }

    /// The second pass of a loop panics. The loop continues with the third
    /// pass.
    fn a_pass_panics() {
        paused_runtime().block_on(async {
            let (trigger, tasks) = new_tasks();
            let passes = Arc::new(AtomicUsize::new(0));

            tasks.spawn_loop("upkeep", PAUSE, {
                let passes = Arc::clone(&passes);

                move || {
                    let passes = Arc::clone(&passes);

                    async move {
                        tokio::task::yield_now().await;

                        assert!(
                            passes.fetch_add(1, Ordering::SeqCst) != 1,
                            "{PANIC_MESSAGE}"
                        );
                    }
                }
            });
            tokio::time::sleep(FOUR_PASSES).await;

            assert_eq!(passes.load(Ordering::SeqCst), 4);

            trigger.trigger();

            assert_eq!(tasks.drain(LIMIT).await, Drained::Clean);
        });
    }

    /// The second call of the body panics before it gives a future.
    fn the_call_of_a_body_panics() {
        paused_runtime().block_on(async {
            let (trigger, tasks) = new_tasks();
            let passes = Arc::new(AtomicUsize::new(0));

            tasks.spawn_loop("upkeep", PAUSE, {
                let passes = Arc::clone(&passes);

                move || {
                    assert!(
                        passes.fetch_add(1, Ordering::SeqCst) != 1,
                        "{PANIC_MESSAGE}"
                    );

                    std::future::ready(())
                }
            });
            tokio::time::sleep(FOUR_PASSES).await;

            assert_eq!(passes.load(Ordering::SeqCst), 4);

            trigger.trigger();

            assert_eq!(tasks.drain(LIMIT).await, Drained::Clean);
        });
    }

    /// Each pass panics. The loop still runs a pass after each pause, and
    /// each pass writes its line.
    fn each_pass_panics() {
        paused_runtime().block_on(async {
            let (trigger, tasks) = new_tasks();
            let passes = Arc::new(AtomicUsize::new(0));

            tasks.spawn_loop("upkeep", PAUSE, {
                let passes = Arc::clone(&passes);

                move || {
                    let passes = Arc::clone(&passes);

                    async move {
                        passes.fetch_add(1, Ordering::SeqCst);

                        panic!("{PANIC_MESSAGE}")
                    }
                }
            });
            tokio::time::sleep(FOUR_PASSES).await;

            assert_eq!(passes.load(Ordering::SeqCst), 4);

            trigger.trigger();

            assert_eq!(tasks.drain(LIMIT).await, Drained::Clean);
        });
    }

    /// A runtime with no timer. The pause of the loop panics there. The loop
    /// writes one line and ends, and its first pass ran.
    fn a_loop_has_no_timer() {
        let no_timer = Builder::new_current_thread().build().unwrap();

        no_timer.block_on(async {
            let (_trigger, tasks) = new_tasks();
            let passes = Arc::new(AtomicUsize::new(0));

            tasks.spawn_loop("upkeep", PAUSE, counting(&passes));

            for _ in 0..YIELDS {
                tokio::task::yield_now().await;
            }

            assert_eq!(passes.load(Ordering::SeqCst), 1);
            assert!(tasks.tracker.is_empty());
        });
    }

    /// No runtime runs on the thread. Each start writes one line and starts
    /// nothing.
    fn no_runtime_runs() {
        let (_trigger, tasks) = new_tasks();
        let passes = Arc::new(AtomicUsize::new(0));

        let written = tasks.spawn_must_complete("ledger-write", async { 7_u8 });
        let synced = tasks.spawn_blocking("index-sync", || 7_u8);
        tasks.spawn_loop("upkeep", PAUSE, counting(&passes));

        assert!(tasks.tracker.is_empty());

        let late = runtime();

        assert_eq!(late.block_on(written), Err(TaskLost::Cancelled));
        assert_eq!(late.block_on(synced), Err(TaskLost::Cancelled));
        assert_eq!(late.block_on(tasks.drain(LIMIT)), Drained::Clean);
        assert_eq!(passes.load(Ordering::SeqCst), 0);
    }

    /// A panic poisons a lock two times. Each caller after it gets the
    /// value, and each poison gives one line.
    fn a_lock_is_poisoned() {
        fn poison(ledger: &Arc<Mutex<Vec<u8>>>, entry: u8) {
            let ledger = Arc::clone(ledger);
            let holder = thread::spawn(move || {
                let mut guard = ledger.lock().unwrap();
                guard.push(entry);

                panic!("{PANIC_MESSAGE}");
            });

            assert!(holder.join().is_err());
        }

        let ledger = Arc::new(Mutex::new(vec![1_u8]));
        poison(&ledger, 2);

        assert!(ledger.is_poisoned());

        // The first caller after the panic gets the value as the panic left
        // it.
        {
            let mut guard = locked(&ledger);

            assert_eq!(*guard, [1, 2]);

            guard.push(3);
        }

        // The next callers get the value too, with no second line.
        assert!(!ledger.is_poisoned());
        assert_eq!(*locked(&ledger), [1, 2, 3]);
        assert_eq!(*locked(&ledger), [1, 2, 3]);

        poison(&ledger, 4);

        assert_eq!(*locked(&ledger), [1, 2, 3, 4]);
        assert_eq!(*locked(&ledger), [1, 2, 3, 4]);
    }

    /// A work whose `poll` gives `at_poll`, and whose drop panics.
    struct Bomb {
        at_poll: Option<u8>,
    }

    impl Future for Bomb {
        type Output = u8;

        fn poll(self: Pin<&mut Self>, _context: &mut Context<'_>) -> Poll<u8> {
            match self.at_poll {
                Some(value) => Poll::Ready(value),
                None => panic!("{PANIC_MESSAGE}"),
            }
        }
    }

    impl Drop for Bomb {
        fn drop(&mut self) {
            // The drop that follows a caught panic. A panic in a drop of an
            // unwind stops the process, and no code can guard that one.
            assert!(thread::panicking(), "{PANIC_MESSAGE}");
        }
    }

    /// The poll of a work panics, and then its drop panics. The task has one
    /// result and one line for the two.
    fn the_drop_of_a_work_panics_too() {
        runtime().block_on(async {
            let (_trigger, tasks) = new_tasks();

            let written = tasks.spawn_must_complete("ledger-write", Bomb { at_poll: None });

            assert_eq!(written.await, Err(TaskLost::Panicked));
            assert_eq!(tasks.drain(LIMIT).await, Drained::Clean);
        });
    }

    /// A work gives its value, and then its drop panics. The task gives
    /// `Panicked` and one line.
    fn the_drop_of_a_ready_work_panics() {
        runtime().block_on(async {
            let (_trigger, tasks) = new_tasks();

            let written = tasks.spawn_must_complete("ledger-write", Bomb { at_poll: Some(7) });

            assert_eq!(written.await, Err(TaskLost::Panicked));
            assert_eq!(tasks.drain(LIMIT).await, Drained::Clean);
        });
    }
}
