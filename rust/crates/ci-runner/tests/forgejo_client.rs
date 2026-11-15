//! The client against a stand-in Forgejo on 127.0.0.1. The stand-in
//! serves one reply recorded from Forgejo 17.0.1 per test, from
//! `tests/fixtures/forgejo-17.0.1/`, and keeps the request it got.
//!
//! The recordings are unchanged but for two edits: the address of the
//! Forgejo host in `html_url` and `url` is `192.0.2.10:3000`, and the
//! organization in `html_url` is `example-org`. Forgejo wrote the runner
//! token of `register.json` as 40 zeros, and the token of
//! `jobs-bad-token.json` is the 40 zeros that the recording sent.

use std::convert::Infallible;
use std::fs;
use std::net::SocketAddr;
use std::os::unix::fs::PermissionsExt;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use creche_ci_runner::client::{ForgejoClient, ForgejoOrigin};
use creche_ci_runner::error::{Call, Fault};
use creche_ci_runner::names::{Label, Labels, RunnerId};
use creche_ci_runner::reply::Removal;
use creche_ci_runner::secret::ApiToken;
use http_body_util::{BodyExt, Full};
use hyper::body::{Bytes, Incoming};
use hyper::server::conn::http1;
use hyper::service::service_fn;
use hyper::{Request, Response, StatusCode};
use hyper_util::rt::TokioIo;
use tokio::net::TcpListener;
use tokio::runtime::{Builder, Runtime};

/// The API token of the tests.
const TOKEN: &str = "7d0c4a9e2b5f8a1c3e6d9b2f5a8c1e4d7b0a3f6c";

/// The bound of each call of a test that gets a reply.
const WAIT: Duration = Duration::from_secs(10);

/// A reply recorded from Forgejo 17.0.1.
macro_rules! recorded {
    ($name:literal) => {
        include_bytes!(concat!("fixtures/forgejo-17.0.1/", $name))
    };
}

#[cfg(test)]
/// What the stand-in got.
#[derive(Debug, Clone)]
struct Seen {
    method: String,
    uri: String,
    authorization: Vec<String>,
    content_type: Option<String>,
    body: Vec<u8>,
}

#[cfg(test)]
/// A stand-in Forgejo that gives `status` and `body` to one request.
struct StandIn {
    runtime: Runtime,
    client: ForgejoClient,
    seen: Arc<Mutex<Option<Seen>>>,
}

#[cfg(test)]
/// `TOKEN` as `ApiToken::read` reads it from a file of mode 0600. The
/// file goes once the token is read.
fn api_token(name: &str) -> ApiToken {
    let dir = std::env::temp_dir().join(format!("ci-runner-client-{}-{name}", std::process::id()));
    let _ = fs::remove_dir_all(&dir);
    fs::create_dir_all(&dir).unwrap();
    let path = dir.join("token");
    fs::write(&path, format!("{TOKEN}\n")).unwrap();
    fs::set_permissions(&path, fs::Permissions::from_mode(0o600)).unwrap();
    let token = ApiToken::read(&path).unwrap();
    fs::remove_dir_all(dir).unwrap();
    token
}

#[cfg(test)]
/// A client of the organization `example-org` at `address`.
fn client(address: SocketAddr, token: ApiToken, wait: Duration) -> ForgejoClient {
    let origin: ForgejoOrigin = format!("http://{address}").parse().unwrap();
    ForgejoClient::new(origin, "example-org".parse().unwrap(), token, wait)
}

#[cfg(test)]
/// A client with `wait` of a stand-in that replies `status` and `body`.
fn stand_in(name: &str, status: StatusCode, body: Vec<u8>, wait: Duration) -> StandIn {
    let runtime = Builder::new_current_thread().enable_all().build().unwrap();
    let seen = Arc::new(Mutex::new(None));
    let listener = runtime.block_on(TcpListener::bind("127.0.0.1:0")).unwrap();
    let address = listener.local_addr().unwrap();

    let kept = Arc::clone(&seen);
    runtime.spawn(async move {
        let (stream, _) = listener.accept().await.unwrap();
        let service = service_fn(move |request: Request<Incoming>| {
            let kept = Arc::clone(&kept);
            let body = body.clone();
            async move {
                let (parts, incoming) = request.into_parts();
                let header = |name| {
                    parts
                        .headers
                        .get_all(name)
                        .iter()
                        .map(|value| value.to_str().unwrap().to_owned())
                        .collect::<Vec<_>>()
                };
                *kept.lock().unwrap() = Some(Seen {
                    method: parts.method.to_string(),
                    uri: parts.uri.to_string(),
                    authorization: header(hyper::header::AUTHORIZATION),
                    content_type: header(hyper::header::CONTENT_TYPE).pop(),
                    body: incoming.collect().await.unwrap().to_bytes().to_vec(),
                });
                let mut response = Response::new(Full::new(Bytes::from(body)));
                *response.status_mut() = status;
                Ok::<_, Infallible>(response)
            }
        });
        let _ = http1::Builder::new()
            .serve_connection(TokioIo::new(stream), service)
            .await;
    });

    StandIn {
        runtime,
        client: client(address, api_token(name), wait),
        seen,
    }
}

#[cfg(test)]
impl StandIn {
    /// The request that the stand-in got.
    fn seen(&self) -> Seen {
        self.seen.lock().unwrap().clone().unwrap()
    }
}

#[cfg(test)]
fn labels(names: &[&str]) -> Labels {
    Labels::new(names.iter().map(|name| name.parse::<Label>().unwrap())).unwrap()
}

#[test]
fn the_jobs_list_sends_the_label_query_and_the_token() {
    let forgejo = stand_in(
        "jobs",
        StatusCode::OK,
        recorded!("jobs-label-match.json").to_vec(),
        WAIT,
    );
    let jobs = forgejo
        .runtime
        .block_on(forgejo.client.waiting_jobs(&labels(&["vm-spike", "kvm"])))
        .unwrap();

    let [job] = jobs.as_slice() else {
        panic!("{jobs:?}");
    };
    assert_eq!(
        job.handle().as_str(),
        "2f923f44-2d31-4d08-ad46-e04a543826e0"
    );

    let seen = forgejo.seen();
    assert_eq!(seen.method, "GET");
    assert_eq!(
        seen.uri,
        "/api/v1/orgs/example-org/actions/runners/jobs?labels=kvm,vm-spike"
    );
    assert_eq!(seen.authorization, [format!("token {TOKEN}")]);
    assert_eq!(seen.content_type, None);
    assert!(seen.body.is_empty());
}

#[test]
fn a_null_jobs_list_is_empty() {
    for body in [
        recorded!("jobs-empty.json"),
        recorded!("jobs-label-none.json"),
    ] {
        let forgejo = stand_in("null", StatusCode::OK, body.to_vec(), WAIT);
        let jobs = forgejo
            .runtime
            .block_on(forgejo.client.waiting_jobs(&labels(&["no-such-label"])))
            .unwrap();
        assert!(jobs.is_empty());
    }
}

#[test]
fn a_registration_sends_an_ephemeral_runner_and_reads_its_token() {
    let forgejo = stand_in(
        "register",
        StatusCode::CREATED,
        recorded!("register.json").to_vec(),
        WAIT,
    );
    let runner = forgejo
        .runtime
        .block_on(forgejo.client.register(&"ci-2f923f44".parse().unwrap()))
        .unwrap();
    assert_eq!(runner.id().get(), 3);
    assert_eq!(
        runner.uuid().as_str(),
        "a4087518-6f64-42b1-8383-c37ab78396af"
    );
    assert_eq!(runner.token().reveal().len(), 40);

    let seen = forgejo.seen();
    assert_eq!(seen.method, "POST");
    assert_eq!(seen.uri, "/api/v1/orgs/example-org/actions/runners");
    assert_eq!(seen.content_type.as_deref(), Some("application/json"));
    let body: serde_json::Value = serde_json::from_slice(&seen.body).unwrap();
    assert_eq!(
        body,
        serde_json::json!({
            "name": "ci-2f923f44",
            "description": "An ephemeral runner of the ci-runner daemon, for one job.",
            "ephemeral": true,
        })
    );
}

#[test]
fn a_removal_reads_204_as_removed_and_404_as_absent() {
    let runner = RunnerId::try_from(3).unwrap();
    for (status, body, outcome) in [
        (StatusCode::NO_CONTENT, &b""[..], Removal::Removed),
        (
            StatusCode::NOT_FOUND,
            recorded!("delete-again.json"),
            Removal::Absent,
        ),
    ] {
        let forgejo = stand_in("remove", status, body.to_vec(), WAIT);
        let got = forgejo
            .runtime
            .block_on(forgejo.client.remove_runner(runner))
            .unwrap();
        assert_eq!(got, outcome);

        let seen = forgejo.seen();
        assert_eq!(seen.method, "DELETE");
        assert_eq!(seen.uri, "/api/v1/orgs/example-org/actions/runners/3");
    }
}

#[test]
fn a_401_names_the_call_and_the_status_and_no_token() {
    // The recorded 401 body, with the token of this test in place of the
    // token that the recording sent.
    let body = std::str::from_utf8(recorded!("jobs-bad-token.json"))
        .unwrap()
        .replace(&"0".repeat(40), TOKEN);
    let forgejo = stand_in(
        "unauthorized",
        StatusCode::UNAUTHORIZED,
        body.into_bytes(),
        WAIT,
    );
    let error = forgejo
        .runtime
        .block_on(forgejo.client.waiting_jobs(&labels(&["kvm"])))
        .unwrap_err();

    assert_eq!(error.call(), Call::ListJobs);
    assert!(matches!(error.fault(), Fault::Unauthorized));
    let shown = format!("{error} {error:?} {:?}", forgejo.client);
    assert!(!shown.contains(TOKEN), "{shown}");
    assert_eq!(
        error.to_string(),
        "the list of waiting jobs failed: status 401: Forgejo refused the API token"
    );
}

#[test]
fn an_echoed_token_in_another_status_is_redacted() {
    let body = serde_json::json!({ "message": format!("no runner for token {TOKEN}") }).to_string();
    let forgejo = stand_in(
        "echo",
        StatusCode::INTERNAL_SERVER_ERROR,
        body.into_bytes(),
        WAIT,
    );
    let error = forgejo
        .runtime
        .block_on(forgejo.client.remove_runner(RunnerId::try_from(3).unwrap()))
        .unwrap_err();

    assert_eq!(
        error.to_string(),
        "the removal of runner 3 failed: status 500: no runner for token <token>"
    );
}

#[test]
fn a_body_past_the_cap_is_refused() {
    let body = vec![b' '; (1 << 20) + 1];
    let forgejo = stand_in("large", StatusCode::OK, body, WAIT);
    let error = forgejo
        .runtime
        .block_on(forgejo.client.waiting_jobs(&labels(&["kvm"])))
        .unwrap_err();
    assert!(matches!(error.fault(), Fault::TooLarge), "{error}");
}

#[test]
fn a_call_with_no_reply_ends_at_its_bound() {
    let runtime = Builder::new_current_thread().enable_all().build().unwrap();
    let listener = runtime.block_on(TcpListener::bind("127.0.0.1:0")).unwrap();
    let address = listener.local_addr().unwrap();
    let wait = Duration::from_millis(200);
    let client = client(address, api_token("bound"), wait);

    // The listener accepts in the kernel and never answers.
    let error = runtime
        .block_on(client.waiting_jobs(&labels(&["kvm"])))
        .unwrap_err();
    assert!(
        matches!(error.fault(), Fault::Timeout(bound) if *bound == wait),
        "{error}"
    );
    drop(listener);
}

#[test]
fn a_refused_connect_is_a_connect_fault() {
    let runtime = Builder::new_current_thread().enable_all().build().unwrap();
    let listener = runtime.block_on(TcpListener::bind("127.0.0.1:0")).unwrap();
    let address = listener.local_addr().unwrap();
    drop(listener);

    let client = client(address, api_token("connect"), WAIT);
    let error = runtime
        .block_on(client.register(&"ci-x".parse().unwrap()))
        .unwrap_err();
    assert_eq!(error.call(), Call::Register);
    assert!(matches!(error.fault(), Fault::Connect(_)), "{error}");
}
