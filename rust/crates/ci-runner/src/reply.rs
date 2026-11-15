//! The reply of each call, read from its status and its body. This is the
//! boundary of the client: each reply becomes a typed value or a `Fault`.
//!
//! Each reply type reads only the fields that the daemon uses and skips
//! every other field, as `creche-vmm-store` reads the `info` reply of
//! Cloud Hypervisor. Forgejo is a third party, and a later Forgejo can
//! add a field. A field that the type reads must have its type, or the
//! read fails. The tests hold the replies recorded from Forgejo 17.0.1.

use hyper::StatusCode;
use serde::Deserialize;
use serde::de::DeserializeOwned;

use crate::error::{Fault, ForgejoMessage, ReplyFault};
use crate::names::{JobHandle, JobId, Labels, RunnerId, RunnerUuid};
use crate::secret::{ApiToken, RunnerToken};

/// The status of a job that waits for a runner.
const WAITING: &str = "waiting";

/// One job that waits for a runner, from the jobs list.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct WaitingJob {
    /// The id of the job.
    id: JobId,
    /// The handle that `forgejo-runner one-job --handle` takes.
    handle: JobHandle,
    /// The labels of `runs-on`.
    runs_on: Labels,
}

impl WaitingJob {
    /// The id of the job.
    #[must_use]
    pub fn id(&self) -> JobId {
        self.id
    }

    /// The handle that `forgejo-runner one-job --handle` takes.
    #[must_use]
    pub fn handle(&self) -> &JobHandle {
        &self.handle
    }

    /// The labels of `runs-on`.
    #[must_use]
    pub fn runs_on(&self) -> &Labels {
        &self.runs_on
    }
}

/// One entry of the jobs list as Forgejo writes it.
#[derive(Deserialize)]
struct JobEntry {
    id: JobId,
    handle: JobHandle,
    runs_on: Labels,
    task_id: u64,
    status: String,
}

/// One runner that the registration made.
#[derive(Debug, Clone, PartialEq, Eq, Deserialize)]
pub struct Registration {
    /// The id, for the removal.
    id: RunnerId,
    /// The uuid, for the runner config of the VM.
    uuid: RunnerUuid,
    /// The token, for the runner config of the VM.
    token: RunnerToken,
}

impl Registration {
    /// The id, for the removal.
    #[must_use]
    pub fn id(&self) -> RunnerId {
        self.id
    }

    /// The uuid, for the runner config of the VM.
    #[must_use]
    pub fn uuid(&self) -> &RunnerUuid {
        &self.uuid
    }

    /// The token, for the runner config of the VM.
    #[must_use]
    pub fn token(&self) -> &RunnerToken {
        &self.token
    }
}

/// The outcome of a removal.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Removal {
    /// Forgejo removed the runner: status 204.
    Removed,
    /// Forgejo has no runner of that id: status 404. Forgejo removes an
    /// ephemeral runner after its one job, so this is a normal outcome.
    Absent,
}

/// The body of a Forgejo error reply.
#[derive(Deserialize)]
struct ErrorBody {
    message: String,
}

/// The jobs list of a reply. Forgejo writes an empty list as `null`, with
/// a label filter or without one. `[]` reads as an empty list too.
pub(crate) fn jobs(
    status: StatusCode,
    body: &[u8],
    token: &ApiToken,
) -> Result<Vec<WaitingJob>, Fault> {
    expect(status, StatusCode::OK, body, token)?;
    let entries: Option<Vec<JobEntry>> = shape(status, body)?;

    entries
        .unwrap_or_default()
        .into_iter()
        .map(|entry| {
            if entry.status != WAITING || entry.task_id != 0 {
                return Err(Fault::Reply {
                    status,
                    fault: ReplyFault::NotWaiting(entry.id),
                });
            }
            Ok(WaitingJob {
                id: entry.id,
                handle: entry.handle,
                runs_on: entry.runs_on,
            })
        })
        .collect()
}

/// The runner of a registration reply: status 201.
pub(crate) fn registration(
    status: StatusCode,
    body: &[u8],
    token: &ApiToken,
) -> Result<Registration, Fault> {
    expect(status, StatusCode::CREATED, body, token)?;
    shape(status, body)
}

/// The outcome of a removal reply: 204, or 404 for no such runner.
pub(crate) fn removal(status: StatusCode, body: &[u8], token: &ApiToken) -> Result<Removal, Fault> {
    if status == StatusCode::NOT_FOUND {
        return Ok(Removal::Absent);
    }
    expect(status, StatusCode::NO_CONTENT, body, token)?;
    Ok(Removal::Removed)
}

/// `Ok` when `status` is `wanted`, else the fault of the status.
fn expect(
    status: StatusCode,
    wanted: StatusCode,
    body: &[u8],
    token: &ApiToken,
) -> Result<(), Fault> {
    if status == wanted {
        return Ok(());
    }
    if status == StatusCode::UNAUTHORIZED {
        return Err(Fault::Unauthorized);
    }

    let message = serde_json::from_slice::<ErrorBody>(body)
        .ok()
        .map(|error| ForgejoMessage::new(&token.redact(&error.message)));
    Err(Fault::Status { status, message })
}

/// `body` read as `T`, or the shape fault with no text of the body.
fn shape<T: DeserializeOwned>(status: StatusCode, body: &[u8]) -> Result<T, Fault> {
    serde_json::from_slice(body).map_err(|error| Fault::Reply {
        status,
        fault: ReplyFault::Shape {
            category: error.classify(),
            line: error.line(),
            column: error.column(),
        },
    })
}

#[cfg(test)]
mod tests {
    use std::fs;
    use std::os::unix::fs::PermissionsExt;
    use std::sync::atomic::{AtomicUsize, Ordering};

    use hyper::StatusCode;
    use serde_json::error::Category;

    use super::{Removal, jobs, registration, removal};
    use crate::error::{Fault, ReplyFault};
    use crate::secret::ApiToken;

    /// A reply recorded from Forgejo 17.0.1.
    macro_rules! recorded {
        ($name:literal) => {
            include_bytes!(concat!("../tests/fixtures/forgejo-17.0.1/", $name))
        };
    }

    /// The count of token files of this test process.
    static FILES: AtomicUsize = AtomicUsize::new(0);

    /// An API token for the redaction, read from a file of its own that
    /// goes once the token is read. It is the token that
    /// `jobs-bad-token.json` quotes.
    fn token() -> ApiToken {
        let file = FILES.fetch_add(1, Ordering::Relaxed);
        let dir =
            std::env::temp_dir().join(format!("ci-runner-reply-{}-{file}", std::process::id()));
        fs::create_dir_all(&dir).unwrap();
        let path = dir.join("token");
        fs::write(&path, "0".repeat(40)).unwrap();
        fs::set_permissions(&path, fs::Permissions::from_mode(0o600)).unwrap();
        let token = ApiToken::read(&path).unwrap();
        fs::remove_dir_all(dir).unwrap();
        token
    }

    #[test]
    fn a_null_list_and_an_empty_list_are_both_empty() {
        let token = token();
        for body in [
            &recorded!("jobs-empty.json")[..],
            recorded!("jobs-label-none.json"),
            b"[]",
            b" null\n",
        ] {
            assert_eq!(jobs(StatusCode::OK, body, &token).unwrap(), []);
        }
    }

    #[test]
    fn a_waiting_job_reads_its_id_handle_and_labels() {
        let token = token();
        for body in [
            recorded!("jobs-one.json"),
            recorded!("jobs-label-match.json"),
        ] {
            let list = jobs(StatusCode::OK, body, &token).unwrap();
            let [job] = list.as_slice() else {
                panic!("{list:?}");
            };
            assert_eq!(job.id().get(), 19);
            assert_eq!(
                job.handle().as_str(),
                "2f923f44-2d31-4d08-ad46-e04a543826e0"
            );
            let labels: Vec<&str> = job.runs_on().iter().map(|label| label.as_str()).collect();
            assert_eq!(labels, ["vm-spike"]);
        }
    }

    #[test]
    fn an_unknown_field_is_skipped_and_a_known_field_keeps_its_type() {
        let token = token();
        let recorded = std::str::from_utf8(recorded!("jobs-one.json")).unwrap();

        let extra = recorded.replacen(r#"{"id":19,"#, r#"{"id":19,"added_later":{"x":[1]},"#, 1);
        assert_eq!(
            jobs(StatusCode::OK, extra.as_bytes(), &token)
                .unwrap()
                .len(),
            1
        );

        for (from, to) in [
            (r#""task_id":0"#, r#""task_id":"0""#),
            (r#""runs_on":["vm-spike"]"#, r#""runs_on":[]"#),
            (r#""runs_on":["vm-spike"]"#, r#""runs_on":["vm spike"]"#),
            (r#""handle":"2f923f44"#, r#""handle":"--rm"#),
            (r#""id":19,"#, r#""id":0,"#),
            (r#""id":19,"#, r#""#),
        ] {
            let changed = recorded.replacen(from, to, 1);
            assert_ne!(changed, recorded);
            let fault = jobs(StatusCode::OK, changed.as_bytes(), &token).unwrap_err();
            assert!(
                matches!(
                    fault,
                    Fault::Reply {
                        fault: ReplyFault::Shape {
                            category: Category::Data,
                            ..
                        },
                        ..
                    }
                ),
                "{to}: {fault:?}"
            );
        }
    }

    #[test]
    fn a_job_that_is_not_waiting_fails_the_list() {
        let token = token();
        let recorded = std::str::from_utf8(recorded!("jobs-one.json")).unwrap();
        for (from, to) in [
            (r#""status":"waiting""#, r#""status":"running""#),
            (r#""task_id":0"#, r#""task_id":7"#),
        ] {
            let changed = recorded.replacen(from, to, 1);
            let fault = jobs(StatusCode::OK, changed.as_bytes(), &token).unwrap_err();
            let Fault::Reply {
                fault: ReplyFault::NotWaiting(job),
                ..
            } = fault
            else {
                panic!("{fault:?}");
            };
            assert_eq!(job.get(), 19);
        }
    }

    #[test]
    fn a_body_that_is_not_json_names_no_text_of_the_body() {
        let token = token();
        for (body, category) in [
            (&b"[{\"id\":19"[..], Category::Eof),
            (b"<html>secret-page</html>", Category::Syntax),
            (b"{\"id\":19}", Category::Data),
        ] {
            let fault = jobs(StatusCode::OK, body, &token).unwrap_err();
            let shown = format!("{fault} {fault:?}");
            assert!(!shown.contains("secret-page"), "{shown}");
            assert!(
                matches!(fault, Fault::Reply { fault: ReplyFault::Shape { category: got, .. }, .. } if got == category),
                "{shown}"
            );
        }
    }

    #[test]
    fn a_registration_reads_its_id_uuid_and_token() {
        let token = token();
        let runner = registration(StatusCode::CREATED, recorded!("register.json"), &token).unwrap();
        assert_eq!(runner.id().get(), 3);
        assert_eq!(
            runner.uuid().as_str(),
            "a4087518-6f64-42b1-8383-c37ab78396af"
        );
        assert_eq!(runner.token().reveal(), "0".repeat(40));
        assert!(!format!("{runner:?}").contains("0000000000"));

        let short = std::str::from_utf8(recorded!("register.json"))
            .unwrap()
            .replacen("\"0000", "\"", 1);
        let fault = registration(StatusCode::CREATED, short.as_bytes(), &token).unwrap_err();
        assert!(!format!("{fault} {fault:?}").contains("0000000000"));

        let fault = registration(StatusCode::OK, recorded!("register.json"), &token).unwrap_err();
        assert!(matches!(
            fault,
            Fault::Status {
                status: StatusCode::OK,
                ..
            }
        ));
    }

    #[test]
    fn a_removal_takes_204_and_404() {
        let token = token();
        assert_eq!(
            removal(StatusCode::NO_CONTENT, b"", &token).unwrap(),
            Removal::Removed
        );
        assert_eq!(
            removal(
                StatusCode::NOT_FOUND,
                recorded!("delete-again.json"),
                &token
            )
            .unwrap(),
            Removal::Absent
        );
        assert!(matches!(
            removal(StatusCode::OK, b"", &token),
            Err(Fault::Status {
                status: StatusCode::OK,
                message: None
            })
        ));
    }

    #[test]
    fn a_401_keeps_no_message_and_another_status_keeps_a_redacted_one() {
        let token = token();
        let fault = jobs(
            StatusCode::UNAUTHORIZED,
            recorded!("jobs-bad-token.json"),
            &token,
        )
        .unwrap_err();
        assert!(matches!(fault, Fault::Unauthorized));
        assert!(!format!("{fault} {fault:?}").contains("0000000000"));

        let fault = jobs(
            StatusCode::NOT_FOUND,
            recorded!("delete-again.json"),
            &token,
        )
        .unwrap_err();
        let Fault::Status {
            status: StatusCode::NOT_FOUND,
            message: Some(message),
        } = &fault
        else {
            panic!("{fault:?}");
        };
        assert_eq!(
            message.as_str(),
            "runner with ID 3: resource does not exist"
        );

        // The 401 message of Forgejo with another status: each copy of the
        // token goes, and the line breaks show escaped.
        let fault = jobs(
            StatusCode::FORBIDDEN,
            recorded!("jobs-bad-token.json"),
            &token,
        )
        .unwrap_err();
        let shown = format!("{fault}");
        assert!(!shown.contains("0000000000"), "{shown}");
        assert!(shown.contains(r#"task with token \"<token>\""#), "{shown}");
        assert!(!shown.contains('\n'), "{shown}");
    }
}
