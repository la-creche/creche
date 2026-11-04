//! The JSON Schema of the wire types, and the check that holds the committed
//! file equal to it (decision CH9-1). The Python models are generated from
//! that file, so a wire type that changes without the file fails here.
//!
//! The same test writes `agent/src/wire.ts`, a TypeScript copy of the
//! schema, from which TypeBox gives the facade its static types and its
//! runtime checks (decision CH22).
//!
//! The schema also holds the route table under the root key `x-routes`
//! (decision CH25). A route type needs no `subschema_for` line of its own:
//! the route table reaches it. The same test writes the table as Python
//! constants in `wire/src/creche_wire/routes.py`, which datamodel-codegen
//! does not write.
//!
//! To write the files after a change of a wire type, run
//! `CRECHE_WIRE_SCHEMA=write cargo test -p creche-wire` in rust/.

use std::collections::BTreeSet;
use std::fmt::Write as _;
use std::path::PathBuf;

use schemars::generate::SchemaSettings;
use schemars::transform::transform_subschemas;
use schemars::{JsonSchema, Schema, SchemaGenerator};
use serde_json::{Map, Value, json};

use super::{
    AutoJobId, ControlHello, ControlReady, DelegationId, FamilyConfig, FamilyStateReply,
    HOST_API_BODY_CAP, HOST_API_HEADER, HOST_API_PROTOCOL, JS_INTEGER_MAX, JobId, JsonRoute,
    MISMATCH_STATUS, ProtocolMismatch, RouteError, SHARED_CODES, SessionId, SessionOrigin,
    TurnEvent, UnixMillis,
};

/// The variable that makes the test write the file instead of checking it.
const WRITE_VAR: &str = "CRECHE_WIRE_SCHEMA";
const WRITE: &str = "write";

/// The committed file, relative to the crate.
const SCHEMA_FILE: &str = "schema/wire.json";

/// The committed TypeScript copy of the schema, relative to the crate.
const TYPESCRIPT_FILE: &str = "../../../agent/src/wire.ts";

/// The lines before the schema in the TypeScript copy.
const TYPESCRIPT_HEAD: &str = "\
// The JSON Schema of the wire types, a copy of rust/crates/wire/schema/wire.json
// (decision CH22). The schema test of rust/crates/wire writes this file. Never
// edit it: run `CRECHE_WIRE_SCHEMA=write cargo test -p creche-wire` in rust/.

export const wire = ";

/// The committed Python constants of the route table, relative to the crate.
const PYTHON_FILE: &str = "../../../wire/src/creche_wire/routes.py";

/// The lines before the imports in the Python constants.
const PYTHON_HEAD: &str = "\
# Generated from the route table of rust/crates/wire (decision CH25). Do not edit.
# To write it again, run `CRECHE_WIRE_SCHEMA=write cargo test -p creche-wire` in rust/.
\"\"\"The routes of the vmm host API, each with its request, reply and error
types (decision CH25). A client sends `HOST_API_PROTOCOL` in the header
`HOST_API_HEADER`.\"\"\"

";

/// The keys of a route entry that name a `$defs` entry.
const ROUTE_TYPES: [&str; 3] = ["request", "reply", "error"];

/// The root key of the route table in the schema.
const ROUTES_KEY: &str = "x-routes";

/// The schema of every wire type, as one document with a `$defs` entry per
/// type, and the route table under `x-routes`. The contract is the one of a
/// reader: what a reader accepts.
pub(super) fn wire_schema() -> Value {
    let mut generator = SchemaSettings::draft2020_12()
        .for_deserialize()
        .with_transform(bound_integers)
        .into_generator();
    let _ = generator.subschema_for::<SessionId>();
    let _ = generator.subschema_for::<JobId>();
    let _ = generator.subschema_for::<AutoJobId>();
    let _ = generator.subschema_for::<DelegationId>();
    let _ = generator.subschema_for::<SessionOrigin>();
    let _ = generator.subschema_for::<UnixMillis>();
    let _ = generator.subschema_for::<TurnEvent>();
    let _ = generator.subschema_for::<FamilyConfig>();
    let _ = generator.subschema_for::<FamilyStateReply>();
    let _ = generator.subschema_for::<ControlHello>();
    let _ = generator.subschema_for::<ControlReady>();
    let routes = route_table(&mut generator);

    json!({
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$defs": generator.take_definitions(true),
        ROUTES_KEY: routes,
    })
}

/// The `x-routes` value: the host API protocol number, its header, the body
/// cap, the mismatch reply with its status, and one entry per route of
/// `route_table!`, keyed by the route name. Each type is
/// a plain name of a `$defs` entry, not a `$ref`, so datamodel-codegen and
/// TypeBox do not read it as a schema.
fn route_table(generator: &mut SchemaGenerator) -> Value {
    let mut routes = Map::new();
    macro_rules! describe {
        (json $route:path) => {
            let entry = json_route::<$route>(generator);
            routes.insert(<$route as JsonRoute>::NAME.to_owned(), entry);
        };
    }
    crate::route_table!(describe);
    let _ = generator.subschema_for::<ProtocolMismatch>();

    json!({
        "body_cap": HOST_API_BODY_CAP,
        "header": HOST_API_HEADER,
        "mismatch": {
            "reply": ProtocolMismatch::schema_name(),
            "status": MISMATCH_STATUS,
        },
        "protocol": HOST_API_PROTOCOL,
        "routes": routes,
    })
}

/// The entry of one JSON route, and the `$defs` entries of its types. The
/// `JsonSchema` bounds live here, not on `JsonRoute` (decision CH25).
fn json_route<R>(generator: &mut SchemaGenerator) -> Value
where
    R: JsonRoute,
    R::Request: JsonSchema,
    R::Reply: JsonSchema,
    R::Error: JsonSchema,
{
    let _ = generator.subschema_for::<R::Request>();
    let _ = generator.subschema_for::<R::Reply>();
    let _ = generator.subschema_for::<R::Error>();
    let statuses: Map<String, Value> = R::Error::STATUSES
        .iter()
        .map(|(code, status)| ((*code).to_owned(), json!(status)))
        .collect();

    json!({
        "error": R::Error::schema_name(),
        "kind": "json",
        "reply": R::Reply::schema_name(),
        "request": R::Request::schema_name(),
        "statuses": statuses,
    })
}

/// Give each integer of `schema` its `minimum` and `maximum` (decision CH22
/// rule 3). datamodel-codegen drops the `format` that schemars writes, so
/// the bounds are the only width the generated model checks.
///
/// schemars bounds an integer of 8 or 16 bits itself. This transform gives
/// an integer of 32 bits the bounds of its width. Any other integer (`u64`,
/// `i64`, `usize`, `isize`) needs bounds of its own type, at most 2^53 - 1
/// from zero, or the schema test fails. JavaScript reads no larger integer
/// exactly.
///
/// A nullable integer (`Option<u64>`) has the type `["integer", "null"]`, so
/// the transform checks it too.
fn bound_integers(schema: &mut Schema) {
    transform_subschemas(&mut bound_integers, schema);
    if !is_integer(schema) {
        return;
    }

    let width = match schema.get("format").and_then(Value::as_str) {
        Some("uint32") => Some((0, i64::from(u32::MAX))),
        Some("int32") => Some((i64::from(i32::MIN), i64::from(i32::MAX))),
        _ => None,
    };
    if let (Some((min, max)), Some(node)) = (width, schema.as_object_mut()) {
        node.entry("minimum").or_insert(json!(min));
        node.entry("maximum").or_insert(json!(max));
    }

    let limit = i64::try_from(JS_INTEGER_MAX).unwrap();
    let bound = |key: &str| schema.get(key).and_then(Value::as_i64);
    assert!(
        bound("minimum").is_some_and(|min| min >= -limit)
            && bound("maximum").is_some_and(|max| max <= limit),
        "an integer of a wire type needs a minimum and a maximum within 2^53 - 1 of zero: {schema:?}"
    );
}

/// List each property of `schema` in its `required`. schemars leaves out a
/// field of type `Option`, because a derived reader takes a missing key as
/// `None`. A struct whose `Option` fields read with `Option::deserialize`
/// refuses a missing key, and this transform makes its schema say so.
pub(super) fn require_each_key(schema: &mut Schema) {
    let keys: Vec<Value> = schema
        .get("properties")
        .and_then(Value::as_object)
        .map(|properties| properties.keys().cloned().map(Value::String).collect())
        .unwrap_or_default();
    schema.insert("required".to_owned(), Value::Array(keys));
}

/// True when `schema` admits an integer: its type is `"integer"`, or an array
/// that holds `"integer"`.
fn is_integer(schema: &Schema) -> bool {
    match schema.get("type") {
        Some(Value::String(name)) => name == "integer",
        Some(Value::Array(names)) => names.iter().any(|name| name == "integer"),
        _ => false,
    }
}

/// The Python constants of the route table `x-routes`: the protocol, its
/// header, the body cap, the mismatch status and one `JsonRoute` per route. The writer
/// uses only forms that `ruff format` keeps as they are, so the file passes
/// the gate unchanged.
fn python_routes(table: &Value) -> String {
    let routes = table["routes"].as_object().unwrap();
    let models: BTreeSet<&str> = routes
        .values()
        .flat_map(|entry| ROUTE_TYPES.map(|key| entry[key].as_str().unwrap()))
        .collect();

    let mut text = String::from(PYTHON_HEAD);
    text.push_str("from typing import Final\n\nfrom .models import (\n");
    for model in models {
        writeln!(text, "    {model},").unwrap();
    }
    text.push_str(")\nfrom .route import JsonRoute\n\n");
    writeln!(text, "HOST_API_PROTOCOL: Final = {}", table["protocol"]).unwrap();
    writeln!(text, "HOST_API_HEADER: Final = {}", table["header"]).unwrap();
    writeln!(text, "HOST_API_BODY_CAP: Final = {}", table["body_cap"]).unwrap();
    let status = &table["mismatch"]["status"];
    writeln!(text, "MISMATCH_STATUS: Final = {status}").unwrap();

    for (name, entry) in routes {
        let constant = name.to_ascii_uppercase();
        writeln!(text, "\n{constant}: Final = JsonRoute(").unwrap();
        writeln!(text, "    name={},", json!(name)).unwrap();
        for key in ROUTE_TYPES {
            writeln!(text, "    {key}={},", entry[key].as_str().unwrap()).unwrap();
        }
        text.push_str(")\n");
    }

    text
}

/// Each committed file, relative to the crate, and the text it must hold.
fn rendered() -> [(&'static str, String); 3] {
    let schema = wire_schema();
    let json = serde_json::to_string_pretty(&schema).unwrap();
    let typescript = format!("{TYPESCRIPT_HEAD}{json} as const;\n");

    [
        (SCHEMA_FILE, format!("{json}\n")),
        (TYPESCRIPT_FILE, typescript),
        (PYTHON_FILE, python_routes(&schema[ROUTES_KEY])),
    ]
}

#[test]
fn the_committed_schema_is_the_schema_of_the_wire_types() {
    let write = std::env::var(WRITE_VAR).as_deref() == Ok(WRITE);

    for (file, text) in rendered() {
        let path = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join(file);
        if write {
            std::fs::write(&path, &text).unwrap();
            continue;
        }

        let committed = std::fs::read_to_string(&path).unwrap_or_default();
        assert!(
            committed == text,
            "{file} is not what the wire types give. Run `{WRITE_VAR}={WRITE} cargo test -p creche-wire` in rust/ and commit the file."
        );
    }
}

#[test]
fn each_wire_type_has_one_definition() {
    let schema = wire_schema();

    assert_eq!(schema["$defs"]["SessionId"]["type"], "string");
}

/// Each route of the schema, by name.
fn schema_routes(schema: &Value) -> Map<String, Value> {
    schema[ROUTES_KEY]["routes"].as_object().unwrap().clone()
}

/// True when `name` matches `^[a-z][a-z_]*$`.
fn is_snake_case(name: &str) -> bool {
    let first = name.chars().next();

    first.is_some_and(|character| character.is_ascii_lowercase())
        && name
            .chars()
            .all(|character| character.is_ascii_lowercase() || character == '_')
}

#[test]
fn each_route_name_is_snake_case() {
    for name in schema_routes(&wire_schema()).keys() {
        assert!(
            is_snake_case(name),
            "route {name}: a route name matches ^[a-z][a-z_]*$"
        );
    }
}

#[test]
fn each_route_type_is_a_wire_type() {
    let schema = wire_schema();

    for (name, entry) in schema_routes(&schema) {
        for key in ROUTE_TYPES {
            let type_name = entry[key].as_str().unwrap();
            assert!(
                schema["$defs"].get(type_name).is_some(),
                "route {name}: the {key} {type_name} is no $defs entry"
            );
        }
    }
}

#[test]
fn the_route_table_holds_create() {
    let routes = schema_routes(&wire_schema());

    assert_eq!(
        routes["create"],
        json!({
            "error": "CreateError",
            "kind": "json",
            "reply": "SessionRow",
            "request": "CreateSession",
            "statuses": {
                "bad_content_type": 415,
                "bad_method": 405,
                "conflict": 409,
                "internal": 500,
                "no_route": 404,
                "not_json": 400,
                "not_valid": 400,
                "too_large": 413,
                "unknown_family": 404,
            },
        })
    );
}

#[test]
fn the_route_table_holds_list_with_the_shared_codes_only() {
    let schema = wire_schema();
    let routes = schema_routes(&schema);
    let statuses: Map<String, Value> = SHARED_CODES
        .iter()
        .map(|(code, status)| ((*code).to_owned(), json!(status)))
        .collect();

    assert_eq!(
        routes["list"],
        json!({
            "error": "ListError",
            "kind": "json",
            "reply": "SessionPage",
            "request": "ListSessions",
            "statuses": statuses,
        })
    );
    assert_eq!(schema[ROUTES_KEY]["body_cap"], json!(1_048_576));
}

#[test]
fn the_route_table_holds_the_mismatch_reply() {
    let schema = wire_schema();

    assert_eq!(
        schema[ROUTES_KEY]["mismatch"],
        json!({"reply": "ProtocolMismatch", "status": 412})
    );
    assert!(schema["$defs"].get("ProtocolMismatch").is_some());
}

/// A client knows the mismatch from the status alone (decision CH33 rule
/// 1), so no route error has its code or its status.
#[test]
fn no_route_error_has_the_mismatch_code_or_status() {
    macro_rules! check {
        (json $route:path) => {
            let name = <$route as JsonRoute>::NAME;
            let statuses = <<$route as JsonRoute>::Error as RouteError>::STATUSES;

            for (code, status) in statuses {
                assert!(
                    *code != "protocol_mismatch" && *status != MISMATCH_STATUS,
                    "route {name}: the code {code} has the mismatch code or status"
                );
            }
        };
    }
    crate::route_table!(check);
}

#[test]
fn the_shared_codes_are_the_codes_of_ch33_in_the_check_order() {
    assert_eq!(
        SHARED_CODES,
        [
            ("no_route", 404),
            ("bad_method", 405),
            ("bad_content_type", 415),
            ("too_large", 413),
            ("not_json", 400),
            ("not_valid", 400),
            ("internal", 500),
        ]
    );
}

#[test]
fn each_route_error_ends_with_the_shared_codes() {
    macro_rules! check {
        (json $route:path) => {
            let name = <$route as JsonRoute>::NAME;
            let statuses = <<$route as JsonRoute>::Error as RouteError>::STATUSES;
            let own = statuses.len().checked_sub(SHARED_CODES.len());

            assert_eq!(
                own.map(|own| &statuses[own..]),
                Some(SHARED_CODES),
                "route {name}: the error does not end with the shared codes"
            );
        };
    }
    crate::route_table!(check);
}

#[test]
fn no_code_of_a_route_is_a_shared_code() {
    macro_rules! check {
        (json $route:path) => {
            let name = <$route as JsonRoute>::NAME;
            let statuses = <<$route as JsonRoute>::Error as RouteError>::STATUSES;
            let own = &statuses[..statuses.len() - SHARED_CODES.len()];

            for (code, _) in own {
                assert!(
                    SHARED_CODES.iter().all(|(shared, _)| shared != code),
                    "route {name}: the code {code} of the route is a shared code"
                );
            }
        };
    }
    crate::route_table!(check);
}

/// The `code` of each `oneOf` member of an error schema, in order.
fn schema_codes(error: &Value) -> Vec<&str> {
    error["oneOf"]
        .as_array()
        .unwrap()
        .iter()
        .map(|member| member["properties"]["code"]["const"].as_str().unwrap())
        .collect()
}

#[test]
fn each_error_code_is_a_code_of_the_schema() {
    let schema = wire_schema();
    macro_rules! check {
        (json $route:path) => {
            let name = <$route as JsonRoute>::NAME;
            let error = <$route as JsonRoute>::Error::schema_name();
            let statuses = <<$route as JsonRoute>::Error as RouteError>::STATUSES;
            let codes: Vec<&str> = statuses.iter().map(|(code, _)| *code).collect();
            let mut unique = codes.clone();
            unique.sort_unstable();
            unique.dedup();

            assert_eq!(
                schema_codes(&schema["$defs"][&*error]),
                codes,
                "route {name}"
            );
            assert_eq!(
                unique.len(),
                codes.len(),
                "route {name}: a code is given twice"
            );
            assert!(
                codes.iter().all(|code| is_snake_case(code)),
                "route {name}: {codes:?}"
            );
            assert!(
                schema["$defs"][&*error]["oneOf"]
                    .as_array()
                    .unwrap()
                    .iter()
                    .all(|member| member.get("title").is_none()),
                "route {name}: a variant of {error} has a title"
            );
        };
    }
    crate::route_table!(check);
}

#[test]
fn each_error_status_is_its_entry_in_the_route_table() {
    let schema = wire_schema();
    let routes = schema_routes(&schema);
    macro_rules! check {
        (json $route:path) => {{
            type Error = <$route as JsonRoute>::Error;
            let name = <$route as JsonRoute>::NAME;
            let statuses = <Error as RouteError>::STATUSES;
            let entry: Map<String, Value> = statuses
                .iter()
                .map(|(code, status)| ((*code).to_owned(), json!(status)))
                .collect();

            assert_eq!(
                routes[name]["statuses"],
                Value::Object(entry),
                "route {name}"
            );
            for (code, status) in statuses {
                assert!(
                    (400..=599).contains(status),
                    "route {name}: the status {status} of {code} is not 4xx or 5xx"
                );
                let value: Error = serde_json::from_value(json!({"code": code})).unwrap();
                assert_eq!(value.status(), *status, "route {name}: {code}");
            }
        }};
    }
    crate::route_table!(check);
}

/// The paths of the objects in `node` that leave a property out of
/// `required` (`wire/AGENTS.md`, "A struct on the wire", rule 4).
fn optional_keys(node: &Value, path: &str, found: &mut Vec<String>) {
    match node {
        Value::Object(map) => {
            if let Some(Value::Object(properties)) = map.get("properties") {
                let required = map.get("required").and_then(Value::as_array);
                for key in properties.keys() {
                    if !required.is_some_and(|keys| keys.contains(&json!(key))) {
                        found.push(format!("{path}/{key}"));
                    }
                }
            }
            for (key, child) in map {
                optional_keys(child, &format!("{path}/{key}"), found);
            }
        }
        Value::Array(items) => {
            for (index, child) in items.iter().enumerate() {
                optional_keys(child, &format!("{path}/{index}"), found);
            }
        }
        _ => {}
    }
}

#[test]
fn each_key_of_each_wire_object_is_required() {
    let mut found = Vec::new();
    optional_keys(&wire_schema(), "", &mut found);

    assert!(found.is_empty(), "keys not in `required`: {found:?}");
}

#[test]
fn a_key_left_out_of_required_is_found() {
    let mut found = Vec::new();
    let node = json!({"$defs": {"A": {"properties": {"a": {}, "b": {}}, "required": ["a"]}}});
    optional_keys(&node, "", &mut found);

    assert_eq!(found, ["/$defs/A/b"]);
}

#[test]
fn an_integer_of_32_bits_gets_the_bounds_of_its_width() {
    let cases = [
        (schemars::schema_for!(u32), 0, i64::from(u32::MAX)),
        (
            schemars::schema_for!(i32),
            i64::from(i32::MIN),
            i64::from(i32::MAX),
        ),
        (schemars::schema_for!(Option<u32>), 0, i64::from(u32::MAX)),
    ];

    for (mut schema, min, max) in cases {
        bound_integers(&mut schema);

        assert_eq!(schema.get("minimum"), Some(&json!(min)), "{schema:?}");
        assert_eq!(schema.get("maximum"), Some(&json!(max)), "{schema:?}");
    }
}

#[test]
#[should_panic(expected = "within 2^53 - 1 of zero")]
fn an_integer_of_64_bits_without_a_maximum_fails() {
    bound_integers(&mut schemars::schema_for!(u64));
}

#[test]
#[should_panic(expected = "within 2^53 - 1 of zero")]
fn a_nullable_integer_of_64_bits_without_a_maximum_fails() {
    bound_integers(&mut schemars::schema_for!(Option<u64>));
}

#[test]
#[should_panic(expected = "within 2^53 - 1 of zero")]
fn an_integer_with_a_minimum_past_the_cap_fails() {
    let limit = i64::try_from(JS_INTEGER_MAX).unwrap();
    let mut schema = Schema::try_from(json!({
        "type": "integer",
        "minimum": -limit - 1,
        "maximum": 0,
    }))
    .unwrap();

    bound_integers(&mut schema);
}
