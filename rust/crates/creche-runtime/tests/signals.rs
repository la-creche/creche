//! The tests of `creche_runtime::signals` that send a real signal.
//!
//! A signal goes to a whole process. A test that sends one to its own
//! process disturbs each other test of that process. So each test here starts
//! this test program again as a child, with the one test
//! `the_child_runs_one_scenario` and a variable that names a scenario. The
//! test sends each signal to that child with the `kill` program.
//!
//! The test and its child share one directory. The child makes a file there
//! after each step, and the test waits for the file. No test waits for a
//! fixed time to know that a step ended.

#[cfg(test)]
mod tests {
    use std::fs::{self, File};
    use std::os::unix::process::ExitStatusExt;
    use std::path::{Path, PathBuf};
    use std::process::{Child, Command, ExitStatus, Stdio};
    use std::thread;
    use std::time::{Duration, Instant};

    use creche_runtime::signals::{OnHangup, install};
    use creche_runtime::tasks::shutdown_pair;
    use creche_testkit::root::TempRoot;
    use tokio::runtime::{Builder, Runtime};
    use tokio::signal::unix::{SignalKind, signal};

    /// The variable that selects the scenario of
    /// [`the_child_runs_one_scenario`].
    const CHILD_VARIABLE: &str = "CRECHE_RUNTIME_SIGNALS_TEST_CHILD";

    /// The variable that holds the directory of the test and its child.
    const DIR_VARIABLE: &str = "CRECHE_RUNTIME_SIGNALS_TEST_DIR";

    /// The name of the child test, as the test program takes it.
    const CHILD_TEST: &str = "tests::the_child_runs_one_scenario";

    /// The longest time that a test waits for one step of its child. A host
    /// with much load is slow.
    const LIMIT: Duration = Duration::from_secs(60);

    /// The longest time that a child runs. A child that lost its test ends
    /// by itself after this time.
    const CHILD_LIMIT: Duration = Duration::from_secs(120);

    /// How long a test waits between two looks at a file or at a child.
    const LOOK: Duration = Duration::from_millis(10);

    /// How long a test waits for a reload that must not start. A longer time
    /// finds more defects, and no time makes a correct child fail.
    const SETTLE: Duration = Duration::from_millis(300);

    /// How many times a child calls `Hangups::next` after the stop signal. A
    /// call that selects its branch by chance gives a wrong item in one of
    /// two calls.
    const LATE_CALLS: usize = 50;

    /// How long the probe of [`sighup_ends_a_plain_program`] waits.
    const PROBE_LIMIT: Duration = Duration::from_secs(10);

    /// The numbers of the three signals. POSIX gives each one its number.
    const SIGHUP: i32 = 1;
    const SIGINT: i32 = 2;
    const SIGTERM: i32 = 15;

    /// The files that a child makes: the handlers are in place, the stop
    /// signal arrived, and the scenario ended. `DONE` holds the count of the
    /// reloads.
    const READY: &str = "ready";
    const STOPPED: &str = "stopped";
    const DONE: &str = "done";

    /// The file that a test makes to end the first reload of its child, and
    /// the file that the child makes when that reload ended.
    const RELEASE: &str = "release";
    const RELEASED: &str = "released";

    /// The files of the output of a child.
    const STDOUT: &str = "stdout";
    const STDERR: &str = "stderr";

    /// What the SIGHUP test says in a run that cannot judge it.
    const RUN_IGNORES_SIGHUP: &str = "this run of the tests ignores SIGHUP, and each child takes \
        that from it. Start the tests with no `nohup`.";

    /// Each scenario of the child.
    #[derive(Debug, Clone, Copy)]
    enum Scenario {
        /// No handler for SIGHUP. The child waits for the stop signal, or
        /// until a signal ends it.
        StopDefault,
        /// A handler for SIGHUP. The child waits in `Hangups::next` for the
        /// stop signal.
        StopReload,
        /// The child takes three stop signals and then ends by itself.
        StopAgain,
        /// The child counts its reloads. The first reload runs until the
        /// test ends it.
        Reload,
        /// The child takes the stop signal and then a SIGHUP. It calls
        /// `Hangups::next` only after the two.
        LateHangup,
    }

    impl Scenario {
        const ALL: [Self; 5] = [
            Self::StopDefault,
            Self::StopReload,
            Self::StopAgain,
            Self::Reload,
            Self::LateHangup,
        ];

        /// The value of [`CHILD_VARIABLE`].
        fn name(self) -> &'static str {
            match self {
                Self::StopDefault => "stop-default",
                Self::StopReload => "stop-reload",
                Self::StopAgain => "stop-again",
                Self::Reload => "reload",
                Self::LateHangup => "late-hangup",
            }
        }

        /// The runtime of the child. One scenario runs on worker threads, as
        /// a daemon with `Threads::Workers` does.
        fn runtime(self) -> Runtime {
            match self {
                Self::StopReload => Builder::new_multi_thread()
                    .worker_threads(2)
                    .enable_all()
                    .build()
                    .unwrap(),
                _ => Builder::new_current_thread().enable_all().build().unwrap(),
            }
        }

        async fn run(self, dir: &Path) {
            match self {
                Self::StopDefault => stop_default(dir).await,
                Self::StopReload => stop_reload(dir).await,
                Self::StopAgain => stop_again(dir).await,
                Self::Reload => reload(dir).await,
                Self::LateHangup => late_hangup(dir).await,
            }
        }
    }

    /// Makes the empty file `name` in `dir`: the step of that name ended.
    fn mark(dir: &Path, name: &str) {
        File::create(dir.join(name)).unwrap();
    }

    /// Waits in the child for a file that the test makes.
    async fn until_exists(file: &Path) {
        while !file.exists() {
            tokio::time::sleep(LOOK).await;
        }
    }

    /// Counts each signal of `kind` that the runtime of the child takes. The
    /// task makes the file `<name>-<count>` for each one.
    ///
    /// This listener is apart from the listeners of `install`. The runtime
    /// gives a signal to each listener at the same time. A test thus knows
    /// from the file that the signal reached each listener of the child.
    fn count_signals(kind: SignalKind, dir: &Path, name: &'static str) {
        let mut listener = signal(kind).unwrap();
        let dir = dir.to_owned();

        tokio::spawn(async move {
            let mut count = 0_usize;

            while listener.recv().await.is_some() {
                count += 1;
                mark(&dir, &format!("{name}-{count}"));
            }
        });
    }

    async fn stop_default(dir: &Path) {
        let (trigger, shutdown) = shutdown_pair();
        let hangups = install(trigger, OnHangup::DefaultAction).unwrap();

        assert!(hangups.is_none());
        assert!(!shutdown.is_cancelled());

        mark(dir, READY);
        shutdown.cancelled().await;
        mark(dir, STOPPED);
    }

    async fn stop_reload(dir: &Path) {
        let (trigger, shutdown) = shutdown_pair();
        let mut hangups = install(trigger, OnHangup::Reload).unwrap().unwrap();

        mark(dir, READY);

        // The wait runs in a task of its own, as the reload loop of a
        // service does. A value that is not `Send` does not build here.
        let waited = tokio::spawn(async move {
            let first = hangups.next().await;
            let second = hangups.next().await;

            (first, second)
        });

        assert_eq!(waited.await.unwrap(), (None, None));
        assert!(shutdown.is_cancelled());

        mark(dir, STOPPED);
    }

    async fn stop_again(dir: &Path) {
        let (trigger, shutdown) = shutdown_pair();
        install(trigger, OnHangup::DefaultAction).unwrap();
        count_signals(SignalKind::terminate(), dir, "term");
        count_signals(SignalKind::interrupt(), dir, "int");

        mark(dir, READY);
        shutdown.cancelled().await;
        mark(dir, STOPPED);

        // The test sends one more SIGTERM and then SIGINT. The child must
        // still run after each one.
        until_exists(&dir.join("term-2")).await;
        until_exists(&dir.join("int-1")).await;

        assert!(shutdown.is_cancelled());

        mark(dir, DONE);
    }

    async fn reload(dir: &Path) {
        let (trigger, shutdown) = shutdown_pair();
        let mut hangups = install(trigger, OnHangup::Reload).unwrap().unwrap();
        count_signals(SignalKind::hangup(), dir, "hup");

        // No signal arrived yet. Each turn drops a wait that did not end.
        for _ in 0..3 {
            let early = tokio::select! {
                biased;
                item = hangups.next() => Some(item),
                () = tokio::task::yield_now() => None,
            };

            assert_eq!(early, None);
        }

        mark(dir, READY);

        let mut reloads = 0_usize;

        loop {
            // A service waits for SIGHUP beside its other work. Each turn of
            // the other work drops the wait, and no signal is lost.
            let item = tokio::select! {
                item = hangups.next() => item,
                () = tokio::time::sleep(LOOK) => continue,
            };

            if item.is_none() {
                break;
            }

            reloads += 1;
            mark(dir, &format!("reload-{reloads}"));

            if reloads == 1 {
                // The first reload runs until the test ends it. The child
                // does not call `next` in that time.
                until_exists(&dir.join(RELEASE)).await;
                mark(dir, RELEASED);
            }
        }

        assert!(shutdown.is_cancelled());

        fs::write(dir.join(DONE), reloads.to_string()).unwrap();
    }

    async fn late_hangup(dir: &Path) {
        let (trigger, shutdown) = shutdown_pair();
        let mut hangups = install(trigger, OnHangup::Reload).unwrap().unwrap();
        count_signals(SignalKind::hangup(), dir, "hup");

        mark(dir, READY);
        shutdown.cancelled().await;
        mark(dir, STOPPED);

        // The test sends SIGHUP now and makes the file when the runtime took
        // the signal. The signal then waits in the listener of `hangups`.
        until_exists(&dir.join(RELEASE)).await;

        for _ in 0..LATE_CALLS {
            assert_eq!(hangups.next().await, None);
        }

        mark(dir, DONE);
    }

    /// The child of each test of this file. Without the variable it does
    /// nothing. With the variable it runs the one scenario that the variable
    /// names.
    #[test]
    fn the_child_runs_one_scenario() {
        let Some(name) = std::env::var_os(CHILD_VARIABLE) else {
            return;
        };
        let dir = PathBuf::from(std::env::var_os(DIR_VARIABLE).unwrap());
        let scenario = Scenario::ALL
            .into_iter()
            .find(|scenario| name == scenario.name())
            .unwrap();

        scenario.runtime().block_on(async {
            tokio::time::timeout(CHILD_LIMIT, scenario.run(&dir))
                .await
                .unwrap();
        });
    }

    /// One child that runs a scenario, and the directory that the test and
    /// the child share. The drop ends a child that still runs.
    struct Running {
        root: TempRoot,
        child: Child,
    }

    impl Running {
        /// Starts the child and waits until its handlers are in place.
        fn start(scenario: Scenario) -> Self {
            let root = TempRoot::new().unwrap();
            let child = Command::new(std::env::current_exe().unwrap())
                .args([CHILD_TEST, "--exact", "--nocapture", "--test-threads=1"])
                .env(CHILD_VARIABLE, scenario.name())
                .env(DIR_VARIABLE, root.path())
                .stdin(Stdio::null())
                .stdout(File::create(root.path().join(STDOUT)).unwrap())
                .stderr(File::create(root.path().join(STDERR)).unwrap())
                .spawn()
                .unwrap();
            let mut running = Self { root, child };

            running.wait_for(READY);

            running
        }

        fn file(&self, name: &str) -> PathBuf {
            self.root.path().join(name)
        }

        /// What the child wrote, for the message of a test that fails.
        fn output(&self) -> String {
            let stdout = fs::read_to_string(self.file(STDOUT)).unwrap_or_default();
            let stderr = fs::read_to_string(self.file(STDERR)).unwrap_or_default();

            format!("the output of the child:\n{stdout}\n{stderr}")
        }

        /// Waits until the child made the file `name`.
        fn wait_for(&mut self, name: &str) {
            let file = self.file(name);
            let deadline = Instant::now() + LIMIT;

            while !file.exists() {
                if let Some(status) = self.child.try_wait().unwrap() {
                    // The child can make the file and end between the two
                    // looks.
                    assert!(
                        file.exists(),
                        "the child ended with {status} before the step {name}. {}",
                        self.output()
                    );

                    return;
                }

                assert!(
                    Instant::now() < deadline,
                    "the child did not come to the step {name}. {}",
                    self.output()
                );

                thread::sleep(LOOK);
            }
        }

        /// Sends the signal `name` to the child with the `kill` program.
        fn send(&self, name: &str) {
            send(self.child.id(), name);
        }

        /// Waits until the child ended, and gives its exit status.
        fn wait(&mut self) -> ExitStatus {
            match ended_inside(&mut self.child, LIMIT) {
                Some(status) => status,
                None => panic!("the child did not end. {}", self.output()),
            }
        }
    }

    impl Drop for Running {
        fn drop(&mut self) {
            // A child that ended gives an error here, and a drop has no
            // caller to give an error to.
            let _ = self.child.kill();
            let _ = self.child.wait();
        }
    }

    /// Sends the signal `name` to the process `pid` with the `kill` program.
    fn send(pid: u32, name: &str) {
        let status = Command::new("kill")
            .args(["-s", name, &pid.to_string()])
            .status()
            .unwrap();

        assert!(status.success(), "kill -s {name} {pid}: {status}");
    }

    /// The exit status of `child`, or `None` when it still runs after
    /// `limit`.
    fn ended_inside(child: &mut Child, limit: Duration) -> Option<ExitStatus> {
        let deadline = Instant::now() + limit;

        loop {
            if let Some(status) = child.try_wait().unwrap() {
                return Some(status);
            }

            if Instant::now() > deadline {
                return None;
            }

            thread::sleep(LOOK);
        }
    }

    /// Whether SIGHUP ends a program that installs no handler, when this
    /// test program starts it.
    ///
    /// A child ignores each signal that the process which started it
    /// ignores, until the child installs a handler. A run of the tests under
    /// `nohup` ignores SIGHUP, so each child of that run ignores it too.
    fn sighup_ends_a_plain_program() -> bool {
        let mut plain = Command::new("sleep")
            .arg("600")
            .stdin(Stdio::null())
            .spawn()
            .unwrap();

        send(plain.id(), "HUP");

        let ended = ended_inside(&mut plain, PROBE_LIMIT);
        let _ = plain.kill();
        let _ = plain.wait();

        ended.is_some_and(|status| status.signal() == Some(SIGHUP))
    }

    /// The child ends with status 0 after the signal `name`, and it saw the
    /// stop signal first.
    fn stops_at(scenario: Scenario, name: &str) {
        let mut running = Running::start(scenario);

        assert!(!running.file(STOPPED).exists());

        running.send(name);
        let status = running.wait();

        assert!(status.success(), "{status}. {}", running.output());
        assert!(running.file(STOPPED).exists(), "{}", running.output());
    }

    #[test]
    fn sigterm_triggers_the_stop_signal() {
        stops_at(Scenario::StopDefault, "TERM");
    }

    #[test]
    fn sigint_triggers_the_stop_signal() {
        stops_at(Scenario::StopDefault, "INT");
    }

    #[test]
    fn sigterm_ends_the_hangups_of_a_program_with_a_reload() {
        stops_at(Scenario::StopReload, "TERM");
    }

    #[test]
    fn sigint_ends_the_hangups_of_a_program_with_a_reload() {
        stops_at(Scenario::StopReload, "INT");
    }

    #[test]
    fn a_second_stop_signal_has_no_other_effect() {
        let mut running = Running::start(Scenario::StopAgain);

        running.send("TERM");
        running.wait_for(STOPPED);
        running.wait_for("term-1");

        // The runtime of the child took each signal when its file is there.
        // The default action of the signal did not end the child.
        running.send("TERM");
        running.wait_for("term-2");
        running.send("INT");
        running.wait_for("int-1");

        let status = running.wait();

        assert!(status.success(), "{status}. {}", running.output());
        assert_eq!(status.signal(), None);
        assert_ne!(status.code(), Some(128 + SIGTERM));
        assert_ne!(status.code(), Some(128 + SIGINT));
        assert!(running.file(DONE).exists(), "{}", running.output());
    }

    #[test]
    fn sighup_ends_a_program_with_the_default_action() {
        assert!(sighup_ends_a_plain_program(), "{RUN_IGNORES_SIGHUP}");

        let mut running = Running::start(Scenario::StopDefault);

        running.send("HUP");
        let status = running.wait();

        // The signal ended the child. No code of the child ran after it.
        assert_eq!(status.signal(), Some(SIGHUP), "{}", running.output());
        assert!(!running.file(STOPPED).exists(), "{}", running.output());
    }

    #[test]
    fn two_sighup_during_one_reload_give_one_more_reload() {
        let mut running = Running::start(Scenario::Reload);

        running.send("HUP");
        running.wait_for("hup-1");
        running.wait_for("reload-1");

        // The first reload runs now. The child took each of the two signals
        // when its file is there.
        running.send("HUP");
        running.wait_for("hup-2");
        running.send("HUP");
        running.wait_for("hup-3");

        assert!(!running.file("reload-2").exists(), "{}", running.output());

        // The first reload ends. The two signals give one more reload.
        mark(running.root.path(), RELEASE);
        running.wait_for("reload-2");
        thread::sleep(SETTLE);

        running.send("TERM");
        let status = running.wait();

        assert!(status.success(), "{status}. {}", running.output());
        assert_eq!(fs::read_to_string(running.file(DONE)).unwrap(), "2");
        assert!(!running.file("reload-3").exists());
    }

    #[test]
    fn a_sighup_after_the_stop_signal_gives_no_reload() {
        let mut running = Running::start(Scenario::LateHangup);

        running.send("TERM");
        running.wait_for(STOPPED);

        // The stop signal is triggered. The child took the SIGHUP when its
        // file is there, and the child did not call `next` yet.
        running.send("HUP");
        running.wait_for("hup-1");
        mark(running.root.path(), RELEASE);

        let status = running.wait();

        assert!(status.success(), "{status}. {}", running.output());
        assert!(running.file(DONE).exists(), "{}", running.output());
    }

    #[test]
    fn a_sighup_after_a_reload_gives_one_reload() {
        let mut running = Running::start(Scenario::Reload);

        running.send("HUP");
        running.wait_for("hup-1");
        running.wait_for("reload-1");
        mark(running.root.path(), RELEASE);
        running.wait_for(RELEASED);

        // No reload runs now. Each signal gives one reload.
        running.send("HUP");
        running.wait_for("hup-2");
        running.wait_for("reload-2");
        running.send("HUP");
        running.wait_for("hup-3");
        running.wait_for("reload-3");

        running.send("INT");
        let status = running.wait();

        assert!(status.success(), "{status}. {}", running.output());
        assert_eq!(fs::read_to_string(running.file(DONE)).unwrap(), "3");
    }
}
