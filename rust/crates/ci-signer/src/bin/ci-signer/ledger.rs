//! The one ledger of the issued runs: the `IssuedRuns` of the core and its
//! journal file (section 4.3 item 7, FR21, FR22).
//!
//! The program holds one `Ledger`, made once at start from the journal
//! (rule 3 of the crate `AGENTS.md`). `issue` records the run and the
//! commit, then appends the line and syncs the file, and only then gives
//! the bytes to sign. A failed write keeps the run and the commit issued in
//! memory, so the signer signs nothing for them until a restart, and a
//! restart reads the journal again (fail closed).

use std::fmt;
use std::fs::{File, OpenOptions};
use std::io::{self, Read, Write};
use std::os::unix::fs::OpenOptionsExt;
use std::path::Path;

use creche_ci_signer::eligibility::Eligible;
use creche_ci_signer::exchange::Request;
use creche_ci_signer::issue::{AlreadyIssued, IssuedRuns, JournalError, Signable};
use creche_ci_signer::signed_at::SignedAt;

/// The journal of the issued runs (FR21). The unit makes its folder with
/// `StateDirectory=ci-signer`, owned by the user `ci-signer`.
pub const JOURNAL_PATH: &str = "/var/lib/ci-signer/issued.tsv";

/// The most bytes of the journal. One line is 123 bytes at most, so the
/// cap holds more than 130000 statements. A larger journal refuses the
/// start.
pub const JOURNAL_MAX_BYTES: u64 = 16 * 1024 * 1024;

/// The mode of a new journal.
const JOURNAL_MODE: u32 = 0o600;

/// Why the ledger refuses the start.
#[derive(Debug)]
pub enum LedgerFault {
    /// The open or the read of the journal failed.
    Io(io::Error),
    /// The journal is no regular file.
    NotRegular,
    /// The journal passes `JOURNAL_MAX_BYTES`.
    TooLarge,
    /// The journal is not UTF-8.
    NotUtf8,
    /// A line of the journal breaks its form.
    Journal(JournalError),
}

impl fmt::Display for LedgerFault {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Io(error) => write!(formatter, "the journal cannot be read: {error}"),
            Self::NotRegular => formatter.write_str("the journal is no regular file"),
            Self::TooLarge => write!(formatter, "the journal passes {JOURNAL_MAX_BYTES} bytes"),
            Self::NotUtf8 => formatter.write_str("the journal is not UTF-8"),
            Self::Journal(error) => write!(formatter, "the journal: {error}"),
        }
    }
}

impl std::error::Error for LedgerFault {}

/// Why an issue gives no statement.
#[derive(Debug)]
pub enum IssueFault {
    /// The run or its commit already got a statement.
    Already(AlreadyIssued),
    /// The append or the sync of the journal failed. The run and the
    /// commit stay issued.
    Write(io::Error),
}

/// The issued runs and their journal.
#[derive(Debug)]
pub struct Ledger {
    issued: IssuedRuns,
    journal: File,
}

impl Ledger {
    /// Opens the journal at `path`, or makes it with the mode 0600, and
    /// reads it once.
    ///
    /// # Errors
    ///
    /// Each `LedgerFault`. A journal that does not read refuses the start.
    pub fn open(path: &Path) -> Result<Self, LedgerFault> {
        let mut journal = OpenOptions::new()
            .read(true)
            .append(true)
            .create(true)
            .mode(JOURNAL_MODE)
            .open(path)
            .map_err(LedgerFault::Io)?;
        let meta = journal.metadata().map_err(LedgerFault::Io)?;
        if !meta.is_file() {
            return Err(LedgerFault::NotRegular);
        }
        if meta.len() > JOURNAL_MAX_BYTES {
            return Err(LedgerFault::TooLarge);
        }

        let mut bytes = Vec::new();
        (&mut journal)
            .take(JOURNAL_MAX_BYTES.saturating_add(1))
            .read_to_end(&mut bytes)
            .map_err(LedgerFault::Io)?;
        let text = String::from_utf8(bytes).map_err(|_| LedgerFault::NotUtf8)?;
        let issued = IssuedRuns::from_journal(&text).map_err(LedgerFault::Journal)?;
        Ok(Self { issued, journal })
    }

    /// Whether the run may still get its statement: the check at `open`.
    ///
    /// # Errors
    ///
    /// `AlreadyIssued` for a run or a commit in the journal.
    pub fn admit(&self, eligible: &Eligible) -> Result<(), AlreadyIssued> {
        self.issued.admit(eligible)
    }

    /// The bytes to sign for the eligible run, after its line is in the
    /// journal and synced (FR21).
    ///
    /// # Errors
    ///
    /// Each `IssueFault`.
    pub fn issue(
        &mut self,
        eligible: Eligible,
        request: Request,
        signed_at: SignedAt,
    ) -> Result<Signable, IssueFault> {
        let issued = self
            .issued
            .issue(eligible, request, signed_at)
            .map_err(IssueFault::Already)?;
        self.journal
            .write_all(issued.line.text().as_bytes())
            .and_then(|()| self.journal.sync_data())
            .map_err(IssueFault::Write)?;
        Ok(issued.signable)
    }
}
