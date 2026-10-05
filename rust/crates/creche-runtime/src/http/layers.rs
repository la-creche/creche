//! What each router of a service needs at its edge.
//!
//! [`edge`] wraps a router with five things:
//!
//! 1. An answer for a path that no route has, and one for a method that a
//!    route does not take. The service states each answer through
//!    [`ErrorBodies`].
//! 2. The two answers of the Python web framework that `axum` does not give:
//!    status 405 for `HEAD`, and status 307 for a known path with one more or
//!    one less final slash.
//! 3. One task for each request. A client that leaves does not stop the
//!    handler: the handler runs to its end, as a handler of a Python service
//!    does. A handler that panics ends its own task only, and the client gets
//!    the answer for [`EdgeFailure::Panic`].
//! 4. [`ClientGone`], for a handler that must stop when its client left.
//! 5. One access line for each request, when the service writes one today.
//!
//! A service that builds its router without [`edge`] loses a handler at each
//! client that leaves. No compiler check finds that
//! (`rust/AGENTS.md`, "The rules for a service", rule 6).
//!
//! The body of a streamed answer runs in the task of the connection and not
//! in the task of the handler. A body stream owns no cleanup, and it selects
//! on the stop signal. The server drops the stream when its client leaves. A
//! panic in a body stream ends the connection and writes no line of this
//! module.
//!
//! A panic of a handler gives three `ERROR` lines, and none holds the message
//! of the panic. That message can hold a part of a request.
//!
//! 1. The panic hook of [`log::init`](crate::log::init) writes the place.
//! 2. [`Tasks`] writes the name of the task.
//! 3. This module writes the method and the path of the request.
//!
//! The Python origins are `attendance/src/attendance/api.py:43-67` and
//! `:232-244`, and the answers of the web framework itself. The doc comment
//! of [`edge`] and of [`read_body`] says what each function does in another
//! way than its Python origin.

use std::error::Error;
use std::fmt;
use std::future;
use std::sync::Arc;

use axum::Router;
use axum::body::{Body, HttpBody};
use axum::extract::Request;
use axum::response::Response;
use bytes::{Bytes, BytesMut};
use http::header::{ALLOW, HOST, LOCATION};
use http::{HeaderMap, HeaderValue, Method, StatusCode, Uri};
use http_body_util::BodyExt;
use tokio_util::sync::CancellationToken;
use tower::ServiceExt;

use crate::readfile::ByteCap;
use crate::tasks::{TaskLost, Tasks};

/// The target of each line that this module writes to the log.
const LOG_TARGET: &str = "http";

/// The name of the task of each request, in the line that [`Tasks`] writes
/// for a panic.
const HANDLER_TASK: &str = "http-request";

/// The name of a method that no method of a route takes.
///
/// A router gives the answer for a wrong method to a request with this
/// method, for each path that a route has. [`edge`] uses it for two things: a
/// `HEAD` request goes to the router with this method, and the question to
/// the route table has it.
const UNROUTED: &str = "UNROUTED";

/// The scheme of the address in a `Location` header. A service has no TLS.
const SCHEME: &str = "http://";

/// A request that the edge refuses before a handler answers, or in place of
/// a handler that gave no answer.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum EdgeFailure {
    /// No route has the path.
    NoRoute,
    /// A route has the path and does not take the method.
    WrongMethod,
    /// The handler panicked.
    Panic,
    /// The runtime stopped the task of the handler.
    HandlerLost,
    /// The body of the request holds more bytes than the cap.
    BodyTooLarge {
        /// The cap of the read.
        cap: ByteCap,
    },
    /// The client stopped in the middle of the body.
    BodyUnreadable,
}

impl fmt::Display for EdgeFailure {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::NoRoute => f.write_str("no route has the path"),
            Self::WrongMethod => f.write_str("the route does not take the method"),
            Self::Panic => f.write_str("the handler panicked"),
            Self::HandlerLost => f.write_str("the task of the handler was lost"),
            Self::BodyTooLarge { cap } => {
                write!(f, "the body holds more than {} bytes", cap.get())
            }
            Self::BodyUnreadable => f.write_str("the body of the request stopped early"),
        }
    }
}

impl Error for EdgeFailure {}

/// The answers of one service for each [`EdgeFailure`].
///
/// Each service has its own error body: contract 02 §14 for `attendance`,
/// the OpenAI error body for `door-owui`. A service implements this trait
/// with the type of its contract. [`starlette::StarletteBodies`] gives the
/// answers of the Python framework.
///
/// [`edge`] calls [`ErrorBodies::answer`] for [`EdgeFailure::NoRoute`],
/// [`EdgeFailure::WrongMethod`], [`EdgeFailure::Panic`] and
/// [`EdgeFailure::HandlerLost`]. A handler calls it for the two failures of
/// [`read_body`].
pub trait ErrorBodies: Clone + Send + Sync + 'static {
    /// The answer for `failure`: the status, the headers and the body.
    ///
    /// The function must not panic. The answer for [`EdgeFailure::Panic`]
    /// and for [`EdgeFailure::HandlerLost`] is made outside the task of the
    /// request, so a panic there ends the connection with no answer.
    fn answer(&self, failure: EdgeFailure) -> Response;
}

/// Whether [`edge`] writes one line of the log for each request.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum AccessLog {
    /// Write one `INFO` line with the method, the path with no query, and
    /// the status. `door-owui`, the chaperone and the trigger listener do
    /// this today.
    Write,
    /// Write no line. `attendance` and the noticeboard do this today.
    Skip,
}

/// Wraps `routes` with the edge of a service. The module doc lists the five
/// parts.
///
/// Call it one time, on the whole router, after each route is in place.
///
/// - The function sets the fallback of the router. A fallback that the
///   router had is gone.
/// - The answer for a wrong method goes to each route that is in place at
///   the call, and to no later route.
/// - A route that the service adds to the result is outside the edge.
///
/// # The answers of the Python framework
///
/// A router behind the function gives the answer of the Python framework to
/// each request of the vectors `runtime.edge.*`.
///
/// - **`HEAD`.** A `HEAD` request on a path that a route has gets status 405
///   and the answer for [`EdgeFailure::WrongMethod`], with the `Allow`
///   header. No handler runs. `axum` alone runs the handler of `GET`. The
///   client gets the headers of the answer and no body, for each status.
/// - **A final slash.** A path that no route has can differ from a path of a
///   route only in its final slashes: `/healthz/` for `/healthz`, or
///   `/healthz` for `/healthz/`. Such a request gets status 307 with no
///   body, for each method. The `Location` header names the path of the
///   route and keeps the query. The path `/` gets no such answer.
/// - **The `Allow` header.** The answer for a wrong method names one method:
///   the first method that the service gave to the path. `axum` alone names
///   each method of the path, and `HEAD` for `GET`. The Python framework
///   names the methods of the first route of the path. So add the methods of
///   one path in the order of the Python routes: `post(create).get(list)`
///   for a `POST` route that comes before a `GET` route.
///
/// # The route table
///
/// The function keeps one clone of `routes`, with no fallback: the route
/// table. For a path that no route has, it asks the route table if a route
/// has the other form of the path. The question is a request with the method
/// `UNROUTED`, with no header and with no body. A route answers it with
/// status 405, and no handler runs. Two rules follow:
///
/// - **Give each route of a service its methods by name.** `any`,
///   `route_service`, `nest_service` and a fallback of a method router make
///   a route for each method. The handler of such a route runs for the
///   question, and it gets a `HEAD` request with the method `UNROUTED`.
/// - **A layer that `routes` has also gets the question.** Give the router a
///   layer of the service before the call only when the layer can get such a
///   request.
///
/// # The log
///
/// The access line has the target `http`: `GET /v1/models 200`. For a `HEAD`
/// request it holds `HEAD`, and not the method that the router got. A
/// request whose client left before the answer writes no access line.
///
/// # The Python origin
///
/// The Python origins are the app that each service builds, for example
/// `build_app` of `attendance/src/attendance/api.py:43-67`, the route loop of
/// the framework (`fastapi/routing.py:2719-2781`), and the `access_log`
/// switch of `attendance/src/attendance/__main__.py:102`. A file of
/// `fastapi`, of `starlette` or of `uvicorn` is the file of the version that
/// `uv.lock` gives. The function differs from those origins in these ways:
///
/// - `uvicorn` writes the address of the client, the path with its query and
///   the HTTP version in an access line
///   (`uvicorn/protocols/http/h11_impl.py:481-489`). The line here holds the
///   method, the path with no query and the status: a query can hold a
///   secret.
/// - Python writes the exception of a handler and its trace to the log
///   (`attendance/src/attendance/api.py:52-67`). The lines here hold the
///   place of the panic, the name of the task, the method and the path. They
///   never hold the message of the panic.
/// - `uvicorn` starts the task of a handler when it reads the head of a
///   request (`uvicorn/protocols/http/h11_impl.py:259-263`). The server here
///   starts it at the first poll of the request. A client that sends a whole
///   request and closes its side in the same moment gets no handler. A
///   Python service runs the handler of such a request to its end.
/// - The Python framework matches a route against the decoded text of the
///   target of a request (`uvicorn/protocols/http/h11_impl.py:201-202`). A
///   router here matches the path as the client sent it: `/%68ealthz` is not
///   the path `/healthz`.
/// - For a request with no `Host` header that names a host, the Python
///   framework writes the address of its listener into the `Location`
///   header (`starlette/datastructures.py:49-54`). The header here then
///   holds the path and the query only.
/// - `uvicorn` takes the scheme of the `Location` header from the header
///   `X-Forwarded-Proto` of a client on the loopback address
///   (`uvicorn/middleware/proxy_headers.py:35-51`). The scheme here is
///   `http` for each client.
///
/// ```
/// use axum::Router;
/// use axum::routing::get;
/// use creche_runtime::http::layers::starlette::StarletteBodies;
/// use creche_runtime::http::layers::{AccessLog, edge};
/// use creche_runtime::tasks::{Tasks, shutdown_pair};
///
/// let (_trigger, shutdown) = shutdown_pair();
/// let tasks = Tasks::new(shutdown);
/// let routes = Router::new().route("/healthz", get(|| async { "ok" }));
///
/// let app: Router = edge(routes, StarletteBodies, tasks, AccessLog::Skip);
/// # drop(app);
/// ```
pub fn edge<E: ErrorBodies>(routes: Router, errors: E, tasks: Tasks, access: AccessLog) -> Router {
    let unrouted = unrouted();
    let table = routes.clone().reset_fallback();
    let no_route = {
        let errors = errors.clone();
        let unrouted = unrouted.clone();

        move |request: Request| unmatched(table.clone(), errors.clone(), unrouted.clone(), request)
    };
    let wrong_method = {
        let errors = errors.clone();

        move || future::ready(method_refused(&errors))
    };
    let front = Arc::new(Front {
        routes: routes
            .fallback(no_route)
            .method_not_allowed_fallback(wrong_method),
        errors,
        tasks,
        access,
        unrouted,
    });

    Router::new().fallback(move |request: Request| answered(Arc::clone(&front), request))
}

/// The method with the name [`UNROUTED`].
fn unrouted() -> Method {
    // `from_bytes` refuses only a text that is no method name, and a test
    // holds that it takes this name. `TRACE` is the standard method that no
    // service of this repository routes.
    Method::from_bytes(UNROUTED.as_bytes()).unwrap_or(Method::TRACE)
}

/// What [`edge`] holds for each request of one router.
struct Front<E> {
    /// The router of the service, with the two fallbacks of [`edge`].
    routes: Router,
    errors: E,
    tasks: Tasks,
    access: AccessLog,
    /// The method that a `HEAD` request gets before the router routes it.
    unrouted: Method,
}

/// The mark of the answer for a wrong method, in the extensions of that
/// answer. An answer of a handler with status 405 has no mark, and its
/// `Allow` header stays as the handler wrote it.
#[derive(Clone, Copy)]
struct MethodRefused;

/// The answer of `errors` for a wrong method, with the mark.
fn method_refused<E: ErrorBodies>(errors: &E) -> Response {
    let mut answer = errors.answer(EdgeFailure::WrongMethod);
    answer.extensions_mut().insert(MethodRefused);

    answer
}

/// Gives the answer to one request of a client.
///
/// The function runs the router in a task of its own. The connection owns
/// this future. It drops the future when the client leaves before the
/// answer. The task of the request then continues to its end, and the signal
/// of [`ClientGone`] fires.
async fn answered<E: ErrorBodies>(front: Arc<Front<E>>, mut request: Request) -> Response {
    let sent = request.method().clone();
    let path = request.uri().path().to_owned();
    if sent == Method::HEAD {
        // No route takes this method, so no handler runs for `HEAD`. The
        // server sends no body to a client that sent `HEAD`.
        *request.method_mut() = front.unrouted.clone();
    }
    let left = CancellationToken::new();
    request
        .extensions_mut()
        .insert(ClientGone { left: left.clone() });
    // The guard fires the signal when this future drops before the answer.
    let leaves = left.drop_guard();

    let routed = front.routes.clone().oneshot(request);
    let ended = front
        .tasks
        .spawn_must_complete(HANDLER_TASK, routed)
        .await
        .map(|answer| answer.unwrap_or_else(|never| match never {}));
    let mut answer = settled(&front.errors, &sent, &path, ended);
    // The client stayed until the answer.
    drop(leaves.disarm());
    keep_first_allowed(&mut answer);

    if front.access == AccessLog::Write {
        crate::info!(LOG_TARGET, "{sent} {path} {}", answer.status().as_u16());
    }

    answer
}

/// The answer of a request whose task ended.
///
/// A task that panicked gives the answer for [`EdgeFailure::Panic`] and one
/// `ERROR` line with the method and the path. The line holds no query and
/// nothing of the panic. A task that the runtime stopped gives the answer
/// for [`EdgeFailure::HandlerLost`] and no line: the process stops then.
fn settled<E: ErrorBodies>(
    errors: &E,
    method: &Method,
    path: &str,
    ended: Result<Response, TaskLost>,
) -> Response {
    match ended {
        Ok(answer) => answer,
        Err(TaskLost::Panicked) => {
            crate::error!(
                LOG_TARGET,
                "the handler of {method} {path} stopped with a panic"
            );

            errors.answer(EdgeFailure::Panic)
        }
        Err(TaskLost::Cancelled) => errors.answer(EdgeFailure::HandlerLost),
    }
}

/// Cuts the `Allow` header of the answer for a wrong method to its first
/// method. `axum` writes each method of the path there, in the order in
/// which the service added them, and `HEAD` after `GET`.
///
/// The Python origin is the answer of the first route of a path
/// (`fastapi/routing.py:2736-2742` and `:1264-1268`).
fn keep_first_allowed(answer: &mut Response) {
    if answer.extensions_mut().remove::<MethodRefused>().is_none() {
        return;
    }
    let Some(allowed) = answer.headers().get(ALLOW) else {
        return;
    };
    let first = allowed
        .as_bytes()
        .split(|byte| *byte == b',')
        .next()
        .unwrap_or_default()
        .trim_ascii();

    if let Ok(first) = HeaderValue::from_bytes(first) {
        answer.headers_mut().insert(ALLOW, first);
    }
}

/// Gives the answer to a request whose path no route has: status 307 when a
/// route has the other form of the path, and the answer of `errors` for
/// [`EdgeFailure::NoRoute`] in each other case.
///
/// The Python origin is the last part of the route loop of the framework
/// (`fastapi/routing.py:2745-2759`, and `starlette/routing.py:705-718`).
async fn unmatched<E: ErrorBodies>(
    table: Router,
    errors: E,
    unrouted: Method,
    request: Request,
) -> Response {
    // The body of the request is not read.
    let (head, _body) = request.into_parts();
    let Some(other) = other_form(head.uri.path()) else {
        return errors.answer(EdgeFailure::NoRoute);
    };
    if !has_route(table, unrouted, &other).await {
        return errors.answer(EdgeFailure::NoRoute);
    }
    let Some(place) = location(&head.headers, &other, head.uri.query()) else {
        return errors.answer(EdgeFailure::NoRoute);
    };

    let mut answer = Response::new(Body::empty());
    *answer.status_mut() = StatusCode::TEMPORARY_REDIRECT;
    answer.headers_mut().insert(LOCATION, place);

    answer
}

/// The other form of `path`: with no final slash for a path that ends in one
/// or more, and with one final slash for each other path. `None` for the
/// path `/`, for a path of slashes only, and for a target that is no path.
///
/// The Python origin is `fastapi/routing.py:2746-2751`.
fn other_form(path: &str) -> Option<String> {
    if path == "/" || !path.starts_with('/') {
        return None;
    }
    if !path.ends_with('/') {
        return Some(format!("{path}/"));
    }
    let cut = path.trim_end_matches('/');

    (!cut.is_empty()).then(|| cut.to_owned())
}

/// Asks the route table if a route has `path`. The answer of the table to a
/// path that no route has is status 404, and each other status says that a
/// route has the path.
///
/// The Python origin is the second loop over the routes
/// (`fastapi/routing.py:2753-2755`). That loop runs no handler and no
/// middleware.
async fn has_route(table: Router, unrouted: Method, path: &str) -> bool {
    let Ok(target) = path.parse::<Uri>() else {
        return false;
    };
    let mut question = Request::new(Body::empty());
    *question.method_mut() = unrouted;
    *question.uri_mut() = target;

    let answer = table
        .oneshot(question)
        .await
        .unwrap_or_else(|never| match never {});

    answer.status() != StatusCode::NOT_FOUND
}

/// The value of the `Location` header for `path`: the scheme `http`, the
/// host of the `Host` header, the path, and the query when it is not empty.
/// With no `Host` header that names a host, the value is the path and the
/// query. `None` when the parts make no header value.
///
/// The Python origin is `URL` of `starlette/datastructures.py:38-62`.
fn location(headers: &HeaderMap, path: &str, query: Option<&str>) -> Option<HeaderValue> {
    let mut place = String::new();
    let host = headers
        .get(HOST)
        .and_then(|host| host.to_str().ok())
        .filter(|host| names_a_host(host));
    if let Some(host) = host {
        place.push_str(SCHEME);
        place.push_str(host);
    }
    place.push_str(path);
    if let Some(query) = query.filter(|query| !query.is_empty()) {
        place.push('?');
        place.push_str(query);
    }

    HeaderValue::from_str(&place).ok()
}

/// Whether `text` is a host with an optional port: a name of ASCII letters,
/// digits, `.` and `-`, or an IPv6 address in brackets.
///
/// The Python framework puts a `Host` header into a `Location` header only
/// when it has this form. A header with `/`, `?`, `#` or `@` can otherwise
/// send a client to another site. The Python origin is `_HOST_RE` of
/// `starlette/datastructures.py:25`.
fn names_a_host(text: &str) -> bool {
    let port = match text.strip_prefix('[') {
        Some(bracketed) => {
            let Some((address, after)) = bracketed.split_once(']') else {
                return false;
            };
            let Some((first, rest)) = address.split_once(':') else {
                return false;
            };
            let in_address = |byte: u8| byte.is_ascii_hexdigit() || byte == b'.' || byte == b':';
            if !first.bytes().all(|byte| byte.is_ascii_hexdigit())
                || rest.is_empty()
                || !rest.bytes().all(in_address)
            {
                return false;
            }

            match after.strip_prefix(':') {
                Some(port) => Some(port),
                None if after.is_empty() => None,
                None => return false,
            }
        }
        None => {
            let (name, port) = match text.split_once(':') {
                Some((name, port)) => (name, Some(port)),
                None => (text, None),
            };
            let in_name = |byte: u8| byte.is_ascii_alphanumeric() || byte == b'.' || byte == b'-';
            if name.is_empty() || !name.bytes().all(in_name) {
                return false;
            }

            port
        }
    };

    port.is_none_or(|port| !port.is_empty() && port.bytes().all(|byte| byte.is_ascii_digit()))
}

/// The signal that the client of a request left before the answer.
///
/// [`edge`] puts one value into the extensions of each request. A handler
/// that must not continue for a client that left takes it and selects on
/// [`ClientGone::gone`]. Each other handler runs to its end.
///
/// The signal also fires when the service ends the connection at its stop,
/// before the answer. It does not fire after the handler gave its answer. A
/// streamed body sees a client that left in another way: the server drops
/// the stream.
///
/// No Python service has this signal. A Python handler runs to its end after
/// its client left.
///
/// ```
/// use std::time::Duration;
///
/// use axum::Extension;
/// use creche_runtime::http::layers::ClientGone;
///
/// async fn slow(Extension(client): Extension<ClientGone>) -> &'static str {
///     tokio::select! {
///         () = client.gone() => "no client reads this answer",
///         () = tokio::time::sleep(Duration::from_secs(30)) => "done",
///     }
/// }
/// ```
///
/// Code outside this module cannot build a value. Only [`edge`] gives one:
///
/// ```compile_fail,E0451
/// use std::time::Duration;
///
/// use axum::Extension;
/// use creche_runtime::http::layers::ClientGone;
///
/// let client = ClientGone {
///     left: tokio_util::sync::CancellationToken::new(),
/// };
/// ```
#[derive(Debug, Clone)]
pub struct ClientGone {
    /// Cancelled when the client left before the answer.
    left: CancellationToken,
}

impl ClientGone {
    /// Waits until the client left. The future never ends for a client that
    /// stays.
    ///
    /// The future is cancel safe: a caller can drop it in a `select!` and
    /// call the function again.
    ///
    /// The function has no Python origin. A Python handler learns that its
    /// client left only when it reads the request again. It then gets the
    /// message `http.disconnect`
    /// (`uvicorn/protocols/http/h11_impl.py:540-541`).
    pub async fn gone(&self) {
        self.left.cancelled().await;
    }
}

/// Reads the body of a request, up to `cap` bytes.
///
/// The function checks no `Content-Type` header. `attendance` reads a JSON
/// body with each content type, and the `Json` extractor of `axum` refuses
/// such a body.
///
/// A body that states a length past the cap is refused before the read of a
/// byte. A body with no stated length is refused at the first byte past the
/// cap.
///
/// The Python origin is `_json` of
/// `attendance/src/attendance/api.py:232-244`. That function reads the whole
/// body and has no cap. Each service gives the cap of its own contract.
///
/// ```
/// use axum::body::Body;
/// use creche_runtime::http::layers::{EdgeFailure, read_body};
/// use creche_runtime::readfile::ByteCap;
///
/// let cap = ByteCap::new(8).ok_or("a cap is 1 byte or more")?;
/// let runtime = tokio::runtime::Builder::new_current_thread().build()?;
///
/// let read = runtime.block_on(read_body(Body::from("{\"n\":1}"), cap));
/// assert_eq!(read.as_deref(), Ok(&b"{\"n\":1}"[..]));
///
/// let refused = runtime.block_on(read_body(Body::from("{\"n\":12}x"), cap));
/// assert_eq!(refused, Err(EdgeFailure::BodyTooLarge { cap }));
/// # Ok::<(), Box<dyn std::error::Error>>(())
/// ```
///
/// # Errors
///
/// [`EdgeFailure::BodyTooLarge`] for a body past the cap, and
/// [`EdgeFailure::BodyUnreadable`] for a body that stops early.
pub async fn read_body(mut body: Body, cap: ByteCap) -> Result<Bytes, EdgeFailure> {
    let limit = cap.get();
    let stated = HttpBody::size_hint(&body).lower();
    if u64::try_from(limit).is_ok_and(|limit| stated > limit) {
        return Err(EdgeFailure::BodyTooLarge { cap });
    }

    let mut bytes = BytesMut::new();
    while let Some(frame) = body.frame().await {
        let frame = frame.map_err(|_| EdgeFailure::BodyUnreadable)?;
        // A frame that holds no data holds the trailers of the body.
        let Ok(data) = frame.into_data() else {
            continue;
        };

        if data.len() > limit.saturating_sub(bytes.len()) {
            return Err(EdgeFailure::BodyTooLarge { cap });
        }
        bytes.extend_from_slice(&data);
    }

    Ok(bytes.freeze())
}

/// The answers of the Python web framework.
pub mod starlette {
    use axum::body::Body;
    use axum::response::Response;
    use http::header::CONTENT_TYPE;
    use http::{HeaderValue, StatusCode};

    use super::{EdgeFailure, ErrorBodies};

    /// The content type of a JSON answer of the framework.
    const JSON: &str = "application/json";

    /// The content type of a text answer of the framework.
    const PLAIN_TEXT: &str = "text/plain; charset=utf-8";

    /// The body of the answer to a path that no route has. Each surface
    /// `runtime.edge.*` holds it in the vector `unknown-path`.
    const NOT_FOUND: &str = "{\"detail\":\"Not Found\"}";

    /// The body of the answer to a method that a route does not take. Each
    /// surface `runtime.edge.*` holds it in the vector `wrong-method`.
    const METHOD_NOT_ALLOWED: &str = "{\"detail\":\"Method Not Allowed\"}";

    /// The body of the answer to an exception that no handler takes
    /// (`starlette/middleware/errors.py:259` of Starlette 1.6.0). No vector
    /// holds it: each Python service has a handler of its own.
    const SERVER_ERROR: &str = "Internal Server Error";

    /// The answers that the Python framework of each service gives today: the
    /// status, the content type and the bytes.
    ///
    /// A port of a service uses it to keep each byte of those answers. A
    /// service with an error body of its own contract writes its own
    /// [`ErrorBodies`].
    ///
    /// | Failure | Status | Content type | Body |
    /// |---|---|---|---|
    /// | [`EdgeFailure::NoRoute`] | 404 | `application/json` | `{"detail":"Not Found"}` |
    /// | [`EdgeFailure::WrongMethod`] | 405 | `application/json` | `{"detail":"Method Not Allowed"}` |
    /// | [`EdgeFailure::Panic`] | 500 | `text/plain; charset=utf-8` | `Internal Server Error` |
    /// | [`EdgeFailure::HandlerLost`] | 500 | `text/plain; charset=utf-8` | `Internal Server Error` |
    /// | [`EdgeFailure::BodyTooLarge`] | 413 | none | empty |
    /// | [`EdgeFailure::BodyUnreadable`] | 400 | none | empty |
    ///
    /// The first three rows are answers of the framework. Each Python
    /// service has a handler of its own for an exception, so the third row
    /// is the answer of no service today. The framework has no answer for
    /// the last three rows.
    ///
    /// The Python origins are `starlette/routing.py` and
    /// `starlette/middleware/errors.py:259` of Starlette 1.6.0.
    ///
    /// ```
    /// use creche_runtime::http::layers::starlette::StarletteBodies;
    /// use creche_runtime::http::layers::{EdgeFailure, ErrorBodies};
    ///
    /// let answer = StarletteBodies.answer(EdgeFailure::NoRoute);
    /// assert_eq!(answer.status().as_u16(), 404);
    /// ```
    #[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
    pub struct StarletteBodies;

    // CONTRACT-QUESTION: no contract and no Python framework gives an answer
    // for a handler that the runtime stopped, for a body past a cap or for a
    // body that stops early. The reading here is the answer of the framework
    // to an exception for the first, and the status alone for the two
    // others: 413 and 400 with no body. A service that needs a body for one
    // of the three writes its own `ErrorBodies`. A change costs one arm of
    // `answer`.
    impl ErrorBodies for StarletteBodies {
        fn answer(&self, failure: EdgeFailure) -> Response {
            match failure {
                EdgeFailure::NoRoute => with_body(StatusCode::NOT_FOUND, JSON, NOT_FOUND),
                EdgeFailure::WrongMethod => {
                    with_body(StatusCode::METHOD_NOT_ALLOWED, JSON, METHOD_NOT_ALLOWED)
                }
                EdgeFailure::Panic | EdgeFailure::HandlerLost => {
                    with_body(StatusCode::INTERNAL_SERVER_ERROR, PLAIN_TEXT, SERVER_ERROR)
                }
                EdgeFailure::BodyTooLarge { .. } => status_only(StatusCode::PAYLOAD_TOO_LARGE),
                EdgeFailure::BodyUnreadable => status_only(StatusCode::BAD_REQUEST),
            }
        }
    }

    /// An answer with a content type and a body.
    fn with_body(status: StatusCode, content_type: &'static str, body: &'static str) -> Response {
        let mut answer = Response::new(Body::from(body));
        *answer.status_mut() = status;
        answer
            .headers_mut()
            .insert(CONTENT_TYPE, HeaderValue::from_static(content_type));

        answer
    }

    /// An answer with no content type and no body.
    fn status_only(status: StatusCode) -> Response {
        let mut answer = Response::new(Body::empty());
        *answer.status_mut() = status;

        answer
    }
}

#[cfg(test)]
mod tests {
    use std::collections::VecDeque;
    use std::fs;
    use std::io;
    use std::path::PathBuf;
    use std::pin::Pin;
    use std::process::{Command, Output, Stdio};
    use std::sync::atomic::{AtomicUsize, Ordering};
    use std::sync::{Arc, Mutex};
    use std::task::{Context, Poll};
    use std::thread;
    use std::time::{Duration, Instant};

    use axum::Extension;
    use axum::middleware::{self, Next};
    use axum::routing::{MethodRouter, any, get, post};
    use creche_testkit::root::TempRoot;
    use creche_testkit::stub::RawHttp;
    use creche_testkit::vectors;
    use http::header::CONTENT_TYPE;
    use http::{HeaderMap, StatusCode};
    use hyper::body::{Frame, SizeHint};
    use tokio::io::AsyncWriteExt;
    use tokio::net::UnixStream;
    use tokio::runtime::{Builder, Runtime};
    use tokio::sync::{Notify, mpsc};
    use tokio::task::JoinHandle;

    use crate::http::server::{Listen, ServeError, SocketDir, bind, serve};
    use crate::tasks::{Drained, ShutdownTrigger, locked, shutdown_pair};

    use super::starlette::StarletteBodies;
    use super::*;

    /// One route of a Python service, as the notes of its surface give it.
    struct Surface {
        /// The surface of `vectors/data/runtime`.
        name: &'static str,
        /// The path that has a route.
        path: &'static str,
        /// The methods of the routes of that path, in the order of the
        /// Python app.
        methods: &'static [&'static str],
    }

    /// Each surface `runtime.edge.*`: the answers of the web framework of one
    /// Python service.
    const SURFACES: [Surface; 5] = [
        Surface {
            name: "runtime.edge.attendance",
            path: "/v1/sessions",
            methods: &["POST", "GET"],
        },
        Surface {
            name: "runtime.edge.chaperone",
            path: "/healthz",
            methods: &["GET"],
        },
        Surface {
            name: "runtime.edge.door_owui",
            path: "/v1/models",
            methods: &["GET"],
        },
        Surface {
            name: "runtime.edge.door_trigger",
            path: "/triggers/chat/hook",
            methods: &["POST"],
        },
        Surface {
            name: "runtime.edge.noticeboard",
            path: "/healthz",
            methods: &["GET"],
        },
    ];

    /// The start of the name of each surface of this module.
    const SURFACE_START: &str = "runtime.edge.";

    /// Each key of the value of a vector that the walk compares.
    const KEYS: [&str; 5] = ["status", "content_type", "body", "allow", "location"];

    /// The key of the cookies of the noticeboard. The web framework sets no
    /// cookie: a layer of the noticeboard does. The port of the noticeboard
    /// holds this key against that layer.
    const COOKIES_KEY: &str = "cookies";

    /// The vector whose handler raises.
    const RAISES: &str = "handler-raises";

    /// The scheme and the `Host` header of each request of a test.
    const ORIGIN: &str = "http://test";

    /// The longest time that a test waits for a step. The time is real, and
    /// a host with much load is slow.
    const LIMIT: Duration = Duration::from_secs(60);

    /// A drain limit that no test here reaches.
    const LONG_DRAIN: Duration = Duration::from_secs(45);

    /// A drain limit that passes, in a test with one request open.
    const SHORT_DRAIN: Duration = Duration::from_millis(300);

    /// A time in which a signal that must not fire does not fire.
    const QUIET: Duration = Duration::from_millis(100);

    /// The body of each route of a test. The Python routes answer JSON too.
    const ROUTE_BODY: &str = "{\"ok\":true}";

    /// The content type of [`ROUTE_BODY`].
    const JSON: &str = "application/json";

    /// The message of each panic of a scenario. No line of the log can hold
    /// it.
    const PANIC_MESSAGE: &str = "heron-request-secret-5586";

    /// A value in the query of a request. No line of the log can hold it.
    const QUERY_SECRET: &str = "magpie-query-secret-7302";

    /// The variable that selects the scenario of
    /// [`the_child_runs_one_scenario`].
    const CHILD_VARIABLE: &str = "CRECHE_RUNTIME_HTTP_LAYERS_TEST_CHILD";

    /// The name of the child test, as the test program takes it.
    const CHILD_TEST: &str = "http::layers::tests::the_child_runs_one_scenario";

    /// The target of the panic hook of the child.
    const CHILD_PROGRAM: &str = "child";

    /// The line that [`Tasks`] writes for a handler that panicked.
    const TASK_LINE: &str = "ERROR tasks the task http-request stopped with a panic";

    /// A scenario that writes to the log. It runs in a child, so its lines do
    /// not go to the output of this test program, and the test can read them.
    struct Scenario {
        /// The value of [`CHILD_VARIABLE`] that selects the scenario.
        name: &'static str,
        /// What the child does.
        run: fn(),
        /// Checks each line of the log: the level, the target and the
        /// message.
        check: fn(&[String]),
    }

    const SCENARIOS: [Scenario; 5] = [
        Scenario {
            name: "panic",
            run: a_handler_panics,
            check: |lines| {
                assert_eq!(lines.len(), 3, "{lines:?}");
                assert_panic_place(&lines[0]);
                assert_eq!(lines[1], TASK_LINE);
                assert_eq!(
                    lines[2],
                    "ERROR http the handler of GET /boom stopped with a panic"
                );
            },
        },
        Scenario {
            name: "fallback-panic",
            run: the_answer_for_no_route_panics,
            check: |lines| {
                assert_eq!(lines.len(), 3, "{lines:?}");
                assert_panic_place(&lines[0]);
                assert_eq!(lines[1], TASK_LINE);
                assert_eq!(
                    lines[2],
                    "ERROR http the handler of GET /no/such/path stopped with a panic"
                );
            },
        },
        Scenario {
            name: "access-write",
            run: a_service_writes_access_lines,
            check: |lines| {
                assert_eq!(lines.len(), 9, "{lines:?}");
                assert_eq!(
                    lines[..5],
                    [
                        "INFO http GET /healthz 200",
                        "INFO http GET /no/such/path 404",
                        "INFO http POST /healthz 405",
                        // `HEAD`, and not the method that the router got.
                        "INFO http HEAD /healthz 405",
                        "INFO http GET /healthz/ 307",
                    ]
                );
                assert_panic_place(&lines[5]);
                assert_eq!(lines[6], TASK_LINE);
                assert_eq!(
                    lines[7],
                    "ERROR http the handler of GET /boom stopped with a panic"
                );
                assert_eq!(lines[8], "INFO http GET /boom 500");
            },
        },
        Scenario {
            name: "access-skip",
            run: a_service_writes_no_access_line,
            check: |lines| assert!(lines.is_empty(), "{lines:?}"),
        },
        Scenario {
            name: "vectors",
            run: a_router_answers_as_each_vector_says,
            check: |lines| {
                // Each handler that raises gives the three lines of a panic.
                assert!(!lines.is_empty(), "{lines:?}");
                assert_eq!(lines.len() % 3, 0, "{lines:?}");
                for panic in lines.chunks(3) {
                    assert_panic_place(&panic[0]);
                    assert_eq!(panic[1], TASK_LINE);
                    assert_eq!(
                        panic[2],
                        "ERROR http the handler of GET /vectors/raises stopped with a panic"
                    );
                }
            },
        },
    ];

    /// The line of the panic hook holds the place of the panic.
    fn assert_panic_place(line: &str) {
        assert!(line.starts_with("ERROR child panic at "), "{line}");
        assert!(line.contains("layers.rs:"), "{line}");
    }

    fn runtime() -> Runtime {
        Builder::new_current_thread().enable_all().build().unwrap()
    }

    /// A runtime with one thread, and a runtime with two worker threads. A
    /// test of a stop or of a client that left runs on the two: a service
    /// selects its own count of threads.
    fn each_runtime() -> [Runtime; 2] {
        let workers = Builder::new_multi_thread()
            .worker_threads(2)
            .enable_all()
            .build()
            .unwrap();

        [runtime(), workers]
    }

    /// Waits for `step`, for [`LIMIT`] at most.
    async fn within<F: Future>(step: F) -> F::Output {
        tokio::time::timeout(LIMIT, step)
            .await
            .expect("the step did not end inside the limit")
    }

    /// The answer of a server, from its raw bytes.
    #[derive(Debug)]
    struct Answer {
        status: u16,
        headers: Vec<(String, String)>,
        body: Vec<u8>,
    }

    impl Answer {
        fn parse(bytes: &[u8]) -> Self {
            let end = bytes
                .windows(4)
                .position(|window| window == b"\r\n\r\n")
                .unwrap_or_else(|| panic!("no head: {:?}", String::from_utf8_lossy(bytes)));
            let head = std::str::from_utf8(&bytes[..end]).unwrap();
            let mut lines = head.split("\r\n");
            let status = lines
                .next()
                .and_then(|line| line.split(' ').nth(1))
                .and_then(|code| code.parse().ok())
                .unwrap_or_else(|| panic!("no status line: {head:?}"));
            let headers = lines
                .map(|line| {
                    let (name, value) = line.split_once(':').unwrap();

                    (name.to_ascii_lowercase(), value.trim().to_owned())
                })
                .collect();

            Self {
                status,
                headers,
                body: bytes[end + 4..].to_vec(),
            }
        }

        fn header(&self, name: &str) -> Option<&str> {
            self.headers
                .iter()
                .find(|(key, _)| key == name)
                .map(|(_, value)| value.as_str())
        }

        fn text(&self) -> &str {
            std::str::from_utf8(&self.body).unwrap()
        }

        /// Each method of the `Allow` header, in sorted order, as a vector
        /// holds it.
        fn allow(&self) -> Option<Vec<String>> {
            let mut methods: Vec<String> = self
                .header("allow")?
                .split(',')
                .map(|method| method.trim().to_owned())
                .collect();
            methods.sort();

            Some(methods)
        }
    }

    /// One whole request with no body. The header `Connection: close` makes
    /// the server close the connection after the answer.
    fn request(method: &str, target: &str) -> Vec<u8> {
        format!("{method} {target} HTTP/1.1\r\nHost: test\r\nConnection: close\r\n\r\n")
            .into_bytes()
    }

    /// A `GET` request with the `Host` header `host`, or with no such header.
    fn request_of(host: Option<&str>, target: &str) -> Vec<u8> {
        let host = host.map_or_else(String::new, |host| format!("Host: {host}\r\n"));

        format!("GET {target} HTTP/1.1\r\n{host}Connection: close\r\n\r\n").into_bytes()
    }

    /// A route for `GET` that counts its calls.
    fn counted(calls: &Arc<AtomicUsize>) -> MethodRouter {
        let calls = Arc::clone(calls);

        get(move || async move {
            calls.fetch_add(1, Ordering::SeqCst);

            ([(CONTENT_TYPE, JSON)], ROUTE_BODY)
        })
    }

    /// A route that answers [`ROUTE_BODY`] for each method of `methods`.
    fn json_route(methods: &[&str]) -> MethodRouter {
        let answer = || async { ([(CONTENT_TYPE, JSON)], ROUTE_BODY) };

        methods
            .iter()
            .fold(MethodRouter::new(), |route, method| match *method {
                "GET" => route.get(answer),
                "POST" => route.post(answer),
                other => panic!("no route for the method {other}"),
            })
    }

    /// The routes of each scenario and of most tests: a route that answers,
    /// and a route that panics.
    fn routes() -> Router {
        Router::new()
            .route("/healthz", json_route(&["GET"]))
            .route("/boom", get(boom))
    }

    async fn boom() -> &'static str {
        panic!("{PANIC_MESSAGE}")
    }

    /// One router behind [`edge`], on a Unix socket of its own.
    struct Service {
        /// Holds the directory of the socket until the test ends.
        _root: TempRoot,
        socket: PathBuf,
        trigger: ShutdownTrigger,
        tasks: Tasks,
        serving: JoinHandle<Result<Drained, ServeError>>,
    }

    impl Service {
        async fn start<E: ErrorBodies>(routes: Router, errors: E, access: AccessLog) -> Self {
            Self::start_with(routes, errors, access, LONG_DRAIN).await
        }

        async fn start_with<E: ErrorBodies>(
            routes: Router,
            errors: E,
            access: AccessLog,
            drain: Duration,
        ) -> Self {
            let root = TempRoot::new().unwrap();
            let socket = root.path().join("s.sock");
            let listen = Listen::Unix {
                socket: socket.to_str().unwrap().parse().unwrap(),
                dir: SocketDir::LeaveAsItIs,
            };
            let bound = bind(listen).await.unwrap();
            let (trigger, shutdown) = shutdown_pair();
            let tasks = Tasks::new(shutdown);
            let app = edge(routes, errors, tasks.clone(), access);
            let serving = tokio::spawn({
                let tasks = tasks.clone();

                async move { serve(vec![bound], app, &tasks, drain).await }
            });

            Self {
                _root: root,
                socket,
                trigger,
                tasks,
                serving,
            }
        }

        async fn starlette(routes: Router) -> Self {
            Self::start(routes, StarletteBodies, AccessLog::Skip).await
        }

        /// Sends one request and gives the answer.
        async fn ask(&self, request: &[u8]) -> Answer {
            Answer::parse(&within(RawHttp::unix(&self.socket, request)).await.unwrap())
        }

        /// Stops the service. Each connection and each handler must end.
        async fn stop(self) {
            self.trigger.trigger();

            assert_eq!(within(self.serving).await.unwrap(), Ok(Drained::Clean));
            assert_eq!(self.tasks.drain(LIMIT).await, Drained::Clean);
        }
    }

    /// A request body in chunks, with no stated length unless the test gives
    /// one. It counts each read.
    struct Chunks {
        left: VecDeque<Result<Frame<Bytes>, io::Error>>,
        stated: Option<u64>,
        reads: Arc<AtomicUsize>,
    }

    impl Chunks {
        fn of(chunks: &[&'static [u8]], reads: &Arc<AtomicUsize>) -> Self {
            Self {
                left: chunks
                    .iter()
                    .map(|chunk| Ok(Frame::data(Bytes::from_static(chunk))))
                    .collect(),
                stated: None,
                reads: Arc::clone(reads),
            }
        }
    }

    impl hyper::body::Body for Chunks {
        type Data = Bytes;
        type Error = io::Error;

        fn poll_frame(
            mut self: Pin<&mut Self>,
            _context: &mut Context<'_>,
        ) -> Poll<Option<Result<Frame<Bytes>, io::Error>>> {
            self.reads.fetch_add(1, Ordering::SeqCst);

            Poll::Ready(self.left.pop_front())
        }

        fn size_hint(&self) -> SizeHint {
            self.stated
                .map_or_else(SizeHint::default, SizeHint::with_exact)
        }
    }

    fn cap(bytes: usize) -> ByteCap {
        ByteCap::new(bytes).unwrap()
    }

    /// Runs one scenario in a child: this test program again, with only the
    /// child test. The child must end inside [`LIMIT`].
    fn run_child(scenario: &str) -> Output {
        let mut child = Command::new(std::env::current_exe().unwrap())
            .args([CHILD_TEST, "--exact", "--nocapture", "--test-threads=1"])
            .env(CHILD_VARIABLE, scenario)
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .unwrap();
        let deadline = Instant::now() + LIMIT;

        // A scenario writes a few lines, so a pipe never fills.
        while child.try_wait().unwrap().is_none() {
            if Instant::now() > deadline {
                child.kill().unwrap();
                child.wait().unwrap();

                panic!("the scenario {scenario} did not end");
            }

            thread::sleep(Duration::from_millis(10));
        }

        child.wait_with_output().unwrap()
    }

    #[test]
    fn a_failure_names_its_reason() {
        let table = [
            (EdgeFailure::NoRoute, "no route has the path"),
            (
                EdgeFailure::WrongMethod,
                "the route does not take the method",
            ),
            (EdgeFailure::Panic, "the handler panicked"),
            (EdgeFailure::HandlerLost, "the task of the handler was lost"),
            (
                EdgeFailure::BodyTooLarge {
                    cap: ByteCap::ONE_MIB,
                },
                "the body holds more than 1048576 bytes",
            ),
            (
                EdgeFailure::BodyUnreadable,
                "the body of the request stopped early",
            ),
        ];

        for (failure, text) in table {
            assert_eq!(failure.to_string(), text);
        }
    }

    #[test]
    fn the_signal_of_a_client_that_left_is_a_request_extension() {
        fn extension<T: Clone + Send + Sync + 'static>() {}

        extension::<ClientGone>();
        extension::<starlette::StarletteBodies>();
    }

    #[test]
    fn each_future_of_the_module_goes_to_another_thread() {
        fn sendable<F: Future + Send>(future: F) {
            drop(future);
        }

        let client = ClientGone {
            left: CancellationToken::new(),
        };

        sendable(client.gone());
        sendable(read_body(Body::empty(), ByteCap::ONE_MIB));
    }

    #[test]
    fn each_edge_surface_of_the_index_is_in_the_table() {
        let named: Vec<&str> = SURFACES.iter().map(|surface| surface.name).collect();
        let mut found = 0_usize;

        for row in vectors::index().unwrap() {
            if row.surface().starts_with(SURFACE_START) {
                assert!(named.contains(&row.surface()), "{}", row.surface());
                found += 1;
            }
        }

        assert_eq!(found, SURFACES.len());
    }

    /// The table [`SURFACES`] holds the route of each vector file. The notes
    /// of the file name the path and its methods in order, in one sentence.
    #[test]
    fn the_notes_of_each_surface_name_the_route_of_the_table() {
        for surface in &SURFACES {
            let sentence = match surface.methods {
                [only] => format!("The path {} has a route for {only}.", surface.path),
                [first, second] => format!(
                    "The path {} has a route for {first} and a route for {second}, in that order.",
                    surface.path
                ),
                other => panic!("no sentence for the methods {other:?}"),
            };
            let vectors = vectors::surface(surface.name).unwrap();

            assert!(
                vectors.notes().iter().any(|note| note.contains(&sentence)),
                "{}: {sentence}",
                surface.name
            );
        }
    }

    /// The parts of an answer of [`StarletteBodies`] that a vector holds.
    fn direct(failure: EdgeFailure) -> (u16, Option<String>, Vec<u8>) {
        let answer = StarletteBodies.answer(failure);
        let status = answer.status().as_u16();
        let content_type = answer
            .headers()
            .get(CONTENT_TYPE)
            .map(|value| value.to_str().unwrap().to_owned());
        let body = runtime()
            .block_on(read_body(answer.into_body(), ByteCap::ONE_MIB))
            .unwrap();

        (status, content_type, body.to_vec())
    }

    #[test]
    fn each_constant_of_the_starlette_answers_is_the_bytes_of_its_vector() {
        let table = [
            ("unknown-path", EdgeFailure::NoRoute),
            ("wrong-method", EdgeFailure::WrongMethod),
        ];

        for surface in &SURFACES {
            let vectors = vectors::surface(surface.name).unwrap();

            for (id, failure) in table {
                let vector = vectors.vector(id).unwrap();
                let value = vector.value().unwrap();
                let (status, content_type, body) = direct(failure);

                assert_eq!(
                    u64::from(status),
                    value["status"].as_u64().unwrap(),
                    "{} {id}",
                    surface.name
                );
                assert_eq!(
                    content_type.as_deref(),
                    value["content_type"].as_str(),
                    "{} {id}",
                    surface.name
                );
                assert_eq!(
                    body,
                    value["body"].as_str().unwrap().as_bytes(),
                    "{} {id}",
                    surface.name
                );
            }
        }
    }

    #[test]
    fn the_starlette_answers_with_no_vector_are_these() {
        let plain = Some(String::from("text/plain; charset=utf-8"));
        let table = [
            // `starlette/middleware/errors.py:259`.
            (
                EdgeFailure::Panic,
                500,
                plain.clone(),
                &b"Internal Server Error"[..],
            ),
            (
                EdgeFailure::HandlerLost,
                500,
                plain,
                &b"Internal Server Error"[..],
            ),
            (
                EdgeFailure::BodyTooLarge {
                    cap: ByteCap::ONE_MIB,
                },
                413,
                None,
                &b""[..],
            ),
            (EdgeFailure::BodyUnreadable, 400, None, &b""[..]),
        ];

        for (failure, status, content_type, body) in table {
            assert_eq!(
                direct(failure),
                (status, content_type, body.to_vec()),
                "{failure}"
            );
        }
    }

    /// The answers of one Python service: the answers of the framework, and
    /// the answer of the service itself to a handler that raises.
    ///
    /// Each Python service has a handler of its own for an exception. The
    /// port of a service states that body in its own [`ErrorBodies`]. This
    /// type stands for such a port: it takes the body and the content type
    /// from the vector `handler-raises` of the surface. The status is the
    /// status of [`StarletteBodies`].
    #[derive(Debug, Clone)]
    struct ServiceBodies {
        content_type: HeaderValue,
        body: String,
    }

    impl ServiceBodies {
        fn of(surface: &vectors::Surface) -> Self {
            let value = surface.vector(RAISES).unwrap().value().unwrap();

            Self {
                content_type: value["content_type"].as_str().unwrap().parse().unwrap(),
                body: value["body"].as_str().unwrap().to_owned(),
            }
        }
    }

    impl ErrorBodies for ServiceBodies {
        fn answer(&self, failure: EdgeFailure) -> Response {
            let mut answer = StarletteBodies.answer(failure);
            if failure == EdgeFailure::Panic {
                *answer.body_mut() = Body::from(self.body.clone());
                answer
                    .headers_mut()
                    .insert(CONTENT_TYPE, self.content_type.clone());
            }

            answer
        }
    }

    /// The parts of an answer that a vector holds.
    #[derive(Debug, PartialEq, Eq)]
    struct Told {
        status: u64,
        content_type: Option<String>,
        body: String,
        /// Each method of the `Allow` header, in sorted order.
        allow: Option<Vec<String>>,
        /// The path of the `Location` header, with its query.
        location: Option<String>,
    }

    impl Told {
        /// The parts of the value of a vector.
        ///
        /// The function fails for a key that the walk does not compare. A
        /// new key of a surface then fails the walk until a part reads it.
        fn by(value: &serde_json::Value) -> Self {
            for key in value.as_object().unwrap().keys() {
                assert!(
                    KEYS.contains(&key.as_str()) || key == COOKIES_KEY,
                    "no part reads the key {key} of a vector"
                );
            }

            Self {
                status: value["status"].as_u64().unwrap(),
                content_type: value["content_type"].as_str().map(str::to_owned),
                body: value["body"].as_str().unwrap().to_owned(),
                allow: value.get("allow").map(|methods| {
                    methods
                        .as_array()
                        .unwrap()
                        .iter()
                        .map(|method| method.as_str().unwrap().to_owned())
                        .collect()
                }),
                location: value
                    .get("location")
                    .map(|path| path.as_str().unwrap().to_owned()),
            }
        }

        /// The parts of the answer of a router. The `Location` header must
        /// hold the scheme and the host of the request before its path.
        fn of(answer: &Answer) -> Self {
            Self {
                status: u64::from(answer.status),
                content_type: answer.header("content-type").map(str::to_owned),
                body: answer.text().to_owned(),
                allow: answer.allow(),
                location: answer.header("location").map(|place| {
                    place
                        .strip_prefix(ORIGIN)
                        .unwrap_or_else(|| panic!("the header {place} names no host"))
                        .to_owned()
                }),
            }
        }
    }

    /// Walks each vector of each surface through a router with [`edge`], on
    /// a real socket. The router has the route of the vector file and the
    /// route that raises.
    ///
    /// The answer to four vectors is an answer of the framework, and the
    /// router has [`StarletteBodies`] for them. The answer to the vector
    /// `handler-raises` is the answer of the service, and the router has
    /// [`ServiceBodies`] for it. That vector makes a handler panic, so the
    /// walk writes to the log and runs in the child.
    fn a_router_answers_as_each_vector_says() {
        runtime().block_on(async {
            for surface in &SURFACES {
                let vectors = vectors::surface(surface.name).unwrap();
                let raise_path = vectors.context()["raise_path"].as_str().unwrap();
                let routes = Router::new()
                    .route(surface.path, json_route(surface.methods))
                    .route(raise_path, get(boom));
                let framework = Service::starlette(routes.clone()).await;
                let service =
                    Service::start(routes, ServiceBodies::of(&vectors), AccessLog::Skip).await;

                for vector in vectors.vectors() {
                    let args = vector.input().args().unwrap();
                    let method = args["method"].as_str().unwrap();
                    let path = args["path"].as_str().unwrap();
                    let asked = if vector.id() == RAISES {
                        &service
                    } else {
                        &framework
                    };
                    let answer = asked.ask(&request(method, path)).await;

                    assert_eq!(
                        Told::of(&answer),
                        Told::by(vector.value().unwrap()),
                        "{} {}",
                        surface.name,
                        vector.id()
                    );
                    // The framework sets no cookie.
                    assert_eq!(answer.header("set-cookie"), None);
                }

                framework.stop().await;
                service.stop().await;
            }
        });
    }

    /// The answers of a service with an error body of its own.
    #[derive(Debug, Clone)]
    struct OwnBodies;

    impl ErrorBodies for OwnBodies {
        fn answer(&self, failure: EdgeFailure) -> Response {
            let (status, body) = match failure {
                EdgeFailure::NoRoute => (StatusCode::NOT_FOUND, "own: no route"),
                EdgeFailure::WrongMethod => (StatusCode::METHOD_NOT_ALLOWED, "own: wrong method"),
                EdgeFailure::HandlerLost => (StatusCode::IM_A_TEAPOT, "own: lost"),
                _ => (StatusCode::BAD_GATEWAY, "own: other"),
            };
            let mut answer = Response::new(Body::from(body));
            *answer.status_mut() = status;

            answer
        }
    }

    #[test]
    fn the_service_states_the_answer_of_each_fallback() {
        runtime().block_on(async {
            let routes = Router::new()
                .route("/healthz", json_route(&["GET"]))
                // The fallback of the router is gone after `edge`.
                .fallback(|| async { "the fallback of the service" });
            let service = Service::start(routes, OwnBodies, AccessLog::Skip).await;

            let unknown = service.ask(&request("GET", "/no/such/path")).await;
            let wrong = service.ask(&request("DELETE", "/healthz")).await;
            let known = service.ask(&request("GET", "/healthz?key=1")).await;

            assert_eq!((unknown.status, unknown.text()), (404, "own: no route"));
            assert_eq!((wrong.status, wrong.text()), (405, "own: wrong method"));
            assert_eq!(wrong.header("allow"), Some("GET"));
            assert_eq!((known.status, known.text()), (200, ROUTE_BODY));
            service.stop().await;
        });
    }

    /// The body of the answer of [`StarletteBodies`] for a wrong method has
    /// 31 bytes, and the body for a path with no route has 22.
    #[test]
    fn the_answer_to_head_has_the_length_of_its_body_and_no_byte_of_a_body() {
        runtime().block_on(async {
            let calls = Arc::new(AtomicUsize::new(0));
            let routes = Router::new().route("/healthz", counted(&calls));
            let service = Service::starlette(routes).await;

            let head = service.ask(&request("HEAD", "/healthz")).await;
            let wrong = service.ask(&request("POST", "/healthz")).await;
            let unknown = service.ask(&request("HEAD", "/no/such/path")).await;
            let slash = service.ask(&request("HEAD", "/healthz/")).await;

            assert_eq!(head.status, 405);
            assert_eq!(head.header("content-type"), Some(JSON));
            assert_eq!(head.header("allow"), Some("GET"));
            assert_eq!(head.header("content-length"), Some("31"));
            assert!(head.body.is_empty(), "{head:?}");
            assert_eq!(wrong.status, 405);
            assert_eq!(wrong.header("content-length"), Some("31"));
            assert_eq!(wrong.body.len(), 31);
            assert_eq!(unknown.status, 404);
            assert_eq!(unknown.header("content-length"), Some("22"));
            assert!(unknown.body.is_empty(), "{unknown:?}");
            assert_eq!(slash.status, 307);
            assert_eq!(slash.header("location"), Some("http://test/healthz"));
            assert!(slash.body.is_empty(), "{slash:?}");
            // No handler ran for a `HEAD` request.
            assert_eq!(calls.load(Ordering::SeqCst), 0);
            service.stop().await;
        });
    }

    #[test]
    fn a_path_that_differs_from_a_route_in_its_final_slashes_gets_status_307() {
        let table = [
            ("GET", "/healthz/", Some("http://test/healthz")),
            ("GET", "/healthz//", Some("http://test/healthz")),
            ("POST", "/healthz/", Some("http://test/healthz")),
            ("DELETE", "/healthz/", Some("http://test/healthz")),
            (
                "GET",
                "/healthz/?a=1&b=%20",
                Some("http://test/healthz?a=1&b=%20"),
            ),
            ("GET", "/healthz/?", Some("http://test/healthz")),
            ("GET", "/dir", Some("http://test/dir/")),
            ("PUT", "/dir?x=1", Some("http://test/dir/?x=1")),
            // The other form of `/dir//` is `/dir`, and no route has it.
            ("GET", "/dir//", None),
            ("GET", "/no/such/path/", None),
            ("GET", "/no/such/path", None),
            // The path `/` and a path of slashes only have no other form.
            ("GET", "/", None),
            ("GET", "//", None),
            // A target that is no path.
            ("OPTIONS", "*", None),
        ];

        runtime().block_on(async {
            let calls = Arc::new(AtomicUsize::new(0));
            let routes = Router::new()
                .route("/healthz", counted(&calls))
                .route("/dir/", counted(&calls));
            let service = Service::starlette(routes).await;

            for (method, target, place) in table {
                let answer = service.ask(&request(method, target)).await;

                match place {
                    Some(place) => {
                        assert_eq!(answer.status, 307, "{method} {target}");
                        assert_eq!(answer.header("location"), Some(place), "{method} {target}");
                        assert_eq!(answer.header("content-type"), None, "{method} {target}");
                        assert_eq!(answer.header("content-length"), Some("0"));
                        assert!(answer.body.is_empty(), "{method} {target}");
                    }
                    None => {
                        assert_eq!(answer.status, 404, "{method} {target}");
                        assert_eq!(answer.header("location"), None, "{method} {target}");
                        assert_eq!(answer.text(), "{\"detail\":\"Not Found\"}");
                    }
                }
            }
            // No request of the table went to a route.
            assert_eq!(calls.load(Ordering::SeqCst), 0);

            assert_eq!(service.ask(&request("GET", "/dir/")).await.status, 200);
            assert_eq!(calls.load(Ordering::SeqCst), 1);
            service.stop().await;
        });
    }

    /// The Python framework writes the address of its listener for a `Host`
    /// header that names no host (`starlette/datastructures.py:49-54`). The
    /// header here then holds the path and the query only.
    ///
    /// `uvicorn` takes the scheme from the header `X-Forwarded-Proto` of a
    /// client on the loopback address
    /// (`uvicorn/middleware/proxy_headers.py:35-51`). The scheme here is
    /// `http` for each client.
    #[test]
    fn the_location_names_the_host_of_a_host_header_that_names_one() {
        let table = [
            (
                Some("192.0.2.10:8340"),
                "http://192.0.2.10:8340/healthz?x=1",
            ),
            (Some("Board.Example"), "http://Board.Example/healthz?x=1"),
            (
                Some("[2001:db8::1]:8340"),
                "http://[2001:db8::1]:8340/healthz?x=1",
            ),
            (Some("other.example/path"), "/healthz?x=1"),
            (Some("user@other.example"), "/healthz?x=1"),
            (Some("other.example:port"), "/healthz?x=1"),
            (None, "/healthz?x=1"),
        ];

        runtime().block_on(async {
            let calls = Arc::new(AtomicUsize::new(0));
            let service =
                Service::starlette(Router::new().route("/healthz", counted(&calls))).await;

            for (host, place) in table {
                let answer = service.ask(&request_of(host, "/healthz/?x=1")).await;

                assert_eq!(answer.status, 307, "{host:?}");
                assert_eq!(answer.header("location"), Some(place), "{host:?}");
            }

            let forwarded = service
                .ask(
                    b"GET /healthz/ HTTP/1.1\r\nHost: test\r\nX-Forwarded-Proto: https\r\n\
                      Connection: close\r\n\r\n",
                )
                .await;

            assert_eq!(forwarded.header("location"), Some("http://test/healthz"));
            service.stop().await;
        });
    }

    /// The table holds what `_HOST_RE` of `starlette/datastructures.py:25`
    /// takes and refuses.
    #[test]
    fn a_host_is_a_name_or_an_address_in_brackets_with_an_optional_port() {
        let hosts = [
            "test",
            "Board.Example",
            "192.0.2.10",
            "192.0.2.10:8340",
            "a-b.c:0",
            "-",
            ".",
            "[::1]",
            "[::1]:8340",
            "[2001:DB8::1]",
            "[::ffff:192.0.2.10]:80",
            "[:.]",
            "[a:b]",
        ];
        let others = [
            "",
            ":8340",
            "test:",
            "test:80a",
            "test:8340:1",
            "te st",
            "test/path",
            "test?query",
            "test#part",
            "user@test",
            "te_st",
            "t\u{e9}st",
            "test:\u{662}",
            "[::1",
            "::1",
            "[]",
            "[:]",
            "[1]",
            "[g::1]",
            "[.:1]",
            "[::1]x",
            "[::1]:",
            "[::1]:x",
            "[::1]]",
            "test\n",
        ];

        for host in hosts {
            assert!(names_a_host(host), "{host:?}");
        }
        for other in others {
            assert!(!names_a_host(other), "{other:?}");
        }
    }

    #[test]
    fn the_other_form_of_a_path_differs_in_its_final_slashes() {
        let table = [
            ("/healthz", Some("/healthz/")),
            ("/healthz/", Some("/healthz")),
            ("/healthz///", Some("/healthz")),
            ("/a/b", Some("/a/b/")),
            ("/a/b/", Some("/a/b")),
            ("/", None),
            ("//", None),
            ("", None),
            ("*", None),
            ("host:443", None),
        ];

        for (path, other) in table {
            assert_eq!(other_form(path).as_deref(), other, "{path:?}");
        }
    }

    /// The Python framework decodes the target of a request first
    /// (`uvicorn/protocols/http/h11_impl.py:201-202`): it serves
    /// `/%68ealthz` as `/healthz`, and it has no route for a target with a
    /// scheme and a host.
    #[test]
    fn a_router_matches_the_path_as_the_client_sent_it() {
        runtime().block_on(async {
            let calls = Arc::new(AtomicUsize::new(0));
            let service =
                Service::starlette(Router::new().route("/healthz", counted(&calls))).await;

            let escaped = service.ask(&request("GET", "/%68ealthz")).await;
            let escaped_slash = service.ask(&request("GET", "/healthz%2F")).await;

            assert_eq!(escaped.status, 404);
            assert_eq!(escaped_slash.status, 404);
            assert_eq!(escaped_slash.header("location"), None);
            assert_eq!(calls.load(Ordering::SeqCst), 0);

            let absolute = service.ask(&request("GET", "http://test/healthz")).await;

            assert_eq!(absolute.status, 200);
            assert_eq!(calls.load(Ordering::SeqCst), 1);
            service.stop().await;
        });
    }

    #[test]
    fn the_method_of_the_question_is_no_method_that_a_route_takes() {
        let method = unrouted();

        assert_eq!(method.as_str(), UNROUTED);
        for routed in [
            Method::GET,
            Method::HEAD,
            Method::POST,
            Method::PUT,
            Method::DELETE,
            Method::PATCH,
            Method::OPTIONS,
            Method::TRACE,
            Method::CONNECT,
        ] {
            assert_ne!(method, routed);
        }
    }

    /// The rule of [`edge`]: each route of a service has its methods by
    /// name. This test holds what a route for each method gets.
    #[test]
    fn a_route_that_takes_each_method_gets_the_question_and_each_head_request() {
        runtime().block_on(async {
            let seen: Arc<Mutex<Vec<String>>> = Arc::new(Mutex::new(Vec::new()));
            let routes = Router::new().route(
                "/each",
                any({
                    let seen = Arc::clone(&seen);

                    move |request: Request| async move {
                        locked(&seen).push(format!("{} {}", request.method(), request.uri()));

                        ROUTE_BODY
                    }
                }),
            );
            let service = Service::starlette(routes).await;

            let head = service.ask(&request("HEAD", "/each")).await;
            let slash = service.ask(&request("GET", "/each/?x=1")).await;

            // The handler made the answer to `HEAD`, and the client got no
            // body.
            assert_eq!(head.status, 200);
            assert!(head.body.is_empty());
            assert_eq!(slash.status, 307);
            assert_eq!(slash.header("location"), Some("http://test/each?x=1"));
            assert_eq!(*locked(&seen), ["UNROUTED /each", "UNROUTED /each"]);
            service.stop().await;
        });
    }

    /// The rule of [`edge`]: a layer that the router has also gets the
    /// question. The question has no header of the request of the client.
    #[test]
    fn a_layer_of_the_router_gets_the_question_with_no_header() {
        runtime().block_on(async {
            let seen: Arc<Mutex<Vec<String>>> = Arc::new(Mutex::new(Vec::new()));
            let record = {
                let seen = Arc::clone(&seen);

                move |request: Request, next: Next| {
                    let seen = Arc::clone(&seen);

                    async move {
                        locked(&seen).push(format!(
                            "{} {} {}",
                            request.method(),
                            request.uri(),
                            request.headers().len()
                        ));

                        next.run(request).await
                    }
                }
            };
            let calls = Arc::new(AtomicUsize::new(0));
            let routes = Router::new()
                .route("/healthz", counted(&calls))
                .layer(middleware::from_fn(record));
            let service = Service::starlette(routes).await;

            let slash = service.ask(&request("GET", "/healthz/?x=1")).await;

            assert_eq!(slash.status, 307);
            assert_eq!(*locked(&seen), ["UNROUTED /healthz 0"]);
            assert_eq!(calls.load(Ordering::SeqCst), 0);
            service.stop().await;
        });
    }

    #[test]
    fn the_allow_header_names_the_first_method_that_the_service_gave_to_the_path() {
        runtime().block_on(async {
            let routes = Router::new()
                .route("/v1/sessions", json_route(&["POST", "GET"]))
                .route("/healthz", json_route(&["GET"]))
                .route(
                    "/own",
                    get(|| async {
                        (
                            StatusCode::METHOD_NOT_ALLOWED,
                            [(ALLOW, "GET, POST")],
                            "the answer of a handler",
                        )
                    }),
                );
            let service = Service::starlette(routes).await;

            let first_post = service.ask(&request("PUT", "/v1/sessions")).await;
            let first_get = service.ask(&request("DELETE", "/healthz")).await;
            let unknown_method = service.ask(&request("BREW", "/healthz")).await;
            let own = service.ask(&request("GET", "/own")).await;

            assert_eq!(first_post.status, 405);
            assert_eq!(first_post.header("allow"), Some("POST"));
            assert_eq!(first_get.header("allow"), Some("GET"));
            assert_eq!(unknown_method.status, 405);
            assert_eq!(unknown_method.header("allow"), Some("GET"));
            // The answer of a handler keeps the header of the handler.
            assert_eq!(own.status, 405);
            assert_eq!(own.header("allow"), Some("GET, POST"));
            assert_eq!(own.text(), "the answer of a handler");
            service.stop().await;
        });
    }

    #[test]
    fn only_the_answer_for_a_wrong_method_loses_methods_of_its_allow_header() {
        let with_header = |value: &'static str| {
            let mut answer = method_refused(&StarletteBodies);
            answer
                .headers_mut()
                .insert(ALLOW, HeaderValue::from_static(value));

            answer
        };

        let mut marked = with_header("POST,GET,HEAD");
        keep_first_allowed(&mut marked);
        assert_eq!(marked.headers()[ALLOW], "POST");
        assert!(marked.extensions().get::<MethodRefused>().is_none());

        let mut spaced = with_header("GET , POST");
        keep_first_allowed(&mut spaced);
        assert_eq!(spaced.headers()[ALLOW], "GET");

        let mut no_header = method_refused(&StarletteBodies);
        keep_first_allowed(&mut no_header);
        assert!(!no_header.headers().contains_key(ALLOW));

        let mut of_a_handler = StarletteBodies.answer(EdgeFailure::WrongMethod);
        of_a_handler
            .headers_mut()
            .insert(ALLOW, HeaderValue::from_static("POST,GET"));
        keep_first_allowed(&mut of_a_handler);
        assert_eq!(of_a_handler.headers()[ALLOW], "POST,GET");
    }

    #[test]
    fn a_handler_ends_after_its_client_left() {
        for runtime in each_runtime() {
            runtime.block_on(async {
                let entered = Arc::new(Notify::new());
                let release = Arc::new(Notify::new());
                let (hand_over, mut signals) = mpsc::channel::<ClientGone>(1);
                let root = TempRoot::new().unwrap();
                let written = root.path().join("written");
                let routes = Router::new().route(
                    "/slow",
                    post({
                        let entered = Arc::clone(&entered);
                        let release = Arc::clone(&release);
                        let written = written.clone();

                        move |Extension(client): Extension<ClientGone>| async move {
                            hand_over.send(client).await.unwrap();
                            entered.notify_one();
                            release.notified().await;
                            fs::write(&written, b"the handler ran to its end").unwrap();

                            "late"
                        }
                    }),
                );
                let service = Service::starlette(routes).await;
                let mut client = UnixStream::connect(&service.socket).await.unwrap();
                client
                    .write_all(b"POST /slow HTTP/1.1\r\nHost: test\r\nContent-Length: 0\r\n\r\n")
                    .await
                    .unwrap();
                within(entered.notified()).await;

                // The client leaves in the middle of the handler. The server
                // then drops the request, and the signal of the handler fires.
                drop(client);
                let signal = within(signals.recv()).await.unwrap();
                within(signal.gone()).await;
                assert!(!written.exists());

                release.notify_one();
                assert_eq!(service.tasks.drain(LIMIT).await, Drained::Clean);
                assert_eq!(fs::read(&written).unwrap(), b"the handler ran to its end");
                service.stop().await;
            });
        }
    }

    #[test]
    fn a_handler_that_waits_for_its_client_stops_when_the_client_leaves() {
        for runtime in each_runtime() {
            runtime.block_on(async {
                let entered = Arc::new(Notify::new());
                let root = TempRoot::new().unwrap();
                let marker = root.path().join("marker");
                let routes = Router::new().route(
                    "/slow",
                    get({
                        let entered = Arc::clone(&entered);
                        let marker = marker.clone();

                        move |Extension(client): Extension<ClientGone>| async move {
                            entered.notify_one();
                            tokio::select! {
                                () = client.gone() => fs::write(&marker, b"gone").unwrap(),
                                () = tokio::time::sleep(LIMIT) => fs::write(&marker, b"stayed").unwrap(),
                            }

                            "late"
                        }
                    }),
                );
                let service = Service::starlette(routes).await;
                let mut client = UnixStream::connect(&service.socket).await.unwrap();
                client
                    .write_all(b"GET /slow HTTP/1.1\r\nHost: test\r\n\r\n")
                    .await
                    .unwrap();
                within(entered.notified()).await;

                drop(client);

                assert_eq!(service.tasks.drain(LIMIT).await, Drained::Clean);
                assert_eq!(fs::read(&marker).unwrap(), b"gone");
                service.stop().await;
            });
        }
    }

    #[test]
    fn the_signal_fires_when_the_service_ends_the_connection_at_its_stop() {
        for runtime in each_runtime() {
            runtime.block_on(async {
                let entered = Arc::new(Notify::new());
                let root = TempRoot::new().unwrap();
                let marker = root.path().join("marker");
                let routes = Router::new().route(
                    "/slow",
                    get({
                        let entered = Arc::clone(&entered);
                        let marker = marker.clone();

                        move |Extension(client): Extension<ClientGone>| async move {
                            entered.notify_one();
                            client.gone().await;
                            fs::write(&marker, b"gone").unwrap();

                            "late"
                        }
                    }),
                );
                let service =
                    Service::start_with(routes, StarletteBodies, AccessLog::Skip, SHORT_DRAIN)
                        .await;
                let client = tokio::spawn({
                    let socket = service.socket.clone();

                    async move { RawHttp::unix(&socket, &request("GET", "/slow")).await }
                });
                within(entered.notified()).await;

                // The client stays. The handler ends only at the signal, so the
                // stop reaches the drain limit with one connection open.
                service.trigger.trigger();

                assert_eq!(
                    within(service.serving).await.unwrap(),
                    Ok(Drained::TimedOut { left: 1 })
                );
                assert_eq!(service.tasks.drain(LIMIT).await, Drained::Clean);
                assert_eq!(fs::read(&marker).unwrap(), b"gone");
                // The client got no answer: the connection ended first.
                assert!(
                    within(client)
                        .await
                        .unwrap()
                        .is_ok_and(|bytes| bytes.is_empty())
                );
            });
        }
    }

    #[test]
    fn a_handler_that_the_runtime_stopped_gives_the_answer_for_a_lost_handler() {
        let answer = settled(
            &OwnBodies,
            &Method::GET,
            "/healthz",
            Err(TaskLost::Cancelled),
        );

        assert_eq!(answer.status(), StatusCode::IM_A_TEAPOT);

        let kept = settled(
            &OwnBodies,
            &Method::GET,
            "/healthz",
            Ok(StarletteBodies.answer(EdgeFailure::NoRoute)),
        );

        assert_eq!(kept.status(), StatusCode::NOT_FOUND);
    }

    #[test]
    fn the_signal_does_not_fire_for_a_client_that_gets_its_answer() {
        for runtime in each_runtime() {
            runtime.block_on(async {
                let kept: Arc<Mutex<Option<ClientGone>>> = Arc::new(Mutex::new(None));
                let routes = Router::new().route(
                    "/healthz",
                    get({
                        let kept = Arc::clone(&kept);

                        move |Extension(client): Extension<ClientGone>| async move {
                            *locked(&kept) = Some(client);

                            ROUTE_BODY
                        }
                    }),
                );
                let service = Service::starlette(routes).await;

                let answer = service.ask(&request("GET", "/healthz")).await;

                assert_eq!(answer.status, 200);
                let signal = locked(&kept).take().unwrap();
                // The connection is closed now, and the signal still waits.
                assert!(tokio::time::timeout(QUIET, signal.gone()).await.is_err());
                service.stop().await;
                assert!(tokio::time::timeout(QUIET, signal.gone()).await.is_err());
            });
        }
    }

    #[test]
    fn a_streamed_answer_goes_through_the_edge() {
        runtime().block_on(async {
            let routes = Router::new().route(
                "/stream",
                get(|| async {
                    let reads = Arc::new(AtomicUsize::new(0));

                    Response::new(Body::new(Chunks::of(&[b"one\n", b"two\n"], &reads)))
                }),
            );
            let service = Service::starlette(routes).await;

            let answer = service.ask(&request("GET", "/stream")).await;

            assert_eq!(answer.status, 200);
            assert_eq!(answer.header("transfer-encoding"), Some("chunked"));
            assert_eq!(answer.text(), "4\r\none\n\r\n4\r\ntwo\n\r\n0\r\n\r\n");
            service.stop().await;
        });
    }

    #[test]
    fn read_body_reads_up_to_the_cap() {
        runtime().block_on(async {
            let reads = Arc::new(AtomicUsize::new(0));

            assert_eq!(read_body(Body::empty(), cap(1)).await.unwrap(), &b""[..]);
            assert_eq!(
                read_body(Body::from("12345678"), cap(8)).await.unwrap(),
                &b"12345678"[..]
            );
            assert_eq!(
                read_body(Body::from("123456789"), cap(8)).await,
                Err(EdgeFailure::BodyTooLarge { cap: cap(8) })
            );
            assert_eq!(
                read_body(
                    Body::new(Chunks::of(&[b"1234", b"", b"5678"], &reads)),
                    cap(8)
                )
                .await
                .unwrap(),
                &b"12345678"[..]
            );
        });
    }

    #[test]
    fn read_body_refuses_a_stated_length_past_the_cap_before_a_read() {
        runtime().block_on(async {
            let reads = Arc::new(AtomicUsize::new(0));
            let mut body = Chunks::of(&[b"1234", b"5678", b"9"], &reads);
            body.stated = Some(9);

            let refused = read_body(Body::new(body), cap(8)).await;

            assert_eq!(refused, Err(EdgeFailure::BodyTooLarge { cap: cap(8) }));
            assert_eq!(reads.load(Ordering::SeqCst), 0);
        });
    }

    #[test]
    fn read_body_stops_at_the_first_byte_past_the_cap() {
        runtime().block_on(async {
            let reads = Arc::new(AtomicUsize::new(0));
            let body = Chunks::of(&[b"1234", b"5678", b"9", b"never read"], &reads);

            let refused = read_body(Body::new(body), cap(8)).await;

            assert_eq!(refused, Err(EdgeFailure::BodyTooLarge { cap: cap(8) }));
            assert_eq!(reads.load(Ordering::SeqCst), 3);
        });
    }

    #[test]
    fn read_body_gives_a_body_that_stops_early_as_unreadable() {
        runtime().block_on(async {
            let reads = Arc::new(AtomicUsize::new(0));
            let mut body = Chunks::of(&[b"1234"], &reads);
            body.left
                .push_back(Err(io::Error::other("the client left")));

            let refused = read_body(Body::new(body), cap(8)).await;

            assert_eq!(refused, Err(EdgeFailure::BodyUnreadable));
        });
    }

    #[test]
    fn read_body_drops_the_trailers_of_a_body() {
        runtime().block_on(async {
            let reads = Arc::new(AtomicUsize::new(0));
            let mut body = Chunks::of(&[b"1234"], &reads);
            body.left.push_back(Ok(Frame::trailers(HeaderMap::new())));

            assert_eq!(
                read_body(Body::new(body), cap(8)).await.unwrap(),
                &b"1234"[..]
            );
        });
    }

    /// A route that reads its body with [`read_body`] and answers what the
    /// read gave.
    fn echo_route(limit: ByteCap) -> MethodRouter {
        post(move |request: Request| async move {
            match read_body(request.into_body(), limit).await {
                Ok(bytes) => Response::new(Body::from(bytes)),
                Err(failure) => StarletteBodies.answer(failure),
            }
        })
    }

    #[test]
    fn a_handler_reads_a_json_body_with_no_content_type() {
        for runtime in each_runtime() {
            runtime.block_on(async {
                let service =
                    Service::starlette(Router::new().route("/echo", echo_route(cap(16)))).await;

                let read = service
                    .ask(
                        b"POST /echo HTTP/1.1\r\nHost: test\r\nConnection: close\r\n\
                          Content-Length: 7\r\n\r\n{\"n\":1}",
                    )
                    .await;
                let chunked = service
                    .ask(
                        b"POST /echo HTTP/1.1\r\nHost: test\r\nConnection: close\r\n\
                          Transfer-Encoding: chunked\r\n\r\n4\r\n{\"n\"\r\n3\r\n:1}\r\n0\r\n\r\n",
                    )
                    .await;
                let too_large = service
                    .ask(
                        b"POST /echo HTTP/1.1\r\nHost: test\r\nConnection: close\r\n\
                          Content-Length: 17\r\n\r\n{\"n\":11111111111}",
                    )
                    .await;

                assert_eq!((read.status, read.text()), (200, "{\"n\":1}"));
                assert_eq!((chunked.status, chunked.text()), (200, "{\"n\":1}"));
                assert_eq!((too_large.status, too_large.text()), (413, ""));
                assert_eq!(too_large.header("content-type"), None);
                service.stop().await;
            });
        }
    }

    #[test]
    fn a_handler_sees_a_body_that_its_client_cut() {
        runtime().block_on(async {
            let (hand_over, mut reads) = mpsc::channel::<Result<Bytes, EdgeFailure>>(1);
            let routes = Router::new().route(
                "/echo",
                post(move |request: Request| async move {
                    let read = read_body(request.into_body(), cap(64)).await;
                    hand_over.send(read).await.unwrap();

                    "late"
                }),
            );
            let service = Service::starlette(routes).await;

            // The head states 10 bytes, and the client closes its side
            // after 4.
            let sent = within(RawHttp::unix_then_eof(
                &service.socket,
                b"POST /echo HTTP/1.1\r\nHost: test\r\nContent-Length: 10\r\n\r\n{\"n\"",
            ))
            .await;

            assert_eq!(
                within(reads.recv()).await.unwrap(),
                Err(EdgeFailure::BodyUnreadable)
            );
            // The client still reads, so the server can send the answer of
            // the handler. No assertion holds those bytes.
            drop(sent);
            service.stop().await;
        });
    }

    /// The gap of the module doc and of the table `UNRECORDED`. The runtime
    /// has one thread, and the client writes the request and closes its side
    /// in one step. The server thus reads the request and the end of the
    /// bytes in one step too.
    #[test]
    fn a_request_whose_client_closed_its_side_at_once_runs_no_handler() {
        let table: [&[u8]; 2] = [
            b"POST /count HTTP/1.1\r\nHost: test\r\n\r\n",
            b"POST /count HTTP/1.1\r\nHost: test\r\nContent-Length: 7\r\n\r\n{\"n\":1}",
        ];

        runtime().block_on(async {
            let calls = Arc::new(AtomicUsize::new(0));
            let routes = Router::new().route(
                "/count",
                post({
                    let calls = Arc::clone(&calls);

                    move || async move {
                        calls.fetch_add(1, Ordering::SeqCst);

                        "counted"
                    }
                }),
            );
            let service = Service::starlette(routes).await;

            for request in table {
                let answer = within(RawHttp::unix_then_eof(&service.socket, request))
                    .await
                    .unwrap();

                assert!(answer.is_empty(), "{answer:?}");
            }
            // The stop waits for each connection and for each handler.
            service.stop().await;
            assert_eq!(calls.load(Ordering::SeqCst), 0);
        });
    }

    #[test]
    fn the_child_runs_one_scenario() {
        let Some(name) = std::env::var_os(CHILD_VARIABLE) else {
            return;
        };
        let scenario = SCENARIOS
            .iter()
            .find(|scenario| name == scenario.name)
            .unwrap();

        crate::log::init(CHILD_PROGRAM);
        (scenario.run)();
    }

    #[test]
    fn each_scenario_writes_its_lines_and_no_request_data() {
        for scenario in SCENARIOS {
            let name = scenario.name;
            let child = run_child(name);
            let stdout = String::from_utf8(child.stdout).unwrap();
            let stderr = String::from_utf8(child.stderr).unwrap();

            assert!(child.status.success(), "{name}: {stdout}\n{stderr}");
            assert!(stdout.contains("1 passed"), "{name}: {stdout}");

            // A line is the time, the level, the target and the message.
            let written: Vec<String> = stderr
                .lines()
                .filter_map(|line| line.split_once(' '))
                .map(|(_time, rest)| rest.to_owned())
                .collect();

            (scenario.check)(&written);
            for secret in [PANIC_MESSAGE, QUERY_SECRET] {
                assert!(!stderr.contains(secret), "{name}: {stderr}");
                assert!(!stdout.contains(secret), "{name}: {stdout}");
            }
        }
    }

    /// A handler panics. Its client gets the answer for a panic, and the
    /// next request of the service gets its answer.
    fn a_handler_panics() {
        runtime().block_on(async {
            let service = Service::starlette(routes()).await;

            let target = format!("/boom?key={QUERY_SECRET}");
            let failed = service.ask(&request("GET", &target)).await;
            let next = service.ask(&request("GET", "/healthz")).await;

            assert_eq!(failed.status, 500);
            assert_eq!(
                failed.header("content-type"),
                Some("text/plain; charset=utf-8")
            );
            assert_eq!(failed.text(), "Internal Server Error");
            assert_eq!((next.status, next.text()), (200, ROUTE_BODY));
            service.stop().await;
        });
    }

    /// The answers of a service whose answer for `NoRoute` panics.
    #[derive(Debug, Clone)]
    struct Brittle;

    impl ErrorBodies for Brittle {
        fn answer(&self, failure: EdgeFailure) -> Response {
            assert_ne!(failure, EdgeFailure::NoRoute, "{PANIC_MESSAGE}");

            StarletteBodies.answer(failure)
        }
    }

    /// The fallback runs in the task of the request too, so its panic is
    /// the answer for a panic and not the end of the connection.
    fn the_answer_for_no_route_panics() {
        runtime().block_on(async {
            let service = Service::start(routes(), Brittle, AccessLog::Skip).await;

            let failed = service.ask(&request("GET", "/no/such/path")).await;
            let next = service.ask(&request("GET", "/healthz")).await;

            assert_eq!(
                (failed.status, failed.text()),
                (500, "Internal Server Error")
            );
            assert_eq!(next.status, 200);
            service.stop().await;
        });
    }

    /// Sends the requests of the two access scenarios.
    async fn ask_each_kind(service: &Service) {
        let with_query = format!("/healthz?key={QUERY_SECRET}");

        assert_eq!(service.ask(&request("GET", &with_query)).await.status, 200);
        assert_eq!(
            service.ask(&request("GET", "/no/such/path")).await.status,
            404
        );
        assert_eq!(service.ask(&request("POST", "/healthz")).await.status, 405);
        assert_eq!(service.ask(&request("HEAD", "/healthz")).await.status, 405);
        let with_slash = format!("/healthz/?key={QUERY_SECRET}");
        assert_eq!(service.ask(&request("GET", &with_slash)).await.status, 307);
    }

    /// A service with `AccessLog::Write` writes one line for each request,
    /// with no query.
    fn a_service_writes_access_lines() {
        runtime().block_on(async {
            let service = Service::start(routes(), StarletteBodies, AccessLog::Write).await;

            ask_each_kind(&service).await;
            assert_eq!(service.ask(&request("GET", "/boom")).await.status, 500);
            service.stop().await;
        });
    }

    /// A service with `AccessLog::Skip` writes no line for a request.
    fn a_service_writes_no_access_line() {
        runtime().block_on(async {
            let service = Service::starlette(routes()).await;

            ask_each_kind(&service).await;
            service.stop().await;
        });
    }
}
