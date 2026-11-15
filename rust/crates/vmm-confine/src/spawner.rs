//! The spawner: one long-lived thread that starts every launch step
//! (decision CH19 (4)).
//!
//! `PR_SET_PDEATHSIG` fires when the thread that forked the child exits,
//! not the process (fact 76). So every launch forks from this one thread,
//! and the thread lives as long as the `Spawner`. A drop of the `Spawner`
//! ends the thread. The kernel then sends SIGKILL only to a launch that
//! has not yet execed CH: the launch step, `systemd-run` or the launch
//! stage. AppArmor clears the death signal at the exec of CH into its slot
//! label, so a drop does not end CH. The `ExecStopPost=` step of the vmm
//! unit ends CH after the vmm stops (decision CH61 rule 1). The vmm keeps
//! its `Spawner` for its whole life.
//!
//! Each launch step gets one pipe for its stdout and stderr in place of
//! the fds of the vmm (decision CH50 rule 12). The spawner starts the
//! reader thread of `output` on the pipe before the fork. The thread keeps
//! the last line of the pipe for `Launched::ended`, so a start whose launch
//! ended names the cause that `systemd-run` wrote.

use std::fmt;
use std::io::{self, Write};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, ExitStatus};
use std::sync::Arc;
use std::sync::mpsc::{self, Receiver, Sender};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};

use crate::launch::{Launch, MODE, PLAN_MAX, Plan};
use crate::output::{self, Sink, Tail};

/// The program of the launch step in production. The kernel resolves this
/// link to the running vmm binary, even after a release renames its file to
/// `.prev` (fact 56). `std::env::current_exe` would give the new name.
pub const SELF_EXE: &str = "/proc/self/exe";

/// How long `Launched::ended` waits after the exit for the end of the
/// output pipe. A process that the launch left behind may still hold the
/// pipe, so the wait has a bound.
pub const TAIL_WAIT: Duration = Duration::from_secs(1);

/// Why a launch did not start.
#[derive(Debug)]
pub enum SpawnError {
    /// The spawner thread is gone.
    Stopped,
    /// The plan did not serialize.
    Plan(serde_json::Error),
    /// The plan has more bytes than `PLAN_MAX`, or more than the pipe
    /// holds. The value is its size.
    TooLarge(usize),
    /// The plan pipe, the output pipe, its reader thread or the launch
    /// step did not start.
    Spawn(io::Error),
}

impl fmt::Display for SpawnError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Stopped => formatter.write_str("the spawner thread is gone"),
            Self::Plan(error) => write!(formatter, "serializing the launch plan: {error}"),
            Self::TooLarge(bytes) => write!(formatter, "the launch plan has {bytes} bytes"),
            Self::Spawn(error) => write!(formatter, "starting the launch step: {error}"),
        }
    }
}

impl std::error::Error for SpawnError {}

/// One launch for the spawner thread, and where its answer goes.
struct Request {
    launch: Launch,
    reply: Sender<Result<Launched, SpawnError>>,
}

/// The handle of the spawner thread.
#[derive(Debug)]
pub struct Spawner {
    requests: Option<Sender<Request>>,
    thread: Option<JoinHandle<()>>,
}

impl Spawner {
    /// Starts the spawner thread. Each launch runs `program launch`. The
    /// vmm passes `SELF_EXE`. The lines of each CH go to the stderr of the
    /// vmm (`output::stderr_sink`).
    ///
    /// # Errors
    ///
    /// The thread did not start.
    pub fn start(program: PathBuf) -> io::Result<Self> {
        Self::with_sink(program, output::stderr_sink())
    }

    /// Starts the spawner thread as `start` does, with the lines of each
    /// CH going to `sink`.
    ///
    /// # Errors
    ///
    /// The thread did not start.
    pub fn with_sink(program: PathBuf, sink: Sink) -> io::Result<Self> {
        let (requests, queue) = mpsc::channel();
        let thread = thread::Builder::new()
            .name("vmm-spawner".to_owned())
            .spawn(move || serve(&program, &sink, &queue))?;
        Ok(Self {
            requests: Some(requests),
            thread: Some(thread),
        })
    }

    /// Starts one launch step with `launch` and gives its handle. The
    /// call waits until the step is forked.
    ///
    /// # Errors
    ///
    /// The `SpawnError` of the spawner thread.
    pub fn launch(&self, launch: Launch) -> Result<Launched, SpawnError> {
        let (reply, answer) = mpsc::channel();
        let requests = self.requests.as_ref().ok_or(SpawnError::Stopped)?;
        requests
            .send(Request { launch, reply })
            .map_err(|_| SpawnError::Stopped)?;
        answer.recv().map_err(|_| SpawnError::Stopped)?
    }
}

impl Drop for Spawner {
    /// Ends the thread. The death signal then ends each launch that has
    /// not yet execed CH. CH under its slot label has no death signal, and
    /// it ends after the vmm unit stops (decision CH61 rule 1).
    fn drop(&mut self) {
        drop(self.requests.take());
        if let Some(thread) = self.thread.take() {
            // An error here means the thread panicked. The thread is gone
            // either way, which is all the drop needs.
            let _ = thread.join();
        }
    }
}

/// The body of the spawner thread. It ends when the `Spawner` is dropped.
fn serve(program: &Path, sink: &Sink, queue: &Receiver<Request>) {
    for request in queue {
        // A caller that stopped waiting drops its answer. Its child then
        // has no handle. Before its exec of CH it dies with this thread.
        // After it, CH ends after the vmm unit stops (decision CH61 rule
        // 1).
        let _ = request.reply.send(spawn(program, sink, request.launch));
    }
}

/// Writes the plan into a new pipe, starts the output reader on a second
/// pipe, then forks one launch step from this thread with the plan pipe
/// on fd 0 and the output pipe on fds 1 and 2.
fn spawn(program: &Path, sink: &Sink, launch: Launch) -> Result<Launched, SpawnError> {
    let id = launch.scope().id().clone();
    let plan = Plan {
        spawner: std::process::id(),
        launch,
    };
    let bytes = serde_json::to_vec(&plan).map_err(SpawnError::Plan)?;
    if bytes.len() > PLAN_MAX {
        return Err(SpawnError::TooLarge(bytes.len()));
    }
    // The whole plan goes in the pipe and the write end closes before the
    // spawn. The step then finds the plan at once and reads to EOF.
    let (reader, mut writer) = io::pipe().map_err(SpawnError::Spawn)?;
    // No reader exists yet, so a blocking write of a plan larger than the
    // pipe would hang this thread and every later launch. The kernel may
    // give a pipe less than `PLAN_MAX`, so the write must not block.
    rustix::fs::fcntl_setfl(&writer, rustix::fs::OFlags::NONBLOCK)
        .map_err(|errno| SpawnError::Spawn(errno.into()))?;
    writer
        .write_all(&bytes)
        .map_err(|error| match error.kind() {
            io::ErrorKind::WouldBlock => SpawnError::TooLarge(bytes.len()),
            _ => SpawnError::Spawn(error),
        })?;
    drop(writer);
    // The reader thread starts before the fork. If the fork fails, the
    // write ends close with this function and the thread ends at once.
    let (output, out) = io::pipe().map_err(SpawnError::Spawn)?;
    let err = out.try_clone().map_err(SpawnError::Spawn)?;
    let tail = Arc::new(Tail::default());
    output::start(id, output, Arc::clone(sink), Arc::clone(&tail)).map_err(SpawnError::Spawn)?;
    let child = Command::new(program)
        .arg(MODE)
        .stdin(reader)
        .stdout(out)
        .stderr(err)
        .spawn()
        .map_err(SpawnError::Spawn)?;
    Ok(Launched {
        child,
        tail,
        exit_seen: None,
    })
}

/// How one launch ended: the exit status of its process and the last line
/// on its output pipe. A `systemd-run` that refuses the scope exits
/// non-zero before CH runs, and its last line is the cause, for example
/// `Failed to start transient scope unit: Unit creche-vm-<id>.scope was
/// already loaded or has a fragment file.`
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Exit {
    status: ExitStatus,
    line: Option<String>,
}

impl Exit {
    /// The exit status of the process.
    #[must_use]
    pub fn status(&self) -> ExitStatus {
        self.status
    }

    /// The last line on the output pipe, with its control chars escaped.
    /// `None` if the launch wrote no line.
    #[must_use]
    pub fn line(&self) -> Option<&str> {
        self.line.as_deref()
    }
}

impl fmt::Display for Exit {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match &self.line {
            Some(line) => write!(formatter, "{}, last line: {line}", self.status),
            None => write!(formatter, "{}, with no output line", self.status),
        }
    }
}

/// One running launch. After the execs the pid is the payload's pid
/// (decision CH8-2). The caller must `wait` to reap it.
#[derive(Debug)]
pub struct Launched {
    child: Child,
    /// The last line of the output pipe.
    tail: Arc<Tail>,
    /// When `ended` first found the exit.
    exit_seen: Option<Instant>,
}

impl Launched {
    /// The pid of the launch step, and later of the payload.
    #[must_use]
    pub fn id(&self) -> u32 {
        self.child.id()
    }

    /// Sends SIGTERM, the fallback after a graceful stop (decision CH19
    /// (3)). A payload that was already reaped gets no signal, so a reused
    /// pid is never hit.
    ///
    /// # Errors
    ///
    /// The status check or the signal failed.
    pub fn terminate(&mut self) -> io::Result<()> {
        if self.child.try_wait()?.is_some() {
            return Ok(());
        }
        let pid = rustix::process::Pid::from_child(&self.child);
        rustix::process::kill_process(pid, rustix::process::Signal::TERM).map_err(io::Error::from)
    }

    /// Sends SIGKILL, the last step of a stop (decision CH34 rule 5).
    ///
    /// # Errors
    ///
    /// The signal failed.
    pub fn kill(&mut self) -> io::Result<()> {
        self.child.kill()
    }

    /// The exit status, if the payload has exited.
    ///
    /// # Errors
    ///
    /// The status check failed.
    pub fn try_wait(&mut self) -> io::Result<Option<ExitStatus>> {
        self.child.try_wait()
    }

    /// How the launch ended, once its process exited and its output pipe
    /// ended. A pipe that is still open `TAIL_WAIT` after the first check
    /// that found the exit counts as ended. `None` while the process runs
    /// or that wait lasts. Each call is a status check, so it does not
    /// block. A check reaps an exit, as `try_wait` does.
    ///
    /// # Errors
    ///
    /// The status check failed.
    pub fn ended(&mut self) -> io::Result<Option<Exit>> {
        let Some(status) = self.child.try_wait()? else {
            return Ok(None);
        };
        let seen = *self.exit_seen.get_or_insert_with(Instant::now);
        let last = self.tail.last();
        if !last.ended && seen.elapsed() < TAIL_WAIT {
            return Ok(None);
        }
        Ok(Some(Exit {
            status,
            line: last.line,
        }))
    }

    /// Waits for the payload to exit.
    ///
    /// # Errors
    ///
    /// The wait failed.
    pub fn wait(&mut self) -> io::Result<ExitStatus> {
        self.child.wait()
    }
}
