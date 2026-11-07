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
//!    rule 16). Then it stays up. A line `pull` first starts a child of
//!    itself, the family stranger, and waits for it. The stranger sends
//!    one `FamilyStatePull` on `creche_wire::guest::FAMILY_STATE_SOCKET`
//!    and writes `family stranger pid <its pid> <answer>`. Guest init
//!    closes its link, so the answer is `closed` (decision CH62 rule 13).
//!    Then the stand-in pulls once itself: one `FamilyStatePull` on that
//!    socket, then one HTTP GET to guest 127.0.0.1:4000 and one to
//!    :8300. It answers `pulled <its pid> <reply key> <status 4000>
//!    <status 8300> <pull ms>`. A link that ends with no answer gives
//!    `closed`, and each other fault `failed` (decisions CH5a, CH6-2,
//!    CH62 rule 13).
//! 9. In mode `markers` it writes `MARKERS` files on the session disk,
//!    each with its own line, and writes `markers written`. Then it stays
//!    up. A fault exits with status 4. A kvm test syncs and kills CH,
//!    then reads them from the disk (decision CH62 rule 23).
//! 10. In mode `linked-relay` it opens the local link, so its state
//!     becomes `running`, and runs the relay of mode `relay` on a thread.
//!     Then it waits for the stop line on the local link, and it exits
//!     with status 0 on it, as mode `turn` does. A fault exits with
//!     status 4. The phase 3 proof of `creche-vmm/tests/kvm_guest.rs`
//!     runs it: the sleep gate snapshots only a `running` facade
//!     (decision CH32 rule 6).
//! 11. In mode `reach` it opens the local link, as mode `linked-relay`
//!     does, so its state becomes `running` (decision CH32 rule 6).
//!     Then it listens on the relay socket with no delay and writes
//!     `relay listening`. Each line of a 1201 link is one action on a
//!     guest TCP stream, and the stand-in answers it with one line. A
//!     kvm test thus sets each request itself: the 3128 and 9418 rows
//!     here (decision CH46 rules 1 and 4), the proxy and mirror tests of
//!     the vmm (decision CH69 rules 22 and 28).
//!     - `dial <id> <port> <hex>` opens TCP to guest 127.0.0.1:<port>,
//!       writes the bytes and reads the reply.
//!     - `send <id> <hex>` writes the bytes on the open stream `<id>`
//!       and reads the reply.
//!     - `close <id>` closes the stream `<id>` and answers `closed
//!       <id>`.
//!
//!     A read stops at the first LF, at `REACH_CAP` (4 KiB), at the
//!     peer close or after `REACH_WAIT` (5 s) with no byte. The bytes
//!     after the first LF stay buffered, and the next `send` on the
//!     stream reads them first. So a reply head of n lines takes n
//!     reads, and `send <id> -` reads the next line with no write. The
//!     answer is `reply <id> <hex read> <open|closed>`. A reset counts as the
//!     peer close. A failed dial, write or read answers `failed <id>
//!     <io error kind>`. A `dial` of an open id answers `failed <id>
//!     AlreadyExists`, and a line of no known form answers `failed <its
//!     second word, or -> InvalidInput`. A stream that closes or fails
//!     leaves the link, so a later `send` on its id answers `failed
//!     <id> NotFound`. `-` stands for no bytes, in a line and in an
//!     answer. Lowercase hex goes out, and either case comes in. Each
//!     stream belongs to its 1201 link, and the end of the link closes
//!     them all. The main thread waits for the stop line on the local
//!     link and exits with status 0 on it, as mode `turn` does.

use std::collections::HashMap;
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

/// The argument that makes the stand-in the family stranger of a `pull`.
const FAMILY_STRANGER: &str = "family-stranger";

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

/// The bytes of one read of mode `reach` at most.
const REACH_CAP: u64 = 4096;

/// How long one read of mode `reach` waits for the next byte.
const REACH_WAIT: Duration = Duration::from_secs(5);

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
        Some(FAMILY_STRANGER) => family_stranger(),
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
        Ok(mode) if mode.trim_end() == "linked-relay" => {
            return linked(RELAY_DELAY, pong).unwrap_or_else(|error| {
                say(&format!("linked relay failed: {error}"));
                ExitCode::from(TURN_FAILED)
            });
        }
        Ok(mode) if mode.trim_end() == "reach" => {
            return linked(Duration::ZERO, reach).unwrap_or_else(|error| {
                say(&format!("reach failed: {error}"));
                ExitCode::from(TURN_FAILED)
            });
        }
        Ok(mode) if mode.trim_end() == "relay" => {
            thread::spawn(|| {
                let Err(error) = relay(RELAY_DELAY, pong);
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

/// Writes one `standin: <text>` line on stderr, in one `write_all`, so a
/// guest-init line on the same console cannot land inside it.
fn say(text: &str) {
    let _ = io::stderr().write_all(format!("standin: {text}\n").as_bytes());
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
    stop_line(&link)
}

/// Reads one line of the local link `link`: status 0 for the stop line,
/// else status 4.
fn stop_line(link: &UnixStream) -> io::Result<ExitCode> {
    let mut line = String::new();
    BufReader::new(link).read_line(&mut line)?;
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

/// The modes `linked-relay` and `reach`: the local link, the relay with
/// `delay` and `answer` on a thread, then the wait for the stop line.
fn linked(delay: Duration, answer: Answer) -> io::Result<ExitCode> {
    let link = UnixStream::connect(LOCAL_LINK_SOCKET)?;
    say("link open");
    thread::spawn(move || {
        let Err(error) = relay(delay, answer);
        say(&format!("relay failed: {error}"));
    });
    say("up");
    stop_line(&link)
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

/// The answer of each line of one relay link, until its end.
type Answer = fn(&UnixStream) -> io::Result<()>;

/// The relay socket after `delay`, and one thread per link that runs
/// `answer`. It returns only the error that ends the listener.
fn relay(delay: Duration, answer: Answer) -> io::Result<Infallible> {
    thread::sleep(delay);
    let path = Path::new(RELAY_SOCKET);
    if let Some(dir) = path.parent() {
        fs::create_dir_all(dir)?;
    }
    let listener = UnixListener::bind(path)?;
    say("relay listening");
    loop {
        let (link, _) = listener.accept()?;
        thread::spawn(move || {
            if let Err(error) = answer(&link) {
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
            let status = Command::new(std::env::current_exe()?)
                .arg(FAMILY_STRANGER)
                .status()?;
            say(&format!("family stranger {status}"));
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

/// The family stranger of a `pull`: a process other than the facade
/// sends a pull on the family-state socket. Guest init closes the link at
/// once, with no host dial (decision CH62 rule 13).
fn family_stranger() -> ExitCode {
    let word = answer(pull());
    say(&format!(
        "family stranger pid {} {word}",
        std::process::id()
    ));
    if word == "closed" {
        ExitCode::SUCCESS
    } else {
        ExitCode::from(CHILD_FAILED)
    }
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

/// Mode `reach`: answers each line of `link` with one line, until its
/// end. The streams of the link close with it.
fn reach(link: &UnixStream) -> io::Result<()> {
    let mut streams = HashMap::new();
    for line in BufReader::new(link).lines() {
        let line = line?;
        writeln!(&*link, "{}", act(&mut streams, &line))?;
    }
    say("reach link ended");
    Ok(())
}

/// The answer of one line of mode `reach`. A stream that the answer
/// names closed leaves `streams`.
fn act(streams: &mut HashMap<String, BufReader<TcpStream>>, line: &str) -> String {
    let words: Vec<&str> = line.split(' ').collect();
    let id = words.get(1).copied().unwrap_or("-");
    let done = match words.as_slice() {
        ["dial", id, port, bytes] if !streams.contains_key(*id) => {
            dial(port, bytes).map(|stream| ((*id).to_owned(), stream))
        }
        ["dial", _, _, _] => Err(io::Error::from(io::ErrorKind::AlreadyExists)),
        ["send", id, bytes] => match streams.remove(*id) {
            Some(stream) => unhex(bytes)
                .and_then(|bytes| stream.get_ref().write_all(&bytes))
                .map(|()| ((*id).to_owned(), stream)),
            None => Err(io::Error::from(io::ErrorKind::NotFound)),
        },
        ["close", id] => {
            return match streams.remove(*id) {
                Some(_) => format!("closed {id}"),
                None => format!("failed {id} {:?}", io::ErrorKind::NotFound),
            };
        }
        _ => Err(io::Error::from(io::ErrorKind::InvalidInput)),
    };
    let (id, mut stream) = match done {
        Ok(done) => done,
        Err(error) => return format!("failed {id} {:?}", error.kind()),
    };
    match read_reply(&mut stream) {
        Ok((bytes, true)) => {
            let answer = format!("reply {id} {} open", hex(&bytes));
            streams.insert(id, stream);
            answer
        }
        Ok((bytes, false)) => format!("reply {id} {} closed", hex(&bytes)),
        Err(error) => format!("failed {id} {:?}", error.kind()),
    }
}

/// Opens TCP to guest 127.0.0.1:`port` and writes `bytes`, in hex.
fn dial(port: &str, bytes: &str) -> io::Result<BufReader<TcpStream>> {
    let port: u16 = port
        .parse()
        .map_err(|_| io::Error::from(io::ErrorKind::InvalidInput))?;
    let bytes = unhex(bytes)?;
    let stream = TcpStream::connect((Ipv4Addr::LOCALHOST, port))?;
    stream.set_read_timeout(Some(REACH_WAIT))?;
    (&stream).write_all(&bytes)?;
    Ok(BufReader::new(stream))
}

/// One read of mode `reach`: the bytes up to the first LF, `REACH_CAP`,
/// the peer close or `REACH_WAIT` with no byte, and whether the stream
/// is still open. A reset is the peer close. The bytes after the first
/// LF stay in `stream` for the next read.
fn read_reply(stream: &mut BufReader<TcpStream>) -> io::Result<(Vec<u8>, bool)> {
    let mut bytes = Vec::new();
    match stream.take(REACH_CAP).read_until(b'\n', &mut bytes) {
        Ok(_) => {
            let open = bytes.ends_with(b"\n") || u64::try_from(bytes.len()) == Ok(REACH_CAP);
            Ok((bytes, open))
        }
        // The bytes before the fault stay in `bytes`.
        Err(error)
            if matches!(
                error.kind(),
                io::ErrorKind::WouldBlock | io::ErrorKind::TimedOut
            ) =>
        {
            Ok((bytes, true))
        }
        Err(error) if error.kind() == io::ErrorKind::ConnectionReset => Ok((bytes, false)),
        Err(error) => Err(error),
    }
}

/// `bytes` in lowercase hex, or `-` for no bytes.
fn hex(bytes: &[u8]) -> String {
    if bytes.is_empty() {
        return "-".to_owned();
    }
    bytes.iter().map(|byte| format!("{byte:02x}")).collect()
}

/// The bytes of the hex word `word`, or none for `-`.
fn unhex(word: &str) -> io::Result<Vec<u8>> {
    if word == "-" {
        return Ok(Vec::new());
    }
    let bad = || io::Error::from(io::ErrorKind::InvalidInput);
    if word.is_empty() || !word.len().is_multiple_of(2) {
        return Err(bad());
    }
    word.as_bytes()
        .chunks(2)
        .map(|pair| {
            std::str::from_utf8(pair)
                .ok()
                .and_then(|pair| u8::from_str_radix(pair, 16).ok())
                .ok_or_else(bad)
        })
        .collect()
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

/// The answers of mode `reach` over loopback TCP, with no VM. The kvm
/// tests prove the same lines through guest init.
#[cfg(test)]
mod tests {
    use std::collections::HashMap;
    use std::io::{Read as _, Write as _};
    use std::net::{Ipv4Addr, TcpListener};
    use std::thread;
    use std::time::Instant;

    use super::{REACH_CAP, REACH_WAIT, act, unhex};

    /// A loopback listener and its port.
    fn listen() -> (TcpListener, u16) {
        let listener = TcpListener::bind((Ipv4Addr::LOCALHOST, 0)).unwrap();
        let port = listener.local_addr().unwrap().port();
        (listener, port)
    }

    /// A line that names no open stream, or holds bad hex, fails with
    /// no dial.
    #[test]
    fn reach_refuses_an_unknown_id_and_bad_hex() {
        let mut streams = HashMap::new();
        assert_eq!(act(&mut streams, "close z"), "failed z NotFound");
        assert_eq!(act(&mut streams, "send z 00"), "failed z NotFound");
        assert_eq!(act(&mut streams, "dial x 1 0"), "failed x InvalidInput");
        assert_eq!(act(&mut streams, "dial x 1 0g"), "failed x InvalidInput");
        assert_eq!(act(&mut streams, "dial x port -"), "failed x InvalidInput");
        assert_eq!(act(&mut streams, "nod x"), "failed x InvalidInput");
        assert_eq!(act(&mut streams, ""), "failed - InvalidInput");
        assert!(unhex("0").is_err());
        assert!(unhex("").is_err());
        assert_eq!(unhex("Ab0f").unwrap(), [0xab, 0x0f]);
        assert_eq!(unhex("-").unwrap(), b"");
    }

    /// Each read ends at the first LF. The next line stays buffered, so
    /// `send <id> -` reads it at once. A second `dial` of an open id
    /// fails, and `close` ends the stream.
    #[test]
    fn reach_reads_one_line_per_action() {
        let (listener, port) = listen();
        let peer = thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            let mut request = [0; 2];
            stream.read_exact(&mut request).unwrap();
            stream.write_all(b"a\r\nb\n").unwrap();
            let mut rest = Vec::new();
            stream.read_to_end(&mut rest).unwrap();
            (request, rest)
        });
        let mut streams = HashMap::new();
        assert_eq!(
            act(&mut streams, &format!("dial p {port} 0a0b")),
            "reply p 610d0a open"
        );
        let start = Instant::now();
        assert_eq!(act(&mut streams, "send p -"), "reply p 620a open");
        assert!(start.elapsed() < REACH_WAIT);
        assert_eq!(
            act(&mut streams, &format!("dial p {port} -")),
            "failed p AlreadyExists"
        );
        assert_eq!(act(&mut streams, "close p"), "closed p");
        assert_eq!(act(&mut streams, "send p 00"), "failed p NotFound");
        assert_eq!(peer.join().unwrap(), ([0x0a, 0x0b], Vec::new()));
    }

    /// A read with no byte ends after `REACH_WAIT`, and the stream stays
    /// open.
    #[test]
    fn reach_reads_a_silent_peer_as_open() {
        let (listener, port) = listen();
        let mut streams = HashMap::new();
        let start = Instant::now();
        assert_eq!(
            act(&mut streams, &format!("dial s {port} -")),
            "reply s - open"
        );
        assert!(start.elapsed() >= REACH_WAIT);
        assert_eq!(act(&mut streams, "close s"), "closed s");
        drop(listener);
    }

    /// A read ends at `REACH_CAP` with the stream open. The next read
    /// gets the rest and the peer close, so the stream leaves the link.
    #[test]
    fn reach_caps_a_read_and_reads_the_close() {
        let (listener, port) = listen();
        let peer = thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            stream.write_all(&[b'a'; 5000]).unwrap();
        });
        let mut streams = HashMap::new();
        let cap = usize::try_from(REACH_CAP).unwrap();
        assert_eq!(
            act(&mut streams, &format!("dial c {port} -")),
            format!("reply c {} open", "61".repeat(cap))
        );
        peer.join().unwrap();
        assert_eq!(
            act(&mut streams, "send c -"),
            format!("reply c {} closed", "61".repeat(5000 - cap))
        );
        assert_eq!(act(&mut streams, "send c -"), "failed c NotFound");
    }

    /// A reset reads as the peer close. The peer closes with an unread
    /// byte, so its kernel sends a reset, not a FIN.
    #[test]
    fn reach_reads_a_reset_as_closed() {
        let (listener, port) = listen();
        let peer = thread::spawn(move || {
            let (stream, _) = listener.accept().unwrap();
            stream.peek(&mut [0]).unwrap();
        });
        let mut streams = HashMap::new();
        let start = Instant::now();
        let answer = act(&mut streams, &format!("dial r {port} 00"));
        peer.join().unwrap();
        assert_eq!(answer, "reply r - closed");
        assert!(start.elapsed() < REACH_WAIT);
        assert_eq!(act(&mut streams, "send r -"), "failed r NotFound");
    }
}
