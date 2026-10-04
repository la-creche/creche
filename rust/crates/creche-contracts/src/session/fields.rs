//! The small types that more than one request and more than one answer hold:
//! a text with a cap, a number in a range, and the closed sets of words.

use std::collections::BTreeMap;
use std::error::Error;
use std::fmt;
use std::str::FromStr;

use serde::{Deserialize, Serialize, Serializer};

use crate::ids::Ulid;

// --- a text with a cap ---

/// What the cap of a text counts.
#[derive(Clone, Copy)]
enum Cap {
    /// Characters. Python's `len` of a `str` counts them.
    Chars(usize),
    /// Bytes of UTF-8 (contract 03 §8).
    Bytes(usize),
}

/// Whether a text can be empty.
#[derive(Clone, Copy, PartialEq, Eq)]
enum Empty {
    Allowed,
    Refused,
}

/// The rule of one text field.
struct TextRule {
    /// What an error text calls a value, with its article: `a prompt`.
    noun: &'static str,
    cap: Cap,
    empty: Empty,
}

/// Which rule of a [`TextRule`] a text breaks.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum TextFault {
    Empty,
    TooLong,
}

fn check_text(text: &str, rule: &TextRule) -> Result<(), TextFault> {
    if text.is_empty() && rule.empty == Empty::Refused {
        return Err(TextFault::Empty);
    }

    let fits = match rule.cap {
        Cap::Chars(max) => text.chars().count() <= max,
        Cap::Bytes(max) => text.len() <= max,
    };

    if fits {
        Ok(())
    } else {
        Err(TextFault::TooLong)
    }
}

fn describe_text(fault: TextFault, rule: &TextRule, f: &mut fmt::Formatter<'_>) -> fmt::Result {
    let noun = rule.noun;
    match (fault, rule.cap) {
        (TextFault::Empty, _) => write!(f, "{noun} has 1 character or more"),
        (TextFault::TooLong, Cap::Chars(max)) => write!(f, "{noun} has {max} characters or less"),
        (TextFault::TooLong, Cap::Bytes(max)) => write!(f, "{noun} has {max} bytes or less"),
    }
}

/// Makes a type that holds one text, and its error type. The whole grammar is
/// one [`TextRule`].
macro_rules! capped_text {
    (
        $(#[$attribute:meta])*
        $name:ident,
        $(#[$error_attribute:meta])*
        $error:ident,
        $rule:ident
    ) => {
        $(#[$attribute])*
        #[derive(Debug, Clone, PartialEq, Eq, Hash, Deserialize)]
        #[serde(try_from = "String")]
        pub struct $name(String);

        impl $name {
            /// The text.
            #[must_use]
            pub fn as_str(&self) -> &str {
                &self.0
            }
        }

        impl FromStr for $name {
            type Err = $error;

            fn from_str(text: &str) -> Result<Self, Self::Err> {
                check_text(text, &$rule).map_err($error::from)?;

                Ok(Self(text.to_owned()))
            }
        }

        impl TryFrom<String> for $name {
            type Error = $error;

            fn try_from(text: String) -> Result<Self, Self::Error> {
                check_text(&text, &$rule).map_err($error::from)?;

                Ok(Self(text))
            }
        }

        impl fmt::Display for $name {
            fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
                f.write_str(&self.0)
            }
        }

        impl Serialize for $name {
            fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
                serializer.serialize_str(&self.0)
            }
        }

        $(#[$error_attribute])*
        ///
        /// No variant holds the text. The text is untrusted, and a caller writes
        /// this error to a log.
        #[derive(Debug, Clone, Copy, PartialEq, Eq)]
        pub enum $error {
            /// The text is empty, and the type permits no empty text.
            Empty,
            /// The text is over the cap of the type.
            TooLong,
        }

        impl From<TextFault> for $error {
            fn from(fault: TextFault) -> Self {
                match fault {
                    TextFault::Empty => Self::Empty,
                    TextFault::TooLong => Self::TooLong,
                }
            }
        }

        impl fmt::Display for $error {
            fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
                let fault = match self {
                    Self::Empty => TextFault::Empty,
                    Self::TooLong => TextFault::TooLong,
                };

                describe_text(fault, &$rule, f)
            }
        }

        impl Error for $error {}
    };
}

/// The cap of a title, in characters (contract 02 §4.2).
const TITLE_MAX_CHARS: usize = 200;

const TITLE: TextRule = TextRule {
    noun: "a title",
    cap: Cap::Chars(TITLE_MAX_CHARS),
    empty: Empty::Allowed,
};

capped_text! {
    /// The display title of a session: 200 characters at most, and it can be
    /// empty (contract 02 §4.2).
    ///
    /// ```
    /// use creche_contracts::session::Title;
    ///
    /// let title: Title = "Kitchen sensor debug".parse()?;
    /// assert_eq!(title.as_str(), "Kitchen sensor debug");
    /// # Ok::<(), creche_contracts::session::TitleError>(())
    /// ```
    ///
    /// Code outside this module cannot build a value from a raw string:
    ///
    /// ```compile_fail,E0423
    /// use creche_contracts::session::Title;
    ///
    /// let title = Title(String::from("Kitchen sensor debug"));
    /// ```
    Title,
    /// Why a text is not a title.
    TitleError,
    TITLE
}

/// The cap of a prompt, in bytes of UTF-8: 256 KiB (contract 02 §5.4).
const PROMPT_MAX_BYTES: usize = 262_144;

const PROMPT: TextRule = TextRule {
    noun: "a prompt",
    cap: Cap::Bytes(PROMPT_MAX_BYTES),
    empty: Empty::Refused,
};

capped_text! {
    /// The user text of one turn: 1 byte to 256 KiB of UTF-8 (contract 02 §5.4).
    ///
    /// ```
    /// use creche_contracts::session::Prompt;
    ///
    /// let prompt: Prompt = "Which sensor dropped out last night?".parse()?;
    /// assert_eq!(prompt.as_str().len(), 36);
    /// # Ok::<(), creche_contracts::session::PromptError>(())
    /// ```
    ///
    /// Code outside this module cannot build a value from a raw string:
    ///
    /// ```compile_fail,E0423
    /// use creche_contracts::session::Prompt;
    ///
    /// let prompt = Prompt(String::new());
    /// ```
    Prompt,
    /// Why a text is not a prompt.
    PromptError,
    PROMPT
}

impl Prompt {
    /// The cap of a prompt, in bytes of UTF-8.
    pub const MAX_BYTES: usize = PROMPT_MAX_BYTES;
}

/// The cap of the idempotency key of a turn, in characters (contract 02 §5.4).
const IDEMPOTENCY_KEY_MAX_CHARS: usize = 200;

const IDEMPOTENCY_KEY: TextRule = TextRule {
    noun: "an idempotency key",
    cap: Cap::Chars(IDEMPOTENCY_KEY_MAX_CHARS),
    empty: Empty::Refused,
};

capped_text! {
    /// The idempotency key of one turn: 1 to 200 characters (contract 02 §5.4,
    /// §6).
    ///
    /// ```
    /// use creche_contracts::session::IdempotencyKey;
    ///
    /// let key: IdempotencyKey = "b7c1e2d0-1f44-4c61-8a2b-9e0d3c5f7a11".parse()?;
    /// assert_eq!(key.as_str().len(), 36);
    /// # Ok::<(), creche_contracts::session::IdempotencyKeyError>(())
    /// ```
    ///
    /// Code outside this module cannot build a value from a raw string:
    ///
    /// ```compile_fail,E0423
    /// use creche_contracts::session::IdempotencyKey;
    ///
    /// let key = IdempotencyKey(String::new());
    /// ```
    IdempotencyKey,
    /// Why a text is not the idempotency key of a turn.
    IdempotencyKeyError,
    IDEMPOTENCY_KEY
}

/// The cap of the idempotency key of a dispatch, in characters (contract 02
/// §13.4.1, contract 04 §4.1).
const DISPATCH_KEY_MAX_CHARS: usize = 128;

const DISPATCH_KEY: TextRule = TextRule {
    noun: "the idempotency key of a dispatch",
    cap: Cap::Chars(DISPATCH_KEY_MAX_CHARS),
    empty: Empty::Refused,
};

capped_text! {
    /// The idempotency key of one dispatch: 1 to 128 characters (contract 02
    /// §13.4.1, §13.4.4).
    ///
    /// ```
    /// use creche_contracts::session::DispatchKey;
    ///
    /// let key: DispatchKey = "morning-triage-2026-10-07".parse()?;
    /// assert_eq!(key.as_str(), "morning-triage-2026-10-07");
    /// # Ok::<(), creche_contracts::session::DispatchKeyError>(())
    /// ```
    ///
    /// Code outside this module cannot build a value from a raw string:
    ///
    /// ```compile_fail,E0423
    /// use creche_contracts::session::DispatchKey;
    ///
    /// let key = DispatchKey(String::new());
    /// ```
    DispatchKey,
    /// Why a text is not the idempotency key of a dispatch.
    DispatchKeyError,
    DISPATCH_KEY
}

impl DispatchKey {
    /// The cap of the key, in characters.
    pub const MAX_CHARS: usize = DISPATCH_KEY_MAX_CHARS;
}

/// The cap of the message of a job, in bytes of UTF-8: 64 KiB (contract 02
/// §13.4.1, contract 04 §7.1).
const JOB_MESSAGE_MAX_BYTES: usize = 65_536;

const JOB_MESSAGE: TextRule = TextRule {
    noun: "the message of a job",
    cap: Cap::Bytes(JOB_MESSAGE_MAX_BYTES),
    empty: Empty::Refused,
};

capped_text! {
    /// The message that one family sends to a job of another family: 1 byte to
    /// 64 KiB of UTF-8 (contract 02 §13.4.1, contract 04 §7.1).
    ///
    /// The calling model wrote the text. It is data, and it is not an
    /// instruction to `attendance`.
    ///
    /// ```
    /// use creche_contracts::session::JobMessage;
    ///
    /// let message: JobMessage = "Triage the open issues.".parse()?;
    /// assert_eq!(message.as_str(), "Triage the open issues.");
    /// # Ok::<(), creche_contracts::session::JobMessageError>(())
    /// ```
    ///
    /// Code outside this module cannot build a value from a raw string:
    ///
    /// ```compile_fail,E0423
    /// use creche_contracts::session::JobMessage;
    ///
    /// let message = JobMessage(String::new());
    /// ```
    JobMessage,
    /// Why a text is not the message of a job.
    JobMessageError,
    JOB_MESSAGE
}

impl JobMessage {
    /// The cap of the message, in bytes of UTF-8.
    pub const MAX_BYTES: usize = JOB_MESSAGE_MAX_BYTES;
}

/// The cap of a steer message, in bytes of UTF-8.
// CONTRACT-QUESTION: contract 02 §5.6 gives a steer message no cap. The Python
// parser has the cap of 4096 bytes. This type takes it.
const STEER_MESSAGE_MAX_BYTES: usize = 4_096;

const STEER_MESSAGE: TextRule = TextRule {
    noun: "a steer message",
    cap: Cap::Bytes(STEER_MESSAGE_MAX_BYTES),
    empty: Empty::Refused,
};

capped_text! {
    /// The message that steers a running turn: 1 to 4096 bytes of UTF-8
    /// (contract 02 §5.6).
    ///
    /// ```
    /// use creche_contracts::session::SteerMessage;
    ///
    /// let message: SteerMessage = "Check the garage too.".parse()?;
    /// assert_eq!(message.as_str(), "Check the garage too.");
    /// # Ok::<(), creche_contracts::session::SteerMessageError>(())
    /// ```
    ///
    /// Code outside this module cannot build a value from a raw string:
    ///
    /// ```compile_fail,E0423
    /// use creche_contracts::session::SteerMessage;
    ///
    /// let message = SteerMessage(String::new());
    /// ```
    SteerMessage,
    /// Why a text is not a steer message.
    SteerMessageError,
    STEER_MESSAGE
}

impl SteerMessage {
    /// The cap of the message, in bytes of UTF-8.
    pub const MAX_BYTES: usize = STEER_MESSAGE_MAX_BYTES;
}

/// The cap of the reason of a stop, in characters.
// CONTRACT-QUESTION: contract 02 §5.7 gives the reason of a stop no cap and no
// grammar. The Python parser has the cap of 200 characters and takes each
// text. This type does the same.
const STOP_REASON_MAX_CHARS: usize = 200;

const STOP_REASON: TextRule = TextRule {
    noun: "the reason of a stop",
    cap: Cap::Chars(STOP_REASON_MAX_CHARS),
    empty: Empty::Refused,
};

capped_text! {
    /// Why a caller stops a turn: 1 to 200 characters of free text (contract 02
    /// §5.7).
    ///
    /// The text becomes the `message` of the `turn_aborted` line. The `reason`
    /// of that line is always `user_stopped`.
    ///
    /// ```
    /// use creche_contracts::session::StopReason;
    ///
    /// let reason: StopReason = "tui_takeover".parse()?;
    /// assert_eq!(reason.as_str(), "tui_takeover");
    /// assert_eq!(StopReason::user_stopped().as_str(), "user_stopped");
    /// # Ok::<(), creche_contracts::session::StopReasonError>(())
    /// ```
    ///
    /// Code outside this module cannot build a value from a raw string:
    ///
    /// ```compile_fail,E0423
    /// use creche_contracts::session::StopReason;
    ///
    /// let reason = StopReason(String::new());
    /// ```
    StopReason,
    /// Why a text is not the reason of a stop.
    StopReasonError,
    STOP_REASON
}

/// The reason of a stop whose body gives none.
const USER_STOPPED: &str = "user_stopped";

impl StopReason {
    /// The reason of a stop whose body gives none: `user_stopped`.
    #[must_use]
    pub fn user_stopped() -> Self {
        Self(USER_STOPPED.to_owned())
    }
}

// --- labels ---

/// The largest count of labels (contract 02 §5.4).
const LABELS_MAX: usize = 10;

/// The cap of the value of one label, in characters.
const LABEL_VALUE_MAX_CHARS: usize = 200;

/// The labels of a session or of a turn: 10 keys at most, and each value has
/// 200 characters at most (contract 02 §4.2, §5.4).
///
/// A key is free text. The keys are in sorted order.
///
/// ```
/// use creche_contracts::session::Labels;
///
/// let labels = Labels::try_from(vec![("door".to_owned(), "owui".to_owned())])?;
/// assert_eq!(labels.get("door"), Some("owui"));
/// # Ok::<(), creche_contracts::session::LabelsError>(())
/// ```
///
/// Code outside this module cannot build a value from a raw map:
///
/// ```compile_fail,E0423
/// use creche_contracts::session::Labels;
///
/// let labels = Labels(std::collections::BTreeMap::new());
/// ```
// CONTRACT-QUESTION: contract 02 §4.2 calls a label a small free string and
// gives a key no cap and no grammar. The Python parser takes each key, the
// empty key too. This type does the same. A cap on a key refuses a body that
// the Python code takes today.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(try_from = "BTreeMap<String, String>")]
pub struct Labels(BTreeMap<String, String>);

impl Labels {
    /// The largest count of labels.
    pub const KEYS_MAX: usize = LABELS_MAX;

    /// The cap of the value of one label, in characters.
    pub const VALUE_MAX_CHARS: usize = LABEL_VALUE_MAX_CHARS;

    /// No label.
    #[must_use]
    pub fn none() -> Self {
        Self(BTreeMap::new())
    }

    /// The value of one label.
    #[must_use]
    pub fn get(&self, key: &str) -> Option<&str> {
        self.0.get(key).map(String::as_str)
    }

    /// Each label, in the order of the keys.
    pub fn iter(&self) -> impl Iterator<Item = (&str, &str)> {
        self.0
            .iter()
            .map(|(key, value)| (key.as_str(), value.as_str()))
    }

    /// The count of labels.
    #[must_use]
    pub fn len(&self) -> usize {
        self.0.len()
    }

    /// Whether there is no label.
    #[must_use]
    pub fn is_empty(&self) -> bool {
        self.0.is_empty()
    }
}

impl TryFrom<BTreeMap<String, String>> for Labels {
    type Error = LabelsError;

    fn try_from(labels: BTreeMap<String, String>) -> Result<Self, Self::Error> {
        if labels.len() > LABELS_MAX {
            return Err(LabelsError::TooMany);
        }

        let long = labels
            .values()
            .any(|value| value.chars().count() > LABEL_VALUE_MAX_CHARS);
        if long {
            return Err(LabelsError::ValueTooLong);
        }

        Ok(Self(labels))
    }
}

impl TryFrom<Vec<(String, String)>> for Labels {
    type Error = LabelsError;

    /// The value of a key that occurs twice is the last one.
    fn try_from(labels: Vec<(String, String)>) -> Result<Self, Self::Error> {
        Self::try_from(labels.into_iter().collect::<BTreeMap<_, _>>())
    }
}

/// Why a map is not the labels of a session or of a turn.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum LabelsError {
    /// The map has more than 10 keys.
    TooMany,
    /// A value has more than 200 characters.
    ValueTooLong,
}

impl fmt::Display for LabelsError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::TooMany => write!(f, "labels have {LABELS_MAX} keys or less"),
            Self::ValueTooLong => {
                write!(
                    f,
                    "the value of a label has {LABEL_VALUE_MAX_CHARS} characters or less"
                )
            }
        }
    }
}

impl Error for LabelsError {}

// --- a number in a range ---

/// Makes a type that holds one whole number in a range, and its error type.
macro_rules! ranged_number {
    (
        $(#[$attribute:meta])*
        $name:ident($inner:ty),
        $(#[$error_attribute:meta])*
        $error:ident,
        $min:expr,
        $max:expr,
        $noun:literal
    ) => {
        $(#[$attribute])*
        #[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
        #[serde(try_from = "u64", into = "u64")]
        pub struct $name($inner);

        impl $name {
            /// The number.
            #[must_use]
            pub fn get(self) -> $inner {
                self.0
            }
        }

        impl TryFrom<i128> for $name {
            type Error = $error;

            fn try_from(number: i128) -> Result<Self, Self::Error> {
                let number = <$inner>::try_from(number).map_err(|_| $error)?;
                if !($min..=$max).contains(&number) {
                    return Err($error);
                }

                Ok(Self(number))
            }
        }

        impl TryFrom<u64> for $name {
            type Error = $error;

            fn try_from(number: u64) -> Result<Self, Self::Error> {
                Self::try_from(i128::from(number))
            }
        }

        impl From<$name> for u64 {
            fn from(number: $name) -> Self {
                Self::from(number.0)
            }
        }

        $(#[$error_attribute])*
        #[derive(Debug, Clone, Copy, PartialEq, Eq)]
        pub struct $error;

        impl fmt::Display for $error {
            fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
                write!(f, concat!($noun, " is {} to {}"), $min, $max)
            }
        }

        impl Error for $error {}
    };
}

ranged_number! {
    /// The limit of a turn or of a sandbox switch, in seconds: 1 to 3600
    /// (contract 02 §5.4, contract 05 §5.1).
    ///
    /// ```
    /// use creche_contracts::session::DeadlineS;
    ///
    /// let deadline = DeadlineS::try_from(600_u64)?;
    /// assert_eq!(deadline.get(), 600);
    /// assert_eq!(DeadlineS::TURN.get(), 3600);
    /// # Ok::<(), creche_contracts::session::DeadlineSError>(())
    /// ```
    ///
    /// Code outside this module cannot build a value from a raw number:
    ///
    /// ```compile_fail,E0423
    /// use creche_contracts::session::DeadlineS;
    ///
    /// let deadline = DeadlineS(0);
    /// ```
    DeadlineS(u32),
    /// Why a number is not the limit of a turn.
    DeadlineSError,
    1,
    3600,
    "a deadline in seconds"
}

impl DeadlineS {
    /// The limit of a turn whose body gives none (contract 02 §5.4).
    pub const TURN: Self = Self(3600);

    /// The limit of a sandbox switch whose body gives none (contract 05 §5.1).
    pub const SWITCH: Self = Self(300);
}

ranged_number! {
    /// The largest count of rows in one answer: 1 to 200 (contract 02 §5.2,
    /// §13.4.2).
    ///
    /// ```
    /// use creche_contracts::session::PageLimit;
    ///
    /// let limit = PageLimit::try_from(50_u64)?;
    /// assert_eq!(limit.get(), 50);
    /// # Ok::<(), creche_contracts::session::PageLimitError>(())
    /// ```
    ///
    /// Code outside this module cannot build a value from a raw number:
    ///
    /// ```compile_fail,E0423
    /// use creche_contracts::session::PageLimit;
    ///
    /// let limit = PageLimit(0);
    /// ```
    PageLimit(u16),
    /// Why a number is not the largest count of rows in one answer.
    PageLimitError,
    1,
    200,
    "a limit"
}

impl PageLimit {
    /// The limit of a session list whose query gives none (contract 02 §5.2).
    pub const SESSIONS: Self = Self(50);

    /// The limit of a job list whose body gives none (contract 02 §13.4.2).
    pub const JOBS: Self = Self(20);
}

ranged_number! {
    /// The count of turns that one session answer holds: 0 to 100 (contract 02
    /// §5.3).
    ///
    /// ```
    /// use creche_contracts::session::TurnsWanted;
    ///
    /// let turns = TurnsWanted::try_from(0_u64)?;
    /// assert_eq!(turns.get(), 0);
    /// assert_eq!(TurnsWanted::NEWEST.get(), 10);
    /// # Ok::<(), creche_contracts::session::TurnsWantedError>(())
    /// ```
    ///
    /// Code outside this module cannot build a value from a raw number:
    ///
    /// ```compile_fail,E0423
    /// use creche_contracts::session::TurnsWanted;
    ///
    /// let turns = TurnsWanted(101);
    /// ```
    TurnsWanted(u8),
    /// Why a number is not the count of turns of one session answer.
    TurnsWantedError,
    0,
    100,
    "a count of turns"
}

impl TurnsWanted {
    /// The count for a query that gives none (contract 02 §5.3).
    pub const NEWEST: Self = Self(10);
}

ranged_number! {
    /// The sequence number of one journal line: 1 or more (contract 02 §2,
    /// §8).
    ///
    /// The numbers of one session have no gap. A record that the stream makes
    /// itself has no such number.
    ///
    /// ```
    /// use creche_contracts::session::JournalSeq;
    ///
    /// let seq = JournalSeq::try_from(42_u64)?;
    /// assert_eq!(seq.get(), 42);
    /// # Ok::<(), creche_contracts::session::JournalSeqError>(())
    /// ```
    ///
    /// Code outside this module cannot build a value from a raw number:
    ///
    /// ```compile_fail,E0423
    /// use creche_contracts::session::JournalSeq;
    ///
    /// let seq = JournalSeq(0);
    /// ```
    JournalSeq(u64),
    /// Why a number is not the sequence number of a journal line.
    JournalSeqError,
    1,
    u64::MAX,
    "a journal sequence number"
}

ranged_number! {
    /// The place of a turn in the queue of its family: 1 or more (contract 02
    /// §8.1). The first turn in a queue has the place 1.
    ///
    /// ```
    /// use creche_contracts::session::QueueDepth;
    ///
    /// let depth = QueueDepth::try_from(3_u64)?;
    /// assert_eq!(depth.get(), 3);
    /// # Ok::<(), creche_contracts::session::QueueDepthError>(())
    /// ```
    ///
    /// Code outside this module cannot build a value from a raw number:
    ///
    /// ```compile_fail,E0423
    /// use creche_contracts::session::QueueDepth;
    ///
    /// let depth = QueueDepth(0);
    /// ```
    QueueDepth(u64),
    /// Why a number is not the place of a turn in a queue.
    QueueDepthError,
    1,
    u64::MAX,
    "the place of a turn in a queue"
}

// --- the text that names a turn ---

/// The text that names a turn, where the Python code checks no grammar: the
/// `turn` of a stored journal line and the `turn` of an events query (contract
/// 02 §5.5, §8).
///
/// The Python code keeps each text there. This type keeps a text that is not a
/// ULID apart, so code that needs a turn id gets one only from [`TurnRef::id`].
///
/// ```
/// use creche_contracts::session::TurnRef;
///
/// let turn = TurnRef::from("01JBQ7WZ0X4T9V6K2H8M3N5PQR".to_owned());
/// assert!(turn.id().is_some());
///
/// let other = TurnRef::from("not a turn".to_owned());
/// assert_eq!(other.id(), None);
/// assert_eq!(other.as_str(), "not a turn");
/// ```
///
/// Code outside this module cannot give a text that is not a ULID an id:
///
/// ```compile_fail,E0423
/// use creche_contracts::session::TurnRef;
///
/// let turn = TurnRef(String::from("not a turn"));
/// ```
// CONTRACT-QUESTION: contract 02 §8 says that `turn` is a ULID or null, and
// §5.5 says that the `turn` of a query is a ULID. The Python reader of a
// journal line and the events route take each text. This type takes each text
// too, and gives it no id. To refuse such a text drops a line from a
// replay that the Python code gives today.
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct TurnRef(Named);

/// What a [`TurnRef`] holds: the id of a turn, or a text that is not one.
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
enum Named {
    Id(Ulid),
    Other(String),
}

impl TurnRef {
    /// The text.
    #[must_use]
    pub fn as_str(&self) -> &str {
        match &self.0 {
            Named::Id(id) => id.as_str(),
            Named::Other(text) => text,
        }
    }

    /// The id of the turn. `None` for a text that is not a ULID: no turn has
    /// that text as its id.
    #[must_use]
    pub fn id(&self) -> Option<&Ulid> {
        match &self.0 {
            Named::Id(id) => Some(id),
            Named::Other(_) => None,
        }
    }
}

impl From<String> for TurnRef {
    fn from(text: String) -> Self {
        match Ulid::try_from(text.clone()) {
            Ok(id) => Self(Named::Id(id)),
            Err(_) => Self(Named::Other(text)),
        }
    }
}

impl From<Ulid> for TurnRef {
    fn from(id: Ulid) -> Self {
        Self(Named::Id(id))
    }
}

// --- the closed sets of words ---

/// Makes an enum of words, with the word of each variant.
macro_rules! words {
    (
        $(#[$attribute:meta])*
        $name:ident,
        $(#[$error_attribute:meta])*
        $error:ident,
        $noun:literal,
        { $($(#[$variant_attribute:meta])* $variant:ident => $word:literal,)+ }
    ) => {
        $(#[$attribute])*
        #[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
        #[serde(try_from = "String", into = "&'static str")]
        pub enum $name {
            $($(#[$variant_attribute])* $variant,)+
        }

        impl $name {
            /// Each value of the set.
            pub const ALL: &'static [Self] = &[$(Self::$variant,)+];

            /// The word on the wire.
            #[must_use]
            pub const fn as_str(self) -> &'static str {
                match self {
                    $(Self::$variant => $word,)+
                }
            }
        }

        impl FromStr for $name {
            type Err = $error;

            fn from_str(text: &str) -> Result<Self, Self::Err> {
                match text {
                    $($word => Ok(Self::$variant),)+
                    _ => Err($error),
                }
            }
        }

        impl TryFrom<String> for $name {
            type Error = $error;

            fn try_from(text: String) -> Result<Self, Self::Error> {
                text.parse()
            }
        }

        impl From<$name> for &'static str {
            fn from(value: $name) -> Self {
                value.as_str()
            }
        }

        impl fmt::Display for $name {
            fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
                f.write_str(self.as_str())
            }
        }

        $(#[$error_attribute])*
        ///
        /// The error does not hold the text. The text is untrusted, and a caller
        /// writes this error to a log.
        #[derive(Debug, Clone, Copy, PartialEq, Eq)]
        pub struct $error;

        impl fmt::Display for $error {
            fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
                f.write_str(concat!("the text is not ", $noun))
            }
        }

        impl Error for $error {}
    };
}

pub(super) use words;

words! {
    /// How a caller wants `run turn` to answer (contract 02 §5.4).
    ///
    /// The set is closed. `attendance` refuses another word with `bad_request`.
    Wait,
    /// Why a text is not a wait mode.
    WaitError,
    "a wait mode",
    {
        /// An event stream that ends after the terminal line of the turn.
        Stream => "stream",
        /// One body when the turn ends.
        Settled => "settled",
        /// One body at once, with the status 202.
        Accepted => "accepted",
    }
}

words! {
    /// Which door holds the writer lease (contract 02 §7.1).
    ///
    /// The set is closed. `attendance` refuses another word with
    /// `bad_request`. A door that reads a lease from a newer `attendance` can
    /// meet a new holder. It refuses the answer.
    // CONTRACT-QUESTION: contract 02 §7.1 lists four holders. §3.1 gives the
    // chaperone a second token, `door-dispatch`, and the Python code has a
    // fifth holder for it, `dispatch`. This type has the five of the Python
    // code. Without the fifth, a session of the dispatch door has no holder.
    Holder,
    /// Why a text is not the holder of a lease.
    HolderError,
    "the holder of a lease",
    {
        /// The Open WebUI door.
        Owui => "owui",
        /// The terminal door.
        Tui => "tui",
        /// The delegate door of the chaperone.
        Delegate => "delegate",
        /// The dispatch door of the chaperone.
        Dispatch => "dispatch",
        /// The trigger door.
        Trigger => "trigger",
    }
}

words! {
    /// Whether a caller takes the writer lease or keeps it (contract 02 §5.9,
    /// §7.4).
    ///
    /// The set is closed. `attendance` refuses another word with `bad_request`.
    Intent,
    /// Why a text is not the intent of a lease call.
    IntentError,
    "the intent of a lease call",
    {
        /// Take the lease. An acquire can take an idle lease of another door.
        Acquire => "acquire",
        /// Keep the lease. A renew takes nothing.
        Renew => "renew",
    }
}

words! {
    /// What a `writer_changed` line says occurred (contract 02 §7.3).
    ///
    /// The set is closed. A reader of the stream that gets another word refuses
    /// the typed form of the line.
    LeaseReason,
    /// Why a text is not the reason of a lease change.
    LeaseReasonError,
    "the reason of a lease change",
    {
        /// No lease was there, or the lease was not in use.
        Granted => "granted",
        /// The holder took its own lease again.
        Renewed => "renewed",
        /// The caller took an idle lease of another holder.
        TakenOver => "taken_over",
        /// The holder gave the lease back.
        Released => "released",
    }
}

words! {
    /// What fired an autonomous job (contract 02 §13.2).
    ///
    /// The set is closed. `attendance` refuses another word with `bad_request`,
    /// because the word goes to a durable record.
    TriggerKind,
    /// Why a text is not the kind of a trigger.
    TriggerKindError,
    "the kind of a trigger",
    {
        /// A timer of the family.
        Timer => "timer",
        /// A declared webhook of the family.
        Webhook => "webhook",
        /// The `enqueue` verb of another family.
        Dispatch => "dispatch",
    }
}

words! {
    /// How `caregiver` replaces the sandbox of a family (contract 05 §5.1).
    ///
    /// The set is closed. `attendance` refuses another word with `bad_request`.
    SwitchMode,
    /// Why a text is not the mode of a sandbox switch.
    SwitchModeError,
    "the mode of a sandbox switch",
    {
        /// The running turns end first.
        Drain => "drain",
        /// The running turns stop.
        Interrupt => "interrupt",
    }
}

words! {
    /// Whether an event stream stays open after the replay (contract 02 §5.5).
    ///
    /// The two words are the names of this type only. No wire holds them: the
    /// events route reads the parameter `follow` as [`EventsQuery`] says.
    ///
    /// [`EventsQuery`]: super::EventsQuery
    Follow,
    /// Why a text is not the follow mode of a stream.
    FollowError,
    "the follow mode of a stream",
    {
        /// The stream stays open. This is the default.
        KeepOpen => "keep_open",
        /// The stream ends after the replay.
        ReplayOnly => "replay_only",
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_cap_in_characters_counts_characters() {
        let two_bytes = "\u{e9}".repeat(TITLE_MAX_CHARS);
        let four_bytes = "\u{1f600}".repeat(TITLE_MAX_CHARS);

        assert!(two_bytes.parse::<Title>().is_ok());
        assert!(four_bytes.parse::<Title>().is_ok());
        assert_eq!(
            format!("{four_bytes}a").parse::<Title>(),
            Err(TitleError::TooLong)
        );
        assert_eq!("".parse::<Title>().map(|title| title.as_str().len()), Ok(0));
    }

    #[test]
    fn a_cap_in_bytes_counts_bytes() {
        let at_the_cap = "\u{e9}".repeat(STEER_MESSAGE_MAX_BYTES / 2);

        assert!(at_the_cap.parse::<SteerMessage>().is_ok());
        assert_eq!(
            format!("{at_the_cap}a").parse::<SteerMessage>(),
            Err(SteerMessageError::TooLong)
        );
        assert_eq!("".parse::<SteerMessage>(), Err(SteerMessageError::Empty));
        assert_eq!("".parse::<Prompt>(), Err(PromptError::Empty));
        assert!("a".repeat(PROMPT_MAX_BYTES).parse::<Prompt>().is_ok());
        assert_eq!(
            "a".repeat(PROMPT_MAX_BYTES + 1).parse::<Prompt>(),
            Err(PromptError::TooLong)
        );
    }

    #[test]
    fn a_capped_text_deserializes_through_its_check() {
        let key: IdempotencyKey = serde_json::from_str("\"k\"").unwrap();

        assert_eq!(key.as_str(), "k");
        assert_eq!(serde_json::to_string(&key).unwrap(), "\"k\"");
        assert!(serde_json::from_str::<IdempotencyKey>("\"\"").is_err());
        assert!(serde_json::from_str::<DispatchKey>(&format!("\"{}\"", "k".repeat(129))).is_err());
        assert!(serde_json::from_str::<JobMessage>("5").is_err());
    }

    #[test]
    fn the_error_of_a_text_says_which_rule_failed() {
        assert_eq!(
            PromptError::Empty.to_string(),
            "a prompt has 1 character or more"
        );
        assert_eq!(
            PromptError::TooLong.to_string(),
            "a prompt has 262144 bytes or less"
        );
        assert_eq!(
            TitleError::TooLong.to_string(),
            "a title has 200 characters or less"
        );
        assert_eq!(
            StopReasonError::TooLong.to_string(),
            "the reason of a stop has 200 characters or less"
        );
    }

    #[test]
    fn labels_have_ten_keys_and_short_values() {
        let many = |count: usize| -> Vec<(String, String)> {
            (0..count)
                .map(|number| (format!("key-{number}"), "v".to_owned()))
                .collect()
        };
        let twice = vec![
            ("door".to_owned(), "owui".to_owned()),
            ("door".to_owned(), "tui".to_owned()),
        ];
        let long = vec![("door".to_owned(), "v".repeat(LABEL_VALUE_MAX_CHARS + 1))];
        let wide = vec![(String::new(), "\u{e9}".repeat(LABEL_VALUE_MAX_CHARS))];

        assert_eq!(
            Labels::try_from(many(LABELS_MAX)).map(|labels| labels.len()),
            Ok(10)
        );
        assert_eq!(
            Labels::try_from(many(LABELS_MAX + 1)),
            Err(LabelsError::TooMany)
        );
        assert_eq!(Labels::try_from(twice).unwrap().get("door"), Some("tui"));
        assert_eq!(Labels::try_from(long), Err(LabelsError::ValueTooLong));
        assert_eq!(
            Labels::try_from(wide).unwrap().get(""),
            Some("\u{e9}".repeat(200).as_str())
        );
        assert!(Labels::none().is_empty());
        assert!(serde_json::from_str::<Labels>(r#"{"door": 5}"#).is_err());
    }

    #[test]
    fn a_number_outside_its_range_is_refused() {
        assert_eq!(DeadlineS::try_from(1_u64).map(DeadlineS::get), Ok(1));
        assert_eq!(DeadlineS::try_from(3600_u64).map(DeadlineS::get), Ok(3600));
        assert_eq!(DeadlineS::try_from(0_u64), Err(DeadlineSError));
        assert_eq!(DeadlineS::try_from(3601_u64), Err(DeadlineSError));
        assert_eq!(DeadlineS::try_from(-1_i128), Err(DeadlineSError));
        assert_eq!(DeadlineS::try_from(i128::MAX), Err(DeadlineSError));
        assert_eq!(PageLimit::try_from(200_u64).map(PageLimit::get), Ok(200));
        assert_eq!(PageLimit::try_from(201_u64), Err(PageLimitError));
        assert_eq!(
            TurnsWanted::try_from(100_u64).map(TurnsWanted::get),
            Ok(100)
        );
        assert_eq!(TurnsWanted::try_from(101_u64), Err(TurnsWantedError));
        assert_eq!(JournalSeq::try_from(0_u64), Err(JournalSeqError));
        assert_eq!(QueueDepth::try_from(0_u64), Err(QueueDepthError));
        assert_eq!(QueueDepth::try_from(1_u64).map(QueueDepth::get), Ok(1));
        assert!(serde_json::from_str::<QueueDepth>("0").is_err());
        assert_eq!(
            JournalSeq::try_from(u64::MAX).map(JournalSeq::get),
            Ok(u64::MAX)
        );
        assert!(serde_json::from_str::<DeadlineS>("0").is_err());
        assert_eq!(serde_json::to_string(&DeadlineS::SWITCH).unwrap(), "300");
        assert_eq!(
            DeadlineSError.to_string(),
            "a deadline in seconds is 1 to 3600"
        );
    }

    #[test]
    fn a_word_outside_its_set_is_refused() {
        assert_eq!("settled".parse::<Wait>(), Ok(Wait::Settled));
        assert_eq!("Settled".parse::<Wait>(), Err(WaitError));
        assert_eq!("stream ".parse::<Wait>(), Err(WaitError));
        assert_eq!("dispatch".parse::<Holder>(), Ok(Holder::Dispatch));
        assert_eq!("door-tui".parse::<Holder>(), Err(HolderError));
        assert_eq!(Holder::ALL.len(), 5);
        assert_eq!(
            serde_json::to_string(&LeaseReason::TakenOver).unwrap(),
            "\"taken_over\""
        );
        assert_eq!(
            serde_json::from_str::<Intent>("\"renew\"").unwrap(),
            Intent::Renew
        );
        assert!(serde_json::from_str::<Intent>("\"release\"").is_err());
        assert_eq!(WaitError.to_string(), "the text is not a wait mode");
        assert_eq!(Follow::KeepOpen.to_string(), "keep_open");
    }
}
