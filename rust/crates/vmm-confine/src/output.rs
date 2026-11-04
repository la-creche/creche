//! The stdout and stderr of each Cloud Hypervisor process (decision CH50
//! rule 12).
//!
//! The spawner gives the launch step one pipe for both fds in place of the
//! fds of the vmm. `systemd-run --scope` and CH keep them, so every line
//! that the launch step, `systemd-run` or CH writes reaches the vmm side of
//! the pipe. One reader thread per VM reads the lines and gives each one to
//! the log sink, tagged with the session id:
//!
//! - `ch <id>: <line>` for a whole line.
//! - `ch <id> cut: <line>` for a line longer than `LINE_CAP` bytes. The
//!   sink gets the first `LINE_CAP` bytes, and the reader drops the rest.
//! - `ch <id> dropped <n> lines` once a window ends in which more than
//!   `RATE_LINES` lines came, and at the end of the pipe.
//!
//! The tag comes before any byte from CH, and the reader escapes each
//! control char, so CH cannot write a line that looks like a vmm line.
//! AppArmor does not check a pipe, so the profile needs no rule for it.
//! The reader keeps reading while it drops lines, so a full pipe never
//! blocks CH.
//!
//! The caps are decision CH52 rule 6: `LINE_CAP` (4 KiB per line), and
//! `RATE_LINES` (100) lines per `RATE_WINDOW` (10 s) per VM.

use std::fmt::Write as _;
use std::io::{self, BufRead, BufReader, PipeReader, Read, Write};
use std::sync::Arc;
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};

use creche_wire::SessionId;

/// The most bytes of one line that reach the sink.
pub const LINE_CAP: usize = 4096;

/// The most lines that reach the sink in one window.
pub const RATE_LINES: u32 = 100;

/// The length of one rate window.
pub const RATE_WINDOW: Duration = Duration::from_secs(10);

/// Where the tagged lines go. The vmm uses `stderr_sink`.
pub type Sink = Arc<dyn Fn(&str) + Send + Sync>;

/// The sink of the vmm: each line goes to the stderr of the vmm with the
/// prefix `vmm: `, as the other lines of the vmm do. Under
/// `creche-vmm.service` that is the journal. A failed write drops the
/// line, because the vmm has no other place to report it.
#[must_use]
pub fn stderr_sink() -> Sink {
    Arc::new(|line| {
        // A failed write loses this line. The next line tries again.
        let _ = writeln!(io::stderr().lock(), "vmm: {line}");
    })
}

/// Starts the reader thread of the VM of `id` on `pipe`. The thread ends
/// at the end of the pipe, when CH and every process that holds the write
/// end have exited.
///
/// # Errors
///
/// The thread did not start.
pub fn start(id: SessionId, pipe: PipeReader, sink: Sink) -> io::Result<JoinHandle<()>> {
    thread::Builder::new()
        .name("vmm-ch-output".to_owned())
        .spawn(move || {
            if let Err(error) = forward(&id, pipe, &mut |line| sink(line)) {
                sink(&format!("ch {id} output read failed: {}", error.kind()));
            }
        })
}

/// Reads `pipe` to its end and gives each tagged line to `sink`, under the
/// byte cap and the rate cap.
///
/// # Errors
///
/// A read failed. The lines before it reached the sink.
pub fn forward(id: &SessionId, pipe: impl Read, sink: &mut dyn FnMut(&str)) -> io::Result<()> {
    let mut reader = BufReader::with_capacity(LINE_CAP, pipe);
    let mut gate = Gate::new(Instant::now());
    let mut line: Vec<u8> = Vec::with_capacity(LINE_CAP);
    let mut cut = false;
    let result = loop {
        let chunk = match reader.fill_buf() {
            Ok(chunk) => chunk,
            Err(error) if error.kind() == io::ErrorKind::Interrupted => continue,
            Err(error) => break Err(error),
        };
        if chunk.is_empty() {
            if !line.is_empty() || cut {
                emit(id, &line, cut, &mut gate, sink);
            }
            break Ok(());
        }
        let newline = chunk.iter().position(|&byte| byte == b'\n');
        let take = newline.unwrap_or(chunk.len());
        let room = LINE_CAP.saturating_sub(line.len());
        cut |= take > room;
        line.extend(chunk.iter().take(take.min(room)));
        reader.consume(take + usize::from(newline.is_some()));
        if newline.is_some() {
            emit(id, &line, cut, &mut gate, sink);
            line.clear();
            cut = false;
        }
    };
    let dropped = gate.take_dropped();
    if dropped > 0 {
        sink(&format!("ch {id} dropped {dropped} lines"));
    }
    result
}

/// Gives one line to `sink` if the gate lets it through, after the drop
/// note of the window before.
fn emit(id: &SessionId, line: &[u8], cut: bool, gate: &mut Gate, sink: &mut dyn FnMut(&str)) {
    let Verdict::Pass { dropped } = gate.admit(Instant::now()) else {
        return;
    };
    if dropped > 0 {
        sink(&format!("ch {id} dropped {dropped} lines"));
    }
    let tag = if cut { " cut" } else { "" };
    sink(&format!("ch {id}{tag}: {}", escaped(line)));
}

/// The text of `line` with each control char escaped. Bytes that are not
/// UTF-8 become U+FFFD.
fn escaped(line: &[u8]) -> String {
    let mut text = String::with_capacity(line.len());
    for letter in String::from_utf8_lossy(line).chars() {
        if letter.is_control() {
            // A write into a String does not fail.
            let _ = write!(text, "{}", letter.escape_default());
        } else {
            text.push(letter);
        }
    }
    text
}

/// The verdict of the rate gate on one line.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum Verdict {
    /// The line goes to the sink. `dropped` lines of the window before did
    /// not.
    Pass { dropped: u64 },
    /// The window has had `RATE_LINES` lines, so the line is dropped.
    Drop,
}

/// The rate cap: at most `RATE_LINES` lines in each `RATE_WINDOW`. A
/// window starts at the first line after the window before ends.
#[derive(Debug)]
pub(crate) struct Gate {
    window: Instant,
    passed: u32,
    dropped: u64,
}

impl Gate {
    /// A gate whose first window starts at `now`.
    pub(crate) fn new(now: Instant) -> Self {
        Self {
            window: now,
            passed: 0,
            dropped: 0,
        }
    }

    /// The verdict on a line that comes at `now`.
    pub(crate) fn admit(&mut self, now: Instant) -> Verdict {
        if now.saturating_duration_since(self.window) >= RATE_WINDOW {
            self.window = now;
            self.passed = 1;
            return Verdict::Pass {
                dropped: self.take_dropped(),
            };
        }
        if self.passed < RATE_LINES {
            self.passed += 1;
            Verdict::Pass { dropped: 0 }
        } else {
            self.dropped += 1;
            Verdict::Drop
        }
    }

    /// The lines dropped since the last note, which the caller reports.
    pub(crate) fn take_dropped(&mut self) -> u64 {
        std::mem::take(&mut self.dropped)
    }
}

#[cfg(test)]
mod tests {
    use std::time::Instant;

    use super::{Gate, RATE_LINES, RATE_WINDOW, Verdict, escaped};

    #[test]
    fn the_gate_drops_lines_over_the_cap_until_the_window_ends() {
        let start = Instant::now();
        let mut gate = Gate::new(start);
        for _ in 0..RATE_LINES {
            assert_eq!(gate.admit(start), Verdict::Pass { dropped: 0 });
        }
        assert_eq!(gate.admit(start), Verdict::Drop);
        assert_eq!(gate.admit(start + RATE_WINDOW / 2), Verdict::Drop);
        assert_eq!(
            gate.admit(start + RATE_WINDOW),
            Verdict::Pass { dropped: 2 }
        );
        // The new window counts the line that opened it.
        for _ in 1..RATE_LINES {
            assert_eq!(
                gate.admit(start + RATE_WINDOW),
                Verdict::Pass { dropped: 0 }
            );
        }
        assert_eq!(gate.admit(start + RATE_WINDOW), Verdict::Drop);
        assert_eq!(gate.take_dropped(), 1);
        assert_eq!(gate.take_dropped(), 0);
    }

    #[test]
    fn a_clock_that_goes_back_stays_in_the_window() {
        let start = Instant::now() + RATE_WINDOW;
        let mut gate = Gate::new(start);
        for _ in 0..RATE_LINES {
            gate.admit(start);
        }
        assert_eq!(gate.admit(start - RATE_WINDOW), Verdict::Drop);
    }

    #[test]
    fn control_chars_are_escaped() {
        assert_eq!(escaped(b"a\rb\x1b[0m\tc"), "a\\rb\\u{1b}[0m\\tc");
        assert_eq!(escaped(b"ok \xff"), "ok \u{fffd}");
    }
}
