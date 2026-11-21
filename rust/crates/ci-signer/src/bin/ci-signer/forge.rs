//! The Forgejo client of the signer: the three calls of sections 4.4 and
//! 4.5, each on a TCP connection of its own, with one bound.
//!
//! | Call | Token | Section |
//! |---|---|---|
//! | `GET /api/v1/repos/{o}/{r}/actions/runs/{id}` | `release-reader` | 4.4 |
//! | `GET /api/v1/repos/{o}/{r}/commits/{sha}/pull` | `release-reader` | 4.4, check 4 |
//! | `POST /api/v1/repos/{o}/{r}/tags` | the bot `ci-signer` | 4.5 step 7 |
//!
//! Each path comes from typed names only, so no path holds a byte that
//! needs an escape. The client gives each reply as a `Reply` of the core,
//! whose readers check the status, the cap and the strict JSON. The token
//! goes in the header `Authorization: token <t>` (M9), never in a path or
//! a log line.

use std::fmt;
use std::net::SocketAddr;
use std::str::FromStr;
use std::time::Duration;

use creche_ci_signer::forgejo::{REPLY_MAX_BYTES, Reply};
use creche_ci_signer::names::{CommitSha, ReleaseTag, RepoFullName};
use http_body_util::{BodyExt, Full, LengthLimitError, Limited};
use hyper::body::Bytes;
use hyper::client::conn::http1;
use hyper::header::{ACCEPT, AUTHORIZATION, CONTENT_TYPE, HOST, HeaderValue};
use hyper::{Method, Request};
use hyper_util::rt::TokioIo;
use serde::Serialize;
use tokio::net::TcpStream;
use tokio::task::JoinHandle;
use tokio::time::timeout;

use creche_ci_signer::eligibility::RunKey;

/// The scheme of the Forgejo origin.
const SCHEME: &str = "http://";

/// The base of each API path.
const API: &str = "/api/v1/repos";

/// The media type of each body.
const JSON: &str = "application/json";

/// The longest TCP connect. A connect on the LAN takes a few milliseconds.
const CONNECT_WAIT: Duration = Duration::from_secs(3);

/// The bytes of a Forgejo token: 40 lower-case hex digits, the form of
/// `CRECHE_READ_TOKEN` of the proc suite.
const TOKEN_BYTES: usize = 40;

// CONTRACT-QUESTION: section 4.1 of the note draws HTTPS from the signer
// to Forgejo, and question 4 (HTTPS on the Forgejo host) is an operator
// step of the window, after the install of the signer (section 7, before
// the window, steps 3 and 6). The runner also speaks plain HTTP to an IP
// address on the LAN (the runner design, section 14, question 1). The
// reading taken: the signer speaks plain HTTP to an IP address on the
// LAN, as the runner does, and refuses `https://`. A change to HTTPS adds
// a TLS crate and a CA file, and changes this type and `exchange` only.
/// Where Forgejo listens: `http://<address>:<port>`, for example
/// `http://192.0.2.10:3000`. The address is an IP literal, so the signer
/// does no DNS lookup.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ForgejoOrigin {
    address: SocketAddr,
    host: HeaderValue,
}

/// Why a text is not a Forgejo origin.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct OriginError;

impl fmt::Display for OriginError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str("a Forgejo origin is http://<address>:<port> with an IP address")
    }
}

impl std::error::Error for OriginError {}

impl FromStr for ForgejoOrigin {
    type Err = OriginError;

    fn from_str(text: &str) -> Result<Self, Self::Err> {
        let rest = text.strip_prefix(SCHEME).ok_or(OriginError)?;
        let address: SocketAddr = rest.parse().map_err(|_| OriginError)?;
        // The canonical text only: `http://192.0.2.10:03000` is refused.
        if address.to_string() != rest {
            return Err(OriginError);
        }
        let host = HeaderValue::from_str(rest).map_err(|_| OriginError)?;
        Ok(Self { address, host })
    }
}

impl fmt::Display for ForgejoOrigin {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(formatter, "{SCHEME}{}", self.address)
    }
}

/// A Forgejo token: 40 lower-case hex digits. `Debug` and `Display` print
/// no digit of it.
pub struct Token(String);

/// Why a text is not a token. It names no byte of the text.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct TokenError;

impl fmt::Display for TokenError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str("a token is 40 lower-case hex digits and an optional LF")
    }
}

impl std::error::Error for TokenError {}

impl Token {
    /// The token of the bytes of a credential file: 40 lower-case hex
    /// digits, then at most one LF.
    ///
    /// # Errors
    ///
    /// `TokenError` for each other text.
    pub fn parse(bytes: &[u8]) -> Result<Self, TokenError> {
        let digits = bytes.strip_suffix(b"\n").unwrap_or(bytes);
        let hex = digits
            .iter()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(byte));
        if digits.len() != TOKEN_BYTES || !hex {
            return Err(TokenError);
        }
        let text = String::from_utf8(digits.to_vec()).map_err(|_| TokenError)?;
        Ok(Self(text))
    }

    fn header(&self) -> Result<HeaderValue, Fault> {
        let mut value =
            HeaderValue::from_str(&format!("token {}", self.0)).map_err(|_| Fault::Request)?;
        value.set_sensitive(true);
        Ok(value)
    }
}

impl fmt::Debug for Token {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str("Token(..)")
    }
}

/// Why a call gave no reply. It holds no byte of a reply and no token.
#[derive(Debug)]
pub enum Fault {
    /// The request could not be built.
    Request,
    /// The connect did not end within its bound.
    ConnectTimeout,
    /// The connect failed.
    Connect(std::io::Error),
    /// The HTTP exchange failed.
    Http(hyper::Error),
    /// The body passed `REPLY_MAX_BYTES`.
    TooLarge,
    /// The read of the body failed.
    Body,
    /// The call did not end within its bound.
    Timeout,
}

impl Fault {
    /// A stable word for the log.
    pub fn code(&self) -> &'static str {
        match self {
            Self::Request => "request",
            Self::ConnectTimeout => "connect_timeout",
            Self::Connect(_) => "connect",
            Self::Http(_) => "http",
            Self::TooLarge => "too_large",
            Self::Body => "body",
            Self::Timeout => "timeout",
        }
    }
}

impl fmt::Display for Fault {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Connect(error) => write!(formatter, "the connect to Forgejo failed: {error}"),
            Self::Http(error) => {
                write!(formatter, "the HTTP exchange with Forgejo failed: {error}")
            }
            other => write!(formatter, "the call to Forgejo failed: {}", other.code()),
        }
    }
}

impl std::error::Error for Fault {}

/// The body of `POST /repos/{o}/{r}/tags`: an annotated tag at a commit
/// (M11, measured on a scratch repository on 2026-11-16).
#[derive(Serialize)]
struct CreateTag<'a> {
    tag_name: &'a str,
    target: &'a str,
    message: &'a str,
}

/// The client of one Forgejo instance.
#[derive(Debug, Clone)]
pub struct Forge {
    origin: ForgejoOrigin,
}

impl Forge {
    /// The client of `origin`.
    pub fn new(origin: ForgejoOrigin) -> Self {
        Self { origin }
    }

    /// `GET /repos/{o}/{r}/actions/runs/{id}` of the key, within `wait`.
    pub async fn run(&self, token: &Token, key: &RunKey, wait: Duration) -> Result<Reply, Fault> {
        let path = format!("{API}/{}/actions/runs/{}", key.repository, key.run);
        self.call(Method::GET, &path, token, None, wait).await
    }

    /// `GET /repos/{o}/{r}/commits/{sha}/pull`, within `wait`.
    pub async fn commit_pull(
        &self,
        token: &Token,
        repository: &RepoFullName,
        commit: &CommitSha,
        wait: Duration,
    ) -> Result<Reply, Fault> {
        let path = format!("{API}/{repository}/commits/{commit}/pull");
        self.call(Method::GET, &path, token, None, wait).await
    }

    /// `POST /repos/{o}/{r}/tags`: the annotated tag `tag` at `commit`
    /// with `message`, within `wait`.
    pub async fn create_tag(
        &self,
        token: &Token,
        repository: &RepoFullName,
        tag: &ReleaseTag,
        commit: &CommitSha,
        message: &str,
        wait: Duration,
    ) -> Result<Reply, Fault> {
        let path = format!("{API}/{repository}/tags");
        let body = serde_json::to_vec(&CreateTag {
            tag_name: tag.as_str(),
            target: commit.as_str(),
            message,
        })
        .map_err(|_| Fault::Request)?;
        self.call(Method::POST, &path, token, Some(body), wait)
            .await
    }

    async fn call(
        &self,
        method: Method,
        path: &str,
        token: &Token,
        body: Option<Vec<u8>>,
        wait: Duration,
    ) -> Result<Reply, Fault> {
        let mut builder = Request::builder()
            .method(method)
            .uri(path)
            .header(HOST, self.origin.host.clone())
            .header(ACCEPT, JSON)
            .header(AUTHORIZATION, token.header()?);
        if body.is_some() {
            builder = builder.header(CONTENT_TYPE, JSON);
        }
        let request = builder
            .body(Full::new(Bytes::from(body.unwrap_or_default())))
            .map_err(|_| Fault::Request)?;
        timeout(wait, exchange(self.origin.address, request, wait))
            .await
            .map_err(|_| Fault::Timeout)?
    }
}

/// The task that drives one connection. Its drop ends the task, so no
/// connection outlives its call.
struct Driver(JoinHandle<()>);

impl Drop for Driver {
    fn drop(&mut self) {
        self.0.abort();
    }
}

/// Connects, sends `request` and reads the status and at most
/// `REPLY_MAX_BYTES` of the body.
async fn exchange(
    address: SocketAddr,
    request: Request<Full<Bytes>>,
    wait: Duration,
) -> Result<Reply, Fault> {
    let stream = timeout(CONNECT_WAIT.min(wait), TcpStream::connect(address))
        .await
        .map_err(|_| Fault::ConnectTimeout)?
        .map_err(Fault::Connect)?;
    let (mut sender, connection) = http1::handshake(TokioIo::new(stream))
        .await
        .map_err(Fault::Http)?;
    let _driver = Driver(tokio::spawn(async move {
        let _ended = connection.await;
    }));

    let response = sender.send_request(request).await.map_err(Fault::Http)?;
    let status = response.status().as_u16();
    let body = Limited::new(response.into_body(), REPLY_MAX_BYTES)
        .collect()
        .await
        .map_err(|error| {
            if error.is::<LengthLimitError>() {
                Fault::TooLarge
            } else {
                Fault::Body
            }
        })?
        .to_bytes();
    Ok(Reply {
        status,
        body: body.to_vec(),
    })
}

#[cfg(test)]
mod tests {
    use super::{ForgejoOrigin, OriginError, Token, TokenError};

    #[test]
    fn an_origin_is_plain_http_to_an_ip_address() {
        for text in ["http://192.0.2.10:3000", "http://[2001:db8::1]:3000"] {
            let origin: ForgejoOrigin = text.parse().unwrap();
            assert_eq!(origin.to_string(), text);
        }
        for text in [
            "192.0.2.10:3000",
            "https://192.0.2.10:3000",
            "http://192.0.2.10",
            "http://192.0.2.10:3000/",
            "http://192.0.2.10:3000/api/v1",
            "http://forgejo.invalid:3000",
            "http://user@192.0.2.10:3000",
            "http://192.0.2.10:03000",
            "HTTP://192.0.2.10:3000",
        ] {
            assert_eq!(text.parse::<ForgejoOrigin>(), Err(OriginError), "{text}");
        }
    }

    #[test]
    fn a_token_is_40_hex_digits_and_prints_none() {
        let digits = "0123456789abcdef0123456789abcdef01234567";
        let token = Token::parse(digits.as_bytes()).unwrap();
        assert_eq!(format!("{token:?}"), "Token(..)");
        assert!(Token::parse(format!("{digits}\n").as_bytes()).is_ok());
        for text in [
            String::new(),
            digits.to_uppercase(),
            format!("{digits}\n\n"),
            format!("{digits}0"),
            format!(" {digits}"),
            digits.replace('a', "g"),
            format!("{digits}\r\n"),
        ] {
            assert_eq!(
                Token::parse(text.as_bytes()).map(drop),
                Err(TokenError),
                "{text:?}"
            );
        }
    }
}
