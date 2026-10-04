//! Nodes to values: a port of the safe constructor of PyYAML 6.0.3
//! (`yaml/constructor.py`).
//!
//! PyYAML makes an empty collection first and fills it later, one level of
//! nesting after the other. This port keeps that order, because the order
//! decides which error the reader reports first.
//!
//! A text can hold a value that this constructor cannot make. This port
//! refuses such a text with [`LoadError::Unreadable`].

use std::collections::HashMap;

use super::compose::{Composer, Content, NodeId, tag};
use super::mark::{Mark, marked};
use super::text::repr_str;
use super::{LoadError, PYTHON_INT_DIGITS};

/// The deepest chain of merge keys that this reader follows.
///
/// CONTRACT-QUESTION: contract 01 gives no limit for a chain of merge keys.
/// This reader calls itself one time for each level, so the reading here is
/// a limit. A family file needs no chain. Another number costs one line
/// here. A chain with no limit costs a function that keeps its own work
/// list.
const MERGE_DEPTH_MAX: usize = 400;

/// The largest count of entries that the merge keys of one text can make.
///
/// CONTRACT-QUESTION: contract 01 gives no limit for the entries that merge
/// keys make. The reading here is a limit, so that a short text cannot make
/// this reader use much time and memory. Another number costs one line
/// here.
const MERGED_ENTRIES_MAX: usize = 100_000;

/// The base of one limb of [`Magnitude`].
const LIMB_BASE: u64 = 1_000_000_000;

/// The count of decimal digits in one limb.
const LIMB_DIGITS: usize = 9;

/// The index of one value in the arena of a loaded text.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub(crate) struct ObjId(usize);

/// The sign of a number.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Sign {
    Plus,
    Minus,
}

impl Sign {
    /// The sign of a text that can start with `-`.
    fn of(text: &str) -> Self {
        if text.starts_with('-') {
            Self::Minus
        } else {
            Self::Plus
        }
    }

    /// The sign of the product of two numbers with these signs.
    fn times(self, other: Self) -> Self {
        if self == other {
            Self::Plus
        } else {
            Self::Minus
        }
    }
}

/// An integer of any size, as Python holds one.
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub(crate) struct Int {
    negative: bool,
    /// The decimal digits, with no zero at the start. Zero is `0`.
    digits: String,
}

impl Int {
    fn new(sign: Sign, digits: String) -> Self {
        let negative = sign == Sign::Minus && digits != "0";

        Self { negative, digits }
    }

    /// The value, when it fits 64 bits with a sign.
    #[must_use]
    pub(crate) fn to_i64(&self) -> Option<i64> {
        self.to_string().parse().ok()
    }
}

impl std::fmt::Display for Int {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        if self.negative {
            f.write_str("-")?;
        }

        f.write_str(&self.digits)
    }
}

/// A date, as `datetime.date` holds one.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub(crate) struct Date {
    pub(crate) year: u32,
    pub(crate) month: u32,
    pub(crate) day: u32,
}

/// A time with a date, as `datetime.datetime` holds one.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub(crate) struct DateTime {
    pub(crate) date: Date,
    pub(crate) hour: u32,
    pub(crate) minute: u32,
    pub(crate) second: u32,
    pub(crate) microsecond: u32,
    /// The offset from UTC in seconds. `None` for a time with no zone.
    pub(crate) offset: Option<i32>,
}

/// One value, as PyYAML gives it to Python.
#[derive(Debug, Clone, PartialEq)]
pub(crate) enum Obj {
    None,
    Bool(bool),
    Int(Int),
    Float(f64),
    /// The one object that PyYAML gives for each plain `.nan`. Python finds a
    /// key by identity first, so two such keys are one key.
    SharedNan,
    Str(String),
    Bytes(Vec<u8>),
    Date(Date),
    DateTime(DateTime),
    List(Vec<ObjId>),
    /// One item of `!!omap` or `!!pairs`: a Python tuple of two values.
    Pair(ObjId, ObjId),
    /// The members in the order of the text. Python has no order for a set.
    Set(Vec<ObjId>),
    /// The entries in the order of the first write of each key.
    Dict(Vec<(ObjId, ObjId)>),
}

/// Every document of one text.
#[derive(Debug, Clone, PartialEq)]
pub(crate) struct Documents {
    objects: Vec<Obj>,
    roots: Vec<ObjId>,
}

/// One value of a loaded text, with the arena that holds its parts.
#[derive(Debug, Clone, Copy)]
pub(crate) struct Value<'a> {
    objects: &'a [Obj],
    id: ObjId,
}

static NONE: Obj = Obj::None;

impl Documents {
    /// The count of documents in the text.
    #[must_use]
    pub(crate) fn count(&self) -> usize {
        self.roots.len()
    }

    /// The first document. `None` for a text with no document.
    #[must_use]
    pub(crate) fn first(&self) -> Option<Value<'_>> {
        let id = *self.roots.first()?;

        Some(Value {
            objects: &self.objects,
            id,
        })
    }
}

impl<'a> Value<'a> {
    #[must_use]
    pub(crate) fn obj(self) -> &'a Obj {
        self.objects.get(self.id.0).unwrap_or(&NONE)
    }

    /// A part of this value.
    #[must_use]
    pub(crate) fn at(self, id: ObjId) -> Value<'a> {
        Value {
            objects: self.objects,
            id,
        }
    }
}

/// How the constructor fills a collection that it made empty.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Fill {
    Seq,
    Map,
    Set,
    /// `!!omap` and `!!pairs`. The text names the tag in an error.
    Pairs(&'static str),
}

#[derive(Debug, Clone, Copy)]
struct Pending {
    node: NodeId,
    obj: ObjId,
    fill: Fill,
}

#[derive(Debug)]
struct Constructor {
    composer: Composer,
    objects: Vec<Obj>,
    constructed: HashMap<NodeId, ObjId>,
    pending: Vec<Pending>,
    merged_entries: usize,
}

/// Every document of `text`, as `list(yaml.safe_load_all(text))` gives them.
pub(super) fn load_all(text: &str) -> Result<Documents, LoadError> {
    let mut constructor = Constructor {
        composer: Composer::new(text)?,
        objects: Vec::new(),
        constructed: HashMap::new(),
        pending: Vec::new(),
        merged_entries: 0,
    };
    let mut roots = Vec::new();
    while constructor.composer.check_node()? {
        let node = constructor.composer.compose_document()?;
        roots.push(constructor.construct_document(node)?);
    }

    if let Some(code) = constructor.composer.lone_surrogate() {
        // CONTRACT-QUESTION: contract 01 §1 says that the file is UTF-8 text.
        // The Python reader makes a string with one lone surrogate from this
        // escape and accepts the file. A Rust `String` cannot hold that
        // value, so this reader refuses the file. To accept the file costs a
        // text type of UTF-16 code units in `Obj` and in each text field of
        // the raw file types.
        return Err(LoadError::Unreadable(format!(
            "the escape for the surrogate U+{code:04X} gives no character"
        )));
    }

    Ok(Documents {
        objects: constructor.objects,
        roots,
    })
}

fn no_value(why: &str) -> LoadError {
    LoadError::Unreadable(why.to_owned())
}

impl Constructor {
    fn add(&mut self, obj: Obj) -> ObjId {
        self.objects.push(obj);

        ObjId(self.objects.len() - 1)
    }

    fn obj(&self, id: ObjId) -> &Obj {
        self.objects.get(id.0).unwrap_or(&NONE)
    }

    fn error(&self, context: Option<(&str, Mark)>, problem: &str, at: Mark) -> LoadError {
        marked(self.composer.buffer(), context, problem, at)
    }

    fn start_of(&self, node: NodeId) -> Mark {
        self.composer.node(node).map_or(
            Mark {
                index: 0,
                line: 0,
                column: 0,
            },
            |node| node.start,
        )
    }

    fn tag_of(&self, node: NodeId) -> &str {
        self.composer.node(node).map_or("", |node| &node.tag)
    }

    fn content_of(&self, node: NodeId) -> Option<&Content> {
        self.composer.node(node).map(|node| &node.content)
    }

    fn construct_document(&mut self, root: NodeId) -> Result<ObjId, LoadError> {
        let data = self.construct_object(root)?;
        while !self.pending.is_empty() {
            for pending in std::mem::take(&mut self.pending) {
                self.fill(pending)?;
            }
        }

        self.constructed.clear();

        Ok(data)
    }

    fn construct_object(&mut self, node: NodeId) -> Result<ObjId, LoadError> {
        if let Some(obj) = self.constructed.get(&node) {
            return Ok(*obj);
        }

        let tag = self.tag_of(node).to_owned();
        let fill = match tag.as_str() {
            tag::SEQ => Some((Obj::List(Vec::new()), Fill::Seq)),
            tag::MAP => Some((Obj::Dict(Vec::new()), Fill::Map)),
            tag::SET => Some((Obj::Set(Vec::new()), Fill::Set)),
            tag::OMAP => Some((Obj::List(Vec::new()), Fill::Pairs("an ordered map"))),
            tag::PAIRS => Some((Obj::List(Vec::new()), Fill::Pairs("pairs"))),
            _ => None,
        };
        let obj = if let Some((empty, fill)) = fill {
            let obj = self.add(empty);
            self.pending.push(Pending { node, obj, fill });
            obj
        } else {
            let value = self.construct_scalar_obj(node, &tag)?;
            self.add(value)
        };
        self.constructed.insert(node, obj);

        Ok(obj)
    }

    fn construct_scalar_obj(&self, node: NodeId, tag: &str) -> Result<Obj, LoadError> {
        match tag {
            tag::NULL => self.construct_scalar(node).map(|_| Obj::None),
            tag::BOOL => construct_bool(&self.construct_scalar(node)?),
            tag::INT => construct_int(&self.construct_scalar(node)?).map(Obj::Int),
            tag::FLOAT => construct_float(&self.construct_scalar(node)?),
            tag::BINARY => self.construct_binary(node),
            tag::TIMESTAMP => construct_timestamp(&self.construct_scalar(node)?),
            tag::STR => self.construct_scalar(node).map(Obj::Str),
            _ => Err(self.error(
                None,
                &format!(
                    "could not determine a constructor for the tag {}",
                    repr_str(tag)
                ),
                self.start_of(node),
            )),
        }
    }

    /// `SafeConstructor.construct_scalar`: the text of a scalar node, or the
    /// value of the `=` key of a mapping node.
    fn construct_scalar(&self, node: NodeId) -> Result<String, LoadError> {
        let mut node = node;
        loop {
            match self.content_of(node) {
                Some(Content::Scalar(value)) => return Ok(value.clone()),
                Some(Content::Mapping(pairs)) => {
                    let default = pairs
                        .iter()
                        .find(|(key, _)| self.tag_of(*key) == tag::VALUE);
                    if let Some((_, value)) = default {
                        node = *value;
                        continue;
                    }
                }
                _ => {}
            }

            let id = self.content_of(node).map_or("scalar", Content::id);

            return Err(self.error(
                None,
                &format!("expected a scalar node, but found {id}"),
                self.start_of(node),
            ));
        }
    }

    fn construct_binary(&self, node: NodeId) -> Result<Obj, LoadError> {
        let text = self.construct_scalar(node)?;
        if let Some(problem) = ascii_problem(&text) {
            return Err(self.error(
                None,
                &format!("failed to convert base64 data into ascii: {problem}"),
                self.start_of(node),
            ));
        }

        decode_base64(text.as_bytes())
            .map(Obj::Bytes)
            .map_err(|problem| {
                self.error(
                    None,
                    &format!("failed to decode base64 data: {problem}"),
                    self.start_of(node),
                )
            })
    }

    fn fill(&mut self, pending: Pending) -> Result<(), LoadError> {
        let Pending { node, obj, fill } = pending;
        let filled = match fill {
            Fill::Seq => Obj::List(self.construct_sequence(node)?),
            Fill::Map => Obj::Dict(self.construct_mapping(node)?),
            Fill::Set => {
                let keys = self.construct_mapping(node)?;
                Obj::Set(keys.into_iter().map(|(key, _)| key).collect())
            }
            Fill::Pairs(what) => Obj::List(self.construct_pairs(node, what)?),
        };
        if let Some(slot) = self.objects.get_mut(obj.0) {
            *slot = filled;
        }

        Ok(())
    }

    fn construct_sequence(&mut self, node: NodeId) -> Result<Vec<ObjId>, LoadError> {
        let Some(Content::Sequence(items)) = self.content_of(node) else {
            let id = self.content_of(node).map_or("scalar", Content::id);

            return Err(self.error(
                None,
                &format!("expected a sequence node, but found {id}"),
                self.start_of(node),
            ));
        };

        items
            .clone()
            .into_iter()
            .map(|item| self.construct_object(item))
            .collect()
    }

    fn construct_mapping(&mut self, node: NodeId) -> Result<Vec<(ObjId, ObjId)>, LoadError> {
        if matches!(self.content_of(node), Some(Content::Mapping(_))) {
            self.flatten_mapping(node, 0)?;
        }

        let Some(Content::Mapping(pairs)) = self.content_of(node) else {
            let id = self.content_of(node).map_or("scalar", Content::id);

            return Err(self.error(
                None,
                &format!("expected a mapping node, but found {id}"),
                self.start_of(node),
            ));
        };

        let mut mapping: Vec<(ObjId, ObjId)> = Vec::new();
        // The place of each key in `mapping`. A scan of `mapping` for each
        // key takes a time that grows with the square of the count of keys.
        let mut places: HashMap<KeyClass, usize> = HashMap::new();
        for (key_node, value_node) in pairs.clone() {
            let key = self.construct_object(key_node)?;
            if matches!(self.obj(key), Obj::List(_) | Obj::Dict(_) | Obj::Set(_)) {
                return Err(self.error(
                    Some(("while constructing a mapping", self.start_of(node))),
                    "found unhashable key",
                    self.start_of(key_node),
                ));
            }

            let value = self.construct_object(value_node)?;
            let class = key_class(self.obj(key));
            let known = class
                .as_ref()
                .and_then(|class| places.get(class))
                .and_then(|place| mapping.get_mut(*place));
            if let Some(entry) = known {
                entry.1 = value;
                continue;
            }

            if let Some(class) = class {
                places.insert(class, mapping.len());
            }

            mapping.push((key, value));
        }

        Ok(mapping)
    }

    fn construct_pairs(&mut self, node: NodeId, what: &str) -> Result<Vec<ObjId>, LoadError> {
        let context = format!("while constructing {what}");
        let start = self.start_of(node);
        let Some(Content::Sequence(items)) = self.content_of(node) else {
            let id = self.content_of(node).map_or("scalar", Content::id);

            return Err(self.error(
                Some((&context, start)),
                &format!("expected a sequence, but found {id}"),
                start,
            ));
        };

        let mut pairs = Vec::new();
        for item in items.clone() {
            let Some(Content::Mapping(entries)) = self.content_of(item) else {
                let id = self.content_of(item).map_or("scalar", Content::id);

                return Err(self.error(
                    Some((&context, start)),
                    &format!("expected a mapping of length 1, but found {id}"),
                    self.start_of(item),
                ));
            };

            let &[(key_node, value_node)] = entries.as_slice() else {
                return Err(self.error(
                    Some((&context, start)),
                    &format!(
                        "expected a single mapping item, but found {} items",
                        entries.len()
                    ),
                    self.start_of(item),
                ));
            };

            let key = self.construct_object(key_node)?;
            let value = self.construct_object(value_node)?;
            pairs.push(self.add(Obj::Pair(key, value)));
        }

        Ok(pairs)
    }

    fn mapping_pairs(&self, node: NodeId) -> Option<Vec<(NodeId, NodeId)>> {
        match self.content_of(node) {
            Some(Content::Mapping(pairs)) => Some(pairs.clone()),
            _ => None,
        }
    }

    fn count_merged(&mut self, added: usize) -> Result<(), LoadError> {
        self.merged_entries = self.merged_entries.saturating_add(added);
        if self.merged_entries > MERGED_ENTRIES_MAX {
            return Err(no_value("the merge keys make too many entries"));
        }

        Ok(())
    }

    /// `flatten_mapping`: puts the entries of each `<<` key into the mapping.
    fn flatten_mapping(&mut self, node: NodeId, depth: usize) -> Result<(), LoadError> {
        if depth > MERGE_DEPTH_MAX {
            return Err(no_value("the merge keys nest too deep"));
        }

        let mut merge: Vec<(NodeId, NodeId)> = Vec::new();
        let mut index = 0;
        loop {
            let entry = match self.content_of(node) {
                Some(Content::Mapping(pairs)) => pairs.get(index).copied(),
                _ => None,
            };
            let Some((key_node, value_node)) = entry else {
                break;
            };

            let key_tag = self.tag_of(key_node);
            if key_tag == tag::VALUE {
                if let Some(key) = self.composer.node_mut(key_node) {
                    tag::STR.clone_into(&mut key.tag);
                }

                index += 1;
                continue;
            }

            if key_tag != tag::MERGE {
                index += 1;
                continue;
            }

            if let Some(Content::Mapping(pairs)) =
                self.composer.node_mut(node).map(|node| &mut node.content)
            {
                pairs.remove(index);
            }

            self.merge_value(node, value_node, depth, &mut merge)?;
        }

        if merge.is_empty() {
            return Ok(());
        }

        if let Some(Content::Mapping(pairs)) =
            self.composer.node_mut(node).map(|node| &mut node.content)
        {
            merge.append(pairs);
            *pairs = merge;
        }

        Ok(())
    }

    /// The entries that one `<<` key adds: those of one mapping, or of each
    /// mapping of a sequence, the last one first.
    fn merge_value(
        &mut self,
        node: NodeId,
        value_node: NodeId,
        depth: usize,
        merge: &mut Vec<(NodeId, NodeId)>,
    ) -> Result<(), LoadError> {
        let context = ("while constructing a mapping", self.start_of(node));
        match self.content_of(value_node).cloned() {
            Some(Content::Mapping(_)) => {
                self.flatten_mapping(value_node, depth + 1)?;
                let pairs = self.mapping_pairs(value_node).unwrap_or_default();
                self.count_merged(pairs.len())?;
                merge.extend(pairs);
            }
            Some(Content::Sequence(items)) => {
                let mut submerge: Vec<Vec<(NodeId, NodeId)>> = Vec::new();
                for subnode in items {
                    if self.mapping_pairs(subnode).is_none() {
                        let id = self.content_of(subnode).map_or("scalar", Content::id);

                        return Err(self.error(
                            Some(context),
                            &format!("expected a mapping for merging, but found {id}"),
                            self.start_of(subnode),
                        ));
                    }

                    self.flatten_mapping(subnode, depth + 1)?;
                    let pairs = self.mapping_pairs(subnode).unwrap_or_default();
                    self.count_merged(pairs.len())?;
                    submerge.push(pairs);
                }

                submerge.reverse();
                merge.extend(submerge.into_iter().flatten());
            }
            other => {
                let id = other.as_ref().map_or("scalar", Content::id);

                return Err(self.error(
                    Some(context),
                    &format!("expected a mapping or list of mappings for merging, but found {id}"),
                    self.start_of(value_node),
                ));
            }
        }

        Ok(())
    }
}

// --- keys ---

/// The exact decimal digits of a float with no fraction.
fn whole_digits(value: f64) -> Option<Int> {
    if !value.is_finite() || value.fract() != 0.0 {
        return None;
    }

    let text = format!("{value:.0}");
    let digits = text.strip_prefix('-').unwrap_or(&text);

    Some(Int::new(Sign::of(&text), digits.to_owned()))
}

/// The number that Python compares for a key: an integer, a boolean and a
/// float with no fraction are equal when their values are equal.
fn whole_number(obj: &Obj) -> Option<Int> {
    match obj {
        Obj::Bool(value) => Some(Int::new(Sign::Plus, u8::from(*value).to_string())),
        Obj::Int(value) => Some(value.clone()),
        Obj::Float(value) => whole_digits(*value),
        _ => None,
    }
}

/// The count of days from 1970-01-01 to a date.
fn days_from_epoch(date: Date) -> i64 {
    let year = i64::from(date.year) - i64::from(date.month <= 2);
    let era = year.div_euclid(400);
    let year_of_era = year.rem_euclid(400);
    let month = i64::from(date.month);
    let shifted = if month > 2 { month - 3 } else { month + 9 };
    let day_of_year = (153 * shifted + 2) / 5 + i64::from(date.day) - 1;
    let day_of_era = year_of_era * 365 + year_of_era / 4 - year_of_era / 100 + day_of_year;

    era * 146_097 + day_of_era - 719_468
}

/// The instant of a time with a zone, in microseconds from 1970.
fn instant(value: DateTime, offset: i32) -> i64 {
    let seconds = days_from_epoch(value.date) * 86_400
        + i64::from(value.hour) * 3_600
        + i64::from(value.minute) * 60
        + i64::from(value.second)
        - i64::from(offset);

    seconds * 1_000_000 + i64::from(value.microsecond)
}

/// What decides whether Python takes two keys of a `dict` as one key. Two
/// keys are one key when their classes are equal.
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
enum KeyClass {
    None,
    SharedNan,
    Str(String),
    Bytes(Vec<u8>),
    /// A boolean, an integer and a float with no fraction: the number.
    Whole(Int),
    /// A float with a fraction, and an infinity: the bits of the float.
    Float(u64),
    Date(Date),
    /// A time with no zone: each field.
    Naive(DateTime),
    /// A time with a zone: the instant in microseconds from 1970.
    Instant(i64),
}

/// The class of a key. `None` for a value that is equal to no key, also not
/// to itself: a float that is not a number.
fn key_class(obj: &Obj) -> Option<KeyClass> {
    match obj {
        Obj::None => Some(KeyClass::None),
        Obj::SharedNan => Some(KeyClass::SharedNan),
        Obj::Str(text) => Some(KeyClass::Str(text.clone())),
        Obj::Bytes(bytes) => Some(KeyClass::Bytes(bytes.clone())),
        Obj::Date(date) => Some(KeyClass::Date(*date)),
        Obj::DateTime(time) => Some(match time.offset {
            None => KeyClass::Naive(*time),
            Some(offset) => KeyClass::Instant(instant(*time, offset)),
        }),
        Obj::Float(value) if value.is_nan() => None,
        Obj::Bool(_) | Obj::Int(_) | Obj::Float(_) => Some(match (whole_number(obj), obj) {
            (Some(number), _) => KeyClass::Whole(number),
            (None, Obj::Float(value)) => KeyClass::Float(value.to_bits()),
            (None, _) => return None,
        }),
        Obj::List(_) | Obj::Pair(..) | Obj::Set(_) | Obj::Dict(_) => None,
    }
}

// --- scalars ---

/// The spaces that Python strips from a text before it reads a number.
fn py_strip(text: &str) -> &str {
    text.trim_matches(|c: char| c.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&c))
}

fn construct_bool(value: &str) -> Result<Obj, LoadError> {
    match value.to_lowercase().as_str() {
        "yes" | "true" | "on" => Ok(Obj::Bool(true)),
        "no" | "false" | "off" => Ok(Obj::Bool(false)),
        _ => Err(no_value("a value with the tag bool is not a boolean word")),
    }
}

/// A natural number of any size: limbs of nine decimal digits, the lowest
/// limb first.
#[derive(Debug, Clone, PartialEq, Eq)]
struct Magnitude(Vec<u64>);

impl Magnitude {
    fn zero() -> Self {
        Self(Vec::new())
    }

    /// `self * factor + addend`. Both numbers are below [`LIMB_BASE`].
    fn mul_add(&mut self, factor: u64, addend: u64) {
        let mut carry = addend;
        for limb in &mut self.0 {
            let product = *limb * factor + carry;
            *limb = product % LIMB_BASE;
            carry = product / LIMB_BASE;
        }

        if carry > 0 {
            self.0.push(carry);
        }
    }

    fn too_long(&self) -> bool {
        self.0.len() > PYTHON_INT_DIGITS / LIMB_DIGITS + 1
    }

    fn digits(&self) -> String {
        let mut limbs = self.0.iter().rev();
        let Some(first) = limbs.next() else {
            return "0".to_owned();
        };

        let mut out = first.to_string();
        for limb in limbs {
            out.push_str(&format!("{limb:0LIMB_DIGITS$}"));
        }

        out
    }
}

fn too_long() -> LoadError {
    no_value("an integer has more than 4300 digits")
}

/// `int(text, base)` for the digits of one number, with no sign.
fn magnitude_of(text: &str, base: u32) -> Result<Magnitude, LoadError> {
    if text.is_empty() {
        return Err(no_value("an integer has no digit"));
    }

    let mut value = Magnitude::zero();
    for c in text.chars() {
        let Some(digit) = c.to_digit(base) else {
            return Err(no_value("an integer holds a character that is no digit"));
        };

        value.mul_add(u64::from(base), u64::from(digit));
        if value.too_long() {
            return Err(too_long());
        }
    }

    Ok(value)
}

/// `int(text, base)`, as Python reads a text that has no underscore.
///
/// CONTRACT-QUESTION: contract 01 names no form of a number. Python also
/// reads a decimal digit that is not ASCII, for example with an integer tag
/// on a quoted text. This reader refuses such a digit. To read such a digit
/// costs a table of the decimal digits of Unicode here.
fn py_int(text: &str, base: u32) -> Result<(Sign, Magnitude), LoadError> {
    let text = py_strip(text);
    let sign = Sign::of(text);
    let text = text.strip_prefix(['+', '-']).unwrap_or(text);
    let prefixes: &[&str] = match base {
        2 => &["0b", "0B"],
        8 => &["0o", "0O"],
        16 => &["0x", "0X"],
        _ => &[],
    };
    let digits = prefixes
        .iter()
        .find_map(|prefix| text.strip_prefix(prefix))
        .unwrap_or(text);
    if base == 10 && digits.len() > PYTHON_INT_DIGITS {
        return Err(too_long());
    }

    Ok((sign, magnitude_of(digits, base)?))
}

fn checked_int(sign: Sign, value: &Magnitude) -> Result<Int, LoadError> {
    let digits = value.digits();
    if digits.len() > PYTHON_INT_DIGITS {
        return Err(too_long());
    }

    Ok(Int::new(sign, digits))
}

/// `construct_yaml_int`.
fn construct_int(value: &str) -> Result<Int, LoadError> {
    let value = value.replace('_', "");
    let sign = Sign::of(&value);
    let value = value.strip_prefix(['+', '-']).unwrap_or(value.as_str());
    if value.is_empty() {
        return Err(no_value("an integer has no digit"));
    }

    if value == "0" {
        return Ok(Int::new(Sign::Plus, "0".to_owned()));
    }

    let (inner_sign, magnitude) = if let Some(digits) = value.strip_prefix("0b") {
        py_int(digits, 2)?
    } else if let Some(digits) = value.strip_prefix("0x") {
        py_int(digits, 16)?
    } else if value.starts_with('0') {
        py_int(value, 8)?
    } else if value.contains(':') {
        return base60_int(sign, value);
    } else {
        py_int(value, 10)?
    };

    checked_int(sign.times(inner_sign), &magnitude)
}

/// A base 60 integer: `1:30` is 90.
fn base60_int(sign: Sign, value: &str) -> Result<Int, LoadError> {
    let mut total = Magnitude::zero();
    let mut total_sign = Sign::Plus;
    for (position, part) in value.split(':').enumerate() {
        let (part_sign, magnitude) = py_int(part, 10)?;
        if position == 0 {
            total = magnitude;
            total_sign = part_sign;
            continue;
        }

        // Only the first part can be large. The pattern of the resolver
        // gives each later part two digits at most.
        let small = magnitude.digits().parse::<u64>().ok();
        let Some(small) = small.filter(|small| *small < LIMB_BASE && part_sign == Sign::Plus)
        else {
            return Err(no_value("a part of a base 60 integer is not below 60"));
        };

        if total_sign == Sign::Minus {
            return Err(no_value("a part of a base 60 integer has a sign"));
        }

        total.mul_add(60, small);
        if total.too_long() {
            return Err(too_long());
        }
    }

    checked_int(sign.times(total_sign), &total)
}

/// `float(text)`, as Python reads a text that has no underscore.
///
/// CONTRACT-QUESTION: as for [`py_int`], this reader refuses a decimal digit
/// that is not ASCII. A change costs the same table.
fn py_float(text: &str) -> Result<f64, LoadError> {
    let text = py_strip(text);
    let unsigned = text.strip_prefix(['+', '-']).unwrap_or(text);
    let word = unsigned.to_ascii_lowercase();
    let number = unsigned
        .bytes()
        .all(|byte| byte.is_ascii_digit() || matches!(byte, b'.' | b'e' | b'E' | b'+' | b'-'));
    if !number && !matches!(word.as_str(), "inf" | "infinity" | "nan") {
        return Err(no_value("a float holds a character that is no digit"));
    }

    text.parse()
        .map_err(|_| no_value("a float is not a decimal number"))
}

/// `construct_yaml_float`.
fn construct_float(value: &str) -> Result<Obj, LoadError> {
    let value = value.replace('_', "").to_lowercase();
    let negative = value.starts_with('-');
    let value = value.strip_prefix(['+', '-']).unwrap_or(value.as_str());
    if value.is_empty() {
        return Err(no_value("a float has no digit"));
    }

    let sign = if negative { -1.0 } else { 1.0 };
    if value == ".inf" {
        return Ok(Obj::Float(sign * f64::INFINITY));
    }

    if value == ".nan" {
        return Ok(Obj::SharedNan);
    }

    if !value.contains(':') {
        return Ok(Obj::Float(sign * py_float(value)?));
    }

    let mut total = 0.0;
    let mut base = Magnitude(vec![1]);
    for part in value.rsplit(':') {
        let digit = py_float(part)?;
        // Python multiplies the float by the exact power of 60. That power
        // is the nearest float, or an error when it is past the last float.
        let power: f64 = base.digits().parse().unwrap_or(f64::INFINITY);
        if power.is_infinite() {
            return Err(no_value("a base 60 float has too many parts"));
        }

        total += digit * power;
        base.mul_add(60, 0);
    }

    Ok(Obj::Float(sign * total))
}

// --- binary ---

/// The text of the Python error for a character that is not ASCII, or
/// `None` for an ASCII text.
fn ascii_problem(text: &str) -> Option<String> {
    let chars: Vec<char> = text.chars().collect();
    let first = chars.iter().position(|c| !c.is_ascii())?;
    let run = chars
        .iter()
        .skip(first)
        .take_while(|c| !c.is_ascii())
        .count();
    if run > 1 {
        return Some(format!(
            "'ascii' codec can't encode characters in position {first}-{}: ordinal not in \
             range(128)",
            first + run - 1
        ));
    }

    let code = chars.get(first).map_or(0, |c| u32::from(*c));
    let shown = if code <= 0xff {
        format!("\\x{code:02x}")
    } else if code <= 0xffff {
        format!("\\u{code:04x}")
    } else {
        format!("\\U{code:08x}")
    };

    Some(format!(
        "'ascii' codec can't encode character '{shown}' in position {first}: ordinal not in \
         range(128)"
    ))
}

fn base64_value(byte: u8) -> Option<u8> {
    match byte {
        b'A'..=b'Z' => Some(byte - b'A'),
        b'a'..=b'z' => Some(byte - b'a' + 26),
        b'0'..=b'9' => Some(byte - b'0' + 52),
        b'+' => Some(62),
        b'/' => Some(63),
        _ => None,
    }
}

/// `binascii.a2b_base64(data)` with its default, which is not strict: a
/// byte outside the alphabet is skipped. The error text is the Python text.
///
/// CONTRACT-QUESTION: contract 01 names no binary value. Python 3.12 stops
/// at the first complete padding. Python 3.13 and 3.14 read on, and this
/// function does the same. To read as Python 3.12 reads costs one early
/// return in the loop below.
fn decode_base64(data: &[u8]) -> Result<Vec<u8>, String> {
    let mut out = Vec::new();
    let mut quad_pos = 0_u8;
    let mut left = 0_u8;
    let mut pads = 0_u8;
    for byte in data {
        if *byte == b'=' {
            if quad_pos >= 2 {
                pads = pads.saturating_add(1).min(4);
            }

            continue;
        }

        let Some(value) = base64_value(*byte) else {
            continue;
        };

        pads = 0;
        match quad_pos {
            0 => left = value,
            1 => {
                out.push((left << 2) | (value >> 4));
                left = value & 0x0f;
            }
            2 => {
                out.push((left << 4) | (value >> 2));
                left = value & 0x03;
            }
            _ => out.push((left << 6) | value),
        }

        quad_pos = (quad_pos + 1) % 4;
    }

    if quad_pos == 0 || quad_pos + pads >= 4 {
        return Ok(out);
    }

    if quad_pos == 1 {
        return Err(format!(
            "Invalid base64-encoded string: number of data characters ({}) cannot be 1 more than \
             a multiple of 4",
            out.len() / 3 * 4 + 1
        ));
    }

    Err("Incorrect padding".to_owned())
}

// --- timestamps ---

fn take_number(text: &[u8], least: usize, most: usize) -> Option<(u32, &[u8])> {
    let count = text
        .iter()
        .take(most)
        .take_while(|byte| byte.is_ascii_digit())
        .count();
    if count < least {
        return None;
    }

    let (digits, rest) = text.split_at_checked(count)?;
    let value = digits
        .iter()
        .fold(0_u32, |value, byte| value * 10 + u32::from(byte - b'0'));

    Some((value, rest))
}

fn take_byte(text: &[u8], wanted: u8) -> Option<&[u8]> {
    match text.split_first() {
        Some((byte, rest)) if *byte == wanted => Some(rest),
        _ => None,
    }
}

fn days_in_month(year: u32, month: u32) -> u32 {
    let leap = (year.is_multiple_of(4) && !year.is_multiple_of(100)) || year.is_multiple_of(400);
    match month {
        2 if leap => 29,
        2 => 28,
        4 | 6 | 9 | 11 => 30,
        _ => 31,
    }
}

fn checked_date(year: u32, month: u32, day: u32) -> Option<Date> {
    let valid =
        year >= 1 && (1..=12).contains(&month) && (1..=days_in_month(year, month)).contains(&day);

    valid.then_some(Date { year, month, day })
}

/// The zone of a timestamp: `Z`, or a sign with hours and optional minutes.
/// The answer is the offset in seconds and the rest of the text.
fn take_zone(text: &[u8]) -> Option<(i32, &[u8])> {
    if let Some(rest) = take_byte(text, b'Z') {
        return Some((0, rest));
    }

    let (sign, rest) = text.split_first()?;
    let sign = match sign {
        b'+' => 1,
        b'-' => -1,
        _ => return None,
    };
    let (hours, rest) = take_number(rest, 1, 2)?;
    let (minutes, rest) = match take_byte(rest, b':').and_then(|rest| take_number(rest, 2, 2)) {
        Some((minutes, rest)) => (minutes, rest),
        None => (0, rest),
    };
    let seconds = i32::try_from(hours * 3_600 + minutes * 60).ok()?;

    Some((sign * seconds, rest))
}

/// `construct_yaml_timestamp`. A text that the Python pattern refuses, and
/// a date that the calendar does not hold, has no value.
fn construct_timestamp(value: &str) -> Result<Obj, LoadError> {
    let not_a_time = || no_value("a value with the tag timestamp is no date and no time");
    let text = value.strip_suffix('\n').unwrap_or(value).as_bytes();
    let parsed = (|| {
        let (year, rest) = take_number(text, 4, 4)?;
        let (month, rest) = take_number(take_byte(rest, b'-')?, 1, 2)?;
        let (day, rest) = take_number(take_byte(rest, b'-')?, 1, 2)?;

        Some((year, month, day, rest))
    })();
    let Some((year, month, day, rest)) = parsed else {
        return Err(not_a_time());
    };

    let date = checked_date(year, month, day).ok_or_else(not_a_time)?;
    if rest.is_empty() {
        return Ok(Obj::Date(date));
    }

    let time = (|| {
        let rest = match rest.split_first()? {
            (b'T' | b't', rest) => rest,
            (b' ' | b'\t', _) => {
                let blanks = rest
                    .iter()
                    .take_while(|byte| matches!(byte, b' ' | b'\t'))
                    .count();
                rest.get(blanks..)?
            }
            _ => return None,
        };
        let (hour, rest) = take_number(rest, 1, 2)?;
        let (minute, rest) = take_number(take_byte(rest, b':')?, 2, 2)?;
        let (second, rest) = take_number(take_byte(rest, b':')?, 2, 2)?;
        let (fraction, rest) = match take_byte(rest, b'.') {
            Some(after) => {
                let count = after
                    .iter()
                    .take_while(|byte| byte.is_ascii_digit())
                    .count();
                after.split_at_checked(count)?
            }
            None => (b"".as_slice(), rest),
        };
        let microsecond = (0..6).fold(0_u32, |value, position| {
            let digit = fraction
                .get(position)
                .map_or(0, |byte| u32::from(byte - b'0'));
            value * 10 + digit
        });
        let blanks = rest
            .iter()
            .take_while(|byte| matches!(byte, b' ' | b'\t'))
            .count();
        let zone_text = rest.get(blanks..)?;
        let offset = if rest.is_empty() {
            None
        } else {
            let (offset, after) = take_zone(zone_text)?;
            if !after.is_empty() {
                return None;
            }

            Some(offset)
        };

        Some((hour, minute, second, microsecond, offset))
    })();
    let Some((hour, minute, second, microsecond, offset)) = time else {
        return Err(not_a_time());
    };

    // Python refuses an offset of 24 hours or more.
    let offset_valid = offset.is_none_or(|offset| offset.abs() < 86_400);
    if hour > 23 || minute > 59 || second > 59 || !offset_valid {
        return Err(not_a_time());
    }

    Ok(Obj::DateTime(DateTime {
        date,
        hour,
        minute,
        second,
        microsecond,
        offset,
    }))
}

#[cfg(test)]
mod tests {
    use super::{
        Date, DateTime, Obj, construct_float, construct_int, construct_timestamp, decode_base64,
        instant, key_class, load_all, whole_number,
    };

    fn int_text(value: &str) -> Option<String> {
        construct_int(value).ok().map(|int| int.to_string())
    }

    fn same_datetime(a: DateTime, b: DateTime) -> bool {
        match (a.offset, b.offset) {
            (None, None) => a == b,
            (Some(first), Some(second)) => instant(a, first) == instant(b, second),
            _ => false,
        }
    }

    /// Whether Python takes two keys of a `dict` as one key: `a == b`, value
    /// by value.
    fn same_key(a: &Obj, b: &Obj) -> bool {
        match (a, b) {
            (Obj::None, Obj::None) | (Obj::SharedNan, Obj::SharedNan) => true,
            (Obj::Str(a), Obj::Str(b)) => a == b,
            (Obj::Bytes(a), Obj::Bytes(b)) => a == b,
            (Obj::Date(a), Obj::Date(b)) => a == b,
            (Obj::DateTime(a), Obj::DateTime(b)) => same_datetime(*a, *b),
            (Obj::Float(a), Obj::Float(b)) => a == b,
            _ => match (whole_number(a), whole_number(b)) {
                (Some(a), Some(b)) => a == b,
                _ => false,
            },
        }
    }

    /// The class of a key decides the same as a comparison of two keys.
    #[test]
    fn two_keys_have_one_class_when_python_takes_them_as_one_key() {
        let date = Date {
            year: 2026,
            month: 10,
            day: 3,
        };
        let time = |hour: u32, offset: Option<i32>| {
            Obj::DateTime(DateTime {
                date,
                hour,
                minute: 0,
                second: 0,
                microsecond: 0,
                offset,
            })
        };
        let int = |text: &str| Obj::Int(construct_int(text).unwrap());
        let keys = [
            Obj::None,
            Obj::SharedNan,
            Obj::Bool(false),
            Obj::Bool(true),
            int("0"),
            int("1"),
            int("-1"),
            int("2"),
            int("99999999999999999999999"),
            Obj::Float(0.0),
            Obj::Float(-0.0),
            Obj::Float(1.0),
            Obj::Float(-1.0),
            Obj::Float(1.5),
            Obj::Float(-1.5),
            Obj::Float(1e23),
            Obj::Float(f64::INFINITY),
            Obj::Float(f64::NEG_INFINITY),
            Obj::Float(f64::NAN),
            Obj::Str(String::new()),
            Obj::Str("1".to_owned()),
            Obj::Str("a".to_owned()),
            Obj::Bytes(Vec::new()),
            Obj::Bytes(b"a".to_vec()),
            Obj::Bytes(b"1".to_vec()),
            Obj::Date(date),
            Obj::Date(Date { day: 4, ..date }),
            time(0, None),
            time(1, None),
            time(0, Some(0)),
            time(1, Some(3600)),
            time(1, Some(0)),
            time(2, Some(3600)),
        ];
        for a in &keys {
            for b in &keys {
                let one_class = key_class(a).is_some_and(|class| Some(class) == key_class(b));
                assert_eq!(one_class, same_key(a, b), "{a:?} {b:?}");
            }
        }
    }

    #[test]
    fn a_key_that_a_mapping_holds_two_times_keeps_its_first_place() {
        let documents = load_all("b: 1\na: 2\n1: 3\nb: 4\ntrue: 5\n1.0: 6\n").unwrap();
        let root = documents.first().unwrap();
        let Obj::Dict(entries) = root.obj() else {
            panic!("the document is no mapping");
        };

        let shown: Vec<(&Obj, &Obj)> = entries
            .iter()
            .map(|(key, value)| (root.at(*key).obj(), root.at(*value).obj()))
            .collect();
        let int = |text: &str| Obj::Int(construct_int(text).unwrap());
        assert_eq!(
            shown,
            [
                (&Obj::Str("b".to_owned()), &int("4")),
                (&Obj::Str("a".to_owned()), &int("2")),
                (&int("1"), &int("6")),
            ]
        );
    }

    #[test]
    fn an_integer_is_the_python_integer() {
        let cases = [
            ("0", "0"),
            ("-0", "0"),
            ("12", "12"),
            ("+12", "12"),
            ("-1_000", "-1000"),
            ("010", "8"),
            ("0_7", "7"),
            ("0b101", "5"),
            ("-0x1F", "-31"),
            ("1:30", "90"),
            ("-1:00:00", "-3600"),
            ("99999999999999999999999", "99999999999999999999999"),
            ("0xFFFFFFFFFFFFFFFFFFFF", "1208925819614629174706175"),
            ("1_0:5", "605"),
        ];
        for (text, wanted) in cases {
            assert_eq!(int_text(text).as_deref(), Some(wanted), "{text}");
        }
    }

    #[test]
    fn a_text_that_is_no_integer_is_refused() {
        for text in ["", "abc", "08", "1:x"] {
            assert_eq!(int_text(text), None, "{text}");
        }

        assert_eq!(
            int_text(&"9".repeat(4300)).map(|text| text.len()),
            Some(4300)
        );
    }

    #[test]
    fn a_float_is_the_python_float() {
        let cases = [
            ("1.5", 1.5),
            ("-1.5", -1.5),
            ("1.", 1.0),
            (".5", 0.5),
            ("1_0.5", 10.5),
            ("1.0E+2", 100.0),
            ("1:30.5", 90.5),
            ("-1:00.", -60.0),
            (".inf", f64::INFINITY),
            ("-.INF", f64::NEG_INFINITY),
        ];
        for (text, wanted) in cases {
            assert_eq!(
                construct_float(text).ok(),
                Some(Obj::Float(wanted)),
                "{text}"
            );
        }

        assert_eq!(construct_float(".NaN").ok(), Some(Obj::SharedNan));
        assert_eq!(construct_float("abc").ok(), None);
        assert_eq!(construct_float("").ok(), None);
    }

    #[test]
    fn a_timestamp_is_the_python_date_or_time() {
        let date = Date {
            year: 2026,
            month: 10,
            day: 3,
        };
        assert_eq!(
            construct_timestamp("2026-10-03").ok(),
            Some(Obj::Date(date))
        );
        assert_eq!(construct_timestamp("2026-10-3").ok(), Some(Obj::Date(date)));
        assert_eq!(
            construct_timestamp("2026-10-03 1:02:03.5 -05:30").ok(),
            Some(Obj::DateTime(DateTime {
                date,
                hour: 1,
                minute: 2,
                second: 3,
                microsecond: 500_000,
                offset: Some(-19_800),
            }))
        );
        assert_eq!(
            construct_timestamp("2026-10-03T01:02:03Z").ok(),
            Some(Obj::DateTime(DateTime {
                date,
                hour: 1,
                minute: 2,
                second: 3,
                microsecond: 0,
                offset: Some(0),
            }))
        );
        assert_eq!(construct_timestamp("soon").ok(), None);
    }

    #[test]
    fn base64_is_the_python_decoder() {
        assert_eq!(decode_base64(b"aGVsbG8="), Ok(b"hello".to_vec()));
        assert_eq!(decode_base64(b"aGVs\nbG8=\n"), Ok(b"hello".to_vec()));
        assert_eq!(
            decode_base64(b"aGVsbG8"),
            Err("Incorrect padding".to_owned())
        );
        assert_eq!(decode_base64(b""), Ok(Vec::new()));
        assert_eq!(decode_base64(b"=="), Ok(Vec::new()));
        assert_eq!(decode_base64(b"aGVsbG8=a"), Ok(b"hello\x1a".to_vec()));
        assert_eq!(decode_base64(b"aG=VsbG8="), Ok(b"hello".to_vec()));
        assert_eq!(decode_base64(b"aG="), Err("Incorrect padding".to_owned()));
        assert_eq!(
            decode_base64(b"aGVsbG8=aGVs"),
            Err("Incorrect padding".to_owned())
        );
        assert_eq!(
            decode_base64(b"a"),
            Err(
                "Invalid base64-encoded string: number of data characters (1) cannot be 1 more \
                 than a multiple of 4"
                    .to_owned()
            )
        );
    }
}
