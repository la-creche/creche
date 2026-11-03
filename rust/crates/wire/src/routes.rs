//! The route table of the vmm host API (decision CH25). Each route is
//! `POST /<name>` with a JSON body, over the socket of decision CH20. The
//! table pairs each route with its request type and its reply type, so the
//! vmm router and the generated clients cannot pair them differently.
//!
//! The traits carry serde bounds only. The `JsonSchema` bounds stay on the
//! test-only schema generator (decision CH9-1), so a crate that depends on
//! this one never needs schemars.

use serde::Serialize;
use serde::de::DeserializeOwned;

use super::{CreateSession, SessionRow};

/// The number of the host API protocol (decision CH25 rule 1). A client
/// sends it in the header `HOST_API_HEADER`, and the vmm checks it before
/// it reads the body. The match is exact. It is separate from the guest
/// init version of decision CH7-6.
// CONTRACT-QUESTION: CH25 rule 1 sets an exact match, but no rule says
// which change needs a new number (plan gap G8). The reading taken: the
// number stays 1 until the owner sets that rule. A later rule may add a
// CI check that a change of a reached type also moves the number.
pub const HOST_API_PROTOCOL: u32 = 1;

/// The name of the request header that carries `HOST_API_PROTOCOL`, as
/// decimal text. It is lowercase, the form of an HTTP/1.1 header name that
/// hyper takes as a constant.
pub const HOST_API_HEADER: &str = "creche-host-api-protocol";

/// A JSON body of a route: a request or a reply.
pub trait Body: Serialize + DeserializeOwned {}

impl<T: Serialize + DeserializeOwned> Body for T {}

/// A route that takes a JSON request and gives a JSON reply. Its path is
/// `/` and `NAME`.
pub trait JsonRoute {
    /// The name of the route, in `snake_case`.
    const NAME: &'static str;
    /// What the client sends.
    type Request: Body;
    /// What the vmm answers on success.
    type Reply: Body;
}

/// The route `create`: attendance makes a row of the host session list
/// (decisions CH21-4, CH25 rule 2). A repeat with the same request gives
/// the same row.
#[derive(Debug)]
pub enum CreateRoute {}

impl JsonRoute for CreateRoute {
    const NAME: &'static str = "create";
    type Request = CreateSession;
    type Reply = SessionRow;
}

/// The one route table (decision CH25). The test-only schema generator and
/// the vmm router both expand it, so no second list of routes exists.
///
/// `$each` is a macro that the caller defines. The table calls it once per
/// route, with the kind of the route and the path of its marker type, as
/// in `$each!(json $crate::CreateRoute)`. The caller expands each route
/// with its concrete type, so a bound that one route does not meet fails
/// to compile.
#[macro_export]
macro_rules! route_table {
    ($each:ident) => {
        $each!(json $crate::CreateRoute);
    };
}
