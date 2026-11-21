//! The control lines of version 2 of the attach exchange, as the signer
//! reads them (the runner design at commit 131ce43, section 6.2). This
//! module is pure: it reads bytes and gives typed values.
//!
//! ```json
//! {"v":2,"op":"open","session":"<run key>","slot":<n>,"port":<P>,"facts":{...}}
//! {"v":2,"op":"conn","session":"<run key>","port":<P>}
//! {"v":2,"op":"close","session":"<run key>"}
//! ```
//!
//! Each line is one strict JSON object of at most `LINE_MAX` bytes with
//! its LF: each key required, no unknown or repeated key, `v` the integer
//! 2. The signer takes only the repository and the run id from the facts,
//! as the key of its own read (section 4.4 of the note). Each other fact
//! must have its form, and the signer trusts none of them.

use std::fmt;

use creche_ci_signer::eligibility::RunKey;
use creche_ci_signer::names::{RepoFullName, RunId};
use serde::Deserialize;

/// The most bytes of one line, with its LF (the runner design, section
/// 6.2, rule 3).
pub const LINE_MAX: usize = 4 * 1024;

/// The version of the exchange.
const VERSION: u64 = 2;

/// The answer to an `open` line that the signer accepts.
pub const ACCEPT: &[u8] = b"{\"v\":2,\"op\":\"open\",\"accept\":true}\n";

/// The answer to an `open` line that the signer refuses.
pub const REFUSE: &[u8] = b"{\"v\":2,\"op\":\"open\",\"accept\":false}\n";

/// The largest id of a job in an `open` line: 2^53 - 1.
const ID_MAX: u64 = (1 << 53) - 1;

/// The most bytes of one `Word`.
const WORD_MAX: usize = 255;

/// Why a line is refused. It holds no byte of the line.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum LineFault {
    /// The line passes `LINE_MAX` bytes with its LF.
    TooLong,
    /// The line is no strict JSON object.
    NotStrict,
    /// A key is missing, unknown or repeated, or a value has the wrong
    /// type.
    Shape,
    /// `v` is not 2.
    Version,
    /// `op` is not the op of this kind of line.
    Op,
    /// A text is no `Word`.
    Word,
    /// An id is 0 or above 2^53 - 1.
    Id,
    /// The port is 0.
    Port,
    /// The repository is no `owner/name` of the names of the core.
    Repository,
}

impl LineFault {
    /// A stable word for the log.
    pub fn code(self) -> &'static str {
        match self {
            Self::TooLong => "too_long",
            Self::NotStrict => "not_strict",
            Self::Shape => "shape",
            Self::Version => "version",
            Self::Op => "op",
            Self::Word => "word",
            Self::Id => "id",
            Self::Port => "port",
            Self::Repository => "repository",
        }
    }
}

impl fmt::Display for LineFault {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(formatter, "the control line is refused: {}", self.code())
    }
}

impl std::error::Error for LineFault {}

/// A text of a line: 1 to 255 bytes, each a printable ASCII byte but the
/// space. So it prints as one word of a log line.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Word(String);

impl Word {
    fn new(text: String) -> Result<Self, LineFault> {
        let printable = text.bytes().all(|byte| byte.is_ascii_graphic());
        if text.is_empty() || text.len() > WORD_MAX || !printable {
            return Err(LineFault::Word);
        }
        Ok(Self(text))
    }
}

impl fmt::Display for Word {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(&self.0)
    }
}

/// A vsock port: 1 or more.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Port(u32);

impl Port {
    fn new(port: u32) -> Result<Self, LineFault> {
        if port == 0 {
            return Err(LineFault::Port);
        }
        Ok(Self(port))
    }
}

impl fmt::Display for Port {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(formatter, "{}", self.0)
    }
}

/// An `open` line: the session, the slot, the port of the row and the key
/// of the signer's own read.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Open {
    session: Word,
    slot: u8,
    port: Port,
    key: RunKey,
}

impl Open {
    /// The session: the run key of the runner. The signer only matches the
    /// later lines of the control connection to it (section 4.4).
    pub fn session(&self) -> &Word {
        &self.session
    }

    /// The slot of the job. Each passed fd is checked against it, and the
    /// slot never comes from the peer.
    pub const fn slot(&self) -> u8 {
        self.slot
    }

    /// The vsock port of the row.
    pub const fn port(&self) -> Port {
        self.port
    }

    /// The repository and the run id of the facts.
    pub fn key(&self) -> &RunKey {
        &self.key
    }
}

impl fmt::Display for Open {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            formatter,
            "session={} slot={} port={} repository={} run={}",
            self.session, self.slot, self.port, self.key.repository, self.key.run
        )
    }
}

/// A line of the daemon after the answer.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Control {
    /// A `conn` line: the fd of one guest connection comes with it.
    Conn {
        /// The session.
        session: Word,
        /// The port of the connection.
        port: Port,
    },
    /// A `close` line: the run ends.
    Close {
        /// The session.
        session: Word,
    },
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct FactsWire {
    repository: String,
    default_branch: String,
    workflow: String,
    event: String,
    #[serde(rename = "ref")]
    git_ref: String,
    commit: String,
    job: u64,
    run: u64,
    origin: String,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct OpenWire {
    v: u64,
    op: String,
    session: String,
    slot: u8,
    port: u32,
    facts: FactsWire,
}

#[derive(Deserialize)]
#[serde(tag = "op", deny_unknown_fields)]
enum ControlWire {
    #[serde(rename = "conn")]
    Conn { v: u64, session: String, port: u32 },
    #[serde(rename = "close")]
    Close { v: u64, session: String },
}

/// `line`, with no LF, read as the JSON object `T` after the strict check.
fn object<'a, T: Deserialize<'a>>(line: &'a [u8]) -> Result<T, LineFault> {
    if line.first() != Some(&b'{') || !creche_wire::strict(line) {
        return Err(LineFault::NotStrict);
    }
    serde_json::from_slice(line).map_err(|_| LineFault::Shape)
}

fn version(v: u64) -> Result<(), LineFault> {
    if v != VERSION {
        return Err(LineFault::Version);
    }
    Ok(())
}

/// Requires a word, and drops it: a fact that the signer does not use.
fn unused_word(text: String) -> Result<(), LineFault> {
    Word::new(text).map(drop)
}

/// Reads `line`, with no LF, as an `open` line.
///
/// # Errors
///
/// The `LineFault` of the first rule that the line breaks.
pub fn open(line: &[u8]) -> Result<Open, LineFault> {
    let wire: OpenWire = object(line)?;
    version(wire.v)?;
    if wire.op != "open" {
        return Err(LineFault::Op);
    }
    let facts = wire.facts;
    for text in [
        facts.default_branch,
        facts.workflow,
        facts.event,
        facts.git_ref,
        facts.commit,
        facts.origin,
    ] {
        unused_word(text)?;
    }
    if facts.job == 0 || facts.job > ID_MAX {
        return Err(LineFault::Id);
    }
    let run = RunId::try_from(facts.run).map_err(|_| LineFault::Id)?;
    let repository: RepoFullName = facts
        .repository
        .parse()
        .map_err(|_| LineFault::Repository)?;
    Ok(Open {
        session: Word::new(wire.session)?,
        slot: wire.slot,
        port: Port::new(wire.port)?,
        key: RunKey { repository, run },
    })
}

/// Reads `line`, with no LF, as a `conn` or a `close` line.
///
/// # Errors
///
/// The `LineFault` of the first rule that the line breaks. An `op` of
/// another word is a `Shape` fault.
pub fn control(line: &[u8]) -> Result<Control, LineFault> {
    match object(line)? {
        ControlWire::Conn { v, session, port } => {
            version(v)?;
            Ok(Control::Conn {
                session: Word::new(session)?,
                port: Port::new(port)?,
            })
        }
        ControlWire::Close { v, session } => {
            version(v)?;
            Ok(Control::Close {
                session: Word::new(session)?,
            })
        }
    }
}

/// The bytes of one stream cut into lines, each at most `LINE_MAX` bytes
/// with its LF.
#[derive(Debug, Default)]
pub struct Lines {
    buffer: Vec<u8>,
}

impl Lines {
    /// Adds `bytes` of the stream.
    pub fn push(&mut self, bytes: &[u8]) {
        self.buffer.extend_from_slice(bytes);
    }

    /// The next whole line with no LF, or `None` while the bytes hold no
    /// LF.
    ///
    /// # Errors
    ///
    /// `LineFault::TooLong` when the line passes `LINE_MAX` bytes.
    pub fn next_line(&mut self) -> Result<Option<Vec<u8>>, LineFault> {
        match self.buffer.iter().position(|byte| *byte == b'\n') {
            Some(end) if end < LINE_MAX => {
                let mut line: Vec<u8> = self.buffer.drain(..=end).collect();
                line.pop();
                Ok(Some(line))
            }
            Some(_) => Err(LineFault::TooLong),
            None if self.buffer.len() >= LINE_MAX => Err(LineFault::TooLong),
            None => Ok(None),
        }
    }
}

#[cfg(test)]
pub(crate) mod tests {
    use super::{ACCEPT, Control, LINE_MAX, LineFault, Lines, REFUSE, control, open};

    /// An `open` line of run 48 of the recordings, in the form of the
    /// runner design, section 6.2.
    pub(crate) const OPEN: &str = r#"{"v":2,"op":"open","session":"2f923f44-2d31-4d08-ad46-e04a543826e0","slot":4,"port":5000,"facts":{"repository":"example-owner/runner-test","default_branch":"main","workflow":"probe.yml","event":"push","ref":"refs/heads/main","commit":"5791a8cdae55087207f12ab919c5eb964494d654","job":42,"run":48,"origin":"http://192.0.2.10:3000"}}"#;

    #[test]
    fn an_open_line_gives_the_session_the_slot_the_port_and_the_key() {
        let open = open(OPEN.as_bytes()).unwrap();
        assert_eq!(open.slot(), 4);
        assert_eq!(open.port().to_string(), "5000");
        assert_eq!(
            open.session().to_string(),
            "2f923f44-2d31-4d08-ad46-e04a543826e0"
        );
        assert_eq!(open.key().run.get(), 48);
        assert_eq!(
            open.key().repository.to_string(),
            "example-owner/runner-test"
        );
    }

    #[test]
    fn an_open_line_that_breaks_a_rule_is_refused() {
        let cases = [
            (OPEN.replace("\"v\":2", "\"v\":1"), LineFault::Version),
            (
                OPEN.replace("\"op\":\"open\"", "\"op\":\"conn\""),
                LineFault::Op,
            ),
            (OPEN.replace("\"main\",", "\"ma in\","), LineFault::Word),
            (OPEN.replace("\"main\",", "\"\","), LineFault::Word),
            (OPEN.replace("\"job\":42", "\"job\":0"), LineFault::Id),
            (OPEN.replace("\"run\":48", "\"run\":0"), LineFault::Id),
            (
                OPEN.replace("\"run\":48", "\"run\":9007199254740992"),
                LineFault::Id,
            ),
            (OPEN.replace("\"port\":5000", "\"port\":0"), LineFault::Port),
            (
                OPEN.replace("example-owner/runner-test", "runner-test"),
                LineFault::Repository,
            ),
            (format!("[{OPEN}]"), LineFault::NotStrict),
            (
                OPEN.replace("\"slot\":4,", "\"slot\":4,\"slot\":4,"),
                LineFault::NotStrict,
            ),
        ];
        for (line, fault) in cases {
            assert_eq!(open(line.as_bytes()), Err(fault), "{line}");
        }
        for line in [
            OPEN.replace("\"slot\":4,", "\"slot\":4,\"extra\":1,"),
            OPEN.replace("\"slot\":4,", ""),
            OPEN.replace("\"origin\"", "\"host\""),
            OPEN.replace("\"slot\":4", "\"slot\":256"),
            OPEN.replace("\"run\":48", "\"run\":\"48\""),
        ] {
            assert_eq!(open(line.as_bytes()), Err(LineFault::Shape), "{line}");
        }
    }

    #[test]
    fn a_conn_and_a_close_line_are_read_and_another_line_is_refused() {
        let conn = br#"{"v":2,"op":"conn","session":"k1","port":5000}"#;
        let close = br#"{"v":2,"op":"close","session":"k1"}"#;
        assert!(matches!(
            control(conn),
            Ok(Control::Conn { session, port }) if session.to_string() == "k1" && port.to_string() == "5000"
        ));
        assert!(matches!(
            control(close),
            Ok(Control::Close { session }) if session.to_string() == "k1"
        ));
        assert_eq!(
            control(br#"{"v":1,"op":"close","session":"k1"}"#),
            Err(LineFault::Version)
        );
        for line in [
            &br#"{"v":2,"op":"open","session":"k1"}"#[..],
            br#"{"v":2,"op":"close","session":"k1","port":5000}"#,
            br#"{"v":2,"op":"conn","session":"k1"}"#,
            br#"{"v":2,"session":"k1"}"#,
        ] {
            assert_eq!(control(line), Err(LineFault::Shape));
        }
    }

    #[test]
    fn the_stream_gives_whole_lines_and_refuses_a_line_past_the_cap() {
        let mut lines = Lines::default();
        lines.push(b"ab\ncd");
        assert_eq!(lines.next_line().unwrap(), Some(b"ab".to_vec()));
        assert_eq!(lines.next_line().unwrap(), None);
        lines.push(b"\n");
        assert_eq!(lines.next_line().unwrap(), Some(b"cd".to_vec()));
        let mut long = Lines::default();
        long.push(&vec![b'x'; LINE_MAX]);
        assert_eq!(long.next_line(), Err(LineFault::TooLong));
        let mut fits = Lines::default();
        let mut line = vec![b'x'; LINE_MAX - 1];
        line.push(b'\n');
        fits.push(&line);
        assert_eq!(
            fits.next_line().unwrap().map(|line| line.len()),
            Some(LINE_MAX - 1)
        );
    }

    #[test]
    fn the_answers_are_of_version_2() {
        assert_eq!(ACCEPT, b"{\"v\":2,\"op\":\"open\",\"accept\":true}\n");
        assert_eq!(REFUSE, b"{\"v\":2,\"op\":\"open\",\"accept\":false}\n");
    }
}
