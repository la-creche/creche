//! The differential test of the config types: each vector of each `config.`
//! surface.
//!
//! A vector is one input and what the Python implementation did with it.
//! The Rust code does the same with each vector, with two exceptions that
//! `rust/AGENTS.md` states: an input on which the Python code raises, and a
//! row of [`DEVIATIONS`].

use std::collections::HashSet;

use creche_vectors::{self as vectors, Marker, Outcome, Vector};
use serde_json::{Map, Value, json};

use super::attendance::{AttendanceConfig, Bind};
use super::mounts::{Credentials, PlaypenEnv, RawPlaypenEnv, RuntimeConfig};
use super::noticeboard::{AccessKey, CookieSecure, NoticeboardConfig};
use super::roster::{RawRoster, Roster, Upstream};
use super::site::{self, OperatorUserError, RepoName, SiteFault, SiteFile, SiteFileError};
use super::{
    AttendanceTarget, BindAddress, ConfigError, ConfigErrors, DirPath, Env, HttpUrl, LAN_ADDRESS,
    LanAddress, chaperone,
};
use crate::ids::SandboxName;
use crate::secret::Secret;

/// What the code did with one input, as a vector file writes it. `None`
/// stands for a key that the vector does not have.
///
/// The type is not `Result`: the lint gate refuses an `unwrap` in a function
/// that returns `Result`, and a replay function stops the test on a vector
/// that it cannot read.
#[derive(Debug, PartialEq)]
enum Did {
    /// The code took the input. The value is the `value` or the `output` of
    /// the vector.
    Took(Option<Value>),
    /// The code refused the input. The value is the `refusal` of the
    /// vector.
    Refused(Option<Value>),
}

/// One surface and the Rust code that replays a vector of it.
struct Against {
    surface: &'static str,
    replay: fn(&Vector) -> Did,
}

/// Each surface of the config module. A test below fails when the index
/// holds a `config.` surface that this table does not name.
const SURFACES: &[Against] = &[
    Against {
        surface: "config.site_file",
        replay: site_file,
    },
    Against {
        surface: "config.roster",
        replay: roster,
    },
    Against {
        surface: "config.runtime_json.write",
        replay: runtime_write,
    },
    Against {
        surface: "config.creds_json.read",
        replay: creds_read,
    },
    Against {
        surface: "config.creds_json.write",
        replay: creds_write,
    },
    Against {
        surface: "config.playpen_env.write",
        replay: playpen_env_write,
    },
    Against {
        surface: "config.playpen_env.read",
        replay: playpen_env_read,
    },
    Against {
        surface: "config.attendance.env",
        replay: attendance_env,
    },
    Against {
        surface: "config.noticeboard.env",
        replay: noticeboard_env,
    },
    Against {
        surface: "config.chaperone.site",
        replay: chaperone_site,
    },
];

/// What the name of the surfaces of this module starts with.
const SURFACE_PREFIX: &str = "config.";

// --- the deviations ---

/// How the Rust code differs from the Python code on one vector.
#[derive(Clone, Copy)]
enum Differs {
    /// The Python code accepts the input. The Rust code refuses it.
    Refuses,
}

struct Deviation {
    surface: &'static str,
    vectors: &'static [&'static str],
    differs: Differs,
    /// The contract section that the decision reads.
    contract: &'static str,
    /// The decision, and its reason.
    decision: &'static str,
}

const STRICT_JSON: &str = "The contract gives a JSON file. Python's json reads NaN, a lone \
    surrogate escape and each depth of nesting that its stack permits. serde_json reads RFC 8259 \
    and Unicode text, and stops at 128 levels of nesting. It refuses the three.";

/// Each vector on which the Rust code differs from the Python code on
/// purpose. Each other vector must be equal.
const DEVIATIONS: &[Deviation] = &[Deviation {
    surface: "config.creds_json.read",
    vectors: &[
        "nan-in-unknown-key",
        "unknown-key-130-levels",
        "key-lone-surrogate",
    ],
    differs: Differs::Refuses,
    contract: "contract 03 §12",
    decision: STRICT_JSON,
}];

/// The decision that covers one vector of one surface.
fn deviation_of(surface: &str, vector: &str) -> Option<&'static Deviation> {
    DEVIATIONS
        .iter()
        .find(|deviation| deviation.surface == surface && deviation.vectors.contains(&vector))
}

/// The count of vectors of one surface that `DEVIATIONS` covers.
fn deviations_in(surface: &str) -> usize {
    DEVIATIONS
        .iter()
        .filter(|deviation| deviation.surface == surface)
        .map(|deviation| deviation.vectors.len())
        .sum()
}

/// What the Python code did with one input.
fn python_did(vector: &Vector) -> Did {
    match vector.result() {
        Outcome::Accepted => Did::Took(vector.value().or(vector.field("output")).cloned()),
        Outcome::Refused | Outcome::Raised => Did::Refused(vector.refusal().cloned()),
    }
}

/// Makes sure that the Rust code differs from the vector as the decision
/// says, and in no other way.
fn differs_as_decided(differs: Differs, vector: &Vector, rust: &Did, at: &str) {
    assert_eq!(vector.result(), Outcome::Accepted, "{at}: the Python code");
    match differs {
        Differs::Refuses => {
            assert!(
                matches!(rust, Did::Refused(_)),
                "{at}: the Rust code accepts"
            );
        }
    }
}

/// Walks each vector of one surface. It prints the counts, and a run with
/// `--nocapture` shows them.
fn walk(name: &str) {
    let against = SURFACES
        .iter()
        .find(|against| against.surface == name)
        .unwrap();
    let surface = vectors::surface(against.surface).unwrap();
    let mut equal = 0;
    let mut deviated = 0;
    for vector in surface.vectors() {
        let at = format!("{name} {}", vector.id());
        let rust = (against.replay)(vector);

        if let Some(deviation) = deviation_of(name, vector.id()) {
            differs_as_decided(deviation.differs, vector, &rust, &at);
            deviated += 1;
        } else if vector.result() == Outcome::Raised {
            assert!(
                matches!(rust, Did::Refused(_)),
                "{at}: the Python code raises"
            );
            equal += 1;
        } else {
            assert_eq!(rust, python_did(vector), "{at}");
            equal += 1;
        }
    }

    assert_eq!(
        deviated,
        deviations_in(name),
        "{name}: a row names no vector"
    );
    println!(
        "{name}: {} vectors: {equal} equal, {deviated} deviations",
        equal + deviated
    );
}

// --- the replay of each surface ---

/// The named arguments of a builder vector.
fn args(vector: &Vector) -> &Map<String, Value> {
    vector.input().args().unwrap()
}

/// The variables of an environment vector.
fn env_of(vector: &Vector) -> Env {
    let pairs = args(vector)
        .iter()
        .map(|(name, value)| (name.clone(), value.as_str().unwrap().to_owned()));

    Env::from_pairs(pairs)
}

/// The names that the Python reader gives a variable when the Rust reader
/// names another one: the Python text, then the Rust name. The noticeboard
/// names `VIEW_BIND` for a host that came from the site file.
const OTHER_NAMES: &[(&str, &str)] = &[("VIEW_BIND", LAN_ADDRESS)];

/// The refusal of an environment vector. The Python reader stops at its
/// first error, and the Rust reader collects each one. The refusal is the
/// variable of the vector when the Rust errors hold it, else the first
/// variable of the Rust errors.
fn named(vector: &Vector, errors: &ConfigErrors) -> Did {
    let rust: Vec<&str> = errors
        .as_slice()
        .iter()
        .map(ConfigError::variable)
        .collect();
    let python = vector
        .refusal()
        .and_then(|refusal| refusal["variable"].as_str());
    let agreed = python.filter(|python| {
        rust.contains(python)
            || OTHER_NAMES
                .iter()
                .any(|(theirs, ours)| theirs == python && rust.contains(ours))
    });

    Did::Refused(Some(json!({"variable": agreed.unwrap_or(rust[0])})))
}

/// The text of a secret. Each secret of a vector is text of a test.
fn text_of(secret: &Secret) -> String {
    String::from_utf8(secret.expose_secret().to_vec()).unwrap()
}

/// The uid that stands for the account of the generator. The generator owns
/// each file that it writes, so the owner is the reader.
const GENERATOR_UID: u32 = 1000;

fn site_file_refusal(error: SiteFileError) -> Did {
    let class = match error {
        SiteFileError::TooLarge => "too_large",
        SiteFileError::NotUtf8 => "unreadable",
        SiteFileError::NotKeyValue => "not_key_value",
        SiteFileError::WrongOwner => "owner",
        SiteFileError::OthersWrite => "mode",
    };

    Did::Refused(Some(json!(class)))
}

/// One field of the value of a site file vector.
fn site_field<T: ToString>(read: Result<T, site::SiteError>) -> Value {
    match read.map_err(|error| error.fault()) {
        Ok(value) => json!({"value": value.to_string()}),
        Err(SiteFault::Unset) => json!({"refused": "unset"}),
        Err(SiteFault::OperatorUser(OperatorUserError::Root)) => json!({"refused": "root"}),
        Err(_) => json!({"refused": "shape"}),
    }
}

fn site_file(vector: &Vector) -> Did {
    let mode = vector.field("params").unwrap()["mode"].as_str().unwrap();
    let mode = u32::from_str_radix(mode, 8).unwrap();
    if let Err(error) = site::check_trust(GENERATOR_UID, GENERATOR_UID, mode) {
        return site_file_refusal(error);
    }

    let file = match SiteFile::parse(&vector.input().bytes().unwrap()) {
        Ok(file) => file,
        Err(error) => return site_file_refusal(error),
    };
    let catalog_name: RepoName = "agent-control".parse().unwrap();
    let github_repo = match file.github_repo(&catalog_name) {
        Ok(name) => json!({"value": name.as_str()}),
        Err(_) => json!({"refused": "shape"}),
    };

    Did::Took(Some(json!({
        "github_owner": site_field(file.github_owner()),
        "operator_user": site_field(file.operator_user()),
        "operator_home": site_field(file.operator_home()),
        "lan_address": site_field(file.lan_address()),
        "github_repo": github_repo,
    })))
}

/// Whether a tree holds a mapping with a key that is not a text. The raw
/// form takes its keys as text, so it cannot hold such a mapping.
fn holds_other_key(tree: &Value) -> bool {
    if matches!(Marker::of(tree).unwrap(), Some(Marker::Entries(_))) {
        return true;
    }

    match tree {
        Value::Array(items) => items.iter().any(holds_other_key),
        Value::Object(fields) => fields.values().any(holds_other_key),
        Value::Null | Value::Bool(_) | Value::Number(_) | Value::String(_) => false,
    }
}

/// The row of one upstream, as a vector writes it.
fn roster_row(name: &str, upstream: &Upstream) -> Value {
    let env: Map<String, Value> = upstream
        .env()
        .iter()
        .map(|(variable, value)| (variable.clone(), json!(value.to_text())))
        .collect();
    let arg_denies: Vec<Value> = upstream
        .arg_denies()
        .iter()
        .map(|deny| json!({"tools": deny.tools(), "arg": deny.arg(), "values": deny.values()}))
        .collect();

    json!({
        "name": name,
        "command": upstream.command(),
        "args": upstream.args(),
        "env": env,
        "arg_denies": arg_denies,
        "tools": upstream.tools(),
    })
}

fn roster(vector: &Vector) -> Did {
    let tree = &args(vector)["document"];
    if holds_other_key(tree) {
        return Did::Refused(None);
    }

    let Ok(raw) = serde_json::from_value::<RawRoster>(tree.clone()) else {
        return Did::Refused(None);
    };
    let Ok(roster) = Roster::try_from(raw) else {
        return Did::Refused(None);
    };
    let rows = roster
        .iter()
        .map(|(name, upstream)| (name.to_owned(), roster_row(name, upstream)));

    Did::Took(Some(Value::Object(rows.collect())))
}

/// The `output` of a writer vector: the text that the writer gives.
fn output(text: String) -> Did {
    Did::Took(Some(json!({"text": text})))
}

fn runtime_write(vector: &Vector) -> Did {
    let raw = serde_json::to_vec(args(vector)).unwrap();

    match RuntimeConfig::parse(&raw) {
        Ok(config) => output(config.to_json()),
        Err(_) => Did::Refused(None),
    }
}

fn creds_read(vector: &Vector) -> Did {
    let Ok(creds) = Credentials::parse(&vector.input().bytes().unwrap()) else {
        return Did::Refused(None);
    };

    Did::Took(Some(json!({
        "epoch": creds.epoch(),
        "litellm_key": text_of(creds.litellm_key()),
        "pep_token": text_of(creds.pep_token()),
        "written_at": creds.written_at(),
        "previous_pep_token": creds.previous_pep_token().map(text_of),
        "previous_expires_at": creds.previous_expires_at(),
    })))
}

fn creds_write(vector: &Vector) -> Did {
    let args = args(vector);
    let secret = |name: &str| Secret::try_from(args[name].as_str()?.to_owned()).ok();
    let text = |name: &str| args[name].as_str().map(str::to_owned);
    let (Some(epoch), Some(litellm_key), Some(pep_token)) = (
        args["epoch"].as_i64(),
        secret("litellm_key"),
        secret("pep_token"),
    ) else {
        return Did::Refused(None);
    };
    let creds = Credentials::new(epoch, litellm_key, pep_token, text("written_at").unwrap())
        .with_previous(secret("previous_pep_token"), text("previous_expires_at"));

    output(creds.to_json().unwrap())
}

fn playpen_env_write(vector: &Vector) -> Did {
    let args = args(vector);
    let text = |name: &str| args[name].as_str().unwrap();
    let (Ok(state_root), Ok(sandbox)) = (
        text("state_root").parse::<DirPath>(),
        text("sandbox").parse::<SandboxName>(),
    ) else {
        return Did::Refused(None);
    };
    if sandbox.family().as_str() != text("family") {
        return Did::Refused(None);
    }

    output(PlaypenEnv::for_sandbox(&state_root, &sandbox).to_text())
}

fn playpen_env_read(vector: &Vector) -> Did {
    let raw = RawPlaypenEnv::parse(vector.input().text().unwrap());
    let variables: Map<String, Value> = raw
        .iter()
        .map(|(name, value)| (name.to_owned(), json!(value)))
        .collect();

    Did::Took(Some(json!({"variables": variables})))
}

fn attendance_env(vector: &Vector) -> Did {
    let config = match AttendanceConfig::from_env(&env_of(vector)) {
        Ok(config) => config,
        Err(errors) => return named(vector, &errors),
    };
    let bind = match config.bind() {
        Bind::SocketOnly => "socket_only",
        Bind::SocketAndLan => "socket_and_lan",
    };

    Did::Took(Some(json!({
        "sessions_root": config.sessions_root().as_str(),
        "state_root": config.state_root().as_str(),
        "work_root": config.work_root().as_str(),
        "socket_path": config.socket_path().as_str(),
        "bind": bind,
        "lan_address": config.lan_address().as_str(),
        "lan_port": config.lan_port().get(),
        "channel_command": config.channel_command().as_str(),
        "log_dir": config.log_dir().as_str(),
        "owui_url": config.owui().url().map_or("", |url| url.as_str()),
        "owui_key_file": config.owui().key_file().as_str(),
        "owui_folder_id": config.owui().folder_id().unwrap_or(""),
        "lock_stale_s": config.lock_stale().as_secs_f64(),
        "lock_poll_s": config.lock_poll().as_secs_f64(),
    })))
}

/// The base URL that the Python noticeboard holds for a Unix socket. No
/// resolver reads the name.
const SOCKET_BASE_URL: &str = "http://sessiond";

fn noticeboard_env(vector: &Vector) -> Did {
    let config = match NoticeboardConfig::from_env(&env_of(vector)) {
        Ok(config) => config,
        Err(errors) => return named(vector, &errors),
    };
    let (socket, url) = match config.attendance() {
        AttendanceTarget::Socket(path) => (Some(path.as_str()), SOCKET_BASE_URL),
        AttendanceTarget::Url(url) => (None, url.as_str()),
    };
    let access_key = match config.access_key() {
        AccessKey::Key(key) => text_of(key),
        AccessKey::Open => String::new(),
        AccessKey::File(_) => panic!("{}: no vector names a key file", vector.id()),
    };

    Did::Took(Some(json!({
        "bind": config.bind().as_str(),
        "port": config.port().get(),
        "state_root": config.state_root().as_str(),
        "registry_dir": config.registry_dir().as_str(),
        "attendance_socket": socket,
        "attendance_url": url,
        "page_size": config.page_size(),
        "cookie_secure": config.cookie_secure() == CookieSecure::On,
        "access_key": access_key,
    })))
}

/// The text of a bind with no bracket.
fn no_brackets(text: &str) -> String {
    text.replace(['[', ']'], "")
}

/// The text of the bind of the chaperone. The Python reader returns the
/// text of `PEP_BIND` as it is, so an IPv6 host can have brackets or none.
/// The Rust type writes an IPv6 host with brackets. The two are equal when
/// the two texts are equal without their brackets. That is one fixed rule:
/// the test does not parse the Python text with the Rust parser.
///
/// `chaperone.site.listener` splits the text into a host and a port. No
/// vector holds the two parts.
fn bind_text(vector: &Vector, bind: &BindAddress) -> String {
    let rust = bind.to_string();
    let python = vector.value().and_then(|value| value["text"].as_str());

    match python {
        Some(text) if no_brackets(text) == no_brackets(&rust) => text.to_owned(),
        _ => rust,
    }
}

/// The text of a URL that can be absent. The Python readers return the
/// empty text for a site with no such service.
fn url_text(url: Option<HttpUrl>) -> String {
    url.map_or_else(String::new, |url| url.to_string())
}

fn chaperone_site(vector: &Vector) -> Did {
    let env = env_of(vector);
    let function = vector.field("params").unwrap()["function"]
        .as_str()
        .unwrap();
    let text = match function {
        "bind" => chaperone::bind(&env).map(|bind| bind_text(vector, &bind)),
        "lan_address" => env
            .require::<LanAddress>(LAN_ADDRESS)
            .map(|address| address.to_string()),
        "tei_url" => chaperone::tei_url(&env).map(url_text),
        "ha_url" => chaperone::ha_url(&env).map(url_text),
        other => panic!("{}: no reader has the name {other}", vector.id()),
    };

    match text {
        Ok(text) => output(text),
        Err(errors) => named(vector, &errors),
    }
}

// --- the tests ---

#[test]
fn the_table_holds_each_config_surface_of_the_index_one_time() {
    let listed: Vec<&str> = SURFACES.iter().map(|against| against.surface).collect();
    let unique: HashSet<&str> = listed.iter().copied().collect();
    let index = vectors::index().unwrap();
    let in_index: HashSet<&str> = index
        .iter()
        .map(|row| row.surface())
        .filter(|surface| surface.starts_with(SURFACE_PREFIX))
        .collect();

    assert_eq!(
        unique.len(),
        listed.len(),
        "the table holds one surface two times"
    );
    assert_eq!(unique, in_index);
}

#[test]
fn each_deviation_names_a_vector_of_a_surface_one_time() {
    let mut seen = HashSet::new();
    for deviation in DEVIATIONS {
        let surface = vectors::surface(deviation.surface).unwrap();
        let ids: HashSet<&str> = surface.vectors().iter().map(|vector| vector.id()).collect();

        assert!(deviation.contract.contains('§'), "{}", deviation.surface);
        assert!(!deviation.decision.is_empty(), "{}", deviation.surface);
        for vector in deviation.vectors {
            assert!(ids.contains(vector), "{} {vector}", deviation.surface);
            assert!(
                seen.insert((deviation.surface, *vector)),
                "{} {vector}: two rows",
                deviation.surface
            );
        }
    }
}

#[test]
fn no_committed_vector_of_a_config_surface_is_raised() {
    // The walk refuses a raised vector. A committed one is also a defect of
    // the Python code that `vectors/AGENTS.md` keeps out of the files.
    for against in SURFACES {
        let surface = vectors::surface(against.surface).unwrap();

        assert!(
            surface
                .vectors()
                .iter()
                .all(|vector| vector.result() != Outcome::Raised),
            "{}",
            against.surface
        );
    }
}

#[test]
fn a_site_file_is_what_the_python_reader_takes() {
    walk("config.site_file");
}

#[test]
fn a_roster_is_what_the_python_reader_takes() {
    walk("config.roster");
}

#[test]
fn a_runtime_json_is_what_the_python_writer_writes() {
    walk("config.runtime_json.write");
}

#[test]
fn a_creds_json_is_what_the_python_reader_takes() {
    walk("config.creds_json.read");
}

#[test]
fn a_creds_json_is_what_the_python_writer_writes() {
    walk("config.creds_json.write");
}

#[test]
fn a_playpen_env_file_is_what_the_python_writer_writes() {
    walk("config.playpen_env.write");
}

#[test]
fn a_playpen_env_file_is_what_the_python_reader_takes() {
    walk("config.playpen_env.read");
}

#[test]
fn the_config_of_attendance_is_what_the_python_reader_takes() {
    walk("config.attendance.env");
}

#[test]
fn the_config_of_the_noticeboard_is_what_the_python_reader_takes() {
    walk("config.noticeboard.env");
}

#[test]
fn the_site_values_of_the_chaperone_are_what_the_python_readers_take() {
    walk("config.chaperone.site");
}
