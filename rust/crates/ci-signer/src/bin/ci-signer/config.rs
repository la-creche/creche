//! The host config of the signer: `/etc/creche/ci-signer/signer.conf`.
//!
//! ```text
//! owner=<the owner of the repositories on Forgejo>
//! forgejo=http://<IP address>:<port>
//! cosign=<the absolute path of the cosign program>
//! ```
//!
//! One `key=value` line for each key, each line with its LF, in any order.
//! The file holds no blank line, no comment, no space around `=` and no
//! other key. Each key is required once. The file holds no secret: the
//! tokens and the key come from the credentials of the unit (section 4.3,
//! items 3 and 4). The parse names the line and the key of a fault, never
//! a value.

use std::fmt;
use std::path::{Component, Path, PathBuf};

use creche_ci_signer::names::Owner;

use crate::forge::ForgejoOrigin;

/// The most bytes of the file.
pub const CONFIG_MAX_BYTES: usize = 4096;

/// The most bytes of the path of the cosign program.
const PROGRAM_MAX_BYTES: usize = 255;

/// The keys of the file, in the order of the module docs.
const KEYS: [Key; 3] = [Key::Owner, Key::Forgejo, Key::Cosign];

/// One key of the file.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Key {
    /// `owner`: the owner of each repository of `signing-pairs.tsv`
    /// (section 4.3 item 6, FR32). A rehearsal names a shadow owner.
    Owner,
    /// `forgejo`: the origin of the Forgejo API.
    Forgejo,
    /// `cosign`: the cosign program.
    Cosign,
}

impl Key {
    /// The word of the key in the file.
    pub fn word(self) -> &'static str {
        match self {
            Self::Owner => "owner",
            Self::Forgejo => "forgejo",
            Self::Cosign => "cosign",
        }
    }

    fn from_word(word: &str) -> Option<Self> {
        KEYS.into_iter().find(|key| key.word() == word)
    }
}

/// Why the file is refused. A fault names a line or a key, never a value.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ConfigFault {
    /// The file passes `CONFIG_MAX_BYTES`.
    TooLarge,
    /// The file is not UTF-8.
    NotUtf8,
    /// The file is empty or does not end with one LF.
    NoFinalLf,
    /// The line is empty.
    Blank(usize),
    /// The line holds a CR.
    CarriageReturn(usize),
    /// The line has no `=`.
    NoEquals(usize),
    /// The line names no key of the file.
    UnknownKey(usize),
    /// A second line names this key.
    Repeated(Key),
    /// No line names this key.
    Missing(Key),
    /// The value of this key breaks its rule.
    Value(Key),
}

impl fmt::Display for ConfigFault {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::TooLarge => write!(formatter, "the file passes {CONFIG_MAX_BYTES} bytes"),
            Self::NotUtf8 => formatter.write_str("the file is not UTF-8"),
            Self::NoFinalLf => formatter.write_str("the file is empty or has no final LF"),
            Self::Blank(line) => write!(formatter, "line {line} is empty"),
            Self::CarriageReturn(line) => write!(formatter, "line {line} holds a CR"),
            Self::NoEquals(line) => write!(formatter, "line {line} has no ="),
            Self::UnknownKey(line) => write!(formatter, "line {line} names no key of the file"),
            Self::Repeated(key) => write!(formatter, "the key {} is in two lines", key.word()),
            Self::Missing(key) => write!(formatter, "the key {} is missing", key.word()),
            Self::Value(key) => write!(formatter, "the value of {} breaks its rule", key.word()),
        }
    }
}

impl std::error::Error for ConfigFault {}

/// The host config.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Config {
    /// The owner of the repositories of the signing pairs.
    pub owner: Owner,
    /// The origin of the Forgejo API.
    pub forgejo: ForgejoOrigin,
    /// The cosign program: an absolute path with no `.` or `..` part.
    pub cosign: PathBuf,
}

/// The path of the cosign program: absolute, normal, at most 255 bytes.
fn program(text: &str) -> Option<PathBuf> {
    let path = Path::new(text);
    let normal = path.components().enumerate().all(|(at, part)| match part {
        Component::RootDir => at == 0,
        Component::Normal(_) => true,
        _ => false,
    });
    let canonical = !text.ends_with('/') && !text.contains("//");
    let fits = text.len() <= PROGRAM_MAX_BYTES && !text.contains('\0');
    (path.is_absolute() && normal && canonical && fits).then(|| path.to_path_buf())
}

impl Config {
    /// The config of the bytes `bytes` of the file.
    ///
    /// # Errors
    ///
    /// The first `ConfigFault`. The lines count from 1.
    pub fn parse(bytes: &[u8]) -> Result<Self, ConfigFault> {
        if bytes.len() > CONFIG_MAX_BYTES {
            return Err(ConfigFault::TooLarge);
        }
        let text = std::str::from_utf8(bytes).map_err(|_| ConfigFault::NotUtf8)?;
        let body = text.strip_suffix('\n').ok_or(ConfigFault::NoFinalLf)?;

        let mut values: [Option<&str>; 3] = [None, None, None];
        for (index, line) in body.split('\n').enumerate() {
            let number = index + 1;
            if line.is_empty() {
                return Err(ConfigFault::Blank(number));
            }
            if line.contains('\r') {
                return Err(ConfigFault::CarriageReturn(number));
            }
            let (word, value) = line.split_once('=').ok_or(ConfigFault::NoEquals(number))?;
            let key = Key::from_word(word).ok_or(ConfigFault::UnknownKey(number))?;
            let slot = KEYS
                .iter()
                .position(|one| *one == key)
                .and_then(|at| values.get_mut(at))
                .ok_or(ConfigFault::UnknownKey(number))?;
            if slot.is_some() {
                return Err(ConfigFault::Repeated(key));
            }
            *slot = Some(value);
        }

        let value = |key: Key| -> Result<&str, ConfigFault> {
            KEYS.iter()
                .position(|one| *one == key)
                .and_then(|at| values.get(at).copied().flatten())
                .ok_or(ConfigFault::Missing(key))
        };
        let owner = value(Key::Owner)?;
        let forgejo = value(Key::Forgejo)?;
        let cosign = value(Key::Cosign)?;
        Ok(Self {
            owner: owner.parse().map_err(|_| ConfigFault::Value(Key::Owner))?,
            forgejo: forgejo
                .parse()
                .map_err(|_| ConfigFault::Value(Key::Forgejo))?,
            cosign: program(cosign).ok_or(ConfigFault::Value(Key::Cosign))?,
        })
    }
}

#[cfg(test)]
mod tests {
    use super::{Config, ConfigFault, Key};

    const GOOD: &str =
        "owner=example-owner\nforgejo=http://192.0.2.10:3000\ncosign=/usr/bin/cosign\n";

    #[test]
    fn the_three_keys_give_the_config() {
        let config = Config::parse(GOOD.as_bytes()).unwrap();
        assert_eq!(config.owner.as_str(), "example-owner");
        assert_eq!(config.forgejo.to_string(), "http://192.0.2.10:3000");
        assert_eq!(config.cosign.to_str(), Some("/usr/bin/cosign"));

        // The order of the lines does not count.
        let turned =
            "cosign=/usr/bin/cosign\nowner=example-owner\nforgejo=http://192.0.2.10:3000\n";
        assert_eq!(Config::parse(turned.as_bytes()).unwrap(), config);
    }

    #[test]
    fn each_malformed_file_is_refused_with_its_fault() {
        let cases: Vec<(String, ConfigFault)> = vec![
            (String::new(), ConfigFault::NoFinalLf),
            (GOOD.trim_end().to_owned(), ConfigFault::NoFinalLf),
            (format!("{GOOD}\n"), ConfigFault::Blank(4)),
            (format!("\n{GOOD}"), ConfigFault::Blank(1)),
            (
                GOOD.replace("owner=", "owner ="),
                ConfigFault::UnknownKey(1),
            ),
            (GOOD.replace("owner=", "# owner"), ConfigFault::NoEquals(1)),
            (GOOD.replace('\n', "\r\n"), ConfigFault::CarriageReturn(1)),
            (format!("{GOOD}extra=1\n"), ConfigFault::UnknownKey(4)),
            (
                format!("{GOOD}owner=other\n"),
                ConfigFault::Repeated(Key::Owner),
            ),
            (
                GOOD.replace("cosign=/usr/bin/cosign\n", ""),
                ConfigFault::Missing(Key::Cosign),
            ),
            (
                GOOD.replace("example-owner", "a/b"),
                ConfigFault::Value(Key::Owner),
            ),
            (
                GOOD.replace("example-owner", ""),
                ConfigFault::Value(Key::Owner),
            ),
            (
                GOOD.replace("http://192.0.2.10:3000", "https://192.0.2.10:3000"),
                ConfigFault::Value(Key::Forgejo),
            ),
            (
                GOOD.replace("http://192.0.2.10:3000", "http://forgejo.invalid:3000"),
                ConfigFault::Value(Key::Forgejo),
            ),
            (
                GOOD.replace("http://192.0.2.10:3000", "http://192.0.2.10:3000/"),
                ConfigFault::Value(Key::Forgejo),
            ),
            (
                GOOD.replace("/usr/bin/cosign", "cosign"),
                ConfigFault::Value(Key::Cosign),
            ),
            (
                GOOD.replace("/usr/bin/cosign", "/usr/bin/../bin/cosign"),
                ConfigFault::Value(Key::Cosign),
            ),
            (
                GOOD.replace("/usr/bin/cosign", "/usr//bin/cosign"),
                ConfigFault::Value(Key::Cosign),
            ),
            (
                GOOD.replace("/usr/bin/cosign", "/usr/bin/"),
                ConfigFault::Value(Key::Cosign),
            ),
            (
                GOOD.replace("/usr/bin/cosign", " /usr/bin/cosign"),
                ConfigFault::Value(Key::Cosign),
            ),
        ];
        for (text, fault) in cases {
            assert_eq!(Config::parse(text.as_bytes()), Err(fault), "{text:?}");
        }
        assert_eq!(
            Config::parse(&[b'o'; super::CONFIG_MAX_BYTES + 1]),
            Err(ConfigFault::TooLarge)
        );
        assert_eq!(Config::parse(b"owner=\xff\n"), Err(ConfigFault::NotUtf8));
    }

    #[test]
    fn a_fault_names_no_value() {
        let text = GOOD.replace("example-owner", "a/secret-looking-value");
        let fault = Config::parse(text.as_bytes()).unwrap_err();
        assert!(!fault.to_string().contains("secret"), "{fault}");
    }
}
