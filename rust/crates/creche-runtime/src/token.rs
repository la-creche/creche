//! A token file into a `Secret`, and the bearer of a request.
//!
//! Each service reads a token from a file and never from a variable or from
//! its arguments (invariant 13). The five Python readers apply different
//! rules to that file. A [`TokenRule`] holds the rules of one reader, and
//! this module has one constant for each reader:
//!
//! | Constant | The Python reader |
//! |---|---|
//! | [`TokenRule::ATTENDANCE`] | `attendance/src/attendance/auth.py:242-265` |
//! | [`TokenRule::ATTENDANCE_PEP_READ`] | `attendance/src/attendance/auth.py:268-279` |
//! | [`TokenRule::DOOR`] | `door-owui/src/agent_door_owui/config.py:117-146`, `chaperone/src/chaperone/delegate.py:141-162` |
//! | [`TokenRule::WEBHOOK`] | `door-trigger/src/agent_door_trigger/tokens.py:35-66` |
//! | [`TokenRule::NOT_EMPTY`] | `caregiver/src/caregiver/switch.py:80-94` |
//!
//! A service states what it does with a [`TokenError`]. At start it gives the
//! error to `service::refuse_start`. No error holds the token, a part of it
//! or the count of its bytes.
//!
//! [`bearer_of`] gives the bytes that a client offers in a request. A service
//! compares them with its token through `Secret::matches`.
//!
//! The vectors `runtime.token.*` and `runtime.bearer.*` hold what each Python
//! copy does. The test of this module walks each vector of each surface that
//! a function here ports, and each result is equal. Some differences from a
//! Python copy are in no vector. The doc comment of [`read`] and of
//! [`CachedToken::current`] names each one, and one plain test holds each
//! one. The exception is the device that `CachedToken::current` compares: no
//! test can move a file to another device.

use std::borrow::Cow;
use std::error::Error;
use std::fmt;
use std::io;
use std::path::{Path, PathBuf};

use ::http::HeaderMap;
use ::http::header::AUTHORIZATION;
use creche_contracts::config::{self, KeyError, MIN_KEY_BYTES};
use creche_contracts::secret::{Secret, SecretError};
use rustix::io::Errno;

use crate::readfile::{self, ByteCap, FileFacts, FileRead, Follow, ReadRefusal};

// CONTRACT-QUESTION: contract 02 §3 rule 7 gives a token a least count of
// bytes and no largest count. Each Python reader reads a token file of each
// size into memory. The reading here is a cap of 1 MiB, as each other file
// that a service reads has a cap. The token that `caregiver` mints for a
// webhook has 43 bytes (`caregiver/src/caregiver/webhook_tokens.py:64-67`).
// A larger cap costs one constant.
/// The largest token file that [`read`] takes: 1 MiB.
const FILE_CAP: ByteCap = ByteCap::ONE_MIB;

/// The six bytes that `bytes.strip` of Python removes: space, tab, line
/// feed, carriage return, vertical tab and form feed.
/// `u8::is_ascii_whitespace` does not hold for the vertical tab.
const ASCII_SPACE: [u8; 6] = *b" \t\n\r\x0b\x0c";

/// The permission bits of the group and of each other user.
const GROUP_AND_OTHER: u32 = 0o077;

/// The read bit of the group: the one bit that
/// [`ModeRule::OwnerAndGroupRead`] permits past the bits of the owner.
const GROUP_READ: u32 = 0o040;

// CONTRACT-QUESTION: contract 02 §3 rule 4 writes the header as
// `Authorization: Bearer <token>` and does not say if a service takes the
// scheme in another case of letters. `attendance`
// (`attendance/src/attendance/auth.py:234-239`), the Open WebUI door
// (`door-owui/src/agent_door_owui/app.py:158-172`) and the trigger listener
// (`door-trigger/src/agent_door_trigger/webhooks.py:165-170`) take only
// `Bearer` and one space. The chaperone
// (`chaperone/src/chaperone/app.py:350-355`) takes the scheme in each case.
// The reading here is the strictest copy: only `Bearer` and one space. This
// module is thus no port of the reader of the chaperone, and its test walks
// no vector of `runtime.bearer.chaperone`. Two vectors of that surface hold
// the scheme in another case of letters. A change costs one more value of
// `BearerTrim`, or the same rule in each Python copy.
/// The start of the header value that holds a bearer: the scheme and one
/// space. [`bearer_of`] reads it, and the client of `http::client` writes
/// it. The value has this one home.
pub(crate) const BEARER: &[u8] = b"Bearer ";

/// Who can read a token file, by its mode.
///
/// The set is closed.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ModeRule {
    /// `0600` or less: no bit for the group and no bit for each other user
    /// (contract 02 §3 rule 5).
    OwnerOnly,
    /// `0640` or less: the group can also read. The chaperone runs as
    /// another user and reads two token files of `attendance` (contract 02
    /// §3 rule 5, the exception).
    OwnerAndGroupRead,
    /// The reader does not check the mode.
    AnyMode,
}

impl fmt::Display for ModeRule {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Self::OwnerOnly => "0600",
            Self::OwnerAndGroupRead => "0600 or 0640",
            Self::AnyMode => "each mode",
        })
    }
}

impl ModeRule {
    /// Whether the rule refuses a file with the permission bits `mode`.
    ///
    /// The rule reads only the bits of the group and of each other user. It
    /// reads no bit of the owner and no setuid, setgid or sticky bit, as the
    /// Python checks do: `attendance/src/attendance/atomic.py:120-127` and
    /// `attendance/src/attendance/auth.py:268-279`.
    const fn refuses(self, mode: u32) -> bool {
        let wide = mode & GROUP_AND_OTHER;

        match self {
            Self::OwnerOnly => wide != 0,
            Self::OwnerAndGroupRead => wide & !GROUP_READ != 0,
            Self::AnyMode => false,
        }
    }
}

/// Which space a reader removes from the two ends of the content.
///
/// The set is closed.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Trim {
    /// The six ASCII bytes that `bytes.strip` of Python removes: space, tab,
    /// line feed, carriage return, vertical tab and form feed.
    AsciiSpace,
    /// Each character that `str.strip` of Python removes:
    /// `creche_contracts::config::python_strip`.
    PythonStrip,
}

/// Which bytes a token can hold.
///
/// The set is closed.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Encoding {
    /// Each byte.
    AnyBytes,
    /// Only UTF-8 text.
    Utf8,
}

/// The rules of one reader of a token file.
///
/// ```
/// use creche_runtime::token::{Encoding, ModeRule, TokenRule, Trim};
///
/// assert_eq!(TokenRule::ATTENDANCE.min_bytes(), 32);
/// assert_eq!(TokenRule::ATTENDANCE.mode(), ModeRule::OwnerOnly);
/// assert_eq!(TokenRule::ATTENDANCE.trim(), Trim::AsciiSpace);
/// assert_eq!(TokenRule::ATTENDANCE.encoding(), Encoding::AnyBytes);
/// assert_eq!(TokenRule::NOT_EMPTY.min_bytes(), 1);
/// ```
///
/// Code outside this module cannot build a rule. Each reader of the platform
/// has its constant, and a new reader gets a new constant here:
///
/// ```compile_fail,E0451
/// use creche_runtime::token::{Encoding, ModeRule, TokenRule, Trim};
///
/// let rule = TokenRule {
///     min_bytes: 0,
///     mode: ModeRule::AnyMode,
///     trim: Trim::AsciiSpace,
///     encoding: Encoding::AnyBytes,
/// };
/// ```
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct TokenRule {
    min_bytes: usize,
    mode: ModeRule,
    trim: Trim,
    encoding: Encoding,
}

impl TokenRule {
    /// A token of `attendance` that only `attendance` and its client read
    /// (`attendance/src/attendance/auth.py:242-265`): 32 bytes or more, mode
    /// `0600`, ASCII space removed, each byte.
    pub const ATTENDANCE: Self = Self {
        min_bytes: MIN_KEY_BYTES,
        mode: ModeRule::OwnerOnly,
        trim: Trim::AsciiSpace,
        encoding: Encoding::AnyBytes,
    };

    /// One of the two tokens of `attendance` that the chaperone reads
    /// (`attendance/src/attendance/auth.py:268-279`): as
    /// [`TokenRule::ATTENDANCE`], and the group can read the file.
    pub const ATTENDANCE_PEP_READ: Self = Self {
        min_bytes: MIN_KEY_BYTES,
        mode: ModeRule::OwnerAndGroupRead,
        trim: Trim::AsciiSpace,
        encoding: Encoding::AnyBytes,
    };

    // CONTRACT-QUESTION: contract 02 §3 rule 5 gives each token file a mode,
    // and `attendance` checks that mode when it reads the file. The Python
    // readers on the other side check no mode: a door for its key file and
    // for its token file, the chaperone for the token file of the delegate
    // door, and `caregiver` for its token file. Contract 04 §7.3 names no
    // mode check for the delegate door. The reading here is the reading of
    // those readers: `DOOR` and `NOT_EMPTY` have `ModeRule::AnyMode`. A mode
    // check costs one word in a constant. The reader then refuses a file
    // that the Python reader accepts.

    /// The key or the token of a door
    /// (`door-owui/src/agent_door_owui/config.py:117-146`,
    /// `chaperone/src/chaperone/delegate.py:141-162`): 32 bytes or more, no
    /// mode check, `str.strip` of Python, UTF-8.
    pub const DOOR: Self = Self {
        min_bytes: MIN_KEY_BYTES,
        mode: ModeRule::AnyMode,
        trim: Trim::PythonStrip,
        encoding: Encoding::Utf8,
    };

    /// The token of a webhook
    /// (`door-trigger/src/agent_door_trigger/tokens.py:35-66`): 32 bytes or
    /// more, mode `0600`, ASCII space removed, then UTF-8.
    pub const WEBHOOK: Self = Self {
        min_bytes: MIN_KEY_BYTES,
        mode: ModeRule::OwnerOnly,
        trim: Trim::AsciiSpace,
        encoding: Encoding::Utf8,
    };

    /// A token that only must not be empty
    /// (`caregiver/src/caregiver/switch.py:80-94`): 1 byte or more, no mode
    /// check, `str.strip` of Python, UTF-8.
    pub const NOT_EMPTY: Self = Self {
        min_bytes: 1,
        mode: ModeRule::AnyMode,
        trim: Trim::PythonStrip,
        encoding: Encoding::Utf8,
    };

    /// The smallest count of bytes of the token, after the trim.
    #[must_use]
    pub const fn min_bytes(self) -> usize {
        self.min_bytes
    }

    /// Who can read the file.
    #[must_use]
    pub const fn mode(self) -> ModeRule {
        self.mode
    }

    /// Which space the reader removes.
    #[must_use]
    pub const fn trim(self) -> Trim {
        self.trim
    }

    /// Which bytes the token can hold.
    #[must_use]
    pub const fn encoding(self) -> Encoding {
        self.encoding
    }
}

/// Why a token file gives no token.
///
/// No variant holds the token, a part of it or the count of its bytes. A
/// caller writes this error to a log.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum TokenError {
    /// The file is absent, the operating system refused the read, or the
    /// path is no regular file.
    Unreadable {
        /// The text of the error, as `strerror` of Python gives it. For a
        /// path that is no regular file, the text of
        /// `readfile::ReadRefusal::NotAFile`.
        os_text: String,
    },
    /// The file holds more than 1 MiB.
    TooLarge,
    /// The file holds no byte, or only space. [`TokenRule::DOOR`] does not
    /// give this error: it gives [`TokenError::TooShort`] for such a file, as
    /// each Python door does.
    Empty,
    /// The token has less bytes than the rule demands.
    TooShort {
        /// The smallest count of bytes of the rule.
        min_bytes: usize,
    },
    /// More users can read the file than the rule permits.
    ModeTooWide {
        /// The mode that the rule permits.
        allowed: ModeRule,
    },
    /// The content is not UTF-8, and the rule demands text.
    NotUtf8,
}

impl fmt::Display for TokenError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Unreadable { os_text } => write!(f, "the token file is not readable: {os_text}"),
            Self::TooLarge => f.write_str("the token file is too large"),
            Self::Empty => f.write_str("the token file is empty"),
            Self::TooShort { min_bytes } => {
                write!(f, "the token has less than {min_bytes} bytes")
            }
            Self::ModeTooWide { allowed } => {
                write!(f, "the mode of the token file is not {allowed}")
            }
            Self::NotUtf8 => f.write_str("the token is not UTF-8"),
        }
    }
}

impl Error for TokenError {}

/// Reads the token file at `path` under `rule`.
///
/// The function reads the file one time, with a cap of 1 MiB, and follows a
/// symlink. The mode is a fact of the file that gave the bytes, so the
/// function makes no second `stat`. The function blocks: in a runtime, call
/// it through `Tasks::spawn_blocking`.
///
/// A rule with [`Trim::AsciiSpace`] reads bytes, as `attendance` and the
/// trigger door do. The checks, in this order:
///
/// 1. Remove the six bytes of ASCII space from the two ends.
/// 2. Refuse a content with no byte left: [`TokenError::Empty`].
/// 3. Refuse a token of less bytes than the rule demands:
///    [`TokenError::TooShort`].
/// 4. With [`Encoding::Utf8`], refuse a token that is not UTF-8:
///    [`TokenError::NotUtf8`].
/// 5. Refuse a mode that the rule does not permit:
///    [`TokenError::ModeTooWide`].
///
/// A rule with [`Trim::PythonStrip`] reads text, as a door, the chaperone and
/// `caregiver` do. Its file must be UTF-8 for each [`Encoding`]. The checks,
/// in this order:
///
/// 1. Refuse a content that is not UTF-8: [`TokenError::NotUtf8`].
/// 2. Read each CR LF and each CR as one LF, as the text mode of Python does.
/// 3. Remove the space of `str.strip` of Python from the two ends.
/// 4. Refuse a token of less bytes than the rule demands. Under a rule of 32
///    bytes, an empty token is [`TokenError::TooShort`] too, as each Python
///    door says. Under each other rule, it is [`TokenError::Empty`].
/// 5. Refuse a mode that the rule does not permit.
///
/// The Python origins are the five readers of the table at the start of this
/// module. Two more copies of the door reader exist:
/// `door-tui/src/agent_door_tui/config.py:119-144` and
/// `door-trigger/src/agent_door_trigger/config.py:210-237`.
///
/// Two more Python readers of a secret file read text, remove the space of
/// `str.strip` and take each count of bytes but zero, as
/// [`TokenRule::NOT_EMPTY`] does:
/// `attendance/src/attendance/owui_copy.py:439-453` and
/// `noticeboard/src/noticeboard/sessions.py:318-339`. No vector covers the
/// two.
///
/// The function differs from its Python origins in three ways. No vector
/// holds one of them:
///
/// 1. Each Python reader reads a token file of each size into memory. This
///    function refuses a file of more than 1 MiB: [`TokenError::TooLarge`].
/// 2. A Python reader reads a device as a file, and it waits at a FIFO. This
///    function refuses a path that is no regular file:
///    [`TokenError::Unreadable`], with the text of the refusal of the read.
/// 3. `attendance` and the trigger door read the mode with a `stat` of the
///    path, apart from the read of the bytes
///    (`attendance/src/attendance/atomic.py:120-127`,
///    `door-trigger/src/agent_door_trigger/tokens.py:69-78`). This function
///    takes the mode of the open file that gave the bytes.
///
/// A [`TokenError`] names more causes than two of the Python readers do. Each
/// side refuses the same files, and a service maps each error to its own
/// answer:
///
/// - The trigger door gives one answer for each file that it does not take,
///   and it checks the mode before the read
///   (`door-trigger/src/agent_door_trigger/tokens.py:43-66`). This function
///   checks the mode last.
/// - `caregiver` gives one refusal for a file that the system does not give
///   and for a file that is not UTF-8
///   (`caregiver/src/caregiver/switch.py:86-89`). This function gives
///   [`TokenError::Unreadable`] for the first and [`TokenError::NotUtf8`]
///   for the second.
///
/// ```
/// use std::path::Path;
///
/// use creche_runtime::token::{self, TokenError, TokenRule};
///
/// let absent = token::read(Path::new("no-such-file"), TokenRule::DOOR);
/// assert_eq!(
///     absent.map(|_| ()),
///     Err(TokenError::Unreadable {
///         os_text: String::from("No such file or directory"),
///     })
/// );
/// ```
///
/// # Errors
///
/// [`TokenError`] with the first rule that the file breaks.
pub fn read(path: &Path, rule: TokenRule) -> Result<Secret, TokenError> {
    read_file(path, rule).map(|(_facts, token)| token)
}

/// The token of the file at `path`, with the facts of the file that gave it.
///
/// [`CachedToken`] keeps the facts. They are the facts of the open file, so
/// the token and the facts are of one file.
fn read_file(path: &Path, rule: TokenRule) -> Result<(FileFacts, Secret), TokenError> {
    match readfile::read_capped(path, FILE_CAP, Follow::Follow) {
        FileRead::Bytes { bytes, facts } => {
            token_of(&bytes, facts.mode(), rule).map(|token| (facts, token))
        }
        FileRead::Absent => Err(TokenError::Unreadable {
            os_text: readfile::os_text(&io::Error::from(Errno::NOENT)),
        }),
        FileRead::Refused(ReadRefusal::Unreadable { os_text, .. }) => {
            Err(TokenError::Unreadable { os_text })
        }
        FileRead::Refused(ReadRefusal::TooLarge { .. }) => Err(TokenError::TooLarge),
        // A directory, a FIFO or a device. The read gives no text of the
        // system for it, so the error holds the text of the refusal. The
        // read gives `Symlink` only to a caller that refuses a symlink.
        FileRead::Refused(refusal @ (ReadRefusal::NotAFile | ReadRefusal::Symlink)) => {
            Err(TokenError::Unreadable {
                os_text: refusal.to_string(),
            })
        }
    }
}

/// The token in the content `bytes` of a file with the permission bits
/// `mode`.
///
/// The function takes no path. The mode is thus the mode of the file that
/// gave the bytes, and not of a file that another process put at the path
/// after the read.
fn token_of(bytes: &[u8], mode: u32, rule: TokenRule) -> Result<Secret, TokenError> {
    let token = match rule.trim {
        Trim::AsciiSpace => bytes_token(bytes, rule)?,
        Trim::PythonStrip => text_token(bytes, rule)?,
    };

    // The mode is the last check. The `attendance` reader checks the count
    // of the bytes first (`attendance/src/attendance/auth.py:251-263`).
    if rule.mode.refuses(mode) {
        return Err(TokenError::ModeTooWide { allowed: rule.mode });
    }

    Ok(token)
}

/// The token of a reader that takes the content as bytes: steps 1 to 4 of
/// the first list of [`read`].
///
/// The Python origins are `attendance/src/attendance/auth.py:251-260` and
/// `door-trigger/src/agent_door_trigger/tokens.py:52-66`.
fn bytes_token(bytes: &[u8], rule: TokenRule) -> Result<Secret, TokenError> {
    let token = ascii_strip(bytes);
    if token.is_empty() {
        return Err(TokenError::Empty);
    }

    if token.len() < rule.min_bytes {
        return Err(TokenError::TooShort {
            min_bytes: rule.min_bytes,
        });
    }

    if rule.encoding == Encoding::Utf8 && str::from_utf8(token).is_err() {
        return Err(TokenError::NotUtf8);
    }

    Secret::try_from(token.to_vec()).map_err(|SecretError::Empty| TokenError::Empty)
}

/// The token of a reader that takes the content as text: steps 1 to 4 of the
/// second list of [`read`].
///
/// The Python origins are `door-owui/src/agent_door_owui/config.py:124-146`,
/// `chaperone/src/chaperone/delegate.py:147-162` and
/// `caregiver/src/caregiver/switch.py:86-94`.
fn text_token(bytes: &[u8], rule: TokenRule) -> Result<Secret, TokenError> {
    let text = str::from_utf8(bytes).map_err(|_| TokenError::NotUtf8)?;
    let text = universal_newlines(text);

    // `key_of_file` holds the strip and the count of a key of 32 bytes, so
    // this module holds no second copy of the two. It gives one refusal for
    // a short key and for an empty key, as each Python door does.
    if rule.min_bytes == MIN_KEY_BYTES {
        return config::key_of_file(&text).map_err(|KeyError::TooShort| TokenError::TooShort {
            min_bytes: MIN_KEY_BYTES,
        });
    }

    let token = config::python_strip(&text);
    if token.is_empty() {
        return Err(TokenError::Empty);
    }

    if token.len() < rule.min_bytes {
        return Err(TokenError::TooShort {
            min_bytes: rule.min_bytes,
        });
    }

    Secret::try_from(token.to_owned()).map_err(|SecretError::Empty| TokenError::Empty)
}

/// The bytes without the ASCII space at the two ends, as `bytes.strip` of
/// Python gives them (`attendance/src/attendance/auth.py:251`).
///
/// The function removes the six bytes of [`ASCII_SPACE`] and no other byte.
/// It keeps the UTF-8 bytes of a space that is not ASCII, for example of
/// U+00A0.
fn ascii_strip(mut bytes: &[u8]) -> &[u8] {
    while let [first, rest @ ..] = bytes
        && ASCII_SPACE.contains(first)
    {
        bytes = rest;
    }

    while let [rest @ .., last] = bytes
        && ASCII_SPACE.contains(last)
    {
        bytes = rest;
    }

    bytes
}

/// The text as the text mode of Python gives it: one LF for each CR LF and
/// for each other CR.
///
/// `Path.read_text` of Python opens the file in that mode
/// (`chaperone/src/chaperone/delegate.py:148`). A CR inside a token is thus
/// an LF in the token that a Python door holds.
fn universal_newlines(text: &str) -> Cow<'_, str> {
    if !text.contains('\r') {
        return Cow::Borrowed(text);
    }

    Cow::Owned(text.replace("\r\n", "\n").replace('\r', "\n"))
}

/// A token file that the service reads again only after the file moved.
///
/// A token can rotate while the service runs, and the writer of the file can
/// start after its reader. This type reads at the time of the call. It
/// compares four facts of the file with the facts of its last read: the
/// device, the inode, the size and the time of the last change. It reads the
/// file only when one of them moved. A `stat` that fails never serves the
/// value of the last read. A read that fails drops the value of the last
/// read.
///
/// The Python origin is `chaperone/src/chaperone/delegate.py:165-210`.
///
/// `Debug` prints no field. The facts of the last read hold the size of the
/// file, and that size gives the length of the token (contract 02 §3 rule
/// 6).
///
/// ```
/// use std::path::PathBuf;
///
/// use creche_runtime::token::{CachedToken, TokenRule};
///
/// let mut token = CachedToken::new(PathBuf::from("no-such-file"), TokenRule::DOOR);
///
/// assert!(token.current().is_err());
/// assert_eq!(format!("{token:?}"), "CachedToken { .. }");
/// ```
///
/// Code outside this module cannot build the type with a token that no file
/// gave:
///
/// ```compile_fail,E0451
/// use std::path::PathBuf;
///
/// use creche_runtime::token::{CachedToken, TokenRule};
///
/// let token = CachedToken {
///     path: PathBuf::from("no-such-file"),
///     rule: TokenRule::DOOR,
///     last: None,
/// };
/// ```
pub struct CachedToken {
    path: PathBuf,
    rule: TokenRule,
    /// The facts and the token of the last read, when that read gave a
    /// token.
    last: Option<(FileFacts, Secret)>,
}

impl fmt::Debug for CachedToken {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("CachedToken").finish_non_exhaustive()
    }
}

impl CachedToken {
    /// A reader of the token file at `path` under `rule`. The call reads no
    /// file.
    ///
    /// The Python origin is `chaperone/src/chaperone/delegate.py:184-187`.
    #[must_use]
    pub fn new(path: PathBuf, rule: TokenRule) -> Self {
        Self {
            path,
            rule,
            last: None,
        }
    }

    /// The token of the file as it is now.
    ///
    /// The call makes one `stat`. It reads the file when the `stat` fails,
    /// when no earlier read gave a token, and when one of four facts of the
    /// file moved: the device, the inode, the size or the time of the last
    /// change. The call blocks for the `stat` and for the read.
    ///
    /// A new mode alone starts no read, as in the Python cache. The call
    /// thus checks the mode of the rule only when it reads the file.
    ///
    /// The Python origin is `chaperone/src/chaperone/delegate.py:189-210`.
    /// The call differs from it in two ways:
    ///
    /// 1. The Python cache compares three facts: the time of the last
    ///    change, the size and the inode. This call also compares the
    ///    device, because one inode number can name two files on two
    ///    devices.
    /// 2. After a read that fails, the Python cache keeps the facts and the
    ///    token of its last good read. It gives that token again when the
    ///    file has those facts again. This call drops the token, and the
    ///    next call reads the file.
    ///
    /// # Errors
    ///
    /// [`TokenError`] when the file gives no token now, also when an earlier
    /// call gave one.
    pub fn current(&mut self) -> Result<&Secret, TokenError> {
        let now = readfile::facts(&self.path);
        let read = match self.last.take() {
            Some((then, token)) if now.is_some_and(|now| same_content(now, then)) => (then, token),
            // The field is empty here. A read that fails thus leaves no
            // token of an earlier read.
            _ => read_file(&self.path, self.rule)?,
        };
        let (_facts, token) = self.last.insert(read);

        Ok(token)
    }
}

// CONTRACT-QUESTION: contract 04 §7.3 names three facts for the compare of
// the delegate token file: the time of the last change, the size and the
// inode. The reading here adds the device, because an inode number is an id
// only on one device. A file on another device with the three facts of the
// last read thus starts a read, and the Python cache gives its old token
// there. No writer of the platform moves a token file to another device. A
// change costs one line of `same_content`.
/// Whether the facts `now` name the file and the content that the facts
/// `then` named: the same device, inode, size and time of the last change.
///
/// The mode is no part of the compare. Contract 04 §7.3 permits a read only
/// after one of its three facts moved, and a `chmod` moves none of them.
fn same_content(now: FileFacts, then: FileFacts) -> bool {
    (now.dev(), now.ino(), now.len(), now.modified_ns())
        == (then.dev(), then.ino(), then.len(), then.modified_ns())
}

/// How [`bearer_of`] treats the space around the value of the header.
///
/// The set is closed.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum BearerTrim {
    /// Remove the space, as `str.strip` of Python does. `attendance` and the
    /// trigger listener do this.
    PythonStrip,
    /// Keep the value as it is. `door-owui` does this.
    Exact,
}

/// The bearer of a request: the bytes after `Bearer ` in the `Authorization`
/// header. `None` for a request with no such header and for an empty value.
///
/// The result is the bytes that the Python service compares. Give them to
/// `Secret::matches`. The bytes can be a token, and the `Debug` of a
/// `Vec<u8>` prints each byte: write the result to no log line.
///
/// 1. Take the first `Authorization` header of the request.
/// 2. Take the value only when it starts with `Bearer` and one space, in
///    that case of letters.
/// 3. Read each byte of the rest as one character of Latin-1, as the Python
///    framework decodes a header.
/// 4. With [`BearerTrim::PythonStrip`], remove the space of `str.strip` of
///    Python from the two ends.
/// 5. Give the UTF-8 bytes of the text.
///
/// A token with a character outside ASCII thus matches only when the client
/// sends that character as one Latin-1 byte. The UTF-8 bytes of the
/// character do not match.
///
/// The Python origins are `attendance/src/attendance/auth.py:139` and
/// `:234-239`, `door-owui/src/agent_door_owui/app.py:158-172` and
/// `door-trigger/src/agent_door_trigger/webhooks.py:165-170`. The chaperone
/// compares the same bytes (`chaperone/src/chaperone/app.py:452-464`).
///
/// A `HeaderValue` holds no control byte of ASCII but the tab. The Python
/// parser of a request gives an app the bytes `0x1c` to `0x1f` and `0x7f`,
/// and `str.strip` of Python removes the first four. The vector
/// `byte-1c-at-the-end` holds such a header, and three Python services take
/// that request. No request can give this function those bytes, so the test
/// gives them to the private reader of a header value. A Rust service thus
/// cannot take that request as the Python service does. The difference is
/// open: `AGENTS.md` of this crate lists it under "Known gaps". The answer of
/// a Rust service to such a request is a rule of its HTTP server.
///
/// This function is no port of the header reader of the chaperone
/// (`chaperone/src/chaperone/app.py:350-355`). That reader also takes the
/// scheme in another case of letters. No value of [`BearerTrim`] stands for
/// its rule.
///
/// ```
/// use creche_runtime::token::{BearerTrim, bearer_of};
/// use http::header::AUTHORIZATION;
/// use http::{HeaderMap, HeaderValue};
///
/// let mut headers = HeaderMap::new();
/// headers.insert(AUTHORIZATION, HeaderValue::from_static("Bearer a-token "));
///
/// assert_eq!(
///     bearer_of(&headers, BearerTrim::PythonStrip),
///     Some(b"a-token".to_vec())
/// );
/// assert_eq!(bearer_of(&headers, BearerTrim::Exact), Some(b"a-token ".to_vec()));
/// assert_eq!(bearer_of(&HeaderMap::new(), BearerTrim::Exact), None);
/// ```
#[must_use]
pub fn bearer_of(headers: &HeaderMap, trim: BearerTrim) -> Option<Vec<u8>> {
    bearer_in(headers.get(AUTHORIZATION)?.as_bytes(), trim)
}

/// The bearer in the bytes of one header value: steps 2 to 5 of
/// [`bearer_of`].
fn bearer_in(value: &[u8], trim: BearerTrim) -> Option<Vec<u8>> {
    let rest = value.strip_prefix(BEARER)?;
    // Latin-1 gives each byte the character with the same number.
    let text: String = rest.iter().copied().map(char::from).collect();
    let token = match trim {
        BearerTrim::PythonStrip => config::python_strip(&text),
        BearerTrim::Exact => &text,
    };

    (!token.is_empty()).then(|| token.as_bytes().to_vec())
}

#[cfg(test)]
mod tests {
    use std::collections::HashSet;
    use std::fs::{self, File};
    use std::os::unix::fs::PermissionsExt;
    use std::time::{Duration, SystemTime};

    use ::http::HeaderValue;
    use creche_testkit::root::TempRoot;
    use creche_testkit::vectors::{self, IndexRow, Marker, Outcome, Surface, Vector};
    use serde_json::Value;

    use super::*;

    /// A token of 32 bytes, the least count of most rules. It is the token
    /// of no deployment.
    const CORE: &str = "0123456789abcdefghijklmnopqrstuv";

    /// `CORE` with one CR, with one LF and with one CR LF in its middle. The
    /// vectors `32-bytes-inner-cr` and `32-bytes-inner-crlf` hold the first
    /// and the third.
    const CORE_WITH_CR: &str = "0123456789abcdef\rhijklmnopqrstuv";
    const CORE_WITH_LF: &str = "0123456789abcdef\nhijklmnopqrstuv";
    const CORE_WITH_CR_LF: &str = "0123456789abcde\r\nhijklmnopqrstuv";

    /// The mode of a token file that each rule takes.
    const OWNER: u32 = 0o600;

    /// One row of a table of files: the rule, the bytes of the file, its mode
    /// and what the read gives.
    type FileRow<'a> = (TokenRule, &'a [u8], u32, Result<Vec<u8>, TokenError>);

    /// One row of a table of header values: the bytes of the value, each trim
    /// that the row is about and the bearer.
    type HeaderRow = (&'static [u8], &'static [BearerTrim], Option<&'static [u8]>);

    /// The five rules of this module.
    const RULES: [TokenRule; 5] = [
        TokenRule::ATTENDANCE,
        TokenRule::ATTENDANCE_PEP_READ,
        TokenRule::DOOR,
        TokenRule::WEBHOOK,
        TokenRule::NOT_EMPTY,
    ];

    /// The text of the system error for a path with no file, on Linux and on
    /// macOS.
    const NO_SUCH_FILE: &str = "No such file or directory";

    /// Writes a file with `bytes` and sets its mode. The umask of the test
    /// run does not set the mode.
    fn write_token(path: &Path, bytes: &[u8], mode: u32) {
        fs::write(path, bytes).unwrap();
        fs::set_permissions(path, fs::Permissions::from_mode(mode)).unwrap();
    }

    /// A token file with `bytes` and `mode`, in a new directory.
    fn token_file(bytes: &[u8], mode: u32) -> (TempRoot, PathBuf) {
        let root = TempRoot::new().unwrap();
        let path = root.path().join("token");
        write_token(&path, bytes, mode);

        (root, path)
    }

    /// What [`read`] gives for a file with `bytes` and `mode`: the bytes of
    /// the token, or the error.
    fn read_bytes(bytes: &[u8], mode: u32, rule: TokenRule) -> Result<Vec<u8>, TokenError> {
        let (_root, path) = token_file(bytes, mode);

        read(&path, rule).map(|token| token.expose_secret().to_vec())
    }

    /// A short name for the variant of an error.
    fn kind_of(error: &TokenError) -> &'static str {
        match error {
            TokenError::Unreadable { .. } => "unreadable",
            TokenError::TooLarge => "too_large",
            TokenError::Empty => "empty",
            TokenError::TooShort { .. } => "short",
            TokenError::ModeTooWide { .. } => "mode",
            TokenError::NotUtf8 => "not_utf8",
        }
    }

    // --- the rules ---

    #[test]
    fn each_constant_holds_the_rules_of_its_python_reader() {
        // The rule, then: the smallest count of bytes, the mode, the trim and
        // the encoding of the Python reader that the doc comment names.
        let table = [
            (
                TokenRule::ATTENDANCE,
                32,
                ModeRule::OwnerOnly,
                Trim::AsciiSpace,
                Encoding::AnyBytes,
            ),
            (
                TokenRule::ATTENDANCE_PEP_READ,
                32,
                ModeRule::OwnerAndGroupRead,
                Trim::AsciiSpace,
                Encoding::AnyBytes,
            ),
            (
                TokenRule::DOOR,
                32,
                ModeRule::AnyMode,
                Trim::PythonStrip,
                Encoding::Utf8,
            ),
            (
                TokenRule::WEBHOOK,
                32,
                ModeRule::OwnerOnly,
                Trim::AsciiSpace,
                Encoding::Utf8,
            ),
            (
                TokenRule::NOT_EMPTY,
                1,
                ModeRule::AnyMode,
                Trim::PythonStrip,
                Encoding::Utf8,
            ),
        ];

        for (rule, min_bytes, mode, trim, encoding) in table {
            assert_eq!(rule.min_bytes(), min_bytes);
            assert_eq!(rule.mode(), mode);
            assert_eq!(rule.trim(), trim);
            assert_eq!(rule.encoding(), encoding);
        }
    }

    #[test]
    fn the_five_constants_are_five_rules() {
        for (at, rule) in RULES.iter().enumerate() {
            for other in RULES.iter().skip(at + 1) {
                assert_ne!(rule, other);
            }
        }
    }

    #[test]
    fn an_error_names_the_rule_and_no_count_of_the_bytes_of_the_token() {
        let table = [
            (
                TokenError::Unreadable {
                    os_text: String::from("No such file or directory"),
                },
                "the token file is not readable: No such file or directory",
            ),
            (TokenError::TooLarge, "the token file is too large"),
            (TokenError::Empty, "the token file is empty"),
            (
                TokenError::TooShort { min_bytes: 32 },
                "the token has less than 32 bytes",
            ),
            (
                TokenError::ModeTooWide {
                    allowed: ModeRule::OwnerOnly,
                },
                "the mode of the token file is not 0600",
            ),
            (
                TokenError::ModeTooWide {
                    allowed: ModeRule::OwnerAndGroupRead,
                },
                "the mode of the token file is not 0600 or 0640",
            ),
            (TokenError::NotUtf8, "the token is not UTF-8"),
        ];

        for (error, text) in table {
            assert_eq!(error.to_string(), text);
        }
    }

    #[test]
    fn a_mode_rule_reads_the_bits_of_the_group_and_of_each_other_user() {
        // The mode, then whether `OwnerOnly` refuses it, then whether
        // `OwnerAndGroupRead` refuses it.
        let table = [
            (0o600, false, false),
            (0o400, false, false),
            (0o700, false, false),
            (0o000, false, false),
            (0o640, true, false),
            (0o440, true, false),
            (0o740, true, false),
            (0o644, true, true),
            (0o660, true, true),
            (0o650, true, true),
            (0o604, true, true),
            (0o620, true, true),
            (0o610, true, true),
            (0o601, true, true),
            (0o777, true, true),
            // No rule reads the setuid bit, the setgid bit or the sticky bit.
            (0o4600, false, false),
            (0o2640, true, false),
            (0o1600, false, false),
            (0o7600, false, false),
        ];

        for (mode, owner_only, group_read) in table {
            assert_eq!(ModeRule::OwnerOnly.refuses(mode), owner_only, "{mode:o}");
            assert_eq!(
                ModeRule::OwnerAndGroupRead.refuses(mode),
                group_read,
                "{mode:o}"
            );
            assert!(!ModeRule::AnyMode.refuses(mode), "{mode:o}");
        }
    }

    // --- the two strips ---

    #[test]
    fn the_ascii_strip_removes_the_six_bytes_of_python_and_no_other() {
        assert_eq!(
            ascii_strip(b" \t\n\r\x0b\x0ctoken\x0c\x0b\r\n\t "),
            b"token"
        );
        assert_eq!(ascii_strip(b"to ken"), b"to ken");
        assert_eq!(ascii_strip(b" \t\n\r\x0b\x0c"), b"");
        assert_eq!(ascii_strip(b""), b"");
        assert_eq!(ASCII_SPACE, *b" \t\n\r\x0b\x0c");

        for byte in 0..=u8::MAX {
            let content = [byte, b'a', byte];

            if ASCII_SPACE.contains(&byte) {
                assert_eq!(ascii_strip(&content), b"a", "{byte:#04x}");
            } else {
                assert_eq!(ascii_strip(&content), content, "{byte:#04x}");
            }
        }

        // The reason for a list by hand: the standard library does not count
        // the vertical tab, and `bytes.strip` of Python removes it.
        assert!(!b'\x0b'.is_ascii_whitespace());
        assert_eq!(b"\x0btoken\x0b".trim_ascii(), b"\x0btoken\x0b");
    }

    #[test]
    fn the_text_mode_of_python_reads_each_cr_as_one_line_feed() {
        let table = [
            ("", ""),
            ("token", "token"),
            ("a\nb", "a\nb"),
            ("a\rb", "a\nb"),
            ("a\r\nb", "a\nb"),
            ("a\r\r\nb", "a\n\nb"),
            ("a\n\rb", "a\n\nb"),
            ("\r", "\n"),
            ("\r\n", "\n"),
            ("token\r\n", "token\n"),
        ];

        for (file, text) in table {
            assert_eq!(universal_newlines(file), text, "{file:?}");
        }

        assert!(matches!(universal_newlines("a\nb"), Cow::Borrowed(_)));
    }

    // --- the read ---

    #[test]
    fn a_reader_of_bytes_keeps_each_byte_between_the_two_ends() {
        // 32 bytes with a NUL, a byte that is not UTF-8, a CR and a space
        // that is not ASCII. `bytes.strip` of Python keeps each one.
        let mut token = vec![0x00, 0xff, b'\r', 0xc2, 0xa0];
        token.extend_from_slice(&CORE.as_bytes()[..27]);
        let file = [b" \t\x0b".as_slice(), &token, b"\x0c\r\n"].concat();

        for rule in [TokenRule::ATTENDANCE, TokenRule::ATTENDANCE_PEP_READ] {
            assert_eq!(read_bytes(&file, OWNER, rule), Ok(token.clone()));
        }

        // The webhook rule keeps a CR too. It demands UTF-8 after the strip.
        assert_eq!(
            read_bytes(
                format!(" {CORE_WITH_CR}\n").as_bytes(),
                OWNER,
                TokenRule::WEBHOOK
            ),
            Ok(CORE_WITH_CR.as_bytes().to_vec())
        );
        assert_eq!(
            read_bytes(&file, OWNER, TokenRule::WEBHOOK),
            Err(TokenError::NotUtf8)
        );
    }

    /// The proof of the Python strip: U+001C and U+0085 at the two ends.
    /// `str::trim` keeps U+001C, and `str.strip` of Python removes it.
    #[test]
    fn a_reader_of_text_removes_each_space_of_python_from_the_two_ends() {
        let files = [
            format!("\u{1c}{CORE}\u{85}"),
            format!("\u{85}{CORE}\u{1c}"),
            format!("\u{1c}\u{85} \u{1f}{CORE}\u{1d}\u{a0}\u{2028}\u{1e}\n"),
        ];

        for file in &files {
            for rule in [TokenRule::DOOR, TokenRule::NOT_EMPTY] {
                assert_eq!(
                    read_bytes(file.as_bytes(), OWNER, rule),
                    Ok(CORE.as_bytes().to_vec()),
                    "{file:?}"
                );
            }
            assert_ne!(file.trim(), CORE, "{file:?}");
        }

        // A reader of bytes removes ASCII space only, so it keeps the two
        // characters.
        let file = &files[0];
        for rule in [TokenRule::ATTENDANCE, TokenRule::WEBHOOK] {
            assert_eq!(
                read_bytes(file.as_bytes(), OWNER, rule),
                Ok(file.clone().into_bytes())
            );
        }
    }

    #[test]
    fn a_reader_of_text_reads_a_cr_inside_a_token_as_a_line_feed() {
        for rule in [TokenRule::DOOR, TokenRule::NOT_EMPTY] {
            assert_eq!(
                read_bytes(CORE_WITH_CR.as_bytes(), OWNER, rule),
                Ok(CORE_WITH_LF.as_bytes().to_vec())
            );
        }

        // CR LF is one character of the token. The 32 bytes of the file are
        // thus a token of 31 bytes.
        assert_eq!(CORE_WITH_CR_LF.len(), 32);
        assert_eq!(
            read_bytes(CORE_WITH_CR_LF.as_bytes(), OWNER, TokenRule::DOOR),
            Err(TokenError::TooShort { min_bytes: 32 })
        );
        assert_eq!(
            read_bytes(CORE_WITH_CR_LF.as_bytes(), OWNER, TokenRule::NOT_EMPTY)
                .map(|token| token.len()),
            Ok(31)
        );
    }

    #[test]
    fn the_error_is_the_first_rule_that_the_file_breaks() {
        let short = &CORE.as_bytes()[..31];
        let not_text = [0xff_u8; 32];
        let short_of = |min_bytes| Err(TokenError::TooShort { min_bytes });
        let too_wide = |allowed| Err(TokenError::ModeTooWide { allowed });
        // The rule, the bytes of the file, its mode and the result.
        let table: Vec<FileRow<'_>> = vec![
            // A reader of bytes: empty, then short, then the mode.
            (TokenRule::ATTENDANCE, b"", 0o644, Err(TokenError::Empty)),
            (TokenRule::ATTENDANCE, b" \n", 0o644, Err(TokenError::Empty)),
            (TokenRule::ATTENDANCE, short, 0o644, short_of(32)),
            (
                TokenRule::ATTENDANCE,
                CORE.as_bytes(),
                0o644,
                too_wide(ModeRule::OwnerOnly),
            ),
            (
                TokenRule::ATTENDANCE,
                CORE.as_bytes(),
                0o640,
                too_wide(ModeRule::OwnerOnly),
            ),
            (
                TokenRule::ATTENDANCE,
                &not_text,
                OWNER,
                Ok(not_text.to_vec()),
            ),
            (
                TokenRule::ATTENDANCE_PEP_READ,
                CORE.as_bytes(),
                0o640,
                Ok(CORE.as_bytes().to_vec()),
            ),
            (
                TokenRule::ATTENDANCE_PEP_READ,
                CORE.as_bytes(),
                0o660,
                too_wide(ModeRule::OwnerAndGroupRead),
            ),
            (TokenRule::ATTENDANCE_PEP_READ, short, 0o660, short_of(32)),
            // The webhook rule: the count, then the encoding, then the mode.
            (TokenRule::WEBHOOK, b"", 0o644, Err(TokenError::Empty)),
            (TokenRule::WEBHOOK, &not_text[..31], 0o644, short_of(32)),
            (
                TokenRule::WEBHOOK,
                &not_text,
                0o644,
                Err(TokenError::NotUtf8),
            ),
            (
                TokenRule::WEBHOOK,
                CORE.as_bytes(),
                0o644,
                too_wide(ModeRule::OwnerOnly),
            ),
            (
                TokenRule::WEBHOOK,
                CORE.as_bytes(),
                OWNER,
                Ok(CORE.as_bytes().to_vec()),
            ),
            // A reader of text: the encoding, then the count. An empty key
            // of a door is short, as each Python door says.
            (
                TokenRule::DOOR,
                b"\xffshort",
                OWNER,
                Err(TokenError::NotUtf8),
            ),
            (TokenRule::DOOR, b"", OWNER, short_of(32)),
            (TokenRule::DOOR, b" \n", OWNER, short_of(32)),
            (TokenRule::DOOR, short, 0o644, short_of(32)),
            (
                TokenRule::DOOR,
                CORE.as_bytes(),
                0o666,
                Ok(CORE.as_bytes().to_vec()),
            ),
            (
                TokenRule::NOT_EMPTY,
                b"\xff",
                OWNER,
                Err(TokenError::NotUtf8),
            ),
            (TokenRule::NOT_EMPTY, b"", OWNER, Err(TokenError::Empty)),
            (
                TokenRule::NOT_EMPTY,
                "\u{a0}\u{1c}\n".as_bytes(),
                OWNER,
                Err(TokenError::Empty),
            ),
            (TokenRule::NOT_EMPTY, b" a\n", 0o666, Ok(b"a".to_vec())),
        ];

        for (rule, bytes, mode, result) in table {
            assert_eq!(read_bytes(bytes, mode, rule), result, "{rule:?} {mode:o}");
        }
    }

    #[test]
    fn a_rule_of_text_with_another_count_tells_empty_from_short() {
        // No constant has this rule. The fields are private, so only this
        // module can build it.
        let rule = TokenRule {
            min_bytes: 16,
            mode: ModeRule::OwnerOnly,
            trim: Trim::PythonStrip,
            encoding: Encoding::AnyBytes,
        };

        assert_eq!(read_bytes(b" \n", OWNER, rule), Err(TokenError::Empty));
        assert_eq!(
            read_bytes(b"short\n", OWNER, rule),
            Err(TokenError::TooShort { min_bytes: 16 })
        );
        assert_eq!(
            read_bytes(b"0123456789abcdef\n", 0o640, rule),
            Err(TokenError::ModeTooWide {
                allowed: ModeRule::OwnerOnly
            })
        );
        assert_eq!(
            read_bytes(b"0123456789abcdef\n", OWNER, rule),
            Ok(b"0123456789abcdef".to_vec())
        );
        // A rule that removes the space of text reads text, also when it
        // names each byte.
        assert_eq!(
            read_bytes(&[0xff; 16], OWNER, rule),
            Err(TokenError::NotUtf8)
        );
    }

    #[test]
    fn a_path_with_no_file_is_unreadable_with_the_text_of_the_system() {
        let (root, path) = token_file(CORE.as_bytes(), OWNER);
        let table = [
            (root.path().join("no-such-token"), NO_SUCH_FILE),
            (root.path().join("no-such-dir").join("token"), NO_SUCH_FILE),
            // A regular file is in the place of a directory of the path.
            (path.join("token"), "Not a directory"),
        ];

        for (path, text) in table {
            for rule in RULES {
                let error = read(&path, rule).map(|_| ()).unwrap_err();

                assert_eq!(
                    error,
                    TokenError::Unreadable {
                        os_text: String::from(text)
                    }
                );
            }
        }
    }

    #[test]
    fn the_read_follows_a_symlink_to_the_token_file() {
        let (root, path) = token_file(format!("{CORE}\n").as_bytes(), OWNER);
        let link = root.path().join("link");
        std::os::unix::fs::symlink(&path, &link).unwrap();

        for rule in RULES {
            let token = read(&link, rule).unwrap();

            assert!(token.matches(CORE.as_bytes()));
        }
    }

    /// The port of `test_the_error_never_carries_the_value`
    /// (`chaperone/tests/test_chaperone_delegate_token_file.py:166-171`), for
    /// each rule and each error that a file can give.
    #[test]
    fn no_error_holds_the_token_or_the_count_of_its_bytes() {
        // A token of 23 bytes and a token of 41 bytes. No text of an error
        // holds one of the two numbers for another reason.
        const SHORT: &str = "tiny-token-of-23-bytes!";
        const LONG: &str = "a-token-that-no-deployment-holds-41-bytes";
        // The second file has a first byte that is not UTF-8: 42 bytes.
        let not_text = [b"\xff".as_slice(), LONG.as_bytes()].concat();
        let files: [(&[u8], u32); 5] = [
            (SHORT.as_bytes(), OWNER),
            (SHORT.as_bytes(), 0o644),
            (LONG.as_bytes(), 0o644),
            (LONG.as_bytes(), 0o660),
            (&not_text, OWNER),
        ];
        let mut kinds = HashSet::new();

        assert_eq!((SHORT.len(), LONG.len(), not_text.len()), (23, 41, 42));
        for rule in RULES {
            for (bytes, mode) in files {
                let Err(error) = read_bytes(bytes, mode, rule) else {
                    continue;
                };
                let text = format!("{error} {error:?}");

                for part in [SHORT, LONG, "tiny", "token-", "23", "41", "42"] {
                    assert!(!text.contains(part), "{text:?} holds {part:?}");
                }
                kinds.insert(kind_of(&error));
            }
        }

        assert_eq!(kinds, HashSet::from(["short", "mode", "not_utf8"]));
    }

    // --- where the read differs from a Python reader, and its finer errors ---

    /// Difference 1 of [`read`]. Each Python reader reads a file of each
    /// size, for example `attendance/src/attendance/auth.py:247`.
    #[test]
    fn a_file_past_the_cap_is_too_large() {
        let at_the_cap = vec![b'a'; FILE_CAP.get()];
        let past_the_cap = vec![b'a'; FILE_CAP.get() + 1];

        assert_eq!(FILE_CAP.get(), 1 << 20);
        for rule in RULES {
            assert_eq!(
                read_bytes(&at_the_cap, OWNER, rule).map(|token| token.len()),
                Ok(FILE_CAP.get())
            );
            assert_eq!(
                read_bytes(&past_the_cap, OWNER, rule),
                Err(TokenError::TooLarge)
            );
        }
    }

    /// Difference 2 of [`read`]. A Python door gives the text of the system
    /// for a directory (`door-owui/src/agent_door_owui/config.py:124-128`),
    /// and each Python reader reads a device as a file.
    #[test]
    fn a_path_that_is_no_regular_file_is_unreadable() {
        let root = TempRoot::new().unwrap();
        let not_a_file = TokenError::Unreadable {
            os_text: String::from("the path is not a regular file"),
        };

        assert_eq!(
            ReadRefusal::NotAFile.to_string(),
            "the path is not a regular file"
        );
        for rule in RULES {
            for path in [root.path(), Path::new("/dev/null")] {
                assert_eq!(
                    read(path, rule).map(|_| ()),
                    Err(not_a_file.clone()),
                    "{}",
                    path.display()
                );
            }
        }
    }

    /// Difference 3 of [`read`]. The reader of `attendance` makes a `stat` of
    /// the path after the read (`attendance/src/attendance/auth.py:261` and
    /// `:278`).
    #[test]
    fn the_mode_check_takes_the_mode_of_the_read() {
        let wide = |allowed| Err(TokenError::ModeTooWide { allowed });
        let checked = |mode, rule| token_of(CORE.as_bytes(), mode, rule).map(|_| ());

        // `token_of` takes the bytes and the mode of one read, and no path.
        assert_eq!(checked(0o600, TokenRule::ATTENDANCE), Ok(()));
        assert_eq!(
            checked(0o640, TokenRule::ATTENDANCE),
            wide(ModeRule::OwnerOnly)
        );
        assert_eq!(checked(0o640, TokenRule::ATTENDANCE_PEP_READ), Ok(()));
        assert_eq!(
            checked(0o644, TokenRule::ATTENDANCE_PEP_READ),
            wide(ModeRule::OwnerAndGroupRead)
        );

        // The mode of the read is the mode of the file behind a symlink.
        let (root, path) = token_file(CORE.as_bytes(), 0o644);
        let link = root.path().join("link");
        std::os::unix::fs::symlink(&path, &link).unwrap();

        assert_eq!(
            read(&link, TokenRule::ATTENDANCE).map(|_| ()),
            wide(ModeRule::OwnerOnly)
        );
    }

    /// The reader of the trigger door checks the mode first, and it gives one
    /// answer for each refusal
    /// (`door-trigger/src/agent_door_trigger/tokens.py:43-66`). [`read`]
    /// checks the mode last, and each cause has its own error.
    #[test]
    fn the_webhook_rule_checks_the_mode_last() {
        let short = &CORE.as_bytes()[..31];

        assert_eq!(
            read_bytes(short, 0o644, TokenRule::WEBHOOK),
            Err(TokenError::TooShort { min_bytes: 32 })
        );
        assert_eq!(
            read_bytes(b"", 0o644, TokenRule::WEBHOOK),
            Err(TokenError::Empty)
        );
        assert_eq!(
            read_bytes(&NOT_TEXT, 0o644, TokenRule::WEBHOOK),
            Err(TokenError::NotUtf8)
        );
        assert_eq!(
            read_bytes(CORE.as_bytes(), 0o644, TokenRule::WEBHOOK),
            Err(TokenError::ModeTooWide {
                allowed: ModeRule::OwnerOnly
            })
        );
    }

    /// The reader of `caregiver` gives one refusal for the two files
    /// (`caregiver/src/caregiver/switch.py:86-89`). [`read`] gives each one
    /// its own error.
    #[test]
    fn a_file_that_is_not_text_is_not_a_file_that_the_system_refuses() {
        let (root, path) = token_file(&NOT_TEXT, OWNER);

        for rule in [TokenRule::NOT_EMPTY, TokenRule::DOOR] {
            assert_eq!(read(&path, rule).map(|_| ()), Err(TokenError::NotUtf8));
            assert_eq!(
                read(&root.path().join("no-such-token"), rule).map(|_| ()),
                Err(TokenError::Unreadable {
                    os_text: String::from(NO_SUCH_FILE)
                })
            );
        }
    }

    // --- the cached token ---

    /// The names and the tokens of
    /// `chaperone/tests/test_chaperone_delegate_token_file.py:34-39`.
    const TOKEN_NAME: &str = "door-delegate.token";
    const FIRST: &str = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
    const SECOND: &str = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";

    /// The content of a token file that is not text: no byte of it is UTF-8.
    const NOT_TEXT: [u8; 32] = [0xff; 32];

    /// A token file as `attendance` writes it: the token, one newline and
    /// the mode `0640` (`write_token` of the Python test, lines 49-52).
    fn write_line(path: &Path, token: &str) {
        write_token(path, format!("{token}\n").as_bytes(), 0o640);
    }

    /// A reader of a token file that no process wrote yet.
    fn cached(rule: TokenRule) -> (TempRoot, PathBuf, CachedToken) {
        let root = TempRoot::new().unwrap();
        let path = root.path().join(TOKEN_NAME);
        let token = CachedToken::new(path.clone(), rule);

        (root, path, token)
    }

    /// The bytes of the token that the reader gives now, or the error.
    fn current(token: &mut CachedToken) -> Result<Vec<u8>, TokenError> {
        token
            .current()
            .map(|secret| secret.expose_secret().to_vec())
    }

    fn token_bytes(text: &str) -> Result<Vec<u8>, TokenError> {
        Ok(text.as_bytes().to_vec())
    }

    /// The time of the last change of the file at `path`.
    fn modified(path: &Path) -> SystemTime {
        fs::metadata(path).unwrap().modified().unwrap()
    }

    /// Sets the time of the last change of the file at `path`, as `os.utime`
    /// of the Python tests does.
    fn set_modified(path: &Path, time: SystemTime) {
        File::options()
            .write(true)
            .open(path)
            .unwrap()
            .set_modified(time)
            .unwrap();
    }

    #[test]
    fn the_two_tokens_of_the_cache_tests_have_the_least_count_of_bytes() {
        assert_eq!(FIRST.len(), MIN_KEY_BYTES);
        assert_eq!(SECOND.len(), MIN_KEY_BYTES);
        assert_ne!(FIRST, SECOND);
    }

    #[test]
    fn the_debug_of_a_cached_token_prints_no_field() {
        let (_root, path, mut token) = cached(TokenRule::DOOR);

        assert_eq!(format!("{token:?}"), "CachedToken { .. }");

        write_line(&path, FIRST);

        assert_eq!(current(&mut token), token_bytes(FIRST));
        // The reader now holds the facts of the file and the token.
        assert_eq!(format!("{token:?}"), "CachedToken { .. }");
        assert_eq!(format!("{token:#?}"), "CachedToken { .. }");
    }

    /// The port of `test_a_file_written_after_start_is_read` (lines 87-97).
    #[test]
    fn a_file_that_a_process_writes_after_the_start_is_read() {
        let (_root, path, mut token) = cached(TokenRule::DOOR);

        assert_eq!(
            current(&mut token),
            Err(TokenError::Unreadable {
                os_text: String::from(NO_SUCH_FILE)
            })
        );

        write_line(&path, FIRST);

        assert_eq!(current(&mut token), token_bytes(FIRST));
    }

    /// The port of `test_a_rotated_file_is_re_read` (lines 100-110).
    #[test]
    fn a_rotated_file_is_read_again() {
        let (_root, path, mut token) = cached(TokenRule::DOOR);
        write_line(&path, FIRST);

        assert_eq!(current(&mut token), token_bytes(FIRST));

        // The new token has another length, so the size of the file moves
        // also when the clock of the file system does not.
        let longer = format!("{SECOND}{SECOND}");
        write_line(&path, &longer);

        assert_eq!(current(&mut token), token_bytes(&longer));
    }

    /// The port of `test_a_replaced_file_is_re_read` (lines 113-125).
    #[test]
    fn a_replaced_file_is_read_again() {
        let (root, path, mut token) = cached(TokenRule::DOOR);
        write_line(&path, FIRST);

        assert_eq!(current(&mut token), token_bytes(FIRST));

        let before = readfile::facts(&path).unwrap();
        let other = root.path().join("next.token");
        write_line(&other, SECOND);
        set_modified(&other, modified(&path));
        fs::rename(&other, &path).unwrap();
        let after = readfile::facts(&path).unwrap();

        // Only the inode moved.
        assert_ne!(after.ino(), before.ino());
        assert_eq!(
            (after.len(), after.modified_ns(), after.mode()),
            (before.len(), before.modified_ns(), before.mode())
        );
        assert_eq!(current(&mut token), token_bytes(SECOND));
    }

    /// The port of `test_an_unchanged_file_is_not_re_read` (lines 128-142).
    #[test]
    fn a_file_with_the_same_facts_is_not_read_again() {
        let (_root, path, mut token) = cached(TokenRule::DOOR);
        write_line(&path, FIRST);

        assert_eq!(current(&mut token), token_bytes(FIRST));

        // The content changes, and each fact is put back: the same inode,
        // the same size and the time of the last change.
        let before = readfile::facts(&path).unwrap();
        let time = modified(&path);
        write_line(&path, SECOND);
        set_modified(&path, time);

        assert_eq!(readfile::facts(&path), Some(before));
        assert_eq!(fs::read(&path).unwrap(), format!("{SECOND}\n").as_bytes());
        // The old token proves that the call read no file.
        assert_eq!(current(&mut token), token_bytes(FIRST));
    }

    /// The port of `test_a_vanished_file_stops_serving_the_cache` (lines
    /// 145-155).
    #[test]
    fn a_file_that_went_away_stops_the_token() {
        let (_root, path, mut token) = cached(TokenRule::DOOR);
        write_line(&path, FIRST);

        assert_eq!(current(&mut token), token_bytes(FIRST));

        fs::remove_file(&path).unwrap();

        assert_eq!(
            current(&mut token),
            Err(TokenError::Unreadable {
                os_text: String::from(NO_SUCH_FILE)
            })
        );
        assert_eq!(
            current(&mut token).map(|_| ()).unwrap_err().to_string(),
            "the token file is not readable: No such file or directory"
        );
    }

    /// The port of `test_a_short_file_still_refuses` (lines 158-163).
    #[test]
    fn a_short_file_still_refuses() {
        let (_root, path, mut token) = cached(TokenRule::DOOR);
        write_line(&path, "tiny");

        assert_eq!(
            current(&mut token),
            Err(TokenError::TooShort { min_bytes: 32 })
        );
    }

    /// The port of `test_the_error_never_carries_the_value` (lines 166-171).
    #[test]
    fn the_error_of_a_cached_token_never_holds_the_value() {
        let (_root, path, mut token) = cached(TokenRule::DOOR);
        write_line(&path, "tiny");
        let error = current(&mut token).unwrap_err();

        assert!(!format!("{error} {error:?}").contains("tiny"));
    }

    /// The port of `test_a_file_that_is_not_text_refuses` (lines 174-185).
    #[test]
    fn a_file_that_is_not_text_refuses() {
        let (_root, path, mut token) = cached(TokenRule::DOOR);
        write_token(&path, &NOT_TEXT, 0o640);
        let error = current(&mut token).unwrap_err();
        let text = format!("{error} {error:?}").to_lowercase();

        assert_eq!(error, TokenError::NotUtf8);
        // The error holds no byte of the file, in no form.
        assert!(!text.contains("ff") && !text.contains("255"), "{text}");
        assert!(error.source().is_none());
    }

    #[test]
    fn a_cached_token_applies_the_mode_of_its_rule() {
        let (_root, path, mut token) = cached(TokenRule::ATTENDANCE);
        write_token(&path, format!("{FIRST}\n").as_bytes(), 0o640);

        assert_eq!(
            current(&mut token),
            Err(TokenError::ModeTooWide {
                allowed: ModeRule::OwnerOnly
            })
        );

        fs::set_permissions(&path, fs::Permissions::from_mode(OWNER)).unwrap();

        assert_eq!(current(&mut token), token_bytes(FIRST));
    }

    /// The `stat` of the Python cache follows a symlink at the path of the
    /// token file, as its read does
    /// (`chaperone/src/chaperone/delegate.py:204-210`).
    #[test]
    fn a_cached_token_follows_a_symlink_to_the_token_file() {
        let (root, link, mut token) = cached(TokenRule::ATTENDANCE_PEP_READ);
        let first = root.path().join("first.token");
        let second = root.path().join("second.token");
        write_line(&first, FIRST);
        write_line(&second, SECOND);
        std::os::unix::fs::symlink(&first, &link).unwrap();

        assert_eq!(current(&mut token), token_bytes(FIRST));
        // The reader holds the facts of the file behind the link. The `stat`
        // of the next call gives the same facts, so that call reads no file.
        let held = token.last.as_ref().map(|(facts, _)| *facts);

        assert_eq!(held, readfile::facts(&first));
        assert_eq!(held, readfile::facts(&link));
        assert_eq!(current(&mut token), token_bytes(FIRST));

        // The link now names another file.
        fs::remove_file(&link).unwrap();
        std::os::unix::fs::symlink(&second, &link).unwrap();

        assert_eq!(current(&mut token), token_bytes(SECOND));

        // The mode rule reads the mode of the file behind the link. The
        // link now names a file that each user can read.
        let wide = root.path().join("wide.token");
        write_token(&wide, format!("{FIRST}\n").as_bytes(), 0o644);
        fs::remove_file(&link).unwrap();
        std::os::unix::fs::symlink(&wide, &link).unwrap();

        assert_eq!(
            current(&mut token),
            Err(TokenError::ModeTooWide {
                allowed: ModeRule::OwnerAndGroupRead
            })
        );
    }

    /// The four facts that [`CachedToken::current`] compares. The mode is
    /// not one of them.
    fn compared(facts: FileFacts) -> (u64, u64, u64, i128) {
        (facts.dev(), facts.ino(), facts.len(), facts.modified_ns())
    }

    /// The Python cache reads no file after a new mode alone
    /// (`chaperone/src/chaperone/delegate.py:204-210`), and
    /// [`CachedToken::current`] does the same. The file here has another
    /// token and the mode `0000` at the second call.
    #[test]
    fn a_new_mode_alone_starts_no_read() {
        let (_root, path, mut token) = cached(TokenRule::DOOR);
        write_line(&path, FIRST);

        assert_eq!(current(&mut token), token_bytes(FIRST));

        // The content changes, and each fact but the mode is put back.
        let before = readfile::facts(&path).unwrap();
        let time = modified(&path);
        write_line(&path, SECOND);
        set_modified(&path, time);
        fs::set_permissions(&path, fs::Permissions::from_mode(0o000)).unwrap();
        let after = readfile::facts(&path).unwrap();

        assert_eq!(compared(after), compared(before));
        assert_ne!(after.mode(), before.mode());
        // The old token proves that the call read no file.
        assert_eq!(current(&mut token), token_bytes(FIRST));
    }

    /// A rule with a mode check reads the mode only when the call reads the
    /// file. A wider mode alone thus leaves the token in use. The next read
    /// of the file refuses that mode.
    #[test]
    fn a_cached_token_checks_the_mode_only_at_a_read() {
        let (_root, path, mut token) = cached(TokenRule::ATTENDANCE_PEP_READ);
        write_line(&path, FIRST);

        assert_eq!(current(&mut token), token_bytes(FIRST));

        let before = readfile::facts(&path).unwrap();
        fs::set_permissions(&path, fs::Permissions::from_mode(0o644)).unwrap();
        let after = readfile::facts(&path).unwrap();

        assert_eq!(compared(after), compared(before));
        assert_eq!(current(&mut token), token_bytes(FIRST));

        // The size of the file moves, so the call reads the file.
        write_token(&path, format!("{SECOND}{SECOND}\n").as_bytes(), 0o644);

        assert_eq!(
            current(&mut token),
            Err(TokenError::ModeTooWide {
                allowed: ModeRule::OwnerAndGroupRead
            })
        );
    }

    /// The time of the last change is one of the four facts. A token of the
    /// same size in the same file is read when that time alone moved.
    #[test]
    fn a_new_time_alone_starts_a_read() {
        let (_root, path, mut token) = cached(TokenRule::DOOR);
        write_line(&path, FIRST);

        assert_eq!(current(&mut token), token_bytes(FIRST));

        let before = readfile::facts(&path).unwrap();
        let later = modified(&path) + Duration::from_secs(1);
        write_line(&path, SECOND);
        set_modified(&path, later);
        let after = readfile::facts(&path).unwrap();

        assert_eq!(
            (after.dev(), after.ino(), after.len()),
            (before.dev(), before.ino(), before.len())
        );
        assert_ne!(after.modified_ns(), before.modified_ns());
        assert_eq!(current(&mut token), token_bytes(SECOND));
    }

    /// The size is one of the four facts. A longer token in the same file is
    /// read when the size alone moved.
    #[test]
    fn a_new_size_alone_starts_a_read() {
        let (_root, path, mut token) = cached(TokenRule::DOOR);
        write_line(&path, FIRST);

        assert_eq!(current(&mut token), token_bytes(FIRST));

        let before = readfile::facts(&path).unwrap();
        let time = modified(&path);
        let longer = format!("{SECOND}{SECOND}");
        write_line(&path, &longer);
        set_modified(&path, time);
        let after = readfile::facts(&path).unwrap();

        assert_eq!(
            (after.dev(), after.ino(), after.modified_ns()),
            (before.dev(), before.ino(), before.modified_ns())
        );
        assert_ne!(after.len(), before.len());
        assert_eq!(current(&mut token), token_bytes(&longer));
    }

    /// Difference 2 of [`CachedToken::current`]. The Python cache keeps the
    /// facts and the token of its last good read
    /// (`chaperone/src/chaperone/delegate.py:196-201`).
    #[test]
    fn a_read_that_fails_drops_the_last_token() {
        let (_root, path, mut token) = cached(TokenRule::DOOR);
        write_line(&path, FIRST);

        assert_eq!(current(&mut token), token_bytes(FIRST));

        let first = readfile::facts(&path).unwrap();
        let time = modified(&path);
        write_line(&path, "tiny");

        assert_eq!(
            current(&mut token),
            Err(TokenError::TooShort { min_bytes: 32 })
        );
        assert!(token.last.is_none());

        // The file gets another token and each fact of the first read.
        write_line(&path, SECOND);
        set_modified(&path, time);

        assert_eq!(readfile::facts(&path), Some(first));
        assert_eq!(current(&mut token), token_bytes(SECOND));
    }

    // --- the bearer ---

    /// One header with the name `Authorization`.
    fn authorization(value: &[u8]) -> HeaderMap {
        let mut headers = HeaderMap::new();
        headers.insert(AUTHORIZATION, HeaderValue::from_bytes(value).unwrap());

        headers
    }

    #[test]
    fn the_bearer_is_the_rest_of_the_value_after_the_scheme() {
        const BOTH: &[BearerTrim] = &[BearerTrim::PythonStrip, BearerTrim::Exact];
        const STRIP: &[BearerTrim] = &[BearerTrim::PythonStrip];
        const EXACT: &[BearerTrim] = &[BearerTrim::Exact];
        // The bytes of the header value, each trim that the row is about and
        // the bearer.
        let table: &[HeaderRow] = &[
            (b"Bearer abc", BOTH, Some(b"abc")),
            (b"Bearer a b", BOTH, Some(b"a b")),
            (b"Bearer abc def", BOTH, Some(b"abc def")),
            // The space at the two ends.
            (b"Bearer  abc\t", STRIP, Some(b"abc")),
            (b"Bearer  abc\t", EXACT, Some(b" abc\t")),
            (b"Bearer ", BOTH, None),
            (b"Bearer    ", STRIP, None),
            (b"Bearer    ", EXACT, Some(b"   ")),
            // The scheme.
            (b"", BOTH, None),
            (b"Bearer", BOTH, None),
            (b"Bearerabc", BOTH, None),
            (b"bearer abc", BOTH, None),
            (b"BEARER abc", BOTH, None),
            (b"Basic abc", BOTH, None),
            (b"Bearer\tabc", BOTH, None),
            (b" Bearer abc", BOTH, None),
            (b"abc", BOTH, None),
            // One byte above 127 is one character of Latin-1. Its UTF-8
            // form has two bytes.
            (b"Bearer caf\xe9", BOTH, Some("caf\u{e9}".as_bytes())),
            // The UTF-8 bytes of that character are two characters of
            // Latin-1, so the bearer has four bytes for them.
            (
                b"Bearer caf\xc3\xa9",
                BOTH,
                Some("caf\u{c3}\u{a9}".as_bytes()),
            ),
            // The bytes 0x85 and 0xa0 are space of Python as Latin-1.
            (b"Bearer \xa0abc\x85", STRIP, Some(b"abc")),
            (
                b"Bearer \xa0abc\x85",
                EXACT,
                Some("\u{a0}abc\u{85}".as_bytes()),
            ),
            // U+00A0 as UTF-8 is `0xc2 0xa0`: the strip removes the second
            // character only.
            (b"Bearer abc\xc2\xa0", STRIP, Some("abc\u{c2}".as_bytes())),
            // No strip removes a control character that is no space.
            (b"Bearer \x7fabc\x00", BOTH, Some(b"\x7fabc\x00")),
        ];

        for (value, trims, bearer) in table {
            for trim in *trims {
                assert_eq!(
                    bearer_in(value, *trim).as_deref(),
                    *bearer,
                    "{value:?} {trim:?}"
                );

                // The last row holds two bytes that no header value holds.
                // Each other row is also a request.
                let Ok(header) = HeaderValue::from_bytes(value) else {
                    assert_eq!(value.last(), Some(&0x00));
                    continue;
                };
                let mut headers = HeaderMap::new();
                headers.insert(AUTHORIZATION, header);

                assert_eq!(
                    bearer_of(&headers, *trim).as_deref(),
                    *bearer,
                    "{value:?} {trim:?}"
                );
            }
        }
    }

    #[test]
    fn a_request_gives_the_bearer_of_its_first_authorization_header() {
        let mut two = HeaderMap::new();
        two.append(AUTHORIZATION, HeaderValue::from_static("Bearer first"));
        two.append(AUTHORIZATION, HeaderValue::from_static("Bearer second"));
        let mut other_name = HeaderMap::new();
        other_name.insert("x-authorization", HeaderValue::from_static("Bearer abc"));
        let mut other_case = HeaderMap::new();
        other_case.insert(
            ::http::HeaderName::from_bytes(b"Authorization").unwrap(),
            HeaderValue::from_static("Bearer abc"),
        );

        for trim in [BearerTrim::PythonStrip, BearerTrim::Exact] {
            assert_eq!(bearer_of(&two, trim), Some(b"first".to_vec()));
            assert_eq!(bearer_of(&other_name, trim), None);
            assert_eq!(bearer_of(&other_case, trim), Some(b"abc".to_vec()));
            assert_eq!(bearer_of(&HeaderMap::new(), trim), None);
        }
    }

    #[test]
    fn the_bearer_of_a_request_matches_the_token_of_the_service() {
        let token = Secret::try_from(String::from(CORE)).unwrap();
        let offered = bearer_of(
            &authorization(format!("Bearer {CORE}").as_bytes()),
            BearerTrim::Exact,
        );
        let other = bearer_of(
            &authorization(format!("Bearer {CORE}x").as_bytes()),
            BearerTrim::Exact,
        );

        assert!(offered.is_some_and(|bytes| token.matches(&bytes)));
        assert!(other.is_some_and(|bytes| !token.matches(&bytes)));
    }

    // --- the differential test ---

    /// The words that one Python reader has for its refusals.
    ///
    /// A [`TokenError`] names six causes. A Python reader has a word for some
    /// of them, and a vector records that word. A reader has no word for a
    /// cause that it cannot meet.
    #[derive(Debug, Clone, Copy, PartialEq, Eq)]
    enum Words {
        /// `attendance`: `unreadable`, `empty`, `short` and `mode`
        /// (`attendance/src/attendance/auth.py:242-265`). Its entry point
        /// returns nothing, so an accepted vector holds no token.
        Book,
        /// A door and the delegate client: `unreadable`, `not_utf8` and
        /// `short` (`door-owui/src/agent_door_owui/config.py:124-144`,
        /// `chaperone/src/chaperone/delegate.py:147-160`).
        Door,
        /// `caregiver`: `unreadable` and `empty`. `unreadable` is also its
        /// word for a file that is not UTF-8
        /// (`caregiver/src/caregiver/switch.py:86-92`).
        Caregiver,
        /// The trigger door: no word. The reader gives one answer for each
        /// file that it does not take
        /// (`door-trigger/src/agent_door_trigger/tokens.py:35-66`).
        NoWord,
    }

    impl Words {
        /// The word of the reader for `error`. `None` when the reader has no
        /// word for it.
        fn word(self, error: &TokenError) -> Option<&'static str> {
            match (self, error) {
                (Self::Book | Self::Door | Self::Caregiver, TokenError::Unreadable { .. })
                | (Self::Caregiver, TokenError::NotUtf8) => Some("unreadable"),
                (Self::Door, TokenError::NotUtf8) => Some("not_utf8"),
                (Self::Book | Self::Caregiver, TokenError::Empty) => Some("empty"),
                (Self::Book | Self::Door, TokenError::TooShort { .. }) => Some("short"),
                (Self::Book, TokenError::ModeTooWide { .. }) => Some("mode"),
                _ => None,
            }
        }

        /// Whether an accepted vector of the reader holds the token.
        fn records_the_token(self) -> bool {
            self != Self::Book
        }
    }

    /// One Python reader of a token file, the rule that stands for it and
    /// the words of the reader.
    struct Reader {
        surface: &'static str,
        rule: TokenRule,
        words: Words,
    }

    /// Each `runtime.token.` surface. The walk reads the table, so each
    /// surface of the table has a walk.
    const READERS: &[Reader] = &[
        Reader {
            surface: "runtime.token.attendance",
            rule: TokenRule::ATTENDANCE,
            words: Words::Book,
        },
        Reader {
            surface: "runtime.token.attendance_pep_read",
            rule: TokenRule::ATTENDANCE_PEP_READ,
            words: Words::Book,
        },
        Reader {
            surface: "runtime.token.door",
            rule: TokenRule::DOOR,
            words: Words::Door,
        },
        Reader {
            surface: "runtime.token.delegate",
            rule: TokenRule::DOOR,
            words: Words::Door,
        },
        Reader {
            surface: "runtime.token.webhook",
            rule: TokenRule::WEBHOOK,
            words: Words::NoWord,
        },
        Reader {
            surface: "runtime.token.caregiver",
            rule: TokenRule::NOT_EMPTY,
            words: Words::Caregiver,
        },
    ];

    /// One Python copy of the bearer reader, and the trim that stands for
    /// it.
    struct BearerCopy {
        surface: &'static str,
        trim: BearerTrim,
    }

    /// Each `runtime.bearer.` surface whose Python copy [`bearer_of`] ports.
    const COPIES: &[BearerCopy] = &[
        BearerCopy {
            surface: "runtime.bearer.attendance",
            trim: BearerTrim::PythonStrip,
        },
        BearerCopy {
            surface: "runtime.bearer.door_owui",
            trim: BearerTrim::Exact,
        },
        BearerCopy {
            surface: "runtime.bearer.door_trigger",
            trim: BearerTrim::PythonStrip,
        },
    ];

    /// The one surface of the bearer group that has no walk here.
    ///
    /// Its Python copy is the header reader of the chaperone
    /// (`chaperone/src/chaperone/app.py:350-355`). That reader takes the
    /// scheme in each case of letters, and [`bearer_of`] is no port of it:
    /// no value of [`BearerTrim`] stands for that rule. `AGENTS.md` of this
    /// crate lists the open point under "Known gaps".
    ///
    /// The test `no_trim_gives_each_result_of_the_surface_with_no_walk` holds
    /// that reason. It fails when a value of `BearerTrim` gives each result
    /// of the surface. Delete this constant then, and name the surface in
    /// [`COPIES`].
    const NO_PORT_HERE: &str = "runtime.bearer.chaperone";

    /// Each value of [`BearerTrim`].
    const EACH_TRIM: [BearerTrim; 2] = [BearerTrim::PythonStrip, BearerTrim::Exact];

    /// The start of the name of each surface of this module.
    const SURFACE_GROUPS: [&str; 2] = ["runtime.token.", "runtime.bearer."];

    /// The token that an accepted vector holds, when it holds one.
    fn python_token(vector: &Vector) -> Option<&str> {
        vector.value()?.get("token")?.as_str()
    }

    /// Makes sure that this module did with a token file what the Python
    /// reader did.
    fn same_as_the_reader(
        reader: &Reader,
        vector: &Vector,
        rust: &Result<Secret, TokenError>,
        at: &str,
    ) {
        match (vector.result(), rust) {
            (Outcome::Accepted, Ok(token)) => {
                let held = python_token(vector);

                assert_eq!(held.is_some(), reader.words.records_the_token(), "{at}");
                if let Some(text) = held {
                    assert!(token.matches(text.as_bytes()), "{at}: another token");
                }
            }
            (Outcome::Refused, Err(error)) => {
                let word = reader.words.word(error).map(Value::from);

                assert_eq!(vector.refusal(), word.as_ref(), "{at}: {error:?}");
            }
            // The Rust code refuses a file on which the Python code raises.
            (Outcome::Raised, Err(_)) => {}
            (python, rust) => panic!("{at}: the Python code {python}, the Rust code {rust:?}"),
        }
    }

    /// Puts the input of a token vector at `path`: the file with its mode,
    /// or no file for the one vector of a path with no file.
    fn place(path: &Path, vector: &Vector, at: &str) {
        if let Some(arguments) = vector.input().args() {
            assert_eq!(arguments.get("file"), Some(&Value::Null), "{at}");
            assert_eq!(arguments.len(), 1, "{at}");
            assert_eq!(vector.params(), None, "{at}");

            return;
        }

        let bytes = vector.input().bytes().unwrap();
        let mode = vector
            .params()
            .and_then(|params| params.get("mode"))
            .and_then(Value::as_str)
            .unwrap_or_else(|| panic!("{at}: the vector names no mode"));

        write_token(path, &bytes, u32::from_str_radix(mode, 8).unwrap());
    }

    /// Walks each vector of one token surface.
    fn walk_reader(reader: &Reader) {
        let surface = vectors::surface(reader.surface).unwrap();
        let root = TempRoot::new().unwrap();

        for (number, vector) in surface.vectors().iter().enumerate() {
            let at = format!("{} {}", reader.surface, vector.id());
            let path = root.path().join(format!("token-{number}"));
            place(&path, vector, &at);

            same_as_the_reader(reader, vector, &read(&path, reader.rule), &at);
        }

        assert!(!surface.vectors().is_empty(), "{}", reader.surface);
    }

    /// The bytes of the `Authorization` header of a bearer vector. `None`
    /// for a request with no such header.
    fn header_of(vector: &Vector, at: &str) -> Option<Vec<u8>> {
        let value = vector
            .input()
            .args()
            .and_then(|arguments| arguments.get("authorization"))
            .unwrap_or_else(|| panic!("{at}: the vector names no header"));

        match value {
            Value::Null => None,
            Value::String(text) => Some(text.clone().into_bytes()),
            // Bytes that are not UTF-8 are a `$base64` marker.
            other => match Marker::of(other) {
                Ok(Some(Marker::Base64(bytes))) => Some(bytes),
                marker => panic!("{at}: the header is {marker:?}"),
            },
        }
    }

    /// The bearer that this module finds in a request with the header
    /// `header`.
    ///
    /// The function builds the request when a header value can hold the
    /// bytes. `bearer_of` must then give what `bearer_in` gives.
    ///
    /// For bytes that no header value holds, the result is the result of
    /// `bearer_in` alone. No request gives a Rust service those bytes, so
    /// such a vector proves the private function and not a service. The
    /// vector `byte-1c-at-the-end` holds such bytes.
    fn offered_in(header: Option<&[u8]>, trim: BearerTrim, at: &str) -> Option<Vec<u8>> {
        let Some(header) = header else {
            return bearer_of(&HeaderMap::new(), trim);
        };
        let offered = bearer_in(header, trim);

        if let Ok(value) = HeaderValue::from_bytes(header) {
            let mut headers = HeaderMap::new();
            headers.insert(AUTHORIZATION, value);

            assert_eq!(bearer_of(&headers, trim), offered, "{at}");
        }

        offered
    }

    /// Makes sure that this module takes a request only when the Python copy
    /// takes it. `takes` says if the bearer of the request matches the token
    /// of the service. The Rust code refuses a request on which the Python
    /// code raises.
    fn same_as_the_copy(vector: &Vector, takes: bool, at: &str) {
        assert_eq!(
            takes,
            vector.result() == Outcome::Accepted,
            "{at}: the Python code {}",
            vector.result()
        );
    }

    /// Whether this module takes the request of one bearer vector under
    /// `trim`: the bearer of the request matches the token of the service.
    fn takes(surface: &Surface, vector: &Vector, trim: BearerTrim, at: &str) -> bool {
        let held = surface
            .context()
            .get("token")
            .and_then(Value::as_str)
            .unwrap();
        // A vector with `params.token` is for a service that holds that
        // token.
        let token = vector
            .params()
            .and_then(|params| params.get("token"))
            .and_then(Value::as_str)
            .unwrap_or(held);
        let token = Secret::try_from(token.to_owned()).unwrap();
        let header = header_of(vector, at);

        offered_in(header.as_deref(), trim, at).is_some_and(|offered| token.matches(&offered))
    }

    /// Walks each vector of one bearer surface.
    fn walk_copy(copy: &BearerCopy) {
        let surface = vectors::surface(copy.surface).unwrap();

        for vector in surface.vectors() {
            let at = format!("{} {}", copy.surface, vector.id());

            same_as_the_copy(vector, takes(&surface, vector, copy.trim, &at), &at);
        }

        assert!(!surface.vectors().is_empty(), "{}", copy.surface);
    }

    /// Whether this module and the Python copy of `surface` give two results
    /// for one vector or more, under `trim`.
    fn differs_under(surface: &Surface, trim: BearerTrim) -> bool {
        surface.vectors().iter().any(|vector| {
            let at = format!("{} {}", surface.name(), vector.id());

            takes(surface, vector, trim, &at) != (vector.result() == Outcome::Accepted)
        })
    }

    #[test]
    fn the_tables_hold_each_surface_of_this_module_one_time() {
        let listed: Vec<&str> = READERS
            .iter()
            .map(|reader| reader.surface)
            .chain(COPIES.iter().map(|copy| copy.surface))
            .chain([NO_PORT_HERE])
            .collect();
        let unique: HashSet<&str> = listed.iter().copied().collect();
        let index = vectors::index().unwrap();
        let in_index: HashSet<&str> = index
            .iter()
            .map(IndexRow::surface)
            .filter(|surface| {
                SURFACE_GROUPS
                    .iter()
                    .any(|group| surface.starts_with(group))
            })
            .collect();

        assert_eq!(unique.len(), listed.len(), "one surface is there twice");
        assert_eq!(unique, in_index);
    }

    #[test]
    fn each_reader_has_a_word_only_for_a_cause_that_it_can_meet() {
        let causes = [
            TokenError::Unreadable {
                os_text: String::from(NO_SUCH_FILE),
            },
            TokenError::TooLarge,
            TokenError::Empty,
            TokenError::TooShort { min_bytes: 32 },
            TokenError::ModeTooWide {
                allowed: ModeRule::OwnerOnly,
            },
            TokenError::NotUtf8,
        ];
        let words_of = |words: Words| -> Vec<Option<&str>> {
            causes.iter().map(|cause| words.word(cause)).collect()
        };

        assert_eq!(
            words_of(Words::Book),
            [
                Some("unreadable"),
                None,
                Some("empty"),
                Some("short"),
                Some("mode"),
                None
            ]
        );
        assert_eq!(
            words_of(Words::Door),
            [
                Some("unreadable"),
                None,
                None,
                Some("short"),
                None,
                Some("not_utf8")
            ]
        );
        assert_eq!(
            words_of(Words::Caregiver),
            [
                Some("unreadable"),
                None,
                Some("empty"),
                None,
                None,
                Some("unreadable")
            ]
        );
        assert_eq!(words_of(Words::NoWord), [None; 6]);
    }

    #[test]
    fn each_token_vector_is_what_its_python_reader_does() {
        for reader in READERS {
            walk_reader(reader);
        }
    }

    #[test]
    fn each_bearer_vector_is_what_its_python_copy_does() {
        for copy in COPIES {
            walk_copy(copy);
        }
    }

    /// The reason for [`NO_PORT_HERE`] holds: under each value of
    /// [`BearerTrim`], this module and the Python copy give two results for
    /// one vector or more of that surface.
    ///
    /// When this test fails, one value of `BearerTrim` gives each result of
    /// the Python copy. Delete `NO_PORT_HERE` then, and name the surface in
    /// [`COPIES`] with that value.
    #[test]
    fn no_trim_gives_each_result_of_the_surface_with_no_walk() {
        let surface = vectors::surface(NO_PORT_HERE).unwrap();

        for trim in EACH_TRIM {
            assert!(
                differs_under(&surface, trim),
                "{trim:?} gives each result of {NO_PORT_HERE}: name the surface in COPIES"
            );
        }
    }

    /// A test of the test: `differs_under` finds no difference on a surface
    /// of [`COPIES`] under the trim of its row. It finds one under each
    /// other trim.
    #[test]
    fn the_check_of_a_difference_reads_the_trim() {
        for copy in COPIES {
            let surface = vectors::surface(copy.surface).unwrap();

            for trim in EACH_TRIM {
                assert_eq!(
                    differs_under(&surface, trim),
                    trim != copy.trim,
                    "{} {trim:?}",
                    copy.surface
                );
            }
        }
    }

    /// A test of the test: the walk fails for a refusal with a word that is
    /// not the word of the reader.
    #[test]
    #[should_panic(expected = "runtime.token.caregiver absent")]
    fn the_walk_fails_for_another_word_of_the_reader() {
        let surface = vectors::surface("runtime.token.caregiver").unwrap();
        let vector = surface.vector("absent").unwrap();
        let reader = READERS
            .iter()
            .find(|reader| reader.surface == surface.name())
            .unwrap();

        assert_eq!(vector.refusal(), Some(&Value::from("unreadable")));
        same_as_the_reader(
            reader,
            vector,
            &Err(TokenError::Empty),
            "runtime.token.caregiver absent",
        );
    }

    /// A test of the test: the walk fails when the Rust code takes a file
    /// that the Python reader refuses.
    #[test]
    #[should_panic(expected = "the Python code refused")]
    fn the_walk_fails_for_a_file_that_only_the_rust_code_takes() {
        let surface = vectors::surface("runtime.token.webhook").unwrap();
        let vector = surface.vector("absent").unwrap();
        let reader = READERS
            .iter()
            .find(|reader| reader.surface == surface.name())
            .unwrap();
        let token = Secret::try_from(String::from(CORE)).unwrap();

        same_as_the_reader(reader, vector, &Ok(token), "runtime.token.webhook absent");
    }

    /// A test of the test: the walk fails when the Rust code gives another
    /// token than the Python reader.
    #[test]
    #[should_panic(expected = "runtime.token.door 32-bytes: another token")]
    fn the_walk_fails_for_another_token() {
        let surface = vectors::surface("runtime.token.door").unwrap();
        let vector = surface.vector("32-bytes").unwrap();
        let reader = READERS
            .iter()
            .find(|reader| reader.surface == surface.name())
            .unwrap();
        let other = Secret::try_from(String::from(SECOND)).unwrap();

        assert_eq!(python_token(vector), Some(CORE));
        same_as_the_reader(reader, vector, &Ok(other), "runtime.token.door 32-bytes");
    }

    /// A test of the test: the walk fails for an accepted vector with no
    /// token, when the reader of the table records the token.
    #[test]
    #[should_panic(expected = "a door reader and a vector with no token")]
    fn the_walk_fails_for_a_vector_with_no_token_of_its_reader() {
        let surface = vectors::surface("runtime.token.attendance").unwrap();
        let vector = surface.vector("32-bytes").unwrap();
        let reader = READERS
            .iter()
            .find(|reader| reader.surface == "runtime.token.door")
            .unwrap();
        let token = Secret::try_from(String::from(CORE)).unwrap();

        assert_eq!(python_token(vector), None);
        same_as_the_reader(
            reader,
            vector,
            &Ok(token),
            "a door reader and a vector with no token",
        );
    }

    /// A test of the test: the walk fails when the Rust code takes a request
    /// that the Python copy refuses.
    #[test]
    #[should_panic(expected = "runtime.bearer.door_owui wrong-token: the Python code refused")]
    fn the_walk_fails_for_a_request_that_only_the_rust_code_takes() {
        let surface = vectors::surface("runtime.bearer.door_owui").unwrap();
        let vector = surface.vector("wrong-token").unwrap();

        same_as_the_copy(vector, true, "runtime.bearer.door_owui wrong-token");
    }

    /// A test of the test: the walk fails when the Rust code refuses a
    /// request that the Python copy takes.
    #[test]
    #[should_panic(expected = "runtime.bearer.door_owui exact: the Python code accepted")]
    fn the_walk_fails_for_a_request_that_only_the_python_copy_takes() {
        let surface = vectors::surface("runtime.bearer.door_owui").unwrap();
        let vector = surface.vector("exact").unwrap();

        same_as_the_copy(vector, false, "runtime.bearer.door_owui exact");
    }

    /// The vector `byte-1c-at-the-end` holds a control byte. No header value
    /// of the HTTP types holds that byte, so no request gives it to
    /// `bearer_of`. The walk gives the bytes to `bearer_in` for that reason.
    #[test]
    fn a_header_value_holds_no_control_byte() {
        let surface = vectors::surface("runtime.bearer.attendance").unwrap();
        let vector = surface.vector("byte-1c-at-the-end").unwrap();
        let header = header_of(vector, "byte-1c-at-the-end").unwrap();
        let token = surface.context().get("token").and_then(Value::as_str);

        assert_eq!(header.last(), Some(&0x1c));
        assert!(HeaderValue::from_bytes(&header).is_err());
        assert_eq!(
            bearer_in(&header, BearerTrim::PythonStrip),
            token.map(|token| token.as_bytes().to_vec())
        );
    }
}
