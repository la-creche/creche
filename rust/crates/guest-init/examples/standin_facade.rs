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
//!    disk starts a child of itself that stays up in its process group,
//!    writes the child pid in the marker and exits with status 3. A start
//!    that finds the marker writes whether that child is gone, then stays
//!    up. Each other start stays up until a signal ends it. With no mode
//!    file it stays up. An other mode exits with status 2.
//! 4. In mode `holder` it starts a child of itself, the holder, that
//!    leaves its process group with `setsid`, so the SIGTERM of the group
//!    misses it, as it misses a process that ignores SIGTERM. The holder
//!    keeps a file of the session disk open for writing and writes to it
//!    until a SIGKILL ends it. The stand-in then stays up.
//! 5. In mode `turn` it opens the local link and writes a turn start and
//!    a turn end on it (decisions CH21, CH62 rule 10). Then it starts a
//!    child of itself, the stranger, that opens the link too and waits
//!    for guest init to close it. Then it waits for the stop line, and it
//!    exits with status 0 on it (decision CH62 rule 9). A fault exits
//!    with status 4.
//! 6. In mode `stream` it opens the local link and writes turn starts
//!    and turn ends on it with no pause, until a signal ends it. It never
//!    reads the stop line, so the stop waits its full `FACADE_STOP_WAIT`.
//! 7. In mode `close-once`, a start that finds no marker on the session
//!    disk writes the marker, opens the local link, closes it and stays
//!    up. Guest init then kills it (decision CH32 rule 7). Each other
//!    start stays up.
//! 8. In mode `relay` it waits `RELAY_DELAY` (2 s), then listens on
//!    `creche_wire::guest::RELAY_SOCKET` and answers each line `ping` of
//!    each link with `pong <its pid>`. It writes `relay listening`, and
//!    `relay link ended` at the end of each link (decisions CH13, CH62
//!    rule 16). Then it stays up. A line `pull` makes it pull once: one
//!    `FamilyStatePull` on `creche_wire::guest::FAMILY_STATE_SOCKET`,
//!    then one HTTP GET to guest 127.0.0.1:4000 and one to :8300. It
//!    answers `pulled <its pid> <reply key> <status 4000> <status 8300>
//!    <pull ms>`. A link that ends with no answer gives `closed`, and each
//!    other fault `failed` (decisions CH5a, CH6-2, CH62 rule 13).
//! 9. In mode `markers` it writes `MARKERS` files on the session disk,
//!    each with its own line, and writes `markers written`. Then it stays
//!    up. A fault exits with status 4. A kvm test syncs and kills CH,
//!    then reads them from the disk (decision CH62 rule 23).

use std::convert::Infallible;
use std::fs::{self, File};
use std::io::{self, BufRead as _, BufReader, Read as _, Write as _};
use std::net::{Ipv4Addr, TcpStream};
use std::os::unix::net::{UnixListener, UnixStream};
use std::path::Path;
use std::process::{Command, ExitCode};
use std::thread;
use std::time::{Duration, Instant};

use creche_wire::guest::{
    CHAPERONE_PORT, FAMILY_STATE_SOCKET, LITELLM_PORT, LOCAL_LINK_SOCKET, RELAY_SOCKET,
    SESSION_DISK_MOUNT, SESSION_ID_ENV,
};
use creche_wire::{
    ControlCommand, FAMILY_STATE_CAP, FamilyStatePull, FamilyStateReply, TurnEnd, TurnEvent,
    TurnStart, UnixMillis,
};

/// The mode file of the test root disk.
const MODE: &str = "/etc/creche-standin/mode";

/// The marker of the modes `exit-once` and `close-once`, on the session
/// disk.
const MARKER: &str = "standin-exited-once";

/// The exit status of the first start in mode `exit-once`.
const EXIT_ONCE: u8 = 3;

/// The exit status of a mode that the stand-in does not know.
const BAD_MODE: u8 = 2;

/// The exit status of a fault in mode `turn`.
const TURN_FAILED: u8 = 4;

/// The exit status of a child that failed.
const CHILD_FAILED: u8 = 5;

/// The argument that makes the stand-in the child of mode `exit-once`.
const CHILD: &str = "child";

/// The argument that makes the stand-in the holder of mode `holder`.
const HOLDER: &str = "holder";

/// The argument that makes the stand-in the stranger of mode `turn`.
const STRANGER: &str = "stranger";

/// The file that the holder keeps open, on the session disk.
const HELD: &str = "standin-held";

/// The pause between two writes of the holder.
const HOLD_PAUSE: Duration = Duration::from_millis(10);

/// The title of the turn end of mode `turn`. `tests/common/mod.rs` holds
/// the same value.
const TURN_TITLE: &str = "standin turn";

/// The `updated_at` of the turn end of mode `turn`, 2030-01-01T00:00:00Z.
/// `tests/common/mod.rs` holds the same value.
const TURN_UPDATED_AT: u64 = 1_893_456_000_000;

/// How long mode `relay` waits before it listens on the relay socket, so
/// a 1201 link of a kvm test comes first.
const RELAY_DELAY: Duration = Duration::from_secs(2);

/// How long a pull waits for each answer.
const PULL_WAIT: Duration = Duration::from_secs(10);

/// The files of mode `markers`, on the session disk.
/// `tests/kvm_guest_init.rs` reads the same names.
const MARKERS: [&str; 3] = ["standin-marker-1", "standin-marker-2", "standin-marker-3"];

fn main() -> ExitCode {
    match std::env::args().nth(1).as_deref() {
        Some(CHILD) => stay_up(),
        Some(HOLDER) => hold(),
        Some(STRANGER) => stranger(),
        _ => facade(),
    }
}

/// The facade itself.
fn facade() -> ExitCode {
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
            match fs::read_to_string(&marker) {
                Ok(child) => say(&first_child(child.trim_end())),
                Err(_) => {
                    if let Err(error) = exit_once(&marker) {
                        say(&format!("first start failed: {error}"));
                    }
                    say(&format!("exit {EXIT_ONCE}"));
                    return ExitCode::from(EXIT_ONCE);
                }
            }
        }
        Ok(mode) if mode.trim_end() == "holder" => {
            if let Err(error) = start_self(HOLDER) {
                say(&format!("holder start failed: {error}"));
            }
        }
        Ok(mode) if mode.trim_end() == "turn" => {
            return turn().unwrap_or_else(|error| {
                say(&format!("turn failed: {error}"));
                ExitCode::from(TURN_FAILED)
            });
        }
        Ok(mode) if mode.trim_end() == "stream" => {
            let Err(error) = stream();
            say(&format!("stream failed: {error}"));
            return ExitCode::from(TURN_FAILED);
        }
        Ok(mode) if mode.trim_end() == "close-once" => {
            let marker = Path::new(SESSION_DISK_MOUNT).join(MARKER);
            if !marker.exists() {
                if let Err(error) = close_once(&marker) {
                    say(&format!("close-once failed: {error}"));
                    return ExitCode::from(TURN_FAILED);
                }
                return stay_up();
            }
        }
        Ok(mode) if mode.trim_end() == "markers" => {
            if let Err(error) = markers() {
                say(&format!("markers failed: {error}"));
                return ExitCode::from(TURN_FAILED);
            }
            say("markers written");
        }
        Ok(mode) if mode.trim_end() == "relay" => {
            thread::spawn(|| {
                let Err(error) = relay();
                say(&format!("relay failed: {error}"));
            });
        }
        Ok(mode) => {
            say(&format!("unknown mode {:?}", mode.trim_end()));
            return ExitCode::from(BAD_MODE);
        }
        Err(_) => {}
    }
    say("up");
    stay_up()
}

/// Parks until a signal ends the process.
fn stay_up() -> ExitCode {
    loop {
        thread::park();
    }
}

/// Starts a child of the stand-in with `arg` and returns its pid. The
/// child is never waited for: guest init reaps it.
fn start_self(arg: &str) -> io::Result<u32> {
    let child = Command::new(std::env::current_exe()?).arg(arg).spawn()?;
    Ok(child.id())
}

/// The first start of mode `exit-once`: a child that stays up, and its
/// pid in `marker`.
fn exit_once(marker: &Path) -> io::Result<()> {
    let child = start_self(CHILD)?;
    say(&format!("child pid {child} started"));
    fs::write(marker, child.to_string())
}

/// Whether the child of the first start, `pid`, is gone.
fn first_child(pid: &str) -> String {
    if Path::new("/proc").join(pid).exists() {
        format!("first child {pid} left")
    } else {
        "first child gone".to_owned()
    }
}

/// The holder of mode `holder`: its own session and process group, and a
/// file of the session disk open for writing.
fn hold() -> ExitCode {
    if let Err(error) = rustix::process::setsid() {
        say(&format!("holder setsid failed: {error}"));
        return ExitCode::from(CHILD_FAILED);
    }
    let mut file = match File::create(Path::new(SESSION_DISK_MOUNT).join(HELD)) {
        Ok(file) => file,
        Err(error) => {
            say(&format!("holder open failed: {error}"));
            return ExitCode::from(CHILD_FAILED);
        }
    };
    say("holder up");
    loop {
        if let Err(error) = file.write_all(b"held\n") {
            say(&format!("holder write failed: {error}"));
            return ExitCode::from(CHILD_FAILED);
        }
        thread::sleep(HOLD_PAUSE);
    }
}

/// Writes one `standin: <text>` line on stderr.
fn say(text: &str) {
    let _ = writeln!(io::stderr(), "standin: {text}");
}

/// The lines of one turn: a turn start, then the turn end of the tests.
fn turn_lines() -> io::Result<Vec<u8>> {
    let end = TurnEnd {
        title: TURN_TITLE
            .parse()
            .map_err(|error| io::Error::other(format!("{error}")))?,
        updated_at: UnixMillis::try_from(TURN_UPDATED_AT)
            .map_err(|error| io::Error::other(format!("{error}")))?,
    };
    let mut lines = Vec::new();
    for event in [TurnEvent::Start(TurnStart {}), TurnEvent::End(end)] {
        serde_json::to_writer(&mut lines, &event)?;
        lines.push(b'\n');
    }
    Ok(lines)
}

/// Mode `turn`: the turn, the stranger, then the wait for the stop line.
fn turn() -> io::Result<ExitCode> {
    let mut link = UnixStream::connect(LOCAL_LINK_SOCKET)?;
    say("link open");
    link.write_all(&turn_lines()?)?;
    say("turn sent");

    let status = Command::new(std::env::current_exe()?)
        .arg(STRANGER)
        .status()?;
    say(&format!("stranger {status}"));

    let mut line = String::new();
    BufReader::new(&link).read_line(&mut line)?;
    match serde_json::from_str::<ControlCommand>(line.trim_end()) {
        Ok(ControlCommand::Stop(_)) => {
            say("stop line");
            Ok(ExitCode::SUCCESS)
        }
        _ => {
            say(&format!("unexpected line {line:?}"));
            Ok(ExitCode::from(TURN_FAILED))
        }
    }
}

/// The stranger of mode `turn`: a process other than the facade opens
/// the local link. Guest init closes it at once (decision CH62 rule 10).
fn stranger() -> ExitCode {
    let mut link = match UnixStream::connect(LOCAL_LINK_SOCKET) {
        Ok(link) => link,
        Err(error) => {
            say(&format!("stranger connect failed: {error}"));
            return ExitCode::from(TURN_FAILED);
        }
    };
    let mut byte = [0_u8; 1];
    match link.read(&mut byte) {
        Ok(0) => {
            say("stranger closed");
            ExitCode::SUCCESS
        }
        Ok(_) => {
            say("stranger got a byte");
            ExitCode::from(TURN_FAILED)
        }
        Err(error) => {
            say(&format!("stranger read failed: {error}"));
            ExitCode::from(TURN_FAILED)
        }
    }
}

/// Mode `stream`: turns on the local link with no pause. It returns only
/// the error that ends the stream.
fn stream() -> io::Result<Infallible> {
    let mut link = UnixStream::connect(LOCAL_LINK_SOCKET)?;
    say("link open");
    let lines = turn_lines()?;
    loop {
        link.write_all(&lines)?;
    }
}

/// The first start of mode `close-once`: the marker, then a local link
/// that closes at once.
fn close_once(marker: &Path) -> io::Result<()> {
    fs::write(marker, b"")?;
    drop(UnixStream::connect(LOCAL_LINK_SOCKET)?);
    say("link closed");
    Ok(())
}

/// Mode `relay`: the relay socket after `RELAY_DELAY`, and one thread per
/// link. It returns only the error that ends the listener.
fn relay() -> io::Result<Infallible> {
    thread::sleep(RELAY_DELAY);
    let path = Path::new(RELAY_SOCKET);
    if let Some(dir) = path.parent() {
        fs::create_dir_all(dir)?;
    }
    let listener = UnixListener::bind(path)?;
    say("relay listening");
    loop {
        let (link, _) = listener.accept()?;
        thread::spawn(move || {
            if let Err(error) = pong(&link) {
                say(&format!("relay link failed: {error}"));
            }
        });
    }
}

/// Answers each line `ping` of `link` with `pong <pid>` and each line
/// `pull` with `pulled ...` until its end.
fn pong(link: &UnixStream) -> io::Result<()> {
    let pid = std::process::id();
    for line in BufReader::new(link).lines() {
        let line = line?;
        if line == "ping" {
            writeln!(&*link, "pong {pid}")?;
        } else if line == "pull" {
            let started = Instant::now();
            let reply = answer(pull());
            let pull_ms = started.elapsed().as_millis();
            let litellm = answer(get(LITELLM_PORT));
            let chaperone = answer(get(CHAPERONE_PORT));
            writeln!(
                &*link,
                "pulled {pid} {reply} {litellm} {chaperone} {pull_ms}"
            )?;
        } else {
            say(&format!("relay got {line:?}"));
        }
    }
    say("relay link ended");
    Ok(())
}

/// The word of one answer: the answer, `closed` for a link that ended
/// with none, or `failed`.
fn answer(got: io::Result<Option<String>>) -> String {
    match got {
        Ok(Some(word)) => word,
        Ok(None) => "closed".to_owned(),
        Err(error) if error.kind() == io::ErrorKind::ConnectionReset => "closed".to_owned(),
        Err(error) => {
            say(&format!("pull failed: {error}"));
            "failed".to_owned()
        }
    }
}

/// One pull on the family-state socket: the key of the reply, or `None`
/// when the link ends with no line (decisions CH5a, CH62 rule 13).
fn pull() -> io::Result<Option<String>> {
    let link = UnixStream::connect(FAMILY_STATE_SOCKET)?;
    link.set_read_timeout(Some(PULL_WAIT))?;
    let mut line = serde_json::to_vec(&FamilyStatePull {})?;
    line.push(b'\n');
    // A link that guest init closed fails the write. The read then names
    // the close.
    let _ = (&link).write_all(&line);
    let mut reply = String::new();
    let cap = u64::try_from(FAMILY_STATE_CAP).map_err(io::Error::other)?;
    BufReader::new(&link).take(cap + 1).read_line(&mut reply)?;
    if reply.is_empty() {
        return Ok(None);
    }
    let key = match serde_json::from_str(reply.trim_end())? {
        FamilyStateReply::State(_) => "state",
        FamilyStateReply::CredsMissing(_) => "creds_missing",
        FamilyStateReply::ConfigMissing(_) => "config_missing",
        FamilyStateReply::NotValid(_) => "not_valid",
        FamilyStateReply::TooLarge(_) => "too_large",
    };
    Ok(Some(key.to_owned()))
}

/// One HTTP GET to the plane at guest 127.0.0.1:`port`: the status code
/// of the answer, or `None` when the link ends with none (decision
/// CH6-2).
fn get(port: u16) -> io::Result<Option<String>> {
    let stream = TcpStream::connect((Ipv4Addr::LOCALHOST, port))?;
    stream.set_read_timeout(Some(PULL_WAIT))?;
    let request =
        format!("GET /standin HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nConnection: close\r\n\r\n");
    // As in `pull`, the read names a close.
    let _ = (&stream).write_all(request.as_bytes());
    let mut status = String::new();
    BufReader::new(&stream).read_line(&mut status)?;
    Ok(status.split(' ').nth(1).map(str::to_owned))
}

/// Mode `markers`: each marker file holds its own name and a LF.
fn markers() -> io::Result<()> {
    for marker in MARKERS {
        fs::write(
            Path::new(SESSION_DISK_MOUNT).join(marker),
            format!("{marker}\n"),
        )?;
    }
    Ok(())
}
