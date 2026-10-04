//! A YAML reader that gives what `yaml.safe_load` of PyYAML 6.0 gives.
//!
//! The Python release tool reads a `component.yaml` with PyYAML, which reads
//! YAML 1.1. A YAML 1.2 reader gives another value for `yes`, `010` and
//! `1:30`, and it accepts and refuses other texts. This module is a port of
//! the five stages of PyYAML, so that one text gives one result in both
//! languages:
//!
//! 1. [`Reader`]: the characters, the line and the column.
//! 2. [`Scanner`]: the tokens.
//! 3. [`Parser`]: the events.
//! 4. [`Composer`]: the nodes, with each anchor and each tag.
//! 5. [`Constructor`]: the values.
//!
//! The stages keep the names and the order of the checks of PyYAML. A fault
//! holds the line of the mark that PyYAML gives. This reader refuses each
//! text for which PyYAML gives no value.
//!
//! The reader keeps no float, no time and no bytes: [`Value::Float`] and
//! [`Value::Other`] say only what a value is. No field of a manifest takes
//! such a value.

use std::collections::{BTreeMap, VecDeque};

/// The deepest nesting of collections that the reader takes.
///
/// CONTRACT-QUESTION: contract 06 §8 and §10 give no limit. The limit of
/// PyYAML is the stack of its interpreter, which is not one number. This
/// reader refuses a text past 128 levels. No valid manifest nests deeper than
/// 3 levels. A larger limit costs stack for each level.
pub(super) const DEPTH_MAX: usize = 128;

/// The largest count of digits that Python reads as a decimal integer.
const INT_DIGITS_MAX: usize = 4300;

/// The largest distance, in characters, between the start of a simple key and
/// its `:`.
const SIMPLE_KEY_MAX: usize = 1024;

/// The longest chain of merge keys that the reader takes: a `<<` value that
/// has a `<<` value of its own, and so on.
///
/// CONTRACT-QUESTION: contract 06 §8 and §10 give no limit for a chain of
/// merge keys. An alias makes a chain with no nesting, so [`DEPTH_MAX`] does
/// not bound it. This reader refuses a chain past 128 levels, which is the
/// limit for the nesting. No manifest of this repository has a merge key. A
/// larger limit costs stack for each level.
const MERGE_DEPTH_MAX: usize = DEPTH_MAX;

/// The largest count of pairs that the merge keys of one document copy.
///
/// CONTRACT-QUESTION: contract 06 §8 and §10 give no limit for a merge key.
/// This reader refuses a document whose merge keys copy more than 65,536
/// pairs, which is four pairs for each byte of the largest manifest. No
/// manifest of this repository has a merge key. A larger limit costs memory:
/// one copied pair is 16 bytes.
pub(super) const MERGE_PAIRS_MAX: usize = 65_536;

const NUL: char = '\0';
const BOM: char = '\u{feff}';
const NEL: char = '\u{85}';
const LINE_SEPARATOR: char = '\u{2028}';
const PARAGRAPH_SEPARATOR: char = '\u{2029}';
const REPLACEMENT: char = '\u{fffd}';

const TAG_PREFIX: &str = "tag:yaml.org,2002:";
const TAG_NULL: &str = "tag:yaml.org,2002:null";
const TAG_BOOL: &str = "tag:yaml.org,2002:bool";
const TAG_INT: &str = "tag:yaml.org,2002:int";
const TAG_FLOAT: &str = "tag:yaml.org,2002:float";
const TAG_BINARY: &str = "tag:yaml.org,2002:binary";
const TAG_TIMESTAMP: &str = "tag:yaml.org,2002:timestamp";
const TAG_OMAP: &str = "tag:yaml.org,2002:omap";
const TAG_PAIRS: &str = "tag:yaml.org,2002:pairs";
const TAG_SET: &str = "tag:yaml.org,2002:set";
const TAG_STR: &str = "tag:yaml.org,2002:str";
const TAG_SEQ: &str = "tag:yaml.org,2002:seq";
const TAG_MAP: &str = "tag:yaml.org,2002:map";
const TAG_MERGE: &str = "tag:yaml.org,2002:merge";
const TAG_VALUE: &str = "tag:yaml.org,2002:value";
const TAG_YAML: &str = "tag:yaml.org,2002:yaml";

/// The tag that says: resolve the scalar as if it had no tag.
const TAG_NON_SPECIFIC: &str = "!";

/// Why a text is not a YAML document that the reader takes.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(super) enum Fault {
    /// The text holds a character that YAML does not permit.
    Unreadable,
    /// The text breaks a rule of YAML. The mark of the problem is on this
    /// line, from 0.
    Line(usize),
    /// A collection nests deeper than [`DEPTH_MAX`] levels, or a chain of
    /// merge keys is longer than [`MERGE_DEPTH_MAX`] levels.
    Deep,
    /// The merge keys copy more than [`MERGE_PAIRS_MAX`] pairs.
    Merge,
}

/// The place of one value in a [`Tree`].
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(super) struct ValueId(usize);

/// One value of a document, as the Python reader sees it.
#[derive(Debug)]
pub(super) enum Value {
    /// `None`.
    Null,
    /// `True` or `False`.
    Bool(bool),
    /// An integer. `None` is an integer that does not fit 64 bits.
    Int(Option<i64>),
    /// A float. The reader does not keep the number.
    Float,
    /// A string.
    Text(Text),
    /// A value of another Python type: a date, a time, bytes, a set or a pair.
    Other,
    /// A list.
    List(Vec<ValueId>),
    /// A mapping.
    Map(Map),
}

/// One string of a document.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(super) struct Text {
    text: String,
    lossy: bool,
}

impl Text {
    /// The string. A lone surrogate of the Python string is U+FFFD here.
    pub(super) fn as_str(&self) -> &str {
        &self.text
    }

    /// Whether the Python string holds a lone surrogate. A Rust string cannot
    /// hold one, so [`Text::as_str`] is then not the Python string.
    pub(super) fn is_lossy(&self) -> bool {
        self.lossy
    }
}

/// One mapping of a document: the value of each string key.
#[derive(Debug, Default)]
pub(super) struct Map {
    entries: BTreeMap<String, ValueId>,
    other_key: bool,
}

impl Map {
    /// The value of one string key.
    pub(super) fn get(&self, key: &str) -> Option<ValueId> {
        self.entries.get(key).copied()
    }

    /// Each string key, in the order of the code points.
    pub(super) fn keys(&self) -> impl Iterator<Item = &str> {
        self.entries.keys().map(String::as_str)
    }

    /// Whether the mapping has a key that is not a string.
    pub(super) fn has_other_key(&self) -> bool {
        self.other_key
    }
}

/// The values of one document.
#[derive(Debug)]
pub(super) struct Tree {
    values: Vec<Value>,
    root: Option<ValueId>,
}

/// What a text with no document gives.
static NO_DOCUMENT: Value = Value::Null;

impl Tree {
    /// The value of the document. A text with no document gives `None`.
    pub(super) fn root(&self) -> &Value {
        self.root.map_or(&NO_DOCUMENT, |id| self.get(id))
    }

    /// One value.
    pub(super) fn get(&self, id: ValueId) -> &Value {
        self.values.get(id.0).unwrap_or(&NO_DOCUMENT)
    }
}

/// Reads the one document of `text`, as `yaml.safe_load` does.
pub(super) fn load(text: &str) -> Result<Tree, Fault> {
    let reader = Reader::new(text)?;
    let mut composer = Composer::new(Parser::new(Scanner::new(reader)));
    let document = composer.single()?;
    let mut constructor = Constructor::new(composer.nodes);
    let root = match document {
        Some(node) => Some(constructor.document(node)?),
        None => None,
    };

    Ok(Tree {
        values: constructor.values,
        root,
    })
}

// --- the classes of characters ---

/// `\r\n\x85\u2028\u2029`.
fn is_break(character: char) -> bool {
    matches!(
        character,
        '\r' | '\n' | NEL | LINE_SEPARATOR | PARAGRAPH_SEPARATOR
    )
}

/// `\0\r\n\x85\u2028\u2029`.
fn ends_line(character: char) -> bool {
    character == NUL || is_break(character)
}

/// `\0 \r\n\x85\u2028\u2029`.
fn ends_word(character: char) -> bool {
    character == ' ' || ends_line(character)
}

/// `\0 \t\r\n\x85\u2028\u2029`.
fn is_blank_or_end(character: char) -> bool {
    character == '\t' || ends_word(character)
}

/// A character of an anchor, of a directive name or of a tag handle.
fn is_word(character: char) -> bool {
    character.is_ascii_alphanumeric() || matches!(character, '-' | '_')
}

/// A character that YAML permits in a stream.
fn is_printable(character: char) -> bool {
    matches!(
        character,
        '\t' | '\n'
            | '\r'
            | ' '..='~'
            | NEL
            | '\u{a0}'..='\u{d7ff}'
            | '\u{e000}'..='\u{fffd}'
            | '\u{10000}'..='\u{10ffff}'
    )
}

// --- stage 1: the reader ---

/// The characters of the text, and where the next one is.
struct Reader {
    chars: Vec<char>,
    pointer: usize,
    line: usize,
    column: i64,
}

impl Reader {
    fn new(text: &str) -> Result<Self, Fault> {
        if !text.chars().all(is_printable) {
            return Err(Fault::Unreadable);
        }

        let mut chars: Vec<char> = text.chars().collect();
        chars.push(NUL);

        Ok(Self {
            chars,
            pointer: 0,
            line: 0,
            column: 0,
        })
    }

    /// The character at `offset` from the next one. Past the end it is NUL.
    fn peek(&self, offset: usize) -> char {
        self.chars
            .get(self.pointer.saturating_add(offset))
            .copied()
            .unwrap_or(NUL)
    }

    /// The next `length` characters, or fewer at the end of the text.
    fn prefix(&self, length: usize) -> String {
        self.chars.iter().skip(self.pointer).take(length).collect()
    }

    /// Whether the next characters are `text`.
    fn starts_with(&self, text: &str) -> bool {
        text.chars()
            .enumerate()
            .all(|(offset, character)| self.peek(offset) == character)
    }

    fn forward(&mut self, length: usize) {
        for _ in 0..length {
            let Some(character) = self.chars.get(self.pointer).copied() else {
                return;
            };
            self.pointer += 1;
            let lone_return = character == '\r' && self.peek(0) != '\n';
            if matches!(character, '\n' | NEL | LINE_SEPARATOR | PARAGRAPH_SEPARATOR) || lone_return
            {
                self.line += 1;
                self.column = 0;
            } else if character != BOM {
                self.column += 1;
            }
        }
    }
}

// --- stage 2: the scanner ---

/// A directive of a document.
#[derive(Debug)]
enum Directive {
    /// `%YAML`. `major_is_one` says whether the first number is 1.
    Yaml { major_is_one: bool },
    /// `%TAG`.
    Tag { handle: String, prefix: String },
    /// A directive with another name. PyYAML reads it and does nothing.
    Other,
}

#[derive(Debug)]
enum TokenKind {
    StreamStart,
    StreamEnd,
    Directive(Directive),
    DocumentStart,
    DocumentEnd,
    BlockSequenceStart,
    BlockMappingStart,
    BlockEnd,
    FlowSequenceStart,
    FlowMappingStart,
    FlowSequenceEnd,
    FlowMappingEnd,
    Key,
    Value,
    BlockEntry,
    FlowEntry,
    Alias(String),
    Anchor(String),
    /// A tag: its handle, if it has one, and its suffix.
    Tag(Option<String>, String),
    Scalar {
        value: Text,
        plain: bool,
    },
}

/// The kind of a token, with no content. The parser asks for it.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Kind {
    StreamStart,
    StreamEnd,
    Directive,
    DocumentStart,
    DocumentEnd,
    BlockSequenceStart,
    BlockMappingStart,
    BlockEnd,
    FlowSequenceStart,
    FlowMappingStart,
    FlowSequenceEnd,
    FlowMappingEnd,
    Key,
    Value,
    BlockEntry,
    FlowEntry,
    Alias,
    Anchor,
    Tag,
    Scalar,
}

impl TokenKind {
    fn kind(&self) -> Kind {
        match self {
            Self::StreamStart => Kind::StreamStart,
            Self::StreamEnd => Kind::StreamEnd,
            Self::Directive(_) => Kind::Directive,
            Self::DocumentStart => Kind::DocumentStart,
            Self::DocumentEnd => Kind::DocumentEnd,
            Self::BlockSequenceStart => Kind::BlockSequenceStart,
            Self::BlockMappingStart => Kind::BlockMappingStart,
            Self::BlockEnd => Kind::BlockEnd,
            Self::FlowSequenceStart => Kind::FlowSequenceStart,
            Self::FlowMappingStart => Kind::FlowMappingStart,
            Self::FlowSequenceEnd => Kind::FlowSequenceEnd,
            Self::FlowMappingEnd => Kind::FlowMappingEnd,
            Self::Key => Kind::Key,
            Self::Value => Kind::Value,
            Self::BlockEntry => Kind::BlockEntry,
            Self::FlowEntry => Kind::FlowEntry,
            Self::Alias(_) => Kind::Alias,
            Self::Anchor(_) => Kind::Anchor,
            Self::Tag(_, _) => Kind::Tag,
            Self::Scalar { .. } => Kind::Scalar,
        }
    }
}

/// One token, with the line of its start mark and of its end mark.
#[derive(Debug)]
struct Token {
    kind: TokenKind,
    start: usize,
    end: usize,
}

/// A place where a simple key can start.
struct SimpleKey {
    token_number: usize,
    required: bool,
    index: usize,
    line: usize,
    column: i64,
}

/// How a block scalar keeps its last line breaks.
#[derive(Clone, Copy, PartialEq, Eq)]
enum Chomping {
    /// No indicator: one line break.
    Clip,
    /// `-`: no line break.
    Strip,
    /// `+`: each line break.
    Keep,
}

/// The style of a block scalar.
#[derive(Clone, Copy, PartialEq, Eq)]
enum BlockStyle {
    Literal,
    Folded,
}

/// The style of a quoted scalar.
#[derive(Clone, Copy, PartialEq, Eq)]
enum Quote {
    Single,
    Double,
}

/// Which of the two indicators `*` and `&` starts a name.
#[derive(Clone, Copy, PartialEq, Eq)]
enum NameKind {
    Alias,
    Anchor,
}

struct Scanner {
    reader: Reader,
    done: bool,
    flow_level: usize,
    tokens: VecDeque<Token>,
    tokens_taken: usize,
    indent: i64,
    indents: Vec<i64>,
    allow_simple_key: bool,
    possible_simple_keys: BTreeMap<usize, SimpleKey>,
}

impl Scanner {
    fn new(reader: Reader) -> Self {
        let mut scanner = Self {
            reader,
            done: false,
            flow_level: 0,
            tokens: VecDeque::new(),
            tokens_taken: 0,
            indent: -1,
            indents: Vec::new(),
            allow_simple_key: true,
            possible_simple_keys: BTreeMap::new(),
        };
        scanner.push(TokenKind::StreamStart, 0, 0);

        scanner
    }

    fn line(&self) -> usize {
        self.reader.line
    }

    /// A fault with the mark of the next character.
    fn fault(&self) -> Fault {
        Fault::Line(self.reader.line)
    }

    fn peek(&self) -> char {
        self.reader.peek(0)
    }

    fn push(&mut self, kind: TokenKind, start: usize, end: usize) {
        self.tokens.push_back(Token { kind, start, end });
    }

    /// Pushes a token of one or more characters that starts here.
    fn push_span(&mut self, kind: TokenKind, length: usize) {
        let start = self.line();
        self.reader.forward(length);
        let end = self.line();
        self.push(kind, start, end);
    }

    fn in_flow(&self) -> bool {
        self.flow_level > 0
    }

    // --- the interface of the parser ---

    fn fill(&mut self) -> Result<(), Fault> {
        while self.need_more_tokens()? {
            self.fetch_more_tokens()?;
        }

        Ok(())
    }

    /// The kind and the two marks of the next token.
    fn peek_token(&mut self) -> Result<(Kind, usize, usize), Fault> {
        self.fill()?;
        match self.tokens.front() {
            Some(token) => Ok((token.kind.kind(), token.start, token.end)),
            // PyYAML reads no token after the end of the stream.
            None => Err(self.fault()),
        }
    }

    fn check_token(&mut self, kinds: &[Kind]) -> Result<bool, Fault> {
        self.fill()?;

        Ok(self
            .tokens
            .front()
            .is_some_and(|token| kinds.contains(&token.kind.kind())))
    }

    fn get_token(&mut self) -> Result<Token, Fault> {
        self.fill()?;
        match self.tokens.pop_front() {
            Some(token) => {
                self.tokens_taken += 1;
                Ok(token)
            }
            None => Err(self.fault()),
        }
    }

    fn need_more_tokens(&mut self) -> Result<bool, Fault> {
        if self.done {
            return Ok(false);
        }

        if self.tokens.is_empty() {
            return Ok(true);
        }

        self.stale_possible_simple_keys()?;

        Ok(self.next_possible_simple_key() == Some(self.tokens_taken))
    }

    fn fetch_more_tokens(&mut self) -> Result<(), Fault> {
        self.scan_to_next_token();
        self.stale_possible_simple_keys()?;
        self.unwind_indent(self.reader.column);
        let character = self.peek();
        match character {
            NUL => return self.fetch_stream_end(),
            '%' if self.check_directive() => return self.fetch_directive(),
            '-' if self.check_document_indicator("---") => {
                return self.fetch_document_indicator(TokenKind::DocumentStart);
            }
            '.' if self.check_document_indicator("...") => {
                return self.fetch_document_indicator(TokenKind::DocumentEnd);
            }
            '[' => return self.fetch_flow_collection_start(TokenKind::FlowSequenceStart),
            '{' => return self.fetch_flow_collection_start(TokenKind::FlowMappingStart),
            ']' => return self.fetch_flow_collection_end(TokenKind::FlowSequenceEnd),
            '}' => return self.fetch_flow_collection_end(TokenKind::FlowMappingEnd),
            ',' => return self.fetch_flow_entry(),
            '-' if self.check_block_entry() => return self.fetch_block_entry(),
            '?' if self.check_key() => return self.fetch_key(),
            ':' if self.check_value() => return self.fetch_value(),
            '*' => return self.fetch_name(NameKind::Alias),
            '&' => return self.fetch_name(NameKind::Anchor),
            '!' => return self.fetch_tag(),
            '|' if !self.in_flow() => return self.fetch_block_scalar(BlockStyle::Literal),
            '>' if !self.in_flow() => return self.fetch_block_scalar(BlockStyle::Folded),
            '\'' => return self.fetch_flow_scalar(Quote::Single),
            '"' => return self.fetch_flow_scalar(Quote::Double),
            _ => {}
        }

        if self.check_plain() {
            return self.fetch_plain();
        }

        Err(self.fault())
    }

    // --- simple keys ---

    fn next_possible_simple_key(&self) -> Option<usize> {
        self.possible_simple_keys
            .values()
            .map(|key| key.token_number)
            .min()
    }

    fn stale_possible_simple_keys(&mut self) -> Result<(), Fault> {
        let line = self.reader.line;
        let index = self.reader.pointer;
        let stale: Vec<usize> = self
            .possible_simple_keys
            .iter()
            .filter(|(_, key)| key.line != line || index.saturating_sub(key.index) > SIMPLE_KEY_MAX)
            .map(|(level, _)| *level)
            .collect();
        for level in stale {
            let required = self
                .possible_simple_keys
                .remove(&level)
                .is_some_and(|key| key.required);
            if required {
                return Err(self.fault());
            }
        }

        Ok(())
    }

    fn save_possible_simple_key(&mut self) -> Result<(), Fault> {
        if !self.allow_simple_key {
            return Ok(());
        }

        let required = !self.in_flow() && self.indent == self.reader.column;
        self.remove_possible_simple_key()?;
        let key = SimpleKey {
            token_number: self.tokens_taken + self.tokens.len(),
            required,
            index: self.reader.pointer,
            line: self.reader.line,
            column: self.reader.column,
        };
        self.possible_simple_keys.insert(self.flow_level, key);

        Ok(())
    }

    fn remove_possible_simple_key(&mut self) -> Result<(), Fault> {
        let required = self
            .possible_simple_keys
            .remove(&self.flow_level)
            .is_some_and(|key| key.required);
        if required {
            return Err(self.fault());
        }

        Ok(())
    }

    // --- indentation ---

    fn unwind_indent(&mut self, column: i64) {
        if self.in_flow() {
            return;
        }

        while self.indent > column {
            let mark = self.line();
            self.indent = self.indents.pop().unwrap_or(-1);
            self.push(TokenKind::BlockEnd, mark, mark);
        }
    }

    fn add_indent(&mut self, column: i64) -> bool {
        if self.indent >= column {
            return false;
        }

        self.indents.push(self.indent);
        self.indent = column;

        true
    }

    // --- the fetchers ---

    fn fetch_stream_end(&mut self) -> Result<(), Fault> {
        self.unwind_indent(-1);
        self.remove_possible_simple_key()?;
        self.allow_simple_key = false;
        self.possible_simple_keys.clear();
        let mark = self.line();
        self.push(TokenKind::StreamEnd, mark, mark);
        self.done = true;

        Ok(())
    }

    fn fetch_directive(&mut self) -> Result<(), Fault> {
        self.unwind_indent(-1);
        self.remove_possible_simple_key()?;
        self.allow_simple_key = false;
        let token = self.scan_directive()?;
        self.tokens.push_back(token);

        Ok(())
    }

    fn fetch_document_indicator(&mut self, kind: TokenKind) -> Result<(), Fault> {
        self.unwind_indent(-1);
        self.remove_possible_simple_key()?;
        self.allow_simple_key = false;
        self.push_span(kind, 3);

        Ok(())
    }

    fn fetch_flow_collection_start(&mut self, kind: TokenKind) -> Result<(), Fault> {
        self.save_possible_simple_key()?;
        self.flow_level = self.flow_level.wrapping_add(1);
        self.allow_simple_key = true;
        self.push_span(kind, 1);

        Ok(())
    }

    fn fetch_flow_collection_end(&mut self, kind: TokenKind) -> Result<(), Fault> {
        self.remove_possible_simple_key()?;
        // PyYAML subtracts with no check. A level below zero is then never
        // equal to zero, so the scanner stays in the flow context.
        self.flow_level = self.flow_level.wrapping_sub(1);
        self.allow_simple_key = false;
        self.push_span(kind, 1);

        Ok(())
    }

    fn fetch_flow_entry(&mut self) -> Result<(), Fault> {
        self.allow_simple_key = true;
        self.remove_possible_simple_key()?;
        self.push_span(TokenKind::FlowEntry, 1);

        Ok(())
    }

    fn fetch_block_entry(&mut self) -> Result<(), Fault> {
        if !self.in_flow() {
            if !self.allow_simple_key {
                return Err(self.fault());
            }

            if self.add_indent(self.reader.column) {
                let mark = self.line();
                self.push(TokenKind::BlockSequenceStart, mark, mark);
            }
        }

        self.allow_simple_key = true;
        self.remove_possible_simple_key()?;
        self.push_span(TokenKind::BlockEntry, 1);

        Ok(())
    }

    fn fetch_key(&mut self) -> Result<(), Fault> {
        if !self.in_flow() {
            if !self.allow_simple_key {
                return Err(self.fault());
            }

            if self.add_indent(self.reader.column) {
                let mark = self.line();
                self.push(TokenKind::BlockMappingStart, mark, mark);
            }
        }

        self.allow_simple_key = !self.in_flow();
        self.remove_possible_simple_key()?;
        self.push_span(TokenKind::Key, 1);

        Ok(())
    }

    fn fetch_value(&mut self) -> Result<(), Fault> {
        if let Some(key) = self.possible_simple_keys.remove(&self.flow_level) {
            let at = key
                .token_number
                .saturating_sub(self.tokens_taken)
                .min(self.tokens.len());
            self.tokens.insert(
                at,
                Token {
                    kind: TokenKind::Key,
                    start: key.line,
                    end: key.line,
                },
            );
            if !self.in_flow() && self.add_indent(key.column) {
                self.tokens.insert(
                    at,
                    Token {
                        kind: TokenKind::BlockMappingStart,
                        start: key.line,
                        end: key.line,
                    },
                );
            }

            self.allow_simple_key = false;
        } else {
            if !self.in_flow() {
                if !self.allow_simple_key {
                    return Err(self.fault());
                }

                if self.add_indent(self.reader.column) {
                    let mark = self.line();
                    self.push(TokenKind::BlockMappingStart, mark, mark);
                }
            }

            self.allow_simple_key = !self.in_flow();
            self.remove_possible_simple_key()?;
        }

        self.push_span(TokenKind::Value, 1);

        Ok(())
    }

    fn fetch_name(&mut self, kind: NameKind) -> Result<(), Fault> {
        self.save_possible_simple_key()?;
        self.allow_simple_key = false;
        let token = self.scan_name(kind)?;
        self.tokens.push_back(token);

        Ok(())
    }

    fn fetch_tag(&mut self) -> Result<(), Fault> {
        self.save_possible_simple_key()?;
        self.allow_simple_key = false;
        let token = self.scan_tag()?;
        self.tokens.push_back(token);

        Ok(())
    }

    fn fetch_block_scalar(&mut self, style: BlockStyle) -> Result<(), Fault> {
        self.allow_simple_key = true;
        self.remove_possible_simple_key()?;
        let token = self.scan_block_scalar(style)?;
        self.tokens.push_back(token);

        Ok(())
    }

    fn fetch_flow_scalar(&mut self, quote: Quote) -> Result<(), Fault> {
        self.save_possible_simple_key()?;
        self.allow_simple_key = false;
        let token = self.scan_flow_scalar(quote)?;
        self.tokens.push_back(token);

        Ok(())
    }

    fn fetch_plain(&mut self) -> Result<(), Fault> {
        self.save_possible_simple_key()?;
        self.allow_simple_key = false;
        let token = self.scan_plain();
        self.tokens.push_back(token);

        Ok(())
    }

    // --- the checkers ---

    fn check_directive(&self) -> bool {
        self.reader.column == 0
    }

    fn check_document_indicator(&self, indicator: &str) -> bool {
        self.reader.column == 0 && self.separator_ahead_of(indicator)
    }

    fn separator_ahead_of(&self, indicator: &str) -> bool {
        self.reader.starts_with(indicator) && is_blank_or_end(self.reader.peek(3))
    }

    /// Whether `---` or `...` and then a blank or the end are next.
    fn separator_ahead(&self) -> bool {
        self.separator_ahead_of("---") || self.separator_ahead_of("...")
    }

    fn check_block_entry(&self) -> bool {
        is_blank_or_end(self.reader.peek(1))
    }

    fn check_key(&self) -> bool {
        self.in_flow() || is_blank_or_end(self.reader.peek(1))
    }

    fn check_value(&self) -> bool {
        self.in_flow() || is_blank_or_end(self.reader.peek(1))
    }

    fn check_plain(&self) -> bool {
        let character = self.peek();
        let starts_token = is_blank_or_end(character)
            || matches!(
                character,
                '-' | '?'
                    | ':'
                    | ','
                    | '['
                    | ']'
                    | '{'
                    | '}'
                    | '#'
                    | '&'
                    | '*'
                    | '!'
                    | '|'
                    | '>'
                    | '\''
                    | '"'
                    | '%'
                    | '@'
                    | '`'
            );
        if !starts_token {
            return true;
        }

        !is_blank_or_end(self.reader.peek(1))
            && (character == '-' || (!self.in_flow() && matches!(character, '?' | ':')))
    }

    // --- the scanners ---

    fn skip_spaces(&mut self) {
        while self.peek() == ' ' {
            self.reader.forward(1);
        }
    }

    fn skip_comment(&mut self) {
        if self.peek() != '#' {
            return;
        }

        while !ends_line(self.peek()) {
            self.reader.forward(1);
        }
    }

    fn scan_to_next_token(&mut self) {
        if self.reader.pointer == 0 && self.peek() == BOM {
            self.reader.forward(1);
        }

        loop {
            self.skip_spaces();
            self.skip_comment();
            if self.scan_line_break().is_none() {
                return;
            }

            if !self.in_flow() {
                self.allow_simple_key = true;
            }
        }
    }

    fn scan_directive(&mut self) -> Result<Token, Fault> {
        let start = self.line();
        self.reader.forward(1);
        let name = self.scan_directive_name()?;
        let (directive, end) = match name.as_str() {
            "YAML" => {
                let major_is_one = self.scan_yaml_directive_value()?;
                (Directive::Yaml { major_is_one }, self.line())
            }
            "TAG" => {
                let (handle, prefix) = self.scan_tag_directive_value()?;
                (Directive::Tag { handle, prefix }, self.line())
            }
            _ => {
                let end = self.line();
                while !ends_line(self.peek()) {
                    self.reader.forward(1);
                }

                (Directive::Other, end)
            }
        };
        self.scan_ignored_line()?;

        Ok(Token {
            kind: TokenKind::Directive(directive),
            start,
            end,
        })
    }

    /// The count of word characters that are next.
    fn word_length(&self) -> usize {
        let mut length = 0;
        while is_word(self.reader.peek(length)) {
            length += 1;
        }

        length
    }

    fn scan_directive_name(&mut self) -> Result<String, Fault> {
        let length = self.word_length();
        if length == 0 {
            return Err(self.fault());
        }

        let name = self.reader.prefix(length);
        self.reader.forward(length);
        if !ends_word(self.peek()) {
            return Err(self.fault());
        }

        Ok(name)
    }

    /// Reads the two numbers of a `%YAML` directive. Gives whether the first
    /// number is 1.
    fn scan_yaml_directive_value(&mut self) -> Result<bool, Fault> {
        self.skip_spaces();
        let major = self.scan_yaml_directive_number()?;
        if self.peek() != '.' {
            return Err(self.fault());
        }

        self.reader.forward(1);
        self.scan_yaml_directive_number()?;
        if !ends_word(self.peek()) {
            return Err(self.fault());
        }

        Ok(major.trim_start_matches('0') == "1")
    }

    fn scan_yaml_directive_number(&mut self) -> Result<String, Fault> {
        if !self.peek().is_ascii_digit() {
            return Err(self.fault());
        }

        let mut length = 0;
        while self.reader.peek(length).is_ascii_digit() {
            length += 1;
        }

        // Python reads no longer text as an integer.
        if length > INT_DIGITS_MAX {
            return Err(self.fault());
        }

        let digits = self.reader.prefix(length);
        self.reader.forward(length);

        Ok(digits)
    }

    fn scan_tag_directive_value(&mut self) -> Result<(String, String), Fault> {
        self.skip_spaces();
        let handle = self.scan_tag_handle()?;
        if self.peek() != ' ' {
            return Err(self.fault());
        }

        self.skip_spaces();
        let prefix = self.scan_tag_uri()?;
        if !ends_word(self.peek()) {
            return Err(self.fault());
        }

        Ok((handle, prefix))
    }

    /// Reads the rest of a line that holds only spaces and a comment.
    fn scan_ignored_line(&mut self) -> Result<(), Fault> {
        self.skip_spaces();
        self.skip_comment();
        if !ends_line(self.peek()) {
            return Err(self.fault());
        }

        self.scan_line_break();

        Ok(())
    }

    fn scan_name(&mut self, kind: NameKind) -> Result<Token, Fault> {
        let start = self.line();
        self.reader.forward(1);
        let length = self.word_length();
        if length == 0 {
            return Err(self.fault());
        }

        let name = self.reader.prefix(length);
        self.reader.forward(length);
        let next = self.peek();
        if !(is_blank_or_end(next) || matches!(next, '?' | ':' | ',' | ']' | '}' | '%' | '@' | '`'))
        {
            return Err(self.fault());
        }

        let kind = match kind {
            NameKind::Alias => TokenKind::Alias(name),
            NameKind::Anchor => TokenKind::Anchor(name),
        };

        Ok(Token {
            kind,
            start,
            end: self.line(),
        })
    }

    fn scan_tag(&mut self) -> Result<Token, Fault> {
        let start = self.line();
        let mut character = self.reader.peek(1);
        let (handle, suffix) = if character == '<' {
            self.reader.forward(2);
            let suffix = self.scan_tag_uri()?;
            if self.peek() != '>' {
                return Err(self.fault());
            }

            self.reader.forward(1);
            (None, suffix)
        } else if is_blank_or_end(character) {
            self.reader.forward(1);
            (None, TAG_NON_SPECIFIC.to_owned())
        } else {
            let mut length = 1;
            let mut use_handle = false;
            while !ends_word(character) {
                if character == '!' {
                    use_handle = true;
                    break;
                }

                length += 1;
                character = self.reader.peek(length);
            }

            let handle = if use_handle {
                self.scan_tag_handle()?
            } else {
                self.reader.forward(1);
                TAG_NON_SPECIFIC.to_owned()
            };

            (Some(handle), self.scan_tag_uri()?)
        };
        if !ends_word(self.peek()) {
            return Err(self.fault());
        }

        Ok(Token {
            kind: TokenKind::Tag(handle, suffix),
            start,
            end: self.line(),
        })
    }

    fn scan_tag_handle(&mut self) -> Result<String, Fault> {
        if self.peek() != '!' {
            return Err(self.fault());
        }

        let mut length = 1;
        let mut character = self.reader.peek(length);
        if character != ' ' {
            while is_word(character) {
                length += 1;
                character = self.reader.peek(length);
            }

            if character != '!' {
                self.reader.forward(length);
                return Err(self.fault());
            }

            length += 1;
        }

        let handle = self.reader.prefix(length);
        self.reader.forward(length);

        Ok(handle)
    }

    fn scan_tag_uri(&mut self) -> Result<String, Fault> {
        let mut uri = String::new();
        let mut any = false;
        let mut length = 0;
        loop {
            let character = self.reader.peek(length);
            let in_uri = character.is_ascii_alphanumeric()
                || matches!(
                    character,
                    '-' | ';'
                        | '/'
                        | '?'
                        | ':'
                        | '@'
                        | '&'
                        | '='
                        | '+'
                        | '$'
                        | ','
                        | '_'
                        | '.'
                        | '!'
                        | '~'
                        | '*'
                        | '\''
                        | '('
                        | ')'
                        | '['
                        | ']'
                        | '%'
                );
            if !in_uri {
                break;
            }

            if character == '%' {
                uri.push_str(&self.reader.prefix(length));
                self.reader.forward(length);
                length = 0;
                uri.push_str(&self.scan_uri_escapes()?);
                any = true;
            } else {
                length += 1;
            }
        }

        if length > 0 {
            uri.push_str(&self.reader.prefix(length));
            self.reader.forward(length);
            any = true;
        }

        if !any {
            return Err(self.fault());
        }

        Ok(uri)
    }

    fn scan_uri_escapes(&mut self) -> Result<String, Fault> {
        let mark = self.fault();
        let mut bytes = Vec::new();
        while self.peek() == '%' {
            self.reader.forward(1);
            if !(0..2).all(|offset| self.reader.peek(offset).is_ascii_hexdigit()) {
                return Err(self.fault());
            }

            let Ok(byte) = u8::from_str_radix(&self.reader.prefix(2), 16) else {
                return Err(self.fault());
            };

            bytes.push(byte);
            self.reader.forward(2);
        }

        String::from_utf8(bytes).map_err(|_| mark)
    }

    fn scan_block_scalar(&mut self, style: BlockStyle) -> Result<Token, Fault> {
        let mut text = String::new();
        let start = self.line();
        self.reader.forward(1);
        let (chomping, increment) = self.scan_block_scalar_indicators()?;
        self.scan_ignored_line()?;
        let min_indent = (self.indent + 1).max(1);
        let (mut breaks, indent, mut end) = match increment {
            None => {
                let (breaks, max_indent, end) = self.scan_block_scalar_indentation();
                (breaks, min_indent.max(max_indent), end)
            }
            Some(increment) => {
                let indent = min_indent + increment - 1;
                let (breaks, end) = self.scan_block_scalar_breaks(indent);
                (breaks, indent, end)
            }
        };
        let mut line_break = None;
        while self.reader.column == indent && self.peek() != NUL {
            text.push_str(&breaks);
            let leading_non_space = !matches!(self.peek(), ' ' | '\t');
            let mut length = 0;
            while !ends_line(self.reader.peek(length)) {
                length += 1;
            }

            text.push_str(&self.reader.prefix(length));
            self.reader.forward(length);
            line_break = self.scan_line_break();
            (breaks, end) = self.scan_block_scalar_breaks(indent);
            if self.reader.column != indent || self.peek() == NUL {
                break;
            }

            let folds = style == BlockStyle::Folded
                && line_break == Some('\n')
                && leading_non_space
                && !matches!(self.peek(), ' ' | '\t');
            if folds {
                if breaks.is_empty() {
                    text.push(' ');
                }
            } else {
                text.extend(line_break);
            }
        }

        if chomping != Chomping::Strip {
            text.extend(line_break);
        }

        if chomping == Chomping::Keep {
            text.push_str(&breaks);
        }

        Ok(Token {
            kind: TokenKind::Scalar {
                value: Text { text, lossy: false },
                plain: false,
            },
            start,
            end,
        })
    }

    fn scan_chomping(&mut self) -> Option<Chomping> {
        let chomping = match self.peek() {
            '+' => Chomping::Keep,
            '-' => Chomping::Strip,
            _ => return None,
        };
        self.reader.forward(1);

        Some(chomping)
    }

    fn scan_increment(&mut self) -> Result<Option<i64>, Fault> {
        let Some(digit) = self.peek().to_digit(10) else {
            return Ok(None);
        };
        if digit == 0 {
            return Err(self.fault());
        }

        self.reader.forward(1);

        Ok(Some(i64::from(digit)))
    }

    fn scan_block_scalar_indicators(&mut self) -> Result<(Chomping, Option<i64>), Fault> {
        let (chomping, increment) = match self.scan_chomping() {
            Some(chomping) => (Some(chomping), self.scan_increment()?),
            None => {
                let increment = self.scan_increment()?;
                let chomping = match increment {
                    Some(_) => self.scan_chomping(),
                    None => None,
                };

                (chomping, increment)
            }
        };
        if !ends_word(self.peek()) {
            return Err(self.fault());
        }

        Ok((chomping.unwrap_or(Chomping::Clip), increment))
    }

    fn scan_block_scalar_indentation(&mut self) -> (String, i64, usize) {
        let mut breaks = String::new();
        let mut max_indent = 0;
        let mut end = self.line();
        while self.peek() == ' ' || is_break(self.peek()) {
            if self.peek() == ' ' {
                self.reader.forward(1);
                max_indent = max_indent.max(self.reader.column);
            } else {
                breaks.extend(self.scan_line_break());
                end = self.line();
            }
        }

        (breaks, max_indent, end)
    }

    fn scan_block_scalar_breaks(&mut self, indent: i64) -> (String, usize) {
        let mut breaks = String::new();
        let mut end = self.line();
        while self.reader.column < indent && self.peek() == ' ' {
            self.reader.forward(1);
        }

        while is_break(self.peek()) {
            breaks.extend(self.scan_line_break());
            end = self.line();
            while self.reader.column < indent && self.peek() == ' ' {
                self.reader.forward(1);
            }
        }

        (breaks, end)
    }

    fn scan_flow_scalar(&mut self, quote: Quote) -> Result<Token, Fault> {
        let mut value = Text {
            text: String::new(),
            lossy: false,
        };
        let start = self.line();
        let closing = self.peek();
        self.reader.forward(1);
        self.scan_flow_scalar_non_spaces(quote, &mut value)?;
        while self.peek() != closing {
            self.scan_flow_scalar_spaces(&mut value.text)?;
            self.scan_flow_scalar_non_spaces(quote, &mut value)?;
        }

        self.reader.forward(1);

        Ok(Token {
            kind: TokenKind::Scalar {
                value,
                plain: false,
            },
            start,
            end: self.line(),
        })
    }

    fn scan_flow_scalar_non_spaces(&mut self, quote: Quote, value: &mut Text) -> Result<(), Fault> {
        let double = quote == Quote::Double;
        loop {
            let mut length = 0;
            loop {
                let character = self.reader.peek(length);
                if is_blank_or_end(character) || matches!(character, '\'' | '"' | '\\') {
                    break;
                }

                length += 1;
            }

            if length > 0 {
                value.text.push_str(&self.reader.prefix(length));
                self.reader.forward(length);
            }

            let character = self.peek();
            if !double && character == '\'' && self.reader.peek(1) == '\'' {
                value.text.push('\'');
                self.reader.forward(2);
            } else if (double && character == '\'') || (!double && matches!(character, '"' | '\\'))
            {
                value.text.push(character);
                self.reader.forward(1);
            } else if double && character == '\\' {
                self.reader.forward(1);
                self.scan_escape(value)?;
            } else {
                return Ok(());
            }
        }
    }

    /// Reads what follows the `\` of a double-quoted scalar.
    fn scan_escape(&mut self, value: &mut Text) -> Result<(), Fault> {
        let character = self.peek();
        if let Some(replacement) = escape_replacement(character) {
            value.text.push(replacement);
            self.reader.forward(1);
            return Ok(());
        }

        if let Some(length) = escape_code_length(character) {
            self.reader.forward(1);
            if !(0..length).all(|offset| self.reader.peek(offset).is_ascii_hexdigit()) {
                return Err(self.fault());
            }

            let Ok(code) = u32::from_str_radix(&self.reader.prefix(length), 16) else {
                return Err(self.fault());
            };
            match char::from_u32(code) {
                Some(decoded) => value.text.push(decoded),
                // A surrogate. Python keeps it as one code point.
                None if code <= u32::from(char::MAX) => {
                    value.text.push(REPLACEMENT);
                    value.lossy = true;
                }
                // A code past U+10FFFF is no character.
                None => return Err(self.fault()),
            }

            self.reader.forward(length);
            return Ok(());
        }

        if is_break(character) {
            self.scan_line_break();
            let breaks = self.scan_flow_scalar_breaks()?;
            value.text.push_str(&breaks);
            return Ok(());
        }

        Err(self.fault())
    }

    fn scan_flow_scalar_spaces(&mut self, text: &mut String) -> Result<(), Fault> {
        let mut length = 0;
        while matches!(self.reader.peek(length), ' ' | '\t') {
            length += 1;
        }

        let whitespaces = self.reader.prefix(length);
        self.reader.forward(length);
        let character = self.peek();
        if character == NUL {
            return Err(self.fault());
        }

        if !is_break(character) {
            text.push_str(&whitespaces);
            return Ok(());
        }

        let line_break = self.scan_line_break();
        let breaks = self.scan_flow_scalar_breaks()?;
        if line_break != Some('\n') {
            text.extend(line_break);
        } else if breaks.is_empty() {
            text.push(' ');
        }

        text.push_str(&breaks);

        Ok(())
    }

    fn scan_flow_scalar_breaks(&mut self) -> Result<String, Fault> {
        let mut breaks = String::new();
        loop {
            if self.separator_ahead() {
                return Err(self.fault());
            }

            while matches!(self.peek(), ' ' | '\t') {
                self.reader.forward(1);
            }

            if !is_break(self.peek()) {
                return Ok(breaks);
            }

            breaks.extend(self.scan_line_break());
        }
    }

    /// The count of characters of a plain scalar that are next on this line.
    fn plain_length(&self) -> usize {
        let flow = self.in_flow();
        let mut length = 0;
        loop {
            let character = self.reader.peek(length);
            let after = self.reader.peek(length + 1);
            let ends_at_colon = character == ':'
                && (is_blank_or_end(after)
                    || (flow && matches!(after, ',' | '[' | ']' | '{' | '}')));
            let ends_in_flow = flow && matches!(character, ',' | '?' | '[' | ']' | '{' | '}');
            if is_blank_or_end(character) || ends_at_colon || ends_in_flow {
                return length;
            }

            length += 1;
        }
    }

    fn scan_plain(&mut self) -> Token {
        let mut text = String::new();
        let start = self.line();
        let mut end = start;
        let indent = self.indent + 1;
        let mut spaces = String::new();
        loop {
            if self.peek() == '#' {
                break;
            }

            let length = self.plain_length();
            if length == 0 {
                break;
            }

            self.allow_simple_key = false;
            text.push_str(&spaces);
            text.push_str(&self.reader.prefix(length));
            self.reader.forward(length);
            end = self.line();
            let Some(next) = self.scan_plain_spaces() else {
                break;
            };
            spaces = next;
            if spaces.is_empty()
                || self.peek() == '#'
                || (!self.in_flow() && self.reader.column < indent)
            {
                break;
            }
        }

        Token {
            kind: TokenKind::Scalar {
                value: Text { text, lossy: false },
                plain: true,
            },
            start,
            end,
        }
    }

    /// Reads the spaces and the line breaks after one line of a plain scalar.
    /// Gives `None` when a document separator follows.
    fn scan_plain_spaces(&mut self) -> Option<String> {
        let mut spaces = String::new();
        let mut length = 0;
        while self.reader.peek(length) == ' ' {
            length += 1;
        }

        let whitespaces = self.reader.prefix(length);
        self.reader.forward(length);
        if !is_break(self.peek()) {
            return Some(whitespaces);
        }

        let line_break = self.scan_line_break();
        self.allow_simple_key = true;
        if self.separator_ahead() {
            return None;
        }

        let mut breaks = String::new();
        while self.peek() == ' ' || is_break(self.peek()) {
            if self.peek() == ' ' {
                self.reader.forward(1);
                continue;
            }

            breaks.extend(self.scan_line_break());
            if self.separator_ahead() {
                return None;
            }
        }

        if line_break != Some('\n') {
            spaces.extend(line_break);
        } else if breaks.is_empty() {
            spaces.push(' ');
        }

        spaces.push_str(&breaks);

        Some(spaces)
    }

    /// Reads one line break. `\r\n`, `\r`, `\n` and NEL give `\n`.
    fn scan_line_break(&mut self) -> Option<char> {
        let character = self.peek();
        if matches!(character, '\r' | '\n' | NEL) {
            let length = if self.reader.starts_with("\r\n") {
                2
            } else {
                1
            };
            self.reader.forward(length);
            return Some('\n');
        }

        if matches!(character, LINE_SEPARATOR | PARAGRAPH_SEPARATOR) {
            self.reader.forward(1);
            return Some(character);
        }

        None
    }
}

/// What one character after `\` stands for in a double-quoted scalar.
fn escape_replacement(character: char) -> Option<char> {
    let replacement = match character {
        '0' => NUL,
        'a' => '\u{7}',
        'b' => '\u{8}',
        't' | '\t' => '\t',
        'n' => '\n',
        'v' => '\u{b}',
        'f' => '\u{c}',
        'r' => '\r',
        'e' => '\u{1b}',
        ' ' => ' ',
        '"' => '"',
        '\\' => '\\',
        '/' => '/',
        'N' => NEL,
        '_' => '\u{a0}',
        'L' => LINE_SEPARATOR,
        'P' => PARAGRAPH_SEPARATOR,
        _ => return None,
    };

    Some(replacement)
}

/// The count of hex digits after `\x`, `\u` and `\U`.
fn escape_code_length(character: char) -> Option<usize> {
    match character {
        'x' => Some(2),
        'u' => Some(4),
        'U' => Some(8),
        _ => None,
    }
}

// --- stage 3: the parser ---

#[derive(Debug)]
enum Event {
    StreamStart,
    StreamEnd,
    DocumentStart,
    DocumentEnd,
    Alias(String),
    Scalar {
        anchor: Option<String>,
        tag: Option<String>,
        /// Whether the composer can resolve the tag from the value.
        resolves: bool,
        value: Text,
    },
    SequenceStart {
        anchor: Option<String>,
        tag: Option<String>,
    },
    SequenceEnd,
    MappingStart {
        anchor: Option<String>,
        tag: Option<String>,
    },
    MappingEnd,
}

/// The kind of an event, with no content. The composer asks for it.
#[derive(Clone, Copy, PartialEq, Eq)]
enum EventKind {
    StreamEnd,
    Alias,
    Scalar,
    SequenceStart,
    SequenceEnd,
    MappingStart,
    MappingEnd,
    Other,
}

/// One event, with the line of its start mark.
#[derive(Debug)]
struct Marked {
    event: Event,
    line: usize,
}

impl Marked {
    fn kind(&self) -> EventKind {
        match self.event {
            Event::StreamEnd => EventKind::StreamEnd,
            Event::Alias(_) => EventKind::Alias,
            Event::Scalar { .. } => EventKind::Scalar,
            Event::SequenceStart { .. } => EventKind::SequenceStart,
            Event::SequenceEnd => EventKind::SequenceEnd,
            Event::MappingStart { .. } => EventKind::MappingStart,
            Event::MappingEnd => EventKind::MappingEnd,
            Event::StreamStart | Event::DocumentStart | Event::DocumentEnd => EventKind::Other,
        }
    }
}

/// What the parser does next. Each state is one `parse_` method of PyYAML.
#[derive(Clone, Copy)]
enum State {
    StreamStart,
    ImplicitDocumentStart,
    DocumentStart,
    DocumentEnd,
    DocumentContent,
    BlockNode,
    BlockSequenceFirstEntry,
    BlockSequenceEntry,
    IndentlessSequenceEntry,
    BlockMappingFirstKey,
    BlockMappingKey,
    BlockMappingValue,
    FlowSequenceFirstEntry,
    FlowSequenceEntry,
    FlowSequenceEntryMappingKey,
    FlowSequenceEntryMappingValue,
    FlowSequenceEntryMappingEnd,
    FlowMappingFirstKey,
    FlowMappingKey,
    FlowMappingValue,
    FlowMappingEmptyValue,
}

/// Which collection context a node is in.
#[derive(Clone, Copy, PartialEq, Eq)]
enum Context {
    Block,
    /// A block node that can be a sequence with no indentation.
    BlockOrIndentless,
    Flow,
}

/// Whether an entry of a flow collection is the first one.
#[derive(Clone, Copy, PartialEq, Eq)]
enum Place {
    First,
    Later,
}

/// A tag as the scanner reads it: a handle, if it has one, and a suffix.
struct RawTag {
    handle: Option<String>,
    suffix: String,
    /// The line of the start mark of the tag.
    line: usize,
}

/// The anchor and the tag of a node.
#[derive(Default)]
struct Properties {
    anchor: Option<String>,
    tag: Option<RawTag>,
    /// The line of the start mark of the first of the two.
    start: Option<usize>,
}

struct Parser {
    scanner: Scanner,
    current: Option<Marked>,
    state: Option<State>,
    states: Vec<State>,
    tag_handles: BTreeMap<String, String>,
}

/// The two tag handles that each document has.
fn default_tag_handles() -> BTreeMap<String, String> {
    BTreeMap::from([
        ("!".to_owned(), "!".to_owned()),
        ("!!".to_owned(), TAG_PREFIX.to_owned()),
    ])
}

fn empty_scalar(line: usize) -> Marked {
    Marked {
        event: Event::Scalar {
            anchor: None,
            tag: None,
            resolves: true,
            value: Text {
                text: String::new(),
                lossy: false,
            },
        },
        line,
    }
}

impl Parser {
    fn new(scanner: Scanner) -> Self {
        Self {
            scanner,
            current: None,
            state: Some(State::StreamStart),
            states: Vec::new(),
            tag_handles: BTreeMap::new(),
        }
    }

    // --- the interface of the composer ---

    fn load(&mut self) -> Result<(), Fault> {
        if self.current.is_none()
            && let Some(state) = self.state
        {
            self.current = Some(self.step(state)?);
        }

        Ok(())
    }

    fn check_event(&mut self, kind: EventKind) -> Result<bool, Fault> {
        self.load()?;

        Ok(self
            .current
            .as_ref()
            .is_some_and(|event| event.kind() == kind))
    }

    /// The kind, the line and the anchor of the next event.
    fn peek_event(&mut self) -> Result<(EventKind, usize, Option<String>), Fault> {
        self.load()?;
        let Some(marked) = &self.current else {
            return Err(self.scanner.fault());
        };
        let anchor = match &marked.event {
            Event::Scalar { anchor, .. }
            | Event::SequenceStart { anchor, .. }
            | Event::MappingStart { anchor, .. } => anchor.clone(),
            _ => None,
        };

        Ok((marked.kind(), marked.line, anchor))
    }

    fn get_event(&mut self) -> Result<Marked, Fault> {
        self.load()?;
        match self.current.take() {
            Some(event) => Ok(event),
            // PyYAML reads no event after the end of the stream.
            None => Err(self.scanner.fault()),
        }
    }

    // --- the states ---

    fn check(&mut self, kinds: &[Kind]) -> Result<bool, Fault> {
        self.scanner.check_token(kinds)
    }

    fn pop_state(&mut self) {
        self.state = self.states.pop();
    }

    fn step(&mut self, state: State) -> Result<Marked, Fault> {
        match state {
            State::StreamStart => self.parse_stream_start(),
            State::ImplicitDocumentStart => self.parse_implicit_document_start(),
            State::DocumentStart => self.parse_document_start(),
            State::DocumentEnd => self.parse_document_end(),
            State::DocumentContent => self.parse_document_content(),
            State::BlockNode => self.parse_node(Context::Block),
            State::BlockSequenceFirstEntry => {
                self.scanner.get_token()?;
                self.parse_block_sequence_entry()
            }
            State::BlockSequenceEntry => self.parse_block_sequence_entry(),
            State::IndentlessSequenceEntry => self.parse_indentless_sequence_entry(),
            State::BlockMappingFirstKey => {
                self.scanner.get_token()?;
                self.parse_block_mapping_key()
            }
            State::BlockMappingKey => self.parse_block_mapping_key(),
            State::BlockMappingValue => self.parse_block_mapping_value(),
            State::FlowSequenceFirstEntry => {
                self.scanner.get_token()?;
                self.parse_flow_sequence_entry(Place::First)
            }
            State::FlowSequenceEntry => self.parse_flow_sequence_entry(Place::Later),
            State::FlowSequenceEntryMappingKey => self.parse_flow_sequence_entry_mapping_key(),
            State::FlowSequenceEntryMappingValue => self.parse_flow_sequence_entry_mapping_value(),
            State::FlowSequenceEntryMappingEnd => {
                self.state = Some(State::FlowSequenceEntry);
                let (_, start, _) = self.scanner.peek_token()?;

                Ok(Marked {
                    event: Event::MappingEnd,
                    line: start,
                })
            }
            State::FlowMappingFirstKey => {
                self.scanner.get_token()?;
                self.parse_flow_mapping_key(Place::First)
            }
            State::FlowMappingKey => self.parse_flow_mapping_key(Place::Later),
            State::FlowMappingValue => self.parse_flow_mapping_value(),
            State::FlowMappingEmptyValue => {
                self.state = Some(State::FlowMappingKey);
                let (_, start, _) = self.scanner.peek_token()?;

                Ok(empty_scalar(start))
            }
        }
    }

    fn parse_stream_start(&mut self) -> Result<Marked, Fault> {
        let token = self.scanner.get_token()?;
        self.state = Some(State::ImplicitDocumentStart);

        Ok(Marked {
            event: Event::StreamStart,
            line: token.start,
        })
    }

    fn parse_implicit_document_start(&mut self) -> Result<Marked, Fault> {
        if self.check(&[Kind::Directive, Kind::DocumentStart, Kind::StreamEnd])? {
            return self.parse_document_start();
        }

        self.tag_handles = default_tag_handles();
        let (_, start, _) = self.scanner.peek_token()?;
        self.states.push(State::DocumentEnd);
        self.state = Some(State::BlockNode);

        Ok(Marked {
            event: Event::DocumentStart,
            line: start,
        })
    }

    fn parse_document_start(&mut self) -> Result<Marked, Fault> {
        while self.check(&[Kind::DocumentEnd])? {
            self.scanner.get_token()?;
        }

        if self.check(&[Kind::StreamEnd])? {
            let token = self.scanner.get_token()?;
            self.state = None;

            return Ok(Marked {
                event: Event::StreamEnd,
                line: token.start,
            });
        }

        let (_, start, _) = self.scanner.peek_token()?;
        self.process_directives()?;
        if !self.check(&[Kind::DocumentStart])? {
            let (_, found, _) = self.scanner.peek_token()?;
            return Err(Fault::Line(found));
        }

        self.scanner.get_token()?;
        self.states.push(State::DocumentEnd);
        self.state = Some(State::DocumentContent);

        Ok(Marked {
            event: Event::DocumentStart,
            line: start,
        })
    }

    fn parse_document_end(&mut self) -> Result<Marked, Fault> {
        let (_, start, _) = self.scanner.peek_token()?;
        if self.check(&[Kind::DocumentEnd])? {
            self.scanner.get_token()?;
        }

        self.state = Some(State::DocumentStart);

        Ok(Marked {
            event: Event::DocumentEnd,
            line: start,
        })
    }

    fn parse_document_content(&mut self) -> Result<Marked, Fault> {
        let empty = [
            Kind::Directive,
            Kind::DocumentStart,
            Kind::DocumentEnd,
            Kind::StreamEnd,
        ];
        if !self.check(&empty)? {
            return self.parse_node(Context::Block);
        }

        let (_, start, _) = self.scanner.peek_token()?;
        self.pop_state();

        Ok(empty_scalar(start))
    }

    fn process_directives(&mut self) -> Result<(), Fault> {
        let mut version_seen = false;
        self.tag_handles.clear();
        while self.check(&[Kind::Directive])? {
            let token = self.scanner.get_token()?;
            match token.kind {
                TokenKind::Directive(Directive::Yaml { major_is_one }) => {
                    if version_seen || !major_is_one {
                        return Err(Fault::Line(token.start));
                    }

                    version_seen = true;
                }
                TokenKind::Directive(Directive::Tag { handle, prefix }) => {
                    if self.tag_handles.contains_key(&handle) {
                        return Err(Fault::Line(token.start));
                    }

                    self.tag_handles.insert(handle, prefix);
                }
                _ => {}
            }
        }

        for (handle, prefix) in default_tag_handles() {
            self.tag_handles.entry(handle).or_insert(prefix);
        }

        Ok(())
    }

    /// Reads the anchor token that is next.
    fn take_anchor(&mut self, properties: &mut Properties) -> Result<(), Fault> {
        let token = self.scanner.get_token()?;
        properties.start.get_or_insert(token.start);
        if let TokenKind::Anchor(name) = token.kind {
            properties.anchor = Some(name);
        }

        Ok(())
    }

    /// Reads the tag token that is next.
    fn take_tag(&mut self, properties: &mut Properties) -> Result<(), Fault> {
        let token = self.scanner.get_token()?;
        properties.start.get_or_insert(token.start);
        if let TokenKind::Tag(handle, suffix) = token.kind {
            properties.tag = Some(RawTag {
                handle,
                suffix,
                line: token.start,
            });
        }

        Ok(())
    }

    /// Reads the anchor and the tag of a node, in each order.
    fn parse_properties(&mut self) -> Result<Properties, Fault> {
        let mut properties = Properties::default();
        if self.check(&[Kind::Anchor])? {
            self.take_anchor(&mut properties)?;
            if self.check(&[Kind::Tag])? {
                self.take_tag(&mut properties)?;
            }
        } else if self.check(&[Kind::Tag])? {
            self.take_tag(&mut properties)?;
            if self.check(&[Kind::Anchor])? {
                self.take_anchor(&mut properties)?;
            }
        }

        Ok(properties)
    }

    fn parse_node(&mut self, context: Context) -> Result<Marked, Fault> {
        if self.check(&[Kind::Alias])? {
            let token = self.scanner.get_token()?;
            self.pop_state();
            let TokenKind::Alias(name) = token.kind else {
                return Err(Fault::Line(token.start));
            };

            return Ok(Marked {
                event: Event::Alias(name),
                line: token.start,
            });
        }

        let Properties {
            anchor,
            tag: raw_tag,
            start,
        } = self.parse_properties()?;
        let tag = match raw_tag {
            Some(RawTag {
                handle: Some(handle),
                suffix,
                line,
            }) => {
                let Some(prefix) = self.tag_handles.get(&handle) else {
                    return Err(Fault::Line(line));
                };

                Some(format!("{prefix}{suffix}"))
            }
            Some(RawTag { suffix, .. }) => Some(suffix),
            None => None,
        };
        let line = match start {
            Some(line) => line,
            None => self.scanner.peek_token()?.1,
        };
        let block = context != Context::Flow;
        if context == Context::BlockOrIndentless && self.check(&[Kind::BlockEntry])? {
            self.state = Some(State::IndentlessSequenceEntry);
            return Ok(Marked {
                event: Event::SequenceStart { anchor, tag },
                line,
            });
        }

        if self.check(&[Kind::Scalar])? {
            let token = self.scanner.get_token()?;
            self.pop_state();
            let TokenKind::Scalar { value, plain } = token.kind else {
                return Err(Fault::Line(token.start));
            };
            let resolves = (plain && tag.is_none()) || tag.as_deref() == Some(TAG_NON_SPECIFIC);

            return Ok(Marked {
                event: Event::Scalar {
                    anchor,
                    tag,
                    resolves,
                    value,
                },
                line,
            });
        }

        let (event, next) = if self.check(&[Kind::FlowSequenceStart])? {
            (
                Event::SequenceStart { anchor, tag },
                State::FlowSequenceFirstEntry,
            )
        } else if self.check(&[Kind::FlowMappingStart])? {
            (
                Event::MappingStart { anchor, tag },
                State::FlowMappingFirstKey,
            )
        } else if block && self.check(&[Kind::BlockSequenceStart])? {
            (
                Event::SequenceStart { anchor, tag },
                State::BlockSequenceFirstEntry,
            )
        } else if block && self.check(&[Kind::BlockMappingStart])? {
            (
                Event::MappingStart { anchor, tag },
                State::BlockMappingFirstKey,
            )
        } else if anchor.is_some() || tag.is_some() {
            let resolves = tag.is_none() || tag.as_deref() == Some(TAG_NON_SPECIFIC);
            self.pop_state();

            return Ok(Marked {
                event: Event::Scalar {
                    anchor,
                    tag,
                    resolves,
                    value: Text {
                        text: String::new(),
                        lossy: false,
                    },
                },
                line,
            });
        } else {
            let (_, found, _) = self.scanner.peek_token()?;
            return Err(Fault::Line(found));
        };
        self.state = Some(next);

        Ok(Marked { event, line })
    }

    /// Ends a block collection, or refuses the token that is there.
    fn parse_block_end(&mut self, event: Event) -> Result<Marked, Fault> {
        if !self.check(&[Kind::BlockEnd])? {
            let (_, found, _) = self.scanner.peek_token()?;
            return Err(Fault::Line(found));
        }

        let token = self.scanner.get_token()?;
        self.pop_state();

        Ok(Marked {
            event,
            line: token.start,
        })
    }

    fn parse_block_sequence_entry(&mut self) -> Result<Marked, Fault> {
        if !self.check(&[Kind::BlockEntry])? {
            return self.parse_block_end(Event::SequenceEnd);
        }

        let token = self.scanner.get_token()?;
        if self.check(&[Kind::BlockEntry, Kind::BlockEnd])? {
            self.state = Some(State::BlockSequenceEntry);
            return Ok(empty_scalar(token.end));
        }

        self.states.push(State::BlockSequenceEntry);
        self.parse_node(Context::Block)
    }

    fn parse_indentless_sequence_entry(&mut self) -> Result<Marked, Fault> {
        if !self.check(&[Kind::BlockEntry])? {
            let (_, start, _) = self.scanner.peek_token()?;
            self.pop_state();

            return Ok(Marked {
                event: Event::SequenceEnd,
                line: start,
            });
        }

        let token = self.scanner.get_token()?;
        if self.check(&[Kind::BlockEntry, Kind::Key, Kind::Value, Kind::BlockEnd])? {
            self.state = Some(State::IndentlessSequenceEntry);
            return Ok(empty_scalar(token.end));
        }

        self.states.push(State::IndentlessSequenceEntry);
        self.parse_node(Context::Block)
    }

    fn parse_block_mapping_key(&mut self) -> Result<Marked, Fault> {
        if !self.check(&[Kind::Key])? {
            return self.parse_block_end(Event::MappingEnd);
        }

        let token = self.scanner.get_token()?;
        if self.check(&[Kind::Key, Kind::Value, Kind::BlockEnd])? {
            self.state = Some(State::BlockMappingValue);
            return Ok(empty_scalar(token.end));
        }

        self.states.push(State::BlockMappingValue);
        self.parse_node(Context::BlockOrIndentless)
    }

    fn parse_block_mapping_value(&mut self) -> Result<Marked, Fault> {
        if !self.check(&[Kind::Value])? {
            self.state = Some(State::BlockMappingKey);
            let (_, start, _) = self.scanner.peek_token()?;
            return Ok(empty_scalar(start));
        }

        let token = self.scanner.get_token()?;
        if self.check(&[Kind::Key, Kind::Value, Kind::BlockEnd])? {
            self.state = Some(State::BlockMappingKey);
            return Ok(empty_scalar(token.end));
        }

        self.states.push(State::BlockMappingKey);
        self.parse_node(Context::BlockOrIndentless)
    }

    /// Reads the `,` before an entry of a flow collection that is not the
    /// first one.
    fn parse_flow_separator(&mut self, place: Place) -> Result<(), Fault> {
        if place == Place::First {
            return Ok(());
        }

        if !self.check(&[Kind::FlowEntry])? {
            let (_, found, _) = self.scanner.peek_token()?;
            return Err(Fault::Line(found));
        }

        self.scanner.get_token()?;

        Ok(())
    }

    fn parse_flow_end(&mut self, event: Event) -> Result<Marked, Fault> {
        let token = self.scanner.get_token()?;
        self.pop_state();

        Ok(Marked {
            event,
            line: token.start,
        })
    }

    fn parse_flow_sequence_entry(&mut self, place: Place) -> Result<Marked, Fault> {
        if self.check(&[Kind::FlowSequenceEnd])? {
            return self.parse_flow_end(Event::SequenceEnd);
        }

        self.parse_flow_separator(place)?;
        if self.check(&[Kind::Key])? {
            let (_, start, _) = self.scanner.peek_token()?;
            self.state = Some(State::FlowSequenceEntryMappingKey);

            return Ok(Marked {
                event: Event::MappingStart {
                    anchor: None,
                    tag: None,
                },
                line: start,
            });
        }

        if self.check(&[Kind::FlowSequenceEnd])? {
            return self.parse_flow_end(Event::SequenceEnd);
        }

        self.states.push(State::FlowSequenceEntry);
        self.parse_node(Context::Flow)
    }

    fn parse_flow_sequence_entry_mapping_key(&mut self) -> Result<Marked, Fault> {
        let token = self.scanner.get_token()?;
        if self.check(&[Kind::Value, Kind::FlowEntry, Kind::FlowSequenceEnd])? {
            self.state = Some(State::FlowSequenceEntryMappingValue);
            return Ok(empty_scalar(token.end));
        }

        self.states.push(State::FlowSequenceEntryMappingValue);
        self.parse_node(Context::Flow)
    }

    fn parse_flow_sequence_entry_mapping_value(&mut self) -> Result<Marked, Fault> {
        if !self.check(&[Kind::Value])? {
            self.state = Some(State::FlowSequenceEntryMappingEnd);
            let (_, start, _) = self.scanner.peek_token()?;
            return Ok(empty_scalar(start));
        }

        let token = self.scanner.get_token()?;
        if self.check(&[Kind::FlowEntry, Kind::FlowSequenceEnd])? {
            self.state = Some(State::FlowSequenceEntryMappingEnd);
            return Ok(empty_scalar(token.end));
        }

        self.states.push(State::FlowSequenceEntryMappingEnd);
        self.parse_node(Context::Flow)
    }

    fn parse_flow_mapping_key(&mut self, place: Place) -> Result<Marked, Fault> {
        if self.check(&[Kind::FlowMappingEnd])? {
            return self.parse_flow_end(Event::MappingEnd);
        }

        self.parse_flow_separator(place)?;
        if self.check(&[Kind::Key])? {
            let token = self.scanner.get_token()?;
            if self.check(&[Kind::Value, Kind::FlowEntry, Kind::FlowMappingEnd])? {
                self.state = Some(State::FlowMappingValue);
                return Ok(empty_scalar(token.end));
            }

            self.states.push(State::FlowMappingValue);
            return self.parse_node(Context::Flow);
        }

        if self.check(&[Kind::FlowMappingEnd])? {
            return self.parse_flow_end(Event::MappingEnd);
        }

        self.states.push(State::FlowMappingEmptyValue);
        self.parse_node(Context::Flow)
    }

    fn parse_flow_mapping_value(&mut self) -> Result<Marked, Fault> {
        if !self.check(&[Kind::Value])? {
            self.state = Some(State::FlowMappingKey);
            let (_, start, _) = self.scanner.peek_token()?;
            return Ok(empty_scalar(start));
        }

        let token = self.scanner.get_token()?;
        if self.check(&[Kind::FlowEntry, Kind::FlowMappingEnd])? {
            self.state = Some(State::FlowMappingKey);
            return Ok(empty_scalar(token.end));
        }

        self.states.push(State::FlowMappingKey);
        self.parse_node(Context::Flow)
    }
}

// --- stage 4: the composer ---

/// The place of one node in the list of the nodes.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
struct NodeId(usize);

#[derive(Debug)]
enum NodeKind {
    Scalar(Text),
    Sequence(Vec<NodeId>),
    Mapping(Vec<(NodeId, NodeId)>),
}

/// One node: its tag, its content and the line of its start mark.
#[derive(Debug)]
struct Node {
    tag: String,
    kind: NodeKind,
    line: usize,
}

struct Composer {
    parser: Parser,
    nodes: Vec<Node>,
    anchors: BTreeMap<String, NodeId>,
}

impl Composer {
    fn new(parser: Parser) -> Self {
        Self {
            parser,
            nodes: Vec::new(),
            anchors: BTreeMap::new(),
        }
    }

    /// The one document of the stream. `None` for a stream with no document.
    fn single(&mut self) -> Result<Option<NodeId>, Fault> {
        self.parser.get_event()?;
        let mut document = None;
        if !self.parser.check_event(EventKind::StreamEnd)? {
            document = Some(self.compose_document()?);
        }

        if !self.parser.check_event(EventKind::StreamEnd)? {
            let event = self.parser.get_event()?;
            return Err(Fault::Line(event.line));
        }

        self.parser.get_event()?;

        Ok(document)
    }

    fn compose_document(&mut self) -> Result<NodeId, Fault> {
        self.parser.get_event()?;
        let node = self.compose_node(0)?;
        self.parser.get_event()?;
        self.anchors.clear();

        Ok(node)
    }

    fn add(&mut self, tag: String, kind: NodeKind, line: usize, anchor: Option<String>) -> NodeId {
        let id = NodeId(self.nodes.len());
        self.nodes.push(Node { tag, kind, line });
        if let Some(anchor) = anchor {
            self.anchors.insert(anchor, id);
        }

        id
    }

    fn compose_node(&mut self, depth: usize) -> Result<NodeId, Fault> {
        if self.parser.check_event(EventKind::Alias)? {
            let marked = self.parser.get_event()?;
            let Event::Alias(name) = marked.event else {
                return Err(Fault::Line(marked.line));
            };

            return self
                .anchors
                .get(&name)
                .copied()
                .ok_or(Fault::Line(marked.line));
        }

        let (kind, line, anchor) = self.parser.peek_event()?;
        if anchor.is_some_and(|name| self.anchors.contains_key(&name)) {
            return Err(Fault::Line(line));
        }

        match kind {
            EventKind::Scalar => self.compose_scalar(),
            EventKind::SequenceStart => self.compose_sequence(depth),
            EventKind::MappingStart => self.compose_mapping(depth),
            // The parser gives no other event where a node starts.
            _ => Err(Fault::Line(line)),
        }
    }

    fn compose_scalar(&mut self) -> Result<NodeId, Fault> {
        let marked = self.parser.get_event()?;
        let Event::Scalar {
            anchor,
            tag,
            resolves,
            value,
        } = marked.event
        else {
            return Err(Fault::Line(marked.line));
        };
        let tag = match tag {
            Some(tag) if tag != TAG_NON_SPECIFIC => tag,
            _ if resolves => resolve_scalar(value.as_str()).to_owned(),
            _ => TAG_STR.to_owned(),
        };

        Ok(self.add(tag, NodeKind::Scalar(value), marked.line, anchor))
    }

    fn compose_sequence(&mut self, depth: usize) -> Result<NodeId, Fault> {
        if depth >= DEPTH_MAX {
            return Err(Fault::Deep);
        }

        let marked = self.parser.get_event()?;
        let Event::SequenceStart { anchor, tag } = marked.event else {
            return Err(Fault::Line(marked.line));
        };
        let tag = collection_tag(tag, TAG_SEQ);
        let id = self.add(tag, NodeKind::Sequence(Vec::new()), marked.line, anchor);
        let mut items = Vec::new();
        while !self.parser.check_event(EventKind::SequenceEnd)? {
            items.push(self.compose_node(depth + 1)?);
        }

        self.parser.get_event()?;
        if let Some(node) = self.nodes.get_mut(id.0) {
            node.kind = NodeKind::Sequence(items);
        }

        Ok(id)
    }

    fn compose_mapping(&mut self, depth: usize) -> Result<NodeId, Fault> {
        if depth >= DEPTH_MAX {
            return Err(Fault::Deep);
        }

        let marked = self.parser.get_event()?;
        let Event::MappingStart { anchor, tag } = marked.event else {
            return Err(Fault::Line(marked.line));
        };
        let tag = collection_tag(tag, TAG_MAP);
        let id = self.add(tag, NodeKind::Mapping(Vec::new()), marked.line, anchor);
        let mut pairs = Vec::new();
        while !self.parser.check_event(EventKind::MappingEnd)? {
            let key = self.compose_node(depth + 1)?;
            let value = self.compose_node(depth + 1)?;
            pairs.push((key, value));
        }

        self.parser.get_event()?;
        if let Some(node) = self.nodes.get_mut(id.0) {
            node.kind = NodeKind::Mapping(pairs);
        }

        Ok(id)
    }
}

/// The tag of a collection: its own tag, or the default one.
fn collection_tag(tag: Option<String>, default: &str) -> String {
    match tag {
        Some(tag) if tag != TAG_NON_SPECIFIC => tag,
        _ => default.to_owned(),
    }
}

// --- the implicit resolvers ---

/// The texts that a pattern of PyYAML sees as the whole value. A `$` of a
/// Python pattern also matches before a final line feed.
fn dollar_forms(value: &str) -> impl Iterator<Item = &str> {
    [Some(value), value.strip_suffix('\n')]
        .into_iter()
        .flatten()
}

/// One implicit resolver of PyYAML: whether the first character of the value
/// selects it, its pattern and its tag.
type Resolver = (bool, fn(&str) -> bool, &'static str);

/// The tag that PyYAML gives a scalar with no tag.
fn resolve_scalar(value: &str) -> &'static str {
    let first = value.chars().next();
    let starts = |set: &str| first.is_some_and(|character| set.contains(character));
    let resolvers: [Resolver; 8] = [
        (starts("yYnNtTfFoO"), is_bool, TAG_BOOL),
        (starts("-+0123456789."), is_float, TAG_FLOAT),
        (starts("-+0123456789"), is_int, TAG_INT),
        (starts("<"), |text| text == "<<", TAG_MERGE),
        (first.is_none() || starts("~nN"), is_null, TAG_NULL),
        (starts("0123456789"), is_timestamp, TAG_TIMESTAMP),
        (starts("="), |text| text == "=", TAG_VALUE),
        (
            starts("!&*"),
            |text| matches!(text, "!" | "&" | "*"),
            TAG_YAML,
        ),
    ];
    for (applies, matches, tag) in resolvers {
        if applies && dollar_forms(value).any(matches) {
            return tag;
        }
    }

    TAG_STR
}

fn is_bool(text: &str) -> bool {
    matches!(
        text,
        "yes"
            | "Yes"
            | "YES"
            | "no"
            | "No"
            | "NO"
            | "true"
            | "True"
            | "TRUE"
            | "false"
            | "False"
            | "FALSE"
            | "on"
            | "On"
            | "ON"
            | "off"
            | "Off"
            | "OFF"
    )
}

fn is_null(text: &str) -> bool {
    matches!(text, "~" | "null" | "Null" | "NULL" | "")
}

fn unsigned(text: &str) -> &str {
    text.strip_prefix(['-', '+']).unwrap_or(text)
}

fn all(text: &str, class: fn(char) -> bool) -> bool {
    text.chars().all(class)
}

fn digit_or_underscore(character: char) -> bool {
    character.is_ascii_digit() || character == '_'
}

/// `[0-9][0-9_]*`.
fn is_digits_run(text: &str) -> bool {
    text.starts_with(|first: char| first.is_ascii_digit()) && all(text, digit_or_underscore)
}

/// `[1-9][0-9_]*`.
fn is_positive_run(text: &str) -> bool {
    text.starts_with(|first: char| matches!(first, '1'..='9')) && all(text, digit_or_underscore)
}

/// `[0-5]?[0-9]`: one part of a base 60 number after the first part.
fn is_sexagesimal_part(part: &str) -> bool {
    match part.as_bytes() {
        [digit] => digit.is_ascii_digit(),
        [tens, units] => matches!(tens, b'0'..=b'5') && units.is_ascii_digit(),
        _ => false,
    }
}

/// `<first>(:[0-5]?[0-9])+`, with `first` as the pattern of the first part.
fn is_sexagesimal(text: &str, first: fn(&str) -> bool) -> bool {
    let mut parts = text.split(':');
    let head = parts.next().is_some_and(first);
    let mut count = 0;
    let rest = parts.all(|part| {
        count += 1;
        is_sexagesimal_part(part)
    });

    head && rest && count > 0
}

/// `[eE][-+][0-9]+`.
fn is_exponent(text: &str) -> bool {
    let Some(rest) = text.strip_prefix(['e', 'E']) else {
        return false;
    };
    let Some(digits) = rest.strip_prefix(['-', '+']) else {
        return false;
    };

    !digits.is_empty() && all(digits, |character| character.is_ascii_digit())
}

/// `<digits>([eE][-+][0-9]+)?`, with `digits` as the pattern before the
/// exponent.
fn with_optional_exponent(text: &str, digits: fn(&str) -> bool) -> bool {
    match text.find(['e', 'E']) {
        Some(at) => {
            let (mantissa, exponent) = text.split_at(at);
            digits(mantissa) && is_exponent(exponent)
        }
        None => digits(text),
    }
}

fn is_float(text: &str) -> bool {
    if matches!(unsigned(text), ".inf" | ".Inf" | ".INF")
        || matches!(text, ".nan" | ".NaN" | ".NAN")
    {
        return true;
    }

    // `\.[0-9][0-9_]*([eE][-+][0-9]+)?`
    if let Some(rest) = text.strip_prefix('.') {
        return with_optional_exponent(rest, is_digits_run);
    }

    let Some((whole, fraction)) = unsigned(text).split_once('.') else {
        return false;
    };

    // `[-+]?[0-9][0-9_]*\.[0-9_]*([eE][-+][0-9]+)?`
    let plain = is_digits_run(whole)
        && with_optional_exponent(fraction, |digits| all(digits, digit_or_underscore));
    // `[-+]?[0-9][0-9_]*(:[0-5]?[0-9])+\.[0-9_]*`
    let base_60 = is_sexagesimal(whole, is_digits_run) && all(fraction, digit_or_underscore);

    plain || base_60
}

fn is_int(text: &str) -> bool {
    let body = unsigned(text);
    if let Some(digits) = body.strip_prefix("0b") {
        return !digits.is_empty() && all(digits, |digit| matches!(digit, '0' | '1' | '_'));
    }

    if let Some(digits) = body.strip_prefix("0x") {
        return !digits.is_empty()
            && all(digits, |digit| digit.is_ascii_hexdigit() || digit == '_');
    }

    if body == "0" || is_positive_run(body) {
        return true;
    }

    if let Some(digits) = body.strip_prefix('0') {
        return !digits.is_empty() && all(digits, |digit| matches!(digit, '0'..='7' | '_'));
    }

    is_sexagesimal(body, is_positive_run)
}

/// Takes `count` ASCII digits from the start of `text`.
fn take_digits(text: &str, count: usize) -> Option<&str> {
    let digits = text.get(..count)?;
    if !all(digits, |digit| digit.is_ascii_digit()) {
        return None;
    }

    text.get(count..)
}

/// Takes one or two ASCII digits, two when two are there.
fn take_one_or_two_digits(text: &str) -> Option<&str> {
    take_digits(text, 2).or_else(|| take_digits(text, 1))
}

fn is_blank(character: char) -> bool {
    matches!(character, ' ' | '\t')
}

/// The time zone of a timestamp: `Z|[-+][0-9][0-9]?(:[0-9][0-9])?`.
fn is_time_zone(text: &str) -> bool {
    if text == "Z" {
        return true;
    }

    let Some(hours) = text.strip_prefix(['-', '+']) else {
        return false;
    };
    let Some(rest) = take_one_or_two_digits(hours) else {
        return false;
    };

    match rest.strip_prefix(':') {
        Some(minutes) => take_digits(minutes, 2) == Some(""),
        None => rest.is_empty(),
    }
}

/// The part of a timestamp after the day: a `T` or blanks, the time, a
/// fraction and a time zone.
fn is_time_of_day(text: &str) -> bool {
    let after_gap = match text.strip_prefix(['T', 't']) {
        Some(rest) => rest,
        None => {
            let rest = text.trim_start_matches(is_blank);
            if rest.len() == text.len() {
                return false;
            }

            rest
        }
    };
    let Some(rest) = take_one_or_two_digits(after_gap).and_then(|rest| rest.strip_prefix(':'))
    else {
        return false;
    };
    let Some(rest) = take_digits(rest, 2).and_then(|rest| rest.strip_prefix(':')) else {
        return false;
    };
    let Some(mut rest) = take_digits(rest, 2) else {
        return false;
    };
    if let Some(fraction) = rest.strip_prefix('.') {
        rest = fraction.trim_start_matches(|digit: char| digit.is_ascii_digit());
    }

    rest.is_empty() || is_time_zone(rest.trim_start_matches(is_blank))
}

fn is_timestamp(text: &str) -> bool {
    let Some(rest) = take_digits(text, 4).and_then(|rest| rest.strip_prefix('-')) else {
        return false;
    };

    // `[0-9]{4}-[0-9]{2}-[0-9]{2}`
    let date = take_digits(rest, 2)
        .and_then(|rest| rest.strip_prefix('-'))
        .and_then(|rest| take_digits(rest, 2))
        == Some("");
    if date {
        return true;
    }

    let Some(rest) = take_one_or_two_digits(rest).and_then(|rest| rest.strip_prefix('-')) else {
        return false;
    };

    // The day takes two digits when the time can still follow them.
    [take_digits(rest, 2), take_digits(rest, 1)]
        .into_iter()
        .flatten()
        .any(is_time_of_day)
}

// --- stage 5: the constructor ---

/// A step that PyYAML runs after the value of a collection exists.
struct Pending {
    node: NodeId,
    value: ValueId,
    step: Step,
}

/// What a pending step fills.
#[derive(Clone, Copy, PartialEq, Eq)]
enum Step {
    Sequence,
    Mapping,
    Set,
    /// `!!omap` and `!!pairs`: a list of pairs.
    Pairs,
}

struct Constructor {
    nodes: Vec<Node>,
    values: Vec<Value>,
    constructed: BTreeMap<NodeId, ValueId>,
    pending: VecDeque<Pending>,
    /// The count of pairs that the merge keys can still copy.
    merge_budget: usize,
}

impl Constructor {
    fn new(nodes: Vec<Node>) -> Self {
        Self {
            nodes,
            values: Vec::new(),
            constructed: BTreeMap::new(),
            pending: VecDeque::new(),
            merge_budget: MERGE_PAIRS_MAX,
        }
    }

    fn line(&self, node: NodeId) -> usize {
        self.nodes.get(node.0).map_or(0, |node| node.line)
    }

    fn fault(&self, node: NodeId) -> Fault {
        Fault::Line(self.line(node))
    }

    fn tag(&self, node: NodeId) -> &str {
        self.nodes.get(node.0).map_or("", |node| node.tag.as_str())
    }

    fn push(&mut self, value: Value) -> ValueId {
        let id = ValueId(self.values.len());
        self.values.push(value);

        id
    }

    fn document(&mut self, root: NodeId) -> Result<ValueId, Fault> {
        let value = self.construct_object(root)?;
        while let Some(step) = self.pending.pop_front() {
            self.run(&step)?;
        }

        Ok(value)
    }

    fn construct_object(&mut self, node: NodeId) -> Result<ValueId, Fault> {
        if let Some(value) = self.constructed.get(&node) {
            return Ok(*value);
        }

        let tag = self.tag(node).to_owned();
        let collection = match tag.as_str() {
            TAG_SEQ => Some((Value::List(Vec::new()), Step::Sequence)),
            TAG_MAP => Some((Value::Map(Map::default()), Step::Mapping)),
            TAG_SET => Some((Value::Other, Step::Set)),
            TAG_OMAP | TAG_PAIRS => Some((Value::List(Vec::new()), Step::Pairs)),
            _ => None,
        };
        let value = match collection {
            Some((empty, step)) => {
                let value = self.push(empty);
                self.pending.push_back(Pending { node, value, step });
                value
            }
            None => {
                let scalar = self.construct_scalar_value(node, &tag)?;
                self.push(scalar)
            }
        };
        self.constructed.insert(node, value);

        Ok(value)
    }

    /// The text of a scalar node. A mapping with the key `=` gives the text
    /// of the value of that key.
    fn construct_scalar(&self, node: NodeId) -> Result<&Text, Fault> {
        let mut current = node;
        // The count of nodes bounds the walk: a mapping can be its own `=`
        // value.
        for _ in 0..=self.nodes.len() {
            match self.nodes.get(current.0).map(|node| &node.kind) {
                Some(NodeKind::Scalar(text)) => return Ok(text),
                Some(NodeKind::Mapping(pairs)) => {
                    let value = pairs
                        .iter()
                        .find(|(key, _)| self.tag(*key) == TAG_VALUE)
                        .map(|(_, value)| *value);
                    match value {
                        Some(value) => current = value,
                        None => return Err(self.fault(current)),
                    }
                }
                _ => return Err(self.fault(current)),
            }
        }

        Err(self.fault(node))
    }

    fn is_scalar(&self, node: NodeId) -> bool {
        matches!(
            self.nodes.get(node.0).map(|node| &node.kind),
            Some(NodeKind::Scalar(_))
        )
    }

    fn construct_scalar_value(&self, node: NodeId, tag: &str) -> Result<Value, Fault> {
        // A tag with no constructor: PyYAML refuses the node.
        if !matches!(
            tag,
            TAG_NULL | TAG_BOOL | TAG_INT | TAG_FLOAT | TAG_BINARY | TAG_TIMESTAMP | TAG_STR
        ) {
            return Err(self.fault(node));
        }

        let text = self.construct_scalar(node)?;
        let no_value = self.fault(node);
        match tag {
            TAG_NULL => Ok(Value::Null),
            TAG_BOOL => bool_value(text.as_str()).map(Value::Bool).ok_or(no_value),
            TAG_INT => int_value(text.as_str()).map(Value::Int).ok_or(no_value),
            // No field of a manifest takes a float or a time, so the reader
            // keeps only that the text is one.
            TAG_FLOAT if is_float_value(text.as_str()) => Ok(Value::Float),
            TAG_TIMESTAMP if self.is_scalar(node) && is_timestamp_value(text.as_str()) => {
                Ok(Value::Other)
            }
            TAG_FLOAT | TAG_TIMESTAMP => Err(no_value),
            TAG_BINARY if base64_decodes(text.as_str()) => Ok(Value::Other),
            TAG_BINARY => Err(no_value),
            _ => Ok(Value::Text(text.clone())),
        }
    }

    fn run(&mut self, step: &Pending) -> Result<(), Fault> {
        match step.step {
            Step::Sequence => self.fill_sequence(step.node, step.value),
            Step::Mapping | Step::Set => self.fill_mapping(step.node, step.value, step.step),
            Step::Pairs => self.fill_pairs(step.node, step.value),
        }
    }

    fn fill_sequence(&mut self, node: NodeId, value: ValueId) -> Result<(), Fault> {
        let Some(NodeKind::Sequence(children)) = self.nodes.get(node.0).map(|node| &node.kind)
        else {
            return Err(self.fault(node));
        };
        let children = children.clone();
        let mut items = Vec::with_capacity(children.len());
        for child in children {
            items.push(self.construct_object(child)?);
        }

        if let Some(slot) = self.values.get_mut(value.0) {
            *slot = Value::List(items);
        }

        Ok(())
    }

    fn fill_mapping(&mut self, node: NodeId, value: ValueId, step: Step) -> Result<(), Fault> {
        if !matches!(
            self.nodes.get(node.0).map(|node| &node.kind),
            Some(NodeKind::Mapping(_))
        ) {
            return Err(self.fault(node));
        }

        self.flatten_mapping(node, 0)?;
        let Some(NodeKind::Mapping(pairs)) = self.nodes.get(node.0).map(|node| &node.kind) else {
            return Err(self.fault(node));
        };
        let pairs = pairs.clone();
        let mut map = Map::default();
        for (key_node, value_node) in pairs {
            let key = self.construct_object(key_node)?;
            // A list, a mapping and a set have no hash in Python.
            let unhashable = matches!(
                self.tag(key_node),
                TAG_SEQ | TAG_MAP | TAG_SET | TAG_OMAP | TAG_PAIRS
            );
            if unhashable {
                return Err(self.fault(key_node));
            }

            let item = self.construct_object(value_node)?;
            match self.values.get(key.0) {
                Some(Value::Text(text)) => {
                    map.entries.insert(text.as_str().to_owned(), item);
                }
                _ => map.other_key = true,
            }
        }

        if step == Step::Mapping
            && let Some(slot) = self.values.get_mut(value.0)
        {
            *slot = Value::Map(map);
        }

        Ok(())
    }

    fn fill_pairs(&mut self, node: NodeId, value: ValueId) -> Result<(), Fault> {
        let Some(NodeKind::Sequence(children)) = self.nodes.get(node.0).map(|node| &node.kind)
        else {
            return Err(self.fault(node));
        };
        let children = children.clone();
        let mut items = Vec::with_capacity(children.len());
        for child in children {
            let pair = match self.nodes.get(child.0).map(|node| &node.kind) {
                Some(NodeKind::Mapping(pairs)) => match pairs.as_slice() {
                    [pair] => *pair,
                    _ => return Err(self.fault(child)),
                },
                _ => return Err(self.fault(child)),
            };
            self.construct_object(pair.0)?;
            self.construct_object(pair.1)?;
            items.push(self.push(Value::Other));
        }

        if let Some(slot) = self.values.get_mut(value.0) {
            *slot = Value::List(items);
        }

        Ok(())
    }

    /// Puts the pairs of each `<<` value at the start of a mapping node, and
    /// makes each `=` key a string key. The step changes the node, as PyYAML
    /// does.
    fn flatten_mapping(&mut self, node: NodeId, depth: usize) -> Result<(), Fault> {
        if depth > MERGE_DEPTH_MAX {
            return Err(Fault::Deep);
        }

        let mut merge: Vec<(NodeId, NodeId)> = Vec::new();
        let mut index = 0;
        loop {
            let Some(NodeKind::Mapping(pairs)) = self.nodes.get(node.0).map(|node| &node.kind)
            else {
                return Ok(());
            };
            let Some((key_node, value_node)) = pairs.get(index).copied() else {
                break;
            };
            match self.tag(key_node) {
                TAG_MERGE => {
                    self.remove_pair(node, index);
                    merge.extend(self.merged_pairs(value_node, depth)?);
                }
                TAG_VALUE => {
                    if let Some(key) = self.nodes.get_mut(key_node.0) {
                        TAG_STR.clone_into(&mut key.tag);
                    }

                    index += 1;
                }
                _ => index += 1,
            }
        }

        if merge.is_empty() {
            return Ok(());
        }

        if let Some(NodeKind::Mapping(pairs)) =
            self.nodes.get_mut(node.0).map(|node| &mut node.kind)
        {
            merge.append(pairs);
            *pairs = merge;
        }

        Ok(())
    }

    fn remove_pair(&mut self, node: NodeId, index: usize) {
        if let Some(NodeKind::Mapping(pairs)) =
            self.nodes.get_mut(node.0).map(|node| &mut node.kind)
            && index < pairs.len()
        {
            pairs.remove(index);
        }
    }

    /// The pairs of a mapping node, as a copy that a merge key adds to
    /// another mapping. The copy counts against the budget of the document.
    fn copied_pairs(&mut self, node: NodeId) -> Result<Vec<(NodeId, NodeId)>, Fault> {
        let pairs = self.pairs_of(node).unwrap_or_default();
        self.merge_budget = self
            .merge_budget
            .checked_sub(pairs.len())
            .ok_or(Fault::Merge)?;

        Ok(pairs)
    }

    fn pairs_of(&self, node: NodeId) -> Option<Vec<(NodeId, NodeId)>> {
        match self.nodes.get(node.0).map(|node| &node.kind) {
            Some(NodeKind::Mapping(pairs)) => Some(pairs.clone()),
            _ => None,
        }
    }

    /// The pairs that one `<<` value adds to a mapping.
    fn merged_pairs(
        &mut self,
        value_node: NodeId,
        depth: usize,
    ) -> Result<Vec<(NodeId, NodeId)>, Fault> {
        let children = match self.nodes.get(value_node.0).map(|node| &node.kind) {
            Some(NodeKind::Mapping(_)) => {
                self.flatten_mapping(value_node, depth + 1)?;
                return self.copied_pairs(value_node);
            }
            Some(NodeKind::Sequence(children)) => children.clone(),
            _ => return Err(self.fault(value_node)),
        };
        let mut submerge = Vec::with_capacity(children.len());
        for child in children {
            if self.pairs_of(child).is_none() {
                return Err(self.fault(child));
            }

            self.flatten_mapping(child, depth + 1)?;
            submerge.push(self.copied_pairs(child)?);
        }

        Ok(submerge.into_iter().rev().flatten().collect())
    }
}

/// The value of a `!!bool` scalar.
fn bool_value(text: &str) -> Option<bool> {
    match text.to_ascii_lowercase().as_str() {
        "yes" | "true" | "on" => Some(true),
        "no" | "false" | "off" => Some(false),
        _ => None,
    }
}

/// A space that Python `int` and `float` take around an ASCII number: the
/// space and U+0009 to U+000D. U+001C to U+001F are not such spaces.
fn is_python_space(character: char) -> bool {
    matches!(character, ' ' | '\t'..='\r')
}

/// What Python `int(text, base)` gives for an ASCII text with no underscore.
///
/// `None` is a text that is no integer. `Some(None)` is an integer that does
/// not fit 64 bits.
///
/// CONTRACT-QUESTION: contract 06 §8 names no digit and no space outside
/// ASCII. Python `int` and `float` also read a decimal digit and a space that
/// are not ASCII: `!!int "\u0664"` is 4. This function and
/// [`is_python_float`] give no value for such a text, as rule 9 of
/// `rust/AGENTS.md` says for an id. The caller then refuses a tagged number
/// that Python reads. A change costs a table of each decimal digit and each
/// space of Unicode.
fn python_int(text: &str, base: u32) -> Option<Option<i64>> {
    let trimmed = text.trim_matches(is_python_space);
    let (negative, body) = match trimmed.strip_prefix('-') {
        Some(rest) => (true, rest),
        None => (false, trimmed.strip_prefix('+').unwrap_or(trimmed)),
    };
    let prefixes: &[&str] = match base {
        2 => &["0b", "0B"],
        8 => &["0o", "0O"],
        16 => &["0x", "0X"],
        _ => &[],
    };
    let digits = prefixes
        .iter()
        .find_map(|prefix| body.strip_prefix(prefix))
        .unwrap_or(body);
    if digits.is_empty() || !digits.chars().all(|digit| digit.is_digit(base)) {
        return None;
    }

    if base == 10 && digits.len() > INT_DIGITS_MAX {
        return None;
    }

    let mut value: Option<i64> = Some(0);
    for digit in digits.chars().filter_map(|digit| digit.to_digit(base)) {
        value = value
            .and_then(|value| value.checked_mul(i64::from(base)))
            .and_then(|value| value.checked_add(i64::from(digit)));
    }

    Some(if negative {
        value.and_then(i64::checked_neg)
    } else {
        value
    })
}

/// The value of a `!!int` scalar, as `construct_yaml_int` of PyYAML gives it.
///
/// `None` is a text that is no integer. `Some(None)` is an integer that does
/// not fit 64 bits.
fn int_value(text: &str) -> Option<Option<i64>> {
    let cleaned = text.replace('_', "");
    let first = cleaned.chars().next()?;
    let negative = first == '-';
    let body = cleaned.strip_prefix(['-', '+']).unwrap_or(&cleaned);
    let magnitude = if body == "0" {
        Some(0)
    } else if let Some(digits) = body.strip_prefix("0b") {
        python_int(digits, 2)?
    } else if let Some(digits) = body.strip_prefix("0x") {
        python_int(digits, 16)?
    } else if body.starts_with('0') {
        python_int(body, 8)?
    } else if body.is_empty() {
        // A sign with no digit is no integer.
        return None;
    } else if body.contains(':') {
        let mut total: Option<i64> = Some(0);
        let mut base: Option<i64> = Some(1);
        let parts: Vec<&str> = body.split(':').collect();
        for part in parts.into_iter().rev() {
            let digit = python_int(part, 10)?;
            let term = digit
                .zip(base)
                .and_then(|(digit, base)| digit.checked_mul(base));
            total = total
                .zip(term)
                .and_then(|(total, term)| total.checked_add(term));
            base = base.and_then(|base| base.checked_mul(60));
        }

        total
    } else {
        python_int(body, 10)?
    };

    Some(if negative {
        magnitude.and_then(i64::checked_neg)
    } else {
        magnitude
    })
}

/// Whether Python `float(text)` takes an ASCII text with no underscore, in
/// lower case: `inf`, `infinity`, `nan` or a decimal number, with a sign and
/// with spaces around it.
fn is_python_float(text: &str) -> bool {
    let trimmed = text.trim_matches(is_python_space);
    let body = trimmed.strip_prefix(['-', '+']).unwrap_or(trimmed);
    if matches!(body, "inf" | "infinity" | "nan") {
        return true;
    }

    let (mantissa, exponent) = match body.split_once('e') {
        Some((mantissa, exponent)) => (mantissa, Some(exponent)),
        None => (body, None),
    };
    let (whole, fraction) = mantissa.split_once('.').unwrap_or((mantissa, ""));
    let digits = |text: &str| all(text, |digit| digit.is_ascii_digit());
    let has_mantissa =
        digits(whole) && digits(fraction) && !(whole.is_empty() && fraction.is_empty());
    let has_exponent = exponent.is_none_or(|exponent| {
        let size = exponent.strip_prefix(['-', '+']).unwrap_or(exponent);
        !size.is_empty() && digits(size)
    });

    has_mantissa && has_exponent
}

/// Whether a `!!float` scalar has a value, as `construct_yaml_float` of
/// PyYAML reads it.
fn is_float_value(text: &str) -> bool {
    let cleaned = text.replace('_', "").to_ascii_lowercase();
    if cleaned.is_empty() {
        return false;
    }

    let body = cleaned.strip_prefix(['-', '+']).unwrap_or(&cleaned);
    if matches!(body, ".inf" | ".nan") {
        return true;
    }

    body.split(':').all(is_python_float)
}

/// The hour, the minute and the second of a timestamp.
type TimeOfDay = (u32, u32, u32);

/// The numbers of a timestamp.
struct Stamp {
    year: u32,
    month: u32,
    day: u32,
    time: Option<TimeOfDay>,
    /// The offset of the time zone, in minutes and with no sign.
    offset: Option<u32>,
}

/// Takes `count` ASCII digits from the start of `text`, as a number.
fn take_number(text: &str, count: usize) -> Option<(u32, &str)> {
    let rest = take_digits(text, count)?;
    let number = text.get(..count)?.parse().ok()?;

    Some((number, rest))
}

/// Takes one or two ASCII digits as a number: the two readings, the longer
/// one first.
fn take_short_number(text: &str) -> impl Iterator<Item = (u32, &str)> {
    [take_number(text, 2), take_number(text, 1)]
        .into_iter()
        .flatten()
}

/// The offset of a time zone in minutes: `Z|[-+][0-9][0-9]?(:[0-9][0-9])?`.
fn match_time_zone(text: &str) -> Option<u32> {
    if text == "Z" {
        return Some(0);
    }

    let hours = text.strip_prefix(['-', '+'])?;
    take_short_number(hours).find_map(|(hour, rest)| {
        if rest.is_empty() {
            return Some(hour * 60);
        }

        match take_number(rest.strip_prefix(':')?, 2)? {
            (minute, "") => Some(hour * 60 + minute),
            _ => None,
        }
    })
}

/// The time and the time zone of a timestamp, from the gap after the day.
fn match_time(text: &str) -> Option<(TimeOfDay, Option<u32>)> {
    let after_gap = match text.strip_prefix(['T', 't']) {
        Some(rest) => rest,
        None => {
            let rest = text.trim_start_matches(is_blank);
            if rest.len() == text.len() {
                return None;
            }

            rest
        }
    };
    take_short_number(after_gap).find_map(|(hour, rest)| {
        let (minute, rest) = take_number(rest.strip_prefix(':')?, 2)?;
        let (second, mut rest) = take_number(rest.strip_prefix(':')?, 2)?;
        if let Some(fraction) = rest.strip_prefix('.') {
            rest = fraction.trim_start_matches(|digit: char| digit.is_ascii_digit());
        }

        let time = (hour, minute, second);
        if rest.is_empty() {
            return Some((time, None));
        }

        let offset = match_time_zone(rest.trim_start_matches(is_blank))?;

        Some((time, Some(offset)))
    })
}

/// The numbers of a text that the timestamp pattern of the constructor of
/// PyYAML matches. The pattern takes a date with a day of one digit, which
/// the pattern of the resolver does not.
fn match_stamp(text: &str) -> Option<Stamp> {
    let (year, rest) = take_number(text, 4)?;
    let rest = rest.strip_prefix('-')?;
    take_short_number(rest).find_map(|(month, rest)| {
        let rest = rest.strip_prefix('-')?;
        take_short_number(rest).find_map(|(day, rest)| {
            let (time, offset) = if rest.is_empty() {
                (None, None)
            } else {
                let (time, offset) = match_time(rest)?;
                (Some(time), offset)
            };

            Some(Stamp {
                year,
                month,
                day,
                time,
                offset,
            })
        })
    })
}

/// The count of minutes in one day.
const DAY_MINUTES: u32 = 24 * 60;

/// Whether a `!!timestamp` scalar is a date or a time of the calendar, as
/// `construct_yaml_timestamp` of PyYAML reads it.
fn is_timestamp_value(text: &str) -> bool {
    let Some(stamp) = dollar_forms(text).find_map(match_stamp) else {
        return false;
    };
    let leap = stamp.year % 4 == 0 && (stamp.year % 100 != 0 || stamp.year % 400 == 0);
    let days = match stamp.month {
        1 | 3 | 5 | 7 | 8 | 10 | 12 => 31,
        4 | 6 | 9 | 11 => 30,
        2 if leap => 29,
        2 => 28,
        _ => return false,
    };
    let date = stamp.year >= 1 && (1..=days).contains(&stamp.day);
    let time = stamp
        .time
        .is_none_or(|(hour, minute, second)| hour <= 23 && minute <= 59 && second <= 59);
    let zone = stamp.offset.is_none_or(|offset| offset < DAY_MINUTES);

    date && time && zone
}

/// Whether Python `base64.decodebytes` takes the text of a `!!binary` scalar.
///
/// CONTRACT-QUESTION: contract 06 §8 names no bytes value. Two supported
/// Python versions differ on data after a complete pad sequence: Python 3.12
/// stops at the pad, and Python 3.13 reads on. This function takes the rule of
/// Python 3.13. The result changes only the detail of a refusal: no field of a
/// manifest takes bytes.
fn base64_decodes(text: &str) -> bool {
    if !text.is_ascii() {
        return false;
    }

    let mut quad = 0_u8;
    let mut pads = 0_u8;
    for byte in text.bytes() {
        if byte == b'=' {
            if quad >= 2 {
                pads = pads.saturating_add(1);
            }

            continue;
        }

        if !(byte.is_ascii_alphanumeric() || matches!(byte, b'+' | b'/')) {
            continue;
        }

        pads = 0;
        quad = (quad + 1) % 4;
    }

    quad == 0 || (quad >= 2 && quad.saturating_add(pads) >= 4)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn text_of(tree: &Tree, value: &Value) -> String {
        match value {
            Value::Null => "null".to_owned(),
            Value::Bool(value) => value.to_string(),
            Value::Int(Some(value)) => value.to_string(),
            Value::Int(None) => "big".to_owned(),
            Value::Float => "float".to_owned(),
            Value::Text(text) => format!("{:?}", text.as_str()),
            Value::Other => "other".to_owned(),
            Value::List(items) => {
                let items: Vec<String> = items
                    .iter()
                    .map(|id| text_of(tree, tree.get(*id)))
                    .collect();
                format!("[{}]", items.join(", "))
            }
            Value::Map(map) => {
                let mut items: Vec<String> = map
                    .keys()
                    .map(|key| {
                        let value = map.get(key).unwrap();
                        format!("{key}: {}", text_of(tree, tree.get(value)))
                    })
                    .collect();
                if map.has_other_key() {
                    items.push("<other key>".to_owned());
                }

                format!("{{{}}}", items.join(", "))
            }
        }
    }

    fn read(text: &str) -> String {
        match load(text) {
            Ok(tree) => text_of(&tree, tree.root()),
            Err(Fault::Unreadable) => "unreadable".to_owned(),
            Err(Fault::Line(line)) => format!("line {}", line + 1),
            Err(Fault::Deep) => "deep".to_owned(),
            Err(Fault::Merge) => "merge".to_owned(),
        }
    }

    #[test]
    fn a_block_document_reads_as_the_python_values() {
        let text = "name: attendance\nkeep: 3\nrelease: yes\nunit: ~\nlist:\n  - a\n  - [b, 1]\n";

        assert_eq!(
            read(text),
            r#"{keep: 3, list: ["a", ["b", 1]], name: "attendance", release: true, unit: null}"#
        );
    }

    #[test]
    fn each_implicit_scalar_gets_the_tag_that_pyyaml_gives() {
        let table = [
            ("yes", TAG_BOOL),
            ("Off", TAG_BOOL),
            ("y", TAG_STR),
            ("yES", TAG_STR),
            ("1.0", TAG_FLOAT),
            ("1.", TAG_FLOAT),
            (".5", TAG_FLOAT),
            ("1e3", TAG_STR),
            ("1.0e3", TAG_STR),
            ("1.0e+3", TAG_FLOAT),
            ("-.inf", TAG_FLOAT),
            (".NaN", TAG_FLOAT),
            ("+.nan", TAG_STR),
            ("1:30.5", TAG_FLOAT),
            ("0", TAG_INT),
            ("-0", TAG_INT),
            ("010", TAG_INT),
            ("09", TAG_STR),
            ("0o10", TAG_STR),
            ("0x1F", TAG_INT),
            ("0b_", TAG_INT),
            ("1_000", TAG_INT),
            ("1:30", TAG_INT),
            ("1:60", TAG_STR),
            ("0:30", TAG_STR),
            ("1:5:9", TAG_INT),
            ("<<", TAG_MERGE),
            ("=", TAG_VALUE),
            ("", TAG_NULL),
            ("~", TAG_NULL),
            ("Null", TAG_NULL),
            ("nULL", TAG_STR),
            ("2026-10-03", TAG_TIMESTAMP),
            ("2026-1-3", TAG_STR),
            ("2026-1-3 4:05:06", TAG_TIMESTAMP),
            ("2026-10-03T04:05:06.5 +01:30", TAG_TIMESTAMP),
            ("2026-10-03t04:05:06Z", TAG_TIMESTAMP),
            ("2026-10-03 04:05:06 ", TAG_STR),
            ("2026-10-03 04:05", TAG_STR),
            ("2026-10-03x", TAG_STR),
            ("yes\n", TAG_BOOL),
            ("yes\n\n", TAG_STR),
            ("12\n", TAG_INT),
        ];
        for (value, tag) in table {
            assert_eq!(resolve_scalar(value), tag, "{value:?}");
        }
    }

    #[test]
    fn an_integer_scalar_has_the_value_that_python_gives() {
        let table = [
            ("0", Some(Some(0))),
            ("-0", Some(Some(0))),
            ("+12", Some(Some(12))),
            ("1_000", Some(Some(1000))),
            ("010", Some(Some(8))),
            ("0_7", Some(Some(7))),
            ("0x1f", Some(Some(31))),
            ("-0b101", Some(Some(-5))),
            ("1:30", Some(Some(90))),
            ("1:0:0", Some(Some(3600))),
            ("0o17", Some(Some(15))),
            (" 12 ", Some(Some(12))),
            ("--5", Some(Some(5))),
            ("99999999999999999999", Some(None)),
            ("-99999999999999999999", Some(None)),
            ("0b_", None),
            ("0x", None),
            ("", None),
            ("_", None),
            ("abc", None),
            ("09", None),
            ("1 2", None),
            ("\t12\r", Some(Some(12))),
            ("1\u{1c}", None),
            ("\u{1f}1", None),
            ("0x1\u{1c}", None),
            ("1:0\u{1f}", None),
        ];
        for (text, value) in table {
            assert_eq!(int_value(text), value, "{text:?}");
        }

        let long = "9".repeat(INT_DIGITS_MAX);
        assert_eq!(int_value(&long), Some(None));
        assert_eq!(int_value(&format!("{long}9")), None);
    }

    #[test]
    fn a_float_and_a_timestamp_scalar_have_a_value_or_none() {
        let floats = [
            "1.5",
            "-1.",
            ".5",
            "1e3",
            "1_0.5",
            "+.inf",
            "-.INF",
            ".NaN",
            "1:30.5",
            " 1.5 ",
            "inf",
            "-Infinity",
            "nan",
            "1E-5",
        ];
        let stamps = [
            "2026-10-03",
            "2024-02-29",
            "2026-1-3",
            "2026-10-03 23:59:59",
            "2026-10-03T04:05:06.123456789Z",
            "2026-10-03t4:05:06 +23:59",
            "2026-10-03 04:05:06.\t-1",
            "2026-10-03\n",
        ];

        for text in floats {
            assert!(is_float_value(text), "{text:?}");
        }

        for text in [
            "",
            "_",
            "abc",
            ".",
            "1e",
            "1.5.5",
            "0x1p3",
            "1 5",
            "+-+1",
            "1:a",
            "1.5\u{1c}",
            "\u{1f}1.5",
        ] {
            assert!(!is_float_value(text), "{text:?}");
        }

        for text in stamps {
            assert!(is_timestamp_value(text), "{text:?}");
        }

        let no_stamps = [
            "",
            "abc",
            "2026-10",
            "0000-01-01",
            "2026-13-01",
            "2026-00-10",
            "2026-02-29",
            "2026-04-31",
            "2026-10-03 24:00:00",
            "2026-10-03 04:60:00",
            "2026-10-03 04:05:60",
            "2026-10-03 04:05:06 +24:00",
            "2026-10-03 04:05:06 +23:60",
            "2026-10-03 04:05",
            "2026-10-03x",
        ];
        for text in no_stamps {
            assert!(!is_timestamp_value(text), "{text:?}");
        }
    }

    #[test]
    fn a_fault_names_the_line_of_the_mark() {
        assert_eq!(read("a: 1\nb: [1, 2\n"), "line 3");
        assert_eq!(read("a: 1\n  b: 2\n"), "line 2");
        assert_eq!(read("a: *nothing\n"), "line 1");
        assert_eq!(read("a: 1\n\tb: 2\n"), "line 2");
        assert_eq!(read("a: \u{7}\n"), "unreadable");
        assert_eq!(read("a: !!python/name:os.system\n"), "line 1");
        assert_eq!(read("a: 1\n---\nb: 2\n"), "line 2");
    }

    #[test]
    fn a_merge_key_and_an_alias_read_as_pyyaml_reads_them() {
        let text = "base: &base {mode: automatic, keep: 1}\nown:\n  <<: *base\n  keep: 2\n";

        assert_eq!(
            read(text),
            r#"{base: {keep: 1, mode: "automatic"}, own: {keep: 2, mode: "automatic"}}"#
        );

        let tree = load("a: &loop [*loop]\n").unwrap();
        let Value::Map(map) = tree.root() else {
            panic!("the root is a mapping");
        };
        let list = map.get("a").unwrap();
        let Value::List(items) = tree.get(list) else {
            panic!("the value is a list");
        };

        assert_eq!(items.as_slice(), [list], "the list holds itself");
    }

    #[test]
    fn a_document_past_the_merge_budget_is_refused() {
        let document = |keys: usize, merges: usize| {
            let pairs: Vec<String> = (0..keys).map(|key| format!("k{key}: 1")).collect();
            let aliases = vec!["*a"; merges];

            format!(
                "a: &a {{{}}}\nb: {{<<: [{}]}}\n",
                pairs.join(", "),
                aliases.join(", ")
            )
        };

        assert!(load(&document(256, 256)).is_ok());
        assert_eq!(load(&document(256, 257)).unwrap_err(), Fault::Merge);
    }

    #[test]
    fn a_merge_chain_past_the_depth_limit_is_refused() {
        // `last` merges the last mapping of a chain of `levels` merge keys.
        // The reader fills `last` before a mapping of the chain, so the merge
        // of `last` walks the whole chain.
        let document = |levels: usize| {
            let chain: String = (1..levels)
                .map(|level| format!("  - &m{level} {{<<: *m{}}}\n", level - 1))
                .collect();

            format!(
                "chain:\n  - &m0 {{k: 1}}\n{chain}last: {{<<: *m{}}}\n",
                levels - 1
            )
        };
        let at_limit = load(&document(128)).unwrap();
        let Value::Map(root) = at_limit.root() else {
            panic!("the root is a mapping");
        };

        assert_eq!(
            text_of(&at_limit, at_limit.get(root.get("last").unwrap())),
            "{k: 1}"
        );
        assert_eq!(load(&document(129)).unwrap_err(), Fault::Deep);
        assert_eq!(read("a: {<<: {<<: {k: 1}}}\n"), "{a: {k: 1}}");
    }

    #[test]
    fn a_quoted_and_a_block_scalar_read_as_pyyaml_reads_them() {
        assert_eq!(read("a: \"x\\ty\\x41\\u00e9\"\n"), "{a: \"x\\tyA\u{e9}\"}");
        assert_eq!(read("a: 'it''s'\n"), "{a: \"it's\"}");
        assert_eq!(read("a: |\n  one\n  two\n"), "{a: \"one\\ntwo\\n\"}");
        assert_eq!(read("a: >-\n  one\n  two\n"), "{a: \"one two\"}");
        assert_eq!(read("a: one\n  two\n"), "{a: \"one two\"}");
    }

    #[test]
    fn a_text_past_the_depth_limit_is_refused() {
        let at_limit = format!(
            "a: {}{}\n",
            "[".repeat(DEPTH_MAX - 1),
            "]".repeat(DEPTH_MAX - 1)
        );
        let past_limit = format!("a: {}{}\n", "[".repeat(DEPTH_MAX), "]".repeat(DEPTH_MAX));

        assert!(load(&at_limit).is_ok());
        assert_eq!(load(&past_limit).unwrap_err(), Fault::Deep);
    }

    #[test]
    fn a_lone_surrogate_escape_makes_a_lossy_text() {
        let tree = load("a: \"\\ud800\"\n").unwrap();
        let Value::Map(map) = tree.root() else {
            panic!("the root is a mapping");
        };
        let Value::Text(text) = tree.get(map.get("a").unwrap()) else {
            panic!("the value is a text");
        };

        assert!(text.is_lossy());
        assert_eq!(text.as_str(), "\u{fffd}");
        assert_eq!(read("a: \"\\UFFFFFFFF\"\n"), "line 1");
    }

    #[test]
    fn base64_is_judged_as_python_judges_it() {
        let taken = [
            "",
            "aGVsbG8=",
            "aGVs bG8=\n",
            "QQ==",
            "QQ==@",
            "@@@",
            "QUJD",
            "QQ===",
            "QQ=\n=",
            "QUI=",
            "QQ==QQ==",
            "====",
            "QUJD=",
        ];
        for text in taken {
            assert!(base64_decodes(text), "{text:?}");
        }

        for text in [
            "aGVsbG8",
            "Q",
            "Q=",
            "QQ",
            "QQ=",
            "QQ=Q",
            "QQ==junk",
            "QUJDQ",
            "caf\u{e9}",
        ] {
            assert!(!base64_decodes(text), "{text:?}");
        }
    }
}
