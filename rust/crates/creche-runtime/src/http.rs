//! HTTP/1.1 over a Unix socket or TCP: the client, the listeners and the edge
//! layer of a router.
//!
//! | Module | What it holds |
//! |---|---|
//! | [`client`] | One request to a service on the same host or on the LAN, with a limit for each phase. |
//! | [`server`] | The bind of a Unix socket or of a TCP address, and the serve loop. |
//! | [`layers`] | What each router of a service needs: an answer for each refusal that the framework makes, and a task for each request. |
//!
//! The workspace takes no TLS crate. A service binds the LAN address or a
//! Unix socket, and the client refuses an `https` URL.

pub mod client;
pub mod layers;
pub mod server;
