//! The facade supervisor: one facade for the life of the VM (decisions
//! CH5d, CH19 rule 1).
//!
//! 1. Guest init starts the facade only after the first hello, because the
//!    facade opens the session of the handshake id (decisions CH21-8,
//!    CH25 rule 2, CH62 rule 6).
//! 2. When the reaper reports the facade pid gone, the state becomes
//!    `exited`. Guest init sends SIGKILL to the process group of the old
//!    facade, and after `RESTART_PAUSE` it starts the next one, so at most
//!    one runs, its children included (decisions CH19 rule 1, CH62 rule
//!    8). The pause has no cap, and `starts` counts every start.
//! 3. Each other reaped pid is an orphan that PID 1 took over. It changes
//!    no state.
//! 4. A control link close never touches the facade (decision CH19 rule
//!    1). Only a stop ends it.
//! 5. The stop: SIGTERM to the facade group, up to `SIGTERM_GRACE` for its exit,
//!    then the power-off, which sends SIGKILL to every process left
//!    (decision CH62 rule 9). With no live facade, the power-off comes at
//!    once.
//!
//! `Supervisor` is the pure state machine: the inputs of the shell in, one
//! `Order` or none out. It reads no clock: the shell runs each timer and
//! calls `timer` when it ends. The Linux shell, `Facade`, starts the
//! process and runs the reaper thread.

use std::num::NonZeroU32;
use std::time::Duration;

use creche_wire::{FacadeExited, FacadeNotStarted, FacadeStarting, FacadeState, StartCount};

/// The pause between a facade exit and the next start (decision CH62
/// rule 8). A correct facade never exits on a typed error (decision CH23
/// rule 2), so a loop means a bug, and the pause keeps it from spinning a
/// vCPU.
pub const RESTART_PAUSE: Duration = Duration::from_secs(1);

/// How long the facade has to exit after SIGTERM, before the SIGKILL of
/// every process left (decision CH62 rule 9).
pub const SIGTERM_GRACE: Duration = Duration::from_secs(2);

/// What the shell does for the supervisor.
#[derive(Debug, PartialEq, Eq)]
pub enum Order {
    /// Start the facade, then call `Supervisor::started` or
    /// `Supervisor::start_failed`.
    Start,
    /// Run a timer of this length, then call `Supervisor::timer`. A new
    /// timer replaces the one that runs.
    Timer(Duration),
    /// Send SIGTERM to the process group of the facade.
    Terminate(NonZeroU32),
    /// Send SIGKILL to the process group of the facade that exited, so
    /// no child of it runs beside the next start.
    Kill(NonZeroU32),
    /// Power the VM off. The power-off sends SIGKILL to every process
    /// left first (decision CH62 rule 9).
    PowerOff,
}

/// Where the facade is in its life.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Life {
    /// No hello came yet.
    Idle,
    /// `Order::Start` went out, and no answer came yet.
    Spawning,
    /// The facade process runs.
    Live(NonZeroU32),
    /// `RESTART_PAUSE` runs, after an exit or a failed start.
    Paused,
    /// SIGTERM went to the facade, and `SIGTERM_GRACE` runs.
    Stopping(NonZeroU32),
    /// `Order::PowerOff` went out.
    Stopped,
}

/// The facade of the VM: its life and its start count.
#[derive(Debug)]
pub struct Supervisor {
    life: Life,
    /// How many times guest init started the facade, or `None` before
    /// the first start.
    starts: Option<StartCount>,
}

impl Default for Supervisor {
    /// The state at boot: no facade.
    fn default() -> Self {
        Self {
            life: Life::Idle,
            starts: None,
        }
    }
}

impl Supervisor {
    /// A hello got its clock set. The first one starts the facade
    /// (decisions CH21-8, CH25 rule 2).
    pub fn hello(&mut self) -> Option<Order> {
        if self.life != Life::Idle {
            return None;
        }
        self.life = Life::Spawning;
        Some(Order::Start)
    }

    /// The shell started the facade as `pid`.
    pub fn started(&mut self, pid: NonZeroU32) -> Option<Order> {
        if self.life != Life::Spawning {
            return None;
        }
        let next = self
            .starts
            .map_or(1, |starts| starts.get().saturating_add(1));
        // A count past 2^53 - 1 stays at the top: at one start a second
        // that takes 285 million years.
        if let Ok(starts) = StartCount::try_from(next) {
            self.starts = Some(starts);
        }
        self.life = Life::Live(pid);
        None
    }

    /// The shell did not start the facade. The next try follows the
    /// restart pause, and the count stays.
    // CONTRACT-QUESTION: no row decides the answer to a failed start, for
    // example a root disk with no facade program. The reading taken:
    // guest init writes the error on the console, counts no start, keeps
    // the state and tries again after `RESTART_PAUSE` (decision CH62 rule
    // 8), with no cap. The vmm sees no `starting`, so it never snapshots
    // the VM. A change costs this arm and its test.
    pub fn start_failed(&mut self) -> Option<Order> {
        if self.life != Life::Spawning {
            return None;
        }
        self.life = Life::Paused;
        Some(Order::Timer(RESTART_PAUSE))
    }

    /// The reaper reaped `pid`. Only the facade pid changes the state
    /// (decision CH19 rule 1). An exit outside the stop kills the group of
    /// the facade before the restart pause. In the stop, the power-off
    /// kills every process left.
    pub fn reaped(&mut self, pid: NonZeroU32) -> Vec<Order> {
        match self.life {
            Life::Live(facade) if facade == pid => {
                self.life = Life::Paused;
                vec![Order::Kill(pid), Order::Timer(RESTART_PAUSE)]
            }
            Life::Stopping(facade) if facade == pid => {
                self.life = Life::Stopped;
                vec![Order::PowerOff]
            }
            _ => Vec::new(),
        }
    }

    /// The timer of the last `Order::Timer` ended.
    pub fn timer(&mut self) -> Option<Order> {
        match self.life {
            Life::Paused => {
                self.life = Life::Spawning;
                Some(Order::Start)
            }
            Life::Stopping(_) => {
                self.life = Life::Stopped;
                Some(Order::PowerOff)
            }
            Life::Idle | Life::Spawning | Life::Live(_) | Life::Stopped => None,
        }
    }

    /// The vmm sent `stop` (decisions CH19 rule 3, CH62 rule 9). A second
    /// stop changes nothing.
    pub fn stop(&mut self) -> Vec<Order> {
        match self.life {
            Life::Live(pid) => {
                self.life = Life::Stopping(pid);
                vec![Order::Terminate(pid), Order::Timer(SIGTERM_GRACE)]
            }
            Life::Idle | Life::Spawning | Life::Paused => {
                self.life = Life::Stopped;
                vec![Order::PowerOff]
            }
            Life::Stopping(_) | Life::Stopped => Vec::new(),
        }
    }

    /// The pid of the live facade.
    #[cfg(target_os = "linux")]
    pub fn pid(&self) -> Option<NonZeroU32> {
        match self.life {
            Life::Live(pid) | Life::Stopping(pid) => Some(pid),
            Life::Idle | Life::Spawning | Life::Paused | Life::Stopped => None,
        }
    }

    /// The facade state that `ControlReady` and `GuestEvent::Facade`
    /// report (decisions CH19 rule 2, CH32 rule 6). A live facade is
    /// `starting`: it has no local link to guest init yet. No turn can
    /// end before that link opens, so `last_turn_end` is `null` (decision
    /// CH32 rule 7).
    pub fn state(&self) -> FacadeState {
        match (self.life, self.starts) {
            (Life::Live(pid) | Life::Stopping(pid), Some(starts)) => {
                FacadeState::Starting(FacadeStarting {
                    last_turn_end: None,
                    pid,
                    starts,
                })
            }
            (_, Some(starts)) => FacadeState::Exited(FacadeExited {
                last_turn_end: None,
                starts,
            }),
            (_, None) => FacadeState::NotStarted(FacadeNotStarted {}),
        }
    }
}

#[cfg(target_os = "linux")]
pub use shell::{Facade, Reaped, signal};

/// The facade process and the reaper thread (decisions CH8-1, CH49 rule
/// 1, CH62 rules 2, 6 and 26). The kvm tests prove it.
#[cfg(target_os = "linux")]
mod shell {
    use std::ffi::OsStr;
    use std::fmt;
    use std::io;
    use std::num::NonZeroU32;
    use std::os::unix::process::CommandExt as _;
    use std::process::{Command, Stdio};
    use std::sync::mpsc::{Receiver, Sender, channel};
    use std::sync::{Arc, Mutex, PoisonError};
    use std::thread;

    use creche_wire::SessionId;
    use creche_wire::guest::{FACADE_PROGRAM, SESSION_ID_ENV, WORKSPACE};
    use rustix::io::Errno;
    use rustix::process::{
        Pid, Signal, WaitId, WaitIdOptions, WaitOptions, WaitStatus, kill_process_group, wait,
        waitid,
    };

    use crate::env_file::Env;

    /// One process that the reaper reaped.
    #[derive(Debug)]
    pub struct Reaped {
        pub pid: NonZeroU32,
        status: WaitStatus,
    }

    impl fmt::Display for Reaped {
        fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
            if let Some(code) = self.status.exit_status() {
                write!(formatter, "exit status {code}")
            } else if let Some(signal) = self.status.terminating_signal() {
                write!(formatter, "signal {signal}")
            } else {
                write!(formatter, "{:?}", self.status)
            }
        }
    }

    /// The facade start and the reaper thread of PID 1.
    #[derive(Debug)]
    pub struct Facade {
        /// The image env of the root disk (decision CH49 rule 1).
        env: Env,
        /// Held while `start` runs. std reaps a child whose exec failed
        /// inside `Command::spawn`, so the reaper takes no child then.
        spawning: Arc<Mutex<()>>,
        /// Wakes the reaper after a start, when it found no child.
        spawned: Sender<()>,
    }

    impl Facade {
        /// Starts the reaper thread. It calls `report` with each reaped
        /// process. It stops when `report` returns false, or after it
        /// reports a wait error.
        ///
        /// # Errors
        ///
        /// The error of the thread spawn.
        pub fn new(
            env: Env,
            report: impl FnMut(io::Result<Reaped>) -> bool + Send + 'static,
        ) -> io::Result<Self> {
            let spawning = Arc::new(Mutex::new(()));
            let (spawned, wake) = channel();
            let lock = Arc::clone(&spawning);
            thread::Builder::new()
                .name("reaper".to_owned())
                .spawn(move || reap_loop(&lock, &wake, report))?;
            Ok(Self {
                env,
                spawning,
                spawned,
            })
        }

        /// Starts the facade at `FACADE_PROGRAM` for `session` and returns
        /// its pid (decisions CH5d, CH62 rules 5, 6 and 26). The env is
        /// the image env plus `CRECHE_SESSION_ID`, the cwd is the
        /// workspace (made when absent), stdin and stdout are /dev/null,
        /// and stderr is the console of PID 1. The facade runs as root,
        /// because the VM is the boundary (decision CH8-1, fact 2).
        ///
        /// # Errors
        ///
        /// The error of the workspace mkdir or of the spawn.
        pub fn start(&self, session: &SessionId) -> io::Result<NonZeroU32> {
            std::fs::create_dir_all(WORKSPACE)?;
            let mut command = Command::new(FACADE_PROGRAM);
            command
                .env_clear()
                .envs(self.env.vars())
                // The handshake id is the last value, so it wins over a
                // name of the image env (decision CH62 rule 6).
                .env(SESSION_ID_ENV, OsStr::new(&session.to_string()))
                .current_dir(WORKSPACE)
                .stdin(Stdio::null())
                .stdout(Stdio::null())
                .stderr(Stdio::inherit())
                // A process group of its own, with the facade pid as its
                // id, so each signal of guest init reaches the children
                // of the facade too.
                // CONTRACT-QUESTION: CH19 rule 1, CH32 rule 7 and CH62
                // rule 9 name only the facade. The reading taken: each
                // SIGTERM and SIGKILL goes to its group, and the group of
                // an exited facade gets SIGKILL before the restart. A
                // change costs this call, `signal` and the `Live` arm of
                // `Supervisor::reaped`.
                .process_group(0);
            let child = {
                let _guard = self.spawning.lock().unwrap_or_else(PoisonError::into_inner);
                command.spawn()?
            };
            // The reaper reaps the facade, so the `Child` is dropped with
            // no wait.
            let pid = NonZeroU32::new(child.id())
                .ok_or_else(|| io::Error::other("the facade has pid 0"))?;
            let _ = self.spawned.send(());
            Ok(pid)
        }
    }

    /// Sends `signal` to the process group of the facade `pid`: SIGTERM
    /// in the stop (decision CH62 rule 9), SIGKILL after its exit.
    ///
    /// # Errors
    ///
    /// The error of `kill`. ESRCH means that no process of the group is
    /// left. A pid past `i32::MAX` is `EINVAL`.
    pub fn signal(pid: NonZeroU32, signal: Signal) -> Result<(), Errno> {
        let pid = i32::try_from(pid.get())
            .ok()
            .and_then(Pid::from_raw)
            .ok_or(Errno::INVAL)?;
        kill_process_group(pid, signal)
    }

    /// Reaps each child of PID 1: the facade and each orphan that PID 1
    /// took over.
    ///
    /// `waitid` with `WNOWAIT` waits for a child that exited and leaves it
    /// a zombie. Then, under the spawn lock, `wait` with `WNOHANG` reaps
    /// each zombie. So the reaper never takes a child that `spawn` reaps
    /// itself. With no child, the reaper waits for the next start.
    fn reap_loop(
        lock: &Mutex<()>,
        wake: &Receiver<()>,
        mut report: impl FnMut(io::Result<Reaped>) -> bool,
    ) {
        loop {
            match waitid(WaitId::All, WaitIdOptions::EXITED | WaitIdOptions::NOWAIT) {
                Ok(_) => {}
                Err(Errno::INTR) => continue,
                Err(Errno::CHILD) => {
                    if wake.recv().is_err() {
                        return;
                    }
                    continue;
                }
                Err(error) => {
                    report(Err(error.into()));
                    return;
                }
            }
            // The reports go out after the lock is dropped. A report can
            // block on a full inbox, and the control loop takes this lock
            // in `Facade::start`.
            let (reaped, failed) = reap_zombies(lock);
            for each in reaped {
                if !report(Ok(each)) {
                    return;
                }
            }
            if let Some(error) = failed {
                report(Err(error.into()));
                return;
            }
        }
    }

    /// Reaps each zombie under the spawn lock, with `wait(WNOHANG)`. It
    /// returns them, and the wait error that stopped it.
    fn reap_zombies(lock: &Mutex<()>) -> (Vec<Reaped>, Option<Errno>) {
        let _guard = lock.lock().unwrap_or_else(PoisonError::into_inner);
        let mut reaped = Vec::new();
        loop {
            match wait(WaitOptions::NOHANG) {
                Ok(Some((pid, status))) => {
                    let Ok(pid) = u32::try_from(pid.as_raw_nonzero().get()) else {
                        continue;
                    };
                    let Some(pid) = NonZeroU32::new(pid) else {
                        continue;
                    };
                    reaped.push(Reaped { pid, status });
                }
                Err(Errno::INTR) => {}
                Ok(None) | Err(Errno::CHILD) => return (reaped, None),
                Err(error) => return (reaped, Some(error)),
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn pid(raw: u32) -> NonZeroU32 {
        NonZeroU32::new(raw).unwrap()
    }

    fn starting(raw: u32, starts: u64) -> FacadeState {
        FacadeState::Starting(FacadeStarting {
            last_turn_end: None,
            pid: pid(raw),
            starts: StartCount::try_from(starts).unwrap(),
        })
    }

    fn exited(starts: u64) -> FacadeState {
        FacadeState::Exited(FacadeExited {
            last_turn_end: None,
            starts: StartCount::try_from(starts).unwrap(),
        })
    }

    /// A supervisor whose facade runs as `raw`.
    fn live(raw: u32) -> Supervisor {
        let mut supervisor = Supervisor::default();
        assert_eq!(supervisor.hello(), Some(Order::Start));
        assert_eq!(supervisor.started(pid(raw)), None);
        supervisor
    }

    #[test]
    fn starts_only_after_the_first_hello() {
        let mut supervisor = Supervisor::default();
        assert_eq!(supervisor.timer(), None);
        assert_eq!(supervisor.reaped(pid(7)), vec![]);
        assert_eq!(
            supervisor.state(),
            FacadeState::NotStarted(FacadeNotStarted {})
        );

        assert_eq!(supervisor.hello(), Some(Order::Start));
        // A later hello, of this link or of a new one, starts nothing.
        assert_eq!(supervisor.hello(), None);
        assert_eq!(supervisor.started(pid(7)), None);
        assert_eq!(supervisor.hello(), None);
        assert_eq!(supervisor.state(), starting(7, 1));
    }

    #[test]
    fn restarts_only_after_the_reaper_reports_the_exit() {
        let mut supervisor = live(7);
        // No timer and no hello starts a second facade while one runs.
        assert_eq!(supervisor.timer(), None);
        assert_eq!(supervisor.hello(), None);
        assert_eq!(supervisor.state(), starting(7, 1));

        assert_eq!(
            supervisor.reaped(pid(7)),
            vec![Order::Kill(pid(7)), Order::Timer(RESTART_PAUSE)]
        );
        assert_eq!(supervisor.state(), exited(1));
    }

    #[test]
    fn waits_the_restart_pause() {
        assert_eq!(RESTART_PAUSE, Duration::from_secs(1));
        let mut supervisor = live(7);
        assert_eq!(
            supervisor.reaped(pid(7)),
            vec![Order::Kill(pid(7)), Order::Timer(RESTART_PAUSE)]
        );
        // Only the end of the pause starts the next one.
        assert_eq!(supervisor.hello(), None);
        assert_eq!(supervisor.timer(), Some(Order::Start));
        assert_eq!(supervisor.timer(), None);
    }

    #[test]
    fn counts_starts_across_restarts() {
        let mut supervisor = live(7);
        supervisor.reaped(pid(7));
        supervisor.timer();
        // During the start the state is still the exit.
        assert_eq!(supervisor.state(), exited(1));
        assert_eq!(supervisor.started(pid(9)), None);
        assert_eq!(supervisor.state(), starting(9, 2));

        // A failed start counts nothing, and the next try follows the
        // pause.
        supervisor.reaped(pid(9));
        supervisor.timer();
        assert_eq!(supervisor.start_failed(), Some(Order::Timer(RESTART_PAUSE)));
        assert_eq!(supervisor.state(), exited(2));
        assert_eq!(supervisor.timer(), Some(Order::Start));
        supervisor.started(pid(11));
        assert_eq!(supervisor.state(), starting(11, 3));
    }

    #[test]
    fn a_failed_first_start_stays_not_started() {
        let mut supervisor = Supervisor::default();
        supervisor.hello();
        assert_eq!(supervisor.start_failed(), Some(Order::Timer(RESTART_PAUSE)));
        assert_eq!(
            supervisor.state(),
            FacadeState::NotStarted(FacadeNotStarted {})
        );
        assert_eq!(supervisor.timer(), Some(Order::Start));
        supervisor.started(pid(7));
        assert_eq!(supervisor.state(), starting(7, 1));
    }

    #[test]
    fn ignores_an_orphan_exit() {
        let mut supervisor = live(7);
        assert_eq!(supervisor.reaped(pid(8)), vec![]);
        assert_eq!(supervisor.state(), starting(7, 1));

        // An orphan during the pause starts nothing either.
        supervisor.reaped(pid(7));
        assert_eq!(supervisor.reaped(pid(8)), vec![]);
        assert_eq!(supervisor.state(), exited(1));
    }

    #[test]
    fn stop_sends_sigterm_then_sigkill_after_the_grace() {
        assert_eq!(SIGTERM_GRACE, Duration::from_secs(2));
        let mut supervisor = live(7);
        assert_eq!(
            supervisor.stop(),
            vec![Order::Terminate(pid(7)), Order::Timer(SIGTERM_GRACE)]
        );
        // The facade is live until the reaper reports it.
        assert_eq!(supervisor.state(), starting(7, 1));
        assert_eq!(supervisor.stop(), vec![]);
        assert_eq!(supervisor.reaped(pid(8)), vec![]);
        // The end of the grace powers off, and the power-off sends
        // SIGKILL to every process left.
        assert_eq!(supervisor.timer(), Some(Order::PowerOff));
        assert_eq!(supervisor.timer(), None);
        assert_eq!(supervisor.stop(), vec![]);
    }

    #[test]
    fn stop_powers_off_when_the_facade_exits_in_the_grace() {
        let mut supervisor = live(7);
        supervisor.stop();
        assert_eq!(supervisor.reaped(pid(7)), vec![Order::PowerOff]);
        assert_eq!(supervisor.state(), exited(1));
        // No restart follows the exit.
        assert_eq!(supervisor.timer(), None);
    }

    #[test]
    fn stop_with_no_live_facade_powers_off_at_once() {
        let mut supervisor = Supervisor::default();
        assert_eq!(supervisor.stop(), vec![Order::PowerOff]);
        assert_eq!(supervisor.hello(), None);

        let mut supervisor = live(7);
        supervisor.reaped(pid(7));
        assert_eq!(supervisor.stop(), vec![Order::PowerOff]);
        // The pause ends with no start.
        assert_eq!(supervisor.timer(), None);
    }
}
