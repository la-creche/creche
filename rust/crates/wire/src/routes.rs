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
// CONTRACT-QUESTION: CH33 rule 1 names a missing header and another
// protocol number. It does not name a value that is no number, such as
// "01", " 1" or "x", or the header given twice. The reading taken: the vmm
// answers `ProtocolMismatch` to each value that is not the exact decimal
// text of HOST_API_PROTOCOL, and to a header given twice.
pub const HOST_API_HEADER: &str = "creche-host-api-protocol";

/// The HTTP status of `ProtocolMismatch` (decision CH33 rule 1). No code of
/// a route error has it, so a client of any protocol knows the mismatch
/// from the status alone.
///
/// After a mismatch, the client retries (decision CH33 rule 10). The rule
/// lives in the client, and `wire/AGENTS.md` states it.
// CONTRACT-QUESTION: CH33 rule 10 says attendance tries 3 times 2 s apart,
// then records the fault vmm_protocol_mismatch. The reading taken: 3
// requests in all, the first one included, so the fault comes 4 s after
// the first mismatch. The rule names attendance only. caregiver keeps the
// rule of CH25 rule 1 (retry a few times, then report a named fault), and
// no decision gives it numbers.
pub const MISMATCH_STATUS: u16 = 412;

/// The one reply of the vmm when the protocol header of a request is
/// missing or names another protocol number (decisions CH25 rule 1, CH33
/// rule 1). The vmm checks the header before the route, so each route and
/// each path gets this reply. The wire form is
/// `{"code":"protocol_mismatch","protocol":<n>}`, with the status
/// `MISMATCH_STATUS`.
///
/// The reply is frozen for all protocol numbers: a new protocol number
/// never changes its body or its status. A client of an older protocol
/// must still read it. A test pins the bytes and the status.
///
/// It is in no route error. A client checks the status `MISMATCH_STATUS`
/// before it reads a route error.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(remote = "Self", deny_unknown_fields)]
#[cfg_attr(test, derive(schemars::JsonSchema))]
pub struct ProtocolMismatch {
    /// Always `protocol_mismatch`.
    code: MismatchCode,
    /// The host API protocol of the vmm that answers: its
    /// `HOST_API_PROTOCOL`.
    pub protocol: u32,
}

object_only!(ProtocolMismatch);

impl ProtocolMismatch {
    /// The reply of a vmm of the protocol `protocol`. The vmm gives its own
    /// `HOST_API_PROTOCOL`.
    pub const fn new(protocol: u32) -> Self {
        Self {
            code: MismatchCode,
            protocol,
        }
    }
}

/// The `code` of `ProtocolMismatch`: always the string `protocol_mismatch`.
/// Its schema is a `const`, as the `code` of a route error is, so the
/// Python model reads it as a `Literal`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
struct MismatchCode;

/// The text of `MismatchCode`.
const MISMATCH_CODE: &str = "protocol_mismatch";

impl Serialize for MismatchCode {
    fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        serializer.serialize_str(MISMATCH_CODE)
    }
}

impl<'de> Deserialize<'de> for MismatchCode {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        let code = String::deserialize(deserializer)?;
        if code == MISMATCH_CODE {
            Ok(Self)
        } else {
            Err(serde::de::Error::custom(
                "the code is not protocol_mismatch",
            ))
        }
    }
}

#[cfg(test)]
impl schemars::JsonSchema for MismatchCode {
    fn inline_schema() -> bool {
        true
    }

    fn schema_name() -> std::borrow::Cow<'static, str> {
        "MismatchCode".into()
    }

    fn json_schema(_: &mut schemars::SchemaGenerator) -> schemars::Schema {
        schemars::json_schema!({ "const": MISMATCH_CODE, "type": "string" })
    }
}

/// A JSON body of a route: a request or a reply.
pub trait Body: Serialize + DeserializeOwned {}

impl<T: Serialize + DeserializeOwned> Body for T {}

/// The typed error of a route (decision CH26 rule 4). A caller matches it
/// on its `code` (decision CH25 rule 3). Declare one only with
/// `route_error!`.
pub trait RouteError: Body {
    /// The `code` and the HTTP status of each variant, in the order of the
    /// enum (decision CH33). The codes of the route come first, then
    /// `SHARED_CODES`.
    const STATUSES: &'static [(&'static str, u16)];

    /// The HTTP status of this error.
    fn status(&self) -> u16;
}

/// Call `$then!` with the codes that every route shares (decision CH33
/// rule 13), as `Name = "code" => status` per variant. The list is in the
/// order in which the vmm checks a request.
///
/// Decision CH26 orders the checks after the protocol header: the
/// `Content-Type`, then the body cap, then the JSON. A request must first
/// reach a route, so `no_route` and `bad_method` come before them. The
/// JSON check gives `not_json`, and the check of the request type gives
/// `not_valid`. `internal` is a fault of the vmm after every check.
///
/// The protocol mismatch reply is in no route error (decision CH25 rule 1).
// The order is the check order of decision CH33 rule 13. A change of the
// order moves the oneOf order and the class names CreateErrorN.
macro_rules! shared_codes {
    ($then:ident! { $($head:tt)* }) => {
        $then! {
            $($head)*
            [
                /// No route has this path.
                NoRoute = "no_route" => 404,
                /// The route has this path, but the method is not `POST`.
                BadMethod = "bad_method" => 405,
                /// The `Content-Type` is not `application/json`.
                BadContentType = "bad_content_type" => 415,
                /// The body is over the cap.
                TooLarge = "too_large" => 413,
                /// The body is not strict JSON.
                NotJson = "not_json" => 400,
                /// The body is JSON, but not the request type of the route.
                NotValid = "not_valid" => 400,
                /// The vmm failed after it took the request. The vmm logs
                /// the details.
                Internal = "internal" => 500,
            ]
        }
    };
}

/// Write `SHARED_CODES` from the list of `shared_codes!`.
macro_rules! shared_list {
    ([$($(#[$meta:meta])* $variant:ident = $code:literal => $status:literal,)+]) => {
        /// The `code` and the HTTP status of each code that every route
        /// shares (decision CH33 rule 13), in the order in which the vmm
        /// checks a request. `route_error!` appends these codes to each
        /// route error, after the codes of the route.
        pub const SHARED_CODES: &[(&str, u16)] = &[$(($code, $status)),+];
    };
}

shared_codes!(shared_list! {});

/// Declare the error of a route, as `Name = "code" => status` per variant.
/// List only the codes of the route. The macro appends the codes of
/// `shared_codes!`, so no route can miss one (decision CH33 rule 13).
///
/// The enum is internally tagged on `code`, so the wire form of a variant
/// is `{"code":"<code>"}`. A variant carries nothing, so it is written
/// `V {}` (decision CH22 rule 3), and no error body holds free text
/// (decision CH33 rule 13). No variant has a schema title: a caller
/// matches the code, not a generated class (decision CH25 rule 3).
/// `object_only!` refuses the array form that a derived reader takes. The
/// one list writes the serde names, `STATUSES` and `status`. A status
/// outside 400 to 599 fails to compile.
macro_rules! route_error {
    (
        $(#[$meta:meta])*
        pub enum $name:ident {
            $($route:tt)*
        }
    ) => {
        shared_codes!(route_error! { @all $(#[$meta])* $name [$($route)*] });
    };
    (
        @all
        $(#[$meta:meta])*
        $name:ident
        [$(
            $(#[$variant_meta:meta])*
            $variant:ident = $code:literal => $status:literal,
        )*]
        [$(
            $(#[$shared_meta:meta])*
            $shared:ident = $shared_code:literal => $shared_status:literal,
        )+]
    ) => {
        route_error! {
            @enum
            $(#[$meta])*
            $name
            $(
                $(#[$variant_meta])*
                $variant = $code => $status,
            )*
            $(
                $(#[$shared_meta])*
                $shared = $shared_code => $shared_status,
            )+
        }
    };
    (
        @enum
        $(#[$meta:meta])*
        $name:ident
        $(
            $(#[$variant_meta:meta])*
            $variant:ident = $code:literal => $status:literal,
        )+
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
