//! The program `creche-probe`: one program on each module of the runtime.
//!
//! The process-level suite under `integration/proc` judges a service as a
//! process. This program is the first Rust program that the suite judges. It
//! proves that the modules of `creche-runtime` work together in one program:
//! the start steps, the config, a key file, the listeners, the edge layer,
//! the tracked tasks, the signals and the log.
//!
//! The program is no service. No release holds it and no unit starts it. It
//! starts as the noticeboard starts, because the suite gives it the variables
//! of the noticeboard: its config type is
//! [`NoticeboardConfig`]. Only `creche-contracts` can read an `Env`, so the
//! program has no config type of its own.
//!
//! # The command line
//!
//! | Command | What the program does |
//! |---|---|
//! | `creche-probe` | It binds the address of the config and serves the three routes until a stop signal. |
//! | `creche-probe --check` | It reads the config and the key file, writes a report to stdout and ends with status 0. It binds nothing. |
//!
//! # The routes
//!
//! | Route | The answer |
//! |---|---|
//! | `GET /healthz` | Status 200 and the body `{"ok":true}`. The route asks for no key. |
//! | `GET /probe/panic` | The handler panics. The edge layer answers for it. |
//! | `GET /probe/slow` | The handler waits, writes one file below the state root and answers status 200. |
//!
//! Each other request gets the answer of the Python web framework, from
//! [`StarletteBodies`].
//!
//! # The exit status
//!
//! | Status | Cause |
//! |---|---|
//! | 0 | A stop signal ended the program, or `--check` passed. |
//! | 78 | The program refused its start: a config that is not valid, a key file that it cannot use, or a wrong word of the command line. |
//! | 3 | The listener did not bind. The Python noticeboard ends with the same status. |
//! | 1 | The listener stopped with an error, or a panic ended the program. |
//!
//! SIGHUP ends the program with the default action of the signal: the program
//! has no reload.
//!
//! # The Python origin
//!
//! The program has no Python origin. Its start has the steps of
//! `noticeboard/src/noticeboard/__main__.py:32-61`, and it differs from that
//! origin in these ways. A plain test holds each one, in this file or in
//! `tests/process.rs`.
//!
//! - The Python service ends a refused start with status 2. The program ends
//!   it with status 78 (`rust/AGENTS.md`, "The rules for a service", rule 17).
//! - The Python service writes the errors of its config as one line, and that
//!   line can hold the path of the key file. The program writes one line for
//!   each error. A line names the variable and never its value.
//! - The Python service reads a key file of each size. The program refuses a
//!   key file of more than 1 MiB.
//! - `--check` of the Python service ends with an error text and with status
//!   1 or 120 when the reader of its stdout left. The program drops the
//!   report and ends with status 0.
//! - The report of `--check` holds the first three lines of the eleven lines
//!   of the Python report. Its third line says only `set` or `empty`.
//! - A stop signal ends the Python service by the signal itself. The program
//!   ends with status 0.

use std::error::Error;
use std::ffi::OsString;
use std::fmt;
use std::num::NonZeroUsize;
use std::path::Path;
use std::process::ExitCode;
use std::sync::Arc;
use std::time::Duration;

use axum::Router;
use axum::extract::{RawQuery, State};
use axum::response::{IntoResponse, Response};
use axum::routing::get;
use creche_contracts::config::noticeboard::{ACCESS_KEY_FILE, AccessKey, NoticeboardConfig};
use creche_contracts::config::{BindAddress, ConfigError, Env};
use creche_runtime::args::{Args, UsageError, Word};
use creche_runtime::atomic::{self, DirSync, FileMode, Parents, Write};
use creche_runtime::http::layers::starlette::StarletteBodies;
use creche_runtime::http::layers::{AccessLog, edge};
use creche_runtime::http::server::{Listen, bind, serve};
use creche_runtime::readfile::{ByteCap, FileRead, Follow, ReadRefusal, read_capped};
use creche_runtime::service::{self, Context, Loaded, Program, Threads};
use creche_runtime::tasks::{Drained, Tasks};
use creche_runtime::{error, log, warning};
use http::header::CONTENT_TYPE;
use http::{HeaderValue, StatusCode};

/// The name of the program: the target of its log lines, and the start of
/// each line of a refused start.
pub const NAME: &str = "creche-probe";

/// The flag that makes the program read its config and end.
pub const CHECK_FLAG: &str = "--check";

/// The path of the route that says that the program serves.
pub const HEALTH_PATH: &str = "/healthz";

/// The path of the route whose handler panics.
pub const PANIC_PATH: &str = "/probe/panic";

/// The path of the route whose handler waits and then writes a file.
pub const SLOW_PATH: &str = "/probe/slow";

/// The body of the answer of [`HEALTH_PATH`]. The Python origin is the
/// `/healthz` route of `noticeboard/src/noticeboard/app.py:195-198`, which
/// writes the same bytes.
pub const HEALTHY: &str = "{\"ok\":true}";

/// The start of the message of the panic of [`PANIC_PATH`]. The query of the
/// request follows it. No line of the log holds the message.
pub const PANIC_MESSAGE: &str = "the probe panics on request";

/// The name of the file that the handler of [`SLOW_PATH`] writes, in the
/// state root of the config.
pub const SLOW_FILE: &str = "probe-slow.done";

/// How long the handler of [`SLOW_PATH`] waits before it writes its file. A
/// client that leaves at once is gone before the write.
const SLOW_PAUSE: Duration = Duration::from_millis(500);

/// How long the listeners wait for an open request after the stop signal.
const OPEN_REQUESTS: Duration = Duration::from_secs(5);

/// How long the program waits for a task after its `main` returned. A
/// handler that still runs then has this time.
///
/// The program has no unit. `systemd/creche-noticeboard.service` gives the
/// service that it stands for 20 seconds, and the process-level suite gives
/// each service that time. `OPEN_REQUESTS` plus this limit is less.
pub const DRAIN: Duration = Duration::from_secs(10);

// CONTRACT-QUESTION: no contract and no answer of the owner names the exit
// status of a program whose listener does not bind. `rust/AGENTS.md`, "The
// rules for a service", rule 17 lists the causes of a refused start, and
// this cause is not one of them. `creche_runtime::service` has the same
// question, and each service states the status in its own `main`. The
// reading here is the status of the Python noticeboard, which is 3: the
// status of `uvicorn` for a start that failed. A restart can repair the
// cause, so the status is not `EX_CONFIG`. A change costs this constant.
/// The exit status of a program whose listener did not bind. The Python
/// origin is `STARTUP_FAILURE` of `uvicorn/config.py:80`. `uvicorn.run` ends
/// the Python noticeboard with it for an address that is in use
/// (`uvicorn/main.py:628-629`).
pub const NO_LISTENER: u8 = 3;

/// The count of worker threads. Two handlers then run at one time, as two
/// handlers of the Python noticeboard do.
const WORKERS: NonZeroUsize = NonZeroUsize::MIN.saturating_add(1);

/// The most bytes that the program reads from a key file. A key has 32 bytes
/// or more, and no rule gives it a largest count.
const KEY_FILE_CAP: ByteCap = ByteCap::ONE_MIB;

/// The content type of the answer of [`HEALTH_PATH`].
const JSON: &str = "application/json";

/// The body of the answer of [`SLOW_PATH`] after the write. The file of the
/// route holds the same bytes.
const SLOW_DONE: &str = "done\n";

/// The name of the task that writes the file of [`SLOW_PATH`].
const SLOW_TASK: &str = "probe-slow-file";

/// How the handler of [`SLOW_PATH`] writes its file. The state root is there
/// before the program starts, so the write makes no directory.
const SLOW_WRITE: Write = Write {
    mode: FileMode::Private,
    parents: Parents::MustExist,
    dir_sync: DirSync::Sync,
};

/// The entry function of the program. The `main` of `creche-probe` calls it
/// with the words of its command line and the variables of its environment.
///
/// The function has the start steps of `creche_runtime::service`:
///
/// 1. It reads the words. A word that the program does not take refuses the
///    start.
/// 2. It parses the config, and `service::load` applies the failure action
///    of the config type.
/// 3. It reads the key file that the config names. A file that it cannot
///    use refuses the start.
/// 4. For [`CHECK_FLAG`], it writes the report and returns status 0.
/// 5. It runs the listeners inside `service::run` until a stop signal.
///
/// Each refused start ends with status 78. A panic of these steps ends the
/// program with status 1. The log then holds the place of the panic and
/// never its message.
///
/// The function sets the panic hook of the process. A test thus does not
/// call it: the tests of `tests/process.rs` start the program as a process.
///
/// The function has no Python origin as a whole. The module doc names the
/// origin of its steps.
pub fn entry<I>(words: I, env: &Env) -> ExitCode
where
    I: IntoIterator<Item = OsString>,
{
    service::enter(NAME, || start(words, env))
}

/// What the command line asks for.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Asked {
    /// Serve until a stop signal.
    Serve,
    /// Read the config, write the report and end.
    Check,
}

/// Reads the words of the command line. The program takes one flag,
/// [`CHECK_FLAG`], and no other word.
///
/// The Python origin is `_parse_args` of
/// `noticeboard/src/noticeboard/__main__.py:97-108`.
fn read_words<I>(words: I) -> Result<Asked, UsageError>
where
    I: IntoIterator<Item = OsString>,
{
    let mut asked = Asked::Serve;

    for word in Args::from_os(words)? {
        match word {
            Word::Flag(flag) if flag == CHECK_FLAG => asked = Asked::Check,
            Word::Flag(word) | Word::Value(word) => {
                return Err(UsageError::Unexpected { word });
            }
        }
    }

    Ok(asked)
}

/// The steps 1 to 5 of [`entry`].
fn start<I>(words: I, env: &Env) -> ExitCode
where
    I: IntoIterator<Item = OsString>,
{
    // A daemon ends with `EX_CONFIG` for a wrong word too.
    let asked = match read_words(words) {
        Ok(asked) => asked,
        Err(error) => return service::refuse_start(NAME, &error),
    };
    let config = match service::load(NAME, NoticeboardConfig::from_env(env)) {
        Loaded::Run(config) => config,
        Loaded::Exit(status) => return status,
        // The type of this config says that the program exits, so `load`
        // gives this result for no config.
        Loaded::RefuseEachCall(errors) => return service::refuse_start(NAME, &errors),
    };
    let key = match key_state(&config) {
        Ok(key) => key,
        Err(error) => return service::refuse_start(NAME, &error),
    };

    match asked {
        Asked::Check => {
            for line in report(&config, key) {
                log::out_line(&line);
            }

            ExitCode::SUCCESS
        }
        Asked::Serve => {
            let program = Program::new(NAME, Threads::Workers(WORKERS), DRAIN);

            service::run(program, |context| serve_until_the_stop(context, config))
        }
    }
}

/// Whether the program has an access key.
///
/// The program keeps no key: no route of it asks for one. It reads the key
/// to prove the start steps of a service that has one.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum KeyState {
    /// The config gives a key.
    Set,
    /// The config gives no key. The config type permits that only for a
    /// loopback bind.
    Empty,
}

/// Why the program cannot use the key file that its config names.
///
/// No variant holds a byte of the file or the path of the file.
#[derive(Debug, Clone, PartialEq, Eq)]
enum KeyFileError {
    /// No file has the name.
    Absent,
    /// A file is there, and the reader does not take it.
    Refused(ReadRefusal),
    /// The bytes of the file are not UTF-8.
    NotUtf8,
    /// The text of the file is no key that the bind of the config permits.
    NotAKey(ConfigError),
}

impl fmt::Display for KeyFileError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Absent => write!(f, "{ACCESS_KEY_FILE}: no file has that name"),
            Self::Refused(refusal) => write!(f, "{ACCESS_KEY_FILE}: {refusal}"),
            Self::NotUtf8 => write!(f, "{ACCESS_KEY_FILE}: the file is not UTF-8"),
            // The error of the config names its own variable.
            Self::NotAKey(error) => write!(f, "{error}"),
        }
    }
}

impl Error for KeyFileError {}

/// Whether the config gives a key. For a config that names a key file, the
/// function reads the file.
fn key_state(config: &NoticeboardConfig) -> Result<KeyState, KeyFileError> {
    match config.access_key() {
        AccessKey::Key(_) => Ok(KeyState::Set),
        AccessKey::Open => Ok(KeyState::Empty),
        AccessKey::File(file) => key_in_file(config, file.as_path()),
    }
}

/// Reads the key file at `path` and applies the key rule of the config to
/// its text.
///
/// The read follows a symlink, as the Python reader does. The function does
/// not use `creche_runtime::token::read`: that reader refuses an empty file,
/// and the config type permits an empty key file for a loopback bind.
///
/// The Python origin is `_access_key` of
/// `noticeboard/src/noticeboard/config.py:178-196`. That reader takes a file
/// of each size. This function refuses a file of more than 1 MiB.
fn key_in_file(config: &NoticeboardConfig, path: &Path) -> Result<KeyState, KeyFileError> {
    let bytes = match read_capped(path, KEY_FILE_CAP, Follow::Follow) {
        FileRead::Bytes { bytes, .. } => bytes,
        FileRead::Absent => return Err(KeyFileError::Absent),
        FileRead::Refused(refusal) => return Err(KeyFileError::Refused(refusal)),
    };
    // The error of this call holds a place in the text and no byte of it.
    let text = std::str::from_utf8(&bytes).map_err(|_| KeyFileError::NotUtf8)?;
    let key = config.key_of_file(text).map_err(KeyFileError::NotAKey)?;

    Ok(match key {
        Some(_) => KeyState::Set,
        None => KeyState::Empty,
    })
}

/// The lines of the report of [`CHECK_FLAG`]: the bind, whether the bind is
/// loopback, and whether a key is set. No line holds a key or a part of one.
///
/// The Python origin is `_report` of
/// `noticeboard/src/noticeboard/__main__.py:64-76`. The report here holds
/// the first three lines of that report.
fn report(config: &NoticeboardConfig, key: KeyState) -> Vec<String> {
    let loopback = if config.on_loopback() { "yes" } else { "no" };
    let key = match key {
        KeyState::Set => "set",
        KeyState::Empty => "empty",
    };

    vec![
        format!(
            "bind          {}:{}",
            config.bind().as_str(),
            config.port().get()
        ),
        format!("loopback      {loopback}"),
        format!("access key    {key}"),
    ]
}

/// The `main` of the program inside the runtime: it binds the address of the
/// config and serves the routes until the stop signal.
///
/// A bind that fails is no refused start: the address can be free at the
/// next start. The status is then [`NO_LISTENER`]. A listener that stops
/// with an error after the bind ends the program with status 1. The Python
/// server has no such end: it writes the error of an accept and continues.
async fn serve_until_the_stop(context: Context, config: NoticeboardConfig) -> ExitCode {
    let address = BindAddress::on(config.bind().clone(), config.port());
    let bound = match bind(Listen::Tcp(address)).await {
        Ok(bound) => vec![bound],
        Err(failure) => {
            error!(NAME, "{failure}");

            return ExitCode::from(NO_LISTENER);
        }
    };
    service::ready(&bound);

    let probe = Probe {
        tasks: context.tasks().clone(),
        slow_file: Arc::from(config.state_root().as_path().join(SLOW_FILE)),
    };
    let app = edge(
        routes(probe),
        StarletteBodies,
        context.tasks().clone(),
        AccessLog::Skip,
    );

    match serve(bound, app, context.tasks(), OPEN_REQUESTS).await {
        Ok(Drained::Clean) => ExitCode::SUCCESS,
        Ok(Drained::TimedOut { left }) => {
            warning!(
                NAME,
                "the stop ended connections with an open request: {left}"
            );

            ExitCode::SUCCESS
        }
        Err(failure) => {
            error!(NAME, "{failure}");

            ExitCode::FAILURE
        }
    }
}

/// What each handler of the program can use.
#[derive(Debug, Clone)]
struct Probe {
    /// The tracked tasks of the program.
    tasks: Tasks,
    /// The file that the handler of [`SLOW_PATH`] writes.
    slow_file: Arc<Path>,
}

/// The three routes of the program. Each route names its one method, as the
/// edge layer demands.
fn routes(probe: Probe) -> Router {
    Router::new()
        .route(HEALTH_PATH, get(health))
        .route(PANIC_PATH, get(panics))
        .route(SLOW_PATH, get(slow))
        .with_state(probe)
}

/// The handler of [`HEALTH_PATH`]: status 200 and [`HEALTHY`], as JSON.
///
/// The Python origin is the `/healthz` route of
/// `noticeboard/src/noticeboard/app.py:195-198`.
async fn health() -> Response {
    (
        StatusCode::OK,
        [(CONTENT_TYPE, HeaderValue::from_static(JSON))],
        HEALTHY,
    )
        .into_response()
}

/// The handler of [`PANIC_PATH`]: it panics, with a message that holds the
/// query of the request.
///
/// The route proves the panic boundary of the edge layer. The client gets
/// the answer of the layer for a panic. The log gets the place of the panic
/// and never this message, so no part of the query reaches the log.
///
/// The handler has no Python origin. No route of a Python service raises on
/// request.
#[expect(
    clippy::panic,
    reason = "the route proves the panic boundary of the edge layer"
)]
async fn panics(RawQuery(query): RawQuery) -> Response {
    panic!("{PANIC_MESSAGE}: {}", query.unwrap_or_default());
}

/// The handler of [`SLOW_PATH`]: it waits for [`SLOW_PAUSE`], writes the
/// file [`SLOW_FILE`] and answers status 200.
///
/// The route proves that a handler runs to its end after its client left.
/// The write runs on the pool for blocking calls, as a tracked task. The
/// handler does not select on the stop signal: its one wait is shorter than
/// the limit that the listeners give an open request.
///
/// The handler has no Python origin.
async fn slow(State(probe): State<Probe>) -> Response {
    tokio::time::sleep(SLOW_PAUSE).await;

    let file = Arc::clone(&probe.slow_file);
    let written = probe
        .tasks
        .spawn_blocking(SLOW_TASK, move || {
            atomic::write(&file, SLOW_DONE.as_bytes(), SLOW_WRITE)
        })
        .await;

    match written {
        Ok(Ok(())) => (StatusCode::OK, SLOW_DONE).into_response(),
        Ok(Err(failure)) => {
            error!(NAME, "{failure}");

            StatusCode::INTERNAL_SERVER_ERROR.into_response()
        }
        Err(lost) => {
            error!(
                NAME,
                "the write of the file {SLOW_FILE} did not end: {lost}"
            );

            StatusCode::INTERNAL_SERVER_ERROR.into_response()
        }
    }
}

#[cfg(test)]
mod tests {
    use std::fs;
    use std::future::Future;
    use std::os::unix::ffi::OsStringExt;
    use std::os::unix::fs::PermissionsExt;
    use std::path::PathBuf;

    use axum::body::Body;
    use creche_contracts::config::noticeboard::{
        ACCESS_KEY, BIND, NoticeboardConfig, PORT, STATE_ROOT,
    };
    use creche_runtime::http::layers::{EdgeFailure, ErrorBodies};
    use creche_runtime::tasks::shutdown_pair;
    use http::header::ALLOW;
    use http::{Method, Request};

    use super::*;
    use crate::root::TempRoot;
    use crate::router::call;

    /// A key of 32 bytes that no deployment uses.
    const KEY: &str = "0123456789abcdef0123456789abcdef";

    /// A key under the least count of bytes.
    const SHORT_KEY: &str = "short-probe-key";

    /// The name of the key file of a test, in its root.
    const KEY_FILE: &str = "view.key";

    /// The loopback address and a LAN address of a test.
    const LOOPBACK: &str = "127.0.0.1";
    const ON_LAN: &str = "192.0.2.10";

    /// The most bytes that a test reads from the body of an answer.
    const BODY_MAX: usize = 4096;

    /// A config with the given variables, on the loopback address.
    fn config_of(pairs: &[(&str, &str)]) -> NoticeboardConfig {
        let mut env = vec![(BIND, LOOPBACK), (PORT, "18370")];
        env.extend_from_slice(pairs);

        NoticeboardConfig::from_env(&Env::from_pairs(env)).unwrap()
    }

    /// A config that names the key file of `root`, with the given bind.
    fn config_with_file(root: &TempRoot, bind: &str) -> NoticeboardConfig {
        let file = key_file(root);

        config_of(&[(BIND, bind), (ACCESS_KEY_FILE, file.to_str().unwrap())])
    }

    fn key_file(root: &TempRoot) -> PathBuf {
        root.path().join(KEY_FILE)
    }

    /// The key state of a config with the bind `bind`, whose key file holds
    /// `content`.
    #[derive(Debug, PartialEq, Eq)]
    struct OfFile(Result<KeyState, KeyFileError>);

    fn state_of_file(content: &[u8], bind: &str) -> OfFile {
        let root = TempRoot::new().unwrap();
        fs::write(key_file(&root), content).unwrap();

        OfFile(key_state(&config_with_file(&root, bind)))
    }

    fn words(arguments: &[&str]) -> Vec<OsString> {
        std::iter::once("creche-probe")
            .chain(arguments.iter().copied())
            .map(OsString::from)
            .collect()
    }

    fn block_on<F: Future>(future: F) -> F::Output {
        tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .unwrap()
            .block_on(future)
    }

    /// The routes of the program behind the edge layer, with `root` as the
    /// state root.
    fn app(state_root: &Path) -> Router {
        let (_trigger, shutdown) = shutdown_pair();
        let tasks = Tasks::new(shutdown);
        let probe = Probe {
            tasks: tasks.clone(),
            slow_file: Arc::from(state_root.join(SLOW_FILE)),
        };

        edge(routes(probe), StarletteBodies, tasks, AccessLog::Skip)
    }

    fn request(method: Method, target: &str) -> Request<Body> {
        Request::builder()
            .method(method)
            .uri(target)
            .body(Body::empty())
            .unwrap()
    }

    /// The status, the content type and the body of an answer.
    async fn parts_of(answer: Response) -> (StatusCode, Option<String>, Vec<u8>) {
        let status = answer.status();
        let content_type = answer
            .headers()
            .get(CONTENT_TYPE)
            .map(|value| value.to_str().unwrap().to_owned());
        let body = axum::body::to_bytes(answer.into_body(), BODY_MAX)
            .await
            .unwrap()
            .to_vec();

        (status, content_type, body)
    }

    #[test]
    fn the_program_takes_no_word_or_the_check_flag() {
        assert_eq!(read_words(words(&[])), Ok(Asked::Serve));
        assert_eq!(read_words(words(&[CHECK_FLAG])), Ok(Asked::Check));
        assert_eq!(
            read_words(words(&[CHECK_FLAG, CHECK_FLAG])),
            Ok(Asked::Check)
        );
    }

    #[test]
    fn the_program_refuses_each_other_word() {
        // (the words, the word that the error names)
        let refused = [
            (vec!["--chec"], "--chec"),
            (vec!["--check=yes"], "--check=yes"),
            (vec!["serve"], "serve"),
            (vec![CHECK_FLAG, "now"], "now"),
            (vec!["--help"], "--help"),
            (vec!["-h"], "-h"),
            (vec![""], ""),
        ];

        for (arguments, word) in refused {
            assert_eq!(
                read_words(words(&arguments)),
                Err(UsageError::Unexpected {
                    word: word.to_owned()
                }),
                "{arguments:?}"
            );
        }
    }

    #[test]
    fn a_word_that_is_not_utf8_refuses_the_command_line() {
        let line = vec![
            OsString::from("creche-probe"),
            OsString::from_vec(vec![0xff]),
        ];

        assert_eq!(read_words(line), Err(UsageError::NotUtf8));
    }

    #[test]
    fn a_key_of_the_environment_is_a_key_that_is_set() {
        assert_eq!(
            key_state(&config_of(&[(ACCESS_KEY, KEY)])),
            Ok(KeyState::Set)
        );
    }

    #[test]
    fn a_loopback_bind_with_no_key_has_an_empty_key() {
        assert_eq!(key_state(&config_of(&[])), Ok(KeyState::Empty));
    }

    #[test]
    fn a_key_file_with_a_full_key_is_a_key_that_is_set() {
        for bind in [LOOPBACK, ON_LAN] {
            assert_eq!(
                state_of_file(KEY.as_bytes(), bind),
                OfFile(Ok(KeyState::Set))
            );
            assert_eq!(
                state_of_file(format!("{KEY}\n").as_bytes(), bind),
                OfFile(Ok(KeyState::Set))
            );
        }
    }

    #[test]
    fn a_loopback_bind_takes_an_empty_key_file_and_a_short_key() {
        assert_eq!(state_of_file(b"", LOOPBACK), OfFile(Ok(KeyState::Empty)));
        assert_eq!(state_of_file(b" \n", LOOPBACK), OfFile(Ok(KeyState::Empty)));
        assert_eq!(
            state_of_file(SHORT_KEY.as_bytes(), LOOPBACK),
            OfFile(Ok(KeyState::Set))
        );
    }

    #[test]
    fn a_lan_bind_refuses_an_empty_key_file_and_a_short_key() {
        for content in ["", "\n", SHORT_KEY] {
            assert_eq!(
                state_of_file(content.as_bytes(), ON_LAN),
                OfFile(Err(KeyFileError::NotAKey(ConfigError::OpenOnLan {
                    variable: BIND
                }))),
                "{content:?}"
            );
        }
    }

    #[test]
    fn a_key_file_that_is_not_there_is_refused_on_loopback_too() {
        let root = TempRoot::new().unwrap();

        for bind in [LOOPBACK, ON_LAN] {
            assert_eq!(
                key_state(&config_with_file(&root, bind)),
                Err(KeyFileError::Absent)
            );
        }
    }

    #[test]
    fn a_key_file_that_is_a_directory_is_refused() {
        let root = TempRoot::new().unwrap();
        fs::create_dir(key_file(&root)).unwrap();

        assert_eq!(
            key_state(&config_with_file(&root, LOOPBACK)),
            Err(KeyFileError::Refused(ReadRefusal::NotAFile))
        );
    }

    #[test]
    fn a_key_file_that_is_not_utf8_is_refused() {
        let mut content = KEY.as_bytes().to_vec();
        content.push(0xff);

        assert_eq!(
            state_of_file(&content, LOOPBACK),
            OfFile(Err(KeyFileError::NotUtf8))
        );
    }

    #[test]
    fn a_key_file_past_the_cap_is_refused() {
        let content = vec![b'k'; KEY_FILE_CAP.get() + 1];

        assert_eq!(
            state_of_file(&content, LOOPBACK),
            OfFile(Err(KeyFileError::Refused(ReadRefusal::TooLarge {
                cap: KEY_FILE_CAP
            })))
        );
    }

    #[test]
    fn the_read_of_a_key_file_follows_a_symlink() {
        let root = TempRoot::new().unwrap();
        let real = root.path().join("real.key");
        fs::write(&real, KEY).unwrap();
        std::os::unix::fs::symlink(&real, key_file(&root)).unwrap();

        assert_eq!(
            key_state(&config_with_file(&root, ON_LAN)),
            Ok(KeyState::Set)
        );
    }

    #[test]
    fn a_key_file_that_the_owner_cannot_read_is_refused() {
        let root = TempRoot::new().unwrap();
        fs::write(key_file(&root), KEY).unwrap();
        fs::set_permissions(key_file(&root), fs::Permissions::from_mode(0o000)).unwrap();

        // The superuser reads each file. The test then has no file that it
        // cannot read.
        if fs::read(key_file(&root)).is_ok() {
            return;
        }

        assert!(matches!(
            key_state(&config_with_file(&root, LOOPBACK)),
            Err(KeyFileError::Refused(ReadRefusal::Unreadable { .. }))
        ));
    }

    #[test]
    fn each_key_file_error_names_a_variable_and_no_path() {
        let root = TempRoot::new().unwrap();
        let errors = [
            KeyFileError::Absent,
            KeyFileError::Refused(ReadRefusal::NotAFile),
            KeyFileError::Refused(ReadRefusal::TooLarge { cap: KEY_FILE_CAP }),
            KeyFileError::NotUtf8,
            KeyFileError::NotAKey(ConfigError::OpenOnLan { variable: BIND }),
        ];

        for error in errors {
            let text = error.to_string();
            let variable = match &error {
                KeyFileError::NotAKey(_) => BIND,
                _ => ACCESS_KEY_FILE,
            };

            assert!(text.starts_with(&format!("{variable}: ")), "{text}");
            assert!(!text.contains(KEY_FILE), "{text}");
            assert!(!text.contains(root.path().to_str().unwrap()), "{text}");
        }
    }

    #[test]
    fn the_report_says_the_bind_and_whether_a_key_is_set() {
        let loopback = config_of(&[(ACCESS_KEY, KEY)]);
        let on_lan = config_of(&[(BIND, ON_LAN), (ACCESS_KEY, KEY)]);

        assert_eq!(
            report(&loopback, KeyState::Set),
            [
                "bind          127.0.0.1:18370",
                "loopback      yes",
                "access key    set",
            ]
        );
        assert_eq!(
            report(&on_lan, KeyState::Set),
            [
                "bind          192.0.2.10:18370",
                "loopback      no",
                "access key    set",
            ]
        );
        assert_eq!(
            report(&config_of(&[]), KeyState::Empty)[2],
            "access key    empty"
        );
    }

    #[test]
    fn no_line_of_the_report_holds_a_key() {
        let config = config_of(&[(ACCESS_KEY, KEY)]);

        for line in report(&config, key_state(&config).unwrap()) {
            assert!(!line.contains(KEY), "{line}");
        }
    }

    #[test]
    fn the_health_route_answers_the_bytes_of_the_python_route() {
        let root = TempRoot::new().unwrap();

        let (status, content_type, body) = block_on(async {
            parts_of(call(app(root.path()), request(Method::GET, HEALTH_PATH)).await).await
        });

        assert_eq!(status, StatusCode::OK);
        assert_eq!(content_type.as_deref(), Some("application/json"));
        assert_eq!(body, HEALTHY.as_bytes());
        assert_eq!(
            serde_json::from_slice::<serde_json::Value>(&body).unwrap(),
            serde_json::json!({ "ok": true })
        );
    }

    #[test]
    fn a_path_with_no_route_and_a_wrong_method_get_the_answer_of_the_framework() {
        let root = TempRoot::new().unwrap();

        block_on(async {
            let unknown = call(app(root.path()), request(Method::GET, "/probe")).await;
            let expected = parts_of(StarletteBodies.answer(EdgeFailure::NoRoute)).await;
            assert_eq!(parts_of(unknown).await, expected);

            for path in [HEALTH_PATH, PANIC_PATH, SLOW_PATH] {
                let refused = call(app(root.path()), request(Method::POST, path)).await;
                let allowed = refused.headers().get(ALLOW).cloned();
                let expected = parts_of(StarletteBodies.answer(EdgeFailure::WrongMethod)).await;

                assert_eq!(parts_of(refused).await, expected, "{path}");
                assert_eq!(allowed.unwrap(), "GET", "{path}");
            }
        });
    }

    #[test]
    fn the_slow_route_writes_its_file_and_then_answers() {
        let root = TempRoot::new().unwrap();
        let file = root.path().join(SLOW_FILE);

        let (status, _content_type, body) = block_on(async {
            let answer = call(app(root.path()), request(Method::GET, SLOW_PATH)).await;
            // The file is there when the answer is.
            assert_eq!(fs::read(&file).unwrap(), SLOW_DONE.as_bytes());

            parts_of(answer).await
        });

        assert_eq!(status, StatusCode::OK);
        assert_eq!(body, SLOW_DONE.as_bytes());
        assert_eq!(
            fs::metadata(&file).unwrap().permissions().mode() & 0o777,
            0o600
        );
    }

    #[test]
    fn the_slow_route_writes_its_file_again_at_each_request() {
        let root = TempRoot::new().unwrap();
        let file = root.path().join(SLOW_FILE);
        fs::write(&file, b"an older file").unwrap();

        block_on(async {
            let answer = call(app(root.path()), request(Method::GET, SLOW_PATH)).await;
            assert_eq!(answer.status(), StatusCode::OK);
        });

        assert_eq!(fs::read(&file).unwrap(), SLOW_DONE.as_bytes());
    }

    #[test]
    fn the_program_has_two_worker_threads() {
        assert_eq!(WORKERS.get(), 2);
    }

    #[test]
    fn the_wait_of_the_slow_route_is_inside_the_limit_of_an_open_request() {
        assert!(SLOW_PAUSE < OPEN_REQUESTS);
    }

    #[test]
    fn the_state_root_variable_names_the_directory_of_the_slow_file() {
        let root = TempRoot::new().unwrap();
        let config = config_of(&[(STATE_ROOT, root.path().to_str().unwrap())]);

        assert_eq!(
            config.state_root().as_path().join(SLOW_FILE),
            root.path().join(SLOW_FILE)
        );
    }
}
