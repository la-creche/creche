//! A YAML value to a raw file: the shape check.
//!
//! The Python validator gives the value to a pydantic model. The model
//! checks the shape only: which fields exist and their types. It reports
//! each wrong type and each unknown field in one pass. A derived
//! `Deserialize` stops at the first error, so this module walks the value
//! by hand, in the order of pydantic: each field of a model in the order of
//! its declaration, then each key of the mapping in the order of the file.

use std::collections::BTreeMap;

use creche_contracts::family::{
    RawDaily, RawEnqueue, RawFamily, RawHaCall, RawHaTriple, RawJob, RawModel, RawMount,
    RawNoFence, RawQuiet, RawRelease, RawSandbox, RawToolGrant, RawTrigger, RawVerbs,
    python_float_text,
};
use creche_contracts::server::{RawFence, RawInstall, RawRun, RawServer, RawSharedSecret, RawTool};

use crate::lax;
use crate::yaml::text::repr_bytes;
use crate::yaml::{DateTime, Obj, ObjId, Value};

const MISSING: &str = "Field required";
const INVALID_KEY: &str = "Keys should be strings";
const LIST_TYPE: &str = "Input should be a valid list";
const DICT_TYPE: &str = "Input should be a valid dictionary";

/// The part of a location that names the key of a mapping entry.
const KEY_PART: &str = "[key]";

/// The two parts that name the arms of the union `list[str] | str`.
const LIST_ARM: &str = "list[str]";
const STR_ARM: &str = "str";

/// One part of a location, as pydantic holds it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) enum Part {
    /// A field name, or the text of a key that is no string and no integer.
    Key(String),
    /// A list index, or a key that is an integer.
    Index(String),
}

/// What is wrong at one location.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) enum Problem {
    /// The message of the pydantic error.
    Said(String),
    /// A field that the model does not know. The parser writes the message:
    /// it names the nearest known field.
    UnknownField,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct Fault {
    pub(crate) loc: Vec<Part>,
    pub(crate) problem: Problem,
}

/// Collects each fault, with the location of the value that the walk reads.
#[derive(Debug, Default)]
pub(crate) struct Walk {
    path: Vec<Part>,
    faults: Vec<Fault>,
}

impl Walk {
    pub(crate) fn into_faults(self) -> Vec<Fault> {
        self.faults
    }

    fn said(&mut self, msg: &str) {
        self.faults.push(Fault {
            loc: self.path.clone(),
            problem: Problem::Said(msg.to_owned()),
        });
    }

    /// Runs `read` with one more part at the end of the location.
    fn at<T>(&mut self, part: Part, read: impl FnOnce(&mut Self) -> T) -> T {
        self.path.push(part);
        let out = read(self);
        self.path.pop();

        out
    }

    fn key<T>(&mut self, name: &str, read: impl FnOnce(&mut Self) -> T) -> T {
        self.at(Part::Key(name.to_owned()), read)
    }
}

// --- the text of a key in a location ---

/// `repr` of a `datetime.timedelta` that is the offset of a zone.
fn offset_repr(offset: i32) -> String {
    if offset == 0 {
        return "datetime.timezone.utc".to_owned();
    }

    let days = offset.div_euclid(86_400);
    let seconds = offset.rem_euclid(86_400);
    let fields = match (days, seconds) {
        (0, seconds) => format!("seconds={seconds}"),
        (days, 0) => format!("days={days}"),
        (days, seconds) => format!("days={days}, seconds={seconds}"),
    };

    format!("datetime.timezone(datetime.timedelta({fields}))")
}

/// `repr` of a `datetime.datetime`.
fn datetime_repr(value: &DateTime) -> String {
    let mut fields = vec![
        value.date.year,
        value.date.month,
        value.date.day,
        value.hour,
        value.minute,
        value.second,
        value.microsecond,
    ];
    if value.microsecond == 0 {
        fields.pop();
        if value.second == 0 {
            fields.pop();
        }
    }

    let fields: Vec<String> = fields.iter().map(u32::to_string).collect();
    let zone = value
        .offset
        .map(|offset| format!(", tzinfo={}", offset_repr(offset)))
        .unwrap_or_default();

    format!("datetime.datetime({}{zone})", fields.join(", "))
}

/// The part of a location for a key, as pydantic makes it: a string is
/// itself, an integer or a boolean is an index, and each other key is its
/// Python `repr`.
fn key_part(key: &Obj) -> Part {
    match key {
        Obj::Str(text) => Part::Key(text.clone()),
        Obj::Bool(value) => Part::Index(u8::from(*value).to_string()),
        Obj::Int(value) if value.to_i64().is_some() => Part::Index(value.to_string()),
        Obj::Int(value) => Part::Key(value.to_string()),
        Obj::None => Part::Key("None".to_owned()),
        Obj::Float(value) => Part::Key(python_float_text(*value)),
        Obj::SharedNan => Part::Key("nan".to_owned()),
        Obj::Bytes(data) => Part::Key(repr_bytes(data)),
        Obj::Date(date) => Part::Key(format!(
            "datetime.date({}, {}, {})",
            date.year, date.month, date.day
        )),
        Obj::DateTime(value) => Part::Key(datetime_repr(value)),
        // A collection is no key: PyYAML refuses it.
        Obj::List(_) | Obj::Pair(..) | Obj::Set(_) | Obj::Dict(_) => Part::Key(String::new()),
    }
}

/// The name that Python gives the type of a value: `type(value).__name__`.
pub(crate) fn python_type(obj: &Obj) -> &'static str {
    match obj {
        Obj::None => "NoneType",
        Obj::Bool(_) => "bool",
        Obj::Int(_) => "int",
        Obj::Float(_) | Obj::SharedNan => "float",
        Obj::Str(_) => "str",
        Obj::Bytes(_) => "bytes",
        Obj::Date(_) => "date",
        Obj::DateTime(_) => "datetime",
        Obj::List(_) => "list",
        Obj::Pair(..) => "tuple",
        Obj::Set(_) => "set",
        Obj::Dict(_) => "dict",
    }
}

// --- scalars ---

fn read_str(value: Value<'_>, walk: &mut Walk) -> Option<String> {
    lax::as_str(value.obj()).map_err(|msg| walk.said(msg)).ok()
}

fn read_bool(value: Value<'_>, walk: &mut Walk) -> Option<bool> {
    lax::as_bool(value.obj()).map_err(|msg| walk.said(msg)).ok()
}

fn read_int(value: Value<'_>, walk: &mut Walk) -> Option<creche_contracts::family::RawInt> {
    lax::as_int(value.obj()).map_err(|msg| walk.said(msg)).ok()
}

fn read_float(value: Value<'_>, walk: &mut Walk) -> Option<f64> {
    lax::as_float(value.obj())
        .map_err(|msg| walk.said(msg))
        .ok()
}

// --- collections ---

/// The items of a value that pydantic takes as a list: a list, a tuple or a
/// set.
fn list_items(value: Value<'_>) -> Option<Vec<ObjId>> {
    match value.obj() {
        Obj::List(items) | Obj::Set(items) => Some(items.clone()),
        Obj::Pair(first, second) => Some(vec![*first, *second]),
        _ => None,
    }
}

fn read_items<'a, T>(
    value: Value<'a>,
    items: &[ObjId],
    walk: &mut Walk,
    read: impl Fn(Value<'a>, &mut Walk) -> Option<T>,
) -> Option<Vec<T>> {
    let read: Vec<Option<T>> = items
        .iter()
        .enumerate()
        .map(|(index, item)| {
            walk.at(Part::Index(index.to_string()), |walk| {
                read(value.at(*item), walk)
            })
        })
        .collect();

    read.into_iter().collect()
}

fn read_list<'a, T>(
    value: Value<'a>,
    walk: &mut Walk,
    read: impl Fn(Value<'a>, &mut Walk) -> Option<T>,
) -> Option<Vec<T>> {
    let Some(items) = list_items(value) else {
        walk.said(LIST_TYPE);

        return None;
    };

    read_items(value, &items, walk, read)
}

fn read_strings(value: Value<'_>, walk: &mut Walk) -> Option<Vec<String>> {
    read_list(value, walk, read_str)
}

fn read_optional<'a, T>(
    value: Value<'a>,
    walk: &mut Walk,
    read: impl FnOnce(Value<'a>, &mut Walk) -> Option<T>,
) -> Option<Option<T>> {
    if matches!(value.obj(), Obj::None) {
        return Some(None);
    }

    read(value, walk).map(Some)
}

fn read_optional_str(value: Value<'_>, walk: &mut Walk) -> Option<Option<String>> {
    read_optional(value, walk, read_str)
}

/// The union `list[str] | str`. When both arms refuse the value, pydantic
/// reports the errors of both.
fn read_grant(value: Value<'_>, walk: &mut Walk) -> Option<RawToolGrant> {
    if let Ok(text) = lax::as_str(value.obj()) {
        return Some(RawToolGrant::Text(text));
    }

    let named = walk.key(LIST_ARM, |walk| read_strings(value, walk));
    if named.is_none() {
        walk.key(STR_ARM, |walk| read_str(value, walk));
    }

    named.map(RawToolGrant::Named)
}

/// A mapping from a string to a value: `dict[str, T]`. A key that is bytes
/// of UTF-8 text is a string.
fn read_dict<'a, T>(
    value: Value<'a>,
    walk: &mut Walk,
    read: impl Fn(Value<'a>, &mut Walk) -> Option<T>,
) -> Option<BTreeMap<String, T>> {
    let Obj::Dict(pairs) = value.obj() else {
        walk.said(DICT_TYPE);

        return None;
    };

    let mut out = BTreeMap::new();
    let mut valid = true;
    for (key_id, value_id) in pairs {
        let key = value.at(*key_id);
        let (name, item) = walk.at(key_part(key.obj()), |walk| {
            let name = walk.key(KEY_PART, |walk| read_str(key, walk));
            let item = read(value.at(*value_id), walk);

            (name, item)
        });
        match (name, item) {
            (Some(name), Some(item)) => {
                out.insert(name, item);
            }
            _ => valid = false,
        }
    }

    valid.then_some(out)
}

// --- models ---

/// The entries of one mapping that a model reads.
struct Fields<'a> {
    value: Value<'a>,
    pairs: &'a [(ObjId, ObjId)],
    used: Vec<&'a str>,
}

impl<'a> Fields<'a> {
    /// The entries of `value`, or the error of pydantic for a value that is
    /// no mapping. `model` is the name of the Python class.
    fn of(value: Value<'a>, model: &str, walk: &mut Walk) -> Option<Self> {
        let Obj::Dict(pairs) = value.obj() else {
            walk.said(&format!(
                "Input should be a valid dictionary or instance of {model}"
            ));

            return None;
        };

        Some(Self {
            value,
            pairs,
            used: Vec::new(),
        })
    }

    fn find(&mut self, name: &'a str) -> Option<Value<'a>> {
        let (_, found) = self
            .pairs
            .iter()
            .find(|(key, _)| matches!(self.value.at(*key).obj(), Obj::Str(text) if text == name))?;
        self.used.push(name);

        Some(self.value.at(*found))
    }

    /// A field with no default.
    fn required<T>(
        &mut self,
        name: &'a str,
        walk: &mut Walk,
        read: impl FnOnce(Value<'a>, &mut Walk) -> Option<T>,
    ) -> Option<T> {
        walk.key(name, |walk| match self.find(name) {
            Some(value) => read(value, walk),
            None => {
                walk.said(MISSING);

                None
            }
        })
    }

    /// A field with a default. The outer `None` is a fault. The inner `None`
    /// is a field that the file does not hold.
    fn optional<T>(
        &mut self,
        name: &'a str,
        walk: &mut Walk,
        read: impl FnOnce(Value<'a>, &mut Walk) -> Option<T>,
    ) -> Option<Option<T>> {
        match self.find(name) {
            Some(value) => walk.key(name, |walk| read(value, walk)).map(Some),
            None => Some(None),
        }
    }

    /// The errors for each key that is no string, and for each field that
    /// the model does not know, in the order of the file.
    fn finish(self, walk: &mut Walk) -> bool {
        let mut clean = true;
        for (key, _) in self.pairs {
            let key = self.value.at(*key).obj();
            let problem = match key {
                Obj::Str(name) if self.used.contains(&name.as_str()) => continue,
                Obj::Str(_) => Problem::UnknownField,
                _ => Problem::Said(INVALID_KEY.to_owned()),
            };
            clean = false;
            walk.at(key_part(key), |walk| {
                walk.faults.push(Fault {
                    loc: walk.path.clone(),
                    problem,
                });
            });
        }

        clean
    }
}

/// Unwraps a default: the value that the file holds, or `default`.
fn or<T>(read: Option<Option<T>>, default: impl FnOnce() -> T) -> Option<T> {
    read.map(|value| value.unwrap_or_else(default))
}

fn read_model_block(value: Value<'_>, walk: &mut Walk) -> Option<RawModel> {
    let mut fields = Fields::of(value, "ModelBlock", walk)?;
    let router = fields.required("router", walk, read_str);
    let budget = fields.required("budget_usd_per_day", walk, read_float);
    let clean = fields.finish(walk);

    clean.then_some(())?;

    Some(RawModel {
        router: router?,
        budget_usd_per_day: budget?,
    })
}

fn read_mount(value: Value<'_>, walk: &mut Walk) -> Option<RawMount> {
    let mut fields = Fields::of(value, "FileMount", walk)?;
    let path = fields.required("path", walk, read_str);
    let mode = fields.required("mode", walk, read_str);
    let clean = fields.finish(walk);

    clean.then_some(())?;

    Some(RawMount {
        path: path?,
        mode: mode?,
    })
}

fn read_sandbox(value: Value<'_>, walk: &mut Walk) -> Option<RawSandbox> {
    let default = RawSandbox::default();
    let mut fields = Fields::of(value, "SandboxBlock", walk)?;
    let cpus = fields.optional("cpus", walk, read_int);
    let memory = fields.optional("memory", walk, read_str);
    let resident = fields.optional("max_resident_processes", walk, read_int);
    let image = fields.optional("image", walk, read_str);
    let clean = fields.finish(walk);

    clean.then_some(())?;

    Some(RawSandbox {
        cpus: or(cpus, || default.cpus.clone())?,
        memory: or(memory, || default.memory.clone())?,
        max_resident_processes: or(resident, || default.max_resident_processes.clone())?,
        image: or(image, || default.image.clone())?,
    })
}

fn read_job(value: Value<'_>, walk: &mut Walk) -> Option<RawJob> {
    let mut fields = Fields::of(value, "JobBlock", walk)?;
    let timeout = fields.optional("timeout", walk, read_str);
    let clean = fields.finish(walk);

    clean.then_some(())?;

    Some(RawJob {
        timeout: or(timeout, || RawJob::default().timeout)?,
    })
}

fn read_trigger(value: Value<'_>, walk: &mut Walk) -> Option<RawTrigger> {
    let mut fields = Fields::of(value, "Trigger", walk)?;
    let cron = fields.optional("cron", walk, |value, walk| {
        read_optional(value, walk, read_str)
    });
    let webhook = fields.optional("webhook", walk, |value, walk| {
        read_optional(value, walk, read_str)
    });
    let enqueue = fields.optional("enqueue", walk, |value, walk| {
        read_optional(value, walk, read_bool)
    });
    let clean = fields.finish(walk);

    clean.then_some(())?;

    Some(RawTrigger {
        cron: cron?.flatten(),
        webhook: webhook?.flatten(),
        enqueue: enqueue?.flatten(),
    })
}

fn read_daily(value: Value<'_>, walk: &mut Walk) -> Option<RawDaily> {
    let mut fields = Fields::of(value, "DailyCall", walk)?;
    let call = fields.required("call", walk, read_str);
    let hour = fields.required("hour", walk, read_int);
    let zone = fields.required("zone", walk, read_str);
    let weekdays_only = fields.optional("weekdays_only", walk, read_bool);
    let clean = fields.finish(walk);

    clean.then_some(())?;

    Some(RawDaily {
        call: call?,
        hour: hour?,
        zone: zone?,
        weekdays_only: or(weekdays_only, || false)?,
    })
}

fn read_quiet(value: Value<'_>, walk: &mut Walk) -> Option<RawQuiet> {
    let mut fields = Fields::of(value, "QuietBlock", walk)?;
    let board = fields.optional("board", walk, |value, walk| {
        read_optional(value, walk, read_str)
    });
    let daily = fields.optional("daily", walk, |value, walk| {
        read_optional(value, walk, read_daily)
    });
    let floor_hours = fields.optional("floor_hours", walk, read_int);
    let clean = fields.finish(walk);

    clean.then_some(())?;

    Some(RawQuiet {
        board: board?.flatten(),
        daily: daily?.flatten(),
        floor_hours: or(floor_hours, || RawQuiet::default().floor_hours)?,
    })
}

fn read_ha_triple(value: Value<'_>, walk: &mut Walk) -> Option<RawHaTriple> {
    let mut fields = Fields::of(value, "HaTriple", walk)?;
    let domain = fields.required("domain", walk, read_str);
    let service = fields.required("service", walk, read_str);
    let entity_id = fields.optional("entity_id", walk, |value, walk| {
        read_optional(value, walk, read_str)
    });
    let clean = fields.finish(walk);

    clean.then_some(())?;

    Some(RawHaTriple {
        domain: domain?,
        service: service?,
        entity_id: entity_id?.flatten(),
    })
}

fn read_no_fence(value: Value<'_>, walk: &mut Walk) -> Option<RawNoFence> {
    let fields = Fields::of(value, "NoFence", walk)?;

    fields.finish(walk).then_some(RawNoFence {})
}

fn read_ha_call(value: Value<'_>, walk: &mut Walk) -> Option<RawHaCall> {
    let mut fields = Fields::of(value, "HaCallFence", walk)?;
    let allow = fields.required("allow", walk, |value, walk| {
        read_list(value, walk, read_ha_triple)
    });
    let clean = fields.finish(walk);

    clean.then_some(())?;

    Some(RawHaCall { allow: allow? })
}

fn read_enqueue(value: Value<'_>, walk: &mut Walk) -> Option<RawEnqueue> {
    let mut fields = Fields::of(value, "EnqueueFence", walk)?;
    let targets = fields.required("targets", walk, read_strings);
    let clean = fields.finish(walk);

    clean.then_some(())?;

    Some(RawEnqueue { targets: targets? })
}

fn read_release(value: Value<'_>, walk: &mut Walk) -> Option<RawRelease> {
    let mut fields = Fields::of(value, "ReleaseFence", walk)?;
    let components = fields.required("components", walk, read_strings);
    let clean = fields.finish(walk);

    clean.then_some(())?;

    Some(RawRelease {
        components: components?,
    })
}

fn read_verbs(value: Value<'_>, walk: &mut Walk) -> Option<RawVerbs> {
    let mut fields = Fields::of(value, "VerbsBlock", walk)?;
    let embed = fields.optional("embed", walk, |value, walk| {
        read_optional(value, walk, read_no_fence)
    });
    let ha_call = fields.optional("ha_call", walk, |value, walk| {
        read_optional(value, walk, read_ha_call)
    });
    let enqueue = fields.optional("enqueue", walk, |value, walk| {
        read_optional(value, walk, read_enqueue)
    });
    let job_status = fields.optional("job_status", walk, |value, walk| {
        read_optional(value, walk, read_no_fence)
    });
    let release = fields.optional("release", walk, |value, walk| {
        read_optional(value, walk, read_release)
    });
    let clean = fields.finish(walk);

    clean.then_some(())?;

    Some(RawVerbs {
        embed: embed?.flatten(),
        ha_call: ha_call?.flatten(),
        enqueue: enqueue?.flatten(),
        job_status: job_status?.flatten(),
        release: release?.flatten(),
    })
}

/// The Python model `FamilyFile`.
pub(crate) fn read_family(value: Value<'_>, walk: &mut Walk) -> Option<RawFamily> {
    let mut fields = Fields::of(value, "FamilyFile", walk)?;
    let name = fields.required("name", walk, read_str);
    let kind = fields.required("kind", walk, read_str);
    let description = fields.required("description", walk, read_str);
    let model = fields.required("model", walk, read_model_block);
    let files = fields.optional("files", walk, |value, walk| {
        read_list(value, walk, read_mount)
    });
    let tools = fields.optional("tools", walk, |value, walk| {
        read_dict(value, walk, read_grant)
    });
    let verbs = fields.optional("verbs", walk, read_verbs);
    let delegates = fields.optional("delegates", walk, read_strings);
    let inflight = fields.optional("max_inflight_delegations", walk, read_int);
    let egress = fields.optional("egress", walk, read_strings);
    let shell = fields.optional("shell", walk, read_bool);
    let sandbox_tools = fields.optional("sandbox_tools", walk, read_strings);
    let system_prompt = fields.optional("system_prompt", walk, read_str);
    let sandbox = fields.optional("sandbox", walk, read_sandbox);
    let skills = fields.optional("skills", walk, read_strings);
    let approval = fields.optional("approval", walk, read_strings);
    let job = fields.optional("job", walk, |value, walk| {
        read_optional(value, walk, read_job)
    });
    let triggers = fields.optional("triggers", walk, |value, walk| {
        read_optional(value, walk, |value, walk| {
            read_list(value, walk, read_trigger)
        })
    });
    let max_running_turns = fields.optional("max_running_turns", walk, |value, walk| {
        read_optional(value, walk, read_int)
    });
    let quiet = fields.optional("quiet", walk, |value, walk| {
        read_optional(value, walk, read_quiet)
    });
    let clean = fields.finish(walk);

    clean.then_some(())?;

    let base = RawFamily::with_defaults(name?, kind?, description?, model?);

    Some(RawFamily {
        files: or(files, Vec::new)?,
        tools: or(tools, BTreeMap::new)?,
        verbs: or(verbs, RawVerbs::default)?,
        delegates: or(delegates, Vec::new)?,
        max_inflight_delegations: or(inflight, || base.max_inflight_delegations.clone())?,
        egress: or(egress, Vec::new)?,
        shell: or(shell, || false)?,
        sandbox_tools: or(sandbox_tools, || base.sandbox_tools.clone())?,
        system_prompt: or(system_prompt, || base.system_prompt.clone())?,
        sandbox: or(sandbox, RawSandbox::default)?,
        skills: or(skills, Vec::new)?,
        approval: or(approval, Vec::new)?,
        job: job?.flatten(),
        triggers: triggers?.flatten(),
        max_running_turns: max_running_turns?.flatten(),
        quiet: quiet?.flatten(),
        ..base
    })
}

// --- the server file ---

/// The default of `install.python` in the Python model `InstallBlock`.
const DEFAULT_PYTHON: &str = "3.12";

fn read_install(value: Value<'_>, walk: &mut Walk) -> Option<RawInstall> {
    let text = read_optional_str;
    let mut fields = Fields::of(value, "InstallBlock", walk)?;
    let source = fields.required("source", walk, read_str);
    let package = fields.optional("package", walk, text);
    let version = fields.optional("version", walk, text);
    let lock = fields.optional("lock", walk, text);
    let python = fields.optional("python", walk, read_str);
    let repo = fields.optional("repo", walk, text);
    let asset = fields.optional("asset", walk, text);
    let sha256 = fields.optional("sha256", walk, text);
    let git_ref = fields.optional("ref", walk, text);
    let clean = fields.finish(walk);

    clean.then_some(())?;

    Some(RawInstall {
        source: source?,
        package: package?.flatten(),
        version: version?.flatten(),
        lock: lock?.flatten(),
        python: or(python, || DEFAULT_PYTHON.to_owned())?,
        repo: repo?.flatten(),
        asset: asset?.flatten(),
        sha256: sha256?.flatten(),
        git_ref: git_ref?.flatten(),
    })
}

fn read_run(value: Value<'_>, walk: &mut Walk) -> Option<RawRun> {
    let mut fields = Fields::of(value, "RunBlock", walk)?;
    let entrypoint = fields.required("entrypoint", walk, read_str);
    let args = fields.optional("args", walk, read_strings);
    let env = fields.optional("env", walk, |value, walk| read_dict(value, walk, read_str));
    let state_dir = fields.optional("state_dir", walk, read_bool);
    let state_dir_env = fields.optional("state_dir_env", walk, |value, walk| {
        read_optional(value, walk, read_str)
    });
    let clean = fields.finish(walk);

    clean.then_some(())?;

    Some(RawRun {
        entrypoint: entrypoint?,
        args: or(args, Vec::new)?,
        env: or(env, BTreeMap::new)?,
        state_dir: or(state_dir, || false)?,
        state_dir_env: state_dir_env?.flatten(),
    })
}

fn read_tool(value: Value<'_>, walk: &mut Walk) -> Option<RawTool> {
    let mut fields = Fields::of(value, "ToolEntry", walk)?;
    let name = fields.required("name", walk, read_str);
    let description = fields.required("description", walk, read_str);
    let write = fields.optional("write", walk, read_bool);
    let clean = fields.finish(walk);

    clean.then_some(())?;

    Some(RawTool {
        name: name?,
        description: description?,
        write: or(write, || false)?,
    })
}

fn read_fence(value: Value<'_>, walk: &mut Walk) -> Option<RawFence> {
    let mut fields = Fields::of(value, "FenceEntry", walk)?;
    let tools = fields.required("tools", walk, read_grant);
    let arg = fields.required("arg", walk, read_str);
    let values = fields.required("values", walk, read_strings);
    let clean = fields.finish(walk);

    clean.then_some(())?;

    Some(RawFence {
        tools: tools?,
        arg: arg?,
        values: values?,
    })
}

fn read_shared(value: Value<'_>, walk: &mut Walk) -> Option<RawSharedSecret> {
    let mut fields = Fields::of(value, "SharedSecretEntry", walk)?;
    let secret = fields.required("secret", walk, read_str);
    let server = fields.required("server", walk, read_str);
    let clean = fields.finish(walk);

    clean.then_some(())?;

    Some(RawSharedSecret {
        secret: secret?,
        server: server?,
    })
}

/// The Python model `McpServerFile`.
pub(crate) fn read_server(value: Value<'_>, walk: &mut Walk) -> Option<RawServer> {
    let mut fields = Fields::of(value, "McpServerFile", walk)?;
    let name = fields.required("name", walk, read_str);
    let identity = fields.required("identity", walk, read_str);
    let install = fields.required("install", walk, read_install);
    let run = fields.required("run", walk, read_run);
    let tools = fields.optional("tools", walk, |value, walk| {
        read_list(value, walk, read_tool)
    });
    let arg_allows = fields.optional("arg_allows", walk, |value, walk| {
        read_list(value, walk, read_fence)
    });
    let arg_denies = fields.optional("arg_denies", walk, |value, walk| {
        read_list(value, walk, read_fence)
    });
    let shared_secrets = fields.optional("shared_secrets", walk, |value, walk| {
        read_list(value, walk, read_shared)
    });
    let clean = fields.finish(walk);

    clean.then_some(())?;

    Some(RawServer {
        name: name?,
        identity: identity?,
        install: install?,
        run: run?,
        tools: or(tools, Vec::new)?,
        arg_allows: or(arg_allows, Vec::new)?,
        arg_denies: or(arg_denies, Vec::new)?,
        shared_secrets: or(shared_secrets, Vec::new)?,
    })
}
