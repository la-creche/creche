//! Text to tokens: a port of the scanner of PyYAML 6.0.3 (`yaml/scanner.py`
//! and `yaml/reader.py`).
//!
//! Each function has the name of the Python function that it ports, and does
//! the same steps in the same order. The scanner gives a token only when the
//! parser asks for one, as in Python. That order decides which error the
//! reader reports first.

use std::collections::{BTreeMap, VecDeque};

use super::LoadError;
use super::mark::{Mark, ends_line, marked, unacceptable};
use super::text::repr_char;

/// A simple key ends on its line, and after this count of characters at most.
const SIMPLE_KEY_SPAN: usize = 1024;

/// The count of hexadecimal digits after each escape letter.
const ESCAPE_CODES: [(char, usize); 3] = [('x', 2), ('u', 4), ('U', 8)];

/// The first and the last surrogate code point. A Python `str` can hold one
/// alone. A Rust `String` cannot.
const SURROGATES: std::ops::RangeInclusive<u32> = 0xd800..=0xdfff;

/// The style of a scalar, as the text wrote it.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(super) enum Style {
    Plain,
    Single,
    Double,
    Literal,
    Folded,
}

/// The value of a directive.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(super) enum Directive {
    /// `%YAML`: whether the major number is 1. The parser reads nothing else.
    Yaml { major_is_one: bool },
    /// `%TAG`: the handle and the prefix.
    Tag { handle: String, prefix: String },
    /// A directive with another name. PyYAML reads it and does nothing.
    Other,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub(super) enum TokenKind {
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
    Tag {
        handle: Option<String>,
        suffix: String,
    },
    Scalar {
        value: String,
        style: Style,
    },
}

impl TokenKind {
    /// `repr(token.id)`: the name that a parser error gives a token.
    pub(super) fn id(&self) -> &'static str {
        match self {
            Self::StreamStart => "'<stream start>'",
            Self::StreamEnd => "'<stream end>'",
            Self::Directive(_) => "'<directive>'",
            Self::DocumentStart => "'<document start>'",
            Self::DocumentEnd => "'<document end>'",
            Self::BlockSequenceStart => "'<block sequence start>'",
            Self::BlockMappingStart => "'<block mapping start>'",
            Self::BlockEnd => "'<block end>'",
            Self::FlowSequenceStart => "'['",
            Self::FlowMappingStart => "'{'",
            Self::FlowSequenceEnd => "']'",
            Self::FlowMappingEnd => "'}'",
            Self::Key => "'?'",
            Self::Value => "':'",
            Self::BlockEntry => "'-'",
            Self::FlowEntry => "','",
            Self::Alias(_) => "'<alias>'",
            Self::Anchor(_) => "'<anchor>'",
            Self::Tag { .. } => "'<tag>'",
            Self::Scalar { .. } => "'<scalar>'",
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub(super) struct Token {
    pub(super) kind: TokenKind,
    pub(super) start: Mark,
    pub(super) end: Mark,
}

#[derive(Debug, Clone, Copy)]
struct SimpleKey {
    token_number: usize,
    required: bool,
    index: usize,
    line: usize,
    column: usize,
    mark: Mark,
}

/// Which of two tokens with a name the scanner reads.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Named {
    Alias,
    Anchor,
}

/// Which kind of quoted scalar the scanner reads.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Quote {
    Single,
    Double,
}

/// How a block scalar ends: `-` strips each final line break, `+` keeps each
/// one, and no indicator keeps one.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Chomping {
    Clip,
    Strip,
    Keep,
}

fn is_break(c: char) -> bool {
    matches!(c, '\r' | '\n' | '\u{85}' | '\u{2028}' | '\u{2029}')
}

/// A space, a tab, a line break or the end of the text.
fn ends_word(c: char) -> bool {
    matches!(c, ' ' | '\t') || ends_line(c)
}

/// A space, a line break or the end of the text. A tab is not in this set.
fn ends_name(c: char) -> bool {
    c == ' ' || ends_line(c)
}

fn is_name_char(c: char) -> bool {
    c.is_ascii_alphanumeric() || matches!(c, '-' | '_')
}

/// Whether a YAML text can hold `c` (`Reader.NON_PRINTABLE`).
fn acceptable(c: char) -> bool {
    matches!(
        c,
        '\t' | '\n' | '\r' | ' '..='~' | '\u{85}' | '\u{a0}'..='\u{d7ff}' | '\u{e000}'..='\u{fffd}'
            | '\u{1_0000}'..='\u{10_ffff}'
    )
}

fn as_signed(count: usize) -> i64 {
    i64::try_from(count).unwrap_or(i64::MAX)
}

#[derive(Debug)]
pub(super) struct Scanner {
    /// The text, one item for each character, with one `\0` at the end.
    buffer: Vec<char>,
    pointer: usize,
    line: usize,
    column: usize,
    done: bool,
    flow_level: usize,
    tokens: VecDeque<Token>,
    tokens_taken: usize,
    indent: i64,
    indents: Vec<i64>,
    allow_simple_key: bool,
    possible_simple_keys: BTreeMap<usize, SimpleKey>,
    /// The first escape for a lone surrogate that the text holds.
    lone_surrogate: Option<u32>,
}

impl Scanner {
    pub(super) fn new(text: &str) -> Result<Self, LoadError> {
        let mut buffer: Vec<char> = text.chars().collect();
        if let Some(position) = buffer.iter().position(|c| !acceptable(*c)) {
            let character = buffer.get(position).copied().unwrap_or('\0');

            return Err(unacceptable(character, position));
        }

        buffer.push('\0');
        let mut scanner = Self {
            buffer,
            pointer: 0,
            line: 0,
            column: 0,
            done: false,
            flow_level: 0,
            tokens: VecDeque::new(),
            tokens_taken: 0,
            indent: -1,
            indents: Vec::new(),
            allow_simple_key: true,
            possible_simple_keys: BTreeMap::new(),
            lone_surrogate: None,
        };
        let mark = scanner.get_mark();
        scanner.push(TokenKind::StreamStart, mark, mark);

        Ok(scanner)
    }

    /// The text, for the snippet of an error.
    pub(super) fn buffer(&self) -> &[char] {
        &self.buffer
    }

    /// The code of the first escape for a lone surrogate, when the text read
    /// so far holds one.
    pub(super) fn lone_surrogate(&self) -> Option<u32> {
        self.lone_surrogate
    }

    // --- the reader ---

    fn peek(&self, ahead: usize) -> char {
        self.buffer
            .get(self.pointer + ahead)
            .copied()
            .unwrap_or('\0')
    }

    fn prefix(&self, length: usize) -> String {
        self.buffer.iter().skip(self.pointer).take(length).collect()
    }

    fn forward(&mut self, length: usize) {
        for _ in 0..length {
            let Some(c) = self.buffer.get(self.pointer).copied() else {
                return;
            };

            self.pointer += 1;
            if matches!(c, '\n' | '\u{85}' | '\u{2028}' | '\u{2029}')
                || (c == '\r' && self.peek(0) != '\n')
            {
                self.line += 1;
                self.column = 0;
            } else if c != '\u{feff}' {
                self.column += 1;
            }
        }
    }

    fn get_mark(&self) -> Mark {
        Mark {
            index: self.pointer,
            line: self.line,
            column: self.column,
        }
    }

    fn error(&self, context: Option<(&str, Mark)>, problem: &str) -> LoadError {
        marked(&self.buffer, context, problem, self.get_mark())
    }

    fn push(&mut self, kind: TokenKind, start: Mark, end: Mark) {
        self.tokens.push_back(Token { kind, start, end });
    }

    // --- what the parser calls ---

    /// The next token, which stays in the queue. `None` after the last one.
    pub(super) fn peek_token(&mut self) -> Result<Option<&Token>, LoadError> {
        while self.need_more_tokens()? {
            self.fetch_more_tokens()?;
        }

        Ok(self.tokens.front())
    }

    /// The next token, which leaves the queue.
    pub(super) fn get_token(&mut self) -> Result<Option<Token>, LoadError> {
        while self.need_more_tokens()? {
            self.fetch_more_tokens()?;
        }

        let token = self.tokens.pop_front();
        if token.is_some() {
            self.tokens_taken += 1;
        }

        Ok(token)
    }

    fn need_more_tokens(&mut self) -> Result<bool, LoadError> {
        if self.done {
            return Ok(false);
        }

        if self.tokens.is_empty() {
            return Ok(true);
        }

        self.stale_possible_simple_keys()?;

        Ok(self.next_possible_simple_key() == Some(self.tokens_taken))
    }

    fn fetch_more_tokens(&mut self) -> Result<(), LoadError> {
        self.scan_to_next_token();
        self.stale_possible_simple_keys()?;
        self.unwind_indent(as_signed(self.column));
        let c = self.peek(0);
        let in_flow = self.flow_level > 0;
        match c {
            '\0' => self.fetch_stream_end(),
            '%' if self.column == 0 => self.fetch_directive(),
            '-' if self.at_document_mark("---") => {
                self.fetch_document_mark(TokenKind::DocumentStart)
            }
            '.' if self.at_document_mark("...") => self.fetch_document_mark(TokenKind::DocumentEnd),
            '[' => self.fetch_flow_start(TokenKind::FlowSequenceStart),
            '{' => self.fetch_flow_start(TokenKind::FlowMappingStart),
            ']' => self.fetch_flow_end(TokenKind::FlowSequenceEnd),
            '}' => self.fetch_flow_end(TokenKind::FlowMappingEnd),
            ',' => self.fetch_flow_entry(),
            '-' if ends_word(self.peek(1)) => self.fetch_block_entry(),
            '?' if in_flow || ends_word(self.peek(1)) => self.fetch_key(),
            ':' if in_flow || ends_word(self.peek(1)) => self.fetch_value(),
            '*' => self.fetch_named(Named::Alias),
            '&' => self.fetch_named(Named::Anchor),
            '!' => self.fetch_tag(),
            '|' if !in_flow => self.fetch_block_scalar(Style::Literal),
            '>' if !in_flow => self.fetch_block_scalar(Style::Folded),
            '\'' => self.fetch_flow_scalar(Quote::Single),
            '"' => self.fetch_flow_scalar(Quote::Double),
            _ if self.check_plain() => self.fetch_plain(),
            _ => Err(self.error(
                Some(("while scanning for the next token", self.get_mark())),
                &format!(
                    "found character {} that cannot start any token",
                    repr_char(c)
                ),
            )),
        }
    }

    // --- simple keys ---

    fn next_possible_simple_key(&self) -> Option<usize> {
        self.possible_simple_keys
            .values()
            .map(|key| key.token_number)
            .min()
    }

    fn missing_colon(&self, key: SimpleKey) -> LoadError {
        self.error(
            Some(("while scanning a simple key", key.mark)),
            "could not find expected ':'",
        )
    }

    fn stale_possible_simple_keys(&mut self) -> Result<(), LoadError> {
        let stale: Vec<(usize, SimpleKey)> = self
            .possible_simple_keys
            .iter()
            .filter(|(_, key)| key.line != self.line || self.pointer - key.index > SIMPLE_KEY_SPAN)
            .map(|(level, key)| (*level, *key))
            .collect();
        for (level, key) in stale {
            if key.required {
                return Err(self.missing_colon(key));
            }

            self.possible_simple_keys.remove(&level);
        }

        Ok(())
    }

    fn save_possible_simple_key(&mut self) -> Result<(), LoadError> {
        let required = self.flow_level == 0 && self.indent == as_signed(self.column);
        if !self.allow_simple_key {
            return Ok(());
        }

        self.remove_possible_simple_key()?;
        let mark = self.get_mark();
        let key = SimpleKey {
            token_number: self.tokens_taken + self.tokens.len(),
            required,
            index: self.pointer,
            line: self.line,
            column: self.column,
            mark,
        };
        self.possible_simple_keys.insert(self.flow_level, key);

        Ok(())
    }

    fn remove_possible_simple_key(&mut self) -> Result<(), LoadError> {
        let Some(key) = self.possible_simple_keys.remove(&self.flow_level) else {
            return Ok(());
        };

        if key.required {
            return Err(self.missing_colon(key));
        }

        Ok(())
    }

    // --- indentation ---

    fn unwind_indent(&mut self, column: i64) {
        if self.flow_level > 0 {
            return;
        }

        while self.indent > column {
            let mark = self.get_mark();
            self.indent = self.indents.pop().unwrap_or(-1);
            self.push(TokenKind::BlockEnd, mark, mark);
        }
    }

    fn add_indent(&mut self, column: usize) -> bool {
        let column = as_signed(column);
        if self.indent >= column {
            return false;
        }

        self.indents.push(self.indent);
        self.indent = column;

        true
    }

    // --- fetchers ---

    fn fetch_stream_end(&mut self) -> Result<(), LoadError> {
        self.unwind_indent(-1);
        self.remove_possible_simple_key()?;
        self.allow_simple_key = false;
        self.possible_simple_keys.clear();
        let mark = self.get_mark();
        self.push(TokenKind::StreamEnd, mark, mark);
        self.done = true;

        Ok(())
    }

    fn fetch_directive(&mut self) -> Result<(), LoadError> {
        self.unwind_indent(-1);
        self.remove_possible_simple_key()?;
        self.allow_simple_key = false;
        let token = self.scan_directive()?;
        self.tokens.push_back(token);

        Ok(())
    }

    fn at_document_mark(&self, mark: &str) -> bool {
        self.column == 0 && self.prefix(3) == mark && ends_word(self.peek(3))
    }

    fn fetch_document_mark(&mut self, kind: TokenKind) -> Result<(), LoadError> {
        self.unwind_indent(-1);
        self.remove_possible_simple_key()?;
        self.allow_simple_key = false;
        let start = self.get_mark();
        self.forward(3);
        let end = self.get_mark();
        self.push(kind, start, end);

        Ok(())
    }

    fn fetch_flow_start(&mut self, kind: TokenKind) -> Result<(), LoadError> {
        self.save_possible_simple_key()?;
        self.flow_level += 1;
        self.allow_simple_key = true;
        self.push_one_char(kind);

        Ok(())
    }

    fn fetch_flow_end(&mut self, kind: TokenKind) -> Result<(), LoadError> {
        self.remove_possible_simple_key()?;
        // PyYAML counts below 0 on a `]` with no `[`. A count below 0 is true
        // in Python, so the scanner then stays in a flow context to the end.
        // The parser refuses that `]` before the difference shows.
        self.flow_level = self.flow_level.saturating_sub(1);
        self.allow_simple_key = false;
        self.push_one_char(kind);

        Ok(())
    }

    fn push_one_char(&mut self, kind: TokenKind) {
        let start = self.get_mark();
        self.forward(1);
        let end = self.get_mark();
        self.push(kind, start, end);
    }

    fn fetch_flow_entry(&mut self) -> Result<(), LoadError> {
        self.allow_simple_key = true;
        self.remove_possible_simple_key()?;
        self.push_one_char(TokenKind::FlowEntry);

        Ok(())
    }

    fn fetch_block_entry(&mut self) -> Result<(), LoadError> {
        if self.flow_level == 0 {
            if !self.allow_simple_key {
                return Err(self.error(None, "sequence entries are not allowed here"));
            }

            if self.add_indent(self.column) {
                let mark = self.get_mark();
                self.push(TokenKind::BlockSequenceStart, mark, mark);
            }
        }

        self.allow_simple_key = true;
        self.remove_possible_simple_key()?;
        self.push_one_char(TokenKind::BlockEntry);

        Ok(())
    }

    fn fetch_key(&mut self) -> Result<(), LoadError> {
        if self.flow_level == 0 {
            if !self.allow_simple_key {
                return Err(self.error(None, "mapping keys are not allowed here"));
            }

            if self.add_indent(self.column) {
                let mark = self.get_mark();
                self.push(TokenKind::BlockMappingStart, mark, mark);
            }
        }

        self.allow_simple_key = self.flow_level == 0;
        self.remove_possible_simple_key()?;
        self.push_one_char(TokenKind::Key);

        Ok(())
    }

    fn fetch_value(&mut self) -> Result<(), LoadError> {
        if let Some(key) = self.possible_simple_keys.remove(&self.flow_level) {
            let at = key
                .token_number
                .saturating_sub(self.tokens_taken)
                .min(self.tokens.len());
            let key_token = Token {
                kind: TokenKind::Key,
                start: key.mark,
                end: key.mark,
            };
            self.tokens.insert(at, key_token);
            if self.flow_level == 0 && self.add_indent(key.column) {
                let start_token = Token {
                    kind: TokenKind::BlockMappingStart,
                    start: key.mark,
                    end: key.mark,
                };
                self.tokens.insert(at, start_token);
            }

            self.allow_simple_key = false;
        } else {
            if self.flow_level == 0 {
                if !self.allow_simple_key {
                    return Err(self.error(None, "mapping values are not allowed here"));
                }

                if self.add_indent(self.column) {
                    let mark = self.get_mark();
                    self.push(TokenKind::BlockMappingStart, mark, mark);
                }
            }

            self.allow_simple_key = self.flow_level == 0;
            self.remove_possible_simple_key()?;
        }

        self.push_one_char(TokenKind::Value);

        Ok(())
    }

    fn fetch_named(&mut self, named: Named) -> Result<(), LoadError> {
        self.save_possible_simple_key()?;
        self.allow_simple_key = false;
        let token = self.scan_anchor(named)?;
        self.tokens.push_back(token);

        Ok(())
    }

    fn fetch_tag(&mut self) -> Result<(), LoadError> {
        self.save_possible_simple_key()?;
        self.allow_simple_key = false;
        let token = self.scan_tag()?;
        self.tokens.push_back(token);

        Ok(())
    }

    fn fetch_block_scalar(&mut self, style: Style) -> Result<(), LoadError> {
        self.allow_simple_key = true;
        self.remove_possible_simple_key()?;
        let token = self.scan_block_scalar(style)?;
        self.tokens.push_back(token);

        Ok(())
    }

    fn fetch_flow_scalar(&mut self, quote: Quote) -> Result<(), LoadError> {
        self.save_possible_simple_key()?;
        self.allow_simple_key = false;
        let token = self.scan_flow_scalar(quote)?;
        self.tokens.push_back(token);

        Ok(())
    }

    fn fetch_plain(&mut self) -> Result<(), LoadError> {
        self.save_possible_simple_key()?;
        self.allow_simple_key = false;
        let token = self.scan_plain();
        self.tokens.push_back(token);

        Ok(())
    }

    fn check_plain(&self) -> bool {
        let c = self.peek(0);
        let indicator = ends_word(c)
            || matches!(
                c,
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
        if !indicator {
            return true;
        }

        !ends_word(self.peek(1)) && (c == '-' || (self.flow_level == 0 && matches!(c, '?' | ':')))
    }

    // --- scanners ---

    fn scan_to_next_token(&mut self) {
        if self.pointer == 0 && self.peek(0) == '\u{feff}' {
            self.forward(1);
        }

        loop {
            while self.peek(0) == ' ' {
                self.forward(1);
            }

            if self.peek(0) == '#' {
                self.skip_line();
            }

            if self.scan_line_break().is_empty() {
                return;
            }

            if self.flow_level == 0 {
                self.allow_simple_key = true;
            }
        }
    }

    /// Moves to the end of the line.
    fn skip_line(&mut self) {
        while !ends_line(self.peek(0)) {
            self.forward(1);
        }
    }

    fn scan_directive(&mut self) -> Result<Token, LoadError> {
        let start = self.get_mark();
        self.forward(1);
        let name = self.scan_directive_name(start)?;
        let (value, end) = match name.as_str() {
            "YAML" => {
                let value = self.scan_yaml_directive(start)?;
                (value, self.get_mark())
            }
            "TAG" => {
                let value = self.scan_tag_directive(start)?;
                (value, self.get_mark())
            }
            _ => {
                let end = self.get_mark();
                self.skip_line();
                (Directive::Other, end)
            }
        };
        self.scan_ignored_line("while scanning a directive", start)?;

        Ok(Token {
            kind: TokenKind::Directive(value),
            start,
            end,
        })
    }

    fn directive_error(&self, start: Mark, expected: &str, found: char) -> LoadError {
        self.error(
            Some(("while scanning a directive", start)),
            &format!("expected {expected}, but found {}", repr_char(found)),
        )
    }

    fn scan_directive_name(&mut self, start: Mark) -> Result<String, LoadError> {
        let length = self.name_length();
        if length == 0 {
            let found = self.peek(0);

            return Err(self.directive_error(start, "alphabetic or numeric character", found));
        }

        let value = self.prefix(length);
        self.forward(length);
        let c = self.peek(0);
        if !ends_name(c) {
            return Err(self.directive_error(start, "alphabetic or numeric character", c));
        }

        Ok(value)
    }

    /// The count of characters of a name from the position on.
    fn name_length(&self) -> usize {
        let mut length = 0;
        while is_name_char(self.peek(length)) {
            length += 1;
        }

        length
    }

    fn scan_yaml_directive(&mut self, start: Mark) -> Result<Directive, LoadError> {
        while self.peek(0) == ' ' {
            self.forward(1);
        }

        let major = self.scan_directive_number(start)?;
        if self.peek(0) != '.' {
            let found = self.peek(0);

            return Err(self.directive_error(start, "a digit or '.'", found));
        }

        self.forward(1);
        self.scan_directive_number(start)?;
        if !ends_name(self.peek(0)) {
            let found = self.peek(0);

            return Err(self.directive_error(start, "a digit or ' '", found));
        }

        Ok(Directive::Yaml {
            major_is_one: major.trim_start_matches('0') == "1",
        })
    }

    fn scan_directive_number(&mut self, start: Mark) -> Result<String, LoadError> {
        let c = self.peek(0);
        if !c.is_ascii_digit() {
            return Err(self.directive_error(start, "a digit", c));
        }

        let mut length = 0;
        while self.peek(length).is_ascii_digit() {
            length += 1;
        }

        if length > super::PYTHON_INT_DIGITS {
            return Err(LoadError::Unreadable(
                "a number of a %YAML directive has more than 4300 digits".to_owned(),
            ));
        }

        let value = self.prefix(length);
        self.forward(length);

        Ok(value)
    }

    fn scan_tag_directive(&mut self, start: Mark) -> Result<Directive, LoadError> {
        while self.peek(0) == ' ' {
            self.forward(1);
        }

        let handle = self.scan_tag_handle("directive", start)?;
        let c = self.peek(0);
        if c != ' ' {
            return Err(self.directive_error(start, "' '", c));
        }

        while self.peek(0) == ' ' {
            self.forward(1);
        }

        let prefix = self.scan_tag_uri("directive", start)?;
        let c = self.peek(0);
        if !ends_name(c) {
            return Err(self.directive_error(start, "' '", c));
        }

        Ok(Directive::Tag { handle, prefix })
    }

    /// The rest of a line that holds nothing but an optional comment.
    fn scan_ignored_line(&mut self, context: &str, start: Mark) -> Result<(), LoadError> {
        while self.peek(0) == ' ' {
            self.forward(1);
        }

        if self.peek(0) == '#' {
            self.skip_line();
        }

        let c = self.peek(0);
        if !ends_line(c) {
            return Err(self.error(
                Some((context, start)),
                &format!(
                    "expected a comment or a line break, but found {}",
                    repr_char(c)
                ),
            ));
        }

        self.scan_line_break();

        Ok(())
    }

    fn scan_anchor(&mut self, named: Named) -> Result<Token, LoadError> {
        let start = self.get_mark();
        let context = match named {
            Named::Alias => "while scanning an alias",
            Named::Anchor => "while scanning an anchor",
        };
        self.forward(1);
        let length = self.name_length();
        let after = self.peek(length);
        let complete =
            ends_word(after) || matches!(after, '?' | ':' | ',' | ']' | '}' | '%' | '@' | '`');
        if length == 0 || !complete {
            self.forward(length);

            return Err(self.error(
                Some((context, start)),
                &format!(
                    "expected alphabetic or numeric character, but found {}",
                    repr_char(after)
                ),
            ));
        }

        let value = self.prefix(length);
        self.forward(length);
        let end = self.get_mark();
        let kind = match named {
            Named::Alias => TokenKind::Alias(value),
            Named::Anchor => TokenKind::Anchor(value),
        };

        Ok(Token { kind, start, end })
    }

    fn scan_tag(&mut self) -> Result<Token, LoadError> {
        let start = self.get_mark();
        let mut c = self.peek(1);
        let (handle, suffix) = if c == '<' {
            self.forward(2);
            let suffix = self.scan_tag_uri("tag", start)?;
            if self.peek(0) != '>' {
                let found = self.peek(0);

                return Err(self.error(
                    Some(("while parsing a tag", start)),
                    &format!("expected '>', but found {}", repr_char(found)),
                ));
            }

            self.forward(1);
            (None, suffix)
        } else if ends_word(c) {
            self.forward(1);
            (None, "!".to_owned())
        } else {
            let mut length = 1;
            let mut use_handle = false;
            while !ends_name(c) {
                if c == '!' {
                    use_handle = true;
                    break;
                }

                length += 1;
                c = self.peek(length);
            }

            let handle = if use_handle {
                self.scan_tag_handle("tag", start)?
            } else {
                self.forward(1);
                "!".to_owned()
            };
            (Some(handle), self.scan_tag_uri("tag", start)?)
        };

        let c = self.peek(0);
        if !ends_name(c) {
            return Err(self.error(
                Some(("while scanning a tag", start)),
                &format!("expected ' ', but found {}", repr_char(c)),
            ));
        }

        let end = self.get_mark();

        Ok(Token {
            kind: TokenKind::Tag { handle, suffix },
            start,
            end,
        })
    }

    fn scan_block_scalar(&mut self, style: Style) -> Result<Token, LoadError> {
        let folded = style == Style::Folded;
        let mut chunks = String::new();
        let start = self.get_mark();
        self.forward(1);
        let (chomping, increment) = self.scan_block_indicators(start)?;
        self.scan_ignored_line("while scanning a block scalar", start)?;
        let min_indent = usize::try_from(self.indent + 1).unwrap_or(1).max(1);
        let (mut breaks, indent, mut end) = match increment {
            None => {
                let (breaks, max_indent, end) = self.scan_block_indentation();
                (breaks, min_indent.max(max_indent), end)
            }
            Some(increment) => {
                let indent = min_indent + increment - 1;
                let (breaks, end) = self.scan_block_breaks(indent);
                (breaks, indent, end)
            }
        };
        let mut line_break = String::new();
        while self.column == indent && self.peek(0) != '\0' {
            chunks.push_str(&breaks);
            let leading_non_space = !matches!(self.peek(0), ' ' | '\t');
            let mut length = 0;
            while !ends_line(self.peek(length)) {
                length += 1;
            }

            chunks.push_str(&self.prefix(length));
            self.forward(length);
            line_break = self.scan_line_break();
            (breaks, end) = self.scan_block_breaks(indent);
            if self.column != indent || self.peek(0) == '\0' {
                break;
            }

            let folds = folded
                && line_break == "\n"
                && leading_non_space
                && !matches!(self.peek(0), ' ' | '\t');
            if !folds {
                chunks.push_str(&line_break);
            } else if breaks.is_empty() {
                chunks.push(' ');
            }
        }

        if chomping != Chomping::Strip {
            chunks.push_str(&line_break);
        }

        if chomping == Chomping::Keep {
            chunks.push_str(&breaks);
        }

        Ok(Token {
            kind: TokenKind::Scalar {
                value: chunks,
                style,
            },
            start,
            end,
        })
    }

    fn scan_chomping(&mut self) -> Option<Chomping> {
        let chomping = match self.peek(0) {
            '+' => Chomping::Keep,
            '-' => Chomping::Strip,
            _ => return None,
        };
        self.forward(1);

        Some(chomping)
    }

    fn scan_increment(&mut self, start: Mark) -> Result<Option<usize>, LoadError> {
        let Some(digit) = self.peek(0).to_digit(10) else {
            return Ok(None);
        };

        if digit == 0 {
            return Err(self.error(
                Some(("while scanning a block scalar", start)),
                "expected indentation indicator in the range 1-9, but found 0",
            ));
        }

        self.forward(1);

        Ok(usize::try_from(digit).ok())
    }

    fn scan_block_indicators(
        &mut self,
        start: Mark,
    ) -> Result<(Chomping, Option<usize>), LoadError> {
        let (chomping, increment) = match self.scan_chomping() {
            Some(chomping) => (Some(chomping), self.scan_increment(start)?),
            None => {
                let increment = self.scan_increment(start)?;
                let chomping = if increment.is_some() {
                    self.scan_chomping()
                } else {
                    None
                };
                (chomping, increment)
            }
        };
        let c = self.peek(0);
        if !ends_name(c) {
            return Err(self.error(
                Some(("while scanning a block scalar", start)),
                &format!(
                    "expected chomping or indentation indicators, but found {}",
                    repr_char(c)
                ),
            ));
        }

        Ok((chomping.unwrap_or(Chomping::Clip), increment))
    }

    fn scan_block_indentation(&mut self) -> (String, usize, Mark) {
        let mut chunks = String::new();
        let mut max_indent = 0;
        let mut end = self.get_mark();
        while self.peek(0) == ' ' || is_break(self.peek(0)) {
            if self.peek(0) == ' ' {
                self.forward(1);
                max_indent = max_indent.max(self.column);
            } else {
                chunks.push_str(&self.scan_line_break());
                end = self.get_mark();
            }
        }

        (chunks, max_indent, end)
    }

    fn scan_block_breaks(&mut self, indent: usize) -> (String, Mark) {
        let mut chunks = String::new();
        let mut end = self.get_mark();
        while self.column < indent && self.peek(0) == ' ' {
            self.forward(1);
        }

        while is_break(self.peek(0)) {
            chunks.push_str(&self.scan_line_break());
            end = self.get_mark();
            while self.column < indent && self.peek(0) == ' ' {
                self.forward(1);
            }
        }

        (chunks, end)
    }

    fn scan_flow_scalar(&mut self, quote: Quote) -> Result<Token, LoadError> {
        let mut chunks = String::new();
        let start = self.get_mark();
        let closing = self.peek(0);
        self.forward(1);
        self.scan_flow_non_spaces(quote, start, &mut chunks)?;
        while self.peek(0) != closing {
            self.scan_flow_spaces(start, &mut chunks)?;
            self.scan_flow_non_spaces(quote, start, &mut chunks)?;
        }

        self.forward(1);
        let end = self.get_mark();
        let style = match quote {
            Quote::Single => Style::Single,
            Quote::Double => Style::Double,
        };

        Ok(Token {
            kind: TokenKind::Scalar {
                value: chunks,
                style,
            },
            start,
            end,
        })
    }

    fn scan_flow_non_spaces(
        &mut self,
        quote: Quote,
        start: Mark,
        chunks: &mut String,
    ) -> Result<(), LoadError> {
        let double = quote == Quote::Double;
        loop {
            let mut length = 0;
            while !matches!(self.peek(length), '\'' | '"' | '\\') && !ends_word(self.peek(length)) {
                length += 1;
            }

            if length > 0 {
                chunks.push_str(&self.prefix(length));
                self.forward(length);
            }

            let c = self.peek(0);
            if !double && c == '\'' && self.peek(1) == '\'' {
                chunks.push('\'');
                self.forward(2);
            } else if (double && c == '\'') || (!double && matches!(c, '"' | '\\')) {
                chunks.push(c);
                self.forward(1);
            } else if double && c == '\\' {
                self.forward(1);
                self.scan_escape(start, chunks)?;
            } else {
                return Ok(());
            }
        }
    }

    /// One escape of a double-quoted scalar, after its `\`.
    fn scan_escape(&mut self, start: Mark, chunks: &mut String) -> Result<(), LoadError> {
        let c = self.peek(0);
        let replacement = match c {
            '0' => Some('\0'),
            'a' => Some('\u{7}'),
            'b' => Some('\u{8}'),
            't' | '\t' => Some('\t'),
            'n' => Some('\n'),
            'v' => Some('\u{b}'),
            'f' => Some('\u{c}'),
            'r' => Some('\r'),
            'e' => Some('\u{1b}'),
            ' ' => Some(' '),
            '"' => Some('"'),
            '\\' => Some('\\'),
            '/' => Some('/'),
            'N' => Some('\u{85}'),
            '_' => Some('\u{a0}'),
            'L' => Some('\u{2028}'),
            'P' => Some('\u{2029}'),
            _ => None,
        };
        if let Some(replacement) = replacement {
            chunks.push(replacement);
            self.forward(1);

            return Ok(());
        }

        if let Some((_, length)) = ESCAPE_CODES.iter().find(|(letter, _)| *letter == c) {
            self.forward(1);

            return self.scan_escape_code(*length, start, chunks);
        }

        if is_break(c) {
            self.scan_line_break();

            return self.scan_flow_breaks(start, chunks);
        }

        Err(self.error(
            Some(("while scanning a double-quoted scalar", start)),
            &format!("found unknown escape character {}", repr_char(c)),
        ))
    }

    fn scan_escape_code(
        &mut self,
        length: usize,
        start: Mark,
        chunks: &mut String,
    ) -> Result<(), LoadError> {
        let mut code: u32 = 0;
        for ahead in 0..length {
            let c = self.peek(ahead);
            let Some(digit) = c.to_digit(16) else {
                return Err(self.error(
                    Some(("while scanning a double-quoted scalar", start)),
                    &format!(
                        "expected escape sequence of {length} hexadecimal numbers, but found {}",
                        repr_char(c)
                    ),
                ));
            };

            code = code.wrapping_mul(16).wrapping_add(digit);
        }

        if SURROGATES.contains(&code) {
            // A Rust `String` cannot hold a lone surrogate. The text reads on
            // with a stand-in, so an error that Python reports first is
            // still the error here. `lone_surrogate` refuses the text at the
            // end.
            self.lone_surrogate.get_or_insert(code);
            chunks.push(char::REPLACEMENT_CHARACTER);
            self.forward(length);

            return Ok(());
        }

        let Some(character) = char::from_u32(code) else {
            return Err(LoadError::Unreadable(format!(
                "the escape for {code:#x} is past the last character U+10FFFF"
            )));
        };

        chunks.push(character);
        self.forward(length);

        Ok(())
    }

    fn scan_flow_spaces(&mut self, start: Mark, chunks: &mut String) -> Result<(), LoadError> {
        let mut length = 0;
        while matches!(self.peek(length), ' ' | '\t') {
            length += 1;
        }

        let whitespaces = self.prefix(length);
        self.forward(length);
        let c = self.peek(0);
        if c == '\0' {
            return Err(self.error(
                Some(("while scanning a quoted scalar", start)),
                "found unexpected end of stream",
            ));
        }

        if !is_break(c) {
            chunks.push_str(&whitespaces);

            return Ok(());
        }

        let line_break = self.scan_line_break();
        let mut breaks = String::new();
        self.scan_flow_breaks(start, &mut breaks)?;
        if line_break != "\n" {
            chunks.push_str(&line_break);
        } else if breaks.is_empty() {
            chunks.push(' ');
        }

        chunks.push_str(&breaks);

        Ok(())
    }

    fn at_any_document_mark(&self) -> bool {
        let prefix = self.prefix(3);

        (prefix == "---" || prefix == "...") && ends_word(self.peek(3))
    }

    fn scan_flow_breaks(&mut self, start: Mark, chunks: &mut String) -> Result<(), LoadError> {
        loop {
            if self.at_any_document_mark() {
                return Err(self.error(
                    Some(("while scanning a quoted scalar", start)),
                    "found unexpected document separator",
                ));
            }

            while matches!(self.peek(0), ' ' | '\t') {
                self.forward(1);
            }

            if !is_break(self.peek(0)) {
                return Ok(());
            }

            chunks.push_str(&self.scan_line_break());
        }
    }

    fn scan_plain(&mut self) -> Token {
        let mut chunks = String::new();
        let start = self.get_mark();
        let mut end = start;
        let indent = self.indent + 1;
        let in_flow = self.flow_level > 0;
        let mut spaces = String::new();
        loop {
            if self.peek(0) == '#' {
                break;
            }

            let mut length = 0;
            loop {
                let c = self.peek(length);
                let after = self.peek(length + 1);
                let colon_ends =
                    ends_word(after) || (in_flow && matches!(after, ',' | '[' | ']' | '{' | '}'));
                if ends_word(c)
                    || (c == ':' && colon_ends)
                    || (in_flow && matches!(c, ',' | '?' | '[' | ']' | '{' | '}'))
                {
                    break;
                }

                length += 1;
            }

            if length == 0 {
                break;
            }

            self.allow_simple_key = false;
            chunks.push_str(&spaces);
            chunks.push_str(&self.prefix(length));
            self.forward(length);
            end = self.get_mark();
            let Some(next_spaces) = self.scan_plain_spaces() else {
                break;
            };

            if next_spaces.is_empty()
                || self.peek(0) == '#'
                || (!in_flow && as_signed(self.column) < indent)
            {
                break;
            }

            spaces = next_spaces;
        }

        Token {
            kind: TokenKind::Scalar {
                value: chunks,
                style: Style::Plain,
            },
            start,
            end,
        }
    }

    /// The spaces and the line breaks after one word of a plain scalar.
    /// `None` when a document mark follows.
    fn scan_plain_spaces(&mut self) -> Option<String> {
        let mut chunks = String::new();
        let mut length = 0;
        while self.peek(length) == ' ' {
            length += 1;
        }

        let whitespaces = self.prefix(length);
        self.forward(length);
        if !is_break(self.peek(0)) {
            return Some(whitespaces);
        }

        let line_break = self.scan_line_break();
        self.allow_simple_key = true;
        if self.at_any_document_mark() {
            return None;
        }

        let mut breaks = String::new();
        while self.peek(0) == ' ' || is_break(self.peek(0)) {
            if self.peek(0) == ' ' {
                self.forward(1);
                continue;
            }

            breaks.push_str(&self.scan_line_break());
            if self.at_any_document_mark() {
                return None;
            }
        }

        if line_break != "\n" {
            chunks.push_str(&line_break);
        } else if breaks.is_empty() {
            chunks.push(' ');
        }

        chunks.push_str(&breaks);

        Some(chunks)
    }

    fn scan_tag_handle(&mut self, name: &str, start: Mark) -> Result<String, LoadError> {
        let context = format!("while scanning a {name}");
        let c = self.peek(0);
        if c != '!' {
            return Err(self.error(
                Some((&context, start)),
                &format!("expected '!', but found {}", repr_char(c)),
            ));
        }

        let mut length = 1;
        let mut c = self.peek(length);
        if c != ' ' {
            while is_name_char(c) {
                length += 1;
                c = self.peek(length);
            }

            if c != '!' {
                self.forward(length);

                return Err(self.error(
                    Some((&context, start)),
                    &format!("expected '!', but found {}", repr_char(c)),
                ));
            }

            length += 1;
        }

        let value = self.prefix(length);
        self.forward(length);

        Ok(value)
    }

    fn scan_tag_uri(&mut self, name: &str, start: Mark) -> Result<String, LoadError> {
        let mut chunks = String::new();
        let mut length = 0;
        let mut c = self.peek(length);
        while c.is_ascii_alphanumeric() || "-;/?:@&=+$,_.!~*'()[]%".contains(c) {
            if c == '%' {
                chunks.push_str(&self.prefix(length));
                self.forward(length);
                length = 0;
                chunks.push_str(&self.scan_uri_escapes(name, start)?);
            } else {
                length += 1;
            }

            c = self.peek(length);
        }

        if length > 0 {
            chunks.push_str(&self.prefix(length));
            self.forward(length);
        }

        if chunks.is_empty() {
            return Err(self.error(
                Some((&format!("while parsing a {name}"), start)),
                &format!("expected URI, but found {}", repr_char(c)),
            ));
        }

        Ok(chunks)
    }

    fn scan_uri_escapes(&mut self, name: &str, start: Mark) -> Result<String, LoadError> {
        let mut codes: Vec<u8> = Vec::new();
        while self.peek(0) == '%' {
            self.forward(1);
            let mut code: u8 = 0;
            for ahead in 0..2 {
                let c = self.peek(ahead);
                let digit = c.to_digit(16).and_then(|digit| u8::try_from(digit).ok());
                let Some(digit) = digit else {
                    return Err(self.error(
                        Some((&format!("while scanning a {name}"), start)),
                        &format!(
                            "expected URI escape sequence of 2 hexadecimal numbers, but found {}",
                            repr_char(c)
                        ),
                    ));
                };

                code = code.wrapping_mul(16).wrapping_add(digit);
            }

            codes.push(code);
            self.forward(2);
        }

        // PyYAML refuses these bytes with the text of a Python decode error.
        // That text names the byte and its offset. It is not written here.
        String::from_utf8(codes).map_err(|_| {
            LoadError::Unreadable("the escapes of a tag are not UTF-8 text".to_owned())
        })
    }

    fn scan_line_break(&mut self) -> String {
        match self.peek(0) {
            '\r' | '\n' | '\u{85}' => {
                if self.peek(0) == '\r' && self.peek(1) == '\n' {
                    self.forward(2);
                } else {
                    self.forward(1);
                }

                "\n".to_owned()
            }
            c @ ('\u{2028}' | '\u{2029}') => {
                self.forward(1);

                c.to_string()
            }
            _ => String::new(),
        }
    }
}
