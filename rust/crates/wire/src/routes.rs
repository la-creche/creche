//! The route table of the vmm host API (decision CH25). Each route is
//! `POST /<name>` with a JSON body, over the socket of decision CH20. The
//! table pairs each route with its request type and its reply type, so the
//! vmm router and the generated clients cannot pair them differently.
//!
//! The traits carry serde bounds only. The `JsonSchema` bounds stay on the
//! test-only schema generator (decision CH9-1), so a crate that depends on
//! this one never needs schemars.

use std::fmt;
use std::future::Future;
use std::time::Duration;

use serde::de::value::MapAccessDeserializer;
use serde::de::{DeserializeOwned, MapAccess, Visitor};
use serde::{Deserialize, Deserializer, Serialize, Serializer};

use super::{
    CreateSession, DropTarget, Dropped, ListSessions, OpenAcp, SessionPage, SessionRow, object_only,
};

/// The number of the host API protocol (decision CH25 rule 1). A client
/// sends it in the header `HOST_API_HEADER`, and the vmm checks it before
/// it reads the body. The match is exact. It is separate from the guest
/// init version of decision CH7-6.
///
/// A change of the schema subset that the routes reach needs a new number
/// (decision CH33 rule 8). `schema/host-api-<N>.json` holds a frozen copy
/// of that subset for the number N. The schema test fails when the subset
/// differs from the file of this number, and it never writes over that
/// file. Raise the number by one, and the test writes a new file.
pub const HOST_API_PROTOCOL: u32 = 1;

/// The name of the request header that carries `HOST_API_PROTOCOL`, as
/// decimal text. It is lowercase, the form of an HTTP/1.1 header name that
/// hyper takes as a constant.
// The vmm answers `ProtocolMismatch` to each value that is not the exact
// decimal text of HOST_API_PROTOCOL, such as "01", " 1" or "x", and to a
// header given twice (decision CH34 rule 1). CH33 rule 1 names only a
// missing header and another protocol number.
pub const HOST_API_HEADER: &str = "creche-host-api-protocol";

/// The most bytes of a request body and of a reply body of each route
/// (decisions CH26, CH33 rule 2): 1 MiB. The vmm refuses a larger request
/// with `too_large`. A client passes this cap to `read` for each reply. A
/// body of exactly this size is under the cap.
// The cap binds each JSON body of the host API (a request, a reply, an
// error), and no ACP byte that an Upgrade route carries after 101
// (decision CH34 rule 3). CH33 rule 2 caps each reply, and CH26 caps each
// request.
pub const HOST_API_BODY_CAP: usize = 1 << 20;

/// The HTTP status of `ProtocolMismatch` (decision CH33 rule 1). No code of
/// a route error has it, so a client of any protocol knows the mismatch
/// from the status alone.
///
/// After a mismatch, the client retries (decision CH33 rule 10). The rule
/// lives in the client, and `wire/AGENTS.md` states it.
// CH33 rule 10 says attendance tries 3 times 2 s apart, then records the
// fault vmm_protocol_mismatch. The 3 tries are 3 requests in all, the
// first one included, so the fault comes 4 s after the first mismatch
// (decision CH34 rule 2). The rule names attendance only. caregiver keeps
// CH25 rule 1 (retry a few times, then report a named fault), with no
// fixed numbers.
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
    /// How long a client waits for the reply, when the route fixes it. The
    /// schema test writes it into `x-routes` in whole seconds, so each
    /// client reads the one value.
    const CLIENT_TIMEOUT: Option<Duration> = None;
}

/// A route that takes a JSON request and, on success, turns the connection
/// into a raw byte stream (decisions CH20, CH25). Its path is `/` and
/// `NAME`. The client sends `Connection: Upgrade` and `Upgrade: TOKEN`. The
/// vmm answers `101 Switching Protocols` with no JSON body, or a typed
/// error before 101.
pub trait UpgradeRoute {
    /// The name of the route, in `snake_case`.
    const NAME: &'static str;
    /// The protocol name of the `Upgrade` header (decision CH26 rule 3).
    const TOKEN: &'static str;
    /// What the client sends.
    type Request: Body;
    /// What the vmm answers on a refusal, before 101.
    type Error: RouteError;
    /// The error of a request whose `Upgrade` header does not name `TOKEN`
    /// (decisions CH26 rule 3, CH34 rule 6). The vmm sends it with
    /// `Upgrade: TOKEN`.
    const UPGRADE_REQUIRED: Self::Error;
}

/// The vmm handler of the JSON route `R` (decision CH26 rule 4). The vmm
/// router expands `route_table!` with the concrete type of each route, so
/// a handler set that misses a route does not build (decision CH25).
pub trait Serve<R: JsonRoute> {
    /// The reply to one request that passed every check of the router.
    fn serve(&self, request: R::Request)
    -> impl Future<Output = Result<R::Reply, R::Error>> + Send;
}

/// The vmm handler of the Upgrade route `R` (decision CH26 rule 4). It
/// admits or refuses a request before 101: every refusal is a typed error
/// (decision CH33 rule 6).
pub trait ServeUpgrade<R: UpgradeRoute> {
    /// What runs on the byte stream after 101. The vmm owns the stream
    /// and its IO, so this crate needs no IO trait.
    type Relay: Send + 'static;

    /// The relay of an admitted request, or the refusal.
    fn admit(
        &self,
        request: R::Request,
    ) -> impl Future<Output = Result<Self::Relay, R::Error>> + Send;
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

/// The route `list`: a client reads one page of the host session list of a
/// family (decisions CH21-5, CH21-7). The family key comes from the list
/// entry of the route table plan (see `ListSessions`). The vmm needs no VM
/// boot.
#[derive(Debug)]
pub enum ListRoute {}

impl JsonRoute for ListRoute {
    const NAME: &'static str = "list";
    type Request = ListSessions;
    type Reply = SessionPage;
    type Error = ListError;
}

route_error! {
    /// The error of the route `list`. It holds the shared codes only: a
    /// family with no rows gives an empty page, and any cursor reads.
    pub enum ListError {}
}

/// The route `drop`: a client drops one session or every session of a
/// family (decisions CH21-2, CH21-10, CH27). It cannot be undone.
///
/// The vmm first stops each open ACP relay of the target (decision CH26
/// rule 5). It then stops every VM of the target at once (decision CH33
/// rule 11), each with the graceful stop of decision CH19 (3): it waits up
/// to `DROP_STOP_WAIT`, sends SIGTERM, waits up to `DROP_SIGTERM_GRACE`,
/// then sends SIGKILL. A kill is no error: the drop goes on. Last it
/// removes the snapshot, the session disk and the row of each session, and
/// answers `Dropped`. A client waits up to `DROP_CLIENT_TIMEOUT` for the
/// reply, and retries a drop that timed out. A retry of a dropped session
/// gets `not_found` (decision CH33 rule 4), and the client counts it as
/// done.
#[derive(Debug)]
pub enum DropRoute {}

impl JsonRoute for DropRoute {
    const NAME: &'static str = "drop";
    type Request = DropTarget;
    type Reply = Dropped;
    type Error = DropError;
    const CLIENT_TIMEOUT: Option<Duration> = Some(DROP_CLIENT_TIMEOUT);
}

/// How long the vmm waits for a VM to power off after the `stop` of
/// decision CH19 (3), before it sends SIGTERM. Guest init gives a running
/// turn up to 30 s.
pub const DROP_STOP_WAIT: Duration = Duration::from_secs(30);

/// How long the vmm waits after SIGTERM before it sends SIGKILL to a VM of
/// a drop (decision CH33 rule 11).
// The vmm waits 30 s for the graceful stop of CH19 (3), then sends
// SIGTERM and waits 5 s, then sends SIGKILL (decision CH34 rule 5). So the
// stop of each VM takes 35 s at most, as CH33 rule 11 asks. The removal of
// disks and rows comes on top. A client waits 60 s for the reply, and
// retries a timeout as a transient.
pub const DROP_SIGTERM_GRACE: Duration = Duration::from_secs(5);

/// How long a client waits for the reply of a drop before it retries it
/// (decision CH33 rule 11). It is above `DROP_STOP_WAIT` and
/// `DROP_SIGTERM_GRACE` together, with room for the removal. caregiver and
/// attendance are clients, so `x-routes` holds it as `client_timeout_s` of
/// the route `drop`, and `routes.py` holds it as `DROP_CLIENT_TIMEOUT_S`.
pub const DROP_CLIENT_TIMEOUT: Duration = Duration::from_secs(60);

const _: () = assert!(
    DROP_STOP_WAIT.as_secs() + DROP_SIGTERM_GRACE.as_secs() <= 35
        && DROP_CLIENT_TIMEOUT.as_secs() > 35,
    "a drop stops each VM in 35 s at most, and a client waits longer",
);

route_error! {
    /// The error of the route `drop`.
    pub enum DropError {
        /// No row has the id of a `SessionDrop`, or the row belongs to
        /// another family (decision CH33 rules 4 and 5). A drop of a family
        /// never gives it.
        NotFound = "not_found" => 404,
    }
}

/// The route `acp`: attendance opens the ACP stream of one session
/// (decisions CH20, CH21-7). After 101 the connection carries raw ACP
/// bytes, which the vmm relays to guest port 1201 (decision CH13).
///
/// The vmm answers 101 only after `ControlReady` (decision CH33 rule 6).
/// When the VM of the session does not run, the vmm boots or restores it
/// first, so the 101 can wait for a boot. A start fault is a typed error
/// before 101. After 101 each failure is the end of the stream, and the
/// client learns the cause from the error of its next Upgrade.
// The vmm checks the protocol header, the route and the method, then the
// Upgrade header (upgrade_required), then the body (Content-Type, cap,
// JSON, type), then the session (not_found, busy), then admission
// (host_full), then the start of the VM (decision CH34 rule 7). The 101
// follows the first ControlReady of the start, whatever FacadeState it
// reports (decision CH34 rule 7). CH33 rule 6 puts every refusal of acp
// before 101.
#[derive(Debug)]
pub enum AcpRoute {}

impl UpgradeRoute for AcpRoute {
    const NAME: &'static str = "acp";
    const TOKEN: &'static str = "creche-acp";
    type Request = OpenAcp;
    type Error = AcpError;
    const UPGRADE_REQUIRED: AcpError = AcpError::UpgradeRequired {};
}

route_error! {
    /// The error of the route `acp`. The vmm sends each code before 101.
    // The codes and statuses follow decision CH34 rule 6: upgrade_required
    // 426, not_found 404, busy 409, host_full 503, and the start faults
    // sandbox_start_failed and egress_assert_failed (the fault names of
    // CH6-3), each 502. An Upgrade that comes while a drop of its session
    // runs gets not_found (decision CH34 rule 6).
    pub enum AcpError {
        /// The request has no `Upgrade` header, or the header does not
        /// name `AcpRoute::TOKEN` (decision CH26 rule 3). The vmm also
        /// sends `Upgrade: creche-acp` with this error (RFC 9110).
        UpgradeRequired = "upgrade_required" => 426,
        /// No row has the id, the row belongs to another family, or a drop
        /// of the session runs (decisions CH21-9, CH33 rule 5).
        NotFound = "not_found" => 404,
        /// The session has an open ACP stream (decision CH33 rule 7).
        /// attendance opens one stream per session.
        Busy = "busy" => 409,
        /// The VM does not fit on the host, and no idle VM can make room
        /// (decisions CH8-6, CH21-3, CH21-9). The vmm never queues: the
        /// client retries.
        HostFull = "host_full" => 503,
        /// The VM did not boot or restore, or guest init sent no
        /// `ControlReady` (decisions CH6-3, CH33 rule 6). The vmm also
        /// writes the fault to its fault file (decision CH21-1).
        SandboxStartFailed = "sandbox_start_failed" => 502,
        /// The VM failed the deny-by-default check of its start (decisions
        /// CH6-3, CH33 rule 6). The vmm also writes the fault to its fault
        /// file (decision CH21-1).
        EgressAssertFailed = "egress_assert_failed" => 502,
    }
}

/// What the tests read of each route of the table, of either kind: its
/// name and its error.
#[cfg(test)]
pub(crate) trait TableRoute {
    /// The `NAME` of the route.
    const NAME: &'static str;
    /// The `Error` of the route.
    type Error: RouteError + schemars::JsonSchema;
}

/// Implement `TableRoute` for one route of `route_table!`.
#[cfg(test)]
macro_rules! table_route {
    (json $route:path) => {
        impl TableRoute for $route {
            const NAME: &'static str = <$route as JsonRoute>::NAME;
            type Error = <$route as JsonRoute>::Error;
        }
    };
    (upgrade $route:path) => {
        impl TableRoute for $route {
            const NAME: &'static str = <$route as UpgradeRoute>::NAME;
            type Error = <$route as UpgradeRoute>::Error;
        }
    };
}

#[cfg(test)]
crate::route_table!(table_route);

/// The one route table (decision CH25). The test-only schema generator and
/// the vmm router both expand it, so no second list of routes exists.
///
/// `$each` is a macro that the caller defines. The table calls it once per
/// route, with the kind of the route (`json` or `upgrade`) and the path of
/// its marker type, as in `$each!(json $crate::CreateRoute)`. The caller expands each route
/// with its concrete type, so a bound that one route does not meet fails
/// to compile.
#[macro_export]
macro_rules! route_table {
    ($each:ident) => {
        $each!(json $crate::CreateRoute);
        $each!(json $crate::ListRoute);
        $each!(json $crate::DropRoute);
        $each!(upgrade $crate::AcpRoute);
    };
}
