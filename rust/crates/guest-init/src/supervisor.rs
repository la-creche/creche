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
//!    1). A close of its local link kills it (decision CH32 rule 7).
//! 5. A live facade is `starting` until it opens its local link, then
//!    `running` until the reap (decision CH32 rules 6 and 7). Its turn
//!    events set `turn_running` and the last turn end, which every later
//!    state keeps (decision CH32 rules 4 and 5).
//! 6. The stop (decision CH62 rule 9): the stop line on the local link,
//!    up to `FACADE_STOP_WAIT`, then SIGTERM to the facade group, up to
//!    `SIGTERM_GRACE`, then the power-off, which sends SIGKILL to every
//!    process left. With no live facade, the power-off comes at once.
//!
//! `Supervisor` is the pure state machine: the inputs of the shell in,
//! the orders out. It reads no clock: the shell runs each timer and calls
//! `timer` when it ends. The Linux shell, `Facade`, starts the process
//! and runs the reaper thread.

use std::num::NonZeroU32;
use std::time::Duration;

use creche_wire::{
    FacadeExited, FacadeNotStarted, FacadeRunning, FacadeStarting, FacadeState, StartCount,
    TurnEnd, TurnEvent,
};

/// The pause between a facade exit and the next start (decision CH62
/// rule 8). A correct facade never exits on a typed error (decision CH23
/// rule 2), so a loop means a bug, and the pause keeps it from spinning a
/// vCPU.
pub const RESTART_PAUSE: Duration = Duration::from_secs(1);

/// How long the facade has to exit after the stop line on its local
/// link, before SIGTERM (decision CH62 rule 9, which amends CH19 rule 3
/// from 30 s to 25 s).
pub const FACADE_STOP_WAIT: Duration = Duration::from_secs(25);

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
    /// Write the stop line on the local link of the facade (decision CH62
    /// rule 9).
    AskStop,
    /// Send SIGTERM to the process group of the facade.
    Terminate(NonZeroU32),
    /// Send SIGKILL to the process group of the facade: its local link
    /// closed (decision CH32 rule 7), or it exited, so no child of it runs
    /// beside the next start.
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
    /// The stop line went to the facade, and `FACADE_STOP_WAIT` runs.
    Asked(NonZeroU32),
    /// SIGTERM went to the facade, and `SIGTERM_GRACE` runs.
    Stopping(NonZeroU32),
    /// `Order::PowerOff` went out.
    Stopped,
}

/// The local link of the facade that runs (decision CH32 rules 6 and 7).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Link {
    /// The facade did not open it yet: the state is `starting`.
    Waiting,
    /// It is open: the state is `running`.
    Open,
    /// It closed. A facade gets one link per start, so the state stays
    /// `running` until the reap.
    Closed,
}

/// The facade of the VM: its life, its start count, its local link and
/// its turns.
#[derive(Debug)]
pub struct Supervisor {
    life: Life,
    /// How many times guest init started the facade, or `None` before
    /// the first start.
    starts: Option<StartCount>,
    link: Link,
    /// True between a turn start and the next turn end (decision CH21).
    turn_running: bool,
    /// The last turn end since boot (decision CH32 rule 4).
    last_turn_end: Option<TurnEnd>,
}

impl Default for Supervisor {
    /// The state at boot: no facade.
    fn default() -> Self {
        Self {
            life: Life::Idle,
            starts: None,
            link: Link::Waiting,
            turn_running: false,
            last_turn_end: None,
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

    /// The shell started the facade as `pid`. It waits for its local link.
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
        self.link = Link::Waiting;
        self.turn_running = false;
        None
    }

    /// The shell did not start the facade. The next try follows the
    /// restart pause, and the count stays.
    // Decision CH66 rule 1: guest init writes the error on the console,
    // counts no start, keeps the state and tries again after
    // `RESTART_PAUSE` (decision CH62 rule 8), with no cap. The vmm sees
    // no `starting`, so it never snapshots the VM.
    pub fn start_failed(&mut self) -> Option<Order> {
        if self.life != Life::Spawning {
            return None;
        }
        self.life = Life::Paused;
        Some(Order::Timer(RESTART_PAUSE))
    }

    /// The reaper reaped `pid`. Only the facade pid changes the state
    /// (decision CH19 rule 1). Its link and its turn end with it. An exit
    /// outside the stop kills the group of the facade before the restart
    /// pause. In the stop, the power-off kills every process left.
    pub fn reaped(&mut self, pid: NonZeroU32) -> Vec<Order> {
        let orders = match self.life {
            Life::Live(facade) if facade == pid => {
                self.life = Life::Paused;
                vec![Order::Kill(pid), Order::Timer(RESTART_PAUSE)]
            }
            Life::Asked(facade) | Life::Stopping(facade) if facade == pid => {
                self.life = Life::Stopped;
                vec![Order::PowerOff]
            }
            _ => return Vec::new(),
        };
        self.link = Link::Closed;
        self.turn_running = false;
        orders
    }

    /// The timer of the last `Order::Timer` ended.
    pub fn timer(&mut self) -> Vec<Order> {
        match self.life {
            Life::Paused => {
                self.life = Life::Spawning;
                vec![Order::Start]
            }
            Life::Asked(pid) => self.terminate(pid),
            Life::Stopping(_) => {
                self.life = Life::Stopped;
                vec![Order::PowerOff]
            }
            Life::Idle | Life::Spawning | Life::Live(_) | Life::Stopped => Vec::new(),
        }
    }

    /// The vmm sent `stop` (decisions CH19 rule 3, CH62 rule 9). A second
    /// stop changes nothing.
    // CONTRACT-QUESTION: CH62 rule 9 starts the stop with the stop line,
    // and a facade with no open local link cannot get it. The reading
    // taken: that stop starts at SIGTERM. A `starting` facade runs no turn
    // that guest init can see (decision CH32 rule 6). A change costs the
    // `Live` arm and its test.
    pub fn stop(&mut self) -> Vec<Order> {
        match self.life {
            Life::Live(pid) if self.link == Link::Open => {
                self.life = Life::Asked(pid);
                vec![Order::AskStop, Order::Timer(FACADE_STOP_WAIT)]
            }
            Life::Live(pid) => self.terminate(pid),
            Life::Idle | Life::Spawning | Life::Paused => {
                self.life = Life::Stopped;
                vec![Order::PowerOff]
            }
            Life::Asked(_) | Life::Stopping(_) | Life::Stopped => Vec::new(),
        }
    }

    /// The SIGTERM step of the stop.
    fn terminate(&mut self, pid: NonZeroU32) -> Vec<Order> {
        self.life = Life::Stopping(pid);
        vec![Order::Terminate(pid), Order::Timer(SIGTERM_GRACE)]
    }

    /// A peer with pid `peer` opened the local link. Guest init keeps it
    /// only when `peer` is the live facade and the facade did not open a
    /// link before (decision CH62 rule 10). Then the state is `running`
    /// (decision CH32 rule 6).
    pub fn linked(&mut self, peer: NonZeroU32) -> bool {
        if self.life != Life::Live(peer) || self.link != Link::Waiting {
            return false;
        }
        self.link = Link::Open;
        true
    }

    /// The local link closed. A facade that is not stopping is killed
    /// (decision CH32 rule 7).
    // CONTRACT-QUESTION: CH32 rule 7 kills the facade at a close, and CH62
    // rule 9 asks it to exit at a stop. A facade may close its link as it
    // exits. The reading taken: during the stop, a close kills nothing,
    // and the stop waits and SIGTERM bound the exit. A change costs the
    // `Asked` and `Stopping` arm and its test.
    pub fn unlinked(&mut self) -> Option<Order> {
        if self.link != Link::Open {
            return None;
        }
        self.link = Link::Closed;
        match self.life {
            Life::Live(pid) => Some(Order::Kill(pid)),
            Life::Asked(_)
            | Life::Stopping(_)
            | Life::Idle
            | Life::Spawning
            | Life::Paused
            | Life::Stopped => None,
        }
    }

    /// A turn event on the open local link (decisions CH21, CH32 rule 4).
    /// No order follows. A start or an end with no turn before it is
    /// kept as it comes: the reader checks each line, not the order.
    pub fn turn(&mut self, event: &TurnEvent) {
        if self.link != Link::Open {
            return;
        }
        match event {
            TurnEvent::Start(_) => self.turn_running = true,
            TurnEvent::End(end) => {
                self.turn_running = false;
                self.last_turn_end = Some(end.clone());
            }
        }
    }

    /// The pid of the live facade.
    pub fn pid(&self) -> Option<NonZeroU32> {
        match self.life {
            Life::Live(pid) | Life::Asked(pid) | Life::Stopping(pid) => Some(pid),
            Life::Idle | Life::Spawning | Life::Paused | Life::Stopped => None,
        }
    }

    /// The facade state that `ControlReady` and `GuestEvent::Facade`
    /// report (decisions CH19 rule 2, CH32 rules 4 to 7).
    pub fn state(&self) -> FacadeState {
        let last_turn_end = self.last_turn_end.clone();
        match (self.pid(), self.starts) {
            (Some(pid), Some(starts)) if self.link == Link::Waiting => {
                FacadeState::Starting(FacadeStarting {
                    last_turn_end,
                    pid,
                    starts,
                })
            }
            (Some(pid), Some(starts)) => FacadeState::Running(FacadeRunning {
                last_turn_end,
                pid,
                starts,
                turn_running: self.turn_running,
            }),
            (None, Some(starts)) => FacadeState::Exited(FacadeExited {
                last_turn_end,
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
                // Decision CH66 rule 3: each SIGTERM and SIGKILL goes to
                // this group, and the group of an exited facade gets
                // SIGKILL before the restart.
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
    /// in the stop (decision CH62 rule 9), SIGKILL when its local link
    /// closed (decision CH32 rule 7) or after its exit.
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

    fn running(raw: u32, turn_running: bool, last_turn_end: Option<TurnEnd>) -> FacadeState {
        FacadeState::Running(FacadeRunning {
            last_turn_end,
            pid: pid(raw),
            starts: StartCount::try_from(1).unwrap(),
            turn_running,
        })
    }

    fn end() -> TurnEnd {
        TurnEnd {
            title: "a turn".parse().unwrap(),
            updated_at: creche_wire::UnixMillis::try_from(1_893_456_000_000).unwrap(),
        }
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
        assert_eq!(supervisor.timer(), vec![]);
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
        assert_eq!(supervisor.timer(), vec![]);
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
        assert_eq!(supervisor.timer(), vec![Order::Start]);
        assert_eq!(supervisor.timer(), vec![]);
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
        assert_eq!(supervisor.timer(), vec![Order::Start]);
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
        assert_eq!(supervisor.timer(), vec![Order::Start]);
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
        assert_eq!(supervisor.timer(), vec![Order::PowerOff]);
        assert_eq!(supervisor.timer(), vec![]);
        assert_eq!(supervisor.stop(), vec![]);
    }

    #[test]
    fn stop_powers_off_when_the_facade_exits_in_the_grace() {
        let mut supervisor = live(7);
        supervisor.stop();
        assert_eq!(supervisor.reaped(pid(7)), vec![Order::PowerOff]);
        assert_eq!(supervisor.state(), exited(1));
        // No restart follows the exit.
        assert_eq!(supervisor.timer(), vec![]);
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
        assert_eq!(supervisor.timer(), vec![]);
    }

    /// With an open local link, the stop writes the stop line first and
    /// waits `FACADE_STOP_WAIT`. Only then SIGTERM goes out, then the
    /// grace, then the power-off (decision CH62 rule 9).
    #[test]
    fn stop_writes_the_stop_line_before_sigterm() {
        assert_eq!(FACADE_STOP_WAIT, Duration::from_secs(25));
        let mut supervisor = live(7);
        assert!(supervisor.linked(pid(7)));
        assert_eq!(
            supervisor.stop(),
            vec![Order::AskStop, Order::Timer(FACADE_STOP_WAIT)]
        );
        assert_eq!(supervisor.stop(), vec![]);
        assert_eq!(
            supervisor.timer(),
            vec![Order::Terminate(pid(7)), Order::Timer(SIGTERM_GRACE)]
        );
        assert_eq!(supervisor.timer(), vec![Order::PowerOff]);

        // A facade that exits in the wait powers off at once.
        let mut supervisor = live(7);
        supervisor.linked(pid(7));
        supervisor.stop();
        assert_eq!(supervisor.reaped(pid(7)), vec![Order::PowerOff]);
        assert_eq!(supervisor.timer(), vec![]);
    }

    /// A close of the link in the stop wait kills nothing: the stop goes
    /// on (the stop close gap of `AGENTS.md` Known gaps). Out of a stop, a
    /// close kills.
    #[test]
    fn a_close_kills_only_out_of_a_stop() {
        let mut supervisor = live(7);
        supervisor.linked(pid(7));
        supervisor.stop();
        assert_eq!(supervisor.unlinked(), None);
        assert_eq!(
            supervisor.timer(),
            vec![Order::Terminate(pid(7)), Order::Timer(SIGTERM_GRACE)]
        );

        let mut supervisor = live(7);
        assert_eq!(supervisor.unlinked(), None, "no link was open");
        supervisor.linked(pid(7));
        assert_eq!(supervisor.unlinked(), Some(Order::Kill(pid(7))));
        assert_eq!(supervisor.unlinked(), None);
        // The killed facade stays `running` until the reap, and it gets
        // no second link (decision CH32 rule 7).
        assert_eq!(supervisor.state(), running(7, false, None));
        assert!(!supervisor.linked(pid(7)));
    }

    /// The link moves `starting` to `running`, and only the live pid gets
    /// it (decisions CH32 rule 6, CH62 rule 10).
    #[test]
    fn links_only_the_live_facade() {
        let mut supervisor = Supervisor::default();
        assert!(!supervisor.linked(pid(7)));
        let mut supervisor = live(7);
        assert!(!supervisor.linked(pid(8)));
        assert_eq!(supervisor.state(), starting(7, 1));
        assert!(supervisor.linked(pid(7)));
        assert_eq!(supervisor.state(), running(7, false, None));
        assert!(!supervisor.linked(pid(7)), "a second link");
    }

    /// The turn flag follows the events, and the last end stays across
    /// the exit (decisions CH21, CH32 rules 4 and 5).
    #[test]
    fn keeps_the_turn_state() {
        let mut supervisor = live(7);
        // No event counts before the link opens.
        supervisor.turn(&TurnEvent::End(end()));
        assert_eq!(supervisor.state(), starting(7, 1));

        supervisor.linked(pid(7));
        supervisor.turn(&TurnEvent::Start(creche_wire::TurnStart {}));
        assert_eq!(supervisor.state(), running(7, true, None));
        supervisor.turn(&TurnEvent::End(end()));
        assert_eq!(supervisor.state(), running(7, false, Some(end())));

        supervisor.reaped(pid(7));
        assert_eq!(
            supervisor.state(),
            FacadeState::Exited(FacadeExited {
                last_turn_end: Some(end()),
                starts: StartCount::try_from(1).unwrap(),
            })
        );
    }
}
