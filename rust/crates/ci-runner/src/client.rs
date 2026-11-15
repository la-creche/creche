//! The Forgejo API client of the daemon (design section 4.2, build plan
//! PR 4). It makes three calls on the runners of one organization:
//!
//! | Call | Request | Reply |
//! |---|---|---|
//! | `waiting_jobs` | `GET /api/v1/orgs/{org}/actions/runners/jobs?labels=a,b` | 200 with a list, or `null` for none |
//! | `register` | `POST /api/v1/orgs/{org}/actions/runners` | 201 with `id`, `uuid`, `token` |
//! | `remove_runner` | `DELETE /api/v1/orgs/{org}/actions/runners/{id}` | 204, or 404 for no such runner |
//!
//! Each request carries `Authorization: token <api token>`. Each reply is
//! untrusted: `reply.rs` reads it into typed values (root `AGENTS.md`,
//! security rules).

use std::fmt;
use std::net::SocketAddr;
use std::str::FromStr;
use std::time::Duration;

use http_body_util::Full;
use hyper::body::Bytes;
use hyper::header::{ACCEPT, AUTHORIZATION, CONTENT_TYPE, HOST, HeaderValue};
use hyper::{Method, Request};

use crate::error::{ApiError, Call, Fault};
use crate::names::{Labels, OrgName, RunnerId, RunnerName};
use crate::reply::{self, Registration, Removal, WaitingJob};
use crate::secret::ApiToken;
use crate::transport::{self, Exchange};

/// The scheme of the Forgejo origin.
const SCHEME: &str = "http://";

/// The media type of each body.
const JSON: &str = "application/json";

/// The description of each runner that the daemon registers.
const RUNNER_DESCRIPTION: &str = "An ephemeral runner of the ci-runner daemon, for one job.";

// CONTRACT-QUESTION: design section 4.1 labels the link to Forgejo
// "HTTPS". Section 4.5 lets each VM reach only "the Forgejo host's HTTP
// port", and the replies of Forgejo 17.0.1 were recorded over plain HTTP.
// The reading taken: plain HTTP to an address on the LAN, the same port
// as the VMs. A change to HTTPS adds a TLS crate to rust/Cargo.lock and a
// CA file to the host, and changes this type and `transport.rs` only.
/// Where Forgejo listens: `http://<address>:<port>`, for example
/// `http://192.0.2.10:3000`. The address is an IP literal, as the fence
/// rule of design section 4.5 names it, so the daemon does no DNS lookup.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ForgejoOrigin {
    /// The socket address.
    address: SocketAddr,
    /// The `Host` header of each request.
    host: HeaderValue,
}

impl ForgejoOrigin {
    /// The socket address.
    #[must_use]
    pub fn address(&self) -> SocketAddr {
        self.address
    }
}

/// Why a text is not a Forgejo origin.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct OriginError;

impl fmt::Display for OriginError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .write_str("a Forgejo origin is http://<address>:<port> with an IP address and no path")
    }
}

impl std::error::Error for OriginError {}

impl FromStr for ForgejoOrigin {
    type Err = OriginError;

    fn from_str(text: &str) -> Result<Self, Self::Err> {
        let rest = text.strip_prefix(SCHEME).ok_or(OriginError)?;
        let address: SocketAddr = rest.parse().map_err(|_| OriginError)?;
        let host = HeaderValue::from_str(&address.to_string()).map_err(|_| OriginError)?;
        Ok(Self { address, host })
    }
}

impl fmt::Display for ForgejoOrigin {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(formatter, "{SCHEME}{}", self.address)
    }
}

/// The client of the runners of one organization.
#[derive(Debug, Clone)]
pub struct ForgejoClient {
    /// Where Forgejo listens.
    origin: ForgejoOrigin,
    /// The organization that owns the runners.
    org: OrgName,
    /// The token of the bot user.
    token: ApiToken,
    /// The bound of each call, from the connect to the last body byte.
    wait: Duration,
}

impl ForgejoClient {
    /// The client of the runners of `org` at `origin`. Each call ends
    /// within `wait`.
    #[must_use]
    pub fn new(origin: ForgejoOrigin, org: OrgName, token: ApiToken, wait: Duration) -> Self {
        Self {
            origin,
            org,
            token,
            wait,
        }
    }

    /// The jobs of the organization that wait for a runner with only
    /// labels of `labels`.
    ///
    /// # Errors
    ///
    /// `ApiError` with `Call::ListJobs` when the call fails or the reply
    /// is not a list of waiting jobs.
    pub async fn waiting_jobs(&self, labels: &Labels) -> Result<Vec<WaitingJob>, ApiError> {
        let call = Call::ListJobs;
        let path = format!("{}/jobs?labels={}", self.runners_path(), labels.query());
        let reply = self.exchange(call, Method::GET, &path, None).await?;
        reply::jobs(reply.status, &reply.body, &self.token)
            .map_err(|fault| ApiError::new(call, fault))
    }

    /// Registers one ephemeral runner named `name`. Forgejo removes the
    /// runner after its one job.
    ///
    /// # Errors
    ///
    /// `ApiError` with `Call::Register` when the call fails or the reply
    /// is not a registration.
    pub async fn register(&self, name: &RunnerName) -> Result<Registration, ApiError> {
        let call = Call::Register;

        // The body that Forgejo 17.0.1 took when the replies were recorded.
        // It holds no labels. A registered runner shows `labels: null`
        // until it connects (the recorded reply of a runner GET).
        let body = serde_json::json!({
            "name": name.as_str(),
            "description": RUNNER_DESCRIPTION,
            "ephemeral": true,
        });
        let path = self.runners_path();
        let reply = self
            .exchange(call, Method::POST, &path, Some(body.to_string()))
            .await?;
        reply::registration(reply.status, &reply.body, &self.token)
            .map_err(|fault| ApiError::new(call, fault))
    }

    /// Removes the runner `runner`. A runner that Forgejo already removed
    /// gives `Removal::Absent`.
    ///
    /// # Errors
    ///
    /// `ApiError` with `Call::Remove` when the call fails or the status
    /// is not 204 or 404.
    pub async fn remove_runner(&self, runner: RunnerId) -> Result<Removal, ApiError> {
        let call = Call::Remove(runner);
        let path = format!("{}/{runner}", self.runners_path());
        let reply = self.exchange(call, Method::DELETE, &path, None).await?;
        reply::removal(reply.status, &reply.body, &self.token)
            .map_err(|fault| ApiError::new(call, fault))
    }

    /// The path of the runners of the organization.
    fn runners_path(&self) -> String {
        format!("/api/v1/orgs/{}/actions/runners", self.org)
    }

    /// One request of `call` with its headers, sent within the bound.
    async fn exchange(
        &self,
        call: Call,
        method: Method,
        path: &str,
        body: Option<String>,
    ) -> Result<Exchange, ApiError> {
        let fail = |fault| ApiError::new(call, fault);

        let mut builder = Request::builder()
            .method(method)
            .uri(path)
            .header(HOST, self.origin.host.clone())
            .header(AUTHORIZATION, self.token.header().clone())
            .header(ACCEPT, JSON);
        if body.is_some() {
            builder = builder.header(CONTENT_TYPE, JSON);
        }
        let request = builder
            .body(Full::new(Bytes::from(body.unwrap_or_default())))
            .map_err(|error| fail(Fault::Build(error)))?;

        transport::exchange(&self.origin, request, self.wait)
            .await
            .map_err(fail)
    }
}

#[cfg(test)]
mod tests {
    use super::{ForgejoOrigin, OriginError};

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
            "HTTP://192.0.2.10:3000",
        ] {
            assert_eq!(text.parse::<ForgejoOrigin>(), Err(OriginError), "{text}");
        }
    }
}
