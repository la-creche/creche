//! One request through a router, with no socket.
//!
//! A test of a handler needs no listener and no client: [`call`] gives one
//! request to the router and returns the answer.
//!
//! The function has no Python origin. A Python test of a route uses the test
//! client of the web framework, for example `TestClient` in
//! `noticeboard/tests/test_noticeboard_app.py:16`.

use axum::Router;
use axum::body::Body;
use axum::response::Response;
use http::Request;
use tower::ServiceExt;

/// Gives `request` to `router` and returns its answer.
///
/// A test of a handler needs no listener and no client for that.
///
/// The function adds no layer. A handler that panics thus makes the test
/// panic, unless the test gives a router with the edge layer of
/// `creche-runtime`. The answer to a path with no route is the answer of
/// `axum`, unless the router has a fallback.
///
/// ```
/// use axum::Router;
/// use axum::body::Body;
/// use axum::routing::get;
/// use creche_testkit::router::call;
/// use http::{Request, StatusCode};
///
/// let router = Router::new().route("/healthz", get(|| async { "ok" }));
/// let runtime = tokio::runtime::Builder::new_current_thread().build()?;
///
/// let answer = runtime.block_on(async {
///     let request = Request::get("/healthz").body(Body::empty())?;
///     let response = call(router, request).await;
///     let status = response.status();
///     let body = axum::body::to_bytes(response.into_body(), 1024).await?;
///
///     Ok::<_, Box<dyn std::error::Error>>((status, body))
/// })?;
///
/// assert_eq!(answer.0, StatusCode::OK);
/// assert_eq!(&answer.1[..], b"ok");
/// # Ok::<(), Box<dyn std::error::Error>>(())
/// ```
pub async fn call(router: Router, request: Request<Body>) -> Response {
    match router.oneshot(request).await {
        Ok(response) => response,
        // The error type of a router has no value.
        Err(never) => match never {},
    }
}

#[cfg(test)]
mod tests {
    use std::future::Future;

    use axum::body::Bytes;
    use axum::http::HeaderMap;
    use axum::routing::{get, post};
    use http::{Method, StatusCode};

    use super::*;

    /// The most bytes that a test reads from the body of an answer.
    const BODY_MAX: usize = 4096;

    fn block_on<F: Future>(future: F) -> F::Output {
        tokio::runtime::Builder::new_current_thread()
            .build()
            .unwrap()
            .block_on(future)
    }

    fn router() -> Router {
        Router::new()
            .route("/healthz", get(|| async { "ok" }))
            .route(
                "/echo",
                post(|headers: HeaderMap, body: Bytes| async move {
                    let caller = headers
                        .get("x-caller")
                        .and_then(|value| value.to_str().ok())
                        .unwrap_or("nobody")
                        .to_owned();

                    (StatusCode::CREATED, [("x-caller", caller)], body)
                }),
            )
    }

    fn request(method: Method, path: &str, body: &'static str) -> Request<Body> {
        Request::builder()
            .method(method)
            .uri(path)
            .header("x-caller", "door-owui")
            .body(Body::from(body))
            .unwrap()
    }

    async fn body_of(response: Response) -> Vec<u8> {
        axum::body::to_bytes(response.into_body(), BODY_MAX)
            .await
            .unwrap()
            .to_vec()
    }

    #[test]
    fn a_request_gets_the_answer_of_its_route() {
        block_on(async {
            let response = call(router(), request(Method::GET, "/healthz", "")).await;

            assert_eq!(response.status(), StatusCode::OK);
            assert_eq!(body_of(response).await, b"ok");
        });
    }

    #[test]
    fn the_handler_gets_the_headers_and_the_body_of_the_request() {
        block_on(async {
            let response = call(router(), request(Method::POST, "/echo", "{\"n\":1}")).await;

            assert_eq!(response.status(), StatusCode::CREATED);
            assert_eq!(response.headers().get("x-caller").unwrap(), "door-owui");
            assert_eq!(body_of(response).await, b"{\"n\":1}");
        });
    }

    #[test]
    fn a_path_with_no_route_gets_the_answer_of_the_router() {
        block_on(async {
            let unknown = call(router(), request(Method::GET, "/no/such/path", "")).await;
            let wrong_method = call(router(), request(Method::POST, "/healthz", "")).await;
            let with_fallback = call(
                router().fallback(|| async { (StatusCode::IM_A_TEAPOT, "no route") }),
                request(Method::GET, "/no/such/path", ""),
            )
            .await;

            assert_eq!(unknown.status(), StatusCode::NOT_FOUND);
            assert_eq!(wrong_method.status(), StatusCode::METHOD_NOT_ALLOWED);
            assert_eq!(with_fallback.status(), StatusCode::IM_A_TEAPOT);
            assert_eq!(body_of(with_fallback).await, b"no route");
        });
    }

    #[test]
    fn one_router_answers_two_calls_through_a_clone() {
        block_on(async {
            let router = router();
            let first = call(router.clone(), request(Method::GET, "/healthz", "")).await;
            let second = call(router, request(Method::GET, "/healthz", "")).await;

            assert_eq!(first.status(), StatusCode::OK);
            assert_eq!(second.status(), StatusCode::OK);
        });
    }
}
