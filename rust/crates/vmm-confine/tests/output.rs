//! The read of CH's stdout and stderr (decision CH50 rule 12): the tag,
//! the byte cap per line, the rate cap and the escape of control chars.

use std::io::{self, Read};
use std::sync::{Arc, mpsc};
use std::time::Duration;

use creche_vmm_confine::output::{self, LINE_CAP, RATE_LINES, Sink, forward};
use creche_wire::SessionId;

const ID: &str = "0123abcd-0000-4000-8000-00000000c5c5";

// Each helper with `cfg(test)` may unwrap (rust/clippy.toml).

/// The lines that `forward` gives the sink for `bytes`.
#[cfg(test)]
fn lines(bytes: &[u8]) -> Vec<String> {
    let id: SessionId = ID.parse().unwrap();
    let mut lines = Vec::new();
    forward(&id, bytes, &mut |line| lines.push(line.to_owned())).unwrap();
    lines
}

#[test]
fn each_line_is_tagged_with_the_session_id() {
    assert_eq!(
        lines(b"first\nsecond\n"),
        [format!("ch {ID}: first"), format!("ch {ID}: second")]
    );
}

#[test]
fn a_last_line_with_no_newline_still_reaches_the_sink() {
    assert_eq!(lines(b"no newline"), [format!("ch {ID}: no newline")]);
}

#[test]
fn an_empty_pipe_gives_no_line() {
    assert!(lines(b"").is_empty());
}

#[test]
fn a_long_line_is_cut_at_the_cap_and_the_next_line_is_whole() {
    let mut bytes = vec![b'x'; LINE_CAP * 3];
    bytes.extend_from_slice(b"\nnext\n");
    let got = lines(&bytes);
    assert_eq!(got.len(), 2, "{got:?}");
    assert_eq!(got[0], format!("ch {ID} cut: {}", "x".repeat(LINE_CAP)));
    assert_eq!(got[1], format!("ch {ID}: next"));
}

#[test]
fn a_line_of_exactly_the_cap_is_not_cut() {
    let mut bytes = vec![b'y'; LINE_CAP];
    bytes.push(b'\n');
    assert_eq!(
        lines(&bytes),
        [format!("ch {ID}: {}", "y".repeat(LINE_CAP))]
    );
}

#[test]
fn lines_over_the_rate_cap_are_dropped_and_counted() {
    let extra = 7;
    let text: String = (0..RATE_LINES + extra)
        .map(|n| format!("line {n}\n"))
        .collect();
    let got = lines(text.as_bytes());
    let count = usize::try_from(RATE_LINES).unwrap();
    assert_eq!(got.len(), count + 1, "{got:?}");
    assert_eq!(got[count - 1], format!("ch {ID}: line {}", RATE_LINES - 1));
    assert_eq!(got[count], format!("ch {ID} dropped {extra} lines"));
}

#[test]
fn a_line_cannot_pose_as_a_vmm_line() {
    assert_eq!(
        lines(b"x\rvmm: fake\x1b[2K\n"),
        [format!("ch {ID}: x\\rvmm: fake\\u{{1b}}[2K")]
    );
}

/// A pipe end that fails its second read.
struct Failing(u8);

impl Read for Failing {
    fn read(&mut self, buf: &mut [u8]) -> io::Result<usize> {
        self.0 += 1;
        if self.0 > 1 {
            return Err(io::Error::other("gone"));
        }
        let mut line: &[u8] = b"before\n";
        line.read(buf)
    }
}

#[test]
fn a_failed_read_keeps_the_lines_before_it() {
    let id: SessionId = ID.parse().unwrap();
    let mut got = Vec::new();
    let result = forward(&id, Failing(0), &mut |line| got.push(line.to_owned()));
    assert!(result.is_err());
    assert_eq!(got, [format!("ch {ID}: before")]);
}

#[test]
fn the_reader_thread_forwards_a_pipe_until_its_writer_closes() {
    let (sender, lines) = mpsc::channel();
    let sink: Sink = Arc::new(move |line| sender.send(line.to_owned()).unwrap());
    let (reader, mut writer) = io::pipe().unwrap();
    let thread = output::start(ID.parse().unwrap(), reader, sink).unwrap();
    io::Write::write_all(&mut writer, b"from ch\n").unwrap();
    let line = lines.recv_timeout(Duration::from_secs(5)).unwrap();
    assert_eq!(line, format!("ch {ID}: from ch"));
    drop(writer);
    thread.join().unwrap();
}
