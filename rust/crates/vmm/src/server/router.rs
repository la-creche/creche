//! The router of the host API (decisions CH25, CH26). `routes!` expands
//! `route_table!` with the concrete type of each route, so a handler set
//! needs a handler for each route of the one table.
//!
//! The order of the checks follows decisions CH26 and CH33 rule 13. The
//! protocol header comes first, in `accept`. Then the route (`no_route`),
//! the method (`bad_method`), the body checks of `BodyCheck`, and the
//! request type (`not_valid`). The handler runs last.
//!
//! This router serves the JSON routes only. An Upgrade route gets
//! `no_route` until the next change adds `ServeUpgrade` and its path
//! (decisions CH26 rule 3, CH34 rule 7).

use std::future::Future;
use std::path::Path;
use std::pin::Pin;
use std::sync::Arc;

use creche_wire::{Body, HOST_API_BODY_CAP, JsonRoute, ListError, RouteError, Serve};
use hyper::body::{Bytes, Incoming};
use hyper::{Method, Request, StatusCode, Uri};
use serde::Serialize;

use super::{BodyCheck, Reply, StopSignal, error_reply, json_reply, run_with};
use crate::ServeError;

/// A handler set: one handler for each route of `route_table!`.
/// Implement it only with `routes!`.
pub trait Routes: Send + Sync + Sized + 'static {
    /// The route named `name`, or `None` when the table has no such route.
    fn find(name: &str) -> Option<Route<Self>>;
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
}

/// Implements `Routes` for the handler set `$handlers`. The expansion of
/// `route_table!` names each JSON route with its concrete type, so a set
/// that misses a handler does not build (decision CH25):
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
                    // An Upgrade route names no route yet: it gets
                    // `no_route` until the next change serves it.
                    (upgrade $route:path) => {};
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
    let router = Arc::new(Router::new(handlers)?);
    let answer = move |request| {
        let router = Arc::clone(&router);
        async move { router.answer(request).await }
    };
    run_with(path, stop, answer).await
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
