//! The decisions and the statement of the signer, with the I/O of each:
//! the two reads of section 4.4, the ledger, cosign and the tag creation
//! of section 4.5. The socket side (`session.rs`, `guest.rs`) calls only
//! `decide` and `sign`.
//!
//! ```text
//!   open line ─► decide: GET runs/{id} ─► decide_run ─► GET commits/{sha}/pull
//!                        ─► decide_merge ─► Ledger::admit ─► Eligible (held by the session)
//!   request  ─► sign: Ledger::issue (journal line synced) ─► cosign ─► TagMessage
//!                        ─► POST tags, one for each tag ─► Answer::Signed
//! ```

use std::fmt;
use std::path::PathBuf;
use std::sync::Mutex;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use creche_ci_signer::eligibility::{Eligible, Refusal, RunKey, decide_merge, decide_run};
use creche_ci_signer::exchange::{Answer, AnswerCode, Request};
use creche_ci_signer::forgejo::{ReplyFault, parse_created_tag};
use creche_ci_signer::issue::AlreadyIssued;
use creche_ci_signer::message::TagMessage;
use creche_ci_signer::names::ReleaseTag;
use creche_ci_signer::pairs::{PairsError, SigningPairs};
use creche_ci_signer::signed_at::SignedAt;

use crate::config::Config;
use crate::cosign::{Cosign, CosignFault};
use crate::credentials::{CredentialFault, Credentials};
use crate::forge::{Fault, Forge};
use crate::ledger::{IssueFault, Ledger, LedgerFault};
use crate::log;

/// The bound of each read of section 4.4. The two reads, the read of the
/// `open` line and the answer must fit in the bound of a session of the
/// daemon, 10 s from the connect to the answer (the runner design,
/// section 6.2, rule 1).
pub const READ_WAIT: Duration = Duration::from_secs(4);

/// The bound of each tag creation of section 4.5 step 7.
pub const TAG_WAIT: Duration = Duration::from_secs(10);

/// The folders of the signer. `main.rs` gives the paths of the unit.
#[derive(Debug, Clone)]
pub struct Paths {
    /// The journal of the issued runs (FR21).
    pub journal: PathBuf,
    /// The work folder of cosign, mode 0700.
    pub work: PathBuf,
    /// The credentials folder of the unit.
    pub credentials: PathBuf,
}

/// Why the signer does not start.
#[derive(Debug)]
pub enum StartFault {
    /// `signing-pairs.tsv` does not parse.
    Pairs(PairsError),
    /// A credential is missing or malformed.
    Credential(CredentialFault),
    /// The journal does not read.
    Ledger(LedgerFault),
    /// The work folder of cosign could not be prepared.
    Work(std::io::Error),
}

impl fmt::Display for StartFault {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Pairs(error) => write!(formatter, "signing-pairs.tsv: {error}"),
            Self::Credential(error) => error.fmt(formatter),
            Self::Ledger(error) => error.fmt(formatter),
            Self::Work(error) => write!(formatter, "the work folder: {error}"),
        }
    }
}

impl std::error::Error for StartFault {}

/// Why an `open` gets no accept.
#[derive(Debug)]
pub enum Decline {
    /// A check of section 4.4 refused the run, or a reply gave no fact.
    Refused(Refusal),
    /// A read gave no reply.
    Read(Fault),
    /// The run or its commit already got a statement.
    Already(AlreadyIssued),
    /// The lock of the ledger failed.
    Ledger,
}

impl Decline {
    /// A stable word for the log. No text of a reply gets into it.
    pub fn code(&self) -> &'static str {
        match self {
            Self::Refused(refusal) => refusal.code(),
            Self::Read(fault) => fault.code(),
            Self::Already(_) => "already_issued",
            Self::Ledger => "ledger",
        }
    }
}

impl fmt::Display for Decline {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Refused(refusal) => write!(formatter, "the run is refused: {refusal}"),
            Self::Read(fault) => fault.fmt(formatter),
            Self::Already(already) => already.fmt(formatter),
            Self::Ledger => formatter.write_str("the lock of the ledger failed"),
        }
    }
}

/// The signer: the pairs, the Forgejo client, the credentials, cosign and
/// the one ledger.
#[derive(Debug)]
pub struct Signer {
    pairs: SigningPairs,
    forge: Forge,
    credentials: Credentials,
    cosign: Cosign,
    ledger: Mutex<Ledger>,
}

impl Signer {
    /// The signer of `config` on the folders `paths`. It reads the
    /// credentials and the journal once.
    ///
    /// # Errors
    ///
    /// Each `StartFault`. Each refuses the start (fail closed).
    pub fn start(config: Config, paths: &Paths) -> Result<Self, StartFault> {
        let pairs = SigningPairs::builtin(config.owner).map_err(StartFault::Pairs)?;
        let credentials = Credentials::read(&paths.credentials).map_err(StartFault::Credential)?;
        let ledger = Ledger::open(&paths.journal).map_err(StartFault::Ledger)?;
        let cosign = Cosign::prepare(config.cosign, credentials.key.clone(), paths.work.clone())
            .map_err(StartFault::Work)?;
        Ok(Self {
            pairs,
            forge: Forge::new(config.forgejo),
            credentials,
            cosign,
            ledger: Mutex::new(ledger),
        })
    }

    /// The checks of section 4.4 for the run of `key`, with the signer's
    /// own reads. Only an `Eligible` run that the ledger admits is
    /// accepted.
    ///
    /// # Errors
    ///
    /// The `Decline` of the first check or read that fails.
    pub async fn decide(&self, key: &RunKey) -> Result<Eligible, Decline> {
        let token = &self.credentials.reader;
        let run = self
            .forge
            .run(token, key, READ_WAIT)
            .await
            .map_err(Decline::Read)?;
        let checked = decide_run(&self.pairs, key, &run).map_err(Decline::Refused)?;
        let pull = self
            .forge
            .commit_pull(token, checked.repository(), checked.commit(), READ_WAIT)
            .await
            .map_err(Decline::Read)?;
        let eligible = decide_merge(checked, &pull).map_err(Decline::Refused)?;

        let ledger = self.ledger.lock().map_err(|_| Decline::Ledger)?;
        ledger.admit(&eligible).map_err(Decline::Already)?;
        Ok(eligible)
    }

    /// The statement of `eligible` with the tags of `request`: the journal
    /// line, the signature and each annotated tag (section 4.5 steps 5 to
    /// 7). Each failure after the journal line leaves the run issued, with
    /// no second statement (fail closed).
    pub async fn sign(&self, eligible: Eligible, request: Request) -> Answer {
        let repository = eligible.repository().clone();
        let commit = eligible.commit().clone();
        let run = eligible.run();
        let tags: Vec<ReleaseTag> = request
            .tags()
            .items()
            .iter()
            .map(|entry| entry.tag.clone())
            .collect();

        let signable = match self.issue(eligible, request) {
            Ok(signable) => signable,
            Err(IssueFault::Already(already)) => {
                log(format_args!("sign run={run}: refused: {already}"));
                return Answer::Refused(AnswerCode::AlreadyIssued);
            }
            Err(IssueFault::Write(error)) => {
                log(format_args!(
                    "sign run={run}: the journal write failed: {error}"
                ));
                return Answer::Refused(AnswerCode::Fault);
            }
        };

        let bundle = match self.cosign.sign(&signable, run).await {
            Ok(bundle) => bundle,
            Err(fault) => return fault_answer(run, &fault),
        };
        let message = TagMessage::new(signable, bundle);
        let text = message.encode();

        for tag in &tags {
            let created = self
                .forge
                .create_tag(
                    &self.credentials.tagger,
                    &repository,
                    tag,
                    &commit,
                    &text,
                    TAG_WAIT,
                )
                .await;
            let checked = match created {
                Ok(reply) => parse_created_tag(&reply).map_err(TagFault::Reply),
                Err(fault) => Err(TagFault::Call(fault)),
            };
            match checked {
                Ok(made)
                    if made.name == tag.as_str()
                        && made.commit == commit
                        && made.message == text => {}
                Ok(_) => {
                    log(format_args!(
                        "sign run={run}: tag {tag}: the reply names another tag"
                    ));
                    return Answer::Refused(AnswerCode::Fault);
                }
                Err(fault) => {
                    log(format_args!("sign run={run}: tag {tag}: {}", fault.code()));
                    return Answer::Refused(AnswerCode::Fault);
                }
            }
        }
        log(format_args!(
            "sign run={run}: signed and tagged {} tags at {commit}",
            tags.len()
        ));
        Answer::Signed(message)
    }

    fn issue(
        &self,
        eligible: Eligible,
        request: Request,
    ) -> Result<creche_ci_signer::issue::Signable, IssueFault> {
        let signed_at = now()
            .ok_or_else(|| IssueFault::Write(std::io::Error::other("the clock is out of range")))?;
        let mut ledger = self
            .ledger
            .lock()
            .map_err(|_| IssueFault::Write(std::io::Error::other("the ledger lock failed")))?;
        ledger.issue(eligible, request, signed_at)
    }
}

/// Why one tag creation failed.
enum TagFault {
    Call(Fault),
    Reply(ReplyFault),
}

impl TagFault {
    fn code(&self) -> &'static str {
        match self {
            Self::Call(fault) => fault.code(),
            Self::Reply(fault) => fault.code(),
        }
    }
}

/// The answer and the log line of a failed sign.
fn fault_answer(run: creche_ci_signer::names::RunId, fault: &CosignFault) -> Answer {
    log(format_args!("sign run={run}: {fault}"));
    Answer::Refused(AnswerCode::Fault)
}

/// The time of the statement: whole seconds of UTC now.
fn now() -> Option<SignedAt> {
    let seconds = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs();
    SignedAt::from_unix_seconds(seconds).ok()
}
