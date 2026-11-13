//! The router of the host API (decisions CH25, CH26). `routes!` expands
//! `route_table!` with the concrete type of each route, so a handler set
//! needs a handler for each route of the one table.
//!
//! The order of the checks follows decisions CH26, CH33 rule 13 and CH34
//! rule 7. The protocol header comes first, in `accept`. Then the route
//! (`no_route`), the method (`bad_method`), the `Upgrade` header of an
//! Upgrade route (`upgrade_required`), the body checks of `BodyCheck`, and
//! the request type (`not_valid`). The handler runs last.

use std::future::Future;
use std::path::Path;
use std::pin::Pin;
use std::sync::Arc;

use creche_wire::{
    Body, HOST_API_BODY_CAP, JsonRoute, ListError, RouteError, Serve, ServeUpgrade, UpgradeRoute,
};
use hyper::body::{Bytes, Incoming};
use hyper::header::{CONNECTION, HeaderValue, UPGRADE};
use hyper::upgrade::Upgraded;
use hyper::{HeaderMap, Method, Request, Response, StatusCode, Uri};
use hyper_util::rt::TokioIo;
use serde::Serialize;

use super::{BodyCheck, Reply, StopSignal, error_reply, json_reply, run_ready};
use crate::ServeError;

/// A handler set: one handler for each route of `route_table!`.
/// Implement it only with `routes!`.
pub trait Routes: Send + Sync + Sized + 'static {
    /// The route named `name`, or `None` when the table has no such route.
    fn find(name: &str) -> Option<Route<Self>>;
}

/// What the vmm runs on the byte stream of an Upgrade route after 101.
pub trait Relay: Send + 'static {
    /// Relays the stream until either end closes it.
    fn run(self, stream: TokioIo<Upgraded>) -> impl Future<Output = ()> + Send;
}

/// The answer of a route to one request.
type Answer<'a> = Pin<Box<dyn Future<Output = Reply> + Send + 'a>>;

/// One route of the handler set `H`. `routes!` makes it.
pub struct Route<H> {
    answer: for<'a> fn(&'a Router<H>, Request<Incoming>) -> Answer<'a>,
}

impl<H> std::fmt::Debug for Route<H> {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str("Route")
    }
}

impl<H: Routes> Route<H> {
    /// The JSON route `R`, served by `H`.
    #[doc(hidden)]
    pub fn json<R: JsonRoute>() -> Self
    where
        H: Serve<R>,
    {
        Self {
            answer: answer_json::<H, R>,
        }
    }

    /// The Upgrade route `R`, admitted by `H`.
    #[doc(hidden)]
    pub fn upgrade<R: UpgradeRoute>() -> Self
    where
        H: ServeUpgrade<R>,
        <H as ServeUpgrade<R>>::Relay: Relay,
    {
        Self {
            answer: answer_upgrade::<H, R>,
        }
    }
}

/// Implements `Routes` for the handler set `$handlers`. The expansion of
/// `route_table!` names each route with its concrete type, so a set that
/// misses a handler does not build (decision CH25):
///
/// ```compile_fail,E0277
/// use creche_wire::{CreateError, CreateRoute, CreateSession, Serve, SessionRow};
///
/// struct OnlyCreate;
///
/// impl Serve<CreateRoute> for OnlyCreate {
///     async fn serve(&self, _: CreateSession) -> Result<SessionRow, CreateError> {
///         Err(CreateError::Internal {})
///     }
/// }
///
/// creche_vmm::routes!(OnlyCreate);
/// ```
#[macro_export]
macro_rules! routes {
    ($handlers:ty) => {
        impl $crate::server::Routes for $handlers {
            fn find(name: &str) -> ::core::option::Option<$crate::server::Route<Self>> {
                macro_rules! each_route {
                    (json $route:path) => {
                        if name == <$route as $crate::server::wire::JsonRoute>::NAME {
                            return ::core::option::Option::Some($crate::server::Route::json::<
                                $route,
                            >());
                        }
                    };
                    (upgrade $route:path) => {
                        if name == <$route as $crate::server::wire::UpgradeRoute>::NAME {
                            return ::core::option::Option::Some($crate::server::Route::upgrade::<
                                $route,
                            >());
                        }
                    };
                }
                $crate::server::wire::route_table!(each_route);
                ::core::option::Option::None
            }
        }
    };
}

/// Serves the route table on a new socket at `path` until `stop` fires.
/// `handlers` answers each request that passes every check.
pub async fn run_routes<H: Routes>(
    path: &Path,
    stop: StopSignal,
    handlers: H,
) -> Result<(), ServeError> {
    run_routes_ready(path, stop, handlers, || Ok(())).await
}

pub(crate) async fn run_routes_ready<H: Routes, R>(
    path: &Path,
    stop: StopSignal,
    handlers: H,
    ready: R,
) -> Result<(), ServeError>
where
    R: FnOnce() -> std::io::Result<()> + Send,
{
    let router = Arc::new(Router::new(handlers)?);
    let answer = move |request| {
        let router = Arc::clone(&router);
        async move { router.answer(request).await }
    };
    run_ready(path, stop, ready, answer).await
}

/// The handler set and the replies of the shared codes, made once at the
/// start of the server (decision CH26).
struct Router<H> {
    handlers: H,
    check: BodyCheck,
    no_route: Reply,
    bad_method: Reply,
    not_valid: Reply,
    internal: Reply,
}

impl<H: Routes> Router<H> {
    fn new(handlers: H) -> Result<Self, ServeError> {
        Ok(Self {
            handlers,
            check: BodyCheck::new()?,
            no_route: error_reply(&ListError::NoRoute {})?,
            bad_method: error_reply(&ListError::BadMethod {})?,
            not_valid: error_reply(&ListError::NotValid {})?,
            internal: error_reply(&ListError::Internal {})?,
        })
    }

    /// The route and the method come before every other check, so a bad
    /// `Content-Type` on an unknown path gets `no_route` (decision CH33
    /// rule 13).
    async fn answer(&self, request: Request<Incoming>) -> Reply {
        let Some(route) = name(request.uri()).and_then(H::find) else {
            return self.no_route.clone();
        };
        if request.method() != Method::POST {
            return self.bad_method.clone();
        }
        (route.answer)(self, request).await
    }

    /// The request of the route, after the body checks. A body that is
    /// strict JSON but not of type `T` gets `not_valid`.
    async fn request<T: Body>(&self, request: Request<Incoming>) -> Result<T, Reply> {
        let body = self.check.read(request).await?;
        serde_json::from_slice(&body).map_err(|_| self.not_valid.clone())
    }

    /// A reply with `value` as its whole body. A value that fails to
    /// serialize gets `internal`, never a cut body.
    // Decision CH40 rule 8: a reply over HOST_API_BODY_CAP (decisions
    // CH33 rule 2, CH34 rule 3) gets `internal` 500, as a failed
    // serialize does.
    fn reply<T: Serialize>(&self, status: StatusCode, value: &T) -> Reply {
        match serde_json::to_vec(value) {
            Ok(body) if body.len() <= HOST_API_BODY_CAP => json_reply(status, Bytes::from(body)),
            _ => self.internal.clone(),
        }
    }

    /// The reply of a route error, with its status (decision CH33 rule 13).
    fn error<E: RouteError>(&self, error: &E) -> Reply {
        match StatusCode::from_u16(error.status()) {
            Ok(status) => self.reply(status, error),
            Err(_) => self.internal.clone(),
        }
    }

    /// `upgrade_required` 426, with `Upgrade: TOKEN` (decisions CH26 rule
    /// 3, CH34 rule 6, RFC 9110 section 15.5.22).
    fn upgrade_required<R: UpgradeRoute>(&self) -> Reply {
        let mut reply = self.error(&R::UPGRADE_REQUIRED);
        match upgrade_headers(reply.headers_mut(), R::TOKEN) {
            Some(()) => reply,
            None => self.internal.clone(),
        }
    }
}

/// The route name of `uri`: its path after the first slash.
// Decision CH40 rule 9: a query, as in `/create?x=1`, names no route, so
// it gets `no_route`. Every route is `POST /<name>` (decision CH25).
fn name(uri: &Uri) -> Option<&str> {
    if uri.query().is_some() {
        return None;
    }
    uri.path().strip_prefix('/')
}

/// Whether the request gives one `Upgrade` header with exactly `token`.
// Decision CH40 rule 10: only the exact token, given once and in that
// case, names it. A list such as `a, creche-acp`, another letter case or
// a header given twice gets `upgrade_required` 426 (decision CH26 rule 3).
fn names_token(headers: &HeaderMap, token: &str) -> bool {
    let mut values = headers.get_all(UPGRADE).iter();
    matches!(
        (values.next(), values.next()),
        (Some(value), None) if value.as_bytes() == token.as_bytes()
    )
}

/// Adds `Connection: upgrade` and `Upgrade: token`, or `None` when the
/// token is no header value.
fn upgrade_headers(headers: &mut HeaderMap, token: &str) -> Option<()> {
    headers.insert(UPGRADE, HeaderValue::from_str(token).ok()?);
    headers.insert(CONNECTION, HeaderValue::from_static("upgrade"));
    Some(())
}

fn answer_json<H, R>(router: &Router<H>, request: Request<Incoming>) -> Answer<'_>
where
    H: Routes + Serve<R>,
    R: JsonRoute,
{
    Box::pin(async move {
        let request = match router.request::<R::Request>(request).await {
            Ok(request) => request,
            Err(reply) => return reply,
        };
        match <H as Serve<R>>::serve(&router.handlers, request).await {
            Ok(reply) => router.reply(StatusCode::OK, &reply),
            Err(error) => router.error(&error),
        }
    })
}

/// Every refusal comes before 101 (decision CH33 rule 6). After 101 the
/// relay owns the stream, and no JSON cap binds it (decision CH34 rule 3).
fn answer_upgrade<H, R>(router: &Router<H>, mut request: Request<Incoming>) -> Answer<'_>
where
    H: Routes + ServeUpgrade<R>,
    R: UpgradeRoute,
    <H as ServeUpgrade<R>>::Relay: Relay,
{
    Box::pin(async move {
        if !names_token(request.headers(), R::TOKEN) {
            return router.upgrade_required::<R>();
        }
        let upgrade = hyper::upgrade::on(&mut request);
        let request = match router.request::<R::Request>(request).await {
            Ok(request) => request,
            Err(reply) => return reply,
        };
        let relay = match <H as ServeUpgrade<R>>::admit(&router.handlers, request).await {
            Ok(relay) => relay,
            Err(error) => return router.error(&error),
        };
        let mut reply = Response::new(http_body_util::Full::new(Bytes::new()));
        *reply.status_mut() = StatusCode::SWITCHING_PROTOCOLS;
        if upgrade_headers(reply.headers_mut(), R::TOKEN).is_none() {
            return router.internal.clone();
        }
        tokio::spawn(async move {
            // A client that leaves before the switch ends the relay unrun.
            if let Ok(upgraded) = upgrade.await {
                relay.run(TokioIo::new(upgraded)).await;
            }
        });
        reply
    })
}
