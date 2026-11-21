//! `ci-signer verify --json --config <path>`: the verify hook of the
//! component (contract 06 §4). Root runs it after each release, before the
//! service restarts. It checks what the operator makes by hand for the
//! signer, so a release on a host with no user, no key or no token fails
//! closed (the operator steps of section 7 of the note):
//!
//! 1. `config`: the host config is a regular file of root that no group
//!    or other user may write, and it parses.
//! 2. `cosign`: the cosign program of the config is a regular, executable
//!    file of root, and root owns each folder above it. No group or other
//!    user may write one of them.
//! 3. `user`: `/etc/passwd` names the user `ci-signer` once, and
//!    `/etc/group` names the group `ci-runner` once.
//! 4. `key`, `reader-token`, `tagger-token`: each source file of a
//!    credential of the unit is a regular file of root with no bit for
//!    group or other. The key is 1 to 4096 bytes, and each token has its
//!    form.
//!
//! It opens no socket, starts no program and writes no file. stdout gets
//! one JSON line. `main.rs` exits 0 when each check passes, else 1. A
//! detail names a path or a fault, never a byte of a file.

use std::fmt;
use std::fs::{self, File, Metadata};
use std::io::{self, Read};
use std::os::unix::fs::{MetadataExt, PermissionsExt};
use std::path::Path;

use serde::Serialize;

use crate::config::{CONFIG_MAX_BYTES, Config};
use crate::forge::Token;

/// The source of the credential `release-signing.key` (section 4.2 step 2).
pub const KEY_SOURCE: &str = "/etc/creche/ci-signer/release-signing.key";

/// The source of the credential `release-reader.token`.
pub const READER_SOURCE: &str = "/etc/creche/ci-signer/release-reader.token";

/// The source of the credential `ci-signer.token`.
pub const TAGGER_SOURCE: &str = "/etc/creche/ci-signer/ci-signer.token";

/// The user of the service (section 4.3 item 2).
pub const SIGNER_USER: &str = "ci-signer";

/// The group of the socket unit (the runner design, section 6.4).
pub const RUNNER_GROUP: &str = "ci-runner";

/// The uid of root.
const ROOT_UID: u32 = 0;

/// The mode bits that let a user other than the owner write a file.
const GROUP_OR_OTHER_WRITE: u32 = 0o022;

/// The mode bits of group and other.
const GROUP_OR_OTHER: u32 = 0o077;

/// The mode bits of a program that its owner may run.
const OWNER_EXEC: u32 = 0o100;

/// The most bytes of `/etc/passwd` and of `/etc/group` that the hook reads.
const ACCOUNTS_MAX_BYTES: u64 = 1024 * 1024;

/// The most bytes of the key file and of a token file.
const KEY_MAX_BYTES: u64 = 4096;
const TOKEN_MAX_BYTES: u64 = 41;

/// The one JSON object of stdout.
#[derive(Debug, Serialize)]
pub struct Report {
    ok: bool,
    checks: Vec<Check>,
}

impl Report {
    /// Whether each check passed.
    pub fn passed(&self) -> bool {
        self.ok
    }
}

/// One check of the hook.
#[derive(Debug, Serialize)]
pub struct Check {
    name: &'static str,
    ok: bool,
    detail: String,
}

/// Why a file is not trusted.
#[derive(Debug)]
enum FileFault {
    Io(io::Error),
    NotRegular,
    NotOwned(u32),
    Mode(u32),
    Size,
    Form,
}

impl fmt::Display for FileFault {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Io(error) => write!(formatter, "cannot be read: {error}"),
            Self::NotRegular => formatter.write_str("is no regular file"),
            Self::NotOwned(uid) => write!(formatter, "is owned by uid {uid}, not by root"),
            Self::Mode(mode) => write!(formatter, "has the mode {mode:04o}"),
            Self::Size => formatter.write_str("is empty or too large"),
            Self::Form => formatter.write_str("breaks its form"),
        }
    }
}

/// The open file at `path`, a regular file of root, and its bytes up to
/// `cap`. The path must not be a link.
fn root_file(path: &Path, cap: u64) -> Result<(Metadata, Vec<u8>), FileFault> {
    let look = fs::symlink_metadata(path).map_err(FileFault::Io)?;
    if !look.file_type().is_file() {
        return Err(FileFault::NotRegular);
    }
    let file = File::open(path).map_err(FileFault::Io)?;
    let meta = file.metadata().map_err(FileFault::Io)?;
    if meta.dev() != look.dev() || meta.ino() != look.ino() || !meta.is_file() {
        return Err(FileFault::NotRegular);
    }
    if meta.uid() != ROOT_UID {
        return Err(FileFault::NotOwned(meta.uid()));
    }
    let mut bytes = Vec::new();
    file.take(cap.saturating_add(1))
        .read_to_end(&mut bytes)
        .map_err(FileFault::Io)?;
    Ok((meta, bytes))
}

/// The mode bits of `meta`.
fn mode(meta: &Metadata) -> u32 {
    meta.permissions().mode() & 0o7777
}

fn check_config(path: &Path) -> Result<Config, String> {
    let cap = u64::try_from(CONFIG_MAX_BYTES).unwrap_or(u64::MAX);
    let (meta, bytes) =
        root_file(path, cap).map_err(|fault| format!("{}: {fault}", path.display()))?;
    if mode(&meta) & GROUP_OR_OTHER_WRITE != 0 {
        return Err(format!(
            "{}: {}",
            path.display(),
            FileFault::Mode(mode(&meta))
        ));
    }
    Config::parse(&bytes).map_err(|fault| format!("{}: {fault}", path.display()))
}

fn check_cosign(program: &Path) -> Result<(), String> {
    let (meta, _) =
        root_file(program, 0).map_err(|fault| format!("{}: {fault}", program.display()))?;
    let bits = mode(&meta);
    if bits & GROUP_OR_OTHER_WRITE != 0 || bits & OWNER_EXEC == 0 {
        return Err(format!("{}: {}", program.display(), FileFault::Mode(bits)));
    }
    for folder in program.ancestors().skip(1) {
        let look = fs::symlink_metadata(folder)
            .map_err(|error| format!("{}: {error}", folder.display()))?;
        if !look.file_type().is_dir() {
            return Err(format!("{}: is no folder", folder.display()));
        }
        if look.uid() != ROOT_UID || mode(&look) & GROUP_OR_OTHER_WRITE != 0 {
            return Err(format!(
                "{}: is not a folder that only root writes",
                folder.display()
            ));
        }
    }
    Ok(())
}

/// Whether the text of `/etc/passwd` or `/etc/group` names `name` once.
fn names_once(text: &str, name: &str) -> bool {
    text.lines()
        .filter(|line| line.split(':').next() == Some(name))
        .count()
        == 1
}

fn check_accounts() -> Result<(), String> {
    for (file, name) in [("/etc/passwd", SIGNER_USER), ("/etc/group", RUNNER_GROUP)] {
        let (_, bytes) = root_file(Path::new(file), ACCOUNTS_MAX_BYTES)
            .map_err(|fault| format!("{file}: {fault}"))?;
        let text = String::from_utf8_lossy(&bytes);
        if !names_once(&text, name) {
            return Err(format!("{file} does not name {name} once"));
        }
    }
    Ok(())
}

/// A credential source: a regular file of root, no bit for group or
/// other, and `form` holds for its bytes.
fn check_secret(path: &str, cap: u64, form: fn(&[u8]) -> bool) -> Result<(), String> {
    let (meta, bytes) =
        root_file(Path::new(path), cap).map_err(|fault| format!("{path}: {fault}"))?;
    if mode(&meta) & GROUP_OR_OTHER != 0 {
        return Err(format!("{path}: {}", FileFault::Mode(mode(&meta))));
    }
    let size = u64::try_from(bytes.len()).unwrap_or(u64::MAX);
    if bytes.is_empty() || size > cap {
        return Err(format!("{path}: {}", FileFault::Size));
    }
    if !form(&bytes) {
        return Err(format!("{path}: {}", FileFault::Form));
    }
    Ok(())
}

fn token_form(bytes: &[u8]) -> bool {
    Token::parse(bytes).is_ok()
}

fn any_form(_bytes: &[u8]) -> bool {
    true
}

fn one(name: &'static str, result: Result<(), String>) -> Check {
    match result {
        Ok(()) => Check {
            name,
            ok: true,
            detail: "passed".to_owned(),
        },
        Err(detail) => Check {
            name,
            ok: false,
            detail,
        },
    }
}

/// The report of each check, with the host config at `config`.
pub fn run(config: &Path) -> Report {
    let mut checks = Vec::new();
    match check_config(config) {
        Ok(found) => {
            checks.push(one("config", Ok(())));
            checks.push(one("cosign", check_cosign(&found.cosign)));
        }
        Err(detail) => {
            checks.push(one("config", Err(detail)));
            checks.push(one("cosign", Err("the config does not name it".to_owned())));
        }
    }
    checks.push(one("user", check_accounts()));
    checks.push(one(
        "key",
        check_secret(KEY_SOURCE, KEY_MAX_BYTES, any_form),
    ));
    checks.push(one(
        "reader-token",
        check_secret(READER_SOURCE, TOKEN_MAX_BYTES, token_form),
    ));
    checks.push(one(
        "tagger-token",
        check_secret(TAGGER_SOURCE, TOKEN_MAX_BYTES, token_form),
    ));
    let ok = checks.iter().all(|check| check.ok);
    Report { ok, checks }
}

#[cfg(test)]
mod tests {
    use std::path::Path;

    use super::{names_once, run};

    #[test]
    fn an_account_is_named_once() {
        let passwd = "root:x:0:0::/root:/bin/sh\nci-signer:x:990:990::/:/usr/sbin/nologin\n";
        assert!(names_once(passwd, "ci-signer"));
        assert!(!names_once(passwd, "ci-runner"));
        assert!(!names_once("ci-signerx:x:1:1::/:/bin/sh\n", "ci-signer"));
        let twice = format!("{passwd}ci-signer:x:991:991::/:/bin/sh\n");
        assert!(!names_once(&twice, "ci-signer"));
    }

    /// The test host has no signer: each check of a missing file fails,
    /// and the report is a refusal that names no byte of a file.
    #[test]
    fn a_host_with_no_signer_fails_each_check() {
        let report = run(Path::new("/nonexistent/ci-signer/signer.conf"));
        assert!(!report.passed());
        let line = serde_json::to_string(&report).unwrap();
        for name in ["config", "cosign", "key", "reader-token", "tagger-token"] {
            assert!(
                line.contains(&format!("\"name\":\"{name}\",\"ok\":false")),
                "{line}"
            );
        }
    }

    /// A config that the test user owns is no config of root.
    #[test]
    fn a_config_of_another_user_is_refused() {
        use std::os::unix::fs::MetadataExt;

        let folder = std::env::temp_dir().join(format!("ci-signer-verify-{}", std::process::id()));
        std::fs::create_dir_all(&folder).unwrap();
        let path = folder.join("signer.conf");
        std::fs::write(
            &path,
            "owner=example-owner\nforgejo=http://192.0.2.10:3000\ncosign=/usr/bin/cosign\n",
        )
        .unwrap();
        let owner = std::fs::metadata(&path).unwrap().uid();
        let report = run(&path);
        std::fs::remove_dir_all(&folder).unwrap();
        let line = serde_json::to_string(&report).unwrap();
        if owner == 0 {
            println!("verify of a config of another user: not run: the test runs as root");
            return;
        }
        assert!(line.contains("not by root"), "{line}");
    }
}
