//! The tests that start `creche-probe` as a process.
//!
//! `creche_testkit::probe` is one program on each module of
//! `creche-runtime`. A test of one module cannot show that the modules work
//! together: the start steps, the listeners, the edge layer, the tracked
//! tasks, the signals and the log. Each test here starts the built program
//! and reads only what crossed the process boundary: an exit status, a
//! port, an answer, a file and the two output streams.
//!
//! Each test has a directory of its own and a port of its own. The program
//! gets its whole environment from the test. One variable of the test
//! program itself goes with it: the file of a coverage measurement, in a run
//! that measures. The test sends a signal with the `kill` program. No test
//! waits for a fixed time to know that a step ended.
//!
//! The process-level suite under `integration/proc` judges the same program
//! with the scenarios of the noticeboard (`bin/proc-rust.sh`). Three of
//! those scenarios hold exit status 2 for a refused start, and the program
//! ends with 78. The test
//! `a_lan_bind_with_no_full_key_ends_with_78_and_shows_no_key` holds the
//! status of the program for the three.

#[cfg(test)]
mod tests {
    use std::collections::BTreeMap;
    use std::fs::{self, File};
    use std::io::Write as _;
    use std::net::{Ipv4Addr, TcpListener, TcpStream};
    use std::os::unix::fs::PermissionsExt;
    use std::os::unix::process::ExitStatusExt;
    use std::path::PathBuf;
    use std::process::{Child, Command, ExitStatus, Stdio};
    use std::thread;
    use std::time::{Duration, Instant};

    use creche_contracts::config::noticeboard::{
        ACCESS_KEY_FILE, BIND, NoticeboardConfig, PAGE_SIZE, PORT, STATE_ROOT,
    };
    use creche_contracts::config::{BindAddress, EX_CONFIG, Env};
    use creche_runtime::args::UsageError;
    use creche_runtime::http::client::{Client, PathAndQuery, Request, Target, Timeouts};
    use creche_runtime::http::layers::starlette::StarletteBodies;
    use creche_runtime::http::layers::{EdgeFailure, ErrorBodies};
    use creche_runtime::readfile::ByteCap;
    use creche_testkit::probe::{
        CHECK_FLAG, DRAIN, HEALTH_PATH, HEALTHY, NAME, NO_LISTENER, PANIC_MESSAGE, PANIC_PATH,
        SLOW_FILE, SLOW_PATH,
    };
    use creche_testkit::root::TempRoot;
    use http::header::CONTENT_TYPE;
    use http::{Method, StatusCode};

    /// The built program. cargo builds it before this test program.
    const PROBE: &str = env!("CARGO_BIN_EXE_creche-probe");

    /// The longest time that a test waits for one step of the program. A
    /// host with much load is slow.
    const LIMIT: Duration = Duration::from_secs(60);

    /// How long a test waits between two looks at a file or at a process.
    const LOOK: Duration = Duration::from_millis(10);

    /// How many times a test starts the program again after a bind that
    /// failed. Another process of the host can take the port between the
    /// moment in which the test selects it and the bind of the program.
    const STARTS: usize = 5;

    /// How long the probe of [`sighup_ends_a_plain_program`] waits.
    const PLAIN_LIMIT: Duration = Duration::from_secs(10);

    /// A key of 32 bytes that no deployment uses.
    const KEY: &str = "0123456789abcdef0123456789abcdef";

    /// A key under the least count of bytes.
    const SHORT_KEY: &str = "short-probe-key";

    /// The loopback address, as the text of a bind and as an address.
    const LOOPBACK: &str = "127.0.0.1";

    /// A LAN address that no test binds: the program refuses its start
    /// before the bind.
    const ON_LAN: &str = "192.0.2.10";

    /// The start of the line that `creche_runtime::service::ready` writes for
    /// a listener. The address follows it.
    const READY: &str = " listening on ";

    /// The start of the message of the line that the panic hook of
    /// `creche_runtime::log::init` writes. The place of the panic follows it.
    const PANIC_LINE: &str = " panic at ";

    /// A text that a test sends in a query. No line of the log can hold it.
    const MARKER: &str = "marker-0f5a3c9e";

    /// The names of the files of a test, in its directory.
    const KEY_FILE: &str = "view.key";
    const STATE_DIR: &str = "state";
    const STDOUT: &str = "stdout";
    const STDERR: &str = "stderr";

    /// The number of the signal. POSIX gives it.
    const SIGHUP: i32 = 1;

    /// The variable that names the file of a coverage measurement. The tool
    /// of the `rust-coverage` job sets it for this test program, and the
    /// program under test needs it too: without it, a measured program
    /// writes its file into the directory of the crate.
    const PROFILE_FILE: &str = "LLVM_PROFILE_FILE";

    /// What the SIGHUP test says in a run that cannot judge it.
    const RUN_IGNORES_SIGHUP: &str = "this run of the tests ignores SIGHUP, and each child takes \
        that from it. Start the tests with no `nohup`.";

    /// Where the stdout of the program goes.
    #[derive(Debug, Clone, Copy)]
    enum Stdout {
        /// To a file of the test.
        File,
        /// To a pipe whose reader left before the program started.
        ReaderLeft,
    }

    /// What one test gives the program: a directory, the variables and the
    /// words.
    struct Setup {
        root: TempRoot,
        env: BTreeMap<&'static str, String>,
        words: Vec<&'static str>,
        stdout: Stdout,
    }

    impl Setup {
        /// A directory with a key file and a state root, and the variables
        /// for a loopback bind. The start gives the port.
        fn new() -> Self {
            let root = TempRoot::new().unwrap();
            let key_file = root.path().join(KEY_FILE);
            let state = root.path().join(STATE_DIR);
            fs::write(&key_file, format!("{KEY}\n")).unwrap();
            fs::set_permissions(&key_file, fs::Permissions::from_mode(0o600)).unwrap();
            fs::create_dir(&state).unwrap();

            let env = BTreeMap::from([
                (BIND, LOOPBACK.to_owned()),
                (ACCESS_KEY_FILE, key_file.to_str().unwrap().to_owned()),
                (STATE_ROOT, state.to_str().unwrap().to_owned()),
            ]);

            Self {
                root,
                env,
                words: Vec::new(),
                stdout: Stdout::File,
            }
        }

        fn with(mut self, variable: &'static str, value: &str) -> Self {
            self.env.insert(variable, value.to_owned());

            self
        }

        fn without(mut self, variable: &'static str) -> Self {
            self.env.remove(variable);

            self
        }

        fn word(mut self, word: &'static str) -> Self {
            self.words.push(word);

            self
        }

        fn reader_left(mut self) -> Self {
            self.stdout = Stdout::ReaderLeft;

            self
        }

        /// Writes `content` as the key file.
        fn key(self, content: &str) -> Self {
            fs::write(self.file(KEY_FILE), content).unwrap();

            self
        }

        fn file(&self, name: &str) -> PathBuf {
            self.root.path().join(name)
        }

        /// The variables of the program for the port `port`.
        fn env_on(&self, port: u16) -> BTreeMap<&'static str, String> {
            let mut env = self.env.clone();
            env.insert(PORT, port.to_string());

            env
        }

        /// Starts the program one time, with the port `port` in its
        /// variables.
        fn spawn(&self, port: u16) -> Child {
            let mut command = Command::new(PROBE);
            command
                .args(&self.words)
                .env_clear()
                .envs(self.env_on(port))
                .stdin(Stdio::null())
                .stderr(File::create(self.file(STDERR)).unwrap());
            if let Some(file) = std::env::var_os(PROFILE_FILE) {
                command.env(PROFILE_FILE, file);
            }

            match self.stdout {
                Stdout::File => {
                    command.stdout(File::create(self.file(STDOUT)).unwrap());
                }
                Stdout::ReaderLeft => {
                    let (reader, writer) = std::io::pipe().unwrap();
                    drop(reader);
                    command.stdout(writer);
                }
            }

            command.spawn().unwrap()
        }

        /// Starts the program with a port that no listener has.
        fn start(self) -> Started {
            let port = free_port();

            self.start_on(port)
        }

        /// Starts the program with the port `port`.
        fn start_on(self, port: u16) -> Started {
            let child = Running(self.spawn(port));

            Started {
                setup: self,
                port,
                child,
            }
        }

        /// Starts the program and waits for the line that says that its
        /// listener is ready.
        fn serve(self) -> Started {
            let mut setup = self;

            for _ in 0..STARTS {
                let mut started = setup.start();

                match started.wait_ready() {
                    Start::Ready => return started,
                    // The drop of the process is the end of this start.
                    Start::Ended(status) if status.code() == Some(i32::from(NO_LISTENER)) => {
                        setup = started.setup;
                    }
                    Start::Ended(status) => panic!(
                        "the program ended with {status} before its listener was ready. {}",
                        started.output()
                    ),
                }
            }

            panic!("the program got no port in {STARTS} starts");
        }
    }

    /// One process of the program. The drop ends a process that still runs.
    struct Running(Child);

    impl Drop for Running {
        fn drop(&mut self) {
            // A process that ended gives an error here, and a drop has no
            // caller to give an error to.
            let _ = self.0.kill();
            let _ = self.0.wait();
        }
    }

    /// How the start of a program that must serve went.
    enum Start {
        /// The program wrote the readiness line of its listener.
        Ready,
        /// The program ended before that line.
        Ended(ExitStatus),
    }

    /// One process of the program, and what its test gave it.
    struct Started {
        setup: Setup,
        port: u16,
        child: Running,
    }

    impl Started {
        fn stdout(&self) -> String {
            fs::read_to_string(self.setup.file(STDOUT)).unwrap_or_default()
        }

        fn stderr(&self) -> String {
            fs::read_to_string(self.setup.file(STDERR)).unwrap_or_default()
        }

        /// What the program wrote, for the message of a test that fails.
        fn output(&self) -> String {
            format!(
                "the output of the program:\n{}\n{}",
                self.stdout(),
                self.stderr()
            )
        }

        /// Waits for the readiness line of the listener, or for the end of a
        /// program that wrote no such line.
        fn wait_ready(&mut self) -> Start {
            let line = format!("{READY}{LOOPBACK}:{}", self.port);
            let deadline = Instant::now() + LIMIT;

            loop {
                if self.stderr().contains(&line) {
                    return Start::Ready;
                }

                if let Some(status) = self.child.0.try_wait().unwrap() {
                    return Start::Ended(status);
                }

                assert!(
                    Instant::now() < deadline,
                    "the program wrote no readiness line. {}",
                    self.output()
                );

                thread::sleep(LOOK);
            }
        }

        /// The exit status of the program, or `None` when it still runs
        /// after `limit`.
        fn ended_inside(&mut self, limit: Duration) -> Option<ExitStatus> {
            ended_inside(&mut self.child.0, limit)
        }

        /// Waits until the program ended, and gives its exit status.
        fn wait(&mut self) -> ExitStatus {
            match self.ended_inside(LIMIT) {
                Some(status) => status,
                None => panic!("the program did not end. {}", self.output()),
            }
        }

        /// Sends the signal `name` to the program with the `kill` program.
        fn send(&self, name: &str) {
            send(self.child.0.id(), name);
        }

        /// Whether a listener takes a connection on the port of the test.
        fn port_is_open(&self) -> bool {
            TcpStream::connect((Ipv4Addr::LOCALHOST, self.port)).is_ok()
        }

        /// Sends one `GET` request with the client of `creche-runtime` and
        /// gives the status, the content type and the body of the answer.
        fn get(&self, path: &str, query: &[(&str, &str)]) -> Answer {
            let address: BindAddress = format!("{LOOPBACK}:{}", self.port).parse().unwrap();
            let client = Client::new(Target::from(&address));
            let segments: Vec<&str> = path.split('/').filter(|part| !part.is_empty()).collect();
            let target = PathAndQuery::from_segments(&segments).with_query(query);
            let request = Request::new(Method::GET, target, Timeouts::each(LIMIT));

            let reply = runtime()
                .block_on(client.send(request, ByteCap::ONE_MIB, LIMIT))
                .unwrap_or_else(|error| panic!("{path}: {error}. {}", self.output()));

            Answer {
                status: reply.status(),
                content_type: reply
                    .headers()
                    .get(CONTENT_TYPE)
                    .map(|value| value.to_str().unwrap().to_owned()),
                body: reply.into_body(),
            }
        }

        /// Ends the program with SIGTERM and holds that it ends with
        /// status 0.
        fn stop(mut self) {
            self.send("TERM");
            let status = self.wait();

            assert_eq!(status.code(), Some(0), "{status}. {}", self.output());
        }
    }

    /// The parts of an answer that a test compares.
    #[derive(Debug, PartialEq, Eq)]
    struct Answer {
        status: StatusCode,
        content_type: Option<String>,
        body: Vec<u8>,
    }

    fn runtime() -> tokio::runtime::Runtime {
        tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .unwrap()
    }

    /// The answer of the Python web framework for `failure`, as the edge
    /// layer gives it.
    fn framework_answer(failure: EdgeFailure) -> Answer {
        let answer = StarletteBodies.answer(failure);
        let status = answer.status();
        let content_type = answer
            .headers()
            .get(CONTENT_TYPE)
            .map(|value| value.to_str().unwrap().to_owned());
        let body = runtime()
            .block_on(axum::body::to_bytes(
                answer.into_body(),
                ByteCap::ONE_MIB.get(),
            ))
            .unwrap()
            .to_vec();

        Answer {
            status,
            content_type,
            body,
        }
    }

    /// A port of the loopback address that no listener has at this moment.
    fn free_port() -> u16 {
        TcpListener::bind((Ipv4Addr::LOCALHOST, 0))
            .unwrap()
            .local_addr()
            .unwrap()
            .port()
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

        let ended = ended_inside(&mut plain, PLAIN_LIMIT);
        let _ = plain.kill();
        let _ = plain.wait();

        ended.is_some_and(|status| status.signal() == Some(SIGHUP))
    }

    /// Each line of a text, with no empty line.
    fn lines_of(text: &str) -> Vec<&str> {
        text.lines().filter(|line| !line.is_empty()).collect()
    }

    /// The program refused its start: status 78, nothing on stdout and no
    /// listener on the port.
    fn refused(started: &mut Started) {
        let status = started.wait();

        assert_eq!(
            status.code(),
            Some(i32::from(EX_CONFIG)),
            "{status}. {}",
            started.output()
        );
        assert_eq!(started.stdout(), "");
        assert!(!started.port_is_open());
    }

    #[test]
    fn a_config_that_is_not_valid_ends_with_78_and_one_line_for_each_error() {
        let setup = Setup::new()
            .with(BIND, "0.0.0.0")
            .with(PAGE_SIZE, "0")
            .with(STATE_ROOT, "state");
        let port = free_port();
        let errors = NoticeboardConfig::from_env(&Env::from_pairs(setup.env_on(port))).unwrap_err();
        let expected: Vec<String> = errors
            .as_slice()
            .iter()
            .map(|error| format!("{NAME}: {error}"))
            .collect();

        let mut started = setup.start_on(port);
        refused(&mut started);

        assert_eq!(expected.len(), 3);
        assert_eq!(lines_of(&started.stderr()), expected);
        for (line, error) in expected.iter().zip(errors.as_slice()) {
            assert!(line.contains(error.variable()), "{line}");
        }
    }

    #[test]
    fn a_key_file_that_is_absent_ends_with_78() {
        for words in [vec![], vec![CHECK_FLAG]] {
            let mut setup = Setup::new();
            fs::remove_file(setup.file(KEY_FILE)).unwrap();
            setup.words = words;

            let mut started = setup.start();
            refused(&mut started);

            let stderr = started.stderr();
            let lines = lines_of(&stderr);
            assert_eq!(lines.len(), 1, "{stderr}");
            assert!(
                lines[0].starts_with(&format!("{NAME}: {ACCESS_KEY_FILE}: ")),
                "{stderr}"
            );
            // A line names the variable and never its value.
            assert!(!stderr.contains(KEY_FILE), "{stderr}");
        }
    }

    #[test]
    fn a_wrong_word_of_the_command_line_ends_with_78() {
        for word in ["--chec", "--check=yes", "serve"] {
            let expected = UsageError::Unexpected {
                word: word.to_owned(),
            };

            let mut started = Setup::new().word(word).start();
            refused(&mut started);

            assert_eq!(lines_of(&started.stderr()), [format!("{NAME}: {expected}")]);
        }
    }

    /// The three cases of the scenario
    /// `test_a_lan_bind_with_no_full_key_refuses_to_start` of
    /// `integration/proc/test_proc_board_start.py`: no key file in the
    /// variables, an empty key file and a short key.
    #[test]
    fn a_lan_bind_with_no_full_key_ends_with_78_and_shows_no_key() {
        let setups = [
            Setup::new().without(ACCESS_KEY_FILE),
            Setup::new().key(""),
            Setup::new().key(SHORT_KEY),
        ];

        for setup in setups {
            let mut started = setup.with(BIND, ON_LAN).start();
            refused(&mut started);

            let stderr = started.stderr();
            let lines = lines_of(&stderr);
            assert_eq!(lines.len(), 1, "{stderr}");
            assert!(
                lines[0].starts_with(&format!("{NAME}: {BIND}: ")),
                "{stderr}"
            );
            assert!(!stderr.contains(SHORT_KEY), "{stderr}");
        }
    }

    #[test]
    fn the_check_ends_with_0_binds_nothing_and_shows_no_key() {
        let mut started = Setup::new().word(CHECK_FLAG).start();
        let status = started.wait();

        assert_eq!(status.code(), Some(0), "{status}. {}", started.output());
        assert_eq!(
            lines_of(&started.stdout()),
            [
                format!("bind          {LOOPBACK}:{}", started.port),
                String::from("loopback      yes"),
                String::from("access key    set"),
            ]
        );
        assert_eq!(started.stderr(), "");
        assert!(!started.output().contains(KEY));
        assert!(!started.port_is_open());
    }

    #[test]
    fn the_check_of_an_empty_key_file_on_loopback_says_that_the_key_is_empty() {
        let mut started = Setup::new().key("").word(CHECK_FLAG).start();
        let status = started.wait();

        assert_eq!(status.code(), Some(0), "{status}. {}", started.output());
        assert_eq!(lines_of(&started.stdout())[2], "access key    empty");
    }

    #[test]
    fn the_check_ends_with_0_and_no_panic_line_when_the_reader_of_its_stdout_left() {
        let mut started = Setup::new().word(CHECK_FLAG).reader_left().start();
        let status = started.wait();

        // The program ended by itself, and no signal ended it.
        assert_eq!(status.code(), Some(0), "{status}. {}", started.output());
        assert_eq!(started.stderr(), "");
    }

    #[test]
    fn the_probe_answers_on_its_port_after_the_readiness_line() {
        let started = Setup::new().serve();

        let answer = started.get(HEALTH_PATH, &[]);

        assert_eq!(
            answer,
            Answer {
                status: StatusCode::OK,
                content_type: Some(String::from("application/json")),
                body: HEALTHY.as_bytes().to_vec(),
            }
        );
        // The readiness line is the one line of a program that serves.
        assert_eq!(lines_of(&started.stderr()).len(), 1, "{}", started.output());

        started.stop();
    }

    #[test]
    fn a_request_that_no_route_takes_gets_the_answer_of_the_framework() {
        let started = Setup::new().serve();

        assert_eq!(
            started.get("/probe", &[]),
            framework_answer(EdgeFailure::NoRoute)
        );

        started.stop();
    }

    #[test]
    fn a_panic_of_a_handler_gets_the_answer_of_the_edge_and_leaves_no_message_in_the_log() {
        let started = Setup::new().serve();

        let answer = started.get(PANIC_PATH, &[("text", MARKER)]);
        let next = started.get(HEALTH_PATH, &[]);

        assert_eq!(answer, framework_answer(EdgeFailure::Panic));
        assert_eq!(next.status, StatusCode::OK);

        let stderr = started.stderr();
        // The hook wrote the place of the panic, with the name of the
        // program as the target of the line.
        assert!(
            stderr.contains(&format!(" ERROR {NAME}{PANIC_LINE}")),
            "{stderr}"
        );
        assert!(!stderr.contains(MARKER), "{stderr}");
        assert!(!stderr.contains(PANIC_MESSAGE), "{stderr}");
        assert!(!started.stdout().contains(MARKER));

        started.stop();
    }

    #[test]
    fn a_handler_runs_to_its_end_after_its_client_left() {
        let started = Setup::new().serve();
        let file = started.setup.file(STATE_DIR).join(SLOW_FILE);
        let request = format!("GET {SLOW_PATH} HTTP/1.1\r\nHost: {LOOPBACK}\r\n\r\n");

        // The client sends the whole request and leaves. The handler waits
        // before its write, so the client is gone at the write.
        let mut client = TcpStream::connect((Ipv4Addr::LOCALHOST, started.port)).unwrap();
        client.write_all(request.as_bytes()).unwrap();
        drop(client);

        let deadline = Instant::now() + LIMIT;
        while !file.exists() {
            assert!(
                Instant::now() < deadline,
                "the handler wrote no file. {}",
                started.output()
            );
            thread::sleep(LOOK);
        }

        // The program still serves after the client that left.
        assert_eq!(started.get(HEALTH_PATH, &[]).status, StatusCode::OK);

        started.stop();
    }

    #[test]
    fn the_slow_route_answers_a_client_that_stays() {
        let started = Setup::new().serve();
        let file = started.setup.file(STATE_DIR).join(SLOW_FILE);

        let answer = started.get(SLOW_PATH, &[]);

        assert_eq!(answer.status, StatusCode::OK);
        assert!(file.exists());

        started.stop();
    }

    #[test]
    fn the_slow_route_makes_no_state_root() {
        let setup = Setup::new();
        let absent = setup.file("no-state-root");
        let started = setup.with(STATE_ROOT, absent.to_str().unwrap()).serve();

        let answer = started.get(SLOW_PATH, &[]);

        assert_eq!(answer.status, StatusCode::INTERNAL_SERVER_ERROR);
        assert_eq!(answer.body, b"");
        assert!(!absent.exists());
        assert!(
            started.stderr().contains(&format!(" ERROR {NAME} ")),
            "{}",
            started.output()
        );

        started.stop();
    }

    #[test]
    fn a_stop_signal_ends_the_probe_with_status_0_inside_the_drain_limit() {
        for name in ["TERM", "INT"] {
            let mut started = Setup::new().serve();

            started.send(name);
            let Some(status) = started.ended_inside(DRAIN) else {
                panic!("the program still runs. {}", started.output());
            };

            assert_eq!(status.code(), Some(0), "{name}: {status}");
            assert_eq!(status.signal(), None, "{name}");
            // The process ended, and the test took its status. No process
            // of the program is left, and nothing listens on its port.
            assert!(!started.port_is_open(), "{name}");
        }
    }

    #[test]
    fn sighup_ends_the_probe_by_the_default_action_of_the_signal() {
        assert!(sighup_ends_a_plain_program(), "{RUN_IGNORES_SIGHUP}");

        let mut started = Setup::new().serve();

        started.send("HUP");
        let status = started.wait();

        // The signal ended the program. No code of the program ran after it.
        assert_eq!(status.signal(), Some(SIGHUP), "{}", started.output());
        assert!(!started.port_is_open());
    }

    #[test]
    fn a_port_that_another_listener_has_ends_the_probe_with_the_status_of_no_listener() {
        let holder = TcpListener::bind((Ipv4Addr::LOCALHOST, 0)).unwrap();
        let port = holder.local_addr().unwrap().port();

        let mut started = Setup::new().start_on(port);
        let status = started.wait();

        assert_eq!(
            status.code(),
            Some(i32::from(NO_LISTENER)),
            "{status}. {}",
            started.output()
        );
        let stderr = started.stderr();
        assert!(stderr.contains(&format!(" ERROR {NAME} ")), "{stderr}");
        assert!(!stderr.contains(READY), "{stderr}");
        // A bind that failed is no refused start.
        assert_ne!(status.code(), Some(i32::from(EX_CONFIG)));
    }
}
