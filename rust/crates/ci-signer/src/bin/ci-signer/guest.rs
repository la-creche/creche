//! One guest connection, after the peer check of `session.rs`: one request
//! line of the allocator and one answer line of the signer (section 4.5
//! steps 3, 4 and 6, FR23).
//!
//! 1. The signer reads one line of at most `REQUEST_MAX_BYTES` bytes and
//!    its LF, within `REQUEST_WAIT`. A byte after the LF, a missing LF or
//!    a longer line is refused. The signer reads the line also when it
//!    refuses at once: a close with unread bytes makes the read of the
//!    guest fail with `ECONNRESET`, and the guest would lose the answer.
//! 2. `Request::parse` passes the line. Only then does the exchange take
//!    the `Eligible` run of its session from the `SessionRun` that each guest
//!    task of the session shares. So a guest that sends no request, or a
//!    request that breaks a rule, leaves the run to a later fd of the
//!    session. Of two exchanges at the same time, exactly one takes it.
//! 3. An exchange that finds no run left answers `already_issued`: an
//!    earlier exchange of the session took the run to `Signer::sign`
//!    (FR22).
//! 4. Else `Signer::sign` gives the answer. The signer writes it within
//!    `ANSWER_WAIT`, and the connection closes.
//!
//! The bytes of the guest are hostile input: `Request::parse` of the core
//! checks the size, the strict JSON, the keys and each limit.

use std::sync::{Arc, Mutex};
use std::time::Duration;

use creche_ci_signer::eligibility::Eligible;
use creche_ci_signer::exchange::{Answer, AnswerCode, REQUEST_MAX_BYTES, Request};
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::UnixStream;
use tokio::time::timeout;

use crate::log;
use crate::signer::Signer;

/// The bound of the read of the request, from the conn line.
pub const REQUEST_WAIT: Duration = Duration::from_secs(30);

/// The bound of the write of the answer.
pub const ANSWER_WAIT: Duration = Duration::from_secs(10);

/// The bound of one whole exchange: the read, the sign, at most 32 tag
/// creations of `signer::TAG_WAIT` each, and the write.
pub const EXCHANGE_WAIT: Duration = Duration::from_secs(10 * 60);

/// The eligible run of one session, shared by each guest task of the
/// session. `None` once an exchange took it. `session.rs` makes it from
/// the `Eligible` of the `accept`.
pub type SessionRun = Arc<Mutex<Option<Eligible>>>;

/// The bytes of one read of the guest.
const CHUNK: usize = 8 * 1024;

/// Why the request line is refused.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum ReadFault {
    /// No LF within `REQUEST_MAX_BYTES` bytes, or the guest closed first.
    Line,
    /// A byte came after the LF.
    Trailing,
    /// The read failed.
    Io,
    /// The line did not come within `REQUEST_WAIT`.
    Timeout,
}

/// The one request line, with no LF.
async fn read_line(guest: &mut UnixStream) -> Result<Vec<u8>, ReadFault> {
    let mut line = Vec::new();
    let mut chunk = [0_u8; CHUNK];
    loop {
        let got = guest.read(&mut chunk).await.map_err(|_| ReadFault::Io)?;
        if got == 0 {
            return Err(ReadFault::Line);
        }
        line.extend_from_slice(chunk.get(..got).unwrap_or_default());
        if let Some(end) = line.iter().position(|byte| *byte == b'\n') {
            if end.saturating_add(1) != line.len() {
                return Err(ReadFault::Trailing);
            }
            line.truncate(end);
            return Ok(line);
        }
        if line.len() > REQUEST_MAX_BYTES {
            return Err(ReadFault::Line);
        }
    }
}

/// The answer to the guest of one session. `held` is the eligible run of
/// the session. The exchange takes it only for a request that parses.
async fn answer(guest: &mut UnixStream, held: &SessionRun, signer: &Signer) -> Answer {
    let read = timeout(REQUEST_WAIT, read_line(guest))
        .await
        .unwrap_or(Err(ReadFault::Timeout));
    let line = match read {
        Ok(line) => line,
        Err(fault) => {
            log(format_args!("guest: no request line: {fault:?}"));
            return Answer::Refused(AnswerCode::Grammar);
        }
    };
    let request = match Request::parse(&line) {
        Ok(request) => request,
        Err(error) => {
            log(format_args!("guest: {error}"));
            return Answer::Refused(error.answer_code());
        }
    };
    // The take and the start of `sign` have no await between them, and
    // `sign` writes the journal line before its first await. So an abort
    // of the task never drops a taken run before its journal line. A
    // poisoned lock answers `fault` (fail closed). The one critical
    // section is a `take`, which does not panic.
    let taken = match held.lock() {
        Ok(mut run) => run.take(),
        Err(_) => {
            log(format_args!("guest: the lock of the session run failed"));
            return Answer::Refused(AnswerCode::Fault);
        }
    };
    let Some(eligible) = taken else {
        log(format_args!("guest: the session already used its run"));
        return Answer::Refused(AnswerCode::AlreadyIssued);
    };
    signer.sign(eligible, request).await
}

/// Serves the guest connection `guest` of a session, then closes it.
pub async fn serve(mut guest: UnixStream, held: SessionRun, signer: Arc<Signer>) {
    let answered = timeout(EXCHANGE_WAIT, answer(&mut guest, &held, &signer)).await;
    let Ok(answer) = answered else {
        log(format_args!("guest: the exchange passed its bound"));
        return;
    };
    let line = answer.to_line();
    let written = timeout(ANSWER_WAIT, async {
        guest.write_all(&line).await?;
        guest.shutdown().await
    })
    .await;
    if !matches!(written, Ok(Ok(()))) {
        log(format_args!("guest: the answer was not written"));
    }
}
