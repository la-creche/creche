//! The typed error of each API call. An error names the call and the
//! HTTP status. It holds no byte of either token.

use std::fmt;
use std::io;
use std::time::Duration;

use hyper::StatusCode;
use serde_json::error::Category;

use crate::names::{JobId, RunnerId};

/// The most characters of a Forgejo error message that an error keeps.
const MESSAGE_MAX: usize = 512;

/// One call of the client.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Call {
    /// `GET /orgs/{org}/actions/runners/jobs`.
    ListJobs,
    /// `POST /orgs/{org}/actions/runners`.
    Register,
    /// `DELETE /orgs/{org}/actions/runners/{id}`.
    Remove(RunnerId),
}

impl fmt::Display for Call {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::ListJobs => formatter.write_str("the list of waiting jobs"),
            Self::Register => formatter.write_str("the registration of a runner"),
            Self::Remove(runner) => write!(formatter, "the removal of runner {runner}"),
        }
    }
}

/// The `message` of a Forgejo error reply, cut to `MESSAGE_MAX`
/// characters, with each copy of the API token replaced by `<token>`.
/// `Display` escapes each control character, so a message cannot add a
/// line to a log.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ForgejoMessage(String);

impl ForgejoMessage {
    /// The message of an already redacted text.
    pub(crate) fn new(redacted: &str) -> Self {
        Self(redacted.chars().take(MESSAGE_MAX).collect())
    }

    /// The message as text, with no escape.
    #[must_use]
    pub fn as_str(&self) -> &str {
        &self.0
    }
}

impl fmt::Display for ForgejoMessage {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(formatter, "{}", self.0.escape_debug())
    }
}

/// Why a reply body is not the reply of its call.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ReplyFault {
    /// The body does not read as the reply type. The fault holds the kind
    /// and the place, and no text of the body: a serde text can quote a
    /// value of the body.
    Shape {
        /// Whether the body is cut short, is not JSON, or is JSON of
        /// another shape.
        category: Category,
        /// The line of the fault, from 1.
        line: usize,
        /// The column of the fault, from 1.
        column: usize,
    },
    /// A job of the waiting list does not have the status `waiting` and
    /// the task id 0.
    NotWaiting(JobId),
}

impl fmt::Display for ReplyFault {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Shape {
                category,
                line,
                column,
            } => {
                let kind = match category {
                    Category::Io => "does not read",
                    Category::Syntax => "is not JSON",
                    Category::Data => "is JSON of another shape",
                    Category::Eof => "is cut short",
                };
                write!(formatter, "the body {kind} at line {line}, column {column}")
            }
            Self::NotWaiting(job) => write!(formatter, "job {job} in the list is not waiting"),
        }
    }
}

/// Why a call failed.
#[derive(Debug)]
pub enum Fault {
    /// The TCP connect to Forgejo failed.
    Connect(io::Error),
    /// The call did not end within its bound.
    Timeout(Duration),
    /// The client did not make a valid HTTP request. The name types allow
    /// only characters that a request path can hold, so this fault shows
    /// a bug of the client.
    Build(hyper::http::Error),
    /// The HTTP exchange failed.
    Http(hyper::Error),
    /// The reply body did not read.
    Body(Box<dyn std::error::Error + Send + Sync>),
    /// The reply body is larger than the cap.
    TooLarge,
    /// Forgejo refused the API token: status 401. The fault holds no
    /// message, because Forgejo puts the token it got into it.
    Unauthorized,
    /// A status that the call does not take.
    Status {
        /// The status.
        status: StatusCode,
        /// The `message` of the body, when the body holds one.
        message: Option<ForgejoMessage>,
    },
    /// The expected status came with a body that is not the reply.
    Reply {
        /// The status.
        status: StatusCode,
        /// Why the body is not the reply.
        fault: ReplyFault,
    },
}

impl fmt::Display for Fault {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Connect(error) => write!(formatter, "the connect to Forgejo failed: {error}"),
            Self::Timeout(wait) => write!(formatter, "no reply within {} ms", wait.as_millis()),
            Self::Build(error) => write!(formatter, "the request is not valid HTTP: {error}"),
            Self::Http(error) => write!(formatter, "the HTTP exchange failed: {error}"),
            Self::Body(error) => write!(formatter, "the reply body did not read: {error}"),
            Self::TooLarge => formatter.write_str("the reply body is larger than the cap"),
            Self::Unauthorized => formatter.write_str("status 401: Forgejo refused the API token"),
            Self::Status {
                status,
                message: Some(message),
            } => write!(formatter, "status {}: {message}", status.as_u16()),
            Self::Status {
                status,
                message: None,
            } => write!(formatter, "status {}", status.as_u16()),
            Self::Reply { status, fault } => {
                write!(formatter, "status {}: {fault}", status.as_u16())
            }
        }
    }
}

/// A failed call: the call and why it failed.
#[derive(Debug)]
pub struct ApiError {
    /// The call.
    call: Call,
    /// Why it failed.
    fault: Fault,
}

impl ApiError {
    /// The error of `call`.
    pub(crate) fn new(call: Call, fault: Fault) -> Self {
        Self { call, fault }
    }

    /// The call that failed.
    #[must_use]
    pub fn call(&self) -> Call {
        self.call
    }

    /// Why the call failed.
    #[must_use]
    pub fn fault(&self) -> &Fault {
        &self.fault
    }
}

impl fmt::Display for ApiError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(formatter, "{} failed: {}", self.call, self.fault)
    }
}

impl std::error::Error for ApiError {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match &self.fault {
            Fault::Connect(error) => Some(error),
            Fault::Build(error) => Some(error),
            Fault::Http(error) => Some(error),
            Fault::Body(error) => Some(error.as_ref()),
            _ => None,
        }
    }
}
