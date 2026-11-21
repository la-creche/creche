//! The pure part of `check_daemon`: the uid of the user of the daemon in
//! the text of `/etc/passwd`, and the refusals.

use std::fmt;
use std::io;

/// The user of the daemon `ci-runner.service` (design section 10.5.1).
/// Only the daemon runs as this user.
pub const DAEMON_USER: &str = "ci-runner";

/// The file that holds the user of the daemon. The install requires a
/// system user made with `useradd` (design section 11.2), and `useradd`
/// writes this file.
#[cfg(target_os = "linux")]
pub(crate) const PASSWD: &str = "/etc/passwd";

/// Why the uid of the user of the daemon could not be read.
#[derive(Debug)]
pub enum UserFault {
    /// `/etc/passwd` could not be read.
    Read(io::Error),
    /// No line of `/etc/passwd` names the user.
    Missing,
    /// Two lines of `/etc/passwd` name the user.
    Twice,
    /// The line of the user has no uid of decimal digits that fits 32 bits.
    Uid,
}

impl fmt::Display for UserFault {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Read(error) => write!(f, "/etc/passwd could not be read: {error}"),
            Self::Missing => write!(f, "/etc/passwd has no user {DAEMON_USER}"),
            Self::Twice => write!(f, "/etc/passwd names the user {DAEMON_USER} twice"),
            Self::Uid => write!(f, "the line of the user {DAEMON_USER} has no valid uid"),
        }
    }
}

/// Why `check_daemon` refused a control connection. Each variant is one
/// step of the check, in its order.
#[derive(Debug)]
pub enum DaemonRefusal {
    /// Step 1: the uid of the user `ci-runner` could not be read.
    User(UserFault),
    /// Step 2: `SO_PEERCRED` of the connection failed.
    PeerCred(io::Error),
    /// Step 3: the peer of the connection runs as this other uid.
    OtherUid(u32),
}

impl fmt::Display for DaemonRefusal {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::User(fault) => write!(f, "the uid of the daemon user: {fault}"),
            Self::PeerCred(error) => write!(f, "SO_PEERCRED failed: {error}"),
            Self::OtherUid(uid) => write!(
                f,
                "the peer runs as uid {uid}, not as the user {DAEMON_USER}"
            ),
        }
    }
}

impl std::error::Error for DaemonRefusal {}

/// The uid of `DAEMON_USER` in the text `passwd` of `/etc/passwd`. A line
/// whose first field is another name is skipped, whatever its form.
///
/// # Errors
///
/// `Missing` with no line of the user, `Twice` with two, and `Uid` when
/// the third field of its line is not decimal digits that fit 32 bits.
#[cfg(any(target_os = "linux", test))]
pub(crate) fn daemon_uid(passwd: &str) -> Result<u32, UserFault> {
    let mut lines = passwd
        .lines()
        .filter(|line| line.split(':').next() == Some(DAEMON_USER));
    let line = lines.next().ok_or(UserFault::Missing)?;
    if lines.next().is_some() {
        return Err(UserFault::Twice);
    }
    let uid = line.split(':').nth(2).ok_or(UserFault::Uid)?;
    if uid.is_empty() || !uid.bytes().all(|b| b.is_ascii_digit()) {
        return Err(UserFault::Uid);
    }
    uid.parse().map_err(|_| UserFault::Uid)
}

#[cfg(test)]
mod tests {
    use super::{UserFault, daemon_uid};

    const PASSWD: &str = "root:x:0:0:root:/root:/bin/bash\n\
        ci-runner-update:x:997:996::/var/lib/ci-runner-update:/usr/sbin/nologin\n\
        ci-runner:x:998:997::/var/lib/ci-runner:/usr/sbin/nologin\n\
        nobody:x:65534:65534:nobody:/nonexistent:/usr/sbin/nologin\n";

    #[test]
    fn the_line_of_the_user_gives_its_uid() {
        assert_eq!(daemon_uid(PASSWD).unwrap(), 998);
    }

    #[test]
    fn a_name_that_only_starts_with_the_user_is_skipped() {
        let text =
            "ci-runner-vm0:x:61000:61000::/:/usr/sbin/nologin\nci-runnerx:x:5:5::/:/bin/sh\n";
        assert!(matches!(daemon_uid(text), Err(UserFault::Missing)));
        assert!(matches!(daemon_uid(""), Err(UserFault::Missing)));
    }

    #[test]
    fn two_lines_of_the_user_are_refused() {
        let text = format!("{PASSWD}ci-runner:x:0:0::/:/bin/sh\n");
        assert!(matches!(daemon_uid(&text), Err(UserFault::Twice)));
    }

    #[test]
    fn a_uid_that_is_not_digits_is_refused() {
        for uid in ["", "+998", "-1", "998 ", "abc", "4294967296"] {
            let text = format!("ci-runner:x:{uid}:997::/:/usr/sbin/nologin\n");
            assert!(matches!(daemon_uid(&text), Err(UserFault::Uid)), "{uid}");
        }
        assert!(matches!(daemon_uid("ci-runner:x"), Err(UserFault::Uid)));
        assert_eq!(
            daemon_uid("ci-runner:x:4294967295:1::/:/bin/sh").unwrap(),
            u32::MAX
        );
    }
}
