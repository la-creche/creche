//! The body checks of a request. They follow the protocol header and the
//! route, in the order of decisions CH26 and CH33 rule 13: the
//! `Content-Type`, then the body cap, then strict JSON. The check of the
//! request type (`not_valid`) belongs to the route.

use std::error::Error;
use std::time::Duration;

use creche_wire::{HOST_API_BODY_CAP, ListError, strict};
use http_body_util::{BodyExt, LengthLimitError, Limited};
use hyper::Request;
use hyper::body::{Body, Bytes};
use hyper::header::CONTENT_TYPE;
use tokio::time::timeout;

use super::{Reply, error_reply};
use crate::ServeError;

/// The one `Content-Type` of a request body.
// Decision CH40 rule 4: only this exact text, given once, passes. A
// parameter such as `; charset=utf-8`, or another letter case, gets
// `bad_content_type`.
const JSON: &[u8] = b"application/json";

/// How long the whole body may take to arrive. Past it, the request gets
/// `not_json`, so a client that stops halfway holds no task for long.
// Decision CH40 rule 6: a body must arrive within 30 s, or it gets
// `not_json`.
const BODY_TIMEOUT: Duration = Duration::from_secs(30);

/// The body checks and their replies, made once at the start of the
/// server, so a request never meets a serialize fault (decision CH26).
#[derive(Debug)]
pub struct BodyCheck {
    bad_content_type: Reply,
    too_large: Reply,
    not_json: Reply,
}

impl BodyCheck {
    /// The checks, with the reply of each shared code (decision CH33 rule
    /// 13).
    pub fn new() -> Result<Self, ServeError> {
        Ok(Self {
            bad_content_type: error_reply(&ListError::BadContentType {})?,
            too_large: error_reply(&ListError::TooLarge {})?,
            not_json: error_reply(&ListError::NotJson {})?,
        })
    }

    /// The whole body of `request`, or the reply of the first check that
    /// it fails. The caller matched the route first.
    ///
    /// The cap of `HOST_API_BODY_CAP` binds this JSON body only. The bytes
    /// of an Upgrade route after 101 never pass here (decision CH34 rule
    /// 3). A body of exactly the cap passes.
    pub async fn read<B>(&self, request: Request<B>) -> Result<Bytes, Reply>
    where
        B: Body,
        B::Error: Into<Box<dyn Error + Send + Sync>>,
    {
        let mut types = request.headers().get_all(CONTENT_TYPE).iter();
        match (types.next(), types.next()) {
            (Some(value), None) if value.as_bytes() == JSON => {}
            _ => return Err(self.bad_content_type.clone()),
        }
        let body = request.into_body();
        // A Content-Length over the cap is refused before a byte is read.
        if usize::try_from(body.size_hint().lower()).map_or(true, |size| size > HOST_API_BODY_CAP) {
            return Err(self.too_large.clone());
        }
        let read = timeout(
            BODY_TIMEOUT,
            Limited::new(body, HOST_API_BODY_CAP).collect(),
        );
        let bytes = match read.await {
            Ok(Ok(collected)) => collected.to_bytes(),
            Ok(Err(error)) if error.is::<LengthLimitError>() => {
                return Err(self.too_large.clone());
            }
            // Decision CH40 rules 5 and 6: a body that does not arrive
            // whole, such as a broken chunk, or not within BODY_TIMEOUT,
            // gets `not_json`.
            Ok(Err(_)) | Err(_) => return Err(self.not_json.clone()),
        };
        if strict(&bytes) {
            Ok(bytes)
        } else {
            Err(self.not_json.clone())
        }
    }
}
