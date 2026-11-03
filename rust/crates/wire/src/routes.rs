//! The route table of the vmm host API (decision CH25). Each route is
//! `POST /<name>` with a JSON body, over the socket of decision CH20. The
//! table pairs each route with its request type and its reply type, so the
//! vmm router and the generated clients cannot pair them differently.
//!
//! The traits carry serde bounds only. The `JsonSchema` bounds stay on the
//! test-only schema generator (decision CH9-1), so a crate that depends on
//! this one never needs schemars.

use std::fmt;

use serde::de::value::MapAccessDeserializer;
use serde::de::{DeserializeOwned, MapAccess, Visitor};
use serde::{Deserialize, Deserializer, Serialize, Serializer};

use super::{CreateSession, SessionRow, object_only};

/// The number of the host API protocol (decision CH25 rule 1). A client
/// sends it in the header `HOST_API_HEADER`, and the vmm checks it before
/// it reads the body. The match is exact. It is separate from the guest
/// init version of decision CH7-6.
// Decision CH33 rule 8 says which change needs a new number. The repo
// holds a frozen copy of the schema subset that the routes reach, one per
// number. CI fails when a change moves that subset and keeps the number.
// A later PR adds that check (PR 10 of the route table plan).
pub const HOST_API_PROTOCOL: u32 = 1;

/// The name of the request header that carries `HOST_API_PROTOCOL`, as
/// decimal text. It is lowercase, the form of an HTTP/1.1 header name that
/// hyper takes as a constant.
pub const HOST_API_HEADER: &str = "creche-host-api-protocol";

/// A JSON body of a route: a request or a reply.
pub trait Body: Serialize + DeserializeOwned {}

impl<T: Serialize + DeserializeOwned> Body for T {}

/// The typed error of a route (decision CH26 rule 4). A caller matches it
/// on its `code` (decision CH25 rule 3). Declare one only with
/// `route_error!`.
pub trait RouteError: Body {
    /// The `code` and the HTTP status of each variant, in the order of the
    /// enum (decision CH33).
    const STATUSES: &'static [(&'static str, u16)];

    /// The HTTP status of this error.
    fn status(&self) -> u16;
}

/// Declare the error of a route, as `Name = "code" => status` per variant.
///
/// The enum is internally tagged on `code`, so the wire form of a variant
/// is `{"code":"<code>"}`. A variant carries nothing, so it is written
/// `V {}` (decision CH22 rule 3). No variant has a schema title: a caller
/// matches the code, not a generated class (decision CH25 rule 3).
/// `object_only!` refuses the array form that a derived reader takes. The
/// one list writes the serde names, `STATUSES` and `status`. A status
/// outside 400 to 599 fails to compile.
macro_rules! route_error {
    (
        $(#[$meta:meta])*
        pub enum $name:ident {
            $(
                $(#[$variant_meta:meta])*
                $variant:ident = $code:literal => $status:literal,
            )+
        }
    ) => {
        $(#[$meta])*
        #[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
        #[serde(remote = "Self", tag = "code", deny_unknown_fields)]
        #[cfg_attr(test, derive(schemars::JsonSchema))]
        pub enum $name {
            $(
                $(#[$variant_meta])*
                #[serde(rename = $code)]
                $variant {},
            )+
        }

        object_only!($name);

        $(
            const _: () = assert!(
                $status >= 400 && $status <= 599,
                concat!("the status of ", $code, " is not 4xx or 5xx"),
            );
        )+

        impl RouteError for $name {
            const STATUSES: &'static [(&'static str, u16)] = &[$(($code, $status)),+];

            fn status(&self) -> u16 {
                match self {
                    $(Self::$variant {} => $status,)+
                }
            }
        }
    };
}

/// A route that takes a JSON request and gives a JSON reply. Its path is
/// `/` and `NAME`.
pub trait JsonRoute {
    /// The name of the route, in `snake_case`.
    const NAME: &'static str;
    /// What the client sends.
    type Request: Body;
    /// What the vmm answers on success.
    type Reply: Body;
    /// What the vmm answers on a refusal.
    type Error: RouteError;
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
    type Error = CreateError;
}

route_error! {
    /// The error of the route `create`.
    pub enum CreateError {
        /// A row with this id exists, and it was made from a different
        /// request (decisions CH25 rule 2, CH33 rule 12). Two requests
        /// differ when their typed values differ, not their bytes.
        Conflict = "conflict" => 409,
        /// The family has no `config/` dir, so the vmm refuses the create
        /// (decision CH33 rule 3). caregiver removes the family files
        /// before it drops the family.
        UnknownFamily = "unknown_family" => 404,
    }
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
