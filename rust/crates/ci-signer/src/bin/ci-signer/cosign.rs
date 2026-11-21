//! The cosign child of the signer: one `sign-blob` for each statement
//! (section 4.5 step 5, measured with cosign v3.1.4 on 2026-11-16, M7).
//!
//! ```text
//! COSIGN_PASSWORD='' cosign sign-blob --key <key> \
//!   --signing-config <work>/empty-signing-config.json \
//!   --bundle <work>/bundle-<run>.json -
//! ```
//!
//! 1. The statement goes on stdin (`-`, measured fact 6). No file holds it.
//! 2. The empty signing config keeps the sign off each network: with no
//!    signing config, sign fetches the one of the public Sigstore (fact 2),
//!    and without it a `--yes` sign posts to the public log (fact 3). The
//!    signer writes the file into its work folder at start, from the bytes
//!    of `EMPTY_SIGNING_CONFIG`, the output of `cosign signing-config
//!    create` with no flags.
//! 3. The child gets an empty environment, with `COSIGN_PASSWORD` empty
//!    (fact 5: without it sign asks for a password and exits 1) and `HOME`
//!    and `TUF_ROOT` on empty folders of the work folder (M7 fact 5 of
//!    section 5: both stay empty).
//! 4. The signer reads only the exit status. stdout and stderr go nowhere.
//!    The bundle file must then hold 1 to `MAX_BUNDLE_BYTES` bytes, and the
//!    signer removes it after the read.

use std::fmt;
use std::fs;
use std::io::{self, Read};
use std::os::unix::fs::{DirBuilderExt, OpenOptionsExt};
use std::path::{Path, PathBuf};
use std::process::Stdio;
use std::time::Duration;

use creche_ci_signer::issue::Signable;
use creche_ci_signer::limits::MAX_BUNDLE_BYTES;
use creche_ci_signer::message::Bundle;
use creche_ci_signer::names::RunId;
use tokio::io::AsyncWriteExt;
use tokio::process::Command;
use tokio::time::timeout;

/// The bytes of `empty-signing-config.json` (section 4.5 step 5).
pub const EMPTY_SIGNING_CONFIG: &str = r#"{"mediaType":"application/vnd.dev.sigstore.signingconfig.v0.2+json","rekorTlogConfig":{},"tsaConfig":{}}"#;

/// The file name of the signing config in the work folder.
const SIGNING_CONFIG: &str = "empty-signing-config.json";

/// The empty folder of `HOME` and of `TUF_ROOT` of the child.
const EMPTY_HOME: &str = "home";

/// The longest sign. A sign of the measured bundle took well under one
/// second on a workstation. A first value.
pub const SIGN_WAIT: Duration = Duration::from_secs(30);

/// The mode of each folder and file of the work folder.
const DIR_MODE: u32 = 0o700;
const FILE_MODE: u32 = 0o600;

/// Why cosign gave no bundle.
#[derive(Debug)]
pub enum CosignFault {
    /// The child did not start, or its stdin failed.
    Spawn(io::Error),
    /// The child did not end within `SIGN_WAIT`. The drop killed it.
    Timeout,
    /// The child ended with a status that is not 0. `None` for a signal.
    Status(Option<i32>),
    /// The bundle file could not be read.
    Read(io::Error),
    /// The bundle is empty or passes `MAX_BUNDLE_BYTES` (FR17).
    Bundle,
}

impl CosignFault {
    /// A stable word for the log.
    pub fn code(&self) -> &'static str {
        match self {
            Self::Spawn(_) => "spawn",
            Self::Timeout => "timeout",
            Self::Status(_) => "status",
            Self::Read(_) => "read",
            Self::Bundle => "bundle",
        }
    }
}

impl fmt::Display for CosignFault {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Status(Some(code)) => write!(formatter, "cosign exited with {code}"),
            Self::Status(None) => formatter.write_str("cosign ended by a signal"),
            Self::Spawn(error) => write!(formatter, "cosign did not run: {error}"),
            Self::Read(error) => write!(formatter, "the bundle file cannot be read: {error}"),
            other => write!(formatter, "cosign gave no bundle: {}", other.code()),
        }
    }
}

impl std::error::Error for CosignFault {}

/// The cosign program, the key and the work folder of the signer.
#[derive(Debug)]
pub struct Cosign {
    program: PathBuf,
    key: PathBuf,
    work: PathBuf,
}

/// Makes `path` a folder of the mode 0700, or keeps it when it is one.
fn folder(path: &Path) -> io::Result<()> {
    match fs::DirBuilder::new().mode(DIR_MODE).create(path) {
        Ok(()) => Ok(()),
        Err(error) if error.kind() == io::ErrorKind::AlreadyExists && path.is_dir() => Ok(()),
        Err(error) => Err(error),
    }
}

impl Cosign {
    /// The cosign of `program` with the key file `key`. It writes the
    /// signing config and makes the empty home in the folder `work`,
    /// which must exist.
    ///
    /// # Errors
    ///
    /// The error of a write in `work`.
    pub fn prepare(program: PathBuf, key: PathBuf, work: PathBuf) -> io::Result<Self> {
        folder(&work.join(EMPTY_HOME))?;
        let config = work.join(SIGNING_CONFIG);
        let _gone = fs::remove_file(&config);
        let mut file = fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(FILE_MODE)
            .open(&config)?;
        io::Write::write_all(&mut file, EMPTY_SIGNING_CONFIG.as_bytes())?;
        file.sync_all()?;
        Ok(Self { program, key, work })
    }

    /// The bundle of `signable`, the statement of `run`.
    ///
    /// # Errors
    ///
    /// Each `CosignFault`. The bundle file is gone after each end.
    pub async fn sign(&self, signable: &Signable, run: RunId) -> Result<Bundle, CosignFault> {
        let bundle = self.work.join(format!("bundle-{run}.json"));
        let _gone = fs::remove_file(&bundle);
        let signed = self.child(signable, &bundle).await;
        let read = signed.and_then(|()| read_bundle(&bundle));
        let _gone = fs::remove_file(&bundle);
        read
    }

    async fn child(&self, signable: &Signable, bundle: &Path) -> Result<(), CosignFault> {
        let home = self.work.join(EMPTY_HOME);
        let mut child = Command::new(&self.program)
            .arg("sign-blob")
            .arg("--key")
            .arg(&self.key)
            .arg("--signing-config")
            .arg(self.work.join(SIGNING_CONFIG))
            .arg("--bundle")
            .arg(bundle)
            .arg("-")
            .env_clear()
            .env("COSIGN_PASSWORD", "")
            .env("HOME", &home)
            .env("TUF_ROOT", &home)
            .current_dir(&self.work)
            .stdin(Stdio::piped())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .kill_on_drop(true)
            .spawn()
            .map_err(CosignFault::Spawn)?;

        let run = async {
            if let Some(mut stdin) = child.stdin.take() {
                stdin
                    .write_all(signable.bytes())
                    .await
                    .map_err(CosignFault::Spawn)?;
                stdin.shutdown().await.map_err(CosignFault::Spawn)?;
            }
            child.wait().await.map_err(CosignFault::Spawn)
        };
        let status = timeout(SIGN_WAIT, run)
            .await
            .map_err(|_| CosignFault::Timeout)??;
        if !status.success() {
            return Err(CosignFault::Status(status.code()));
        }
        Ok(())
    }
}

/// The bundle file: 1 to `MAX_BUNDLE_BYTES` bytes of a regular file. The
/// work folder has the mode 0700 of the user of the signer, so no other
/// user can put a link in the place of the file.
fn read_bundle(path: &Path) -> Result<Bundle, CosignFault> {
    let file = fs::File::open(path).map_err(CosignFault::Read)?;
    let regular = file.metadata().map_err(CosignFault::Read)?.is_file();
    if !regular {
        return Err(CosignFault::Bundle);
    }
    let cap = u64::try_from(MAX_BUNDLE_BYTES)
        .map_err(|_| CosignFault::Bundle)?
        .saturating_add(1);
    let mut bytes = Vec::new();
    file.take(cap)
        .read_to_end(&mut bytes)
        .map_err(CosignFault::Read)?;
    Bundle::new(bytes).map_err(|_| CosignFault::Bundle)
}

#[cfg(test)]
mod tests {
    use super::EMPTY_SIGNING_CONFIG;

    #[test]
    fn the_signing_config_is_the_measured_one() {
        // Section 4.5 step 5: the output of `cosign signing-config create
        // --out empty-signing-config.json` with no flags.
        let value: serde_json::Value = serde_json::from_str(EMPTY_SIGNING_CONFIG).unwrap();
        assert_eq!(
            value,
            serde_json::json!({
                "mediaType": "application/vnd.dev.sigstore.signingconfig.v0.2+json",
                "rekorTlogConfig": {},
                "tsaConfig": {}
            })
        );
    }
}
