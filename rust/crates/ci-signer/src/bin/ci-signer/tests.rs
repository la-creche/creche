//! The tests of the program: a stand-in Forgejo that serves the recorded
//! replies of Forgejo 17.0.1, a stand-in cosign, and a stand-in daemon on a
//! socketpair (Linux only).
//!
//! Each run reply is a recording of `tests/fixtures/forgejo-17.0.1/`. The
//! recordings come from the repository `runner-test`, and the signer takes
//! its pairs from `signing-pairs.tsv`. So a test that isolates one check
//! changes `repository.full_name` to `example-owner/creche` and
//! `workflow_id` to `release.yml` in test code, as the tests of the core
//! change the status. Each recorded run is `success`, so a test of an
//! unfinished run changes the status to `running`.
//!
//! The kernel checks of `ci-runner-peer` pass only for a real slot unit of
//! a runner host and for the user `ci-runner`. So the session tests start
//! at `session::serve`, after the daemon check, and each passed fd is
//! refused by the peer check: the test process runs in no slot unit. The
//! guest exchange that follows an accepted fd is tested on its own, from
//! an `Eligible` run of the recordings in a `guest::SessionRun`, as the session
//! holds it.

use std::collections::BTreeMap;
use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use creche_ci_signer::eligibility::{Eligible, RunKey, decide_merge, decide_run};
use creche_ci_signer::forgejo::{REPLY_MAX_BYTES, Reply};
use creche_ci_signer::pairs::SigningPairs;
use creche_ci_signer::statement::Statement;
use http_body_util::{BodyExt, Full};
use hyper::body::{Bytes, Incoming};
use hyper::server::conn::http1;
use hyper::service::service_fn;
use hyper::{Request, Response};
use hyper_util::rt::TokioIo;
use serde_json::Value;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::{TcpListener, UnixStream};

use crate::config::Config;
use crate::guest;
use crate::signer::{Paths, Signer};

/// The owner of the recordings.
const OWNER: &str = "example-owner";

/// The repository of the signing pairs that each isolated case names.
const REPOSITORY: &str = "example-owner/creche";

/// The merge commit of run 48 of the recordings.
const MERGE_COMMIT: &str = "5791a8cdae55087207f12ab919c5eb964494d654";

/// The token of `release-reader` and of the bot `ci-signer` in the tests.
const READER: &str = "1111111111111111111111111111111111111111";
const TAGGER: &str = "2222222222222222222222222222222222222222";

/// One request that the stand-in Forgejo got.
#[derive(Debug, Clone)]
pub(crate) struct Seen {
    pub(crate) method: String,
    pub(crate) path: String,
    pub(crate) authorization: String,
    pub(crate) body: Vec<u8>,
}

/// The reply of the stand-in to each `POST .../tags`.
#[derive(Debug, Default, Clone)]
pub(crate) enum TagReply {
    /// 201 with the recorded create reply, its name, commit and message
    /// those of the request.
    #[default]
    Echo,
    /// The echo, then this change in test code.
    Changed(fn(&mut Value)),
    /// This reply, whatever the request.
    Fixed(Reply),
}

/// The replies of the stand-in by method and path, and what it got.
#[derive(Debug, Default)]
pub(crate) struct Forgejo {
    replies: BTreeMap<(String, String), Reply>,
    tag_reply: TagReply,
    pub(crate) seen: Mutex<Vec<Seen>>,
}

fn fixture(name: &str) -> Vec<u8> {
    let path = Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("tests/fixtures/forgejo-17.0.1")
        .join(name);
    fs::read(path).unwrap()
}

/// The status of a recording, from `index.tsv`.
fn status(name: &str) -> u16 {
    let index = String::from_utf8(fixture("index.tsv")).unwrap();
    let row = index
        .lines()
        .find(|line| line.split('\t').next() == Some(name))
        .unwrap();
    row.split('\t').nth(3).unwrap().parse().unwrap()
}

/// The recorded reply `name` with `change` made in test code.
pub(crate) fn recorded(name: &str, change: impl FnOnce(&mut Value)) -> Reply {
    let mut value: Value = serde_json::from_slice(&fixture(name)).unwrap();
    change(&mut value);
    Reply {
        status: status(name),
        body: serde_json::to_vec(&value).unwrap(),
    }
}

/// A recorded run made a run of the release workflow of creche.
pub(crate) fn as_release(run: &mut Value) {
    run["repository"]["full_name"] = Value::from(REPOSITORY);
    run["workflow_id"] = Value::from("release.yml");
}

/// `as_release`, and the status `running`.
pub(crate) fn running_release(run: &mut Value) {
    as_release(run);
    run["status"] = Value::from("running");
}

pub(crate) fn key(run: u64) -> RunKey {
    RunKey {
        repository: REPOSITORY.parse().unwrap(),
        run: run.try_into().unwrap(),
    }
}

fn run_path(run: u64) -> String {
    format!("/api/v1/repos/{REPOSITORY}/actions/runs/{run}")
}

fn pull_path() -> String {
    format!("/api/v1/repos/{REPOSITORY}/commits/{MERGE_COMMIT}/pull")
}

impl Forgejo {
    /// The stand-in with the run `run` as `reply` and the recorded PR of
    /// the merge commit.
    pub(crate) fn with_run(run: u64, reply: Reply) -> Self {
        let mut forgejo = Self::default();
        forgejo.get(&run_path(run), reply);
        forgejo.get(
            &pull_path(),
            recorded("p1-commit-pull-merge-sha.json", |_| {}),
        );
        forgejo
    }

    pub(crate) fn get(&mut self, path: &str, reply: Reply) {
        self.replies
            .insert(("GET".to_owned(), path.to_owned()), reply);
    }

    pub(crate) fn posts(&self) -> Vec<Seen> {
        self.seen
            .lock()
            .unwrap()
            .iter()
            .filter(|seen| seen.method == "POST")
            .cloned()
            .collect()
    }

    /// The reply to one request. A `POST .../tags` gets its `TagReply`.
    fn answer(&self, seen: &Seen) -> Reply {
        if seen.method == "POST" && seen.path.ends_with("/tags") {
            let request: Value = serde_json::from_slice(&seen.body).unwrap();
            let change = match &self.tag_reply {
                TagReply::Fixed(reply) => return reply.clone(),
                TagReply::Echo => None,
                TagReply::Changed(change) => Some(*change),
            };
            return recorded("p2-tag-create.json", |tag| {
                tag["name"] = request["tag_name"].clone();
                tag["message"] = request["message"].clone();
                tag["commit"]["sha"] = request["target"].clone();
                if let Some(change) = change {
                    change(tag);
                }
            });
        }
        self.replies
            .get(&(seen.method.clone(), seen.path.clone()))
            .cloned()
            .unwrap_or(Reply {
                status: 404,
                body: b"{}".to_vec(),
            })
    }

    /// Serves on a loopback port and gives the origin.
    pub(crate) async fn start(self: &Arc<Self>) -> String {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let forgejo = Arc::clone(self);
        tokio::spawn(async move {
            loop {
                let Ok((stream, _)) = listener.accept().await else {
                    return;
                };
                let forgejo = Arc::clone(&forgejo);
                tokio::spawn(async move {
                    let service = service_fn(move |request: Request<Incoming>| {
                        let forgejo = Arc::clone(&forgejo);
                        async move {
                            let method = request.method().to_string();
                            let path = request.uri().path().to_owned();
                            let authorization = request
                                .headers()
                                .get("authorization")
                                .map(|value| value.to_str().unwrap().to_owned())
                                .unwrap_or_default();
                            let body = request.into_body().collect().await?.to_bytes().to_vec();
                            let seen = Seen {
                                method,
                                path,
                                authorization,
                                body,
                            };
                            let reply = forgejo.answer(&seen);
                            forgejo.seen.lock().unwrap().push(seen);
                            let response = Response::builder()
                                .status(reply.status)
                                .body(Full::new(Bytes::from(reply.body)))
                                .unwrap();
                            Ok::<_, hyper::Error>(response)
                        }
                    });
                    let _ended = http1::Builder::new()
                        .serve_connection(TokioIo::new(stream), service)
                        .await;
                });
            }
        });
        format!("http://{address}")
    }
}

/// The stand-in cosign. It requires the command line and the environment
/// of section 4.5 step 5, drains stdin and writes a bundle at the path of
/// `--bundle`. Each failed check exits with its own status. It uses
/// builtins of `sh` only, because the child has no `PATH`. `Host::new`
/// puts the paths of the test host and the signing config in place of
/// `@CREDENTIALS@`, `@WORK@` and `@SIGNING_CONFIG@`.
///
/// 1. `COSIGN_PASSWORD` is set and empty (M7 fact 5).
/// 2. The argv is `sign-blob --key <key> --signing-config <config>
///    --bundle <bundle> -`, with the paths of the host.
/// 3. The signing config holds the one line `EMPTY_SIGNING_CONFIG`, so
///    sign stays off the public log (M7 fact 3).
/// 4. `CARGO_MANIFEST_DIR`, a variable of the test process, is unset: the
///    child gets an empty environment.
const STAND_IN_COSIGN: &str = r#"#!/bin/sh
[ "${COSIGN_PASSWORD-unset}" = "" ] || exit 3
[ $# -eq 8 ] && [ "$1" = "sign-blob" ] || exit 4
[ "$2" = "--key" ] && [ "$3" = "@CREDENTIALS@/release-signing.key" ] || exit 5
[ -z "${CARGO_MANIFEST_DIR+x}" ] || exit 6
[ "$4" = "--signing-config" ] && [ "$5" = "@WORK@/empty-signing-config.json" ] || exit 7
config=""
IFS= read -r config < "$5"
[ "$config" = '@SIGNING_CONFIG@' ] || exit 7
[ "$6" = "--bundle" ] && [ "$7" = "@WORK@/bundle-48.json" ] || exit 8
[ "$8" = "-" ] || exit 9
while IFS= read -r line; do :; done
printf '%s' '{"mediaType":"application/vnd.dev.sigstore.bundle.v0.3+json","standIn":true}' > "$7"
"#;

/// A stand-in cosign that fails.
const FAILING_COSIGN: &str = "#!/bin/sh\nexit 1\n";

/// The folders of one test, removed at its drop.
pub(crate) struct Host {
    pub(crate) root: PathBuf,
    pub(crate) paths: Paths,
}

impl Drop for Host {
    fn drop(&mut self) {
        let _gone = fs::remove_dir_all(&self.root);
    }
}

static NEXT: std::sync::atomic::AtomicUsize = std::sync::atomic::AtomicUsize::new(0);

impl Host {
    /// A new host with the credentials and the stand-in `cosign`.
    pub(crate) fn new(cosign: &str) -> Self {
        let number = NEXT.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
        let root =
            std::env::temp_dir().join(format!("ci-signer-test-{}-{number}", std::process::id()));
        let _gone = fs::remove_dir_all(&root);
        let credentials = root.join("credentials");
        let work = root.join("work");
        fs::create_dir_all(&credentials).unwrap();
        fs::create_dir_all(&work).unwrap();
        fs::write(credentials.join("release-signing.key"), b"stand-in key\n").unwrap();
        fs::write(
            credentials.join("release-reader.token"),
            format!("{READER}\n"),
        )
        .unwrap();
        fs::write(credentials.join("ci-signer.token"), format!("{TAGGER}\n")).unwrap();
        let program = root.join("cosign");
        let cosign = cosign
            .replace("@CREDENTIALS@", credentials.to_str().unwrap())
            .replace("@WORK@", work.to_str().unwrap())
            .replace("@SIGNING_CONFIG@", crate::cosign::EMPTY_SIGNING_CONFIG);
        fs::write(&program, cosign).unwrap();
        fs::set_permissions(&program, fs::Permissions::from_mode(0o755)).unwrap();
        let paths = Paths {
            journal: root.join("issued.tsv"),
            work,
            credentials,
        };
        Self { root, paths }
    }

    pub(crate) fn config(&self, origin: &str) -> Config {
        let text = format!(
            "owner={OWNER}\nforgejo={origin}\ncosign={}\n",
            self.root.join("cosign").display()
        );
        Config::parse(text.as_bytes()).unwrap()
    }

    pub(crate) fn journal(&self) -> String {
        fs::read_to_string(&self.paths.journal).unwrap_or_default()
    }
}

/// A runtime of real time for the tests that talk to the stand-in.
pub(crate) fn runtime() -> tokio::runtime::Runtime {
    tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .unwrap()
}

/// A runtime of paused time for the tests of a bound. No stand-in Forgejo
/// runs in it: an idle runtime moves the clock, so each wait of a call
/// would pass at once.
pub(crate) fn paused() -> tokio::runtime::Runtime {
    tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .start_paused(true)
        .build()
        .unwrap()
}

/// The eligible run 48 of the recordings, with no read of Forgejo.
fn eligible_48() -> Eligible {
    let pairs = SigningPairs::builtin(OWNER.parse().unwrap()).unwrap();
    let run = recorded("p1-run-push-main.json", running_release);
    let checked = decide_run(&pairs, &key(48), &run).unwrap();
    decide_merge(checked, &recorded("p1-commit-pull-merge-sha.json", |_| {})).unwrap()
}

/// The request line of two tags: one with an asset, one with none.
const REQUEST: &str = "{\"v\":1,\"tags\":[{\"tag\":\"vmm-v0.1.24\",\"assets\":[{\"name\":\"vmm-x86_64-unknown-linux-musl.tar.gz\",\"sha256\":\"0000000000000000000000000000000000000000000000000000000000000000\"}]},{\"tag\":\"handover-v0.1.96\",\"assets\":[]}]}\n";

/// The run of one session, as `session.rs` holds it for its guest tasks.
fn held(eligible: Eligible) -> guest::SessionRun {
    Arc::new(Mutex::new(Some(eligible)))
}

/// Sends `request` on a fresh guest connection of the session of `held`,
/// and gives the answer line.
async fn exchange(signer: &Arc<Signer>, held: &guest::SessionRun, request: &[u8]) -> Value {
    let (ours, theirs) = UnixStream::pair().unwrap();
    let task = tokio::spawn(guest::serve(theirs, Arc::clone(held), Arc::clone(signer)));
    talk(ours, request, task).await
}

/// The guest side of one exchange: `request` on `ours`, then the answer
/// line, after the end of the task `served` of the signer.
async fn talk(mut ours: UnixStream, request: &[u8], served: tokio::task::JoinHandle<()>) -> Value {
    ours.write_all(request).await.unwrap();
    let mut answer = Vec::new();
    ours.read_to_end(&mut answer).await.unwrap();
    served.await.unwrap();
    assert_eq!(answer.last(), Some(&b'\n'), "{answer:?}");
    serde_json::from_slice(&answer).unwrap()
}

#[test]
fn the_eligible_run_gets_its_statement_its_journal_line_and_its_tags() {
    runtime().block_on(async {
        let host = Host::new(STAND_IN_COSIGN);
        let forgejo = Arc::new(Forgejo::with_run(
            48,
            recorded("p1-run-push-main.json", running_release),
        ));
        let origin = forgejo.start().await;
        let signer = Arc::new(Signer::start(host.config(&origin), &host.paths).unwrap());
        // The stand-in cosign requires that this variable of the test
        // process does not reach the child.
        assert!(std::env::var_os("CARGO_MANIFEST_DIR").is_some());

        let eligible = signer.decide(&key(48)).await.unwrap();
        assert_eq!(eligible.commit().as_str(), MERGE_COMMIT);
        let session = held(eligible);
        let answer = exchange(&signer, &session, REQUEST.as_bytes()).await;
        assert_eq!(answer["v"], 1, "{answer}");

        // The statement takes its facts from Forgejo, the tags from the
        // request, sorted by name (FR7, FR25).
        let statement =
            creche_ci_signer::base64_decode(answer["signed"]["statement"].as_str().unwrap())
                .unwrap();
        let statement = Statement::from_canonical(&statement).unwrap();
        assert_eq!(statement.repository().to_string(), REPOSITORY);
        assert_eq!(statement.workflow().as_str(), "release.yml");
        assert_eq!(statement.commit().as_str(), MERGE_COMMIT);
        assert_eq!(statement.run().get(), 48);
        let tags: Vec<&str> = statement
            .tags()
            .items()
            .iter()
            .map(|entry| entry.tag.as_str())
            .collect();
        assert_eq!(tags, ["handover-v0.1.96", "vmm-v0.1.24"]);

        // The journal holds one line of run 48 at the merge commit (FR21).
        let journal = host.journal();
        assert_eq!(journal.lines().count(), 1, "{journal}");
        assert!(
            journal.starts_with(&format!("48\t{MERGE_COMMIT}\t")),
            "{journal}"
        );

        // The two reads used the token of release-reader. Each tag is one
        // POST with the token of the bot ci-signer, at the merge commit,
        // with the one message of the statement (section 4.5 step 7).
        let seen = forgejo.seen.lock().unwrap().clone();
        let reads: Vec<&Seen> = seen.iter().filter(|one| one.method == "GET").collect();
        assert_eq!(reads.len(), 2);
        assert!(
            reads
                .iter()
                .all(|one| one.authorization == format!("token {READER}"))
        );
        let posts = forgejo.posts();
        assert_eq!(posts.len(), 2);
        let mut messages = Vec::new();
        for post in &posts {
            assert_eq!(post.path, format!("/api/v1/repos/{REPOSITORY}/tags"));
            assert_eq!(post.authorization, format!("token {TAGGER}"));
            let body: Value = serde_json::from_slice(&post.body).unwrap();
            assert_eq!(body["target"], MERGE_COMMIT);
            messages.push(body["message"].as_str().unwrap().to_owned());
        }
        assert_eq!(messages[0], messages[1]);
        assert!(messages[0].starts_with("release-statement-1\n"));

        // A second request of the same session has no eligible run left.
        let again = exchange(&signer, &session, REQUEST.as_bytes()).await;
        assert_eq!(
            again,
            serde_json::json!({"v": 1, "refused": "already_issued"})
        );

        // A second open of the run is refused: the journal holds it (FR22).
        let decline = signer.decide(&key(48)).await.unwrap_err();
        assert_eq!(decline.code(), "already_issued");
    });
}

/// Each failed check of section 4.4 on the recorded replies gives no
/// statement: `decide` refuses, and no POST and no journal line follow.
#[test]
fn each_failed_check_gives_no_statement() {
    let cases: Vec<(u64, Reply, Option<Reply>, &str)> = vec![
        // The reply names another repository than the key of the open line.
        (
            48,
            recorded("p1-run-push-main.json", |run| {
                run["status"] = Value::from("running")
            }),
            None,
            "reply_mismatch",
        ),
        // Check 1: the workflow of the run is not the one of the pair.
        (
            48,
            recorded("p1-run-push-main.json", |run| {
                running_release(run);
                run["workflow_id"] = Value::from("probe.yml");
            }),
            None,
            "not_a_pair",
        ),
        // Check 2: a PR, a pull_request_target and a workflow_dispatch.
        (
            70,
            recorded("p1-run-pull-request.json", running_release),
            None,
            "not_push",
        ),
        (
            69,
            recorded("p11-run-prt.json", running_release),
            None,
            "not_push",
        ),
        (
            104,
            recorded("p1-run-workflow-dispatch.json", running_release),
            None,
            "not_push",
        ),
        // Check 3: a branch, a tag, and a tag named main.
        (
            56,
            recorded("p1-run-push-branch.json", running_release),
            None,
            "not_main",
        ),
        (
            65,
            recorded("p1-run-push-tag.json", running_release),
            None,
            "not_main",
        ),
        (
            15,
            recorded("p1-run-push-tag-main.json", running_release),
            None,
            "not_main",
        ),
        // Check 5: the recorded run is over.
        (
            48,
            recorded("p1-run-push-main.json", as_release),
            None,
            "finished",
        ),
        // Check 4: the commit is no PR merge (the recorded 404).
        (
            48,
            recorded("p1-run-push-main.json", running_release),
            Some(recorded("p1-commit-pull-pr-head.json", |_| {})),
            "no_pull",
        ),
        (
            48,
            recorded("p1-run-push-main.json", running_release),
            Some(recorded("p1-commit-pull-merge-sha.json", |pull| {
                pull["merged"] = Value::from(false)
            })),
            "not_merged",
        ),
        (
            48,
            recorded("p1-run-push-main.json", running_release),
            Some(recorded("p1-commit-pull-merge-sha.json", |pull| {
                pull["base"]["ref"] = Value::from("rec-release")
            })),
            "other_base",
        ),
        // A reply that is no run.
        (
            48,
            Reply {
                status: 404,
                body: b"{}".to_vec(),
            },
            None,
            "fault",
        ),
        // A body past the cap: the client stops its read at the cap.
        (
            48,
            Reply {
                status: 200,
                body: vec![b' '; REPLY_MAX_BYTES + 1],
            },
            None,
            "too_large",
        ),
    ];
    runtime().block_on(async {
        for (run, reply, pull, code) in cases {
            let host = Host::new(STAND_IN_COSIGN);
            let mut forgejo = Forgejo::with_run(run, reply);
            if let Some(pull) = pull {
                forgejo.get(&pull_path(), pull);
            }
            let forgejo = Arc::new(forgejo);
            let origin = forgejo.start().await;
            let signer = Signer::start(host.config(&origin), &host.paths).unwrap();
            let decline = signer.decide(&key(run)).await.unwrap_err();
            assert_eq!(decline.code(), code, "run {run}");
            assert!(forgejo.posts().is_empty(), "{code}");
            assert_eq!(host.journal(), "", "{code}");
        }
    });
}

/// A failed sign leaves the run issued: no tag, the answer `fault`, and a
/// second open of the run is refused (fail closed).
#[test]
fn a_failed_sign_keeps_the_run_issued_and_makes_no_tag() {
    runtime().block_on(async {
        let host = Host::new(FAILING_COSIGN);
        let forgejo = Arc::new(Forgejo::with_run(
            48,
            recorded("p1-run-push-main.json", running_release),
        ));
        let origin = forgejo.start().await;
        let signer = Arc::new(Signer::start(host.config(&origin), &host.paths).unwrap());
        let eligible = signer.decide(&key(48)).await.unwrap();
        let answer = exchange(&signer, &held(eligible), REQUEST.as_bytes()).await;
        assert_eq!(answer, serde_json::json!({"v": 1, "refused": "fault"}));
        assert!(forgejo.posts().is_empty());
        assert_eq!(host.journal().lines().count(), 1);
        assert_eq!(
            signer.decide(&key(48)).await.unwrap_err().code(),
            "already_issued"
        );
        // The bundle file is gone.
        assert_eq!(fs::read_dir(&host.paths.work).unwrap().count(), 2);
    });
}

/// A `POST /tags` reply that is not 201 with the name, the commit and the
/// message of the request gives `fault`. The loop stops at the first bad
/// tag, and the run stays issued: the allocator gets no statement for a tag
/// that does not exist or that another user made.
#[test]
fn a_tag_reply_that_is_not_the_request_gives_fault() {
    let cases = [
        // Refused by Forgejo: the tag protection, and the recorded 500 of
        // a message past the limit of Forgejo.
        TagReply::Fixed(Reply {
            status: 422,
            body: b"{}".to_vec(),
        }),
        TagReply::Fixed(recorded("p2-tag-size-b131072.json", |_| {})),
        // 201, but one field is not the one of the request.
        TagReply::Changed(|tag| {
            tag["commit"]["sha"] = Value::from("0123456789abcdef0123456789abcdef01234567");
        }),
        TagReply::Changed(|tag| tag["name"] = Value::from("vmm-v0.1.25")),
        TagReply::Changed(|tag| tag["message"] = Value::from("annotated probe")),
    ];
    runtime().block_on(async {
        for tag_reply in cases {
            let host = Host::new(STAND_IN_COSIGN);
            let mut forgejo =
                Forgejo::with_run(48, recorded("p1-run-push-main.json", running_release));
            forgejo.tag_reply = tag_reply.clone();
            let forgejo = Arc::new(forgejo);
            let origin = forgejo.start().await;
            let signer = Arc::new(Signer::start(host.config(&origin), &host.paths).unwrap());
            let eligible = signer.decide(&key(48)).await.unwrap();

            let answer = exchange(&signer, &held(eligible), REQUEST.as_bytes()).await;
            assert_eq!(
                answer,
                serde_json::json!({"v": 1, "refused": "fault"}),
                "{tag_reply:?}"
            );
            assert_eq!(forgejo.posts().len(), 1, "{tag_reply:?}");
            assert_eq!(host.journal().lines().count(), 1, "{tag_reply:?}");
            assert_eq!(
                signer.decide(&key(48)).await.unwrap_err().code(),
                "already_issued",
                "{tag_reply:?}"
            );
        }
    });
}

/// A request that breaks the grammar or a limit gets its refusal and no
/// statement. The bytes of the guest are hostile.
#[test]
fn a_bad_request_gets_its_refusal_and_no_statement() {
    let cases: [(&[u8], &str); 5] = [
        (b"{\"v\":1,\"tags\":[]}\n", "limits"),
        (b"{\"v\":2,\"tags\":[]}\n", "grammar"),
        (b"{\"v\":1,\"tags\":[],\"run\":48}\n", "grammar"),
        (b"not json\n", "grammar"),
        (b"{\"v\":1,\"tags\":[]}\nmore", "grammar"),
    ];
    runtime().block_on(async {
        for (request, word) in cases {
            let host = Host::new(STAND_IN_COSIGN);
            let forgejo = Arc::new(Forgejo::default());
            let origin = forgejo.start().await;
            let signer = Arc::new(Signer::start(host.config(&origin), &host.paths).unwrap());
            let answer = exchange(&signer, &held(eligible_48()), request).await;
            assert_eq!(
                answer,
                serde_json::json!({"v": 1, "refused": word}),
                "{word}"
            );
            assert_eq!(host.journal(), "");
            assert!(forgejo.seen.lock().unwrap().is_empty());
        }
    });
}

/// The answer of an exchange that found no run left in its session.
fn already_issued() -> Value {
    serde_json::json!({"v": 1, "refused": "already_issued"})
}

/// A stand-in Forgejo with the running run 48, its signer, and the held
/// run of one accepted session of run 48.
async fn session_48() -> (Host, Arc<Forgejo>, Arc<Signer>, guest::SessionRun) {
    let host = Host::new(STAND_IN_COSIGN);
    let forgejo = Arc::new(Forgejo::with_run(
        48,
        recorded("p1-run-push-main.json", running_release),
    ));
    let origin = forgejo.start().await;
    let signer = Arc::new(Signer::start(host.config(&origin), &host.paths).unwrap());
    let session = held(signer.decide(&key(48)).await.unwrap());
    (host, forgejo, signer, session)
}

/// The one statement of run 48: a signed answer, one journal line and the
/// two tag creations.
fn assert_one_statement(host: &Host, forgejo: &Forgejo, answer: &Value) {
    assert!(answer["signed"]["statement"].is_string(), "{answer}");
    let journal = host.journal();
    assert_eq!(journal.lines().count(), 1, "{journal}");
    assert!(
        journal.starts_with(&format!("48\t{MERGE_COMMIT}\t")),
        "{journal}"
    );
    assert_eq!(forgejo.posts().len(), 2);
}

/// A guest that closes with no request, as a guest helper that exits,
/// leaves the run of its session to the next fd. That fd gets the
/// statement and the journal line (FR22: `already_issued` only after the
/// run took its statement).
#[test]
fn a_guest_with_no_request_leaves_the_run_to_the_next_fd() {
    runtime().block_on(async {
        let (host, forgejo, signer, session) = session_48().await;

        let (ours, theirs) = UnixStream::pair().unwrap();
        let served = tokio::spawn(guest::serve(
            theirs,
            Arc::clone(&session),
            Arc::clone(&signer),
        ));
        drop(ours);
        served.await.unwrap();
        assert_eq!(host.journal(), "");

        let answer = exchange(&signer, &session, REQUEST.as_bytes()).await;
        assert_one_statement(&host, &forgejo, &answer);
    });
}

/// A request that breaks the grammar or a limit leaves the run of its
/// session to the next fd. That fd gets the statement and the journal
/// line.
#[test]
fn a_bad_request_leaves_the_run_to_the_next_fd() {
    runtime().block_on(async {
        let (host, forgejo, signer, session) = session_48().await;

        let grammar = exchange(&signer, &session, b"{\"v\":2,\"tags\":[]}\n").await;
        assert_eq!(grammar, serde_json::json!({"v": 1, "refused": "grammar"}));
        let limits = exchange(&signer, &session, b"{\"v\":1,\"tags\":[]}\n").await;
        assert_eq!(limits, serde_json::json!({"v": 1, "refused": "limits"}));
        assert_eq!(host.journal(), "");

        let answer = exchange(&signer, &session, REQUEST.as_bytes()).await;
        assert_one_statement(&host, &forgejo, &answer);
    });
}

/// Two valid requests on two fds of one session, one after the other: the
/// first gets the statement, the second `already_issued`.
#[test]
fn a_second_valid_request_gets_already_issued() {
    runtime().block_on(async {
        let (host, forgejo, signer, session) = session_48().await;
        let first = exchange(&signer, &session, REQUEST.as_bytes()).await;
        let second = exchange(&signer, &session, REQUEST.as_bytes()).await;
        assert_one_statement(&host, &forgejo, &first);
        assert_eq!(second, already_issued());
    });
}

/// The rounds of the test of two requests at the same time.
const RACE_ROUNDS: usize = 8;

/// Two valid requests on two fds of one session, sent at the same time on
/// two worker threads: exactly one gets the statement, the other
/// `already_issued`.
#[test]
fn two_requests_at_once_get_exactly_one_statement() {
    let threads = tokio::runtime::Builder::new_multi_thread()
        .worker_threads(2)
        .enable_all()
        .build()
        .unwrap();
    threads.block_on(async {
        for round in 0..RACE_ROUNDS {
            let (host, forgejo, signer, session) = session_48().await;
            let barrier = Arc::new(tokio::sync::Barrier::new(2));
            let mut guests = Vec::new();
            for _ in 0..2 {
                let (ours, theirs) = UnixStream::pair().unwrap();
                let served = tokio::spawn(guest::serve(
                    theirs,
                    Arc::clone(&session),
                    Arc::clone(&signer),
                ));
                let barrier = Arc::clone(&barrier);
                guests.push(tokio::spawn(async move {
                    barrier.wait().await;
                    talk(ours, REQUEST.as_bytes(), served).await
                }));
            }
            let mut answers = Vec::new();
            for guest in guests {
                answers.push(guest.await.unwrap());
            }

            let signed: Vec<&Value> = answers
                .iter()
                .filter(|answer| answer.get("signed").is_some())
                .collect();
            assert_eq!(signed.len(), 1, "round {round}: {answers:?}");
            assert_one_statement(&host, &forgejo, signed[0]);
            assert_eq!(
                answers.iter().filter(|a| **a == already_issued()).count(),
                1,
                "round {round}: {answers:?}"
            );
        }
    });
}

/// The bound of the request: a guest that sends nothing gets `grammar`
/// after `REQUEST_WAIT`, and no statement.
#[test]
fn a_guest_that_sends_nothing_is_refused_at_the_bound() {
    paused().block_on(async {
        let host = Host::new(STAND_IN_COSIGN);
        let config = host.config("http://127.0.0.1:9");
        let signer = Arc::new(Signer::start(config, &host.paths).unwrap());
        let (mut ours, theirs) = UnixStream::pair().unwrap();
        let started = tokio::time::Instant::now();
        let task = tokio::spawn(guest::serve(
            theirs,
            held(eligible_48()),
            Arc::clone(&signer),
        ));
        let mut answer = Vec::new();
        ours.read_to_end(&mut answer).await.unwrap();
        task.await.unwrap();
        assert!(started.elapsed() >= guest::REQUEST_WAIT);
        assert!(started.elapsed() < guest::REQUEST_WAIT + Duration::from_secs(1));
        assert_eq!(answer, b"{\"v\":1,\"refused\":\"grammar\"}\n");
        assert_eq!(host.journal(), "");
    });
}

/// The cap of the request line: a guest that streams bytes with no LF is
/// cut off after `REQUEST_MAX_BYTES`, long before `REQUEST_WAIT`. So a
/// guest cannot fill the memory of the signer.
#[test]
fn a_guest_that_streams_with_no_lf_is_cut_off_at_the_cap() {
    runtime().block_on(async {
        let host = Host::new(STAND_IN_COSIGN);
        let config = host.config("http://127.0.0.1:9");
        let signer = Arc::new(Signer::start(config, &host.paths).unwrap());
        let (ours, theirs) = UnixStream::pair().unwrap();
        let (_answer, mut writer) = ours.into_split();

        // 64 MiB in chunks of 64 KiB, with no LF. The writer then keeps
        // the connection open, so only the cap can end the read.
        let flood = tokio::spawn(async move {
            let chunk = vec![b'x'; 64 * 1024];
            for _ in 0..1024 {
                if writer.write_all(&chunk).await.is_err() {
                    break;
                }
            }
            std::future::pending::<()>().await;
            drop(writer);
        });
        let served = tokio::time::timeout(
            Duration::from_secs(1),
            guest::serve(theirs, held(eligible_48()), Arc::clone(&signer)),
        )
        .await;
        flood.abort();
        assert!(served.is_ok(), "the read passed the cap");
        assert_eq!(host.journal(), "");
    });
}

/// A start with a missing credential or a bad journal refuses (fail
/// closed).
#[test]
fn a_start_with_no_credential_or_a_bad_journal_is_refused() {
    let host = Host::new(STAND_IN_COSIGN);
    fs::remove_file(host.paths.credentials.join("ci-signer.token")).unwrap();
    let fault = Signer::start(host.config("http://127.0.0.1:9"), &host.paths).unwrap_err();
    assert!(fault.to_string().contains("ci-signer.token"), "{fault}");

    let host = Host::new(STAND_IN_COSIGN);
    fs::write(
        host.paths.credentials.join("release-reader.token"),
        b"short\n",
    )
    .unwrap();
    assert!(Signer::start(host.config("http://127.0.0.1:9"), &host.paths).is_err());

    // The key: an empty file and a file past 4096 bytes are refused by
    // their size, and a file of 4096 bytes passes.
    for (bytes, refused) in [(0, true), (4097, true), (4096, false)] {
        let host = Host::new(STAND_IN_COSIGN);
        fs::write(
            host.paths.credentials.join("release-signing.key"),
            vec![b'k'; bytes],
        )
        .unwrap();
        let started = Signer::start(host.config("http://127.0.0.1:9"), &host.paths);
        assert_eq!(started.is_err(), refused, "{bytes} bytes");
        if let Err(fault) = started {
            assert!(fault.to_string().contains("release-signing.key"), "{fault}");
        }
    }

    let host = Host::new(STAND_IN_COSIGN);
    fs::write(&host.paths.journal, b"48\tnot-a-commit\tx\n").unwrap();
    let fault = Signer::start(host.config("http://127.0.0.1:9"), &host.paths).unwrap_err();
    assert!(fault.to_string().contains("journal"), "{fault}");

    let host = Host::new(STAND_IN_COSIGN);
    fs::write(
        &host.paths.journal,
        format!("48\t{MERGE_COMMIT}\t{}", "0".repeat(64)),
    )
    .unwrap();
    assert!(Signer::start(host.config("http://127.0.0.1:9"), &host.paths).is_err());
}

/// The real cosign, when the machine has one: the bundle of the signer
/// verifies with the public key and no log entry (section 5, M7 fact 1).
#[test]
fn the_bundle_of_the_real_cosign_verifies() {
    let Some(cosign) = [
        "/usr/bin/cosign",
        "/usr/local/bin/cosign",
        "/opt/homebrew/bin/cosign",
    ]
    .into_iter()
    .map(PathBuf::from)
    .find(|path| path.is_file()) else {
        println!("real cosign: not run: no cosign on this machine");
        return;
    };
    let host = Host::new(STAND_IN_COSIGN);
    let made = std::process::Command::new(&cosign)
        .arg("generate-key-pair")
        .env("COSIGN_PASSWORD", "")
        .current_dir(&host.paths.credentials)
        .output()
        .unwrap();
    assert!(made.status.success(), "{made:?}");
    let key = host.paths.credentials.join("cosign.key");
    let signer =
        crate::cosign::Cosign::prepare(cosign.clone(), key, host.paths.work.clone()).unwrap();

    let mut ledger = crate::ledger::Ledger::open(&host.paths.journal).unwrap();
    let request =
        creche_ci_signer::exchange::Request::parse(REQUEST.trim_end().as_bytes()).unwrap();
    let signed_at =
        creche_ci_signer::signed_at::SignedAt::from_unix_seconds(1_800_000_000).unwrap();
    let signable = ledger
        .issue(eligible_48(), request, signed_at)
        .map_err(|_| "issue")
        .unwrap();
    let bundle = runtime()
        .block_on(signer.sign(&signable, 48.try_into().unwrap()))
        .unwrap();

    let statement = host.root.join("statement.json");
    let bundle_path = host.root.join("statement.sigstore.json");
    fs::write(&statement, signable.bytes()).unwrap();
    fs::write(&bundle_path, bundle.bytes()).unwrap();
    let verified = std::process::Command::new(&cosign)
        .arg("verify-blob")
        .arg("--key")
        .arg(host.paths.credentials.join("cosign.pub"))
        .arg("--bundle")
        .arg(&bundle_path)
        .arg("--insecure-ignore-tlog")
        .arg(&statement)
        .output()
        .unwrap();
    assert!(verified.status.success(), "{verified:?}");
    println!(
        "real cosign: the bundle of {} bytes verifies",
        bundle.bytes().len()
    );
}

#[cfg(target_os = "linux")]
mod linux;
