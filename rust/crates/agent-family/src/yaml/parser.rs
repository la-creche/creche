//! Tokens to events: a port of the parser of PyYAML 6.0.3 (`yaml/parser.py`).
//!
//! The parser is a state machine. Each state has the name of the Python
//! function that it ports. The parser makes an event only when the composer
//! asks for one, as in Python.

use std::collections::BTreeMap;

use super::LoadError;
use super::mark::{Mark, marked};
use super::scanner::{Directive, Scanner, Style, Token, TokenKind};
use super::text::repr_str;

/// The prefix of each tag that YAML defines.
pub(super) const YAML_TAG_PREFIX: &str = "tag:yaml.org,2002:";

/// The tag `!`. It tells the resolver to read a scalar as a plain one.
const NON_SPECIFIC_TAG: &str = "!";

#[derive(Debug, Clone, PartialEq, Eq)]
pub(super) enum EventKind {
    StreamStart,
    StreamEnd,
    DocumentStart,
    DocumentEnd,
    Alias(String),
    Scalar {
        anchor: Option<String>,
        tag: Option<String>,
        /// Whether the resolver reads the value as a plain scalar.
        plain_implicit: bool,
        value: String,
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

#[derive(Debug, Clone, PartialEq, Eq)]
pub(super) struct Event {
    pub(super) kind: EventKind,
    pub(super) start: Mark,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
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

/// Which kind of node a state reads.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum NodeForm {
    Block,
    /// A block node, or a sequence at the indent of its mapping key.
    BlockOrIndentless,
    Flow,
}

/// Whether a flow collection reads its first entry.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Position {
    First,
    Later,
}

/// The anchor, the tag and the marks that come before the content of a node.
#[derive(Debug)]
struct Properties {
    anchor: Option<String>,
    tag: Option<String>,
    start: Mark,
}

#[derive(Debug)]
pub(super) struct Parser {
    scanner: Scanner,
    current: Option<Event>,
    tag_handles: BTreeMap<String, String>,
    yaml_directive_seen: bool,
    states: Vec<State>,
    marks: Vec<Mark>,
    state: Option<State>,
}

fn default_tags() -> BTreeMap<String, String> {
    BTreeMap::from([
        ("!".to_owned(), "!".to_owned()),
        ("!!".to_owned(), YAML_TAG_PREFIX.to_owned()),
    ])
}

fn empty_scalar(mark: Mark) -> Event {
    Event {
        kind: EventKind::Scalar {
            anchor: None,
            tag: None,
            plain_implicit: true,
            value: String::new(),
        },
        start: mark,
    }
}

impl Parser {
    pub(super) fn new(text: &str) -> Result<Self, LoadError> {
        Ok(Self {
            scanner: Scanner::new(text)?,
            current: None,
            tag_handles: BTreeMap::new(),
            yaml_directive_seen: false,
            states: Vec::new(),
            marks: Vec::new(),
            state: Some(State::StreamStart),
        })
    }

    /// The text, for the snippet of an error.
    pub(super) fn buffer(&self) -> &[char] {
        self.scanner.buffer()
    }

    pub(super) fn lone_surrogate(&self) -> Option<u32> {
        self.scanner.lone_surrogate()
    }

    // --- what the composer calls ---

    /// The next event, which stays in its place. `None` after the last one.
    pub(super) fn peek_event(&mut self) -> Result<Option<&Event>, LoadError> {
        if self.current.is_none()
            && let Some(state) = self.state
        {
            self.current = Some(self.run(state)?);
        }

        Ok(self.current.as_ref())
    }

    /// The next event, which the caller takes.
    pub(super) fn get_event(&mut self) -> Result<Option<Event>, LoadError> {
        self.peek_event()?;

        Ok(self.current.take())
    }

    // --- tokens ---

    fn error(&self, context: Option<(&str, Mark)>, problem: &str, at: Mark) -> LoadError {
        marked(self.scanner.buffer(), context, problem, at)
    }

    /// The kind and the marks of the next token. The scanner always ends
    /// with a stream end token, so a missing token reads as that one.
    fn peek(&mut self) -> Result<Token, LoadError> {
        let token = self.scanner.peek_token()?.cloned();

        Ok(token.unwrap_or(Token {
            kind: TokenKind::StreamEnd,
            start: Mark {
                index: 0,
                line: 0,
                column: 0,
            },
            end: Mark {
                index: 0,
                line: 0,
                column: 0,
            },
        }))
    }

    fn take(&mut self) -> Result<Token, LoadError> {
        let token = self.peek()?;
        self.scanner.get_token()?;

        Ok(token)
    }

    fn pop_state(&mut self) {
        self.state = self.states.pop();
    }

    fn last_mark(&self) -> Mark {
        self.marks.last().copied().unwrap_or(Mark {
            index: 0,
            line: 0,
            column: 0,
        })
    }

    // --- the states ---

    fn run(&mut self, state: State) -> Result<Event, LoadError> {
        match state {
            State::StreamStart => self.parse_stream_start(),
            State::ImplicitDocumentStart => self.parse_implicit_document(),
            State::DocumentStart => self.parse_document_start(),
            State::DocumentEnd => self.parse_document_end(),
            State::DocumentContent => self.parse_document_content(),
            State::BlockNode => self.parse_node(NodeForm::Block),
            State::BlockSequenceFirstEntry => {
                let token = self.take()?;
                self.marks.push(token.start);
                self.parse_block_sequence_entry()
            }
            State::BlockSequenceEntry => self.parse_block_sequence_entry(),
            State::IndentlessSequenceEntry => self.parse_indentless_entry(),
            State::BlockMappingFirstKey => {
                let token = self.take()?;
                self.marks.push(token.start);
                self.parse_block_mapping_key()
            }
            State::BlockMappingKey => self.parse_block_mapping_key(),
            State::BlockMappingValue => self.parse_block_mapping_value(),
            State::FlowSequenceFirstEntry => {
                let token = self.take()?;
                self.marks.push(token.start);
                self.parse_flow_sequence_entry(Position::First)
            }
            State::FlowSequenceEntry => self.parse_flow_sequence_entry(Position::Later),
            State::FlowSequenceEntryMappingKey => self.parse_flow_seq_map_key(),
            State::FlowSequenceEntryMappingValue => self.parse_flow_seq_map_value(),
            State::FlowSequenceEntryMappingEnd => {
                self.state = Some(State::FlowSequenceEntry);
                let token = self.peek()?;

                Ok(Event {
                    kind: EventKind::MappingEnd,
                    start: token.start,
                })
            }
            State::FlowMappingFirstKey => {
                let token = self.take()?;
                self.marks.push(token.start);
                self.parse_flow_mapping_key(Position::First)
            }
            State::FlowMappingKey => self.parse_flow_mapping_key(Position::Later),
            State::FlowMappingValue => self.parse_flow_mapping_value(),
            State::FlowMappingEmptyValue => {
                self.state = Some(State::FlowMappingKey);
                let token = self.peek()?;

                Ok(empty_scalar(token.start))
            }
        }
    }

    fn parse_stream_start(&mut self) -> Result<Event, LoadError> {
        let token = self.take()?;
        self.state = Some(State::ImplicitDocumentStart);

        Ok(Event {
            kind: EventKind::StreamStart,
            start: token.start,
        })
    }

    fn parse_implicit_document(&mut self) -> Result<Event, LoadError> {
        let token = self.peek()?;
        if matches!(
            token.kind,
            TokenKind::Directive(_) | TokenKind::DocumentStart | TokenKind::StreamEnd
        ) {
            return self.parse_document_start();
        }

        self.tag_handles = default_tags();
        self.states.push(State::DocumentEnd);
        self.state = Some(State::BlockNode);

        Ok(Event {
            kind: EventKind::DocumentStart,
            start: token.start,
        })
    }

    fn parse_document_start(&mut self) -> Result<Event, LoadError> {
        while self.peek()?.kind == TokenKind::DocumentEnd {
            self.take()?;
        }

        let token = self.peek()?;
        if token.kind == TokenKind::StreamEnd {
            self.take()?;
            self.state = None;

            return Ok(Event {
                kind: EventKind::StreamEnd,
                start: token.start,
            });
        }

        self.process_directives()?;
        let next = self.peek()?;
        if next.kind != TokenKind::DocumentStart {
            return Err(self.error(
                None,
                &format!("expected '<document start>', but found {}", next.kind.id()),
                next.start,
            ));
        }

        self.take()?;
        self.states.push(State::DocumentEnd);
        self.state = Some(State::DocumentContent);

        Ok(Event {
            kind: EventKind::DocumentStart,
            start: token.start,
        })
    }

    fn parse_document_end(&mut self) -> Result<Event, LoadError> {
        let token = self.peek()?;
        if token.kind == TokenKind::DocumentEnd {
            self.take()?;
        }

        self.state = Some(State::DocumentStart);

        Ok(Event {
            kind: EventKind::DocumentEnd,
            start: token.start,
        })
    }

    fn parse_document_content(&mut self) -> Result<Event, LoadError> {
        let token = self.peek()?;
        if matches!(
            token.kind,
            TokenKind::Directive(_)
                | TokenKind::DocumentStart
                | TokenKind::DocumentEnd
                | TokenKind::StreamEnd
        ) {
            self.pop_state();

            return Ok(empty_scalar(token.start));
        }

        self.parse_node(NodeForm::Block)
    }

    fn process_directives(&mut self) -> Result<(), LoadError> {
        self.yaml_directive_seen = false;
        self.tag_handles = BTreeMap::new();
        loop {
            let token = self.peek()?;
            let TokenKind::Directive(directive) = token.kind else {
                break;
            };

            self.take()?;
            match directive {
                Directive::Yaml { major_is_one } => {
                    if self.yaml_directive_seen {
                        return Err(self.error(
                            None,
                            "found duplicate YAML directive",
                            token.start,
                        ));
                    }

                    if !major_is_one {
                        return Err(self.error(
                            None,
                            "found incompatible YAML document (version 1.* is required)",
                            token.start,
                        ));
                    }

                    self.yaml_directive_seen = true;
                }
                Directive::Tag { handle, prefix } => {
                    if self.tag_handles.contains_key(&handle) {
                        return Err(self.error(
                            None,
                            &format!("duplicate tag handle {}", repr_str(&handle)),
                            token.start,
                        ));
                    }

                    self.tag_handles.insert(handle, prefix);
                }
                Directive::Other => {}
            }
        }

        for (handle, prefix) in default_tags() {
            self.tag_handles.entry(handle).or_insert(prefix);
        }

        Ok(())
    }

    /// The anchor and the tag before the content of a node, in each order.
    fn parse_properties(&mut self) -> Result<Properties, LoadError> {
        let mut anchor = None;
        let mut tag = None;
        let mut start = None;
        let mut tag_mark = None;
        let first = self.peek()?;
        if let TokenKind::Anchor(name) = first.kind {
            self.take()?;
            start = Some(first.start);
            anchor = Some(name);
            let second = self.peek()?;
            if let TokenKind::Tag { handle, suffix } = second.kind {
                self.take()?;
                tag_mark = Some(second.start);
                tag = Some((handle, suffix));
            }
        } else if let TokenKind::Tag { handle, suffix } = first.kind {
            self.take()?;
            start = Some(first.start);
            tag_mark = Some(first.start);
            tag = Some((handle, suffix));
            let second = self.peek()?;
            if let TokenKind::Anchor(name) = second.kind {
                self.take()?;
                anchor = Some(name);
            }
        }

        let tag = match tag {
            None => None,
            Some((None, suffix)) => Some(suffix),
            Some((Some(handle), suffix)) => {
                let Some(prefix) = self.tag_handles.get(&handle) else {
                    let at = tag_mark.unwrap_or(first.start);

                    return Err(self.error(
                        Some(("while parsing a node", start.unwrap_or(first.start))),
                        &format!("found undefined tag handle {}", repr_str(&handle)),
                        at,
                    ));
                };

                Some(format!("{prefix}{suffix}"))
            }
        };
        let start = match start {
            Some(start) => start,
            None => self.peek()?.start,
        };

        Ok(Properties { anchor, tag, start })
    }

    fn parse_node(&mut self, form: NodeForm) -> Result<Event, LoadError> {
        let first = self.peek()?;
        if let TokenKind::Alias(name) = first.kind {
            self.take()?;
            self.pop_state();

            return Ok(Event {
                kind: EventKind::Alias(name),
                start: first.start,
            });
        }

        let Properties { anchor, tag, start } = self.parse_properties()?;
        let block = form != NodeForm::Flow;
        let token = self.peek()?;
        let kind = match token.kind {
            TokenKind::BlockEntry if form == NodeForm::BlockOrIndentless => {
                self.state = Some(State::IndentlessSequenceEntry);
                EventKind::SequenceStart { anchor, tag }
            }
            TokenKind::Scalar { value, style } => {
                self.take()?;
                self.pop_state();
                let plain_implicit = (style == Style::Plain && tag.is_none())
                    || tag.as_deref() == Some(NON_SPECIFIC_TAG);
                EventKind::Scalar {
                    anchor,
                    tag,
                    plain_implicit,
                    value,
                }
            }
            TokenKind::FlowSequenceStart => {
                self.state = Some(State::FlowSequenceFirstEntry);
                EventKind::SequenceStart { anchor, tag }
            }
            TokenKind::FlowMappingStart => {
                self.state = Some(State::FlowMappingFirstKey);
                EventKind::MappingStart { anchor, tag }
            }
            TokenKind::BlockSequenceStart if block => {
                self.state = Some(State::BlockSequenceFirstEntry);
                EventKind::SequenceStart { anchor, tag }
            }
            TokenKind::BlockMappingStart if block => {
                self.state = Some(State::BlockMappingFirstKey);
                EventKind::MappingStart { anchor, tag }
            }
            _ if anchor.is_some() || tag.is_some() => {
                self.pop_state();
                let plain_implicit = tag.is_none() || tag.as_deref() == Some(NON_SPECIFIC_TAG);
                EventKind::Scalar {
                    anchor,
                    tag,
                    plain_implicit,
                    value: String::new(),
                }
            }
            other => {
                let context = if block {
                    "while parsing a block node"
                } else {
                    "while parsing a flow node"
                };

                return Err(self.error(
                    Some((context, start)),
                    &format!("expected the node content, but found {}", other.id()),
                    token.start,
                ));
            }
        };

        Ok(Event { kind, start })
    }

    fn parse_block_sequence_entry(&mut self) -> Result<Event, LoadError> {
        let token = self.peek()?;
        if token.kind == TokenKind::BlockEntry {
            self.take()?;
            let next = self.peek()?;
            if matches!(next.kind, TokenKind::BlockEntry | TokenKind::BlockEnd) {
                self.state = Some(State::BlockSequenceEntry);

                return Ok(empty_scalar(token.end));
            }

            self.states.push(State::BlockSequenceEntry);

            return self.parse_node(NodeForm::Block);
        }

        if token.kind != TokenKind::BlockEnd {
            return Err(self.error(
                Some(("while parsing a block collection", self.last_mark())),
                &format!("expected <block end>, but found {}", token.kind.id()),
                token.start,
            ));
        }

        self.take()?;
        self.pop_state();
        self.marks.pop();

        Ok(Event {
            kind: EventKind::SequenceEnd,
            start: token.start,
        })
    }

    fn parse_indentless_entry(&mut self) -> Result<Event, LoadError> {
        let token = self.peek()?;
        if token.kind == TokenKind::BlockEntry {
            self.take()?;
            let next = self.peek()?;
            if matches!(
                next.kind,
                TokenKind::BlockEntry | TokenKind::Key | TokenKind::Value | TokenKind::BlockEnd
            ) {
                self.state = Some(State::IndentlessSequenceEntry);

                return Ok(empty_scalar(token.end));
            }

            self.states.push(State::IndentlessSequenceEntry);

            return self.parse_node(NodeForm::Block);
        }

        self.pop_state();

        Ok(Event {
            kind: EventKind::SequenceEnd,
            start: token.start,
        })
    }

    fn parse_block_mapping_key(&mut self) -> Result<Event, LoadError> {
        let token = self.peek()?;
        if token.kind == TokenKind::Key {
            self.take()?;
            let next = self.peek()?;
            if matches!(
                next.kind,
                TokenKind::Key | TokenKind::Value | TokenKind::BlockEnd
            ) {
                self.state = Some(State::BlockMappingValue);

                return Ok(empty_scalar(token.end));
            }

            self.states.push(State::BlockMappingValue);

            return self.parse_node(NodeForm::BlockOrIndentless);
        }

        if token.kind != TokenKind::BlockEnd {
            return Err(self.error(
                Some(("while parsing a block mapping", self.last_mark())),
                &format!("expected <block end>, but found {}", token.kind.id()),
                token.start,
            ));
        }

        self.take()?;
        self.pop_state();
        self.marks.pop();

        Ok(Event {
            kind: EventKind::MappingEnd,
            start: token.start,
        })
    }

    fn parse_block_mapping_value(&mut self) -> Result<Event, LoadError> {
        let token = self.peek()?;
        if token.kind != TokenKind::Value {
            self.state = Some(State::BlockMappingKey);

            return Ok(empty_scalar(token.start));
        }

        self.take()?;
        let next = self.peek()?;
        if matches!(
            next.kind,
            TokenKind::Key | TokenKind::Value | TokenKind::BlockEnd
        ) {
            self.state = Some(State::BlockMappingKey);

            return Ok(empty_scalar(token.end));
        }

        self.states.push(State::BlockMappingKey);

        self.parse_node(NodeForm::BlockOrIndentless)
    }

    /// The `,` between two entries of a flow collection.
    fn take_flow_entry(&mut self, context: &str, closing: char) -> Result<(), LoadError> {
        let token = self.peek()?;
        if token.kind != TokenKind::FlowEntry {
            return Err(self.error(
                Some((context, self.last_mark())),
                &format!("expected ',' or '{closing}', but got {}", token.kind.id()),
                token.start,
            ));
        }

        self.take()?;

        Ok(())
    }

    fn parse_flow_sequence_entry(&mut self, position: Position) -> Result<Event, LoadError> {
        if self.peek()?.kind != TokenKind::FlowSequenceEnd {
            if position == Position::Later {
                self.take_flow_entry("while parsing a flow sequence", ']')?;
            }

            let token = self.peek()?;
            if token.kind == TokenKind::Key {
                self.state = Some(State::FlowSequenceEntryMappingKey);

                return Ok(Event {
                    kind: EventKind::MappingStart {
                        anchor: None,
                        tag: None,
                    },
                    start: token.start,
                });
            }

            if token.kind != TokenKind::FlowSequenceEnd {
                self.states.push(State::FlowSequenceEntry);

                return self.parse_node(NodeForm::Flow);
            }
        }

        let token = self.take()?;
        self.pop_state();
        self.marks.pop();

        Ok(Event {
            kind: EventKind::SequenceEnd,
            start: token.start,
        })
    }

    fn parse_flow_seq_map_key(&mut self) -> Result<Event, LoadError> {
        let token = self.take()?;
        let next = self.peek()?;
        if matches!(
            next.kind,
            TokenKind::Value | TokenKind::FlowEntry | TokenKind::FlowSequenceEnd
        ) {
            self.state = Some(State::FlowSequenceEntryMappingValue);

            return Ok(empty_scalar(token.end));
        }

        self.states.push(State::FlowSequenceEntryMappingValue);

        self.parse_node(NodeForm::Flow)
    }

    fn parse_flow_seq_map_value(&mut self) -> Result<Event, LoadError> {
        let token = self.peek()?;
        if token.kind != TokenKind::Value {
            self.state = Some(State::FlowSequenceEntryMappingEnd);

            return Ok(empty_scalar(token.start));
        }

        self.take()?;
        let next = self.peek()?;
        if matches!(next.kind, TokenKind::FlowEntry | TokenKind::FlowSequenceEnd) {
            self.state = Some(State::FlowSequenceEntryMappingEnd);

            return Ok(empty_scalar(token.end));
        }

        self.states.push(State::FlowSequenceEntryMappingEnd);

        self.parse_node(NodeForm::Flow)
    }

    fn parse_flow_mapping_key(&mut self, position: Position) -> Result<Event, LoadError> {
        if self.peek()?.kind != TokenKind::FlowMappingEnd {
            if position == Position::Later {
                self.take_flow_entry("while parsing a flow mapping", '}')?;
            }

            let token = self.peek()?;
            if token.kind == TokenKind::Key {
                self.take()?;
                let next = self.peek()?;
                if matches!(
                    next.kind,
                    TokenKind::Value | TokenKind::FlowEntry | TokenKind::FlowMappingEnd
                ) {
                    self.state = Some(State::FlowMappingValue);

                    return Ok(empty_scalar(token.end));
                }

                self.states.push(State::FlowMappingValue);

                return self.parse_node(NodeForm::Flow);
            }

            if token.kind != TokenKind::FlowMappingEnd {
                self.states.push(State::FlowMappingEmptyValue);

                return self.parse_node(NodeForm::Flow);
            }
        }

        let token = self.take()?;
        self.pop_state();
        self.marks.pop();

        Ok(Event {
            kind: EventKind::MappingEnd,
            start: token.start,
        })
    }

    fn parse_flow_mapping_value(&mut self) -> Result<Event, LoadError> {
        let token = self.peek()?;
        if token.kind != TokenKind::Value {
            self.state = Some(State::FlowMappingKey);

            return Ok(empty_scalar(token.start));
        }

        self.take()?;
        let next = self.peek()?;
        if matches!(next.kind, TokenKind::FlowEntry | TokenKind::FlowMappingEnd) {
            self.state = Some(State::FlowMappingKey);

            return Ok(empty_scalar(token.end));
        }

        self.states.push(State::FlowMappingKey);

        self.parse_node(NodeForm::Flow)
    }
}
