//! The stand-in facade of the kvm tests (decision CH62 rule 12, gap
//! Q54-12 option A). It is a test program in no release row. A kvm test
//! puts it at `creche_wire::guest::FACADE_PROGRAM` of the root disk that
//! the test builds, and guest init starts it there.
//!
//! 1. It writes `standin: stdout` on stdout, which guest init sends to
//!    /dev/null, so the line never reaches the console (decision CH62
//!    rule 26).
//! 2. It writes three lines on stderr, which guest init sends to the
//!    console: `standin: session <CRECHE_SESSION_ID>`, `standin: env
//!    <each name, in byte order>` and `standin: cwd <dir>` (decisions
//!    CH49 rule 1, CH62 rules 5 and 6).
//! 3. It reads its mode from `/etc/creche-standin/mode` of the root disk.
//!    In mode `exit-once`, a start that finds no marker on the session
//!    disk writes the marker and exits with status 3. Each other start
//!    stays up until a signal ends it. With no mode file it stays up. An
//!    other mode exits with status 2.

use std::fs;
use std::io::{self, Write as _};
use std::path::Path;
use std::process::ExitCode;
use std::thread;

use creche_wire::guest::{SESSION_DISK_MOUNT, SESSION_ID_ENV};

/// The mode file of the test root disk.
const MODE: &str = "/etc/creche-standin/mode";

/// The marker of mode `exit-once`, on the session disk.
const MARKER: &str = "standin-exited-once";

/// The exit status of the first start in mode `exit-once`.
const EXIT_ONCE: u8 = 3;

/// The exit status of a mode that the stand-in does not know.
const BAD_MODE: u8 = 2;

fn main() -> ExitCode {
    let _ = writeln!(io::stdout(), "standin: stdout");
    let _ = io::stdout().flush();
    say(&format!(
        "session {}",
        std::env::var(SESSION_ID_ENV).unwrap_or_default()
    ));
    let mut names: Vec<String> = std::env::vars_os()
        .map(|(name, _)| name.to_string_lossy().into_owned())
        .collect();
    names.sort();
    say(&format!("env {}", names.join(" ")));
    let cwd = std::env::current_dir()
        .map_or_else(|error| error.to_string(), |dir| dir.display().to_string());
    say(&format!("cwd {cwd}"));

    match fs::read_to_string(MODE) {
        Ok(mode) if mode.trim_end() == "exit-once" => {
            let marker = Path::new(SESSION_DISK_MOUNT).join(MARKER);
            if !marker.exists() {
                if let Err(error) = fs::write(&marker, b"") {
                    say(&format!("marker write failed: {error}"));
                }
                say(&format!("exit {EXIT_ONCE}"));
                return ExitCode::from(EXIT_ONCE);
            }
        }
        Ok(mode) => {
            say(&format!("unknown mode {:?}", mode.trim_end()));
            return ExitCode::from(BAD_MODE);
        }
        Err(_) => {}
    }
    say("up");
    loop {
        thread::park();
    }
}

/// Writes one `standin: <text>` line on stderr.
fn say(text: &str) {
    let _ = writeln!(io::stderr(), "standin: {text}");
}
