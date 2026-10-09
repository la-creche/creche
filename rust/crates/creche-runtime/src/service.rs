//! The steps of the start of a program.
//!
//! Each program of the platform runs inside [`run`]: a daemon, a verify hook
//! and a command that runs to its end. [`run`] builds the one `tokio` runtime
//! of the process. The attribute `#[tokio::main]` is not for a service: its
//! code calls `expect`.
//!
//! The parse of the config runs before the runtime starts, and no `await` is
//! in it. [`load`] applies the failure action of the config type
//! (`rust/AGENTS.md`, "The config of a process"). A start that fails for a
//! reason outside the config type goes through [`refuse_start`]. Three
//! examples are a wrong word of the command line, a token file that is not
//! valid and an `https` URL. Each refused start of a daemon ends with
//! `EX_CONFIG`. A unit with `RestartPreventExitStatus=78` then stays
//! stopped.
//!
//! # The form of a `main`
//!
//! The `main` of a program has three steps and no other code
//! (`rust/AGENTS.md`, "The panic rule"):
//!
//! 1. Call [`log::init`] with the name of the program.
//! 2. Build the variables one time, with `Env::from_os(std::env::vars_os())`.
//! 3. Return the `ExitCode` of the entry function of the library.
//!
//! The entry function of a daemon has these steps, inside [`enter`]:
//!
//! 1. Read the arguments with [`args`](crate::args). Give a wrong word to
//!    [`refuse_start`].
//! 2. Read each file that the config needs.
//! 3. Give the result of the parse of the config to [`load`].
//! 4. Read each token file and build each
//!    [`Target`](crate::http::client::Target). Give the error of one to
//!    [`refuse_start`].
//! 5. For the flag `--check`, write the report with [`log::out_line`] and
//!    return status 0. Bind nothing.
//! 6. Call [`run`]. Its `main` binds each listener, calls [`ready`] and
//!    serves until the stop signal.
//!
//! In the example, the config type of the noticeboard stands for the config
//! type of the service.
//!
//! ```no_run
//! use std::ffi::OsString;
//! use std::process::ExitCode;
//! use std::time::Duration;
//!
//! use axum::Router;
//! use axum::routing::get;
//! use creche_contracts::config::noticeboard::NoticeboardConfig;
//! use creche_contracts::config::{BindAddress, Env};
//! use creche_runtime::args::{Args, UsageError, Word};
//! use creche_runtime::http::client::Target;
//! use creche_runtime::http::layers::starlette::StarletteBodies;
//! use creche_runtime::http::layers::{AccessLog, edge};
//! use creche_runtime::http::server::{Listen, bind, serve};
//! use creche_runtime::layout::StateRoot;
//! use creche_runtime::service::{self, Context, Loaded, Program, Threads};
//! use creche_runtime::token::{self, TokenRule};
//! use creche_runtime::{error, log};
//!
//! const NAME: &str = "example-service";
//!
//! /// How long the listeners wait for an open request after the stop
//! /// signal.
//! const OPEN_REQUESTS: Duration = Duration::from_secs(10);
//!
//! /// How long the program waits for a task after its `main` returned. A
//! /// handler that still runs then has this time. `OPEN_REQUESTS` plus
//! /// `DRAIN` is less than `TimeoutStopSec=` of the unit of the service.
//! const DRAIN: Duration = Duration::from_secs(8);
//!
//! // The whole `main` of the program.
//! fn main() -> ExitCode {
//!     log::init(NAME);
//!     let env = Env::from_os(std::env::vars_os());
//!
//!     entry(std::env::args_os(), &env)
//! }
//!
//! /// The entry function of the library.
//! fn entry(words: impl IntoIterator<Item = OsString>, env: &Env) -> ExitCode {
//!     service::enter(NAME, || start(words, env))
//! }
//!
//! /// What the command line asks for.
//! enum Asked {
//!     Serve,
//!     Check,
//! }
//!
//! fn read_words(words: impl IntoIterator<Item = OsString>) -> Result<Asked, UsageError> {
//!     let mut asked = Asked::Serve;
//!
//!     for word in Args::from_os(words)? {
//!         match word {
//!             Word::Flag(flag) if flag == "--check" => asked = Asked::Check,
//!             Word::Flag(word) | Word::Value(word) => {
//!                 return Err(UsageError::Unexpected { word });
//!             }
//!         }
//!     }
//!
//!     Ok(asked)
//! }
//!
//! fn start(words: impl IntoIterator<Item = OsString>, env: &Env) -> ExitCode {
//!     // A daemon ends with `EX_CONFIG` for a wrong word too.
//!     let asked = match read_words(words) {
//!         Ok(asked) => asked,
//!         Err(error) => return service::refuse_start(NAME, &error),
//!     };
//!     let config = match service::load(NAME, NoticeboardConfig::from_env(env)) {
//!         Loaded::Run(config) => config,
//!         Loaded::Exit(status) => return status,
//!         // The type of this config says that the program exits. A config
//!         // whose type says `RefuseEachCall` starts a router that refuses
//!         // each call here.
//!         Loaded::RefuseEachCall(errors) => return service::refuse_start(NAME, &errors),
//!     };
//!
//!     let token_file = StateRoot::new(config.state_root().clone())
//!         .tokens_dir()
//!         .join("view-ro.token");
//!     let _token = match token::read(&token_file, TokenRule::ATTENDANCE) {
//!         Ok(token) => token,
//!         Err(error) => return service::refuse_start(NAME, &error),
//!     };
//!     let _attendance = match Target::try_from(config.attendance()) {
//!         Ok(target) => target,
//!         Err(error) => return service::refuse_start(NAME, &error),
//!     };
//!
//!     if let Asked::Check = asked {
//!         log::out_line(&format!("bind {}:{}", config.bind().as_str(), config.port().get()));
//!
//!         return ExitCode::SUCCESS;
//!     }
//!
//!     let program = Program::new(NAME, Threads::One, DRAIN);
//!
//!     service::run(program, |context| serve_until_the_stop(context, config))
//! }
//!
//! async fn serve_until_the_stop(context: Context, config: NoticeboardConfig) -> ExitCode {
//!     let address = BindAddress::on(config.bind().clone(), config.port());
//!     // A bind that fails is no refused start: the address can be free at
//!     // the next start.
//!     let bound = match bind(Listen::Tcp(address)).await {
//!         Ok(bound) => vec![bound],
//!         Err(failure) => {
//!             error!(NAME, "{failure}");
//!
//!             return ExitCode::FAILURE;
//!         }
//!     };
//!     service::ready(&bound);
//!
//!     let routes = Router::new().route("/healthz", get(|| async { "ok" }));
//!     let app = edge(routes, StarletteBodies, context.tasks().clone(), AccessLog::Skip);
//!
//!     match serve(bound, app, context.tasks(), OPEN_REQUESTS).await {
//!         Ok(_drained) => ExitCode::SUCCESS,
//!         Err(failure) => {
//!             error!(NAME, "{failure}");
//!
//!             ExitCode::FAILURE
//!         }
//!     }
//! }
//! ```
//!
//! A program that runs to its end has the same form, with [`Threads::One`].
//! Three examples are a verify hook, the command that fires one trigger and
//! the door for a terminal. Such a program states its own exit status for a
//! check that fails. It does not use [`load`] when a config that is not valid
//! is one of its results.
//!
//! ```no_run
//! use std::process::ExitCode;
//! use std::time::Duration;
//!
//! use creche_contracts::config::Env;
//! use creche_contracts::config::noticeboard::NoticeboardConfig;
//! use creche_runtime::log;
//! use creche_runtime::service::{self, Context, Program, Threads};
//!
//! const NAME: &str = "example-verify";
//!
//! /// How long the program waits for a task after its `main` returned.
//! const DRAIN: Duration = Duration::from_secs(5);
//!
//! // The whole `main` of the program.
//! fn main() -> ExitCode {
//!     log::init(NAME);
//!     let env = Env::from_os(std::env::vars_os());
//!
//!     entry(&env)
//! }
//!
//! /// The entry function of the library.
//! fn entry(env: &Env) -> ExitCode {
//!     service::enter(NAME, || {
//!         let Ok(config) = NoticeboardConfig::from_env(env) else {
//!             log::out_line("FAIL config");
//!
//!             return ExitCode::FAILURE;
//!         };
//!         let program = Program::new(NAME, Threads::One, DRAIN);
//!
//!         service::run(program, |context| check(context, config))
//!     })
//! }
//!
//! async fn check(context: Context, config: NoticeboardConfig) -> ExitCode {
//!     let root = config.state_root().as_path().to_owned();
//!     // A call of `std::fs` blocks, so it runs on the pool for such calls.
//!     let listed = context
//!         .tasks()
//!         .spawn_blocking("list-state-root", move || std::fs::read_dir(root).is_ok());
//!
//!     if listed.await == Ok(true) {
//!         log::out_line("PASS state root");
//!
//!         ExitCode::SUCCESS
//!     } else {
//!         log::out_line("FAIL state root");
//!
//!         ExitCode::FAILURE
//!     }
//! }
//! ```
//!
//! # The panic hook and the last boundary
//!
//! [`enter`], [`run`], [`load`] and [`refuse_start`] each set the panic hook
//! of the process first, as [`log::init`] does, with the name of the
//! program. The `main` of a program calls `log::init` first. The four hold
//! the hook for a `main` that omits the call. A test that calls one of the
//! four in its own process replaces the panic hook of the test program. So
//! the rest of each body is in a private function, and a test calls that
//! function.
//!
//! [`enter`] and [`run`] are the last boundary for a panic
//! (`rust/AGENTS.md`, "The panic rule", clause 7). A panic that no other
//! boundary takes ends the program there, with one `ERROR` line and exit
//! status 1. The status of a panic is never `EX_CONFIG`.
//!
//! # No readiness signal
//!
//! A service tells no program that it is ready: each unit has `Type=simple`.
//! [`ready`] writes one line for each listener, for the operator. No program
//! reads it.
//!
//! # The Python origin
//!
//! The Python origin is the `main` of each service:
//!
//! | The program | Where |
//! |---|---|
//! | `attendance` | `attendance/src/attendance/__main__.py:47-65` |
//! | The noticeboard | `noticeboard/src/noticeboard/__main__.py:32-52` |
//! | The Open WebUI door | `door-owui/src/agent_door_owui/__main__.py:23-43` |
//! | The chaperone | `chaperone/src/chaperone/__main__.py:105-180` |
//! | The trigger door | `door-trigger/src/agent_door_trigger/cli.py:48-59` and `:197-209` |
//! | `caregiver` | `caregiver/src/caregiver/cli.py:442-500` and `:717-740` |
//!
//! The module differs from those origins in these ways. A plain test holds
//! each one.
//!
//! - A Python service ends a refused start with status 2, with status 1 or
//!   with status 78 (`attendance/src/attendance/__main__.py:32`,
//!   `door-owui/src/agent_door_owui/__main__.py:36`,
//!   `chaperone/src/chaperone/__main__.py:192`). [`load`] and
//!   [`refuse_start`] give `EX_CONFIG` for each one.
//! - A Python service writes the errors of its config as one line. [`load`]
//!   writes one line for each error.
//! - `uvicorn` waits with no limit for its open requests at a stop. [`run`]
//!   waits for the drain limit of the program after its `main` returned.
//! - A Python service that `uvicorn.run` starts ends by the signal itself
//!   after a stop signal. A program here ends with the status of its `main`.
//! - An exception that no code handles ends a Python program with a trace
//!   and status 1. A panic that no boundary takes ends a program here with
//!   two `ERROR` lines and status 1. The lines hold the place of the panic
//!   and never its message.

use std::fmt;
use std::future::Future;
use std::io;
use std::num::NonZeroUsize;
use std::panic::{self, AssertUnwindSafe};
use std::pin::Pin;
use std::process::ExitCode;
use std::sync::Arc;
use std::task::{self, Poll};
use std::time::{Duration, Instant};

use creche_contracts::config::roster::RosterErrors;
use creche_contracts::config::site::SiteErrors;
use creche_contracts::config::{self, Checked, ConfigErrors, EX_CONFIG, Start};
use tokio::runtime::{Builder, Runtime};

use crate::clock::SystemClock;
use crate::entropy::OsEntropy;
use crate::http::server::Bound;
use crate::log;
use crate::readfile::os_text;
use crate::signals::{self, Hangups, OnHangup, SignalError};
use crate::tasks::{Drained, Shutdown, ShutdownTrigger, Tasks, shutdown_pair};

/// The target of each line that this module writes to the log.
const LOG_TARGET: &str = "service";

// CONTRACT-QUESTION: no contract and no answer of the owner names the exit
// status of a program that the operating system gives no runtime or no
// signal handler. `rust/AGENTS.md`, "The rules for a service", rule 17 lists
// the causes of a refused start, and this cause is not one of them. The
// reading here is `EX_OSERR`, status 71: a restart can repair the cause, so
// systemd must start the unit again. A Python service continues with no
// handler on a system that gives it none. A change costs one constant.
//
// The same question is open for a listener that does not bind. A Python
// service ends with status 3 there, which is the status of `uvicorn` for a
// start that failed (`uvicorn/server.py:183`). The first example of this
// module returns the failure status of the standard library. Each service
// states that status in its own `main`.
/// The exit status `EX_OSERR` of `sysexits.h`: the operating system gave the
/// program no runtime or no signal handler. A restart can repair that, so
/// the status is not `EX_CONFIG`.
const EX_OSERR: u8 = 71;

/// The exit status of a program that a panic ended (`rust/AGENTS.md`, "The
/// panic rule", clause 7).
const PANIC_STATUS: u8 = 1;

/// How many threads run the tasks of a program.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Threads {
    /// One thread: the thread of `main`. Use it for a program that runs to
    /// its end.
    One,
    /// This count of worker threads. Two handlers then run at one time, so
    /// each piece of state needs one owner task or a lock.
    Workers(NonZeroUsize),
}

/// What [`run`] must know about a program.
///
/// A program names three values: its name, its count of threads and its
/// drain limit. A program with a reload also names [`OnHangup::Reload`].
///
/// The drain limit is the longest time that the program runs after its
/// `main` returned. The limit starts at that return, also when a stop signal
/// came before it. It holds the wait for the tracked tasks and the stop of
/// the runtime.
///
/// systemd kills the process at `TimeoutStopSec=` of its unit. The time that
/// `main` uses after the stop signal plus the drain limit must thus be less
/// than that value. The unit files are in `systemd/`. A service names its
/// own unit beside its drain constant.
///
/// A `main` that serves gives [`serve`](crate::http::server::serve) a limit
/// of its own for the open requests. That limit is a part of the time that
/// `main` uses after the stop signal. A handler that still runs at that
/// limit is a tracked task, and it has the drain limit.
///
/// The type has no Python origin. A Python service gives the same facts to
/// `uvicorn` and to its signal calls, for example
/// `attendance/src/attendance/__main__.py:189-208`.
///
/// ```
/// use std::time::Duration;
///
/// use creche_runtime::service::{Program, Threads};
/// use creche_runtime::signals::OnHangup;
///
/// let program = Program::new("attendance", Threads::One, Duration::from_secs(55))
///     .with_on_hangup(OnHangup::Reload);
///
/// assert_eq!(program.name(), "attendance");
/// assert_eq!(program.threads(), Threads::One);
/// assert_eq!(program.on_hangup(), OnHangup::Reload);
/// assert_eq!(program.drain(), Duration::from_secs(55));
/// ```
///
/// Code outside this module cannot build a value from raw parts. It starts
/// with [`Program::new`], so each program states its drain limit:
///
/// ```compile_fail,E0451
/// use std::time::Duration;
///
/// use creche_runtime::service::{Program, Threads};
/// use creche_runtime::signals::OnHangup;
///
/// let program = Program {
///     name: "attendance",
///     threads: Threads::One,
///     on_hangup: OnHangup::Reload,
///     drain: Duration::from_secs(55),
/// };
/// ```
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Program {
    /// The name of the program: the target of the line of its panic hook.
    name: &'static str,
    /// How many threads run its tasks.
    threads: Threads,
    /// What the program does at SIGHUP.
    on_hangup: OnHangup,
    /// How long the program runs after its `main` returned, at most.
    drain: Duration,
}

impl Program {
    /// The program with this name, this count of threads and this drain
    /// limit. It has no SIGHUP handler: [`OnHangup::DefaultAction`]. SIGHUP
    /// then ends the process, as it ends a Python service that installs no
    /// handler.
    #[must_use]
    pub const fn new(name: &'static str, threads: Threads, drain: Duration) -> Self {
        Self {
            name,
            threads,
            on_hangup: OnHangup::DefaultAction,
            drain,
        }
    }

    /// The program with this action at SIGHUP.
    #[must_use]
    pub const fn with_on_hangup(mut self, on_hangup: OnHangup) -> Self {
        self.on_hangup = on_hangup;

        self
    }

    /// The name of the program.
    #[must_use]
    pub const fn name(&self) -> &'static str {
        self.name
    }

    /// How many threads run the tasks of the program.
    #[must_use]
    pub const fn threads(&self) -> Threads {
        self.threads
    }

    /// What the program does at SIGHUP.
    #[must_use]
    pub const fn on_hangup(&self) -> OnHangup {
        self.on_hangup
    }

    /// How long the program runs after its `main` returned, at most.
    #[must_use]
    pub const fn drain(&self) -> Duration {
        self.drain
    }
}

/// What [`run`] gives to the `main` of a program.
///
/// Only [`run`] makes a value. Read it through its functions:
///
/// ```
/// use std::process::ExitCode;
///
/// use creche_runtime::service::Context;
///
/// async fn main_of_a_program(mut context: Context) -> ExitCode {
///     let hangups = context.take_hangups();
///     assert!(hangups.is_none(), "this program has no reload");
///
///     context.shutdown().cancelled().await;
///
///     ExitCode::SUCCESS
/// }
/// ```
///
/// Code outside this module cannot build a value or change a field. The
/// stop signal of a context is then always the one that the signal handlers
/// of the process trigger:
///
/// ```compile_fail,E0451
/// use std::process::ExitCode;
///
/// use creche_runtime::service::Context;
///
/// fn forge(context: Context) -> Context {
///     Context {
///         hangups: None,
///         ..context
///     }
/// }
/// ```
#[derive(Debug)]
pub struct Context {
    /// The tasks that must end before the process exits.
    tasks: Tasks,
    /// The stop signal of the process.
    shutdown: Shutdown,
    /// The SIGHUP signals, until the program takes them.
    hangups: Option<Hangups>,
    /// The clock of the host.
    clock: Arc<SystemClock>,
    /// The random source of the operating system.
    entropy: Arc<OsEntropy>,
}

impl Context {
    /// The tasks that must end before the process exits. [`run`] waits for
    /// them after `main` returns.
    #[must_use]
    pub const fn tasks(&self) -> &Tasks {
        &self.tasks
    }

    /// The stop signal of the process. SIGTERM and SIGINT trigger it, and
    /// [`run`] triggers it after `main` returns.
    #[must_use]
    pub const fn shutdown(&self) -> &Shutdown {
        &self.shutdown
    }

    /// Gives the SIGHUP signals to the task that reads them.
    ///
    /// `None` for a program with [`OnHangup::DefaultAction`], and for each
    /// call after the first one.
    #[must_use]
    pub const fn take_hangups(&mut self) -> Option<Hangups> {
        self.hangups.take()
    }

    /// The clock of the host.
    #[must_use]
    pub const fn clock(&self) -> &Arc<SystemClock> {
        &self.clock
    }

    /// The random source of the operating system.
    #[must_use]
    pub const fn entropy(&self) -> &Arc<OsEntropy> {
        &self.entropy
    }
}

/// The result of work that a panic ended. The panic hook wrote the place of
/// the panic.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
struct Panicked;

/// Runs `work` and catches its panic. The function drops the payload of the
/// panic, so no code reads the message.
fn caught<T>(work: impl FnOnce() -> T) -> Result<T, Panicked> {
    panic::catch_unwind(AssertUnwindSafe(work)).map_err(|payload| {
        // The drop of a payload can panic too. That panic must not leave
        // the boundary.
        let _ = panic::catch_unwind(AssertUnwindSafe(move || drop(payload)));

        Panicked
    })
}

/// The future of the `main` of a program, with a guard against its panic.
struct Guarded<Fut> {
    /// `None` after the end of the work.
    work: Option<Pin<Box<Fut>>>,
}

impl<Fut: Future> Future for Guarded<Fut> {
    type Output = Result<Fut::Output, Panicked>;

    fn poll(mut self: Pin<&mut Self>, context: &mut task::Context<'_>) -> Poll<Self::Output> {
        let Some(work) = self.work.as_mut() else {
            // The work ended at an earlier poll, and that poll gave its
            // result.
            return Poll::Ready(Err(Panicked));
        };
        let polled = caught(|| work.as_mut().poll(context));

        if matches!(polled, Ok(Poll::Pending)) {
            return Poll::Pending;
        }

        // The drop of the work can panic too, after a value and after a
        // panic. The program has one result for the two.
        let released = caught(|| drop(self.work.take()));

        match (polled, released) {
            (Ok(Poll::Ready(value)), Ok(())) => Poll::Ready(Ok(value)),
            _ => Poll::Ready(Err(Panicked)),
        }
    }
}

/// Calls `main` and runs its future to the end, behind a guard against a
/// panic. The call of `main` makes the future, so a panic in that call is a
/// panic of `main`.
async fn to_its_end<F, Fut>(main: F, context: Context) -> Result<ExitCode, Panicked>
where
    F: FnOnce(Context) -> Fut,
    Fut: Future<Output = ExitCode>,
{
    let work = caught(|| Box::pin(main(context)))?;

    Guarded { work: Some(work) }.await
}

/// Writes the one line of a program that a panic ended, and gives its exit
/// status.
fn ended_by_panic(name: &str) -> ExitCode {
    crate::error!(LOG_TARGET, "the program {name} stopped with a panic");

    ExitCode::from(PANIC_STATUS)
}

/// Runs `body` behind the last boundary for a panic.
fn behind_boundary(name: &str, body: impl FnOnce() -> ExitCode) -> ExitCode {
    match caught(body) {
        Ok(status) => status,
        Err(Panicked) => ended_by_panic(name),
    }
}

/// Runs the body of the entry function of a program behind the last boundary
/// for a panic.
///
/// The entry function of a library is one call of this function. Its `body`
/// reads the arguments, loads the config and calls [`run`]. A panic of
/// `body` ends the program with one `ERROR` line and exit status 1. The line
/// never holds the message of the panic. Without this call, a panic before
/// [`run`] ends the process with the status of the Rust runtime, which is
/// 101.
///
/// A service crate does not call `catch_unwind` itself (`rust/AGENTS.md`,
/// "The panic rule", clause 8). This function and [`run`] hold that call
/// for the code of an entry function.
///
/// The function first sets the panic hook of the process, as [`log::init`]
/// does, with `program`.
///
/// The Python origin is the interpreter: it ends a program with status 1 for
/// an exception that no code handles, and writes the trace. This function
/// writes no message of the panic.
pub fn enter<F>(program: &'static str, body: F) -> ExitCode
where
    F: FnOnce() -> ExitCode,
{
    log::init(program);

    behind_boundary(program, body)
}

/// The two steps of [`run`] that ask the operating system for a resource. A
/// test gives steps that fail.
trait Steps {
    /// Builds the runtime of the process.
    fn runtime(&self, threads: Threads) -> io::Result<Runtime>;

    /// Installs the signal handlers. The call runs inside the runtime.
    fn signals(
        &self,
        trigger: ShutdownTrigger,
        on_hangup: OnHangup,
    ) -> Result<Option<Hangups>, SignalError>;
}

/// The steps on the operating system of the host.
struct Host;

impl Steps for Host {
    fn runtime(&self, threads: Threads) -> io::Result<Runtime> {
        let mut builder = match threads {
            Threads::One => Builder::new_current_thread(),
            Threads::Workers(count) => {
                let mut builder = Builder::new_multi_thread();
                builder.worker_threads(count.get());

                builder
            }
        };

        builder.enable_all().build()
    }

    fn signals(
        &self,
        trigger: ShutdownTrigger,
        on_hangup: OnHangup,
    ) -> Result<Option<Hangups>, SignalError> {
        signals::install(trigger, on_hangup)
    }
}

/// Runs `main` inside the runtime of the process and returns its exit status.
///
/// The steps of the function:
///
/// 1. It sets the panic hook of the process, as [`log::init`] does, with the
///    name of the program. A panic of a task then writes its place and never
///    its message, also when the `main` of the program did not call
///    `log::init`.
/// 2. It builds the runtime. [`Threads::One`] gives a runtime on the thread
///    of the caller.
/// 3. It makes the stop signal and the [`Tasks`], and installs the signal
///    handlers.
/// 4. It calls `main` with the [`Context`] and runs it to its end. The
///    function does not stop a `main` that continues after the stop signal.
/// 5. It triggers the stop signal and waits for the tracked tasks, for the
///    drain limit of the program at most. Tasks that still run at the limit
///    are one `ERROR` line with their count.
/// 6. It gives the runtime the rest of the drain limit for its stop. Then
///    the function returns, also when a blocking call still runs. The
///    process ends without that call when the program returns from its
///    `main`.
///
/// The drain limit starts when `main` returns, also when a stop signal came
/// before that. The time that `main` uses after the signal is thus no part
/// of the limit ([`Program`]).
///
/// The exit status is the status that `main` returns, with two exceptions:
///
/// - The operating system gives no runtime or no signal handler. `main` does
///   not run. The function writes one `ERROR` line and returns status 71,
///   `EX_OSERR` of `sysexits.h`.
/// - `main` panics. The function writes one `ERROR` line and returns status
///   1, after steps 5 and 6. The status is never `EX_CONFIG`.
///
/// Return the result from the entry function of the program. The lint gate
/// refuses `std::process::exit`.
///
/// The Python origins are `asyncio.run` of
/// `attendance/src/attendance/__main__.py:64` and `uvicorn.run` of each
/// other service, for example
/// `noticeboard/src/noticeboard/__main__.py:61`. The function differs from
/// them in these ways:
///
/// - `uvicorn` waits with no limit for an open request at a stop
///   (`uvicorn/server.py:288-291`). The function waits for the drain limit.
/// - A Python service continues with no handler on a system that gives it
///   none (`attendance/src/attendance/__main__.py:205`). The function does
///   not run `main` then.
/// - A service that `uvicorn.run` starts ends by the stop signal itself
///   (`uvicorn/server.py:339-340`). The function returns the status of
///   `main`.
pub fn run<F, Fut>(program: Program, main: F) -> ExitCode
where
    F: FnOnce(Context) -> Fut,
    Fut: Future<Output = ExitCode>,
{
    log::init(program.name);

    run_through(&Host, program, main)
}

/// [`run`] on the given [`Steps`]. The function sets no panic hook.
fn run_through<S, F, Fut>(steps: &S, program: Program, main: F) -> ExitCode
where
    S: Steps,
    F: FnOnce(Context) -> Fut,
    Fut: Future<Output = ExitCode>,
{
    behind_boundary(program.name, || start(steps, program, main))
}

/// The steps 2 to 6 of [`run`].
fn start<S, F, Fut>(steps: &S, program: Program, main: F) -> ExitCode
where
    S: Steps,
    F: FnOnce(Context) -> Fut,
    Fut: Future<Output = ExitCode>,
{
    let name = program.name;
    let runtime = match steps.runtime(program.threads) {
        Ok(runtime) => runtime,
        Err(error) => {
            crate::error!(
                LOG_TARGET,
                "the runtime of the program {name} did not start: {}",
                os_text(&error)
            );

            return ExitCode::from(EX_OSERR);
        }
    };
    let (trigger, shutdown) = shutdown_pair();
    let tasks = Tasks::new(shutdown.clone());

    let (status, stopped_at) = runtime.block_on(async {
        let status = match steps.signals(trigger.clone(), program.on_hangup) {
            Ok(hangups) => {
                let context = Context {
                    tasks: tasks.clone(),
                    shutdown,
                    hangups,
                    clock: Arc::new(SystemClock::new()),
                    entropy: Arc::new(OsEntropy::new()),
                };

                match to_its_end(main, context).await {
                    Ok(status) => status,
                    Err(Panicked) => ended_by_panic(name),
                }
            }
            Err(error) => {
                crate::error!(
                    LOG_TARGET,
                    "the signal handlers of the program {name} are not in place: {error}"
                );

                ExitCode::from(EX_OSERR)
            }
        };

        // The drain limit starts here, at the return of `main`. The time
        // that `main` used after a stop signal is no part of it.
        let stopped_at = Instant::now();
        trigger.trigger();

        if let Drained::TimedOut { left } = tasks.drain(rest_of(program.drain, stopped_at)).await {
            crate::error!(
                LOG_TARGET,
                "the drain limit of the program {name} passed, and tasks still run: {left}"
            );
        }

        (status, stopped_at)
    });

    // A runtime that drops by itself waits with no limit for each blocking
    // call. This call waits for the rest of the drain limit at most.
    runtime.shutdown_timeout(rest_of(program.drain, stopped_at));

    status
}

/// The part of the drain limit that is left, for a `main` that returned at
/// `stopped_at`.
fn rest_of(drain: Duration, stopped_at: Instant) -> Duration {
    drain.saturating_sub(stopped_at.elapsed())
}

/// The errors of one parse, as one line of text for each error.
///
/// [`load`] writes each line to stderr. A line names the variable or the key
/// and the reason. It never holds a value: a value can be a secret.
pub trait ErrorLines {
    /// One line for each error, in the order of the parse.
    fn lines(&self) -> Vec<String>;
}

/// The text of each error of a list, in order.
fn text_of_each<T: fmt::Display>(errors: &[T]) -> Vec<String> {
    errors.iter().map(ToString::to_string).collect()
}

impl ErrorLines for ConfigErrors {
    fn lines(&self) -> Vec<String> {
        text_of_each(self.as_slice())
    }
}

impl ErrorLines for SiteErrors {
    fn lines(&self) -> Vec<String> {
        text_of_each(self.as_slice())
    }
}

impl ErrorLines for RosterErrors {
    fn lines(&self) -> Vec<String> {
        text_of_each(self.as_slice())
    }
}

/// What a program does after the parse of its config at start.
#[derive(Debug)]
pub enum Loaded<C, E> {
    /// The config is valid. The program runs with it.
    Run(C),
    /// The config is not valid, and its type says that the program exits.
    /// [`load`] wrote each error to stderr. Return this status from the
    /// entry function. It is `EX_CONFIG`.
    Exit(ExitCode),
    /// The config is not valid, and its type says that the program starts
    /// and refuses each call. The program publishes these errors as a fault.
    RefuseEachCall(E),
}

/// Applies the failure action of the config type `C` to the result of its
/// parse.
///
/// For a type that says [`AtStart::ExitConfig`](config::AtStart::ExitConfig),
/// the function writes one line to stderr for each error:
/// `<program>: <error>`. A line names the variable and never its value. The
/// exit status is then `EX_CONFIG`, and the unit of the daemon must hold
/// `RestartPreventExitStatus=78`.
///
/// The function first sets the panic hook of the process, as [`log::init`]
/// does, with `program`.
///
/// The Python origin is the `except ConfigError` block of each `main`, for
/// example `noticeboard/src/noticeboard/__main__.py:37-44`. That block writes
/// one line for the whole config and returns status 2.
pub fn load<C: Checked, E: ErrorLines>(
    program: &'static str,
    parsed: Result<C, E>,
) -> Loaded<C, E> {
    log::init(program);

    load_to(&mut io::stderr().lock(), program, parsed)
}

/// [`load`] on the given writer. The function sets no panic hook.
fn load_to<C: Checked, E: ErrorLines>(
    writer: &mut dyn io::Write,
    program: &str,
    parsed: Result<C, E>,
) -> Loaded<C, E> {
    match config::start(parsed) {
        Start::Run(config) => Loaded::Run(config),
        Start::Exit { errors } => {
            for line in errors.lines() {
                write_refusal(writer, program, &line);
            }

            Loaded::Exit(ExitCode::from(EX_CONFIG))
        }
        Start::RefuseEachCall { errors } => Loaded::RefuseEachCall(errors),
    }
}

/// Writes one line: `<program>: <reason>`. A control character of the reason
/// is an escape there, as in a line of the log, so one reason is one line.
fn write_refusal(writer: &mut dyn io::Write, program: &str, reason: &dyn fmt::Display) {
    let mut line = String::new();
    log::escape_into(&mut line, &format!("{program}: {reason}"));

    log::write_line(writer, &line);
}

/// Writes the reason of a refused start to stderr and gives the exit status
/// `EX_CONFIG`.
///
/// A daemon calls it for each cause of a refused start that is outside its
/// config type: a wrong word of the command line, a token file or a key file
/// that it cannot use, and a URL that its client cannot use. No restart
/// repairs such a cause. With `EX_CONFIG`, a unit that holds
/// `RestartPreventExitStatus=78` stays stopped (`rust/AGENTS.md`, "The rules
/// for a service", rule 17).
///
/// The line is `<program>: <reason>`. `reason` is an error of this crate or
/// of `creche-contracts`, and no such error holds a secret.
///
/// The function first sets the panic hook of the process, as [`log::init`]
/// does, with `program`.
///
/// The Python origin is the `except TokenError` block of
/// `attendance/src/attendance/__main__.py:56-58`, which returns status 2.
/// `caregiver` returns status 1 for its token file
/// (`caregiver/src/caregiver/cli.py:496-498`).
pub fn refuse_start(program: &'static str, reason: &dyn fmt::Display) -> ExitCode {
    log::init(program);

    refuse_to(&mut io::stderr().lock(), program, reason)
}

/// [`refuse_start`] on the given writer. The function sets no panic hook.
fn refuse_to(writer: &mut dyn io::Write, program: &str, reason: &dyn fmt::Display) -> ExitCode {
    write_refusal(writer, program, reason);

    ExitCode::from(EX_CONFIG)
}

/// Writes one `INFO` line for each listener: `listening on <address>`. The
/// target of the line is `service`.
///
/// The Python origins are the line of
/// `noticeboard/src/noticeboard/__main__.py:60` and the start message of
/// `uvicorn`. No program reads the line.
pub fn ready(bound: &[Bound]) {
    for listener in bound {
        crate::info!(LOG_TARGET, "listening on {}", listener.describe());
    }
}

#[cfg(test)]
pub(crate) mod tests {
    use std::sync::atomic::{AtomicBool, Ordering};
    use std::sync::mpsc;
    use std::task::Waker;

    use creche_contracts::config::noticeboard::NoticeboardConfig;
    use creche_contracts::config::roster::{RawRoster, Roster};
    use creche_contracts::config::site::{Site, SiteFile};
    use creche_contracts::config::{AtReload, AtStart, Env, FailureAction, HttpUrl};
    use creche_testkit::root::TempRoot;
    use rustix::io::Errno;
    use rustix::process::{Signal, getpid, kill_process};
    use tokio::runtime::{Handle, RuntimeFlavor};

    use super::*;
    use crate::args::UsageError;
    use crate::clock::Clock;
    use crate::entropy::Entropy;
    use crate::http::client::Target;
    use crate::http::server::tests::{
        CHILD_PROGRAM, LIMIT, LONG_DRAIN, SHORT_DRAIN, run_child, within,
    };
    use crate::http::server::{Listen, SocketDir, bind};
    use crate::token::{self, TokenRule};

    /// The variable that makes [`the_child_panics_in_one_entry`] act. Its
    /// value names the entry.
    const ENTRY_VARIABLE: &str = "CRECHE_RUNTIME_SERVICE_TEST_ENTRY";

    /// The name of that child test, as the test program takes it.
    const ENTRY_TEST: &str = "service::tests::the_child_panics_in_one_entry";

    /// The variable that selects the scenario of
    /// [`the_child_runs_one_scenario`].
    const SCENARIO_VARIABLE: &str = "CRECHE_RUNTIME_SERVICE_TEST_SCENARIO";

    /// The name of that child test, as the test program takes it.
    const SCENARIO_TEST: &str = "service::tests::the_child_runs_one_scenario";

    /// The message of each panic of a test. No line of a child can hold it.
    const PANIC_MESSAGE: &str = "zebra-crossing-secret-4711";

    /// Each function that a `main` can call first with the name of its
    /// program, as a value of [`ENTRY_VARIABLE`].
    const ENTRIES: [&str; 4] = ["enter", "run", "load", "refuse_start"];

    /// The status that the `main` of a test returns. No constant of the
    /// module has this value.
    const STATUS_OF_MAIN: u8 = 7;

    /// How long a task of a test works after its `main` returned.
    const TASK_TIME: Duration = Duration::from_millis(100);

    /// Whether `status` is the exit status `expected`. An `ExitCode` has no
    /// compare, so the function compares the debug text of the two.
    pub(crate) fn same_status(status: ExitCode, expected: u8) -> bool {
        format!("{status:?}") == format!("{:?}", ExitCode::from(expected))
    }

    fn two_workers() -> Threads {
        Threads::Workers(NonZeroUsize::new(2).unwrap())
    }

    /// A program of a test, on one thread and on two worker threads. A
    /// service selects its own count of threads.
    fn each_program(drain: Duration) -> [Program; 2] {
        [Threads::One, two_workers()].map(|threads| Program::new(CHILD_PROGRAM, threads, drain))
    }

    /// The runtime of the host, and no signal handler. A test in the test
    /// process must not take a signal of the test program.
    struct NoHandlers;

    impl Steps for NoHandlers {
        fn runtime(&self, threads: Threads) -> io::Result<Runtime> {
            Host.runtime(threads)
        }

        fn signals(
            &self,
            _trigger: ShutdownTrigger,
            _on_hangup: OnHangup,
        ) -> Result<Option<Hangups>, SignalError> {
            Ok(None)
        }
    }

    /// An operating system that gives no runtime.
    struct NoRuntime;

    impl Steps for NoRuntime {
        fn runtime(&self, _threads: Threads) -> io::Result<Runtime> {
            Err(io::Error::from_raw_os_error(Errno::MFILE.raw_os_error()))
        }

        fn signals(
            &self,
            _trigger: ShutdownTrigger,
            _on_hangup: OnHangup,
        ) -> Result<Option<Hangups>, SignalError> {
            Ok(None)
        }
    }

    /// An operating system that gives no signal handler.
    struct NoSignals(SignalError);

    impl NoSignals {
        /// The steps with the error that `install` gives on a thread with no
        /// runtime. Call it outside a runtime.
        fn new() -> Self {
            let (trigger, _shutdown) = shutdown_pair();

            Self(signals::install(trigger, OnHangup::Reload).unwrap_err())
        }
    }

    impl Steps for NoSignals {
        fn runtime(&self, threads: Threads) -> io::Result<Runtime> {
            Host.runtime(threads)
        }

        fn signals(
            &self,
            _trigger: ShutdownTrigger,
            _on_hangup: OnHangup,
        ) -> Result<Option<Hangups>, SignalError> {
            Err(self.0.clone())
        }
    }

    /// A config whose type says that the program exits.
    #[derive(Debug, PartialEq)]
    struct Exits;

    impl Checked for Exits {
        const FAILURE: FailureAction = FailureAction::new(AtStart::ExitConfig, AtReload::NotRead);
    }

    /// A config whose type says that the program refuses each call.
    #[derive(Debug, PartialEq)]
    struct Refuses;

    impl Checked for Refuses {
        const FAILURE: FailureAction =
            FailureAction::new(AtStart::RefuseEachCall, AtReload::KeepLastGood);
    }

    /// The errors of a parse that failed for two variables.
    #[derive(Debug, PartialEq)]
    struct TwoErrors;

    impl ErrorLines for TwoErrors {
        fn lines(&self) -> Vec<String> {
            vec![
                String::from("PORT: the variable is not set"),
                String::from("BIND: the value is not UTF-8"),
            ]
        }
    }

    /// A writer that refuses each write, as a closed stream does.
    pub(crate) struct Closed;

    impl io::Write for Closed {
        fn write(&mut self, _bytes: &[u8]) -> io::Result<usize> {
            Err(io::Error::from(io::ErrorKind::BrokenPipe))
        }

        fn flush(&mut self) -> io::Result<()> {
            Err(io::Error::from(io::ErrorKind::BrokenPipe))
        }
    }

    /// The lines that a call wrote, with no newline at the end of a line.
    pub(crate) fn lines_of(written: &[u8]) -> Vec<&str> {
        std::str::from_utf8(written).unwrap().lines().collect()
    }

    // --- the two exit statuses of the module ---

    #[test]
    fn a_panic_is_status_1_and_a_system_with_no_runtime_is_status_71() {
        // The numbers are in this test, and not only the names: a change of
        // a constant must fail a test. Clause 7 of the panic rule states the
        // 1, and `sysexits.h` states the 71.
        assert_eq!(PANIC_STATUS, 1);
        assert_eq!(EX_OSERR, 71);
        assert_ne!(PANIC_STATUS, EX_CONFIG);
        assert_ne!(EX_OSERR, EX_CONFIG);
    }

    // --- the program and the context ---

    #[test]
    fn a_program_has_no_sighup_handler_until_it_names_one() {
        let drain = Duration::from_secs(18);
        let program = Program::new("noticeboard", two_workers(), drain);

        assert_eq!(program.name(), "noticeboard");
        assert_eq!(program.threads(), two_workers());
        assert_eq!(program.on_hangup(), OnHangup::DefaultAction);
        assert_eq!(program.drain(), drain);

        let with_reload = program.with_on_hangup(OnHangup::Reload);

        assert_eq!(with_reload.on_hangup(), OnHangup::Reload);
        assert_eq!(with_reload.name(), program.name());
        assert_eq!(with_reload.threads(), program.threads());
        assert_eq!(with_reload.drain(), program.drain());
        assert_ne!(with_reload, program);
    }

    #[test]
    fn the_runtime_has_the_threads_of_the_program() {
        let one = Host.runtime(Threads::One).unwrap();
        let flavor = one.block_on(async { Handle::current().runtime_flavor() });
        assert_eq!(flavor, RuntimeFlavor::CurrentThread);
        one.shutdown_timeout(LIMIT);

        let three = Threads::Workers(NonZeroUsize::new(3).unwrap());
        let workers = Host.runtime(three).unwrap();
        let (flavor, count) = workers.block_on(async {
            let handle = Handle::current();

            (handle.runtime_flavor(), handle.metrics().num_workers())
        });
        assert_eq!(flavor, RuntimeFlavor::MultiThread);
        assert_eq!(count, 3);
        workers.shutdown_timeout(LIMIT);
    }

    #[test]
    fn run_returns_the_status_of_main() {
        for program in each_program(LONG_DRAIN) {
            let status = run_through(&NoHandlers, program, |_context| async {
                ExitCode::from(STATUS_OF_MAIN)
            });

            assert!(same_status(status, STATUS_OF_MAIN), "{program:?}");
            assert!(!same_status(status, 0), "{program:?}");
        }
    }

    #[test]
    fn run_waits_for_a_task_that_must_complete() {
        for program in each_program(LONG_DRAIN) {
            let written = Arc::new(AtomicBool::new(false));
            let synced = Arc::new(AtomicBool::new(false));
            let (in_task, in_call) = (Arc::clone(&written), Arc::clone(&synced));

            let status = run_through(&NoHandlers, program, |context| async move {
                // `main` returns before the two end, and it drops the two
                // results.
                drop(
                    context
                        .tasks()
                        .spawn_must_complete("slow-write", async move {
                            tokio::time::sleep(TASK_TIME).await;
                            in_task.store(true, Ordering::SeqCst);
                        }),
                );
                drop(context.tasks().spawn_blocking("slow-sync", move || {
                    std::thread::sleep(TASK_TIME);
                    in_call.store(true, Ordering::SeqCst);
                }));

                ExitCode::SUCCESS
            });

            assert!(same_status(status, 0), "{program:?}");
            assert!(written.load(Ordering::SeqCst), "{program:?}");
            assert!(synced.load(Ordering::SeqCst), "{program:?}");
        }
    }

    #[test]
    fn main_gets_the_parts_of_the_process_and_run_triggers_the_stop_after_it() {
        for program in each_program(LONG_DRAIN) {
            let (to_test, from_main) = mpsc::channel();

            let status = run_through(&NoHandlers, program, |mut context| async move {
                let mut bytes = [0_u8; 16];
                let filled = context.entropy().fill(&mut bytes).is_ok();
                let after_1970 = context.clock().now() > std::time::UNIX_EPOCH;
                let no_hangups = context.take_hangups().is_none();
                let running = !context.shutdown().is_cancelled();
                let signals = (
                    context.shutdown().clone(),
                    context.tasks().shutdown().clone(),
                );

                to_test
                    .send((filled, after_1970, no_hangups, running, signals))
                    .unwrap();

                ExitCode::SUCCESS
            });
            let (filled, after_1970, no_hangups, running, signals) = from_main.recv().unwrap();

            assert!(same_status(status, 0), "{program:?}");
            assert!(filled, "{program:?}");
            assert!(after_1970, "{program:?}");
            assert!(no_hangups, "{program:?}");
            assert!(running, "{program:?}");
            // The stop signal of the context and of the tasks is one signal,
            // and `run` triggered it after `main`.
            assert!(signals.0.is_cancelled(), "{program:?}");
            assert!(signals.1.is_cancelled(), "{program:?}");
        }
    }

    // --- the stop ---

    #[test]
    fn run_triggers_the_stop_before_it_waits_for_the_tasks() {
        for program in each_program(LONG_DRAIN) {
            let ended = Arc::new(AtomicBool::new(false));
            let in_task = Arc::clone(&ended);
            let started = Instant::now();

            let status = run_through(&NoHandlers, program, |context| async move {
                let stop = context.shutdown().clone();
                // The task ends only at the stop signal, as a loop of a
                // service does. No signal comes here: `main` returns.
                drop(
                    context
                        .tasks()
                        .spawn_must_complete("until-the-stop", async move {
                            stop.cancelled().await;
                            in_task.store(true, Ordering::SeqCst);
                        }),
                );

                ExitCode::from(STATUS_OF_MAIN)
            });
            let waited = started.elapsed();

            assert!(same_status(status, STATUS_OF_MAIN), "{program:?}");
            assert!(ended.load(Ordering::SeqCst), "{program:?}");
            // A wait before the trigger takes the whole drain limit.
            assert!(waited < LONG_DRAIN / 2, "{program:?}: {waited:?}");
        }
    }

    #[test]
    fn the_rest_of_a_drain_limit_is_never_less_than_no_time() {
        let stopped_at = Instant::now();

        assert!(rest_of(LONG_DRAIN, stopped_at) <= LONG_DRAIN);
        assert!(rest_of(LONG_DRAIN, stopped_at) > LONG_DRAIN / 2);
        std::thread::sleep(Duration::from_millis(2));
        assert_eq!(
            rest_of(Duration::from_millis(1), stopped_at),
            Duration::ZERO
        );
        assert_eq!(rest_of(Duration::ZERO, stopped_at), Duration::ZERO);
    }

    // --- the boundary ---

    /// A payload of a panic whose drop panics.
    struct PanicsAtDrop;

    impl Drop for PanicsAtDrop {
        fn drop(&mut self) {
            panic!("{PANIC_MESSAGE}");
        }
    }

    /// A future that is ready at its second poll, with a value or with a
    /// panic.
    struct SecondPoll<T> {
        polled: bool,
        end: Option<T>,
    }

    impl<T: Unpin> Future for SecondPoll<T> {
        type Output = T;

        fn poll(mut self: Pin<&mut Self>, context: &mut task::Context<'_>) -> Poll<T> {
            if !self.polled {
                self.polled = true;
                context.waker().wake_by_ref();

                return Poll::Pending;
            }

            match self.end.take() {
                Some(value) => Poll::Ready(value),
                None => panic!("{PANIC_MESSAGE}"),
            }
        }
    }

    fn poll_once<F: Future + Unpin>(future: &mut F) -> Poll<F::Output> {
        Pin::new(future).poll(&mut task::Context::from_waker(Waker::noop()))
    }

    #[test]
    fn a_boundary_gives_the_value_of_its_work() {
        assert_eq!(caught(|| 7), Ok(7));
        assert!(same_status(
            behind_boundary(CHILD_PROGRAM, || ExitCode::from(STATUS_OF_MAIN)),
            STATUS_OF_MAIN
        ));
    }

    #[test]
    fn a_boundary_takes_a_panic_and_a_payload_whose_drop_panics() {
        let plain: Result<(), Panicked> = caught(|| panic!("{PANIC_MESSAGE}"));
        let at_drop: Result<(), Panicked> = caught(|| panic::panic_any(PanicsAtDrop));

        assert_eq!(plain, Err(Panicked));
        assert_eq!(at_drop, Err(Panicked));
    }

    #[test]
    fn a_guarded_future_gives_its_value_after_a_wait() {
        let mut guarded = Guarded {
            work: Some(Box::pin(SecondPoll {
                polled: false,
                end: Some(7),
            })),
        };

        assert_eq!(poll_once(&mut guarded), Poll::Pending);
        assert_eq!(poll_once(&mut guarded), Poll::Ready(Ok(7)));
        // A poll after the end gives no second value and does not panic.
        assert_eq!(poll_once(&mut guarded), Poll::Ready(Err(Panicked)));
    }

    #[test]
    fn a_guarded_future_takes_a_panic_of_a_poll() {
        let mut guarded = Guarded {
            work: Some(Box::pin(SecondPoll::<u8> {
                polled: false,
                end: None,
            })),
        };

        assert_eq!(poll_once(&mut guarded), Poll::Pending);
        assert_eq!(poll_once(&mut guarded), Poll::Ready(Err(Panicked)));
        assert!(guarded.work.is_none());
    }

    #[test]
    fn a_guarded_future_takes_a_panic_of_the_drop_of_its_work() {
        /// A future that is ready at once, and whose drop panics.
        struct ReadyThenPanics {
            _at_drop: PanicsAtDrop,
        }

        impl Future for ReadyThenPanics {
            type Output = u8;

            fn poll(self: Pin<&mut Self>, _context: &mut task::Context<'_>) -> Poll<u8> {
                Poll::Ready(7)
            }
        }

        let mut guarded = Guarded {
            work: Some(Box::pin(ReadyThenPanics {
                _at_drop: PanicsAtDrop,
            })),
        };

        assert_eq!(poll_once(&mut guarded), Poll::Ready(Err(Panicked)));
        assert!(guarded.work.is_none());
    }

    // --- the load of a config ---

    #[test]
    fn a_valid_config_runs_and_writes_no_line() {
        let mut written = Vec::new();

        let loaded = load_to::<Exits, TwoErrors>(&mut written, CHILD_PROGRAM, Ok(Exits));

        assert!(matches!(loaded, Loaded::Run(Exits)));
        assert!(written.is_empty());
    }

    #[test]
    fn a_config_that_exits_writes_one_line_for_each_error_and_gives_ex_config() {
        let mut written = Vec::new();

        let loaded = load_to::<Exits, TwoErrors>(&mut written, "noticeboard", Err(TwoErrors));

        let Loaded::Exit(status) = loaded else {
            panic!("the type of the config says that the program exits");
        };
        assert!(same_status(status, EX_CONFIG));
        assert!(!same_status(status, 0));
        assert_eq!(
            lines_of(&written),
            [
                "noticeboard: PORT: the variable is not set",
                "noticeboard: BIND: the value is not UTF-8"
            ]
        );
        assert!(written.ends_with(b"\n"));
    }

    #[test]
    fn a_config_that_refuses_each_call_gives_its_errors_and_writes_no_line() {
        let mut written = Vec::new();

        let loaded = load_to::<Refuses, TwoErrors>(&mut written, CHILD_PROGRAM, Err(TwoErrors));

        assert!(matches!(loaded, Loaded::RefuseEachCall(TwoErrors)));
        assert!(written.is_empty());
    }

    #[test]
    fn the_lines_of_a_real_config_name_each_variable_and_no_value() {
        // Each value has a mark that no line can hold.
        let env = Env::from_pairs([
            ("VIEW_BIND", "127.0.0.1"),
            ("VIEW_PORT", "port-zebra-4711"),
            ("VIEW_STATE_ROOT", "relative/root-zebra-4712"),
            ("VIEW_SESSIOND_URL", "ftp://url-zebra-4713"),
        ]);
        let mut written = Vec::new();

        let loaded = load_to(
            &mut written,
            "noticeboard",
            NoticeboardConfig::from_env(&env),
        );

        let Loaded::Exit(status) = loaded else {
            panic!("the config is not valid, and its type says that the program exits");
        };
        assert!(same_status(status, EX_CONFIG));

        let text = String::from_utf8(written).unwrap();
        // The config type gives the order of the errors. Each line starts
        // with the program and names one variable.
        let mut named: Vec<&str> = text
            .lines()
            .map(|line| {
                let error = line.strip_prefix("noticeboard: ").unwrap();

                error.split_once(": ").unwrap().0
            })
            .collect();
        named.sort_unstable();
        assert_eq!(
            named,
            ["VIEW_PORT", "VIEW_SESSIOND_URL", "VIEW_STATE_ROOT"],
            "{text}"
        );
        assert!(!text.contains("zebra"), "{text}");
        assert!(!text.contains("; "), "{text}");
    }

    #[test]
    fn each_error_list_of_a_config_gives_one_line_for_each_error() {
        let env = Env::from_pairs([("VIEW_BIND", "127.0.0.1"), ("VIEW_PORT", "0")]);
        let config = NoticeboardConfig::from_env(&env).unwrap_err();
        assert_eq!(config.as_slice().len(), 1);
        assert_eq!(config.lines(), [config.as_slice()[0].to_string()]);
        assert!(config.lines()[0].starts_with("VIEW_PORT: "));

        let site = Site::try_from(&SiteFile::parse(b"").unwrap()).unwrap_err();
        assert_eq!(site.as_slice().len(), 4);
        assert_eq!(site.lines().len(), 4);
        for (line, error) in site.lines().iter().zip(site.as_slice()) {
            assert_eq!(line, &error.to_string());
            assert!(line.starts_with(&format!("{}: ", error.key())), "{line}");
        }
        // The text of the whole list is one line. The lines are not.
        assert!(site.to_string().contains("; "));
        assert!(site.lines().iter().all(|line| !line.contains("; ")));

        let raw: RawRoster = serde_json::from_str(
            r#"{"web-search": {"command": "web-search", "arg_denies": [
                {"tools": [], "arg": "site", "values": ["a"]},
                {"tools": ["search"], "arg": "", "values": ["a"]}]}}"#,
        )
        .unwrap();
        let roster = Roster::try_from(raw).unwrap_err();
        assert_eq!(roster.as_slice().len(), 2);
        assert_eq!(roster.lines().len(), 2);
        for (line, issue) in roster.lines().iter().zip(roster.as_slice()) {
            assert_eq!(line, &issue.to_string());
            assert!(line.contains(issue.upstream()), "{line}");
        }
        assert_ne!(roster.lines()[0], roster.lines()[1]);
    }

    // --- a refused start ---

    #[test]
    fn each_refused_start_writes_one_line_and_gives_ex_config() {
        let root = TempRoot::new().unwrap();
        let token = token::read(&root.path().join("absent.token"), TokenRule::ATTENDANCE);
        let token_error = token.unwrap_err();
        let word_error = UsageError::Unexpected {
            word: String::from("--verbose"),
        };
        let url: HttpUrl = "https://192.0.2.10:8340".parse().unwrap();
        let target_error = Target::try_from(&url).unwrap_err();
        let env = Env::from_pairs([("VIEW_BIND", "127.0.0.1"), ("VIEW_PORT", "0")]);
        let config_error = NoticeboardConfig::from_env(&env).unwrap_err();
        let reasons: [(&dyn fmt::Display, &str); 4] = [
            (&token_error, "the token file is not readable: "),
            (&word_error, "unrecognized arguments: --verbose"),
            (
                &target_error,
                "the URL is an https URL, and the client has no TLS",
            ),
            (&config_error, "VIEW_PORT: "),
        ];

        for (reason, start_of_text) in reasons {
            let mut written = Vec::new();

            let status = refuse_to(&mut written, "attendance", reason);

            assert!(same_status(status, EX_CONFIG), "{start_of_text}");
            assert!(!same_status(status, 0), "{start_of_text}");
            let lines = lines_of(&written);
            assert_eq!(lines.len(), 1, "{lines:?}");
            assert_eq!(lines[0], format!("attendance: {reason}"));
            assert!(
                lines[0].starts_with(&format!("attendance: {start_of_text}")),
                "{lines:?}"
            );
        }
    }

    #[test]
    fn a_reason_with_a_control_character_stays_one_line() {
        let reason = UsageError::Unexpected {
            word: String::from("--one\ntwo\u{1b}[2J\u{2028}three"),
        };
        let mut written = Vec::new();

        let _status = refuse_to(&mut written, CHILD_PROGRAM, &reason);

        assert_eq!(
            lines_of(&written),
            ["child: unrecognized arguments: --one\\ntwo\\u{1b}[2J\\u{2028}three"]
        );
    }

    #[test]
    fn a_closed_stream_does_not_stop_a_refusal() {
        let status = refuse_to(&mut Closed, CHILD_PROGRAM, &"the token file is empty");
        let loaded = load_to::<Exits, TwoErrors>(&mut Closed, CHILD_PROGRAM, Err(TwoErrors));

        assert!(same_status(status, EX_CONFIG));
        assert!(matches!(loaded, Loaded::Exit(_)));
    }

    // --- each entry in a child ---

    async fn main_that_panics(_context: Context) -> ExitCode {
        panic!("{PANIC_MESSAGE}")
    }

    /// Calls one entry and then panics. The code calls no `log::init`.
    fn enter_and_panic(entry: &str) {
        match entry {
            "enter" => {
                let _status = enter(CHILD_PROGRAM, || ExitCode::SUCCESS);
            }
            "run" => {
                let program = Program::new(CHILD_PROGRAM, Threads::One, LONG_DRAIN);
                let _status = run(program, main_that_panics);
            }
            "load" => {
                let _loaded = load::<Exits, TwoErrors>(CHILD_PROGRAM, Err(TwoErrors));
            }
            "refuse_start" => {
                let reason = "the token file is empty";
                let _status = refuse_start(CHILD_PROGRAM, &reason);
            }
            other => panic!("no entry has the name {other}"),
        }

        panic!("{PANIC_MESSAGE}");
    }

    /// The child of the test below. Without the variable it does nothing.
    /// With the variable it calls one entry and panics, in the entry or
    /// after it. The harness takes the panic, so the child test passes.
    #[test]
    fn the_child_panics_in_one_entry() {
        let Some(entry) = std::env::var_os(ENTRY_VARIABLE) else {
            return;
        };
        let entry = entry.into_string().unwrap();
        let caught = panic::catch_unwind(|| enter_and_panic(&entry));

        assert!(caught.is_err());
    }

    #[test]
    fn each_entry_sets_the_panic_hook() {
        // The hook is one for the whole process, so each entry runs in a
        // child: this test program again, with only the child test.
        for entry in ENTRIES {
            let child = run_child(ENTRY_TEST, ENTRY_VARIABLE, entry);
            let stderr = String::from_utf8(child.stderr).unwrap();
            let stdout = String::from_utf8(child.stdout).unwrap();
            let hook_line = format!(" ERROR {CHILD_PROGRAM} panic at ");

            assert!(child.status.success(), "{entry}: {stdout}\n{stderr}");
            assert!(stdout.contains("1 passed"), "{entry}: {stdout}");
            assert!(
                stderr.lines().any(|line| line.contains(&hook_line)),
                "{entry}: {stderr}"
            );
            assert!(!stderr.contains(PANIC_MESSAGE), "{entry}: {stderr}");
            assert!(!stdout.contains(PANIC_MESSAGE), "{entry}: {stdout}");
        }
    }

    // --- the scenarios that write a line or take a signal ---

    /// A scenario that runs in a child: this test program again, with one
    /// test and one thread.
    ///
    /// A scenario that writes to the log runs there, so its lines do not go
    /// to the output of this test program, and the test can read them. A
    /// scenario that installs a signal handler runs there too: a handler
    /// stays for the life of its process.
    struct Scenario {
        /// The value of [`SCENARIO_VARIABLE`] that selects the scenario.
        name: &'static str,
        /// What the child does.
        run: fn(),
        /// How many panics the child catches. The panic hook writes one
        /// line for each one.
        panics: usize,
        /// Checks the lines of this module: the level and the message of
        /// each one.
        check: fn(&[String]),
    }

    /// The line of a program that a panic ended.
    const PANIC_LINE: &str = "ERROR the program child stopped with a panic";

    const SCENARIOS: [Scenario; 11] = [
        Scenario {
            name: "main-panics",
            run: the_main_of_a_program_panics,
            panics: 2,
            // One line for each of the two runtimes.
            check: |lines| assert_eq!(lines, [PANIC_LINE, PANIC_LINE]),
        },
        Scenario {
            name: "main-call-panics",
            run: the_call_of_main_panics,
            panics: 2,
            // One line for each of the two runtimes.
            check: |lines| assert_eq!(lines, [PANIC_LINE, PANIC_LINE]),
        },
        Scenario {
            name: "enter-panics",
            run: the_body_of_an_entry_function_panics,
            panics: 1,
            check: |lines| assert_eq!(lines, [PANIC_LINE]),
        },
        Scenario {
            name: "no-runtime",
            run: the_system_gives_no_runtime,
            panics: 0,
            check: |lines| {
                assert_eq!(
                    lines,
                    [
                        "ERROR the runtime of the program child did not start: Too many open \
                         files"
                    ]
                );
            },
        },
        Scenario {
            name: "no-signals",
            run: the_system_gives_no_signal_handler,
            panics: 0,
            check: |lines| {
                // The line ends with the text of the error of the steps.
                let cause = NoSignals::new().0;

                assert_eq!(
                    lines,
                    [format!(
                        "ERROR the signal handlers of the program child are not in place: {cause}"
                    )]
                );
            },
        },
        Scenario {
            name: "blocking-call",
            run: a_blocking_call_never_ends,
            panics: 0,
            check: |lines| {
                let line = "ERROR the drain limit of the program child passed, and tasks still \
                            run: 1";

                // One line for each of the two runtimes.
                assert_eq!(lines, [line, line]);
            },
        },
        Scenario {
            name: "limit-after-main",
            run: the_drain_limit_starts_at_the_return_of_main,
            panics: 0,
            // A limit that passed is one line. This limit does not pass.
            check: no_line,
        },
        Scenario {
            name: "sigterm",
            run: sigterm_stops_a_program,
            panics: 0,
            check: no_line,
        },
        Scenario {
            name: "sighup-reload",
            run: sighup_is_one_item_for_a_program_with_a_reload,
            panics: 0,
            check: no_line,
        },
        Scenario {
            name: "no-reload",
            run: a_program_with_no_reload_gets_no_hangups,
            panics: 0,
            check: no_line,
        },
        Scenario {
            name: "ready",
            run: ready_names_each_listener,
            panics: 0,
            check: |lines| {
                assert_eq!(lines.len(), 2, "{lines:?}");
                assert!(lines[0].starts_with("INFO listening on /"), "{lines:?}");
                assert!(lines[0].ends_with("/a.sock"), "{lines:?}");
                assert!(lines[1].starts_with("INFO listening on /"), "{lines:?}");
                assert!(lines[1].ends_with("/b.sock"), "{lines:?}");
            },
        },
    ];

    /// The check of a scenario that writes no line of this module.
    fn no_line(lines: &[String]) {
        assert!(lines.is_empty(), "{lines:?}");
    }

    /// The child of the test below. Without the variable it does nothing.
    #[test]
    fn the_child_runs_one_scenario() {
        let Some(name) = std::env::var_os(SCENARIO_VARIABLE) else {
            return;
        };
        let scenario = SCENARIOS
            .iter()
            .find(|scenario| name == scenario.name)
            .unwrap();

        log::init(CHILD_PROGRAM);
        (scenario.run)();
    }

    #[test]
    fn each_scenario_writes_its_lines() {
        for scenario in SCENARIOS {
            let name = scenario.name;
            let child = run_child(SCENARIO_TEST, SCENARIO_VARIABLE, name);
            let stdout = String::from_utf8(child.stdout).unwrap();
            let stderr = String::from_utf8(child.stderr).unwrap();

            assert!(child.status.success(), "{name}: {stdout}\n{stderr}");
            assert!(stdout.contains("1 passed"), "{name}: {stdout}");
            // No line holds the message of a panic.
            assert!(!stderr.contains(PANIC_MESSAGE), "{name}: {stderr}");
            assert!(!stdout.contains(PANIC_MESSAGE), "{name}: {stdout}");

            // A line is the time, the level, the target and the message. The
            // target of a line of the panic hook is the program.
            let lines_with_target = |wanted: &str| -> Vec<String> {
                stderr
                    .lines()
                    .filter_map(|line| {
                        let mut parts = line.splitn(4, ' ');
                        let (_time, level, target, message) =
                            (parts.next()?, parts.next()?, parts.next()?, parts.next()?);

                        (target == wanted).then(|| format!("{level} {message}"))
                    })
                    .collect()
            };
            let of_the_hook = lines_with_target(CHILD_PROGRAM);

            assert_eq!(of_the_hook.len(), scenario.panics, "{name}: {stderr}");
            for line in &of_the_hook {
                assert!(line.starts_with("ERROR panic at "), "{name}: {stderr}");
            }
            (scenario.check)(&lines_with_target(LOG_TARGET));
        }
    }

    /// A panic of `main` ends the program with the status of a panic, and
    /// not with `EX_CONFIG`. A task that must complete still ends whole.
    fn the_main_of_a_program_panics() {
        for program in each_program(LONG_DRAIN) {
            let written = Arc::new(AtomicBool::new(false));
            let in_task = Arc::clone(&written);

            let status = run(program, |context| async move {
                drop(
                    context
                        .tasks()
                        .spawn_must_complete("slow-write", async move {
                            tokio::time::sleep(TASK_TIME).await;
                            in_task.store(true, Ordering::SeqCst);
                        }),
                );
                // The panic comes after a wait, at a later poll.
                tokio::task::yield_now().await;

                main_that_panics(context).await
            });

            assert!(same_status(status, 1), "{program:?}");
            assert!(!same_status(status, EX_CONFIG), "{program:?}");
            assert!(written.load(Ordering::SeqCst), "{program:?}");
        }
    }

    /// The call of `main` makes the future. A panic there is a panic of
    /// `main` too: a task that the call started still ends whole.
    fn the_call_of_main_panics() {
        for program in each_program(LONG_DRAIN) {
            let written = Arc::new(AtomicBool::new(false));
            let in_task = Arc::clone(&written);

            let status = run(program, move |context| -> std::future::Ready<ExitCode> {
                drop(
                    context
                        .tasks()
                        .spawn_must_complete("slow-write", async move {
                            tokio::time::sleep(TASK_TIME).await;
                            in_task.store(true, Ordering::SeqCst);
                        }),
                );

                panic!("{PANIC_MESSAGE}")
            });

            assert!(same_status(status, 1), "{program:?}");
            assert!(!same_status(status, EX_CONFIG), "{program:?}");
            // Without the guard of the call, the panic leaves the runtime,
            // and no wait for the tasks runs.
            assert!(written.load(Ordering::SeqCst), "{program:?}");
        }
    }

    fn the_body_of_an_entry_function_panics() {
        let through = enter(CHILD_PROGRAM, || ExitCode::from(STATUS_OF_MAIN));
        let panicked = enter(CHILD_PROGRAM, || panic!("{PANIC_MESSAGE}"));

        assert!(same_status(through, STATUS_OF_MAIN));
        assert!(same_status(panicked, 1));
        assert!(!same_status(panicked, EX_CONFIG));
    }

    fn the_system_gives_no_runtime() {
        let program = Program::new(CHILD_PROGRAM, Threads::One, LONG_DRAIN);
        let ran = AtomicBool::new(false);

        let status = run_through(&NoRuntime, program, |_context| async {
            ran.store(true, Ordering::SeqCst);

            ExitCode::SUCCESS
        });

        assert!(same_status(status, 71));
        assert!(!same_status(status, EX_CONFIG));
        assert!(!ran.load(Ordering::SeqCst));
    }

    fn the_system_gives_no_signal_handler() {
        let program = Program::new(CHILD_PROGRAM, Threads::One, LONG_DRAIN);
        let steps = NoSignals::new();
        let ran = AtomicBool::new(false);

        let status = run_through(&steps, program, |_context| async {
            ran.store(true, Ordering::SeqCst);

            ExitCode::SUCCESS
        });

        assert!(same_status(status, 71));
        assert!(!same_status(status, EX_CONFIG));
        assert!(!ran.load(Ordering::SeqCst));
    }

    /// `run` returns at the drain limit, with the status of `main`, while
    /// one blocking call still runs.
    fn a_blocking_call_never_ends() {
        for program in each_program(SHORT_DRAIN) {
            let (release, released) = mpsc::channel::<()>();
            let started = Instant::now();

            let status = run(program, |context| async move {
                drop(context.tasks().spawn_blocking("stuck-call", move || {
                    // The call ends only when the test drops its end.
                    let _ = released.recv();
                }));

                ExitCode::from(STATUS_OF_MAIN)
            });
            let waited = started.elapsed();

            assert!(same_status(status, STATUS_OF_MAIN), "{program:?}");
            assert!(waited >= SHORT_DRAIN, "{program:?}: {waited:?}");
            assert!(waited < LIMIT, "{program:?}: {waited:?}");
            drop(release);
        }
    }

    /// The drain limit starts at the return of `main`. A `main` that
    /// continues after the stop signal for longer than the limit leaves a
    /// task the whole limit.
    fn the_drain_limit_starts_at_the_return_of_main() {
        const DRAIN: Duration = Duration::from_secs(3);
        const MAIN_AFTER_THE_SIGNAL: Duration = Duration::from_millis(3500);
        let program = Program::new(CHILD_PROGRAM, Threads::One, DRAIN);
        let written = Arc::new(AtomicBool::new(false));
        let in_task = Arc::clone(&written);

        let status = run(program, |context| async move {
            kill_process(getpid(), Signal::TERM).unwrap();
            within(context.shutdown().cancelled()).await;
            // `main` continues after the signal, as a `main` that waits for
            // its open requests does.
            tokio::time::sleep(MAIN_AFTER_THE_SIGNAL).await;
            drop(
                context
                    .tasks()
                    .spawn_must_complete("late-write", async move {
                        tokio::time::sleep(TASK_TIME).await;
                        in_task.store(true, Ordering::SeqCst);
                    }),
            );

            ExitCode::from(STATUS_OF_MAIN)
        });

        assert!(same_status(status, STATUS_OF_MAIN));
        // A limit that starts at the signal passed before `main` returned,
        // and the task then gets no time.
        assert!(written.load(Ordering::SeqCst));
    }

    fn sigterm_stops_a_program() {
        for program in each_program(LONG_DRAIN) {
            let status = run(program, |context| async move {
                assert!(!context.shutdown().is_cancelled());
                kill_process(getpid(), Signal::TERM).unwrap();
                within(context.shutdown().cancelled()).await;

                ExitCode::from(STATUS_OF_MAIN)
            });

            assert!(same_status(status, STATUS_OF_MAIN), "{program:?}");
        }
    }

    fn sighup_is_one_item_for_a_program_with_a_reload() {
        for program in each_program(LONG_DRAIN) {
            let program = program.with_on_hangup(OnHangup::Reload);

            let status = run(program, |mut context| async move {
                let mut hangups = context.take_hangups().unwrap();
                assert!(context.take_hangups().is_none());

                kill_process(getpid(), Signal::HUP).unwrap();
                assert_eq!(within(hangups.next()).await, Some(()));
                assert!(!context.shutdown().is_cancelled());

                ExitCode::from(STATUS_OF_MAIN)
            });

            assert!(same_status(status, STATUS_OF_MAIN), "{program:?}");
        }
    }

    fn a_program_with_no_reload_gets_no_hangups() {
        let program = Program::new(CHILD_PROGRAM, Threads::One, LONG_DRAIN);

        let status = run(program, |mut context| async move {
            assert!(context.take_hangups().is_none());

            ExitCode::from(STATUS_OF_MAIN)
        });

        assert!(same_status(status, STATUS_OF_MAIN));
    }

    fn ready_names_each_listener() {
        let root = TempRoot::new().unwrap();
        let listen = |name: &str| Listen::Unix {
            socket: root.path().join(name).to_str().unwrap().parse().unwrap(),
            dir: SocketDir::LeaveAsItIs,
        };
        let runtime = Host.runtime(Threads::One).unwrap();

        runtime.block_on(async {
            let first = bind(listen("a.sock")).await.unwrap();
            let second = bind(listen("b.sock")).await.unwrap();

            ready(&[]);
            ready(&[first, second]);
        });
        runtime.shutdown_timeout(LIMIT);
    }
}
