//! Events to a graph of nodes: a port of the composer and of the resolver of
//! PyYAML 6.0.3 (`yaml/composer.py`, `yaml/resolver.py`).
//!
//! The resolver is YAML 1.1. It gives a plain scalar its tag: `yes` is a
//! boolean, `010` is an octal integer and `1:30` is a base 60 integer. Each
//! pattern of the Python resolver is one function here that reads bytes.

use std::collections::HashMap;

use super::LoadError;
use super::mark::{Mark, marked};
use super::parser::{EventKind, Parser};
use super::text::repr_str;

/// The deepest nesting of collections that this reader composes.
///
/// CONTRACT-QUESTION: contract 01 gives no limit for the nesting of a file.
/// The deepest field of a family file is at level 5. This reader calls
/// itself two times for each level, and a call that has no stack left stops
/// the process. The reading here is a limit with a wide margin: a debug
/// build composes a text of this depth on a stack of 1 MiB. A larger number
/// costs stack in each caller, or a composer that keeps its own work list.
pub(crate) const NESTING_MAX: usize = 128;

/// The index of one node in the arena of the composer.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub(super) struct NodeId(usize);

#[derive(Debug, Clone, PartialEq, Eq)]
pub(super) enum Content {
    Scalar(String),
    Sequence(Vec<NodeId>),
    Mapping(Vec<(NodeId, NodeId)>),
}

impl Content {
    /// `node.id`: the name that a constructor error gives a node.
    pub(super) fn id(&self) -> &'static str {
        match self {
            Self::Scalar(_) => "scalar",
            Self::Sequence(_) => "sequence",
            Self::Mapping(_) => "mapping",
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub(super) struct Node {
    pub(super) tag: String,
    pub(super) start: Mark,
    pub(super) content: Content,
}

/// The tags of YAML 1.1 that the safe constructor knows, and the two that it
/// refuses. Every other tag is `Other`.
pub(super) mod tag {
    pub(in super::super) const NULL: &str = "tag:yaml.org,2002:null";
    pub(in super::super) const BOOL: &str = "tag:yaml.org,2002:bool";
    pub(in super::super) const INT: &str = "tag:yaml.org,2002:int";
    pub(in super::super) const FLOAT: &str = "tag:yaml.org,2002:float";
    pub(in super::super) const BINARY: &str = "tag:yaml.org,2002:binary";
    pub(in super::super) const TIMESTAMP: &str = "tag:yaml.org,2002:timestamp";
    pub(in super::super) const OMAP: &str = "tag:yaml.org,2002:omap";
    pub(in super::super) const PAIRS: &str = "tag:yaml.org,2002:pairs";
    pub(in super::super) const SET: &str = "tag:yaml.org,2002:set";
    pub(in super::super) const STR: &str = "tag:yaml.org,2002:str";
    pub(in super::super) const SEQ: &str = "tag:yaml.org,2002:seq";
    pub(in super::super) const MAP: &str = "tag:yaml.org,2002:map";
    pub(in super::super) const MERGE: &str = "tag:yaml.org,2002:merge";
    pub(in super::super) const VALUE: &str = "tag:yaml.org,2002:value";
    pub(in super::super) const YAML: &str = "tag:yaml.org,2002:yaml";
}

#[derive(Debug)]
pub(super) struct Composer {
    parser: Parser,
    nodes: Vec<Node>,
    anchors: HashMap<String, NodeId>,
}

impl Composer {
    pub(super) fn new(text: &str) -> Result<Self, LoadError> {
        Ok(Self {
            parser: Parser::new(text)?,
            nodes: Vec::new(),
            anchors: HashMap::new(),
        })
    }

    pub(super) fn buffer(&self) -> &[char] {
        self.parser.buffer()
    }

    pub(super) fn lone_surrogate(&self) -> Option<u32> {
        self.parser.lone_surrogate()
    }

    pub(super) fn node(&self, id: NodeId) -> Option<&Node> {
        self.nodes.get(id.0)
    }

    pub(super) fn node_mut(&mut self, id: NodeId) -> Option<&mut Node> {
        self.nodes.get_mut(id.0)
    }

    fn add(&mut self, node: Node) -> NodeId {
        self.nodes.push(node);

        NodeId(self.nodes.len() - 1)
    }

    /// `check_node`: whether the stream holds one more document.
    pub(super) fn check_node(&mut self) -> Result<bool, LoadError> {
        if self
            .parser
            .peek_event()?
            .is_some_and(|event| event.kind == EventKind::StreamStart)
        {
            self.parser.get_event()?;
        }

        Ok(self
            .parser
            .peek_event()?
            .is_some_and(|event| event.kind != EventKind::StreamEnd))
    }

    /// `compose_document`: the root node of the next document.
    pub(super) fn compose_document(&mut self) -> Result<NodeId, LoadError> {
        self.parser.get_event()?;
        let node = self.compose_node(0)?;
        self.parser.get_event()?;
        self.anchors.clear();

        Ok(node)
    }

    fn compose_node(&mut self, depth: usize) -> Result<NodeId, LoadError> {
        let Some(event) = self.parser.get_event()? else {
            return Err(LoadError::Unreadable(
                "the events end inside a node".to_owned(),
            ));
        };

        let start = event.start;
        match event.kind {
            EventKind::Alias(name) => self.anchors.get(&name).copied().ok_or_else(|| {
                marked(
                    self.buffer(),
                    None,
                    &format!("found undefined alias {}", repr_str(&name)),
                    start,
                )
            }),
            EventKind::Scalar {
                anchor,
                tag,
                plain_implicit,
                value,
            } => {
                self.check_anchor(anchor.as_deref(), start)?;
                let tag = match tag {
                    Some(tag) if tag != "!" => tag,
                    _ if plain_implicit => resolve_plain(&value).to_owned(),
                    // A quoted scalar with no tag is a string.
                    _ => tag::STR.to_owned(),
                };
                let id = self.add(Node {
                    tag,
                    start,
                    content: Content::Scalar(value),
                });
                self.keep_anchor(anchor, id);

                Ok(id)
            }
            EventKind::SequenceStart { anchor, tag } => {
                self.check_anchor(anchor.as_deref(), start)?;
                let id = self.add(Node {
                    tag: collection_tag(tag, tag::SEQ),
                    start,
                    content: Content::Sequence(Vec::new()),
                });
                self.keep_anchor(anchor, id);
                self.compose_sequence(id, depth)?;

                Ok(id)
            }
            EventKind::MappingStart { anchor, tag } => {
                self.check_anchor(anchor.as_deref(), start)?;
                let id = self.add(Node {
                    tag: collection_tag(tag, tag::MAP),
                    start,
                    content: Content::Mapping(Vec::new()),
                });
                self.keep_anchor(anchor, id);
                self.compose_mapping(id, depth)?;

                Ok(id)
            }
            _ => Err(LoadError::Unreadable(
                "an event that starts no node is in the place of a node".to_owned(),
            )),
        }
    }

    fn check_anchor(&self, anchor: Option<&str>, at: Mark) -> Result<(), LoadError> {
        let Some(name) = anchor else {
            return Ok(());
        };

        let Some(first) = self.anchors.get(name).and_then(|id| self.node(*id)) else {
            return Ok(());
        };

        Err(marked(
            self.buffer(),
            Some((
                &format!(
                    "found duplicate anchor {}; first occurrence",
                    repr_str(name)
                ),
                first.start,
            )),
            "second occurrence",
            at,
        ))
    }

    fn keep_anchor(&mut self, anchor: Option<String>, id: NodeId) {
        if let Some(name) = anchor {
            self.anchors.insert(name, id);
        }
    }

    fn at_end(&mut self, end: &EventKind) -> Result<bool, LoadError> {
        Ok(self
            .parser
            .peek_event()?
            .is_none_or(|event| event.kind == *end))
    }

    fn deeper(depth: usize) -> Result<usize, LoadError> {
        if depth >= NESTING_MAX {
            return Err(LoadError::Unreadable(format!(
                "the text nests deeper than {NESTING_MAX} levels"
            )));
        }

        Ok(depth + 1)
    }

    fn compose_sequence(&mut self, id: NodeId, depth: usize) -> Result<(), LoadError> {
        let depth = Self::deeper(depth)?;
        while !self.at_end(&EventKind::SequenceEnd)? {
            let item = self.compose_node(depth)?;
            if let Some(Content::Sequence(items)) = self.node_mut(id).map(|node| &mut node.content)
            {
                items.push(item);
            }
        }

        self.parser.get_event()?;

        Ok(())
    }

    fn compose_mapping(&mut self, id: NodeId, depth: usize) -> Result<(), LoadError> {
        let depth = Self::deeper(depth)?;
        while !self.at_end(&EventKind::MappingEnd)? {
            let key = self.compose_node(depth)?;
            let value = self.compose_node(depth)?;
            if let Some(Content::Mapping(pairs)) = self.node_mut(id).map(|node| &mut node.content) {
                pairs.push((key, value));
            }
        }

        self.parser.get_event()?;

        Ok(())
    }
}

fn collection_tag(tag: Option<String>, default: &str) -> String {
    match tag {
        Some(tag) if tag != "!" => tag,
        _ => default.to_owned(),
    }
}

// --- the resolver ---

/// The tag of a plain scalar with no tag of its own.
fn resolve_plain(value: &str) -> &'static str {
    // Python takes the patterns to try from the first character, and a line
    // break has none.
    if value.starts_with('\n') {
        return tag::STR;
    }

    // A Python pattern that ends with `$` also matches before one final
    // newline. Only a block scalar with the tag `!` holds such a value.
    let text = value.strip_suffix('\n').unwrap_or(value).as_bytes();
    if is_bool(text) {
        tag::BOOL
    } else if is_float(text) {
        tag::FLOAT
    } else if is_int(text) {
        tag::INT
    } else if text == b"<<" {
        tag::MERGE
    } else if matches!(text, b"" | b"~" | b"null" | b"Null" | b"NULL") {
        tag::NULL
    } else if is_timestamp(text) {
        tag::TIMESTAMP
    } else if text == b"=" {
        tag::VALUE
    } else if matches!(text, b"!" | b"&" | b"*") {
        tag::YAML
    } else {
        tag::STR
    }
}

fn is_bool(text: &[u8]) -> bool {
    matches!(
        text,
        b"yes"
            | b"Yes"
            | b"YES"
            | b"no"
            | b"No"
            | b"NO"
            | b"true"
            | b"True"
            | b"TRUE"
            | b"false"
            | b"False"
            | b"FALSE"
            | b"on"
            | b"On"
            | b"ON"
            | b"off"
            | b"Off"
            | b"OFF"
    )
}

fn strip_sign(text: &[u8]) -> &[u8] {
    match text.split_first() {
        Some((b'+' | b'-', rest)) => rest,
        _ => text,
    }
}

fn all(text: &[u8], class: fn(&u8) -> bool) -> bool {
    text.iter().all(class)
}

fn digit_or_underscore(byte: &u8) -> bool {
    byte.is_ascii_digit() || *byte == b'_'
}

/// `[0-9][0-9_]*`, then the rest.
fn split_digits(text: &[u8]) -> Option<(&[u8], &[u8])> {
    if !text.first().is_some_and(u8::is_ascii_digit) {
        return None;
    }

    let end = text
        .iter()
        .position(|byte| !digit_or_underscore(byte))
        .unwrap_or(text.len());

    Some(text.split_at(end))
}

/// `(?:[eE][-+][0-9]+)?` to the end of the text.
fn is_exponent_or_empty(text: &[u8]) -> bool {
    match text {
        [] => true,
        [b'e' | b'E', b'+' | b'-', digits @ ..] => {
            !digits.is_empty() && all(digits, u8::is_ascii_digit)
        }
        _ => false,
    }
}

/// `[0-9_]*`, then the rest.
fn split_fraction(text: &[u8]) -> (&[u8], &[u8]) {
    let end = text
        .iter()
        .position(|byte| !digit_or_underscore(byte))
        .unwrap_or(text.len());

    text.split_at(end)
}

/// `(?::[0-5]?[0-9])+`, then the rest. `None` when no part is there.
fn split_base60(mut text: &[u8]) -> Option<&[u8]> {
    let mut parts = 0;
    while let Some((b':', rest)) = text.split_first() {
        let end = rest
            .iter()
            .position(|byte| !byte.is_ascii_digit())
            .unwrap_or(rest.len());
        let (digits, after) = rest.split_at(end);
        match digits {
            [_] | [b'0'..=b'5', _] => {}
            _ => return None,
        }

        parts += 1;
        text = after;
    }

    (parts > 0).then_some(text)
}

fn is_float(text: &[u8]) -> bool {
    let unsigned = strip_sign(text);
    if matches!(unsigned, b".inf" | b".Inf" | b".INF") {
        return true;
    }

    if matches!(text, b".nan" | b".NaN" | b".NAN") {
        return true;
    }

    // `\.[0-9][0-9_]*(?:[eE][-+][0-9]+)?` takes no sign.
    if let Some((b'.', rest)) = text.split_first() {
        return split_digits(rest).is_some_and(|(_, after)| is_exponent_or_empty(after));
    }

    let Some((_, rest)) = split_digits(unsigned) else {
        return false;
    };

    match rest.split_first() {
        Some((b'.', after)) => is_exponent_or_empty(split_fraction(after).1),
        Some((b':', _)) => match split_base60(rest).and_then(|after| after.split_first()) {
            Some((b'.', after)) => all(after, digit_or_underscore),
            _ => false,
        },
        _ => false,
    }
}

fn is_int(text: &[u8]) -> bool {
    let unsigned = strip_sign(text);
    match unsigned {
        [b'0', b'b', rest @ ..] if !rest.is_empty() => {
            return all(rest, |byte| matches!(byte, b'0' | b'1' | b'_'));
        }
        [b'0', b'x', rest @ ..] if !rest.is_empty() => {
            return all(rest, |byte| byte.is_ascii_hexdigit() || *byte == b'_');
        }
        [b'0'] => return true,
        [b'0', rest @ ..] => {
            return all(rest, |byte| matches!(byte, b'0'..=b'7' | b'_'));
        }
        [b'1'..=b'9', ..] => {}
        _ => return false,
    }

    let Some((_, rest)) = split_digits(unsigned) else {
        return false;
    };

    rest.is_empty() || split_base60(rest).is_some_and(<[u8]>::is_empty)
}

/// Up to `most` digits and at least `least`, then the rest.
fn take_digits(text: &[u8], least: usize, most: usize) -> Option<&[u8]> {
    let count = text
        .iter()
        .take(most)
        .take_while(|byte| byte.is_ascii_digit())
        .count();
    if count < least {
        return None;
    }

    text.get(count..)
}

fn take_byte(text: &[u8], wanted: u8) -> Option<&[u8]> {
    match text.split_first() {
        Some((byte, rest)) if *byte == wanted => Some(rest),
        _ => None,
    }
}

fn skip_blanks(text: &[u8]) -> &[u8] {
    let end = text
        .iter()
        .position(|byte| !matches!(byte, b' ' | b'\t'))
        .unwrap_or(text.len());

    text.get(end..).unwrap_or(&[])
}

/// The date form `YYYY-MM-DD`, or the long form with a time.
fn is_timestamp(text: &[u8]) -> bool {
    let date = take_digits(text, 4, 4)
        .and_then(|rest| take_byte(rest, b'-'))
        .and_then(|rest| take_digits(rest, 2, 2))
        .and_then(|rest| take_byte(rest, b'-'))
        .and_then(|rest| take_digits(rest, 2, 2));
    if date.is_some_and(<[u8]>::is_empty) {
        return true;
    }

    is_long_timestamp(text).unwrap_or(false)
}

fn is_long_timestamp(text: &[u8]) -> Option<bool> {
    let rest = take_digits(text, 4, 4)?;
    let rest = take_digits(take_byte(rest, b'-')?, 1, 2)?;
    let rest = take_digits(take_byte(rest, b'-')?, 1, 2)?;
    let rest = match rest.split_first()? {
        (b'T' | b't', rest) => rest,
        (b' ' | b'\t', _) => skip_blanks(rest),
        _ => return None,
    };
    let rest = take_digits(rest, 1, 2)?;
    let rest = take_digits(take_byte(rest, b':')?, 2, 2)?;
    let rest = take_digits(take_byte(rest, b':')?, 2, 2)?;
    let rest = match take_byte(rest, b'.') {
        Some(fraction) => take_digits(fraction, 0, usize::MAX)?,
        None => rest,
    };
    if rest.is_empty() {
        return Some(true);
    }

    let zone = skip_blanks(rest);
    if zone == b"Z" {
        return Some(true);
    }

    let (sign, hours) = zone.split_first()?;
    if !matches!(sign, b'+' | b'-') {
        return None;
    }

    let rest = take_digits(hours, 1, 2)?;
    if rest.is_empty() {
        return Some(true);
    }

    Some(take_digits(take_byte(rest, b':')?, 2, 2)?.is_empty())
}

#[cfg(test)]
mod tests {
    use super::{Composer, LoadError, NESTING_MAX, resolve_plain, tag};

    /// The stack of the thread that composes a text in these tests. A
    /// default thread has two times this stack.
    const SMALL_STACK: usize = 1 << 20;

    fn compose(text: &str) -> Result<(), LoadError> {
        let mut composer = Composer::new(text)?;
        while composer.check_node()? {
            composer.compose_document()?;
        }

        Ok(())
    }

    /// What the composer answers for `text`, on a thread with a small stack.
    /// `None` is the answer `Ok`.
    fn compose_on_a_small_stack(text: String) -> Option<LoadError> {
        let thread = std::thread::Builder::new()
            .stack_size(SMALL_STACK)
            .spawn(move || compose(&text).err());

        thread.unwrap().join().unwrap()
    }

    fn too_deep() -> LoadError {
        LoadError::Unreadable(format!("the text nests deeper than {NESTING_MAX} levels"))
    }

    #[test]
    fn sequences_nest_to_the_limit_and_no_deeper() {
        let nested = |levels: usize| format!("{}{}", "[".repeat(levels), "]".repeat(levels));

        assert_eq!(compose_on_a_small_stack(nested(NESTING_MAX)), None);
        assert_eq!(
            compose_on_a_small_stack(nested(NESTING_MAX + 1)),
            Some(too_deep())
        );
    }

    #[test]
    fn mappings_nest_to_the_limit_and_no_deeper() {
        let nested = |levels: usize| format!("{}1{}", "{a: ".repeat(levels), "}".repeat(levels));

        assert_eq!(compose_on_a_small_stack(nested(NESTING_MAX)), None);
        assert_eq!(
            compose_on_a_small_stack(nested(NESTING_MAX + 1)),
            Some(too_deep())
        );
    }

    /// What PyYAML 6.0.3 answers for `Resolver().resolve(ScalarNode, text,
    /// (True, False))`.
    const RESOLVED: [(&str, &str); 74] = [
        ("yes", tag::BOOL),
        ("No", tag::BOOL),
        ("ON", tag::BOOL),
        ("off", tag::BOOL),
        ("True", tag::BOOL),
        ("FALSE", tag::BOOL),
        ("y", tag::STR),
        ("n", tag::STR),
        ("tRue", tag::STR),
        ("oN", tag::STR),
        ("", tag::NULL),
        ("~", tag::NULL),
        ("null", tag::NULL),
        ("Null", tag::NULL),
        ("NULL", tag::NULL),
        ("nULL", tag::STR),
        ("none", tag::STR),
        ("<<", tag::MERGE),
        ("=", tag::VALUE),
        ("0", tag::INT),
        ("-0", tag::INT),
        ("+12", tag::INT),
        ("1_000", tag::INT),
        ("010", tag::INT),
        ("0_", tag::INT),
        ("08", tag::STR),
        ("0o10", tag::STR),
        ("0b101", tag::INT),
        ("0b", tag::STR),
        ("0b2", tag::STR),
        ("0x1F", tag::INT),
        ("0x", tag::STR),
        ("0X1F", tag::STR),
        ("1:30", tag::INT),
        ("1:5", tag::INT),
        ("1:60", tag::STR),
        ("0:2", tag::STR),
        ("1:30:59", tag::INT),
        ("1:", tag::STR),
        ("12abc", tag::STR),
        ("1e3", tag::STR),
        ("1e0", tag::STR),
        ("1.0", tag::FLOAT),
        ("1.", tag::FLOAT),
        ("1._", tag::FLOAT),
        ("-1.5", tag::FLOAT),
        (".5", tag::FLOAT),
        ("-.5", tag::STR),
        ("+.5", tag::STR),
        ("._5", tag::STR),
        ("1.0e+2", tag::FLOAT),
        ("1.0e2", tag::STR),
        ("1.e+2", tag::FLOAT),
        (".5e-3", tag::FLOAT),
        ("1.0e+", tag::STR),
        ("1:30.5", tag::FLOAT),
        ("1:30.", tag::FLOAT),
        ("0:30.5", tag::FLOAT),
        ("1:30.5e+1", tag::STR),
        (".inf", tag::FLOAT),
        ("-.INF", tag::FLOAT),
        ("+.Inf", tag::FLOAT),
        (".iNf", tag::STR),
        (".nan", tag::FLOAT),
        (".NaN", tag::FLOAT),
        ("-.nan", tag::STR),
        ("2026-10-03", tag::TIMESTAMP),
        ("2026-1-3", tag::STR),
        ("2026-10-03T10:20:30", tag::TIMESTAMP),
        ("2026-1-3 1:20:30.5 +1:00", tag::TIMESTAMP),
        ("2026-10-03t10:20:30Z", tag::TIMESTAMP),
        ("2026-10-03 10:20", tag::STR),
        ("2026-10-03T10:20:30 -05", tag::TIMESTAMP),
        ("2026-10-03\n", tag::TIMESTAMP),
    ];

    #[test]
    fn a_plain_scalar_takes_the_tag_that_pyyaml_gives_it() {
        for (text, wanted) in RESOLVED {
            assert_eq!(resolve_plain(text), wanted, "{text:?}");
        }
    }
}
