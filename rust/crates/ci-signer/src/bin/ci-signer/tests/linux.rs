//! The session tests on a socketpair, with the test as a stand-in daemon.
//! Linux only, as the session.

use std::io::IoSlice;
use std::mem::MaybeUninit;
use std::os::fd::{AsFd, BorrowedFd};
use std::sync::Arc;
use std::time::Duration;

use rustix::net::{SendAncillaryBuffer, SendAncillaryMessage, SendFlags};
use serde_json::Value;
use tokio::io::{AsyncReadExt, AsyncWriteExt, Interest};
use tokio::net::UnixStream;

use super::{Forgejo, Host, STAND_IN_COSIGN, paused, recorded, running_release, runtime};
use crate::line::tests::OPEN;
use crate::session::{self, End};
use crate::signer::Signer;

/// The open line of run 48 of the release workflow of creche.
fn open_48() -> String {
    format!(
        "{}\n",
        OPEN.replace("example-owner/runner-test", super::REPOSITORY)
            .replace("probe.yml", "release.yml")
    )
}

const SESSION: &str = "2f923f44-2d31-4d08-ad46-e04a543826e0";

fn conn(session: &str) -> String {
    format!("{{\"v\":2,\"op\":\"conn\",\"session\":\"{session}\",\"port\":5000}}\n")
}

fn close(session: &str) -> String {
    format!("{{\"v\":2,\"op\":\"close\",\"session\":\"{session}\"}}\n")
}

/// The most fds that `send_fds` passes with one line.
const SEND_MAX: usize = 8;

/// Writes `line` on the control connection, with `fd` by SCM_RIGHTS.
async fn send(control: &UnixStream, line: &str, fd: Option<BorrowedFd<'_>>) {
    let fds: Vec<BorrowedFd<'_>> = fd.into_iter().collect();
    send_fds(control, line, &fds).await;
}

/// Writes `line` on the control connection, with each of `fds` by
/// SCM_RIGHTS.
async fn send_fds(control: &UnixStream, line: &str, fds: &[BorrowedFd<'_>]) {
    assert!(fds.len() <= SEND_MAX);
    control
        .async_io(Interest::WRITABLE, || {
            let mut space = [MaybeUninit::uninit(); rustix::cmsg_space!(ScmRights(SEND_MAX))];
            let mut ancillary = SendAncillaryBuffer::new(&mut space);
            if !fds.is_empty() {
                assert!(ancillary.push(SendAncillaryMessage::ScmRights(fds)));
            }
            let sent = rustix::net::sendmsg(
                control,
                &[IoSlice::new(line.as_bytes())],
                &mut ancillary,
                SendFlags::empty(),
            )?;
            assert_eq!(sent, line.len());
            Ok(())
        })
        .await
        .unwrap();
}

/// The signer of a stand-in Forgejo with the running run 48.
async fn signer_48(
    host: &Host,
    reply: creche_ci_signer::forgejo::Reply,
) -> (Arc<Signer>, Arc<Forgejo>) {
    let forgejo = Arc::new(Forgejo::with_run(48, reply));
    let origin = forgejo.start().await;
    let signer = Arc::new(Signer::start(host.config(&origin), &host.paths).unwrap());
    (signer, forgejo)
}

/// How a stream of the test ended after its bytes.
#[derive(Debug, PartialEq, Eq)]
enum Ending {
    /// The other end closed with nothing of ours unread.
    Eof,
    /// The other end closed with bytes of ours unread: Linux gives
    /// `ECONNRESET` after the queued bytes.
    Reset,
}

/// Reads `stream` to its end: its bytes and how it ended.
async fn drain(stream: &mut UnixStream) -> (Vec<u8>, Ending) {
    let mut bytes = Vec::new();
    let mut chunk = [0_u8; 4096];
    loop {
        match stream.read(&mut chunk).await {
            Ok(0) => return (bytes, Ending::Eof),
            Ok(count) => bytes.extend_from_slice(&chunk[..count]),
            Err(error) if error.kind() == std::io::ErrorKind::ConnectionReset => {
                return (bytes, Ending::Reset);
            }
            Err(error) => panic!("{error}"),
        }
    }
}

/// Reads the control connection to its end: the one answer line.
async fn rest(mut daemon: UnixStream) -> Vec<u8> {
    drain(&mut daemon).await.0
}

/// The full exchange with a stand-in daemon: open, accept, a conn line
/// with an fd. The peer of that fd is the test process, which runs in no
/// slot unit, so the peer check refuses it before the signer reads a byte.
/// The guest sees the end of the fd, and no statement and no tag exist.
#[test]
fn a_passed_fd_whose_peer_is_in_no_slot_unit_is_refused() {
    runtime().block_on(async {
        let host = Host::new(STAND_IN_COSIGN);
        let (signer, forgejo) =
            signer_48(&host, recorded("p1-run-push-main.json", running_release)).await;
        let (daemon, service) = UnixStream::pair().unwrap();
        let task = tokio::spawn(async move { session::serve(service, &signer).await });

        send(&daemon, &open_48(), None).await;
        let mut answer = [0_u8; 34];
        let mut daemon = daemon;
        daemon.read_exact(&mut answer).await.unwrap();
        assert_eq!(&answer, crate::line::ACCEPT);

        let (mut guest, passed) = UnixStream::pair().unwrap();
        guest.write_all(super::REQUEST.as_bytes()).await.unwrap();
        send(&daemon, &conn(SESSION), Some(passed.as_fd())).await;
        drop(passed);

        let end = task.await.unwrap();
        assert!(matches!(end, End::Peer), "{end:?}");
        // The signer closed the fd with the request unread, so the guest
        // gets no answer and a reset.
        assert_eq!(drain(&mut guest).await, (Vec::new(), Ending::Reset));
        assert!(rest(daemon).await.is_empty());
        assert!(forgejo.posts().is_empty());
        assert_eq!(host.journal(), "");
    });
}

/// A conn line or a close line of a session that the signer did not
/// accept is refused: the session ends and its fd closes unread.
#[test]
fn a_line_of_another_session_is_refused() {
    runtime().block_on(async {
        for line in [
            conn("another-session"),
            close("another-session"),
            conn(SESSION).replace("5000", "5001"),
        ] {
            let host = Host::new(STAND_IN_COSIGN);
            let (signer, _forgejo) =
                signer_48(&host, recorded("p1-run-push-main.json", running_release)).await;
            let (daemon, service) = UnixStream::pair().unwrap();
            let task = tokio::spawn(async move { session::serve(service, &signer).await });
            send(&daemon, &open_48(), None).await;
            let (mut guest, passed) = UnixStream::pair().unwrap();
            send(&daemon, &line, Some(passed.as_fd())).await;
            drop(passed);
            let end = task.await.unwrap();
            assert!(matches!(end, End::OtherSession), "{line}: {end:?}");
            assert_eq!(drain(&mut guest).await, (Vec::new(), Ending::Eof));
            assert_eq!(rest(daemon).await, crate::line::ACCEPT);
        }
    });
}

/// An open line of a run that fails a check of section 4.4 gets
/// `accept:false`, and the session ends: a later conn line reaches no
/// session.
#[test]
fn a_refused_open_answers_false_and_ends_the_session() {
    runtime().block_on(async {
        let host = Host::new(STAND_IN_COSIGN);
        // The recorded run 48 is over (check 5).
        let (signer, forgejo) =
            signer_48(&host, recorded("p1-run-push-main.json", super::as_release)).await;
        let (daemon, service) = UnixStream::pair().unwrap();
        let task = tokio::spawn(async move { session::serve(service, &signer).await });
        send(&daemon, &open_48(), None).await;
        let end = task.await.unwrap();
        assert!(matches!(end, End::Declined("finished")), "{end:?}");
        assert_eq!(rest(daemon).await, crate::line::REFUSE);
        assert!(forgejo.posts().is_empty());
        assert_eq!(host.journal(), "");
    });
}

/// The close line ends the session, a close line with an fd and a conn line
/// with no fd end it, and an open line past 4 KiB is refused.
#[test]
fn close_no_fd_and_a_long_line_end_the_session() {
    runtime().block_on(async {
        let host = Host::new(STAND_IN_COSIGN);
        let (signer, _forgejo) =
            signer_48(&host, recorded("p1-run-push-main.json", running_release)).await;

        let (daemon, service) = UnixStream::pair().unwrap();
        let session_signer = Arc::clone(&signer);
        let task = tokio::spawn(async move { session::serve(service, &session_signer).await });
        send(&daemon, &open_48(), None).await;
        send(&daemon, &close(SESSION), None).await;
        assert!(matches!(task.await.unwrap(), End::Closed));
        assert_eq!(rest(daemon).await, crate::line::ACCEPT);

        // A close line whose fd is still queued: an fd with no conn line.
        let (daemon, service) = UnixStream::pair().unwrap();
        let session_signer = Arc::clone(&signer);
        let task = tokio::spawn(async move { session::serve(service, &session_signer).await });
        send(&daemon, &open_48(), None).await;
        let (_guest, passed) = UnixStream::pair().unwrap();
        send(&daemon, &close(SESSION), Some(passed.as_fd())).await;
        drop(passed);
        let end = task.await.unwrap();
        assert!(matches!(end, End::NoFd), "{end:?}");

        let (daemon, service) = UnixStream::pair().unwrap();
        let session_signer = Arc::clone(&signer);
        let task = tokio::spawn(async move { session::serve(service, &session_signer).await });
        send(&daemon, &open_48(), None).await;
        send(&daemon, &conn(SESSION), None).await;
        assert!(matches!(task.await.unwrap(), End::NoFd));

        let (daemon, service) = UnixStream::pair().unwrap();
        let session_signer = Arc::clone(&signer);
        let task = tokio::spawn(async move { session::serve(service, &session_signer).await });
        let long = format!("{}\n", "x".repeat(crate::line::LINE_MAX));
        send(&daemon, &long, None).await;
        let end = task.await.unwrap();
        assert!(
            matches!(end, End::Line(crate::line::LineFault::TooLong)),
            "{end:?}"
        );
        assert_eq!(rest(daemon).await, crate::line::REFUSE);
    });
}

/// A conn line with more fds than the control buffer of the signer holds:
/// the kernel cuts the control data (`MSG_CTRUNC`), and the read of the
/// line fails. The signer drops each fd that came, and checks none.
#[test]
fn a_cut_of_the_control_data_ends_the_session() {
    runtime().block_on(async {
        let host = Host::new(STAND_IN_COSIGN);
        let (signer, forgejo) =
            signer_48(&host, recorded("p1-run-push-main.json", running_release)).await;
        let (mut daemon, service) = UnixStream::pair().unwrap();
        let task = tokio::spawn(async move { session::serve(service, &signer).await });
        send(&daemon, &open_48(), None).await;
        let mut answer = [0_u8; 34];
        daemon.read_exact(&mut answer).await.unwrap();
        assert_eq!(&answer, crate::line::ACCEPT);

        let pairs: Vec<(UnixStream, UnixStream)> =
            (0..SEND_MAX).map(|_| UnixStream::pair().unwrap()).collect();
        let fds: Vec<BorrowedFd<'_>> = pairs.iter().map(|(_, passed)| passed.as_fd()).collect();
        send_fds(&daemon, &conn(SESSION), &fds).await;

        let end = task.await.unwrap();
        assert!(matches!(end, End::Read), "{end:?}");
        assert!(rest(daemon).await.is_empty());
        assert!(forgejo.posts().is_empty());
        assert_eq!(host.journal(), "");
    });
}

/// The bound of the open line: a daemon that sends nothing ends the
/// session at `ANSWER_BOUND`, not later, with no read of Forgejo.
#[test]
fn a_daemon_that_sends_nothing_is_dropped_at_the_bound() {
    paused().block_on(async {
        let host = Host::new(STAND_IN_COSIGN);
        let signer =
            Arc::new(Signer::start(host.config("http://127.0.0.1:9"), &host.paths).unwrap());
        let (daemon, service) = UnixStream::pair().unwrap();
        let started = tokio::time::Instant::now();
        let end = session::serve(service, &signer).await;
        assert!(matches!(end, End::OpenTimeout), "{end:?}");
        assert!(started.elapsed() >= session::ANSWER_BOUND);
        assert!(started.elapsed() < session::ANSWER_BOUND + Duration::from_secs(1));
        drop(daemon);
    });
}

/// An open line with the facts of a real job gives the key of the read,
/// and nothing else of the facts reaches the signer.
#[test]
fn the_open_line_names_the_key_only() {
    let open = crate::line::open(open_48().trim_end().as_bytes()).unwrap();
    let value: Value = serde_json::from_str(open_48().trim_end()).unwrap();
    assert_eq!(
        open.key().run.get(),
        value["facts"]["run"].as_u64().unwrap()
    );
    assert_eq!(
        open.key().repository.to_string(),
        value["facts"]["repository"]
    );
}
