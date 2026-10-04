//! The numbers of one line from the playpen.
//!
//! The playpen is JavaScript. A count comes as a large integer or as a
//! float, and the Python host keeps an integer of any size. The types here
//! keep what the Python host keeps (contract 03 §13 rule 7).

use std::error::Error;
use std::fmt;
use std::str::FromStr;

/// The largest count of digits in one integer of a line. Python reads no
/// longer text as an integer, and the Python host refuses the line.
pub const MAX_INTEGER_DIGITS: usize = 4300;

/// The text of the integer zero.
const ZERO: &str = "0";

/// The sign of a negative integer.
const MINUS: char = '-';

/// An integer of any size, as a line from the playpen holds it.
///
/// A reader that needs a machine integer calls [`Integer::to_u64`] or
/// [`Integer::to_i64`]. Each returns `None` for a value that does not fit.
///
/// ```
/// use creche_contracts::channel::number::Integer;
///
/// let large: Integer = "36893488147419103232".parse()?;
/// assert_eq!(large.to_u64(), None);
/// assert_eq!(Integer::from(7_u64).to_i64(), Some(7));
/// # Ok::<(), creche_contracts::channel::number::IntegerError>(())
/// ```
///
/// Code outside this module cannot build a value from a raw text:
///
/// ```compile_fail,E0423
/// use creche_contracts::channel::number::Integer;
///
/// let integer = Integer(String::from("007"));
/// ```
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct Integer(String);

impl Integer {
    /// The integer as decimal text: no `+`, no zero at the start, and `0`
    /// for zero.
    #[must_use]
    pub fn as_str(&self) -> &str {
        &self.0
    }

    /// Whether the integer is below zero.
    #[must_use]
    pub fn is_negative(&self) -> bool {
        self.0.starts_with(MINUS)
    }

    /// The integer as a `u64`. `None` when it does not fit.
    #[must_use]
    pub fn to_u64(&self) -> Option<u64> {
        self.0.parse().ok()
    }

    /// The integer as an `i64`. `None` when it does not fit.
    #[must_use]
    pub fn to_i64(&self) -> Option<i64> {
        self.0.parse().ok()
    }

    /// The nearest `f64`, with ties to even. `None` when the integer is past
    /// the range of an `f64`. Python raises `OverflowError` there.
    pub(super) fn to_f64(&self) -> Option<f64> {
        self.0.parse::<f64>().ok().filter(|float| float.is_finite())
    }
}

impl FromStr for Integer {
    type Err = IntegerError;

    /// Reads the integer of a JSON number: `-?(0|[1-9][0-9]*)`.
    fn from_str(text: &str) -> Result<Self, Self::Err> {
        let digits = text.strip_prefix(MINUS).unwrap_or(text);
        if digits.is_empty() || !digits.bytes().all(|byte| byte.is_ascii_digit()) {
            return Err(IntegerError::NotDigits);
        }

        if digits.len() > 1 && digits.starts_with(ZERO) {
            return Err(IntegerError::ZeroAtStart);
        }

        if digits.len() > MAX_INTEGER_DIGITS {
            return Err(IntegerError::TooLong);
        }

        // Python reads `-0` as the integer 0.
        let text = if digits == ZERO { ZERO } else { text };

        Ok(Self(text.to_owned()))
    }
}

impl From<u64> for Integer {
    fn from(value: u64) -> Self {
        Self(value.to_string())
    }
}

impl From<i64> for Integer {
    fn from(value: i64) -> Self {
        Self(value.to_string())
    }
}

impl fmt::Display for Integer {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.0)
    }
}

/// Why a text is not an integer of a line.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum IntegerError {
    /// The text is not an optional `-` and then ASCII digits.
    NotDigits,
    /// The text has a zero before another digit.
    ZeroAtStart,
    /// The text has more than [`MAX_INTEGER_DIGITS`] digits.
    TooLong,
}

impl fmt::Display for IntegerError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::NotDigits => f.write_str("an integer is an optional - and then ASCII digits"),
            Self::ZeroAtStart => f.write_str("an integer has no zero before another digit"),
            Self::TooLong => write!(f, "an integer has at most {MAX_INTEGER_DIGITS} digits"),
        }
    }
}

impl Error for IntegerError {}

/// A count that the sandbox reported: an integer that is 0 or more.
///
/// The value is advisory (contract 03 §13 rule 7). It has no upper limit,
/// because the Python host has none. [`Count::to_u64`] returns `None` for a
/// count past 64 bits.
///
/// ```
/// use creche_contracts::channel::number::Count;
///
/// assert_eq!(Count::from(12_u64).to_u64(), Some(12));
/// ```
///
/// Code outside this module cannot build a count from an integer below 0:
///
/// ```compile_fail,E0423
/// use creche_contracts::channel::number::{Count, Integer};
///
/// let count = Count(Integer::from(-1_i64));
/// ```
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct Count(Integer);

impl Count {
    /// The count as an integer.
    #[must_use]
    pub fn as_integer(&self) -> &Integer {
        &self.0
    }

    /// The count as a `u64`. `None` when it does not fit.
    #[must_use]
    pub fn to_u64(&self) -> Option<u64> {
        self.0.to_u64()
    }

    /// Whether the count is zero.
    #[must_use]
    pub fn is_zero(&self) -> bool {
        self.0.as_str() == ZERO
    }
}

impl TryFrom<Integer> for Count {
    type Error = NegativeCount;

    fn try_from(integer: Integer) -> Result<Self, Self::Error> {
        if integer.is_negative() {
            return Err(NegativeCount);
        }

        Ok(Self(integer))
    }
}

impl From<u64> for Count {
    fn from(value: u64) -> Self {
        Self(Integer::from(value))
    }
}

impl fmt::Display for Count {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        self.0.fmt(f)
    }
}

/// An integer below 0 is not a count.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct NegativeCount;

impl fmt::Display for NegativeCount {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("a count is 0 or more")
    }
}

impl Error for NegativeCount {}

/// The place of one message in its turn: an integer that is 1 or more
/// (contract 03 §5.1).
///
/// The value has no upper limit, because the Python host has none. The check
/// of rule 4 of §13 calls [`TurnSeq::to_u64`]. A value past 64 bits is a gap.
///
/// ```
/// use creche_contracts::channel::number::{Count, TurnSeq};
///
/// let seq = TurnSeq::try_from(Count::from(17_u64))?;
/// assert_eq!(seq.to_u64(), Some(17));
/// assert!(TurnSeq::try_from(Count::from(0_u64)).is_err());
/// # Ok::<(), creche_contracts::channel::number::ZeroTurnSeq>(())
/// ```
///
/// Code outside this module cannot build a value from a raw count:
///
/// ```compile_fail,E0423
/// use creche_contracts::channel::number::{Count, TurnSeq};
///
/// let seq = TurnSeq(Count::from(0_u64));
/// ```
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct TurnSeq(Count);

impl TurnSeq {
    /// The place as a count.
    #[must_use]
    pub fn as_count(&self) -> &Count {
        &self.0
    }

    /// The place as a `u64`. `None` when it does not fit.
    #[must_use]
    pub fn to_u64(&self) -> Option<u64> {
        self.0.to_u64()
    }
}

impl TryFrom<Count> for TurnSeq {
    type Error = ZeroTurnSeq;

    fn try_from(count: Count) -> Result<Self, Self::Error> {
        if count.is_zero() {
            return Err(ZeroTurnSeq);
        }

        Ok(Self(count))
    }
}

impl fmt::Display for TurnSeq {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        self.0.fmt(f)
    }
}

/// The count 0 is not the place of a message in a turn.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ZeroTurnSeq;

impl fmt::Display for ZeroTurnSeq {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("the place of a message in a turn is 1 or more")
    }
}

impl Error for ZeroTurnSeq {}

/// The cost in US dollars that the sandbox reported for one turn.
///
/// The value is advisory (contract 03 §13 rule 7). It is finite and never
/// below zero. The Python host reads `NaN`, an infinity and a value below
/// zero as no cost, and this type does the same.
///
/// ```
/// use creche_contracts::channel::number::Cost;
///
/// assert_eq!(Cost::ZERO.get(), 0.0);
/// ```
///
/// Code outside this module cannot build a cost from a raw number:
///
/// ```compile_fail,E0423
/// use creche_contracts::channel::number::Cost;
///
/// let cost = Cost(-1.0);
/// ```
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Cost(f64);

impl Cost {
    /// No cost.
    pub const ZERO: Self = Self(0.0);

    /// The cost that the sandbox reported: `value`, or [`Cost::ZERO`] for a
    /// value below zero and for a value that is not finite.
    pub(super) fn of(value: f64) -> Self {
        if value < 0.0 || !value.is_finite() {
            return Self::ZERO;
        }

        Self(value)
    }

    /// The cost. It is finite.
    #[must_use]
    pub fn get(self) -> f64 {
        self.0
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn integer(text: &str) -> Integer {
        text.parse().unwrap()
    }

    #[test]
    fn an_integer_keeps_its_text() {
        for text in [
            "0",
            "7",
            "-7",
            "18446744073709551616",
            "-9223372036854775809",
        ] {
            assert_eq!(integer(text).as_str(), text);
            assert_eq!(integer(text).to_string(), text);
        }

        assert_eq!(integer("-0").as_str(), "0");
        assert!(!integer("-0").is_negative());
        assert!(integer("-1").is_negative());
        assert_eq!(integer(&"9".repeat(MAX_INTEGER_DIGITS)).to_u64(), None);
    }

    #[test]
    fn a_text_that_is_no_integer_is_refused() {
        let refused = [
            ("", IntegerError::NotDigits),
            ("-", IntegerError::NotDigits),
            ("+1", IntegerError::NotDigits),
            ("1.0", IntegerError::NotDigits),
            ("1e2", IntegerError::NotDigits),
            ("--1", IntegerError::NotDigits),
            ("1\n", IntegerError::NotDigits),
            ("\u{0661}", IntegerError::NotDigits),
            ("01", IntegerError::ZeroAtStart),
            ("-01", IntegerError::ZeroAtStart),
            ("00", IntegerError::ZeroAtStart),
        ];

        for (text, error) in refused {
            assert_eq!(text.parse::<Integer>(), Err(error), "{text:?}");
        }

        let long = "9".repeat(MAX_INTEGER_DIGITS + 1);
        assert_eq!(long.parse::<Integer>(), Err(IntegerError::TooLong));
        assert_eq!(
            format!("-{long}").parse::<Integer>(),
            Err(IntegerError::TooLong)
        );
        assert!(IntegerError::TooLong.to_string().contains("4300"));
    }

    #[test]
    fn an_integer_fits_a_machine_integer_or_does_not() {
        assert_eq!(integer("18446744073709551615").to_u64(), Some(u64::MAX));
        assert_eq!(integer("18446744073709551616").to_u64(), None);
        assert_eq!(integer("-1").to_u64(), None);
        assert_eq!(integer("-9223372036854775808").to_i64(), Some(i64::MIN));
        assert_eq!(integer("9223372036854775808").to_i64(), None);
        assert_eq!(Integer::from(-5_i64), integer("-5"));
    }

    #[test]
    fn an_integer_rounds_to_the_nearest_float() {
        assert_eq!(integer("2").to_f64(), Some(2.0));
        assert_eq!(
            integer("9007199254740993").to_f64(),
            Some(9_007_199_254_740_992.0)
        );
        assert_eq!(
            integer(&format!("1{}", "0".repeat(308))).to_f64(),
            Some(1e308)
        );
        assert_eq!(integer(&format!("1{}", "0".repeat(309))).to_f64(), None);
        assert_eq!(integer(&format!("-1{}", "0".repeat(309))).to_f64(), None);
    }

    #[test]
    fn a_count_is_zero_or_more() {
        assert_eq!(Count::try_from(integer("0")).unwrap().to_u64(), Some(0));
        assert!(Count::try_from(integer("0")).unwrap().is_zero());
        assert_eq!(Count::try_from(integer("-1")), Err(NegativeCount));
        assert_eq!(Count::from(8_u64).to_string(), "8");
        assert_eq!(Count::from(8_u64).as_integer(), &integer("8"));
    }

    #[test]
    fn a_turn_seq_is_one_or_more() {
        let seq = TurnSeq::try_from(Count::from(1_u64)).unwrap();

        assert_eq!(seq.to_u64(), Some(1));
        assert_eq!(seq.to_string(), "1");
        assert_eq!(seq.as_count(), &Count::from(1_u64));
        assert_eq!(TurnSeq::try_from(Count::from(0_u64)), Err(ZeroTurnSeq));
    }

    #[test]
    fn a_cost_is_finite_and_never_below_zero() {
        assert_eq!(Cost::of(0.002).get().to_bits(), 0.002_f64.to_bits());
        assert_eq!(Cost::of(-0.5), Cost::ZERO);
        assert_eq!(Cost::of(f64::NEG_INFINITY), Cost::ZERO);
        assert_eq!(Cost::of(f64::INFINITY), Cost::ZERO);
        assert_eq!(Cost::of(f64::NAN), Cost::ZERO);
        assert_eq!(Cost::of(f64::MAX).get().to_bits(), f64::MAX.to_bits());
        assert!(Cost::of(-0.0).get().is_sign_negative());
    }
}
