//! The two secrets of the client (design section 6, "The registration
//! credential").
//!
//! 1. `ApiToken`: the token of the bot user that registers each runner.
//!    It stays on the host, in a file that only the daemon user reads.
//!    `ApiToken::read` reads it from the path that the caller gives.
//! 2. `RunnerToken`: the token of one ephemeral runner, from the
//!    registration reply. A later PR writes it into the seed of one VM.
//!
//! Neither type has `Display`, and `Debug` prints only the type name. So a
//! log line or an error that holds one shows no byte of the secret.

use std::fmt;
use std::fs::File;
use std::io::{self, Read};
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};

use hyper::header::HeaderValue;
use serde::Deserialize;

/// The length of a Forgejo token. An access token and a runner token both
/// have 40 characters.
const TOKEN_LEN: usize = 40;

/// The most bytes of a token file: the token and one newline.
const TOKEN_FILE_CAP: usize = TOKEN_LEN + 1;

/// The mode bits that refuse a token file: any bit of others, and the
/// write bit of the group. The design gives the file to the daemon user
/// only. A group that reads it is allowed, for a root-owned file of the
/// group of the daemon user.
const OPEN_MODE_BITS: u32 = 0o027;

/// The prefix of the `Authorization` value of the Forgejo API.
const TOKEN_SCHEME: &str = "token ";

/// Whether `text` has the shape of a Forgejo token: 40 ASCII letters or
/// digits. Forgejo writes hex, and this rule also takes the other
/// letters, so a change of the alphabet does not stop the daemon. Each
/// such text is safe in a header and in a file.
fn is_token(text: &str) -> bool {
    text.len() == TOKEN_LEN && text.bytes().all(|byte| byte.is_ascii_alphanumeric())
}

/// The token of the bot user, as the client sends it.
#[derive(Clone)]
pub struct ApiToken {
    /// The token itself. The client removes it from each error message
    /// of Forgejo.
    text: String,
    /// `token <text>`, marked sensitive, for the `Authorization` header.
    header: HeaderValue,
}

impl ApiToken {
    /// Reads the token from the file at `path`. The file holds the token
    /// and at most one newline after it.
    ///
    /// # Errors
    ///
    /// `TokenFileError` when the file does not open, is not a regular
    /// file, is open to others, is too large or holds no token. The error
    /// holds no byte of the file.
    pub fn read(path: &Path) -> Result<Self, TokenFileError> {
        let fail = |fault| TokenFileError {
            path: path.to_path_buf(),
            fault,
        };

        let file = File::open(path).map_err(|error| fail(TokenFault::Io(error)))?;

        // The check reads the mode of the open file, so a swap of the path
        // after the open changes nothing.
        let metadata = file
            .metadata()
            .map_err(|error| fail(TokenFault::Io(error)))?;
        if !metadata.is_file() {
            return Err(fail(TokenFault::NotFile));
        }
        let mode = metadata.permissions().mode();
        if mode & OPEN_MODE_BITS != 0 {
            return Err(fail(TokenFault::OpenToOthers(mode & 0o7777)));
        }

        let mut bytes = Vec::new();
        // One byte past the cap shows a file that is too large.
        let take = u64::try_from(TOKEN_FILE_CAP.saturating_add(1)).unwrap_or(u64::MAX);
        file.take(take)
            .read_to_end(&mut bytes)
            .map_err(|error| fail(TokenFault::Io(error)))?;
        if bytes.len() > TOKEN_FILE_CAP {
            return Err(fail(TokenFault::TooLarge));
        }

        let text = String::from_utf8(bytes).map_err(|_| fail(TokenFault::NotToken))?;
        let token = text.strip_suffix('\n').unwrap_or(&text);
        Self::new(token).ok_or_else(|| fail(TokenFault::NotToken))
    }

    /// The token of `text`, or `None` when `text` is not a token.
    fn new(text: &str) -> Option<Self> {
        if !is_token(text) {
            return None;
        }
        let mut header = HeaderValue::from_str(&format!("{TOKEN_SCHEME}{text}")).ok()?;
        header.set_sensitive(true);
        Some(Self {
            text: text.to_owned(),
            header,
        })
    }

    /// The value of the `Authorization` header.
    pub(crate) fn header(&self) -> &HeaderValue {
        &self.header
    }

    /// `message` with each copy of the token replaced by `<token>`.
    /// Forgejo puts the token it got into some error messages.
    pub(crate) fn redact(&self, message: &str) -> String {
        message.replace(&self.text, "<token>")
    }
}

impl fmt::Debug for ApiToken {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str("ApiToken(..)")
    }
}

/// Why `ApiToken::read` failed.
#[derive(Debug)]
pub enum TokenFault {
    /// The open, the stat or the read failed.
    Io(io::Error),
    /// The path is not a regular file.
    NotFile,
    /// The mode gives others a bit, or the group the write bit. The value
    /// is the mode.
    OpenToOthers(u32),
    /// The file holds more than the token and one newline.
    TooLarge,
    /// The file does not hold 40 ASCII letters or digits.
    NotToken,
}

/// The token file and why it does not give a token.
#[derive(Debug)]
pub struct TokenFileError {
    /// The path that the caller gave.
    path: PathBuf,
    /// Why the read failed.
    fault: TokenFault,
}

impl TokenFileError {
    /// The path that the caller gave.
    #[must_use]
    pub fn path(&self) -> &Path {
        &self.path
    }

    /// Why the read failed.
    #[must_use]
    pub fn fault(&self) -> &TokenFault {
        &self.fault
    }
}

impl fmt::Display for TokenFileError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        let path = self.path.display();
        match &self.fault {
            TokenFault::Io(error) => {
                write!(formatter, "the token file {path} does not read: {error}")
            }
            TokenFault::NotFile => write!(formatter, "the token file {path} is not a regular file"),
            TokenFault::OpenToOthers(mode) => write!(
                formatter,
                "the token file {path} has mode {mode:o}: others or the group can get it"
            ),
            TokenFault::TooLarge => write!(
                formatter,
                "the token file {path} holds more than one token and one newline"
            ),
            TokenFault::NotToken => write!(
                formatter,
                "the token file {path} does not hold 40 ASCII letters or digits"
            ),
        }
    }
}

impl std::error::Error for TokenFileError {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match &self.fault {
            TokenFault::Io(error) => Some(error),
            _ => None,
        }
    }
}

/// The token of one ephemeral runner. Forgejo gives it once, in the
/// registration reply.
#[derive(Clone, PartialEq, Eq, Deserialize)]
#[serde(try_from = "String")]
pub struct RunnerToken(String);

impl RunnerToken {
    /// The token as text, for the one seed file that carries it.
    #[must_use]
    pub fn reveal(&self) -> &str {
        &self.0
    }
}

/// Why a text is not a runner token. It holds no byte of the text.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct NotToken;

impl fmt::Display for NotToken {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str("a runner token is 40 ASCII letters or digits")
    }
}

impl std::error::Error for NotToken {}

impl TryFrom<String> for RunnerToken {
    type Error = NotToken;

    fn try_from(text: String) -> Result<Self, Self::Error> {
        if !is_token(&text) {
            return Err(NotToken);
        }
        Ok(Self(text))
    }
}

impl fmt::Debug for RunnerToken {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str("RunnerToken(..)")
    }
}

#[cfg(test)]
mod tests {
    use std::fs;
    use std::os::unix::fs::PermissionsExt;
    use std::path::PathBuf;

    use super::{ApiToken, NotToken, RunnerToken, TokenFault};

    const TOKEN: &str = "3f9a0c1e7b2d4f6a8c0e1b3d5f7a9c2e4b6d8f0a";

    /// `TOKEN` less its first character: 39 characters.
    const SHORT: &str = "f9a0c1e7b2d4f6a8c0e1b3d5f7a9c2e4b6d8f0a";

    /// A fresh dir of this test process under the temp dir.
    fn scratch(name: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!("ci-runner-{}-{name}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        dir
    }

    /// Writes `bytes` to `name` in `dir` with `mode`.
    fn token_file(dir: &std::path::Path, name: &str, bytes: &[u8], mode: u32) -> PathBuf {
        let path = dir.join(name);
        fs::write(&path, bytes).unwrap();
        fs::set_permissions(&path, fs::Permissions::from_mode(mode)).unwrap();
        path
    }

    #[test]
    fn a_token_file_reads_with_or_without_one_newline() {
        let dir = scratch("read");
        for (name, bytes, mode) in [
            ("bare", TOKEN.to_owned(), 0o600),
            ("newline", format!("{TOKEN}\n"), 0o400),
            ("group", format!("{TOKEN}\n"), 0o640),
        ] {
            let path = token_file(&dir, name, bytes.as_bytes(), mode);
            let token = ApiToken::read(&path).unwrap();
            assert_eq!(token.header().to_str().unwrap(), format!("token {TOKEN}"));
            assert!(token.header().is_sensitive());
        }
        fs::remove_dir_all(dir).unwrap();
    }

    #[test]
    fn a_bad_token_file_is_refused_and_the_error_shows_no_byte_of_it() {
        let dir = scratch("refuse");
        let cases: [(&str, String, u32); 7] = [
            ("other-read", TOKEN.to_owned(), 0o604),
            ("group-write", TOKEN.to_owned(), 0o620),
            ("two-newlines", format!("{TOKEN}\n\n"), 0o600),
            ("short", SHORT.to_owned(), 0o600),
            ("space", format!("{SHORT} "), 0o600),
            ("crlf", format!("{SHORT}\r\n"), 0o600),
            ("long", format!("{TOKEN}{TOKEN}"), 0o600),
        ];
        for (name, bytes, mode) in cases {
            let path = token_file(&dir, name, bytes.as_bytes(), mode);
            let error = ApiToken::read(&path).unwrap_err();
            assert_eq!(error.path(), path);
            let shown = format!("{error} {error:?}");
            assert!(!shown.contains(SHORT), "{name}: {shown}");
        }

        let missing = ApiToken::read(&dir.join("missing")).unwrap_err();
        assert!(matches!(missing.fault(), TokenFault::Io(_)));
        let not_file = ApiToken::read(&dir).unwrap_err();
        assert!(matches!(not_file.fault(), TokenFault::NotFile));
        fs::remove_dir_all(dir).unwrap();
    }

    #[test]
    fn no_token_prints_in_debug_and_redact_hides_it() {
        let dir = scratch("debug");
        let path = token_file(&dir, "token", TOKEN.as_bytes(), 0o600);
        let token = ApiToken::read(&path).unwrap();
        assert_eq!(format!("{token:?}"), "ApiToken(..)");
        assert_eq!(
            token.redact(&format!("task with token \"{TOKEN}\": no [sha: {TOKEN}]")),
            "task with token \"<token>\": no [sha: <token>]"
        );
        fs::remove_dir_all(dir).unwrap();

        let runner = RunnerToken::try_from(TOKEN.to_owned()).unwrap();
        assert_eq!(runner.reveal(), TOKEN);
        assert_eq!(format!("{runner:?}"), "RunnerToken(..)");
        assert_eq!(RunnerToken::try_from(SHORT.to_owned()), Err(NotToken));
    }
}
