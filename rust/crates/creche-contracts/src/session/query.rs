//! The queries of the session API: list sessions, get one session, and read
//! the event stream (contract 02 §5.2, §5.3, §5.5).
//!
//! A raw query holds each parameter as the text of the URL, after the HTTP
//! layer decoded it. A conversion that can fail makes the valid query.

use creche_util::pytext;

use super::error::{ApiError, ErrorCode};
use super::fields::{Follow, PageLimit, TurnRef, TurnsWanted};
use super::json::INT_DIGITS_MAX;
use super::state::{SessionKind, SessionState};
use crate::ids::FamilyName;

fn bad(message: impl Into<String>) -> ApiError {
    ApiError::new(ErrorCode::BadRequest, message)
}

/// The white space that Python's `int` drops around a number: the white space
/// of C in ASCII, and each white space character outside ASCII.
fn is_int_space(character: char) -> bool {
    match character {
        ' ' | '\t' | '\n' | '\u{b}' | '\u{c}' | '\r' => true,
        _ => !character.is_ascii() && character.is_whitespace(),
    }
}

/// The digit separator that Python's `int` permits between two digits.
const UNDERSCORE: u8 = b'_';

/// The whole number that Python's `int` reads from a text. `None` for a text
/// that it refuses.
///
/// The text can have white space around it and one sign. One underscore can
/// stand between two digits. A number has 4300 digits at most. A number
/// outside `i128` becomes the nearest end of `i128`.
///
/// Python reads a decimal digit of each script. This function reads the
/// digits 0 to 9 only (`rust/AGENTS.md`, rule 9).
fn python_int(text: &str) -> Option<i128> {
    let trimmed = text.trim_matches(is_int_space);
    let (negative, digits) = match trimmed.strip_prefix('-') {
        Some(digits) => (true, digits),
        None => (false, trimmed.strip_prefix('+').unwrap_or(trimmed)),
    };
    let bytes = digits.as_bytes();
    let starts_and_ends_with_a_digit = bytes.first().is_some_and(u8::is_ascii_digit)
        && bytes.last().is_some_and(u8::is_ascii_digit);
    let one_underscore_at_a_time = bytes
        .windows(2)
        .all(|pair| pair != [UNDERSCORE, UNDERSCORE]);
    let only_digits_and_underscores = bytes
        .iter()
        .all(|byte| byte.is_ascii_digit() || *byte == UNDERSCORE);
    let count = bytes.iter().filter(|byte| byte.is_ascii_digit()).count();
    if !starts_and_ends_with_a_digit
        || !one_underscore_at_a_time
        || !only_digits_and_underscores
        || count > INT_DIGITS_MAX
    {
        return None;
    }

    let magnitude =
        bytes
            .iter()
            .filter(|byte| byte.is_ascii_digit())
            .fold(0_i128, |value, byte| {
                value
                    .saturating_mul(10)
                    .saturating_add(i128::from(byte.saturating_sub(b'0')))
            });

    Some(if negative {
        magnitude.saturating_neg()
    } else {
        magnitude
    })
}

/// The number of one parameter. `Ok(None)` for an absent parameter.
fn number(name: &str, text: Option<&str>) -> Result<Option<i128>, ApiError> {
    text.map(|text| python_int(text).ok_or_else(|| bad(format!("{name} is not a number"))))
        .transpose()
}

// --- list sessions ---

/// The parameters of `GET /v1/sessions`, as the text of the URL.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct RawListQuery<'a> {
    /// Only the sessions of one family.
    pub family: Option<&'a str>,
    /// Only the sessions in one state.
    pub state: Option<&'a str>,
    /// Only the sessions of families of one kind.
    pub kind: Option<&'a str>,
    /// The largest count of sessions in the answer.
    pub limit: Option<&'a str>,
    /// The cursor of the page before.
    pub cursor: Option<&'a str>,
}

/// The query of `GET /v1/sessions`: list sessions (contract 02 §5.2).
///
/// `limit` is a whole number as Python's `int` reads it: white space around
/// it, a sign, and one underscore between two digits are permitted.
///
/// ```
/// use creche_contracts::session::{ListQuery, RawListQuery};
///
/// let raw = RawListQuery { family: Some("chat"), limit: Some("5"), ..RawListQuery::default() };
/// let query = ListQuery::try_from(raw)?;
/// assert_eq!(query.limit().get(), 5);
/// assert_eq!(query.state(), None);
/// # Ok::<(), creche_contracts::session::ApiError>(())
/// ```
///
/// Code outside this module cannot build a value from raw fields:
///
/// ```compile_fail,E0451
/// use creche_contracts::session::ListQuery;
///
/// fn first_page(query: ListQuery) -> ListQuery {
///     ListQuery { cursor: None, ..query }
/// }
/// ```
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ListQuery {
    family: Option<FamilyName>,
    state: Option<SessionState>,
    kind: Option<SessionKind>,
    limit: PageLimit,
    cursor: Option<String>,
}

impl TryFrom<RawListQuery<'_>> for ListQuery {
    type Error = ApiError;

    fn try_from(raw: RawListQuery<'_>) -> Result<Self, Self::Error> {
        let limit = number("limit", raw.limit)?;
        let family = raw
            .family
            .map(|family| {
                family
                    .parse()
                    .map_err(|_| bad("family is not a family name").in_family(family))
            })
            .transpose()?;
        let state = raw
            .state
            .map(str::parse)
            .transpose()
            .map_err(|_| bad("state is not a known value"))?;
        let kind = raw
            .kind
            .map(str::parse)
            .transpose()
            .map_err(|_| bad("kind is not a known value"))?;
        let limit = limit.map_or(Ok(PageLimit::SESSIONS), |limit| {
            PageLimit::try_from(limit).map_err(|_| bad("limit is outside 1 to 200"))
        })?;

        Ok(Self {
            family,
            state,
            kind,
            limit,
            cursor: raw.cursor.map(str::to_owned),
        })
    }
}

impl ListQuery {
    /// Only the sessions of this family.
    #[must_use]
    pub fn family(&self) -> Option<&FamilyName> {
        self.family.as_ref()
    }

    /// Only the sessions in this state.
    #[must_use]
    pub fn state(&self) -> Option<SessionState> {
        self.state
    }

    /// Only the sessions of families of this kind.
    #[must_use]
    pub fn kind(&self) -> Option<SessionKind> {
        self.kind
    }

    /// The largest count of sessions in the answer.
    #[must_use]
    pub fn limit(&self) -> PageLimit {
        self.limit
    }

    /// The cursor of the page before. `attendance` made the text, and the
    /// parser does not read it.
    #[must_use]
    pub fn cursor(&self) -> Option<&str> {
        self.cursor.as_deref()
    }
}

// --- get one session ---

impl TurnsWanted {
    /// The count of turns that the parameter `turns` of
    /// `GET /v1/sessions/{family}/{session}` asks for (contract 02 §5.3).
    ///
    /// ```
    /// use creche_contracts::session::TurnsWanted;
    ///
    /// assert_eq!(TurnsWanted::from_param(None)?.get(), 10);
    /// assert_eq!(TurnsWanted::from_param(Some("100"))?.get(), 100);
    /// assert!(TurnsWanted::from_param(Some("101")).is_err());
    /// # Ok::<(), creche_contracts::session::ApiError>(())
    /// ```
    pub fn from_param(turns: Option<&str>) -> Result<Self, ApiError> {
        number("turns", turns)?.map_or(Ok(Self::NEWEST), |turns| {
            Self::try_from(turns).map_err(|_| bad("turns is outside 0 to 100"))
        })
    }
}

// --- read the event stream ---

/// The parameters of `GET .../events`, as the text of the URL.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct RawEventsQuery<'a> {
    /// The stream starts after this sequence number.
    pub from_seq: Option<&'a str>,
    /// Only the lines of one turn.
    pub turn: Option<&'a str>,
    /// Whether the stream stays open after the replay.
    pub follow: Option<&'a str>,
}

/// The three texts that mean that a stream ends after the replay.
const DO_NOT_FOLLOW: [&str; 3] = ["0", "false", "no"];

/// The query of `GET .../events`: read the event stream (contract 02 §5.5).
///
/// `from_seq` is a whole number as Python's `int` reads it. `turn` is free
/// text. `follow` is false for `0`, `false` and `no`, in each case and with
/// white space around it. Each other text is true.
///
/// ```
/// use creche_contracts::session::{EventsQuery, Follow, RawEventsQuery};
///
/// let raw = RawEventsQuery { from_seq: Some("41"), follow: Some("No"), ..RawEventsQuery::default() };
/// let query = EventsQuery::try_from(raw)?;
/// assert_eq!(query.from_seq(), 41);
/// assert_eq!(query.follow(), Follow::ReplayOnly);
/// # Ok::<(), creche_contracts::session::ApiError>(())
/// ```
///
/// Code outside this module cannot build a value from raw fields:
///
/// ```compile_fail,E0451
/// use creche_contracts::session::EventsQuery;
///
/// fn from_the_start(query: EventsQuery) -> EventsQuery {
///     EventsQuery { from_seq: 0, ..query }
/// }
/// ```
// CONTRACT-QUESTION: contract 02 §5.5 says that `follow` is a bool. The Python
// route reads each text that is not `0`, `false` or `no` as true. This type
// does the same. A refusal of another text refuses a query that the Python
// code takes today.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct EventsQuery {
    from_seq: u64,
    turn: Option<TurnRef>,
    follow: Follow,
}

impl TryFrom<RawEventsQuery<'_>> for EventsQuery {
    type Error = ApiError;

    fn try_from(raw: RawEventsQuery<'_>) -> Result<Self, Self::Error> {
        let from_seq = number("from_seq", raw.from_seq)?.unwrap_or(0);
        if from_seq < 0 {
            return Err(bad("from_seq is negative"));
        }

        // The Python code takes a number of each size here. No journal has a
        // sequence number past 64 bits, so this parser refuses a larger one.
        let from_seq = u64::try_from(from_seq)
            .map_err(|_| bad(format!("from_seq is outside 0 to {}", u64::MAX)))?;
        let follow = raw.follow.map_or(Follow::KeepOpen, |follow| {
            let word = pytext::strip(follow);
            let stop = DO_NOT_FOLLOW.iter().any(|no| word.eq_ignore_ascii_case(no));

            if stop {
                Follow::ReplayOnly
            } else {
                Follow::KeepOpen
            }
        });

        Ok(Self {
            from_seq,
            turn: raw.turn.map(|turn| TurnRef::from(turn.to_owned())),
            follow,
        })
    }
}

impl EventsQuery {
    /// The stream starts after this sequence number. 0 means each line.
    #[must_use]
    pub fn from_seq(&self) -> u64 {
        self.from_seq
    }

    /// Only the lines of this turn.
    #[must_use]
    pub fn turn(&self) -> Option<&TurnRef> {
        self.turn.as_ref()
    }

    /// Whether the stream stays open after the replay.
    #[must_use]
    pub fn follow(&self) -> Follow {
        self.follow
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_number_is_what_the_int_of_python_reads_in_ascii_digits() {
        let read = [
            ("5", 5),
            ("-0", 0),
            ("+5", 5),
            ("007", 7),
            ("1_0", 10),
            (" 5 ", 5),
            ("\t5\n", 5),
            ("\u{a0}5\u{a0}", 5),
            ("5\u{2003}", 5),
            ("-12", -12),
        ];
        let refused = [
            "",
            " ",
            "ten",
            "1 0",
            "+ 5",
            "+-5",
            "10_",
            "_10",
            "1__0",
            "5.0",
            "1e1",
            "0x10",
            "\u{1c}5\u{1f}",
            "\u{1c}5\u{a0}",
            "+\u{a0}5",
            "\u{661}\u{660}",
            "\u{ff15}",
            "\u{b2}",
            "+",
            "-",
        ];

        for (text, value) in read {
            assert_eq!(python_int(text), Some(value), "{text:?}");
        }
        for text in refused {
            assert_eq!(python_int(text), None, "{text:?}");
        }

        assert_eq!(python_int(&"9".repeat(INT_DIGITS_MAX)), Some(i128::MAX));
        assert_eq!(python_int(&"9".repeat(INT_DIGITS_MAX + 1)), None);
        assert_eq!(
            python_int(&format!("-{}", "9".repeat(60))),
            Some(-i128::MAX)
        );
    }

    #[test]
    fn the_number_of_a_list_is_read_before_each_other_parameter() {
        let raw = RawListQuery {
            family: Some("Chat"),
            limit: Some("ten"),
            ..RawListQuery::default()
        };
        let refusal = ListQuery::try_from(raw).unwrap_err();
        let family = ListQuery::try_from(RawListQuery {
            limit: Some("0"),
            ..raw
        })
        .unwrap_err();

        assert_eq!(refusal.message(), "limit is not a number");
        assert_eq!(family.message(), "family is not a family name");
        assert_eq!(family.family(), Some("Chat"));
    }

    #[test]
    fn a_stream_follows_but_for_three_words() {
        let follow = |text: &str| {
            EventsQuery::try_from(RawEventsQuery {
                follow: Some(text),
                ..RawEventsQuery::default()
            })
            .map(|query| query.follow())
        };

        for text in ["0", "false", "no", "FALSE", " No\t", "\u{a0}0\u{1f}"] {
            assert_eq!(follow(text), Ok(Follow::ReplayOnly), "{text:?}");
        }
        for text in ["", "1", "true", "yes", "off", "f", "00", "\u{ff2e}\u{ff2f}"] {
            assert_eq!(follow(text), Ok(Follow::KeepOpen), "{text:?}");
        }
    }

    #[test]
    fn a_sequence_number_past_64_bits_is_refused() {
        let from = |text: &str| {
            EventsQuery::try_from(RawEventsQuery {
                from_seq: Some(text),
                ..RawEventsQuery::default()
            })
        };

        assert_eq!(
            from("18446744073709551615").map(|query| query.from_seq()),
            Ok(u64::MAX)
        );
        assert_eq!(
            from("18446744073709551616").unwrap_err().message(),
            "from_seq is outside 0 to 18446744073709551615"
        );
        assert_eq!(from("-1").unwrap_err().message(), "from_seq is negative");
    }
}
