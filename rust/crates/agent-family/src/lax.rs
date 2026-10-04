//! A YAML value to a number, a boolean or a string, as pydantic reads it.
//!
//! The Python models of `agent_family` read each field in the lax mode of
//! pydantic 2: the text `"2"` is the integer 2, and `on` is true. Each
//! function here gives the value that pydantic gives, or the message of the
//! pydantic error.

use creche_contracts::family::RawInt;

use crate::yaml::{Int, Obj, PYTHON_INT_DIGITS};

pub(crate) const STRING_TYPE: &str = "Input should be a valid string";
const STRING_UNICODE: &str =
    "Input should be a valid string, unable to parse raw data as a unicode string";
const INT_TYPE: &str = "Input should be a valid integer";
const INT_PARSING: &str = "Input should be a valid integer, unable to parse string as an integer";
const INT_SIZE: &str = "Unable to parse input string as an integer, exceeded maximum size";
const INT_FROM_FLOAT: &str = "Input should be a valid integer, got a number with a fractional part";
const FINITE_NUMBER: &str = "Input should be a finite number";
const FLOAT_TYPE: &str = "Input should be a valid number";
const FLOAT_PARSING: &str = "Input should be a valid number, unable to parse string as a number";
const BOOL_TYPE: &str = "Input should be a valid boolean";
const BOOL_PARSING: &str = "Input should be a valid boolean, unable to interpret input";

/// 2 to the power 63. pydantic reads a float as an integer only between
/// this number and its negative, and not at either one.
const I64_EDGE: f64 = 9_223_372_036_854_775_808.0;

/// A string field: a string, or bytes that are UTF-8 text.
pub(crate) fn as_str(obj: &Obj) -> Result<String, &'static str> {
    match obj {
        Obj::Str(text) => Ok(text.clone()),
        Obj::Bytes(data) => String::from_utf8(data.clone()).map_err(|_| STRING_UNICODE),
        _ => Err(STRING_TYPE),
    }
}

fn raw_int(value: &Int) -> RawInt {
    RawInt::from_decimal(&value.to_string()).unwrap_or_else(|| RawInt::from(0))
}

/// The digits of a text after the zeros at its start, as pydantic takes
/// them. A `-` can follow the zeros: pydantic reads `0-1` as -1.
fn after_leading_zeros(text: &str) -> Option<&str> {
    let mut chars = text.char_indices();
    match chars.next() {
        Some((_, '0')) => {}
        Some((_, '1'..='9')) => return Some(text),
        _ => return None,
    }

    let mut previous = 0;
    for (index, c) in chars {
        match c {
            '0' | '_' => previous = index,
            '1'..='9' | '-' => return text.get(index..),
            '.' => return text.get(previous..),
            _ => return None,
        }
    }

    text.get(previous..)
}

/// The text without a final `.0`, `.00` and so on.
fn without_zero_fraction(text: &str) -> &str {
    match text.split_once('.') {
        Some((whole, zeros)) if !zeros.is_empty() && zeros.bytes().all(|byte| byte == b'0') => {
            whole
        }
        _ => text,
    }
}

/// The text without each `_`, when each one is between two other characters
/// and no two are together.
fn without_underscores(text: &str) -> Option<String> {
    if text.starts_with('_') || text.ends_with('_') || text.contains("__") {
        return None;
    }

    Some(text.replace('_', ""))
}

fn int_from_text(text: &str) -> Result<RawInt, &'static str> {
    let text = text.trim();
    let (negative, body) = match text.strip_prefix('-') {
        Some(rest) => (true, rest),
        None => (false, text.strip_prefix('+').unwrap_or(text)),
    };
    let rest = after_leading_zeros(body).ok_or(INT_PARSING)?;
    let cleaned = without_underscores(without_zero_fraction(rest)).ok_or(INT_PARSING)?;
    let (negative, digits) = match cleaned.strip_prefix('-') {
        Some(_) if negative => return Err(INT_PARSING),
        Some(digits) => (true, digits),
        None => (negative, cleaned.as_str()),
    };
    if digits.len() + usize::from(negative) > PYTHON_INT_DIGITS {
        return Err(INT_SIZE);
    }

    if digits.is_empty() || !digits.bytes().all(|byte| byte.is_ascii_digit()) {
        return Err(INT_PARSING);
    }

    let digits = digits.trim_start_matches('0');
    let digits = if digits.is_empty() { "0" } else { digits };
    let sign = if negative && digits != "0" { "-" } else { "" };

    RawInt::from_decimal(&format!("{sign}{digits}")).ok_or(INT_PARSING)
}

/// The integer of a float with no fraction, when pydantic takes it.
fn whole_float(value: f64) -> Result<i64, &'static str> {
    if !value.is_finite() {
        return Err(FINITE_NUMBER);
    }

    if value.fract() != 0.0 {
        return Err(INT_FROM_FLOAT);
    }

    if value <= -I64_EDGE || value >= I64_EDGE {
        return Err(INT_SIZE);
    }

    format!("{value:.0}").parse().map_err(|_| INT_SIZE)
}

/// An integer field.
pub(crate) fn as_int(obj: &Obj) -> Result<RawInt, &'static str> {
    match obj {
        Obj::Bool(value) => Ok(RawInt::from(i64::from(*value))),
        Obj::Int(value) => Ok(raw_int(value)),
        Obj::Float(value) => whole_float(*value).map(RawInt::from),
        Obj::SharedNan => Err(FINITE_NUMBER),
        Obj::Str(text) => int_from_text(text),
        Obj::Bytes(data) => {
            let text = std::str::from_utf8(data).map_err(|_| INT_PARSING)?;

            int_from_text(text)
        }
        _ => Err(INT_TYPE),
    }
}

/// pydantic reads the text with no space at either end. When that fails,
/// it reads the text as it is, without its underscores.
fn float_from_text(text: &str) -> Result<f64, &'static str> {
    if let Ok(value) = text.trim().parse() {
        return Ok(value);
    }

    let cleaned = without_underscores(text).ok_or(FLOAT_PARSING)?;

    cleaned.parse().map_err(|_| FLOAT_PARSING)
}

/// A float field.
pub(crate) fn as_float(obj: &Obj) -> Result<f64, &'static str> {
    match obj {
        Obj::Bool(value) => Ok(f64::from(u8::from(*value))),
        Obj::Int(value) => {
            // Python refuses an integer past the last float.
            let float: f64 = value.to_string().parse().map_err(|_| FLOAT_TYPE)?;
            if float.is_infinite() {
                return Err(FLOAT_TYPE);
            }

            Ok(float)
        }
        Obj::Float(value) => Ok(*value),
        Obj::SharedNan => Ok(f64::NAN),
        Obj::Str(text) => float_from_text(text),
        Obj::Bytes(data) => {
            let text = std::str::from_utf8(data).map_err(|_| FLOAT_PARSING)?;

            float_from_text(text)
        }
        _ => Err(FLOAT_TYPE),
    }
}

fn bool_from_text(text: &str) -> Result<bool, &'static str> {
    const FALSE: [&str; 6] = ["0", "off", "f", "false", "n", "no"];
    const TRUE: [&str; 6] = ["1", "on", "t", "true", "y", "yes"];
    if FALSE.iter().any(|word| text.eq_ignore_ascii_case(word)) {
        return Ok(false);
    }

    if TRUE.iter().any(|word| text.eq_ignore_ascii_case(word)) {
        return Ok(true);
    }

    Err(BOOL_PARSING)
}

fn bool_from_int(value: Option<i64>) -> Result<bool, &'static str> {
    match value {
        Some(0) => Ok(false),
        Some(1) => Ok(true),
        Some(_) => Err(BOOL_PARSING),
        None => Err(BOOL_TYPE),
    }
}

/// A boolean field.
pub(crate) fn as_bool(obj: &Obj) -> Result<bool, &'static str> {
    match obj {
        Obj::Bool(value) => Ok(*value),
        Obj::Int(value) => bool_from_int(value.to_i64()),
        Obj::Float(value) => bool_from_int(whole_float(*value).ok()),
        Obj::Str(text) => bool_from_text(text),
        Obj::Bytes(data) => {
            let text = std::str::from_utf8(data).map_err(|_| BOOL_PARSING)?;

            bool_from_text(text)
        }
        _ => Err(BOOL_TYPE),
    }
}

#[cfg(test)]
mod tests {
    use super::{bool_from_text, float_from_text, int_from_text};

    fn int(text: &str) -> Option<String> {
        int_from_text(text).ok().map(|value| value.to_string())
    }

    /// What pydantic 2.13 answers for `TypeAdapter(int).validate_python`.
    #[test]
    fn a_text_is_the_integer_that_pydantic_reads() {
        let accepted = [
            ("2", "2"),
            (" 2 ", "2"),
            ("+2", "2"),
            ("-2", "-2"),
            ("2.0", "2"),
            ("2.00", "2"),
            ("02", "2"),
            ("-02", "-2"),
            ("00", "0"),
            ("-0", "0"),
            ("-00.0", "0"),
            ("1_0", "10"),
            ("0_1", "1"),
            ("0__1", "1"),
            ("-0__1", "-1"),
            ("1_0.0", "10"),
            ("0-1", "-1"),
            ("0_-_1", "-1"),
            ("0-1.0", "-1"),
            ("\u{a0}2", "2"),
            ("9223372036854775808", "9223372036854775808"),
            ("-9_223372036854775809", "-9223372036854775809"),
            ("99999999999999999999999.0", "99999999999999999999999"),
        ];
        for (text, wanted) in accepted {
            assert_eq!(int(text).as_deref(), Some(wanted), "{text:?}");
        }
    }

    #[test]
    fn a_text_that_pydantic_refuses_is_no_integer() {
        let refused = [
            "", " ", "2.", ".0", "0.", "0x2", "1__0", "_1", "1_", "-_1", "+_1", "2e0", "--2",
            "+-2", "-0-1", "0+1", "0--1", "2 3", "+ 2", "2.5", "2.0_0", "1_.0", "0_.0", "0_",
            "\u{662}", "\u{ff12}", "0.0.0", "- 1",
        ];
        for text in refused {
            assert_eq!(int(text), None, "{text:?}");
        }
    }

    #[test]
    fn a_text_is_the_float_that_pydantic_reads() {
        let accepted = [
            ("2.5", 2.5),
            (" 2.5 ", 2.5),
            ("1e3", 1000.0),
            ("1_000.5", 1000.5),
            ("1_e3", 1000.0),
            ("+_1", 1.0),
            ("._1", 0.1),
            (".5", 0.5),
            ("5.", 5.0),
            ("-.5e3", -500.0),
            ("inf", f64::INFINITY),
            ("-Infinity", f64::NEG_INFINITY),
            ("1e400", f64::INFINITY),
        ];
        for (text, wanted) in accepted {
            assert_eq!(float_from_text(text).ok(), Some(wanted), "{text:?}");
        }

        assert!(float_from_text("NaN").is_ok_and(f64::is_nan));
        let refused = [
            "", "1__0", "_1", "1_", "0x10", "1,5", "1e", "e1", "1 .5", "\u{661}", " 0_1", "1_0 ",
        ];
        for text in refused {
            assert_eq!(float_from_text(text).ok(), None, "{text:?}");
        }
    }

    #[test]
    fn a_text_is_the_boolean_that_pydantic_reads() {
        for text in ["1", "on", "ON", "t", "T", "true", "tRuE", "y", "yes"] {
            assert_eq!(bool_from_text(text), Ok(true), "{text:?}");
        }

        for text in ["0", "off", "f", "FALSE", "n", "No"] {
            assert_eq!(bool_from_text(text), Ok(false), "{text:?}");
        }

        for text in ["", " yes", "yes ", "2", "01", "1.0", "enable"] {
            assert!(bool_from_text(text).is_err(), "{text:?}");
        }
    }
}
