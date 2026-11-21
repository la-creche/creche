//! The credentials of the unit (section 4.3, items 3 and 4): the private
//! key and the two tokens. `LoadCredential=` of `creche-ci-signer.service`
//! copies each root-owned file of `/etc/creche/ci-signer/` into the
//! credentials folder of the service, which systemd names in
//! `CREDENTIALS_DIRECTORY`. Only the service reads that folder. The user
//! `ci-runner` cannot read it.
//!
//! | Credential | Source on the host | Use |
//! |---|---|---|
//! | `release-signing.key` | `/etc/creche/ci-signer/release-signing.key` | `cosign sign-blob --key` |
//! | `release-reader.token` | `/etc/creche/ci-signer/release-reader.token` | the two reads of section 4.4 |
//! | `ci-signer.token` | `/etc/creche/ci-signer/ci-signer.token` | `POST /tags` of section 4.5 step 7 |
//!
//! The signer reads each token once at start. It passes the key to cosign
//! by its path and never reads its bytes.

use std::fmt;
use std::fs::File;
use std::io::{self, Read};
use std::path::{Path, PathBuf};

use crate::forge::Token;

/// The name of the private key in the credentials folder.
pub const KEY: &str = "release-signing.key";

/// The name of the token of the bot `release-reader`.
pub const READER_TOKEN: &str = "release-reader.token";

/// The name of the token of the bot `ci-signer`.
pub const TAGGER_TOKEN: &str = "ci-signer.token";

/// The variable in which systemd names the credentials folder.
pub const CREDENTIALS_DIRECTORY: &str = "CREDENTIALS_DIRECTORY";

/// The most bytes of a token file: 40 digits and one LF.
const TOKEN_MAX_BYTES: u64 = 41;

/// The most bytes of the key file. The measured key is a PEM of about 650
/// bytes.
const KEY_MAX_BYTES: u64 = 4096;

/// Why a credential is refused. It names the credential, never a byte.
#[derive(Debug)]
pub enum CredentialFault {
    /// The file could not be opened or read.
    Io(&'static str, io::Error),
    /// The file is no regular file, is empty or passes its cap.
    Shape(&'static str),
}

impl fmt::Display for CredentialFault {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Io(name, error) => write!(formatter, "the credential {name}: {error}"),
            Self::Shape(name) => write!(formatter, "the credential {name} breaks its form"),
        }
    }
}

impl std::error::Error for CredentialFault {}

/// The key path and the two tokens.
#[derive(Debug)]
pub struct Credentials {
    /// The path of the private key, for cosign.
    pub key: PathBuf,
    /// The token of `release-reader`.
    pub reader: Token,
    /// The token of the bot `ci-signer`.
    pub tagger: Token,
}

/// The open file `name` of `folder`, a regular file of at most `cap` bytes,
/// read in full.
fn read(folder: &Path, name: &'static str, cap: u64) -> Result<Vec<u8>, CredentialFault> {
    let file = File::open(folder.join(name)).map_err(|error| CredentialFault::Io(name, error))?;
    let meta = file
        .metadata()
        .map_err(|error| CredentialFault::Io(name, error))?;
    if !meta.is_file() || meta.len() == 0 || meta.len() > cap {
        return Err(CredentialFault::Shape(name));
    }
    let mut bytes = Vec::new();
    file.take(cap.saturating_add(1))
        .read_to_end(&mut bytes)
        .map_err(|error| CredentialFault::Io(name, error))?;
    Ok(bytes)
}

impl Credentials {
    /// The credentials of the folder `folder`.
    ///
    /// # Errors
    ///
    /// Each `CredentialFault`. A missing or malformed credential refuses
    /// the start.
    pub fn read(folder: &Path) -> Result<Self, CredentialFault> {
        read(folder, KEY, KEY_MAX_BYTES)?;
        let reader = Token::parse(&read(folder, READER_TOKEN, TOKEN_MAX_BYTES)?)
            .map_err(|_| CredentialFault::Shape(READER_TOKEN))?;
        let tagger = Token::parse(&read(folder, TAGGER_TOKEN, TOKEN_MAX_BYTES)?)
            .map_err(|_| CredentialFault::Shape(TAGGER_TOKEN))?;
        Ok(Self {
            key: folder.join(KEY),
            reader,
            tagger,
        })
    }
}
