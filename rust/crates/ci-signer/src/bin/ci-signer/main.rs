//! `ci-signer`: the release signer of `docs/design/forgejo-release-path.md`,
//! section 4. A service on the attach interface of the CI runner (the
//! runner design at commit 131ce43, section 6).
//!
//! ```text
//! ci-signer serve --config <path>           the service, under creche-ci-signer.socket
//! ci-signer verify --json --config <path>   the verify hook of the component
//! ```
//!
//! `serve` runs under `creche-ci-signer.service` as the user `ci-signer`:
//!
//! 1. It reads the host config (`config.rs`), the credentials of the unit
//!    (`credentials.rs`) and the journal of the issued runs (`ledger.rs`)
//!    once. A fault in any of them exits 78 and serves nothing.
//! 2. It takes the listening socket of stdin (`serve.rs`) and serves each
//!    control connection of the runner daemon as one session
//!    (`session.rs`): the daemon check, the `open` line, the checks of
//!    section 4.4 (`signer.rs`), the answer, and the peer check of each
//!    passed fd before the guest exchange (`guest.rs`).
//!
//! The exit status: 0 for a verify that passed, 1 for one that failed, 2
//! for a fault of the argv, 78 for a fault of the start of `serve`.
//!
//! ```text
//!   ci-runner daemon ──open, conn + fd, close──► /run/ci-signer/attach.sock
//!                                                  │ session.rs
//!                  check_daemon, check(fd, slot) ──┤
//!                                                  ▼
//!   Forgejo ◄── GET runs, GET pull, POST tags ── signer.rs ──► cosign sign-blob
//!                                                  │
//!   job VM ◄──── one request line, one answer ── guest.rs
//! ```

// The service and its session need the peer check, which needs the
// kernel of Linux. On another OS only the pure parts and their tests
// build, so the parts that only the service calls are dead there.
#![cfg_attr(
    not(target_os = "linux"),
    expect(dead_code, reason = "only the service of Linux calls them")
)]

mod config;
mod cosign;
mod credentials;
mod forge;
mod guest;
mod ledger;
mod line;
#[cfg(target_os = "linux")]
mod serve;
#[cfg(target_os = "linux")]
mod session;
mod signer;
#[cfg(test)]
mod tests;
mod verify;

use std::path::PathBuf;
use std::process::ExitCode;

/// The prefix of each log line.
const PREFIX: &str = "ci-signer";

/// The usage line.
const USAGE: &str =
    "usage: ci-signer serve --config <path> | ci-signer verify --json --config <path>";

/// The exit status of a fault of the argv.
const USAGE_FAULT: u8 = 2;

/// The exit status of a fault of the start of `serve` (EX_CONFIG).
const START_FAULT: u8 = 78;

/// The work folder of cosign: `RuntimeDirectory=ci-signer-work` of the
/// unit, mode 0700.
const WORK_FOLDER: &str = "/run/ci-signer-work";

/// The worker threads of the runtime. A session waits on sockets and on
/// Forgejo, and the one heavy step, cosign, is a child process.
const WORKERS: usize = 2;

/// Prints one line of the log to stdout, which the unit sends to the
/// journal. A line holds no byte of a guest, of a reply or of a secret.
pub fn log(text: std::fmt::Arguments<'_>) {
    println!("{PREFIX}: {text}");
}

/// The mode of one run.
#[derive(Debug, PartialEq, Eq)]
enum Mode {
    /// The service, with the host config at the path.
    Serve(PathBuf),
    /// The verify hook, with the host config at the path.
    Verify(PathBuf),
}

/// The mode of the arguments `args`, with no program name. The path must
/// be absolute.
fn mode(args: &[String]) -> Option<Mode> {
    let path = |text: &String| Some(PathBuf::from(text)).filter(|path| path.is_absolute());
    match args {
        [serve, flag, config] if serve == "serve" && flag == "--config" => {
            path(config).map(Mode::Serve)
        }
        [verify, json, flag, config]
            if verify == "verify" && json == "--json" && flag == "--config" =>
        {
            path(config).map(Mode::Verify)
        }
        _ => None,
    }
}

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let Some(mode) = mode(&args) else {
        eprintln!("{USAGE}");
        return ExitCode::from(USAGE_FAULT);
    };
    match mode {
        Mode::Verify(config) => hook(&verify::run(&config)),
        Mode::Serve(config) => serve_mode(&config),
    }
}

/// Writes the report of the hook as one line. Exit 0 when each check
/// passed, else 1 (contract 06 §4 rule 3).
fn hook(report: &verify::Report) -> ExitCode {
    match serde_json::to_string(report) {
        Ok(line) => println!("{line}"),
        Err(error) => {
            eprintln!("{PREFIX}: the report does not serialize: {error}");
            return ExitCode::FAILURE;
        }
    }
    if report.passed() {
        ExitCode::SUCCESS
    } else {
        ExitCode::FAILURE
    }
}

/// A fault of the start: one line on stderr and the exit status 78.
fn refuse(text: std::fmt::Arguments<'_>) -> ExitCode {
    eprintln!("{PREFIX}: {text}");
    ExitCode::from(START_FAULT)
}

#[cfg(target_os = "linux")]
fn serve_mode(config_path: &std::path::Path) -> ExitCode {
    let bytes = match std::fs::read(config_path) {
        Ok(bytes) => bytes,
        Err(error) => return refuse(format_args!("{}: {error}", config_path.display())),
    };
    let config = match config::Config::parse(&bytes) {
        Ok(config) => config,
        Err(fault) => return refuse(format_args!("{}: {fault}", config_path.display())),
    };
    let Some(credentials) = std::env::var_os(credentials::CREDENTIALS_DIRECTORY) else {
        return refuse(format_args!(
            "{} is not set: run serve under creche-ci-signer.service",
            credentials::CREDENTIALS_DIRECTORY
        ));
    };
    let paths = signer::Paths {
        journal: PathBuf::from(ledger::JOURNAL_PATH),
        work: PathBuf::from(WORK_FOLDER),
        credentials: PathBuf::from(credentials),
    };
    let signer = match signer::Signer::start(config, &paths) {
        Ok(signer) => signer,
        Err(fault) => return refuse(format_args!("{fault}")),
    };
    let listener = match serve::listener() {
        Ok(listener) => listener,
        Err(error) => return refuse(format_args!("{error}")),
    };
    let runtime = match tokio::runtime::Builder::new_multi_thread()
        .worker_threads(WORKERS)
        .enable_all()
        .build()
    {
        Ok(runtime) => runtime,
        Err(error) => return refuse(format_args!("the runtime: {error}")),
    };
    match runtime.block_on(serve::serve(listener, signer)) {
        Ok(()) => ExitCode::SUCCESS,
        Err(error) => refuse(format_args!("{error}")),
    }
}

/// The service needs the peer check, which needs the kernel of Linux.
#[cfg(not(target_os = "linux"))]
fn serve_mode(_config_path: &std::path::Path) -> ExitCode {
    refuse(format_args!("serve runs on Linux only"))
}

#[cfg(test)]
mod argv_tests {
    use std::path::PathBuf;

    use super::{Mode, mode};

    fn args(words: &[&str]) -> Vec<String> {
        words.iter().map(|word| (*word).to_owned()).collect()
    }

    #[test]
    fn the_argv_names_one_mode() {
        let config = "/etc/creche/ci-signer/signer.conf";
        assert_eq!(
            mode(&args(&["serve", "--config", config])),
            Some(Mode::Serve(PathBuf::from(config)))
        );
        assert_eq!(
            mode(&args(&["verify", "--json", "--config", config])),
            Some(Mode::Verify(PathBuf::from(config)))
        );
        for words in [
            &[][..],
            &["serve"],
            &["serve", "--config"],
            &["serve", "--config", "signer.conf"],
            &["serve", "--config", config, "x"],
            &["verify", "--config", config],
            &["verify", "--json", "--config", "relative"],
            &["other"],
        ] {
            assert_eq!(mode(&args(words)), None, "{words:?}");
        }
    }
}
