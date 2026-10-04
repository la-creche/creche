//! A YAML reader that reads a text as PyYAML 6.0.3 reads it.
//!
//! The Python validator calls `yaml.safe_load_all`. PyYAML is a YAML 1.1
//! reader: `yes` is a boolean, `010` is eight and `1:30` is ninety. A reader
//! for YAML 1.2 gives other values for the same text, so it accepts another
//! set of family files. This module is a port of the PyYAML code path that
//! the validator uses: the reader, the scanner, the parser, the composer,
//! the resolver and the safe constructor. A text has the same value here as
//! in Python, and an error has the same message.
//!
//! PyYAML has the MIT license:
//!
//! Copyright (c) 2017-2021 Ingy döt Net
//! Copyright (c) 2006-2016 Kirill Simonov
//!
//! Permission is hereby granted, free of charge, to any person obtaining a
//! copy of this software and associated documentation files (the
//! "Software"), to deal in the Software without restriction, including
//! without limitation the rights to use, copy, modify, merge, publish,
//! distribute, sublicense, and/or sell copies of the Software, and to permit
//! persons to whom the Software is furnished to do so, subject to the
//! following conditions:
//!
//! The above copyright notice and this permission notice shall be included
//! in all copies or substantial portions of the Software.
//!
//! THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS
//! OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
//! MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN
//! NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM,
//! DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR
//! OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE
//! USE OR OTHER DEALINGS IN THE SOFTWARE.

mod compose;
mod construct;
mod mark;
mod parser;
mod scanner;
pub(crate) mod text;

#[cfg(test)]
pub(crate) use compose::NESTING_MAX;
pub(crate) use construct::{DateTime, Documents, Int, Obj, ObjId, Value};

/// The largest count of decimal digits that Python reads as one integer, and
/// that Python writes for one integer.
pub(crate) const PYTHON_INT_DIGITS: usize = 4300;

/// Why a text has no value.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) enum LoadError {
    /// PyYAML refuses the text with a `YAMLError`. The field is the message
    /// of that error, as PyYAML writes it.
    Syntax(String),
    /// The text holds a value that this reader cannot make, for example a
    /// string with a lone surrogate. The field is the reason of this reader.
    Unreadable(String),
}

impl std::fmt::Display for LoadError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Syntax(message) | Self::Unreadable(message) => f.write_str(message),
        }
    }
}

impl std::error::Error for LoadError {}

/// Every document of `text`, as `list(yaml.safe_load_all(text))` gives them.
///
/// # Errors
///
/// Returns a [`LoadError`] for a text that PyYAML refuses, and for a text
/// with a value that this reader cannot make.
pub(crate) fn load_all(text: &str) -> Result<Documents, LoadError> {
    construct::load_all(text)
}
