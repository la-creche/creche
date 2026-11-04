//! The body checks of a request. They follow the protocol header and the
//! route, in the order of decisions CH26 and CH33 rule 13: the
//! `Content-Type`, then the body cap, then strict JSON. The check of the
//! request type (`not_valid`) belongs to the route.

use std::collections::HashSet;
use std::error::Error;
use std::fmt;
use std::time::Duration;

use creche_wire::{HOST_API_BODY_CAP, ListError};
use http_body_util::{BodyExt, LengthLimitError, Limited};
use hyper::Request;
use hyper::body::{Body, Bytes};
use hyper::header::CONTENT_TYPE;
use serde::de::{DeserializeSeed, Deserializer, Error as _, MapAccess, SeqAccess, Visitor};
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

/// The most arrays and objects that are open at one point of a strict
/// text: `DEPTH_MAX` of `wire/src/creche_wire/strictjson.py`.
const DEPTH_MAX: usize = 64;

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

/// Whether `bytes` is one strict JSON text: the rules of
/// `wire/src/creche_wire/strictjson.py` that serde_json can see. serde_json
/// checks the UTF-8, the grammar, the escapes, the constants, the floats
/// and the trailing data. `Strict` checks the keys and the depth.
// Decision CH40 rule 7: serde_json reads an integer past 64 bits, and the
// integer `-0`, as a float, so this check passes both. strictjson.py
// refuses them. The type check of an integer field then refuses either
// one as `not_valid`.
pub(crate) fn strict(bytes: &[u8]) -> bool {
    let mut reader = serde_json::Deserializer::from_slice(bytes);
    Strict { open: 0 }.deserialize(&mut reader).is_ok() && reader.end().is_ok()
}

/// Reads one value and refuses a key given twice in one object, or more
/// than `DEPTH_MAX` arrays and objects open at one point.
#[derive(Debug, Clone, Copy)]
struct Strict {
    /// The arrays and objects open around this value.
    open: usize,
}

impl Strict {
    /// The reader of a value inside one more array or object.
    fn inside<E: serde::de::Error>(self) -> Result<Self, E> {
        let open = self.open.saturating_add(1);
        if open > DEPTH_MAX {
            return Err(E::custom("too deep"));
        }
        Ok(Self { open })
    }
}

impl<'de> DeserializeSeed<'de> for Strict {
    type Value = ();

    fn deserialize<D: Deserializer<'de>>(self, deserializer: D) -> Result<(), D::Error> {
        deserializer.deserialize_any(self)
    }
}

impl<'de> Visitor<'de> for Strict {
    type Value = ();

    fn expecting(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str("a strict JSON value")
    }

    fn visit_unit<E>(self) -> Result<(), E> {
        Ok(())
    }

    fn visit_bool<E>(self, _: bool) -> Result<(), E> {
        Ok(())
    }

    fn visit_i64<E>(self, _: i64) -> Result<(), E> {
        Ok(())
    }

    fn visit_u64<E>(self, _: u64) -> Result<(), E> {
        Ok(())
    }

    fn visit_f64<E>(self, _: f64) -> Result<(), E> {
        Ok(())
    }

    fn visit_str<E>(self, _: &str) -> Result<(), E> {
        Ok(())
    }

    fn visit_seq<A: SeqAccess<'de>>(self, mut items: A) -> Result<(), A::Error> {
        let inside = self.inside()?;
        while items.next_element_seed(inside)?.is_some() {}
        Ok(())
    }

    fn visit_map<A: MapAccess<'de>>(self, mut entries: A) -> Result<(), A::Error> {
        let inside = self.inside()?;
        // serde_json decodes each escape of a key, so two keys are equal
        // when their code points are equal.
        let mut keys = HashSet::new();
        while let Some(key) = entries.next_key::<String>()? {
            if !keys.insert(key) {
                return Err(A::Error::custom("a key given twice"));
            }
            entries.next_value_seed(inside)?;
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::{DEPTH_MAX, strict};

    fn nested(open: usize) -> Vec<u8> {
        let mut text = "[".repeat(open);
        text.push_str(&"]".repeat(open));
        text.into_bytes()
    }

    fn nested_objects(open: usize) -> Vec<u8> {
        let mut text = r#"{"a":"#.repeat(open);
        text.push('1');
        text.push_str(&"}".repeat(open));
        text.into_bytes()
    }

    #[test]
    fn only_a_strict_text_passes() {
        for text in [
            &br#"{"a":1,"b":[true,null,-2,1.5,"\ud83d\ude00"]}"#[..],
            b" \t\r\n{} ",
            b"\"x\"",
            b"0",
        ] {
            assert!(strict(text), "{:?} failed", String::from_utf8_lossy(text));
        }
        assert!(strict(&nested(DEPTH_MAX)));
        assert!(strict(&nested_objects(DEPTH_MAX)));
        for text in [
            &b""[..],
            br#"{"a":1,"a":2}"#,
            br#"{"a":1,"a":2}"#,
            br#"{"a":{"b":1,"b":1}}"#,
            br#"["\ud800"]"#,
            b"\"\x01\"",
            b"\"\xff\"",
            b"\xef\xbb\xbf{}",
            b"[NaN]",
            b"[1e999]",
            b"[01]",
            b"{} {}",
            b"{",
        ] {
            assert!(!strict(text), "{:?} passed", String::from_utf8_lossy(text));
        }
        assert!(!strict(&nested(DEPTH_MAX + 1)));
        assert!(!strict(&nested_objects(DEPTH_MAX + 1)));
    }
}
