//! The protocol header check: the first check of each request, before the
//! route and the body (decisions CH25 rule 1, CH26, CH33 rule 1).

use creche_wire::{HOST_API_HEADER, HOST_API_PROTOCOL, MISMATCH_STATUS, ProtocolMismatch};
use hyper::HeaderMap;
use hyper::body::Bytes;

use super::{Reply, json_reply, status_of};
use crate::ServeError;

/// The protocol check and its frozen reply, made once at the start of the
/// server. A request never meets a serialize fault (decision CH26: never
/// an empty reply on a failed serialize).
#[derive(Debug)]
pub(super) struct ProtocolCheck {
    /// The exact decimal text of `HOST_API_PROTOCOL`.
    text: String,
    mismatch: Reply,
}

impl ProtocolCheck {
    pub(super) fn new() -> Result<Self, ServeError> {
        let reply = ProtocolMismatch::new(HOST_API_PROTOCOL);
        let body = serde_json::to_vec(&reply).map_err(|_| ServeError::Reply)?;
        Ok(Self {
            text: HOST_API_PROTOCOL.to_string(),
            mismatch: json_reply(status_of(MISMATCH_STATUS)?, Bytes::from(body)),
        })
    }

    /// The mismatch reply (decision CH33 rule 1), or `None` when the request
    /// gives the header exactly once with the exact decimal text of
    /// `HOST_API_PROTOCOL`. `01`, `x`, a value with a space inside and a
    /// header given twice each get the reply (decision CH34 rule 1). HTTP
    /// strips the spaces around the value first, so ` 1 ` reads as `1`
    /// (decision CH40 rule 3).
    pub(super) fn refuse(&self, headers: &HeaderMap) -> Option<Reply> {
        let mut values = headers.get_all(HOST_API_HEADER).iter();
        match (values.next(), values.next()) {
            (Some(value), None) if value.as_bytes() == self.text.as_bytes() => None,
            _ => Some(self.mismatch.clone()),
        }
    }
}

#[cfg(test)]
mod tests {
    use hyper::HeaderMap;
    use hyper::header::HeaderValue;

    use super::{HOST_API_HEADER, HOST_API_PROTOCOL, ProtocolCheck};

    fn refused(values: &[String]) -> bool {
        let mut headers = HeaderMap::new();
        for value in values {
            headers.append(HOST_API_HEADER, HeaderValue::from_str(value).unwrap());
        }
        ProtocolCheck::new().unwrap().refuse(&headers).is_some()
    }

    #[test]
    fn only_the_exact_text_once_passes() {
        let text = HOST_API_PROTOCOL.to_string();
        assert!(!refused(std::slice::from_ref(&text)));
        for form in [
            vec![],
            vec![(HOST_API_PROTOCOL - 1).to_string()],
            vec![(HOST_API_PROTOCOL + 1).to_string()],
            vec![format!("0{text}")],
            vec![format!(" {text}")],
            vec![format!("{text} ")],
            vec!["x".to_owned()],
            vec![text.clone(), text.clone()],
        ] {
            assert!(refused(&form), "{form:?} passed");
        }
    }
}
