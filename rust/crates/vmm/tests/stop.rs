//! The stop of one VM: `stop`, then SIGTERM, then SIGKILL (decisions CH19
//! (3), CH34 rule 5, fact 75). The fakes run on paused time, so each wait
//! of 30 s and 5 s passes at once. A shell child proves the real signals.

use std::fs;
use std::io::{self, BufRead, BufReader};
use std::os::unix::process::ExitStatusExt;
use std::path::PathBuf;
use std::process::{Child, Command, ExitStatus, Stdio};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use creche_vmm::control::ControlError;
use creche_vmm::stop::{KILL_WAIT, StopError, StopLink, Stopped, VmProcess, stop};
use creche_vmm_store::layout::{RunRoot, VmRunDir};
use tokio::runtime::Builder;
use tokio::time::Instant;

const ID: &str = "0f8e2a4c-1b3d-4e5f-8a9b-0c1d2e3f4a5b";

/// One step of a stop, and when it came after the start.
type Log = Arc<Mutex<Vec<(&'static str, Duration)>>>;

/// The steps that fail in a `Fake`. A failed step is not in the log.
#[derive(Debug, Clone, Copy, Default)]
struct Fails {
    send: bool,
    /// The write of `stop` never completes, as on a full send buffer.
    stall: bool,
    term: bool,
    kill: bool,
}

/// A fake VM. Its process exits at the step `exits_on`, at once for
/// `gone`, and never for any other name.
#[derive(Debug, Clone)]
struct Fake {
    start: Instant,
    log: Log,
    exits_on: &'static str,
    fails: Fails,
}

/// The process of a `Fake`.
#[derive(Debug)]
struct FakeProcess(Fake);

/// The control link of a `Fake`.
#[derive(Debug)]
struct FakeLink(Fake);

// `cfg(test)` lets the helpers lock with `expect` (rust/clippy.toml).
#[cfg(test)]
impl Fake {
    fn record(&self, step: &'static str) {
        let at = self.start.elapsed();
        self.log.lock().expect("the log").push((step, at));
    }

    fn steps(&self) -> Vec<(&'static str, Duration)> {
        self.log.lock().expect("the log").clone()
    }
}

impl VmProcess for FakeProcess {
    fn exited(&mut self) -> io::Result<bool> {
        let exits_on = self.0.exits_on;
        Ok(exits_on == "gone" || self.0.steps().iter().any(|(step, _)| *step == exits_on))
    }

    fn terminate(&mut self) -> io::Result<()> {
        if self.0.fails.term {
            return Err(io::ErrorKind::PermissionDenied.into());
        }
        self.0.record("term");
        Ok(())
    }

    fn kill(&mut self) -> io::Result<()> {
        if self.0.fails.kill {
            return Err(io::ErrorKind::PermissionDenied.into());
        }
        self.0.record("kill");
        Ok(())
    }
}

impl StopLink for FakeLink {
    async fn send_stop(&self) -> Result<(), ControlError> {
        if self.0.fails.send {
            return Err(ControlError::Io(io::ErrorKind::BrokenPipe));
        }
        if self.0.fails.stall {
            std::future::pending::<()>().await;
        }
        self.0.record("stop");
        Ok(())
    }
}

/// A run root under `/tmp` with the run dir of `ID` and a stale
/// `api.sock.lock`, removed on drop. The temp dir of some hosts is too
/// long for the socket limit of `RunRoot`.
#[derive(Debug)]
struct TempRun(PathBuf, VmRunDir);

// `cfg(test)` lets the helper unwrap (rust/clippy.toml).
#[cfg(test)]
impl TempRun {
    fn new(name: &str) -> Self {
        let path = PathBuf::from(format!("/tmp/vmm-stop-{}-{name}", std::process::id()));
        let _ = fs::remove_dir_all(&path);
        let vm = RunRoot::new(path.clone()).unwrap().vm(&ID.parse().unwrap());
        fs::create_dir_all(vm.path()).unwrap();
        fs::write(vm.api_socket_lock(), b"").unwrap();
        Self(path, vm)
    }
}

impl Drop for TempRun {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

/// What a stop of a fake VM gave: the result, the steps, and whether
/// `api.sock.lock` stays.
type Outcome = (
    Result<Stopped, StopError>,
    Vec<(&'static str, Duration)>,
    bool,
);

/// Stops a fake VM on paused time, with a control link when `link` is
/// set.
#[cfg(test)]
fn try_stop(name: &str, exits_on: &'static str, link: bool, fails: Fails) -> Outcome {
    let runtime = Builder::new_current_thread()
        .enable_time()
        .start_paused(true)
        .build()
        .unwrap();
    let run = TempRun::new(name);
    let vm = &run.1;
    let (stopped, steps) = runtime.block_on(async {
        let fake = Fake {
            start: Instant::now(),
            log: Log::default(),
            exits_on,
            fails,
        };
        let link = link.then(|| FakeLink(fake.clone()));
        let mut process = FakeProcess(fake.clone());
        let stopped = stop(link.as_ref(), &mut process, vm).await;
        (stopped, fake.steps())
    });
    (stopped, steps, vm.api_socket_lock().exists())
}

/// Stops a fake VM on paused time, and checks that the stop worked and
/// removed `api.sock.lock`. `link` is `Some(fails)` for a control link
/// whose send fails when `fails` is set.
#[cfg(test)]
fn stop_fake(
    name: &str,
    exits_on: &'static str,
    link: Option<bool>,
) -> (Stopped, Vec<(&'static str, Duration)>) {
    let fails = Fails {
        send: link == Some(true),
        ..Fails::default()
    };
    let (stopped, steps, lock) = try_stop(name, exits_on, link.is_some(), fails);
    assert!(!lock, "api.sock.lock stays");
    (stopped.unwrap(), steps)
}

const fn secs(secs: u64) -> Duration {
    Duration::from_secs(secs)
}

#[test]
fn a_guest_that_powers_off_gets_no_signal() {
    let (stopped, steps) = stop_fake("off", "stop", Some(false));
    assert_eq!(stopped, Stopped::PoweredOff);
    assert_eq!(steps, [("stop", secs(0))]);
}

#[test]
fn sigterm_follows_30_s_after_stop() {
    let (stopped, steps) = stop_fake("term", "term", Some(false));
    assert_eq!(stopped, Stopped::Terminated);
    assert_eq!(steps, [("stop", secs(0)), ("term", secs(30))]);
}

#[test]
fn sigkill_follows_5_s_after_sigterm_and_is_no_error() {
    let (stopped, steps) = stop_fake("kill", "kill", Some(false));
    assert_eq!(stopped, Stopped::Killed);
    assert_eq!(
        steps,
        [("stop", secs(0)), ("term", secs(30)), ("kill", secs(35))]
    );
}

#[test]
fn a_stop_that_does_not_go_out_skips_its_wait() {
    for (name, link) in [("nolink", None), ("failed", Some(true))] {
        let (stopped, steps) = stop_fake(name, "kill", link);
        assert_eq!(stopped, Stopped::Killed);
        assert_eq!(steps, [("term", secs(0)), ("kill", secs(5))]);
    }
}

/// A write of `stop` that never completes counts as not sent. Its timeout
/// counts inside the 30 s, so SIGTERM still follows 30 s after the start
/// (decision CH50 rule 3).
#[test]
fn a_stalled_stop_write_gets_sigterm_at_30_s() {
    let fails = Fails {
        stall: true,
        ..Fails::default()
    };
    let (stopped, steps, lock) = try_stop("stall", "term", true, fails);
    assert_eq!(stopped.unwrap(), Stopped::Terminated);
    assert_eq!(steps, [("term", secs(30))]);
    assert!(!lock, "api.sock.lock stays");
}

/// A failed SIGTERM skips its 5 s wait, so SIGKILL follows at once.
#[test]
fn a_failed_sigterm_skips_its_wait() {
    let fails = Fails {
        term: true,
        ..Fails::default()
    };
    let (stopped, steps, lock) = try_stop("termfails", "kill", true, fails);
    assert_eq!(stopped.unwrap(), Stopped::Killed);
    assert_eq!(steps, [("stop", secs(0)), ("kill", secs(30))]);
    assert!(!lock, "api.sock.lock stays");
}

/// A failed SIGKILL is an error, and the files of a process that may
/// still run stay.
#[test]
fn a_failed_sigkill_is_an_error() {
    let fails = Fails {
        kill: true,
        ..Fails::default()
    };
    let (stopped, steps, lock) = try_stop("killfails", "kill", true, fails);
    assert!(matches!(stopped, Err(StopError::Kill(_))), "{stopped:?}");
    assert_eq!(steps, [("stop", secs(0)), ("term", secs(30))]);
    assert!(lock, "api.sock.lock is gone");
}

/// A process that outlives SIGKILL gets `KILL_WAIT`, then the stop gives
/// `Hung`, so it ends within the 60 s wait of the client (decisions CH33
/// rule 11, CH34 rule 5).
#[test]
fn a_process_that_outlives_sigkill_is_hung() {
    let (stopped, steps, lock) = try_stop("hung", "never", true, Fails::default());
    assert!(matches!(stopped, Err(StopError::Hung)), "{stopped:?}");
    assert_eq!(
        steps,
        [("stop", secs(0)), ("term", secs(30)), ("kill", secs(35))]
    );
    assert_eq!(KILL_WAIT, secs(5));
    assert!(lock, "api.sock.lock is gone");
}

#[test]
fn a_gone_process_gets_no_step() {
    let (stopped, steps) = stop_fake("gone", "gone", Some(false));
    assert_eq!(stopped, Stopped::Gone);
    assert!(steps.is_empty());
}

/// A shell child: the status checks, SIGTERM and SIGKILL of `std`.
#[derive(Debug)]
struct Shell {
    child: Child,
    status: Option<ExitStatus>,
    terminated: bool,
}

impl VmProcess for Shell {
    fn exited(&mut self) -> io::Result<bool> {
        self.status = self.child.try_wait()?;
        Ok(self.status.is_some())
    }

    fn terminate(&mut self) -> io::Result<()> {
        let pid = self.child.id().to_string();
        let status = Command::new("kill").args(["-TERM", &pid]).status()?;
        if status.success() {
            self.terminated = true;
            Ok(())
        } else {
            Err(io::ErrorKind::Other.into())
        }
    }

    /// Sends SIGKILL, then reaps the child. Paused time runs `KILL_WAIT`
    /// at once, so the reap keeps the test from a race with the exit.
    fn kill(&mut self) -> io::Result<()> {
        self.child.kill()?;
        self.status = Some(self.child.wait()?);
        Ok(())
    }
}

#[test]
fn a_child_that_ignores_sigterm_gets_sigkill() {
    let mut child = Command::new("/bin/sh")
        .args(["-c", "trap '' TERM; echo ready; while :; do sleep 1; done"])
        .stdout(Stdio::piped())
        .spawn()
        .unwrap();
    // The trap is set before the line, so SIGTERM never ends the shell.
    let mut ready = String::new();
    BufReader::new(child.stdout.take().unwrap())
        .read_line(&mut ready)
        .unwrap();
    assert_eq!(ready, "ready\n");

    let runtime = Builder::new_current_thread()
        .enable_time()
        .start_paused(true)
        .build()
        .unwrap();
    let run = TempRun::new("shell");
    let mut shell = Shell {
        child,
        status: None,
        terminated: false,
    };
    let stopped = runtime
        .block_on(stop(None::<&FakeLink>, &mut shell, &run.1))
        .unwrap();
    assert_eq!(stopped, Stopped::Killed);
    assert!(shell.terminated, "SIGTERM came first");
    assert_eq!(shell.status.and_then(|status| status.signal()), Some(9));
    assert!(!run.1.api_socket_lock().exists());
}
